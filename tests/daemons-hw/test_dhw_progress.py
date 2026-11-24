# plinius-progress (the initramfs' progress and message view, in unl0kr's look): the command line,
# the configuration files in unl0kr's order, the progress bar fed by cryptsetup's JSON lines, the
# buttons and their exit status, the timeout, the boot logo tinted with the accent colour and its
# turn, and the handover of the display to the compositor on SIGUSR1 (DRM master dropped, the
# CRTCs watched until the compositor's framebuffer replaced ours, the backlight written again).
#
# LVGL and BuffyBox's shared code are stand-ins (progress/fake_progress.[ch]) that record the
# objects the program builds; they are written out as JSON when it exits. The DRM device, the
# handover's sleeps (a fake clock) and pwrite on the backlight are faked as well; the backlight
# itself is a file in tmp. The program's own code runs unchanged.

import json
import os
import resource
import signal
import shutil
import struct
import subprocess
import time

import pytest

from conftest import GCOV_DIR
from dhw_harness import HERE, PKG, build_harness, sandboxed_source

SRC = PKG / "plinius-progress" / "plinius-progress.c"
FAKES = HERE / "progress"

FORWARDED = ["unl0kr/config.h", "lvgl/lvgl.h"] + [f"shared/{n}.h" for n in (
    "background", "backends", "cli_common", "display", "indev", "log", "terminal", "theme",
    "themes")]

HARNESS = r"""
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <inttypes.h>
#include <limits.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/epoll.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <time.h>
#include <unistd.h>
#include <xf86drm.h>
#include <xf86drmMode.h>
#include "fake_progress.h"

int fake_fstat(int fd, struct stat *st);
int fake_usleep(useconds_t us);
ssize_t fake_pwrite(int fd, const void *buf, size_t n, off_t offset);
ssize_t fake_read(int fd, void *buf, size_t n);
int fake_epoll_wait(int epfd, struct epoll_event *events, int max, int timeout);
void fake_exit_now(int status) __attribute__((noreturn));
#define fstat(fd, st) fake_fstat(fd, st)
#define usleep(us) fake_usleep(us)
#define pwrite(...) fake_pwrite(__VA_ARGS__)
#define read(...) fake_read(__VA_ARGS__)
#define epoll_wait(...) fake_epoll_wait(__VA_ARGS__)
#define _exit(s) fake_exit_now(s)
#include "plinius-progress/plinius-progress.c"
#undef fstat
#undef usleep
#undef pwrite
#undef read
#undef epoll_wait
#undef _exit
#include "fake_progress.c"
"""

ARCH_BLUE = (0x17, 0x93, 0xd1)
ASAN = {"ASAN_OPTIONS": "detect_leaks=0:abort_on_error=0:exitcode=86",
        "UBSAN_OPTIONS": "print_stacktrace=1:halt_on_error=1:exitcode=87", "LC_ALL": "C"}


