# plinius-wake: when a pick-up, a tilt out of lying flat or a double tap wakes the screen
# (screen state from DRM and the always-on display flag, hold time while flat), the wake key
# written to uinput, the touch chip's gesture mode and the settings. The proximity check talks
# to sensor-proxy-bridge over D-Bus and is not run here (no sensors proxy: wakes at once).

import functools
import os
import shutil

import pytest

from dhw_harness import PKG, build_harness, kv, run, sandboxed_source

SRC = PKG / "plinius-wake" / "plinius-wake.c"

HARNESS = r"""
#define _GNU_SOURCE
#include <unistd.h>
#include <fcntl.h>
#include <glib.h>
#include <gio/gio.h>
#include <stdio.h>
#include <stdlib.h>
#include <linux/input.h>
static gint64 fake_now;
#define g_get_monotonic_time() (fake_now)
static gboolean fake_emit_signal(GDBusConnection *c, const char *dest, const char *path, const char *iface,
				 const char *name, GVariant *params, GError **error)
{
	gchar *text = g_variant_print(g_variant_ref_sink(params), FALSE);
	printf("signal=%s\n", text);
	g_free(text);
	g_variant_unref(params);
	return TRUE;
}
#define g_dbus_connection_emit_signal fake_emit_signal
#define main real_main
#include "plinius-wake.c"
#undef main

static int key_rd = -1;

static void keys(void)
{
	/* A wake is a press and a release of KEY_WAKEUP, each with its SYN_REPORT */
	struct input_event ev[2];
	int presses = 0, releases = 0;
	while (read(key_rd, ev, sizeof ev) == sizeof ev)
		if (ev[0].type == EV_KEY && ev[0].code == KEY_WAKEUP && ev[1].type == EV_SYN &&
		    ev[1].code == SYN_REPORT)
			ev[0].value ? presses++ : releases++;
	printf("wake-keys=%d\n", presses == releases ? presses : -1);
}

int main(int argc, char **argv)
{
	int p[2];
	if (pipe2(p, O_NONBLOCK) < 0)
		abort();
	key_rd = p[0];
	self.uinput_fd = p[1];
	for (int i = 1; i < argc; i++) {
		g_auto(GStrv) a = g_strsplit(argv[i], ":", -1);
		if (!strcmp(a[0], "tilt-setting")) {
			self.tilt = atoi(a[1]);
		} else if (!strcmp(a[0], "brighten")) {
			self.brighten = atoi(a[1]);
		} else if (!strcmp(a[0], "claimed")) {
			self.accelerometer_claimed = atoi(a[1]);
		} else if (!strcmp(a[0], "picked-up")) {
			on_bridge_signal(NULL, NULL, "PickedUp", NULL, NULL);
			keys();
		} else if (!strcmp(a[0], "screen")) {
			printf("screen=%d\n", screen_on());
		} else if (!strcmp(a[0], "accel")) {
			/* accel:<ms>:<tilt>: AccelerometerTilt changed at that time */
			fake_now = g_ascii_strtoll(a[1], NULL, 10) * 1000;
			GVariantBuilder b;
			g_variant_builder_init(&b, G_VARIANT_TYPE("a{sv}"));
			g_variant_builder_add(&b, "{sv}", "AccelerometerTilt", g_variant_new_string(a[2]));
			g_autoptr(GVariant) changed = g_variant_ref_sink(g_variant_builder_end(&b));
			on_sensors_properties_changed(NULL, changed, NULL, NULL);
			printf("at %s ", a[1]);
			keys();
		} else if (!strcmp(a[0], "double-tap")) {
			self.double_tap = atoi(a[1]);
			apply_double_tap();
			printf("tap: dir=%d fd=%d rescan=%d\n", self.touch_dir != NULL, self.tap_fd >= 0,
			       self.rescan_source != 0);
		} else if (!strcmp(a[0], "tap-event")) {
			on_double_tap(self.tap_fd, G_IO_PRI, NULL);
			keys();
		} else if (!strcmp(a[0], "load")) {
			load_settings();
			printf("settings=%d %d %d\n", self.double_tap, self.tilt, self.brighten);
		} else if (!strcmp(a[0], "set")) {
			GError *error = NULL;
			gboolean ok = on_set_property(NULL, NULL, NULL, NULL, a[1], g_variant_new_boolean(atoi(a[2])),
						      &error, NULL);
			printf("set %s=%d\n", a[1], ok);
			g_clear_error(&error);
		} else if (!strcmp(a[0], "get")) {
			GError *error = NULL;
			g_autoptr(GVariant) v = on_get_property(NULL, NULL, NULL, NULL, a[1], &error, NULL);
			g_autofree char *text = v ? g_variant_print(v, FALSE) : g_strdup("error");
			printf("get %s=%s\n", a[1], text);
			g_clear_error(&error);
		}
	}
	return 0;
}
"""


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    d = tmp_path_factory.mktemp("wake")
    sandboxed_source(SRC, d, [
        ("/var/lib/plinius-wake/settings", d / "settings"),
        ("/sys/bus/spi/devices", d / "spi"),
        ("/sys/class/drm", d / "drm"),
        ("/run/user/*/plinius-aod", f"{d}/run-user/*/plinius-aod"),
        ("/dev/uinput", d / "uinput"),
    ])
    exe = build_harness(d, "wake", HARNESS, pkgs=("gio-unix-2.0",))
    return d, functools.partial(run, exe)


