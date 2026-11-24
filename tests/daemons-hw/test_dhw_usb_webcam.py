# plinius-usb-webcam: the stream into the webcam function of plinius-usb. Which pipelines run for
# what plinius-usb tells (WebcamDevice, WebcamCamera) and what the computer wants (uvcsink's
# "streaming"), what they are made of, retrying after a failure, and ending when uvcsink keeps the
# node open. GStreamer is real but only its core elements are loaded: the pipelines are stand-ins
# (usb/dhw_usb_webcam_harness.c), no camera and no UVC device is reached. main() runs against a
# fake plinius-usb on a private bus (dhw_usb_fakeservice.h).

import os
import subprocess
import time
from pathlib import Path

import pytest

from dhw_harness import build_harness, run, sandboxed_source
from dhw_usb_phone import USB, Daemon, PrivateBus, Result

SRC = USB / "plinius-usb-webcam.c"
HERE = Path(__file__).resolve().parent
OUT = ('intervideosrc channel=plinius-usb-webcam ! videoconvert ! videoscale ! '
       'video/x-raw,pixel-aspect-ratio=1/1 ! jpegenc quality=85 ! '
       'capssetter caps="image/jpeg,parsed=(boolean)true,colorimetry=(string)2:4:7:1" ! '
       'uvcsink name=sink v4l2sink::device={}')
CAMERA = ("pipewiresrc target-object={} ! videoflip video-direction=auto ! "
          "intervideosink channel=plinius-usb-webcam")


def gst_env(d, env):
    """Only GStreamer's core elements, its registry in tmp: nothing of the host's plugins"""
    plugins = d / "gst-plugins"
    if not plugins.exists():
        plugins.mkdir()
        pc = subprocess.run(["pkg-config", "--variable=pluginsdir", "gstreamer-1.0"], capture_output=True,
                            text=True).stdout.strip()
        (plugins / "libgstcoreelements.so").symlink_to(f"{pc}/libgstcoreelements.so")
    env.update(GST_PLUGIN_SYSTEM_PATH_1_0=str(plugins), GST_PLUGIN_PATH_1_0="",
               GST_REGISTRY_1_0=str(d / "gst-registry.bin"), XDG_CACHE_HOME=str(d / "cache"),
               GST_DEBUG="0")
    return env


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    d = tmp_path_factory.mktemp("usb-webcam")
    sandboxed_source(SRC, d, [], allow=("/proc/self/fd",))
    code = (HERE / "usb" / "dhw_usb_webcam_harness.c").read_text()
    exe = build_harness(d, "webcam", code, flags=[f"-I{HERE / 'usb'}", "-Wno-unused-parameter"],
                        pkgs=("gio-unix-2.0", "gstreamer-1.0", "gstreamer-base-1.0"))
    return d, exe


class Webcam:
    def __init__(self, d, exe, tmp):
        self.d, self.exe = d, exe
        self.cwd = tmp
        self.tmp = tmp / "dev"
        self.tmp.mkdir()

    def node(self, name="video3", major=81, minor=3):
        """The gadget's video node (a file the harness takes for a character device), named
        relative to the harness's directory: a node name has at most 64 characters"""
        (self.tmp / name).write_text(f"chardev {major}:{minor}\n")
        return f"dev/{name}"

    def env(self):
        return gst_env(self.d, dict(os.environ))

    def run(self, *commands, check=True):
        return Result(run(self.exe, *commands, env=self.env(), check=check, timeout=60, cwd=self.cwd))


@pytest.fixture
def cam(built, tmp_path):
    return Webcam(*built, tmp_path)


def on(cam, *more, camera="hal_camera_1", check=True):
    node = cam.node()
    return node, cam.run(f"prop WebcamDevice {node}", f"prop WebcamCamera {camera}", "update", *more,
                         check=check)


# ---- The out pipeline: as long as there is a node ----

