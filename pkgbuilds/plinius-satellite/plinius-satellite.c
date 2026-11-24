// SPDX-License-Identifier: MIT
// plinius-satellite: tells when the phone is connected through a satellite instead of a cell
// tower, and treats that connection with care.
//
// Satellite services such as Starlink Direct to Cell are plain LTE seen from the phone: the
// satellite is a cell of a partner network, the modem reports nothing special. Android's
// satellite mode works the same way: it compares the network the phone is registered on with a
// list of satellite networks (NtnCapabilityResolver, carrier config
// carrier_supported_satellite_services_per_provider_bundle). This does the same with oFono:
//
// 1. Detection: the PLMN (MCC + MNC) and the name of the network each modem is registered on,
//    compared with the providers in /usr/share/plinius-satellite/providers.conf and
//    /etc/plinius-satellite/providers.d/*.conf (PLMN first, then the network name as a pattern).
// 2. State on the system bus: org.plinius.Satellite (Active, Network, Provider, Plmn, Services,
//    Modem). Phosh shows a satellite instead of the signal bars, plinius-satellite-notify tells
//    the user what works and what to do.
// 3. Policy while connected (/etc/plinius-satellite/satellite.conf):
//    - Metered: the mobile connection becomes metered, only in the active connection (NetworkManager
//      Reapply), the saved profile stays as it is. Apps that care (updates, sync, backups) wait.
//    - DataRoaming: a satellite of the user's own operator often shows as roaming. If the provider
//      offers data there, roaming data is allowed while the satellite lasts and switched off again
//      afterwards (as Android's KEY_SATELLITE_IGNORE_DATA_ROAMING_SETTING_BOOL). A state file
//      remembers this, so a crash or a reboot in between cannot leave roaming on.
//
// 4. Scan (method, and plinius-satellite --scan), Assess (networks found by others, e.g. GNOME Settings): the networks around with a forecast whether the
//    phone could register there (home, satellite, national, roaming). Needs useNetworkScan=true in
//    /etc/ofono/binder.conf, else the RIL's old getAvailableNetworks finds hardly anything.
//
// A satellite connection comes and goes as satellites pass. It stays "active" for a while
// (Hysteresis) when the registration is lost, unless the modem is on a cell tower again.
#include <gio/gio.h>
#include <glib-unix.h>
#include <glib/gstdio.h>
#include <string.h>

#define BUS_NAME "org.plinius.Satellite"
#define OBJECT_PATH "/org/plinius/Satellite"
#define INTERFACE "org.plinius.Satellite"

#define OFONO "org.ofono"
#define NM "org.freedesktop.NetworkManager"
#define NM_PATH "/org/freedesktop/NetworkManager"
#define NM_DEVICE NM ".Device"
#define NM_DEVICE_STATE_ACTIVATED 100
#define NM_METERED_YES 1

#define PROVIDERS_FILE "/usr/share/plinius-satellite/providers.conf"
#define PROVIDERS_DIR "/etc/plinius-satellite/providers.d"
#define POLICY_FILE "/etc/plinius-satellite/satellite.conf"
/* StateDirectory= of the service; the modem whose roaming we switched on */
#define ROAMING_STATE "/var/lib/plinius-satellite/roaming"

#define CALL_TIMEOUT_MS 10000
/* oFono sends several changes at once (name, MCC, MNC, status), look once they are all there */
#define SETTLE_MS 500

typedef struct {
	char *id;	    /* group name in the providers file */
	char *name;	    /* shown to the user */
	char **plmns;	    /* MCC + MNC, e.g. 310830 */
	char **patterns;    /* network names, casefolded globs */
	char **services;    /* sms, mms, data, voice, emergency */
} Provider;

typedef struct {
	char *path;
	gboolean netreg;   /* has org.ofono.NetworkRegistration */
	gboolean connman;  /* has org.ofono.ConnectionManager */
	char *status;	   /* registered, roaming, searching, ... */
	char *mcc, *mnc, *name;
	gboolean roaming_allowed;
} Modem;

static struct {
	GMainLoop *loop;
	GDBusConnection *bus;
	GPtrArray *providers;
	GHashTable *modems; /* path -> Modem */
	gboolean ofono_up;

	/* Policy */
	gboolean policy_metered;
	gboolean policy_roaming;
	guint hysteresis_s;

	/* What is published */
	gboolean active;
	char *modem;	   /* object path, "/" if none */
	char *network;	   /* name the network broadcasts */
	char *plmn;
	const Provider *provider;

	/* What we changed and must undo */
	char *roaming_held;	 /* modem whose RoamingAllowed we switched on */
	char *metered_device;	 /* NetworkManager device we made metered */
	gint32 metered_before;	 /* its connection.metered before */

	guint settle_id;
	guint leave_id;
} self;

/* ---- Providers and policy ---- */

static void provider_free(gpointer data)
{
	Provider *p = data;

	g_free(p->id);
	g_free(p->name);
	g_strfreev(p->plmns);
	g_strfreev(p->patterns);
	g_strfreev(p->services);
	g_free(p);
}

static char **casefold_list(char **list)
{
	for (char **s = list; s && *s; s++) {
		char *folded = g_utf8_casefold(*s, -1);

		g_free(*s);
		*s = folded;
	}
	return list;
}

