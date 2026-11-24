# plinius-sar: SAR sensor events (layout, channels, threshold, delay before "away"), the scene
# sent to the modem (AT+ERFIDX or the SAR indicator), the RIL's answers, the configuration and
# the command written to the sensor hub. libgbinder is faked, the sensor hub is a file.

import errno
import functools
import os
import struct

import pytest

from dhw_harness import PKG, build_harness, kv, run, sandboxed_source

SRC = PKG / "plinius-sar" / "plinius-sar.c"

HARNESS = r"""
#include <glib.h>
#include <gio/gio.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#define main real_main
#include "plinius-sar.c"
#undef main

static struct slot *slot;

static void flush_log(const char *label)
{
	printf("%s=%s\n", label, fake_gb_log ? g_strdelimit(fake_gb_log->str, "\n", ';') : "");
	if (fake_gb_log)
		g_string_truncate(fake_gb_log, 0);
}

int main(int argc, char **argv)
{
	self.loop = g_main_loop_new(NULL, FALSE);
	self.slots = g_ptr_array_new();
	slot = g_new0(struct slot, 1);
	slot->name = g_strdup("slot1");
	slot->client = &fake_gb_instance;
	slot->scene_sent = slot->scene_requested = G_MININT;
	g_ptr_array_add(self.slots, slot);
	for (int i = 1; i < argc; i++) {
		g_auto(GStrv) a = g_strsplit(argv[i], "|", -1);
		if (!strcmp(a[0], "config")) {
			load_config(a[1]);
			printf("config=%d %d %d %d %u %u %u %u %u", self.method, self.near_scene, self.far_scene,
			       self.near_threshold, self.channel_mask, self.far_delay_ms, self.sampling_ms,
			       self.refresh_s, self.retry_s);
			for (char **n = self.slot_names; *n; n++)
				printf(" %s", *n);
			printf("\n");
		} else if (!strcmp(a[0], "events")) {
			int fd = open(a[1], O_RDONLY);
			on_sensor_readable(fd, G_IO_IN, NULL);
			close(fd);
			/* A pending "away" comes from the main loop after FarDelayMs */
			for (int t = 0; t < 200 && self.far_timer; t++) {
				g_main_context_iteration(NULL, FALSE);
				g_usleep(1000);
			}
			printf("near=%d words=%d,%d,%d\n", self.near, self.last_words[0], self.last_words[1],
			       self.last_words[2]);
			flush_log("sent");
		} else if (!strcmp(a[0], "answer")) {
			/* answer|code|type|serial offset (0: the one sent)|error|modem line */
			FakeGbValue v[] = {
				{'i', 1}, {'i', 20}, {'i', atoi(a[2])}, {'i', slot->serial + atoi(a[3])},
				{'i', atoi(a[4])}, {'i', 1}, {'s', 0, 0, a[5] ? a[5] : ""},
			};
			fake_gb_request_vals = v;
			fake_gb_request_n = G_N_ELEMENTS(v);
			int status = -1;
			on_response(NULL, NULL, atoi(a[1]), 0, &status, slot);
			printf("answer: sent=%d requested=%d failures=%u retry=%d timer=%d\n", slot->scene_sent,
			       slot->scene_requested, slot->failures, slot->retry_timer != 0, slot->answer_timer != 0);
			flush_log("acked");
		} else if (!strcmp(a[0], "refresh")) {
			on_refresh(NULL);
			flush_log("refresh");
		} else if (!strcmp(a[0], "enable")) {
			self.hf_fd = open(a[1], O_WRONLY | O_CREAT | O_TRUNC, 0600);
			printf("enable=%d\n", sensor_enable(atoi(a[2])));
			close(self.hf_fd);
		}
	}
	return 0;
}
"""


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    d = tmp_path_factory.mktemp("sar")
    sandboxed_source(SRC, d, [
        ("/etc/plinius-sar.conf", d / "default.conf"),
        ("/dev/hf_manager", d / "hf_manager"),
    ], allow=("/dev/binder",))
    exe = build_harness(d, "sar", HARNESS, pkgs=("gio-unix-2.0",))
    return d, functools.partial(run, exe)


@pytest.fixture
def sar(built):
    return built


def event(words, sensor_type=72, action=0, ts=1_500_000_000):
    w = list(words) + [0] * (16 - len(words))
    return struct.pack("<qBBBB16i", ts, sensor_type, 3, action, 0, *w)


def config(d, text):
    p = d / "sar.conf"
    p.write_text("[SAR]\n" + text)
    return p


def test_event_layout():
    # struct hf_manager_event, packed: 8 + 4 + 16 * 4
    assert len(event([1])) == 76


def test_defaults(sar):
    d, binary = sar
    out = kv(binary(f"config|{d}/none.conf").stdout)
    assert out["config"] == "0 0 -1 1 7 1000 200 600 30 slot1 slot2"


def test_config(sar):
    d, binary = sar
    c = config(d, "Method=indicator\nNearScene=3\nFarScene=-1\nNearThreshold=0\nChannels=0;2;7\n"
                  "FarDelayMs=-5\nSamplingMs=1\nRefreshSeconds=0\nRetrySeconds=99999\nSlots=slot2\n")
    # Threshold at least 1, channel 7 does not exist, delays and intervals clamped
    assert kv(binary(f"config|{c}").stdout)["config"] == "1 3 -1 1 5 0 20 0 3600 slot2"


def test_config_without_valid_channels(sar):
    d, binary = sar
    c = config(d, "Channels=5;9\nMethod=bogus\n")
    assert kv(binary(f"config|{c}").stdout)["config"].startswith("0 0 -1 1 7 ")


def run_events(d, binary, cfg, *batches):
    args = [f"config|{config(d, cfg)}"]
    for n, batch in enumerate(batches):
        p = d / f"ev{n}"
        p.write_bytes(b"".join(batch))
        args.append(f"events|{p}")
    out = binary(*args).stdout
    near = [line.split()[0] for line in out.splitlines() if line.startswith("near=")]
    sent = [line[5:] for line in out.splitlines() if line.startswith("sent=")]
    return near, sent


def test_near_and_away_send_scenes(sar):
    d, binary = sar
    near, sent = run_events(d, binary, "FarDelayMs=0\n", [event([0, 5, 0])], [event([0, 0, 0])])
    assert near == ["near=1", "near=0"]
    assert sent[0] == "request 11;i32 1;i32 2;s16 AT+ERFIDX=1,0;s16 ;i32 0;tx-async 11 flags 1;"
    assert sent[1] == "request 11;i32 2;i32 2;s16 AT+ERFIDX=1,-1;s16 ;i32 0;tx-async 11 flags 1;"


