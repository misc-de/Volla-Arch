# camera-hal-bridge as a whole: what it asks of the Camera2 NDK (which camera opens, the preview
# and still streams, the settings in each request), what it gives PipeWire (nodes, formats,
# frames turned upright, the clock while driving), what it says on the bus (Active, the
# settings, TakePicture's answers and JPEGs) and how it gets through what fails on the way.
#
# The bridge's own main runs in camera_bridge_harness.c: the NDK is a fake behind
# fakes/hybris/common/dlfcn.h (FAKE_HYBRIS_HOOKS), PipeWire's stream, context and registry are
# wrapped, iio-sensor-proxy and plinius-appaccess are served by the harness itself. Every bus is
# a private dbus-daemon of the test (system and session bus point to it), never a real one.

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest

from conftest import GCOV_DIR
from dhw_harness import PKG, build_harness, kv, run, sandboxed_source

SRC = PKG / "camera-hal-bridge" / "camera-hal-bridge.c"
HARNESS = Path(__file__).resolve().parent / "camera_bridge_harness.c"
SLOW = 5 if GCOV_DIR else 1
BRIDGE = "org.plinius.CameraBridge"
BRIDGE_PATH = "/org/plinius/CameraBridge"

WRAPPED = ["pw_stream_new_simple", "pw_stream_connect", "pw_stream_update_params", "pw_stream_destroy",
           "pw_stream_get_node_id", "pw_stream_is_driving", "pw_stream_get_nsec",
           "pw_stream_trigger_process", "pw_stream_get_data_loop", "pw_stream_dequeue_buffer",
           "pw_stream_queue_buffer", "pw_context_new", "pw_context_connect", "pw_core_disconnect",
           "pw_context_destroy", "pw_proxy_add_object_listener", "pw_proxy_destroy",
           "write", "read", "strdup", "sd_bus_creds_get_pid"]

# Shorter waits than on the phone, the same logic: retries, the photo and close timeouts, the
# front port poll and the wait for the HAL's camera list
TIMINGS = [
    ("#define CAMERA_WAIT_US 500000", "#define CAMERA_WAIT_US 2000"),
    ("#define CAPTURE_TIMEOUT_S 12 ", "#define CAPTURE_TIMEOUT_S 1 "),
    ("#define SESSION_CLOSE_TIMEOUT_S 5", "#define SESSION_CLOSE_TIMEOUT_S 1"),
    ("#define OPEN_RETRY_FAST_MS 250", "#define OPEN_RETRY_FAST_MS 100"),
    ("#define OPEN_RETRY_SLOW_MS 1000", "#define OPEN_RETRY_SLOW_MS 20"),
    ("#define FRONT_POLL_MS 500", "#define FRONT_POLL_MS 20"),
]

# The phone's cameras by HAL id (0-3 have a model entry), and some without an entry
CAMERAS = "0:b:90,1:f:270,2:b:90:n,3:b:90:s"

# The process the harness gives the bridge as the caller of a bus method
CALLER = 4321


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    d = tmp_path_factory.mktemp("camera-bridge")
    sandboxed_source(SRC, d, [(
        '"/sys/devices/platform/soc/1a00e000.seninf-top/1a00e000.seninf-top:seninf-csi-port-1/"',
        f'"{d}/front-port/"'), ('#define PROC_DIR "/proc/"', f'#define PROC_DIR "{d}/proc/"'), *TIMINGS])
    exe = build_harness(d, "camera_bridge", HARNESS.read_text(),
                        flags=[f"-I{SRC.parent}", "-DFAKE_HYBRIS_HOOKS",
                               *(f"-Wl,--wrap={f}" for f in WRAPPED)],
                        pkgs=("libpipewire-0.3", "libsystemd"))
    return d, exe


@pytest.fixture
def bus():
    """A private dbus-daemon without service files (nothing is ever activated); its socket in a
    short directory, a unix socket path has to fit 108 bytes."""
    if shutil.which("dbus-daemon") is None or shutil.which("gdbus") is None:
        pytest.skip("no dbus-daemon/gdbus")
    with tempfile.TemporaryDirectory(prefix="chb") as d:
        conf = Path(d) / "bus.conf"
        conf.write_text(f"""<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:path={d}/bus</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
""")
        p = subprocess.Popen(["dbus-daemon", f"--config-file={conf}", "--nofork", "--print-address"],
                             stdout=subprocess.PIPE, text=True)
        address = p.stdout.readline().strip()
        assert address.startswith("unix:"), address
        try:
            yield address
        finally:
            p.terminate()
            p.wait(timeout=10)


class Bridge:
    """The bridge's main in the harness, driven by commands on stdin; its stdout and stderr are
    collected line by line."""

    def __init__(self, exe, env, args=("daemon",)):
        self.p = subprocess.Popen([str(exe), *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, env=env, bufsize=1)
        self.out, self.err = [], []
        self.viewers = set()   # cameras the caller already gets the stream of (viewer())
        self.cv = threading.Condition()
        self.readers = [threading.Thread(target=self._pump, args=(f, store), daemon=True)
                        for f, store in ((self.p.stdout, self.out), (self.p.stderr, self.err))]
        for t in self.readers:
            t.start()

    def _pump(self, f, store):
        for line in f:
            with self.cv:
                store.append(line.rstrip("\n"))
                self.cv.notify_all()
        with self.cv:
            self.cv.notify_all()

    def wait(self, match, count=1, since=0, err=False, timeout=10):
        """The lines (from index since) that match, once there are count of them"""
        store = self.err if err else self.out
        test = match if callable(match) else (lambda l: match in l)
        deadline = time.monotonic() + timeout * SLOW
        with self.cv:
            while True:
                found = [l for l in store[since:] if test(l)]
                if len(found) >= count:
                    return found
                left = deadline - time.monotonic()
                if left <= 0 or (self.p.poll() is not None and not any(t.is_alive() for t in self.readers)):
                    raise AssertionError(f"no {count} x {match!r}\nstdout:\n" + "\n".join(self.out) +
                                         "\nstderr:\n" + "\n".join(self.err[-60:]))
                self.cv.wait(min(left, 0.2))

    def mark(self, err=False):
        with self.cv:
            return len(self.err if err else self.out)

    def cmd(self, line):
        since = self.mark()
        self.p.stdin.write(line + "\n")
        self.p.stdin.flush()
        self.wait(lambda l: l == f"ok {line}", since=since)
        return since

    def lines(self, prefix, since=0):
        with self.cv:
            return [l for l in self.out[since:] if l.startswith(prefix)]

    def errors(self, text):
        with self.cv:
            return [l for l in self.err if text in l]

    def finish(self, timeout=20):
        rc = self.p.wait(timeout=timeout * SLOW)
        for t in self.readers:
            t.join(timeout=5)
        text = "\n".join(self.err)
        assert "AddressSanitizer" not in text and "runtime error" not in text, text
        return rc

    def quit(self):
        self.cmd("quit")
        return self.finish()

    def kill(self):
        if self.p.poll() is None:
            self.p.kill()
            self.p.wait()


def pictures_config(config_dir, value):
    """$XDG_CONFIG_HOME/user-dirs.dirs with XDG_PICTURES_DIR as xdg-user-dirs writes it"""
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "user-dirs.dirs").write_text(
        "# This file is written by xdg-user-dirs-update\n"
        'XDG_DESKTOP_DIR="$HOME/Desktop"\n'
        f'XDG_PICTURES_DIR="{value}"\n')
    return config_dir


@pytest.fixture
def start(built, bus, tmp_path, tmp_path_factory):
    d, exe = built
    started = []
    # The test's own folder is the pictures folder, TakePicture puts its photos there
    config = pictures_config(tmp_path_factory.mktemp("config"), tmp_path)

    def start(cameras=CAMERAS, fail=None, system=True, session=True, args=("daemon",), ready=True, **extra):
        none = f"unix:path={d}/no-such-bus"
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(d),
            "XDG_RUNTIME_DIR": str(d / "run"),
            "XDG_CONFIG_HOME": str(config),
            "DBUS_SESSION_BUS_ADDRESS": bus if session else none,
            "DBUS_SYSTEM_BUS_ADDRESS": bus if system else none,
            "HARNESS_BUS": bus,
            "HARNESS_CAMERAS": cameras,
            "PIPEWIRE_DEBUG": "4",
            "PIPEWIRE_LOG_SYSTEMD": "false",
            "ASAN_OPTIONS": "detect_leaks=0:abort_on_error=0:exitcode=86",
            "UBSAN_OPTIONS": "print_stacktrace=1:halt_on_error=1:exitcode=87",
            "LC_ALL": "C",
        }
        if fail:
            env["HARNESS_FAIL"] = fail
        env.update({k: str(v) for k, v in extra.items()})
        b = Bridge(exe, env, args)
        started.append(b)
        if ready:
            # All nodes are there once the registry is asked for
            b.wait("pw context_new")
        return b

    yield start
    for b in started:
        b.kill()


@pytest.fixture
def proc(built):
    """The bridge's /proc: proc(pid, cgroup) gives process pid that cgroup (None: no process)"""
    d, _ = built
    made = []

    def proc(pid, cgroup):
        f = d / "proc" / str(pid) / "cgroup"
        f.parent.mkdir(parents=True, exist_ok=True)
        if cgroup is None:
            f.unlink(missing_ok=True)
        else:
            f.write_text(cgroup)
            made.append(f)
    yield proc
    for f in made:
        f.unlink(missing_ok=True)


def app_cgroup(unit, uid=1000):
    """/proc/<pid>/cgroup of a process in an app's unit of the session"""
    return f"0::/user.slice/user-{uid}.slice/user@{uid}.service/app.slice/{unit}\n"