static void load_providers_file(const char *path)
{
	g_autoptr(GKeyFile) file = g_key_file_new();
	g_autoptr(GError) error = NULL;
	g_auto(GStrv) groups = NULL;

	if (!g_key_file_load_from_file(file, path, G_KEY_FILE_NONE, &error)) {
		if (!g_error_matches(error, G_FILE_ERROR, G_FILE_ERROR_NOENT))
			g_warning("%s: %s", path, error->message);
		return;
	}
	groups = g_key_file_get_groups(file, NULL);
	for (char **g = groups; *g; g++) {
		Provider *p = g_new0(Provider, 1);

		/* A later file with the same group replaces the entry, e.g. to correct it locally */
		for (guint i = 0; i < self.providers->len; i++)
			if (g_str_equal(((Provider *)self.providers->pdata[i])->id, *g)) {
				g_ptr_array_remove_index(self.providers, i);
				break;
			}
		p->id = g_strdup(*g);
		p->name = g_key_file_get_string(file, *g, "Name", NULL);
		if (!p->name)
			p->name = g_strdup(*g);
		p->plmns = g_key_file_get_string_list(file, *g, "Plmn", NULL, NULL);
		p->patterns = casefold_list(g_key_file_get_string_list(file, *g, "NetworkName", NULL, NULL));
		p->services = g_key_file_get_string_list(file, *g, "Services", NULL, NULL);
		if (!p->services)
			p->services = g_strdupv((char *[]){"sms", NULL});
		/* Disabled=true in /etc switches off a shipped entry */
		if (g_key_file_get_boolean(file, *g, "Disabled", NULL) || (!p->plmns && !p->patterns)) {
			provider_free(p);
			continue;
		}
		g_ptr_array_add(self.providers, p);
	}
}

static void load_providers(void)
{
	g_autoptr(GDir) dir = NULL;
	g_autoptr(GPtrArray) names = g_ptr_array_new_with_free_func(g_free);
	const char *name;

	self.providers = g_ptr_array_new_with_free_func(provider_free);
	load_providers_file(PROVIDERS_FILE);
	dir = g_dir_open(PROVIDERS_DIR, 0, NULL);
	while (dir && (name = g_dir_read_name(dir)))
		if (g_str_has_suffix(name, ".conf"))
			g_ptr_array_add(names, g_build_filename(PROVIDERS_DIR, name, NULL));
	g_ptr_array_sort_values(names, (GCompareFunc)g_strcmp0);
	for (guint i = 0; i < names->len; i++)
		load_providers_file(names->pdata[i]);
	g_message("%u satellite providers known", self.providers->len);
}

static void load_policy(void)
{
	g_autoptr(GKeyFile) file = g_key_file_new();
	g_autoptr(GError) error = NULL;

	self.policy_metered = TRUE;
	self.policy_roaming = TRUE;
	self.hysteresis_s = 60;
	if (!g_key_file_load_from_file(file, POLICY_FILE, G_KEY_FILE_NONE, &error))
		return;
	if (g_key_file_has_key(file, "Policy", "Metered", NULL))
		self.policy_metered = g_key_file_get_boolean(file, "Policy", "Metered", NULL);
	if (g_key_file_has_key(file, "Policy", "DataRoaming", NULL))
		self.policy_roaming = g_key_file_get_boolean(file, "Policy", "DataRoaming", NULL);
	if (g_key_file_has_key(file, "Policy", "Hysteresis", NULL))
		self.hysteresis_s = CLAMP(g_key_file_get_integer(file, "Policy", "Hysteresis", NULL), 0, 600);
}

static gboolean has_service(const Provider *p, const char *service)
{
	return p && g_strv_contains((const char *const *)p->services, service);
}

/* The provider of a network, PLMN before name */
static const Provider *match_provider(const char *plmn, const char *name)
{
	g_autofree char *folded = name && *name ? g_utf8_casefold(name, -1) : NULL;

	for (guint i = 0; plmn && *plmn && i < self.providers->len; i++) {
		const Provider *p = self.providers->pdata[i];

		if (p->plmns && g_strv_contains((const char *const *)p->plmns, plmn))
			return p;
	}
	for (guint i = 0; folded && i < self.providers->len; i++) {
		const Provider *p = self.providers->pdata[i];

		for (char **pat = p->patterns; pat && *pat; pat++)
			if (g_pattern_match_simple(*pat, folded))
				return p;
	}
	return NULL;
}

/* ---- D-Bus helpers ---- */

static GVariant *call(const char *dest, const char *path, const char *iface, const char *method, GVariant *args,
		      const char *reply_type, GError **error)
{
	return g_dbus_connection_call_sync(self.bus, dest, path, iface, method, args,
					   reply_type ? G_VARIANT_TYPE(reply_type) : NULL, G_DBUS_CALL_FLAGS_NONE,
					   CALL_TIMEOUT_MS, NULL, error);
}

static GVariant *get_property(const char *dest, const char *path, const char *iface, const char *property)
{
	g_autoptr(GVariant) reply = call(dest, path, "org.freedesktop.DBus.Properties", "Get",
					 g_variant_new("(ss)", iface, property), "(v)", NULL);
	GVariant *value = NULL;

	if (reply)
		g_variant_get(reply, "(v)", &value);
	return value;
}

/* ---- Modems ---- */

static void modem_free(gpointer data)
{
	Modem *m = data;

	g_free(m->path);
	g_free(m->status);
	g_free(m->mcc);
	g_free(m->mnc);
	g_free(m->name);
	g_free(m);
}