def test_away_only_after_delay(sar):
    d, binary = sar
    # With a delay the away event alone does not switch back within the batch; the main loop
    # does after FarDelayMs
    near, sent = run_events(d, binary, "FarDelayMs=30\n", [event([0, 5, 0])],
                            [event([0, 0, 0]), event([0, 4, 0])], [event([0, 0, 0])])
    # The second batch's near cancels the pending away: nothing sent then
    assert near == ["near=1", "near=1", "near=0"]
    assert sent[1] == ""
    assert "AT+ERFIDX=1,-1" in sent[2]


@pytest.mark.parametrize("words,cfg,near", [
    ([0, 0, -3], "", 1),                         # negative values count by their size
    ([0, 0, 0, 9, 9, 9], "", 0),                  # words 3-5 are no channels
    ([0, 0, 5], "Channels=0;1\n", 0),             # channel 2 not counted
    ([2, 0, 0], "NearThreshold=3\n", 0),
    ([3, 0, 0], "NearThreshold=3\n", 1),
])
def test_channels_and_threshold(sar, words, cfg, near):
    d, binary = sar
    n, _ = run_events(d, binary, cfg, [event(words)])
    assert n == [f"near={near}"]


def test_other_events_ignored(sar):
    d, binary = sar
    n, sent = run_events(d, binary, "", [event([5, 5, 5], sensor_type=1), event([5, 5, 5], action=1),
                                         event([5, 5, 5])[:40]])
    # Another sensor, a flush, and a torn half event: no change
    assert n == ["near=0"] and sent == [""]


def test_indicator_method(sar):
    d, binary = sar
    near, sent = run_events(d, binary, "Method=indicator\nFarDelayMs=0\nNearScene=2\n",
                            [event([5])], [event([0])])
    assert sent[0].startswith("request 12;i32 1;i32 1;s16 1,2;i32 0;tx-async 12")
    assert sent[1].startswith("request 12;i32 2;i32 0;s16 1,-1;i32 0;tx-async 12")


def test_answers(sar):
    d, binary = sar
    p = d / "near"
    p.write_bytes(event([5]))
    out = binary(f"config|{config(d, '')}", f"events|{p}",
                 "answer|7|0|1|0|",            # wrong serial: not ours
                 "answer|7|0|0|2|ERROR",       # rejected: retried later
                 "answer|7|2|0|0|OK",          # confirmed; ACK_EXP wants an acknowledgement
                 "refresh").stdout
    answers = [line for line in out.splitlines() if line.startswith("answer:")]
    assert answers[0] == "answer: sent=-2147483648 requested=0 failures=0 retry=0 timer=1"
    assert answers[1] == "answer: sent=-2147483648 requested=0 failures=1 retry=1 timer=0"
    assert answers[2] == "answer: sent=0 requested=0 failures=0 retry=1 timer=0"
    acked = [line for line in out.splitlines() if line.startswith("acked=")]
    assert acked[2] == "acked=request 25;tx-async 25 flags 1;"
    # The refresh sends the scene again although the modem confirmed it
    assert "AT+ERFIDX=1,0" in kv(out)["refresh"]


def test_sensor_enable_command(sar):
    d, binary = sar
    p = d / "hf"
    out = binary(f"config|{config(d, 'SamplingMs=250')}", f"enable|{p}|1").stdout
    assert kv(out)["enable"] == "1"
    data = p.read_bytes()
    assert len(data) == 52
    sensor_type, action, length = data[0], data[1], data[2]
    assert (sensor_type, action, length) == (72, 1, 16)
    delay, latency = struct.unpack("<qq", data[4:20])
    assert (delay, latency) == (250_000_000, 0)
    binary(f"enable|{p}|0")
    assert p.read_bytes()[:3] == bytes([72, 0, 0])


# ---- The daemon as it runs: main() with the modem's slots, the sensor hub and a fake clock ----
#
# Arguments before "--" go to the daemon's main(), the commands after it run in its main loop:
# ev=<w0>,<w1>,<w2>[/...] one batch of SAR events from the sensor hub; wait=<ms> advances the fake
# clock (timers fire); answer=<slot>|<code>|<type>|<serial offset>|<error>[|<modem line>] is the
# RIL's response; die=<slot> the radio extension going away; service=<slot>|0|1 whether the service
# manager has it; register=<status> the status of setResponseFunctionsMtk; transact-fails=0|1;
# usr1, term: those signals; state prints each slot. Every binder call is printed ("gb ...").
# Before the start (environment): FAKE_HF_OPEN=enoent:<n>|enoent:all|eacces, FAKE_HF_WRITE=
# einval:<n>|eio, FAKE_SM=none|nowait, FAKE_MISSING=<slot>.

