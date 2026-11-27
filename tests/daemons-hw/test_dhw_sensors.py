# sensor-proxy-bridge: screen orientation from the accelerometer (thresholds, hysteresis, settle
# time), tilt, compass heading, proximity and light, the sensor list of the HAL and which D-Bus
# signal an event becomes. libgbinder is faked (fakes/gbinder.h), D-Bus signals are printed.

import functools
import os
import math
import re

import pytest

from dhw_harness import PKG, build_harness, kv, run, sandboxed_source

SRC = PKG / "sensor-proxy-bridge" / "sensor-proxy-bridge.c"

HARNESS = r"""
#include <glib.h>
#include <gio/gio.h>
#include <stdio.h>
#include <stdlib.h>
static gint64 fake_now;
#define g_get_monotonic_time() (fake_now)

/* D-Bus: printed instead of sent; claims watch nothing */
static gboolean fake_emit_signal(GDBusConnection *c, const char *dest, const char *path, const char *iface,
				 const char *name, GVariant *params, GError **error)
{
	gchar *text = params ? g_variant_print(g_variant_ref_sink(params), FALSE) : g_strdup("");
	printf("signal=%s %s %s\n", path, name, text);
	g_free(text);
	if (params)
		g_variant_unref(params);
	return TRUE;
}
#define g_dbus_connection_emit_signal fake_emit_signal
#define g_bus_watch_name_on_connection(...) (1u)
#define g_bus_unwatch_name(id) ((void)(id))

#define main real_main
#include "sensor-proxy-bridge.c"
#undef main

static float f(const char *s) { return (float)g_ascii_strtod(s, NULL); }

static void init_claims(void)
{
	for (int id = 0; id < N_SENSORS; id++)
		self.sensors[id].claims = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, unwatch);
	self.list = g_array_new(FALSE, TRUE, sizeof(struct hal_sensor_info));
	g_array_set_clear_func(self.list, hal_sensor_info_clear);
	self.running = g_hash_table_new_full(NULL, NULL, NULL, g_free);
}

/* A SensorInfo parcel in the order hal_read_sensor_list reads it */
#define SENSOR(handle, type, range, flags) \
	{'i', 1}, {'i', 100}, {'i', handle}, {'s', 0, 0, "name"}, {'s', 0, 0, "vendor"}, {'i', 1}, \
	{'i', type}, {'s', 0, 0, "type"}, {'f', 0, range}, {'f', 0, 0.1}, {'f', 0, 0.5}, {'i', 0}, \
	{'u', 0}, {'u', 0}, {'s', 0, 0, ""}, {'i', 0}, {'u', flags}

static void print_sensors(void)
{
	for (int id = 0; id < N_SENSORS; id++)
		printf("%s=%d %d %d %.1f\n", self.sensors[id].name, self.sensors[id].handle, self.sensors[id].wakeup,
		       self.sensors[id].one_shot, self.sensors[id].max_range);
	printf("north=%d\n", self.orientation_north);
}

int main(int argc, char **argv)
{
	const char *cmd = argv[1];
	init_claims();
	if (!strcmp(cmd, "orientation")) {
		const char *o = orientation_of(f(argv[2]), f(argv[3]), f(argv[4]), argv[5]);
		printf("orientation=%s\n", o ? o : "(null)");
	} else if (!strcmp(cmd, "tilt")) {
		printf("tilt=%s\n", tilt_of(f(argv[2])));
	} else if (!strcmp(cmd, "azimuth")) {
		float q[4] = {f(argv[2]), f(argv[3]), f(argv[4]), f(argv[5])};
		printf("azimuth=%.3f\n", azimuth_of(q));
	} else if (!strcmp(cmd, "compass")) {
		/* A sequence of rotations about the vertical axis, by heading in degrees */
		for (int i = 2; i < argc; i++) {
			double a = -g_ascii_strtod(argv[i], NULL) * M_PI / 180.0;
			float q[4] = {0, 0, sin(a / 2), cos(a / 2)};
			on_compass(q);
			printf("heading=%.2f\n", self.heading);
		}
	} else if (!strcmp(cmd, "proximity")) {
		self.sensors[PROXIMITY].max_range = 5;
		for (int i = 2; i < argc; i++) {
			on_proximity(f(argv[i]));
			printf("near=%d\n", self.near);
		}
	} else if (!strcmp(cmd, "light")) {
		for (int i = 2; i < argc; i++) {
			on_light(f(argv[i]));
			printf("light=%.2f\n", self.light_level);
		}
	} else if (!strcmp(cmd, "settle")) {
		/* argv: settle ms, then "<t_ms>:<x>,<y>,<z>" samples */
		self.settle_ms = atoi(argv[2]);
		for (int i = 3; i < argc; i++) {
			float x, y, z;
			long t;
			sscanf(argv[i], "%ld:%f,%f,%f", &t, &x, &y, &z);
			fake_now = t * 1000;
			on_accelerometer(x, y, z);
			printf("at %ld=%s\n", t, self.orientation);
		}
	} else if (!strcmp(cmd, "list")) {
		FakeGbValue v[] = {
			{'i', 0}, {'i', 6},
			SENSOR(10, TYPE_ACCELEROMETER, 39.2, SENSOR_FLAG_WAKE_UP),
			SENSOR(11, TYPE_ACCELEROMETER, 78.4, 0),
			SENSOR(12, TYPE_ACCELEROMETER, 19.6, 0),
			SENSOR(20, TYPE_PICK_UP_GESTURE, 1, SENSOR_FLAG_WAKE_UP | SENSOR_FLAG_ONE_SHOT),
			SENSOR(30, TYPE_GAME_ROTATION_VECTOR, 1, 0),
			SENSOR(40, TYPE_PROXIMITY, 5, SENSOR_FLAG_WAKE_UP | 0x2),
		};
		fake_gb_reply_vals = v;
		fake_gb_reply_n = G_N_ELEMENTS(v);
		hal_read_sensor_list();
		print_sensors();
		/* One game rotation event: both orientation signals */
		GArray *batch = g_array_new(FALSE, TRUE, sizeof(struct hal_event));
		struct hal_event e = {.sensor = 30, .payload.data = {0, 0, 0.6f, 0.8f}};
		g_array_append_val(batch, e);
		dispatch_events(batch);
		/* Claims on the shared sensor: on once, off only when both are released */
		g_string_truncate(fake_gb_log ? fake_gb_log : (fake_gb_log = g_string_new(NULL)), 0);
		claim(&self.sensors[ORIENTATION], ":1.1");
		claim(&self.sensors[GAME_ORIENTATION], ":1.2");
		release(&self.sensors[ORIENTATION], ":1.1");
		printf("log-before-last-release=%s\n", g_strdelimit(g_strdup(fake_gb_log->str), "\n", ';'));
		g_string_truncate(fake_gb_log, 0);
		release(&self.sensors[GAME_ORIENTATION], ":1.2");
		printf("log-last-release=%s\n", g_strdelimit(g_strdup(fake_gb_log->str), "\n", ';'));
		/* The one-shot pick-up gesture is armed again after it fired, while claimed */
		claim(&self.sensors[PICK_UP], ":1.3");
		g_string_truncate(fake_gb_log, 0);
		on_pick_up();
		printf("log-pick-up=%s\n", g_strdelimit(g_strdup(fake_gb_log->str), "\n", ';'));
	} else if (!strcmp(cmd, "list-with-rotation")) {
		FakeGbValue v[] = {
			{'i', 0}, {'i', 2},
			SENSOR(30, TYPE_GAME_ROTATION_VECTOR, 1, 0),
			SENSOR(31, TYPE_ROTATION_VECTOR, 1, 0),
		};
		fake_gb_reply_vals = v;
		fake_gb_reply_n = G_N_ELEMENTS(v);
		hal_read_sensor_list();
		print_sensors();
	} else if (!strcmp(cmd, "list-exception")) {
		FakeGbValue v[] = {{'i', -1}, {'i', 1}, SENSOR(11, TYPE_ACCELEROMETER, 78.4, 0)};
		fake_gb_reply_vals = v;
		fake_gb_reply_n = G_N_ELEMENTS(v);
		hal_read_sensor_list();
		print_sensors();
	} else if (!strcmp(cmd, "settings")) {
		load_settings();
		printf("loaded=%u\n", self.settle_ms);
		for (int i = 2; i < argc; i++) {
			GError *error = NULL;
			gboolean ok = on_set_property(NULL, NULL, NULL, NULL, "OrientationSettleTime",
						      g_variant_new_uint32(atoi(argv[i])), &error, NULL);
			printf("set %s=%d %u\n", argv[i], ok, self.settle_ms);
			g_clear_error(&error);
		}
	}
	return 0;
}
"""


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    d = tmp_path_factory.mktemp("sensors")
    sandboxed_source(SRC, d, [("/var/lib/sensor-proxy-bridge/settings", d / "settings"),
                              ("/dev/anbox-binder", d / "anbox-binder")],
                     allow=("/dev/binder",))
    exe = build_harness(d, "sensors", HARNESS, pkgs=("gio-unix-2.0",))
    return d, functools.partial(run, exe)


