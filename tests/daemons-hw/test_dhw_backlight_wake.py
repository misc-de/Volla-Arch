# plinius-backlight-wake: the end of an always-on display from inotify events, the light
# sensor's screen colour (fixed, or from phoc's file in the runtime directory), the display hooks, and the loop itself (display Off -> On sets the
# backlight again and runs the hooks). Every path (DRM, LED, runtime dirs, hooks) is in tmp.

import os
import signal
import struct
import subprocess
import time

import pytest

from dhw_harness import PKG, build_harness, kv, run, sandboxed_source

SRC = PKG / "plinius-backlight-wake" / "plinius-backlight-wake.c"

HARNESS = r"""
#include <signal.h>
#include <sys/resource.h>
#define main real_main
#include "plinius-backlight-wake.c"
#undef main

int main(int argc, char **argv)
{
	const char *cmd = argv[1];
	if (!strcmp(cmd, "ended")) {
		int fd = open(argv[2], O_RDONLY);
		int als_changed = 0;
		int ended = read_events(fd, &als_changed);
		printf("ended=%d\nals_changed=%d\n", ended, als_changed);
	} else if (!strcmp(cmd, "parse")) {
		char out[ALS_COLOR_MAX] = "";
		int r = parse_als_color(argv[2], out, sizeof(out));
		printf("parse=%d\nout=%s\n", r, out);
	} else if (!strcmp(cmd, "load")) {
		load_als_color();
		printf("color=%s\n", als_color);
		keep_als_color();
	} else if (!strcmp(cmd, "shown")) {
		printf("shown=%d\n", aod_shown());
	} else if (!strcmp(cmd, "als")) {
		keep_als_color();
		keep_als_color();
	} else if (!strcmp(cmd, "als-refused")) {
		/* A write the driver refuses: here a file size limit of 0 (EFBIG), lifted again before
		 * exit; reading still works */
		struct rlimit saved, none;
		getrlimit(RLIMIT_FSIZE, &saved);
		none = saved;
		none.rlim_cur = 0;
		signal(SIGXFSZ, SIG_IGN);
		setrlimit(RLIMIT_FSIZE, &none);
		keep_als_color();
		keep_als_color();
		setrlimit(RLIMIT_FSIZE, &saved);
	} else if (!strcmp(cmd, "hooks")) {
		run_hooks(atoi(argv[2]));
	} else if (!strcmp(cmd, "read")) {
		char buf[8];
		int fd = open(argv[2], O_RDONLY);
		int r = read_text(fd, buf, sizeof buf);
		printf("read=%d [%s]\n", r, buf);
	}
	return 0;
}
"""


# The daemon as it is, its main() unchanged. It only ends by a signal: SIGTERM writes gcov's
# counters (in a coverage build) before it exits, so the loop's lines are counted.
DAEMON = r"""
#include <signal.h>
#include "plinius-backlight-wake.c"

extern void __gcov_dump(void) __attribute__((weak));

static void on_term(int sig)
{
	if (__gcov_dump)
		__gcov_dump();
	_exit(0);
}

__attribute__((constructor)) static void catch_term(void)
{
	signal(SIGTERM, on_term);
}
"""


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    d = tmp_path_factory.mktemp("blwake")
    sandboxed_source(SRC, d, [
        ("/usr/lib/plinius/display.d", d / "hooks"),
        ("/run/user/*", f"{d}/run-user/*"),
        ("/sys/kernel/pix_manager/als_color", d / "als_color"),
        ("/sys/class/drm/", f"{d}/drm/"),
        ("/sys/class/leds/", f"{d}/leds/"),
    ])
    helpers = build_harness(d, "blwake", HARNESS, pkgs=())
    daemon = build_harness(d, "daemon", DAEMON, pkgs=())
    return d, helpers, daemon


@pytest.fixture
def blw(built):
    d, helpers, daemon = built
    import shutil
    for sub in ("hooks", "run-user", "drm", "leds"):
        shutil.rmtree(d / sub, ignore_errors=True)
    (d / "run-user/1000").mkdir(parents=True)
    (d / "hooks").mkdir()
    (d / "drm/card0-DSI-1").mkdir(parents=True)
    (d / "leds/lcd-backlight").mkdir(parents=True)
    (d / "hook.log").write_text("")
    return d, helpers, daemon


def inotify_event(mask, name=b"", wd=1):
    padded = name + b"\0" * ((16 - len(name) % 16) % 16 if name else 0)
    return struct.pack("iIII", wd, mask, 0, len(padded)) + padded


