# plinius-faced (the face unlock service) as it runs, without phone, camera or NPU: its startup
# (bus name, object, signal subscriptions, the sessions logind lists), how it follows sessions
# coming and going, the panel's power state and the always-on display, loading the models when a
# locked session may use them and dropping them (by executing itself again) when none does, the
# APU check, the idle exit, polkit's answers, Cancel and busy requests, and its failures.
# tests/daemons-security/test_dsec_face.py covers the decisions inside (which frames count, the
# thresholds, the failure limit); this file the service around them.
#
# The copy of plinius-faced.c has its paths in tmp_path (sandboxed_source). D-Bus is scripted:
# logind's answers come from "session" lines, asynchronous calls (polkit, UnlockSession) wait for
# a "reply", signals and timers are printed; GLib's timers are recorded and fired by the test,
# camera and recognition chain are stand-ins fed with scripted frames.

import functools
import os
import shutil

import pytest

from dhw_harness import PKG, build_harness, run, sandboxed_source

FACE = PKG / "plinius-face"
SRC = FACE / "plinius-faced.c"
TFLITE = PKG / "plinius-tflite"
DSEC_C = PKG.parent / "tests" / "daemons-security" / "c"

HARNESS = r"""
#define _GNU_SOURCE
#include <errno.h>
#include <glob.h>
#include <limits.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <gio/gio.h>
#include <glib-unix.h>
#include <glib/gstdio.h>

#include "dsec_fakebus.h"
#include "face-camera.h"
#include "face-pipeline.h"

#define FAKEBUS ((GDBusConnection *) &fakebus_dummy)
static int fakebus_dummy;
static GMutex out_lock;

static void G_GNUC_PRINTF (1, 2)
out (const char *fmt, ...)
{
	va_list args;

	g_mutex_lock (&out_lock);
	va_start (args, fmt);
	vprintf (fmt, args);
	va_end (args);
	putchar ('\n');
	g_mutex_unlock (&out_lock);
}

static void
log_handler (const char *domain, GLogLevelFlags level, const char *message, gpointer data)
{
	out ("%s: %s", level & G_LOG_LEVEL_CRITICAL ? "critical" : level & G_LOG_LEVEL_WARNING ? "warning" : "log",
	     message);
}

/* ---- plinius-tflite ---- */

void
pltfl_options_init (PlTflOptions *options)
{
	memset (options, 0, sizeof *options);
}

void
pltfl_exit (int status)
{
	out ("exit %d", status);
	fflush (stdout);
	exit (status);
}

/* ---- Logind as a table ---- */

enum { NOUSER = 1, NOSEAT = 2, NOREPLY = 4, NOCLASS = 8, NOLOCK = 16 };

typedef struct {
	char id[32], seat[32], cls[32], path[96];
	guint32 uid;
	int locked, flags;
} Session;

static Session sessions[8];
static int nsessions;
static gboolean list_fails;

static Session *
session_by_path (const char *path)
{
	for (int k = 0; k < nsessions; k++)
		if (!strcmp (sessions[k].path, path))
			return &sessions[k];
	return NULL;
}

static GVariant *
logind_call (const char *path, const char *method, GVariant *params, GError **error)
{
	Session *s = session_by_path (path);

	if (!strcmp (method, "ListSessions") && !list_fails) {
		GVariantBuilder b;

		g_variant_builder_init (&b, G_VARIANT_TYPE ("a(susso)"));
		for (int k = 0; k < nsessions; k++)
			g_variant_builder_add (&b, "(susso)", sessions[k].id, sessions[k].uid, "user",
					       sessions[k].seat, sessions[k].path);
		return g_variant_ref_sink (g_variant_new ("(a(susso))", &b));
	}
	if (s && !strcmp (method, "Get")) {
		const char *iface, *prop;

		g_variant_get (params, "(&s&s)", &iface, &prop);
		if (!strcmp (prop, "Class") && !(s->flags & NOCLASS))
			return g_variant_ref_sink (g_variant_new ("(v)", g_variant_new_string (s->cls)));
		if (!strcmp (prop, "LockedHint") && !(s->flags & NOLOCK))
			return g_variant_ref_sink (g_variant_new ("(v)", g_variant_new_boolean (s->locked)));
	}
	if (s && !strcmp (method, "GetAll") && !(s->flags & NOREPLY)) {
		GVariantBuilder b;

		g_variant_builder_init (&b, G_VARIANT_TYPE ("a{sv}"));
		if (!(s->flags & NOUSER))
			g_variant_builder_add (&b, "{sv}", "User",
					       g_variant_new ("(uo)", s->uid, "/org/freedesktop/login1/user/_1"));
		if (!(s->flags & NOSEAT))
			g_variant_builder_add (&b, "{sv}", "Seat",
					       g_variant_new ("(so)", s->seat, "/org/freedesktop/login1/seat/seat0"));
		g_variant_builder_add (&b, "{sv}", "Class", g_variant_new_string (s->cls));
		return g_variant_ref_sink (g_variant_new ("(a{sv})", &b));
	}
	g_set_error (error, G_DBUS_ERROR, G_DBUS_ERROR_UNKNOWN_OBJECT, "fake logind: %s %s", method, path);
	return NULL;
}

static GVariant *
my_call_sync (GDBusConnection *bus, const char *dest, const char *path, const char *iface,
	      const char *method, GVariant *params, const GVariantType *reply_type,
	      GDBusCallFlags flags, int timeout, GCancellable *cancellable, GError **error)
{
	if (!strcmp (dest, "org.freedesktop.login1")) {
		g_autoptr (GVariant) p = params ? g_variant_ref_sink (params) : NULL;

		return logind_call (path, method, p, error);
	}
	return fake_call_sync (bus, dest, path, iface, method, params, reply_type, flags, timeout,
			       cancellable, error);
}

/* ---- Asynchronous calls: answered by "reply N ..." ---- */

typedef struct {
	char *method;
	GVariantType *reply_type;
	GAsyncReadyCallback callback;
	gpointer data;
	GVariant *reply;
	char *error;
} Call;

static GPtrArray *calls;

static void
my_call (GDBusConnection *bus, const char *dest, const char *path, const char *iface, const char *method,
	 GVariant *params, const GVariantType *reply_type, GDBusCallFlags flags, int timeout,
	 GCancellable *cancellable, GAsyncReadyCallback callback, gpointer data)
{
	Call *call = g_new0 (Call, 1);
	g_autoptr (GVariant) p = params ? g_variant_ref_sink (params) : NULL;
	g_autofree char *text = p ? g_variant_print (p, TRUE) : g_strdup ("-");

	call->method = g_strdup (method);
	call->reply_type = reply_type ? g_variant_type_copy (reply_type) : NULL;
	call->callback = callback;
	call->data = data;
	if (!calls)
		calls = g_ptr_array_new ();
	g_ptr_array_add (calls, call);
	out ("call %u %s %s %s timeout=%s", calls->len, dest, method, text,
	     timeout == G_MAXINT ? "max" : timeout == -1 ? "default" : "other");
}

static GVariant *
my_call_finish (GDBusConnection *bus, GAsyncResult *res, GError **error)
{
	Call *call = (Call *) res;

	if (call->error) {
		g_set_error_literal (error, G_DBUS_ERROR, G_DBUS_ERROR_FAILED, call->error);
		return NULL;
	}
	return g_variant_ref (call->reply);
}

static gboolean
my_emit (GDBusConnection *bus, const char *dest, const char *path, const char *iface, const char *name,
	 GVariant *params, GError **error)
{
	g_autoptr (GVariant) p = g_variant_ref_sink (params);
	g_autofree char *text = g_variant_print (p, TRUE);

	out ("signal %s %s %s %s", path, iface, name, text);
	return TRUE;
}

/* ---- Subscriptions, object, name, Unix signals ---- */

typedef struct {
	char *member;
	GDBusSignalCallback callback;
	gpointer data;
} Subscription;

static Subscription subscriptions[8];
static int nsubscriptions;

static guint
my_subscribe (GDBusConnection *bus, const char *sender, const char *iface, const char *member,
	      const char *path, const char *arg0, GDBusSignalFlags flags, GDBusSignalCallback callback,
	      gpointer data, GDestroyNotify free_data)
{
	subscriptions[nsubscriptions] = (Subscription) { g_strdup (member), callback, data };
	out ("subscribe %s %s %s %s %s", sender, iface, member, path ? path : "-", arg0 ? arg0 : "-");
	return ++nsubscriptions;
}

static const GDBusInterfaceVTable *registered_vtable;

static guint
my_register (GDBusConnection *bus, const char *path, GDBusInterfaceInfo *info,
	     const GDBusInterfaceVTable *vtable, gpointer data, GDestroyNotify free_data, GError **error)
{
	GString *methods = g_string_new (NULL);

	for (GDBusMethodInfo **m = info->methods; *m; m++)
		g_string_append_printf (methods, "%s%s", methods->len ? "," : "", (*m)->name);
	for (GDBusSignalInfo **s = info->signals; *s; s++)
		g_string_append_printf (methods, ",signal:%s", (*s)->name);
	out ("register %s %s %s", path, info->name, methods->str);
	g_string_free (methods, TRUE);
	registered_vtable = vtable;
	return 1;
}

static GBusAcquiredCallback own_acquired;
static GBusNameLostCallback own_lost;

static guint
my_own_name (GBusType type, const char *name, GBusNameOwnerFlags flags, GBusAcquiredCallback acquired,
	     GBusNameAcquiredCallback name_acquired, GBusNameLostCallback lost, gpointer data,
	     GDestroyNotify free_data)
{
	out ("own_name %s %s", type == G_BUS_TYPE_SYSTEM ? "system" : "other", name);
	own_acquired = acquired;
	own_lost = lost;
	return 1;
}

static GSourceFunc unix_handlers[65];

static guint
my_unix_signal_add (int signum, GSourceFunc handler, gpointer data)
{
	out ("unix_signal %d", signum);
	unix_handlers[signum] = handler;
	return 1;
}

static void commands (void);

static void
my_loop_run (GMainLoop *loop)
{
	commands ();
}

/* ---- Timers ---- */

typedef struct {
	guint ms;
	GSourceFunc fn;
	gpointer data;
	gboolean active;
} Timer;

static Timer timers[4096];
static guint ntimers;
static const char *fn_name (GSourceFunc fn);

static guint
my_timeout_add (guint ms, GSourceFunc fn, gpointer data)
{
	g_assert (ntimers < G_N_ELEMENTS (timers));
	timers[ntimers] = (Timer) { ms, fn, data, TRUE };
	out ("timer+ %s %ums", fn_name (fn), ms);
	return ++ntimers;
}

static guint
my_timeout_add_seconds (guint s, GSourceFunc fn, gpointer data)
{
	return my_timeout_add (s * 1000, fn, data);
}

static gboolean
my_source_remove (guint id)
{
	g_assert (id >= 1 && id <= ntimers && timers[id - 1].active);
	timers[id - 1].active = FALSE;
	out ("timer- %s", fn_name (timers[id - 1].fn));
	return TRUE;
}

/* ---- Leaving the process ---- */

static gboolean exec_fails;

static int
my_close_range (unsigned first, unsigned last, int flags)
{
	out ("close_range %u %s", first, last == UINT_MAX ? "max" : "other");
	return 0;
}

static int
my_execl (const char *path, const char *arg, ...)
{
	out ("exec %s %s", path, arg);
	if (exec_fails)
		return -1;
	fflush (stdout);
	exit (0);
}

static G_NORETURN void
my__exit (int status)
{
	out ("_exit %d", status);
	fflush (stdout);
	exit (status);
}

/* ---- Clock: a job's time is up at once when the frames run out ---- */

static GMutex frames_lock;
static GCond frames_cond;
static gint64 clock_offset;
static gboolean blocked, released;

static gint64
fake_monotonic (void)
{
	gint64 offset;

	g_mutex_lock (&frames_lock);
	offset = clock_offset;
	g_mutex_unlock (&frames_lock);
	return g_get_monotonic_time () + offset;
}

/* The worker stops here until "release" */
static void
block_worker (void)
{
	g_mutex_lock (&frames_lock);
	blocked = TRUE;
	g_cond_broadcast (&frames_cond);
	while (!released)
		g_cond_wait (&frames_cond, &frames_lock);
	released = FALSE;
	blocked = FALSE;
	g_mutex_unlock (&frames_lock);
}

/* ---- Camera and recognition chain ---- */

static GQueue frames = G_QUEUE_INIT;
static gboolean models_fail, models_block, camera_fails;

struct FaceCamera {
	int unused;
};

struct FacePipeline {
	int unused;
};

FaceCamera *
face_camera_open (char **error)
{
	if (camera_fails) {
		*error = strdup ("no camera here");
		return NULL;
	}
	out ("camera open");
	return g_new0 (FaceCamera, 1);
}

void
face_camera_close (FaceCamera *camera)
{
	out ("camera close");
	g_free (camera);
}

void
face_camera_format (FaceCamera *camera, int *width, int *height, int *orientation, size_t *bytes)
{
	*width = 1280;
	*height = 720;
	*orientation = 270;
	*bytes = 16;
}

static char *current_frame;

int
face_camera_next_frame (FaceCamera *camera, uint8_t *dst, int timeout_ms, int64_t *timestamp_ns)
{
	g_mutex_lock (&frames_lock);
	g_free (current_frame);
	current_frame = g_queue_pop_head (&frames);
	if (!current_frame)
		clock_offset += (gint64) 3600 * G_USEC_PER_SEC;
	g_mutex_unlock (&frames_lock);
	if (!current_frame)
		return 0;
	if (!strcmp (current_frame, "w"))
		block_worker ();
	return strcmp (current_frame, "c") ? 1 : -1;
}

FacePipeline *
face_pipeline_new (const char *model_dir, const PlTflOptions *options, char **error)
{
	out ("pipeline_new %s accel=%s fp16=%d cache=%s", model_dir,
	     options->accelerator ? options->accelerator : "-", options->allow_fp16, options->cache_dir);
	static int loads;

	/* A service that loads again and again would never let the test end */
	if (++loads == 50) {
		out ("endless model loading");
		fflush (stdout);
		exit (0);
	}
	if (models_block)
		block_worker ();
	if (models_fail) {
		*error = strdup ("no models here");
		return NULL;
	}
	return g_new0 (FacePipeline, 1);
}

void
face_pipeline_free (FacePipeline *pipeline)
{
	g_free (pipeline);
}

const char *
face_pipeline_describe (FacePipeline *pipeline)
{
	return "stub";
}

float
face_similarity (const float *a, const float *b)
{
	float dot = 0;

	for (int v = 0; v < FACE_EMBEDDING_SIZE; v++)
		dot += a[v] * b[v];
	return dot;
}

/* g: a clear face of similarity 0.9 to the templates, n: no face, w: none after waiting */
int
face_pipeline_run (FacePipeline *pipeline, const FaceFrame *frame, FaceResult *r)
{
	const char *spec = current_frame ? current_frame : "n";

	memset (r, 0, sizeof *r);
	if (!strcmp (spec, "e"))
		return -1;
	if (strcmp (spec, "g"))
		return 0;
	r->score = r->presence = 0.95f;
	r->keypoints[0] = (FacePoint) { 300, 500 };
	r->keypoints[1] = (FacePoint) { 400, 500 };
	r->embedding[0] = 0.9f;
	r->embedding[1] = sqrtf (1 - 0.81f);
	return 1;
}

#undef g_dbus_connection_call_sync
#undef g_dbus_connection_call
#undef g_dbus_connection_emit_signal
#define g_dbus_connection_call_sync my_call_sync
#define g_dbus_connection_call my_call
#define g_dbus_connection_call_finish my_call_finish
#define g_dbus_connection_emit_signal my_emit
#define g_dbus_connection_signal_subscribe my_subscribe
#define g_dbus_connection_register_object my_register
#define g_bus_own_name my_own_name
#define g_unix_signal_add my_unix_signal_add
#define g_main_loop_run my_loop_run
#define g_timeout_add my_timeout_add
#define g_timeout_add_seconds my_timeout_add_seconds
#define g_source_remove my_source_remove
#define g_get_monotonic_time fake_monotonic
#define close_range my_close_range
#define execl my_execl
#define _exit my__exit
#define main faced_main
#include "plinius-faced.c"
#undef main
#undef g_get_monotonic_time
#undef g_source_remove
#undef g_timeout_add

static const char *
fn_name (GSourceFunc fn)
{
	return fn == on_screen_poll ? "on_screen_poll" : fn == on_release_due ? "on_release_due" :
	       fn == on_idle_exit ? "on_idle_exit" : fn == on_models_retry ? "on_models_retry" : "?";
}

/* ---- Commands ---- */

static void
wait_job (void)
{
	while (TRUE) {
		gboolean busy;

		g_mutex_lock (&self.lock);
		busy = self.job != NULL;
		g_mutex_unlock (&self.lock);
		if (!busy)
			break;
		g_main_context_iteration (NULL, TRUE);
	}
	while (g_main_context_iteration (NULL, FALSE))
		;
}

static int
compare_watches (gconstpointer a, gconstpointer b)
{
	return strcmp ((*(Watch **) a)->id, (*(Watch **) b)->id);
}

static void
print_watches (void)
{
	g_autoptr (GPtrArray) list = g_ptr_array_new ();
	GHashTableIter iter;
	gpointer value;

	g_hash_table_iter_init (&iter, self.sessions);
	while (g_hash_table_iter_next (&iter, NULL, &value))
		g_ptr_array_add (list, value);
	g_ptr_array_sort (list, compare_watches);
	for (guint k = 0; k < list->len; k++) {
		Watch *w = list->pdata[k];

		out ("watch %s uid=%u locked=%d pin_seen=%d by_face=%d failures=%d", w->id, (unsigned) w->uid,
		     w->locked, w->pin_seen, w->by_face, w->failures);
	}
	out ("watches %u", list->len);
}

static void
dispatch (const char *member, const char *path, GVariant *params)
{
	g_autoptr (GVariant) p = g_variant_ref_sink (params);

	for (int k = 0; k < nsubscriptions; k++)
		if (!strcmp (subscriptions[k].member, member))
			subscriptions[k].callback (FAKEBUS, LOGIND, path, "org.freedesktop.DBus.Properties",
						   member, p, subscriptions[k].data);
}

static char *
session_path (const char *id)
{
	return g_strdup_printf ("/org/freedesktop/login1/session/%s", id);
}

static FakeInv *last_inv;

static void
commands (void)
{
	static char line[4096];

	while (fgets (line, sizeof line, stdin)) {
		char a[1024] = "", b[1024] = "", c[64] = "";
		long n1, n2, n3;

		line[strcspn (line, "\n")] = '\0';
		if (fb_command (line))
			continue;
		if (!strcmp (line, "main")) {
			faced_main ();
		} else if (!strcmp (line, "own")) {
			own_acquired (FAKEBUS, BUS_NAME, NULL);
		} else if (!strcmp (line, "lost")) {
			own_lost (FAKEBUS, BUS_NAME, NULL);
		} else if (sscanf (line, "unix %ld", &n1) == 1) {
			unix_handlers[n1] (NULL);
		} else if (sscanf (line, "session %31s %ld %31s %31s %ld %63s", a, &n1, b, c, &n2, a + 64) >= 5) {
			Session *s = &sessions[nsessions++];

			snprintf (s->id, sizeof s->id, "%s", a);
			snprintf (s->seat, sizeof s->seat, "%s", strcmp (b, "-") ? b : "");
			snprintf (s->cls, sizeof s->cls, "%s", c);
			g_autofree char *path = session_path (a);
			snprintf (s->path, sizeof s->path, "%s", path);
			s->uid = (guint32) n1;
			s->locked = (int) n2;
			s->flags = (strstr (a + 64, "nouser") ? NOUSER : 0) | (strstr (a + 64, "noseat") ? NOSEAT : 0) |
				   (strstr (a + 64, "noreply") ? NOREPLY : 0) | (strstr (a + 64, "noclass") ? NOCLASS : 0) |
				   (strstr (a + 64, "nolock") ? NOLOCK : 0);
		} else if (!strcmp (line, "listfail")) {
			list_fails = TRUE;
		} else if (!strncmp (line, "frames", 6)) {
			g_auto (GStrv) parts = g_strsplit (line + 6, " ", -1);

			g_mutex_lock (&frames_lock);
			g_queue_clear_full (&frames, g_free);
			for (char **p = parts; *p; p++)
				if (**p)
					g_queue_push_tail (&frames, g_strdup (*p));
			g_mutex_unlock (&frames_lock);
		} else if (sscanf (line, "models %1023s", a) == 1) {
			models_fail = !strcmp (a, "fail");
			models_block = !strcmp (a, "block");
		} else if (sscanf (line, "camera %1023s", a) == 1) {
			camera_fails = !strcmp (a, "fail");
		} else if (!strcmp (line, "execfails")) {
			exec_fails = TRUE;
		} else if (sscanf (line, "settings %ld %ld %ld", &n1, &n2, &n3) == 3) {
			Settings s = { n2 != 0, n3 != 0 };

			save_settings ((uid_t) n1, &s, NULL);
		} else if (sscanf (line, "templates %ld %ld", &n1, &n2) == 2) {
			static float t[MAX_TEMPLATES][FACE_EMBEDDING_SIZE];

			for (int i = 0; i < n2; i++)
				t[i][0] = 1;
			save_templates ((uid_t) n1, t, (int) n2, NULL);
		} else if (sscanf (line, "method %1023s %1023s", a, b) >= 1 && !strncmp (line, "method ", 7)) {
			FakeInv *inv = g_new0 (FakeInv, 1);
			GVariant *params = NULL;

			if (!strcmp (a, "SetEnabled"))
				params = g_variant_new ("(b)", !strcmp (b, "true"));
			else if (!strcmp (a, "SetStrictness"))
				params = g_variant_new ("(s)", b);
			fake_inv_reset (inv, g_intern_string (a), params);
			last_inv = inv;
			registered_vtable->method_call (FAKEBUS, ":1.5", OBJECT_PATH, INTERFACE, a, inv->params,
							(GDBusMethodInvocation *) inv, NULL);
			fake_inv_print (inv);
		} else if (!strcmp (line, "inv")) {
			fake_inv_print (last_inv);
		} else if (sscanf (line, "reply %ld error %1023[^\n]", &n1, a) == 2) {
			Call *call = calls->pdata[n1 - 1];

			call->error = g_strdup (a);
			call->callback (NULL, (GAsyncResult *) call, call->data);
		} else if (sscanf (line, "reply %ld %1023[^\n]", &n1, a) == 2) {
			Call *call = calls->pdata[n1 - 1];
			g_autoptr (GError) error = NULL;

			call->reply = g_variant_parse (call->reply_type, a, NULL, NULL, &error);
			g_assert_no_error (error);
			call->callback (NULL, (GAsyncResult *) call, call->data);
		} else if (!strcmp (line, "wait")) {
			wait_job ();
		} else if (!strcmp (line, "blocked")) {
			gint64 until = g_get_monotonic_time () + 10 * G_USEC_PER_SEC;
			gboolean ok = TRUE;

			g_mutex_lock (&frames_lock);
			while (!blocked && ok)
				ok = g_cond_wait_until (&frames_cond, &frames_lock, until);
			g_mutex_unlock (&frames_lock);
			out ("%s", ok ? "worker waits" : "worker never came");
		} else if (!strcmp (line, "release")) {
			g_mutex_lock (&frames_lock);
			released = TRUE;
			g_cond_broadcast (&frames_cond);
			g_mutex_unlock (&frames_lock);
		} else if (sscanf (line, "fire %1023s", a) == 1) {
			guint k;

			for (k = ntimers; k > 0; k--)
				if (timers[k - 1].active && !strcmp (fn_name (timers[k - 1].fn), a))
					break;
			if (!k) {
				out ("no timer %s", a);
				continue;
			}
			if (!timers[k - 1].fn (timers[k - 1].data))
				timers[k - 1].active = FALSE;
			out ("fired %s", a);
		} else if (!strcmp (line, "timers")) {
			GString *list = g_string_new ("timers");

			for (guint k = 0; k < ntimers; k++)
				if (timers[k].active)
					g_string_append_printf (list, " %s", fn_name (timers[k].fn));
			out ("%s", list->str);
			g_string_free (list, TRUE);
		} else if (!strcmp (line, "watches")) {
			print_watches ();
		} else if (!strcmp (line, "state")) {
			g_mutex_lock (&self.lock);
			out ("state job=%s models=%d pending=%d screen=%d", self.job ? "busy" : "none",
			     self.pipeline != NULL, self.unlock_pending, self.screen_on);
			g_mutex_unlock (&self.lock);
		} else if (sscanf (line, "lockhint %1023s %ld", a, &n1) == 2) {
			g_autofree char *path = session_path (a);
			GVariantBuilder changed;

			g_variant_builder_init (&changed, G_VARIANT_TYPE ("a{sv}"));
			g_variant_builder_add (&changed, "{sv}", "LockedHint", g_variant_new_boolean (n1 != 0));
			dispatch ("PropertiesChanged", path,
				  g_variant_new ("(sa{sv}as)", "org.freedesktop.login1.Session", &changed, NULL));
		} else if (sscanf (line, "otherprops %1023s %1023s", a, b) == 2) {
			/* A change of another interface ("iface"), or without LockedHint ("nohint") */
			g_autofree char *path = session_path (a);
			GVariantBuilder changed;

			g_variant_builder_init (&changed, G_VARIANT_TYPE ("a{sv}"));
			g_variant_builder_add (&changed, "{sv}", strcmp (b, "nohint") ? "LockedHint" : "Active",
					       g_variant_new_boolean (FALSE));
			dispatch ("PropertiesChanged", path,
				  g_variant_new ("(sa{sv}as)", strcmp (b, "iface") ? "org.freedesktop.login1.Session" :
						 "org.freedesktop.login1.User", &changed, NULL));
		} else if (sscanf (line, "new %1023s", a) == 1) {
			g_autofree char *path = session_path (a);

			dispatch ("SessionNew", LOGIND_PATH, g_variant_new ("(so)", a, path));
		} else if (sscanf (line, "removed %1023s", a) == 1) {
			g_autofree char *path = session_path (a);

			dispatch ("SessionRemoved", LOGIND_PATH, g_variant_new ("(so)", a, path));
		} else if (sscanf (line, "start %1023s %ld %1023s", a, &n1, b) >= 2 && !strncmp (line, "start ", 6)) {
			JobKind kind = !strcmp (a, "enroll") ? JOB_ENROLL : !strcmp (a, "unlock") ? JOB_UNLOCK :
				       JOB_PRELOAD;

			out ("%s", start_job (kind, (uid_t) n1, NULL, b[0] ? b : NULL) ? "started" : "busy");
		} else if (sscanf (line, "write %1023s %1023[^\n]", a, b) == 2 ||
			   sscanf (line, "write %1023s", a) == 1) {
			/* Files of the test's directory only */
			g_assert (g_str_has_prefix (a, getenv ("FACE_ROOT")));
			g_autofree char *dir = g_path_get_dirname (a);
			g_mkdir_with_parents (dir, 0755);
			g_file_set_contents (a, b, -1, NULL);
		} else if (sscanf (line, "rmdir %1023s", a) == 1) {
			g_assert (g_str_has_prefix (a, getenv ("FACE_ROOT")));
			g_rmdir (a);
		} else if (sscanf (line, "rm %1023s", a) == 1) {
			g_assert (g_str_has_prefix (a, getenv ("FACE_ROOT")));
			g_unlink (a);
		} else if (!strcmp (line, "try")) {
			try_unlock ();
		} else if (!strcmp (line, "sync")) {
			sync_models ();
		} else {
			out ("<unknown command: %.60s>", line);
		}
	}
}

int
main (void)
{
	setvbuf (stdout, NULL, _IOLBF, 0);
	g_log_set_default_handler (log_handler, NULL);
	commands ();
	out ("end of commands");
	return 0;
}
"""

