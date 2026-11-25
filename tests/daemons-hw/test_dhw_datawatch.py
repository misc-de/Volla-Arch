# plinius-datawatch on a scripted D-Bus (the fake bus of the satellite tests): NetworkManager
# appearing with its devices, PropertiesChanged of a modem device, DeviceAdded/DeviceRemoved,
# timers firing and the answers to CheckConnectivity and ActivateConnection. Checked are the
# calls the daemon makes (when it checks again, when it reconnects which connection on which
# device), the waits between attempts and what it writes to the journal.

import pytest

from dhw_harness import PKG, build_harness, run, sandboxed_source
from test_dhw_satellite_bus import FAKEBUS, HARNESS_MAIN

DW = PKG / "plinius-datawatch"

HARNESS = FAKEBUS + r"""
static GBusNameAppearedCallback nm_appeared;
static GBusNameVanishedCallback nm_vanished;
static guint fake_watch(GDBusConnection *c, const char *name, GBusNameWatcherFlags flags,
			GBusNameAppearedCallback appeared, GBusNameVanishedCallback vanished, gpointer data,
			GDestroyNotify free_data)
{
	printf("watch %s\n", name);
	nm_appeared = appeared;
	nm_vanished = vanished;
	return 1;
}
#define g_bus_watch_name_on_connection fake_watch

/* The clock the daemon reads, set by the test in seconds */
static gint64 fake_now;
#define g_get_monotonic_time() (fake_now)

/* The end of the run: what comes after it is cleanup */
#define g_hash_table_destroy(t) (printf("teardown\n"), g_hash_table_destroy(t))

#define main real_main
#include "plinius-datawatch.c"
#undef main
""" + HARNESS_MAIN + r"""
static gboolean command(char **a)
{
	if (!strcmp(a[0], "reply")) {
		set_response(a[1], a[2]);
	} else if (!strcmp(a[0], "nm-up")) {
		nm_appeared(NULL, NM, ":1.3", NULL);
	} else if (!strcmp(a[0], "nm-down")) {
		nm_vanished(NULL, NM, NULL);
	} else if (!strcmp(a[0], "props")) {
		/* props|device|a{sv} (|interface) */
		g_autofree char *p = g_strdup_printf("('%s', %s, @as [])", a[3] ? a[3] : NM_DEVICE, a[2]);
		deliver(NM, a[1], DBUS_PROPS, "PropertiesChanged", p);
	} else if (!strcmp(a[0], "rawprops")) {
		/* rawprops|device|params: PropertiesChanged with any arguments */
		deliver(NM, a[1], DBUS_PROPS, "PropertiesChanged", a[2]);
	} else if (!strcmp(a[0], "nmsig")) {
		/* nmsig|member|params, on NetworkManager's own object */
		deliver(NM, NM_PATH, NM, a[1], a[2]);
	} else if (!strcmp(a[0], "fire")) {
		fire_timers(a[1] ? atoi(a[1]) : 0);
	} else if (!strcmp(a[0], "async-done")) {
		set_response("async", a[1]);
		async_cb(NULL, NULL, async_data);
	} else if (!strcmp(a[0], "clock")) {
		fake_now = (gint64)atoi(a[1]) * G_USEC_PER_SEC;
	} else if (!strcmp(a[0], "sigterm")) {
		unix_handlers[SIGTERM](NULL);
	} else if (!strcmp(a[0], "state")) {
		GHashTableIter it;
		gpointer k, v;
		printf("modems=%u\n", g_hash_table_size(self.modems));
		g_hash_table_iter_init(&it, self.modems);
		while (g_hash_table_iter_next(&it, &k, &v)) {
			Modem *m = v;
			printf("modem %s iface=%s state=%u c4=%u c6=%u attempts=%u busy=%d ours=%d timer=%d\n", m->path,
			       m->iface ? m->iface : "-", m->state, m->c4, m->c6, m->attempts, m->busy, m->ours, m->timer != 0);
		}
	} else {
		return FALSE;
	}
	return TRUE;
}
"""

NMPATH = "/org/freedesktop/NetworkManager"
PROPS = "org.freedesktop.DBus.Properties"
DEV = "org.freedesktop.NetworkManager.Device"
ACTIVE = "org.freedesktop.NetworkManager.Connection.Active"
MODEM = "/org/freedesktop/NetworkManager/Devices/36"
WIFI = "/org/freedesktop/NetworkManager/Devices/35"
AC = "/org/freedesktop/NetworkManager/ActiveConnection/5"
CONN = "/org/freedesktop/NetworkManager/Settings/2"

