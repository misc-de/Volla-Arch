# plinius-torch: the LED current per brightness level (never above --max-current or the
# driver's maximum), torch mode only, switching off (level 0, timeout, start with --off) and
# the change uevent for Phosh. V4L2 ioctls are faked; subdevices and sysfs are files in tmp. The
# daemon runs with uleds as a FIFO, logind's PrepareForSleep from a fake on a private bus.

import fnmatch
import functools
import os
import re
import struct

import pytest

from dhw_daemon_bus import Daemon, FakeService, PrivateBus, poll, sanitized_env
from dhw_harness import PKG, build_harness, kv, run, sandboxed_source

SRC = PKG / "plinius-torch" / "plinius-torch.c"

HARNESS = r"""
#define _GNU_SOURCE
#include <fcntl.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <errno.h>
#include <sys/ioctl.h>
#include <linux/videodev2.h>
#include <linux/uleds.h>
#include <systemd/sd-event.h>
static int fake_ioctl(int fd, unsigned long req, ...);
static int drv_min = 1000, drv_max = 200000, query_fails;
/* Registering the LED with uleds: the device node is a FIFO in the test, the registration
 * itself is printed (FAKE_ULEDS_SHORT: the kernel takes less than the whole struct) */
static ssize_t fake_write(int fd, const void *buf, size_t n);
/* FAKE_NO_TIMER: sd-event cannot create the off timer */
static int fake_add_time(sd_event *e, sd_event_source **s, clockid_t clock, uint64_t usec, uint64_t accuracy,
			 sd_event_time_handler_t callback, void *userdata)
{
	if (getenv("FAKE_NO_TIMER"))
		return -ENOMEM;
	return sd_event_add_time(e, s, clock, usec, accuracy, callback, userdata);
}
#define ioctl fake_ioctl
#define write fake_write
#define sd_event_add_time fake_add_time
#define main real_main
#include "plinius-torch.c"
#undef main
#undef ioctl
#undef write
#undef sd_event_add_time

static ssize_t fake_write(int fd, const void *buf, size_t n)
{
	if (n == sizeof(struct uleds_user_dev)) {
		const struct uleds_user_dev *dev = buf;
		printf("uleds name=%s max=%d\n", dev->name, dev->max_brightness);
		return getenv("FAKE_ULEDS_SHORT") ? (ssize_t)n - 1 : (ssize_t)n;
	}
	return write(fd, buf, n);
}

static int fake_ioctl(int fd, unsigned long req, ...)
{
	va_list ap;
	va_start(ap, req);
	void *arg = va_arg(ap, void *);
	va_end(ap);
	int led = -1;
	for (int i = 0; i < MAX_LEDS; i++)
		if (fds[i] == fd)
			led = i;
	if (req == VIDIOC_QUERYCTRL) {
		struct v4l2_queryctrl *q = arg;
		if (query_fails || q->id != V4L2_CID_FLASH_TORCH_INTENSITY) {
			errno = EINVAL;
			return -1;
		}
		q->minimum = drv_min;
		q->maximum = drv_max;
		return 0;
	}
	if (req == VIDIOC_S_CTRL) {
		struct v4l2_control *c = arg;
		const char *name = c->id == V4L2_CID_FLASH_TORCH_INTENSITY ? "intensity"
				 : c->id == V4L2_CID_FLASH_LED_MODE ? "mode" : "other";
		const char *value = "";
		if (c->id == V4L2_CID_FLASH_LED_MODE)
			value = c->value == V4L2_FLASH_LED_MODE_TORCH ? "torch"
			      : c->value == V4L2_FLASH_LED_MODE_NONE ? "none" : "FLASH";
		printf("ctrl led%d %s=%d%s\n", led, name, c->value, value);
		return 0;
	}
	errno = ENOTTY;
	return -1;
}

int main(int argc, char **argv)
{
	/* daemon <options>: the daemon itself (argv[1] stands in for its name) */
	if (argc > 1 && !strcmp(argv[1], "daemon")) {
		setvbuf(stdout, NULL, _IOLBF, 0);
		return real_main(argc - 1, argv + 1);
	}
	sd_event *event = NULL;
	sd_event_default(&event);
	sd_event_add_time(event, &off_timer, CLOCK_BOOTTIME, 0, 0, on_timeout, NULL);
	sd_event_source_set_enabled(off_timer, SD_EVENT_OFF);
	int p[2];
	if (pipe(p) < 0)
		abort();
	for (int i = 1; i < argc; i++) {
		char *a = argv[i];
		if (!strncmp(a, "max=", 4)) {
			max_current = atoi(a + 4);
		} else if (!strncmp(a, "levels=", 7)) {
			levels = atoi(a + 7);
		} else if (!strncmp(a, "driver=", 7)) {
			sscanf(a + 7, "%d,%d", &drv_min, &drv_max);
		} else if (!strcmp(a, "query-fails")) {
			query_fails = 1;
		} else if (!strncmp(a, "level=", 6)) {
			int level = atoi(a + 6);
			if (write(p[1], &level, sizeof level) != sizeof level)
				abort();
			on_level(NULL, p[0], 0, NULL);
			int enabled = -1;
			sd_event_source_get_enabled(off_timer, &enabled);
			printf("level %d: lit=%d leds=%d timer=%s\n", level, lit, n_subdevs,
			       enabled == SD_EVENT_ONESHOT ? "armed" : "off");
		} else if (!strcmp(a, "short-level")) {
			/* Less than a whole level from uleds */
			if (write(p[1], "\1", 1) != 1)
				abort();
			on_level(NULL, p[0], 0, NULL);
			printf("short-level: lit=%d\n", lit);
		} else if (!strcmp(a, "timeout")) {
			on_timeout(NULL, 0, NULL);
			printf("timeout: lit=%d\n", lit);
		} else if (!strcmp(a, "off-only")) {
			char *args[] = {"plinius-torch", "--off", NULL};
			printf("main=%d\n", real_main(2, args));
		} else if (!strncmp(a, "args=", 5)) {
			char *args[8] = {"plinius-torch"};
			int n = 1;
			for (char *t = strtok(a + 5, ","); t && n < 7; t = strtok(NULL, ","))
				args[n++] = t;
			args[n] = NULL;
			optind = 1;
			printf("main=%d\n", real_main(n, args));
		}
	}
	return 0;
}
"""


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    d = tmp_path_factory.mktemp("torch")
    sandboxed_source(SRC, d, [
        ("/sys/class/leds/", f"{d}/leds/"),
        ("/sys/class/video4linux", d / "v4l"),
        ('"/dev/%s"', f'"{d}/dev/%s"'),
        ("/dev/uleds", d / "uleds"),
    ])
    exe = build_harness(d, "torch", HARNESS, pkgs=("libsystemd",))
    return d, functools.partial(run, exe)