@pytest.fixture
def sensors(built):
    return built[1]


G = 9.80665


def vec(angle_deg, tilt_deg=90.0):
    """Gravity reading for the screen turned clockwise by angle and tilted tilt_deg from flat."""
    a, t = math.radians(angle_deg), math.radians(tilt_deg)
    return (f"{G * math.sin(t) * math.sin(a):.5f}", f"{G * math.sin(t) * math.cos(a):.5f}",
            f"{G * math.cos(t):.5f}")


@pytest.mark.parametrize("angle,current,expected", [
    (0, "undefined", "normal"),
    (90, "undefined", "right-up"),
    (180, "undefined", "bottom-up"),
    (270, "undefined", "left-up"),
    (-90, "undefined", "left-up"),
    (44, "undefined", "normal"),        # no orientation yet: the nearest
    (46, "undefined", "right-up"),
    (55, "normal", "normal"),           # within 45 + 15 degrees: stays
    (65, "normal", "right-up"),
    (-55, "normal", "normal"),          # across 0/360
    (-65, "normal", "left-up"),
    (25, "right-up", "normal"),
    (35, "right-up", "right-up"),
    (10, "left-up", "normal"),          # 260 degrees apart one way, 100 the other
    (200, "bottom-up", "bottom-up"),
])
def test_orientation_hysteresis(sensors, angle, current, expected):
    x, y, z = vec(angle)
    assert kv(sensors("orientation", x, y, z, current).stdout)["orientation"] == expected


@pytest.mark.parametrize("tilt,expected", [
    (0, "(null)"),      # flat on the table: keep the last orientation
    (30, "(null)"),
    (34, "(null)"),
    (36, "right-up"),
    (90, "right-up"),
    (140, "right-up"),  # face down but tilted far enough (40 degrees from flat)
    (150, "(null)"),
    (170, "(null)"),
])
def test_orientation_needs_tilt(sensors, tilt, expected):
    x, y, z = vec(90, tilt)
    assert kv(sensors("orientation", x, y, z, "undefined").stdout)["orientation"] == expected


@pytest.mark.parametrize("g,expected", [
    (1.0, "face-up"), (0.91, "face-up"), (0.89, "tilted-up"), (0.36, "tilted-up"),
    (0.34, "vertical"), (0, "vertical"), (-0.34, "vertical"), (-0.36, "tilted-down"),
    (-0.91, "face-down"),
])
def test_tilt(sensors, g, expected):
    assert kv(sensors("tilt", f"{g * G:.4f}").stdout)["tilt"] == expected


@pytest.mark.parametrize("q,expected", [
    ((0, 0, 0, 1), 0.0),
    # 90 degrees counter-clockwise about z (top of the phone points west): azimuth -90
    ((0, 0, math.sin(math.pi / 4), math.cos(math.pi / 4)), -90.0),
    ((0, 0, -math.sin(math.pi / 4), math.cos(math.pi / 4)), 90.0),
    # w left out (0): computed from x, y, z as Android does
    ((0, 0, -math.sin(math.pi / 4), 0), 90.0),
    ((0, 0, math.sin(math.pi / 8), math.cos(math.pi / 8)), -45.0),
])
def test_azimuth(sensors, q, expected):
    out = kv(sensors("azimuth", *(f"{v:.7f}" for v in q)).stdout)
    assert float(out["azimuth"]) == pytest.approx(expected, abs=0.01)


def test_compass_heading_range_and_small_changes(sensors):
    out = sensors("compass", "90", "90.5", "92", "359.6", "0.3", "1.2", "270").stdout
    headings = [float(line.split("=")[1]) for line in out.splitlines() if line.startswith("heading=")]
    assert headings == pytest.approx([90, 90, 92, 359.6, 359.6, 1.2, 270], abs=0.02)
    # From 359.6 to 0.3 is 0.7 degrees across north, not 359.3: not reported


def test_proximity(sensors):
    # Near below the maximum range (binary sensors report 0 or their range)
    out = sensors("proximity", "0", "4.9", "5", "1").stdout
    assert [line[5:] for line in out.splitlines() if line.startswith("near=")] == ["1", "1", "0", "1"]
    # Only changes are signalled
    assert out.count("ProximityNear") == 3


def test_light_ignores_tiny_changes(sensors):
    out = sensors("light", "100", "100.4", "100.6", "0").stdout
    levels = [float(line[6:]) for line in out.splitlines() if line.startswith("light=")]
    assert levels == pytest.approx([100, 100, 100.6, 0])
    assert out.count("LightLevel") == 3


def test_orientation_settles_before_turning(sensors):
    up, right, left = vec(0), vec(90), vec(270)
    s = lambda t, v: f"{t}:{v[0]},{v[1]},{v[2]}"  # noqa: E731
    out = sensors("settle", "500", s(0, up), s(600, up), s(700, right), s(1100, right),
                  s(1150, left), s(1600, left), s(1700, left)).stdout
    states = [line.split("=")[1] for line in out.splitlines() if line.startswith("at ")]
    # normal only after holding 500 ms (first sample starts the wait); the move to left-up
    # restarts it, so right-up never shows
    assert states == ["undefined", "normal", "normal", "normal", "normal", "normal", "left-up"]
    assert out.count("AccelerometerOrientation") == 2


def test_sensor_list_choice(sensors):
    out = sensors("list").stdout
    k = kv(out)
    # The non-wakeup accelerometer is preferred, the first of them kept
    assert k["Accelerometer"] == "11 0 0 78.4"
    assert k["PickUp"] == "20 1 1 1.0"           # one-shot reporting mode recognised
    assert k["Proximity"] == "40 1 0 5.0"         # on-change mode (0x2) is not one-shot
    # No rotation vector: the game rotation vector stands in, without north
    assert k["Orientation"].startswith("30 0 0 ")
    assert k["GameOrientation"] == "30 0 0 1.0"
    assert k["north"] == "0"
    assert k["Light"].startswith("-1 ")
    sig = [line for line in out.splitlines() if line.startswith("signal=")]
    assert len(sig) == 3 and sig[2].endswith(" PickedUp ")
    assert " Orientation (0.0, 0.0, 0.6" in sig[0]
    assert " GameOrientation (0.0, 0.0, 0.6" in sig[1]
    # Shared HAL sensor: switched on once, off only after the last claim is gone
    assert k["log-before-last-release"].count("tx 1;") == 1
    assert "tx 2;" in k["log-before-last-release"]
    assert k["log-last-release"] == "request;i32 30;i32 0;tx 1;"
    # Re-armed: batch + activate(1) for the pick-up handle
    assert k["log-pick-up"].endswith("request;i32 20;i32 1;tx 1;")


def test_sensor_list_prefers_real_rotation_vector(sensors):
    k = kv(sensors("list-with-rotation").stdout)
    assert k["Orientation"].startswith("31 ")
    assert k["GameOrientation"].startswith("30 ")
    assert k["north"] == "1"


def test_sensor_list_exception_takes_nothing(sensors):
    k = kv(sensors("list-exception").stdout)
    assert k["Accelerometer"].startswith("-1 ")


def test_settle_time_settings(built):
    d, sensors = built
    (d / "settings").write_text("[orientation]\nsettle-ms=1200\n")
    out = sensors("settings", "99", "3001", "100").stdout
    k = kv(out)
    assert k["loaded"] == "1200"
    assert k["set 99"] == "0 1200"
    assert k["set 3001"] == "0 1200"
    assert k["set 100"] == "1 100"
    assert "settle-ms=100" in (d / "settings").read_text()
    # Out of range in the file: the default stays
    (d / "settings").write_text("[orientation]\nsettle-ms=50\n")
    assert kv(sensors("settings").stdout)["loaded"] == "500"


