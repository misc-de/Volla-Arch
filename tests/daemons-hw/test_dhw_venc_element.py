# pliniush264enc of plinius-vdec as GStreamer runs it: the real element (gstpliniusvenc.c) in a
# GstHarness against a fake MediaTek encoder and dma-heap (vdec/venc_element_harness.c). Checked:
# what it asks of the driver (formats, controls, buffers), that every frame goes in with the
# driver's pitch and comes out as its picture with its timestamp and key flag, the defaults by
# size, draining, a new format, and every way the device can fail.

import os
import re
from pathlib import Path

import pytest

from dhw_harness import PKG, build_harness, run, sandboxed_source

HERE = Path(__file__).resolve().parent
VDEC = PKG / "plinius-vdec"


@pytest.fixture(scope="module")
def exe(tmp_path_factory):
    d = tmp_path_factory.mktemp("venc-element")
    sandboxed_source(VDEC / "gstpliniusvenc.c", d, [
        ('"/dev/dma_heap/mtk_mm"', '"dev/dma_heap/mtk_mm"'),
        ('"/sys/class/video4linux"', '"sys/class/video4linux"'),
        ('g_build_filename("/dev", entry', 'g_build_filename("dev", entry'),
    ])
    return build_harness(d, "venc", (HERE / "vdec" / "venc_element_harness.c").read_text(),
                         flags=["-Wno-unused-parameter", "-Wno-missing-field-initializers"],
                         pkgs=("gstreamer-1.0", "gstreamer-video-1.0", "gstreamer-check-1.0"))


@pytest.fixture
def root(tmp_path):
    """The phone's files as far as the element looks at them"""
    r = tmp_path / "root"
    v4l = r / "sys/class/video4linux"
    for entry, name in (("video0", "other-device\n"), ("video1", None), ("video3", "mtk-vcodec-enc\n")):
        (v4l / entry).mkdir(parents=True)
        if name:
            (v4l / entry / "name").write_text(name)
    (r / "dev/dma_heap").mkdir(parents=True)
    for f in ("dev/video3", "dev/dma_heap/mtk_mm"):
        (r / f).write_bytes(b"")
    return r


class Result:
    def __init__(self, out, log):
        self.out = out.splitlines()
        self.log = log.read_text().splitlines() if log.exists() else []
        self.pictures = [m.groupdict() for m in
                         (re.match(r"picture pts=(?P<pts>\d+) key=(?P<key>\d) (?P<data>.*)", l) for l in self.out) if m]

    def lines(self, prefix):
        return [line for line in self.out if line.startswith(prefix)]

    def logged(self, prefix):
        return [line for line in self.log if line.startswith(prefix)]


def harness(exe, root, script, **fake):
    tmp = root.parent
    (tmp / "script").write_text("\n".join(script) + "\n")
    log = tmp / "fake.log"
    log.unlink(missing_ok=True)
    env = dict(os.environ, FAKE_LOG=str(log), HOME=str(tmp), XDG_CACHE_HOME=str(tmp / "cache"),
               GST_REGISTRY_1_0=str(tmp / "registry.bin"), GST_PLUGIN_SYSTEM_PATH_1_0="",
               GST_PLUGIN_PATH_1_0="", GST_DEBUG="0")
    env.update({k: str(v) for k, v in fake.items()})
    return Result(run(exe, tmp / "script", env=env, cwd=root, timeout=60).stdout, log)


