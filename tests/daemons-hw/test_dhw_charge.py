# plinius-charge: the charge limit (stop at 80 %, start again at 75 %), what it writes to the
# charger driver, fast charging and the settings. The driver's sysfs files are files in tmp; the
# daemon's main() runs on a private dbus-daemon (dhw_daemon_bus).

import functools

import pytest

from dhw_daemon_bus import Daemon, PrivateBus, sanitized_env
from dhw_harness import PKG, build_harness, kv, run, sandboxed_source

SRC = PKG / "plinius-charge" / "plinius-charge.c"

HARNESS = r"""
#include <glib.h>
#include <gio/gio.h>
#include <stdio.h>
#include <stdlib.h>
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
#include "plinius-charge.c"
#undef main

static void put(const char *path, const char *text)
{
	g_file_set_contents(path, text, -1, NULL);
}

static void state(const char *label)
{
	g_autofree char *disable = NULL, *fast = NULL;
	g_file_get_contents(CHARGE_DISABLE, &disable, NULL, NULL);
	g_file_get_contents(FAST_CHARGE, &fast, NULL, NULL);
	printf("L %s stopped:%d disable:%s fast:%s\n", label, self.stopped, disable ? g_strstrip(disable) : "-",
	       fast ? g_strstrip(fast) : "-");
}

/* What main() does before the main loop */
static void start(void)
{
	load_settings();
	self.stopped = read_sysfs(CHARGE_DISABLE) == 1;
	apply_fast_charge();
	apply_limit();
}

int main(int argc, char **argv)
{
	/* The daemon itself, on the bus DBUS_SYSTEM_BUS_ADDRESS names (the test's own) */
	if (argc > 1 && !strcmp(argv[1], "main")) {
		setvbuf(stdout, NULL, _IOLBF, 0);
		return real_main();
	}
	for (int i = 1; i < argc; i++) {
		const char *a = argv[i];
		if (g_str_has_prefix(a, "cap=")) {
			if (!strcmp(a + 4, "none"))
				remove(BATTERY_CAPACITY);
			else
				put(BATTERY_CAPACITY, a + 4);
			check_level(NULL);
			state(a);
		} else if (!strcmp(a, "start")) {
			start();
			printf("limit=%d fastsetting=%d\n", self.limit, self.fast_charge);
			state("start");
		} else if (!strcmp(a, "stop")) {
			/* main() after the loop */
			set_stopped(FALSE);
			state("stop");
		} else if (g_str_has_prefix(a, "set:")) {
			/* set:<Property>:<0|1> */
			g_auto(GStrv) p = g_strsplit(a, ":", 3);
			GError *error = NULL;
			gboolean ok = on_set_property(NULL, NULL, NULL, NULL, p[1], g_variant_new_boolean(atoi(p[2])),
						      &error, NULL);
			printf("%s=%d\n", a, ok);
			g_clear_error(&error);
			state(a);
		} else if (g_str_has_prefix(a, "get:")) {
			GError *error = NULL;
			g_autoptr(GVariant) v = on_get_property(NULL, NULL, NULL, NULL, a + 4, &error, NULL);
			g_autofree char *text = v ? g_variant_print(v, TRUE) : g_strdup("error");
			printf("%s=%s\n", a, text);
			g_clear_error(&error);
		}
	}
	return 0;
}
"""


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    d = tmp_path_factory.mktemp("charge")
    sandboxed_source(SRC, d, [
        ("/sys/devices/platform/charger", d / "charger"),
        ("/sys/class/power_supply/battery/capacity", d / "capacity"),
        ("/var/lib/plinius-charge/settings", d / "settings"),
    ])
    exe = build_harness(d, "charge", HARNESS, pkgs=("gio-unix-2.0",))
    return d, functools.partial(run, exe)


@pytest.fixture
def charge(built):
    d, binary = built
    (d / "charger").mkdir(exist_ok=True)
    for f in ("charger/cmd_charge_disable", "charger/user_fast_charge_enabled", "capacity", "settings"):
        (d / f).unlink(missing_ok=True)
    (d / "charger/cmd_charge_disable").write_text("0\n")
    (d / "charger/user_fast_charge_enabled").write_text("1\n")
    return d, binary


def states(out):
    """The state after each cap=<level> step, in order"""
    return [line.split(" ", 2)[2] for line in out.splitlines() if line.startswith("L cap=")]


def state(out, label):
    return [line.split(" ", 2)[2] for line in out.splitlines() if line.startswith(f"L {label} ")][-1]