@pytest.fixture
def wake(built):
    d, binary = built
    for sub in ("spi", "drm", "run-user"):
        shutil.rmtree(d / sub, ignore_errors=True)
    (d / "settings").unlink(missing_ok=True)
    (d / "drm/card0").mkdir(parents=True)          # no dpms: the card itself
    (d / "drm/card0-DSI-1").mkdir()
    (d / "run-user/1000").mkdir(parents=True)
    return d, binary


def screen(d, state):
    (d / "drm/card0-DSI-1/dpms").write_text(state + "\n")


def wake_keys(out):
    return [int(line.split("wake-keys=")[1]) for line in out.splitlines() if "wake-keys=" in line]


def test_screen_state(wake):
    d, binary = wake
    assert kv(binary("screen").stdout)["screen"] == "0"     # unknown counts as off
    screen(d, "On")
    assert kv(binary("screen").stdout)["screen"] == "1"
    (d / "run-user/1000/plinius-aod").write_text("")
    assert kv(binary("screen").stdout)["screen"] == "0"     # always-on display: off


@pytest.mark.parametrize("state,tilt,brighten,woken", [
    ("Off", 1, 0, 1),   # screen off: Tilt wakes
    ("Off", 0, 1, 0),   # PickUpBrighten does not switch the screen on
    ("On", 0, 1, 1),    # screen on (dimmed): the pick-up counts as activity
    ("On", 1, 0, 0),
    ("Off", 0, 0, 0),
])
def test_pick_up(wake, state, tilt, brighten, woken):
    d, binary = wake
    screen(d, state)
    assert wake_keys(binary(f"tilt-setting:{tilt}", f"brighten:{brighten}", "picked-up").stdout) == [woken]


def test_pick_up_during_always_on_display_needs_tilt(wake):
    d, binary = wake
    screen(d, "On")
    (d / "run-user/1000/plinius-aod").write_text("")
    assert wake_keys(binary("tilt-setting:0", "brighten:1", "picked-up").stdout) == [0]
    assert wake_keys(binary("tilt-setting:1", "brighten:0", "picked-up").stdout) == [1]


def test_tilt_fallback_hold_time(wake):
    d, binary = wake
    screen(d, "Off")
    out = binary("tilt-setting:1", "claimed:1",
                 "accel:1000:face-up", "accel:2000:vertical",            # flat only 1 s
                 "accel:3000:face-up", "accel:3500:face-up",            # still flat, since 3000
                 "accel:4600:tilted-up",                                # 1.6 s: wakes
                 "accel:5000:vertical").stdout                           # not flat before
    assert wake_keys(out) == [0, 0, 0, 0, 1, 0]


def test_tilt_fallback_only_while_claimed(wake):
    d, binary = wake
    screen(d, "Off")
    out = binary("tilt-setting:1", "claimed:0", "accel:0:face-up", "accel:5000:vertical").stdout
    assert wake_keys(out) == [0, 0]


def test_tilt_fallback_off_without_settings(wake):
    d, binary = wake
    screen(d, "Off")
    out = binary("claimed:1", "accel:0:face-up", "accel:5000:vertical").stdout
    assert wake_keys(out) == [0, 0]