PATHS = {
    "/var/lib/plinius-face": "state",
    "/usr/share/plinius-face": "models",
    "/var/cache/plinius-face": "cache",
    "/run/plinius-face/sessions": "run/sessions",
    "/sys/class/drm/card*-DSI-*/dpms": "drm/card*-DSI-*/dpms",
    "/run/user/*/plinius-aod": "user/*/plinius-aod",
    "/sys/class/remoteproc": "remoteproc",
    "/proc/self/exe": "self-exe",
}


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    d = tmp_path_factory.mktemp("faced")
    sandboxed_source(SRC, d, [(old, d / new) for old, new in PATHS.items()])
    exe = build_harness(d, "faced", HARNESS, pkgs=("gio-unix-2.0",),
                        flags=[f"-I{FACE}", f"-I{TFLITE}", f"-I{DSEC_C}", "-Wno-unused-parameter"])
    return d, exe


@pytest.fixture
def faced(built):
    d, exe = built
    for sub in ("state", "run", "drm", "user", "remoteproc", "cache", "models"):
        if (d / sub).is_file():
            (d / sub).unlink()
        shutil.rmtree(d / sub, ignore_errors=True)
    (d / "self-exe").unlink(missing_ok=True)
    (d / "state").mkdir()
    (d / "run").mkdir()
    return d, functools.partial(script, exe, d)


