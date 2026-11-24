# vibrator-ff-bridge: how feedbackd's force-feedback effects become vibrations: strength from
# magnitude and gain, the minimum tap of the vendor driver, the LED-class and HAL backends, and
# the uinput upload/erase handshake, finding the vendor device, and the daemon. ioctl() is faked,
# the devices are pipes, FIFOs or files, sysfs is tmp; binder is fakes/gbinder.h.

import functools
import os
import shutil
import struct

import pytest

from dhw_daemon_bus import Daemon, poll, sanitized_env
from dhw_harness import PKG, build_harness, kv, run, sandboxed_source

SRC = PKG / "vibrator-ff-bridge" / "vibrator-ff-bridge.c"

HARNESS = r"""
#define _GNU_SOURCE
#include <unistd.h>
#include <glib.h>
#include <stdio.h>
#include <stdlib.h>
#include <fcntl.h>
#include <errno.h>
#include <sys/ioctl.h>
#include <linux/uinput.h>
static gint64 fake_now;
#define g_get_monotonic_time() (fake_now)

/* open: which file each fd is, so EVIOCGNAME can answer from <dir>-names/<entry> */
static char *fd_paths[1024];
static int fake_open(const char *path, int flags, ...)
{
	int fd = open(path, flags, 0600);
	if (fd >= 0 && fd < 1024) {
		g_free(fd_paths[fd]);
		fd_paths[fd] = g_strdup(path);
	}
	return fd;
}

/* ioctl: the vendor FF device and our uinput device, as far as the daemon uses them */
static struct ff_effect upload_effect;   /* what UI_BEGIN_FF_UPLOAD hands over */
static int upload_retval = 1;
/* Scripted failures: EVIOCSFF, the begin of an upload or erase, the n-th uinput set-up call */
static int sff_fails, begin_fails, uinput_fail_at, uinput_calls;
static int fake_ioctl(int fd, unsigned long req, ...)
{
	va_list ap;
	va_start(ap, req);
	void *arg = va_arg(ap, void *);
	va_end(ap);
	if ((req & ~((unsigned long)_IOC_SIZEMASK << _IOC_SIZESHIFT)) == EVIOCGNAME(0)) {
		g_autofree char *dir = fd >= 0 && fd < 1024 && fd_paths[fd] ? g_path_get_dirname(fd_paths[fd]) : NULL;
		g_autofree char *base = dir ? g_path_get_basename(fd_paths[fd]) : NULL;
		g_autofree char *file = dir ? g_strdup_printf("%s-names/%s", dir, base) : NULL;
		g_autofree char *name = NULL;
		if (!file || !g_file_get_contents(file, &name, NULL, NULL)) {
			errno = ENOTTY;
			return -1;
		}
		g_strlcpy(arg, g_strstrip(name), _IOC_SIZE(req));
		return strlen(arg);
	}
	if (req == UI_SET_EVBIT || req == UI_SET_FFBIT || req == UI_DEV_SETUP || req == UI_DEV_CREATE ||
	    req == UI_DEV_DESTROY) {
		const char *op = req == UI_SET_EVBIT ? "evbit" : req == UI_SET_FFBIT ? "ffbit"
			       : req == UI_DEV_SETUP ? "setup" : req == UI_DEV_CREATE ? "create" : "destroy";
		if (req == UI_DEV_SETUP) {
			const struct uinput_setup *us = arg;
			printf("uinput setup name=%s bus=%u effects=%u\n", us->name, us->id.bustype, us->ff_effects_max);
		} else if (req == UI_SET_EVBIT || req == UI_SET_FFBIT) {
			printf("uinput %s %ld\n", op, (long)(intptr_t)arg);
		} else {
			printf("uinput %s\n", op);
		}
		if (++uinput_calls == uinput_fail_at) {
			errno = EINVAL;
			return -1;
		}
		return 0;
	}
	if (req == EVIOCSFF) {
		struct ff_effect *e = arg;
		printf("upload type=%u length=%u id=%d\n", e->type, e->replay.length, e->id);
		if (sff_fails) {
			errno = ENOSPC;
			return -1;
		}
		if (e->id < 0)
			e->id = 3;
		return 0;
	}
	if (begin_fails && (req == UI_BEGIN_FF_UPLOAD || req == UI_BEGIN_FF_ERASE)) {
		errno = EINVAL;
		return -1;
	}
	if (req == UI_BEGIN_FF_UPLOAD) {
		((struct uinput_ff_upload *)arg)->effect = upload_effect;
		return 0;
	}
	if (req == UI_END_FF_UPLOAD) {
		upload_retval = ((struct uinput_ff_upload *)arg)->retval;
		return 0;
	}
	if (req == UI_BEGIN_FF_ERASE) {
		((struct uinput_ff_erase *)arg)->effect_id = 2;
		return 0;
	}
	if (req == UI_END_FF_ERASE)
		return 0;
	errno = ENOTTY;
	return -1;
}
#define ioctl fake_ioctl
#define open fake_open

/* No service manager (FAKE_NO_SM): the binder device is not there */
#include <gbinder.h>
static gboolean no_servicemanager;
static GBinderServiceManager *servicemanager_or_none(const char *dev)
{
	return no_servicemanager ? NULL : gbinder_servicemanager_new(dev);
}
#define gbinder_servicemanager_new servicemanager_or_none

#define main real_main
#include "vibrator-ff-bridge.c"
#undef main
#undef open

static int kernel_rd = -1;

static void use_kernel_ff(void)
{
	int p[2];
	if (pipe2(p, O_NONBLOCK) < 0)
		abort();
	kernel_rd = p[0];
	bridge.kfd = p[1];
}

/* Events written to the vendor FF device */
static void dump_kernel(void)
{
	struct input_event ev;
	while (read(kernel_rd, &ev, sizeof ev) == sizeof ev)
		printf("event type=%u code=%u value=%d\n", ev.type, ev.code, ev.value);
}

static void dump_sysfs(void)
{
	g_autofree char *duration = NULL, *activate = NULL;
	g_file_get_contents(SYSFS_DIR "/duration", &duration, NULL, NULL);
	g_file_get_contents(SYSFS_DIR "/activate", &activate, NULL, NULL);
	printf("sysfs=%s %s\n", duration ? duration : "-", activate ? activate : "-");
}

static struct ff_effect rumble(int id, int strong, int weak, int length, int delay)
{
	struct ff_effect e = {.type = FF_RUMBLE, .id = id};
	e.u.rumble.strong_magnitude = strong;
	e.u.rumble.weak_magnitude = weak;
	e.replay.length = length;
	e.replay.delay = delay;
	return e;
}

int main(int argc, char **argv)
{
	/* daemon: the bridge itself; FAKE_UPLOAD=<id>,<strong>,<length> is what feedbackd uploads */
	if (argc > 1 && !strcmp(argv[1], "daemon")) {
		setvbuf(stdout, NULL, _IOLBF, 0);
		if (getenv("FAKE_UPLOAD")) {
			int id, strong, length;
			sscanf(getenv("FAKE_UPLOAD"), "%d,%d,%d", &id, &strong, &length);
			upload_effect = rumble(id, strong, 0, length, 0);
		}
		return real_main(argc - 1, argv + 1);
	}
	for (int i = 1; i < argc; i++) {
		g_auto(GStrv) p = g_strsplit(argv[i], ":", -1);
		const char *c = p[0];
		if (!strcmp(c, "amp-rumble")) {
			struct ff_effect e = rumble(0, atoi(p[1]), atoi(p[2]), 0, 0);
			bridge.gain = atoi(p[3]);
			printf("amp=%.4f\n", effect_amplitude(&e));
		} else if (!strcmp(c, "amp-periodic")) {
			struct ff_effect e = {.type = FF_PERIODIC};
			e.u.periodic.magnitude = atoi(p[1]);
			bridge.gain = atoi(p[2]);
			printf("amp=%.4f\n", effect_amplitude(&e));
		} else if (!strcmp(c, "kernel")) {
			use_kernel_ff();
		} else if (!strcmp(c, "sysfs")) {
			bridge.sysfs = TRUE;
		} else if (!strcmp(c, "hal")) {
			/* HAL reply: no exception, capabilities p[1] */
			static FakeGbValue v[2];
			v[0] = (FakeGbValue){'i', 0};
			v[1] = (FakeGbValue){'i', atoi(p[1])};
			fake_gb_reply_vals = v;
			fake_gb_reply_n = 2;
		} else if (!strcmp(c, "now")) {
			fake_now = g_ascii_strtoll(p[1], NULL, 10) * 1000;
		} else if (!strcmp(c, "on")) {
			vibrate_on(atoi(p[1]), g_ascii_strtod(p[2], NULL));
			if (kernel_rd >= 0)
				dump_kernel();
			if (bridge.sysfs)
				dump_sysfs();
		} else if (!strcmp(c, "off")) {
			vibrate_off();
			if (kernel_rd >= 0)
				dump_kernel();
			if (bridge.sysfs)
				dump_sysfs();
			printf("off done\n");
		} else if (!strcmp(c, "upload")) {
			/* upload:<id>:<type: r rumble, c constant>:<strong>:<length>:<delay> */
			upload_effect = rumble(atoi(p[1]), atoi(p[3]), 0, atoi(p[4]), atoi(p[5]));
			if (p[2][0] == 'c')
				upload_effect.type = FF_CONSTANT;
			handle_upload(1);
			printf("upload %s=%d\n", p[1], upload_retval);
		} else if (!strcmp(c, "erase")) {
			handle_erase(1);
			printf("used2=%d\n", bridge.used[2]);
		} else if (!strcmp(c, "play")) {
			play(atoi(p[1]), atoi(p[2]));
			/* A delayed effect starts from the main loop */
			for (int t = 0; t < 50 && bridge.delay_id; t++) {
				g_main_context_iteration(NULL, FALSE);
				g_usleep(2000);
			}
			if (kernel_rd >= 0)
				dump_kernel();
			if (bridge.sysfs)
				dump_sysfs();
			printf("played %s\n", p[1]);
		} else if (!strcmp(c, "fail")) {
			/* fail:<sff|begin|binder|no-service|no-sm> */
			if (!strcmp(p[1], "sff"))
				sff_fails = 1;
			else if (!strcmp(p[1], "begin"))
				begin_fails = 1;
			else if (!strcmp(p[1], "binder"))
				fake_gb_status = -1;
			else if (!strcmp(p[1], "no-service"))
				fake_gb_no_service = TRUE;
			else if (!strcmp(p[1], "no-sm"))
				no_servicemanager = TRUE;
		} else if (!strcmp(c, "hal-exception")) {
			static FakeGbValue v[1] = {{'i', -3}};
			fake_gb_reply_vals = v;
			fake_gb_reply_n = 1;
		} else if (!strcmp(c, "kernel-broken")) {
			/* A vendor device that takes no more events (its read end) */
			int q[2];
			if (pipe(q) < 0)
				abort();
			bridge.kfd = q[0];
		} else if (!strcmp(c, "hal-died")) {
			on_hal_died(NULL, NULL);
			printf("hal client=%d remote=%d death=%lu\n", bridge.client != NULL, bridge.remote != NULL,
			       bridge.death_id);
		} else if (!strcmp(c, "find")) {
			int fd = kernel_ff_open();
			printf("found=%s\n", fd >= 0 ? fd_paths[fd] + strlen(BASE) : "none");
		} else if (!strcmp(c, "retry")) {
			/* The minute of looking for the vendor device: how many tries until it stops */
			int n = 1;
			while (kernel_ff_retry(NULL) == G_SOURCE_CONTINUE)
				n++;
			printf("retry tries=%d kernel=%s sysfs=%d client=%d\n", n,
			       bridge.kfd >= 0 ? fd_paths[bridge.kfd] + strlen(BASE) : "none", bridge.sysfs,
			       bridge.client != NULL);
			dump_sysfs();
		} else if (!strcmp(c, "create")) {
			/* create:<n>: the n-th set-up call fails (0: none) */
			uinput_fail_at = atoi(p[1]);
			printf("created=%d\n", uinput_create());
		} else if (!strcmp(c, "feed")) {
			/* feed:<type>,<code>,<value>:...: events from feedbackd, then the uinput device's
			 * state (or hup: the device failed) */
			int q[2];
			if (pipe2(q, O_NONBLOCK) < 0)
				abort();
			gboolean hup = FALSE;
			for (int k = 1; p[k]; k++) {
				if (!strcmp(p[k], "hup")) {
					hup = TRUE;
					continue;
				}
				struct input_event ev = {0};
				int t, cd, v;
				sscanf(p[k], "%d,%d,%d", &t, &cd, &v);
				ev.type = t, ev.code = cd, ev.value = v;
				if (write(q[1], &ev, sizeof ev) != sizeof ev)
					abort();
			}
			bridge.fd = q[0];
			g_autoptr(GMainLoop) loop = g_main_loop_new(NULL, FALSE);
			gboolean keep = on_uinput(q[0], hup ? G_IO_HUP : G_IO_IN, loop);
			printf("feed keep=%d failed=%d used2=%d used5=%d\n", keep, uinput_failed, bridge.used[2],
			       bridge.used[5]);
			if (kernel_rd >= 0)
				dump_kernel();
			if (bridge.sysfs)
				dump_sysfs();
			close(q[0]);
			close(q[1]);
		} else if (!strcmp(c, "uinput")) {
			/* Events from feedbackd through our uinput device: gain values in p[1..] */
			int q[2];
			if (pipe2(q, O_NONBLOCK) < 0)
				abort();
			for (int k = 1; p[k]; k++) {
				struct input_event ev = {.type = EV_FF, .code = FF_GAIN, .value = atoi(p[k])};
				if (write(q[1], &ev, sizeof ev) != sizeof ev)
					abort();
				printf("gain-in=%d\n", ev.value);
			}
			struct input_event ev = {.type = EV_UINPUT, .code = UI_FF_UPLOAD, .value = 1};
			upload_effect = rumble(5, 0xffff, 0, 30, 0);
			if (write(q[1], &ev, sizeof ev) != sizeof ev)
				abort();
			on_uinput(q[0], G_IO_IN, NULL);
			printf("gain=%u used5=%d upload=%d\n", bridge.gain, bridge.used[5], upload_retval);
		}
		if (fake_gb_log && fake_gb_log->len) {
			printf("binder=%s\n", g_strdelimit(fake_gb_log->str, "\n", ';'));
			g_string_truncate(fake_gb_log, 0);
		}
	}
	return 0;
}
"""


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    d = tmp_path_factory.mktemp("vibrator")
    sandboxed_source(SRC, d, [
        ("/sys/class/leds/vibrator", d / "leds-vibrator"),
        ('"/dev/input"', f'"{d}/input"'),
        ("/dev/uinput", d / "uinput"),
    ], allow=("/dev/binder",))
    exe = build_harness(d, "vibrator", HARNESS, flags=[f'-DBASE="{d}"'])
    return d, functools.partial(run, exe)