def test_no_node_no_pipeline(cam):
    r = cam.run("prop WebcamDevice -", "update", "state")
    assert r.of("update") == []
    assert r.of("state") == ["state device=- out=0 camera=- streaming=0 retry=0 retry_s=0 rdev=0"]
    assert "Webcam off" in r.stderr


def test_the_node_gets_the_out_pipeline(cam):
    node, r = on(cam, "state")
    # The camera only once the computer streams
    assert r.of("update") == [f"pipeline {OUT.format(node)}"]
    assert r.of("state") == [f"state device={node} out=1 camera=- streaming=0 retry=0 retry_s=0 rdev=set"]
    assert f"Webcam on {node}" in r.stderr


@pytest.mark.parametrize("device", ["/dev/video3;rm", "/dev/video 3", "x" * 65, "/dev/$(id)"])
def test_a_node_name_that_is_not_safe(cam, device):
    r = cam.run(f"prop WebcamDevice {device}", "update", "state")
    assert r.of("update") == []
    assert r.of("state")[0].startswith("state device=- out=0")


@pytest.mark.parametrize("value", ["nil", "int"])
def test_a_property_missing_or_of_another_type(cam, value):
    r = cam.run(f"prop WebcamDevice {value}", "update", "state")
    assert r.of("state")[0].startswith("state device=- out=0")


def test_without_a_proxy(cam):
    r = cam.run("proxy none", "update", "state")
    assert r.of("state")[0].startswith("state device=- out=0")


def test_a_node_not_there_yet_is_retried(cam):
    node = "dev/video3"
    r = cam.run(f"prop WebcamDevice {node}", "update", "state", "advance 1", "state", "advance 1", "state",
                "advance 4", "advance 8", "advance 16", "state", "advance 30", "state")
    assert r.of("update") == []
    assert "retry=2 retry_s=2" in r.of("state")[0]
    assert "retry=1 retry_s=2" in r.of("state", 1)[0]
    # Twice as long each time, at most 30 s
    assert "retry=4 retry_s=4" in r.of("state", 2)[0]
    assert "retry=30 retry_s=30" in r.of("state", 3)[0]
    assert "retry=30 retry_s=30" in r.of("state", 4)[0]


def test_the_node_shows_up(cam):
    node = "dev/video3"
    r = cam.run(f"prop WebcamDevice {node}", "update", "advance 2", f"node {node}", "advance 4", "state")
    assert r.of("advance 2") == []
    assert r.of("advance 4") == [f"pipeline {OUT.format(node)}"]
    # Started: the next failure waits the shortest time again
    assert r.of("state") == [f"state device={node} out=1 camera=- streaming=0 retry=0 retry_s=0 rdev=set"]


def test_node_gone_stops_everything(cam):
    node, r = on(cam, "streaming 1", "prop WebcamDevice -", "update", "state")
    assert r.of("update", 1) == []
    assert r.of("state") == ["state device=- out=0 camera=- streaming=0 retry=0 retry_s=0 rdev=0"]
    assert "Camera hal_camera_1 off" in r.stderr
    assert "Webcam off" in r.stderr


def test_a_new_node_restarts_the_out_pipeline(cam):
    node, r = on(cam)
    other = cam.node("video4", minor=4)
    r = cam.run(f"prop WebcamDevice {node}", "update", f"prop WebcamDevice {other}", "update", "state")
    assert r.of("update", 1) == [f"pipeline {OUT.format(other)}"]
    assert f"device={other} out=1" in r.of("state")[0]


def test_the_same_node_changes_nothing(cam):
    node, r = on(cam, "update")
    assert r.of("update", 1) == []


def test_a_regular_file_is_no_node_to_watch(cam):
    node = "dev/video3"
    (cam.tmp / "video3").write_text("not a device\n")
    r = cam.run(f"prop WebcamDevice {node}", "update", "state")
    assert "out=1" in r.of("state")[0]
    assert "rdev=0" in r.of("state")[0]