IN_CREATE, IN_DELETE = 0x100, 0x200
IN_CLOSE_WRITE, IN_MOVED_FROM, IN_MOVED_TO = 0x8, 0x40, 0x80


@pytest.mark.parametrize("events,ended", [
    ([], 0),
    ([inotify_event(IN_CREATE, b"plinius-aod")], 0),                 # shown, not ended
    ([inotify_event(IN_DELETE, b"plinius-aod")], 1),
    ([inotify_event(IN_DELETE, b"plinius-aod-old")], 0),             # another file
    ([inotify_event(IN_DELETE, b"bus"), inotify_event(IN_CREATE, b"x"),
      inotify_event(IN_DELETE, b"plinius-aod")], 1),                 # among others
    ([inotify_event(IN_DELETE)], 0),                                 # no name
])
def test_aod_ended(blw, events, ended):
    d, helpers, daemon = blw
    (d / "events").write_bytes(b"".join(events))
    out = kv(run(helpers, "ended", d / "events").stdout)
    assert out["ended"] == str(ended)
    assert out["als_changed"] == "0"


@pytest.mark.parametrize("events,changed", [
    ([inotify_event(IN_MOVED_TO, b"plinius-als-color")], 1),          # g_file_set_contents
    ([inotify_event(IN_CLOSE_WRITE, b"plinius-als-color")], 1),       # written in place
    ([inotify_event(IN_DELETE, b"plinius-als-color")], 1),            # phoc gone
    ([inotify_event(IN_CREATE, b"plinius-als-color.ABC123"),
      inotify_event(IN_CLOSE_WRITE, b"plinius-als-color.ABC123"),
      inotify_event(IN_MOVED_FROM, b"plinius-als-color.ABC123")], 0),  # its temporary file
    ([inotify_event(IN_CREATE, b"plinius-als-color")], 0),            # created, not written yet
    ([inotify_event(IN_DELETE, b"plinius-aod"),
      inotify_event(IN_MOVED_TO, b"plinius-als-color")], 1),
])
def test_als_file_events(blw, events, changed):
    d, helpers, daemon = blw
    (d / "events").write_bytes(b"".join(events))
    out = kv(run(helpers, "ended", d / "events").stdout)
    assert out["als_changed"] == str(changed)


@pytest.mark.parametrize("text,ok,out", [
    ("A:255R:224G:113B:0", 0, "A:255R:224G:113B:0"),
    ("A:255R:0G:0B:0", 0, "A:255R:0G:0B:0"),
    ("A:255R:007G:0B:0", 0, "A:255R:7G:0B:0"),       # as the driver reads it back
    ("A:255R:256G:0B:0", -1, ""),
    ("A:255R:-1G:-1B:-1", -1, ""),                  # the driver's "unknown" cannot be written
    ("A:255R:1G:2B:3x", -1, ""),
    ("A:255R:1G:2B:3 ", -1, ""),
    ("A:2555R:1G:2B:3", -1, ""),
    ("A:255R:1G:2", -1, ""),
    ("", -1, ""),
])
def test_parse_als_color(blw, text, ok, out):
    d, helpers, daemon = blw
    r = kv(run(helpers, "parse", text).stdout)
    assert r["parse"] == str(ok)
    assert r.get("out", "") == out


def test_als_color_from_phoc(blw):
    d, helpers, daemon = blw
    (d / "als_color").write_text("A:0")
    # No file: the fixed colour
    assert kv(run(helpers, "load").stdout)["color"] == "A:255R:128G:128B:128"
    # phoc's colour, also with a newline
    f = d / "run-user/1000/plinius-als-color"
    f.write_text("A:255R:224G:224B:224\n")
    assert kv(run(helpers, "load").stdout)["color"] == "A:255R:224G:224B:224"
    assert (d / "als_color").read_bytes() == b"A:255R:224G:224B:224"
    # Garbage: the fixed colour again
    f.write_text("white")
    assert kv(run(helpers, "load").stdout)["color"] == "A:255R:128G:128B:128"
    # Not a regular file, or a link: ignored
    f.unlink()
    f.mkdir()
    assert kv(run(helpers, "load").stdout)["color"] == "A:255R:128G:128B:128"
    f.rmdir()
    (d / "elsewhere").write_text("A:255R:1G:1B:1")
    f.symlink_to(d / "elsewhere")
    assert kv(run(helpers, "load").stdout)["color"] == "A:255R:128G:128B:128"