@pytest.fixture
def vib(built):
    d, binary = built
    (d / "leds-vibrator").mkdir(exist_ok=True)
    for f in ("duration", "activate"):
        (d / "leds-vibrator" / f).write_text("")
    for sub in ("input", "input-names"):
        shutil.rmtree(d / sub, ignore_errors=True)
        (d / sub).mkdir()
    (d / "uinput").unlink(missing_ok=True)
    return binary


def amps(out):
    return [float(line[4:]) for line in out.splitlines() if line.startswith("amp=")]


def test_effect_amplitude(vib):
    out = vib("amp-rumble:65535:0:65535", "amp-rumble:0:32768:65535", "amp-rumble:65535:0:32768",
              "amp-rumble:0:0:65535", "amp-periodic:32767:65535", "amp-periodic:-16384:65535",
              "amp-periodic:16384:0").stdout
    assert amps(out) == pytest.approx([1.0, 0.5, 0.5, 0.0, 1.0, 0.5, 0.0], abs=1e-3)


def events(out):
    """Events written to the vendor device (in order) and effects uploaded to it (ioctl)"""
    return ([line for line in out.splitlines() if line.startswith("event")],
            [line for line in out.splitlines() if line.startswith("upload type")])


def test_short_pulse_becomes_full_tap(vib):
    # feedbackd's key press: 7 ms at 20 % is not felt on the linear motor
    out = vib("kernel", "now:1000", "on:7:0.2").stdout
    assert events(out) == ([
        "event type=21 code=96 value=32767",        # FF_GAIN at full strength
        "event type=21 code=3 value=1",              # play the uploaded effect
    ], ["upload type=82 length=20 id=-1"])          # FF_CONSTANT for 20 ms


