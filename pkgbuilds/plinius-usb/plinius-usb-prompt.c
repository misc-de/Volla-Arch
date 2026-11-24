// SPDX-License-Identifier: MIT
// plinius-usb-prompt: asks in the session which USB port stays active when a pogo accessory and a
// USB-C device meet on the Plinius Plus. plinius-usb decides when (property PortQuestion) and
// does the switching (method ChoosePort), this only is the notification with its two buttons.
// A notification, because the question must never be in the way: without an answer nothing
// changes. Hence it's not on the lock screen either, plinius-usb doesn't ask while locked.
#include <gio/gio.h>
#include <glib-unix.h>
#include <glib/gi18n.h>
#include <locale.h>

#define GETTEXT_DOMAIN "plinius-usb"

#define USB_NAME "org.plinius.Usb"
#define USB_PATH "/org/plinius/Usb"
#define USB_INTERFACE "org.plinius.Usb"

#define NOTIFY_NAME "org.freedesktop.Notifications"
#define NOTIFY_PATH "/org/freedesktop/Notifications"
#define NOTIFY_INTERFACE "org.freedesktop.Notifications"

/* The question closes when plinius-usb says so, this is for a service that went away */
#define QUESTION_EXPIRE_MS 8000
#define NOTE_EXPIRE_MS 5000

static struct {
	GMainLoop *loop;
	GDBusProxy *usb;
	GDBusConnection *session;
	/* Whose answer is awaited, 0 if none */
	guint32 question_id;
	char *active_port;
} self;

/* Send a notification; actions: NULL or pairs of key and label */
static guint32 notify(guint32 replaces, const char *title, const char *body, const char *const *actions,
		      gboolean urgent, int expire_ms)
{
	g_autoptr(GVariant) reply = NULL;
	g_autoptr(GError) error = NULL;
	GVariantBuilder hints;
	guint32 id = 0;

	g_variant_builder_init(&hints, G_VARIANT_TYPE("a{sv}"));
	/* Critical: a banner also with "do not disturb", and it stays for the whole time */
	g_variant_builder_add(&hints, "{sv}", "urgency", g_variant_new_byte(urgent ? 2 : 1));
	/* About the moment only, nothing to keep in the list */
	g_variant_builder_add(&hints, "{sv}", "transient", g_variant_new_boolean(TRUE));
	g_variant_builder_add(&hints, "{sv}", "category", g_variant_new_string("device"));

	reply = g_dbus_connection_call_sync(self.session, NOTIFY_NAME, NOTIFY_PATH, NOTIFY_INTERFACE, "Notify",
					    g_variant_new("(susss^asa{sv}i)", "plinius-usb", replaces,
							  "plinius-usb-symbolic", title, body,
							  actions ? actions : (const char *const[]){NULL}, &hints,
							  expire_ms),
					    G_VARIANT_TYPE("(u)"), G_DBUS_CALL_FLAGS_NONE, 2000, NULL, &error);
	if (!reply) {
		g_warning("Cannot show the notification: %s", error->message);
		return 0;
	}
	g_variant_get(reply, "(u)", &id);
	return id;
}

static void close_question(void)
{
	if (!self.question_id)
		return;
	g_dbus_connection_call(self.session, NOTIFY_NAME, NOTIFY_PATH, NOTIFY_INTERFACE, "CloseNotification",
			       g_variant_new("(u)", self.question_id), NULL, G_DBUS_CALL_FLAGS_NONE, 2000, NULL, NULL,
			       NULL);
	self.question_id = 0;
}

static char *usb_string(const char *property)
{
	g_autoptr(GVariant) value = g_dbus_proxy_get_cached_property(self.usb, property);

	return value ? g_variant_dup_string(value, NULL) : g_strdup("");
}

static void show_question(const char *stays)
{
	static const char *const actions[] = {"usb-c", "USB-C", "pogo", "Pogo", NULL};
	const char *body = g_str_equal(stays, "pogo")
				   ? _("A pogo accessory and a USB-C device are connected, only one of them can "
				       "carry data. Without an answer, Pogo stays active.")
				   : _("A pogo accessory and a USB-C device are connected, only one of them can "
				       "carry data. Without an answer, USB-C stays active.");

	self.question_id = notify(self.question_id, _("Which port should be active?"), body, actions, TRUE,
				  QUESTION_EXPIRE_MS);
}