def test_als_color_newest_session_counts(blw):
    d, helpers, daemon = blw
    (d / "run-user/2000").mkdir()
    old = d / "run-user/1000/plinius-als-color"
    new = d / "run-user/2000/plinius-als-color"
    old.write_text("A:255R:10G:10B:10")
    new.write_text("A:255R:20G:20B:20")
    os.utime(old, ns=(2_000_000_000_000_000_000, 2_000_000_000_000_000_000))
    os.utime(new, ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))
    assert kv(run(helpers, "load").stdout)["color"] == "A:255R:10G:10B:10"
    # The newest is broken: the next one counts
    old.write_text("broken")
    os.utime(old, ns=(2_000_000_000_000_000_000, 2_000_000_000_000_000_000))
    assert kv(run(helpers, "load").stdout)["color"] == "A:255R:20G:20B:20"


def test_aod_shown(blw):
    d, helpers, daemon = blw
    assert kv(run(helpers, "shown").stdout)["shown"] == "0"
    (d / "run-user/1000/plinius-aod").write_text("")
    assert kv(run(helpers, "shown").stdout)["shown"] == "1"


def test_als_color_written_without_newline(blw):
    d, helpers, daemon = blw
    (d / "als_color").write_text("A:0")
    run(helpers, "als")
    assert (d / "als_color").read_bytes() == b"A:255R:128G:128B:128"


def test_als_color_left_alone_when_set(blw):
    d, helpers, daemon = blw
    # The driver reads it back with a newline: no write then
    (d / "als_color").write_text("A:255R:128G:128B:128\n")
    run(helpers, "als")
    assert (d / "als_color").read_text() == "A:255R:128G:128B:128\n"


def test_read_text_cuts_at_newline_and_buffer(blw):
    d, helpers, daemon = blw
    (d / "f").write_text("On\nmore")
    assert kv(run(helpers, "read", d / "f").stdout)["read"] == "0 [On]"
    (d / "f").write_text("Standby-long")
    assert kv(run(helpers, "read", d / "f").stdout)["read"] == "0 [Standby]"


def hook(d, name, executable=True):
    h = d / "hooks" / name
    h.write_text(f'#!/bin/sh\necho "{name} $1" >> "{d}/hook.log"\n')
    h.chmod(0o755 if executable else 0o644)


def test_hooks_in_name_order(blw):
    d, helpers, daemon = blw
    hook(d, "20-nfc")
    hook(d, "10-first")
    hook(d, "30-not-executable", executable=False)
    (d / "hooks/40-dir").mkdir()
    run(helpers, "hooks", 1)
    run(helpers, "hooks", 0)
    assert (d / "hook.log").read_text() == "10-first on\n20-nfc on\n10-first off\n20-nfc off\n"


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def set_dpms(path, state):
    """Overwrite in place with the same length: the daemon keeps the file open and must never
    see it half written"""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT)
    try:
        os.pwrite(fd, {"On": b"On\n\n", "Off": b"Off\n"}[state], 0)
    finally:
        os.close(fd)


def test_loop_sets_backlight_and_runs_hooks(blw):
    d, helpers, daemon = blw
    hook(d, "10-log")
    dpms = d / "drm/card0-DSI-1/dpms"
    bright = d / "leds/lcd-backlight/brightness"
    set_dpms(dpms, "Off")
    bright.write_text("100\n")
    (d / "als_color").write_text("A:0")
    log = d / "hook.log"
    env = dict(os.environ, ASAN_OPTIONS="detect_leaks=0")
    p = subprocess.Popen([str(daemon)], env=env, stderr=subprocess.PIPE, text=True)
    try:
        # At start: the current state
        assert wait_for(lambda: log.read_text() == "10-log off\n")
        before = bright.stat().st_mtime_ns
        time.sleep(0.05)
        set_dpms(dpms, "On")
        assert wait_for(lambda: log.read_text() == "10-log off\n10-log on\n")
        # The backlight was written (value - 1, then the value again) and the colour set
        assert bright.stat().st_mtime_ns > before
        assert bright.read_text() == "100\n"
        assert (d / "als_color").read_bytes() == b"A:255R:128G:128B:128"
        # Always-on display shown: counts as off
        (d / "run-user/1000/plinius-aod").write_text("")
        assert wait_for(lambda: log.read_text().endswith("10-log on\n10-log off\n"))
        # Over: on again, backlight set again
        before = bright.stat().st_mtime_ns
        time.sleep(0.05)
        (d / "run-user/1000/plinius-aod").unlink()
        assert wait_for(lambda: log.read_text().endswith("10-log off\n10-log on\n"))
        assert wait_for(lambda: bright.stat().st_mtime_ns > before)
        # Dark on purpose (0): not touched
        bright.write_text("0\n")
        set_dpms(dpms, "Off")
        assert wait_for(lambda: log.read_text().endswith("10-log on\n10-log off\n"))
        before = bright.stat().st_mtime_ns
        set_dpms(dpms, "On")
        assert wait_for(lambda: log.read_text().endswith("10-log off\n10-log on\n"))
        time.sleep(0.6)   # past the settle time after an always-on display
        assert bright.stat().st_mtime_ns == before
        assert p.poll() is None
    finally:
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=5)