def script(exe, d, *cmds, start=("main", "own"), check=True):
    """Runs the commands after the service's own start (main, then the bus name is acquired);
    returns the output lines (and the exit code with check=False)."""
    f = d / "cmds"
    f.write_text("\n".join([*start, *cmds]) + "\n")
    r = run("sh", "-c", 'exec "$0" < "$1"', exe, f, check=False,
            env={**os.environ, "FACE_ROOT": str(d) + "/"})
    assert r.returncode in (0, 1), f"exit {r.returncode}\n{r.stdout}\n{r.stderr}"
    lines = r.stdout.splitlines()
    assert not [line for line in lines if line.startswith(("critical", "<unknown"))], r.stdout
    if check:
        assert r.returncode == 0, r.stdout
        return lines
    return lines, r.returncode


def only(lines, *prefixes):
    return [line for line in lines if line.startswith(prefixes)]


def dpms(d, state):
    p = d / "drm" / "card0-DSI-1" / "dpms"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(state)


def enrolled(uid=1001, enabled=1, strict=1):
    return [f"settings {uid} {enabled} {strict}", f"templates {uid} 3"]


SESSION = "/org/freedesktop/login1/session/"
POLKIT_YES = "((true, false, {}),)"
POLKIT_NO = "((false, true, {}),)"


