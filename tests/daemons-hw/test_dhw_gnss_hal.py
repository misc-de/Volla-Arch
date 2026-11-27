# gnss-nmea-bridge: the receiver's life cycle against the HAL and the clients. The HAL is the
# scripted libgbinder of fakes/gbinder.h (each transaction can be made to fail), clients are real
# socket pairs watched by GLib; g_socket_send can be scripted to write only part of a line, so the
# slow-reader handling is checked byte for byte. main() runs for real too: it listens on a socket
# in tmp, a test connects to it, receives NMEA and stops it with SIGTERM.

import functools
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from conftest import GCOV_DIR
from dhw_harness import PKG, build_harness, run, sandboxed_source

SRC = PKG / "gnss-nmea-bridge" / "gnss-nmea-bridge.c"

HARNESS = r"""
#include <glib.h>
#include <gio/gio.h>
#include <glib-unix.h>
#include <grp.h>
#include <sys/socket.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <gbinder.h>

/* ---- HAL: per-code binder status and AIDL exception ---- */
static int tx_status[64], tx_ex[64];
static FakeGbValue tx_reply[1];
static GBinderRemoteReply *my_transact(GBinderClient *c, guint32 code, GBinderLocalRequest *req, int *status)
{
	fake_gb_logf("tx %u\n", code);
	*status = code < 64 ? tx_status[code] : 0;
	if (*status)
		return NULL;   /* the binder call itself failed: no reply parcel */
	tx_reply[0] = (FakeGbValue){'i', code < 64 ? tx_ex[code] : 0};
	fake_gb_reply_vals = tx_reply;
	fake_gb_reply_n = 1;
	return &fake_gb_instance;
}
#define gbinder_client_transact_sync_reply my_transact

static GBinderServiceManager *my_sm_new(const char *dev)
{
	if (getenv("FAKE_NO_BINDER"))
		return NULL;
	return gbinder_servicemanager_new(dev);
}
#define gbinder_servicemanager_new my_sm_new

static void my_remove_handler(GBinderRemoteObject *o, gulong id)
{
	fake_gb_logf("remove-death-handler %lu\n", id);
}
#define gbinder_remote_object_remove_handler my_remove_handler

/* ---- the group geoclue: our own group when FAKE_GEOCLUE is set ---- */
static struct group *fake_getgrnam(const char *name)
{
	static struct group g;
	if (!getenv("FAKE_GEOCLUE") || strcmp(name, "geoclue"))
		return NULL;
	g.gr_name = (char *)"geoclue";
	g.gr_gid = getgid();
	return &g;
}
#define getgrnam fake_getgrnam

/* ---- timers: recorded and fired by the test (unit mode), real ones under main() ---- */
static gboolean fake_timers = TRUE;
typedef struct { guint id; guint seconds; GSourceFunc fn; gpointer data; } Timer;
static GArray *timers;
static guint next_id = 100;
static guint add_timer(guint seconds, GSourceFunc fn, gpointer data)
{
	Timer t = {next_id++, seconds, fn, data};
	if (!timers)
		timers = g_array_new(FALSE, FALSE, sizeof(Timer));
	g_array_append_val(timers, t);
	return t.id;
}
static guint my_idle_add(GSourceFunc fn, gpointer data)
{
	return fake_timers ? add_timer(0, fn, data) : g_idle_add(fn, data);
}
static guint my_timeout_add_seconds(guint s, GSourceFunc fn, gpointer data)
{
	return fake_timers ? add_timer(s, fn, data) : g_timeout_add_seconds(s, fn, data);
}
#define g_idle_add my_idle_add
#define g_timeout_add_seconds my_timeout_add_seconds

/* ---- socket writes: a script of what each send does ----
 * a number: write at most that many bytes; W: would block; E: broken pipe; T (in waits): time out */
static char **send_script;
static int send_pos;
static gssize my_socket_send(GSocket *s, const gchar *buf, gsize len, GCancellable *c, GError **err)
{
	const char *step = send_script && send_script[send_pos] ? send_script[send_pos++] : NULL;
	if (step && *step == 'W') {
		g_set_error(err, G_IO_ERROR, G_IO_ERROR_WOULD_BLOCK, "would block");
		return -1;
	}
	if (step && *step == 'E') {
		g_set_error(err, G_IO_ERROR, G_IO_ERROR_BROKEN_PIPE, "broken pipe");
		return -1;
	}
	if (step && *step != 'T')
		len = MIN(len, (gsize)atoi(step));
	printf("send %zu\n", len);
	return g_socket_send(s, buf, len, c, err);
}
static gboolean my_timed_wait(GSocket *s, GIOCondition cond, gint64 timeout, GCancellable *c, GError **err)
{
	if (send_script && send_script[send_pos] && *send_script[send_pos] == 'T') {
		send_pos++;
		g_set_error(err, G_IO_ERROR, G_IO_ERROR_TIMED_OUT, "timed out");
		return FALSE;
	}
	printf("wait %" G_GINT64_FORMAT "\n", timeout);
	return TRUE;
}
#define g_socket_send my_socket_send
#define g_socket_condition_timed_wait my_timed_wait

/* ---- the raw parcel of a callback: missing when FAKE_NO_DATA is set ---- */
static const void *my_get_data(const GBinderReader *r, gsize *size)
{
	return getenv("FAKE_NO_DATA") ? NULL : gbinder_reader_get_data(r, size);
}
#define gbinder_reader_get_data my_get_data

#define main real_main
#include "gnss-nmea-bridge.c"
#undef main
#undef g_idle_add
#undef g_timeout_add_seconds

static int peers[8], n_peers;

static void iterate(void)
{
	for (int i = 0; i < 20; i++)
		g_main_context_iteration(NULL, FALSE);
}

static void dump(int fd)
{
	char buf[8192];
	ssize_t n = read(fd, buf, sizeof buf - 1);
	if (n < 0)
		n = 0;
	buf[n] = 0;
	printf("got=");
	for (char *p = buf; *p; p++) {
		if (*p == '\r')
			fputs("<CR>", stdout);
		else if (*p == '\n')
			fputs("<LF>|", stdout);
		else
			putchar(*p);
	}
	putchar('\n');
}

static void flush_log(void)
{
	if (fake_gb_log && fake_gb_log->len) {
		g_auto(GStrv) lines = g_strsplit(fake_gb_log->str, "\n", -1);
		for (char **l = lines; *l; l++)
			if (**l)
				printf("gb %s\n", *l);
		g_string_truncate(fake_gb_log, 0);
	}
}

static GFileMonitor *watch;

/* Android calls IGnss: the reply's parcel goes to the log */
static void android_call(guint code, FakeGbValue *v, int n)
{
	int status = 99;
	fake_gb_request_iface = GNSS_IFACE;
	fake_gb_request_vals = v;
	fake_gb_request_n = n;
	GBinderLocalReply *reply = on_android_call(NULL, NULL, code, 0, &status, NULL);
	flush_log();
	printf("acall %u status=%d reply=%s\n", code, status, reply ? "yes" : "no");
}

static void callback(guint code, const char *iface, FakeGbValue *v, int n)
{
	int status = 99;
	fake_gb_request_iface = iface;
	fake_gb_request_vals = v;
	fake_gb_request_n = n;
	GBinderLocalReply *reply = on_callback(NULL, NULL, code, 0, &status, NULL);
	flush_log();
	printf("callback %u status=%d reply=%s\n", code, status, reply ? "yes" : "no");
}

/* SIGUSR1 under main(): the HAL reports one sentence with a fix */
static gboolean inject_nmea(gpointer data)
{
	static FakeGbValue v[] = {{'l', 1}, {'s', 0, 0, "$GPGSV,1,1,00*79"}};
	int status;
	fake_gb_request_iface = GNSS_CB_IFACE;
	fake_gb_request_vals = v;
	fake_gb_request_n = 2;
	on_callback(NULL, NULL, CB_NMEA, 0, &status, NULL);
	g_message("injected");
	return G_SOURCE_CONTINUE;
}

int main(int argc, char **argv)
{
	if (argc > 1 && !strcmp(argv[1], "main")) {
		fake_timers = FALSE;
		g_unix_signal_add(SIGUSR1, inject_nmea, NULL);
		int rc = real_main(argc - 1, argv + 1);
		printf("rc=%d running=%d client=%s\n", rc, bridge.running, bridge.client ? "set" : "none");
		flush_log();
		return 0;
	}
	for (int i = 1; i < argc; i++) {
		g_auto(GStrv) a = g_strsplit(argv[i], "|", -1);
		if (!strcmp(a[0], "client")) {
			int sv[2];
			if (socketpair(AF_UNIX, SOCK_STREAM, 0, sv) < 0)
				abort();
			GSocket *s = g_socket_new_from_fd(sv[0], NULL);
			GSocketConnection *c = g_socket_connection_factory_create_connection(s);
			g_object_unref(s);
			peers[n_peers++] = sv[1];
			fcntl(sv[1], F_SETFL, O_NONBLOCK);
			on_incoming(NULL, c, NULL, NULL);
			g_object_unref(c);
		} else if (!strcmp(a[0], "close")) {
			close(peers[atoi(a[1])]);
			iterate();
		} else if (!strcmp(a[0], "input")) {
			if (write(peers[atoi(a[1])], "hello", 5) != 5)
				abort();
			iterate();
		} else if (!strcmp(a[0], "read")) {
			dump(peers[atoi(a[1])]);
		} else if (!strcmp(a[0], "script")) {
			g_strfreev(send_script);
			send_script = g_strsplit(a[1], ",", -1);
			send_pos = 0;
		} else if (!strcmp(a[0], "line")) {
			broadcast(a[1]);
		} else if (!strcmp(a[0], "fail")) {
			/* fail|code|status|exception */
			tx_status[atoi(a[1])] = atoi(a[2]);
			tx_ex[atoi(a[1])] = atoi(a[3]);
		} else if (!strcmp(a[0], "heal")) {
			memset(tx_status, 0, sizeof tx_status);
			memset(tx_ex, 0, sizeof tx_ex);
			fake_gb_no_service = FALSE;
		} else if (!strcmp(a[0], "noservice")) {
			fake_gb_no_service = TRUE;
		} else if (!strcmp(a[0], "timers")) {
			/* Fire what is due now; a timer's callback may add the next one */
			GArray *due = timers;
			timers = NULL;
			for (guint j = 0; due && j < due->len; j++) {
				Timer *t = &g_array_index(due, Timer, j);
				printf("timer %us\n", t->seconds);
				t->fn(t->data);
			}
			if (due)
				g_array_free(due, TRUE);
		} else if (!strcmp(a[0], "die")) {
			on_hal_died(NULL, NULL);
		} else if (!strcmp(a[0], "nmea")) {
			FakeGbValue v[] = {{'l', 1}, {'s', 0, 0, a[1]}};
			callback(CB_NMEA, GNSS_CB_IFACE, v, 2);
		} else if (!strcmp(a[0], "nmea-empty")) {
			callback(CB_NMEA, GNSS_CB_IFACE, NULL, 0);
		} else if (!strcmp(a[0], "remove-unknown")) {
			/* A connection the bridge does not hold (already dropped) */
			int sv[2];
			if (socketpair(AF_UNIX, SOCK_STREAM, 0, sv) < 0)
				abort();
			GSocket *s = g_socket_new_from_fd(sv[0], NULL);
			GSocketConnection *c = g_socket_connection_factory_create_connection(s);
			g_object_unref(s);
			client_remove(c);
			printf("still open=%d\n", !g_io_stream_is_closed(G_IO_STREAM(c)));
			g_object_unref(c);
			close(sv[1]);
		} else if (!strcmp(a[0], "location")) {
			FakeGbValue v[] = {
				{'i', 1}, {'i', 92}, {'i', 0x1}, {'d', 0, 52.520008}, {'d', 0, 13.404954},
				{'d', 0, 0}, {'d', 0, 0}, {'d', 0, 0}, {'d', 0, 0}, {'d', 0, 0}, {'d', 0, 0},
				{'d', 0, 0}, {'l', 1700000000123LL},
			};
			bridge.last_nmea_us = -100 * G_USEC_PER_SEC;
			callback(CB_LOCATION, GNSS_CB_IFACE, v, G_N_ELEMENTS(v));
		} else if (!strcmp(a[0], "caps")) {
			FakeGbValue v[] = {{'u', 0x1234}};
			callback(CB_SET_CAPABILITIES, GNSS_CB_IFACE, v, atoi(a[1]));
		} else if (!strcmp(a[0], "cb")) {
			/* cb|code|interface: any other transaction */
			callback(atoi(a[1]), a[2], NULL, 0);
		} else if (!strcmp(a[0], "state")) {
			flush_log();
			printf("state running=%d clients=%u client=%s retry=%s\n", bridge.running,
			       g_list_length(bridge.clients), bridge.client ? "set" : "none",
			       bridge.retry_id ? "pending" : "none");
		} else if (!strcmp(a[0], "acall")) {
			/* acall|code[|o]: Android calls IGnss, with its callback object when o */
			FakeGbValue v[] = {{'o'}};
			android_call(atoi(a[1]), v, a[2] && *a[2] == 'o' ? 1 : 0);
		} else if (!strcmp(a[0], "adie")) {
			on_android_died(NULL, NULL);
		} else if (!strcmp(a[0], "apresence")) {
			fake_gb_sm_present = atoi(a[1]);
			on_android_presence(NULL, NULL);
		} else if (!strcmp(a[0], "aadded")) {
			on_android_added(NULL, atoi(a[1]), NULL);
		} else if (!strcmp(a[0], "astart")) {
			android_start();
		} else if (!strcmp(a[0], "awatch")) {
			watch = android_watch();
			printf("watch=%s node=%s\n", watch ? "set" : "none", android.node);
		} else if (!strcmp(a[0], "anode")) {
			/* anode|mode: create or chmod the device node (octal mode), - removes it */
			if (*a[1] == '-') {
				unlink(ANDROID_DEVICE);
			} else {
				int fd = open(ANDROID_DEVICE, O_CREAT | O_WRONLY, 0600);
				if (fd >= 0)
					close(fd);
				chmod(ANDROID_DEVICE, strtol(a[1], NULL, 8));
			}
		} else if (!strcmp(a[0], "spin")) {
			/* Let the file monitor deliver: up to 2 s, until Android's binder is open */
			for (int j = 0; j < 200 && !android.sm; j++) {
				while (g_main_context_iteration(NULL, FALSE))
					;
				g_usleep(10000);
			}
		} else if (!strcmp(a[0], "astate")) {
			flush_log();
			printf("astate sm=%s callback=%s started=%d sv=%d nmea=%d\n",
			       android.sm ? "set" : "none", android.callback ? "set" : "none",
			       android.started, android.sv_status, android.nmea);
		}
	}
	flush_log();
	return 0;
}
"""


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    d = tmp_path_factory.mktemp("gnss_hal")
    run_dir = d / "run"
    # A socket's path holds at most 107 bytes: with a long --basetemp the socket goes to a short
    # directory of its own
    short = None
    if len(str(run_dir / "nmea.sock")) > 100:
        short = run_dir = Path(tempfile.mkdtemp(prefix="dhw-gnss-"))
    sandboxed_source(SRC, d, [("/run/gnss-nmea-bridge/nmea.sock", run_dir / "nmea.sock"),
                              ("/dev/anbox-binder", d / "anbox-binder")],
                     allow=("/dev/binder",))
    exe = build_harness(d, "gnss_hal", HARNESS, pkgs=("gio-unix-2.0",))
    yield d, exe, run_dir
    if short:
        shutil.rmtree(short, ignore_errors=True)