SERVICE = r"""
#include <errno.h>
#include <fcntl.h>
#include <glib.h>
#include <gio/gio.h>
#include <glib-unix.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include "gbinder.h"

/* ---- Fake clock ---- */
typedef struct { guint id; gint64 due; guint interval; GSourceFunc fn; gpointer data; } Timer;
static GArray *timers;
static gint64 now_ms;
static guint next_id = 100;
static gboolean quit_called;
static guint fake_timeout_add(guint ms, GSourceFunc fn, gpointer data)
{
	if (!timers)
		timers = g_array_new(FALSE, FALSE, sizeof(Timer));
	Timer t = {next_id++, now_ms + ms, ms, fn, data};
	g_array_append_val(timers, t);
	return t.id;
}
static guint fake_timeout_add_seconds(guint s, GSourceFunc fn, gpointer data)
{
	return fake_timeout_add(s * 1000, fn, data);
}
static gboolean fake_source_remove(guint id)
{
	for (guint i = 0; timers && i < timers->len; i++)
		if (g_array_index(timers, Timer, i).id == id) {
			g_array_remove_index(timers, i);
			return TRUE;
		}
	printf("source_remove of unknown %u\n", id);
	return FALSE;
}
static void advance(gint64 ms)
{
	gint64 end = now_ms + ms;
	while (!quit_called) {
		int best = -1;
		for (guint i = 0; timers && i < timers->len; i++)
			if (g_array_index(timers, Timer, i).due <= end &&
			    (best < 0 || g_array_index(timers, Timer, i).due < g_array_index(timers, Timer, best).due))
				best = i;
		if (best < 0)
			break;
		Timer t = g_array_index(timers, Timer, best);
		now_ms = t.due;
		gboolean again = t.fn(t.data);
		for (guint i = 0; i < timers->len; i++)
			if (g_array_index(timers, Timer, i).id == t.id) {
				if (again)
					g_array_index(timers, Timer, i).due += t.interval;
				else
					g_array_remove_index(timers, i);
				break;
			}
	}
	if (!quit_called)
		now_ms = end;
}
static gint64 slept_us;
static void fake_usleep(gulong us)
{
	slept_us += us;
}
static void fake_quit(GMainLoop *loop)
{
	printf("quit at %" G_GINT64_FORMAT "\n", now_ms);
	quit_called = TRUE;
}
/* Signals: the handler is wrapped to see it ran */
static int signals_handled;
typedef struct { GSourceFunc fn; gpointer data; } SigHandler;
static gboolean sig_trampoline(gpointer data)
{
	SigHandler *h = data;
	signals_handled++;
	return h->fn(h->data);
}
static guint fake_signal_add(int signum, GSourceFunc fn, gpointer data)
{
	SigHandler *h = g_new0(SigHandler, 1);
	h->fn = fn;
	h->data = data;
	return g_unix_signal_add(signum, sig_trampoline, h);
}

/* ---- The sensor hub: a pipe the harness writes events into; commands are printed ---- */
static int hf_pipe[2] = { -1, -1 };
static int hf_open_tries;
static int hf_write_einval;
static int hf_write_errno;
static int hf_read_errno;
static int fake_open(const char *path, int flags)
{
	const char *mode = getenv("FAKE_HF_OPEN");
	hf_open_tries++;
	if (mode && g_str_has_prefix(mode, "enoent:") &&
	    (!strcmp(mode + 7, "all") || hf_open_tries <= atoi(mode + 7))) {
		errno = ENOENT;
		return -1;
	}
	if (mode && !strcmp(mode, "eacces")) {
		errno = EACCES;
		return -1;
	}
	if (!g_str_has_suffix(path, "/hf_manager") || pipe(hf_pipe) < 0)
		abort();
	return hf_pipe[0];
}
static ssize_t fake_write(int fd, const void *buf, size_t n)
{
	const unsigned char *c = buf;
	if (hf_write_einval > 0) {
		hf_write_einval--;
		errno = EINVAL;
		return -1;
	}
	if (hf_write_errno) {
		errno = hf_write_errno;
		return -1;
	}
	gint64 delay = 0;
	memcpy(&delay, c + 4, sizeof(delay));
	printf("hf type=%u action=%u length=%u delay=%" G_GINT64_FORMAT " at %" G_GINT64_FORMAT "\n", c[0], c[1],
	       c[2], c[2] ? delay : 0, now_ms);
	return 0;   /* the driver's result, not the byte count */
}
static ssize_t fake_read(int fd, void *buf, size_t n)
{
	if (hf_read_errno) {
		char drop;
		if (read(fd, &drop, 1) != 1)
			abort();
		errno = hf_read_errno;
		hf_read_errno = 0;
		return -1;
	}
	return read(fd, buf, n);
}

/* ---- Binder: one object per slot, the handlers the daemon gives are kept ---- */
typedef struct { struct fake_gb_obj obj; char *name; GBinderRemoteObjectNotifyFunc died; void *died_data; } FakeRemote;
typedef struct { struct fake_gb_obj obj; char *iface; GBinderLocalTransactFunc fn; void *user; } FakeLocal;
static GHashTable *remotes;   /* slot name -> FakeRemote */
static GPtrArray *locals;
static GHashTable *missing;   /* slot names the service manager does not have */
static const char *slot_of(void *c)
{
	GHashTableIter it;
	gpointer k, v;
	g_hash_table_iter_init(&it, remotes);
	while (g_hash_table_iter_next(&it, &k, &v))
		if (v == c)
			return k;
	return "?";
}
static GBinderServiceManager *sar_sm_new(const char *dev)
{
	const char *f = getenv("FAKE_SM");
	gbinder_servicemanager_new(dev);
	return f && !strcmp(f, "none") ? NULL : &fake_gb_instance;
}
static gboolean sar_sm_wait(GBinderServiceManager *sm, long ms)
{
	const char *f = getenv("FAKE_SM");
	fake_gb_logf("wait %ld\n", ms);
	return !(f && !strcmp(f, "nowait"));
}
static GBinderRemoteObject *sar_get_service(GBinderServiceManager *sm, const char *name, int *status)
{
	const char *slot = strrchr(name, '/') + 1;
	gbinder_servicemanager_get_service_sync(sm, name, status);
	if (g_hash_table_contains(missing, slot))
		return NULL;
	FakeRemote *r = g_hash_table_lookup(remotes, slot);
	if (!r) {
		r = g_new0(FakeRemote, 1);
		r->name = g_strdup(slot);
		g_hash_table_insert(remotes, r->name, r);
	}
	return (GBinderRemoteObject *)r;
}
static gulong sar_add_death(GBinderRemoteObject *o, GBinderRemoteObjectNotifyFunc fn, void *user)
{
	FakeRemote *r = (FakeRemote *)o;
	r->died = fn;
	r->died_data = user;
	return 7;
}
static void sar_remove_handler(GBinderRemoteObject *o, gulong id)
{
	fake_gb_logf("remove-death-handler %s %lu\n", ((FakeRemote *)o)->name, id);
	((FakeRemote *)o)->died = NULL;
}
static GBinderClient *sar_client_new(GBinderRemoteObject *o, const char *iface)
{
	fake_gb_logf("client %s %s\n", ((FakeRemote *)o)->name, iface);
	return (GBinderClient *)o;
}
static GBinderLocalObject *sar_local_new(GBinderServiceManager *sm, const char *iface, GBinderLocalTransactFunc fn,
					 void *user)
{
	FakeLocal *l = g_new0(FakeLocal, 1);
	l->iface = g_strdup(iface);
	l->fn = fn;
	l->user = user;
	g_ptr_array_add(locals, l);
	return (GBinderLocalObject *)l;
}
static void sar_set_stability(GBinderLocalObject *o, int stability)
{
	fake_gb_logf("stability %s %#x\n", ((FakeLocal *)o)->iface, stability);
}
static GBinderLocalRequest *sar_new_request2(GBinderClient *c, guint32 code)
{
	fake_gb_logf("request %u %s\n", code, slot_of(c));
	return &fake_gb_instance;
}
#define gbinder_servicemanager_new sar_sm_new
#define gbinder_servicemanager_wait sar_sm_wait
#define gbinder_servicemanager_get_service_sync sar_get_service
#define gbinder_remote_object_add_death_handler sar_add_death
#define gbinder_remote_object_remove_handler sar_remove_handler
#define gbinder_client_new sar_client_new
#define gbinder_servicemanager_new_local_object sar_local_new
#define gbinder_local_object_set_stability sar_set_stability
#define gbinder_client_new_request2 sar_new_request2

static void script_loop(GMainLoop *loop);
#define g_main_loop_run script_loop
#define g_main_loop_quit fake_quit
#define g_timeout_add fake_timeout_add
#define g_timeout_add_seconds fake_timeout_add_seconds
#define g_source_remove fake_source_remove
#define g_usleep fake_usleep
#define g_unix_signal_add fake_signal_add
#define open fake_open
#define write fake_write
#define read fake_read
#define main real_main
#include "plinius-sar.c"
#undef main
#undef open
#undef write
#undef read
#undef g_main_loop_run
#undef g_main_loop_quit
#undef g_unix_signal_add

static char **script;

static void flush_gb(void)
{
	if (!fake_gb_log || !fake_gb_log->len)
		return;
	g_auto(GStrv) lines = g_strsplit(fake_gb_log->str, "\n", -1);
	for (char **l = lines; *l; l++)
		if (**l)
			printf("gb %s\n", *l);
	g_string_truncate(fake_gb_log, 0);
}

static struct slot *find_slot(const char *name)
{
	for (guint i = 0; self.slots && i < self.slots->len; i++) {
		struct slot *s = g_ptr_array_index(self.slots, i);
		if (!strcmp(s->name, name))
			return s;
	}
	printf("no slot %s\n", name);
	exit(98);
}

static FakeLocal *local_of(struct slot *s, const char *iface)
{
	for (guint i = locals->len; i-- > 0;) {
		FakeLocal *l = g_ptr_array_index(locals, i);
		if (l->user == s && !strcmp(l->iface, iface))
			return l;
	}
	printf("no %s object for %s\n", iface, s->name);
	exit(98);
}

static void drain(void)
{
	while (!quit_called && g_main_context_iteration(NULL, FALSE))
		;
}

static void command(const char *arg)
{
	g_auto(GStrv) a = g_strsplit(arg, "=", 2);
	if (!strcmp(a[0], "ev")) {
		g_auto(GStrv) evs = g_strsplit(a[1], "/", -1);
		for (char **e = evs; *e; e++) {
			struct hf_manager_event ev = {.timestamp = 1234567000000 + now_ms * 1000000,
						      .sensor_type = SENSOR_TYPE_SAR, .accuracy = 3};
			g_auto(GStrv) w = g_strsplit(*e, ",", -1);
			for (int i = 0; w[i] && i < 16; i++)
				ev.word[i] = atoi(w[i]);
			if (write(hf_pipe[1], &ev, sizeof(ev)) != sizeof(ev))
				abort();
		}
		drain();
	} else if (!strcmp(a[0], "wait")) {
		advance(atoi(a[1]));
	} else if (!strcmp(a[0], "answer") || !strcmp(a[0], "short")) {
		g_auto(GStrv) v = g_strsplit(a[1], "|", -1);
		struct slot *s = find_slot(v[0]);
		FakeGbValue vals[] = {
			{'i', 1}, {'i', 20}, {'i', v[2] ? atoi(v[2]) : 0}, {'i', s->serial + (v[2] ? atoi(v[3]) : 0)},
			{'i', v[2] ? atoi(v[4]) : 0}, {'i', v[2] && v[5] ? 1 : 0}, {'s', 0, 0, v[2] && v[5] ? v[5] : ""},
		};
		fake_gb_request_vals = vals;
		/* "short": the response ends after its type */
		fake_gb_request_n = !strcmp(a[0], "short") ? 3 : G_N_ELEMENTS(vals);
		FakeLocal *l = local_of(s, MODEM_RESPONSE_IFACE);
		int status = -1;
		GBinderLocalReply *reply = l->fn((GBinderLocalObject *)l, NULL, atoi(v[1]), 0, &status, l->user);
		printf("response status=%d reply=%s\n", status, reply ? "yes" : "no");
	} else if (!strcmp(a[0], "indication")) {
		struct slot *s = find_slot(a[1]);
		FakeLocal *l = local_of(s, MODEM_INDICATION_IFACE);
		int status = -1;
		GBinderLocalReply *reply = l->fn((GBinderLocalObject *)l, NULL, 3, 0, &status, l->user);
		printf("indication status=%d reply=%s\n", status, reply ? "yes" : "no");
	} else if (!strcmp(a[0], "die")) {
		FakeRemote *r = g_hash_table_lookup(remotes, a[1]);
		if (!r || !r->died)
			exit(98);
		r->died((GBinderRemoteObject *)r, r->died_data);
	} else if (!strcmp(a[0], "service")) {
		g_auto(GStrv) v = g_strsplit(a[1], "|", 2);
		if (atoi(v[1]))
			g_hash_table_remove(missing, v[0]);
		else
			g_hash_table_add(missing, g_strdup(v[0]));
	} else if (!strcmp(a[0], "register")) {
		fake_gb_status = atoi(a[1]);
	} else if (!strcmp(a[0], "transact-fails")) {
		fake_gb_transact_fails = atoi(a[1]);
	} else if (!strcmp(a[0], "read-errno")) {
		/* The next read of the sensor hub fails with this errno */
		hf_read_errno = atoi(a[1]);
		if (write(hf_pipe[1], "x", 1) != 1)
			abort();
		drain();
	} else if (!strcmp(a[0], "hf-write-errno")) {
		hf_write_errno = atoi(a[1]);
	} else if (!strcmp(a[0], "usr1") || !strcmp(a[0], "term")) {
		int before = signals_handled;
		raise(!strcmp(a[0], "usr1") ? SIGUSR1 : SIGTERM);
		while (signals_handled == before)
			g_main_context_iteration(NULL, TRUE);
	} else if (!strcmp(a[0], "state")) {
		printf("state at %" G_GINT64_FORMAT " near=%d retry=%d refresh=%d far=%d slept=%" G_GINT64_FORMAT "\n",
		       now_ms, self.near, self.retry_timer != 0, self.refresh_timer != 0, self.far_timer != 0, slept_us);
		for (guint i = 0; self.slots && i < self.slots->len; i++) {
			struct slot *s = g_ptr_array_index(self.slots, i);
			printf("slot %s connected=%d sent=%d requested=%d failures=%u resends=%u retry=%d answer=%d\n",
			       s->name, s->client != NULL, s->scene_sent == G_MININT ? 99 : s->scene_sent,
			       s->scene_requested == G_MININT ? 99 : s->scene_requested, s->failures, s->resends,
			       s->retry_timer != 0, s->answer_timer != 0);
		}
	} else {
		printf("unknown command %s\n", arg);
		exit(98);
	}
}

static void script_loop(GMainLoop *loop)
{
	/* What the daemon did before its main loop */
	flush_gb();
	printf("> loop\n");
	for (char **a = script; a && *a && !quit_called; a++) {
		printf("> %s\n", *a);
		command(*a);
		flush_gb();
		fflush(stdout);
	}
	printf("> loop end\n");
	fflush(stdout);
}

int main(int argc, char **argv)
{
	int n = 1;
	while (n < argc && strcmp(argv[n], "--"))
		n++;
	script = n < argc ? argv + n + 1 : NULL;
	argv[n] = NULL;
	remotes = g_hash_table_new(g_str_hash, g_str_equal);
	missing = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, NULL);
	locals = g_ptr_array_new();
	if (getenv("FAKE_MISSING"))
		g_hash_table_add(missing, g_strdup(getenv("FAKE_MISSING")));
	const char *w = getenv("FAKE_HF_WRITE");
	if (w && g_str_has_prefix(w, "einval:"))
		hf_write_einval = atoi(w + 7);
	else if (w && !strcmp(w, "eio"))
		hf_write_errno = EIO;
	int r = real_main(n, argv);
	flush_gb();
	printf("exit %d tries=%d slept=%" G_GINT64_FORMAT "\n", r, hf_open_tries, slept_us);
	return r;
}
"""