# ---- Startup ----

def test_startup_wiring(faced):
    d, sh = faced
    out = sh()
    assert out[:3] == ["unix_signal 15", "unix_signal 2", "own_name system org.plinius.Face"]
    assert out[3] == "timer+ on_idle_exit 120000ms"
    assert out[4] == ("register /org/plinius/Face org.plinius.Face1 GetUser,SetEnabled,"
                      "SetStrictness,Enroll,Cancel,Delete,Verify,signal:EnrollProgress,"
                      "signal:EnrollFinished")
    assert out[5:8] == [
        "subscribe org.freedesktop.login1 org.freedesktop.DBus.Properties PropertiesChanged - "
        "org.freedesktop.login1.Session",
        "subscribe org.freedesktop.login1 org.freedesktop.login1.Manager SessionNew "
        "/org/freedesktop/login1 -",
        "subscribe org.freedesktop.login1 org.freedesktop.login1.Manager SessionRemoved "
        "/org/freedesktop/login1 -",
    ]
    # No session may use the face: the panel is not read
    assert not only(out, "timer+ on_screen_poll")
    # The main loop ended (end of the script): the process ends through pltfl_exit
    assert out[-1] == "exit 0"


def test_exit_on_signal_and_lost_name(faced):
    d, sh = faced
    out, code = sh("unix 15", "never reached", check=False)
    assert (out[-1], code) == ("exit 0", 0)
    out, code = sh("unix 2", check=False)
    assert (out[-1], code) == ("exit 0", 0)
    out, code = sh("lost", check=False)
    assert out[-2:] == ["warning: lost the bus name org.plinius.Face", "exit 1"]
    assert code == 1