@pytest.fixture
def gnss(built):
    return functools.partial(run, built[1])


def gb(out):
    """The HAL transactions and parcels, in order."""
    return [line[3:] for line in out.splitlines() if line.startswith("gb ")]


def states(out):
    return [line[6:] for line in out.splitlines() if line.startswith("state ")]


START = ["get-service android.hardware.gnss.IGnss/default", "request", "object", "tx 1",
         "request", "i32 1", "i32 28", "i32 0", "i32 0", "i32 1000", "i32 0", "i32 0", "i32 0",
         "tx 20", "tx 14", "tx 25"]


def test_first_client_starts_the_receiver_last_one_stops_it(gnss):
    out = gnss("client", "state", "client", "state", "close|0", "state", "close|1", "state",
               "timers", "state").stdout
    log = gb(out)
    # setCallback, then setPositionMode with 1 Hz standalone, start, startNmea - once
    assert log[:len(START)] == START
    s = states(out)
    assert s[0] == "running=1 clients=1 client=set retry=none"
    assert s[1] == "running=1 clients=2 client=set retry=none"
    assert s[2] == "running=1 clients=1 client=set retry=none"
    # The last client gone: stopping waits for the idle callback (never inside a HAL callback)
    assert s[3] == "running=1 clients=0 client=set retry=pending"
    assert "timer 0s" in out
    assert log[len(START):] == ["tx 26", "tx 15"]
    assert s[4] == "running=0 clients=0 client=set retry=none"


