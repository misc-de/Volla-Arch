/*
 * Harness for plinius-usb-prompt.c (its main() renamed): the notification asking which port stays
 * active, and what an answer does. It needs no display: the question is a notification.
 *
 * Command mode: Notify (g_dbus_connection_call_sync) is printed as "notify ..." and answered with
 * ids from 10 on ("notify-fails" makes the next one fail); CloseNotification and ChoosePort
 * (async) are printed, "chosen ok|error" answers the last ChoosePort; plinius-usb's cached
 * properties come from "prop NAME VALUE"; "signal ActionInvoked ID KEY" / "signal
 * NotificationClosed ID" are the notification server's signals.
 * Daemon mode (argv[1] "daemon"): the program's main() against dhw_usb_fakeservice.h on the
 * private bus of the test, as both the session and the system bus; commands on stdin.
 */
#define _GNU_SOURCE
#include <gio/gio.h>
#include <glib-unix.h>
#include <glib/gi18n.h>
#include <locale.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "dhw_usb_fakeservice.h"

static gboolean daemon_mode;
static int fake_object;
#define FAKE ((gpointer)&fake_object)
static GHashTable *props;
static guint32 notify_next = 10;
static gboolean notify_fails;
static GAsyncReadyCallback chosen_callback;

static GVariant *fake_call_sync(GDBusConnection *bus, const char *dest, const char *path, const char *iface,
				const char *method, GVariant *params, const GVariantType *reply_type,
				GDBusCallFlags flags, int timeout, GCancellable *cancellable, GError **error)
{
	g_autoptr(GVariant) p = g_variant_ref_sink(params);
	const char *app, *icon, *title, *body, *category = "";
	guint32 replaces;
	g_autofree const char **actions = NULL;
	g_autoptr(GVariant) hints = NULL;
	g_autofree char *joined = NULL;
	guchar urgency = 0;
	gboolean transient = FALSE;
	gint32 expire;

	if ((gpointer)bus != FAKE)
		return g_dbus_connection_call_sync(bus, dest, path, iface, method, p, reply_type, flags, timeout,
						   cancellable, error);
	g_variant_get(p, "(&su&s&s&s^a&s@a{sv}i)", &app, &replaces, &icon, &title, &body, &actions, &hints, &expire);
	g_variant_lookup(hints, "urgency", "y", &urgency);
	g_variant_lookup(hints, "transient", "b", &transient);
	g_variant_lookup(hints, "category", "&s", &category);
	joined = g_strjoinv(",", (char **)actions);
	printf("notify %s %s replaces=%u title=%s body=%s actions=%s urgency=%u transient=%d category=%s "
	       "expire=%d timeout=%d\n",
	       dest, method, replaces, title, body, joined, urgency, transient, category, expire, timeout);
	if (notify_fails) {
		notify_fails = FALSE;
		g_set_error(error, G_DBUS_ERROR, G_DBUS_ERROR_SERVICE_UNKNOWN, "fake bus: no notification server");
		return NULL;
	}
	return g_variant_ref_sink(g_variant_new("(u)", replaces ? replaces : notify_next++));
}

static void fake_call(GDBusConnection *bus, const char *dest, const char *path, const char *iface,
		      const char *method, GVariant *params, const GVariantType *reply_type, GDBusCallFlags flags,
		      int timeout, GCancellable *cancellable, GAsyncReadyCallback callback, gpointer data)
{
	g_autoptr(GVariant) p = g_variant_ref_sink(params);
	g_autofree char *text = g_variant_print(p, FALSE);

	if ((gpointer)bus != FAKE) {
		g_dbus_connection_call(bus, dest, path, iface, method, p, reply_type, flags, timeout, cancellable,
				       callback, data);
		return;
	}
	printf("call %s %s %s\n", dest, method, text);
}

static GVariant *fake_cached_property(GDBusProxy *proxy, const char *name)
{
	const char *value;

	if ((gpointer)proxy != FAKE)
		return g_dbus_proxy_get_cached_property(proxy, name);
	value = g_hash_table_lookup(props, name);
	return value ? g_variant_ref_sink(g_variant_new_string(strcmp(value, "-") ? value : "")) : NULL;
}

static void fake_proxy_call(GDBusProxy *proxy, const char *method, GVariant *params, GDBusCallFlags flags,
			    int timeout, GCancellable *cancellable, GAsyncReadyCallback callback, gpointer data)
{
	g_autoptr(GVariant) p = g_variant_ref_sink(params);
	g_autofree char *text = g_variant_print(p, FALSE);

	if ((gpointer)proxy != FAKE) {
		g_dbus_proxy_call(proxy, method, p, flags, timeout, cancellable, callback, data);
		return;
	}
	printf("proxy %s %s\n", method, text);
	chosen_callback = callback;
}