def test_sessions_at_startup(faced):
    d, sh = faced
    out = sh("session s1 1001 seat0 user 1", "session s2 1002 - user 1",
             "session s3 1003 seat0 greeter 0", "session s4 1004 seat0 user 0 noclass",
             "session s5 1005 seat0 user 1 nolock", "session s6 1006 seat0 user 0",
             "main", "own", "watches", start=())
    # No seat (an ssh login), not a user session, no answer: not watched; LockedHint without an
    # answer counts as unlocked
    assert only(out, "watch") == [
        "watch s1 uid=1001 locked=1 pin_seen=0 by_face=0 failures=0",
        "watch s5 uid=1005 locked=0 pin_seen=0 by_face=0 failures=0",
        "watch s6 uid=1006 locked=0 pin_seen=0 by_face=0 failures=0",
        "watches 3",
    ]
    assert "log: watching session s1 of uid 1001 (locked)" in out
    assert "log: watching session s6 of uid 1006 (unlocked)" in out


def test_no_session_list(faced):
    d, sh = faced
    out = sh("session s1 1001 seat0 user 1", "listfail", "main", "own", "watches", start=())
    assert only(out, "watch") == ["watches 0"]


def test_what_the_old_process_knew_survives_the_re_exec_once(faced):
    d, sh = faced
    (d / "run" / "sessions").write_text("s1 1 4\n")
    out = sh("session s1 1001 seat0 user 0", "main", "own", "watches", start=())
    assert only(out, "watch s1") == ["watch s1 uid=1001 locked=0 pin_seen=1 by_face=0 failures=4"]
    # Read once, then gone: after a crash the face waits for a PIN again
    assert not (d / "run" / "sessions").exists()


# ---- Sessions coming and going ----

def test_new_and_removed_sessions(faced):
    d, sh = faced
    out = sh("session s7 1007 seat0 user 1", "session s8 1008 seat0 user 1 nouser",
             "session s9 1009 seat0 user 1 noseat", "session s10 1010 seat0 user 1 noreply",
             "session s11 1011 - user 1",
             "listfail", "main", "own",
             "new s7", "new s7", "new s8", "new s9", "new s10", "new s11", "new s12", "watches",
             "removed s7", "removed s12", "watches", start=())
    assert only(out, "watch") == [
        # Announced twice, watched once; without User, Seat, an answer or a seat: not at all
        "watch s7 uid=1007 locked=1 pin_seen=0 by_face=0 failures=0", "watches 1",
        "watches 0",
    ]


def test_property_changes_that_are_not_the_lock(faced):
    d, sh = faced
    out = sh("session s1 1001 seat0 user 1", "main", "own", "otherprops s1 iface",
             "otherprops s1 nohint", "lockhint s1 1", "lockhint nobody 0", "watches", start=())
    # Another interface, no LockedHint, no change, an unknown session: all the same as before
    assert only(out, "watch s1") == ["watch s1 uid=1001 locked=1 pin_seen=0 by_face=0 failures=0"]
    assert not only(out, "log: session")


# ---- Screen and models ----

def locked_and_ready(d, failures=0):
    """Session s1 of uid 1001, locked, face unlock on, a PIN unlock seen before the re-exec"""
    (d / "run" / "sessions").write_text(f"s1 1 {failures}\n")
    return ["session s1 1001 seat0 user 1", *enrolled(), "main", "own"]