def touch(d):
    t = d / "spi/spi0.0"
    t.mkdir(parents=True)
    (t / "fts_gesture_mode").write_text("0")
    (t / "double_tap_pressed").write_text("0")
    return t


def test_double_tap(wake):
    d, binary = wake
    t = touch(d)
    (d / "spi/spi1.0").mkdir()        # another SPI device without the attributes
    out = binary("double-tap:1", "tap-event").stdout
    assert "tap: dir=1 fd=1 rescan=0" in out
    assert wake_keys(out) == [0]
    assert (t / "fts_gesture_mode").read_text() == "1"
    (t / "double_tap_pressed").write_text("1")
    out = binary("double-tap:1", "tap-event", "double-tap:0").stdout
    assert wake_keys(out) == [1]
    assert "tap: dir=1 fd=0 rescan=0" in out
    # Switched off: the chip leaves gesture mode
    assert (t / "fts_gesture_mode").read_text() == "0"


def test_double_tap_without_touch_driver_rescans(wake):
    d, binary = wake
    out = binary("double-tap:1", "get:HasDoubleTap").stdout
    assert "tap: dir=0 fd=0 rescan=1" in out
    assert kv(out)["get HasDoubleTap"] == "false"


def test_settings(wake):
    d, binary = wake
    touch(d)
    out = binary("set:Tilt:1", "set:PickUpBrighten:1", "set:HasTilt:1").stdout
    k = kv(out)
    assert k["set Tilt"] == "1" and k["set PickUpBrighten"] == "1"
    assert k["set HasTilt"] == "0"          # read-only
    assert "'Tilt': <true>" in out
    text = (d / "settings").read_text()
    assert "tilt=true" in text and "pick-up-brighten=true" in text and "double-tap=false" in text
    assert kv(binary("load").stdout)["settings"] == "0 1 1"
    assert kv(binary("get:HasDoubleTap").stdout)["get HasDoubleTap"] == "true"


# ---- The service as it runs: main(), its D-Bus object, sensor-proxy-bridge behind proxies ----
#
# fakes/dhw_scripted_gdbus.h stands in for the bus and the proxies of net.hadess.SensorProxy and
# org.plinius.SensorProxyBridge; the main loop runs the script after "--". The uinput device is a
# plain file: its ioctls are printed and succeed (unless scripted to fail), the wake keys land in
# the file, so a test counts them there.