@pytest.fixture(scope="module")
def service_built(tmp_path_factory):
    d = tmp_path_factory.mktemp("sar-service")
    sandboxed_source(SRC, d, [
        ("/etc/plinius-sar.conf", d / "default.conf"),
        ("/dev/hf_manager", d / "hf_manager"),
    ], allow=("/dev/binder",))
    exe = build_harness(d, "sar-service", SERVICE, pkgs=("gio-unix-2.0",))
    return d, exe


@pytest.fixture
def daemon(service_built):
    d, exe = service_built
    (d / "default.conf").unlink(missing_ok=True)

    def start(*args, script=(), env=None, check=True, cfg=None):
        e = {k: v for k, v in os.environ.items() if not k.startswith("FAKE_")}
        e.update(env or {})
        argv = list(args)
        if cfg is not None:
            argv += ["-c", str(config(d, cfg))]
        r = run(exe, *argv, "--", *script, env=e, check=check)
        return r
    return d, start


def at_sent(out):
    """(slot, command) of every scene sent, in order"""
    sent, slot, code = [], None, None
    for line in out.splitlines():
        if line.startswith("gb request "):
            _, _, code, slot = line.split()
        elif line.startswith("gb s16 AT+") and code == "11":
            sent.append((slot, line[7:]))
    return sent