/* One property of NetworkRegistration or ConnectionManager */
static void modem_set(Modem *m, const char *iface, const char *key, GVariant *value)
{
	if (g_str_equal(iface, "org.ofono.ConnectionManager")) {
		if (g_str_equal(key, "RoamingAllowed") && g_variant_is_of_type(value, G_VARIANT_TYPE_BOOLEAN)) {
			m->roaming_allowed = g_variant_get_boolean(value);
			/* Switched off by the user meanwhile: nothing left for us to undo */
			if (!m->roaming_allowed && !g_strcmp0(self.roaming_held, m->path)) {
				g_clear_pointer(&self.roaming_held, g_free);
				g_unlink(ROAMING_STATE);
			}
		}
		return;
	}
	/* Other interfaces (SimManager: the SIM's home network) share key names, not meanings */
	if (!g_str_equal(iface, "org.ofono.NetworkRegistration") || !g_variant_is_of_type(value, G_VARIANT_TYPE_STRING))
		return;
	if (g_str_equal(key, "Status")) {
		g_free(m->status);
		m->status = g_variant_dup_string(value, NULL);
	} else if (g_str_equal(key, "MobileCountryCode")) {
		g_free(m->mcc);
		m->mcc = g_variant_dup_string(value, NULL);
	} else if (g_str_equal(key, "MobileNetworkCode")) {
		g_free(m->mnc);
		m->mnc = g_variant_dup_string(value, NULL);
	} else if (g_str_equal(key, "Name")) {
		g_free(m->name);
		m->name = g_variant_dup_string(value, NULL);
	}
}

static void modem_read(Modem *m, const char *iface)
{
	g_autoptr(GVariant) reply = call(OFONO, m->path, iface, "GetProperties", NULL, "(a{sv})", NULL);
	g_autoptr(GVariantIter) iter = NULL;
	const char *key;
	GVariant *value;

	if (!reply)
		return;
	g_variant_get(reply, "(a{sv})", &iter);
	while (g_variant_iter_loop(iter, "{&sv}", &key, &value))
		modem_set(m, iface, key, value);
}

/* The interfaces a modem has now; read the new ones, forget the ones that went */
static void modem_interfaces(Modem *m, GVariant *interfaces)
{
	g_autofree const char **list = g_variant_get_strv(interfaces, NULL);
	gboolean netreg = g_strv_contains(list, "org.ofono.NetworkRegistration");
	gboolean connman = g_strv_contains(list, "org.ofono.ConnectionManager");

	if (netreg && !m->netreg)
		modem_read(m, "org.ofono.NetworkRegistration");
	if (!netreg) {
		g_clear_pointer(&m->status, g_free);
		g_clear_pointer(&m->mcc, g_free);
		g_clear_pointer(&m->mnc, g_free);
		g_clear_pointer(&m->name, g_free);
	}
	if (connman && !m->connman)
		modem_read(m, "org.ofono.ConnectionManager");
	m->netreg = netreg;
	m->connman = connman;
}

static void modem_add(const char *path, GVariant *props)
{
	Modem *m = g_new0(Modem, 1);
	g_autoptr(GVariant) interfaces = g_variant_lookup_value(props, "Interfaces", G_VARIANT_TYPE_STRING_ARRAY);

	m->path = g_strdup(path);
	g_hash_table_replace(self.modems, m->path, m);
	if (interfaces)
		modem_interfaces(m, interfaces);
}

static void read_modems(void)
{
	g_autoptr(GVariant) reply = call(OFONO, "/", "org.ofono.Manager", "GetModems", NULL, "(a(oa{sv}))", NULL);
	g_autoptr(GVariantIter) iter = NULL;
	const char *path;
	GVariant *props;

	g_hash_table_remove_all(self.modems);
	if (!reply)
		return;
	g_variant_get(reply, "(a(oa{sv}))", &iter);
	while (g_variant_iter_loop(iter, "(&o@a{sv})", &path, &props))
		modem_add(path, props);
}

static gboolean registered(const Modem *m)
{
	return !g_strcmp0(m->status, "registered") || !g_strcmp0(m->status, "roaming");
}

static char *modem_plmn(const Modem *m)
{
	return m->mcc && m->mnc ? g_strconcat(m->mcc, m->mnc, NULL) : NULL;
}

/* ---- Policy: roaming ---- */

static gboolean set_roaming(const char *modem, gboolean allowed)
{
	g_autoptr(GError) error = NULL;
	g_autoptr(GVariant) reply = call(OFONO, modem, "org.ofono.ConnectionManager", "SetProperty",
					 g_variant_new("(sv)", "RoamingAllowed", g_variant_new_boolean(allowed)), NULL,
					 &error);

	if (!reply) {
		g_warning("%s: cannot %s roaming data: %s", modem, allowed ? "allow" : "stop", error->message);
		return FALSE;
	}
	return TRUE;
}

static void sync_roaming(void)
{
	Modem *m = self.active ? g_hash_table_lookup(self.modems, self.modem) : NULL;
	gboolean want = m && self.policy_roaming && has_service(self.provider, "data") && m->connman &&
			!g_strcmp0(m->status, "roaming");

	/* Undo on another modem or when the satellite is gone */
	if (self.roaming_held && (!want || g_strcmp0(self.roaming_held, m->path))) {
		Modem *held = g_hash_table_lookup(self.modems, self.roaming_held);

		/* A modem that went away is undone once it is back (state file) */
		if (held && held->connman) {
			g_message("%s: roaming data off again", self.roaming_held);
			if (set_roaming(self.roaming_held, FALSE)) {
				g_clear_pointer(&self.roaming_held, g_free);
				g_unlink(ROAMING_STATE);
			}
		}
	}
	if (want && !self.roaming_held && !m->roaming_allowed) {
		/* First the state file, so that it is undone even if we die right after */
		g_autoptr(GError) error = NULL;

		if (!g_file_set_contents(ROAMING_STATE, m->path, -1, &error)) {
			g_warning("Not allowing roaming data, no state file: %s", error->message);
			return;
		}
		g_message("%s: roaming data allowed for %s", m->path, self.provider->name);
		if (set_roaming(m->path, TRUE))
			self.roaming_held = g_strdup(m->path);
		else
			g_unlink(ROAMING_STATE);
	}
}

/* ---- Policy: metered ---- */