def sums(k, w, h):
    """What the fake reads from frame k: the sums of the visible Y and UV bytes, the first of each"""
    y = sum((k + x + yy) % 256 for yy in range(h) for x in range(w))
    uv = sum((k + 2 * x + yy) % 256 for yy in range(h // 2) for x in range(w))
    return f"y={y} uv={uv} first={k % 256},{k % 256}"


def caps(w, h, fmt="NV12", rate="30/1"):
    return f"caps video/x-raw,format={fmt},width={w},height={h},framerate={rate}"


FRAME = 1_000_000_000 // 30


# ---- Registration ----

def test_registered_above_the_software_encoders(exe, root):
    res = harness(exe, root, [])
    assert res.lines("registered") == ["registered 1"]
    assert res.lines("rank") == ["rank 258"]   # primary + 2


def test_registered_only_where_the_encoder_is(exe, root):
    (root / "sys/class/video4linux/video3/name").write_text("mtk-vcodec-dec\n")
    assert harness(exe, root, []).lines("registered") == ["registered 0"]


def test_not_registered_without_video4linux(exe, root):
    import shutil
    shutil.rmtree(root / "sys/class/video4linux")
    assert harness(exe, root, []).lines("registered") == ["registered 0"]


# ---- Encoding ----

def test_frames_in_pictures_out(exe, root):
    res = harness(exe, root, ["element", caps(160, 128), "push 1", "push 2", "push 3", "eos"])
    assert res.lines("push") == ["push ok"] * 3
    assert res.lines("eos") == ["eos 1"]
    # Each frame read with the driver's pitch, out as its picture with its time; the first a key
    # frame (the GOP of one second), the frames in two planes of their full size
    assert [p["pts"] for p in res.pictures] == [str(i * FRAME) for i in range(3)]
    assert [p["key"] for p in res.pictures] == ["1", "0", "0"]
    assert [p["data"] for p in res.pictures] == [
        f"pic {i} {sums(i + 1, 160, 128)} bytesused={160 * 128}/{160 * 64}" for i in range(3)]
    assert res.logged("open") == ["open dev/video3"]
    assert res.logged("s_fmt") == ["s_fmt bitstream H264 160x128 size 4194304", "s_fmt frames NM12 160x128 pitch 160"]
    assert res.logged("s_parm") == ["s_parm 30/1"]
    # 720p and smaller: 9 Mbit/s, High profile at level 3.1, the parameter sets with the first
    # picture, a key frame each second
    assert res.logged("ctrl") == ["ctrl bitrate 9000000", "ctrl profile 4", "ctrl level 9", "ctrl header 1",
                                  "ctrl gop 30"]
    assert res.logged("reqbufs") == ["reqbufs frames 4 dmabuf", "reqbufs bitstream 4 mmap"]
    # One dma-buf per plane of each frame buffer
    assert res.logged("alloc") == [f"alloc {160 * 128}", f"alloc {160 * 64}"] * 4
    assert res.logged("stream") == ["streamon frames", "streamon bitstream", "streamoff frames",
                                    "streamoff bitstream"]
    assert not res.logged("qbuf without")
    # Cache maintenance around the copy into each plane
    assert res.lines("syncs") == ["syncs 12"]


def test_nv21_with_lines_the_driver_pads(exe, root):
    # 1080 wide: the driver's lines are 1088 long (as on the phone), the frame's own are 1080
    res = harness(exe, root, ["element", caps(1080, 128, "NV21"), "push 5", "eos"])
    assert res.logged("s_fmt frames") == ["s_fmt frames NM21 1080x128 pitch 1088"]
    assert res.pictures[0]["data"].startswith(f"pic 0 {sums(5, 1080, 128)} ")


@pytest.mark.parametrize("w,h,bitrate,level,size", [
    (1280, 720, 9_000_000, 9, 4 << 20),
    (1920, 1080, 20_000_000, 12, 4 << 20),
    (1080, 1920, 20_000_000, 12, 4 << 20),
    (2160, 3840, 50_000_000, 15, 2160 * 3840),   # 4K upright: room for its larger pictures
])
def test_defaults_by_size(exe, root, w, h, bitrate, level, size):
    res = harness(exe, root, ["element", caps(w, h), "push 0", "eos"])
    assert res.logged("ctrl bitrate") == [f"ctrl bitrate {bitrate}"]
    assert res.logged("ctrl level") == [f"ctrl level {level}"]
    assert res.logged("s_fmt bitstream") == [f"s_fmt bitstream H264 {w}x{h} size {size}"]
    assert len(res.pictures) == 1


def test_bit_rate_and_key_frame_interval_from_the_properties(exe, root):
    res = harness(exe, root, ["element bitrate=5000 key-int-max=2", caps(160, 128, rate="25/1"),
                              *[f"push {k}" for k in range(5)], "eos"])
    assert res.lines("properties") == ["properties bitrate=5000 key-int-max=2"]
    assert res.logged("ctrl bitrate") == ["ctrl bitrate 5000000"]
    assert res.logged("ctrl gop") == ["ctrl gop 2"]
    assert res.logged("s_parm") == ["s_parm 25/1"]
    assert [p["key"] for p in res.pictures] == ["1", "0", "1", "0", "1"]


def test_a_rate_the_stream_does_not_say_is_taken_as_30(exe, root):
    res = harness(exe, root, ["element", caps(160, 128, rate="0/1"), "push 0", "eos"])
    assert res.logged("s_parm") == ["s_parm 30/1"]
    assert res.logged("ctrl gop") == ["ctrl gop 30"]


def test_a_forced_key_frame(exe, root):
    res = harness(exe, root, ["element", caps(160, 128), "push 0", "push 1", "push 2 key", "push 3", "eos"])
    assert res.logged("ctrl force-key") == ["ctrl force-key 1"]
    assert [p["key"] for p in res.pictures] == ["1", "0", "1", "0"]


def test_eos_waits_for_the_pictures_still_coming(exe, root):
    # Pictures that come later: the frames go in, the end waits for each picture
    res = harness(exe, root, ["element", caps(160, 128), "push 0", "push 1", "eos"], FAKE_LAZY=3)
    assert len(res.pictures) == 2
    assert res.lines("push") == ["push ok"] * 2


def test_more_frames_than_buffers(exe, root):
    # Two frame buffers only: each new frame waits for one the encoder gave back
    res = harness(exe, root, ["element", caps(160, 128), *[f"push {k}" for k in range(6)], "eos"],
                  FAKE_FRAMES=2, FAKE_LAZY=2)
    assert len(res.pictures) == 6
    assert res.logged("alloc") == [f"alloc {160 * 128}", f"alloc {160 * 64}"] * 2


def test_a_new_format_opens_the_encoder_again(exe, root):
    res = harness(exe, root, ["element", caps(160, 128), "push 0", caps(320, 160), "push 1", "eos"])
    assert res.logged("open") == ["open dev/video3", "open dev/video3"]
    assert res.logged("s_fmt frames") == ["s_fmt frames NM12 160x128 pitch 160", "s_fmt frames NM12 320x160 pitch 320"]
    assert [p["data"].split(" bytesused")[0] for p in res.pictures] == [f"pic 0 {sums(0, 160, 128)}",
                                                                         f"pic 1 {sums(1, 320, 160)}"]


def test_a_flush_closes_the_encoder_and_the_next_frame_opens_it(exe, root):
    res = harness(exe, root, ["element", caps(160, 128), "push 0", "flush", "push 1", "eos"])
    assert res.logged("open") == ["open dev/video3", "open dev/video3"]
    assert len(res.pictures) == 2


def test_the_bitstream_after_its_offset(exe, root):
    res = harness(exe, root, ["element", caps(160, 128), "push 0", "eos"], FAKE_OFFSET=7)
    assert res.pictures[0]["data"].startswith("pic 0 ")


def test_pictures_the_element_cannot_place(exe, root):
    # An empty picture, one for a frame it does not know, a buffer index out of range: left out
    res = harness(exe, root, ["element", caps(160, 128), "push 0", "push 1", "push 2", "push 3", "eos"],
                  FAKE_EMPTY=1, FAKE_STRANGER=2, FAKE_BAD_INDEX=1)
    assert [p["data"].split()[1] for p in res.pictures] == ["0", "3"]


# ---- Failures ----

def test_without_the_heap_it_does_not_start(exe, root):
    (root / "dev/dma_heap/mtk_mm").unlink()
    res = harness(exe, root, ["start"])
    assert res.lines("start") == ["start FAILURE"]
    # Ours first, then the base class's own
    assert res.lines("error")[0] == "error Cannot open dev/dma_heap/mtk_mm | No such file or directory"


def test_without_the_device(exe, root):
    res = harness(exe, root, ["element", "nodev", caps(160, 128), "push 0"])
    assert res.lines("push") == ["push error"]
    assert res.lines("error") == ["error Cannot use the hardware video encoder | no mtk-vcodec-enc device: not found"]


@pytest.mark.parametrize("fault,step", [
    ("open:1", "open"),
    ("s_fmt_bitstream:1", "VIDIOC_S_FMT (bitstream)"),
    ("s_fmt_frames:1", "VIDIOC_S_FMT (frames)"),
    ("reqbufs_frames:1", "VIDIOC_REQBUFS (frames)"),
    ("alloc:3", "frame buffers"),
    ("mmap_frame:2", "frame buffers"),
    ("reqbufs_bitstream:1", "VIDIOC_REQBUFS (bitstream)"),
    ("querybuf:2", "VIDIOC_QUERYBUF"),
    ("mmap_bitstream:1", "mapping a bitstream buffer"),
    ("qbuf_bitstream:3", "VIDIOC_QBUF (bitstream)"),
    ("streamon_frames:1", "VIDIOC_STREAMON"),
    ("streamon_bitstream:1", "VIDIOC_STREAMON"),
])
def test_the_encoder_cannot_be_set_up(exe, root, fault, step):
    res = harness(exe, root, ["element", caps(160, 128), "push 0"], FAKE_FAIL=fault)
    assert res.lines("push") == ["push error"]
    errors = res.lines("error")
    assert len(errors) == 1 and errors[0].startswith(f"error Cannot use the hardware video encoder | {step}: ")
    # What was opened is closed again
    assert len(res.logged("open")) == len(res.logged("close device"))


@pytest.mark.parametrize("fake", [{"FAKE_PLANES": 1}, {"FAKE_PITCH": 144}, {"FAKE_UV_PITCH": 144},
                                  {"FAKE_Y_SHORT": 1}, {"FAKE_UV_SHORT": 1}])
def test_a_frame_layout_that_does_not_fit(exe, root, fake):
    res = harness(exe, root, ["element", caps(160, 128), "push 0"], **fake)
    assert res.lines("error") == ["error Cannot use the hardware video encoder | the frame layout: Invalid argument"]


def test_no_buffers_at_all(exe, root):
    res = harness(exe, root, ["element", caps(160, 128), "push 0"], FAKE_BITSTREAMS=0)
    assert res.lines("error")[0].startswith("error Cannot use the hardware video encoder | VIDIOC_REQBUFS (bitstream)")


def test_settings_the_driver_refuses_are_only_warned_about(exe, root):
    res = harness(exe, root, ["element", caps(160, 128), "push 0", "eos"], FAKE_FAIL="s_ctrl:0,s_parm:0")
    assert len(res.pictures) == 1 and not res.lines("error")


def test_a_frame_the_driver_refuses(exe, root):
    res = harness(exe, root, ["element", caps(160, 128), "push 0", "push 1"], FAKE_FAIL="qbuf_frame:2")
    assert res.lines("push") == ["push ok", "push error"]
    assert res.lines("error") == ["error The hardware video encoder refused a frame | VIDIOC_QBUF: Input/output error"]


def test_a_bitstream_buffer_that_cannot_go_back(exe, root):
    res = harness(exe, root, ["element", caps(160, 128), "push 0"], FAKE_FAIL="qbuf_bitstream:5")
    assert res.lines("push") == ["push error"]


def test_the_encoder_that_fails(exe, root):
    res = harness(exe, root, ["element", caps(160, 128), "push 0"], FAKE_POLLERR=1)
    assert res.lines("error") == ["error The hardware video encoder failed | "]


def test_poll_that_fails(exe, root):
    res = harness(exe, root, ["element", caps(160, 128), "push 0"], FAKE_FAIL="poll:0:9")
    assert res.lines("push") == ["push error"]


def test_poll_that_fails_while_waiting_for_a_frame_buffer(exe, root):
    res = harness(exe, root, ["element", caps(160, 128), "push 0", "push 1"], FAKE_FRAMES=1, FAKE_HOLD=1,
                  FAKE_FAIL="poll:2:9")
    assert res.lines("push") == ["push ok", "push error"]


def test_poll_that_fails_while_draining(exe, root):
    res = harness(exe, root, ["element", caps(160, 128), "push 0", "eos"], FAKE_LAZY=100, FAKE_FAIL="poll:2:9")
    assert res.pictures == []


def test_an_encoder_that_keeps_the_frames(exe, root):
    # The frame buffers never come back: after the last free one, the next frame gives up
    res = harness(exe, root, ["element", caps(160, 128), *[f"push {k}" for k in range(3)]],
                  FAKE_FRAMES=2, FAKE_HOLD=1, FAKE_LAZY=1000)
    assert res.lines("push") == ["push ok", "push ok", "push error"]
    assert res.lines("error") == ["error The hardware video encoder stopped | no frame buffer free for 2000 ms"]


def test_pictures_that_never_come_end_the_stream_anyway(exe, root):
    res = harness(exe, root, ["element", caps(160, 128), "push 0", "eos"], FAKE_LAZY=100000)
    assert res.lines("eos") == ["eos 1"]
    assert res.pictures == []


def test_a_frame_that_cannot_be_read(exe, root):
    res = harness(exe, root, ["element", caps(160, 128), "pushshort"])
    assert res.lines("push") == ["push error"]
    assert res.lines("error") == ["error Cannot read the frame | "]


def test_stopped_while_open(exe, root):
    res = harness(exe, root, ["element", caps(160, 128), "push 0", "stop"])
    assert res.logged("close device") == ["close device"]
    assert res.logged("streamoff") == ["streamoff frames", "streamoff bitstream"]
