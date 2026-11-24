# plinius-mce: charger and battery state from the kernel's battery (what ofono uses to quiet the
# modem), the display state with the Wi-Fi driver's suspend mode, and which signals go out.
# sysfs and /proc/net/wlan/driver are files in tmp, signals are printed.

import functools
import os
import shutil

import pytest

from dhw_harness import PKG, build_harness, run, sandboxed_source

SRC = PKG / "plinius-mce" / "plinius-mce.c"

HARNESS = r"""
#include <glib.h>
#include <gio/gio.h>
#include <stdio.h>
#include <stdlib.h>
static gboolean fake_emit_signal(GDBusConnection *c, const char *dest, const char *path, const char *iface,
				 const char *name, GVariant *params, GError **error)
{
	gchar *text = g_variant_print(g_variant_ref_sink(params), FALSE);
	printf("signal %s%s\n", name, text);
	g_free(text);
	g_variant_unref(params);
	return TRUE;
}
#define g_dbus_connection_emit_signal fake_emit_signal
#define main real_main
#include "plinius-mce.c"
#undef main

static void put(const char *path, const char *text)
{
	if (!strcmp(text, "-"))
		remove(path);
	else
		g_file_set_contents(path, text, -1, NULL);
}

int main(int argc, char **argv)
{
	for (int i = 1; i < argc; i++) {
		g_auto(GStrv) a = g_strsplit(argv[i], "|", -1);
		if (!strcmp(a[0], "power")) {
			/* power|status|capacity|notify */
			put(BATTERY "/status", a[1]);
			put(BATTERY "/capacity", a[2]);
			read_power(atoi(a[3]));
			printf("state %s/%s: charger=%s battery=%s level=%d\n", a[1], a[2], mce.charger,
			       mce.battery_status, mce.battery_level);
		} else if (!strcmp(a[0], "bus")) {
			mce.bus = (GDBusConnection *)&mce;   /* never dereferenced: emit is faked */
		} else if (!strcmp(a[0], "display")) {
			set_display(a[1], TRUE);
			printf("display=%s\n", mce.display);
		} else if (!strcmp(a[0], "read-display")) {
			read_display();
			printf("display=%s\n", mce.display);
		}
	}
	return 0;
}
"""


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    d = tmp_path_factory.mktemp("mce")
    sandboxed_source(SRC, d, [
        ("/sys/class/drm/card0-DSI-1/dpms", d / "dpms"),
        ("/proc/net/wlan/driver", d / "wlan_driver"),
        ("/sys/class/power_supply/battery", d / "battery"),
    ])
    exe = build_harness(d, "mce", HARNESS, pkgs=("gio-unix-2.0", "libudev"))
    return d, functools.partial(run, exe)


@pytest.fixture
def mce(built):
    d, binary = built
    shutil.rmtree(d / "battery", ignore_errors=True)
    (d / "battery").mkdir()
    (d / "wlan_driver").unlink(missing_ok=True)
    (d / "dpms").unlink(missing_ok=True)
    return d, binary


@pytest.mark.parametrize("status,capacity,expected", [
    ("Discharging\n", "50\n", "charger=off battery=ok level=50"),
    ("Charging\n", "50\n", "charger=on battery=ok level=50"),
    # The 80 % limit of plinius-charge: plugged in all the same
    ("Not charging\n", "80\n", "charger=on battery=ok level=80"),
    ("Full\n", "100\n", "charger=on battery=full level=100"),
    ("Discharging\n", "16\n", "charger=off battery=ok level=16"),
    ("Discharging\n", "15\n", "charger=off battery=low level=15"),
    ("Discharging\n", "6\n", "charger=off battery=low level=6"),
    ("Discharging\n", "5\n", "charger=off battery=empty level=5"),
    ("Discharging\n", "0\n", "charger=off battery=empty level=0"),
    ("Unknown\n", "50\n", "charger=unknown battery=ok level=50"),
    ("-", "-", "charger=unknown battery=unknown level=-1"),
])
def test_power_state(mce, status, capacity, expected):
    d, binary = mce
    out = binary(f"power|{status}|{capacity}|0").stdout
    assert out.strip().endswith(expected)