@pytest.fixture
def front_port(built):
    d, _ = built
    p = d / "front-port" / "power"
    p.mkdir(parents=True, exist_ok=True)
    status = p / "runtime_status"
    status.unlink(missing_ok=True)
    yield status
    status.unlink(missing_ok=True)


def gdbus(bus, method, *args, timeout=20):
    """A call to the bridge on the test bus: (ok, output)"""
    r = subprocess.run(["gdbus", "call", "--address", bus, "--dest", BRIDGE, "--object-path", BRIDGE_PATH,
                        "--timeout", str(timeout * SLOW), "--method", method, *args],
                       capture_output=True, text=True, timeout=timeout * SLOW + 10)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def get(bus, prop):
    ok, out = gdbus(bus, "org.freedesktop.DBus.Properties.Get", f"'{BRIDGE}'", f"'{prop}'")
    assert ok, out
    return out


def set_(bus, prop, value):
    return gdbus(bus, "org.freedesktop.DBus.Properties.Set", f"'{BRIDGE}'", f"'{prop}'", f"<'{value}'>")


def take_picture_async(bus, node, path):
    """TakePicture in the background (it is answered once the still is there)"""
    result = {}

    def call():
        result["reply"] = gdbus(bus, f"{BRIDGE}.TakePicture", f"'{node}'", f"'{path}'")

    t = threading.Thread(target=call, daemon=True)
    t.start()

    def wait():
        t.join(timeout=30 * SLOW)
        assert "reply" in result, "TakePicture got no answer"
        return result["reply"]
    return wait


def viewer(b, cam="0"):
    """The caller (CALLER) as a client the camera streams to, once per camera"""
    if cam not in b.viewers:
        b.viewers.add(cam)
        consumer(b, 200 + 3 * int(cam), CALLER, node_out=str(100 + int(cam)))


def open_camera(b, cam="0"):
    """Streams camera cam's node and waits for the camera to run"""
    since = b.mark()
    b.cmd(f"stream hal_camera_{cam} streaming")
    b.wait(f"ndk ACameraCaptureSession_setRepeatingRequest {cam} preview", since=since)
    return since


def stop_camera(b, cam="0"):
    since = b.mark()
    b.cmd(f"stream hal_camera_{cam} paused")
    b.wait(f"ndk ACameraDevice_close {cam}", since=since)
    b.wait(lambda l: l.startswith("ndk AImageReader_delete"), count=2, since=since)
    return since


def objects(b):
    since = b.cmd("objects")
    line = b.lines("objects ", since)[0]
    return dict(kv.split("=") for kv in line.split()[1:])


def settings_of(b, cam, request, since):
    """The entries the bridge set in a request, the last value of each"""
    out = {}
    for line in b.lines(f"ndk set {cam} {request} ", since):
        k, v = line.split()[-1].split("=")
        out[k] = int(v)
    return out


def wait_rotation(b, node, degrees):
    deadline = time.monotonic() + 10 * SLOW
    while True:
        since = b.cmd(f"rotation {node}")
        line = b.lines("rotation ", since)[0]
        if f"device={degrees} " in line:
            return line
        assert time.monotonic() < deadline, line
        time.sleep(0.02)


def wait_capture(b, node, state):
    deadline = time.monotonic() + 10 * SLOW
    while True:
        since = b.cmd(f"capture {node}")
        if b.lines("capture ", since)[0].endswith(f"state={state}"):
            return
        assert time.monotonic() < deadline
        time.sleep(0.02)


def source_pixel(xp, yp, w, h, degrees):
    """Where pixel (xp, yp) of a plane turned clockwise by degrees comes from in the w x h source"""
    return {0: (xp, yp), 90: (yp, h - 1 - xp), 180: (w - 1 - xp, h - 1 - yp), 270: (w - 1 - yp, xp)}[degrees]