/* NetworkManager's device of an oFono modem: oFono2MM names it after the modem's path */
static char *nm_device_for(const char *modem)
{
	g_autoptr(GVariant) reply = call(NM, NM_PATH, NM, "GetDevices", NULL, "(ao)", NULL);
	g_autoptr(GVariantIter) iter = NULL;
	const char *dev;

	if (!reply)
		return NULL;
	g_variant_get(reply, "(ao)", &iter);
	while (g_variant_iter_next(iter, "&o", &dev)) {
		g_autoptr(GVariant) name = get_property(NM, dev, NM_DEVICE, "Interface");

		if (name && g_str_equal(g_variant_get_string(name, NULL), modem))
			return g_strdup(dev);
	}
	return NULL;
}

/* Set connection.metered in the device's active connection only; returns the value before or -1 */
static gint32 reapply_metered(const char *device, gint32 metered)
{
	g_autoptr(GError) error = NULL;
	g_autoptr(GVariant) state = get_property(NM, device, NM_DEVICE, "State");
	g_autoptr(GVariant) applied = NULL, settings = NULL, conn = NULL, reply = NULL;
	GVariantBuilder builder, conn_builder;
	GVariantIter iter;
	const char *name;
	GVariant *value;
	guint64 version;
	gint32 before = 0;

	if (!state || g_variant_get_uint32(state) != NM_DEVICE_STATE_ACTIVATED)
		return -1;
	applied = call(NM, device, NM_DEVICE, "GetAppliedConnection", g_variant_new("(u)", 0), "(a{sa{sv}}t)", &error);
	if (!applied) {
		g_warning("%s: no applied connection: %s", device, error->message);
		return -1;
	}
	g_variant_get(applied, "(@a{sa{sv}}t)", &settings, &version);

	/* The same settings, only connection.metered changed */
	g_variant_builder_init(&builder, G_VARIANT_TYPE("a{sa{sv}}"));
	g_variant_iter_init(&iter, settings);
	while (g_variant_iter_next(&iter, "{&s@a{sv}}", &name, &value)) {
		if (g_str_equal(name, "connection")) {
			GVariantIter props;
			const char *key;
			GVariant *v;

			g_variant_builder_init(&conn_builder, G_VARIANT_TYPE("a{sv}"));
			g_variant_iter_init(&props, value);
			while (g_variant_iter_next(&props, "{&sv}", &key, &v)) {
				if (g_str_equal(key, "metered"))
					before = g_variant_get_int32(v);
				else
					g_variant_builder_add(&conn_builder, "{sv}", key, v);
				g_variant_unref(v);
			}
			g_variant_builder_add(&conn_builder, "{sv}", "metered", g_variant_new_int32(metered));
			g_variant_builder_add(&builder, "{s@a{sv}}", name, g_variant_builder_end(&conn_builder));
		} else {
			g_variant_builder_add(&builder, "{s@a{sv}}", name, value);
		}
		g_variant_unref(value);
	}
	reply = call(NM, device, NM_DEVICE, "Reapply",
		     g_variant_new("(@a{sa{sv}}tu)", g_variant_builder_end(&builder), version, 0), NULL, &error);
	if (!reply) {
		g_warning("%s: cannot set metered to %d: %s", device, metered, error->message);
		return -1;
	}
	return before;
}

static void sync_metered(void)
{
	g_autofree char *device = NULL;

	if (self.active && self.policy_metered)
		device = nm_device_for(self.modem);
	if (self.metered_device && g_strcmp0(device, self.metered_device)) {
		/* -1: the connection is gone, and with it our change */
		if (reapply_metered(self.metered_device, self.metered_before) >= 0)
			g_message("%s: metered back to %d", self.metered_device, self.metered_before);
		g_clear_pointer(&self.metered_device, g_free);
	}
	if (device && !self.metered_device) {
		gint32 before = reapply_metered(device, NM_METERED_YES);

		if (before >= 0) {
			g_message("%s: metered while on the satellite", device);
			self.metered_before = before;
			self.metered_device = g_steal_pointer(&device);
		}
	}
}

/* ---- Evaluation and publishing ---- */

static GVariant *services_variant(void)
{
	return g_variant_new_strv(self.provider ? (const char *const *)self.provider->services : NULL,
				  self.provider ? -1 : 0);
}

static void publish(void)
{
	GVariantBuilder changed;

	g_variant_builder_init(&changed, G_VARIANT_TYPE("a{sv}"));
	g_variant_builder_add(&changed, "{sv}", "Active", g_variant_new_boolean(self.active));
	g_variant_builder_add(&changed, "{sv}", "Modem", g_variant_new_object_path(self.modem));
	g_variant_builder_add(&changed, "{sv}", "Network", g_variant_new_string(self.network ? self.network : ""));
	g_variant_builder_add(&changed, "{sv}", "Plmn", g_variant_new_string(self.plmn ? self.plmn : ""));
	g_variant_builder_add(&changed, "{sv}", "Provider",
			      g_variant_new_string(self.provider ? self.provider->name : ""));
	g_variant_builder_add(&changed, "{sv}", "Services", services_variant());
	g_dbus_connection_emit_signal(self.bus, NULL, OBJECT_PATH, "org.freedesktop.DBus.Properties",
				      "PropertiesChanged", g_variant_new("(sa{sv}as)", INTERFACE, &changed, NULL),
				      NULL);
}