@pytest.mark.parametrize("amplitude,gain", [
    (0.0, int(0.4 * 0x7fff)),   # the floor: weak effects are still felt
    (0.5, int(0.7 * 0x7fff)),
    (1.0, 0x7fff),
    (3.0, 0x7fff),              # clamped
])
def test_longer_pulse_strength(vib, amplitude, gain):
    out = vib("kernel", f"on:100:{amplitude}").stdout
    assert events(out) == ([f"event type=21 code=96 value={gain}", "event type=21 code=3 value=1"],
                           ["upload type=82 length=100 id=-1"])


def test_tap_is_not_cut_short(vib):
    # feedbackd stops after its own 7 ms; the 20 ms tap ends by itself
    out = vib("kernel", "now:1000", "on:7:0.2", "now:1007", "off", "now:1021", "off").stdout
    lines = out.splitlines()
    first_off = lines.index("off done")
    assert not [ln for ln in lines[3:first_off] if ln.startswith("event")]
    assert lines[first_off + 1] == "event type=21 code=3 value=0"


def test_long_pulse_stops_at_once(vib):
    out = vib("kernel", "now:1000", "on:200:1", "now:1001", "off").stdout
    assert events(out)[0][-1] == "event type=21 code=3 value=0"


def test_zero_length_does_nothing(vib):
    assert events(vib("kernel", "on:0:1").stdout) == ([], [])