def test_limit_hysteresis(charge):
    d, binary = charge
    (d / "settings").write_text("[charge]\nlimit=true\n")
    out = binary("start", "cap=50", "cap=79", "cap=80", "cap=81", "cap=79", "cap=76", "cap=75",
                 "cap=76", "cap=79", "cap=80").stdout
    stopped = [s.split()[0] for s in states(out)]
    assert stopped == ["stopped:0", "stopped:0", "stopped:1", "stopped:1", "stopped:1", "stopped:1",
                       "stopped:0", "stopped:0", "stopped:0", "stopped:1"]
    # The driver file follows: 1 = charging disabled
    assert [s.split()[1] for s in states(out)][2] == "disable:1"
    assert [s.split()[1] for s in states(out)][6] == "disable:0"


def test_start_keeps_a_stop_of_the_previous_run(charge):
    d, binary = charge
    (d / "settings").write_text("[charge]\nlimit=true\n")
    (d / "charger/cmd_charge_disable").write_text("1\n")
    (d / "capacity").write_text("78\n")
    out = binary("start").stdout
    # Between the levels: stays stopped, no charging up from 78 again
    assert state(out, "start").startswith("stopped:1 disable:1")


def test_start_without_limit_allows_charging(charge):
    d, binary = charge
    (d / "charger/cmd_charge_disable").write_text("1\n")
    (d / "capacity").write_text("90\n")
    out = binary("start").stdout
    # No settings: limit off, fast charging on; a stop left by an earlier run is lifted
    assert kv(out)["limit"] == "0 fastsetting=1"
    assert state(out, "start") == "stopped:0 disable:0 fast:1"


def test_unreadable_level_changes_nothing(charge):
    d, binary = charge
    (d / "settings").write_text("[charge]\nlimit=true\n")
    out = binary("start", "cap=85", "cap=none", "cap=50").stdout
    assert [s.split()[0] for s in states(out)] == ["stopped:1", "stopped:1", "stopped:0"]


def test_stop_never_leaves_charging_disabled(charge):
    d, binary = charge
    (d / "settings").write_text("[charge]\nlimit=true\n")
    out = binary("start", "cap=95", "stop").stdout
    assert states(out) == ["stopped:1 disable:1 fast:1"]
    assert state(out, "stop").startswith("stopped:0 disable:0")


def test_switching_the_limit_off_allows_charging_at_once(charge):
    d, binary = charge
    (d / "settings").write_text("[charge]\nlimit=true\n")
    out = binary("start", "cap=85", "set:Limit:0").stdout
    assert state(out, "set:Limit:0").startswith("stopped:0 disable:0")
    assert "'Limit': <false>" in out
    assert "limit=false" in (d / "settings").read_text()


def test_switching_the_limit_on_checks_the_level_at_once(charge):
    d, binary = charge
    (d / "capacity").write_text("92\n")
    out = binary("start", "set:Limit:1").stdout
    assert state(out, "set:Limit:1").startswith("stopped:1 disable:1")


def test_fast_charge_setting(charge):
    d, binary = charge
    (d / "settings").write_text("[charge]\nfast=false\n")
    out = binary("start", "set:FastCharge:1", "set:LimitStop:1").stdout
    assert state(out, "start").endswith("fast:0")
    assert state(out, "set:FastCharge:1").endswith("fast:1")
    # Read-only properties are refused
    assert "set:LimitStop:1=0" in out
    text = (d / "settings").read_text()
    assert "fast=true" in text


def test_fast_charge_left_alone_without_driver_file(charge):
    d, binary = charge
    (d / "charger/user_fast_charge_enabled").unlink()
    out = binary("start").stdout
    assert state(out, "start").endswith("fast:-")
    assert not (d / "charger/user_fast_charge_enabled").exists()


def test_properties(charge):
    d, binary = charge
    out = kv(binary("get:LimitStop", "get:LimitStart", "get:HasLimit", "get:HasFastCharge",
                    "get:Nothing").stdout)
    assert out["get:LimitStop"] == "uint32 80"
    assert out["get:LimitStart"] == "uint32 75"
    assert out["get:HasLimit"] == "true"
    assert out["get:Nothing"] == "error"


def test_driver_that_cannot_be_written_is_not_taken_as_stopped(charge):
    d, binary = charge
    import shutil
    shutil.rmtree(d / "charger")
    (d / "capacity").write_text("92\n")
    r = binary("set:Limit:1")
    # Still charging as far as the daemon knows, so the next check tries again
    assert state(r.stdout, "set:Limit:1").startswith("stopped:0 disable:-")
    assert "Cannot write" in r.stderr and "cmd_charge_disable" in r.stderr


def test_settings_that_cannot_be_saved_still_switch(charge):
    d, binary = charge
    (d / "settings").mkdir()
    try:
        r = binary("set:FastCharge:0")
        assert "set:FastCharge:0=1" in r.stdout
        assert state(r.stdout, "set:FastCharge:0").endswith("fast:0")
        assert "Cannot save the settings" in r.stderr
    finally:
        (d / "settings").rmdir()


