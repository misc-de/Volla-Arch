/*
 * dhw_usb_fakeservice.h: plinius-usb and the notification server as seen by plinius-usb-webcam and
 * plinius-usb-prompt in their daemon-mode tests. It runs in a thread of its own with its own main
 * context, on a connection of its own to the private bus of the test (DBUS_SYSTEM_BUS_ADDRESS):
 * the program under test reaches it over the bus like the real services. Never the host's buses.
 *
 *   org.plinius.Usb /org/plinius/Usb: string properties (WebcamDevice, WebcamCamera, PortQuestion,
 *     ActivePort), ChoosePort(s) -> "service ChoosePort KEY" (an error once "svc choose-fails")
 *   org.freedesktop.Notifications: Notify -> "service Notify ID replaces=.. title=.. actions=..
 *     urgency=.. expire=..", CloseNotification -> "service Close ID"
 *
 * svc_command() takes "svc set PROP VALUE" ("-" empty), "svc signal ActionInvoked ID KEY",
 * "svc signal NotificationClosed ID", "svc release" (plinius-usb goes), "svc own" (comes back),
 * "svc choose-fails". Each answers once the service did it.
 */
#pragma once

#include <gio/gio.h>
#include <stdio.h>
#include <string.h>

static const char svc_xml[] =
	"<node>"
	" <interface name='org.plinius.Usb'>"
	"  <method name='ChoosePort'><arg name='port' type='s' direction='in'/></method>"
	"  <property name='WebcamDevice' type='s' access='read'/>"
	"  <property name='WebcamCamera' type='s' access='read'/>"
	"  <property name='PortQuestion' type='s' access='read'/>"
	"  <property name='ActivePort' type='s' access='read'/>"
	" </interface>"
	" <interface name='org.freedesktop.Notifications'>"
	"  <method name='Notify'>"
	"   <arg type='s' direction='in'/><arg type='u' direction='in'/><arg type='s' direction='in'/>"
	"   <arg type='s' direction='in'/><arg type='s' direction='in'/><arg type='as' direction='in'/>"
	"   <arg type='a{sv}' direction='in'/><arg type='i' direction='in'/><arg type='u' direction='out'/>"
	"  </method>"
	"  <method name='CloseNotification'><arg type='u' direction='in'/></method>"
	" </interface>"
	"</node>";

static struct {
	GMainContext *context;
	GDBusConnection *bus;
	GHashTable *props;
	guint32 next_id;
	gboolean choose_fails;
	GMutex mutex;
	GCond cond;
	gboolean done;
} svc;

static void svc_method(GDBusConnection *bus, const char *sender, const char *path, const char *iface,
		       const char *method, GVariant *params, GDBusMethodInvocation *invocation, gpointer data)
{
	if (!strcmp(method, "ChoosePort")) {
		const char *key;

		g_variant_get(params, "(&s)", &key);
		printf("service ChoosePort %s\n", key);
		if (svc.choose_fails)
			g_dbus_method_invocation_return_error_literal(invocation, G_DBUS_ERROR, G_DBUS_ERROR_FAILED,
								      "There is no question open");
		else
			g_dbus_method_invocation_return_value(invocation, NULL);
	} else if (!strcmp(method, "Notify")) {
		const char *app, *icon, *title, *body;
		guint32 replaces;
		g_autofree const char **actions = NULL;
		g_autoptr(GVariant) hints = NULL;
		g_autofree char *joined = NULL;
		guchar urgency = 0;
		gboolean transient = FALSE;
		const char *category = "";
		gint32 expire;
		guint32 id;

		g_variant_get(params, "(&su&s&s&s^a&s@a{sv}i)", &app, &replaces, &icon, &title, &body, &actions, &hints,
			      &expire);
		g_variant_lookup(hints, "urgency", "y", &urgency);
		g_variant_lookup(hints, "transient", "b", &transient);
		g_variant_lookup(hints, "category", "&s", &category);
		joined = g_strjoinv(",", (char **)actions);
		id = replaces ? replaces : ++svc.next_id;
		printf("service Notify %u app=%s icon=%s replaces=%u title=%s body=%s actions=%s urgency=%u "
		       "transient=%d category=%s expire=%d\n",
		       id, app, icon, replaces, title, body, joined, urgency, transient, category, expire);
		g_dbus_method_invocation_return_value(invocation, g_variant_new("(u)", id));
	} else {
		guint32 id;

		g_variant_get(params, "(u)", &id);
		printf("service Close %u\n", id);
		g_dbus_method_invocation_return_value(invocation, NULL);
	}
}

static GVariant *svc_get(GDBusConnection *bus, const char *sender, const char *path, const char *iface,
			 const char *property, GError **error, gpointer data)
{
	const char *value = g_hash_table_lookup(svc.props, property);

	return g_variant_new_string(value ? value : "");
}

static const GDBusInterfaceVTable svc_vtable = {svc_method, svc_get, NULL, {0}};

static void svc_own(const char *name)
{
	g_autoptr(GVariant) r = g_dbus_connection_call_sync(svc.bus, "org.freedesktop.DBus", "/org/freedesktop/DBus",
							     "org.freedesktop.DBus", "RequestName",
							     g_variant_new("(su)", name, 0), NULL, 0, -1, NULL, NULL);

	if (!r)
		abort();
}