NONE, PORTAL, LIMITED, FULL = 1, 2, 3, 4


@pytest.fixture(scope="module")
def exe(tmp_path_factory):
    d = tmp_path_factory.mktemp("datawatch")
    sandboxed_source(DW / "plinius-datawatch.c", d, [])
    return build_harness(d, "datawatch", HARNESS, pkgs=("gio-unix-2.0",))


@pytest.fixture
def dw(exe):
    def go(*events, check=True):
        return run(exe, "main", "--", *events, check=check)
    return go


def devices(*paths):
    items = ", ".join(f"objectpath '{p}'" for p in paths)
    return f"reply|Get {NMPATH} org.freedesktop.NetworkManager Devices|(<@ao [{items}]>,)"


def device(path, type_=8, state=100, c4=FULL, c6=LIMITED, iface="ccmni1"):
    return (f"reply|GetAll {path} {PROPS}|({{'DeviceType': <uint32 {type_}>, 'State': <uint32 {state}>, "
            f"'Ip4Connectivity': <uint32 {c4}>, 'Ip6Connectivity': <uint32 {c6}>, "
            f"'IpInterface': <'{iface}'>}},)")


def get(path, iface, prop, value):
    return f"reply|Get {path} {iface} {prop}|(<{value}>,)"


def conn(c4=None, c6=None, state=None, iface=None):
    """PropertiesChanged of the modem device."""
    parts = []
    if state is not None:
        parts.append(f"'State': <uint32 {state}>")
    if c4 is not None:
        parts.append(f"'Ip4Connectivity': <uint32 {c4}>")
    if c6 is not None:
        parts.append(f"'Ip6Connectivity': <uint32 {c6}>")
    if iface is not None:
        parts.append(f"'IpInterface': <'{iface}'>")
    return f"props|{MODEM}|@a{{sv}} {{{', '.join(parts)}}}"


# NetworkManager up with a Wi-Fi device and a working modem
UP = (devices(WIFI, MODEM), device(WIFI, type_=2, iface="wlan0"), device(MODEM), "nm-up")
# What a reconnect reads: the device's active connection, its profile and name
ACTIVE_CONN = (get(MODEM, DEV, "ActiveConnection", f"objectpath '{AC}'"),
               get(AC, ACTIVE, "Connection", f"objectpath '{CONN}'"),
               get(AC, ACTIVE, "Id", "'Mobile Broadband'"))
# NetworkManager's fresh check still finds no internet
STILL_BAD = (get(MODEM, DEV, "Ip4Connectivity", f"uint32 {LIMITED}"),
             get(MODEM, DEV, "Ip6Connectivity", f"uint32 {LIMITED}"))
ACTIVATE = f"ActivateConnection(objectpath '{CONN}', objectpath '{MODEM}', objectpath '/')"


def calls(out, method):
    return [line for line in out.splitlines() if line.startswith("async=") and f" {method}" in line]


def timers(out):
    """Waits set and dropped while the events ran (not those dropped at the exit)."""
    return [line for line in out.split("teardown")[0].splitlines() if line.startswith("timer")]


def state(out):
    return [line for line in out.splitlines() if line.startswith("modem ")][-1]


def test_main_wiring_and_clean_exit(dw):
    r = dw("sigterm")
    out = r.stdout
    assert "bus system" in out
    assert "subscribe sender=org.freedesktop.NetworkManager iface=org.freedesktop.NetworkManager member=* " \
           f"path={NMPATH}" in out
    assert f"subscribe sender=org.freedesktop.NetworkManager iface={PROPS} member=PropertiesChanged path=*" in out
    assert "watch org.freedesktop.NetworkManager" in out
    assert "unix-signal 15" in out and "unix-signal 2" in out
    assert out.rstrip().endswith("rc=0")
    assert "quit" in out


def test_without_system_bus(exe):
    import os
    r = run(exe, "main", "--", env={**os.environ, "FAKE_NO_SYSTEM_BUS": "1"})
    assert "rc=1" in r.stdout
    assert "no system bus" in r.stderr


def test_only_modems_are_watched(dw):
    r = dw(*UP, "state")
    assert "modems=1" in r.stdout
    assert f"modem {MODEM} iface=ccmni1 state=100 c4=4 c6=3 attempts=0 busy=0 ours=0 timer=0" in r.stdout
    assert f"Watching modem {MODEM} (ccmni1): state 100, IPv4 full, IPv6 limited" in r.stderr
    assert not timers(r.stdout)


