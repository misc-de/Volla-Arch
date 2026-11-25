// SPDX-License-Identifier: MIT
// plinius-datawatch: brings the mobile data connection back when it is up but carries nothing.
//
// After a change of network (a cell or radio technology change, Wi-Fi going away) the mobile
// connection sometimes stays "connected" in NetworkManager with an address and routes, but no
// packet gets through: NetworkManager's connectivity check fails and Phosh shows the
// "no route" icon at the right of the top bar. Switching mobile data off and on again cures
// it; this daemon does the same by itself, so the user does not have to.
//
// For every modem device of NetworkManager:
// - Bad: the device is activated, its IPv4 connectivity is NONE or LIMITED and its IPv6
//   connectivity is not FULL. PORTAL (a captive portal, e.g. "data volume used up") and
//   UNKNOWN (checks switched off in Settings > Privacy, or not done yet) are never bad:
//   reconnecting does not help there.
// - After GRACE_S of bad, NetworkManager is asked to check again (CheckConnectivity), so a
//   stale result never triggers anything. Still bad: the device's active connection is
//   activated again on it (ActivateConnection on an active connection reconnects it), as
//   Phosh's mobile data switch does with off and on.
// - Still bad afterwards: again, each time after a longer wait (DELAYS_S), so a phone without
//   service does not keep the modem busy. FULL connectivity resets the count.
// - A device that leaves the activated state without us (data switched off, flight mode,
//   SIM removed) is left alone and its count reset.
//
// Every reconnect and every recovery goes to the journal at notice level (stored there), with
// the device, its interface and how long it was without internet.
#include <gio/gio.h>
#include <glib-unix.h>
#include <string.h>

#define NM "org.freedesktop.NetworkManager"
#define NM_PATH "/org/freedesktop/NetworkManager"
#define NM_DEVICE NM ".Device"
#define NM_ACTIVE NM ".Connection.Active"
#define DBUS_PROPS "org.freedesktop.DBus.Properties"

#define NM_DEVICE_TYPE_MODEM 8
#define NM_DEVICE_STATE_ACTIVATED 100

/* NMConnectivityState */
#define NM_CONNECTIVITY_UNKNOWN 0
#define NM_CONNECTIVITY_NONE 1
#define NM_CONNECTIVITY_PORTAL 2
#define NM_CONNECTIVITY_LIMITED 3
#define NM_CONNECTIVITY_FULL 4

#define CALL_TIMEOUT_MS 10000
/* One failed check is not yet an outage: a cell change can lose a few packets. The check after
 * the wait is our own (CheckConnectivity), NetworkManager's next one can be up to 300 s away (its
 * interval doubles from 15 s after an activation, seen in the DNS filter's log 2026-11-24) */
#define GRACE_S 20
/* Waits before the 2nd, 3rd, ... reconnect while it does not help */
static const guint DELAYS_S[] = {60, 300, 900, 1800};

typedef struct {
	char *path;
	char *iface;	   /* IpInterface, e.g. ccmni1 */
	guint32 state;	   /* NMDeviceState */
	guint32 c4, c6;	   /* Ip4Connectivity, Ip6Connectivity */
	guint timer;	   /* pending look after a wait */
	guint attempts;	   /* reconnects since the last FULL */
	gboolean busy;	   /* a check or a reconnect is under way */
	gboolean ours;	   /* we reconnect it: until it has been down and up again */
	gboolean down;	   /* ... and it has been down since */
	gboolean bad;	   /* without internet since bad_since */
	gint64 bad_since;  /* monotonic µs */
	gint64 reconnected;/* monotonic µs of the last reconnect, 0 if none */
} Modem;

static struct {
	GMainLoop *loop;
	GDBusConnection *bus;
	GHashTable *modems; /* path -> Modem */
} self;

static const char *connectivity_name(guint32 c)
{
	static const char *const names[] = {"unknown", "none", "portal", "limited", "full"};
	return c < G_N_ELEMENTS(names) ? names[c] : "?";
}

static gboolean is_bad(const Modem *m)
{
	return m->state == NM_DEVICE_STATE_ACTIVATED && (m->c4 == NM_CONNECTIVITY_NONE || m->c4 == NM_CONNECTIVITY_LIMITED) &&
	       m->c6 != NM_CONNECTIVITY_FULL;
}

