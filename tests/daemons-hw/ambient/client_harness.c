// The plinius-ambient client (plinius-ambient.c) as a whole, its main included, against the
// stand-ins for Wayland and EGL (fakewl.c), GL (fakegl.c) and the system bus (below). The
// arguments are a script: those before "run" are done before main (the settings it starts
// with), the others one after the other in its main loop, "wait:<ms>" letting the loop run.
//
// The system bus: every call is a line "dbus call <name> <method>", answered by the script
// ("reply:<method>:ok" or ":error"); "signal:<member>:<GVariant text>" is a signal arriving,
// "appeared" sensor-proxy-bridge (re)starting.
#define G_LOG_DOMAIN "plinius-ambient"
#include <gio/gio.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "fakewl.h"

void fakegl_report(void);

/* ---- The system bus ---- */

static int bus_token;

static GDBusConnection *fake_bus_get_sync(GBusType type, GCancellable *cancellable, GError **error)
{
	if (getenv("FAKE_NO_BUS")) {
		g_set_error_literal(error, G_IO_ERROR, G_IO_ERROR_NOT_FOUND, "no system bus");
		return NULL;
	}
	return (GDBusConnection *)&bus_token;
}

struct pending {
	char method[64];
	GAsyncReadyCallback callback;
	gpointer data;
};
static struct pending pending[256];
static int n_pending;

struct fake_result {
	gboolean ok;
	const char *reply;
	char method[64];
};

static void fake_call(GDBusConnection *bus, const char *name, const char *path, const char *iface, const char *method,
		      GVariant *params, const GVariantType *reply_type, GDBusCallFlags flags, int timeout,
		      GCancellable *cancellable, GAsyncReadyCallback callback, gpointer data)
{
	printf("dbus call %s %s %s\n", name, path, method);
	if (n_pending < 256) {
		snprintf(pending[n_pending].method, sizeof(pending[n_pending].method), "%s", method);
		pending[n_pending].callback = callback;
		pending[n_pending].data = data;
		n_pending++;
	}
}

static GVariant *fake_call_finish(GDBusConnection *bus, GAsyncResult *result, GError **error)
{
	struct fake_result *r = (struct fake_result *)result;
	if (!r->ok) {
		g_set_error(error, G_DBUS_ERROR, G_DBUS_ERROR_UNKNOWN_METHOD, "No such method %s", r->method);
		return NULL;
	}
	return g_variant_ref_sink(g_variant_new_parsed(r->reply ? r->reply : "()"));
}

static GVariant *fake_call_sync(GDBusConnection *bus, const char *name, const char *path, const char *iface,
				const char *method, GVariant *params, const GVariantType *reply_type,
				GDBusCallFlags flags, int timeout, GCancellable *cancellable, GError **error)
{
	printf("dbus sync %s %s\n", name, method);
	return NULL;
}

struct subscription {
	char member[64], sender[64], path[64];
	GDBusSignalCallback callback;
};
static struct subscription subscriptions[16];
static int n_subscriptions;

static guint fake_subscribe(GDBusConnection *bus, const char *sender, const char *iface, const char *member,
			    const char *path, const char *arg0, GDBusSignalFlags flags, GDBusSignalCallback callback,
			    gpointer data, GDestroyNotify free_data)
{
	struct subscription *s = &subscriptions[n_subscriptions++];
	snprintf(s->member, sizeof(s->member), "%s", member);
	snprintf(s->sender, sizeof(s->sender), "%s", sender);
	snprintf(s->path, sizeof(s->path), "%s", path);
	s->callback = callback;
	printf("dbus subscribe %s %s %s\n", sender, path, member);
	return n_subscriptions;
}

static GBusNameAppearedCallback appeared;

static guint fake_watch(GDBusConnection *bus, const char *name, GBusNameWatcherFlags flags,
			GBusNameAppearedCallback on_appeared, GBusNameVanishedCallback on_vanished, gpointer data,
			GDestroyNotify free_data)
{
	printf("dbus watch %s\n", name);
	appeared = on_appeared;
	return 1;
}