SERVICE = r"""
#define G_DISABLE_CAST_CHECKS
#include <errno.h>
#include <fcntl.h>
#include <glib.h>
#include <gio/gio.h>
#include <linux/uinput.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/ioctl.h>
#include <unistd.h>
#include "dhw_scripted_gdbus.h"

static gint64 fake_now;
#define g_get_monotonic_time() (fake_now)

static void script_loop(GMainLoop *loop);
#define g_main_loop_run script_loop

/* uinput: printed; fails for the request named in ioctl_fails */
static unsigned long ioctl_fails;
static int fake_ioctl(int fd, unsigned long request, ...)
{
	const char *name = request == UI_SET_EVBIT ? "UI_SET_EVBIT" : request == UI_SET_KEYBIT ? "UI_SET_KEYBIT"
			 : request == UI_DEV_SETUP ? "UI_DEV_SETUP" : request == UI_DEV_CREATE ? "UI_DEV_CREATE"
			 : request == UI_DEV_DESTROY ? "UI_DEV_DESTROY" : "?";
	printf("ioctl %s\n", name);
	if (request == ioctl_fails) {
		errno = ENOTTY;
		return -1;
	}
	return 0;
}
#define ioctl fake_ioctl

#define main real_main
#include "plinius-wake.c"
#undef main
#undef g_main_loop_run
#undef ioctl

static char **script;
static const char *uinput_path;
static long keys_seen;

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

/* Wake keys written since the last look: presses with their release and SYN_REPORTs */
static void keys(void)
{
	g_autofree char *data = NULL;
	gsize len = 0;
	g_file_get_contents(uinput_path, &data, &len, NULL);
	const struct input_event *ev = (const struct input_event *)data;
	long n = len / sizeof *ev, presses = 0, releases = 0, bad = 0;
	for (long i = keys_seen; i + 1 < n; i += 2) {
		if (ev[i].type != EV_KEY || ev[i].code != KEY_WAKEUP || ev[i + 1].type != EV_SYN)
			bad++;
		else
			ev[i].value ? presses++ : releases++;
	}
	keys_seen = n;
	printf("wake-keys=%ld\n", bad || presses != releases ? -1 : presses);
}

static const char *state_word = "state";
static void print_state(void)
{
	printf("%s tap=%d fd=%d rescan=%d accel=%d pickup=%d display=%d proximity=%d\n", state_word, self.double_tap,
	       self.tap_fd >= 0, self.rescan_source != 0, self.accelerometer_claimed, self.pick_up_claimed,
	       self.display_on, self.proximity_source != 0);
}

static gboolean lose_name(gpointer data)
{
	sg_lose_name();
	return G_SOURCE_REMOVE;
}

#define SENSORS "net.hadess.SensorProxy"
#define BRIDGE "org.plinius.SensorProxyBridge"

static const char *iface_of(const char *s)
{
	return !strcmp(s, "bridge") ? BRIDGE : SENSORS;
}

static void command(const char *a)
{
	g_auto(GStrv) w = g_strsplit(a, ":", 3);
	if (!strcmp(w[0], "reply")) {
		g_auto(GStrv) kv = g_strsplit(a + 6, "=", 2);
		sg_reply(kv[0], kv[1]);
	} else if (!strcmp(w[0], "prop") || !strcmp(w[0], "changed")) {
		/* prop:<sensors|bridge>:<Name>=<value> */
		g_auto(GStrv) kv = g_strsplit(w[2], "=", 2);
		if (!strcmp(w[0], "prop"))
			sg_proxy_prop(iface_of(w[1]), kv[0], kv[1]);
		else
			sg_proxy_changed(iface_of(w[1]), kv[0], kv[1]);
	} else if (!strcmp(w[0], "owner")) {
		sg_proxy_owner(iface_of(w[1]), strcmp(w[2], "-") ? w[2] : NULL);
		print_state();
	} else if (!strcmp(w[0], "proxy-fail")) {
		sg_proxy_fails = g_strdup(iface_of(w[1]));
	} else if (!strcmp(w[0], "picked-up")) {
		sg_proxy_signal(BRIDGE, "PickedUp");
	} else if (!strcmp(w[0], "now")) {
		fake_now = g_ascii_strtoll(w[1], NULL, 10) * 1000;
	} else if (!strcmp(w[0], "display")) {
		g_autofree char *params = g_strdup_printf("(%s,)", w[1]);
		sg_invoke("/org/plinius/Wake", "org.plinius.Wake", ":1.3", "SetDisplay", params);
		print_state();
	} else if (!strcmp(w[0], "get")) {
		sg_get("/org/plinius/Wake", "org.plinius.Wake", w[1]);
	} else if (!strcmp(w[0], "set")) {
		g_auto(GStrv) kv = g_strsplit(w[1], "=", 2);
		sg_set("/org/plinius/Wake", "org.plinius.Wake", kv[0], kv[1]);
		print_state();
	} else if (!strcmp(w[0], "run")) {
		run_for(atoi(w[1]));
	} else if (!strcmp(w[0], "keys")) {
		keys();
	} else if (!strcmp(w[0], "state")) {
		print_state();
	} else if (!strcmp(w[0], "lose-name")) {
		/* From the running loop, as GDBus calls it: the loop ends */
		g_idle_add(lose_name, NULL);
		g_main_loop_run(self.loop);
		printf("loop quit\n");
	} else if (!strcmp(w[0], "sigterm")) {
		raise(SIGTERM);
		g_main_loop_run(self.loop);
		printf("loop quit\n");
	} else if (!strcmp(w[0], "rescan")) {
		printf("rescan=%s\n", rescan_touch(NULL) ? "continue" : "remove");
	} else if (!strcmp(w[0], "touch")) {
		/* touch:<dir>: the touch module came, with its attributes */
		g_mkdir_with_parents(w[1], 0755);
		g_autofree char *mode = g_build_filename(w[1], "fts_gesture_mode", NULL);
		g_autofree char *tap = g_build_filename(w[1], "double_tap_pressed", NULL);
		g_file_set_contents(mode, "0", -1, NULL);
		g_file_set_contents(tap, "0", -1, NULL);
	} else if (!strcmp(w[0], "tap-event")) {
		on_double_tap(self.tap_fd, G_IO_PRI, NULL);
		keys();
	} else if (!strcmp(w[0], "tap-fd-bad")) {
		/* The attribute went away under the open fd: reads fail */
		dup2(open("/dev/null", O_WRONLY), self.tap_fd);
		on_double_tap(self.tap_fd, G_IO_PRI, NULL);
	} else if (!strcmp(w[0], "uinput-bad")) {
		close(self.uinput_fd);
	} else {
		g_error("unknown command %s", a);
	}
	fflush(stdout);
}

static void script_loop(GMainLoop *loop)
{
	while (g_main_context_iteration(NULL, FALSE))
		;
	print_state();
	for (; script && *script; script++)
		command(*script);
}

int main(int argc, char **argv)
{
	uinput_path = argv[1];
	if (argv[2] && g_str_has_prefix(argv[2], "ioctl-fails:"))
		ioctl_fails = !strcmp(argv[2] + 12, "UI_DEV_CREATE") ? UI_DEV_CREATE : UI_SET_EVBIT;
	script = argv + 2;
	if (ioctl_fails)
		script++;
	while (*script && strcmp(*script, "start")) {
		command(*script);
		script++;
	}
	if (*script)
		script++;
	int r = real_main();
	state_word = "final";
	print_state();
	return r;
}
"""