@pytest.fixture
def torch(built):
    d, binary = built
    import shutil
    for sub in ("leds", "v4l", "dev"):
        shutil.rmtree(d / sub, ignore_errors=True)
    (d / "uleds").unlink(missing_ok=True)
    (d / "notify").unlink(missing_ok=True)
    (d / "leds/white:torch").mkdir(parents=True)
    (d / "leds/white:torch/brightness").write_text("7\n")
    (d / "leds/white:torch/uevent").write_text("")
    (d / "dev").mkdir()
    for i, name in enumerate(["ocp81378-led 0", "imx582", "ocp81378-led 1"]):
        sub = d / "v4l" / f"v4l-subdev{i}"
        sub.mkdir(parents=True)
        (sub / "name").write_text(name + "\n")
        (d / "dev" / f"v4l-subdev{i}").write_text("")
    (d / "v4l" / "video0").mkdir()
    (d / "v4l" / "video0" / "name").write_text("ocp81378-led fake\n")   # not a subdevice
    return d, binary


def ctrls(out):
    return [line[5:] for line in out.splitlines() if line.startswith("ctrl ")]


@pytest.mark.parametrize("level,current", [
    (10, 98900),   # the highest level is the cap
    (5, 49450),
    (1, 9890),
    (15, 98900),   # above the levels: clamped
])
def test_intensity_per_level(torch, level, current):
    d, binary = torch
    out = binary(f"level={level}").stdout
    assert sorted(ctrls(out)) == sorted([
        f"led0 intensity={current}", "led0 mode=2torch",
        f"led1 intensity={current}", "led1 mode=2torch",
    ])
    assert "lit=1 leds=2 timer=armed" in out