#define g_bus_get_sync fake_bus_get_sync
#define g_dbus_connection_call fake_call
#define g_dbus_connection_call_finish fake_call_finish
#define g_dbus_connection_call_sync fake_call_sync
#define g_dbus_connection_signal_subscribe fake_subscribe
#define g_bus_watch_name_on_connection fake_watch
#define main real_main
#include "plinius-ambient.c"
#undef main

/* ---- The script ---- */

static char **script;
static int script_len, script_pos;
static GSettings *settings, *background;

static void reply(const char *method, gboolean ok, const char *value)
{
	for (int i = 0; i < n_pending; i++) {
		if (strcmp(pending[i].method, method))
			continue;
		struct pending p = pending[i];
		memmove(&pending[i], &pending[i + 1], (n_pending - i - 1) * sizeof(pending[0]));
		n_pending--;
		struct fake_result r = {.ok = ok, .reply = value};
		snprintf(r.method, sizeof(r.method), "%s", method);
		p.callback(NULL, (GAsyncResult *)&r, p.data);
		return;
	}
	printf("no pending call %s\n", method);
}

static void state(void)
{
	printf("effect=%s\nconfigured=%d\nstalled=%d\nresting=%d\ndisplay_on=%d\ntold_visible=%d\n",
	       app.effect ? app.effect->name : "none", app.configured, app.stalled, app.resting, app.display_on,
	       app.told_visible);
	printf("gravity_claimed=%d\nmotion_claimed=%d\norientation_claimed=%d\norientation_sensor=%s\n",
	       app.gravity_claimed, app.motion_claimed, app.orientation_claimed, app.orientation_sensor);
	printf("gravity=%.4f,%.4f\nshake=%.4f\nimpulse=%.4f,%.4f\nhas_orientation=%d\norientation=%.3f,%.3f,%.3f,%.3f\n",
	       app.scene.gravity[0], app.scene.gravity[1], app.scene.shake, app.scene.impulse[0], app.scene.impulse[1],
	       app.scene.has_orientation, app.scene.orientation[0], app.scene.orientation[1], app.scene.orientation[2],
	       app.scene.orientation[3]);
	printf("size=%dx%d\nbuffer=%dx%d\nscale=%.4f\nscreen_angle=%d\ndensity=%d\nsurface=%d\nstate=%d\n", app.width,
	       app.height, app.buffer_width, app.buffer_height, app.scene.scale, app.scene.screen_angle,
	       app.scene.density, app.surface != NULL, app.state != NULL);
	printf("background=%d\n", app.effect && app.effect->render);
	printf("frame_callback=%d\ndraw_timer=%d\nstall_timer=%d\npicture=%s\nvideo=%s\n", app.frame_callback != NULL,
	       app.draw_timer != 0, app.stall_timer != 0, app.scene.picture ? app.scene.picture : "",
	       app.scene.video ? app.scene.video : "");
}

static void set_key(GSettings *s, const char *spec, gboolean boolean)
{
	char key[64];
	const char *value = strchr(spec, ':');
	snprintf(key, sizeof(key), "%.*s", (int)(value - spec), spec);
	if (boolean)
		g_settings_set_boolean(s, key, atoi(value + 1));
	else
		g_settings_set_string(s, key, value + 1);
}