def test_loop_follows_phoc_colour(blw):
    d, helpers, daemon = blw
    dpms = d / "drm/card0-DSI-1/dpms"
    bright = d / "leds/lcd-backlight/brightness"
    set_dpms(dpms, "On")
    bright.write_text("100\n")
    (d / "als_color").write_text("A:0")
    f = d / "run-user/1000/plinius-als-color"
    p = start_daemon(daemon)
    try:
        assert wait_for(lambda: (d / "als_color").read_bytes() == b"A:255R:128G:128B:128")
        # Replaced as phoc does it (temporary file, then renamed)
        tmp = d / "run-user/1000/plinius-als-color.XYZ"
        tmp.write_text("A:255R:224G:224B:224")
        tmp.rename(f)
        assert wait_for(lambda: (d / "als_color").read_bytes() == b"A:255R:224G:224B:224")
        # The driver forgot it (display off and on): set again
        (d / "als_color").write_text("A:-1R:-1G:-1B:-1")
        assert wait_for(lambda: (d / "als_color").read_bytes() == b"A:255R:224G:224B:224")
        # phoc gone: the fixed colour
        f.unlink()
        assert wait_for(lambda: (d / "als_color").read_bytes() == b"A:255R:128G:128B:128")
        assert p.poll() is None
    finally:
        stop_daemon(p)


def test_missing_connector_exits(blw):
    d, helpers, daemon = blw
    r = run(daemon, "card9-none", check=False)
    assert r.returncode == 1
    assert "missing" in r.stderr


def test_als_color_refused_is_reported_once(blw):
    d, helpers, daemon = blw
    (d / "als_color").write_text("A:0")
    r = run(helpers, "als-refused")
    # Two tries, one message: the loop would repeat it four times a second
    assert r.stderr.count("plinius-backlight-wake: " + str(d / "als_color")) == 1
    assert "File too large" in r.stderr
    assert (d / "als_color").read_text() == "A:0"


def test_read_text_fails_on_an_unreadable_file(blw):
    d, helpers, daemon = blw
    # A directory opens, but reading it fails (EISDIR)
    assert kv(run(helpers, "read", d / "hooks").stdout)["read"].startswith("-1 ")


def start_daemon(daemon):
    env = dict(os.environ, ASAN_OPTIONS="detect_leaks=0")
    return subprocess.Popen([str(daemon)], env=env, stderr=subprocess.PIPE, text=True)


def stop_daemon(p):
    p.send_signal(signal.SIGTERM)
    _, err = p.communicate(timeout=5)
    return err


def test_loop_reports_a_backlight_it_cannot_set(blw):
    d, helpers, daemon = blw
    hook(d, "10-log")
    dpms = d / "drm/card0-DSI-1/dpms"
    bright = d / "leds/lcd-backlight/brightness"
    set_dpms(dpms, "Off")
    bright.write_text("100\n")
    bright.chmod(0o444)
    log = d / "hook.log"
    p = start_daemon(daemon)
    try:
        assert wait_for(lambda: log.read_text() == "10-log off\n")
        set_dpms(dpms, "On")
        assert wait_for(lambda: log.read_text() == "10-log off\n10-log on\n")
        assert p.poll() is None
    finally:
        err = stop_daemon(p)
    assert err.count("plinius-backlight-wake: brightness: Permission denied") == 1
    assert bright.read_text() == "100\n"


def test_loop_with_unreadable_display_state_does_nothing(blw):
    d, helpers, daemon = blw
    hook(d, "10-log")
    # The connector's state cannot be read (here a directory): counted as off at start, and the
    # loop goes on polling without acting
    (d / "drm/card0-DSI-1/dpms").mkdir()
    bright = d / "leds/lcd-backlight/brightness"
    bright.write_text("100\n")
    before = bright.stat().st_mtime_ns
    log = d / "hook.log"
    p = start_daemon(daemon)
    try:
        assert wait_for(lambda: log.read_text() == "10-log off\n")
        time.sleep(0.6)
        assert p.poll() is None
    finally:
        stop_daemon(p)
    assert log.read_text() == "10-log off\n"
    assert bright.stat().st_mtime_ns == before