def test_sysfs_backend(vib):
    out = vib("sysfs", "on:120:0.3", "off").stdout
    s = [line for line in out.splitlines() if line.startswith("sysfs=")]
    assert s == ["sysfs=120 1", "sysfs=120 0"]


def test_hal_backend_with_amplitude_control(vib):
    out = kv(vib("hal:4", "on:50:0.25").stdout)
    log = out["binder"]
    # getCapabilities, setAmplitude(0.25), on(50 ms, no callback)
    assert "tx 1;" in log
    assert "float 0.2500;tx 6;" in log
    assert log.endswith("request;i32 50;object null;tx 3;")


def test_hal_backend_without_amplitude_control(vib):
    out = vib("hal:0", "on:50:0.25", "off").stdout
    on, off = [line[7:] for line in out.splitlines() if line.startswith("binder=")]
    assert "tx 6;" not in on and on.endswith("tx 3;")
    assert off == "tx 2;"


def test_hal_amplitude_never_zero(vib):
    # 0 would be "off" for the HAL: the weakest effect is 0.01
    assert "float 0.0100;tx 6;" in kv(vib("hal:4", "on:50:0").stdout)["binder"]


def test_upload_play_erase(vib):
    out = vib("kernel", "upload:2:r:32768:150:0", "upload:16:r:1:1:0", "upload:3:c:1:1:0",
              "play:2:1", "erase", "play:2:1").stdout
    k = kv(out)
    assert k["upload 2"] == "0"
    assert k["upload 16"] == str(-22)   # beyond the slots: EINVAL
    assert k["upload 3"] == str(-22)    # FF_CONSTANT is not offered
    lines = out.splitlines()
    first = lines.index("played 2")
    # Played: 150 ms at about half strength (0.4 + 0.6 * 32768 / 65535)
    assert "upload type=82 length=150 id=-1" in lines[:first]
    assert f"event type=21 code=96 value={int((0.4 + 0.6 * 32768 / 65535) * 0x7fff)}" in lines[:first]
    assert k["used2"] == "0"
    # After the erase playing it only stops
    assert lines[first + 1:-1] == ["used2=0", "event type=21 code=3 value=0"]