# ---- The service as it runs: main(), the HAL connection, the event queue, the D-Bus object ----
#
# libgbinder is fakes/gbinder.h, with wrappers here for what a test steers: which transaction gets
# no reply, the death handler, the HAL callback object. The fast message queues are in memory:
# the script pushes events, the reader thread takes them as from the HAL's queue, and what it
# writes to the wake lock queue is printed. The bus is fakes/dhw_scripted_gdbus.h; the main loop
# runs the script given after the sensor list.

SERVICE = r"""
#define _GNU_SOURCE /* memfd_create */
#define G_DISABLE_CAST_CHECKS
#include <glib.h>
#include <gio/gio.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <gbinder.h>
#include "dhw_scripted_gdbus.h"

static gint64 fake_now;
#define g_get_monotonic_time() (fake_now)

/* ---- HAL steering ---- */
static int no_reply_code = -1;          /* this transaction gets no reply */
static int exception_code = -1;         /* this transaction answers with an exception */
static int sm_missing, sm_not_ready, android_sm_missing;
static int usleeps;
/* The HAL's death handler is the first one, Android's the ones after it */
static GBinderRemoteObjectNotifyFunc death_handler, android_death;
static GBinderLocalTransactFunc hal_callback, android_fn;
static FakeGbValue exception_reply[] = {{'i', -3}};

static int last_code;
static GBinderRemoteReply *wrap_transact(GBinderClient *c, guint32 code, GBinderLocalRequest *req, int *status)
{
	GBinderRemoteReply *reply = gbinder_client_transact_sync_reply(c, code, req, status);
	last_code = code;
	if ((int)code == no_reply_code) {
		*status = GBINDER_STATUS_FAILED;
		return NULL;
	}
	return reply;
}
#define gbinder_client_transact_sync_reply wrap_transact
/* The reply of the last transaction: an exception for exception_code */
static void wrap_init_reader(GBinderRemoteReply *reply, GBinderReader *r)
{
	gbinder_remote_reply_init_reader(reply, r);
	if (last_code == exception_code) {
		r->v = exception_reply;
		r->n = 1;
	}
}
#define gbinder_remote_reply_init_reader wrap_init_reader

static GBinderServiceManager *wrap_sm_new(const char *dev)
{
	GBinderServiceManager *sm = gbinder_servicemanager_new(dev);
	if (android_sm_missing && strcmp(dev, "/dev/binder"))
		return NULL;
	return sm_missing ? NULL : sm;
}
#define gbinder_servicemanager_new wrap_sm_new
static gboolean wrap_sm_wait(GBinderServiceManager *sm, long ms)
{
	return !sm_not_ready;
}
#define gbinder_servicemanager_wait wrap_sm_wait
static gulong wrap_add_death(GBinderRemoteObject *o, GBinderRemoteObjectNotifyFunc fn, void *user)
{
	if (!death_handler)
		death_handler = fn;
	else
		android_death = fn;
	printf("death-handler\n");
	return 1;
}
#define gbinder_remote_object_add_death_handler wrap_add_death
static GBinderLocalObject *wrap_new_local(GBinderServiceManager *sm, const char *iface, GBinderLocalTransactFunc fn,
					  void *user)
{
	printf("local-object %s\n", iface);
	if (!strcmp(iface, "android.hardware.sensors.ISensors"))
		android_fn = fn;
	else
		hal_callback = fn;
	return gbinder_servicemanager_new_local_object(sm, iface, fn, user);
}
#define gbinder_servicemanager_new_local_object wrap_new_local
static void fake_usleep(gulong us)
{
	usleeps++;
}
#define g_usleep fake_usleep

/* ---- Fast message queues in memory ---- */
typedef struct {
	gsize item;
	GMutex lock;
	GCond cond;
	GByteArray *data;
	guint32 woken;
	int read_fails;
} FakeFmq;
static FakeFmq *events_q, *wake_q;

static GBinderFmq *fake_fmq_new(gsize item, gsize n, int type, int flags, int fd, gsize size)
{
	FakeFmq *q = g_new0(FakeFmq, 1);
	q->item = item;
	g_mutex_init(&q->lock);
	g_cond_init(&q->cond);
	q->data = g_byte_array_new();
	if (item == sizeof(guint32))
		wake_q = q;
	else
		events_q = q;
	printf("fmq %zu x %zu\n", item, n);
	return (GBinderFmq *)q;
}
static gsize fake_fmq_available(GBinderFmq *fq)
{
	FakeFmq *q = (FakeFmq *)fq;
	g_mutex_lock(&q->lock);
	gsize n = q->data->len / q->item;
	g_mutex_unlock(&q->lock);
	return n;
}
static int fake_fmq_wait(GBinderFmq *fq, guint32 bits, guint32 *state, int ms)
{
	FakeFmq *q = (FakeFmq *)fq;
	/* Shorter than asked: the test need not wait a second for the thread to end */
	gint64 end = (g_get_monotonic_time)() + MIN(ms, 50) * G_TIME_SPAN_MILLISECOND;
	g_mutex_lock(&q->lock);
	while (!q->data->len)
		if (!g_cond_wait_until(&q->cond, &q->lock, end))
			break;
	int r = q->data->len ? 0 : -1;
	if (!r)
		*state = bits;
	g_mutex_unlock(&q->lock);
	return r;
}
static gboolean fake_fmq_read(GBinderFmq *fq, void *data, gsize n)
{
	FakeFmq *q = (FakeFmq *)fq;
	g_mutex_lock(&q->lock);
	gboolean ok = !q->read_fails && q->data->len >= n * q->item;
	if (q->read_fails)
		q->read_fails--;
	if (ok) {
		memcpy(data, q->data->data, n * q->item);
		g_byte_array_remove_range(q->data, 0, n * q->item);
	}
	g_mutex_unlock(&q->lock);
	return ok;
}
static gboolean fake_fmq_write(GBinderFmq *fq, const void *data, gsize n)
{
	FakeFmq *q = (FakeFmq *)fq;
	g_mutex_lock(&q->lock);
	g_byte_array_append(q->data, data, n * q->item);
	g_cond_broadcast(&q->cond);
	g_mutex_unlock(&q->lock);
	return TRUE;
}
static int fake_fmq_wake(GBinderFmq *fq, guint32 bits)
{
	FakeFmq *q = (FakeFmq *)fq;
	g_mutex_lock(&q->lock);
	q->woken |= bits;
	g_mutex_unlock(&q->lock);
	return 0;
}
#define gbinder_fmq_new fake_fmq_new
#define gbinder_fmq_available_to_read fake_fmq_available
#define gbinder_fmq_wait_timeout fake_fmq_wait
#define gbinder_fmq_read fake_fmq_read
#define gbinder_fmq_write fake_fmq_write
#define gbinder_fmq_wake fake_fmq_wake

/* Batches the reader thread hands to the main loop: counted, so a test waits for all of them */
static gint events_written, events_dispatched;
typedef struct {
	GSourceFunc fn;
	gpointer data;
} Counted;
static gboolean counted_dispatch(gpointer p)
{
	Counted *c = p;
	gint n = ((GArray *)c->data)->len;
	gboolean r = c->fn(c->data);
	g_atomic_int_add(&events_dispatched, n);
	g_free(c);
	return r;
}
static guint counted_idle_add(GSourceFunc fn, gpointer data)
{
	Counted *c = g_new(Counted, 1);
	c->fn = fn;
	c->data = data;
	return g_idle_add(counted_dispatch, c);
}

static void script_loop(GMainLoop *loop);
#define g_main_loop_run script_loop
#define g_idle_add counted_idle_add

#define main real_main
#include "sensor-proxy-bridge.c"
#undef main
#undef g_main_loop_run
#undef g_idle_add

/* A SensorInfo parcel in the order hal_read_sensor_list reads it */
static FakeGbValue list[200] = {{'i', 0}, {'i', 0}};
static int list_n = 2;
static void add_sensor(int handle, int type, double range, int flags, int min_delay, int max_delay)
{
	FakeGbValue v[] = {{'i', 1}, {'i', 100}, {'i', handle}, {'s', 0, 0, "name"}, {'s', 0, 0, "vendor"}, {'i', 1},
			   {'i', type}, {'s', 0, 0, "type"}, {'f', 0, range}, {'f', 0, 0.1}, {'f', 0, 0.5}, {'i', min_delay},
			   {'u', 0}, {'u', 0}, {'s', 0, 0, ""}, {'i', max_delay}, {'u', flags}};
	memcpy(list + list_n, v, sizeof v);
	list_n += G_N_ELEMENTS(v);
	list[1].i++;
}

static char **script;

static gboolean quit_loop(gpointer loop)
{
	g_main_loop_quit(loop);
	return G_SOURCE_REMOVE;
}

static void run_for(unsigned ms)
{
	GMainLoop *loop = g_main_loop_new(NULL, FALSE);
	g_timeout_add(ms, quit_loop, loop);
	g_main_loop_run(loop);
	g_main_loop_unref(loop);
}

static int next_type, next_type_handle = -1;
static void push_event(int handle, const char *values)
{
	struct hal_event e = {.timestamp = 1, .sensor = handle};
	if (handle == next_type_handle) {
		e.type = next_type;
		next_type_handle = -1;
	}
	g_auto(GStrv) v = g_strsplit(values, ",", -1);
	for (int i = 0; v[i] && i < 16; i++)
		e.payload.data[i] = g_ascii_strtod(v[i], NULL);
	fake_fmq_write((GBinderFmq *)events_q, &e, 1);
	events_written++;
}

/* Until every pushed event went through the reader thread and the main loop (at most 10 s) */
static void drain(void)
{
	gint64 end = (g_get_monotonic_time)() + 10 * G_TIME_SPAN_SECOND;
	while (g_atomic_int_get(&events_dispatched) < events_written && (g_get_monotonic_time)() < end)
		if (!g_main_context_iteration(NULL, FALSE))
			usleep(1000);
	printf("drained %d of %d\n", g_atomic_int_get(&events_dispatched), events_written);
}

static void print_log(void)
{
	printf("hal=%s\n", fake_gb_log ? g_strdelimit(g_strdup(fake_gb_log->str), "\n", ';') : "");
	if (fake_gb_log)
		g_string_truncate(fake_gb_log, 0);
}

/* ---- Android: shared memory for its queues, scripted calls ---- */
#include <sys/mman.h>
#include <fcntl.h>
/* A queue's memory: read counter at 0, write counter at 8, event flag word at 16, ring at 64 */
typedef struct {
	char name[16];
	int fd;
	guint8 *map;
	gsize len;
} Mem;
static Mem mems[4];
static int n_mems;
static Mem *mem(const char *name)
{
	for (int i = 0; i < n_mems; i++)
		if (!strcmp(mems[i].name, name))
			return &mems[i];
	g_error("no memory %s", name);
}
static FakeGbValue avals[256];
/* Values: i<n> int32, l<n> int64, o object, h@<mem> its fd, hpipe a pipe, h<n> that fd */
static int parse_vals(const char *spec)
{
	int n = 0;
	g_auto(GStrv) t = g_strsplit(spec ? spec : "", ",", -1);
	for (int i = 0; t[i] && *t[i]; i++) {
		FakeGbValue v = {t[i][0]};
		const char *arg = t[i] + 1;
		if (v.kind == 'h' && *arg == '@') {
			v.i = mem(arg + 1)->fd;
		} else if (v.kind == 'h' && !strcmp(arg, "pipe")) {
			int p[2];
			if (pipe(p) < 0)
				abort();
			v.i = p[0];
		} else {
			v.i = g_ascii_strtoll(arg, NULL, 10);
		}
		avals[n++] = v;
	}
	return n;
}
static void android_call(guint code, const char *spec)
{
	int status = 99;
	fake_gb_request_vals = avals;
	fake_gb_request_n = parse_vals(spec);
	GBinderLocalReply *reply = android_fn(NULL, NULL, code, 0, &status, NULL);
	printf("acall %u status=%d reply=%s\n", code, status, reply ? "yes" : "no");
	print_log();
}
/* The queue in a memory: counters, flag word and the sensor handles of the unread items */
static void print_queue(const char *name, gsize quantum, gsize ring)
{
	Mem *m = mem(name);
	guint64 r = *(guint64 *)m->map, w = *(guint64 *)(m->map + 8);
	printf("aqueue %s read=%" G_GUINT64_FORMAT " write=%" G_GUINT64_FORMAT " flag=%u items=", name, r, w,
	       *(guint32 *)(m->map + 16));
	for (guint64 at = r; quantum == sizeof(struct hal_event) && at + quantum <= w; at += quantum) {
		const struct hal_event *e = (const struct hal_event *)(m->map + 64 + at % ring);
		printf("%d/%d,", e->sensor, e->type);
	}
	printf("\n");
}

static gboolean hal_dies(gpointer data)
{
	death_handler(NULL, NULL);
	return G_SOURCE_REMOVE;
}

static gboolean lose_name(gpointer data)
{
	sg_lose_name();
	return G_SOURCE_REMOVE;
}

static void command(const char *a)
{
	g_auto(GStrv) w = g_strsplit(a, strchr(a, '|') ? "|" : ":", -1);
	if (!strcmp(w[0], "call")) {
		/* call|<path>|<iface>|<sender>|<method> (get, set likewise) */
		sg_invoke(w[1], w[2], w[3], w[4], NULL);
	} else if (!strcmp(w[0], "get")) {
		sg_get(w[1], w[2], w[3]);
	} else if (!strcmp(w[0], "set")) {
		sg_set(w[1], w[2], w[3], w[4]);
	} else if (!strcmp(w[0], "event")) {
		/* event:<handle>:<v0,v1,...> */
		push_event(atoi(w[1]), w[2]);
	} else if (!strcmp(w[0], "run")) {
		run_for(atoi(w[1]));
	} else if (!strcmp(w[0], "drain")) {
		drain();
	} else if (!strcmp(w[0], "now")) {
		fake_now = g_ascii_strtoll(w[1], NULL, 10) * 1000;
	} else if (!strcmp(w[0], "vanish")) {
		sg_vanish(w[1]);
	} else if (!strcmp(w[0], "hal")) {
		print_log();
	} else if (!strcmp(w[0], "wake-lock")) {
		guint32 total = 0;
		g_mutex_lock(&wake_q->lock);
		for (guint i = 0; i < wake_q->data->len / 4; i++)
			total += ((guint32 *)wake_q->data->data)[i];
		printf("wake-lock written=%u items=%u woken=%u events-woken=%u\n", total, wake_q->data->len / 4,
		       wake_q->woken, events_q->woken);
		g_mutex_unlock(&wake_q->lock);
	} else if (!strcmp(w[0], "read-fails")) {
		events_q->read_fails = 1;
	} else if (!strcmp(w[0], "callback")) {
		int status = -1;
		GBinderLocalReply *reply = hal_callback(NULL, NULL, 1, 0, &status, NULL);
		printf("callback status=%d reply=%p\n", status, (void *)reply);
	} else if (!strcmp(w[0], "hal-died")) {
		g_idle_add(hal_dies, NULL);
		g_main_loop_run(self.loop);
		printf("loop quit\n");
	} else if (!strcmp(w[0], "lose-name")) {
		g_idle_add(lose_name, NULL);
		g_main_loop_run(self.loop);
		printf("loop quit\n");
	} else if (!strcmp(w[0], "amem")) {
		/* amem|name|bytes: shared memory for a queue */
		Mem *m = &mems[n_mems++];
		g_strlcpy(m->name, w[1], sizeof m->name);
		m->len = atoi(w[2]);
		m->fd = memfd_create(w[1], 0);
		if (m->fd < 0 || ftruncate(m->fd, m->len) < 0)
			abort();
		m->map = mmap(NULL, m->len, PROT_READ | PROT_WRITE, MAP_SHARED, m->fd, 0);
	} else if (!strcmp(w[0], "acall")) {
		/* acall|code|values */
		android_call(atoi(w[1]), w[2]);
	} else if (!strcmp(w[0], "aqueue")) {
		/* aqueue|name|quantum|ring bytes */
		print_queue(w[1], atoi(w[2]), atoi(w[3]));
	} else if (!strcmp(w[0], "aset")) {
		/* aset|name|read|write: the counters as the other side moves them */
		Mem *m = mem(w[1]);
		*(guint64 *)m->map = g_ascii_strtoull(w[2], NULL, 10);
		*(guint64 *)(m->map + 8) = g_ascii_strtoull(w[3], NULL, 10);
	} else if (!strcmp(w[0], "atype")) {
		/* atype|handle|type: the next event's SensorType */
		next_type = atoi(w[2]);
		next_type_handle = atoi(w[1]);
	} else if (!strcmp(w[0], "adie")) {
		android_death(NULL, NULL);
	} else if (!strcmp(w[0], "apresence")) {
		fake_gb_sm_present = atoi(w[1]);
		on_android_presence(NULL, NULL);
		print_log();
	} else if (!strcmp(w[0], "aadded")) {
		on_android_added(NULL, atoi(w[1]), NULL);
	} else if (!strcmp(w[0], "anode")) {
		/* anode|mode: the device node (octal mode) */
		int fd = open(ANDROID_DEVICE, O_CREAT | O_WRONLY, 0600);
		if (fd >= 0)
			close(fd);
		chmod(ANDROID_DEVICE, strtol(w[1], NULL, 8));
	} else if (!strcmp(w[0], "spin")) {
		/* Let the file monitor deliver: up to 2 s, until Android's binder is open */
		for (int j = 0; j < 200 && !android.sm; j++) {
			while (g_main_context_iteration(NULL, FALSE))
				;
			usleep(10000);
		}
		printf("android sm=%s\n", android.sm ? "set" : "none");
		print_log();
	} else if (!strcmp(w[0], "running")) {
		/* running|handle: the period the HAL sensor runs at */
		const gint64 *p = g_hash_table_lookup(self.running, GINT_TO_POINTER(atoi(w[1])));
		printf("running %s=%" G_GINT64_FORMAT "\n", w[1], p ? *p : (gint64)-1);
	} else if (!strcmp(w[0], "sigterm")) {
		raise(SIGTERM);
		g_main_loop_run(self.loop);
		printf("loop quit\n");
	} else {
		g_error("unknown command %s", a);
	}
	fflush(stdout);
}

static void script_loop(GMainLoop *loop)
{
	while (g_main_context_iteration(NULL, FALSE))
		;
	print_log();
	for (; script && *script; script++)
		command(*script);
}

int main(int argc, char **argv)
{
	int i = 1;
	/* Before "start": sensor:<handle>:<type>:<range>:<flags>, HAL steering, settle time file */
	for (; i < argc && strcmp(argv[i], "start"); i++) {
		g_auto(GStrv) w = g_strsplit(argv[i], ":", -1);
		if (!strcmp(w[0], "sensor"))
			/* sensor:<handle>:<type>:<range>:<flags>[:<min delay µs>:<max delay µs>] */
			add_sensor(atoi(w[1]), atoi(w[2]), g_ascii_strtod(w[3], NULL), atoi(w[4]),
				   w[5] ? atoi(w[5]) : 0, w[5] && w[6] ? atoi(w[6]) : 0);
		else if (!strcmp(w[0], "no-reply"))
			no_reply_code = atoi(w[1]);
		else if (!strcmp(w[0], "exception"))
			exception_code = atoi(w[1]);
		else if (!strcmp(w[0], "status"))
			fake_gb_status = atoi(w[1]);
		else if (!strcmp(w[0], "no-sm"))
			sm_missing = 1;
		else if (!strcmp(w[0], "sm-not-ready"))
			sm_not_ready = 1;
		else if (!strcmp(w[0], "no-service"))
			fake_gb_no_service = TRUE;
		else if (!strcmp(w[0], "no-android-sm"))
			android_sm_missing = 1;
		else if (!strcmp(w[0], "android")) {
			/* The container's binder, open to us */
			int fd = open(ANDROID_DEVICE, O_CREAT | O_WRONLY, 0600);
			if (fd < 0)
				abort();
			close(fd);
		}
		else
			g_error("unknown option %s", argv[i]);
	}
	fake_gb_reply_vals = list;
	fake_gb_reply_n = list_n;
	script = i < argc ? argv + i + 1 : NULL;
	int r = real_main();
	printf("exit %d usleeps=%d\n", r, usleeps);
	print_log();
	return r;
}
"""