# ---- The camera pipeline: only while the computer takes the picture ----

def test_streaming_starts_the_camera(cam):
    node, r = on(cam, "streaming 1", "state", "streaming 0", "state")
    assert r.of("streaming 1") == [f"pipeline {CAMERA.format('hal_camera_1')}"]
    assert "camera=hal_camera_1 streaming=1" in r.of("state")[0]
    assert r.of("streaming 0") == []
    assert "camera=- streaming=0" in r.of("state", 1)[0]
    assert "The computer takes the picture" in r.stderr
    assert "The computer no longer takes the picture" in r.stderr
    assert "Camera hal_camera_1 on" in r.stderr
    assert "Camera hal_camera_1 off" in r.stderr


def test_switching_cameras_restarts_only_the_camera(cam):
    node, r = on(cam, "streaming 1", "prop WebcamCamera hal_camera_0", "update", "state")
    assert r.of("update", 1) == [f"pipeline {CAMERA.format('hal_camera_0')}"]
    assert f"device={node} out=1 camera=hal_camera_0 streaming=1" in r.of("state")[0]


@pytest.mark.parametrize("camera", ["nil", "hal;camera", "-"])
def test_no_camera_without_a_safe_name(cam, camera):
    node, r = on(cam, "streaming 1", "state", camera=camera)
    assert r.of("streaming 1") == []
    assert "camera=- streaming=1" in r.of("state")[0]


def test_a_streaming_notification_that_changes_nothing(cam):
    node, r = on(cam, "idle", "state")
    assert r.of("idle") == []
    r = cam.run("idle", "state")
    assert "out=0" in r.of("state")[0]


# ---- Failures ----

def test_an_error_of_the_out_pipeline(cam):
    node, r = on(cam, "streaming 1", "message out error", "state", "advance 2", "state")
    assert r.of("message out error") == []
    assert "state device=- out=0 camera=- streaming=0 retry=2" in r.of("state")[0]
    assert "Out pipeline: Device is busy (dhw debug)" in r.stderr
    # Tried again: the node is still the one plinius-usb names
    assert r.of("advance 2") == [f"pipeline {OUT.format(node)}"]
    assert f"device={node} out=1" in r.of("state", 1)[0]


def test_the_out_pipeline_ends(cam):
    node, r = on(cam, "message out eos", "state")
    assert "out=0" in r.of("state")[0]
    assert "Out pipeline ended" in r.stderr


def test_an_error_of_the_camera_pipeline(cam):
    node, r = on(cam, "streaming 1", "message-nodebug camera", "state", "advance 2", "state")
    # Only the camera stops; the computer keeps its device
    assert f"device={node} out=1 camera=- streaming=1 retry=2" in r.of("state")[0]
    assert "Camera pipeline: Gone ()" in r.stderr
    assert r.of("advance 2") == [f"pipeline {CAMERA.format('hal_camera_1')}"]


def test_the_camera_pipeline_ends(cam):
    node, r = on(cam, "streaming 1", "message camera eos", "state")
    assert "camera=- streaming=1" in r.of("state")[0]
    assert "Camera pipeline ended" in r.stderr


def test_other_messages_are_no_failure(cam):
    node, r = on(cam, "streaming 1", "message out info", "message camera info", "state")
    assert f"device={node} out=1 camera=hal_camera_1 streaming=1 retry=0" in r.of("state")[0]


@pytest.mark.parametrize("which,how,warning", [
    ("out", "parse", 'Cannot build intervideosrc'),
    ("out", "start", "Cannot start intervideosrc"),
])
def test_an_out_pipeline_that_cannot_start(cam, which, how, warning):
    node = cam.node()
    r = cam.run(f"fail {which} {how}", f"prop WebcamDevice {node}", "update", "state", "advance 2", "state")
    assert "out=0" in r.of("state")[0] and "retry=2" in r.of("state")[0]
    assert warning in r.stderr
    assert f"device={node} out=1" in r.of("state", 1)[0]