def test_delayed_effect(vib):
    out = vib("kernel", "upload:2:r:65535:40:10", "play:2:1").stdout
    assert "upload type=82 length=40 id=-1" in out


def test_gain_from_feedbackd_is_clamped(vib):
    out = kv(vib("uinput:70000").stdout)
    assert out["gain"] == "65535 used5=1 upload=0"
    out = kv(vib("uinput:-5").stdout)
    assert out["gain"].startswith("0 ")


# ---- Backends that fail ----

def test_vendor_device_that_takes_no_events(vib):
    r = vib("kernel-broken", "on:100:1")
    # Reported, the effect is still uploaded and started as far as the driver lets it
    assert "aw-haptic-hv: Bad file descriptor" in r.stderr
    assert "upload type=82 length=100 id=-1" in r.stdout


def test_vendor_device_refusing_the_effect(vib):
    r = vib("kernel", "fail:sff", "on:100:1")
    out, uploads = events(r.stdout)
    # The strength was set, but nothing is started
    assert out == [f"event type=21 code=96 value={0x7fff}"]
    assert "cannot upload effect" in r.stderr


def test_sysfs_vibrator_gone(built, vib):
    d, binary = built
    shutil.rmtree(d / "leds-vibrator")
    r = vib("sysfs", "on:120:1")
    # Without a duration it is never activated
    assert "sysfs=- -" in r.stdout
    assert r.stderr.count("leds-vibrator/duration") == 1 and "activate" not in r.stderr