def test_client_input_is_discarded(gnss):
    out = gnss("client", "input|0", "state").stdout
    assert states(out)[-1] == "running=1 clients=1 client=set retry=none"


def test_hal_not_registered_yet_is_retried(gnss):
    out = gnss("noservice", "client", "state", "timers", "state", "heal", "timers", "state").stdout
    s = states(out)
    assert s[0] == "running=0 clients=1 client=none retry=pending"
    assert "timer 3s" in out
    # Still not there: the next retry is scheduled
    assert s[1] == "running=0 clients=1 client=none retry=pending"
    assert s[2] == "running=1 clients=1 client=set retry=none"
    assert gb(out).count("get-service android.hardware.gnss.IGnss/default") == 3


@pytest.mark.parametrize("fail,expected_tail", [
    # setCallback refused (AIDL exception): all references dropped again
    ("1|0|-3", ["tx 1", "remove-death-handler 1"]),
    # the binder call failed, no reply at all
    ("1|-32|0", ["tx 1", "remove-death-handler 1"]),
    # setPositionMode refused: start is not tried
    ("20|0|-8", ["tx 20"]),
    ("14|0|-1", ["tx 20", "tx 14"]),
])
def test_refused_start_is_retried(gnss, fail, expected_tail):
    out = gnss(f"fail|{fail}", "client", "state").stdout
    assert gb(out)[-len(expected_tail):] == expected_tail
    s = states(out)[0]
    assert s.startswith("running=0 clients=1 ") and s.endswith("retry=pending")
    if fail.startswith("1|"):
        assert "client=none" in s