def test_nm_without_device_list_watches_nothing(dw):
    r = dw("nm-up", "state")
    assert "modems=0" in r.stdout
    assert "Cannot read Devices of /org/freedesktop/NetworkManager" in r.stderr


def test_unreadable_device_is_skipped(dw):
    r = dw(devices(MODEM), "nm-up", "state")
    assert "modems=0" in r.stdout
    assert f"Cannot read device {MODEM}" in r.stderr


def test_device_without_type_is_skipped(dw):
    r = dw(devices(MODEM), f"reply|GetAll {MODEM} {PROPS}|(@a{{sv}} {{}},)", "nm-up", "state")
    assert "modems=0" in r.stdout


def test_no_internet_reconnects_after_a_fresh_check(dw):
    r = dw(*UP, *ACTIVE_CONN, *STILL_BAD, "clock|100", conn(c4=LIMITED), "clock|120", "fire|20000",
           "async-done|(uint32 3,)", "state")
    out = r.stdout
    assert timers(out)[0] == "timer+ 20000ms"
    check = calls(out, "CheckConnectivity")
    assert len(check) == 1 and f"{NMPATH} org.freedesktop.NetworkManager CheckConnectivity" in check[0]
    act = calls(out, "ActivateConnection")
    assert len(act) == 1 and ACTIVATE in act[0]
    assert f"modem {MODEM} iface=ccmni1 state=100 c4=3 c6=3 attempts=1 busy=1 ours=1 timer=0" in out
    assert (f"{MODEM} (ccmni1): no internet for 20 s (IPv4 limited, IPv6 limited), "
            "reconnecting \"Mobile Broadband\" (attempt 1)") in r.stderr


def test_fresh_check_with_internet_does_nothing(dw):
    r = dw(*UP, *ACTIVE_CONN, get(MODEM, DEV, "Ip4Connectivity", f"uint32 {FULL}"),
           get(MODEM, DEV, "Ip6Connectivity", f"uint32 {LIMITED}"),
           "clock|10", conn(c4=LIMITED), "clock|30", "fire|20000", "async-done|(uint32 4,)", "state")
    assert not calls(r.stdout, "ActivateConnection")
    assert "attempts=0 busy=0 ours=0 timer=0" in state(r.stdout)
    assert "internet again by itself after 20 s" in r.stderr


@pytest.mark.parametrize("c4,c6", [(PORTAL, LIMITED), (0, 0), (LIMITED, FULL), (FULL, NONE)])
def test_portal_unknown_or_one_family_working_is_left_alone(dw, c4, c6):
    r = dw(*UP, conn(c4=c4, c6=c6), "state")
    assert not timers(r.stdout)


def test_none_counts_like_limited(dw):
    r = dw(*UP, conn(c4=NONE, c6=NONE))
    assert timers(r.stdout) == ["timer+ 20000ms"]


def test_bad_while_not_activated_is_left_alone(dw):
    r = dw(*UP, conn(state=70, c4=LIMITED), "state")
    assert not timers(r.stdout)


def test_internet_back_within_the_grace_cancels_the_wait(dw):
    r = dw(*UP, "clock|0", conn(c4=LIMITED), "clock|7", conn(c4=FULL), "state")
    assert timers(r.stdout) == ["timer+ 20000ms", "timer- 20000ms"]
    assert "internet again" not in r.stderr


def test_more_changes_while_bad_keep_one_wait(dw):
    r = dw(*UP, conn(c4=LIMITED), conn(c6=NONE), conn(iface="ccmni2"), "state")
    assert timers(r.stdout) == ["timer+ 20000ms"]
    assert "iface=ccmni2" in state(r.stdout)


def reconnect_once():
    """Bad, wait, check, reconnect; then the device goes down and comes up still without internet."""
    return (conn(c4=LIMITED), "fire|20000", "async-done|(uint32 3,)", "async-done|(objectpath '/ac/6',)",
            conn(state=110), conn(state=30), conn(state=40), conn(state=100, c4=0, c6=0), conn(c4=LIMITED))


def test_waits_grow_while_reconnecting_does_not_help(dw):
    events = [*UP, *ACTIVE_CONN, *STILL_BAD, *reconnect_once()]
    for wait in (60000, 300000, 900000, 1800000):
        events += [f"fire|{wait}", "async-done|(uint32 3,)", "async-done|(objectpath '/ac/6',)",
                   conn(state=30), conn(state=100, c4=LIMITED)]
    r = dw(*events, "state")
    # Each wait is set twice: after NetworkManager's answer (dropped when the device goes down)
    # and once the device is up again without internet
    adds = [t for t in timers(r.stdout) if t.startswith("timer+")]
    assert adds == ["timer+ 20000ms"] + [f"timer+ {w}ms" for w in (60000, 300000, 900000, 1800000, 1800000)
                                         for _ in range(2)]
    assert len(calls(r.stdout, "ActivateConnection")) == 5
    assert "attempts=5" in state(r.stdout)