def test_never_above_the_driver_maximum(torch):
    d, binary = torch
    out = binary("max=500000", "driver=1000,120000", "level=10").stdout
    assert "led0 intensity=120000" in ctrls(out)


def test_driver_minimum(torch):
    d, binary = torch
    out = binary("driver=20000,200000", "level=1").stdout
    assert "led0 intensity=20000" in ctrls(out)


def test_no_flash_mode_ever(torch):
    d, binary = torch
    out = binary("level=3", "level=10", "level=0", "level=4", "timeout").stdout
    assert all("FLASH" not in c for c in ctrls(out))


def test_level_zero_switches_off(torch):
    d, binary = torch
    out = binary("level=4", "level=0").stdout
    assert ctrls(out)[-2:] == ["led0 mode=0none", "led1 mode=0none"]
    assert "level 0: lit=0 leds=2 timer=off" in out
    # Phosh is told about each change
    assert (d / "leds/white:torch/uevent").read_text() == "change\n"


def test_timeout_switches_off_through_sysfs(torch):
    d, binary = torch
    out = binary("level=4", "timeout").stdout
    assert "timeout: lit=0" in out
    assert ctrls(out)[-2:] == ["led0 mode=0none", "led1 mode=0none"]
    # So that the brightness Phosh reads matches
    assert (d / "leds/white:torch/brightness").read_text() == "0\n"


def test_driver_refusing_the_query_sets_nothing(torch):
    d, binary = torch
    out = binary("query-fails", "level=10").stdout
    assert not [c for c in ctrls(out) if "intensity" in c or "torch" in c]


def test_off_at_start(torch):
    d, binary = torch
    out = binary("off-only").stdout
    # Whatever a crashed run left on: both LEDs to mode none, then exit
    assert sorted(ctrls(out)) == ["led0 mode=0none", "led1 mode=0none"]
    assert "main=0" in out


@pytest.mark.parametrize("args", ["--max-current,0", "--timeout,-1", "--levels,0", "--bogus"])
def test_invalid_options(torch, args):
    d, binary = torch
    assert kv(binary(f"args={args}").stdout)["main"] == "2"


def test_missing_uleds_fails(torch):
    d, binary = torch
    out = binary("args=--timeout,60").stdout
    assert kv(out)["main"] == "1"


def test_led_that_cannot_be_opened_is_skipped(torch):
    d, binary = torch
    # The second flash LED's node is not a device that opens
    (d / "dev" / "v4l-subdev2").unlink()
    (d / "dev" / "v4l-subdev2").mkdir()
    r = binary("level=10", "level=0")
    # Only the other LED is switched (its index depends on the directory order)
    c = ctrls(r.stdout)
    assert [x.split()[1] for x in c] == ["intensity=98900", "mode=2torch", "mode=0none"]
    assert len({x.split()[0] for x in c}) == 1
    assert "v4l-subdev2: Is a directory" in r.stderr
    assert "level 0: lit=0 leds=2 timer=off" in r.stdout


def test_led_refusing_the_torch_is_reported(torch):
    d, binary = torch
    r = binary("query-fails", "level=3")
    assert r.stderr.count("cannot switch on") == 2