def test_models_load_when_a_locked_session_may_use_them(faced):
    d, sh = faced
    out = sh(*locked_and_ready(d), "wait", "state", start=())
    assert f"pipeline_new {d}/models accel=- fp16=1 cache={d}/cache" in out
    assert "log: the APU is not running, models on the CPU" in out
    assert "log: models loaded: stub" in out
    assert "state job=none models=1 pending=0 screen=0" in out
    # Loading is a job: the idle exit waits for its end
    assert out.index("timer- on_idle_exit") < out.index("log: models loaded: stub")


@pytest.mark.parametrize("names,accel", [
    ({"remoteproc0": "scp\n", "remoteproc1": "apusys-rv\n"}, "mtk-neuron"),
    ({"remoteproc0": "scp\n", "remoteproc1": None}, "-"),
    ({}, "-"),
])
def test_npu_only_with_the_apu_running(faced, names, accel):
    d, sh = faced
    (d / "remoteproc").mkdir()
    for entry, name in names.items():
        (d / "remoteproc" / entry).mkdir()
        if name is not None:
            (d / "remoteproc" / entry / "name").write_text(name)
    out = sh(*locked_and_ready(d), "wait", start=())
    assert only(out, "pipeline_new")[0].split()[2] == f"accel={accel}"


def test_models_that_do_not_load_are_not_loaded_again_and_again(faced):
    d, sh = faced
    out = sh("models fail", *locked_and_ready(d), "wait", "state", "timers",
             "fire on_models_retry", "wait", "state", "timers", start=())
    assert "endless model loading" not in out
    assert only(out, "warning") == ["warning: models: no models here"] * 2
    # One try, then the next one only when the pause is over (and a pause again after that)
    assert len(only(out, "pipeline_new")) == 2
    assert only(out, "state") == ["state job=none models=0 pending=0 screen=0"] * 2
    assert [t for t in only(out, "timers") if "on_models_retry" in t] and \
        all(t.split().count("on_models_retry") == 1 for t in only(out, "timers"))


def test_models_that_load_on_the_retry_end_the_pauses(faced):
    d, sh = faced
    out = sh("models fail", *locked_and_ready(d), "wait", "models ok", "fire on_models_retry", "wait",
             "state", "timers", start=())
    assert len(only(out, "pipeline_new")) == 2
    assert "state job=none models=1 pending=0 screen=0" in out
    assert "on_models_retry" not in only(out, "timers")[-1]


def test_retry_after_the_models_are_no_longer_wanted_loads_nothing(faced):
    d, sh = faced
    out = sh("models fail", *locked_and_ready(d), "wait", "lockhint s1 0", "fire on_models_retry",
             "wait", "state", start=())
    assert len(only(out, "pipeline_new")) == 1
    assert "state job=none models=0 pending=0 screen=0" in out


def screen(d):
    return str(d / "drm" / "card0-DSI-1" / "dpms")


def screen_comes_on(d, *frames):
    """Locked and ready with the models loaded and the panel off; then it comes on"""
    dpms(d, "Off\n")
    return [*locked_and_ready(d), "wait", f"frames {' '.join(frames)}",
            f"write {screen(d)} On", "fire on_screen_poll"]


def test_screen_coming_on_unlocks_with_the_face(faced):
    d, sh = faced
    out = sh(*screen_comes_on(d, "g", "g", "g"), "wait", "reply 1 ()", "watches", "state",
             start=())
    assert only(out, "call ") == [
        "call 1 org.freedesktop.login1 UnlockSession ('s1',) timeout=default"]
    assert "watch s1 uid=1001 locked=1 pin_seen=1 by_face=1 failures=0" in out
    assert "state job=none models=1 pending=0 screen=1" in out
    # The camera only while trying
    assert out.count("camera open") == out.count("camera close") == 1
    assert not only(out, "warning")


def test_unlock_refused_by_logind(faced):
    d, sh = faced
    out = sh(*screen_comes_on(d, "g", "g", "g"), "wait", "reply 1 error Access denied",
             start=())
    warnings = only(out, "warning")
    assert len(warnings) == 1
    assert warnings[0].startswith("warning: UnlockSession: ") and "Access denied" in warnings[0]


def test_screen_already_on_at_start_is_no_change(faced):
    d, sh = faced
    dpms(d, "On\n")
    out = sh(*locked_and_ready(d), "wait", "frames g g g", "fire on_screen_poll", "state",
             start=())
    assert "state job=none models=1 pending=0 screen=1" in out
    assert not only(out, "call ", "camera")


@pytest.mark.parametrize("setup", ["aod", "no-panel", "panel-gone"])
def test_screen_counts_as_off(faced, setup):
    d, sh = faced
    dpms(d, "Off\n")
    cmds = [*locked_and_ready(d), "wait", "frames g g g"]
    if setup == "aod":
        # The always-on display: dpms goes on for each picture, the panel only dozes
        cmds += [f"write {d}/user/1001/plinius-aod", f"write {screen(d)} On"]
    elif setup == "no-panel":
        shutil.rmtree(d / "drm")
    else:
        cmds += [f"rm {screen(d)}"]
    out = sh(*cmds, "fire on_screen_poll", "state", start=())
    assert "state job=none models=1 pending=0 screen=0" in out
    assert not only(out, "camera")


def test_screen_going_off_stops_the_try(faced):
    d, sh = faced
    out = sh(*screen_comes_on(d, "w", "g", "g", "g"), "blocked", f"write {screen(d)} Off",
             "fire on_screen_poll", "release", "wait", "watches", "state", start=())
    assert "worker waits" in out
    assert not only(out, "call ")
    # No face was seen: no failed try
    assert "watch s1 uid=1001 locked=1 pin_seen=1 by_face=0 failures=0" in out
    assert "state job=none models=1 pending=0 screen=0" in out


def test_screen_on_while_the_models_load_tries_afterwards(faced):
    d, sh = faced
    dpms(d, "Off\n")
    out = sh("models block", *locked_and_ready(d), "blocked", "frames g g g",
             f"write {screen(d)} On", "fire on_screen_poll", "state", "release", "wait", "state",
             start=())
    states = only(out, "state")
    assert states == ["state job=busy models=0 pending=1 screen=1",
                      "state job=none models=1 pending=0 screen=1"]
    assert only(out, "call ") == [
        "call 1 org.freedesktop.login1 UnlockSession ('s1',) timeout=default"]


def test_screen_off_forgets_a_pending_try(faced):
    d, sh = faced
    dpms(d, "Off\n")
    out = sh("models block", *locked_and_ready(d), "blocked", "frames g g g",
             f"write {screen(d)} On", "fire on_screen_poll", f"write {screen(d)} Off",
             "fire on_screen_poll", "state", "release", "wait", "state", start=())
    assert only(out, "state") == ["state job=busy models=0 pending=0 screen=0",
                                  "state job=none models=1 pending=0 screen=0"]
    assert not only(out, "call ", "camera")