@pytest.fixture(scope="module")
def exe(tmp_path_factory):
    if shutil.which("pkg-config") is None:
        pytest.skip("no pkg-config")
    r = subprocess.run(["pkg-config", "--cflags", "libdrm"], capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip("libdrm headers missing on the host")
    d = tmp_path_factory.mktemp("progress")
    (d / "plinius-progress").mkdir()
    # Only noted by the fake configuration, never read
    sandboxed_source(SRC, d / "plinius-progress", [], allow=("/usr/share/unl0kr/", "/etc/unl0kr"))
    for h in FORWARDED:
        (d / h).parent.mkdir(parents=True, exist_ok=True)
        (d / h).write_text('#include "fake_progress.h"\n')
    return build_harness(d, "progress", HARNESS, pkgs=(), fakes=False,
                         flags=[f"-I{FAKES}", *r.stdout.split()])


class Result:
    def __init__(self, proc, out, err, tmp):
        self.code, self.out, self.err = proc.returncode, out, err
        log = tmp / "log"
        self.log = log.read_text().splitlines() if log.exists() else []
        tree = tmp / "tree.json"
        self.objects = json.loads(tree.read_text()) if tree.exists() else None
        self.tmp = tmp

    def lines(self, prefix):
        return [line[len(prefix):].strip() for line in self.log
                if line == prefix or line.startswith(prefix + " ")]

    def of_type(self, kind):
        return [o for o in self.objects if o["type"] == kind]

    def children(self, parent):
        return [o for o in self.objects if o["parent"] == parent["id"]]

    def image_data(self, obj):
        return (self.tmp / f"tree.json.{obj['id']}.bin").read_bytes()


def environment(tmp, extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("FAKE_")}
    env.update(ASAN)
    env.update({"FAKE_LOG": str(tmp / "log"), "FAKE_TREE": str(tmp / "tree.json")})
    env.update({k: str(v) for k, v in extra.items()})
    return env


@pytest.fixture
def progress(exe, tmp_path):
    """Runs the program to its end: stdin from `stdin` (bytes, written then closed)."""
    counter = iter(range(1000))

    def start(*args, stdin=b"", env=None, stdin_file=None, preexec=None, timeout=20):
        tmp = tmp_path / f"run{next(counter)}"
        tmp.mkdir()
        p = subprocess.Popen([str(exe), *map(str, args)], stdin=stdin_file or subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env=environment(tmp, env or {}), preexec_fn=preexec)
        out, err = p.communicate(None if stdin_file else stdin,
                                 timeout=timeout * (5 if GCOV_DIR else 1))
        return Result(p, out.decode(), err.decode(), tmp)
    return start


@pytest.fixture
def started(exe, tmp_path):
    """Starts the program and waits until its main loop runs (for signals)."""
    procs = []

    def start(*args, env=None):
        tmp = tmp_path / f"sig{len(procs)}"
        tmp.mkdir()
        p = subprocess.Popen([str(exe), *map(str, args)], stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env=environment(tmp, env or {}))
        procs.append(p)
        before = b""
        while (line := p.stderr.readline()) != b"LOOP\n":
            assert line, f"ended before its main loop: {before.decode()}"
            before += line

        def finish(sig):
            p.send_signal(sig)
            out, err = p.communicate(timeout=20 * (5 if GCOV_DIR else 1))
            return Result(p, out.decode(), (before + err).decode(), tmp)
        return finish
    yield start
    for p in procs:
        if p.poll() is None:
            p.kill()
            p.wait()


def config(tmp_path, name="extra.conf", **keys):
    f = tmp_path / name
    f.write_text("".join(f"{k}={v}\n" for k, v in keys.items()))
    return f


# --- Command line and configuration ---------------------------------------------------------------

def test_help_and_version(progress):
    r = progress("-h")
    assert r.code == 0 and r.err.startswith("Usage: plinius-progress [OPTION]")
    assert "-H, --handover" in r.err
    r = progress("--version")
    assert r.code == 0 and r.out == "plinius-progress fake-version\n"


@pytest.mark.parametrize("args,message", [
    (["-x"], "Usage: plinius-progress"),
    (["-g", "big"], "Invalid geometry big"),
    (["--dpi", "0"], "Invalid dpi 0"),
])
def test_bad_options_fail(progress, args, message):
    r = progress(*args)
    assert r.code == 1 and message in r.err
    # Nothing was shown
    assert "DISPLAY" not in "".join(r.log)


def test_configuration_in_unl0krs_order_and_display_options(progress, tmp_path):
    a = config(tmp_path, "a.conf", backend=3, fbdev_force_refresh="true", prevent_graphics="true")
    b = config(tmp_path, "b.conf", allow_keyboard="true")
    r = progress("-p", "-C", a, "--config-override", b, "-g", "720x1440@10,20", "-d", "300", "-v")
    assert r.code == 0, r.err
    assert [line for line in r.log if line.startswith("CONFIG")] == [
        "CONFIG_FILE /usr/share/unl0kr/unl0kr.conf",
        "CONFIG_DIR /usr/share/unl0kr/unl0kr.conf.d",
        "CONFIG_FILE /etc/unl0kr.conf",
        "CONFIG_DIR /etc/unl0kr.conf.d",
        f"CONFIG_OVERRIDE {a}",
        f"CONFIG_OVERRIDE {b}",
    ]
    assert r.lines("LOG_LEVEL") == ["2"]
    assert r.lines("LOG_CB") == ["bbx"]
    assert r.lines("DISPLAY") == ["backend=3 hor=720 ver=1440 x=10 y=20 dpi=300 force_refresh=1"]
    # The quirks turned around: graphics mode off, keyboard input left on
    assert r.lines("TERMINAL_PREPARE") == ["graphics=0 no_keyboard=0"]
    # The terminal is restored when it exits
    assert r.log[-1] == "TERMINAL_RESET"


def test_defaults_without_options(progress):
    r = progress("-p")
    assert r.lines("DISPLAY") == ["backend=0 hor=-1 ver=-1 x=0 y=0 dpi=0 force_refresh=0"]
    assert r.lines("TERMINAL_PREPARE") == ["graphics=1 no_keyboard=1"]
    assert r.lines("LOG_LEVEL") == []
    # No button: no input devices (the power key must not shut down during the encryption)
    assert r.lines("INDEV") == []


# --- Progress bar ----------------------------------------------------------------------------------

def cryptsetup_line(done, size, eta=None):
    fields = {"time_ms": "1500", "device_bytes": str(done), "device_size": str(size),
              "speed": "52428800"}
    if eta is not None:
        fields["eta_ms"] = str(eta)
    return json.dumps(fields, separators=(",", ":"))


def expected_label(done, size, eta, template):
    permille = done * 1000 // size
    text = f"{permille // 10} %"
    if template and eta:
        minutes = (eta + 59999) // 60000
        text += "  ·  " + template.replace("%s", str(minutes), 1)
    return permille, text


def test_progress_bar_follows_cryptsetup(progress):
    template = "%s min left"
    steps = [(268435456, 1073741824, 120000), (300000000, 1073741824, 60001),
             (999, 1000, 1), (1000, 1000, 0), (512, 1024, None)]
    lines = [cryptsetup_line(*s) for s in steps]
    r = progress("-p", "-r", template, stdin=("\n".join(lines) + "\n").encode())
    assert r.code == 0, r.err
    want = [expected_label(*s, template) for s in steps]
    assert r.lines("BAR") == [f"{p} no-anim" for p, _ in want]
    label = r.of_type("label")[-1]
    texts = [line.split(" ", 1)[1] for line in r.lines("TEXT") if line.split(" ")[0] == str(label["id"])]
    assert texts == ["0 %"] + [t for _, t in want]
    # At the end of the input: the last frame drawn, the terminal restored
    assert r.log[-2:] == ["REFRESH", "TERMINAL_RESET"]


def test_progress_ignores_other_lines_and_overlong_ones(progress):
    good = cryptsetup_line(1, 4, 0)
    stdin = "\n".join([
        "Progress: 10.0%, ETA 00:12",                      # not JSON
        cryptsetup_line(5, 0),                             # no size yet
        '{"device_bytes":"5"}',                            # half a line
        cryptsetup_line(3, 4, 0) + " " * 1100,             # longer than the line buffer: dropped
        good,
    ]) + "\n"
    r = progress("-p", stdin=stdin.encode())
    assert r.code == 0
    assert r.lines("BAR") == ["250 no-anim"]


def test_line_split_across_reads(exe, tmp_path):
    env = environment(tmp_path, {})
    p = subprocess.Popen([str(exe), "-p"], stdin=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    line = cryptsetup_line(7, 10, 0).encode() + b"\n"
    p.stdin.write(line[:20])
    p.stdin.flush()
    time.sleep(0.2)
    p.stdin.write(line[20:])
    p.stdin.close()
    assert p.wait(timeout=20) == 0
    log = (tmp_path / "log").read_text().splitlines()
    assert [x for x in log if x.startswith("BAR")] == ["BAR 700 no-anim"]


def test_remaining_text_is_not_a_format(progress):
    # Texts come from the caller: no printf format from outside, only the first %s is replaced
    r = progress("-p", "-r", "%n%x %s left, %s", stdin=(cryptsetup_line(1, 2, 90000) + "\n").encode())
    assert r.code == 0
    assert r.of_type("label")[-1]["text"] == "50 %  ·  %n%x 2 left, %s"
    r = progress("-p", "-r", "a while", stdin=(cryptsetup_line(1, 2, 90000) + "\n").encode())
    assert r.of_type("label")[-1]["text"] == "50 %  ·  a while"


def test_animated_bar_when_configured(progress, tmp_path):
    r = progress("-p", "-C", config(tmp_path, animations="true"),
                 stdin=(cryptsetup_line(1, 2) + "\n").encode())
    assert r.lines("BAR") == ["500 anim"]


def test_a_read_that_would_block_is_retried(progress):
    r = progress("-p", stdin=(cryptsetup_line(1, 2) + "\n").encode(), env={"FAKE_READ_EAGAIN": 1})
    assert r.code == 0
    assert r.lines("READ") == ["EAGAIN"]
    assert r.lines("BAR") == ["500 no-anim"]


# --- The view --------------------------------------------------------------------------------------

def styles(obj):
    return obj["styles"]


@pytest.mark.parametrize("res,padding,side", [
    ("1080x2400", 96, 96),        # portrait: ver / 2.5 / 10
    ("2400x1080", 60, 660 + 60),  # landscape: ver / 1.8 / 10, a centred portrait column
])
def test_layout_like_unl0kr(progress, res, padding, side):
    r = progress("-t", "Encrypting", "-m", "Do not switch off", "-i", "warning", "-p",
                 env={"FAKE_RES": res})
    screen = r.of_type("screen")[0]
    s = styles(screen)
    assert (s["pad_left/main"], s["pad_right/main"], s["pad_row/main"]) == (side, side, padding)
    assert s["flex_flow/main"] == 1 and s["removed_flags/main"] == 1 << 4
    assert r.lines("THEME_APPLY_OBJ") == [str(screen["id"])]
    kids = r.children(screen)
    assert [k["type"] for k in kids] == ["label", "label", "bar", "label", "label"]
    icon, title, bar, percent, message = kids
    assert icon["text"] == ""  # LV_SYMBOL_WARNING
    assert styles(icon)["text_color/main"] == "#f67400"
    assert title["text"] == "Encrypting"
    for label in (icon, title, percent, message):
        assert label["width"] == "100%" and styles(label)["text_align/main"] == 2
    assert percent["text"] == "0 %"
    assert message["text"] == "Do not switch off" and styles(message)["text_opa/main"] == 204
    assert bar["range"] == [0, 1000]
    assert (bar["width"], bar["height"]) == ("100%", 48)
    b = styles(bar)
    # The theme's bar colours (the fake theme: border 2, #123456, radius 5, accent #3584e4)
    assert b == {"bg_opa/main": 0, "border_width/main": 4, "border_color/main": "#123456",
                 "radius/main": 10, "pad_all/main": 6, "bg_opa/indicator": 255,
                 "bg_color/indicator": "#3584e4", "radius/indicator": 10,
                 "anim_duration/main": 800}
    assert r.lines("BACKGROUND") == []


def test_ok_icon_glass_theme_accent_and_background(progress, tmp_path):
    conf = config(tmp_path, theme=1, accent="ff8800", background="/boot/bg.png")
    r = progress("-i", "ok", "-p", "-C", conf)
    screen = r.of_type("screen")[0]
    icon = r.children(screen)[0]
    assert icon["text"] == ""  # LV_SYMBOL_OK
    assert styles(icon)["text_color/main"] == "#ff8800"
    assert r.lines("THEME") == ["id=1 accent=ff8800"]
    assert r.lines("BACKGROUND") == [f"{screen['id']} /boot/bg.png"]
    bar = r.of_type("bar")[0]
    # A glass surface as the track
    assert styles(bar)["bg_color/main"] == "#ffffff" and styles(bar)["bg_opa/main"] == 40
    assert styles(bar)["bg_color/indicator"] == "#ff8800"


def test_message_view_stays_until_terminated(started):
    finish = started("-t", "Please wait", "-T", "1")
    time.sleep(1.3)
    r = finish(signal.SIGTERM)
    assert r.code == 0
    # -T only counts with a button or the logo
    assert r.lines("TIMER") == []
    assert [o["type"] for o in r.objects] == ["screen", "label"]
    assert r.log[-1] == "TERMINAL_RESET"


# --- Buttons ---------------------------------------------------------------------------------------

def test_one_button(progress, tmp_path):
    conf = config(tmp_path, keyboard="false", haptic="true", layout="de")
    r = progress("-t", "Done", "-b", "Continue", "-C", conf, env={"FAKE_PRESS": 1})
    assert r.code == 0, r.err
    assert r.lines("INDEV") == ["keyboard=0 pointer=1 touchscreen=1 feedback=1 keymap=de"]
    button = r.of_type("button")[0]
    assert button["focused"] and button["in_group"] and button["clickable"]
    s = styles(button)
    assert (s["pad_hor/main"], s["pad_ver/main"]) == (96, 48)
    # The keyboard focus is outlined in the accent colour
    assert (s["outline_width/focus_key"], s["outline_pad/focus_key"], s["outline_color/focus_key"]) == \
        (6, 6, "#3584e4")
    assert "width" not in button and "bg_color/main" not in s
    label = r.children(button)[0]
    assert label["text"] == "Continue" and label["centered"]
    assert r.lines("PRESS") == [str(button["id"])]
    assert r.log[-1] == "TERMINAL_RESET"


@pytest.mark.parametrize("press,status", [(1, 0), (2, 2)])
def test_two_buttons(progress, press, status):
    r = progress("-b", "Restart", "-B", "Rescue system", env={"FAKE_PRESS": press})
    assert r.code == status
    first, second = r.of_type("button")
    assert first["width"] == second["width"] == "100%"
    # The first one filled, as the default, and focused
    assert styles(first)["bg_color/main"] == styles(first)["border_color/main"] == "#3584e4"
    assert styles(first)["bg_opa/main"] == 255
    assert "bg_color/main" not in styles(second)
    assert first["focused"] and not second["focused"]
    assert [r.children(b)[0]["text"] for b in (first, second)] == ["Restart", "Rescue system"]


def test_button_times_out(progress):
    t0 = time.monotonic()
    r = progress("-b", "OK", "-T", "1")
    assert r.code == 0
    assert time.monotonic() - t0 >= 1
    assert r.lines("TIMER") == ["1000"] and r.lines("TIMER_FIRED") == ["1000"]


def test_no_input_devices_fails(progress):
    r = progress("-b", "OK", env={"FAKE_INDEV": 0})
    assert r.code == 1
    assert r.log[-1] == "TERMINAL_RESET"
    assert r.objects == []


# --- Failures --------------------------------------------------------------------------------------

def test_no_display(progress):
    r = progress("-p", env={"FAKE_NO_DISPLAY": 1})
    assert r.code == 1 and r.log[-1] == "TERMINAL_RESET"


def test_stdin_that_cannot_be_watched(progress, tmp_path):
    # epoll refuses regular files
    f = tmp_path / "input"
    f.write_text(cryptsetup_line(1, 2) + "\n")
    with open(f) as stdin:
        r = progress("-p", stdin_file=stdin)
    assert r.code == 1 and "Cannot watch stdin" in r.err


def no_spare_fds():
    hard = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    resource.setrlimit(resource.RLIMIT_NOFILE, (4, hard))


def test_no_file_descriptor_for_epoll(progress):
    # 0-2 and the fake's log are open: epoll_create1 gets none
    r = progress("-p", preexec=no_spare_fds)
    assert r.code == 1 and "epoll_create1() failed" in r.err


def test_epoll_wait_failing(progress):
    r = progress("-p", env={"FAKE_EPOLL_FAIL": 1})
    assert r.code == 1 and "epoll_wait() failed" in r.err
    assert r.log[-1] == "TERMINAL_RESET"


# --- Boot logo -------------------------------------------------------------------------------------

def lvgl_image(w, h, pixels, stride=None, magic=0x19, cf=0x10, extra=b""):
    """An LVGL 9 image file: the 12-byte header (magic, cf, flags; w, h; stride) and the rows;
    pixels are (r, g, b, a) row by row, stored as B, G, R, A."""
    stride = stride or w * 4
    header = struct.pack("<BBHHHHH", magic, cf, 0, w, h, stride, 0)
    rows = b""
    for y in range(h):
        row = b"".join(bytes((b, g, r_, a)) for r_, g, b, a in pixels[y * w:(y + 1) * w])
        rows += row + b"\xee" * (stride - len(row))
    return header + rows + extra


def tint(pixels, accent, w, stride):
    """phoc's tint: white mixed with the logo's blue by its red channel, the same mix with the accent"""
    ar, ag, ab = accent >> 16 & 0xff, accent >> 8 & 0xff, accent & 0xff
    out = b""
    for y in range(len(pixels) // w):
        row = b""
        for r_, g, b, a in pixels[y * w:(y + 1) * w]:
            if a:
                blue = 255 - r_
                r_, g, b = (255 - blue * (255 - ar) // 255, 255 - blue * (255 - ag) // 255,
                            255 - blue * (255 - ab) // 255)
            row += bytes((b, g, r_, a))
        out += row + b"\xee" * (stride - len(row))
    return out


MARK = [(*ARCH_BLUE, 255), (255, 255, 255, 255), (0, 0, 0, 0), (*ARCH_BLUE, 128),
        (0, 0x93, 0xd1, 255), (10, 20, 30, 0)]


@pytest.fixture
def logo(tmp_path):
    d = tmp_path / "logo"
    d.mkdir()
    (d / "mark.bin").write_bytes(lvgl_image(3, 2, MARK, stride=16))
    (d / "wordmark.bin").write_bytes(lvgl_image(100, 1, [(*ARCH_BLUE, 255)] * 100))
    return d


def test_logo_tinted_with_the_accent(progress, logo, tmp_path):
    conf = config(tmp_path, accent="ff8800")
    r = progress("-l", logo, "-T", "1", "-C", conf)
    assert r.code == 0, r.err
    screen = r.of_type("screen")[0]
    s = styles(screen)
    assert (s["bg_color/main"], s["bg_opa/main"], s["pad_row/main"], s["pad_all/main"]) == \
        ("#000000", 255, 0, 0)
    assert s["flex_flow/main"] == 1 and s["removed_flags/main"] == 1 << 4
    mark, wordmark = r.of_type("image")
    assert mark["image"] == {"w": 3, "h": 2, "stride": 16, "cf": 0x10, "size": 32}
    assert r.image_data(mark) == tint(MARK, 0xff8800, 3, 16)
    assert r.image_data(wordmark) == tint([(*ARCH_BLUE, 255)] * 100, 0xff8800, 100, 400)
    # White stays white, the blue takes the accent
    assert r.image_data(mark)[4:8] == bytes((255, 255, 255, 255))
    assert r.image_data(mark)[20:24] == bytes((0x00, 0x88, 0xff, 255))
    # No label without -m, no view of the progress kind
    assert r.of_type("label") == [] and r.lines("BACKGROUND") == []
    assert r.lines("ANIM") == [f"obj={mark['id']} values=0..359 duration=2400 repeat=infinite"]
    assert r.lines("TIMER") == ["1000"]


def test_logo_step_below_the_wordmark(started, logo):
    r = started("-l", logo, "-m", "Starting the system")(signal.SIGTERM)
    assert r.code == 0
    _, wordmark = r.of_type("image")
    step = r.of_type("label")[0]
    assert step["text"] == "Starting the system"
    # Outside the layout, so that the logo stays where phoc draws its own
    assert step["flags"] & (1 << 17)
    assert styles(step)["text_color/main"] == "#ffffff" and styles(step)["text_opa/main"] == 153
    assert r.of_type("screen")[0]["layout_updated"]
    # The gap: 12/220 of the wordmark's width
    assert (step["align_to"], step["align"], step["align_x"], step["align_y"]) == \
        (wordmark["id"], 17, 0, int(100 * 12 / 220 + 0.5))


def lvgl_cos(angle):
    """|cos| as LVGL's table gives it (round(32767 sin), ±32767 as ±32768)"""
    import math
    v = round(32767 * abs(math.cos(math.radians(angle))))
    return 32768 if v == 32767 else v


def test_logo_turns_like_a_coin(started, logo):
    angles = [0, 30, 45, 89, 90, 135, 180, 270, 300, 359]
    r = started("-l", logo, env={"FAKE_ANIM_ANGLES": ",".join(map(str, angles))})(signal.SIGTERM)
    want = []
    for angle in angles:
        turn = lvgl_cos(angle)
        # Narrowed to a sliver edge-on, never to nothing; darker while turned away
        want.append(f"{angle} scale_x={max(turn * 256 // 32768, 3)} opa={140 + (255 - 140) * turn // 32768}")
    assert r.lines("TURN") == want
    assert want[0] == "0 scale_x=256 opa=255" and want[4] == "90 scale_x=3 opa=140"


@pytest.mark.parametrize("broken", [
    "missing", "short", "magic", "format", "width", "stride", "truncated",
])
def test_logo_images_that_are_not_shown(progress, logo, broken):
    target = logo / ("mark.bin" if broken in ("missing", "magic") else "wordmark.bin")
    good = [(*ARCH_BLUE, 255)] * 4
    data = {"missing": None, "short": b"\x19\x10",
            "magic": lvgl_image(2, 2, good, magic=0x18), "format": lvgl_image(2, 2, good, cf=0x0f),
            "width": lvgl_image(0, 2, []), "stride": lvgl_image(2, 2, good, stride=7)[:12],
            "truncated": lvgl_image(2, 2, good)[:-1]}[broken]
    if data is None:
        target.unlink()
    else:
        target.write_bytes(data)
    r = progress("-l", logo, "-T", "1")
    assert r.code == 1
    if broken == "missing":
        assert f"Cannot open {target}" in r.err
    else:
        assert f"{target} is not an ARGB8888 LVGL image" in r.err
    assert r.log[-1] == "TERMINAL_RESET"
    assert r.of_type("image") == []


# --- Handover of the display -----------------------------------------------------------------------

DRM = {"FAKE_DRM_FD": 9, "FAKE_RENDER_FD": 5, "FAKE_CRTCS": "31:5,32:0,33:7:gone,34:missing",
       "FAKE_REPLACED_AT_MS": 350}


def handed(r):
    return [line.removeprefix("plinius-progress: ") for line in r.err.splitlines()
            if line.startswith("plinius-progress: ")]


def test_handover_keeps_the_picture_until_the_compositor_shows_its_own(started, logo, tmp_path):
    backlight = tmp_path / "brightness"
    backlight.write_text("120\n")
    finish = started("-l", logo, "-H", "-L", backlight, env=DRM)
    r = finish(signal.SIGUSR1)
    assert r.code == 0, r.err
    mark = r.of_type("image")[0]
    # The mark turned to the front before the last frame
    assert r.lines("ANIM_DELETE") == [f"obj={mark['id']} stopped"]
    assert r.lines("REFRESH") == ["mark scale_x=256 opa=255"]
    # The card, not the render node
    assert r.lines("DROP_MASTER") == ["fd=9"]
    # 31 and 33 scan out the logo; 32 shows nothing, 34 cannot be read. The compositor's frame
    # comes at 350 ms of the clock, polled every 100 ms after the 50 ms for the page flip
    assert handed(r) == ["display handed over, 2 CRTC(s) showing the logo",
                         "replaced by the compositor after 300 ms",
                         "backlight set again (120)"]
    # One below first: the LED core skips a write of the value it has
    assert r.lines("PWRITE") == ["119@0", "120@0"]
    assert backlight.read_text() == "120\n"
    assert r.lines("_EXIT") == ["0"]
    # Its name tells the sender of the signal that the display is free
    assert r.lines("COMM") == ["logo-handed"]
    # The terminal keeps its graphics mode: it belongs to the session by now
    assert "TERMINAL_RESET" not in r.log


def test_compositor_that_never_comes(started, logo, tmp_path):
    backlight = tmp_path / "brightness"
    backlight.write_text("120\n")
    r = started("-l", logo, "-H", "-L", backlight, env={**DRM, "FAKE_REPLACED_AT_MS": -1})(signal.SIGUSR1)
    assert r.code == 0
    assert handed(r) == ["display handed over, 2 CRTC(s) showing the logo",
                         "not replaced within 30000 ms"]
    # Not replaced: the backlight is not touched
    assert r.lines("PWRITE") == []


@pytest.mark.parametrize("value,writes", [("0\n", ["1@0", "0@0"]), ("7", ["6@0", "7@0"])])
def test_backlight_values(started, logo, tmp_path, value, writes):
    backlight = tmp_path / "brightness"
    backlight.write_text(value)
    r = started("-l", logo, "-H", "-L", backlight, env=DRM)(signal.SIGUSR1)
    assert r.lines("PWRITE") == writes


def test_empty_backlight_file_is_left_alone(started, logo, tmp_path):
    backlight = tmp_path / "brightness"
    backlight.write_text("")
    r = started("-l", logo, "-H", "-L", backlight, env=DRM)(signal.SIGUSR1)
    assert r.code == 0 and r.lines("PWRITE") == []
    assert "backlight" not in r.err


def test_backlight_that_cannot_be_written(started, logo, tmp_path):
    backlight = tmp_path / "brightness"
    backlight.write_text("50")
    r = started("-l", logo, "-H", "-L", backlight, env={**DRM, "FAKE_PWRITE_FAIL": 1})(signal.SIGUSR1)
    assert r.code == 0
    assert "plinius-progress: cannot set the backlight again: Input/output error" in r.err


def test_backlight_that_cannot_be_opened(started, logo, tmp_path):
    missing = tmp_path / "no" / "brightness"
    r = started("-l", logo, "-H", "-L", missing, env=DRM)(signal.SIGUSR1)
    assert r.code == 0
    assert f"plinius-progress: cannot open {missing}: No such file or directory" in r.err
    assert "replaced by the compositor after 300 ms" in r.err
    assert r.lines("PWRITE") == []


def test_backlight_only_with_handover(progress, logo, tmp_path):
    # -L without -H is not opened at all
    r = progress("-l", logo, "-L", tmp_path / "no" / "brightness", "-T", "1")
    assert r.code == 0 and "cannot open" not in r.err


def test_handover_without_drm(started, logo):
    r = started("-l", logo, "-H")(signal.SIGUSR1)
    assert r.code == 0
    assert "plinius-progress: cannot give up DRM master (fd -1: no DRM device), exiting" in r.err
    assert r.lines("DROP_MASTER") == []


def test_handover_when_not_master(started, logo):
    import errno
    r = started("-l", logo, "-H", env={**DRM, "FAKE_DROP_MASTER_ERRNO": errno.EACCES})(signal.SIGUSR1)
    assert r.code == 0
    assert "plinius-progress: cannot give up DRM master (fd 9: Permission denied), exiting" in r.err
    assert "display handed over" not in r.err


def test_handover_without_crtc_resources(started, logo, tmp_path):
    backlight = tmp_path / "brightness"
    backlight.write_text("3")
    r = started("-l", logo, "-H", "-L", backlight, env={**DRM, "FAKE_NO_RESOURCES": 1})(signal.SIGUSR1)
    assert handed(r)[:2] == ["display handed over, 0 CRTC(s) showing the logo",
                             "replaced by the compositor after 0 ms"]


def test_sigusr1_needs_handover(started, logo):
    # Without -H the signal is not ours: it ends the program as signals do
    r = started("-l", logo, env=DRM)(signal.SIGUSR1)
    assert r.code == -signal.SIGUSR1


def test_logo_ends_on_sigterm(started, logo):
    r = started("-l", logo, "-H", env=DRM)(signal.SIGTERM)
    assert r.code == 0
    assert r.lines("DROP_MASTER") == [] and r.log[-1] == "TERMINAL_RESET"