@pytest.fixture(scope="module")
def service_built(tmp_path_factory):
    d = tmp_path_factory.mktemp("sensors-service")
    sandboxed_source(SRC, d, [("/var/lib/sensor-proxy-bridge/settings", d / "settings"),
                              ("/dev/anbox-binder", d / "anbox-binder")],
                     allow=("/dev/binder",))
    exe = build_harness(d, "sensors_service", SERVICE, pkgs=("gio-unix-2.0",))
    return d, exe


@pytest.fixture
def service(service_built):
    d, exe = service_built
    (d / "settings").unlink(missing_ok=True)
    (d / "anbox-binder").unlink(missing_ok=True)
    yield d, functools.partial(run, exe)
    (d / "anbox-binder").unlink(missing_ok=True)


ALL = ("sensor:10:1:39.2:0", "sensor:11:5:10000:0", "sensor:12:8:5:3", "sensor:13:20:1:0",
       "sensor:14:25:1:5", "sensor:15:9:19.6:0", "sensor:16:10:19.6:0", "sensor:17:11:1:0",
       "sensor:18:15:1:0")
P, IF = "/net/hadess/SensorProxy", "net.hadess.SensorProxy"
BRIDGE = "org.plinius.SensorProxyBridge"
GRAV_P, GRAV_IF = "/net/hadess/SensorProxy/Gravity", "org.plinius.SensorProxyBridge.Gravity"
COMPASS_P, COMPASS_IF = "/net/hadess/SensorProxy/Compass", "net.hadess.SensorProxy.Compass"