static gboolean is_good(const Modem *m)
{
	return m->state == NM_DEVICE_STATE_ACTIVATED && (m->c4 == NM_CONNECTIVITY_FULL || m->c6 == NM_CONNECTIVITY_FULL);
}

static guint wait_s(const Modem *m)
{
	if (!m->attempts)
		return GRACE_S;
	return DELAYS_S[MIN(m->attempts - 1, G_N_ELEMENTS(DELAYS_S) - 1)];
}

static void modem_free(gpointer data)
{
	Modem *m = data;

	if (m->timer)
		g_source_remove(m->timer);
	g_free(m->path);
	g_free(m->iface);
	g_free(m);
}

static gint64 seconds_since(gint64 t)
{
	return (g_get_monotonic_time() - t) / G_USEC_PER_SEC;
}

/* ===================================================================== NetworkManager calls */

static GVariant *get_property(const char *path, const char *iface, const char *name)
{
	g_autoptr(GError) error = NULL;
	g_autoptr(GVariant) reply = g_dbus_connection_call_sync(self.bus, NM, path, DBUS_PROPS, "Get",
								g_variant_new("(ss)", iface, name), G_VARIANT_TYPE("(v)"),
								G_DBUS_CALL_FLAGS_NONE, CALL_TIMEOUT_MS, NULL, &error);
	GVariant *v = NULL;

	if (!reply) {
		g_warning("Cannot read %s of %s: %s", name, path, error->message);
		return NULL;
	}
	g_variant_get(reply, "(v)", &v);
	return v;
}

/* State, connectivity and interface from a{sv} (GetAll or PropertiesChanged) */
static void apply_properties(Modem *m, GVariant *props)
{
	g_autofree char *iface = NULL;
	guint32 u;

	if (g_variant_lookup(props, "State", "u", &u))
		m->state = u;
	if (g_variant_lookup(props, "Ip4Connectivity", "u", &u))
		m->c4 = u;
	if (g_variant_lookup(props, "Ip6Connectivity", "u", &u))
		m->c6 = u;
	if (g_variant_lookup(props, "IpInterface", "s", &iface)) {
		g_free(m->iface);
		m->iface = g_steal_pointer(&iface);
	}
}

static void evaluate(Modem *m);

static gboolean on_timer(gpointer data);

static void schedule(Modem *m)
{
	if (!m->timer && !m->busy)
		m->timer = g_timeout_add_seconds(wait_s(m), on_timer, m);
}

static void unschedule(Modem *m)
{
	if (m->timer) {
		g_source_remove(m->timer);
		m->timer = 0;
	}
}

static void on_activated(GObject *source, GAsyncResult *res, gpointer data)
{
	g_autofree char *path = data;
	g_autoptr(GError) error = NULL;
	g_autoptr(GVariant) reply = g_dbus_connection_call_finish(self.bus, res, &error);
	Modem *m = g_hash_table_lookup(self.modems, path);

	if (!reply)
		g_warning("%s: reconnecting failed: %s", path, error->message);
	if (!m)
		return;
	m->busy = FALSE;
	if (!reply)
		m->ours = m->down = FALSE;
	evaluate(m);
}

/* Activate the device's active connection again on it: NetworkManager takes it down and up */
static void reconnect(Modem *m)
{
	g_autoptr(GVariant) active = get_property(m->path, NM_DEVICE, "ActiveConnection");
	g_autoptr(GVariant) conn = NULL;
	g_autoptr(GVariant) id = NULL;
	const char *active_path = active ? g_variant_get_string(active, NULL) : "/";

	/* Counted also when nothing could be done, so the waits grow all the same */
	m->attempts++;
	if (!strcmp(active_path, "/")) {
		g_message("%s: no active connection to reconnect", m->path);
		return;
	}
	conn = get_property(active_path, NM_ACTIVE, "Connection");
	if (!conn)
		return;
	id = get_property(active_path, NM_ACTIVE, "Id");
	m->busy = TRUE;
	m->ours = TRUE;
	m->down = FALSE;
	m->reconnected = g_get_monotonic_time();
	g_message("%s (%s): no internet for %" G_GINT64_FORMAT " s (IPv4 %s, IPv6 %s), reconnecting \"%s\" (attempt %u)",
		  m->path, m->iface ? m->iface : "-", seconds_since(m->bad_since), connectivity_name(m->c4),
		  connectivity_name(m->c6), id ? g_variant_get_string(id, NULL) : "?", m->attempts);
	g_dbus_connection_call(self.bus, NM, NM_PATH, NM, "ActivateConnection",
			       g_variant_new("(ooo)", g_variant_get_string(conn, NULL), m->path, "/"),
			       G_VARIANT_TYPE("(o)"), G_DBUS_CALL_FLAGS_NONE, CALL_TIMEOUT_MS * 3, NULL, on_activated,
			       g_strdup(m->path));
}

