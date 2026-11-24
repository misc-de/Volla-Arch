# plinius-dictated: the microphone level for the key (RMS in dBFS onto 0..1), cutting the raw
# float stream from pw-record into VAD windows (also samples split between two reads), the
# utterance queue to the recognizer and its results, and what Stop finishes. sherpa-onnx is
# replaced by fakes/sherpa-onnx/c-api/c-api.h, D-Bus signals are printed.

import functools
import os
import math

import pytest

from dhw_harness import PKG, build_harness, kv, run, sandboxed_source

SRC = PKG / "plinius-dictate" / "plinius-dictated.c"

HARNESS = r"""
#define _GNU_SOURCE
#include <unistd.h>
#include <glib.h>
#include <gio/gio.h>
#include <gio/gunixinputstream.h>
#include <stdio.h>
#include <stdlib.h>
static gint64 fake_now;
#define g_get_monotonic_time() (fake_now)
static gboolean fake_emit_signal(GDBusConnection *c, const char *dest, const char *path, const char *iface,
				 const char *name, GVariant *params, GError **error)
{
	gchar *text = g_variant_print(g_variant_ref_sink(params), FALSE);
	printf("signal=%s %s%s\n", dest, name, text);
	g_free(text);
	g_variant_unref(params);
	return TRUE;
}
#define g_dbus_connection_emit_signal fake_emit_signal
#define main real_main
#include "plinius-dictated.c"
#undef main

static const char *fake_text_for(const float *samples, int32_t n)
{
	static __thread char buf[64];
	if (n == 7)
		return "   ";            /* nothing recognized */
	g_snprintf(buf, sizeof buf, "  words of %d samples, first %.0f ", n, samples[0]);
	return buf;
}

static Dictate d;
static gboolean read_done;

static void on_chunk(GObject *source, GAsyncResult *res, gpointer data)
{
	on_read(source, res, data);
	read_done = TRUE;
}

int main(int argc, char **argv)
{
	d.vad = create_vad();
	d.worker = g_thread_pool_new(run_job, NULL, 1, TRUE, NULL);
	d.client = g_strdup(":1.42");
	d.loaded = TRUE;
	float counter = 0;
	for (int i = 1; i < argc; i++) {
		g_auto(GStrv) a = g_strsplit(argv[i], ":", -1);
		if (!strcmp(a[0], "level")) {
			/* level:<amplitude>:<samples>:<time ms> */
			gsize n = atoi(a[2]);
			float *s = g_new(float, n);
			for (gsize k = 0; k < n; k++)
				s[k] = (k % 2 ? -1 : 1) * g_ascii_strtod(a[1], NULL);
			fake_now = g_ascii_strtoll(a[3], NULL, 10) * 1000;
			update_level(&d, s, n);
			g_free(s);
		} else if (!strcmp(a[0], "feed")) {
			gsize n = atoi(a[1]);
			float *s = g_new(float, n);
			for (gsize k = 0; k < n; k++)
				s[k] = counter++;
			feed_samples(&d, s, n);
			g_free(s);
			printf("window=%zu accepted=%ld last-first=%.0f\n", d.n_window, fake_vad_accepted, fake_vad_last[0]);
		} else if (!strcmp(a[0], "speech")) {
			fake_vad_speech = atoi(a[1]);
		} else if (!strcmp(a[0], "pipe")) {
			/* pipe:<floats>:<bytes per read>: the stream as pw-record writes it */
			int n = atoi(a[1]), chunk = atoi(a[2]), p[2];
			if (pipe(p) < 0)
				abort();
			for (int k = 0; k < n; k++) {
				float f = 1000 + k;
				if (write(p[1], &f, sizeof f) != sizeof f)
					abort();
			}
			close(p[1]);
			GInputStream *in = g_unix_input_stream_new(p[0], TRUE);
			for (int r = 0; r < (int)(n * sizeof(float) + chunk - 1) / chunk; r++) {
				read_done = FALSE;
				g_input_stream_read_async(in, d.readbuf, chunk, G_PRIORITY_DEFAULT, NULL, on_chunk, &d);
				while (!read_done)
					g_main_context_iteration(NULL, TRUE);
			}
			g_object_unref(in);
			printf("window=%zu partial=%zu accepted=%ld\n", d.n_window, d.n_partial, fake_vad_accepted);
			printf("samples=");
			for (gsize k = 0; k < d.n_window; k++)
				printf("%.0f,", d.window[k]);
			printf("\n");
		} else if (!strcmp(a[0], "segment")) {
			/* segment:<n>:<first value> */
			int n = atoi(a[1]);
			float *s = g_new(float, n);
			for (int k = 0; k < n; k++)
				s[k] = atoi(a[2]) + k;
			fake_vad_segments[fake_vad_n_segments++] = (SherpaOnnxSpeechSegment){s, n, 0};
		} else if (!strcmp(a[0], "drain")) {
			drain_vad(&d);
			printf("queued: pending=%u utterances=%u\n", d.pending, d.utterances);
		} else if (!strcmp(a[0], "new-session")) {
			d.session++;
		} else if (!strcmp(a[0], "wait")) {
			while (d.pending > 0)
				g_main_context_iteration(NULL, TRUE);
			printf("done: pending=%u utterances=%u client=%s activity=%s\n", d.pending, d.utterances,
			       d.client ? d.client : "(none)", d.activity ? d.activity : "");
		} else if (!strcmp(a[0], "stop")) {
			stop_session(&d);
			printf("stopped: window=%zu accepted=%ld flushed=%d reset=%d client=%s state=%s\n", d.n_window,
			       fake_vad_accepted, fake_vad_flushed, fake_vad_reset, d.client ? d.client : "(none)",
			       d.state ? d.state : "");
		} else if (!strcmp(a[0], "activity")) {
			/* activity:<recorder>:<speech>:<loaded>:<utterances> */
			d.recorder = atoi(a[1]) ? (GSubprocess *)&d : NULL;
			d.speech = atoi(a[2]);
			d.loaded = atoi(a[3]);
			d.utterances = atoi(a[4]);
			update_activity(&d);
			printf("activity=%s\n", d.activity);
			d.recorder = NULL;
		}
	}
	return 0;
}
"""


