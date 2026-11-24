// SPDX-License-Identifier: MIT
// plinius-satellite-notify: tells the user in the session when the phone is connected through a
// satellite (org.plinius.Satellite from plinius-satellite), what works there and what helps.
// The notification stays while the satellite does and goes away with it.
#include <gio/gio.h>
#include <glib-unix.h>
#include <glib/gi18n.h>
#include <locale.h>

#define GETTEXT_DOMAIN "plinius-satellite"

#define SAT_NAME "org.plinius.Satellite"
#define SAT_PATH "/org/plinius/Satellite"
#define SAT_INTERFACE "org.plinius.Satellite"

#define NOTIFY_NAME "org.freedesktop.Notifications"
#define NOTIFY_PATH "/org/freedesktop/Notifications"
#define NOTIFY_INTERFACE "org.freedesktop.Notifications"

static struct {
	GMainLoop *loop;
	GDBusProxy *sat;
	GDBusConnection *session;
	guint32 note_id;  /* the notification shown, 0 if none */
	char *shown_for;  /* provider and services it was shown for */
} self;

static guint32 notify(guint32 replaces, const char *title, const char *body)
{
	g_autoptr(GVariant) reply = NULL;
	g_autoptr(GError) error = NULL;
	GVariantBuilder hints;
	guint32 id = 0;

	g_variant_builder_init(&hints, G_VARIANT_TYPE("a{sv}"));
	g_variant_builder_add(&hints, "{sv}", "urgency", g_variant_new_byte(1));
	/* Stays in the list while the satellite lasts, we close it afterwards */
	g_variant_builder_add(&hints, "{sv}", "resident", g_variant_new_boolean(TRUE));
	g_variant_builder_add(&hints, "{sv}", "category", g_variant_new_string("network.connected"));

	reply = g_dbus_connection_call_sync(self.session, NOTIFY_NAME, NOTIFY_PATH, NOTIFY_INTERFACE, "Notify",
					    g_variant_new("(susss^asa{sv}i)",
							  /* Translators: who the notification is from */
							  _("Satellite"), replaces,
							  "plinius-satellite-symbolic", title, body,
							  (const char *const[]){NULL}, &hints, 0),
					    G_VARIANT_TYPE("(u)"), G_DBUS_CALL_FLAGS_NONE, 2000, NULL, &error);
	if (!reply) {
		g_warning("Cannot show the notification: %s", error->message);
		return 0;
	}
	g_variant_get(reply, "(u)", &id);
	return id;
}

static void close_note(void)
{
	if (!self.note_id)
		return;
	g_dbus_connection_call(self.session, NOTIFY_NAME, NOTIFY_PATH, NOTIFY_INTERFACE, "CloseNotification",
			       g_variant_new("(u)", self.note_id), NULL, G_DBUS_CALL_FLAGS_NONE, 2000, NULL, NULL,
			       NULL);
	self.note_id = 0;
}

/* "Text messages and data work." in the user's language, from the provider's services */
static char *services_text(const char *const *services)
{
	gboolean sms = g_strv_contains(services, "sms"), mms = g_strv_contains(services, "mms");
	gboolean data = g_strv_contains(services, "data"), voice = g_strv_contains(services, "voice");

	if (voice && data)
		return g_strdup(_("Calls, messages and data work, slower than usual."));
	if (voice)
		return g_strdup(_("Calls and text messages work."));
	if (data && (sms || mms))
		return g_strdup(_("Text messages and some apps work, slower than usual."));
	if (data)
		return g_strdup(_("Some apps work, slower than usual."));
	return g_strdup(_("Only text messages work."));
}