def test_power_signals_only_on_change(mce):
    d, binary = mce
    out = binary("bus", "power|Discharging|50|1", "power|Discharging|50|1", "power|Charging|50|1",
                 "power|Charging|14|1", "power|Charging|14|0").stdout
    signals = [line for line in out.splitlines() if line.startswith("signal")]
    assert signals == [
        "signal charger_state_ind('off',)",
        "signal battery_status_ind('ok',)",
        "signal battery_level_ind(50,)",
        "signal charger_state_ind('on',)",
        "signal battery_status_ind('low',)",
        "signal battery_level_ind(14,)",
    ]


def test_display_and_wifi_suspend_mode(mce):
    d, binary = mce
    (d / "wlan_driver").write_text("")
    out = binary("bus", "display|off").stdout
    assert (d / "wlan_driver").read_text() == "SETSUSPENDMODE 1"
    assert "signal display_status_ind('off',)" in out
    assert "signal system_inactivity_ind(true,)" in out
    (d / "wlan_driver").write_text("")
    out = binary("bus", "display|off", "display|dimmed").stdout
    # Anything but "off" is on
    assert out.splitlines()[-1] == "display=on"
    assert (d / "wlan_driver").read_text().startswith("SETSUSPENDMODE 0")


def test_display_without_wifi_driver(mce):
    d, binary = mce
    out = binary("display|off").stdout
    assert out.strip() == "display=off"
    assert not (d / "wlan_driver").exists()


@pytest.mark.parametrize("dpms,expected", [("On\n", "on"), ("Off\n", "off"), ("Standby\n", "off")])
def test_display_at_start(mce, dpms, expected):
    d, binary = mce
    (d / "dpms").write_text(dpms)
    out = binary("read-display").stdout
    assert out.strip().endswith(f"display={expected}")
    assert "signal" not in out


# ---- The service as it runs: main() with a fake udev monitor over a scripted bus ----
#
# fakes/dhw_scripted_gdbus.h answers for the bus; udev's monitor is a pipe ("uevent" writes to
# it, the daemon's watch reads it); the main loop runs the commands given after the binary:
# call:<method> asks com.nokia.mce.request, set:<state> calls org.plinius.Mce.SetDisplay,
# battery:<status>|<capacity> changes the kernel's files, lose gives the name away.
# FAKE_UDEV=<step> makes that step of the monitor set-up fail; FAKE_WRITE_FAILS=1 makes the
# Wi-Fi driver refuse the command.