def expected_frame(degrees, sw=1280, sh=720):
    """What on_process puts into a buffer from the harness's test frame (Y = x + y, V = x + 3y,
    U = 2x + y) of sw x sh: the samples the harness prints"""
    w, h = (sh, sw) if degrees in (90, 270) else (sw, sh)
    y = [sum(source_pixel(x, yy, sw, sh, degrees)) % 256 for x, yy in ((0, 0), (1, 0), (0, 1), (w - 1, h - 1))]
    vu = []
    for x, yy in ((0, 0), (1, 0), (0, 1)):
        sx, sy = source_pixel(x, yy, sw // 2, sh // 2, degrees)
        vu.append(f"{(sx + 3 * sy) % 256}/{(2 * sx + sy) % 256}")
    return w, f"y={','.join(map(str, y))} vu={','.join(vu)}"


# ----- starting and stopping -----

def test_start_and_stop(start):
    b = start()
    b.wait("pw stream_connect hal_camera_3")
    assert b.quit() == 0
    # The NDK is loaded and the binder thread pool started before the cameras are asked for
    assert b.out[:6] == ["hybris dlopen libcamera2ndk.so", "hybris dlopen libmediandk.so",
                         "hybris dlopen libbinder_ndk.so", "ndk ABinderProcess_startThreadPool",
                         "ndk ACameraManager_create", "ndk ACameraManager_getCameraIdList 4"]
    # Each camera a driving NV21 source of its upright sizes, one format each, 720p first; at the end the
    # registry, the core and the streams go
    assert b.lines("pw stream_connect") == [
        f"pw stream_connect hal_camera_{i} output=1 any=1 map=1 driver=1 params=2 video=1 raw=1 nv21=1 "
        "size=720x1280 then 1080x1920 rate=30/1" for i in range(4)]
    assert b.out[b.out.index("ok quit") + 1:] == [
        "pw registry_destroy listeners=0", "pw core_disconnect", "pw context_destroy",
        *(f"pw stream_destroy hal_camera_{i}" for i in range(4)), "exit 0"]


@pytest.mark.parametrize("fail,message", [
    ("dlopen:libcamera2ndk.so", "camera-hal-bridge: cannot load the NDK: fake: library not found"),
    ("dlopen:libbinder_ndk.so", "camera-hal-bridge: cannot load the NDK: fake: library not found"),
    ("dlsym:ACameraManager_create", "camera-hal-bridge: no symbol ACameraManager_create"),
    ("dlsym:AImage_delete", "camera-hal-bridge: no symbol AImage_delete"),
    ("dlsym:ABinderProcess_startThreadPool", "camera-hal-bridge: no symbol ABinderProcess_startThreadPool"),
])
def test_without_the_ndk_the_bridge_gives_up(start, fail, message):
    b = start(fail=fail, ready=False)
    assert b.finish() == 1
    assert message in b.err
    # No camera asked for, no thread pool started, nothing in PipeWire
    assert not b.lines("ndk ") and not b.lines("pw ")


def test_waits_for_the_hal_to_list_its_cameras(start):
    # At boot the provider in the container lists its cameras a few seconds after the bridge starts
    b = start(fail="ACameraManager_getCameraIdList*3", ready=False)
    b.wait("pw stream_connect hal_camera_3")
    assert b.lines("ndk ACameraManager_getCameraIdList") == [
        *["ndk ACameraManager_getCameraIdList failed"] * 3, "ndk ACameraManager_getCameraIdList 4"]
    assert b.wait("camera-hal-bridge: no camera list", count=3, err=True) == [
        "camera-hal-bridge: no camera list: -10001"] * 3
    assert b.quit() == 0


def test_without_cameras_the_bridge_gives_up(start):
    b = start(fail="ACameraManager_getCameraIdList", ready=False)
    assert b.finish() == 1
    # The first look and 60 more
    assert len(b.errors("no camera list")) == 61
    assert "camera-hal-bridge: no cameras" in b.err
    assert not b.lines("pw ")


def test_camera_names(start):
    # Ids without a model entry are named by facing, the second of a facing with a number
    b = start(cameras="4:b:0,5:b:180,6:f:90,7:f:0")
    b.wait("pw stream_connect hal_camera_7")
    names = [re.search(r"node.name=(\S+) node.description=(.*) location=(\w+)", l).groups()
             for l in b.lines("pw stream_new")]
    assert names == [("hal_camera_4", "Back Camera", "back"), ("hal_camera_5", "Back Camera 2", "back"),
                     ("hal_camera_6", "Front Camera", "front"), ("hal_camera_7", "Front Camera 2", "front")]
    # The node size is the upright frame: a quarter turn swaps the sides
    assert [re.search(r"size=(\S+)", l).group(1) for l in b.lines("pw stream_connect")] == [
        "1280x720", "1280x720", "720x1280", "1280x720"]
    assert b.quit() == 0


def test_the_sizes_on_offer_are_the_hals_yuv_outputs(start):
    # 4K too where the HAL lists it; a camera without characteristics offers the default alone
    b = start(cameras="0:b:90:a,4:b:0:p")
    b.wait("pw stream_connect hal_camera_4")
    assert [re.search(r"size=(.*) rate", l).group(1) for l in b.lines("pw stream_connect")] == [
        "720x1280 then 1080x1920,2160x3840", "1280x720"]
    b.wait("hal_camera_0: camera 0, sensor orientation 90, sizes 1280x720 1920x1080 3840x2160", err=True)
    assert b.quit() == 0


def update_params(b, since):
    line = b.lines("pw update_params hal_camera_0", since)[-1]
    return re.search(r"size=(\d+) stride=(\d+)", line).groups()


def test_the_size_the_consumer_picks(start):
    b = start(cameras="0:b:90:a")
    # Picked before the stream starts: the camera opens at that size
    since = b.cmd("param hal_camera_0 format 1080x1920")
    assert update_params(b, since) == (str(1920 * 1080 * 3 // 2), "1080")
    b.wait("hal_camera_0: 1080x1920", err=True)
    since = open_camera(b)
    assert b.lines("ndk AImageReader_new 1920x1080", since)
    # Frames of that size go out turned upright, a frame of another size is left out
    since = b.cmd("frame 0 1920 1080")
    assert b.lines("pw trigger", since)
    since = b.cmd("process hal_camera_0")
    stride, samples = expected_frame(90, 1920, 1080)
    assert b.lines("pw queue", since) == [f"pw queue hal_camera_0 offset=0 stride={stride} size=3110400 "
                                          f"transform=0 {samples}"]
    since = b.cmd("frame 0")
    assert b.lines("ndk AImage_delete", since) == ["ndk AImage_delete 1280x720"]
    assert not b.lines("pw trigger", since)
    # Another size while open: the camera opens again with it, 4K now
    since = b.cmd("param hal_camera_0 format 2160x3840")
    assert update_params(b, since) == (str(3840 * 2160 * 3 // 2), "2160")
    b.wait("hal_camera_0: closed for another size", err=True, since=b.mark(err=True) - 5)
    b.wait("ndk AImageReader_new 3840x2160", since=since)
    b.wait("ndk ACameraCaptureSession_setRepeatingRequest 0 preview", since=since)
    b.cmd("frame 0 3840 2160")
    since = b.cmd("process hal_camera_0")
    stride, samples = expected_frame(90, 3840, 2160)
    assert b.lines("pw queue", since) == [f"pw queue hal_camera_0 offset=0 stride={stride} size=12441600 "
                                          f"transform=0 {samples}"]
    # The same size again, one not on offer, a format without a size: nothing changes
    for fmt in ("format 2160x3840", "format 999x999", "format"):
        since = b.cmd(f"param hal_camera_0 {fmt}")
        assert update_params(b, since) == (str(3840 * 2160 * 3 // 2), "2160")
    time.sleep(0.2)
    assert len(b.lines("ndk AImageReader_new 3840x2160")) == 1
    # Back to the default
    since = b.cmd("param hal_camera_0 format 720x1280")
    b.wait("ndk AImageReader_new 1280x720", since=since)
    assert b.quit() == 0


def test_a_size_change_is_no_failed_open(start):
    # Reopening for another size goes at once, without the wait after a failure
    b = start(cameras="0:b:90:a")
    open_camera(b)
    since = b.cmd("param hal_camera_0 format 1080x1920")
    b.wait("ndk ACameraCaptureSession_setRepeatingRequest 0 preview", since=since)
    assert not b.errors("cannot open the camera")
    assert b.quit() == 0


@pytest.mark.parametrize("cameras,cam,still", [
    (CAMERAS, "0", "4608x3456"),   # the 64MP sensor reads out 4624x3472, larger JPEGs are only scaled
    (CAMERAS, "1", "3264x2448"),
    ("4:b:0", "4", "4000x3000"),   # no model entry: the largest output JPEG on offer
])
def test_still_size(start, cameras, cam, still):
    b = start(cameras=cameras)
    since = open_camera(b, cam)
    assert b.lines("ndk AImageReader_new", since) == [
        "ndk AImageReader_new 1280x720 yuv max=4", f"ndk AImageReader_new {still} jpeg max=2"]
    assert b.quit() == 0


def test_a_camera_without_characteristics_is_left_out(start):
    b = start(cameras="0:b:90,1:f:270,2:b:90", fail="ACameraManager_getCameraCharacteristics@2")
    b.wait("pw stream_connect hal_camera_2")
    b.wait("camera-hal-bridge: camera 1: no characteristics: -10001", err=True)
    assert [l.split()[2] for l in b.lines("pw stream_new")] == ["hal_camera_0", "hal_camera_2"]
    assert b.quit() == 0


def test_at_most_eight_cameras(start):
    b = start(cameras=",".join(f"{i}:b:0" for i in range(4, 14)))
    b.wait("pw context_new")
    assert [l.split()[2] for l in b.lines("pw stream_new")] == [f"hal_camera_{i}" for i in range(4, 12)]
    # What the HAL handed out went back
    assert all(v == "0" for v in objects(b).values())
    assert b.quit() == 0


@pytest.mark.parametrize("fail", ["pw_stream_new_simple", "pw_stream_connect@2"])
def test_without_pipewire_the_bridge_gives_up(start, fail):
    b = start(fail=fail, ready=False)
    assert b.finish() == 1
    assert "camera-hal-bridge: cannot connect to PipeWire" in b.err
    # Nothing goes on after the first camera that cannot connect
    assert not b.lines("pw context_new")
    assert len(b.lines("pw stream_new")) == (1 if fail == "pw_stream_new_simple" else 2)


def test_without_a_descriptor_for_the_wakeups_the_bridge_gives_up(start):
    b = start(HARNESS_NO_FDS=1, ready=False)
    assert b.finish() == 1
    assert b.errors("camera-hal-bridge: eventfd: Too many open files")
    assert not b.lines("pw ")


# ----- a camera while its node streams -----

def test_streaming_opens_the_camera_and_pausing_closes_it(start):
    b = start()
    since = open_camera(b, "0")
    # A preview stream at the preview size and a still stream at the sensor's size, both in the
    # session from the start; the still request feeds the preview too
    assert b.lines("ndk ", since) == [
        "ndk ACameraManager_openCamera 0",
        "ndk AImageReader_new 1280x720 yuv max=4",
        "ndk AImageReader_new 4608x3456 jpeg max=2",
        "ndk ACaptureSessionOutputContainer_add yuv",
        "ndk ACaptureSessionOutputContainer_add jpeg",
        "ndk ACameraDevice_createCaptureRequest 0 preview",
        "ndk ACameraDevice_createCaptureRequest 0 still",
        "ndk ACaptureRequest_addTarget 0 preview yuv",
        "ndk ACaptureRequest_addTarget 0 still yuv",
        "ndk ACaptureRequest_addTarget 0 still jpeg",
        "ndk set 0 preview CONTROL_MODE=1",
        "ndk set 0 preview SCENE_MODE=0",
        "ndk set 0 preview AWB_MODE=1",
        "ndk set 0 preview FLASH_MODE=0",
        "ndk set 0 preview AE_EXPOSURE_COMPENSATION=0",
        "ndk ACameraDevice_createCaptureSession 0 outputs=2",
        "ndk ACameraCaptureSession_setRepeatingRequest 0 preview n=1",
    ]
    b.wait("hal_camera_0: open", err=True)
    # The accelerometer runs while a camera streams
    b.wait("sensorproxy ClaimAccelerometer")
    # The same state again is no news
    since2 = b.cmd("stream hal_camera_0 streaming")
    since = stop_camera(b, "0")
    assert b.lines("ndk ", since) == [
        "ndk ACameraCaptureSession_stopRepeating 0", "ndk ACameraCaptureSession_close 0",
        "ndk ACameraDevice_close 0", "ndk AImageReader_delete jpeg", "ndk AImageReader_delete yuv"]
    assert len(b.lines("ndk ACameraManager_openCamera", since2)) == 0
    b.wait("sensorproxy ReleaseAccelerometer")
    b.wait("hal_camera_0: closed", err=True)
    assert all(v == "0" for v in objects(b).values())
    assert b.quit() == 0


@pytest.mark.parametrize("cam,degrees", [("0", 90), ("1", 270), ("4", 0), ("5", 180)])
def test_frames_are_turned_upright(start, cam, degrees):
    b = start(cameras="0:b:90,1:f:270,4:b:0,5:b:180")
    open_camera(b, cam)
    b.cmd(f"io hal_camera_{cam}")
    since = b.cmd(f"frame {cam}")
    # Each frame drives the graph, with the clock set by the stream: a frame time ahead, no rate
    assert b.lines("pw trigger", since) == [
        f"pw trigger hal_camera_{cam} nsec=5000000000 next=5033333333 delay=0 rate_diff=1.0 no_rate=1"]
    # The image went back to the reader
    assert b.lines("ndk AImage_delete", since) == ["ndk AImage_delete 1280x720"]
    since = b.cmd(f"process hal_camera_{cam}")
    stride, samples = expected_frame(degrees)
    assert b.lines("pw queue", since) == [
        f"pw queue hal_camera_{cam} offset=0 stride={stride} size=1382400 transform=0 {samples}"]
    b.cmd(f"frame {cam}")
    b.wait("frames 1280x720", err=True)
    assert b.errors("frames 1280x720, stride 1280/1280/1280, chroma pixel stride 2, NV21") == [
        b.errors("frames 1280x720")[0]]   # said once
    assert b.quit() == 0


def test_the_clock_is_kept_only_while_driving(start):
    b = start()
    open_camera(b)
    since = b.cmd("frame 0")
    assert b.lines("pw trigger", since) == ["pw trigger hal_camera_0 without position"]
    # Another area than the position is not taken for it
    b.cmd("io hal_camera_0 clock")
    since = b.cmd("frame 0")
    assert b.lines("pw trigger", since) == ["pw trigger hal_camera_0 without position"]
    b.cmd("io hal_camera_0")
    b.cmd("driving hal_camera_0 0")
    since = b.cmd("frame 0")
    # A follower: the clock is the driver's, only the process is triggered
    assert b.lines("pw trigger", since) == [
        "pw trigger hal_camera_0 nsec=0 next=0 delay=0 rate_diff=0.0 no_rate=0"]
    assert b.quit() == 0


def test_frames_the_bridge_cannot_use(start):
    b = start()
    open_camera(b)
    b.cmd("io hal_camera_0")
    # Not the preview size: dropped, the image goes back, no frame for the graph
    since = b.cmd("frame 0 1920")
    assert b.lines("ndk AImage_delete", since) == ["ndk AImage_delete 1920x720"]
    assert not b.lines("pw trigger", since)
    # The reader has no image after all: nothing to copy, nothing to give back
    b.cmd("fail AImageReader_acquireLatestImage")
    since = b.cmd("frame 0")
    b.cmd("still 0")
    assert not b.lines("pw trigger", since) and not b.lines("ndk AImage_delete", since)
    b.cmd("unfail AImageReader_acquireLatestImage")
    since = b.cmd("frame 0")
    assert b.lines("pw trigger", since)
    assert objects(b)["image"] == "0"
    assert b.quit() == 0


def test_buffers(start):
    b = start()
    open_camera(b)
    b.cmd("frame 0")
    stride, samples = expected_frame(90)
    # No buffer free: nothing to fill
    b.cmd("buffer hal_camera_0 none")
    since = b.cmd("process hal_camera_0")
    assert not b.lines("pw queue", since)
    # Too small or not mapped: queued with nothing in it, but with the transform
    for mode in ("small", "nodata"):
        b.cmd(f"buffer hal_camera_0 {mode}")
        since = b.cmd("process hal_camera_0")
        assert b.lines("pw queue", since) == ["pw queue hal_camera_0 empty transform=0 first=238"]
    # Without the transform meta: the frame alone
    b.cmd("buffer hal_camera_0 nometa")
    since = b.cmd("process hal_camera_0")
    assert b.lines("pw queue", since) == [
        f"pw queue hal_camera_0 offset=0 stride={stride} size=1382400 transform=99 {samples}"]
    assert b.quit() == 0


def test_buffers_are_asked_for_once_the_format_is_set(start):
    b = start(cameras="0:b:90,4:b:0")
    since = b.cmd("param hal_camera_0 other")
    b.cmd("param hal_camera_0 null")
    assert not b.lines("pw update_params", since)
    # Buffers of a whole NV21 frame with the node's stride, and the transform meta
    since = b.cmd("param hal_camera_0 format")
    b.cmd("param hal_camera_4 format")
    assert b.lines("pw update_params", since) == [
        "pw update_params hal_camera_0 n=2 buffers=4(2..8) range=1 blocks=1 size=1382400 stride=720 "
        "meta=VideoTransform meta_size=4",
        "pw update_params hal_camera_4 n=2 buffers=4(2..8) range=1 blocks=1 size=1382400 stride=1280 "
        "meta=VideoTransform meta_size=4"]
    assert b.quit() == 0


# ----- opening that fails, cameras that go away -----

OPEN_STEPS = [
    ("ACameraManager_openCamera@1", "ACameraManager_openCamera(c->bridge->manager, c->id"),
    ("AImageReader_new@1", "AImageReader_new(width, height, AIMAGE_FORMAT_YUV_420_888"),
    ("AImageReader_new@2", "AImageReader_new(c->picture_width, c->picture_height, AIMAGE_FORMAT_JPEG"),
    ("AImageReader_setImageListener@1", "AImageReader_setImageListener(s->preview_reader"),
    ("AImageReader_setImageListener@2", "AImageReader_setImageListener(s->still_reader"),
    ("AImageReader_getWindow@1", "AImageReader_getWindow(s->preview_reader"),
    ("AImageReader_getWindow@2", "AImageReader_getWindow(s->still_reader"),
    ("ACaptureSessionOutputContainer_create@1", "ACaptureSessionOutputContainer_create(&s->outputs)"),
    ("ACaptureSessionOutput_create@1", "ACaptureSessionOutput_create(preview_window"),
    ("ACaptureSessionOutput_create@2", "ACaptureSessionOutput_create(still_window"),
    ("ACaptureSessionOutputContainer_add@1", "ACaptureSessionOutputContainer_add(s->outputs, s->preview_output)"),
    ("ACaptureSessionOutputContainer_add@2", "ACaptureSessionOutputContainer_add(s->outputs, s->still_output)"),
    ("ACameraOutputTarget_create@1", "ACameraOutputTarget_create(preview_window"),
    ("ACameraOutputTarget_create@2", "ACameraOutputTarget_create(still_window"),
    ("ACameraDevice_createCaptureRequest@1", "TEMPLATE_PREVIEW"),
    ("ACameraDevice_createCaptureRequest@2", "TEMPLATE_STILL_CAPTURE"),
    ("ACaptureRequest_addTarget@1", "ACaptureRequest_addTarget(s->preview_request, s->preview_target)"),
    ("ACaptureRequest_addTarget@2", "ACaptureRequest_addTarget(s->still_request, s->preview_target)"),
    ("ACaptureRequest_addTarget@3", "ACaptureRequest_addTarget(s->still_request, s->still_target)"),
    ("ACameraDevice_createCaptureSession@1", "ACameraDevice_createCaptureSession(s->device"),
    ("ACameraCaptureSession_setRepeatingRequest@1", "ACameraCaptureSession_setRepeatingRequest(s->capture_session"),
]


@pytest.mark.parametrize("fail,step", OPEN_STEPS, ids=[f for f, _ in OPEN_STEPS])
def test_a_failed_open_frees_what_it_got_and_tries_again(start, fail, step):
    b = start(fail=fail)
    b.cmd("stream hal_camera_0 streaming")
    b.wait("ndk ACameraCaptureSession_setRepeatingRequest 0 preview n=1")
    # Said which step failed, with the HAL's code
    b.wait(lambda l: "hal_camera_0: " in l and step in l and l.endswith(": -10001"), err=True)
    assert b.lines("ndk ACameraManager_openCamera 0") == (
        ["ndk ACameraManager_openCamera 0 failed", "ndk ACameraManager_openCamera 0"] if "openCamera" in fail
        else ["ndk ACameraManager_openCamera 0"] * 2)
    stop_camera(b)
    # Nothing of the failed try is left (nor of the one that worked)
    assert all(v == "0" for v in objects(b).values()), objects(b)
    assert b.quit() == 0


def test_a_camera_that_does_not_open(start, bus, tmp_path):
    b = start(fail="ACameraManager_openCamera")
    b.cmd("stream hal_camera_0 streaming")
    # Three seconds of fast tries (on the phone), then once a warning and slower tries
    b.wait("hal_camera_0: cannot open the camera, trying on", err=True)
    b.wait("ndk ACameraManager_openCamera 0 failed", count=15)
    # A photo that waited long enough for it
    viewer(b)
    ok, out = gdbus(bus, f"{BRIDGE}.TakePicture", "'hal_camera_0'", f"'{tmp_path / 'photo.jpg'}'")
    assert not ok and "System.Error.ENODEV" in out and "hal_camera_0: no picture: No such device" in out
    b.cmd("unfail ACameraManager_openCamera")
    b.wait("ndk ACameraCaptureSession_setRepeatingRequest 0 preview n=1")
    assert len(b.errors("cannot open the camera")) == 1
    stop_camera(b)
    assert all(v == "0" for v in objects(b).values())
    assert b.quit() == 0


def test_a_photo_asked_for_while_the_camera_opens_is_taken_once_it_is_there(start, bus, tmp_path):
    b = start(fail="ACameraManager_openCamera")
    b.cmd("stream hal_camera_0 streaming")
    b.wait("ndk ACameraManager_openCamera 0 failed")
    target = tmp_path / "photo.jpg"
    viewer(b)
    answer = take_picture_async(bus, "hal_camera_0", target)
    wait_capture(b, "hal_camera_0", 1)   # CAPTURE_REQUESTED
    assert len(b.lines("ndk ACameraManager_openCamera 0 failed")) < 13
    b.cmd("unfail ACameraManager_openCamera")
    b.wait("ndk ACameraCaptureSession_capture 0 still n=1")
    b.cmd("still 0")
    assert answer() == (True, "()")
    assert target.read_bytes()[:2] == b"\xff\xd8"
    assert b.quit() == 0


@pytest.mark.parametrize("how,log", [("disconnect", "hal_camera_0: camera disconnected"),
                                     ("error", "hal_camera_0: camera error 4")])
def test_a_lost_camera_is_opened_again(start, how, log):
    b = start()
    open_camera(b)
    since = b.cmd(f"{how} 0")
    b.wait("ndk ACameraDevice_close 0", since=since)
    b.wait("ndk ACameraCaptureSession_setRepeatingRequest 0 preview", since=since)
    b.wait(log, err=True)
    assert len(b.lines("ndk ACameraManager_openCamera 0")) == 2
    stop_camera(b)
    assert all(v == "0" for v in objects(b).values())
    assert b.quit() == 0


def test_a_session_that_does_not_close(start):
    # The HAL never tells of the closed session: the bridge waits a while, then closes the device
    b = start(HARNESS_NO_SESSION_CLOSED=1)
    open_camera(b)
    t0 = time.monotonic()
    stop_camera(b)
    assert time.monotonic() - t0 >= 0.9
    b.wait("hal_camera_0: the session did not close in time", err=True)
    assert b.quit() == 0


# ----- settings on the bus -----

def test_settings_on_the_bus(start, bus):
    b = start()
    b.wait("pw context_new")
    for prop, value in [("SceneMode", "auto"), ("WhiteBalance", "auto"), ("ExposureCompensation", "0"),
                        ("NoiseReduction", "auto")]:
        assert get(bus, prop) == f"(<'{value}'>,)"
    ok, out = set_(bus, "SceneMode", "moon")
    assert not ok and 'org.freedesktop.DBus.Error.InvalidArgs: unknown SceneMode "moon"' in out
    assert set_(bus, "SceneMode", "night")[0]
    b.wait("bridge-changed SceneMode=night")
    assert get(bus, "SceneMode") == "(<'night'>,)"
    # The same value again changes nothing and is not announced
    assert set_(bus, "SceneMode", "night")[0]
    for prop, value in [("WhiteBalance", "daylight"), ("ExposureCompensation", "-2"), ("NoiseReduction", "off")]:
        assert set_(bus, prop, value)[0]
        b.wait(f"bridge-changed {prop}={value}")
        assert get(bus, prop) == f"(<'{value}'>,)"
    assert b.lines("bridge-changed") == ["bridge-changed SceneMode=night", "bridge-changed WhiteBalance=daylight",
                                         "bridge-changed ExposureCompensation=-2", "bridge-changed NoiseReduction=off"]
    assert b.quit() == 0


@pytest.mark.parametrize("cam,prop,value,expected", [
    ("0", "SceneMode", "night", {"CONTROL_MODE": 2, "SCENE_MODE": 5}),
    ("0", "SceneMode", "action", {"CONTROL_MODE": 2, "SCENE_MODE": 2}),
    # Not offered by the camera: auto
    ("0", "SceneMode", "hdr", {"CONTROL_MODE": 1, "SCENE_MODE": 0}),
    ("4", "SceneMode", "night", {"CONTROL_MODE": 1, "SCENE_MODE": 0}),
    ("0", "WhiteBalance", "cloudy-daylight", {"AWB_MODE": 6}),
    ("0", "WhiteBalance", "fluorescent", {"AWB_MODE": 3}),
    ("0", "WhiteBalance", "incandescent", {"AWB_MODE": 2}),
    # EV in the camera's steps (1/6 EV), rounded and kept in its range (here -6..6 in 0.3 EV)
    ("0", "ExposureCompensation", "+2", {"AE_EXPOSURE_COMPENSATION": 12}),
    ("0", "ExposureCompensation", "-1", {"AE_EXPOSURE_COMPENSATION": -6}),
    ("3", "ExposureCompensation", "+1", {"AE_EXPOSURE_COMPENSATION": 3}),
    ("3", "ExposureCompensation", "-1", {"AE_EXPOSURE_COMPENSATION": -3}),
    ("3", "ExposureCompensation", "+2", {"AE_EXPOSURE_COMPENSATION": 6}),
    ("3", "ExposureCompensation", "-2", {"AE_EXPOSURE_COMPENSATION": -6}),
    # No compensation on offer (a step of 0, or none at all)
    ("5", "ExposureCompensation", "+2", {"AE_EXPOSURE_COMPENSATION": 0}),
    ("4", "ExposureCompensation", "+2", {"AE_EXPOSURE_COMPENSATION": 0}),
])
def test_a_setting_applies_to_the_streaming_camera_at_once(start, bus, cam, prop, value, expected):
    b = start(cameras="0:b:90,3:b:90:s,4:b:0:p,5:b:0:z")
    open_camera(b, cam)
    since = b.mark()
    assert set_(bus, prop, value)[0]
    b.wait(f"ndk ACameraCaptureSession_setRepeatingRequest {cam} preview", since=since)
    applied = settings_of(b, cam, "preview", since)
    assert {k: applied[k] for k in expected} == expected
    assert applied["FLASH_MODE"] == 0   # the flash button uses the LED past the camera
    assert b.quit() == 0


def test_a_setting_made_before_streaming_applies_when_the_camera_opens(start, bus):
    b = start()
    b.wait("pw context_new")
    assert set_(bus, "SceneMode", "night")[0]
    assert set_(bus, "WhiteBalance", "daylight")[0]
    b.wait("bridge-changed WhiteBalance=daylight")
    since = open_camera(b, "0")
    applied = settings_of(b, "0", "preview", since)
    assert applied["SCENE_MODE"] == 5 and applied["CONTROL_MODE"] == 2 and applied["AWB_MODE"] == 5
    assert len(b.lines("ndk ACameraCaptureSession_setRepeatingRequest 0", since)) == 1
    assert b.quit() == 0


# ----- photos -----

JPEG = bytes([0xff, 0xd8]) + b"JPG0" + bytes([0xff, 0xd9])


def take(b, bus, path, node="hal_camera_0"):
    """TakePicture up to the HAL's capture call; returns the answer's waiter and the mark"""
    viewer(b, node[-1])
    since = b.mark()
    answer = take_picture_async(bus, node, path)
    b.wait(f"ndk ACameraCaptureSession_capture {node[-1]}", since=since)
    return answer, since


def test_take_picture(start, bus, tmp_path):
    b = start()
    open_camera(b, "0")
    # A still nobody asked for goes back to the HAL
    since = b.cmd("still 0")
    assert b.lines("ndk AImage_delete", since) == ["ndk AImage_delete 4000x3000"]
    assert set_(bus, "NoiseReduction", "off")[0]
    b.wait("bridge-changed NoiseReduction=off")
    target = tmp_path / "photo.jpg"
    answer, since = take(b, bus, target)
    # The settings, the JPEG quality, the rotation for how the phone is held (portrait: the
    # sensor's 90) and MediaTek's noise reduction (off: 0) in the still request
    assert settings_of(b, "0", "still", since) == {
        "CONTROL_MODE": 1, "SCENE_MODE": 0, "AWB_MODE": 1, "FLASH_MODE": 0, "AE_EXPOSURE_COMPENSATION": 0,
        "JPEG_QUALITY": 95, "JPEG_ORIENTATION": 90, "MTK_MFB_MODE": 0}
    assert b.lines("ndk ACameraCaptureSession_capture", since) == ["ndk ACameraCaptureSession_capture 0 still n=1"]
    b.cmd("still 0")
    assert answer() == (True, "()")
    assert target.read_bytes() == JPEG
    assert not (tmp_path / "photo.jpg.part").exists()
    b.wait("hal_camera_0: picture of 8 bytes in", err=True)
    assert objects(b)["image"] == "0"
    # The next one
    assert set_(bus, "NoiseReduction", "on")[0]
    answer, since = take(b, bus, tmp_path / "second.jpg")
    assert settings_of(b, "0", "still", since)["MTK_MFB_MODE"] == 1
    b.cmd("still 0")
    assert answer() == (True, "()")
    assert (tmp_path / "second.jpg").read_bytes() == JPEG
    assert b.quit() == 0


@pytest.mark.parametrize("cam,orientation,jpeg_orientation,mfb", [
    ("0", "left-up", 180, 255),   # back camera: sensor + device; noise reduction auto
    ("1", "left-up", 180, 255),   # front camera: sensor - device
    ("1", "right-up", 0, 255),
    ("2", "normal", 90, None),    # no multi-frame noise reduction on offer: not set
])
def test_photo_rotation_and_noise_reduction(start, bus, tmp_path, cam, orientation, jpeg_orientation, mfb):
    b = start()
    b.cmd(f"orient {orientation}")
    open_camera(b, cam)
    b.wait("sensorproxy ClaimAccelerometer")
    wait_rotation(b, f"hal_camera_{cam}", {"normal": 0, "left-up": 90, "right-up": 270}[orientation])
    answer, since = take(b, bus, tmp_path / "p.jpg", f"hal_camera_{cam}")
    applied = settings_of(b, cam, "still", since)
    assert applied["JPEG_ORIENTATION"] == jpeg_orientation
    assert applied.get("MTK_MFB_MODE") == mfb
    b.cmd(f"still {cam}")
    assert answer() == (True, "()")
    assert b.quit() == 0


def test_take_picture_refused(start, bus, tmp_path):
    b = start()
    b.wait("pw context_new")
    call = lambda node, path: gdbus(bus, f"{BRIDGE}.TakePicture", f"'{node}'", f"'{path}'")
    ok, out = call("hal_camera_0", "photo.jpg")
    assert not ok and "org.freedesktop.DBus.Error.InvalidArgs: not an absolute path: photo.jpg" in out
    ok, out = call("hal_camera_9", "/x.jpg")
    assert not ok and "org.freedesktop.DBus.Error.InvalidArgs: no camera node hal_camera_9" in out
    ok, out = call("hal_camera_0", "/x.jpg")
    assert not ok and "org.freedesktop.DBus.Error.NotSupported: hal_camera_0 is not streaming" in out
    open_camera(b, "0")
    viewer(b)
    b.cmd("fail strdup")
    ok, out = call("hal_camera_0", str(tmp_path / "x.jpg"))
    assert not ok and "org.freedesktop.DBus.Error.NoMemory: Cannot allocate memory" in out
    b.cmd("unfail strdup")
    answer, _ = take(b, bus, tmp_path / "first.jpg")
    ok, out = call("hal_camera_0", str(tmp_path / "second.jpg"))
    assert not ok and "org.freedesktop.DBus.Error.LimitsExceeded: hal_camera_0 is taking a picture already" in out
    b.cmd("still 0")
    assert answer() == (True, "()")
    assert (tmp_path / "first.jpg").exists() and not (tmp_path / "second.jpg").exists()
    assert b.quit() == 0


def test_a_photo_only_as_a_new_file_in_the_pictures_folder(start, bus, tmp_path):
    """Attack review 2026-11-22, S36: the photo of a caller that gets the stream goes nowhere but
    into the pictures folder, and never replaces a file"""
    b = start()
    open_camera(b, "0")
    viewer(b)
    keys = tmp_path.parent / f"{tmp_path.name}-ssh" / "authorized_keys"
    keys.parent.mkdir()
    keys.write_text("ssh-ed25519 AAAA\n")
    (tmp_path / "keys").symlink_to(keys.parent)
    call = lambda path: gdbus(bus, f"{BRIDGE}.TakePicture", "'hal_camera_0'", f"'{path}'")
    since = b.mark(err=True)
    ok, out = call(keys)
    assert not ok and ("org.freedesktop.DBus.Error.AccessDenied: hal_camera_0: a photo only as a new .jpg "
                       "or .jpeg in the pictures folder") in out, out
    b.wait(f"hal_camera_0: picture refused at {keys}, no JPEG in the pictures folder", err=True, since=since)
    ok, out = call(tmp_path / "keys" / "x.jpg")
    assert not ok and "hal_camera_0: no picture at" in out and (
        "System.Error.ELOOP" in out or "System.Error.ENOTDIR" in out), out
    ok, out = call(tmp_path / "none" / "x.jpg")
    assert not ok and "org.freedesktop.DBus.Error.FileNotFound: hal_camera_0: no picture at" in out, out
    assert keys.read_text() == "ssh-ed25519 AAAA\n" and not list(keys.parent.glob("x.jpg*"))
    # A file that is there stays as it is
    old = tmp_path / "old.jpg"
    old.write_bytes(b"old")
    answer, _ = take(b, bus, old)
    b.cmd("still 0")
    ok, out = answer()
    assert not ok and "org.freedesktop.DBus.Error.FileExists: hal_camera_0: no picture: File exists" in out, out
    assert old.read_bytes() == b"old" and not (tmp_path / "old.jpg.part").exists()
    # Where Snapshot stores its photos
    (tmp_path / "Camera").mkdir()
    photo = tmp_path / "Camera" / "Photo from 2026-11-22 10-00-00.123456.jpeg"
    answer, _ = take(b, bus, photo)
    b.cmd("still 0")
    assert answer() == (True, "()")
    assert photo.read_bytes() == JPEG
    assert objects(b)["image"] == "0"
    assert b.quit() == 0


DENIED = "org.freedesktop.DBus.Error.AccessDenied: hal_camera_0: only for who gets its stream"


def test_a_photo_only_for_who_gets_the_stream(start, bus, tmp_path, proc):
    """Attack review 2026-11-18, L16: TakePicture took a photo for anyone on the session bus"""
    b = start()
    b.wait("pw registry_add_listener")
    call = lambda: gdbus(bus, f"{BRIDGE}.TakePicture", "'hal_camera_0'", f"'{tmp_path / 'x.jpg'}'")

    def refused(pid):
        since = b.mark(err=True)
        ok, out = call()
        assert not ok and DENIED in out, out
        b.wait(f"hal_camera_0: picture refused to process {pid}, which does not get the stream",
               err=True, since=since)

    def taken(pid):
        b.cmd(f"caller {pid}")
        answer, _ = take(b, bus, tmp_path / f"{pid}.jpg")
        b.cmd("still 0")
        assert answer() == (True, "()")
        b.cmd(f"caller {CALLER}")

    open_camera(b, "0")
    b.viewers.add("0")   # take() must not make the caller a client: this test says who is one
    # Nobody gets the stream
    refused(CALLER)
    # Snapshot through the camera portal: the portal's process, the app id from the portal
    consumer(b, 50, 1234, portal=True)
    b.cmd("clientinfo 50 org.gnome.Snapshot")
    proc(5000, app_cgroup("app-gnome-org.gnome.Snapshot-77.scope"))
    taken(5000)
    # Started by D-Bus activation, in a slice of its own (as on the phone) or without one: the
    # same app
    proc(5001, app_cgroup("app-dbus\\x2d:1.2\\x2dorg.gnome.Snapshot.slice/dbus-:1.2-org.gnome.Snapshot@0.service"))
    taken(5001)
    proc(5006, app_cgroup("dbus-:1.2-org.gnome.Snapshot@0.service"))
    taken(5006)
    # Another app, a process in no app's unit, one gone, one without a cgroup v2 line, and the
    # portal's own process (its client is the app's, not its own)
    proc(5002, app_cgroup("app-gnome-org.example.Spy-3.scope"))
    proc(5003, "0::/user.slice/user-1000.slice/session-2.scope\n")
    proc(5004, None)
    proc(5005, "1:name=systemd:/\n")
    proc(5007, app_cgroup("app-dbus\\x2d:1.2\\x2dorg.gnome.Snapshot.slice"))
    proc(1234, "0::/user.slice/user-1000.slice/user@1000.service/session.slice/xdg-desktop-portal.service\n")
    for pid in (5002, 5003, 5004, 5005, 5007, 1234):
        b.cmd(f"caller {pid}")
        refused(pid)
    # A portal client whose app id has not come (yet) gets no photo for anyone
    b.cmd("remove 50")
    consumer(b, 60, 1234, portal=True)
    b.cmd("caller 5000")
    refused(5000)
    # A plain client: its own process, and another process of its app
    consumer(b, 70, 6000)
    proc(6000, app_cgroup("app-flatpak-org.example.Cam-12.scope"))
    proc(6001, app_cgroup("app-flatpak-org.example.Cam-13.scope"))
    taken(6000)
    taken(6001)
    b.cmd("caller 6002")
    refused(6002)
    # Its stream goes to the client of another camera: not this one's
    b.cmd("remove 70")
    consumer(b, 80, 6000, node_out="101")
    b.cmd("caller 6000")
    refused(6000)
    # The bus does not tell who calls
    b.cmd("fail sd_bus_creds_get_pid")
    refused(0)
    b.cmd("unfail sd_bus_creds_get_pid")
    # The process the bus names: the test's gdbus, which gets no stream
    b.cmd("caller real")
    ok, out = call()
    assert not ok and DENIED in out
    assert not list(tmp_path.glob("x.jpg*"))
    assert b.quit() == 0


def test_without_the_registry_nobody_gets_a_photo(start, bus, tmp_path):
    b = start(fail="pw_context_connect")
    b.wait("no registry, camera uses are not counted per app", err=True)
    open_camera(b, "0")
    ok, out = gdbus(bus, f"{BRIDGE}.TakePicture", "'hal_camera_0'", f"'{tmp_path / 'x.jpg'}'")
    assert not ok and DENIED in out
    assert b.quit() == 0


@pytest.mark.parametrize("unit,app", [
    ("app-gnome-org.gnome.Snapshot-1234.scope", "org.gnome.Snapshot"),
    ("app-flatpak-org.gnome.Snapshot-5.scope", "org.gnome.Snapshot"),
    ("app-org.gnome.Snapshot-5.scope", "org.gnome.Snapshot"),
    ("app-gnome-org.example.My\\x2dApp-9.scope", "org.example.My-App"),
    ("app-gnome-org.gnome.Snapshot@1.service", "org.gnome.Snapshot"),
    ("app-org.gnome.Snapshot.service", "org.gnome.Snapshot"),
    ("dbus-:1.2-org.gnome.Snapshot@0.service", "org.gnome.Snapshot"),
    ("dbus-:1.2-org.gnome.Snapshot.service", "org.gnome.Snapshot"),
    # Not an app: slices, other units, an escaped NUL, characters an app id has not, too long
    ("app-gnome.slice", "-"),
    ("session-2.scope", "-"),
    ("dbus-:1.2.service", "-"),
    ("dbus-:x.scope", "-"),
    ("dbus-:1.2-@0.service", "-"),   # no app id at all
    ("app-gnome-org.ex\\x00ample-1.scope", "-"),
    ("app-gnome-org.ex\\x2fample-1.scope", "-"),
    ("app-gnome-" + "a" * 256 + "-1.scope", "-"),
    ("app-gnome-" + "a" * 1100 + "-1.scope", "-"),
])
def test_the_app_of_a_unit(check, unit, app):
    assert kv(check(f"unit:{unit}").stdout)["app"] == app


@pytest.mark.parametrize("how,error,message", [
    # The errno as sd-bus names it
    ("stillfail", "System.Error.ECANCELED", "Operation canceled"),
    ("capture", "System.Error.ECANCELED", "Operation canceled"),
    ("empty", "org.freedesktop.DBus.Error.IOError", "Input/output error"),
    ("timeout", "org.freedesktop.DBus.Error.Timeout", "Connection timed out"),
    ("pause", "System.Error.ECANCELED", "Operation canceled"),
    ("disconnect", "System.Error.ECANCELED", "Operation canceled"),
])
def test_a_photo_that_fails(start, bus, tmp_path, how, error, message):
    b = start()
    open_camera(b, "0")
    target = tmp_path / "photo.jpg"
    if how == "capture":
        b.cmd("fail ACameraCaptureSession_capture@1")
    answer, since = take(b, bus, target)
    if how == "stillfail":
        b.cmd("stillfail 0")
    elif how == "empty":
        b.cmd("still 0 empty")
    elif how == "pause":
        b.cmd("stream hal_camera_0 paused")
    elif how == "disconnect":
        b.cmd("disconnect 0")
    ok, out = answer()
    assert not ok and f"{error}: hal_camera_0" in out and f"hal_camera_0: no picture: {message}" in out
    assert not target.exists() and not target.with_name("photo.jpg.part").exists()
    if how == "stillfail":
        b.wait("hal_camera_0: still failed, reason 1", err=True)
    if how == "capture":
        b.wait("hal_camera_0: cannot request a still: -10001", err=True)
    if how == "timeout":
        # The still that comes too late goes back to the HAL
        since = b.cmd("still 0")
        assert b.lines("ndk AImage_delete", since) == ["ndk AImage_delete 4000x3000"]
    # The next photo works
    if how == "pause":
        open_camera(b, "0")
    if how == "disconnect":
        b.wait("ndk ACameraManager_openCamera 0", count=2)
        b.wait("ndk ACameraCaptureSession_setRepeatingRequest 0 preview", count=2)
    answer, _ = take(b, bus, tmp_path / "next.jpg")
    b.cmd("still 0")
    assert answer() == (True, "()")
    assert objects(b)["image"] == "0"
    assert b.quit() == 0


# ----- Active: the front camera in use -----

def test_active_while_the_front_camera_streams(start, bus):
    b = start()
    b.wait("pw context_new")
    assert get(bus, "Active") == "(<false>,)"
    open_camera(b, "0")
    b.wait("sensorproxy ClaimAccelerometer")
    assert get(bus, "Active") == "(<false>,)"
    open_camera(b, "1")
    b.wait("bridge-changed Active=true")
    assert get(bus, "Active") == "(<true>,)"
    stop_camera(b, "0")
    assert get(bus, "Active") == "(<true>,)"
    stop_camera(b, "1")
    b.wait("bridge-changed Active=false")
    assert b.lines("bridge-changed") == ["bridge-changed Active=true", "bridge-changed Active=false"]
    assert b.quit() == 0


@pytest.mark.parametrize("system", [True, False])
def test_active_while_someone_else_uses_the_front_sensor(start, bus, front_port, system):
    # Face unlock or a root tool open the sensor past the bridge: its CSI port is powered
    front_port.write_text("suspended\n")
    b = start(system=system)
    b.wait("pw context_new")
    assert get(bus, "Active") == "(<false>,)"
    front_port.write_text("active\n")
    b.wait("bridge-changed Active=true")
    front_port.write_text("suspended\n")
    b.wait("bridge-changed Active=false")
    assert b.quit() == 0


# ----- the phone's orientation -----

def test_the_orientation_follows_the_accelerometer(start):
    b = start()
    open_camera(b, "0")
    open_camera(b, "1")
    b.wait("sensorproxy ClaimAccelerometer")
    b.wait("sensorproxy get normal")
    assert len(b.lines("sensorproxy ClaimAccelerometer")) == 1   # once for all cameras
    for value, degrees in [("left-up", 90), ("bottom-up", 180), ("right-up", 270), ("normal", 0), ("left-up", 90)]:
        b.cmd(f"orient {value}")
        wait_rotation(b, "hal_camera_0", degrees)
    # Lying flat: the last orientation stays
    since = b.cmd("orient undefined")
    b.wait("sensorproxy get undefined", since=since)
    wait_rotation(b, "hal_camera_0", 90)
    # The video transform of each camera's frames: the front camera looks the other way
    for cam, transform in [("0", 1), ("1", 3)]:
        b.cmd(f"frame {cam}")
        since = b.cmd(f"process hal_camera_{cam}")
        assert f" transform={transform} " in b.lines("pw queue", since)[0]
    stop_camera(b, "0")
    stop_camera(b, "1")
    b.wait("sensorproxy ReleaseAccelerometer")
    # Released: portrait until the next claim reads it again
    wait_rotation(b, "hal_camera_0", 0)
    assert b.quit() == 0


def test_without_iio_sensor_proxy_the_orientation_stays_portrait(start):
    b = start(HARNESS_NO_SENSORPROXY=1)
    open_camera(b, "0")
    b.wait("accelerometer claim: ", err=True)
    b.cmd("orient left-up")
    time.sleep(0.2)
    assert wait_rotation(b, "hal_camera_0", 0)
    stop_camera(b, "0")
    b.wait("accelerometer release: ", err=True)
    assert b.quit() == 0


@pytest.mark.parametrize("system", [True, False])
def test_a_wakeup_that_cannot_be_read_is_only_logged(start, bus, system):
    b = start(fail="read_error@1", system=system)
    open_camera(b, "1")
    b.wait("cannot read the wakeup: Bad file descriptor", err=True)
    b.wait("bridge-changed Active=true")
    assert b.quit() == 0


# ----- buses missing -----

def test_without_a_system_bus(start, bus, tmp_path):
    b = start(system=False)
    b.wait("no system bus, the camera orientation stays portrait", err=True)
    b.wait("pw registry_add_listener")
    b.cmd("global 60 client pid=777")
    b.cmd("global 61 node client=60")
    b.cmd("global 62 link out=101 in=61")
    # The session bus still has the bridge: Active and photos
    open_camera(b, "1")
    b.wait("bridge-changed Active=true")
    answer, _ = take(b, bus, tmp_path / "p.jpg", "hal_camera_1")
    b.cmd("still 1")
    assert answer() == (True, "()")
    # No accelerometer, and plinius-appaccess is not reached (the use is dropped quietly)
    time.sleep(0.2)
    assert not b.lines("sensorproxy ") and not b.lines("appaccess ")
    assert not b.errors("not counted")
    assert b.quit() == 0


def test_without_a_session_bus(start, bus):
    b = start(session=False)
    b.wait("no session bus, the shell cannot show the camera in use", err=True)
    open_camera(b, "0")
    b.wait("sensorproxy ClaimAccelerometer")
    b.cmd("orient left-up")
    wait_rotation(b, "hal_camera_0", 90)
    ok, out = gdbus(bus, "org.freedesktop.DBus.Properties.Get", f"'{BRIDGE}'", "'Active'", timeout=5)
    assert not ok
    assert b.quit() == 0


def test_without_any_bus_the_cameras_still_work(start):
    b = start(session=False, system=False)
    b.wait("no system bus", err=True)
    b.wait("no session bus", err=True)
    open_camera(b, "0")
    since = b.cmd("frame 0")
    assert b.lines("pw trigger", since)
    stop_camera(b, "0")
    assert b.quit() == 0


def test_another_owner_of_the_bridge_name(start, bus):
    b = start(HARNESS_TAKE_BRIDGE_NAME=1)
    b.wait("no session bus, the shell cannot show the camera in use", err=True)
    assert b.quit() == 0


# ----- who a camera streams to -----

def consumer(b, client, pid, node_out="100", portal=False):
    """A client with a node linked to the output node (ids client, client + 1, client + 2)"""
    b.cmd(f"global {client} client pid={pid}" + (" access=portal" if portal else ""))
    b.cmd(f"global {client + 1} node client={client}")
    b.cmd(f"global {client + 2} link out={node_out} in={client + 1}")


def test_uses_are_counted_per_app(start):
    b = start()
    b.wait("pw registry_add_listener")
    # Snapshot through the camera portal: its process is the portal's, the app id in the
    # client's own properties
    since = b.mark()
    consumer(b, 50, 1234, portal=True)
    assert b.lines("pw ", since) == ["pw bind 50 PipeWire:Interface:Client", "pw proxy_listener 50"]
    b.cmd("clientinfo 50 org.gnome.Snapshot")
    # A portal client whose info has no app id (yet): its process
    consumer(b, 90, 55, portal=True)
    b.cmd("clientinfo 90 -")
    b.cmd("clientinfo 90 none")
    # A plain client (a tool): its process
    consumer(b, 60, 777)
    # Not this camera's: a link from another node, to a node of an unknown client, to an unknown
    # node; globals without properties and of other types are ignored
    b.cmd("global 70 link out=101 in=51")
    b.cmd("global 71 node client=999")
    b.cmd("global 72 link out=100 in=71")
    b.cmd("global 73 link out=100 in=999")
    b.cmd("global 74 client -")
    b.cmd("global 75 port node=100")
    b.cmd("global 76 link out=100")
    b.cmd("global 77 node")
    open_camera(b, "0")
    uses = b.wait("appaccess CountUse", count=3)
    assert sorted(uses) == ["appaccess CountUse app= pid=55 what=camera",
                            "appaccess CountUse app= pid=777 what=camera",
                            "appaccess CountUse app=org.gnome.Snapshot pid=1234 what=camera"]
    # The client goes, its proxy with it
    since = b.cmd("remove 50")
    assert b.lines("pw proxy_destroy", since) == ["pw proxy_destroy 50 listeners=0"]
    stop_camera(b, "0")
    since = open_camera(b, "0")
    uses = b.wait("appaccess CountUse", count=2, since=since)
    time.sleep(0.2)
    assert sorted(b.lines("appaccess CountUse", since)) == [
        "appaccess CountUse app= pid=55 what=camera", "appaccess CountUse app= pid=777 what=camera"]
    since = b.cmd("quit")
    assert b.finish() == 0
    # The proxies still bound go at the end, their listeners first
    assert b.lines("pw proxy_destroy", since) == ["pw proxy_destroy 90 listeners=0"]


@pytest.mark.parametrize("missing", [False, True])
def test_uses_not_counted_without_appaccess(start, missing):
    b = start(**({"HARNESS_NO_APPACCESS": 1} if missing else {"HARNESS_APPACCESS_FAIL": 1}))
    b.wait("pw registry_add_listener")
    consumer(b, 60, 777)
    open_camera(b, "0")
    b.wait("not counted (, 777): ", err=True)
    if not missing:
        b.wait("appaccess CountUse app= pid=777 what=camera")
        b.wait("not counted (, 777): refused by the test", err=True)
    # The next use tries again, on a new connection
    stop_camera(b, "0")
    open_camera(b, "0")
    b.wait("not counted (, 777): ", err=True, count=2)
    assert b.quit() == 0


@pytest.mark.parametrize("fail", ["pw_context_new", "pw_context_connect", "pw_core_get_registry"])
def test_without_the_registry_uses_are_not_counted(start, fail):
    b = start(fail=fail)
    b.wait("no registry, camera uses are not counted per app", err=True)
    open_camera(b, "0")
    since = b.cmd("frame 0")
    assert b.lines("pw trigger", since)
    since = b.cmd("quit")
    assert b.finish() == 0
    assert not b.lines("pw registry_destroy") and not b.lines("pw core_disconnect")


def test_a_portal_client_that_cannot_be_bound_counts_by_its_process(start):
    b = start(fail="pw_registry_bind")
    b.wait("pw registry_add_listener")
    since = b.mark()
    consumer(b, 50, 1234, portal=True)
    assert b.lines("pw ", since) == ["pw bind 50 PipeWire:Interface:Client"]
    open_camera(b, "0")
    b.wait("appaccess CountUse app= pid=1234 what=camera")
    assert b.quit() == 0


def test_a_stream_without_a_node_id_reports_nobody(start):
    b = start()
    b.wait("pw registry_add_listener")
    consumer(b, 60, 777)
    b.cmd("nodeid hal_camera_0 invalid")
    open_camera(b, "0")
    stop_camera(b, "0")
    b.cmd("nodeid hal_camera_0 100")
    open_camera(b, "0")
    b.wait("appaccess CountUse app= pid=777 what=camera")
    time.sleep(0.2)
    assert len(b.lines("appaccess CountUse")) == 1
    assert b.quit() == 0


def test_a_full_registry_table_drops_new_globals(start):
    b = start()
    b.wait("pw registry_add_listener")
    b.cmd("globals 1000 512")
    consumer(b, 60, 777)
    open_camera(b, "0")
    stop_camera(b, "0")
    # Room again
    for i in range(3):
        b.cmd(f"remove {1000 + i}")
    consumer(b, 60, 777)
    open_camera(b, "0")
    b.wait("appaccess CountUse app= pid=777 what=camera")
    time.sleep(0.2)
    assert len(b.lines("appaccess CountUse")) == 1
    assert b.quit() == 0


# ----- single functions -----

@pytest.fixture
def check(built, bus):
    d, exe = built

    def check(*args, fail=None, **extra):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(d), "XDG_RUNTIME_DIR": str(d / "run"),
               "DBUS_SESSION_BUS_ADDRESS": bus, "DBUS_SYSTEM_BUS_ADDRESS": f"unix:path={d}/no-such-bus",
               "PIPEWIRE_DEBUG": "4", "PIPEWIRE_LOG_SYSTEMD": "false"}
        if fail:
            env["HARNESS_FAIL"] = fail
        env.update(extra)
        return run(exe, *args, env=env)
    return check


def test_write_jpeg_goes_on_after_an_interrupted_write(check, tmp_path):
    target = tmp_path / "p.jpg"
    assert kv(check(f"jpeg:{target}", fail="write_eintr@1").stdout)["jpeg"] == "0"
    assert target.read_bytes() == bytes([0xff, 0xd8, 1, 2, 3, 0xff, 0xd9])


def test_write_jpeg_that_writes_nothing(check, tmp_path):
    target = tmp_path / "p.jpg"
    assert kv(check(f"jpeg:{target}", fail="write_zero@1").stdout)["jpeg"] == str(-5)   # -EIO
    assert list(tmp_path.iterdir()) == []


def test_write_jpeg_onto_a_folder(check, tmp_path):
    target = tmp_path / "folder"
    (target / "inside").mkdir(parents=True)
    assert kv(check(f"jpeg:{target}").stdout)["jpeg"] == str(-17)   # -EEXIST: nothing is replaced
    assert sorted(p.name for p in tmp_path.iterdir()) == ["folder"]


def test_write_jpeg_with_a_name_too_long(check):
    assert kv(check("longjpeg").stdout)["jpeg"] == str(-36)   # -ENAMETOOLONG


@pytest.mark.parametrize("lines,pictures", [
    (None, "{home}/Pictures"),                                 # no user-dirs.dirs: the default
    (['XDG_PICTURES_DIR="$HOME/Bilder"'], "{home}/Bilder"),
    (['XDG_PICTURES_DIR="$HOME/Bilder/"'], "{home}/Bilder"),
    (['  XDG_PICTURES_DIR = "/media/card/Fotos//"'], "/media/card/Fotos"),
    (['XDG_PICTURES_DIR="$HOMEFotos"'], "{home}/Fotos"),      # as GLib reads it
    # The last line counts; what is no assignment of an absolute or $HOME path is passed over
    (['XDG_PICTURES_DIR="$HOME/A"', 'XDG_PICTURES_DIR="$HOME/B"'], "{home}/B"),
    (['# XDG_PICTURES_DIR="/x"', 'XDG_PICTURES_DIRS="/x"', 'XDG_PICTURES_DIR "/x"',
      'XDG_PICTURES_DIR=/x', 'XDG_PICTURES_DIR="relative"', 'XDG_PICTURES_DIR="/x',
      'XDG_VIDEOS_DIR="/x"'], "{home}/Pictures"),
    # The home or / as the pictures folder: no photo anywhere
    (['XDG_PICTURES_DIR="$HOME/"'], "-"),
    (['XDG_PICTURES_DIR="$HOME"'], "-"),
    (['XDG_PICTURES_DIR="/"'], "-"),
    (['XDG_PICTURES_DIR="' + "/x" * 2100 + '"'], "-"),        # too long for a path
])
def test_the_pictures_folder(check, tmp_path, lines, pictures):
    home = tmp_path / "home"
    if lines is not None:
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "user-dirs.dirs").write_text("\n".join(lines) + "\n")
    out = kv(check("pictures", HOME=str(home), XDG_CONFIG_HOME=str(tmp_path / "config")).stdout)
    assert out["pictures"] == pictures.format(home=home)


def test_the_pictures_folder_needs_a_home(check, tmp_path):
    (tmp_path / ".config").mkdir()
    (tmp_path / ".config" / "user-dirs.dirs").write_text('XDG_PICTURES_DIR="$HOME/Bilder"\n')
    # Without XDG_CONFIG_HOME (or a relative one) the file is in ~/.config; a home with a
    # trailing slash is still the home
    assert kv(check("pictures", HOME=str(tmp_path), XDG_CONFIG_HOME="config").stdout)["pictures"] == \
        f"{tmp_path}/Bilder"
    (tmp_path / ".config" / "user-dirs.dirs").write_text('XDG_PICTURES_DIR="$HOME/"\n')
    assert kv(check("pictures", HOME=f"{tmp_path}//").stdout)["pictures"] == "-"
    assert kv(check("pictures", HOME="").stdout)["pictures"] == "-"
    assert kv(check("pictures", HOME="home").stdout)["pictures"] == "-"
    assert kv(check("pictures", HOME="/" + "h" * 4100).stdout)["pictures"] == "-"


def target(check, pictures, path):
    """picture_target of path with pictures as the pictures folder: (result, name, folder)"""
    config = pictures.parent / "config"
    config.mkdir(exist_ok=True)
    (config / "user-dirs.dirs").write_text(f'XDG_PICTURES_DIR="{pictures}"\n')
    out = kv(check(f"target:{path}", XDG_CONFIG_HOME=str(config)).stdout)
    return int(out["target"]), out["name"], out["folder"]


def test_a_photo_goes_only_into_the_pictures_folder(check, tmp_path):
    """Attack review 2026-11-22, S36: TakePicture wrote to any path the caller named"""
    pics = tmp_path / "Pictures"
    (pics / "Camera").mkdir(parents=True)
    # As Snapshot stores its photos, and right in the folder; .jpg too, in any case
    name = "Photo from 2026-11-22 10-00-00.123456.jpeg"
    assert target(check, pics, pics / "Camera" / name) == (0, name, str(pics / "Camera"))
    assert target(check, pics, pics / "x.JPG") == (0, "x.JPG", str(pics))
    # Anything else is refused
    (tmp_path / ".ssh").mkdir()
    for path in (tmp_path / ".ssh" / "authorized_keys", tmp_path / ".ssh" / "x.jpg",
                 f"{pics}X/x.jpg",                  # only a prefix of the folder's name
                 f"{pics}/Camera/../../.ssh/x.jpg", f"{pics}/./x.jpg", f"{pics}//x.jpg",
                 f"{pics}/Camera/authorized_keys", f"{pics}/Camera/.jpg", f"{pics}/x.jpg.png",
                 f"{pics}/Camera/", pics):
        assert target(check, pics, path) == (-13, "-", "-"), path   # -EACCES
    # A folder below that is a symlink is not followed, even into the pictures folder
    (pics / "keys").symlink_to(tmp_path / ".ssh")
    (pics / "same").symlink_to(pics / "Camera")
    for link in ("keys", "same"):
        res = target(check, pics, pics / link / "x.jpg")[0]
        assert res in (-20, -40), (link, res)                     # -ENOTDIR / -ELOOP
    assert target(check, pics, pics / "none" / "x.jpg")[0] == -2    # -ENOENT
    assert target(check, pics, pics / ("x" * 252 + ".jpg"))[0] == -36   # no room for .part
    assert target(check, pics, pics / ("x" * 246 + ".jpg"))[0] == 0
    # The pictures folder itself may be a symlink (onto the memory card): the user's choice
    card = tmp_path / "card"
    card.mkdir()
    (tmp_path / "Bilder").symlink_to(card)
    assert target(check, tmp_path / "Bilder", tmp_path / "Bilder" / "x.jpg") == (0, "x.jpg", str(card))
    # … but there has to be one, and the home is none
    assert target(check, tmp_path / "gone", tmp_path / "gone" / "x.jpg")[0] == -2
    (tmp_path / "config" / "user-dirs.dirs").write_text('XDG_PICTURES_DIR="$HOME/"\n')
    out = kv(check(f"target:{tmp_path}/x.jpg", HOME=str(tmp_path), XDG_CONFIG_HOME=str(tmp_path / "config")).stdout)
    assert out["target"] == "-13"


def test_a_wakeup_that_cannot_be_written_is_logged(check):
    r = check("wake")
    assert "cannot wake the orientation thread: Bad file descriptor" in r.stderr


def test_the_use_queue_holds_sixteen(check):
    # Uses wait for the reporting thread; beyond 16 they are dropped, the first ones kept
    assert kv(check("queue:18").stdout)["queued"] == "16 first=500 last=515"


def test_messages_without_the_strings_the_vtable_says_are_refused(check):
    out = kv(check("badmsg").stdout)
    assert int(out["set"]) < 0 and int(out["take"]) < 0