@pytest.fixture(scope="module")
def service_built(tmp_path_factory):
    d = tmp_path_factory.mktemp("wake-service")
    sandboxed_source(SRC, d, [
        ("/var/lib/plinius-wake/settings", d / "settings"),
        ("/sys/bus/spi/devices", d / "spi"),
        ("/sys/class/drm", d / "drm"),
        ("/run/user/*/plinius-aod", f"{d}/run-user/*/plinius-aod"),
        ("/dev/uinput", d / "uinput"),
    ])
    exe = build_harness(d, "wake_service", SERVICE, pkgs=("gio-unix-2.0",))
    return d, lambda *args, **kw: run(exe, d / "uinput", *args, **kw)


@pytest.fixture
def service(service_built):
    d, binary = service_built
    for sub in ("spi", "drm", "run-user"):
        shutil.rmtree(d / sub, ignore_errors=True)
    (d / "settings").unlink(missing_ok=True)
    (d / "uinput").write_bytes(b"")
    (d / "drm/card0-DSI-1").mkdir(parents=True)
    (d / "drm/card0-DSI-1/dpms").write_text("Off\n")
    (d / "run-user/1000").mkdir(parents=True)
    return d, binary


def state_lines(out):
    return [dict(x.split("=") for x in line.split()[1:]) for line in out.splitlines() if line.startswith("state ")]


PICKUP = ("prop:bridge:HasPickUp=true",)
ACCEL = ("prop:sensors:HasAccelerometer=true",)
CLAIMS_OK = ("reply:ClaimPickUp=()", "reply:ReleasePickUp=()", "reply:ClaimAccelerometer=()",
             "reply:ReleaseAccelerometer=()")


def test_service_start_and_stop(service):
    d, binary = service
    (d / "settings").write_text("[wake]\ndouble-tap=false\ntilt=true\npick-up-brighten=false\n")
    r = binary(*PICKUP, *CLAIMS_OK, "start", "sigterm")
    out = r.stdout
    # The uinput device with KEY_WAKEUP, the object and both proxies on the bridge's name
    assert out.index("ioctl UI_SET_EVBIT") < out.index("ioctl UI_SET_KEYBIT") < out.index("ioctl UI_DEV_CREATE")
    assert "own-name system org.plinius.Wake" in out
    assert "register /org/plinius/Wake org.plinius.Wake" in out
    assert "proxy net.hadess.SensorProxy /net/hadess/SensorProxy net.hadess.SensorProxy" in out
    assert "proxy net.hadess.SensorProxy /net/hadess/SensorProxy org.plinius.SensorProxyBridge" in out
    # Tilt on from the settings: the pick-up gesture is claimed (not the accelerometer)
    first = state_lines(out)[0]
    assert first["pickup"] == "1" and first["accel"] == "0" and first["display"] == "0"
    assert "Pick-up detection on (PickUp)" in r.stderr
    # SIGTERM: the loop ends, the gesture is released, the device destroyed
    assert out.index("proxy-call-sync org.plinius.SensorProxyBridge ReleasePickUp") > out.index("ClaimPickUp")
    final = [line for line in out.splitlines() if line.startswith("final ")]
    assert "pickup=0" in final[0]
    assert "unown-name 7" in out and out.rstrip().splitlines()[-2] == "ioctl UI_DEV_DESTROY"
    assert r.returncode == 0