def test_start_nmea_refused_still_runs(gnss):
    # An old HAL without startNmea: positions come from the location callback instead
    out = gnss("fail|25|0|-1", "client", "state").stdout
    assert states(out)[0] == "running=1 clients=1 client=set retry=none"


def test_hal_death_reconnects_while_clients_wait(gnss):
    out = gnss("client", "state", "die", "state").stdout
    log = gb(out)
    assert log[len(START):] == ["remove-death-handler 1"] + START
    assert states(out)[1] == "running=1 clients=1 client=set retry=none"


def test_hal_death_without_clients_stays_off(gnss):
    out = gnss("client", "close|0", "timers", "die", "state").stdout
    assert states(out)[-1] == "running=0 clients=0 client=none retry=none"
    assert gb(out)[-1] == "remove-death-handler 1"


def test_callback_object_delivers_nmea_and_locations(gnss):
    out = gnss("client", "nmea|$GPGSV,1,1,00*79", "read|0", "location", "read|0").stdout
    lines = out.splitlines()
    assert "callback 5 status=0 reply=yes" in lines
    assert "callback 4 status=0 reply=yes" in lines
    got = [line for line in lines if line.startswith("got=")]
    assert got[0] == "got=$GPGSV,1,1,00*79<CR><LF>|"
    assert got[1].startswith("got=$GPRMC,221320.12,A,5231.2005,N,01324.2972,E,")
    # Every answer is an AIDL reply without exception
    assert gb(out).count("reply-i32 0") == 2