@pytest.fixture(scope="module")
def dictate(tmp_path_factory):
    d = tmp_path_factory.mktemp("dictate")
    sandboxed_source(SRC, d, [("/usr/share/plinius-dictate", d / "data")])
    exe = build_harness(d, "dictate", HARNESS, pkgs=("gio-unix-2.0",))
    return functools.partial(run, exe)


def levels(out):
    return [float(line.split("Level(")[1].rstrip(",)")) for line in out.splitlines() if "Level(" in line]


@pytest.mark.parametrize("amplitude,level", [
    (0.25, (20 * math.log10(0.25) + 60) / 48),   # -12 dBFS: nearly full
    (1.0, 1.0),                                   # clamped
    (10 ** (-30 / 20), 0.625),                    # -30 dBFS
    (0.001, 0.0),                                 # -60 dBFS: silence
    (0.0, 0.0),
])
def test_level_mapping(dictate, amplitude, level):
    out = dictate(f"level:{amplitude}:1600:1000").stdout
    assert levels(out) == pytest.approx([level], abs=1e-4)


def test_level_rate(dictate):
    # About 15 a second: samples in between are summed into the next value
    out = dictate("level:0.25:100:1000", "level:1:100:1030", "level:0.001:100:1067").stdout
    vals = levels(out)
    assert len(vals) == 2
    # The second covers the samples of the two later calls, and only them
    rms = math.sqrt((100 * 1.0 + 100 * 1e-6) / 200)
    assert vals[1] == pytest.approx(min(1.0, (20 * math.log10(rms) + 60) / 48), abs=1e-4)
    # Signals go to the caller only
    assert all(line.startswith("signal=:1.42 ") for line in out.splitlines() if "Level" in line)


def test_vad_windows(dictate):
    out = dictate("feed:1000", "feed:100", "feed:24").stdout
    w = [kv(line) for line in out.splitlines() if line.startswith("window=")]
    # 512-sample windows; the rest waits for the next samples
    assert [x["window"].split()[0] for x in w] == ["488", "76", "100"]
    assert out.splitlines()[-1].endswith("accepted=1024 last-first=512")


