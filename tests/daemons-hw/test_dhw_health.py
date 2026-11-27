# health-hal-bridge: IHealth for Android in the Waydroid container, forwarded to the phone's
# health HAL. libgbinder is the scripted fakes/gbinder.h: Android's calls and the HAL's replies
# are scripted values, what the daemon sends shows in the log. Android's callbacks are distinct
# objects, so registering, unregistering and their death can be told apart; the device node is a
# file in tmp, watched by the real GFileMonitor. main() runs for real until SIGTERM.

import functools
import os

import pytest

from dhw_harness import PKG, build_harness, run, sandboxed_source

SRC = PKG / "health-hal-bridge" / "health-hal-bridge.c"

HARNESS = r"""
#define _GNU_SOURCE
#define G_DISABLE_CAST_CHECKS
#include <glib.h>
#include <gio/gio.h>
#include "dhw_scripted_gdbus.h"
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <unistd.h>
#include <gbinder.h>

/* Android's callbacks: 'o' values whose number picks the object */
static struct fake_gb_obj objs[8];
static GBinderRemoteObject *my_read_object(GBinderReader *r)
{
	if (!r || r->pos >= r->n || r->v[r->pos].kind != 'o')
		return NULL;
	return &objs[r->v[r->pos++].i % 8];
}
#define gbinder_reader_read_object my_read_object

/* Death handlers: the last one each object got */
static GBinderRemoteObjectNotifyFunc death_fn[9];
static void *death_user[9];
static gulong my_add_death(GBinderRemoteObject *o, GBinderRemoteObjectNotifyFunc fn, void *user)
{
	int i = o >= objs && o < objs + 8 ? (int)(o - objs) : 8;
	death_fn[i] = fn;
	death_user[i] = user;
	fake_gb_logf("death-handler %d\n", i);
	return 10 + i;
}
#define gbinder_remote_object_add_death_handler my_add_death
static void my_remove_handler(GBinderRemoteObject *o, gulong id)
{
	fake_gb_logf("remove-death-handler %lu\n", id);
}
#define gbinder_remote_object_remove_handler my_remove_handler
static GBinderClient *my_client_new(GBinderRemoteObject *o, const char *iface)
{
	fake_gb_logf("client %s\n", iface);
	return gbinder_client_new(o, iface);
}
#define gbinder_client_new my_client_new

/* The HAL: a binder status per code and the reply values */
static int tx_status[64];
static FakeGbValue hal_vals[32];
static GBinderRemoteReply *my_transact(GBinderClient *c, guint32 code, GBinderLocalRequest *req, int *status)
{
	fake_gb_logf("tx %u\n", code);
	if (status)
		*status = code < 64 ? tx_status[code] : 0;
	return code < 64 && tx_status[code] == -99 ? NULL : &fake_gb_instance;
}
#define gbinder_client_transact_sync_reply my_transact
static GBinderServiceManager *my_sm_new(const char *dev)
{
	if (getenv("FAKE_NO_BINDER") && !strcmp(dev, getenv("FAKE_NO_BINDER")))
		return NULL;
	return gbinder_servicemanager_new(dev);
}
#define gbinder_servicemanager_new my_sm_new

#define main real_main
#include "health-hal-bridge.c"
#undef main

/* i<n> int32, l<n> int64, s<text> string16, o<n> callback object */
static FakeGbValue vals[32];
static int parse(FakeGbValue *v, const char *spec)
{
	g_auto(GStrv) t = g_strsplit(spec ? spec : "", ",", -1);
	int n = 0;
	for (int i = 0; t[i] && *t[i] && n < 32; i++) {
		v[n] = (FakeGbValue){t[i][0]};
		if (t[i][0] == 's')
			v[n].s = g_strdup(t[i] + 1);
		else
			v[n].i = g_ascii_strtoll(t[i] + 1, NULL, 10);
		n++;
	}
	return n;
}

static void flush_log(void)
{
	if (fake_gb_log && fake_gb_log->len) {
		printf("gb=%s\n", g_strdelimit(fake_gb_log->str, "\n", ';'));
		g_string_truncate(fake_gb_log, 0);
	} else {
		printf("gb=\n");
	}
}

static gboolean send_term(gpointer data)
{
	raise(SIGTERM);
	return G_SOURCE_REMOVE;
}

static GFileMonitor *watch;

int main(int argc, char **argv)
{
	if (argc > 1 && !strcmp(argv[1], "main")) {
		sg_init();
		sg.no_bus = argc > 3 && !strcmp(argv[3], "nobus");
		g_timeout_add(atoi(argv[2]), send_term, NULL);
		int rc = real_main(argc - 1, argv + 1);
		printf("rc=%d android=%s\n", rc, self.android_sm ? "set" : "none");
		flush_log();
		return 0;
	}
	for (int i = 1; i < argc; i++) {
		g_auto(GStrv) a = g_strsplit(argv[i], "|", -1);
		if (!strcmp(a[0], "hal")) {
			/* hal|values: what the HAL replies */
			fake_gb_reply_vals = hal_vals;
			fake_gb_reply_n = parse(hal_vals, a[1]);
		} else if (!strcmp(a[0], "fail")) {
			/* fail|code|status (-99: no reply at all) */
			tx_status[atoi(a[1])] = atoi(a[2]);
		} else if (!strcmp(a[0], "noservice")) {
			fake_gb_no_service = TRUE;
		} else if (!strcmp(a[0], "acall")) {
			/* acall|code|values: Android calls IHealth */
			int status = 99;
			fake_gb_request_iface = HEALTH_IFACE;
			fake_gb_request_vals = vals;
			fake_gb_request_n = parse(vals, a[1] ? a[2] : NULL);
			GBinderLocalReply *reply = on_android_call(NULL, NULL, atoi(a[1]), 0, &status, NULL);
			printf("acall %s status=%d reply=%s\n", a[1], status, reply ? "yes" : "no");
			flush_log();
		} else if (!strcmp(a[0], "upower")) {
			/* upower|n: n PropertiesChanged of UPower's devices, then the delay */
			for (int k = 0; k < atoi(a[1]); k++)
				on_upower_changed(NULL, UPOWER_NAME, "/org/freedesktop/UPower/devices/battery_battery",
						  "org.freedesktop.DBus.Properties", "PropertiesChanged", NULL, NULL);
			gint64 end = (g_get_monotonic_time)() + (PUSH_DELAY_MS + 300) * G_TIME_SPAN_MILLISECOND;
			while ((g_get_monotonic_time)() < end) {
				while (g_main_context_iteration(NULL, FALSE))
					;
				g_usleep(5000);
			}
			printf("upower pushed\n");
			flush_log();
		} else if (!strcmp(a[0], "die")) {
			/* die|n: Android's callback n is gone */
			death_fn[atoi(a[1])](NULL, death_user[atoi(a[1])]);
			flush_log();
		} else if (!strcmp(a[0], "haldie")) {
			on_hal_died(NULL, NULL);
			flush_log();
		} else if (!strcmp(a[0], "presence")) {
			fake_gb_sm_present = atoi(a[1]);
			on_android_presence(NULL, NULL);
			flush_log();
		} else if (!strcmp(a[0], "added")) {
			on_android_added(NULL, atoi(a[1]), NULL);
		} else if (!strcmp(a[0], "start")) {
			android_start();
			flush_log();
		} else if (!strcmp(a[0], "watch")) {
			watch = android_watch();
			printf("watch=%s node=%s\n", watch ? "set" : "none", self.node);
		} else if (!strcmp(a[0], "node")) {
			/* node|octal mode: the container's binder node */
			int fd = open(ANDROID_DEVICE, O_CREAT | O_WRONLY, 0600);
			if (fd >= 0)
				close(fd);
			chmod(ANDROID_DEVICE, strtol(a[1], NULL, 8));
		} else if (!strcmp(a[0], "spin")) {
			/* Until the file monitor opened the container's binder (at most 2 s) */
			for (int t = 0; t < 200 && !self.android_sm; t++) {
				while (g_main_context_iteration(NULL, FALSE))
					;
				g_usleep(10000);
			}
			flush_log();
		} else if (!strcmp(a[0], "state")) {
			printf("state hal=%s callbacks=%u android=%s\n", self.client ? "set" : "none",
			       g_list_length(self.callbacks), self.android_sm ? "set" : "none");
		}
	}
	return 0;
}
"""