def test_internet_after_a_reconnect_resets_the_count(dw):
    r = dw(*UP, *ACTIVE_CONN, *STILL_BAD, "clock|0", conn(c4=LIMITED), "clock|20", "fire|20000",
           "async-done|(uint32 3,)", "clock|25", "async-done|(objectpath '/ac/6',)", conn(state=30), "clock|31",
           conn(state=100, c4=FULL), conn(c4=LIMITED), "state")
    assert f"{MODEM} (ccmni1): internet again, 11 s after reconnect 1" in r.stderr
    assert timers(r.stdout)[-1] == "timer+ 20000ms"
    assert "attempts=0" in state(r.stdout)


def test_reply_before_the_device_goes_down_keeps_the_count(dw):
    # NetworkManager answers ActivateConnection at once, the device is still activated then
    r = dw(*UP, *ACTIVE_CONN, *STILL_BAD, conn(c4=LIMITED), "fire|20000", "async-done|(uint32 3,)",
           "async-done|(objectpath '/ac/6',)", "state", conn(state=30), conn(state=100, c4=LIMITED), "state")
    lines = [line for line in r.stdout.splitlines() if line.startswith("modem ")]
    assert "attempts=1 busy=0 ours=1 timer=1" in lines[0]
    assert "attempts=1 busy=0 ours=0 timer=1" in lines[1]
    assert timers(r.stdout) == ["timer+ 20000ms", "timer+ 60000ms", "timer- 60000ms", "timer+ 60000ms"]


def test_user_switching_data_off_starts_afresh(dw):
    r = dw(*UP, *ACTIVE_CONN, *STILL_BAD, *reconnect_once(), conn(state=30), conn(state=100, c4=LIMITED), "state")
    # The reconnect was ours and over; going down again was the user's: back to the first wait
    assert timers(r.stdout)[-1] == "timer+ 20000ms"
    assert "attempts=0" in state(r.stdout)


def test_failed_reconnect_is_tried_again_later(dw):
    r = dw(*UP, *ACTIVE_CONN, *STILL_BAD, conn(c4=LIMITED), "fire|20000", "async-done|(uint32 3,)",
           "async-done|!No suitable device", "state")
    assert f"{MODEM}: reconnecting failed: No suitable device" in r.stderr
    assert "attempts=1 busy=0 ours=0 timer=1" in state(r.stdout)
    assert timers(r.stdout)[-1] == "timer+ 60000ms"


def test_failed_check_keeps_the_last_result(dw):
    r = dw(*UP, *ACTIVE_CONN, conn(c4=LIMITED), "fire|20000", "async-done|!Connectivity checking is disabled")
    assert f"{MODEM}: connectivity check failed: Connectivity checking is disabled" in r.stderr
    assert len(calls(r.stdout, "ActivateConnection")) == 1


def test_fresh_check_without_properties_keeps_the_last_result(dw):
    r = dw(*UP, *ACTIVE_CONN, conn(c4=LIMITED), "fire|20000", "async-done|(uint32 3,)")
    assert f"Cannot read Ip4Connectivity of {MODEM}" in r.stderr
    assert len(calls(r.stdout, "ActivateConnection")) == 1


def test_no_active_connection_counts_and_waits(dw):
    r = dw(*UP, *STILL_BAD, get(MODEM, DEV, "ActiveConnection", "objectpath '/'"), conn(c4=LIMITED),
           "fire|20000", "async-done|(uint32 3,)", "state")
    assert f"{MODEM}: no active connection to reconnect" in r.stderr
    assert not calls(r.stdout, "ActivateConnection")
    assert "attempts=1 busy=0" in state(r.stdout)
    assert timers(r.stdout)[-1] == "timer+ 60000ms"


def test_unreadable_active_connection(dw):
    r = dw(*UP, *STILL_BAD, conn(c4=LIMITED), "fire|20000", "async-done|(uint32 3,)")
    assert f"Cannot read ActiveConnection of {MODEM}" in r.stderr
    assert f"{MODEM}: no active connection to reconnect" in r.stderr