def test_hal_transaction_failing(vib):
    r = vib("hal:0", "fail:binder", "on:50:1")
    # Each failed call is reported; the pulse is still sent
    assert kv(r.stdout)["binder"].endswith("i32 50;object null;tx 3;")
    assert "IVibrator transaction 1 failed (status -1, exception 0)" in r.stderr
    assert "IVibrator transaction 3 failed" in r.stderr


def test_hal_exception(vib):
    r = vib("hal-exception", "on:50:1")
    assert "IVibrator transaction 1 failed (status 0, exception -3)" in r.stderr


def test_hal_connects_once(vib):
    out = vib("hal:0", "on:50:1", "on:60:1").stdout
    logs = [line[7:] for line in out.splitlines() if line.startswith("binder=")]
    assert logs[0].startswith("servicemanager /dev/binder;get-service android.hardware.vibrator.IVibrator/default;")
    # The second pulse reuses the connection
    assert logs[1] == "request;i32 60;object null;tx 3;"


def test_hal_not_there_yet(vib):
    out = vib("fail:no-service", "on:50:1", "on:50:1").stdout
    logs = [line[7:] for line in out.splitlines() if line.startswith("binder=")]
    # Looked up again on the next pulse (the container may start later), nothing sent
    assert logs == ["servicemanager /dev/binder;get-service android.hardware.vibrator.IVibrator/default;",
                    "get-service android.hardware.vibrator.IVibrator/default;"]


def test_no_binder(vib):
    out = vib("fail:no-sm", "on:50:1").stdout
    assert "binder=" not in out


def test_hal_died_reconnects(vib):
    out = vib("hal:0", "on:50:1", "hal-died", "on:50:1").stdout
    assert kv(out)["hal client"] == "0 remote=0 death=0"
    logs = [line[7:] for line in out.splitlines() if line.startswith("binder=")]
    assert logs[-1].startswith("get-service android.hardware.vibrator.IVibrator/default;")


# ---- feedbackd's requests through the uinput device ----

EV_FF, EV_UINPUT, UI_FF_UPLOAD, UI_FF_ERASE = 0x15, 0x0101, 1, 2