def test_no_flash_leds_yet(torch):
    d, binary = torch
    import shutil
    shutil.rmtree(d / "v4l")
    r = binary("level=4")
    # Phosh's switch still shows on (and goes off with the timer); the LEDs come later
    assert "no V4L2 subdevice named ocp81378-led* (yet)" in r.stderr
    assert "level 4: lit=1 leds=0 timer=armed" in r.stdout
    assert ctrls(r.stdout) == []


def test_subdevice_without_a_name_is_not_a_flash_led(torch):
    d, binary = torch
    (d / "v4l" / "v4l-subdev0" / "name").unlink()
    out = binary("level=10").stdout
    assert [x.split()[1] for x in ctrls(out)] == ["intensity=98900", "mode=2torch"]
    assert "leds=1" in out


def test_change_that_cannot_be_announced(torch):
    d, binary = torch
    import shutil
    shutil.rmtree(d / "leds")
    r = binary("level=2")
    assert "cannot announce the change" in r.stderr
    assert "level 2: lit=1" in r.stdout


def test_partial_level_is_ignored(torch):
    d, binary = torch
    out = binary("short-level").stdout
    assert "short-level: lit=0" in out
    assert ctrls(out) == []


def test_off_at_start_only_for_the_matching_leds(torch):
    d, binary = torch
    out = binary("args=--off,--match,imx582").stdout
    assert ctrls(out) == ["led0 mode=0none"]
    assert kv(out)["main"] == "0"


def test_uleds_refusing_the_led_fails(torch):
    d, binary = torch
    os.mkfifo(d / "uleds")
    r = binary("args=--timeout,60", env={**os.environ, "FAKE_ULEDS_SHORT": "1"})
    assert kv(r.stdout)["main"] == "1"
    assert "uleds name=white:torch max=10" in r.stdout


def test_no_off_timer_fails(torch):
    d, binary = torch
    os.mkfifo(d / "uleds")
    r = binary("args=--levels,4", env={**os.environ, "FAKE_NO_TIMER": "1"})
    assert kv(r.stdout)["main"] == "1"
    assert "cannot create the off timer" in r.stderr


LOGIN1 = """<node><interface name="org.freedesktop.login1.Manager">
  <signal name="PrepareForSleep"><arg type="b"/></signal></interface></node>"""


class TorchDaemon:
    """The daemon with uleds as a FIFO the test writes levels into, and a notify socket"""

    def __init__(self, d, binary, bus_address, *args):
        import socket
        os.mkfifo(d / "uleds")
        self.notify = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.notify.bind(str(d / "notify"))
        self.notify.settimeout(20)
        self.p = Daemon([binary.args[0], "daemon", *args],
                        sanitized_env(DBUS_SYSTEM_BUS_ADDRESS=bus_address, NOTIFY_SOCKET=d / "notify"))
        # Its end stays open: no EOF for the daemon, and writes never block on a missing reader
        self.fifo = os.open(d / "uleds", os.O_RDWR)

    def ready(self):
        return self.notify.recv(4096).decode()

    def level(self, n):
        os.write(self.fifo, struct.pack("i", n))

    def stop(self, *a):
        try:
            return self.p.stop(*a)
        finally:
            os.close(self.fifo)
            self.notify.close()


@pytest.fixture
def bus(tmp_path):
    b = PrivateBus(tmp_path)
    yield b
    b.close()