/* NetworkManager checked again: fresh connectivity of the device, then decide */
static void on_checked(GObject *source, GAsyncResult *res, gpointer data)
{
	g_autofree char *path = data;
	g_autoptr(GError) error = NULL;
	g_autoptr(GVariant) reply = g_dbus_connection_call_finish(self.bus, res, &error);
	g_autoptr(GVariant) c4 = NULL;
	g_autoptr(GVariant) c6 = NULL;
	Modem *m = g_hash_table_lookup(self.modems, path);

	if (!m)
		return;
	m->busy = FALSE;
	if (!reply) {
		/* Checks switched off (or NetworkManager busy): its last result stands */
		g_warning("%s: connectivity check failed: %s", path, error->message);
	} else {
		c4 = get_property(path, NM_DEVICE, "Ip4Connectivity");
		c6 = get_property(path, NM_DEVICE, "Ip6Connectivity");
		if (c4)
			m->c4 = g_variant_get_uint32(c4);
		if (c6)
			m->c6 = g_variant_get_uint32(c6);
	}
	if (is_bad(m))
		reconnect(m);
	evaluate(m);
}

static gboolean on_timer(gpointer data)
{
	Modem *m = data;

	/* A wait only exists while the device is bad: every change goes through evaluate() */
	m->timer = 0;
	m->busy = TRUE;
	g_dbus_connection_call(self.bus, NM, NM_PATH, NM, "CheckConnectivity", NULL, G_VARIANT_TYPE("(u)"),
			       G_DBUS_CALL_FLAGS_NONE, CALL_TIMEOUT_MS * 3, NULL, on_checked, g_strdup(m->path));
	return G_SOURCE_REMOVE;
}

/* After every change of the device: wait, act, or forget */
static void evaluate(Modem *m)
{
	/* Our reconnect is over once the device has been down and is activated again */
	if (m->ours && m->state != NM_DEVICE_STATE_ACTIVATED)
		m->down = TRUE;
	else if (m->ours && m->down)
		m->ours = m->down = FALSE;

	if (is_good(m)) {
		unschedule(m);
		if (m->attempts)
			g_message("%s (%s): internet again, %" G_GINT64_FORMAT " s after reconnect %u", m->path,
				  m->iface ? m->iface : "-", seconds_since(m->reconnected), m->attempts);
		/* Shorter is NetworkManager's first check after an activation, not worth a line */
		else if (m->bad && seconds_since(m->bad_since) >= GRACE_S)
			g_message("%s (%s): internet again by itself after %" G_GINT64_FORMAT " s", m->path,
				  m->iface ? m->iface : "-", seconds_since(m->bad_since));
		m->attempts = 0;
		m->bad = m->ours = m->down = FALSE;
		return;
	}
	if (is_bad(m)) {
		if (!m->bad) {
			m->bad = TRUE;
			m->bad_since = g_get_monotonic_time();
		}
		schedule(m);
		return;
	}
	/* Neither: activating, unknown or portal connectivity, or down */
	unschedule(m);
	if (m->state == NM_DEVICE_STATE_ACTIVATED)
		return;
	if (!m->ours && !m->busy) {
		/* Down without us: the user's or the system's choice, start afresh next time */
		m->attempts = 0;
		m->bad = FALSE;
	}
}

/* ===================================================================== devices */