def test_requests_that_cannot_be_taken(vib):
    out = vib("kernel", "fail:begin", "upload:2:r:65535:40:0", "erase").stdout
    # Never acknowledged by the daemon (END not called): uinput keeps its own answer
    assert kv(out)["upload 2"] == "1"


def test_play_and_erase_from_feedbackd(vib):
    out = vib("kernel", "upload:2:r:65535:150:0",
              f"feed:{EV_FF},2,1:{EV_UINPUT},{UI_FF_ERASE},1:{EV_FF},2,1").stdout
    k = kv(out)
    assert k["feed keep"] == "1 failed=0 used2=0 used5=0"
    ev, uploads = events(out)
    # Played once at full strength; after the erase the second play only stops
    assert uploads == ["upload type=82 length=150 id=-1"]
    assert ev == [f"event type=21 code=96 value={0x7fff}", "event type=21 code=3 value=1",
                  "event type=21 code=3 value=0"]


def test_failed_uinput_device_ends_the_loop(vib):
    # Polling a failed fd would be ready at once again: the source goes, the daemon exits
    assert kv(vib("feed:hup").stdout)["feed keep"] == "0 failed=1 used2=0 used5=0"


# ---- Finding the vendor device ----

def vendor_device(d, name="aw-haptic-hv", entry="event3"):
    (d / "input" / entry).write_bytes(b"")
    (d / "input-names" / entry).write_text(name + "\n")


def test_find_vendor_device(built, vib):
    d, binary = built
    (d / "input" / "mice").write_bytes(b"")          # not an event device
    (d / "input" / "event0").mkdir()                 # cannot be opened
    vendor_device(d, "fts_ts", "event1")             # another device
    (d / "input" / "event2").write_bytes(b"")        # no name
    vendor_device(d)
    assert kv(vib("find").stdout)["found"] == "/input/event3"


def test_no_vendor_device(built, vib):
    d, binary = built
    # Every entry is looked at (the order of the directory decides nothing here)
    (d / "input" / "mice").write_bytes(b"")
    (d / "input" / "event0").mkdir()
    vendor_device(d, "fts_ts", "event1")
    (d / "input" / "event2").write_bytes(b"")
    assert kv(vib("find").stdout)["found"] == "none"


def test_vendor_device_never_comes(vib):
    # One try every 2 s for a minute, then it gives up
    out = vib("retry").stdout
    assert kv(out)["retry tries"] == "30 kernel=none sysfs=0 client=0"


def test_vendor_device_coming_later_takes_over(built, vib):
    d, binary = built
    vendor_device(d)
    out = vib("sysfs", "on:500:1", "retry", "on:100:1").stdout
    # The LED vibrator is stopped and no longer used; the next pulse goes to the vendor device
    assert kv(out)["retry tries"] == "1 kernel=/input/event3 sysfs=0 client=0"
    assert "sysfs=500 0" in out
    assert "upload type=82 length=100 id=-1" in out
    assert read_events(d / "input" / "event3") == [(EV_FF, 96, 0x7fff), (EV_FF, 3, 1)]


def test_vendor_device_coming_later_replaces_the_hal(built, vib):
    d, binary = built
    vendor_device(d)
    out = vib("hal:0", "on:50:1", "retry").stdout
    assert kv(out)["retry tries"] == "1 kernel=/input/event3 sysfs=0 client=0"
    # The HAL got its off before the switch
    assert kv(out)["binder"] == "tx 2;"


def read_events(path):
    data = path.read_bytes()
    size = struct.calcsize("llHHi")
    return [struct.unpack("llHHi", data[i:i + size])[2:] for i in range(0, len(data), size)]


# ---- The uinput device ----

FF_BITS = [0x50, 0x51, 0x5a, 0x58, 0x59, 0x5b, 0x5c, 0x60]


def test_uinput_device(built, vib):
    d, binary = built
    (d / "uinput").write_bytes(b"")
    out = vib("create:0").stdout
    lines = [line for line in out.splitlines() if line.startswith("uinput ")]
    # Force feedback with the effects feedbackd uses, 16 slots, a virtual bus
    assert lines == ["uinput evbit 21", *[f"uinput ffbit {b}" for b in FF_BITS],
                     "uinput setup name=vibrator-ff-bridge bus=6 effects=16", "uinput create"]
    assert kv(out)["created"] == "1"