static void set_state(const Modem *m, const Provider *p)
{
	g_autofree char *plmn = m ? modem_plmn(m) : NULL;
	const char *modem = m ? m->path : "/";
	const char *network = m ? m->name : NULL;

	if (self.active == (p != NULL) && self.provider == p && !g_strcmp0(self.modem, modem) &&
	    !g_strcmp0(self.network, network) && !g_strcmp0(self.plmn, plmn))
		return;
	if (p && !self.active)
		g_message("On the satellite: %s (%s, %s)", p->name, network ? network : "", plmn ? plmn : "");
	else if (!p && self.active)
		g_message("Off the satellite");
	self.active = p != NULL;
	self.provider = p;
	g_free(self.modem);
	self.modem = g_strdup(modem);
	g_free(self.network);
	self.network = g_strdup(network);
	g_free(self.plmn);
	self.plmn = g_steal_pointer(&plmn);
	publish();
}

static void apply_policy(void)
{
	sync_roaming();
	sync_metered();
}

static gboolean on_leave(gpointer user_data)
{
	self.leave_id = 0;
	set_state(NULL, NULL);
	apply_policy();
	return G_SOURCE_REMOVE;
}

static void evaluate(void)
{
	Modem *m, *found = NULL;
	const Provider *provider = NULL;
	g_autoptr(GList) paths = g_hash_table_get_keys(self.modems);

	/* In path order, so that with two SIMs the choice is always the same */
	paths = g_list_sort(paths, (GCompareFunc)g_strcmp0);
	for (GList *l = paths; l; l = l->next) {
		g_autofree char *plmn = NULL;
		const Provider *p;

		m = g_hash_table_lookup(self.modems, l->data);
		if (!registered(m))
			continue;
		plmn = modem_plmn(m);
		p = match_provider(plmn, m->name);
		if (p && !found) {
			found = m;
			provider = p;
		}
	}

	if (found) {
		g_clear_handle_id(&self.leave_id, g_source_remove);
		set_state(found, provider);
	} else if (self.active) {
		m = g_hash_table_lookup(self.modems, self.modem);
		/* Back on a tower with the same SIM: at once, also while waiting; only lost: wait for the
		 * next satellite */
		if ((m && registered(m)) || !self.hysteresis_s) {
			g_clear_handle_id(&self.leave_id, g_source_remove);
			on_leave(NULL);
			return;
		}
		if (!self.leave_id)
			self.leave_id = g_timeout_add_seconds(self.hysteresis_s, on_leave, NULL);
	}
	apply_policy();
}

static gboolean on_settled(gpointer user_data)
{
	self.settle_id = 0;
	evaluate();
	return G_SOURCE_REMOVE;
}

static void schedule_evaluate(void)
{
	if (!self.settle_id)
		self.settle_id = g_timeout_add(SETTLE_MS, on_settled, NULL);
}

/* ---- Signals ---- */

static void on_ofono_signal(GDBusConnection *bus, const char *sender, const char *path, const char *iface,
			    const char *signal, GVariant *params, gpointer user_data)
{
	Modem *m;

	if (g_str_equal(iface, "org.ofono.Manager")) {
		if (g_str_equal(signal, "ModemAdded")) {
			const char *modem;
			g_autoptr(GVariant) props = NULL;

			g_variant_get(params, "(&o@a{sv})", &modem, &props);
			modem_add(modem, props);
		} else if (g_str_equal(signal, "ModemRemoved")) {
			const char *modem;

			g_variant_get(params, "(&o)", &modem);
			g_hash_table_remove(self.modems, modem);
		}
		schedule_evaluate();
		return;
	}
	if (!g_str_equal(signal, "PropertyChanged") || !(m = g_hash_table_lookup(self.modems, path)))
		return;

	const char *key;
	g_autoptr(GVariant) value = NULL;

	g_variant_get(params, "(&sv)", &key, &value);
	if (g_str_equal(iface, "org.ofono.Modem")) {
		if (g_str_equal(key, "Interfaces"))
			modem_interfaces(m, value);
	} else {
		modem_set(m, iface, key, value);
	}
	schedule_evaluate();
}

static void on_ofono_appeared(GDBusConnection *bus, const char *name, const char *owner, gpointer user_data)
{
	self.ofono_up = TRUE;
	read_modems();
	/* A modem left with roaming on by an earlier run (crash, reboot): undo once seen */
	if (!self.roaming_held) {
		g_autofree char *held = NULL;

		if (g_file_get_contents(ROAMING_STATE, &held, NULL, NULL) && g_variant_is_object_path(held))
			self.roaming_held = g_steal_pointer(&held);
	}
	evaluate();
}

static void on_ofono_vanished(GDBusConnection *bus, const char *name, gpointer user_data)
{
	if (!self.ofono_up)
		return;
	self.ofono_up = FALSE;
	g_hash_table_remove_all(self.modems);
	g_clear_handle_id(&self.leave_id, g_source_remove);
	set_state(NULL, NULL);
	/* Nothing to undo in oFono now; the state file undoes roaming when it is back */
	g_clear_pointer(&self.roaming_held, g_free);
	sync_metered();
}

/* The mobile connection came up while on the satellite: make it metered */
static void on_nm_device_state(GDBusConnection *bus, const char *sender, const char *path, const char *iface,
			       const char *signal, GVariant *params, gpointer user_data)
{
	guint32 state = 0;

	g_variant_get(params, "(uuu)", &state, NULL, NULL);
	if (!g_strcmp0(path, self.metered_device) && state != NM_DEVICE_STATE_ACTIVATED)
		/* The connection went down, our change with it */
		g_clear_pointer(&self.metered_device, g_free);
	if (self.active && state == NM_DEVICE_STATE_ACTIVATED)
		sync_metered();
}

/* ---- Network scan with an assessment ---- */

/* A full scan (startNetworkScan, useNetworkScan=true in /etc/ofono/binder.conf) takes about a minute */
#define SCAN_TIMEOUT_MS 240000

static GPtrArray *scan_waiting; /* GDBusMethodInvocation, all get the same answer */