GET_VERSION, GET_HASH = 0x00ffffff, 0x00fffffe
REGISTER, UNREGISTER, UPDATE, CAPACITY, CHARGE_STATUS, INFO = 1, 2, 3, 7, 9, 12
# A HealthInfo as the HAL replies it: no exception, non-null, then (shortened) fields
INFO_REPLY = "hal|i0,i1,i92,i1,i0,i55"


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    d = tmp_path_factory.mktemp("health")
    sandboxed_source(SRC, d, [("/dev/anbox-binder", d / "anbox-binder")], allow=("/dev/binder",))
    exe = build_harness(d, "health", HARNESS, pkgs=("gio-unix-2.0",))
    return d, exe


@pytest.fixture
def health(built):
    d, exe = built
    node = d / "anbox-binder"
    node.unlink(missing_ok=True)
    yield functools.partial(run, exe)
    node.unlink(missing_ok=True)


def calls(out):
    """Per call (acall/cb): its line and the log after it."""
    lines = out.splitlines()
    return [(line, lines[i + 1][3:]) for i, line in enumerate(lines)
            if line.startswith(("acall ", "cb ")) and i + 1 < len(lines) and lines[i + 1].startswith("gb=")]


def states(out):
    return [line[6:] for line in out.splitlines() if line.startswith("state ")]