@pytest.mark.parametrize("n", [0, 1])
def test_callback_capabilities_and_others_are_acknowledged(gnss, n):
    out = gnss(f"caps|{n}", "cb|9|android.hardware.gnss.IGnssCallback").stdout
    assert "callback 1 status=0 reply=yes" in out
    assert "callback 9 status=0 reply=yes" in out
    assert gb(out) == ["reply-i32 0", "reply-i32 0"]


def test_nmea_parcel_without_timestamp_is_ignored(gnss):
    out = gnss("client", "nmea-empty", "read|0").stdout
    assert "callback 5 status=0 reply=yes" in out
    assert "got=\n" in out


def test_removing_an_unknown_client_changes_nothing(gnss):
    out = gnss("client", "remove-unknown", "state").stdout
    assert "still open=1" in out
    assert states(out)[-1] == "running=1 clients=1 client=set retry=none"


def test_callback_with_another_interface_is_refused(gnss):
    out = gnss("cb|5|android.hardware.gnss.IGnss").stdout
    assert "callback 5 status=-1 reply=no" in out
    assert gb(out) == []


def test_slow_reader_skips_a_sentence(gnss):
    # Nothing written at all: this sentence is skipped, the client stays
    out = gnss("client", "script|W", "line|$A*00\r\n", "line|$B*00\r\n", "read|0", "state").stdout
    assert "got=$B*00<CR><LF>|" in out
    assert states(out)[-1].startswith("running=1 clients=1 ")


def test_part_of_a_line_is_finished(gnss):
    # Three bytes, then the rest after waiting for the socket: one complete line
    out = gnss("client", "script|3,2", "line|$GPGSV*79\r\n", "read|0", "state").stdout
    assert "send 3" in out and "wait 100000" in out
    assert "got=$GPGSV*79<CR><LF>|" in out
    assert states(out)[-1].startswith("running=1 clients=1 ")