@pytest.mark.parametrize("how,warning", [("parse", 'no element "pipewiresrc"'),
                                         ("start", "Cannot start pipewiresrc")])
def test_a_camera_that_cannot_start(cam, how, warning):
    node, r = on(cam, f"fail camera {how}", "streaming 1", "state", "advance 2", "state")
    assert "camera=- streaming=1 retry=2" in r.of("state")[0]
    assert warning in r.stderr
    assert "camera=hal_camera_1" in r.of("state", 1)[0]


def test_one_retry_at_a_time(cam):
    node, r = on(cam, "streaming 1", "message camera eos", "message out eos", "state")
    # Both failed, one timer
    assert "retry=2 retry_s=2" in r.of("state")[0]


# ---- uvcsink keeps the node open ----

def test_the_helper_ends_when_the_node_stays_open(cam):
    node, r = on(cam, f"hold {'dev/video3'}", "streaming 1", "prop WebcamDevice -", "update", "state",
                 check=False)
    # systemd starts it again (Restart=on-failure), which closes every fd
    assert r.returncode == 75
    assert "uvcsink keeps the webcam node open: ending, so that it closes" in r.stderr
    assert "> state" not in r.stdout


def test_the_helper_ends_after_an_error_with_the_node_open(cam):
    node, r = on(cam, f"hold {'dev/video3'}", "message out error", "state", check=False)
    assert r.returncode == 75


def test_a_start_that_failed_with_the_node_open(cam):
    node = cam.node()
    r = cam.run(f"hold {node}", "fail out start", f"prop WebcamDevice {node}", "update", check=False)
    assert r.returncode == 75


def test_another_device_held_is_not_the_node(cam):
    node, r = on(cam, f"hold {cam.node('video9', minor=9)}", "prop WebcamDevice -", "update", "state")
    assert r.of("state")[0].startswith("state device=- out=0")


# ---- main() on a private bus ----

@pytest.fixture
def bus(tmp_path):
    b = PrivateBus(tmp_path)
    yield b
    b.close()


def helper(cam, address, *properties):
    """The program's main() with its environment; *properties: those of the fake plinius-usb
    ("-": none runs)"""
    class Program:
        exe = cam.exe
        env = cam.env
    return Daemon(Program, address, args=("daemon", *(properties or ["-"])), cwd=cam.cwd)


def wait_for(predicate, d, timeout=10):
    end = time.monotonic() + timeout * d.slow
    while not predicate():
        assert time.monotonic() < end, d.stderr
        time.sleep(0.02)


def test_main_follows_plinius_usb(cam, bus):
    node = cam.node()
    d = helper(cam, bus.address, "WebcamDevice=", "WebcamCamera=hal_camera_1")
    try:
        d.command(f"svc set WebcamDevice {node}")
        assert d.wait_line(lambda line: line.startswith("pipeline ")) == f"pipeline {OUT.format(node)}"
        # plinius-usb goes (a restart): its properties go with it, and come back with it
        d.command("svc release")
        wait_for(lambda: "Webcam off" in d.stderr, d)
        d.command("svc own")
        assert d.wait_line(lambda line: line.startswith("pipeline ")) == f"pipeline {OUT.format(node)}"
        d.command("quit")
        assert d.finish() == 0
    finally:
        d.kill()


def test_main_without_plinius_usb_running(cam, bus):
    d = helper(cam, bus.address)
    try:
        d.command("quit")
        assert d.finish() == 0
        assert "Webcam off" in d.stderr
    finally:
        d.kill()


def test_main_without_a_bus(cam, tmp_path):
    d = helper(cam, f"unix:path={tmp_path}/no-bus")
    try:
        assert d.finish() == 1
        assert "Cannot reach org.plinius.Usb" in d.stderr
    finally:
        d.kill()