/* The modem to scan with: the one on the satellite, else the first with a registration */
static const char *scan_modem(void)
{
	g_autoptr(GList) paths = g_hash_table_get_keys(self.modems);

	if (self.active)
		return self.modem;
	paths = g_list_sort(paths, (GCompareFunc)g_strcmp0);
	for (GList *l = paths; l; l = l->next) {
		Modem *m = g_hash_table_lookup(self.modems, l->data);

		if (m->netreg)
			return m->path;
	}
	return NULL;
}

/* MCC + MNC of the SIM in a modem (from the IMSI), NULL if unknown */
static char *sim_plmn(const char *modem)
{
	g_autoptr(GVariant) reply = call(OFONO, modem, "org.ofono.SimManager", "GetProperties", NULL, "(a{sv})", NULL);
	g_autoptr(GVariant) props = NULL;
	const char *mcc = NULL, *mnc = NULL;

	if (!reply)
		return NULL;
	props = g_variant_get_child_value(reply, 0);
	if (!g_variant_lookup(props, "MobileCountryCode", "&s", &mcc) ||
	    !g_variant_lookup(props, "MobileNetworkCode", "&s", &mnc))
		return NULL;
	return g_strconcat(mcc, mnc, NULL);
}

/*
 * Could the phone register on this network? A forecast, the network decides:
 *   Kind:    home (the SIM's network), satellite (in the provider list), national (another network
 *            of the SIM's country), roaming (another country)
 *   Verdict: registered (on it now), yes, maybe (the provider's device list, the roaming
 *            contract), unlikely (no national roaming as a rule)
 */
static GVariant *assess(const char *plmn, const char *name, GVariant *techs, const char *home, const char *current)
{
	const Provider *p = match_provider(plmn, name);
	const char *kind, *verdict;
	GVariantBuilder b;

	if (p)
		kind = "satellite";
	else if (home && !g_strcmp0(plmn, home))
		kind = "home";
	else if (home && plmn && !strncmp(plmn, home, 3))
		kind = "national";
	else
		kind = "roaming";

	if (current && !g_strcmp0(plmn, current))
		verdict = "registered";
	else if (g_str_equal(kind, "home"))
		verdict = "yes";
	else if (g_str_equal(kind, "national"))
		verdict = "unlikely";
	else
		verdict = "maybe";

	g_variant_builder_init(&b, G_VARIANT_TYPE("a{sv}"));
	g_variant_builder_add(&b, "{sv}", "Plmn", g_variant_new_string(plmn ? plmn : ""));
	g_variant_builder_add(&b, "{sv}", "Name", g_variant_new_string(name ? name : ""));
	g_variant_builder_add(&b, "{sv}", "Technologies",
			      techs ? techs : g_variant_new_strv(NULL, 0)); /* "v" takes a reference of its own */
	g_variant_builder_add(&b, "{sv}", "Kind", g_variant_new_string(kind));
	g_variant_builder_add(&b, "{sv}", "Verdict", g_variant_new_string(verdict));
	g_variant_builder_add(&b, "{sv}", "Provider", g_variant_new_string(p ? p->name : ""));
	if (p)
		g_variant_builder_add(&b, "{sv}", "Services",
				      g_variant_new_strv((const char *const *)p->services, -1));
	return g_variant_builder_end(&b);
}

static void scan_answer(GVariant *networks, const GError *error)
{
	g_autoptr(GVariant) result = networks ? g_variant_ref_sink(g_variant_new("(@aa{sv})", networks)) : NULL;

	for (guint i = 0; i < scan_waiting->len; i++) {
		GDBusMethodInvocation *inv = scan_waiting->pdata[i];

		if (result)
			g_dbus_method_invocation_return_value(inv, result); /* takes a reference of its own */
		else
			g_dbus_method_invocation_return_gerror(inv, error);
	}
	g_ptr_array_set_size(scan_waiting, 0);
}

static void on_scanned(GObject *source, GAsyncResult *res, gpointer user_data)
{
	g_autofree char *modem = user_data;
	g_autoptr(GError) error = NULL;
	g_autoptr(GVariant) reply = g_dbus_connection_call_finish(self.bus, res, &error);
	g_autoptr(GVariantIter) iter = NULL;
	g_autofree char *home = NULL, *current = NULL;
	GVariantBuilder list;
	GVariant *props;
	Modem *m;

	if (!reply) {
		g_warning("%s: scan failed: %s", modem, error->message);
		scan_answer(NULL, error);
		return;
	}
	home = sim_plmn(modem);
	m = g_hash_table_lookup(self.modems, modem);
	current = m && registered(m) ? modem_plmn(m) : NULL;

	g_variant_builder_init(&list, G_VARIANT_TYPE("aa{sv}"));
	g_variant_get(reply, "(a(oa{sv}))", &iter);
	while (g_variant_iter_loop(iter, "(&o@a{sv})", NULL, &props)) {
		const char *mcc = NULL, *mnc = NULL, *name = NULL;
		g_autoptr(GVariant) techs = g_variant_lookup_value(props, "Technologies", G_VARIANT_TYPE_STRING_ARRAY);
		g_autofree char *plmn = NULL;

		g_variant_lookup(props, "MobileCountryCode", "&s", &mcc);
		g_variant_lookup(props, "MobileNetworkCode", "&s", &mnc);
		g_variant_lookup(props, "Name", "&s", &name);
		plmn = mcc && mnc ? g_strconcat(mcc, mnc, NULL) : NULL;
		g_variant_builder_add_value(&list, assess(plmn, name, techs, home, current));
	}
	g_message("%s: scan done", modem);
	scan_answer(g_variant_builder_end(&list), NULL);
}

/* Who may scan: root, or the user in front of the phone with the screen unlocked. A scan can
 * pause the mobile data for a moment, so not anyone. */
