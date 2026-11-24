# plinius-vdec, the GStreamer element (gstpliniusvdec.c) in host GStreamer: GstHarness pads (and
# once a real appsrc ! pliniush264dec ! appsink pipeline) around the element, with a fake
# MediaTek decoder, fake dma-heaps and a fake MDP below it (vdec/vdec_element_harness.c). Every
# picture that comes out is compared with the one the fake decoder made from that access unit,
# computed here independently: crop, odd sizes, the zero-copy pool versus the copy into
# downstream buffers, the order and timestamps, draining, the parameter sets sent again after a
# flush, and every way the device can refuse. The device paths are relative in the test copy;
# each test runs in a directory of its own with sys/, dev/ and vendor/ as plain files.

import os
import re
from pathlib import Path

import pytest

from dhw_harness import PKG, build_harness, run, sandboxed_source

VDEC = PKG / "plinius-vdec"
HERE = Path(__file__).resolve().parent
MIB4 = 4 * 1024 * 1024
H264 = "video/x-h264, stream-format=byte-stream, alignment=au, profile=high"
H265 = "video/x-h265, stream-format=byte-stream, alignment=au, profile=main"
VP9 = "video/x-vp9, profile=(string)0"


@pytest.fixture(scope="module")
def exe(tmp_path_factory):
    d = tmp_path_factory.mktemp("vdec-element")
    sandboxed_source(VDEC / "gstpliniusvdec.c", d, [
        ('"/dev/dma_heap/mtk_mm"', '"dev/dma_heap/mtk_mm"'),
        ('"/dev/dma_heap/mtk_mm-uncached"', '"dev/dma_heap/mtk_mm-uncached"'),
        ('"/sys/class/video4linux"', '"sys/class/video4linux"'),
        ('g_build_filename("/dev", entry', 'g_build_filename("dev", entry'),
        ('"/vendor/lib64/mt6878/libdpframework.so"', '"vendor/lib64/mt6878/libdpframework.so"'),
        ('"/vendor/lib64/mt6878"', '"vendor/lib64/mt6878"'),
    ])
    return build_harness(d, "element", (HERE / "vdec" / "vdec_element_harness.c").read_text(),
                         flags=[f"-I{VDEC}", "-Wno-unused-parameter", "-Wno-missing-field-initializers"],
                         pkgs=("gstreamer-1.0", "gstreamer-video-1.0", "gstreamer-allocators-1.0",
                               "gstreamer-app-1.0", "gstreamer-check-1.0", "gio-2.0"))


@pytest.fixture
def root(tmp_path):
    """The phone's files as far as the element looks at them"""
    r = tmp_path / "root"
    v4l = r / "sys/class/video4linux"
    for entry, name in (("video0", "other-device\n"), ("video1", None), ("video3", "mtk-vcodec-dec\n")):
        (v4l / entry).mkdir(parents=True)
        if name:
            (v4l / entry / "name").write_text(name)
    (r / "dev/dma_heap").mkdir(parents=True)
    for f in ("dev/video3", "dev/dma_heap/mtk_mm", "dev/dma_heap/mtk_mm-uncached"):
        (r / f).write_bytes(b"")
    (r / "vendor/lib64/mt6878").mkdir(parents=True)
    (r / "vendor/lib64/mt6878/libdpframework.so").write_bytes(b"")
    (tmp_path / "plugins").mkdir()
    return r