/* Follow what plinius-usb says, also when it was just started */
static void update(void)
{
	g_autofree char *question = usb_string("PortQuestion");
	g_autofree char *port = usb_string("ActivePort");

	if (*question && !self.question_id)
		show_question(question);
	else if (!*question)
		close_question();

	/* Only the accessory, nothing to ask: it took the port as it was plugged in */
	if (g_str_equal(port, "pogo") && !g_strcmp0(self.active_port, "none"))
		notify(0, _("Pogo accessory active"), _("USB-C carries no data until the accessory is removed."), NULL,
		       FALSE, NOTE_EXPIRE_MS);
	g_free(self.active_port);
	self.active_port = g_steal_pointer(&port);
}

static void on_chosen(GObject *source, GAsyncResult *result, gpointer user_data)
{
	g_autoptr(GVariant) reply = NULL;
	g_autoptr(GError) error = NULL;

	reply = g_dbus_proxy_call_finish(G_DBUS_PROXY(source), result, &error);
	/* Locked in the meantime or too late: nothing changes, as without an answer */
	if (!reply)
		g_message("The choice did not count: %s", error->message);
}

static void on_notify_signal(GDBusConnection *connection, const char *sender, const char *path, const char *iface,
			     const char *signal_name, GVariant *params, gpointer user_data)
{
	guint32 id = 0;

	if (g_str_equal(signal_name, "NotificationClosed")) {
		g_variant_get(params, "(uu)", &id, NULL);
		if (id == self.question_id)
			self.question_id = 0;
	} else if (g_str_equal(signal_name, "ActionInvoked")) {
		const char *key = NULL;
		g_autofree char *stays = NULL;

		g_variant_get(params, "(u&s)", &id, &key);
		if (id != self.question_id)
			return;
		self.question_id = 0;
		stays = usb_string("PortQuestion");
		if (!g_str_equal(key, "usb-c") && !g_str_equal(key, "pogo"))
			return;
		g_dbus_proxy_call(self.usb, "ChoosePort", g_variant_new("(s)", key), G_DBUS_CALL_FLAGS_NONE, -1, NULL,
				  on_chosen, NULL);
		/* The driver only takes the choice when its 10 seconds are over */
		if (*stays && !g_str_equal(stays, key))
			notify(0, g_str_equal(key, "pogo") ? _("Switching to Pogo") : _("Switching to USB-C"),
			       _("This can take up to 10 seconds. It stays that way until the accessory or the cable "
				 "is removed."),
			       NULL, FALSE, NOTE_EXPIRE_MS);
	}
}

static void on_usb_changed(GDBusProxy *proxy, GVariant *changed, const char *const *invalidated, gpointer user_data)
{
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
	/* Without a plinius-usb yet: it shows up later, the proxy follows */
	self.usb = g_dbus_proxy_new_for_bus_sync(G_BUS_TYPE_SYSTEM, G_DBUS_PROXY_FLAGS_DO_NOT_AUTO_START, NULL,
						 USB_NAME, USB_PATH, USB_INTERFACE, NULL, &error);
	if (!self.usb) {
		g_warning("No connection to plinius-usb: %s", error->message);
		return 1;
	}
	self.active_port = usb_string("ActivePort");

	g_signal_connect(self.usb, "g-properties-changed", G_CALLBACK(on_usb_changed), NULL);
	g_signal_connect(self.usb, "notify::g-name-owner", G_CALLBACK(on_owner_changed), NULL);
	g_dbus_connection_signal_subscribe(self.session, NOTIFY_NAME, NOTIFY_INTERFACE, NULL, NOTIFY_PATH, NULL,
					   G_DBUS_SIGNAL_FLAGS_NONE, on_notify_signal, NULL, NULL);

	update();

	self.loop = g_main_loop_new(NULL, FALSE);
	g_unix_signal_add(SIGTERM, on_signal, NULL);
	g_unix_signal_add(SIGINT, on_signal, NULL);
	g_main_loop_run(self.loop);

	close_question();
	g_dbus_connection_flush_sync(self.session, NULL, NULL);
	return 0;
}