def hal_lines(out):
    return [line[4:] for line in out.splitlines() if line.startswith("hal=")]


def replies(out):
    return [line for line in out.splitlines() if line.startswith("reply ")]


def test_service_connects_and_serves(service):
    d, binary = service
    r = binary(*ALL, "start", f"call|{P}|{IF}|:1.5|ClaimAccelerometer", "hal", "sigterm")
    out = r.stdout
    start = hal_lines(out)[0]
    # The HAL from the service manager, the queues and the callback handed over in initialize(),
    # then the sensor list
    assert start.startswith("servicemanager /dev/binder;get-service android.hardware.sensors.ISensors/default;")
    assert "request;fmq;fmq;object set;tx 6;tx 5;" in start
    assert "local-object android.hardware.sensors.ISensorsCallback" in out
    assert "fmq 96 x 128" in out and "fmq 4 x 128" in out
    assert "own-name system net.hadess.SensorProxy" in out
    for path, iface in ((P, IF), (P, BRIDGE), (COMPASS_P, COMPASS_IF), (GRAV_P, GRAV_IF)):
        assert f"register {path} {iface}" in out
    # Claimed: batch at 10 Hz, then activate
    assert hal_lines(out)[1] == "request;i32 10;i64 100000000;i64 0;tx 2;request;i32 10;i32 1;tx 1;"
    assert "reply ClaimAccelerometer ok ()" in out
    # SIGTERM: the claimed sensor is switched off on the way out
    assert "loop quit" in out
    assert "exit 0 usleeps=0" in out
    assert hal_lines(out)[-1] == "request;i32 10;i32 0;tx 1;"
    assert "unown-name 7" in out


def test_service_claims_per_client(service):
    d, binary = service
    r = binary(*ALL, "start", "hal",
               f"call|{P}|{IF}|:1.5|ClaimProximity", f"call|{P}|{IF}|:1.6|ClaimProximity",
               f"call|{P}|{IF}|:1.5|ClaimProximity", "hal",
               "vanish|:1.5", "hal",
               f"call|{P}|{IF}|:1.7|ReleaseProximity", "hal",
               f"call|{P}|{IF}|:1.6|ReleaseProximity", "hal")
    out = r.stdout
    claims, after_vanish, stranger, last = hal_lines(out)[2:6]
    # Switched on once for both clients, the same client twice is one claim
    lines = out.splitlines()
    assert claims.count("tx 1;") == 1 and lines.count("watch :1.5") == 1 and "watch :1.6" in lines
    # The first client left the bus: the second keeps it on
    assert after_vanish == ""
    assert "unwatch :1.5" in out
    # Someone who never claimed it cannot release it
    assert stranger == ""
    assert last == "request;i32 12;i32 0;tx 1;"
    assert replies(out).count("reply ReleaseProximity ok ()") == 2


def test_service_unavailable_and_unknown(service):
    d, binary = service
    r = binary("sensor:10:1:39.2:0", "start", f"call|{P}|{IF}|:1.5|ClaimLight", f"call|{P}|{IF}|:1.5|Calibrate",
               f"call|{GRAV_P}|{GRAV_IF}|:1.5|ReleaseGravity")
    rs = replies(r.stdout)
    assert rs[0] == "reply ClaimLight error org.freedesktop.DBus.Error.Failed: No Light"
    assert rs[1] == "reply Calibrate error org.freedesktop.DBus.Error.UnknownMethod: Unknown method Calibrate"
    assert rs[2] == "reply ReleaseGravity error org.freedesktop.DBus.Error.Failed: No Gravity"