static gboolean scan_allowed(const char *sender, GError **error)
{
	g_autoptr(GVariant) reply = call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
					 "GetConnectionUnixUser", g_variant_new("(s)", sender), "(u)", error);
	g_autoptr(GVariant) session = NULL, user = NULL, locked = NULL;
	const char *path;
	guint32 uid, owner;

	if (!reply)
		return FALSE;
	g_variant_get(reply, "(u)", &uid);
	if (uid == 0)
		return TRUE;
	session = get_property("org.freedesktop.login1", "/org/freedesktop/login1/seat/seat0",
			       "org.freedesktop.login1.Seat", "ActiveSession");
	if (session && g_variant_is_of_type(session, G_VARIANT_TYPE("(so)"))) {
		g_variant_get(session, "(&s&o)", NULL, &path);
		user = get_property("org.freedesktop.login1", path, "org.freedesktop.login1.Session", "User");
		locked = get_property("org.freedesktop.login1", path, "org.freedesktop.login1.Session", "LockedHint");
	}
	if (user && g_variant_is_of_type(user, G_VARIANT_TYPE("(uo)")) && locked &&
	    g_variant_is_of_type(locked, G_VARIANT_TYPE_BOOLEAN)) {
		g_variant_get(user, "(uo)", &owner, NULL);
		if (owner == uid && !g_variant_get_boolean(locked))
			return TRUE;
	}
	g_set_error(error, G_DBUS_ERROR, G_DBUS_ERROR_ACCESS_DENIED,
		    "Only the user of the unlocked phone may scan for networks");
	return FALSE;
}

static void start_scan(GDBusMethodInvocation *inv)
{
	g_autoptr(GError) error = NULL;
	const char *modem;

	if (!scan_allowed(g_dbus_method_invocation_get_sender(inv), &error)) {
		g_dbus_method_invocation_return_gerror(inv, error);
		return;
	}
	g_ptr_array_add(scan_waiting, inv);
	/* A scan is running already: this caller gets its result too */
	if (scan_waiting->len > 1)
		return;
	modem = scan_modem();
	if (!modem) {
		g_set_error(&error, G_DBUS_ERROR, G_DBUS_ERROR_FAILED, "No modem with a network registration");
		scan_answer(NULL, error);
		return;
	}
	g_message("%s: scanning for networks", modem);
	g_dbus_connection_call(self.bus, OFONO, modem, "org.ofono.NetworkRegistration", "Scan", NULL,
			       G_VARIANT_TYPE("(a(oa{sv}))"), G_DBUS_CALL_FLAGS_NONE, SCAN_TIMEOUT_MS, NULL,
			       on_scanned, g_strdup(modem));
}

/* Assess networks someone else found (GNOME Settings scans through ModemManager): (PLMN, name) */
static void assess_networks(GDBusMethodInvocation *inv, GVariant *params)
{
	const char *modem = scan_modem();
	g_autofree char *home = modem ? sim_plmn(modem) : NULL;
	g_autofree char *current = NULL;
	g_autoptr(GVariantIter) iter = NULL;
	Modem *m = modem ? g_hash_table_lookup(self.modems, modem) : NULL;
	GVariantBuilder list;
	const char *plmn, *name;

	current = m && registered(m) ? modem_plmn(m) : NULL;
	g_variant_builder_init(&list, G_VARIANT_TYPE("aa{sv}"));
	g_variant_get(params, "(a(ss))", &iter);
	while (g_variant_iter_next(iter, "(&s&s)", &plmn, &name))
		g_variant_builder_add_value(&list, assess(plmn, name, NULL, home, current));
	g_dbus_method_invocation_return_value(inv, g_variant_new("(aa{sv})", &list));
}

static void on_method_call(GDBusConnection *bus, const char *sender, const char *path, const char *iface,
			   const char *method, GVariant *params, GDBusMethodInvocation *inv, gpointer user_data)
{
	if (g_str_equal(method, "Scan"))
		start_scan(inv);
	else if (g_str_equal(method, "Assess"))
		assess_networks(inv, params);
	else
		g_dbus_method_invocation_return_error(inv, G_DBUS_ERROR, G_DBUS_ERROR_UNKNOWN_METHOD,
						      "No method %s", method);
}

/* ---- plinius-satellite --scan: the networks around, in the terminal ---- */

static int scan_cli(void)
{
	g_autoptr(GError) error = NULL;
	g_autoptr(GDBusConnection) bus = g_bus_get_sync(G_BUS_TYPE_SYSTEM, NULL, &error);
	g_autoptr(GVariant) reply = NULL;
	g_autoptr(GVariantIter) iter = NULL;
	GVariant *net;

	if (bus) {
		g_print("Scanning, this takes about a minute...\n");
		reply = g_dbus_connection_call_sync(bus, BUS_NAME, OBJECT_PATH, INTERFACE, "Scan", NULL,
						    G_VARIANT_TYPE("(aa{sv})"), G_DBUS_CALL_FLAGS_NONE,
						    SCAN_TIMEOUT_MS + 10000, NULL, &error);
	}
	if (!reply) {
		g_printerr("Scan failed: %s\n", error->message);
		return 1;
	}
	g_print("%-7s %-26s %-12s %-10s %-11s %s\n", "PLMN", "NAME", "TECH", "KIND", "REGISTER?", "NOTE");
	g_variant_get(reply, "(aa{sv})", &iter);
	while (g_variant_iter_loop(iter, "@a{sv}", &net)) {
		const char *plmn = "", *name = "", *kind = "", *verdict = "", *provider = "";
		g_autofree const char **techs = NULL;
		g_autoptr(GVariant) tv = g_variant_lookup_value(net, "Technologies", G_VARIANT_TYPE_STRING_ARRAY);
		g_autofree char *tech = NULL;
		const char *note = "";

		g_variant_lookup(net, "Plmn", "&s", &plmn);
		g_variant_lookup(net, "Name", "&s", &name);
		g_variant_lookup(net, "Kind", "&s", &kind);
		g_variant_lookup(net, "Verdict", "&s", &verdict);
		g_variant_lookup(net, "Provider", "&s", &provider);
		techs = tv ? g_variant_get_strv(tv, NULL) : NULL;
		tech = techs ? g_strjoinv(",", (char **)techs) : g_strdup("");
		if (g_str_equal(verdict, "registered"))
			note = "on it now";
		else if (g_str_equal(kind, "home"))
			note = "network of the SIM";
		else if (g_str_equal(kind, "satellite"))
			note = "satellite, the provider's device list decides";
		else if (g_str_equal(kind, "national"))
			note = "other network of the SIM's country, usually no national roaming";
		else
			note = "abroad, needs roaming in the contract";
		g_print("%-7s %-26.26s %-12s %-10s %-11s %s%s%s\n", plmn, name, tech, kind, verdict, note,
			*provider ? ": " : "", provider);
	}
	return 0;
}