def sections(out, cmd):
    """The output of each run of that command of the script"""
    lines = out.splitlines()
    found = []
    for start in (i + 1 for i, line in enumerate(lines) if line == f"> {cmd}"):
        end = next((i for i in range(start, len(lines)) if lines[i].startswith("> ")), len(lines))
        found.append("\n".join(lines[start:end]))
    assert found, f"{cmd} not in the script"
    return found


def section(out, cmd):
    """The output of the first run of that command"""
    return sections(out, cmd)[0]


def test_daemon_bad_option(daemon):
    d, start = daemon
    r = start("--bogus", check=False)
    assert r.returncode == 2
    assert "Unknown option --bogus" in r.stderr


def test_daemon_unreadable_config_is_reported(daemon):
    d, start = daemon
    (d / "confdir").mkdir(exist_ok=True)
    r = start("-c", str(d / "confdir"), script=["term"])
    assert f"Cannot read {d}/confdir" in r.stderr
    # The defaults: both slots
    assert "gb get-service vendor.mediatek.hardware.mtkradioex.modem.IMtkRadioExModem/slot1" in r.stdout
    assert "gb get-service vendor.mediatek.hardware.mtkradioex.modem.IMtkRadioExModem/slot2" in r.stdout


def test_daemon_start_connects_slots_and_turns_the_sensor_on(daemon):
    d, start = daemon
    r = start(cfg="SamplingMs=100\n", script=["state"])
    out = r.stdout[:r.stdout.index("> loop\n")]
    assert "gb servicemanager /dev/binder" in out
    assert "gb wait 30000" in out
    # Each slot: its client, both local objects as vendor stability, setResponseFunctionsMtk
    assert ("gb client slot1 vendor.mediatek.hardware.mtkradioex.modem.IMtkRadioExModem\n"
            "gb stability vendor.mediatek.hardware.mtkradioex.modem.IMtkRadioExModemResponse 0x3\n"
            "gb stability vendor.mediatek.hardware.mtkradioex.modem.IMtkRadioExModemIndication 0x3\n"
            "gb request 26 slot1\ngb object set\ngb object set\ngb tx 26") in out
    # Nobody near: normal power for both
    assert at_sent(out) == [("slot1", "AT+ERFIDX=1,-1"), ("slot2", "AT+ERFIDX=1,-1")]
    assert "hf type=72 action=1 length=16 delay=100000000 at 0" in out
    assert "SAR sensor on, every 100 ms" in r.stderr
    assert "state at 0 near=0 retry=0 refresh=1 far=0" in r.stdout


def test_daemon_near_and_away_and_normal_power_at_exit(daemon):
    d, start = daemon
    out = start(cfg="Slots=slot1\nFarDelayMs=500\n", script=[
        "answer=slot1|7|0|0|0", "ev=0,7,0", "answer=slot1|7|0|0|0", "ev=0,0,0", "wait=499",
        "ev=0,0,0", "wait=1", "answer=slot1|7|0|0|0", "ev=9,0,0", "answer=slot1|7|0|0|0", "term",
    ]).stdout
    assert at_sent(section(out, "ev=0,7,0")) == [("slot1", "AT+ERFIDX=1,0")]
    # Away only after the delay, counted from the first quiet event
    assert at_sent(section(out, "ev=0,0,0")) == []
    assert at_sent(section(out, "wait=499")) == []
    assert at_sent(section(out, "wait=1")) == [("slot1", "AT+ERFIDX=1,-1")]
    # Stopping: sensor off, the modem back to normal power
    tail = out[out.index("quit at"):]
    assert "hf type=72 action=0 length=0" in tail
    assert at_sent(tail) == [("slot1", "AT+ERFIDX=1,-1")]