def signal_lines(out, name):
    return [line.split(name, 1)[1] for line in out.splitlines() if line.startswith("signal ") and f" {name}(" in line
            or line.endswith(f" {name}()")]


def test_service_events_become_signals(service):
    d, binary = service
    r = binary(*ALL, "start",
               "event:15:0,9.8,0.5", "event:16:0.1,0.2,0.3", "event:17:0,0,0.6,0", "event:18:0,0,0.6,0.8",
               "event:11:250", "event:12:0", "event:14:1", "event:99:1", "drain", "wake-lock")
    out = r.stdout
    assert f"signal * {GRAV_P} {GRAV_IF} Gravity(0.0, 9.8000001907348633, 0.5)" in out
    assert f"signal * {GRAV_P} {GRAV_IF} Motion(0.10000000149011612, 0.20000000298023224, 0.30000001192092896)" in out
    # w left out (0): from x, y, z
    orient = [line for line in out.splitlines() if " Orientation(" in line][0]
    x, y, z, w = (float(v) for v in orient.split("Orientation(")[1].rstrip(")").split(","))
    assert z == pytest.approx(0.6) and w == pytest.approx(0.8)
    assert f"signal * {GRAV_P} {GRAV_IF} GameOrientation(0.0, 0.0, 0.60000002384185791, 0.80000001192092896)" in out
    assert "PropertiesChanged('net.hadess.SensorProxy', {'LightLevel': <250.0>}, [])" in out
    assert "PropertiesChanged('net.hadess.SensorProxy', {'ProximityNear': <true>}, [])" in out
    assert f"signal * {P} {BRIDGE} PickedUp()" in out
    # Wakeup events (proximity and the pick-up gesture) are reported as handled to the HAL's wake lock:
    # two in all, one queue item per batch the daemon read (one or two, as the events arrived)
    lock = re.search(r"wake-lock written=(\d+) items=(\d+) woken=(\d+) events-woken=(\d+)", out)
    assert lock and lock.group(1) == "2" and lock.group(2) in ("1", "2") and lock.group(4) == "2", out


def test_service_event_not_lost_when_a_read_fails(service):
    d, binary = service
    r = binary(*ALL, "start", "read-fails", "event:11:250", "drain")
    assert r.stdout.count("{'LightLevel': <250.0>}") == 1


def test_service_orientation_through_the_queue(service):
    d, binary = service
    up, flat = "0,9.8,0", "0,0,9.8"
    r = binary(*ALL, "start", "now:0", f"event:10:{up}", "drain", "now:300", f"event:10:{flat}", "drain",
               "now:600", f"event:10:{up}", "drain", "now:900", f"event:10:{up}", "drain",
               "now:1200", f"event:10:{up}", "drain", f"get|{P}|{IF}|AccelerometerOrientation")
    out = r.stdout
    # Laid flat in between: the wait for "normal" starts again at 600 ms, so it shows at 1200
    assert out.count("{'AccelerometerOrientation': <'normal'>}") == 1
    assert "get AccelerometerOrientation='normal'" in out
    tilts = [line.split("<")[1].split(">")[0] for line in out.splitlines() if "AccelerometerTilt" in line]
    assert tilts == ["'face-up'", "'vertical'"]


def test_service_compass_first_heading_after_a_claim(service):
    d, binary = service
    q = "0,0,-0.70710678,0.70710678"       # 90 degrees
    r = binary(*ALL, "start", f"call|{COMPASS_P}|{COMPASS_IF}|:1.5|ClaimCompass", f"event:13:{q}", "drain",
               f"event:13:{q}", "drain", f"call|{COMPASS_P}|{COMPASS_IF}|:1.5|ReleaseCompass",
               f"call|{COMPASS_P}|{COMPASS_IF}|:1.5|ClaimCompass", f"event:13:{q}", "drain",
               f"get|{COMPASS_P}|{COMPASS_IF}|CompassHeading")
    # The same heading again is not news, but after a new claim the first one is reported
    assert r.stdout.count("'CompassHeading'") == 2
    heading = float(kv(r.stdout)["get CompassHeading"])
    assert heading == pytest.approx(90.0, abs=0.01)


def test_service_properties(service):
    d, binary = service
    r = binary(*ALL, "start", *(f"get|{P}|{IF}|{p}" for p in (
        "HasAccelerometer", "AccelerometerOrientation", "AccelerometerTilt", "HasAmbientLight", "LightLevelUnit",
        "LightLevel", "HasProximity", "ProximityNear", "OrientationSettleTime", "HasCompass", "CompassHeading",
        "HasPickUp", "HasGravity", "HasMotion", "HasOrientation", "HasGameOrientation", "OrientationReference",
        "Bogus")))
    k = kv(r.stdout)
    assert k["get HasAccelerometer"] == "true" and k["get AccelerometerOrientation"] == "'undefined'"
    assert k["get AccelerometerTilt"] == "'vertical'" and k["get HasAmbientLight"] == "true"
    assert k["get LightLevelUnit"] == "'lux'" and k["get LightLevel"] == "0.0"
    assert k["get HasProximity"] == "true" and k["get ProximityNear"] == "false"
    assert k["get OrientationSettleTime"] == "500" and k["get HasCompass"] == "true"
    assert k["get CompassHeading"] == "-1.0" and k["get HasPickUp"] == "true"
    assert k["get HasGravity"] == "true" and k["get HasMotion"] == "true"
    assert k["get HasOrientation"] == "true" and k["get HasGameOrientation"] == "true"
    assert k["get OrientationReference"] == "'north'"
    assert "get Bogus error Unknown property Bogus" in r.stdout
    r = binary("sensor:18:15:1:0", "start", f"get|{GRAV_P}|{GRAV_IF}|OrientationReference",
               f"get|{P}|{IF}|HasAccelerometer")
    k = kv(r.stdout)
    assert k["get OrientationReference"] == "'relative'" and k["get HasAccelerometer"] == "false"


def test_service_settle_time_property(service):
    d, binary = service
    r = binary(*ALL, "start", f"set|{P}|{BRIDGE}|OrientationSettleTime|uint32 500",
               f"set|{P}|{BRIDGE}|Other|uint32 5", f"set|{P}|{BRIDGE}|OrientationSettleTime|uint32 50",
               f"set|{P}|{IF}|LightLevel|2.0")
    out = r.stdout
    # The same value: accepted, nothing saved
    assert "set OrientationSettleTime=1\n" in out
    assert not (d / "settings").exists()
    assert "set Other=0 Unknown property Other" in out
    assert "set OrientationSettleTime=0 Settle time 50 ms out of 100-3000" in out
    # The iio-sensor-proxy interface has nothing writable
    assert "set LightLevel read-only interface" in out
    r = binary(*ALL, "start", f"set|{P}|{BRIDGE}|OrientationSettleTime|uint32 800")
    assert "settle-ms=800" in (d / "settings").read_text()
    # Read at the next start
    r = binary(*ALL, "start", f"get|{P}|{BRIDGE}|OrientationSettleTime")
    assert kv(r.stdout)["get OrientationSettleTime"] == "800"


def test_service_settings_cannot_be_saved(service):
    d, binary = service
    (d / "settings").mkdir()
    try:
        r = binary(*ALL, "start", f"set|{P}|{BRIDGE}|OrientationSettleTime|uint32 800",
                   f"get|{P}|{BRIDGE}|OrientationSettleTime")
    finally:
        (d / "settings").rmdir()
    assert "Cannot save the settings" in r.stderr
    assert kv(r.stdout)["get OrientationSettleTime"] == "800"


@pytest.mark.parametrize("args,usleeps", [
    (("no-sm",), 0), (("sm-not-ready",), 0),
    # The HAL registers late: looked for twice a second for 60 s
    (("no-service",), 120),
    (("exception:6",), 0), (("status:-1",), 0), (("no-reply:6",), 0),
])
def test_service_without_hal(service, args, usleeps):
    d, binary = service
    r = binary(*args, "sensor:10:1:39.2:0", "start", check=False)
    assert r.returncode == 1
    assert "Cannot initialize the sensors HAL" in r.stderr
    assert f"exit 1 usleeps={usleeps}" in r.stdout
    assert "own-name" not in r.stdout
    if args == ("exception:6",):
        assert "ISensors call 6 failed (status 0, exception -3)" in r.stderr