def test_the_panel_is_read_only_while_the_face_may_unlock(faced):
    d, sh = faced
    dpms(d, "Off\n")
    (d / "run" / "sessions").write_text("s1 1 0\n")
    out = sh("session s1 1001 seat0 user 0", *enrolled(), "main", "own", "timers",
             "lockhint s1 1", "timers", "wait", "lockhint s1 0", "timers", start=())
    polls = ["on_screen_poll" in t for t in only(out, "timers")]
    # Unlocked: no reads, 4 wake-ups a second less; locked: reads; unlocked again: none
    assert polls == [False, True, False]
    assert only(out, "timer+ on_screen_poll") == ["timer+ on_screen_poll 250ms"]
    assert only(out, "timer- on_screen_poll") == ["timer- on_screen_poll"]


@pytest.mark.parametrize("state,enabled", [
    ("s1 0 0\n", 1),   # no PIN since the start
    ("s1 1 0\n", 0),   # face unlock off
    ("s1 1 5\n", 1),   # too many failed tries
])
def test_no_panel_reads_for_a_lock_the_face_may_not_open(faced, state, enabled):
    d, sh = faced
    (d / "run" / "sessions").write_text(state)
    out = sh("session s1 1001 seat0 user 1", f"settings 1001 {enabled} 1", "templates 1001 3",
             "main", "own", "timers", start=())
    assert not only(out, "timer+ on_screen_poll")


def test_a_session_ending_while_locked_gives_back_the_panel_and_the_models(faced):
    d, sh = faced
    out = sh(*locked_and_ready(d), "wait", "removed s1", "removed s1", "timers", start=())
    assert only(out, "timer- on_screen_poll") == ["timer- on_screen_poll"]
    assert "timer+ on_release_due 30000ms" in out
    assert "on_screen_poll" not in only(out, "timers")[-1]


def test_a_lock_during_a_job_reads_the_panel_at_once(faced):
    d, sh = faced
    dpms(d, "Off\n")
    (d / "run" / "sessions").write_text("s1 1 0\n")
    out = sh("session s1 1001 seat0 user 0", *enrolled(), "main", "own", "frames w",
             "method Verify", "blocked", "lockhint s1 1", f"write {screen(d)} On",
             "fire on_screen_poll", "state", "release", "wait", start=())
    # The screen came on while the Verify ran: the try follows its end
    assert out.index("timer+ on_screen_poll 250ms") < out.index("fired on_screen_poll")
    assert "state job=busy models=1 pending=1 screen=1" in out


def test_pin_unlock_during_a_try_stops_it(faced):
    d, sh = faced
    (d / "run" / "sessions").write_text("s1 1 3\n")
    out = sh(*screen_comes_on(d, "w", "g", "g", "g"), "blocked", "lockhint s1 0",
             "release", "wait", "watches", start=())
    assert not only(out, "call ")
    assert "log: session s1 unlocked" in out
    assert "watch s1 uid=1001 locked=0 pin_seen=1 by_face=0 failures=0" in out


# ---- Dropping the models ----

def unlocked_by_face(d):
    return [*screen_comes_on(d, "g", "g", "g"), "wait", "reply 1 ()", "lockhint s1 0"]


def test_models_are_dropped_by_executing_again(faced):
    d, sh = faced
    target = d / "the-service-binary"
    target.write_text("")
    (d / "self-exe").symlink_to(target)
    out = sh(*unlocked_by_face(d), "timers", "fire on_release_due", "never reached", start=())
    assert "timer+ on_release_due 30000ms" in out
    assert out[-3:] == ["log: dropping the models", "close_range 3 max",
                        f"exec {target} {target}"]
    # What only this process knew about the session goes over to the new one
    assert (d / "run" / "sessions").read_text() == "s1 1 0\n"
    assert (d / "run" / "sessions").stat().st_mode & 0o777 == 0o600


def test_exec_failure_ends_the_process(faced):
    d, sh = faced
    (d / "self-exe").symlink_to(d / "the-service-binary")
    out, code = sh("execfails", *unlocked_by_face(d), "fire on_release_due", "never reached",
                   start=(), check=False)
    assert out[-1] == "_exit 1" and code == 1


def test_locking_again_keeps_the_models(faced):
    d, sh = faced
    out = sh(*unlocked_by_face(d), "lockhint s1 1", "timers", start=())
    assert out.index("timer+ on_release_due 30000ms") < out.index("timer- on_release_due")
    assert "on_release_due" not in only(out, "timers")[-1]
    # Locking with the screen on does not try: that would undo the lock button
    assert len(only(out, "call ")) == 1


def test_no_drop_without_the_own_binary(faced):
    d, sh = faced
    out = sh(*unlocked_by_face(d), "fire on_release_due", "state", start=())
    assert "fired on_release_due" in out
    assert "state job=none models=1 pending=0 screen=1" in out
    assert not only(out, "exec", "close_range")


def test_no_drop_during_a_job(faced):
    d, sh = faced
    (d / "self-exe").symlink_to(d / "the-service-binary")
    out = sh(*unlocked_by_face(d), "frames w", "method Verify", "blocked", "fire on_release_due",
             "release", "wait", "state", start=())
    assert "fired on_release_due" in out
    assert not only(out, "exec")
    # The release is planned again once the job is over
    assert only(out, "timer+ on_release_due") == ["timer+ on_release_due 30000ms"] * 2


def test_no_drop_while_a_session_wants_them(faced):
    d, sh = faced
    (d / "self-exe").symlink_to(d / "the-service-binary")
    # Models loaded by a Verify; a locked session of a user who then switches face unlock on
    # (which counts as a PIN) wants them before the release is due
    out = sh("session s1 1001 seat0 user 1", "settings 1001 0 1", "templates 1001 3", "main", "own",
             "frames g g g g g", "method Verify", "wait", "inv", "method SetEnabled true",
             f"reply 1 {POLKIT_YES}", "inv", "fire on_release_due", "watches", start=())
    assert only(out, "ok (true")
    assert "watch s1 uid=1001 locked=1 pin_seen=1 by_face=0 failures=0" in out
    assert "fired on_release_due" in out
    assert not only(out, "exec")


# ---- Idle exit ----

@pytest.mark.parametrize("files,stays", [
    ({}, False),
    ({"1001.conf": "[face]\nenabled=true\n"}, False),           # on, but no face
    ({"1001.conf": "[face]\nenabled=false\n", "1001.emb": None}, False),
    ({"abc.conf": "[face]\nenabled=true\n", "1001.emb": None}, False),
    ({"1001.conf": "[face]\nenabled=true\n", "1001.emb": None}, True),
    ({"0.txt": "", "1002.conf": "[face]\nenabled=true\n", "1002.emb": None}, True),
])
def test_idle_exit_unless_face_unlock_is_on(faced, files, stays):
    d, sh = faced
    for name, content in files.items():
        if content is None:
            uid = int(name.split(".")[0])
            sh(f"templates {uid} 3", start=())
        else:
            (d / "state" / name).write_text(content)
    out, code = sh("fire on_idle_exit", "timers", check=False)
    if stays:
        assert out[-3:-1] == ["fired on_idle_exit", "timers"]
    else:
        assert out[-2:] == ["log: idle, exiting", "exit 0"]
    assert code == 0


def test_each_request_moves_the_idle_exit(faced):
    d, sh = faced
    out = sh("method GetUser")
    assert out[-4:] == ["timer- on_idle_exit", "timer+ on_idle_exit 120000ms",
                        "ok (false, false, 'strict')", "exit 0"]