@pytest.mark.parametrize("script", ["3,T", "3,E", "E"])
def test_torn_line_drops_the_client(gnss, script):
    # A half sentence that cannot be finished, or a broken connection: the client is dropped
    out = gnss("client", "client", f"script|{script}", "line|$GPGSV*79\r\n", "state").stdout
    s = states(out)[-1]
    assert s.startswith("running=1 clients=1 ")


def test_torn_line_of_the_last_client_stops_the_receiver(gnss):
    out = gnss("client", "script|E", "line|$GPGSV*79\r\n", "state", "timers", "state").stdout
    s = states(out)
    assert s[0] == "running=1 clients=0 client=set retry=pending"
    assert s[1] == "running=0 clients=0 client=set retry=none"
    assert gb(out)[-2:] == ["tx 26", "tx 15"]


# ---- main(): a real socket in tmp ----

def env_for(extra):
    e = dict(os.environ)
    e.update({"ASAN_OPTIONS": "detect_leaks=0:abort_on_error=0:exitcode=86",
              "UBSAN_OPTIONS": "print_stacktrace=1:halt_on_error=1:exitcode=87", "G_DEBUG": "",
              "LC_ALL": "C", "G_MESSAGES_DEBUG": ""})
    e.pop("FAKE_GEOCLUE", None)
    e.pop("FAKE_NO_BINDER", None)
    e.update(extra)
    return e


SLOW = 5 if GCOV_DIR else 1


def wait_for(cond, what, timeout=10):
    end = time.monotonic() + timeout * SLOW
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