@pytest.mark.parametrize("chunk", [4, 7, 6, 13, 2048])
def test_samples_split_between_reads(dictate, chunk):
    out = dictate(f"pipe:40:{chunk}").stdout
    k = kv(out)
    # Every sample arrives whole and in order, whatever the read boundaries
    assert k["window"] == "40 partial=0 accepted=0"
    assert k["samples"] == ",".join(str(1000 + i) for i in range(40)) + ","


def test_utterances_recognized_in_order(dictate):
    out = dictate("segment:8000:1", "segment:7:0", "segment:4000:9", "drain", "wait").stdout
    assert "queued: pending=3 utterances=3" in out
    texts = [line for line in out.splitlines() if "Text(" in line]
    # Stripped; an empty result is not sent
    assert texts == ["signal=:1.42 Text('words of 8000 samples, first 1',)",
                     "signal=:1.42 Text('words of 4000 samples, first 9',)"]
    assert "done: pending=0 utterances=0" in out


def test_results_of_an_old_session_are_dropped(dictate):
    out = dictate("segment:8000:1", "drain", "new-session", "wait").stdout
    assert "Text(" not in out


def test_stop_finishes_the_last_utterance(dictate):
    out = dictate("feed:600", "stop", "wait").stdout
    # The 88 samples of the open window still go to the VAD, then flush and reset
    assert "stopped: window=0 accepted=600 flushed=1 reset=1 client=(none) state=idle" in out
    assert "StateChanged('idle',)" in out


def test_stop_keeps_the_client_until_results_are_delivered(dictate):
    out = dictate("segment:8000:1", "stop", "wait").stdout
    assert "stopped: window=0 accepted=0 flushed=1 reset=1 client=:1.42" in out
    assert "signal=:1.42 Text('words of 8000 samples, first 1',)" in out
    assert "client=(none) activity=idle" in out


@pytest.mark.parametrize("state,activity", [
    ("0:0:1:0", "idle"),
    ("1:1:0:0", "speech"),          # speech first, even while loading
    ("1:0:0:0", "loading"),
    ("1:0:1:2", "recognizing"),
    ("0:0:1:1", "recognizing"),     # stopped, last utterances still queued
    ("1:0:1:0", "listening"),
])
def test_activity(dictate, state, activity):
    assert kv(dictate(f"activity:{state}").stdout)["activity"] == activity


# ---- The service as it runs: main(), Start/Stop over a scripted bus, pw-record, the settings ----
#
# fakes/dhw_scripted_gdbus.h is the session bus; the main loop runs the script after the
# arguments. pw-record is a stub on PATH that logs its arguments, writes float samples and waits
# for SIGTERM (or ends at once); the settings come from the package's schema, compiled into tmp,
# with the keyfile backend in tmp.