def test_daemon(torch, bus):
    from gi.repository import GLib
    d, binary = torch
    login1 = FakeService(bus, "org.freedesktop.login1", "/org/freedesktop/login1", LOGIN1, {})
    t = TorchDaemon(d, binary, bus.address, "--timeout", "1", "--max-current", "50000")
    try:
        assert t.ready() == "READY=1"
        t.p.wait_for("white:torch ready (2 LED(s) present), at most 50000 uA each, off after 1 s")
        (d / "leds/white:torch/uevent").write_text("")
        t.level(5)
        # Half of the cap on both LEDs, and Phosh is told
        poll(lambda: (d / "leds/white:torch/uevent").read_text() == "change\n", what="uevent")
        # The timeout switches it off through sysfs, so the brightness Phosh reads is 0
        t.p.wait_for("switching off: timeout")
        assert (d / "leds/white:torch/brightness").read_text() == "0\n"
        (d / "leds/white:torch/brightness").write_text("3\n")
        t.level(3)
        poll(lambda: sum("intensity=15000" in line for line in t.p.out) == 2, what="level 3")
        # Waking up changes nothing, going to sleep switches off
        login1.emit("/org/freedesktop/login1", "org.freedesktop.login1.Manager", "PrepareForSleep",
                    GLib.Variant("(b)", (False,)))
        login1.emit("/org/freedesktop/login1", "org.freedesktop.login1.Manager", "PrepareForSleep",
                    GLib.Variant("(b)", (True,)))
        t.p.wait_for("switching off: suspend")
        assert (d / "leds/white:torch/brightness").read_text() == "0\n"
        t.level(10)
        poll(lambda: sum("intensity=50000" in line for line in t.p.out) == 2, what="level 10")
        code, err, out = t.stop()
    finally:
        login1.close()
    assert code == 0
    # Off first (what an earlier run may have left on), then registered with uleds
    lines = out.splitlines()
    assert lines[2] == "uleds name=white:torch max=10"
    # Only these, in order: off at start, on at 5, off (timeout), on at 3, off (suspend), on at
    # 10, off (SIGTERM)
    modes = [line.split()[2] for line in lines if line.startswith("ctrl ") and "mode" in line]
    assert modes == (["mode=0none"] * 2 + ["mode=2torch"] * 2) * 3 + ["mode=0none"] * 2
    assert "intensity=25000" in out
    assert err.count("switching off") == 2


def test_daemon_without_a_bus_still_runs(torch, tmp_path):
    d, binary = torch
    t = TorchDaemon(d, binary, f"unix:path={tmp_path}/no-bus", "--levels", "4", "--match", "ocp81378-led 1")
    assert t.ready() == "READY=1"
    t.p.wait_for("white:torch ready (1 LED(s) present), at most 98900 uA each, off after 900 s")
    t.level(2)
    poll(lambda: any("led0 mode=2torch" in line for line in t.p.out), what="torch on")
    code, err, out = t.stop()
    assert code == 0
    assert "uleds name=white:torch max=4" in out
    assert "led0 intensity=49450" in out
    assert out.splitlines()[-1] == "ctrl led0 mode=0none"


# ---- Wiring ----

def test_feedbackd_does_not_take_the_torch_as_notification_led():
    # feedbackd's rule claims every "*:torch" LED (a pattern trigger, then blinks it for
    # messages); 73-plinius-torch.rules comes after it and takes that back for our LED
    name = re.search(r'^#define LED_NAME "([^"]+)"$', SRC.read_text(), re.M).group(1)
    assert fnmatch.fnmatchcase(f"/devices/virtual/misc/uleds/{name}", "*/*:torch")
    rules_name = "73-plinius-torch.rules"
    assert rules_name > "72-feedbackd.rules"
    rules = (PKG / "plinius-torch" / rules_name).read_text()
    rule = [line for line in rules.splitlines() if line and not line.startswith("#")]
    assert rule == [
        'ACTION=="remove", GOTO="plinius_torch_end"',
        f'SUBSYSTEM=="leds", KERNEL=="{name}", ENV{{FEEDBACKD_TYPE}}="", RUN="/usr/bin/true"',
        'LABEL="plinius_torch_end"']
    pkgbuild = (PKG / "plinius-torch" / "PKGBUILD").read_text()
    assert re.search(rf"^\s+{re.escape(rules_name)}$", pkgbuild, re.M)
    assert f'install -Dm644 {rules_name} -t "$pkgdir/usr/lib/udev/rules.d"' in pkgbuild