class Daemon:
    def __init__(self, exe, env):
        self.p = subprocess.Popen([str(exe), "main"], env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
        os.set_blocking(self.p.stderr.fileno(), False)
        self.err = ""

    def stderr_has(self, text):
        try:
            self.err += os.read(self.p.stderr.fileno(), 65536).decode()
        except BlockingIOError:
            pass
        return text in self.err

    def count(self, text):
        self.stderr_has(text)
        return self.err.count(text)

    def stop(self):
        self.p.send_signal(signal.SIGTERM)
        out, err = self.p.communicate(timeout=20 * SLOW)
        self.err += err or ""
        return out


@pytest.mark.parametrize("geoclue,mode", [(True, 0o660), (False, 0o600)])
def test_main_serves_nmea_on_the_socket(built, geoclue, mode):
    d, exe, run_dir = built
    run_dir.mkdir(exist_ok=True)
    sock_path = run_dir / "nmea.sock"
    sock_path.write_text("stale")   # a leftover of an earlier run is replaced
    env = env_for({"FAKE_GEOCLUE": "1"} if geoclue else {})
    daemon = Daemon(exe, env)
    try:
        wait_for(lambda: sock_path.is_socket(), "the socket")
        # Only geoclue's group may read positions; without that group only the owner
        assert sock_path.stat().st_mode & 0o777 == mode
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.connect(str(sock_path))
        wait_for(lambda: daemon.stderr_has("GNSS started"), "the receiver to start")
        daemon.p.send_signal(signal.SIGUSR1)
        c.settimeout(10 * SLOW)
        assert c.recv(100) == b"$GPGSV,1,1,00*79\r\n"
        c.close()
        wait_for(lambda: daemon.stderr_has("GNSS stopped"), "the receiver to stop")
        # A second client while it is off: started again, stopped at exit
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.connect(str(sock_path))
        wait_for(lambda: daemon.count("GNSS started") == 2, "the receiver to start again")
    finally:
        out = daemon.stop()
    assert daemon.p.returncode == 0, daemon.err
    log = gb(out)
    assert log[0] == "servicemanager /dev/binder"
    assert log[1:1 + len(START)] == START
    # Stopped at exit while a client was still connected, then the HAL released
    assert log[-3:] == ["tx 26", "tx 15", "remove-death-handler 1"]
    assert "rc=0 running=0 client=none" in out
    assert not sock_path.exists()
    c.close()


def test_main_without_binder(built):
    d, exe, run_dir = built
    r = subprocess.run([str(exe), "main"], env=env_for({"FAKE_NO_BINDER": "1"}),
                       capture_output=True, text=True, timeout=20 * SLOW)
    assert "rc=1" in r.stdout
    assert "Cannot open /dev/binder" in r.stderr


def test_main_cannot_listen(built):
    d, exe, run_dir = built
    shutil.rmtree(run_dir, ignore_errors=True)
    r = subprocess.run([str(exe), "main"], env=env_for({}), capture_output=True, text=True,
                       timeout=20 * SLOW)
    assert "rc=1" in r.stdout
    assert "Cannot listen on" in r.stderr


# ---- IGnss for Android (Waydroid container) ----

CB = "android.hardware.gnss.IGnssCallback"
GET_VERSION, GET_HASH = 0x00ffffff, 0x00fffffe
SET_CALLBACK, CLOSE, START_, STOP_ = 1, 2, 14, 15
START_SV, STOP_SV, START_NMEA, STOP_NMEA = 23, 24, 25, 26
NOT_SUPPORTED = ["i32 -7", "s16 Not supported", "i32 0"]


def calls(out):
    """Per Android call: the reply parcel (gb lines just before it) and its status."""
    result, pending = [], []
    for line in out.splitlines():
        if line.startswith("gb "):
            pending.append(line[3:])
        elif line.startswith("acall "):
            result.append((line, pending))
            pending = []
        else:
            pending = []
    return result


def astates(out):
    return [line[7:] for line in out.splitlines() if line.startswith("astate ")]


@pytest.fixture
def node(built):
    """The test's stand-in for /dev/anbox-binder, absent before and after each test."""
    path = built[0] / "anbox-binder"
    path.unlink(missing_ok=True)
    yield path
    path.unlink(missing_ok=True)


def test_android_gets_version_and_hash(gnss):
    c = calls(gnss(f"acall|{GET_VERSION}", f"acall|{GET_HASH}").stdout)
    assert c[0] == (f"acall {GET_VERSION} status=0 reply=yes", ["i32 0", "i32 2"])
    assert c[1] == (f"acall {GET_HASH} status=0 reply=yes",
                    ["i32 0", "s16 fc957f1d3d261d065ff5e5415f2d21caa79c310f"])


def test_set_callback_needs_an_object(gnss):
    out = gnss(f"acall|{SET_CALLBACK}", "astate").stdout
    assert calls(out)[0][1] == NOT_SUPPORTED
    assert astates(out) == ["sm=none callback=none started=0 sv=0 nmea=0"]


def test_set_callback_reports_no_capabilities(gnss):
    out = gnss(f"acall|{SET_CALLBACK}|o", "astate").stdout
    # setCapabilitiesCb(0), one-way, then EX_NONE
    assert calls(out)[0][1] == ["request", "i32 0", "tx-async 1 flags 1", "i32 0"]
    assert astates(out) == ["sm=none callback=set started=0 sv=0 nmea=0"]


def test_android_start_and_stop_run_the_receiver(gnss):
    out = gnss(f"acall|{SET_CALLBACK}|o", f"acall|{START_}", "state", f"acall|{STOP_}", "state").stdout
    c = calls(out)
    assert c[1][1][:len(START)] == START and c[1][1][-1] == "i32 0"
    assert c[2][1] == ["tx 26", "tx 15", "i32 0"]
    assert states(out) == ["running=1 clients=0 client=set retry=none",
                           "running=0 clients=0 client=set retry=none"]


def test_receiver_stays_on_for_nmea_clients_when_android_stops(gnss):
    out = gnss("client", f"acall|{SET_CALLBACK}|o", f"acall|{START_}", f"acall|{STOP_}", "state").stdout
    assert states(out) == ["running=1 clients=1 client=set retry=none"]
    assert calls(out)[-1][1] == ["i32 0"]


def test_locations_and_status_go_to_android(gnss):
    out = gnss(f"acall|{SET_CALLBACK}|o", f"acall|{START_}", "location", f"cb|2|{CB}").stdout
    log = gb(out)
    i = log.index("tx-async 4 flags 1")
    # The parcel after the interface header, one-way, before the bridge handles it itself
    assert log[i - 2:i] == ["request", "bytes 52"]
    assert log[-3:] == ["bytes 0", "tx-async 2 flags 1", "reply-i32 0"]


def test_satellites_and_nmea_only_when_android_asked(gnss):
    out = gnss(f"acall|{SET_CALLBACK}|o", f"acall|{START_}", "nmea|$GPGSV,1,1,00*79", f"cb|3|{CB}",
               f"acall|{START_SV}", f"acall|{START_NMEA}", "astate", "nmea|$GPGSV,1,1,00*79",
               f"cb|3|{CB}", f"acall|{STOP_SV}", f"acall|{STOP_NMEA}", "astate", f"cb|3|{CB}",
               "nmea|$GPGSV,1,1,00*79").stdout
    fwd = [line for line in gb(out) if line.startswith("tx-async")]
    assert fwd == ["tx-async 1 flags 1", "tx-async 5 flags 1", "tx-async 3 flags 1"]
    assert astates(out) == ["sm=none callback=set started=1 sv=1 nmea=1",
                            "sm=none callback=set started=1 sv=0 nmea=0"]


def test_nothing_goes_to_android_before_start_or_without_callback(gnss):
    out = gnss(f"cb|2|{CB}", "location", f"acall|{SET_CALLBACK}|o", f"cb|2|{CB}").stdout
    assert [line for line in gb(out) if line.startswith("tx-async")] == ["tx-async 1 flags 1"]


def test_callback_without_raw_parcel_is_not_forwarded(gnss):
    out = gnss(f"acall|{SET_CALLBACK}|o", f"acall|{START_}", f"cb|2|{CB}",
               env=dict(os.environ, FAKE_NO_DATA="1")).stdout
    assert "tx-async 2 flags 1" not in gb(out)
    assert "callback 2 status=0 reply=yes" in out


@pytest.mark.parametrize("code", [16, 17, 18, 19, 20])
def test_injection_and_position_mode_are_acknowledged(gnss, code):
    assert calls(gnss(f"acall|{code}").stdout)[0][1] == ["i32 0"]


@pytest.mark.parametrize("code", [3, 4, 13, 27])
def test_extensions_are_not_supported(gnss, code):
    assert calls(gnss(f"acall|{code}").stdout)[0][1] == NOT_SUPPORTED


@pytest.mark.parametrize("step", [f"acall|{CLOSE}", "adie", "apresence|0"])
def test_android_gone_stops_the_receiver(gnss, step):
    out = gnss(f"acall|{SET_CALLBACK}|o", f"acall|{START_}", f"acall|{START_SV}", step, "state",
               "astate").stdout
    assert "remove-death-handler 1" in gb(out)
    assert states(out)[-1] == "running=0 clients=0 client=set retry=none"
    assert astates(out) == ["sm=none callback=none started=0 sv=0 nmea=0"]


def test_new_callback_replaces_the_old_one(gnss):
    out = gnss(f"acall|{SET_CALLBACK}|o", f"acall|{SET_CALLBACK}|o", "astate").stdout
    assert gb(out).count("remove-death-handler 1") == 1
    assert astates(out) == ["sm=none callback=set started=0 sv=0 nmea=0"]


def test_registration_follows_the_service_manager(gnss):
    out = gnss("apresence|1", "aadded|0", "aadded|-2147483647")
    assert gb(out.stdout) == ["add-service android.hardware.gnss.IGnss/default"]
    assert "Serving GNSS to Android" in out.stderr
    assert "Cannot serve GNSS to Android (status -2147483647)" in out.stderr


def test_start_waits_for_an_open_node(gnss, node):
    if os.geteuid() == 0:
        pytest.skip("root opens any node")
    out = gnss("astart", "astate", "anode|0", "astart", "astate", "anode|600", "astart", "astate",
               "astart", "astate").stdout
    assert astates(out) == ["sm=none callback=none started=0 sv=0 nmea=0"] * 2 + \
        ["sm=set callback=none started=0 sv=0 nmea=0"] * 2
    # Opened once, registered once
    assert gb(out) == [f"servicemanager {node}", "add-service android.hardware.gnss.IGnss/default"]


def test_container_start_opens_the_node(gnss, node):
    if os.geteuid() == 0:
        pytest.skip("root opens any node")
    out = gnss("anode|0", "awatch", "astart", "astate", "anode|666", "spin", "astate").stdout
    assert f"watch=set node={node.name}" in out
    assert astates(out) == ["sm=none callback=none started=0 sv=0 nmea=0",
                            "sm=set callback=none started=0 sv=0 nmea=0"]


def test_watch_without_node_watches_its_directory(gnss, node):
    out = gnss("awatch").stdout
    assert f"watch=set node={node.name}" in out


def test_node_that_is_no_binder_is_skipped(gnss, node):
    out = gnss("anode|600", "astart", "astate", env=dict(os.environ, FAKE_NO_BINDER="1")).stdout
    assert astates(out) == ["sm=none callback=none started=0 sv=0 nmea=0"]
    assert gb(out) == []