SERVICE = r"""
#define G_DISABLE_CAST_CHECKS
#include <glib.h>
#include <gio/gio.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sherpa-onnx/c-api/c-api.h>
#include "dhw_scripted_gdbus.h"

static gint64 fake_now;
#define g_get_monotonic_time() (fake_now)

static int recognizer_fails, vad_fails, read_error;
static gssize bytes_read;
static const SherpaOnnxOfflineRecognizer *wrap_create_recognizer(const SherpaOnnxOfflineRecognizerConfig *c)
{
	return recognizer_fails ? NULL : SherpaOnnxCreateOfflineRecognizer(c);
}
static const SherpaOnnxVoiceActivityDetector *wrap_create_vad(const SherpaOnnxVadModelConfig *c, float s)
{
	return vad_fails ? NULL : SherpaOnnxCreateVoiceActivityDetector(c, s);
}
#define SherpaOnnxCreateOfflineRecognizer wrap_create_recognizer
#define SherpaOnnxCreateVoiceActivityDetector wrap_create_vad

/* A read of the microphone pipe that fails (not cancelled) */
static gssize wrap_read_finish(GInputStream *s, GAsyncResult *res, GError **error)
{
	gssize n = g_input_stream_read_finish(s, res, error);
	if (n > 0)
		bytes_read += n;
	if (read_error && n > 0) {
		g_set_error_literal(error, G_IO_ERROR, G_IO_ERROR_BROKEN_PIPE, "broken pipe");
		return -1;
	}
	return n;
}
#define g_input_stream_read_finish wrap_read_finish

static const char *fake_text_for(const float *samples, int32_t n)
{
	static __thread char buf[64];
	g_snprintf(buf, sizeof buf, " heard %d samples ", n);
	return buf;
}

/* GSubprocess sends a signal later, from GLib's worker thread. The real daemon lives on after
 * Stop(), this harness ends right after its script: it waits for the programs signalled during
 * the script before it ends. Not for those the daemon signals on its own way out (in_tail),
 * which it has to wait for itself. */
static GPtrArray *signalled;
static gboolean in_tail;
static void wrap_send_signal(GSubprocess *p, gint sig)
{
	g_subprocess_send_signal(p, sig);
	if (in_tail)
		return;
	if (!signalled)
		signalled = g_ptr_array_new_with_free_func(g_object_unref);
	g_ptr_array_add(signalled, g_object_ref(p));
}
#define g_subprocess_send_signal wrap_send_signal

static void script_loop(GMainLoop *loop);
#define g_main_loop_run script_loop

#define main real_main
#include "plinius-dictated.c"
#undef main
#undef g_main_loop_run
#undef g_input_stream_read_finish
#undef g_subprocess_send_signal

static char **script;
static Dictate *D;

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

static void print_state(void)
{
	if (!D) {
		printf("state none\n");
		return;
	}
	printf("state client=%s recording=%d pending=%u utterances=%u exit=%d state=%s activity=%s session=%u "
	       "watches=%d settings=%d\n", D->client ? D->client : "-", D->recorder != NULL, D->pending,
	       D->utterances, D->exit_timeout != 0, D->state ? D->state : "-", D->activity ? D->activity : "-",
	       D->session, sg_active_watches(), D->settings != NULL);
}

static gboolean lose_name(gpointer data)
{
	sg_lose_name();
	return G_SOURCE_REMOVE;
}

#define PATH "/org/plinius/Dictate1"
#define IFACE_ "org.plinius.Dictate1"

static void command(const char *a)
{
	g_auto(GStrv) w = g_strsplit(a, ":", 2);
	if (!strcmp(w[0], "Start") || !strcmp(w[0], "Stop") || !strcmp(w[0], "Bogus")) {
		sg_invoke(PATH, IFACE_, w[1], w[0], NULL);
	} else if (!strcmp(w[0], "get")) {
		sg_get(PATH, IFACE_, w[1]);
	} else if (!strcmp(w[0], "run")) {
		run_for(atoi(w[1]));
	} else if (!strcmp(w[0], "wait-read") || !strcmp(w[0], "wait-jobs") || !strcmp(w[0], "wait-ended")) {
		/* Until pw-record's samples are read, the worker is done, or the recording ended (at most 10 s) */
		gint64 end = (g_get_monotonic_time)() + 10 * G_TIME_SPAN_SECOND;
		while ((g_get_monotonic_time)() < end &&
		       (w[0][5] == 'r' ? bytes_read < atoi(w[1]) * 4 : w[0][5] == 'j' ? D->pending > 0 : D->recorder != NULL))
			g_main_context_iteration(NULL, TRUE);
	} else if (!strcmp(w[0], "now")) {
		fake_now = g_ascii_strtoll(w[1], NULL, 10) * 1000;
	} else if (!strcmp(w[0], "speech")) {
		fake_vad_speech = atoi(w[1]);
	} else if (!strcmp(w[0], "segment")) {
		int n = atoi(w[1]);
		float *s = g_new0(float, n);
		fake_vad_segments[fake_vad_n_segments++] = (SherpaOnnxSpeechSegment){s, n, 0};
	} else if (!strcmp(w[0], "vanish")) {
		sg_vanish(w[1]);
	} else if (!strcmp(w[0], "state")) {
		print_state();
	} else if (!strcmp(w[0], "recognizer-fails")) {
		recognizer_fails = 1;
	} else if (!strcmp(w[0], "vad-fails")) {
		vad_fails = 1;
	} else if (!strcmp(w[0], "emit-fails")) {
		sg.emit_fails = TRUE;
	} else if (!strcmp(w[0], "register-fails")) {
		sg.register_fails = TRUE;
	} else if (!strcmp(w[0], "no-name")) {
		sg.no_bus = TRUE;
	} else if (!strcmp(w[0], "lose-name")) {
		g_idle_add(lose_name, NULL);
	} else if (!strcmp(w[0], "read-error")) {
		read_error = 1;
	} else if (!strcmp(w[0], "wait-exit")) {
		/* The real loop until the daemon quits it (exit timer, signal) */
		g_main_loop_run(D->loop);
		printf("loop quit\n");
	} else if (!strcmp(w[0], "sigterm")) {
		raise(SIGTERM);
	} else {
		g_error("unknown command %s", a);
	}
	fflush(stdout);
}

static gboolean started;
static gboolean startup_done(gpointer loop)
{
	started = TRUE;
	g_main_loop_quit(loop);
	return G_SOURCE_REMOVE;
}

static void script_loop(GMainLoop *loop)
{
	/* The start as in the real loop: bus acquired (or the name lost), then the script; a daemon
	 * that quits its loop meanwhile ends without it */
	g_idle_add_full(G_PRIORITY_LOW, startup_done, loop, NULL);
	g_main_loop_run(loop);
	if (!started) {
		printf("quit at start\n");
		return;
	}
	D = sg.objects->len ? ((SgObject *)sg.objects->pdata[0])->data : NULL;
	print_state();
	for (; script && *script; script++)
		command(*script);
	in_tail = TRUE;
}

int main(int argc, char **argv)
{
	/* pw-record's stand-in ends with this process (it inherits the environment) */
	g_autofree char *pid = g_strdup_printf("%d", (int)getpid());

	g_setenv("FAKE_PW_PARENT", pid, TRUE);
	sg_init();
	script = argv + 1;
	while (*script && strcmp(*script, "start")) {
		command(*script);
		script++;
	}
	if (*script)
		script++;
	int r = real_main(1, argv);
	for (guint i = 0; signalled && i < signalled->len; i++)
		g_subprocess_wait(signalled->pdata[i], NULL, NULL);
	printf("exit %d\n", r);
	return r;
}
"""