def test_switch_and_limit_properties(charge):
    d, binary = charge
    (d / "settings").write_text("[charge]\nlimit=true\nfast=false\n")
    out = kv(binary("start", "get:Limit", "get:FastCharge").stdout)
    assert out["get:Limit"] == "true" and out["get:FastCharge"] == "false"


@pytest.mark.parametrize("prop,value", [("Limit", 0), ("FastCharge", 1)])
def test_setting_the_same_value_changes_nothing(charge, prop, value):
    d, binary = charge
    out = binary(f"set:{prop}:{value}").stdout
    assert f"set:{prop}:{value}=1" in out
    # Neither saved nor announced
    assert "signal=" not in out
    assert not (d / "settings").exists()


@pytest.fixture
def bus(tmp_path):
    b = PrivateBus(tmp_path)
    yield b
    b.close()


def daemon(built, bus_address):
    d, binary = built
    return Daemon([binary.args[0], "main"], sanitized_env(DBUS_SYSTEM_BUS_ADDRESS=bus_address))


def test_daemon_on_the_bus(charge, bus):
    from gi.repository import GLib
    d, binary = charge
    (d / "settings").write_text("[charge]\nlimit=true\n")
    (d / "capacity").write_text("85\n")
    p = daemon(charge, bus.address)
    bus.wait_name("org.plinius.Charge")
    # At start the limit already stopped charging (85 % is above the stop level)
    assert (d / "charger/cmd_charge_disable").read_text() == "1\n"

    def get(prop):
        return bus.call("org.plinius.Charge", "/org/plinius/Charge", "org.freedesktop.DBus.Properties", "Get",
                        GLib.Variant("(ss)", ("org.plinius.Charge", prop)), "(v)")[0]

    assert (get("Limit"), get("FastCharge"), get("LimitStop"), get("LimitStart"), get("HasLimit")) == \
        (True, True, 80, 75, True)
    bus.call("org.plinius.Charge", "/org/plinius/Charge", "org.freedesktop.DBus.Properties", "Set",
             GLib.Variant("(ssv)", ("org.plinius.Charge", "Limit", GLib.Variant("b", False))))
    assert (d / "charger/cmd_charge_disable").read_text() == "0\n"
    assert get("Limit") is False
    bus.call("org.plinius.Charge", "/org/plinius/Charge", "org.freedesktop.DBus.Properties", "Set",
             GLib.Variant("(ssv)", ("org.plinius.Charge", "Limit", GLib.Variant("b", True))))
    assert (d / "charger/cmd_charge_disable").read_text() == "1\n"
    code, err, out = p.stop()
    assert code == 0
    # The changes were announced, and the stop never leaves the battery without charging
    assert "signal=('org.plinius.Charge', {'Limit': <false>}, [])" in out
    assert "signal=('org.plinius.Charge', {'Limit': <true>}, [])" in out
    assert (d / "charger/cmd_charge_disable").read_text() == "0\n"
    assert "limit=true" in (d / "settings").read_text()


@pytest.mark.parametrize("sig", ["SIGTERM", "SIGINT"])
def test_daemon_signals_end_it(charge, bus, sig):
    import signal
    p = daemon(charge, bus.address)
    bus.wait_name("org.plinius.Charge")
    code, err, out = p.stop(getattr(signal, sig))
    assert code == 0


def test_daemon_without_its_name_stops_and_allows_charging(charge, bus):
    d, binary = charge
    (d / "charger/cmd_charge_disable").write_text("1\n")
    (d / "settings").write_text("[charge]\nlimit=true\n")
    (d / "capacity").write_text("78\n")
    first = daemon(charge, bus.address)
    bus.wait_name("org.plinius.Charge")
    second = daemon(charge, bus.address)
    # The second one ends by itself
    assert second.proc.wait(timeout=20) == 0
    err = second.stop()[1]
    assert "Lost or could not own org.plinius.Charge" in err
    first.stop()
    assert (d / "charger/cmd_charge_disable").read_text() == "0\n"


def test_daemon_without_a_bus(charge, tmp_path):
    d, binary = charge
    (d / "settings").write_text("[charge]\nlimit=true\n")
    (d / "capacity").write_text("95\n")
    p = daemon(charge, f"unix:path={tmp_path}/no-bus")
    code = p.proc.wait(timeout=20)
    assert code == 0
    assert "Lost or could not own" in p.stop()[1]
    # It stopped charging at start, but leaves the battery charging when it goes
    assert (d / "charger/cmd_charge_disable").read_text() == "0\n"