def test_version_and_hash(health):
    c = calls(health(f"acall|{GET_VERSION}", f"acall|{GET_HASH}").stdout)
    assert c[0] == (f"acall {GET_VERSION} status=0 reply=yes", "i32 0;i32 1;")
    assert c[1] == (f"acall {GET_HASH} status=0 reply=yes", "i32 0;s16 94e77215594f8ad98ab33a769263d48fdabed92e;")


@pytest.mark.parametrize("code", [UPDATE, CAPACITY, CHARGE_STATUS, INFO, 4, 8, 10, 11])
def test_queries_pass_unchanged(health, code):
    out = health("hal|i0,i55", f"acall|{code}|i7").stdout
    line, log = calls(out)[0]
    # Connected once, the request's arguments and the HAL's reply copied as they are
    assert log == (f"servicemanager /dev/binder;get-service android.hardware.health.IHealth/default;"
                   f"client android.hardware.health.IHealth;death-handler 8;request;bytes 4;tx {code};bytes 8;")


@pytest.mark.parametrize("code", [13, 14, 15, 0, 42])
def test_later_methods_are_not_androids(health, code):
    log = calls(health(f"acall|{code}").stdout)[0][1]
    assert log == "i32 -7;s16 Not supported;i32 0;"


@pytest.mark.parametrize("steps", [["noservice"], ["fail|7|-1"], ["fail|7|-99"]],
                         ids=["no-hal", "binder-error", "no-reply"])
def test_hal_unavailable(health, steps):
    out = health(*steps, f"acall|{CAPACITY}")
    log = calls(out.stdout)[0][1]
    assert log.endswith("i32 -5;s16 Health HAL unavailable;i32 0;")


def test_no_binder_device(health):
    out = health(f"acall|{CAPACITY}", env=dict(os.environ, FAKE_NO_BINDER="/dev/binder"))
    assert calls(out.stdout)[0][1] == "i32 -5;s16 Health HAL unavailable;i32 0;"


def test_register_callback_gets_current_info(health):
    out = health(INFO_REPLY, f"acall|{REGISTER}|o1", "state", f"acall|{REGISTER}|o1", "state").stdout
    first, again = calls(out)
    # Android's callback kept (client, death handler), the HAL connected, then getHealthInfo and
    # the HealthInfo one-way to Android's callback; no callback of ours at the HAL
    assert first[1] == ("client android.hardware.health.IHealthInfoCallback;death-handler 1;"
                        "servicemanager /dev/binder;get-service android.hardware.health.IHealth/default;"
                        "client android.hardware.health.IHealth;death-handler 8;"
                        "request;tx 12;request;bytes 20;tx-async 1 flags 1;i32 0;")
    # The same callback again: kept once, given the HealthInfo again
    assert again[1] == "request;tx 12;request;bytes 20;tx-async 1 flags 1;i32 0;"
    assert states(out) == ["hal=set callbacks=1 android=none"] * 2


def test_register_without_object_or_hal(health):
    out = health(f"acall|{REGISTER}", "noservice", f"acall|{REGISTER}|o2", "state").stdout
    c = calls(out)
    assert c[0][1] == "i32 -3;s16 Bad argument;i32 0;"
    # Without the HAL the callback is still kept: it hears from the HAL once that is there
    assert c[1][1].endswith("get-service android.hardware.health.IHealth/default;i32 0;")
    assert states(out) == ["hal=none callbacks=1 android=none"]


@pytest.mark.parametrize("steps", [["hal|i-3"], ["hal|i0"], ["fail|12|-1"], ["fail|12|-99"]],
                         ids=["exception", "empty", "binder", "no-reply"])
def test_no_health_info_nothing_sent(health, steps):
    out = health(*steps, f"acall|{REGISTER}|o1", "state").stdout
    assert "tx-async" not in out
    assert calls(out)[0][1].endswith("i32 0;")


