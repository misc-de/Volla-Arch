# nfc-hal-bridge: INfc for Android in the Waydroid container, forwarded asynchronously to the
# phone's NFC HAL, and who has the controller (nfcd's setting). libgbinder is the scripted
# fakes/gbinder.h: Android's calls and the HAL's replies are scripted values, the HAL's reply to
# an asynchronous call is delivered by the test ("reply" step); D-Bus is dhw_scripted_gdbus.h.
# main() runs for real until SIGTERM.

import functools
import os

import pytest

from dhw_harness import PKG, build_harness, run, sandboxed_source

SRC = PKG / "nfc-hal-bridge" / "nfc-hal-bridge.c"

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

static GBinderRemoteObjectNotifyFunc death_fn[9];
static gulong my_add_death(GBinderRemoteObject *o, GBinderRemoteObjectNotifyFunc fn, void *user)
{
	int i = o >= objs && o < objs + 8 ? (int)(o - objs) : 8;
	death_fn[i] = fn;
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
static GBinderLocalObject *my_new_local(GBinderServiceManager *sm, const char *iface, GBinderLocalTransactFunc fn,
					void *user)
{
	fake_gb_logf("local-object %s\n", iface);
	return gbinder_servicemanager_new_local_object(sm, iface, fn, user);
}
#define gbinder_servicemanager_new_local_object my_new_local
static GBinderServiceManager *my_sm_new(const char *dev)
{
	if (getenv("FAKE_NO_BINDER") && !strcmp(dev, getenv("FAKE_NO_BINDER")))
		return NULL;
	return gbinder_servicemanager_new(dev);
}
#define gbinder_servicemanager_new my_sm_new

#define main real_main
#include "nfc-hal-bridge.c"
#undef main

/* i<n> int32, s<text> string16, o<n> callback object */
static FakeGbValue vals[32], reply_vals[32];
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
		/* main|ms|bus[|powered]: nobus, or the reply of nfcd's GetEnabled (true, false, none);
		 * powered: nfcd's adapter answers GetPowered with true */
		sg_init();
		sg.no_bus = !strcmp(argv[3], "nobus");
		if (strcmp(argv[3], "none") && !sg.no_bus)
			sg_reply("GetEnabled", !strcmp(argv[3], "true") ? "(true,)" : "(false,)");
		if (argc > 4 && !strcmp(argv[4], "powered"))
			sg_reply("GetPowered", "(true,)");
		g_timeout_add(atoi(argv[2]), send_term, NULL);
		int rc = real_main(argc - 1, argv + 1);
		printf("rc=%d android=%s linux_nfc=%d powered=%d\n", rc, self.android_sm ? "set" : "none",
		       self.linux_nfc, self.linux_powered);
		flush_log();
		return 0;
	}
	for (int i = 1; i < argc; i++) {
		g_auto(GStrv) a = g_strsplit(argv[i], "|", -1);
		if (!strcmp(a[0], "linux")) {
			/* linux|0/1: nfcd's setting as the signal brings it */
			GVariant *p = g_variant_ref_sink(g_variant_new("(b)", atoi(a[1])));
			on_nfcd_enabled(NULL, NFCD_SETTINGS, "/", NFCD_SETTINGS_IF, "EnabledChanged", p, NULL);
			g_variant_unref(p);
			flush_log();
		} else if (!strcmp(a[0], "powered")) {
			/* powered|0/1: nfcd's adapter as its signal brings it (x: a bad signal) */
			GVariant *p = g_variant_ref_sink(!strcmp(a[1], "x") ? g_variant_new("(s)", "on")
								    : g_variant_new("(b)", atoi(a[1])));
			on_nfcd_powered(NULL, NFCD_DAEMON, NFCD_ADAPTER, NFCD_ADAPTER_IF, "PoweredChanged", p, NULL);
			g_variant_unref(p);
			flush_log();
		} else if (!strcmp(a[0], "wstate")) {
			printf("wstate waiting=%d powered=%d android=%s\n", self.android_waiting, self.linux_powered,
			       self.android_cb ? "set" : "none");
		} else if (!strcmp(a[0], "badsignal")) {
			GVariant *p = g_variant_ref_sink(g_variant_new("(s)", "on"));
			on_nfcd_enabled(NULL, NFCD_SETTINGS, "/", NFCD_SETTINGS_IF, "EnabledChanged", p, NULL);
			g_variant_unref(p);
			printf("linux_nfc=%d\n", self.linux_nfc);
		} else if (!strcmp(a[0], "noservice")) {
			fake_gb_no_service = TRUE;
		} else if (!strcmp(a[0], "txfails")) {
			fake_gb_transact_fails = TRUE;
		} else if (!strcmp(a[0], "acall")) {
			/* acall|code|values: Android calls INfc */
			int status = 99;
			fake_gb_request_iface = NFC_IFACE;
			fake_gb_request_vals = vals;
			fake_gb_request_n = parse(vals, a[1] ? a[2] : NULL);
			GBinderLocalReply *reply = on_android_call(NULL, NULL, atoi(a[1]), 0, &status, NULL);
			printf("acall %s status=%d reply=%s\n", a[1], status, reply ? "yes" : "later");
			flush_log();
		} else if (!strcmp(a[0], "reply")) {
			/* reply|status|values: the HAL answers the last asynchronous call (-99: no parcel) */
			int status = atoi(a[1]);
			fake_gb_reply_vals = reply_vals;
			fake_gb_reply_n = parse(reply_vals, a[2]);
			fake_gb_reply_fn(NULL, status == -99 ? NULL : &fake_gb_instance, status == -99 ? 0 : status,
					 fake_gb_reply_user);
			printf("replied\n");
			flush_log();
		} else if (!strcmp(a[0], "cb")) {
			/* cb|code|interface|values: the HAL calls our callback */
			int status = 99;
			fake_gb_request_iface = a[2];
			fake_gb_request_vals = vals;
			fake_gb_request_n = parse(vals, a[3]);
			GBinderLocalReply *reply = on_hal_callback(NULL, NULL, atoi(a[1]), 0, &status, NULL);
			printf("cb %s status=%d reply=%s\n", a[1], status, reply ? "yes" : "no");
			flush_log();
		} else if (!strcmp(a[0], "die")) {
			death_fn[atoi(a[1])](NULL, NULL);
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
			int fd = open(ANDROID_DEVICE, O_CREAT | O_WRONLY, 0600);
			if (fd >= 0)
				close(fd);
			chmod(ANDROID_DEVICE, strtol(a[1], NULL, 8));
		} else if (!strcmp(a[0], "spin")) {
			for (int t = 0; t < 200 && !self.android_sm; t++) {
				while (g_main_context_iteration(NULL, FALSE))
					;
				g_usleep(10000);
			}
			flush_log();
		} else if (!strcmp(a[0], "state")) {
			printf("state hal=%s android=%s callback=%s linux_nfc=%d sm=%s\n", self.client ? "set" : "none",
			       self.android_cb ? "set" : "none", self.callback ? "set" : "none", self.linux_nfc,
			       self.android_sm ? "set" : "none");
		}
	}
	return 0;
}
"""

GET_VERSION, GET_HASH = 0x00ffffff, 0x00fffffe
OPEN, CLOSE, CORE_INITIALIZED, GET_CONFIG, WRITE, VERBOSE = 1, 2, 3, 5, 8, 10
CB = "android.hardware.nfc.INfcClientCallback"


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    d = tmp_path_factory.mktemp("nfc")
    sandboxed_source(SRC, d, [("/dev/anbox-binder", d / "anbox-binder")], allow=("/dev/binder",))
    exe = build_harness(d, "nfc", HARNESS, pkgs=("gio-unix-2.0",))
    return d, exe


@pytest.fixture
def nfc(built):
    d, exe = built
    node = d / "anbox-binder"
    node.unlink(missing_ok=True)
    yield functools.partial(run, exe)
    node.unlink(missing_ok=True)


def logs(out, prefix):
    """The log line after each line starting with prefix."""
    lines = out.splitlines()
    return [lines[i + 1][3:] for i, line in enumerate(lines)
            if line.startswith(prefix) and i + 1 < len(lines) and lines[i + 1].startswith("gb=")]


def states(out):
    return [line[6:] for line in out.splitlines() if line.startswith("state ")]


OPENED = ("client android.hardware.nfc.INfcClientCallback;death-handler 1;"
          "local-object android.hardware.nfc.INfcClientCallback;object;tx-async 1 flags 0;block;")


def test_version_and_hash(nfc):
    out = nfc(f"acall|{GET_VERSION}", f"acall|{GET_HASH}").stdout
    assert logs(out, "acall ") == ["i32 0;i32 1;", "i32 0;s16 37acf94ceb095ad537b66c3bd77c30a6ad5f5b0e;"]


@pytest.mark.parametrize("code", [0, 11, 42])
def test_unknown_methods(nfc, code):
    assert logs(nfc(f"acall|{code}").stdout, "acall ") == ["i32 -7;s16 Not supported;i32 0;"]


def wstates(out):
    return [line[7:] for line in out.splitlines() if line.startswith("wstate ")]


def test_open_while_linux_has_nfc_waits(nfc):
    """Android's open() is answered at once, the HAL is not asked: Android waits for the HAL's
    open event as it does for a slow controller."""
    r = nfc("linux|1", f"acall|{OPEN}|o1", "wstate", f"acall|{CORE_INITIALIZED}", f"cb|2|{CB}|i0,i0")
    a = logs(r.stdout, "acall ")
    assert a[0].endswith("death-handler 1;i32 0;") and "tx-async" not in a[0]
    assert "acall 1 status=0 reply=yes" in r.stdout
    assert wstates(r.stdout) == ["waiting=1 powered=0 android=set"]
    assert "Android waits for NFC: it is switched on for Linux" in r.stderr
    # Nothing else reaches the HAL meanwhile, and nothing of the HAL reaches Android
    assert a[1].endswith("i32 -5;s16 NFC is switched on for Linux;i32 0;")
    assert logs(r.stdout, "cb ") == ["reply-i32 0;"]


def test_linux_letting_go_gives_the_hal_to_the_waiting_android(nfc):
    r = nfc("linux|1", f"acall|{OPEN}|o1", "linux|0", "wstate", "reply|0|i0", f"cb|2|{CB}|i0,i0")
    after = r.stdout.split("acall 1 status=0 reply=yes\n")[1].splitlines()
    # Our callback to the HAL now, the HAL's answer goes nowhere but its open event to Android
    assert after[1] == "gb=request;local-object android.hardware.nfc.INfcClientCallback;object;tx-async 1 flags 0;"
    assert wstates(r.stdout) == ["waiting=0 powered=0 android=set"]
    assert "NFC released by Linux: Android opens it" in r.stderr
    assert "did not open" not in r.stderr
    assert logs(r.stdout, "cb ") == ["request;bytes 8;tx-async 2 flags 1;reply-i32 0;"]


@pytest.mark.parametrize("reply", ["reply|-1|", "reply|0|i-5", "reply|-99|"], ids=["binder", "exception", "no-parcel"])
def test_hal_that_does_not_open_for_the_waiting_android(nfc, reply):
    r = nfc("linux|1", f"acall|{OPEN}|o1", "linux|0", reply)
    assert "NFC HAL did not open for Android" in r.stderr


def test_give_when_the_call_fails(nfc):
    r = nfc("linux|1", f"acall|{OPEN}|o1", "txfails", "linux|0", "wstate")
    assert "NFC HAL did not open for Android" in r.stderr
    assert wstates(r.stdout) == ["waiting=0 powered=0 android=set"]


def test_android_waits_until_nfcd_has_closed_the_hal(nfc):
    """nfcd closes the HAL when its adapter powers off; opening it for Android before would let
    nfcd's late close end Android's session."""
    r = nfc("powered|1", "linux|1", f"acall|{OPEN}|o1", "linux|0", "wstate", "powered|x", "powered|1",
            "wstate", "powered|0", "wstate")
    assert wstates(r.stdout) == ["waiting=1 powered=1 android=set", "waiting=1 powered=1 android=set",
                                 "waiting=0 powered=0 android=set"]
    assert r.stderr.count("Android opens it") == 1
    # Linux is off but nfcd still powered: a new open waits as well
    r = nfc("powered|1", "linux|0", f"acall|{OPEN}|o1", "wstate")
    assert wstates(r.stdout) == ["waiting=1 powered=1 android=set"]


def test_powered_off_without_a_waiting_android(nfc):
    r = nfc("linux|0", f"acall|{OPEN}|o1", "reply|0|i0", "powered|0", "linux|0")
    assert "Android opens it" not in r.stderr
    assert r.stdout.count("tx-async 1 flags 0") == 1


def test_waiting_android_gives_up(nfc):
    """close() or its death while it waits: nothing to close at the HAL, and nothing to give later."""
    r = nfc("linux|1", f"acall|{OPEN}|o1", f"acall|{CLOSE}|i0", "wstate", "linux|0")
    a = logs(r.stdout, "acall ")
    assert a[1] == "request;remove-death-handler 11;i32 0;"
    assert wstates(r.stdout) == ["waiting=0 powered=0 android=none"]
    assert "Android opens it" not in r.stderr and "tx-async 2" not in r.stdout
    r = nfc("linux|1", f"acall|{OPEN}|o1", "die|1", "wstate", "linux|0")
    assert wstates(r.stdout) == ["waiting=0 powered=0 android=none"]
    assert "tx-async" not in r.stdout


def test_linux_switching_on_again_while_android_waits(nfc):
    r = nfc("linux|1", f"acall|{OPEN}|o1", "linux|1", "wstate")
    # No error event: Android has not had the controller yet
    assert "tx-async 2 flags 1" not in r.stdout
    assert wstates(r.stdout) == ["waiting=1 powered=0 android=set"]


def test_main_reads_nfcds_adapter(built, nfc):
    d, exe = built
    (d / "anbox-binder").write_text("")
    r = nfc("main", "100", "false", "powered")
    assert "rc=0 android=set linux_nfc=0 powered=1" in r.stdout
    assert ("subscribe org.sailfishos.nfc.daemon /nfc0 org.sailfishos.nfc.Adapter PoweredChanged"
            in r.stdout)
    assert "NFC for Linux: off (powered)" in r.stderr


def test_open_session_and_reply_come_asynchronously(nfc):
    out = nfc("linux|0", f"acall|{OPEN}|o1", "state", "reply|0|i0", f"acall|{CORE_INITIALIZED}", "reply|0|i0",
              f"acall|{WRITE}|i4,i16909060", "reply|0|i0,i4", f"acall|{GET_CONFIG}", "reply|0|i0,i1,i60").stdout
    a = logs(out, "acall ")
    # open(): Android's callback kept, ours to the HAL, the request waits for the HAL's reply
    assert a[0].endswith(OPENED)
    assert "acall 1 status=0 reply=later" in out
    assert states(out) == ["hal=set android=set callback=set linux_nfc=0 sm=none"]
    # Further calls: their arguments as they are, the HAL's reply back as it is
    assert a[1] == "request;bytes 0;tx-async 3 flags 0;block;"
    assert a[2] == "request;bytes 8;tx-async 8 flags 0;block;"
    r = logs(out, "replied")
    assert r == ["bytes 4;complete 0;", "bytes 4;complete 0;", "bytes 8;complete 0;", "bytes 12;complete 0;"]


@pytest.mark.parametrize("reply", ["reply|-1|", "reply|-99|"], ids=["binder-error", "no-parcel"])
def test_failed_hal_call_becomes_an_exception(nfc, reply):
    r = nfc("linux|0", f"acall|{OPEN}|o1", reply)
    assert logs(r.stdout, "replied") == ["i32 -5;s16 NFC HAL unavailable;i32 0;complete 0;"]
    assert "NFC HAL call failed" in r.stderr


def test_calls_without_an_open_session(nfc):
    out = nfc("linux|0", f"acall|{WRITE}|i1,i2", f"acall|{OPEN}", "state").stdout
    a = logs(out, "acall ")
    assert a[0].endswith("i32 -5;s16 NFC HAL unavailable;i32 0;")
    assert a[1].endswith("i32 -3;s16 Bad argument;i32 0;")


@pytest.mark.parametrize("linux", ["0", "1"])
def test_settings_are_read_before_open(nfc, linux):
    """Android asks for the HAL's configuration (presence check, host allow list, routes) before
    open(); the two questions reach the HAL without a session, even while Linux has NFC."""
    out = nfc(f"linux|{linux}", f"acall|{GET_CONFIG}", "reply|0|i0,i1,i60", f"acall|{VERBOSE}", "reply|0|i0,i0",
              f"acall|{CORE_INITIALIZED}", "state").stdout
    a = logs(out, "acall ")
    assert a[0].endswith("request;bytes 0;tx-async 5 flags 0;block;")
    assert a[1] == "request;bytes 0;tx-async 10 flags 0;block;"
    assert logs(out, "replied") == ["bytes 12;complete 0;", "bytes 8;complete 0;"]
    # Anything else still needs an open session
    why = "NFC is switched on for Linux" if linux == "1" else "NFC HAL unavailable"
    assert a[2] == f"request;i32 -5;s16 {why};i32 0;"
    assert states(out) == [f"hal=set android=none callback=none linux_nfc={linux} sm=none"]


@pytest.mark.parametrize("steps", [["noservice"], ["linux|0", "txfails"]], ids=["no-hal", "transact-fails"])
def test_hal_unavailable(nfc, steps):
    out = nfc(*steps, f"acall|{OPEN}|o1").stdout
    assert logs(out, "acall ")[-1].endswith("s16 NFC HAL unavailable;i32 0;")


def test_no_binder_device(nfc):
    out = nfc(f"acall|{OPEN}|o1", env=dict(os.environ, FAKE_NO_BINDER="/dev/binder")).stdout
    assert logs(out, "acall ") == ["i32 -5;s16 NFC HAL unavailable;i32 0;"]


def test_hal_events_and_data_go_to_android(nfc):
    out = nfc("linux|0", f"acall|{OPEN}|o1", f"cb|2|{CB}|i0,i0", f"cb|1|{CB}|i3,i1", f"cb|7|{CB}",
              f"cb|2|android.hardware.Other").stdout
    c = logs(out, "cb ")
    assert c[0] == "request;bytes 8;tx-async 2 flags 1;reply-i32 0;"
    assert c[1] == "request;bytes 8;tx-async 1 flags 1;reply-i32 0;"
    assert c[2] == "reply-i32 0;"
    assert "cb 2 status=-1 reply=no" in out


def test_hal_callback_without_android(nfc):
    out = nfc(f"cb|2|{CB}|i0,i0").stdout
    assert logs(out, "cb ") == ["reply-i32 0;"]


def test_close_ends_the_session(nfc):
    out = nfc("linux|0", f"acall|{OPEN}|o1", "reply|0|i0", f"acall|{CLOSE}|i0", "state", f"cb|2|{CB}|i1,i0").stdout
    assert logs(out, "acall ")[1] == "request;bytes 4;remove-death-handler 11;tx-async 2 flags 0;block;"
    assert states(out) == ["hal=set android=none callback=set linux_nfc=0 sm=none"]
    # The HAL's close event no longer goes anywhere
    assert logs(out, "cb ") == ["reply-i32 0;"]


def test_linux_switching_nfc_on_takes_it_back(nfc):
    r = nfc("linux|0", f"acall|{OPEN}|o1", "reply|0|i0", "linux|1", "state", "linux|1")
    # Android hears of an error and its session is gone; the HAL is not closed: nfcd opens it
    # in the same moment, and a late close would end nfcd's session
    after_reply = r.stdout.split("replied\n")[1].splitlines()
    assert after_reply[1] == "gb=request;i32 5;i32 2;tx-async 2 flags 1;remove-death-handler 11;"
    assert "tx-async 2 flags 1;request;i32 0" not in r.stdout
    # Switched on again: nothing more to take back
    assert after_reply[-1] == "gb="
    assert states(r.stdout) == ["hal=set android=none callback=set linux_nfc=1 sm=none"]
    assert "Android loses it" in r.stderr


def test_bad_signal_changes_nothing(nfc):
    out = nfc("linux|0", "badsignal").stdout
    assert "linux_nfc=0" in out


@pytest.mark.parametrize("step", ["die|1", "presence|0"])
def test_android_gone_closes_the_hal(nfc, step):
    out = nfc("linux|0", f"acall|{OPEN}|o1", "reply|0|i0", step, "state").stdout
    log = out.split(f"replied\n")[1]
    assert "request;i32 0;tx-async 2 flags 1;" in log
    assert states(out) == ["hal=set android=none callback=set linux_nfc=0 sm=none"]


def test_container_gone_without_session(nfc):
    out = nfc("presence|0", "presence|1", "added|0", "added|-1")
    assert "gb=add-service android.hardware.nfc.INfc/default;" in out.stdout
    assert "Serving NFC to Android" in out.stderr
    assert "Cannot serve NFC to Android (status -1)" in out.stderr


def test_hal_death_ends_the_session(nfc):
    out = nfc("linux|0", f"acall|{OPEN}|o1", "reply|0|i0", "haldie", "state").stdout
    assert states(out) == ["hal=none android=none callback=set linux_nfc=0 sm=none"]
    assert "remove-death-handler 18" in out and "remove-death-handler 11" in out


def test_second_open_replaces_androids_callback(nfc):
    out = nfc("linux|0", f"acall|{OPEN}|o1", "reply|0|i0", f"acall|{OPEN}|o2", "state").stdout
    second = logs(out, "acall ")[1]
    assert second.startswith("request;remove-death-handler 11;client android.hardware.nfc.INfcClientCallback;"
                             "death-handler 2;")
    # Our own callback at the HAL stays the one
    assert "local-object" not in second


def test_start_waits_for_an_open_node(built, nfc):
    d, exe = built
    if os.geteuid() == 0:
        pytest.skip("root opens any node")
    out = nfc("start", "node|0", "watch", "start", "state", "node|600", "spin", "state", "start").stdout
    assert "watch=set node=anbox-binder" in out
    assert [s.split()[-1] for s in states(out)] == ["sm=none", "sm=set"]
    assert f"gb=servicemanager {d}/anbox-binder;local-object android.hardware.nfc.INfc;add-service android.hardware.nfc.INfc/default;" in out
    assert out.rstrip().endswith("gb=")


def test_node_that_is_no_binder(built, nfc):
    d, exe = built
    out = nfc("node|600", "start", "state", env=dict(os.environ, FAKE_NO_BINDER=str(d / "anbox-binder"))).stdout
    assert states(out)[0].endswith("sm=none")


@pytest.mark.parametrize("bus,linux", [("false", 0), ("true", 1), ("none", 1), ("nobus", 1)])
def test_main_reads_nfcds_setting(built, nfc, bus, linux):
    d, exe = built
    (d / "anbox-binder").write_text("")
    r = nfc("main", "100", bus)
    assert f"rc=0 android=set linux_nfc={linux}" in r.stdout
    assert "powered=0" in r.stdout
    if bus == "nobus":
        assert "NFC stays with Linux" in r.stderr
    else:
        assert ("subscribe org.sailfishos.nfc.settings / org.sailfishos.nfc.Settings EnabledChanged"
                in r.stdout)
        assert f"NFC for Linux: {'on' if linux else 'off'}" in r.stderr