def test_service_name_lost_ends_the_loop(service):
    d, binary = service
    r = binary("start", "lose-name")
    assert "Lost or could not own org.plinius.Wake" in r.stderr
    assert "loop quit" in r.stdout
    assert "ioctl UI_DEV_DESTROY" in r.stdout


@pytest.mark.parametrize("failing", ["UI_SET_EVBIT", "UI_DEV_CREATE"])
def test_service_without_uinput(service, failing):
    d, binary = service
    r = binary(f"ioctl-fails:{failing}", "start", check=False)
    assert r.returncode == 1
    assert "Cannot create the uinput device" in r.stderr
    assert "own-name" not in r.stdout


def test_service_without_uinput_node(service):
    d, binary = service
    (d / "uinput").unlink()
    r = binary("start", check=False)
    assert r.returncode == 1
    assert "Cannot create the uinput device: No such file or directory" in r.stderr


def test_pick_up_with_proximity_free(service):
    d, binary = service
    r = binary(*PICKUP, *CLAIMS_OK, "prop:sensors:HasProximity=true", "prop:sensors:ProximityNear=false",
               "reply:ClaimProximity=()", "start", "set:Tilt=true", "picked-up", "state", "picked-up", "keys",
               "run:450", "keys")
    out = r.stdout
    # The proximity sensor is claimed and gets 300 ms; a second pick-up meanwhile is not a second check
    assert out.count("proxy-call-sync net.hadess.SensorProxy ClaimProximity") == 1
    assert state_lines(out)[-1]["proximity"] == "1"
    assert [line for line in out.splitlines() if line.startswith("wake-keys=")] == ["wake-keys=0", "wake-keys=1"]
    assert "proxy-call net.hadess.SensorProxy ReleaseProximity" in out
    assert "Waking the screen (pick-up)" in r.stderr


def test_pick_up_in_a_pocket(service):
    d, binary = service
    r = binary(*PICKUP, *CLAIMS_OK, "prop:sensors:HasProximity=true", "prop:sensors:ProximityNear=true",
               "reply:ClaimProximity=()", "start", "set:Tilt=true", "picked-up", "run:450", "keys")
    assert "wake-keys=0" in r.stdout
    assert "pick-up ignored: proximity sensor covered" in r.stderr
    assert "proxy-call net.hadess.SensorProxy ReleaseProximity" in r.stdout


def test_pick_up_when_proximity_cannot_be_claimed(service):
    d, binary = service
    r = binary(*PICKUP, *CLAIMS_OK, "prop:sensors:HasProximity=true", "reply:ClaimProximity=!busy",
               "start", "set:Tilt=true", "picked-up", "keys")
    # Better woken than not at all
    assert "wake-keys=1" in r.stdout
    assert "Cannot claim the proximity sensor: busy" in r.stderr


def test_wake_key_write_failure_is_logged(service):
    d, binary = service
    r = binary(*PICKUP, *CLAIMS_OK, "start", "set:Tilt=true", "uinput-bad", "picked-up")
    assert "Writing the wake key failed: Bad file descriptor" in r.stderr


def test_display_hook_claims_for_brighten(service):
    d, binary = service
    r = binary(*PICKUP, *CLAIMS_OK, "start", "set:PickUpBrighten=true", "display:true", "display:true",
               "display:false")
    out = r.stdout
    s = state_lines(out)
    # PickUpBrighten needs the sensor only while the screen is on
    set_state, on, again, off = s[-4:]
    assert set_state["pickup"] == "0"
    assert on["pickup"] == "1" and on["display"] == "1"
    assert again == on
    assert off["pickup"] == "0" and off["display"] == "0"
    assert out.count("reply SetDisplay ok ()") == 3
    assert out.count("ClaimPickUp") == 1 and out.count("ReleasePickUp") == 1