PW_RECORD = """#!{python}
import os, select, signal, struct, sys
log = os.environ["FAKE_PW_LOG"]
with open(log, "a") as f:
    f.write("start %d " % os.getpid() + " ".join(sys.argv[1:]) + "\\n")
# A SIGTERM is noted in a pipe (the handler only makes Python's C handler write the wakeup fd),
# so it counts even when it comes together with the end of the daemon's pipe
wake_r, wake_w = os.pipe()
os.set_blocking(wake_r, False)
os.set_blocking(wake_w, False)
signal.signal(signal.SIGTERM, lambda *_: None)
signal.set_wakeup_fd(wake_w)
def leave():
    try:
        got = os.read(wake_r, 64)
    except BlockingIOError:
        got = b""
    if bytes([signal.SIGTERM]) in got:
        with open(log, "a") as f:
            f.write("terminated %d\\n" % os.getpid())
    sys.exit(0)
n = int(os.environ.get("FAKE_PW_SAMPLES", "0"))
sys.stdout.buffer.write(struct.pack("<%df" % n, *[0.25 if i % 2 else -0.25 for i in range(n)]))
sys.stdout.flush()
if os.environ.get("FAKE_PW_MODE") == "exit":
    leave()
# Never outlives the daemon (it would keep the test's output pipes open): waits for a SIGTERM
# or the daemon's end. The daemon's pid comes from the harness, not from os.getppid(): under
# load the daemon can be gone before this has started. A daemon that ended stays a zombie as
# long as this holds the test's pipes, so its pid is still its own. It signals before it ends:
# the SIGTERM is in the wakeup pipe by the time its end is seen.
daemon = os.pidfd_open(int(os.environ["FAKE_PW_PARENT"]))
p = select.poll()
p.register(daemon, select.POLLIN)
p.register(wake_r, select.POLLIN)
p.poll()
leave()
"""


@pytest.fixture(scope="module")
def service_built(tmp_path_factory):
    import shutil
    import subprocess
    import sys
    d = tmp_path_factory.mktemp("dictate-service")
    sandboxed_source(SRC, d, [("/usr/share/plinius-dictate", d / "data")])
    exe = build_harness(d, "dictate_service", SERVICE, pkgs=("gio-unix-2.0",))
    if shutil.which("glib-compile-schemas") is None:
        pytest.skip("no glib-compile-schemas")
    (d / "schemas").mkdir()
    shutil.copy(PKG / "plinius-dictate" / "org.plinius.dictate.gschema.xml", d / "schemas")
    subprocess.run(["glib-compile-schemas", str(d / "schemas")], check=True)
    (d / "no-schemas").mkdir()
    (d / "bin").mkdir()
    (d / "bin/pw-record").write_text(PW_RECORD.format(python=sys.executable))
    (d / "bin/pw-record").chmod(0o755)
    return d, exe