# ---- Requests ----

def test_polkit_is_asked_for_the_sender(faced):
    d, sh = faced
    out = sh("method Enroll")
    assert only(out, "call ") == [
        "call 1 org.freedesktop.PolicyKit1 CheckAuthorization (('system-bus-name', "
        "{'name': <':1.5'>}), 'org.plinius.face.enroll', @a{ss} {}, uint32 1, '') timeout=max"]
    assert "pending" in out


@pytest.mark.parametrize("answer,warning", [
    (f"reply 1 {POLKIT_NO}", None),
    ("reply 1 error no agent", "warning: polkit: "),
])
def test_polkit_says_no(faced, answer, warning):
    d, sh = faced
    out = sh(*enrolled(enabled=0), "method SetEnabled true", answer, "inv")
    assert out[-2] == "error org.plinius.Face.Error.Denied: not authorized"
    assert bool(only(out, "warning: polkit")) == bool(warning)
    assert "enabled=false" in (d / "state" / "1001.conf").read_text()


def test_enroll_runs_to_its_signal(faced):
    d, sh = faced
    out = sh("frames " + " ".join(["n"] + ["g"] * 30), "method Enroll", f"reply 1 {POLKIT_YES}",
             "inv", "wait")
    # Answered once polkit said yes, before the job's first frame
    assert "ok" in out
    signals = only(out, "signal")
    assert signals[0] == ("signal /org/plinius/Face org.plinius.Face1 EnrollProgress "
                          "(uint32 1001, uint32 0, 'no-face')")
    assert signals[1].endswith("EnrollProgress (uint32 1001, uint32 3, '')")
    assert signals[-1] == ("signal /org/plinius/Face org.plinius.Face1 EnrollFinished "
                           "(uint32 1001, true, '')")
    assert (d / "state" / "1001.emb").stat().st_size == 8 + 4 + 11 * 128 * 4
    assert "log: uid 1001: face enrolled (11 templates)" in out


def test_enroll_that_cannot_be_stored(faced):
    d, sh = faced
    shutil.rmtree(d / "state")
    out = sh("frames " + " ".join(["g"] * 30), "method Enroll", f"reply 1 {POLKIT_YES}", "wait")
    finished = only(out, "signal /org/plinius/Face org.plinius.Face1 EnrollFinished")
    assert len(finished) == 1
    assert finished[0].startswith("signal /org/plinius/Face org.plinius.Face1 EnrollFinished "
                                  "(uint32 1001, false, 'cannot store the face: ")


def test_cancel_stops_ones_own_enrollment_only(faced):
    d, sh = faced
    frames = "frames g g w " + " ".join(["g"] * 30)
    out = sh(frames, "method Enroll", f"reply 1 {POLKIT_YES}", "blocked", "bus caller 1002",
             "method Cancel", "release", "wait")
    # Another user's Cancel is answered but changes nothing
    assert only(out, "signal /org/plinius/Face org.plinius.Face1 EnrollFinished") == [
        "signal /org/plinius/Face org.plinius.Face1 EnrollFinished (uint32 1001, true, '')"]
    (d / "state" / "1001.emb").unlink()
    out = sh(frames, "method Enroll", f"reply 1 {POLKIT_YES}", "blocked", "method Cancel",
             "release", "wait")
    assert only(out, "signal /org/plinius/Face org.plinius.Face1 EnrollFinished") == [
        "signal /org/plinius/Face org.plinius.Face1 EnrollFinished (uint32 1001, false, 'cancelled')"]
    assert not (d / "state" / "1001.emb").exists()


def test_one_request_at_a_time(faced):
    d, sh = faced
    out = sh(*enrolled(), "frames w", "method Verify", "blocked", "method Verify", "method Enroll",
             f"reply 1 {POLKIT_YES}", "inv", "release", "wait")
    busy = "error org.plinius.Face.Error.Busy: the camera is in use for another request"
    assert out.count(busy) == 2
    assert "error org.plinius.Face.Error.Failed: no clear picture of a face" not in out


def test_verify_without_models(faced):
    d, sh = faced
    out = sh(*enrolled(), "models fail", "method Verify", "wait", "inv")
    assert out[-2] == "error org.plinius.Face.Error.Failed: models: no models here"
    assert not only(out, "camera")


def test_switching_on_needs_a_face(faced):
    d, sh = faced
    out = sh("settings 1001 0 1", "method SetEnabled true", f"reply 1 {POLKIT_YES}", "inv")
    assert out[-2] == "error org.plinius.Face.Error.NotEnrolled: no face enrolled"


def test_switching_on_counts_as_a_pin_unlock(faced):
    d, sh = faced
    (d / "run" / "sessions").write_text("s1 0 5\ns2 0 5\n")
    out = sh("session s1 1001 seat0 user 1", "session s2 1002 seat0 user 1", *enrolled(enabled=0),
             "main", "own", "method SetEnabled true", f"reply 1 {POLKIT_YES}", "inv", "watches",
             start=())
    assert out[-5] == "ok"
    assert "watch s1 uid=1001 locked=1 pin_seen=1 by_face=0 failures=0" in out
    # Another user's session stays as it was
    assert "watch s2 uid=1002 locked=1 pin_seen=0 by_face=0 failures=5" in out
    assert "enabled=true" in (d / "state" / "1001.conf").read_text()


def test_settings_that_cannot_be_stored(faced):
    d, sh = faced
    shutil.rmtree(d / "state")
    (d / "state").write_text("")
    out = sh("method SetStrictness strict")
    assert out[-2].startswith("error org.plinius.Face.Error.Failed: ")


def test_delete_that_fails(faced):
    d, sh = faced
    (d / "state" / "1001.emb").mkdir()
    (d / "state" / "1001.emb" / "x").write_text("")
    out = sh("settings 1001 1 1", "method Delete")
    assert out[-2] == "error org.plinius.Face.Error.Failed: Is a directory"
    # Face unlock stays as it was: nothing was deleted
    assert "enabled=true" in (d / "state" / "1001.conf").read_text()


def test_unknown_method(faced):
    d, sh = faced
    out = sh("method Frobnicate")
    assert out[-2] == "error org.plinius.Face.Error.Unknown: Frobnicate"


def test_enroll_without_camera(faced):
    d, sh = faced
    out = sh("camera fail", "method Enroll", f"reply 1 {POLKIT_YES}", "wait")
    assert only(out, "signal") == [
        "signal /org/plinius/Face org.plinius.Face1 EnrollFinished "
        "(uint32 1001, false, 'camera: no camera here')"]


def test_models_are_dropped_even_without_the_sessions_file(faced):
    d, sh = faced
    (d / "self-exe").symlink_to(d / "the-service-binary")
    out = sh(*unlocked_by_face(d), f"rmdir {d}/run", f"write {d}/run x",
             "fire on_release_due", start=())
    # /run/plinius-face is gone (here a file in its place): the face waits for a PIN again
    warnings = only(out, "warning")
    assert len(warnings) == 1 and "sessions" in warnings[0]
    assert out[-1] == f"exec {d / 'the-service-binary'} {d / 'the-service-binary'}"