def test_accelerometer_fallback_and_tilt_signal(service):
    d, binary = service
    r = binary(*ACCEL, *CLAIMS_OK, "start", "set:Tilt=true", "now:0", "changed:sensors:AccelerometerTilt='face-up'",
               "now:2000", "changed:sensors:AccelerometerTilt='vertical'", "keys",
               "changed:sensors:HasProximity=false", "keys")
    s = state_lines(r.stdout)
    # No pick-up gesture: the accelerometer, and its tilt out of lying flat wakes
    assert s[-1]["accel"] == "1" and s[-1]["pickup"] == "0"
    assert "Pick-up detection on (Accelerometer)" in r.stderr
    assert [line for line in r.stdout.splitlines() if line.startswith("wake-keys=")] == ["wake-keys=1",
                                                                                          "wake-keys=0"]


def test_claim_failure_leaves_it_unclaimed(service):
    d, binary = service
    r = binary(*PICKUP, "reply:ClaimPickUp=!no such sensor", "start", "set:Tilt=true")
    assert state_lines(r.stdout)[-1]["pickup"] == "0"
    assert "Cannot claim the PickUp sensor: no such sensor" in r.stderr


def test_release_failure_still_counts_as_released(service):
    d, binary = service
    r = binary(*PICKUP, "reply:ClaimPickUp=()", "start", "set:Tilt=true", "set:Tilt=false")
    # A bridge that does not answer has no claim to keep either
    assert state_lines(r.stdout)[-1]["pickup"] == "0"


def test_bridge_restart_claims_again(service):
    d, binary = service
    r = binary(*PICKUP, *CLAIMS_OK, "start", "set:Tilt=true", "owner:bridge:-", "owner:bridge::1.77")
    out = r.stdout
    gone, back = state_lines(out)[-2:]
    # Gone: its claims went with it, and without an owner there is no pick-up sensor (nor the
    # accelerometer of the same service, which has one); back: claimed again
    assert gone["pickup"] == "0"
    assert back["pickup"] == "1"
    assert out.count("proxy-call-sync org.plinius.SensorProxyBridge ClaimPickUp") == 2


def test_properties(service):
    d, binary = service
    t = d / "spi/spi0.0"
    t.mkdir(parents=True)
    (t / "fts_gesture_mode").write_text("0")
    (t / "double_tap_pressed").write_text("0")
    r = binary(*ACCEL, *CLAIMS_OK, "start", "get:DoubleTap", "get:Tilt", "get:PickUpBrighten", "get:HasDoubleTap",
               "get:HasTilt", "get:Nonsense", "set:DoubleTap=true", "set:DoubleTap=true", "get:DoubleTap",
               "set:Tilt=false", "set:PickUpBrighten=false", "set:HasTilt=true")
    k = kv(r.stdout)
    assert k["get DoubleTap"] == "true"
    assert (k["get Tilt"], k["get PickUpBrighten"]) == ("false", "false")
    assert k["get HasDoubleTap"] == "true" and k["get HasTilt"] == "true"
    assert "get Nonsense error Unknown property Nonsense" in r.stdout
    # Unchanged values are accepted without a signal or a save
    assert r.stdout.count("PropertiesChanged") == 1
    assert "signal * /org/plinius/Wake org.freedesktop.DBus.Properties PropertiesChanged" \
           "('org.plinius.Wake', {'DoubleTap': <true>}, [])" in r.stdout
    assert "set HasTilt=0 Property HasTilt is read-only" in r.stdout
    assert "double-tap=true" in (d / "settings").read_text()
    assert "Double tap to wake on" in r.stderr
    # At the exit the chip leaves gesture mode again, as the driver had it
    assert (t / "fts_gesture_mode").read_text() == "0"


def test_settings_that_cannot_be_saved(service):
    d, binary = service
    (d / "settings").mkdir()
    try:
        r = binary(*PICKUP, *CLAIMS_OK, "start", "set:Tilt=true")
    finally:
        (d / "settings").rmdir()
    assert "Cannot save the settings" in r.stderr
    # Still applied for this run
    assert state_lines(r.stdout)[-1]["pickup"] == "1"