static void update(void)
{
	g_autoptr(GVariant) active = g_dbus_proxy_get_cached_property(self.sat, "Active");
	g_autoptr(GVariant) provider = g_dbus_proxy_get_cached_property(self.sat, "Provider");
	g_autoptr(GVariant) services = g_dbus_proxy_get_cached_property(self.sat, "Services");
	g_autofree const char **list = NULL;
	g_autofree char *joined = NULL, *key = NULL, *works = NULL, *body = NULL, *title = NULL;
	const char *name;

	if (!active || !g_variant_get_boolean(active) || !provider || !services) {
		close_note();
		g_clear_pointer(&self.shown_for, g_free);
		return;
	}
	name = g_variant_get_string(provider, NULL);
	list = g_variant_get_strv(services, NULL);
	joined = g_strjoinv(",", (char **)list);
	key = g_strconcat(name, ":", joined, NULL);
	/* Once per satellite connection, not again for every change of the network name */
	if (self.note_id && !g_strcmp0(key, self.shown_for))
		return;

	works = services_text(list);
	/* Translators: %s is the satellite service, e.g. "T-Mobile T-Satellite" */
	title = *name ? g_strdup_printf(_("Connected via satellite (%s)"), name) : g_strdup(_("Connected via satellite"));
	body = g_strdup_printf("%s %s", works,
			       _("Stay outdoors with a clear view of the sky. Sending can take a few minutes."));
	self.note_id = notify(self.note_id, title, body);
	g_free(self.shown_for);
	self.shown_for = g_steal_pointer(&key);
}

static void on_notify_signal(GDBusConnection *connection, const char *sender, const char *path, const char *iface,
			     const char *signal_name, GVariant *params, gpointer user_data)
{
	guint32 id = 0;

	/* Dismissed by the user: not shown again until the next satellite connection */
	if (g_str_equal(signal_name, "NotificationClosed")) {
		g_variant_get(params, "(uu)", &id, NULL);
		if (id == self.note_id)
			self.note_id = 0;
	}
}

static void on_sat_changed(GDBusProxy *proxy, GVariant *changed, const char *const *invalidated, gpointer user_data)
{
	g_autoptr(GVariant) active = g_dbus_proxy_get_cached_property(self.sat, "Active");

	/* Closed by the user and still the same satellite: leave it closed */
	if (!self.note_id && self.shown_for && active && g_variant_get_boolean(active)) {
		g_autoptr(GVariant) provider = g_dbus_proxy_get_cached_property(self.sat, "Provider");

		if (provider && g_str_has_prefix(self.shown_for, g_variant_get_string(provider, NULL)))
			return;
	}
	update();
}

static void on_owner_changed(GObject *object, GParamSpec *pspec, gpointer user_data)
{
	update();
}

static gboolean on_signal(gpointer user_data)
{
	g_main_loop_quit(self.loop);
	return G_SOURCE_REMOVE;
}

int main(void)
{
	g_autoptr(GError) error = NULL;

	setlocale(LC_ALL, "");
	textdomain(GETTEXT_DOMAIN);
	bind_textdomain_codeset(GETTEXT_DOMAIN, "UTF-8");

	self.session = g_bus_get_sync(G_BUS_TYPE_SESSION, NULL, &error);
	if (!self.session) {
		g_warning("No session bus: %s", error->message);
		return 1;
	}
	/* Without plinius-satellite yet: it shows up later, the proxy follows */
	self.sat = g_dbus_proxy_new_for_bus_sync(G_BUS_TYPE_SYSTEM, G_DBUS_PROXY_FLAGS_DO_NOT_AUTO_START, NULL,
						 SAT_NAME, SAT_PATH, SAT_INTERFACE, NULL, &error);
	if (!self.sat) {
		g_warning("No connection to plinius-satellite: %s", error->message);
		return 1;
	}
	g_signal_connect(self.sat, "g-properties-changed", G_CALLBACK(on_sat_changed), NULL);
	g_signal_connect(self.sat, "notify::g-name-owner", G_CALLBACK(on_owner_changed), NULL);
	g_dbus_connection_signal_subscribe(self.session, NOTIFY_NAME, NOTIFY_INTERFACE, "NotificationClosed",
					   NOTIFY_PATH, NULL, G_DBUS_SIGNAL_FLAGS_NONE, on_notify_signal, NULL, NULL);

	update();

	self.loop = g_main_loop_new(NULL, FALSE);
	g_unix_signal_add(SIGTERM, on_signal, NULL);
	g_unix_signal_add(SIGINT, on_signal, NULL);
	g_main_loop_run(self.loop);

	close_note();
	g_dbus_connection_flush_sync(self.session, NULL, NULL);
	return 0;
}