static gpointer svc_thread(gpointer data)
{
	const char *address = g_getenv("DBUS_SYSTEM_BUS_ADDRESS");
	GDBusNodeInfo *info = g_dbus_node_info_new_for_xml(svc_xml, NULL);
	GMainLoop *loop;

	g_main_context_push_thread_default(svc.context);
	svc.bus = g_dbus_connection_new_for_address_sync(address, G_DBUS_CONNECTION_FLAGS_AUTHENTICATION_CLIENT |
									   G_DBUS_CONNECTION_FLAGS_MESSAGE_BUS_CONNECTION,
							  NULL, NULL, NULL);
	if (!svc.bus)
		abort();
	g_dbus_connection_register_object(svc.bus, "/org/plinius/Usb", info->interfaces[0], &svc_vtable, NULL, NULL,
					  NULL);
	g_dbus_connection_register_object(svc.bus, "/org/freedesktop/Notifications", info->interfaces[1], &svc_vtable,
					  NULL, NULL, NULL);
	svc_own("org.plinius.Usb");
	svc_own("org.freedesktop.Notifications");
	g_mutex_lock(&svc.mutex);
	svc.done = TRUE;
	g_cond_signal(&svc.cond);
	g_mutex_unlock(&svc.mutex);
	loop = g_main_loop_new(svc.context, FALSE);
	g_main_loop_run(loop);
	return NULL;
}

static void svc_wait(void)
{
	g_mutex_lock(&svc.mutex);
	while (!svc.done)
		g_cond_wait(&svc.cond, &svc.mutex);
	svc.done = FALSE;
	g_mutex_unlock(&svc.mutex);
}

/* Starts the service with these properties ("PROP=VALUE" ...); returns once it has its names */
static void svc_start(char **initial)
{
	svc.context = g_main_context_new();
	svc.props = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, g_free);
	for (char **p = initial; p && *p; p++) {
		g_auto(GStrv) kv = g_strsplit(*p, "=", 2);

		if (kv[0] && kv[1])
			g_hash_table_insert(svc.props, g_strdup(kv[0]), g_strdup(kv[1]));
	}
	g_thread_new("fake-service", svc_thread, NULL);
	svc_wait();
}

static gboolean svc_run(gpointer data)
{
	char *line = data;
	char a[256] = "", b[256] = "", c[256] = "";

	if (sscanf(line, "svc set %255s %255s", a, b) == 2) {
		GVariantBuilder changed;

		g_hash_table_insert(svc.props, g_strdup(a), g_strdup(strcmp(b, "-") ? b : ""));
		g_variant_builder_init(&changed, G_VARIANT_TYPE("a{sv}"));
		g_variant_builder_add(&changed, "{sv}", a, g_variant_new_string(strcmp(b, "-") ? b : ""));
		g_dbus_connection_emit_signal(svc.bus, NULL, "/org/plinius/Usb", "org.freedesktop.DBus.Properties",
					      "PropertiesChanged",
					      g_variant_new("(sa{sv}as)", "org.plinius.Usb", &changed, NULL), NULL);
	} else if (sscanf(line, "svc signal ActionInvoked %255s %255s", a, b) == 2) {
		g_dbus_connection_emit_signal(svc.bus, NULL, "/org/freedesktop/Notifications",
					      "org.freedesktop.Notifications", "ActionInvoked",
					      g_variant_new("(us)", (guint32)atoi(a), b), NULL);
	} else if (sscanf(line, "svc signal NotificationClosed %255s", a) == 1) {
		g_dbus_connection_emit_signal(svc.bus, NULL, "/org/freedesktop/Notifications",
					      "org.freedesktop.Notifications", "NotificationClosed",
					      g_variant_new("(uu)", (guint32)atoi(a), 2), NULL);
	} else if (!strcmp(line, "svc release")) {
		g_autoptr(GVariant) r = g_dbus_connection_call_sync(svc.bus, "org.freedesktop.DBus",
								     "/org/freedesktop/DBus", "org.freedesktop.DBus",
								     "ReleaseName", g_variant_new("(s)", "org.plinius.Usb"),
								     NULL, 0, -1, NULL, NULL);
	} else if (!strcmp(line, "svc own")) {
		svc_own("org.plinius.Usb");
	} else if (!strcmp(line, "svc choose-fails")) {
		svc.choose_fails = TRUE;
	}
	(void)c;
	g_dbus_connection_flush_sync(svc.bus, NULL, NULL);
	g_free(line);
	g_mutex_lock(&svc.mutex);
	svc.done = TRUE;
	g_cond_signal(&svc.cond);
	g_mutex_unlock(&svc.mutex);
	return G_SOURCE_REMOVE;
}

static gboolean svc_barrier(gpointer data)
{
	g_mutex_lock(&svc.mutex);
	svc.done = TRUE;
	g_cond_signal(&svc.cond);
	g_mutex_unlock(&svc.mutex);
	return G_SOURCE_REMOVE;
}

/* The program under test ended: what it sent last reaches the service before the process (and
 * the service with it) ends. Its messages are on the bus already (it flushed); a call of the
 * service's own comes back after them, the barrier after their dispatch. */
static void svc_settle(void)
{
	g_autoptr(GVariant) id = NULL;

	if (!svc.bus)
		return;
	id = g_dbus_connection_call_sync(svc.bus, "org.freedesktop.DBus", "/org/freedesktop/DBus",
					 "org.freedesktop.DBus", "GetId", NULL, NULL, 0, -1, NULL, NULL);
	g_main_context_invoke(svc.context, svc_barrier, NULL);
	svc_wait();
}

/* Runs a "svc ..." command in the service's thread; TRUE if it was one */
static gboolean svc_command(const char *line)
{
	if (!g_str_has_prefix(line, "svc "))
		return FALSE;
	g_main_context_invoke(svc.context, svc_run, g_strdup(line));
	svc_wait();
	return TRUE;
}