@pytest.fixture
def service(service_built):
    import shutil
    d, exe = service_built
    for sub in ("data", "config"):
        shutil.rmtree(d / sub, ignore_errors=True)
    (d / "data/parakeet-tdt-0.6b-v3-int8").mkdir(parents=True)
    (d / "data/parakeet-tdt-0.6b-v3-int8/encoder.int8.onnx").write_text("")
    (d / "data/silero_vad_v5.onnx").write_text("")
    (d / "config/glib-2.0/settings").mkdir(parents=True)
    (d / "pw.log").write_text("")

    def binary(*args, settings="enabled=true", schema=True, samples=0, mode="stay", path=True, **kw):
        (d / "config/glib-2.0/settings/keyfile").write_text(f"[org/plinius/dictate]\n{settings}\n")
        env = dict(os.environ, GSETTINGS_SCHEMA_DIR=str(d / ("schemas" if schema else "no-schemas")),
                   XDG_DATA_DIRS=str(d / "no-schemas"), GSETTINGS_BACKEND="keyfile",
                   XDG_CONFIG_HOME=str(d / "config"), FAKE_PW_LOG=str(d / "pw.log"),
                   FAKE_PW_SAMPLES=str(samples), FAKE_PW_MODE=mode,
                   PATH=str(d / "bin") if path else str(d / "no-schemas"))
        return run(exe, *args, env=env, **kw)
    return d, binary


def states(out):
    return [dict(x.split("=", 1) for x in line.split()[1:]) for line in out.splitlines()
            if line.startswith("state ") and line != "state none"]


def signals(out, name):
    return [line.split(name, 1)[1] for line in out.splitlines()
            if line.startswith("signal ") and f" {name}(" in line]


def test_service_start_records_and_stop_ends(service):
    d, binary = service
    r = binary("start", "Start::1.42", "state", "wait-read:100", "wait-jobs", "Stop::1.99", "state", "Stop::1.42",
               "state", samples=100)
    out = r.stdout
    assert "own-name session org.plinius.Dictate1" in out
    assert "register /org/plinius/Dictate1 org.plinius.Dictate1" in out
    assert "reply Start ok ()" in out
    # pw-record: 16 kHz mono float32 to stdout, as voice input
    log = [line.split(" ", 2)[2] if line.startswith("start ") else line.split()[0]
           for line in (d / "pw.log").read_text().splitlines()]
    assert log[0].startswith("--raw --rate 16000 --channels 1 --format f32 --media-role Communication "
                             "--latency 64ms -P")
    assert log[0].endswith(" -")
    started, other_stop, stopped = states(out)[1:]
    assert started["client"] == ":1.42" and started["recording"] == "1" and started["state"] == "listening"
    assert started["watches"] == "1" and started["exit"] == "0" and started["settings"] == "1"
    # Only the caller of Start() may stop it
    assert other_stop["recording"] == "1"
    assert stopped["recording"] == "0" and stopped["client"] == "-" and stopped["state"] == "idle"
    assert stopped["watches"] == "0" and stopped["exit"] == "1"
    # The microphone is closed
    assert log[-1] == "terminated"
    # Signals only to the caller: listening (the model was loaded meanwhile), then idle
    assert signals(out, "StateChanged") == ["('listening',)", "('idle',)"]
    assert all(line.startswith("signal :1.42 ") for line in out.splitlines() if line.startswith("signal "))
    assert signals(out, "Activity")[0] == "('loading',)"
    assert signals(out, "Activity")[-1] == "('idle',)"
    # The 100 samples reached the VAD's window
    assert "Recognizer loaded" in r.stderr