SERVICE = r"""
#define G_DISABLE_CAST_CHECKS
#include <errno.h>
#include <glib.h>
#include <gio/gio.h>
#include <glib-unix.h>
#include <libudev.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include "dhw_scripted_gdbus.h"

static void script_loop(GMainLoop *loop);
#define g_main_loop_run script_loop

static int uevent_pipe[2] = { -1, -1 };
static int devices_unref;
static int udev_step_fails(const char *step)
{
	const char *f = getenv("FAKE_UDEV");
	return f && !strcmp(f, step);
}
static struct udev *fake_udev_new(void)
{
	return udev_step_fails("new") ? NULL : (struct udev *)uevent_pipe;
}
static struct udev_monitor *fake_monitor_new(struct udev *udev, const char *name)
{
	printf("udev monitor %s\n", name);
	if (udev_step_fails("monitor") || pipe(uevent_pipe) < 0)
		return NULL;
	return (struct udev_monitor *)uevent_pipe;
}
static int fake_filter(struct udev_monitor *m, const char *subsystem, const char *devtype)
{
	printf("udev filter %s %s\n", subsystem, devtype ? devtype : "(any)");
	return udev_step_fails("filter") ? -EINVAL : 0;
}
static int fake_enable(struct udev_monitor *m)
{
	return udev_step_fails("enable") ? -EPERM : 0;
}
static int fake_get_fd(struct udev_monitor *m)
{
	return uevent_pipe[0];
}
/* One byte per uevent: 'd' a device, 'n' none (the monitor's buffer was empty) */
static struct udev_device *fake_receive(struct udev_monitor *m)
{
	char c = 0;
	if (read(uevent_pipe[0], &c, 1) != 1 || c == 'n')
		return NULL;
	return (struct udev_device *)uevent_pipe;
}
static struct udev_device *fake_device_unref(struct udev_device *d)
{
	devices_unref++;
	return NULL;
}
#define udev_new fake_udev_new
#define udev_monitor_new_from_netlink fake_monitor_new
#define udev_monitor_filter_add_match_subsystem_devtype fake_filter
#define udev_monitor_enable_receiving fake_enable
#define udev_monitor_get_fd fake_get_fd
#define udev_monitor_receive_device fake_receive
#define udev_device_unref fake_device_unref

static ssize_t fake_write(int fd, const void *buf, size_t n)
{
	if (getenv("FAKE_WRITE_FAILS")) {
		errno = EIO;
		return -1;
	}
	return write(fd, buf, n);
}
#define write fake_write
/* Fatal as in the daemon, but through exit(): gcov keeps the counters of the run */
#define g_error(...) (printf("fatal: " __VA_ARGS__), printf("\n"), fflush(stdout), exit(99))

#define main real_main
#include "plinius-mce.c"
#undef main
#undef g_main_loop_run
#undef write

static char **script;

static void put(const char *name, const char *text)
{
	g_autofree char *path = g_build_filename(BATTERY, name, NULL);
	g_file_set_contents(path, text, -1, NULL);
}

static void command(const char *a)
{
	if (g_str_has_prefix(a, "call:")) {
		sg_invoke(MCE_REQUEST_PATH, MCE_REQUEST_IF, ":1.5", a + 5, NULL);
	} else if (g_str_has_prefix(a, "set:")) {
		g_autofree char *params = g_strdup_printf("('%s',)", a + 4);
		sg_invoke(PLINIUS_PATH, PLINIUS_IF, ":1.6", "SetDisplay", params);
	} else if (g_str_has_prefix(a, "battery:")) {
		g_auto(GStrv) v = g_strsplit(a + 8, "|", 2);
		put("status", v[0]);
		put("capacity", v[1]);
	} else if (g_str_has_prefix(a, "uevent:")) {
		if (write(uevent_pipe[1], a + 7, 1) != 1)
			abort();
		/* The watch on the monitor's fd runs */
		g_main_context_iteration(NULL, TRUE);
		printf("devices unref=%d\n", devices_unref);
	} else if (!strcmp(a, "lose")) {
		sg_lose_name();
	} else {
		printf("unknown command %s\n", a);
		exit(98);
	}
	fflush(stdout);
}

static void script_loop(GMainLoop *loop)
{
	/* The name is acquired from the main loop, as with GDBus */
	while (g_main_context_iteration(NULL, FALSE))
		;
	printf("started display=%s charger=%s battery=%s level=%d\n", mce.display, mce.charger,
	       mce.battery_status, mce.battery_level);
	fflush(stdout);
	for (char **a = script; a && *a; a++)
		command(*a);
}

int main(int argc, char **argv)
{
	script = argv + 1;
	/* register-fails is needed before the name is acquired */
	if (argc > 1 && !strcmp(argv[1], "register-fails")) {
		sg_init();
		sg.register_fails = TRUE;
		script++;
	}
	return real_main();
}
"""


@pytest.fixture(scope="module")
def service_built(tmp_path_factory):
    d = tmp_path_factory.mktemp("mce-service")
    sandboxed_source(SRC, d, [
        ("/sys/class/drm/card0-DSI-1/dpms", d / "dpms"),
        ("/proc/net/wlan/driver", d / "wlan_driver"),
        ("/sys/class/power_supply/battery", d / "battery"),
    ])
    exe = build_harness(d, "mce-service", SERVICE, pkgs=("gio-unix-2.0",))
    return d, exe


@pytest.fixture
def service(service_built):
    d, exe = service_built
    shutil.rmtree(d / "battery", ignore_errors=True)
    (d / "battery").mkdir()
    (d / "battery/status").write_text("Discharging\n")
    (d / "battery/capacity").write_text("60\n")
    (d / "wlan_driver").write_text("")
    (d / "dpms").write_text("On\n")

    def start(*script, env=None, check=True):
        e = dict(os.environ)
        for k in ("FAKE_UDEV", "FAKE_WRITE_FAILS"):
            e.pop(k, None)
        e.update(env or {})
        return run(exe, *script, env=e, check=check)
    return d, start


def test_service_start_reads_state_and_owns_the_name(service):
    d, start = service
    (d / "dpms").write_text("Off\n")
    out = start().stdout
    assert "own-name system com.nokia.mce" in out
    assert "register /com/nokia/mce/request com.nokia.mce.request" in out
    assert "register /org/plinius/Mce org.plinius.Mce" in out
    assert "udev monitor udev" in out
    assert "udev filter power_supply (any)" in out
    assert "started display=off charger=off battery=ok level=60" in out
    # The display is off: the Wi-Fi driver drops broadcasts; no signal at start
    assert (d / "wlan_driver").read_text() == "SETSUSPENDMODE 1"
    assert "signal" not in out