def test_daemon_exit_leaves_a_confirmed_normal_power_alone(daemon):
    d, start = daemon
    out = start(cfg="Slots=slot1\n", script=["answer=slot1|7|0|0|0", "term"]).stdout
    assert at_sent(out[out.index("quit at"):]) == []


def test_daemon_sensor_read_errors(daemon):
    d, start = daemon
    r = start(cfg="Slots=slot1\n", script=[f"read-errno={errno.EAGAIN}", "state",
                                            f"read-errno={errno.EIO}", "state"], check=False)
    # EAGAIN: nothing; any other error ends the daemon with 1
    assert "state at 0 near=0" in r.stdout
    assert "Reading the sensor hub failed: Input/output error" in r.stderr
    assert r.returncode == 1
    assert r.stdout.rstrip().endswith("exit 1 tries=1 slept=0")


def test_daemon_waits_for_the_sensor_hub(daemon):
    d, start = daemon
    r = start(cfg="Slots=slot1\n", script=["state"], env={"FAKE_HF_OPEN": "enoent:3"})
    # Three times not there yet, 0.2 s each
    assert "state at 0 near=0 retry=0 refresh=1 far=0 slept=600000" in r.stdout


def test_daemon_gives_up_on_a_missing_sensor_hub(daemon):
    d, start = daemon
    r = start(cfg="Slots=slot1\n", env={"FAKE_HF_OPEN": "enoent:all"}, check=False)
    assert r.returncode == 1
    # 150 tries over 30 s
    assert r.stdout.rstrip().endswith("exit 1 tries=150 slept=30000000")
    assert f"Cannot open {d}/hf_manager: No such file or directory" in r.stderr


def test_daemon_sensor_hub_refused_at_once(daemon):
    d, start = daemon
    r = start(cfg="Slots=slot1\n", env={"FAKE_HF_OPEN": "eacces"}, check=False)
    assert r.returncode == 1
    assert r.stdout.rstrip().endswith("exit 1 tries=1 slept=0")


def test_daemon_sensor_command_retried_while_the_hub_refuses(daemon):
    d, start = daemon
    r = start(cfg="Slots=slot1\n", script=["state"], env={"FAKE_HF_WRITE": "einval:10"})
    # Ten refusals, 0.3 s apart, then taken
    assert "slept=3000000" in r.stdout
    assert "hf type=72 action=1" in r.stdout


@pytest.mark.parametrize("mode,warning", [
    ("einval:11", "SAR sensor command 1 failed: Invalid argument"),
    ("eio", "SAR sensor command 1 failed: Input/output error"),
])
def test_daemon_sensor_that_cannot_be_enabled(daemon, mode, warning):
    d, start = daemon
    r = start(cfg="Slots=slot1\n", env={"FAKE_HF_WRITE": mode}, check=False)
    assert r.returncode == 1
    assert warning in r.stderr


@pytest.mark.parametrize("mode", ["none", "nowait"])
def test_daemon_without_service_manager(daemon, mode):
    d, start = daemon
    r = start(env={"FAKE_SM": mode}, check=False)
    assert r.returncode == 1
    assert "No binder service manager on /dev/binder" in r.stderr
    assert "hf type" not in r.stdout


def test_daemon_missing_slot_is_looked_for_every_5_s(daemon):
    d, start = daemon
    out = start(cfg="", env={"FAKE_MISSING": "slot2"}, script=[
        "ev=5,0,0", "answer=slot1|7|0|0|0", "state", "wait=5000", "service=slot2|1", "wait=5000",
        "state"]).stdout
    first, second = sections(out, "state")
    assert "state at 0 near=1 retry=1" in first
    assert "slot slot2 connected=0" in first
    # Not there at the first retry; found at the second, it gets the current scene at once
    assert "gb get-service vendor.mediatek.hardware.mtkradioex.modem.IMtkRadioExModem/slot2" in \
        sections(out, "wait=5000")[0]
    assert at_sent(sections(out, "wait=5000")[0]) == []
    assert at_sent(sections(out, "wait=5000")[1]) == [("slot2", "AT+ERFIDX=1,0")]
    assert "state at 10000 near=1 retry=0" in second
    assert "slot slot2 connected=1" in second


def test_daemon_slot_whose_registration_fails_is_retried(daemon):
    d, start = daemon
    r = start(cfg="Slots=slot1\n", env={"FAKE_MISSING": "slot1"}, script=[
        "service=slot1|1", "register=-1", "wait=5000", "state", "register=0", "wait=5000", "state"])
    out = r.stdout
    assert "slot1: setResponseFunctionsMtk failed (-1)" in r.stderr
    failed, connected = sections(out, "wait=5000")
    # Registering failed: disconnected again, nothing sent, the next try 5 s later
    assert "gb tx 26\ngb remove-death-handler slot1 7" in failed
    assert at_sent(failed) == []
    assert "slot slot1 connected=0" in sections(out, "state")[0]
    assert "state at 5000 near=0 retry=1" in sections(out, "state")[0]
    assert at_sent(connected) == [("slot1", "AT+ERFIDX=1,-1")]
    assert "slot slot1 connected=1" in sections(out, "state")[1]


def test_daemon_radio_extension_gone_and_back(daemon):
    d, start = daemon
    r = start(cfg="Slots=slot1\n", script=[
        "answer=slot1|7|0|0|2", "state",   # rejected: a retry is pending
        "die=slot1", "state", "wait=2000", "state"])
    out = r.stdout
    states = sections(out, "state")
    assert "slot slot1 connected=1 sent=99 requested=-1 failures=1 resends=0 retry=1 answer=0" in states[0]
    assert "slot1: radio extension gone, waiting for it" in r.stderr
    assert section(out, "die=slot1") == "gb remove-death-handler slot1 7"
    # Its timers went with it, the slot waits for a new connection
    assert "slot slot1 connected=0 sent=99 requested=99 failures=0 resends=0 retry=0 answer=0" in states[1]
    assert "state at 0 near=0 retry=1" in states[1]
    # Two seconds later: connected again, the scene sent again
    assert at_sent(section(out, "wait=2000")) == [("slot1", "AT+ERFIDX=1,-1")]
    assert "slot slot1 connected=1 sent=99 requested=-1 failures=0 resends=0 retry=0 answer=1" in states[2]
    assert "source_remove of unknown" not in out