def test_unreadable_profile_of_the_active_connection(dw):
    r = dw(*UP, *STILL_BAD, get(MODEM, DEV, "ActiveConnection", f"objectpath '{AC}'"), conn(c4=LIMITED),
           "fire|20000", "async-done|(uint32 3,)", "state")
    assert f"Cannot read Connection of {AC}" in r.stderr
    assert not calls(r.stdout, "ActivateConnection")
    assert timers(r.stdout)[-1] == "timer+ 60000ms"


def test_unnamed_connection_and_interface(dw):
    r = dw(devices(MODEM), f"reply|GetAll {MODEM} {PROPS}|({{'DeviceType': <uint32 8>, 'State': <uint32 100>}},)",
           "nm-up", *STILL_BAD, get(MODEM, DEV, "ActiveConnection", f"objectpath '{AC}'"),
           get(AC, ACTIVE, "Connection", f"objectpath '{CONN}'"), "clock|5", conn(c4=LIMITED), "clock|25",
           "fire|20000", "async-done|(uint32 3,)")
    assert f"Watching modem {MODEM} (-): state 100, IPv4 unknown, IPv6 unknown" in r.stderr
    assert f"Cannot read Id of {AC}" in r.stderr
    assert f"{MODEM} (-): no internet for 20 s (IPv4 limited, IPv6 limited), reconnecting \"?\"" in r.stderr
    assert len(calls(r.stdout, "ActivateConnection")) == 1


def test_connectivity_names_beyond_the_known_ones(dw):
    r = dw(devices(MODEM), device(MODEM, c4=9, c6=FULL), "nm-up")
    assert "IPv4 ?, IPv6 full" in r.stderr


def test_device_added_and_removed(dw):
    r = dw("nm-up", device(MODEM, c4=LIMITED), f"nmsig|DeviceAdded|(objectpath '{MODEM}',)",
           f"nmsig|DeviceAdded|(objectpath '{MODEM}',)", "state",
           f"nmsig|DeviceRemoved|(objectpath '{MODEM}',)", f"nmsig|DeviceRemoved|(objectpath '{WIFI}',)", "state")
    out = r.stdout
    assert out.count(f"call=org.freedesktop.NetworkManager {MODEM} {PROPS} GetAll") == 1
    assert timers(out) == ["timer+ 20000ms", "timer- 20000ms"]
    assert f"Modem {MODEM} gone" in r.stderr
    assert [line for line in out.splitlines() if line.startswith("modems=")] == ["modems=1", "modems=0"]


def test_other_signals_are_ignored(dw):
    r = dw(*UP, "nmsig|StateChanged|(uint32 70,)", "nmsig|CheckPermissions|()",
           f"props|{MODEM}|@a{{sv}} {{'State': <uint32 30>}}|{DEV}.Modem",
           f"props|{WIFI}|@a{{sv}} {{'Ip4Connectivity': <uint32 3>}}",
           f"reply|x|y", "state")
    assert "state=100 c4=4" in state(r.stdout)
    assert not timers(r.stdout)


def test_signals_with_other_arguments_are_ignored(dw):
    r = dw(devices(WIFI), device(WIFI, type_=2), "nm-up", device(MODEM),
           f"nmsig|DeviceAdded|('{MODEM}', 1)", "state", f"nmsig|DeviceAdded|(objectpath '{MODEM}',)",
           f"rawprops|{MODEM}|('{DEV}',)", f"rawprops|{MODEM}|('{DEV}', {{'State': <uint32 30>}})", "state")
    lines = [line for line in r.stdout.splitlines() if line.startswith("modems=")]
    assert lines == ["modems=0", "modems=1"]
    assert "state=100 c4=4" in state(r.stdout)


def test_nm_restart_forgets_the_old_devices(dw):
    r = dw(*UP, *ACTIVE_CONN, conn(c4=LIMITED), "fire|20000", "nm-down", "async-done|(uint32 3,)", "state")
    assert "modems=0" in r.stdout
    assert not calls(r.stdout, "ActivateConnection")


def test_reconnect_answer_after_nm_restart(dw):
    r = dw(*UP, *ACTIVE_CONN, *STILL_BAD, conn(c4=LIMITED), "fire|20000", "async-done|(uint32 3,)", "nm-down",
           "async-done|!NetworkManager went away", "state")
    assert "reconnecting failed: NetworkManager went away" in r.stderr
    assert "modems=0" in r.stdout


def test_removing_a_modem_with_a_pending_wait(dw):
    r = dw(*UP, conn(c4=LIMITED), "nm-down", "state")
    assert timers(r.stdout) == ["timer+ 20000ms", "timer- 20000ms"]