def test_changes_reported_by_upower_go_to_every_callback(health):
    out = health(INFO_REPLY, f"acall|{REGISTER}|o1", f"acall|{REGISTER}|o2", "upower|3").stdout
    # Three signals, one HealthInfo for both callbacks
    pushed = out.split("upower pushed\n")[1].splitlines()[0]
    assert pushed == "gb=request;tx 12;request;bytes 20;tx-async 1 flags 1;request;bytes 20;tx-async 1 flags 1;"


def test_upower_without_callbacks_asks_nothing(health):
    out = health(INFO_REPLY, "upower|1").stdout
    assert out.split("upower pushed\n")[1].startswith("gb=\n")


def test_update_reaches_every_callback(health):
    out = health(INFO_REPLY, f"acall|{REGISTER}|o1", f"acall|{REGISTER}|o2", f"acall|{UPDATE}").stdout
    # update() itself (no arguments, the HAL's reply as it is), then the HealthInfo to both
    assert calls(out)[2][1] == ("request;bytes 0;tx 3;bytes 24;request;tx 12;request;bytes 20;tx-async 1 flags 1;"
                                "request;bytes 20;tx-async 1 flags 1;")


def test_unregister_and_death(health):
    out = health(INFO_REPLY, f"acall|{REGISTER}|o1", f"acall|{REGISTER}|o2", f"acall|{REGISTER}|o3",
                 f"acall|{UNREGISTER}|o1", f"acall|{UNREGISTER}|o1", f"acall|{UNREGISTER}", "die|2", "state",
                 "upower|1").stdout
    c = calls(out)
    assert c[3][1] == "remove-death-handler 11;i32 0;"
    assert c[4][1] == "i32 -3;s16 Bad argument;i32 0;"
    assert c[5][1] == "i32 -3;s16 Bad argument;i32 0;"
    # A dead callback needs no handler removed; one is left
    assert "callbacks=1" in states(out)[0]
    assert out.split("upower pushed\n")[1].splitlines()[0].count("tx-async") == 1


def test_hal_death_reconnects(health):
    out = health(INFO_REPLY, f"acall|{REGISTER}|o1", "haldie", "state", f"acall|{REGISTER}|o1", "state").stdout
    assert "remove-death-handler 18" in out
    assert states(out) == ["hal=none callbacks=1 android=none", "hal=set callbacks=1 android=none"]
    assert "get-service android.hardware.health.IHealth/default;client" in calls(out)[1][1]


def test_container_gone_forgets_its_callbacks(health):
    out = health(INFO_REPLY, f"acall|{REGISTER}|o1", f"acall|{REGISTER}|o2", "presence|0", "state",
                 "presence|1", "added|0", "added|-2147483647")
    assert "remove-death-handler 11;remove-death-handler 12;" in out.stdout
    assert states(out.stdout) == ["hal=set callbacks=0 android=none"]
    assert "gb=add-service android.hardware.health.IHealth/default;" in out.stdout
    assert "Serving the battery to Android" in out.stderr
    assert "Cannot serve the battery to Android (status -2147483647)" in out.stderr


def test_start_waits_for_an_open_node(built, health):
    d, exe = built
    if os.geteuid() == 0:
        pytest.skip("root opens any node")
    out = health("start", "node|0", "watch", "start", "state", "node|600", "spin", "state", "start").stdout
    assert f"watch=set node=anbox-binder" in out
    assert states(out) == ["hal=none callbacks=0 android=none", "hal=none callbacks=0 android=set"]
    assert f"gb=servicemanager {d}/anbox-binder;add-service android.hardware.health.IHealth/default;" in out
    assert out.rstrip().endswith("gb=")


def test_node_that_is_no_binder(built, health):
    d, exe = built
    out = health("node|600", "start", "state", env=dict(os.environ, FAKE_NO_BINDER=str(d / "anbox-binder"))).stdout
    assert "android=none" in states(out)[0]


@pytest.mark.parametrize("node", [True, False])
def test_main_serves_until_sigterm(built, health, node):
    d, exe = built
    if node:
        (d / "anbox-binder").write_text("")
    r = health("main", "100")
    assert f"rc=0 android={'set' if node else 'none'}" in r.stdout
    # UPower's devices are watched
    assert ("subscribe org.freedesktop.UPower (null) org.freedesktop.DBus.Properties PropertiesChanged"
            in r.stdout)


def test_main_without_system_bus(health):
    r = health("main", "100", "nobus")
    assert "rc=0" in r.stdout
    assert "No system bus" in r.stderr