def test_service_utterance_is_recognized_and_sent(service):
    d, binary = service
    r = binary("start", "segment:8000", "now:1000", "Start::1.42", "wait-read:600", "wait-jobs", "state", "Stop::1.42",
               "wait-jobs", "state", samples=600)
    out = r.stdout
    # The segment comes out of the VAD with the first samples, the worker recognizes it
    assert signals(out, "Text") == ["('heard 8000 samples',)"]
    # The level of the samples (-12 dBFS: 0.25 amplitude) for the key
    level = float(signals(out, "Level")[0].strip("(,)"))
    assert level == pytest.approx((20 * math.log10(0.25) + 60) / 48, abs=1e-4)
    final = states(out)[-1]
    assert final["pending"] == "0" and final["client"] == "-"


def test_service_disabled_in_the_settings(service):
    d, binary = service
    r = binary("start", "Start::1.42", "state", settings="enabled=false")
    assert "reply Start error org.plinius.Dictate1.Error.Disabled: Dictation is switched off in the settings" \
        in r.stdout
    assert states(r.stdout)[-1]["recording"] == "0"
    assert (d / "pw.log").read_text() == ""


def test_service_without_models(service):
    d, binary = service
    (d / "data/silero_vad_v5.onnx").unlink()
    r = binary("start", "Start::1.42", "state")
    assert "reply Start error org.plinius.Dictate1.Error.NoModel: The speech model is not installed" in r.stdout
    (d / "data/silero_vad_v5.onnx").write_text("")
    (d / "data/parakeet-tdt-0.6b-v3-int8/encoder.int8.onnx").unlink()
    r = binary("start", "Start::1.42")
    assert "Error.NoModel" in r.stdout


def test_service_without_pw_record(service):
    d, binary = service
    r = binary("start", "Start::1.42", "state", path=False)
    assert "reply Start error org.plinius.Dictate1.Error.Microphone: " in r.stdout
    s = states(r.stdout)[-1]
    # Nothing left of the session; the exit timer runs again
    assert s["client"] == "-" and s["watches"] == "0" and s["exit"] == "1"


def test_service_second_caller_takes_over(service):
    d, binary = service
    r = binary("start", "Start::1.42", "wait-read:10", "Start::1.43", "state", "wait-read:20", samples=10)
    s = states(r.stdout)[-1]
    assert s["client"] == ":1.43" and s["recording"] == "1" and s["session"] == "2"
    log = [line.split()[:2] for line in (d / "pw.log").read_text().splitlines()]
    started = [pid for word, pid in log if word == "start"]
    # Two recordings, and the first one got SIGTERM (at exit only the current one is ended)
    assert len(started) == 2
    assert sorted(pid for word, pid in log if word == "terminated") == sorted(started)
    # The keyboard learns the state from scratch: the new caller gets listening
    assert "signal :1.43 /org/plinius/Dictate1 org.plinius.Dictate1 StateChanged('listening',)" in r.stdout


def test_service_client_vanishes(service):
    d, binary = service
    r = binary("start", "segment:4000", "Start::1.42", "vanish::1.42", "state", "wait-jobs", "state", samples=600)
    after, later = states(r.stdout)[-2:]
    assert after["client"] == "-" and after["recording"] == "0" and after["watches"] == "0"
    assert "Client :1.42 left, stopping" in r.stderr
    # Its utterance is not delivered to anyone
    assert signals(r.stdout, "Text") == []
    assert later["pending"] == "0" and later["exit"] == "1"


def test_service_recording_ends_by_itself(service):
    d, binary = service
    r = binary("start", "Start::1.42", "wait-ended", "state", samples=10, mode="exit")
    s = states(r.stdout)[-1]
    # pw-record ended without Stop(): an error for the keyboard, the session ends
    assert "Recording ended unexpectedly" in r.stderr
    assert s["state"] == "error" and s["client"] == "-" and s["recording"] == "0"
    assert signals(r.stdout, "StateChanged") == ["('listening',)", "('error',)"]


def test_service_stops_after_long_silence(service):
    d, binary = service
    r = binary("start", "now:0", "Start::1.42", "run:50", "now:5000", "run:1100", "state", "speech:1",
               "now:20000", "run:1100", "state", "speech:0", "now:31000", "run:1100", "state")
    a, b, c = states(r.stdout)[-3:]
    # 5 s: still listening; silent for long but the VAD hears speech right now: still on
    assert a["recording"] == "1" and b["recording"] == "1"
    # More than 10 s since the last voice: stopped by itself
    assert c["recording"] == "0" and c["client"] == "-"
    assert "No speech for 10 s, stopping" in r.stderr