static void add_device(const char *path)
{
	g_autoptr(GError) error = NULL;
	g_autoptr(GVariant) reply = NULL;
	g_autoptr(GVariant) props = NULL;
	guint32 type = 0;
	Modem *m;

	if (g_hash_table_contains(self.modems, path))
		return;
	reply = g_dbus_connection_call_sync(self.bus, NM, path, DBUS_PROPS, "GetAll", g_variant_new("(s)", NM_DEVICE),
					    G_VARIANT_TYPE("(a{sv})"), G_DBUS_CALL_FLAGS_NONE, CALL_TIMEOUT_MS, NULL,
					    &error);
	if (!reply) {
		g_warning("Cannot read device %s: %s", path, error->message);
		return;
	}
	props = g_variant_get_child_value(reply, 0);
	if (!g_variant_lookup(props, "DeviceType", "u", &type) || type != NM_DEVICE_TYPE_MODEM)
		return;
	m = g_new0(Modem, 1);
	m->path = g_strdup(path);
	apply_properties(m, props);
	g_hash_table_insert(self.modems, m->path, m);
	g_message("Watching modem %s (%s): state %u, IPv4 %s, IPv6 %s", m->path, m->iface ? m->iface : "-", m->state,
		  connectivity_name(m->c4), connectivity_name(m->c6));
	evaluate(m);
}

static void on_nm_signal(GDBusConnection *bus, const char *sender, const char *path, const char *iface,
			 const char *signal, GVariant *params, gpointer user_data)
{
	const char *dev;

	if (!g_variant_is_of_type(params, G_VARIANT_TYPE("(o)")))
		return;
	g_variant_get(params, "(&o)", &dev);
	if (!strcmp(signal, "DeviceAdded"))
		add_device(dev);
	else if (!strcmp(signal, "DeviceRemoved") && g_hash_table_remove(self.modems, dev))
		g_message("Modem %s gone", dev);
}

static void on_properties_changed(GDBusConnection *bus, const char *sender, const char *path, const char *iface,
				  const char *signal, GVariant *params, gpointer user_data)
{
	g_autoptr(GVariant) changed = NULL;
	const char *which;
	Modem *m = g_hash_table_lookup(self.modems, path);

	if (!m || !g_variant_is_of_type(params, G_VARIANT_TYPE("(sa{sv}as)")))
		return;
	g_variant_get_child(params, 0, "&s", &which);
	if (strcmp(which, NM_DEVICE))
		return;
	changed = g_variant_get_child_value(params, 1);
	apply_properties(m, changed);
	evaluate(m);
}

static void on_nm_appeared(GDBusConnection *bus, const char *name, const char *owner, gpointer user_data)
{
	g_autoptr(GVariant) devices = get_property(NM_PATH, NM, "Devices");
	GVariantIter it;
	const char *dev;

	if (!devices)
		return;
	g_variant_iter_init(&it, devices);
	while (g_variant_iter_next(&it, "&o", &dev))
		add_device(dev);
}

static void on_nm_vanished(GDBusConnection *bus, const char *name, gpointer user_data)
{
	/* A restarted NetworkManager has new device paths; the old ones mean nothing any more */
	g_hash_table_remove_all(self.modems);
}

static gboolean on_quit(gpointer data)
{
	g_main_loop_quit(self.loop);
	return G_SOURCE_REMOVE;
}

int main(int argc, char **argv)
{
	g_autoptr(GError) error = NULL;

	self.bus = g_bus_get_sync(G_BUS_TYPE_SYSTEM, NULL, &error);
	if (!self.bus) {
		g_printerr("plinius-datawatch: no system bus: %s\n", error->message);
		return 1;
	}
	self.modems = g_hash_table_new_full(g_str_hash, g_str_equal, NULL, modem_free);
	self.loop = g_main_loop_new(NULL, FALSE);
	g_dbus_connection_signal_subscribe(self.bus, NM, NM, NULL, NM_PATH, NULL, G_DBUS_SIGNAL_FLAGS_NONE, on_nm_signal,
					   NULL, NULL);
	g_dbus_connection_signal_subscribe(self.bus, NM, DBUS_PROPS, "PropertiesChanged", NULL, NM_DEVICE,
					   G_DBUS_SIGNAL_FLAGS_NONE, on_properties_changed, NULL, NULL);
	g_bus_watch_name_on_connection(self.bus, NM, G_BUS_NAME_WATCHER_FLAGS_NONE, on_nm_appeared, on_nm_vanished, NULL,
				       NULL);
	g_unix_signal_add(SIGTERM, on_quit, NULL);
	g_unix_signal_add(SIGINT, on_quit, NULL);
	g_main_loop_run(self.loop);
	g_hash_table_destroy(self.modems);
	return 0;
}