def test_service_start_resets_a_left_over_suspend_mode(service):
    d, start = service
    (d / "wlan_driver").write_text("SETSUSPENDMODE 1")
    start()
    assert (d / "wlan_driver").read_text().startswith("SETSUSPENDMODE 0")


def test_service_requests_answer_the_current_state(service):
    d, start = service
    out = start("call:get_display_status", "call:get_charger_state", "call:get_battery_status",
                "call:get_battery_level", "call:get_tklock_mode", "call:get_inactivity_status",
                "set:off", "call:get_display_status", "call:get_inactivity_status").stdout
    replies = [line for line in out.splitlines() if line.startswith("reply ")]
    assert replies == [
        "reply get_display_status ok ('on',)",
        "reply get_charger_state ok ('off',)",
        "reply get_battery_status ok ('ok',)",
        "reply get_battery_level ok (60,)",
        # Phosh has its own lock screen: MCE's touch lock is never on
        "reply get_tklock_mode ok ('unlocked',)",
        "reply get_inactivity_status ok (false,)",
        "reply SetDisplay ok ()",
        "reply get_display_status ok ('off',)",
        "reply get_inactivity_status ok (true,)",
    ]


def test_service_set_display_signals_and_switches_wifi(service):
    d, start = service
    out = start("set:off", "set:off", "set:on").stdout
    signals = [line for line in out.splitlines() if line.startswith("signal ")]
    # The second "off" changes nothing and sends nothing
    assert signals == [
        "signal * /com/nokia/mce/signal com.nokia.mce.signal display_status_ind('off',)",
        "signal * /com/nokia/mce/signal com.nokia.mce.signal system_inactivity_ind(true,)",
        "signal * /com/nokia/mce/signal com.nokia.mce.signal display_status_ind('on',)",
        "signal * /com/nokia/mce/signal com.nokia.mce.signal system_inactivity_ind(false,)",
    ]
    assert (d / "wlan_driver").read_text().startswith("SETSUSPENDMODE 0")


def test_service_unknown_request_is_an_error(service):
    d, start = service
    out = start("call:req_display_state_on").stdout
    assert ("reply req_display_state_on error org.freedesktop.DBus.Error.UnknownMethod: "
            "unknown method req_display_state_on") in out


def test_service_uevent_reads_the_battery_again(service):
    d, start = service
    out = start("battery:Charging|61", "uevent:d", "battery:Charging|61", "uevent:n",
                "battery:Discharging|14", "uevent:d").stdout
    signals = [line.rsplit(" ", 1)[-1] for line in out.splitlines() if line.startswith("signal ")]
    assert signals == [
        "charger_state_ind('on',)", "battery_level_ind(61,)",
        # Nothing changed with the second uevent (one without a device): nothing sent
        "charger_state_ind('off',)", "battery_status_ind('low',)", "battery_level_ind(14,)",
    ]
    # Each received device is released, an empty read has none to release
    assert [line for line in out.splitlines() if line.startswith("devices")] == [
        "devices unref=1", "devices unref=1", "devices unref=2"]


@pytest.mark.parametrize("step", ["new", "monitor", "filter", "enable"])
def test_service_without_udev_monitor_stops(service, step):
    d, start = service
    r = start(env={"FAKE_UDEV": step}, check=False)
    assert r.returncode == 99
    assert "fatal: udev monitor for power_supply failed" in r.stdout
    assert "own-name" not in r.stdout


def test_service_object_that_cannot_be_registered_stops(service):
    d, start = service
    r = start("register-fails", check=False)
    assert r.returncode == 99
    assert "fatal: register: fake bus: object exists" in r.stdout


def test_service_lost_name_stops(service):
    d, start = service
    r = start("lose", check=False)
    assert r.returncode == 99
    assert "fatal: could not own com.nokia.mce" in r.stdout


def test_service_wifi_driver_refusing_the_command_is_only_a_warning(service):
    d, start = service
    r = start("set:off", env={"FAKE_WRITE_FAILS": "1"})
    assert "Wi-Fi suspend mode: Input/output error" in r.stderr
    assert "reply SetDisplay ok ()" in r.stdout
    assert "display_status_ind('off',)" in r.stdout