def test_service_speech_keeps_it_listening(service):
    d, binary = service
    r = binary("start", "now:0", "Start::1.42", "now:8000", "speech:1", "wait-read:600", "speech:0", "now:15000",
               "run:1100", "state", samples=600)
    # Speech heard at 8 s: at 15 s that is 7 s ago, not the 15 s since Start()
    assert states(r.stdout)[-1]["recording"] == "1"
    assert "Activity('speech',)" in r.stdout


def test_service_signal_that_cannot_be_sent(service):
    d, binary = service
    r = binary("start", "emit-fails", "Start::1.42", "state")
    # Logged, the session goes on
    assert "Emitting StateChanged failed: fake bus: connection closed" in r.stderr
    assert "reply Start ok ()" in r.stdout and states(r.stdout)[-1]["recording"] == "1"


def test_service_read_error(service):
    d, binary = service
    r = binary("start", "read-error", "Start::1.42", "wait-read:50", "state", samples=50)
    assert "Reading the microphone failed: broken pipe" in r.stderr
    # The session stays (the silence check ends it later)
    assert states(r.stdout)[-1]["recording"] == "1"


def test_service_recognizer_fails_to_load(service):
    d, binary = service
    r = binary("start", "recognizer-fails", "segment:8000", "Start::1.42", "wait-read:600", "Stop::1.42", "wait-jobs",
               "state", samples=600)
    assert "Loading the recognizer from" in r.stderr and "failed" in r.stderr
    # Nothing recognized, nothing sent; the session still ends cleanly
    assert signals(r.stdout, "Text") == []
    assert states(r.stdout)[-1]["pending"] == "0"


def test_service_properties_and_unknown_method(service):
    d, binary = service
    r = binary("start", "get:State", "get:Other", "Bogus::1.42", "Start::1.42", "get:State")
    out = r.stdout
    assert "get State='idle'" in out and "get State='listening'" in out
    assert "get Other error (none)" in out
    assert "reply Bogus error org.freedesktop.DBus.Error.UnknownMethod: Unknown method Bogus" in out


def test_service_exits_when_unused(service):
    d, binary = service
    # Activated but never called: gone after keep-model seconds
    r = binary("start", "state", "wait-exit", settings="keep-model=1")
    assert "No dictation for 1 s, exiting to free the model" in r.stderr
    assert "loop quit" in r.stdout and r.stdout.rstrip().endswith("exit 0")
    assert "unown-name 7" in r.stdout


def test_service_without_schema_uses_defaults(service):
    d, binary = service
    r = binary("start", "Start::1.42", "state", schema=False)
    assert "Settings schema org.plinius.dictate missing, using defaults" in r.stderr
    # No setting to switch it off: allowed
    s = states(r.stdout)[-1]
    assert s["settings"] == "0" and s["recording"] == "1"


def test_service_terminated_with_recording_open(service):
    d, binary = service
    # The first sample read: pw-record has its SIGTERM handler (it writes after setting it)
    r = binary("start", "Start::1.42", "wait-read:1", "sigterm", "wait-exit", samples=1)
    assert "loop quit" in r.stdout
    # The microphone is closed on the way out: the daemon waits until pw-record has ended
    # (GSubprocess sends the signal from its worker thread, which an exit right after can beat)
    assert (d / "pw.log").read_text().splitlines()[-1].startswith("terminated ")
    assert r.stdout.rstrip().endswith("exit 0")


def test_service_without_vad(service):
    d, binary = service
    r = binary("vad-fails", "start", check=False)
    assert r.returncode == 1
    assert "Creating the voice activity detector from" in r.stderr


def test_service_name_problems(service):
    d, binary = service
    # Each ends the service at once
    r = binary("no-name", "start", "state")
    assert "Lost or could not get the bus name org.plinius.Dictate1" in r.stderr
    assert "quit at start" in r.stdout and r.stdout.rstrip().endswith("exit 0")
    r = binary("register-fails", "start", "state")
    assert "Registering the object failed: fake bus: object exists" in r.stderr
    assert "quit at start" in r.stdout
    r = binary("start", "lose-name", "wait-exit")
    assert "Lost or could not get the bus name org.plinius.Dictate1" in r.stderr
    assert "loop quit" in r.stdout