/* One command; FALSE ends this turn of the loop for a while */
static gboolean command(const char *a, guint *wait)
{
	if (!strncmp(a, "set:", 4)) {
		set_key(settings, a + 4, FALSE);
	} else if (!strncmp(a, "setb:", 5)) {
		set_key(settings, a + 5, TRUE);
	} else if (!strncmp(a, "bg:", 3)) {
		if (background)
			set_key(background, a + 3, FALSE);
	} else if (!strncmp(a, "wait:", 5)) {
		*wait = atoi(a + 5);
		return FALSE;
	} else if (!strncmp(a, "configure:", 10)) {
		int w, h;
		sscanf(a + 10, "%dx%d", &w, &h);
		printf("configured=%d\n", fakewl_configure(w, h));
	} else if (!strcmp(a, "closed")) {
		fakewl_closed();
	} else if (!strcmp(a, "frame")) {
		printf("frame-done=%d\n", fakewl_frame_done());
	} else if (!strncmp(a, "output:", 7)) {
		int t, w, h, s;
		sscanf(a + 7, "%d,%d,%d,%d", &t, &w, &h, &s);
		fakewl_output_events(t, w, h, s);
	} else if (!strncmp(a, "reply:", 6)) {
		char method[64], how[16];
		const char *value = NULL;
		sscanf(a + 6, "%63[^:]:%15[^:]", method, how);
		const char *third = strchr(strchr(a + 6, ':') + 1, ':');
		if (third)
			value = third + 1;
		reply(method, !strcmp(how, "ok"), value);
	} else if (!strncmp(a, "signal:", 7)) {
		char member[64];
		sscanf(a + 7, "%63[^:]", member);
		const char *text = strchr(a + 7, ':') + 1;
		for (int i = 0; i < n_subscriptions; i++)
			if (!strcmp(subscriptions[i].member, member)) {
				GVariant *params = g_variant_ref_sink(g_variant_new_parsed(text));
				subscriptions[i].callback((GDBusConnection *)&bus_token, subscriptions[i].sender,
							  subscriptions[i].path, "iface", member, params, NULL);
				g_variant_unref(params);
			}
	} else if (!strcmp(a, "appeared")) {
		appeared((GDBusConnection *)&bus_token, GRAVITY_BUS_NAME, ":1.42", NULL);
	} else if (!strcmp(a, "readable")) {
		fakewl_readable();
	} else if (!strcmp(a, "hangup")) {
		fakewl_hangup();
	} else if (!strcmp(a, "queued")) {
		fakewl_queued = 1;
		fakewl_readable();
	} else if (!strcmp(a, "read-error")) {
		fakewl_read_error = 1;
		fakewl_readable();
	} else if (!strcmp(a, "dispatch-error")) {
		fakewl_dispatch_error = 1;
		fakewl_readable();
	} else if (!strcmp(a, "state")) {
		state();
	} else if (!strcmp(a, "pending")) {
		printf("pending=%d\n", fakewl_pending_frames());
	} else if (!strcmp(a, "report")) {
		fakegl_report();
		fakewl_report();
	} else if (!strcmp(a, "wake")) {
		/* scene.wake, as the video calls it for a decoded frame */
		app.scene.wake();
	} else if (!strcmp(a, "quit")) {
		raise(SIGTERM);
	} else {
		printf("unknown command %s\n", a);
	}
	return TRUE;
}

static gboolean run_script(gpointer data)
{
	while (script_pos < script_len) {
		guint wait = 0;
		const char *a = script[script_pos++];
		printf("> %s\n", a);
		if (!command(a, &wait)) {
			g_timeout_add(wait, run_script, NULL);
			return G_SOURCE_REMOVE;
		}
	}
	return G_SOURCE_REMOVE;
}

int main(int argc, char **argv)
{
	setvbuf(stdout, NULL, _IOLBF, 0);
	settings = g_settings_new("org.plinius.ambient");
	GSettingsSchemaSource *schemas = g_settings_schema_source_get_default();
	g_autoptr(GSettingsSchema) bg = schemas ? g_settings_schema_source_lookup(schemas, "org.gnome.desktop.background", TRUE)
						: NULL;
	if (bg)
		background = g_settings_new_full(bg, NULL, NULL);
	int i = 1;
	for (; i < argc && strcmp(argv[i], "run"); i++) {
		guint wait;
		command(argv[i], &wait);
	}
	script = argv + i + (i < argc);
	script_len = argc - i - (i < argc);
	g_idle_add(run_script, NULL);
	int status = real_main();
	printf("exit=%d\n", status);
	fakegl_report();
	fakewl_report();
	return status;
}