def test_without_sensor_proxies(service):
    d, binary = service
    r = binary("proxy-fail:sensors", "start", "set:Tilt=true", "get:HasTilt", "picked-up")
    assert "No sensors: fake bus: no proxy for net.hadess.SensorProxy" in r.stderr
    assert kv(r.stdout)["get HasTilt"] == "false"
    r = binary("proxy-fail:bridge", "start", "get:HasTilt")
    assert "No sensor-proxy-bridge: fake bus: no proxy for org.plinius.SensorProxyBridge" in r.stderr


def test_double_tap_touch_module_comes_later(service):
    d, binary = service
    (d / "settings").write_text("[wake]\ndouble-tap=true\n")
    t = d / "spi/spi0.0"
    r = binary("start", "state", "rescan", f"touch:{t}", "rescan", "rescan")
    s = state_lines(r.stdout)
    # Not there at start: looked for again every 5 s until the touch module is loaded
    assert s[0]["rescan"] == "1" and s[0]["fd"] == "0"
    assert [line for line in r.stdout.splitlines() if line.startswith("rescan=")] == [
        "rescan=continue", "rescan=remove", "rescan=remove"]
    assert "Double tap to wake on" in r.stderr


def test_rescan_stops_when_double_tap_is_switched_off(service):
    d, binary = service
    (d / "settings").write_text("[wake]\ndouble-tap=true\n")
    r = binary("start", "set:DoubleTap=false", "rescan")
    assert state_lines(r.stdout)[-1]["rescan"] == "0"
    assert "rescan=remove" in r.stdout


def test_double_tap_attribute_problems(service):
    d, binary = service
    t = d / "spi/spi0.0"
    t.mkdir(parents=True)
    (t / "fts_gesture_mode").mkdir()            # cannot be written
    (t / "double_tap_pressed").write_text("0")
    r = binary("start", "set:DoubleTap=true")
    assert "Cannot write" in r.stderr and "fts_gesture_mode" in r.stderr
    assert state_lines(r.stdout)[-1]["fd"] == "0" and state_lines(r.stdout)[-1]["rescan"] == "1"
    (t / "fts_gesture_mode").rmdir()
    (t / "fts_gesture_mode").write_text("0")
    (t / "double_tap_pressed").unlink()
    (t / "double_tap_pressed").mkdir()          # opens, but cannot be read
    r = binary("start", "set:DoubleTap=true", "tap-event")
    assert "Reading" in r.stderr and "double_tap_pressed failed: Is a directory" in r.stderr
    assert "wake-keys=0" in r.stdout


@pytest.mark.skipif(os.geteuid() == 0, reason="root opens an unreadable file all the same")
def test_double_tap_attribute_unreadable(service):
    d, binary = service
    t = d / "spi/spi0.0"
    t.mkdir(parents=True)
    (t / "fts_gesture_mode").write_text("0")
    (t / "double_tap_pressed").write_text("0")
    (t / "double_tap_pressed").chmod(0)
    try:
        r = binary("start", "set:DoubleTap=true")
    finally:
        (t / "double_tap_pressed").chmod(0o644)
    assert "Cannot open" in r.stderr and "Permission denied" in r.stderr
    assert state_lines(r.stdout)[-1]["rescan"] == "1"


def test_double_tap_read_error_keeps_watching(service):
    d, binary = service
    t = d / "spi/spi0.0"
    t.mkdir(parents=True)
    (t / "fts_gesture_mode").write_text("0")
    (t / "double_tap_pressed").write_text("1")
    r = binary("start", "set:DoubleTap=true", "tap-fd-bad", "keys")
    assert "Reading double_tap_pressed failed: Bad file descriptor" in r.stderr
    assert "wake-keys=0" in r.stdout


def test_proxy_owner_is_not_leaked(service):
    import subprocess
    d, binary = service
    env = dict(os.environ, ASAN_OPTIONS="detect_leaks=1:exitcode=86", LSAN_OPTIONS="exitcode=0",
               G_DEBUG="", LC_ALL="C")
    r = subprocess.run([str(d / "wake_service"), str(d / "uinput"), *PICKUP, *CLAIMS_OK, "start",
                        "set:PickUpBrighten=true", "display:true", "display:false", "display:true"],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    leaks = [block for block in r.stderr.split("\n\n") if "leak of" in block]
    assert not [block for block in leaks if " in proxy_has " in block]