def test_daemon_dying_while_an_answer_is_due(daemon):
    d, start = daemon
    out = start(cfg="Slots=slot1\n", script=["state", "die=slot1", "state", "wait=1000000"]).stdout
    before, after = sections(out, "state")
    assert "answer=1" in before
    assert "slot slot1 connected=0 sent=99 requested=99 failures=0 resends=0 retry=0 answer=0" in after
    # No timer of the gone slot fires later; only the reconnection happens
    assert "no answer" not in out
    assert "source_remove of unknown" not in out


def test_daemon_rejections_logged_once_then_quietly_retried(daemon):
    d, start = daemon
    r = start(cfg="Slots=slot1\nRetrySeconds=10\n", script=[
        "answer=slot1|7|0|0|1|ERROR", "wait=10000", "answer=slot1|7|0|0|6", "wait=10000",
        "answer=slot1|7|0|0|44", "wait=10000", "answer=slot1|7|0|0|99", "wait=10000",
        "answer=slot1|7|0|0|0|OK", "state"],
        env={"G_MESSAGES_DEBUG": "all"})
    # GLib prints debug messages to stdout
    err = r.stderr + r.stdout
    assert err.count("SAR scene -1 rejected") == 4
    assert ("slot1: SAR scene -1 rejected: RIL RADIO_NOT_AVAILABLE (1), modem: ERROR; "
            "retrying every 10 s") in err
    # The ones after the first only at debug level
    assert "slot1: SAR scene -1 rejected: RIL REQUEST_NOT_SUPPORTED (6)" in err
    assert "slot1: SAR scene -1 rejected: RIL INVALID_ARGUMENTS (44)" in err
    assert "slot1: SAR scene -1 rejected: RIL error (99)" in err
    assert err.count("retrying every") == 1
    assert "slot1: SAR scene -1 set after 4 failures" in err
    # Each retry sent the scene again
    assert len(at_sent(r.stdout)) == 5
    assert "slot slot1 connected=1 sent=-1 requested=-1 failures=0" in r.stdout


def test_daemon_unknown_or_unreadable_responses(daemon):
    d, start = daemon
    r = start(cfg="Slots=slot1\n", script=["answer=slot1|3|0|0|0", "short=slot1|7", "indication=slot1",
                                            "state"])
    out = r.stdout
    assert section(out, "answer=slot1|3|0|0|0") == "response status=0 reply=no"
    assert "slot1: unreadable response 7" in r.stderr
    assert section(out, "indication=slot1") == "indication status=0 reply=no"
    # Still waiting for the answer to the scene
    assert "slot slot1 connected=1 sent=99 requested=-1 failures=0 resends=0 retry=0 answer=1" in out


def test_daemon_missing_answers_register_again_and_resend(daemon):
    d, start = daemon
    r = start(cfg="Slots=slot1\nRetrySeconds=60\nRefreshSeconds=0\n", script=[
        "wait=5000", "wait=5000", "register=-1", "wait=5000", "state", "register=0", "wait=60000",
        "state", "wait=5000", "wait=5000", "wait=5000", "wait=5000", "state", "wait=46000", "state"])
    out = r.stdout
    waits = sections(out, "wait=5000")
    states = sections(out, "state")
    # 1st and 2nd missing answer: registered again (another client may have taken the answers)
    # and the scene sent again
    for s in waits[:2]:
        assert "gb request 26 slot1" in s and at_sent(s) == [("slot1", "AT+ERFIDX=1,-1")]
    assert "slot1: no answer within 5 s, registering again and resending" in r.stderr
    # 3rd: registering again fails, a retry comes after RetrySeconds
    assert "gb tx 26" in waits[2] and at_sent(waits[2]) == []
    assert "slot slot1 connected=1 sent=99 requested=-1 failures=0 resends=3 retry=1 answer=0" in states[0]
    # The retry sends the scene again
    assert at_sent(section(out, "wait=60000")) == [("slot1", "AT+ERFIDX=1,-1")]
    assert "resends=3 retry=0 answer=1" in states[1]
    # The 4th missing answer in a row: no more resends, wait RetrySeconds
    assert "slot1: the RIL does not answer; trying again in 60 s" in r.stderr
    assert at_sent(waits[3]) == []
    assert "resends=0 retry=1 answer=0" in states[2]
    # 60 s after that the scene is sent again, and it starts over
    assert at_sent(section(out, "wait=46000")) == [("slot1", "AT+ERFIDX=1,-1")]
    assert "resends=0 retry=0 answer=1" in states[3]


def test_daemon_transaction_that_cannot_be_sent(daemon):
    d, start = daemon
    r = start(cfg="Slots=slot1\nFarDelayMs=0\n", script=["transact-fails=1", "ev=5", "state"])
    assert "slot1: cannot send AT+ERFIDX=1,0" in r.stderr


def test_daemon_indicator_that_cannot_be_sent(daemon):
    d, start = daemon
    r = start(cfg="Slots=slot1\nMethod=indicator\n", script=["transact-fails=1", "ev=5"])
    assert "slot1: cannot send SAR indicator 1" in r.stderr


def test_daemon_quick_change_replaces_the_answer_timer(daemon):
    d, start = daemon
    out = start(cfg="Slots=slot1\nFarDelayMs=0\n", script=[
        "ev=5", "wait=3000", "ev=0", "wait=3000", "state", "wait=2000", "state"]).stdout
    # The answer timer runs from the last scene sent (at 3 s): at 6 s none is missing yet, at 8 s
    # the answer is overdue and the scene resent
    first, second = sections(out, "state")
    assert "state at 6000" in first and first.endswith("requested=-1 failures=0 resends=0 retry=0 answer=1")
    assert "state at 8000" in second and second.endswith("requested=-1 failures=0 resends=1 retry=0 answer=1")
    assert "source_remove of unknown" not in out


def test_daemon_refresh_and_sigusr1_send_the_scene_again(daemon):
    d, start = daemon
    out = start(cfg="Slots=slot1\nRefreshSeconds=60\n", script=[
        "answer=slot1|7|0|0|0", "wait=59999", "wait=1", "answer=slot1|7|0|0|0", "usr1"]).stdout
    assert at_sent(section(out, "wait=59999")) == []
    assert at_sent(section(out, "wait=1")) == [("slot1", "AT+ERFIDX=1,-1")]
    assert at_sent(section(out, "usr1")) == [("slot1", "AT+ERFIDX=1,-1")]