def test_service_sensor_list_without_reply(service):
    d, binary = service
    r = binary("no-reply:5", "sensor:10:1:39.2:0", "start", f"get|{P}|{IF}|HasAccelerometer")
    # Initialized, but no sensor list: nothing to offer
    assert kv(r.stdout)["get HasAccelerometer"] == "false"


def test_service_activate_failure_is_logged(service):
    d, binary = service
    r = binary("exception:2", *ALL, "start", f"call|{P}|{IF}|:1.5|ClaimLight")
    assert "ISensors call 2 failed (status 0, exception -3)" in r.stderr
    # The client gets its claim all the same (activate went through)
    assert "reply ClaimLight ok ()" in r.stdout


def test_service_hal_death_and_name_loss_end_it(service):
    d, binary = service
    r = binary(*ALL, "start", "callback", "hal-died")
    # The callback for dynamic sensors answers OK without a reply
    assert "callback status=0 reply=(nil)" in r.stdout
    assert "Sensors HAL died" in r.stderr and "loop quit" in r.stdout
    r = binary(*ALL, "start", "lose-name")
    assert "Lost or could not own net.hadess.SensorProxy" in r.stderr and "loop quit" in r.stdout


def test_service_shared_wakeup_sensor_counts_each_event_once(service):
    d, binary = service
    # No rotation vector; the only game rotation vector is a wakeup sensor
    r = binary("sensor:18:15:1:1", "start", "event:18:0,0,0.6,0.8", "drain", "wake-lock")
    # One event out of the queue: both signals, but one handled wakeup event
    assert "GameOrientation(" in r.stdout and " Orientation(" in r.stdout
    assert "wake-lock written=1 items=1" in r.stdout


def test_service_shared_wakeup_sensor_two_events_count_two(service):
    d, binary = service
    r = binary("sensor:18:15:1:1", "start", "event:18:0,0,0.6,0.8", "event:18:0,0,0.8,0.6", "drain",
               "wake-lock")
    assert "wake-lock written=2 " in r.stdout


@pytest.mark.parametrize("method", ["Ping", "Get", "XXXXXXXLight", "ClaimLights", "Release"])
def test_service_method_names_that_are_neither_claim_nor_release(service, method):
    d, binary = service
    # Short names are not read past their end, and only Claim<Name>/Release<Name> are those
    r = binary(*ALL, "start", f"call|{P}|{IF}|:1.5|{method}")
    assert replies(r.stdout) == [
        f"reply {method} error org.freedesktop.DBus.Error.UnknownMethod: Unknown method {method}"]


# ---- ISensors for Android (Waydroid container) ----

GET_VERSION, GET_HASH = 0x00ffffff, 0x00fffffe
ACTIVATE, BATCH, CONFIG_DIRECT, FLUSH, LIST, INITIALIZE, INJECT, REGISTER_DIRECT, OP_MODE, UNREGISTER_DIRECT = \
    range(1, 11)
# Each Android test gets the two queues' memory first
MEM = ("amem|ev|4096", "amem|wl|4096")
EV, WL = "aqueue|ev|96|768", "aqueue|wl|4|64"


def mq(mem, quantum, ring, grantors=4, fd=None, fd_index=0, n_fds=1, n_ints=0, item=None, header=1):
    """An MQDescriptor as Android writes it: the grantors (read and write counter, ring, event
    flag word) and the NativeHandle with the memory's file descriptor."""
    vals = [f"i{header}", "i100", f"i{grantors}"]
    for off, ext in [(0, 8), (8, 8), (64, ring), (16, 4), (24, 4)][:grantors]:
        vals += ["i1", "i24", f"i{fd_index}", f"i{off}", f"l{ext}"]
    vals += ["i1", "i100", f"i{n_fds}"]
    vals += ["i1", "i0", fd or f"h@{mem}"] * n_fds
    vals += [f"i{n_ints}", f"i{item or quantum}", "i1"]
    return vals


def init(events=None, wake_lock=None, callback=True):
    vals = (events or mq("ev", 96, 768)) + (wake_lock or mq("wl", 4, 64)) + (["o"] if callback else [])
    return f"acall|{INITIALIZE}|" + ",".join(vals)


def acall(code, *values):
    return f"acall|{code}|" + ",".join(values)


def areplies(out):
    """Per Android call: its code and the reply parcel."""
    lines = out.splitlines()
    return [(int(line.split()[1]), lines[i + 1][4:]) for i, line in enumerate(lines)
            if line.startswith("acall ") and "status=0 reply=yes" in line]


def queue(out, name):
    return [line.split(" ", 2)[2] for line in out.splitlines() if line.startswith(f"aqueue {name} ")]


def running(out):
    return [line[8:] for line in out.splitlines() if line.startswith("running ")]


ANDROID = (*ALL, "android", "start", *MEM)


def test_android_registers_and_gets_version_hash_and_list(service):
    d, binary = service
    out = binary("sensor:10:1:39.2:0", "sensor:19:1:39.2:3971", "android", "start",
                 acall(GET_VERSION), acall(GET_HASH), acall(LIST)).stdout
    assert f"add-service android.hardware.sensors.ISensors/default" in hal_lines(out)[0]
    assert "local-object android.hardware.sensors.ISensors" in out
    r = areplies(out)
    assert r[0] == (GET_VERSION, "i32 0;i32 1;")
    assert r[1] == (GET_HASH, "i32 0;s16 c38c6b75e592ba5a6400b6348ad7f27db3064410;")
    # Both sensors, the second without its direct report and channel flags (0xf80): 3971 -> 3
    code, parcel = r[2]
    assert parcel.startswith("i32 0;i32 2;parcelable {;i32 10;s16 name;")
    assert parcel.count("parcelable {") == 2
    assert parcel.endswith("s16 ;i32 0;i32 3;};")


def test_android_activates_merged_with_dbus_claims(service):
    d, binary = service
    out = binary(*ANDROID, init(), f"call|{P}|{IF}|:1.5|ClaimAccelerometer", "hal", "running|10",
                 acall(BATCH, "i10", "l50000000"), "running|10", acall(ACTIVATE, "i10", "i1"), "running|10",
                 acall(ACTIVATE, "i10", "i0"), "running|10", acall(ACTIVATE, "i10", "i1"),
                 f"call|{P}|{IF}|:1.5|ReleaseAccelerometer", "hal", "running|10",
                 acall(ACTIVATE, "i10", "i0"), "running|10").stdout
    # D-Bus 10 Hz; Android's batch() alone changes nothing while it has the sensor off; on, it
    # runs at Android's 20 Hz; Android off again: back to D-Bus's rate without switching off
    assert running(out) == ["10=100000000", "10=100000000", "10=50000000", "10=100000000", "10=50000000",
                            "10=-1"]
    r = dict()
    for code, parcel in areplies(out):
        r.setdefault(code, []).append(parcel)
    assert r[BATCH] == ["i32 0;"]
    assert r[ACTIVATE][0] == "request;i32 10;i64 50000000;i64 0;tx 2;i32 0;"
    assert r[ACTIVATE][1] == "request;i32 10;i64 100000000;i64 0;tx 2;i32 0;"
    assert r[ACTIVATE][3] == "request;i32 10;i32 0;tx 1;i32 0;"


def test_android_default_rate_and_limits(service):
    d, binary = service
    # min delay 10 ms, max delay 100 ms
    out = binary("sensor:10:1:39.2:0", "sensor:21:4:10:0:10000:100000", "android", "start", *MEM, init(),
                 acall(ACTIVATE, "i10", "i1"), "running|10", acall(BATCH, "i21", "l1000"),
                 acall(ACTIVATE, "i21", "i1"), "running|21", acall(BATCH, "i21", "l900000000"),
                 "running|21", acall(BATCH, "i21", "l-5")).stdout
    # Without batch() Android gets SENSOR_DELAY_NORMAL (200 ms); periods within the sensor's limits
    assert running(out) == ["10=200000000", "21=10000000", "21=100000000"]
    assert areplies(out)[-1] == (BATCH, "request;i32 21;i64 10000000;i64 0;tx 2;i32 0;")