/* ---- Exported object ---- */

static const char introspection[] =
	"<node>"
	" <interface name='" INTERFACE "'>"
	"  <property name='Active' type='b' access='read'/>"
	"  <property name='Modem' type='o' access='read'/>"
	"  <property name='Network' type='s' access='read'/>"
	"  <property name='Plmn' type='s' access='read'/>"
	"  <property name='Provider' type='s' access='read'/>"
	"  <property name='Services' type='as' access='read'/>"
	"  <method name='Scan'><arg name='networks' type='aa{sv}' direction='out'/></method>"
	"  <method name='Assess'><arg name='networks' type='a(ss)' direction='in'/>"
	"   <arg name='assessed' type='aa{sv}' direction='out'/></method>"
	" </interface>"
	"</node>";

static GVariant *on_get_property(GDBusConnection *bus, const char *sender, const char *path, const char *iface,
				 const char *property, GError **error, gpointer user_data)
{
	if (g_str_equal(property, "Active"))
		return g_variant_new_boolean(self.active);
	if (g_str_equal(property, "Modem"))
		return g_variant_new_object_path(self.modem);
	if (g_str_equal(property, "Network"))
		return g_variant_new_string(self.network ? self.network : "");
	if (g_str_equal(property, "Plmn"))
		return g_variant_new_string(self.plmn ? self.plmn : "");
	if (g_str_equal(property, "Provider"))
		return g_variant_new_string(self.provider ? self.provider->name : "");
	if (g_str_equal(property, "Services"))
		return services_variant();
	g_set_error(error, G_DBUS_ERROR, G_DBUS_ERROR_UNKNOWN_PROPERTY, "No property %s", property);
	return NULL;
}

static const GDBusInterfaceVTable vtable = {on_method_call, on_get_property, NULL, {0}};

static void on_name_lost(GDBusConnection *bus, const char *name, gpointer user_data)
{
	g_warning("Lost the bus name %s", name);
	g_main_loop_quit(self.loop);
}

static gboolean on_signal(gpointer user_data)
{
	g_main_loop_quit(self.loop);
	return G_SOURCE_REMOVE;
}

int main(int argc, char **argv)
{
	g_autoptr(GError) error = NULL;
	GDBusNodeInfo *info;

	if (argc > 1 && g_str_equal(argv[1], "--scan"))
		return scan_cli();
	if (argc > 1) {
		g_printerr("Usage: %s [--scan]\n", argv[0]);
		return 2;
	}

	self.modem = g_strdup("/");
	scan_waiting = g_ptr_array_new();
	self.modems = g_hash_table_new_full(g_str_hash, g_str_equal, NULL, modem_free);
	load_providers();
	load_policy();

	self.bus = g_bus_get_sync(G_BUS_TYPE_SYSTEM, NULL, &error);
	if (!self.bus) {
		g_warning("No system bus: %s", error->message);
		return 1;
	}
	info = g_dbus_node_info_new_for_xml(introspection, NULL);
	g_dbus_connection_register_object(self.bus, OBJECT_PATH, info->interfaces[0], &vtable, NULL, NULL, NULL);
	g_dbus_node_info_unref(info);

	g_dbus_connection_signal_subscribe(self.bus, OFONO, NULL, NULL, NULL, NULL, G_DBUS_SIGNAL_FLAGS_NONE,
					   on_ofono_signal, NULL, NULL);
	g_dbus_connection_signal_subscribe(self.bus, NM, NM_DEVICE, "StateChanged", NULL, NULL,
					   G_DBUS_SIGNAL_FLAGS_NONE, on_nm_device_state, NULL, NULL);
	g_bus_watch_name_on_connection(self.bus, OFONO, G_BUS_NAME_WATCHER_FLAGS_NONE, on_ofono_appeared,
				       on_ofono_vanished, NULL, NULL);
	g_bus_own_name_on_connection(self.bus, BUS_NAME, G_BUS_NAME_OWNER_FLAGS_NONE, NULL, on_name_lost, NULL,
				     NULL);

	self.loop = g_main_loop_new(NULL, FALSE);
	g_unix_signal_add(SIGTERM, on_signal, NULL);
	g_unix_signal_add(SIGINT, on_signal, NULL);
	g_main_loop_run(self.loop);

	/* Leaving: undo what we changed, a satellite or not */
	g_clear_handle_id(&self.leave_id, g_source_remove);
	set_state(NULL, NULL);
	apply_policy();
	g_dbus_connection_flush_sync(self.bus, NULL, NULL);
	return 0;
}