def test_daemon_without_refresh(daemon):
    d, start = daemon
    out = start(cfg="Slots=slot1\nRefreshSeconds=0\n", script=["state"]).stdout
    assert "refresh=0" in out


def test_daemon_disconnected_slot_gets_nothing(daemon):
    d, start = daemon
    out = start(cfg="Slots=slot1;slot2\nFarDelayMs=0\n", env={"FAKE_MISSING": "slot2"},
                script=["ev=5", "usr1"]).stdout
    assert {slot for slot, _ in at_sent(out)} == {"slot1"}


# ---- --monitor and --scene ----

def test_monitor_prints_the_events(daemon):
    d, start = daemon
    r = start("--monitor", cfg="Channels=1\n", script=["ev=0,3,-2,7,8,9/0,0,0", "term"])
    out = r.stdout
    lines = [line for line in out.splitlines() if "channels" in line]
    assert lines == ["1234.567  channels 0 3 -2  (7 8 9)  accuracy 3  -> near",
                     "1234.567  channels 0 0 0  (0 0 0)  accuracy 3  -> away"]
    # Nothing sent to the modem, the sensor off at the end
    assert "gb" not in out
    assert "hf type=72 action=0 length=0" in out.split("quit at")[1]
    assert out.rstrip().endswith("exit 0 tries=1 slept=0")


def test_monitor_without_sensor(daemon):
    d, start = daemon
    r = start("--monitor", env={"FAKE_HF_OPEN": "eacces"}, check=False)
    assert r.returncode == 1


def test_scene_once_to_every_slot(daemon):
    d, start = daemon
    r = start("--scene", "2", script=["answer=slot1|7|2|0|0|OK", "answer=slot2|7|0|0|0"])
    out = r.stdout
    assert at_sent(out) == [("slot1", "AT+ERFIDX=1,2"), ("slot2", "AT+ERFIDX=1,2")]
    # Nothing from the sensor; ACK_EXP acknowledged; done after the last answer
    assert "hf type" not in out
    assert "gb request 25 slot1" in out
    assert "quit at 0" in section(out, "answer=slot2|7|0|0|0")
    assert out.rstrip().endswith("exit 0 tries=0 slept=0")


def test_scene_rejected_exits_1(daemon):
    d, start = daemon
    r = start("--scene", "-1", cfg="Slots=slot1\n", script=["answer=slot1|7|0|0|2", "state"],
              check=False)
    assert r.returncode == 1
    # No retry in one-shot mode
    assert "quit at 0" in r.stdout
    assert "retry=1" not in r.stdout


def test_scene_without_answer_exits_1(daemon):
    d, start = daemon
    r = start("--scene", "1", script=["answer=slot1|7|0|0|0", "wait=10000"], check=False)
    assert r.returncode == 1
    assert "slot2: no answer from the RIL within 5 s" in r.stderr
    assert "quit at 5000" in r.stdout


def test_scene_without_any_connected_slot_times_out(daemon):
    d, start = daemon
    r = start("--scene", "1", cfg="Slots=slot1\n", env={"FAKE_MISSING": "slot1"},
              script=["wait=10000"], check=False)
    assert r.returncode == 1
    assert "No answer from the modem within 5 s" in r.stderr
    assert "quit at 5000" in r.stdout


def test_scene_without_service_manager(daemon):
    d, start = daemon
    r = start("--scene", "1", env={"FAKE_SM": "none"}, check=False)
    assert r.returncode == 1


def test_daemon_same_scene_for_near_and_away_sends_nothing_new(daemon):
    d, start = daemon
    out = start(cfg="Slots=slot1\nNearScene=-1\nFarDelayMs=0\n", script=[
        "answer=slot1|7|0|0|0", "ev=5", "ev=0", "state"]).stdout
    assert at_sent(section(out, "ev=5")) == []
    assert at_sent(section(out, "ev=0")) == []


def test_daemon_near_again_while_away_is_unanswered(daemon):
    d, start = daemon
    out = start(cfg="Slots=slot1\nFarDelayMs=0\nRefreshSeconds=0\n", script=[
        "ev=5", "answer=slot1|7|0|0|0",     # near: scene 0 confirmed
        "ev=0",                              # away: -1 sent, no answer yet
        "ev=5",                              # near again
        "answer=slot1|7|0|0|0", "state"]).stdout
    sent = at_sent(out[:out.index("> loop end")])
    # The scene for a body near is the last one the modem is asked for
    assert sent[-1] == ("slot1", "AT+ERFIDX=1,0")
    assert at_sent(sections(out, "ev=5")[1]) == [("slot1", "AT+ERFIDX=1,0")]
    assert "slot slot1 connected=1 sent=0" in section(out, "state")


def test_daemon_away_again_while_near_is_unanswered(daemon):
    d, start = daemon
    out = start(cfg="Slots=slot1\nFarDelayMs=0\nRefreshSeconds=0\n", script=[
        "answer=slot1|7|0|0|0",              # normal power confirmed
        "ev=5",                              # near: 0 sent, no answer yet
        "ev=0",                              # away again
        "answer=slot1|7|0|0|0", "state"]).stdout
    assert at_sent(section(out, "ev=0")) == [("slot1", "AT+ERFIDX=1,-1")]
    assert "slot slot1 connected=1 sent=-1 requested=-1" in section(out, "state")


def test_daemon_answer_to_a_replaced_scene_is_not_taken_for_the_new_one(daemon):
    d, start = daemon
    out = start(cfg="Slots=slot1\nFarDelayMs=0\nRefreshSeconds=0\n", script=[
        "answer=slot1|7|0|0|0", "ev=5", "ev=0",
        "answer=slot1|7|0|-1|0", "state"]).stdout    # the late answer to "near"
    assert "slot slot1 connected=1 sent=-1 requested=-1 failures=0 resends=0 retry=0 answer=1" \
        in section(out, "state")


def test_daemon_exit_while_near_is_unanswered_sends_normal_power(daemon):
    d, start = daemon
    out = start(cfg="Slots=slot1\nFarDelayMs=0\n", script=[
        "answer=slot1|7|0|0|0", "ev=5", "term"]).stdout
    assert at_sent(out[out.index("quit at"):]) == [("slot1", "AT+ERFIDX=1,-1")]


def test_daemon_refresh_while_an_answer_is_due_sends_again(daemon):
    d, start = daemon
    out = start(cfg="Slots=slot1\n", script=["usr1"]).stdout
    assert at_sent(section(out, "usr1")) == [("slot1", "AT+ERFIDX=1,-1")]