def test_android_unknown_sensors_and_modes(service):
    d, binary = service
    out = binary(*ANDROID, init(), acall(ACTIVATE, "i99", "i1"), acall(ACTIVATE, "i10"), acall(BATCH, "i99", "l1"),
                 acall(FLUSH, "i99"), acall(FLUSH, "i10"), acall(FLUSH), acall(OP_MODE, "i0"), acall(OP_MODE, "i1"),
                 acall(INJECT), acall(UNREGISTER_DIRECT, "i1"), acall(CONFIG_DIRECT), acall(REGISTER_DIRECT),
                 acall(42)).stdout
    no_such = "i32 -3;s16 No such sensor;i32 0;"
    assert [p for c, p in areplies(out)[1:]] == [
        no_such, no_such, no_such, no_such, "i32 -3;s16 Sensor not active;i32 0;", no_such,
        "i32 0;", "i32 -7;s16 Only the normal mode;i32 0;", "i32 0;", "i32 0;",
        "i32 -7;s16 Not supported;i32 0;", "i32 -7;s16 Not supported;i32 0;", "i32 -7;s16 Not supported;i32 0;"]


def test_android_flush_goes_to_the_hal(service):
    d, binary = service
    out = binary(*ANDROID, init(), acall(ACTIVATE, "i10", "i1"), acall(FLUSH, "i10")).stdout
    assert areplies(out)[-1] == (FLUSH, "request;i32 10;tx 4;i32 0;")
    out = binary("exception:4", *ANDROID, init(), acall(ACTIVATE, "i10", "i1"), acall(FLUSH, "i10")).stdout
    assert areplies(out)[-1] == (FLUSH, "request;i32 10;tx 4;i32 -3;s16 Sensor not active;i32 0;")


def test_android_gets_events_of_its_sensors_and_flush_completes(service):
    d, binary = service
    out = binary(*ANDROID, init(), EV, acall(ACTIVATE, "i10", "i1"), f"call|{P}|{IF}|:1.5|ClaimProximity",
                 "atype|10|1", "event:10:0,0,9.8", "drain", "atype|12|8", "event:12:5", "drain",
                 "atype|11|0", "event:11:1", "drain", EV, "aset|wl|0|8", "atype|10|1", "event:10:0,0,9.8",
                 "drain", WL, EV).stdout
    q = queue(out, "ev")
    assert q[0] == "read=0 write=0 flag=0 items="
    # Its accelerometer and the flush-complete event (type 0), not the proximity only D-Bus uses;
    # Android is woken through the event flag word
    assert q[1] == "read=0 write=192 flag=1 items=10/1,11/0,"
    assert q[2] == "read=0 write=288 flag=1 items=10/1,11/0,10/1,"
    # Its wake lock counts are read away: the bridge holds no wake lock for Android
    assert queue(out, "wl") == ["read=8 write=8 flag=0 items="]


def test_android_queue_full_drops_and_warns(service):
    d, binary = service
    r = binary(*ANDROID, init(), acall(ACTIVATE, "i10", "i1"), "aset|ev|0|768", "atype|10|1", "event:10:1",
               "drain", EV, "aset|ev|0|672", "atype|10|1", "event:10:1", "drain", EV, "aset|ev|0|900",
               "atype|10|1", "event:10:1", "drain", EV)
    q = queue(r.stdout, "ev")
    # Full: nothing written, no wake-up; one item free: written; counters broken: nothing
    assert q[0] == "read=0 write=768 flag=0 items=" + "0/0," * 8
    assert q[1].startswith("read=0 write=768 flag=1 ")
    assert q[2].startswith("read=0 write=900 flag=1 ")
    assert r.stderr.count("Android's sensor queue is full: 1 events dropped") == 2


def test_android_one_shot_sensor_is_off_after_firing(service):
    d, binary = service
    out = binary(*ALL, "sensor:20:17:1:4", "android", "start", *MEM, init(), acall(ACTIVATE, "i20", "i1"),
                 "running|20", "hal", "atype|20|17", "event:20:1", "drain", "running|20", "hal",
                 acall(ACTIVATE, "i20", "i1"), "running|20").stdout
    # Significant motion fires once: the HAL switched it off itself, so no deactivate; Android
    # arms it again
    assert running(out) == ["20=200000000", "20=-1", "20=200000000"]
    assert out.split("running 20=-1\n")[1].startswith("hal=\n")
    assert queue(out, "ev") == []


@pytest.mark.parametrize("events", [
    mq("ev", 96, 768, header=0),           # null parcelable
    mq("ev", 96, 768, grantors=2),         # too few grantors
    mq("ev", 96, 768, grantors=5),         # too many
    mq("ev", 96, 768, fd_index=1),         # a file descriptor the handle has not
    mq("ev", 96, 768, n_fds=0),            # no file descriptor
    mq("ev", 96, 768, fd="h-1"),           # one that is not open
    mq("ev", 96, 768, fd="hpipe"),         # one that cannot be mapped
    mq("ev", 96, 768, n_ints=1),           # ints in the handle
    mq("ev", 96, 768, item=4),             # items of another size
    mq("ev", 96, 50),                      # a ring smaller than one item
    mq("ev", 96, 0),                       # an empty grantor
    mq("ev", 96, 768)[:20],                # cut short
], ids=["null", "2-grantors", "5-grantors", "fd-index", "no-fd", "bad-fd", "pipe", "ints", "item",
        "small-ring", "empty", "short"])
def test_android_unusable_queues_are_refused(service, events):
    d, binary = service
    r = binary(*ANDROID, init(events=events), acall(ACTIVATE, "i10", "i1"), "atype|10|1", "event:10:1", "drain")
    assert areplies(r.stdout)[0] == (INITIALIZE, "i32 -3;s16 Bad queues or callback;i32 0;")
    assert "Android handed over an unusable queue" in r.stderr
    assert "death-handler" not in r.stdout.split("acall 6")[1]


@pytest.mark.parametrize("args", [{"wake_lock": mq("wl", 4, 64, item=96)}, {"callback": False}],
                         ids=["wake-lock", "no-callback"])
def test_android_initialize_needs_both_queues_and_a_callback(service, args):
    d, binary = service
    out = binary(*ANDROID, init(**args)).stdout
    assert areplies(out)[0] == (INITIALIZE, "i32 -3;s16 Bad queues or callback;i32 0;")


def test_android_three_grantors_without_event_flag(service):
    d, binary = service
    out = binary(*ANDROID, init(events=mq("ev", 96, 768, grantors=3)), acall(ACTIVATE, "i10", "i1"),
                 "atype|10|1", "event:10:1", "drain", EV).stdout
    # Written, but there is no flag word to wake Android through
    assert queue(out, "ev") == ["read=0 write=96 flag=0 items=10/1,"]


@pytest.mark.parametrize("gone", ["adie", "apresence|0", init()])
def test_android_gone_or_restarted_switches_its_sensors_off(service, gone):
    d, binary = service
    out = binary(*ANDROID, init(), acall(ACTIVATE, "i10", "i1"), acall(BATCH, "i11", "l1000000"), "hal", gone,
                 "hal", "running|10", acall(ACTIVATE, "i11", "i1"), "running|11").stdout
    after = out.split("acall 2 status=0 reply=yes\n", 1)[1]
    assert "request;i32 10;i32 0;tx 1;" in "".join(hal_lines(after))
    # Its batch() periods are forgotten as well: the light comes back at the default
    assert running(out) == ["10=-1", "11=200000000"]


def test_android_registers_again_when_the_container_restarts(service):
    d, binary = service
    r = binary(*ANDROID, "apresence|0", "apresence|1", "aadded|0", "aadded|-2147483647")
    assert hal_lines(r.stdout)[-3:-1] == ["", "add-service android.hardware.sensors.ISensors/default;"]
    assert "Cannot serve the sensors to Android (status -2147483647)" in r.stderr


def test_android_sensors_off_on_the_way_out(service):
    d, binary = service
    out = binary(*ANDROID, init(), acall(ACTIVATE, "i11", "i1"), "hal", "sigterm").stdout
    assert hal_lines(out)[-1] == "request;i32 11;i32 0;tx 1;"


def test_android_waits_for_the_node(service):
    d, binary = service
    if os.geteuid() == 0:
        pytest.skip("root opens any node")
    out = binary(*ALL, "start", "spin", "anode|0", "spin", "anode|600", "spin").stdout
    assert out.count("android sm=none") == 2
    assert "android sm=set" in out
    assert hal_lines(out)[-2].endswith("anbox-binder;add-service android.hardware.sensors.ISensors/default;")


def test_android_node_that_is_no_binder(service):
    d, binary = service
    out = binary(*ALL, "no-android-sm", "android", "start", "spin").stdout
    assert "android sm=none" in out
    assert "local-object android.hardware.sensors.ISensors\n" not in out