static char chosen_answer[64];

static GVariant *fake_proxy_call_finish(GDBusProxy *proxy, GAsyncResult *result, GError **error)
{
	if (result)
		return g_dbus_proxy_call_finish(proxy, result, error);
	if (!strcmp(chosen_answer, "ok"))
		return g_variant_ref_sink(g_variant_new("()"));
	g_set_error(error, G_DBUS_ERROR, G_DBUS_ERROR_ACCESS_DENIED, "Unlock the phone first");
	return NULL;
}

#define g_dbus_connection_call_sync fake_call_sync
#define g_dbus_connection_call fake_call
#define g_dbus_proxy_get_cached_property fake_cached_property
#define g_dbus_proxy_call fake_proxy_call
#define g_dbus_proxy_call_finish fake_proxy_call_finish
#define main real_main
#include "plinius-usb-prompt.c"
#undef main
#undef g_dbus_connection_call_sync
#undef g_dbus_connection_call
#undef g_dbus_proxy_get_cached_property
#undef g_dbus_proxy_call
#undef g_dbus_proxy_call_finish

static const char *arg(const char *line, const char *keyword)
{
	return g_str_has_prefix(line, keyword) ? line + strlen(keyword) : NULL;
}

static void command(const char *line)
{
	char a[4096] = "", b[4096] = "";
	const char *x;

	printf("> %s\n", line);
	if (svc_command(line)) {
		/* done by the service */
	} else if ((x = arg(line, "prop ")) && sscanf(x, "%4095s %4095s", a, b) == 2) {
		if (strcmp(b, "nil"))
			g_hash_table_insert(props, g_strdup(a), g_strdup(b));
		else
			g_hash_table_remove(props, a);
	} else if (!strcmp(line, "start")) {
		/* What main() does before its loop */
		self.active_port = usb_string("ActivePort");
		update();
	} else if (!strcmp(line, "update")) {
		update();
	} else if (!strcmp(line, "close")) {
		close_question();
	} else if (!strcmp(line, "notify-fails")) {
		notify_fails = TRUE;
	} else if ((x = arg(line, "signal ActionInvoked ")) && sscanf(x, "%4095s %4095s", a, b) == 2) {
		on_notify_signal(NULL, ":1.2", NOTIFY_PATH, NOTIFY_INTERFACE, "ActionInvoked",
				 g_variant_new("(us)", (guint32)atoi(a), b), NULL);
	} else if ((x = arg(line, "signal NotificationClosed ")) && sscanf(x, "%4095s", a) == 1) {
		on_notify_signal(NULL, ":1.2", NOTIFY_PATH, NOTIFY_INTERFACE, "NotificationClosed",
				 g_variant_new("(uu)", (guint32)atoi(a), 2), NULL);
	} else if ((x = arg(line, "chosen ")) && sscanf(x, "%63s", chosen_answer) == 1) {
		chosen_callback(NULL, NULL, NULL);
	} else if (!strcmp(line, "state")) {
		printf("state question=%u active=%s\n", self.question_id, self.active_port ?: "-");
	} else if (!strcmp(line, "quit")) {
		raise(SIGTERM);
	} else if (*line) {
		printf("<unknown command: %.60s>\n", line);
	}
	if (daemon_mode)
		puts("done");
}

static gboolean on_stdin(GIOChannel *channel, GIOCondition condition, gpointer user_data)
{
	g_autofree char *line = NULL;

	if (g_io_channel_read_line(channel, &line, NULL, NULL, NULL) != G_IO_STATUS_NORMAL)
		return G_SOURCE_REMOVE;
	g_strchomp(line);
	command(line);
	return G_SOURCE_CONTINUE;
}

int main(int argc, char **argv)
{
	setvbuf(stdout, NULL, _IOLBF, 0);
	props = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, g_free);
	if (argc > 1 && !strcmp(argv[1], "daemon")) {
		GIOChannel *in = g_io_channel_unix_new(0);
		int r;

		daemon_mode = TRUE;
		if (argc > 2 && strcmp(argv[2], "-"))
			svc_start(argv + 2);
		g_io_add_watch(in, G_IO_IN | G_IO_HUP, on_stdin, NULL);
		r = real_main();
		svc_settle();
		printf("exit %d\n", r);
		return r;
	}
	self.session = FAKE;
	self.usb = FAKE;
	for (int i = 1; i < argc; i++)
		command(argv[i]);
	return 0;
}