@pytest.mark.parametrize("step", [1, 2, 9, 10, 11])
def test_uinput_device_set_up_failing(built, vib, step):
    d, binary = built
    (d / "uinput").write_bytes(b"")
    r = vib(f"create:{step}")
    assert kv(r.stdout)["created"] == "0"
    assert "Cannot set up uinput device: Invalid argument" in r.stderr
    assert len([line for line in r.stdout.splitlines() if line.startswith("uinput ")]) == step


def test_no_uinput(built, vib):
    d, binary = built
    r = vib("create:0")
    assert kv(r.stdout)["created"] == "0"
    assert f"Cannot open {d}/uinput: No such file or directory" in r.stderr


# ---- The daemon ----

def daemon(built, **env):
    d, binary = built
    os.mkfifo(d / "uinput")
    p = Daemon([binary.args[0], "daemon"], sanitized_env(**env))
    fifo = os.open(d / "uinput", os.O_RDWR)
    return p, fifo


def feed(fifo, *evs):
    for t, c, v in evs:
        os.write(fifo, struct.pack("llHHi", 0, 0, t, c, v))


def test_daemon_with_the_led_vibrator(built, vib):
    d, binary = built
    p, fifo = daemon(built, FAKE_UPLOAD="4,65535,250")
    try:
        p.wait_for(f"Backend: {d}/leds-vibrator")
        poll(lambda: any("uinput create" in line for line in p.out), what="uinput device")
        feed(fifo, (EV_UINPUT, UI_FF_UPLOAD, 1), (EV_FF, 4, 1))
        poll(lambda: (d / "leds-vibrator/activate").read_text() == "1", what="vibration")
        assert (d / "leds-vibrator/duration").read_text() == "250"
        code, err, out = p.stop()
    finally:
        os.close(fifo)
    assert code == 0
    # Stopped when the daemon goes, the device removed
    assert (d / "leds-vibrator/activate").read_text() == "0"
    assert out.splitlines()[-1] == "uinput destroy"


def test_daemon_with_the_vendor_device(built, vib):
    d, binary = built
    vendor_device(d)
    p, fifo = daemon(built, FAKE_UPLOAD="1,65535,30")
    try:
        p.wait_for(f"Backend: aw-haptic-hv ({d}/input/event3)")
        poll(lambda: any("uinput create" in line for line in p.out), what="uinput device")
        feed(fifo, (EV_FF, 96, 0x8000), (EV_UINPUT, UI_FF_UPLOAD, 1), (EV_FF, 1, 1), (EV_FF, 1, 0))
        poll(lambda: len(read_events(d / "input/event3")) == 3, what="vendor events")
        code, err, out = p.stop()
    finally:
        os.close(fifo)
    assert code == 0
    # Half of feedbackd's gain: 0.4 + 0.6 * 0.5 of the driver's maximum, played, stopped, and
    # stopped once more when the daemon goes
    assert read_events(d / "input/event3") == [(EV_FF, 96, int((0.4 + 0.6 * 0x8000 / 0xffff) * 0x7fff)),
                                               (EV_FF, 3, 1), (EV_FF, 3, 0), (EV_FF, 3, 0)]
    assert "upload type=82 length=30 id=-1" in out


def test_daemon_with_the_hal(built, vib):
    d, binary = built
    shutil.rmtree(d / "leds-vibrator")
    p, fifo = daemon(built)
    try:
        p.wait_for("Backend: android.hardware.vibrator.IVibrator/default")
        poll(lambda: any("uinput create" in line for line in p.out), what="uinput device")
        code, err, out = p.stop(sig=2)
    finally:
        os.close(fifo)
    assert code == 0


def test_daemon_without_uinput(built, vib):
    d, binary = built
    r = vib("daemon", check=False)
    assert r.returncode == 1
    assert f"Cannot open {d}/uinput" in r.stderr