class Result:
    def __init__(self, out, log, frames_file):
        self.out = out.splitlines()
        self.log = log.read_text().splitlines() if log.exists() else []
        self.syncs = int(next((line.split()[1] for line in self.out if line.startswith("syncs ")), -1))
        data = frames_file.read_bytes() if frames_file.exists() else b""
        self.frames = []
        for line in self.out:
            m = re.match(r"frame pts=(\d+) (\d+)x(\d+) stride (\d+)/(\d+) dmabuf=(\d) meta=(\d)", line)
            if m:
                pts, w, h, s0, s1, dmabuf, meta = map(int, m.groups())
                size = w * h + w * (h // 2)
                self.frames.append({"pts": pts, "w": w, "h": h, "strides": (s0, s1), "dmabuf": dmabuf,
                                    "meta": meta, "data": data[:size]})
                data = data[size:]

    def lines(self, prefix):
        return [line for line in self.out if line.startswith(prefix)]

    def logged(self, prefix):
        return [line for line in self.log if line.startswith(prefix)]


def harness(exe, root, script, plugins=(), **fake):
    tmp = root.parent
    for p in plugins:
        link = tmp / "plugins" / p
        if not link.exists():
            link.symlink_to(Path("/usr/lib/gstreamer-1.0") / p)
    files = []
    lines = []
    for i, item in enumerate(script):
        if isinstance(item, bytes):
            f = tmp / f"au{i}"
            f.write_bytes(item)
            lines.append(f"push {f}")
        elif isinstance(item, tuple):
            f = tmp / f"au{i}"
            f.write_bytes(item[1])
            lines.append(f"{item[0]} {f}")
        else:
            lines.append(item)
        files.append(lines[-1])
    (tmp / "script").write_text("\n".join(lines) + "\n")
    log, frames = tmp / "fake.log", tmp / "frames.bin"
    for f in (log, frames):
        if f.exists():
            f.unlink()
    env = dict(os.environ, FAKE_LOG=str(log), FRAMES_OUT=str(frames), HOME=str(tmp),
               XDG_CACHE_HOME=str(tmp / "cache"), GST_REGISTRY_1_0=str(tmp / "registry.bin"),
               GST_PLUGIN_SYSTEM_PATH_1_0=str(tmp / "plugins"), GST_PLUGIN_PATH_1_0="",
               GST_REGISTRY_FORK="no", **{k: str(v) for k, v in fake.items()})
    env.pop("GST_DEBUG", None)
    return Result(run(exe, tmp / "script", env=env, cwd=root, timeout=60).stdout, log, frames)


# ---- What the fake decoder makes of an access unit (its last byte k) ----

def picture(k, w, h):
    y = bytes((k * 7 + x + 3 * row) & 255 for row in range(h) for x in range(w))
    uv = bytes((k * 13 + 5 * i + 11 * row) & 255 for row in range(h // 2) for i in range(w))
    return y + uv


SPS, PPS = b"\x00\x00\x00\x01\x67SPS1", b"\x00\x00\x01\x68PP"


def h264(k, key=False, sps=SPS):
    """An access unit: with key the parameter sets (a 4- and a 3-byte start code) before the IDR"""
    head = sps + PPS if key else b""
    return head + (b"\x00\x00\x00\x01\x65" if key else b"\x00\x00\x00\x01\x41") + b"slice" + bytes([k])


VPS, SPS5, PPS5 = b"\x00\x00\x00\x01\x40\x01VPS", b"\x00\x00\x00\x01\x42\x01SPS", b"\x00\x00\x00\x01\x44\x01PPS"


def h265(k, key=False):
    head = VPS + SPS5 + PPS5 if key else b""
    return head + (b"\x00\x00\x00\x01\x26\x01" if key else b"\x00\x00\x00\x01\x02\x01") + b"slice" + bytes([k])


def caps(w, h, kind=H264):
    return f"caps {kind}, width={w}, height={h}, framerate=25/1"


def decoded(res):
    """What reached the decoder: (frame number, bytes) per queued access unit"""
    out = []
    for line in res.logged("decode "):
        _, number, size, data = (line.split() + [""])[:4]
        out.append((int(number), bytes.fromhex(data)))
    return out


def mdp_calls(res):
    return [line for line in res.log if re.match(r"mdp \d", line)]


def check_frames(res, ks, w, h, first_pts=0):
    assert [f["data"] == picture(k, w, h) for f, k in zip(res.frames, ks)] == [True] * len(ks)
    assert len(res.frames) == len(ks)
    assert [f["pts"] for f in res.frames] == [first_pts + 40 * i for i in range(len(ks))]
    assert all((f["w"], f["h"]) == (w, h) for f in res.frames)


# ---- Decoding ----

def test_zero_copy_into_its_own_dma_bufs(exe, root):
    ks = [11, 22, 33, 44, 55]
    res = harness(exe, root, ["element pliniush264dec", "harness", "meta", caps(64, 48),
                              h264(ks[0], key=True), *[h264(k) for k in ks[1:]], "eos"], FAKE_CROP="64x48")
    check_frames(res, ks, 64, 48)
    # Downstream reads the video meta: the MDP writes whole decoded lines (coded width 80)
    # straight into the buffers that go out, which show the visible 64
    assert all(f["dmabuf"] and f["meta"] and f["strides"] == (80, 80) for f in res.frames)
    assert res.logged("open") == ["open dev/video3"]
    assert res.logged("s_fmt") == ["s_fmt output H264"]
    assert res.logged("subscribe") == ["subscribe 5", "subscribe 2"]
    allocs = res.logged("alloc")
    # 8 input buffers of at least 4 MiB, 6 + 4 pictures in the uncached heap large enough for the
    # MDP's plane checks (coded width 80), one NV12 target, then the pool's own buffers
    # (sized for what the MDP checks: the visible height)
    src = 80 * 12 // 8 * 48 + 80 * 12 // 8 // 2 * 24
    cap = -(-src // 4096) * 4096
    pool = -(-80 * 48 * 3 // 2 // 4096) * 4096
    assert allocs[:8] == [f"alloc heap {MIB4}"] * 8
    assert res.logged("reqbufs capture") == ["reqbufs capture 10 -> 10", "reqbufs capture 0 -> 0"]
    assert allocs[8:18] == [f"alloc hw-heap {cap}"] * 10
    assert allocs[18] == f"alloc heap {80 * 48 * 3 // 2}"
    assert set(allocs[19:]) == {f"alloc heap {pool}"}
    # The whole buffer as the source, the visible size, the pool's pitch
    assert not res.logged("decoder:")
    assert set(mdp_calls(res)) == {f"mdp {cap} (buffer {cap}) coded 80 64x48 pitch 80 into {pool} bytes"}
    # In decode order with their frame numbers; drained and closed at the end
    assert [n for n, _ in decoded(res)] == [0, 1, 2, 3, 4]
    assert decoded(res)[0][1] == h264(ks[0], key=True)
    assert "decoder cmd 1" in res.log and res.log.count("close device") == 1
    # Cache maintenance around each write of an input buffer and each picture the MDP wrote
    assert res.syncs == 2 * 5 + 2 * 5
    assert res.lines("push") == ["push ok"] * 5


def test_copied_when_downstream_wants_tight_lines(exe, root):
    # 72 wide: the element's buffers have lines of 80, downstream without video meta wants 72
    ks = [3, 4, 5]
    res = harness(exe, root, ["element pliniush264dec", "harness", caps(72, 40),
                              h264(ks[0], key=True), h264(ks[1]), h264(ks[2]), "eos"], FAKE_CROP="72x40")
    check_frames(res, ks, 72, 40)
    assert all(not f["dmabuf"] and f["strides"] == (72, 72) for f in res.frames)
    # Into the element's own NV12 target with lines of 80, then copied
    cap = mdp_calls(res)[0].split()[1]
    assert set(mdp_calls(res)) == {f"mdp {cap} (buffer {cap}) coded 80 72x40 pitch 80 into {80 * 40 * 3 // 2} bytes"}


def test_downstream_with_video_meta_takes_padded_lines(exe, root):
    ks = [9, 8]
    res = harness(exe, root, ["element pliniush264dec", "harness", "meta", caps(72, 40),
                              h264(ks[0], key=True), h264(ks[1]), "eos"], FAKE_CROP="72x40")
    check_frames(res, ks, 72, 40)
    assert all(f["dmabuf"] and f["meta"] and f["strides"] == (80, 80) for f in res.frames)


def test_odd_crop_is_made_even(exe, root):
    res = harness(exe, root, ["element pliniush264dec", "harness", caps(63, 47), h264(7, key=True), "eos"],
                  FAKE_CROP="63x47", FAKE_CODED=64, FAKE_H=48)
    check_frames(res, [7], 62, 46)


@pytest.mark.parametrize("crop", ["none", "0x0"])
def test_without_a_crop_the_caps_size(exe, root, crop):
    res = harness(exe, root, ["element pliniush264dec", "harness", caps(48, 32), h264(5, key=True), "eos"],
                  FAKE_CROP=crop)
    check_frames(res, [5], 48, 32)


def test_new_caps_drain_and_reopen(exe, root):
    res = harness(exe, root, ["element pliniush264dec", "harness", caps(48, 32), h264(1, key=True), h264(2),
                              caps(32, 16), h264(3, key=True), "eos"], FAKE_CROP="none", FAKE_DELAY=1)
    # The held picture of the first stream comes out before the new size
    sizes = [(f["w"], f["h"]) for f in res.frames]
    assert sizes == [(48, 32), (48, 32), (32, 16)]
    assert [f["data"] for f in res.frames] == [picture(1, 48, 32), picture(2, 48, 32), picture(3, 32, 16)]
    assert res.logged("open") == ["open dev/video3"] * 2
    assert res.log.count("decoder cmd 1") == 2


def test_pictures_held_back_come_out_when_draining(exe, root):
    ks = [1, 2, 3, 4]
    res = harness(exe, root, ["element pliniush264dec", "harness", caps(64, 48), h264(1, key=True),
                              *[h264(k) for k in ks[1:]], "eos"], FAKE_CROP="64x48", FAKE_DELAY=2)
    # Two pictures in the decoder until the end of the stream
    pulled_before_eos = res.out.index(next(line for line in res.out if line.startswith("eos")))
    assert len([line for line in res.out[:pulled_before_eos] if line.startswith("frame")]) == 2
    check_frames(res, ks, 64, 48)


def test_a_decoder_that_never_ends_the_drain(exe, root):
    res = harness(exe, root, ["element pliniush264dec", "harness", caps(64, 48), h264(1, key=True), h264(2),
                              "eos"], FAKE_CROP="64x48", FAKE_DELAY=1, FAKE_NO_LAST=1)
    check_frames(res, [1, 2], 64, 48)
    # Given up after the drain timeout (3 s), not hanging
    secs = float(res.lines("eos")[0].split()[3])
    assert 2.9 <= secs < 20


def test_a_decoder_refusing_to_drain_drops_held_pictures(exe, root):
    res = harness(exe, root, ["element pliniush264dec", "harness", caps(64, 48), h264(1, key=True), h264(2),
                              h264(3), "eos"], FAKE_CROP="64x48", FAKE_DELAY=2, FAKE_FAIL="decoder_cmd:0")
    assert [f["data"] for f in res.frames] == [picture(1, 64, 48)]
    assert res.lines("eos")[0].startswith("eos 1")
    assert "close device" in res.log


def test_empty_and_unknown_pictures_are_skipped(exe, root):
    res = harness(exe, root, ["element pliniush264dec", "harness", caps(64, 48), h264(1, key=True), h264(2),
                              "eos"], FAKE_CROP="64x48", FAKE_BOGUS=1, FAKE_EMPTY=1)
    check_frames(res, [1, 2], 64, 48)


@pytest.mark.parametrize("fault", ["poll:2:4", "eintr:3", "dqbuf_cap:1", "dqbuf_out:1"])
def test_transient_device_trouble(exe, root, fault):
    # An interrupted poll or ioctl is retried; a failed dequeue only delays the picture
    res = harness(exe, root, ["element pliniush264dec", "harness", caps(64, 48), h264(1, key=True), h264(2),
                              "eos"], FAKE_CROP="64x48", FAKE_FAIL=fault)
    check_frames(res, [1, 2], 64, 48)


def test_buffer_indexes_out_of_range_are_ignored(exe, root):
    res = harness(exe, root, ["element pliniush264dec", "harness", caps(64, 48), h264(1, key=True), "eos"],
                  FAKE_CROP="64x48", FAKE_BAD_INDEX=1)
    check_frames(res, [1], 64, 48)


# ---- Parameter sets after a flush ----

def test_h264_parameter_sets_sent_again_after_a_flush(exe, root):
    new_sps = b"\x00\x00\x00\x01\x67SPS2"
    res = harness(exe, root, ["element pliniush264dec", "harness", caps(64, 48),
                              h264(1, key=True), h264(2), "flush", h264(3),
                              h264(4, key=True, sps=new_sps), "flush", h264(5), "eos"], FAKE_CROP="64x48")
    sent = [data for _, data in decoded(res)]
    assert sent[0] == h264(1, key=True) and sent[1] == h264(2)
    # The reopened decoder starts with the parameter sets it had (start codes as they came)
    assert sent[2] == SPS + PPS + h264(3)
    # Only the first unit after the reopening gets them; a unit with its own replaces them
    assert sent[3] == h264(4, key=True, sps=new_sps)
    assert sent[4] == new_sps + PPS + h264(5)
    assert res.logged("open") == ["open dev/video3"] * 3
    assert [f["data"] for f in res.frames] == [picture(k, 64, 48) for k in (1, 2, 3, 4, 5)]


def test_h265_parameter_sets_sent_again_after_a_flush(exe, root):
    res = harness(exe, root, ["element pliniush265dec", "harness",
                              caps(64, 48, H265),
                              h265(1, key=True), "flush", h265(2), "eos"], FAKE_CROP="64x48")
    assert res.logged("s_fmt") == ["s_fmt output H265", "s_fmt output H265"]
    assert [data for _, data in decoded(res)] == [h265(1, key=True), VPS + SPS5 + PPS5 + h265(2)]
    check_frames(res, [1, 2], 64, 48)


def test_vp9_has_no_parameter_sets(exe, root):
    frames = [b"\x82\x49\x83" + bytes([k]) for k in (1, 2)]
    res = harness(exe, root, ["element pliniusvp9dec", "harness", caps(64, 48, VP9),
                              frames[0], "flush", frames[1], "eos"], FAKE_CROP="64x48")
    assert res.logged("s_fmt") == ["s_fmt output VP90", "s_fmt output VP90"]
    assert [data for _, data in decoded(res)] == frames
    check_frames(res, [1, 2], 64, 48)


def test_first_unit_without_parameter_sets(exe, root):
    res = harness(exe, root, ["element pliniush264dec", "harness", caps(64, 48), h264(1), "eos"],
                  FAKE_CROP="64x48")
    assert [data for _, data in decoded(res)] == [h264(1)]


# ---- Refusals ----

def errors(res):
    return res.lines("error ")


@pytest.mark.parametrize("fake,message,flow", [
    ({"FAKE_FAIL": "open:1:13"}, "error Cannot open dev/video3 | Permission denied", "error"),
    ({"FAKE_FAIL": "s_fmt:1:22"}, "error The decoder refuses Plinius hardware H.264 decoder | Invalid argument",
     "error"),
    ({"FAKE_OTHER_FORMAT": 1}, "error The decoder refuses Plinius hardware H.264 decoder | other format chosen",
     "error"),
    ({"FAKE_FAIL": "reqbufs_out:1:12"}, "error Cannot get decoder input buffers | Cannot allocate memory", "error"),
    ({"FAKE_FAIL": "alloc:3:12"}, "error Cannot allocate decoder buffers | ", "error"),
    ({"FAKE_FAIL": "unmappable:1"}, "error Cannot allocate decoder buffers | ", "error"),
    ({"FAKE_FAIL": "streamon_out:1:5"}, "error Cannot start the decoder | Input/output error", "error"),
    ({"FAKE_FOURCC": 1}, "error Unsupported picture format from the decoder | P010 (10-bit video is not supported "
     "yet)", "not-negotiated"),
    ({"FAKE_FAIL": "g_fmt:1"}, None, "not-negotiated"),
    ({"FAKE_FAIL": "reqbufs_cap:1"}, None, "not-negotiated"),
    ({"FAKE_CAP_COUNT": 0}, None, "not-negotiated"),
    ({"FAKE_FAIL": "alloc:9"}, None, "not-negotiated"),
    ({"FAKE_FAIL": "qbuf_cap:1"}, None, "not-negotiated"),
    ({"FAKE_FAIL": "alloc:19"}, None, "not-negotiated"),
    ({"FAKE_FAIL": "streamon_cap:1"}, None, "not-negotiated"),
    ({"FAKE_FAIL": "mdp:1"}, "error Cannot convert the decoded picture | MDP said no", "error"),
    ({"FAKE_FAIL": "qbuf_out:1"}, "error Cannot queue data for the decoder | ", "error"),
    ({"FAKE_FAIL": "poll:1"}, None, "error"),
])
def test_device_refusals(exe, root, fake, message, flow):
    res = harness(exe, root, ["element pliniush264dec", "harness", caps(64, 48), h264(1, key=True)],
                  FAKE_CROP="64x48", **fake)
    assert res.lines("push") == [f"push {flow}"]
    if message:
        assert errors(res)[:1] == [message]
    assert res.frames == []
    # Nothing left open: every device that was opened was closed again by the teardown
    assert res.log.count("close device") == res.log.count("open dev/video3")


def test_a_picture_buffer_that_cannot_be_queued_again(exe, root):
    # The picture in it still goes out and the frame is an error; the decoder goes on with one
    # buffer less
    res = harness(exe, root, ["element pliniush264dec", "harness", caps(64, 48), h264(1, key=True), h264(2)],
                  FAKE_CROP="64x48", FAKE_FAIL="qbuf_cap:11")
    assert res.lines("push") == ["push error", "push ok"]
    assert [f["data"] for f in res.frames] == [picture(1, 64, 48), picture(2, 64, 48)]


def test_without_the_decoder_device(exe, root):
    for entry in ("video3", "video0", "video1"):
        for f in (root / "sys/class/video4linux" / entry).iterdir():
            f.unlink()
        (root / "sys/class/video4linux" / entry).rmdir()
    for setup in ([], ["rmdir"]):
        if setup:
            (root / "sys/class/video4linux").rmdir()
        res = harness(exe, root, ["element pliniush264dec", "harness", caps(64, 48), h264(1, key=True)])
        assert res.lines("push") == ["push error"]
        assert errors(res)[:1] == ["error No MediaTek video decoder found | "]


def test_a_frame_too_large_for_the_input_buffers(exe, root):
    res = harness(exe, root, ["element pliniush264dec", "harness", caps(64, 48),
                              b"\x00\x00\x00\x01\x65" + b"\x55" * (MIB4 - 4)], FAKE_CROP="64x48")
    assert res.lines("push") == ["push error"]
    assert errors(res)[:1] == [f"error Frame of {MIB4 + 1} bytes is too large | "]


def test_input_buffers_follow_a_larger_size_of_the_decoder(exe, root):
    res = harness(exe, root, ["element pliniush264dec", "harness", caps(64, 48),
                              b"\x00\x00\x00\x01\x65" + b"\x55" * (MIB4 - 4), "eos"],
                  FAKE_CROP="64x48", FAKE_OUT_SIZEIMAGE=MIB4 + 4096)
    assert res.lines("push") == ["push ok"]
    assert res.logged("alloc")[:8] == [f"alloc heap {MIB4 + 4096}"] * 8


def test_a_decoder_that_takes_no_more_data(exe, root):
    # No input buffer comes back: after eight frames the element waits 5 s, then gives up
    res = harness(exe, root, ["element pliniush264dec", "harness", caps(64, 48), *[h264(k) for k in range(1, 10)]],
                  FAKE_HOLD_OUTPUT=1, FAKE_NO_SOURCE_CHANGE=1)
    assert res.lines("push") == ["push ok"] * 8 + ["push error"]
    assert errors(res)[:1] == ["error The decoder does not take more data | "]


def test_waiting_for_an_input_buffer_ends_on_a_device_error(exe, root):
    res = harness(exe, root, ["element pliniush264dec", "harness", caps(64, 48), *[h264(k) for k in range(1, 10)]],
                  FAKE_HOLD_OUTPUT=1, FAKE_NO_SOURCE_CHANGE=1, FAKE_FAIL="poll:9")
    assert res.lines("push") == ["push ok"] * 8 + ["push error"]


def test_an_unreadable_input_buffer(exe, root):
    res = harness(exe, root, ["element pliniush264dec", "harness", caps(64, 48),
                              ("pushunmappable", h264(1, key=True))])
    assert res.lines("push") == ["push error"]


def test_downstream_buffers_that_cannot_be_written(exe, root):
    res = harness(exe, root, ["element pliniush264dec", "harness", "badalloc", caps(72, 40), h264(1, key=True)],
                  FAKE_CROP="72x40")
    assert res.lines("push") == ["push error"]
    assert res.frames == []


def test_decoded_lines_wider_than_the_picture_without_video_meta(exe, root):
    """2026-11-21: a 720 wide video (coded width 768) came out sheared with green steps: the MDP
    was given the visible width. It converts whole decoded lines now; a downstream that cannot
    read the video meta gets the visible part copied into its tightly packed buffers."""
    ks = [7, 8]
    res = harness(exe, root, ["element pliniush264dec", "harness", caps(64, 48), h264(ks[0], key=True),
                              h264(ks[1]), "eos"], FAKE_CROP="64x48")
    check_frames(res, ks, 64, 48)
    assert all(not f["dmabuf"] and f["strides"] == (64, 64) for f in res.frames)
    assert all(" coded 80 64x48 pitch 80 into " in c for c in mdp_calls(res))


def test_decoded_lines_as_wide_as_the_picture_go_straight_out_without_video_meta(exe, root):
    # Coded width 64: no padding, the element's own buffers fit a downstream without video meta
    res = harness(exe, root, ["element pliniush264dec", "harness", caps(64, 48), h264(3, key=True), "eos"],
                  FAKE_CROP="64x48", FAKE_CODED=64)
    check_frames(res, [3], 64, 48)
    assert res.frames[0]["dmabuf"] and res.frames[0]["strides"] == (64, 64)


def test_the_pool_cannot_get_buffers(exe, root):
    # 8 input, 10 picture and 1 NV12 buffer, then the pool's first
    res = harness(exe, root, ["element pliniush264dec", "harness", "meta", caps(64, 48), h264(1, key=True)],
                  FAKE_CROP="64x48", FAKE_FAIL="alloc:20:12")
    assert res.lines("push") != ["push ok"] or res.frames == []
    assert res.frames == []


@pytest.mark.parametrize("fake,hw_heap,message", [
    ({}, False, None),
    ({"FAKE_FAIL": "mdp_new:1"}, True, "error Cannot use the MDP for colour conversion | no MDP service here"),
    ({"FAKE_FAIL": "mdp_new:1"}, False, "error Cannot use the MDP for colour conversion | no MDP service here"),
    ({"heap": 1}, True, "error Cannot open dev/dma_heap/mtk_mm | No such file or directory"),
])
def test_starting(exe, root, fake, hw_heap, message):
    if not hw_heap:
        (root / "dev/dma_heap/mtk_mm-uncached").unlink()
    if fake.pop("heap", None):
        (root / "dev/dma_heap/mtk_mm").unlink()
    res = harness(exe, root, ["element pliniush264dec", "state paused", "state null"], **fake)
    if message is None:
        assert res.lines("state") == ["state SUCCESS", "state SUCCESS"]
        assert res.log == ["mdp new", "mdp free"]
    else:
        assert res.lines("state")[0] == "state FAILURE"
        assert errors(res)[:1] == [message]


def test_without_the_uncached_heap_pictures_come_from_the_other(exe, root):
    (root / "dev/dma_heap/mtk_mm-uncached").unlink()
    res = harness(exe, root, ["element pliniush264dec", "harness", caps(64, 48), h264(1, key=True), "eos"],
                  FAKE_CROP="64x48")
    check_frames(res, [1], 64, 48)
    assert not res.logged("alloc hw-heap")


def test_downstreams_minimum_of_buffers_is_kept(exe, root):
    res = harness(exe, root, ["element pliniush264dec", "harness", "meta", "proposepool 6", caps(64, 48),
                              h264(1, key=True), "eos"], FAKE_CROP="64x48")
    check_frames(res, [1], 64, 48)
    assert res.frames[0]["dmabuf"]
    pool = -(-80 * 48 * 3 // 2 // 4096) * 4096
    # Downstream wants 6, more than the 4 the element asks for: 6 allocated when the pool starts
    assert res.logged("alloc heap").count(f"alloc heap {pool}") == 6


def test_a_downstream_pool_that_cannot_be_configured(exe, root):
    # 72 wide without video meta: downstream's own pool stays, and it refuses its configuration;
    # GstVideoDecoder then makes a pool of its own, the pictures are copied into it
    res = harness(exe, root, ["element pliniush264dec", "harness", "proposepool 2 refusing", caps(72, 40),
                              h264(1, key=True), "eos"], FAKE_CROP="72x40")
    assert res.lines("push") == ["push ok"]
    check_frames(res, [1], 72, 40)
    assert not res.frames[0]["dmabuf"]


def test_the_same_caps_again_change_nothing(exe, root):
    res = harness(exe, root, ["element pliniush264dec", "harness", caps(64, 48), h264(1, key=True), caps(64, 48),
                              h264(2), "eos"], FAKE_CROP="64x48")
    check_frames(res, [1, 2], 64, 48)
    assert res.logged("open") == ["open dev/video3"]


# ---- Registration, the pool, a real pipeline ----

def test_registered_only_on_the_phone(exe, root):
    res = harness(exe, root, ["factories"])
    assert res.lines("plugin") == ["plugin pliniusvdec 0.1 MIT plinius-vdec"]
    templates = res.lines("template sink")
    assert res.lines("factory") == [f"factory {n} rank 258 klass Codec/Decoder/Video/Hardware" for n in
                                    ("pliniush264dec", "pliniush265dec", "pliniusvp9dec")]
    assert "alignment=(string)au" in templates[0] and "video/x-h265" in templates[1] and "video/x-vp9" in templates[2]
    assert res.lines("template src")[0].startswith("template src video/x-raw, format=(string)NV12")
    (root / "vendor/lib64/mt6878/libdpframework.so").unlink()
    res = harness(exe, root, ["factories", "element pliniush264dec"])
    assert res.lines("factory") == [f"factory {n} missing" for n in
                                    ("pliniush264dec", "pliniush265dec", "pliniusvp9dec")]
    assert res.lines("element") == ["element unknown"]


def test_the_pool_is_nv12_with_padded_lines(exe, root):
    res = harness(exe, root, ["pool"])
    assert res.lines("pool ") == [
        "pool I420 accepted=0 size=0",
        f"pool NV12 accepted=1 size={112 * 50 * 3 // 2}",
        "pool video meta 1",
        f"pool buffer {112 * 50 * 3 // 2} bytes, dmabuf 1, offsets 0/{112 * 50} strides 112/112",
        f"pool wide lines {128 * 50 * 3 // 2} bytes, 100x50 strides 128/128",
    ]
    assert res.logged("alloc") == [f"alloc heap {-(-112 * 50 * 3 // 2 // 4096) * 4096}",
                                   f"alloc heap {-(-128 * 50 * 3 // 2 // 4096) * 4096}"]


def test_in_a_pipeline(exe, root):
    if not all((Path("/usr/lib/gstreamer-1.0") / p).exists() for p in ("libgstapp.so", "libgstcoreelements.so")):
        pytest.skip("no appsrc/appsink on the host")
    ks = [10, 20, 30]
    res = harness(exe, root, [f"pipeline pliniush264dec {H264}, width=64, height=48, framerate=25/1",
                              h264(ks[0], key=True), h264(ks[1]), h264(ks[2])],
                  plugins=("libgstapp.so", "libgstcoreelements.so"), FAKE_CROP="64x48")
    assert res.lines("eos") == ["eos 1"]
    assert errors(res) == []
    check_frames(res, ks, 64, 48)
