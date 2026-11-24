# plinius-ambient, a video or an animated GIF as the background (video.c), in host GStreamer and
# gdk-pixbuf, drawn with the stand-in for OpenGL ES 2 (ambient/fakegl.c). The videos are made
# here from raw frames whose every byte the test knows (Matroska, no codec needed); every texture
# upload is compared with the plane of one of those frames, with the stride as texture width.
# Covered: the planes of NV12, I420 and YV12 (V before U) and the RGB formats, the colour matrix
# of the stream's colorimetry, covering the screen like a wallpaper with the pixel aspect ratio
# and the orientation tag, playing only while seen, pausing and releasing the decoder and going
# on from where it was, looping at the end without a gap, giving up looping a still picture,
# audio left undecoded, and every error ending in Phosh's own background (a transparent clear)
# with one warning per file. GIFs go through gdk-pixbuf on a thread of their own, with the
# frames' delays, a stop while hidden, and a GIF that plays a number of times starting again.

import os
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from ambient_helpers import build, draws, gl, glsl_errors, uniforms
from dhw_harness import kv, run

GST_LAUNCH = shutil.which("gst-launch-1.0")
PIL = pytest.importorskip("PIL.Image")

HARNESS = r"""
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include "video.c"

void fakegl_report(void);

static struct scene sc = {.width = 360, .height = 800};
static struct video *v;
static int wakes, renders;
static int width = 720, height = 1600;
static GstCaps *caps;
static int strides[3], n_strides;

static gboolean drawing = TRUE;

static void on_wake(void)
{
	wakes++;
}

/* Runs the main loop; every wake draws a frame, as the client does */
static void iterate(int ms, const char *until)
{
	gint64 end = g_get_monotonic_time() + ms * 1000;
	while (g_get_monotonic_time() < end) {
		int before = wakes;
		g_main_context_iteration(NULL, FALSE);
		if (wakes != before && v && drawing) {
			video_effect.render(v, &sc, width, height);
			renders++;
		}
		if (until && v) {
			if (!strcmp(until, "frame") && v->have_frame)
				return;
			if (!strcmp(until, "started") && v->started)
				return;
			if (!strcmp(until, "ended") && v->ended)
				return;
			if (!strcmp(until, "failed") && v->failed)
				return;
			if (!strncmp(until, "renders", 7) && renders >= atoi(until + 7))
				return;
		}
		g_usleep(500);
	}
	if (until)
		printf("timeout=%s\n", until);
}

static void post(GstMessage *m)
{
	gst_element_post_message(v->pipeline, m);
}

int main(int argc, char **argv)
{
	sc.wake = on_wake;
	gst_init(NULL, NULL);
	/* libgsttag registers it once a plugin using it is loaded: the tag messages may come first */
	if (!gst_tag_exists(TAG_IMAGE_ORIENTATION))
		gst_tag_register(TAG_IMAGE_ORIENTATION, GST_TAG_FLAG_META, G_TYPE_STRING, "image orientation", "", NULL);
	for (int i = 1; i < argc; i++) {
		char *a = argv[i];
		if (!strncmp(a, "video:", 6)) {
			sc.video = a + 6;
		} else if (!strcmp(a, "nowake")) {
			sc.wake = NULL;
		} else if (!strcmp(a, "create")) {
			v = video_effect.create(&sc);
			renders = 0;
		} else if (!strncmp(a, "visible:", 8)) {
			video_effect.set_visible(v, atoi(a + 8));
		} else if (!strncmp(a, "size:", 5)) {
			sscanf(a + 5, "%dx%d", &width, &height);
		} else if (!strncmp(a, "until:", 6)) {
			char what[32];
			int ms;
			sscanf(a + 6, "%31[^:]:%d", what, &ms);
			iterate(ms, what);
		} else if (!strncmp(a, "iterate:", 8)) {
			iterate(atoi(a + 8), NULL);
		} else if (!strcmp(a, "render")) {
			video_effect.render(v, &sc, width, height);
		} else if (!strcmp(a, "release")) {
			/* The release timer fires (it is 10 s) */
			printf("release-timer=%d\n", v->release_timer != 0);
			if (v->release_timer) {
				g_source_remove(v->release_timer);
				on_release(v);
			}
		} else if (!strncmp(a, "tag:", 4)) {
			GstTagList *tags = gst_tag_list_new(TAG_IMAGE_ORIENTATION, a + 4, NULL);
			post(gst_message_new_tag(GST_OBJECT(v->pipeline), tags));
		} else if (!strcmp(a, "tag-other")) {
			post(gst_message_new_tag(GST_OBJECT(v->pipeline), gst_tag_list_new(GST_TAG_TITLE, "x", NULL)));
		} else if (!strcmp(a, "eos")) {
			post(gst_message_new_eos(GST_OBJECT(v->pipeline)));
		} else if (!strcmp(a, "segment-done")) {
			post(gst_message_new_segment_done(GST_OBJECT(v->pipeline), GST_FORMAT_TIME, 0));
		} else if (!strcmp(a, "warning")) {
			GError *e = g_error_new_literal(GST_STREAM_ERROR, GST_STREAM_ERROR_DECODE, "a test warning");
			post(gst_message_new_warning(GST_OBJECT(v->pipeline), e, NULL));
			g_error_free(e);
		} else if (!strcmp(a, "warning-bare")) {
			post(gst_message_new_warning(GST_OBJECT(v->pipeline), NULL, NULL));
		} else if (!strcmp(a, "error-bare")) {
			post(gst_message_new_error(GST_OBJECT(v->pipeline), NULL, NULL));
		} else if (!strcmp(a, "stop-pipeline")) {
			/* READY: nothing to seek in (NULL would flush the bus) */
			gst_element_set_state(v->pipeline, GST_STATE_READY);
		} else if (!strncmp(a, "autoplug:", 9)) {
			GstCaps *caps = !strcmp(a + 9, "EMPTY") ? gst_caps_new_empty()
				      : !strcmp(a + 9, "ANY") ? gst_caps_new_any()
				      : gst_caps_from_string(a + 9);
			printf("autoplug=%d\n", on_autoplug_continue(NULL, NULL, caps, NULL));
			gst_caps_unref(caps);
		} else if (!strncmp(a, "unknown:", 8)) {
			GstCaps *caps = !strcmp(a + 8, "EMPTY") ? gst_caps_new_empty() : gst_caps_from_string(a + 8);
			on_unknown_type(v->pipeline, NULL, caps, NULL);
			gst_caps_unref(caps);
		} else if (!strncmp(a, "pad:", 4)) {
			/* A pad of the given caps offered to the converter */
			GstCaps *caps = gst_caps_from_string(a + 4);
			GstPadTemplate *t = gst_pad_template_new("src", GST_PAD_SRC, GST_PAD_ALWAYS, caps);
			GstPad *pad = gst_pad_new_from_template(t, "src");
			GstIterator *it = gst_bin_iterate_elements(GST_BIN(v->pipeline));
			GValue item = G_VALUE_INIT;
			GstElement *convert = NULL;
			while (gst_iterator_next(it, &item) == GST_ITERATOR_OK) {
				GstElement *e = g_value_get_object(&item);
				if (g_str_has_prefix(GST_OBJECT_NAME(e), "videoconvert"))
					convert = e;
				g_value_reset(&item);
			}
			gst_iterator_free(it);
			on_pad_added(v->pipeline, pad, convert);
			GstPad *sink = gst_element_get_static_pad(convert, "sink");
			printf("linked=%d\n", gst_pad_is_linked(sink) && gst_pad_get_peer(pad) == sink);
			on_no_more_pads(v->pipeline, convert);
			gst_object_unref(sink);
			gst_object_unref(t);
			gst_caps_unref(caps);
		} else if (!strncmp(a, "caps:", 5)) {
			gst_caps_replace(&caps, NULL);
			caps = gst_caps_from_string(a + 5);
		} else if (!strncmp(a, "strides:", 8)) {
			n_strides = sscanf(a + 8, "%d,%d,%d", &strides[0], &strides[1], &strides[2]);
		} else if (!strncmp(a, "sample:", 7)) {
			/* A decoded frame n as the appsink hands it over, its lines as long as asked
			 * (plinius-vdec pads them to 16 bytes and says so in the video meta); every byte of a
			 * plane, also the padding, the value the test computes */
			int n = atoi(a + 7);
			GstVideoInfo info;
			gst_video_info_from_caps(&info, caps);
			gsize offset[GST_VIDEO_MAX_PLANES] = {0}, size = 0;
			gint stride[GST_VIDEO_MAX_PLANES] = {0};
			int heights[GST_VIDEO_MAX_PLANES] = {0}, index[GST_VIDEO_MAX_PLANES];
			for (int c = GST_VIDEO_INFO_N_COMPONENTS(&info) - 1; c >= 0; c--) {
				int pl = GST_VIDEO_INFO_COMP_PLANE(&info, c);
				index[pl] = c;
				heights[pl] = GST_VIDEO_INFO_COMP_HEIGHT(&info, c);
			}
			for (guint pl = 0; pl < GST_VIDEO_INFO_N_PLANES(&info); pl++) {
				stride[pl] = (int)pl < n_strides ? strides[pl] : GST_VIDEO_INFO_PLANE_STRIDE(&info, pl);
				offset[pl] = size;
				size += (gsize)stride[pl] * heights[pl];
			}
			GstBuffer *b = gst_buffer_new_allocate(NULL, size, NULL);
			GstMapInfo m;
			gst_buffer_map(b, &m, GST_MAP_WRITE);
			for (guint pl = 0; pl < GST_VIDEO_INFO_N_PLANES(&info); pl++)
				for (int y = 0; y < heights[pl]; y++)
					for (int x = 0; x < stride[pl]; x++)
						m.data[offset[pl] + y * stride[pl] + x] = (n * 41 + index[pl] * 89 + y * 7 + x * 3) & 0xff;
			gst_buffer_unmap(b, &m);
			gst_buffer_add_video_meta_full(b, 0, GST_VIDEO_INFO_FORMAT(&info), GST_VIDEO_INFO_WIDTH(&info),
						       GST_VIDEO_INFO_HEIGHT(&info), GST_VIDEO_INFO_N_PLANES(&info), offset,
						       stride);
			GstSample *sample = gst_sample_new(b, caps, NULL, NULL);
			gst_buffer_unref(b);
			g_mutex_lock(&v->feed->lock);
			if (v->feed->sample)
				gst_sample_unref(v->feed->sample);
			v->feed->sample = sample;
			feed_notify(v->feed);
			g_mutex_unlock(&v->feed->lock);
		} else if (!strcmp(a, "matrix-unknown")) {
			/* GStreamer's caps always name a matrix: a stream info without one, directly */
			GstVideoInfo info;
			gst_video_info_set_format(&info, GST_VIDEO_FORMAT_I420, 16, 16);
			info.colorimetry.matrix = GST_VIDEO_COLOR_MATRIX_UNKNOWN;
			info.colorimetry.range = GST_VIDEO_COLOR_RANGE_0_255;
			set_yuv_matrix(v, &info);
			printf("gl uniform u_matrix");
			for (int k = 0; k < 9; k++)
				printf(" %.6g", v->matrix[k]);
			printf("\ngl uniform u_offset %.6g %.6g %.6g\ngl uniform u_opaque 1\n", v->offset[0], v->offset[1],
			       v->offset[2]);
		} else if (!strcmp(a, "nodraw")) {
			drawing = FALSE;
		} else if (!strcmp(a, "fail-twice")) {
			fail(v, "the first reason", NULL);
			fail(v, "the second reason", NULL);
		} else if (!strcmp(a, "sample-short")) {
			/* Fewer bytes than the caps say, and no meta: the frame cannot be mapped */
			GstBuffer *b = gst_buffer_new_allocate(NULL, 10, NULL);
			g_mutex_lock(&v->feed->lock);
			v->feed->sample = gst_sample_new(b, caps, NULL, NULL);
			g_mutex_unlock(&v->feed->lock);
			gst_buffer_unref(b);
		} else if (!strcmp(a, "allocation")) {
			GstCaps *c = gst_caps_from_string("video/x-raw,format=NV12,width=16,height=16");
			GstQuery *q = gst_query_new_allocation(c, TRUE);
			printf("proposed=%d\n", on_propose_allocation(NULL, q, NULL));
			printf("video-meta=%d\n", gst_query_find_allocation_meta(q, GST_VIDEO_META_API_TYPE, NULL));
			gst_query_unref(q);
			gst_caps_unref(c);
		} else if (!strcmp(a, "async-done")) {
			post(gst_message_new_async_done(GST_OBJECT(v->pipeline), GST_CLOCK_TIME_NONE));
		} else if (!strcmp(a, "sample-nobuffer")) {
			/* A sample without a buffer: nothing to upload */
			g_mutex_lock(&v->feed->lock);
			v->feed->sample = gst_sample_new(NULL, caps, NULL, NULL);
			g_mutex_unlock(&v->feed->lock);
		} else if (!strncmp(a, "picture-rgb:", 12)) {
			/* A GIF frame without alpha, as glycin decodes some */
			int w, h;
			sscanf(a + 12, "%dx%d", &w, &h);
			GdkPixbuf *p = gdk_pixbuf_new(GDK_COLORSPACE_RGB, FALSE, 8, w, h);
			guint8 *px = gdk_pixbuf_get_pixels(p);
			int stride = gdk_pixbuf_get_rowstride(p);
			for (int y = 0; y < h; y++)
				for (int x = 0; x < w * 3; x++)
					px[y * stride + x] = (x * 7 + y * 13) & 0xff;
			if (!v->gl_ready)
				v->gl_ready = setup_gl(v);
			upload_picture(v, p);
			g_object_unref(p);
		} else if (!strcmp(a, "state")) {
			printf("running=%d\nstarted=%d\nended=%d\nfailed=%d\nhave_frame=%d\nresume_at=%" G_GINT64_FORMAT
			       "\nrotation=%d\nflip=%d\nlayout=%d\naspect=%.4f\nopaque=%.0f\nrenders=%d\nwakes=%d\n",
			       v->running, v->started, v->ended, v->failed, v->have_frame, v->resume_at, v->rotation,
			       v->flip, v->layout, v->aspect, v->opaque, renders, wakes);
			printf("news=%d\n", video_effect.wake(v, &sc));
		} else if (!strcmp(a, "step")) {
			printf("step=%d\n", video_effect.step(v, &sc, 0.016f));
			struct vertex_buffer vb = {0};
			video_effect.build(v, &sc, &vb);
			video_effect.update(v, &sc, 2, 0.5f);
			printf("built=%zu\n", vb.len);
		} else if (!strcmp(a, "position")) {
			gint64 pos = -1;
			gst_element_query_position(v->pipeline, GST_FORMAT_TIME, &pos);
			printf("position=%" G_GINT64_FORMAT "\n", pos);
		} else if (!strcmp(a, "destroy")) {
			video_effect.destroy(v);
			v = NULL;
			fakegl_report();
		} else if (!strncmp(a, "sleep:", 6)) {
			g_usleep(atoi(a + 6) * 1000);
		}
	}
	return 0;
}
"""

FPS = 30


def strides(fmt, w, h, custom=()):
    """The plane layout: (width, height, stride, bytes per texel) per plane, in memory order
    (YV12: Y, V, U), GStreamer's default strides unless custom ones are given"""
    r4 = lambda n: (n + 3) // 4 * 4
    cw, ch = (w + 1) // 2, (h + 1) // 2
    if fmt == "NV12":
        planes = [(w, h, r4(w), 1), (cw, ch, r4(w), 2)]
    elif fmt in ("I420", "YV12"):
        planes = [(w, h, r4(w), 1), (cw, ch, r4(cw), 1), (cw, ch, r4(cw), 1)]
    else:
        planes = [(w, h, w * 4, 4)]
    return [(pw, ph, custom[i] if i < len(custom) else st, bpp) for i, (pw, ph, st, bpp) in enumerate(planes)]


def component(fmt, i):
    """The first component in memory plane i (YV12 keeps V in plane 1)"""
    return {1: 2, 2: 1}.get(i, i) if fmt == "YV12" else i


def plane(n, index, h, stride):
    """Frame n's plane holding component index, every byte (also the padding) a value of its own"""
    return bytes((n * 41 + index * 89 + y * 7 + x * 3) & 0xFF for y in range(h) for x in range(stride))


def texture_of(data, pw, ph, st, bpp):
    """What reaches the texture: whole lines as the texture's width when the stride is a whole
    number of texels, else the lines packed to the frame's width"""
    tex_w = st // bpp if st % bpp == 0 and st // bpp >= pw else pw
    return tex_w, b"".join(data[y * st:y * st + tex_w * bpp] for y in range(ph))


def frame_bytes(fmt, n, w, h):
    return b"".join(plane(n, component(fmt, i), ph, st) for i, (pw, ph, st, _) in enumerate(strides(fmt, w, h)))


def make_video(path, fmt, w, h, frames, *, extra="", audio=False, par="1/1"):
    if not GST_LAUNCH:
        pytest.skip("no gst-launch-1.0")
    raw = path.with_suffix(".raw")
    raw.write_bytes(b"".join(frame_bytes(fmt, n, w, h) for n in range(frames)))
    pipe = (f"filesrc location={raw} ! rawvideoparse format={fmt.lower()} width={w} height={h} "
            f"framerate={FPS}/1 pixel-aspect-ratio={par} {extra} ! matroskamux name=m ! filesink location={path}")
    if audio:
        pipe += f" audiotestsrc num-buffers={frames} samplesperbuffer=1600 ! audio/x-raw,rate=48000 ! m."
    r = subprocess.run([GST_LAUNCH, "-q", *pipe.split()], capture_output=True, text=True,
                       env={**os.environ, "GST_REGISTRY": str(path.parent / "registry.bin")})
    assert r.returncode == 0, r.stderr
    return path


@pytest.fixture(scope="module")
def exe(tmp_path_factory):
    d = tmp_path_factory.mktemp("ambient-video")
    return build(d, "video", HARNESS, [], included=["video.c"],
                 pkgs=("glib-2.0", "gdk-pixbuf-2.0", "gstreamer-1.0", "gstreamer-app-1.0", "gstreamer-video-1.0"),
                 flags=["-Wno-deprecated-declarations"])


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    return tmp_path_factory.mktemp("ambient-media")


@pytest.fixture
def play(exe, tmp_path, media):
    dump = tmp_path / "dump"
    dump.mkdir()

    def call(*args, env=None, check=True):
        e = {"FAKEGL_DUMP": str(dump), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
             "XDG_CACHE_HOME": str(tmp_path / "cache"), "GST_REGISTRY": str(media / "registry.bin")}
        e.update(env or {})
        return run(exe, *args, env=e, check=check, timeout=60)
    return call


def uploads(out):
    """(texture, size, format, dump file) of every upload, full and partial"""
    result = []
    for line in gl(out, "tex "):
        f = line.split()
        if f[1] == "image":
            result.append((int(f[2]), f[3], f[4], f[5]))
        elif f[1] == "sub":
            result.append((int(f[3]), f[5], f[6], f[7]))
    return result


def frames_shown(out, fmt, w, h, frames, custom=()):
    """The frame number of each upload of a whole frame: the planes, in the order the shader
    reads them (Y, U, V), each the plane of the same frame"""
    planes = strides(fmt, w, h, custom)
    # The shader's order: YV12 uploads plane 2 (U) before plane 1 (V)
    order = [0, 2, 1] if fmt == "YV12" else range(len(planes))
    ups = [u for u in uploads(out) if u[3] != "-"]
    shown = []
    for i in range(0, len(ups) - len(planes) + 1, len(planes)):
        ns = []
        for p, (tex, size, f, path) in zip(order, ups[i:i + len(planes)]):
            pw, ph, st, bpp = planes[p]
            data = Path(path).read_bytes()
            match = None
            for n in range(frames):
                tex_w, expected = texture_of(plane(n, component(fmt, p), ph, st), pw, ph, st, bpp)
                if data == expected:
                    assert size == f"{tex_w}x{ph}"
                    match = n
            ns.append(match)
        assert len(set(ns)) == 1 and ns[0] is not None, ns
        shown.append(ns[0])
    return shown


# ---- Formats ----

@pytest.mark.parametrize("fmt,w,h", [("I420", 30, 22), ("YV12", 30, 22)])
def test_file_frames_reach_the_textures(play, media, fmt, w, h):
    video = make_video(media / f"planes-{fmt}.mkv", fmt, w, h, 10)
    r = play("video:" + str(video), "create", "visible:1", "until:renders3:5000", "state", "destroy")
    out = r.stdout
    assert "timeout" not in out
    s = kv(out)
    assert s["have_frame"] == "1" and s["failed"] == "0" and s["layout"] == "2"
    # Y, U, V in textures of their own, as wide as their strides; the frames in their order
    assert [u[2] for u in uploads(out)[:3]] == ["LUMINANCE"] * 3
    shown = frames_shown(out, fmt, w, h, 10)
    assert len(shown) >= 3 and shown == sorted(shown)
    # The first upload makes the textures, the next ones only replace their content
    assert len(gl(out, "tex image")) == 3
    assert f"Video {video}: {w}×{h} {fmt}" in r.stderr
    # The texture coordinates cut the padding off: 30 of 32 texels, 15 of 16
    u = uniforms(out)
    assert u["u_scale0"] == pytest.approx([30 / 32, 1], rel=1e-5)
    assert u["u_limit0"] == pytest.approx([29.5 / 32, 21.5 / 22], rel=1e-5)
    assert u["u_scale1"] == pytest.approx([15 / 16, 1], rel=1e-5)
    assert u["u_limit1"] == pytest.approx([14.5 / 16, 10.5 / 11], rel=1e-5)
    # Drawn as one quad over the whole surface, the planes on units 0..2
    d = draws(out)[-1]
    assert d["mode"] == "TRIANGLE_STRIP" and d["count"] == 4 and d["blend"] == "off"
    assert [u[f"u_plane{i}"] for i in range(3)] == [[0], [1], [2]]
    # A video covers what is below it
    assert u["u_opaque"] == [1]
    assert gl(out, "live") == ["live shaders 0 programs 0 buffers 0 textures 0 framebuffers 0"]


@pytest.fixture
def any_video(media):
    """A video the samples are handed over for (its pipeline stays unstarted)"""
    return make_video(media / "loop.mkv", "I420", 16, 16, 10)


@pytest.mark.parametrize("fmt,w,h,custom,layout,gl_formats", [
    # plinius-vdec's NV12: lines padded to 64 bytes
    ("NV12", 50, 20, (64, 64), 1, ["LUMINANCE", "LUMINANCE_ALPHA"]),
    # A stride of no whole number of texels: packed
    ("NV12", 50, 20, (64, 63), 1, ["LUMINANCE", "LUMINANCE_ALPHA"]),
    ("I420", 50, 20, (64, 32, 32), 2, ["LUMINANCE"] * 3),
    ("YV12", 50, 20, (), 2, ["LUMINANCE"] * 3),
    ("RGBA", 10, 4, (), 0, ["RGBA"]),
    ("RGBA", 10, 4, (42,), 0, ["RGBA"]),
    ("BGRx", 10, 4, (48,), 0, ["RGBA"]),
])
def test_sample_planes_reach_the_textures(play, any_video, fmt, w, h, custom, layout, gl_formats):
    stride_arg = ["strides:" + ",".join(map(str, custom))] if custom else []
    out = play("video:" + str(any_video), "create", f"caps:video/x-raw,format={fmt},width={w},height={h}",
               *stride_arg, "sample:3", "render", "sample:5", "render", "state").stdout
    assert kv(out)["layout"] == str(layout)
    assert [u[2] for u in uploads(out)[:len(gl_formats)]] == gl_formats
    assert frames_shown(out, fmt, w, h, 8, custom) == [3, 5]


@pytest.mark.parametrize("fmt,swapped,opaque", [("RGBA", False, 0), ("BGRA", True, 0), ("RGBx", False, 1),
                                                ("BGRx", True, 1)])
def test_rgb_formats(play, any_video, fmt, swapped, opaque):
    out = play("video:" + str(any_video), "create", f"caps:video/x-raw,format={fmt},width=8,height=8",
               "sample:0", "render").stdout
    u = uniforms(out)
    # Blue and red trade places for BGR; the alpha of RGBx is padding, not transparency
    assert apply(u, [1, 0, 0]) == pytest.approx([0, 0, 1] if swapped else [1, 0, 0])
    assert apply(u, [0, 1, 0]) == pytest.approx([0, 1, 0])
    assert u["u_offset"] == [0, 0, 0] and u["u_opaque"] == [opaque]


def apply(u, rgb):
    m, o = u["u_matrix"], u["u_offset"]
    return [sum(m[c * 3 + r] * rgb[c] for c in range(3)) + o[r] for r in range(3)]


@pytest.mark.parametrize("colorimetry,kr,kb,full", [
    ("bt709", 0.2126, 0.0722, False),
    ("bt601", 0.299, 0.114, False),
    ("1:4:0:0", 0.299, 0.114, True),   # BT.601 in full range (JPEG)
    (None, 0.2126, 0.0722, True),      # full range, no matrix known: BT.709
])
def test_colour_matrix_of_the_stream(play, any_video, colorimetry, kr, kb, full):
    if colorimetry:
        out = play("video:" + str(any_video), "create",
                   f"caps:video/x-raw,format=NV12,width=16,height=16,colorimetry={colorimetry}",
                   "sample:0", "render").stdout
    else:
        out = play("video:" + str(any_video), "create", "matrix-unknown").stdout
    u = uniforms(out)
    lo, span, cspan = (0, 255, 255) if full else (16, 219, 224)
    # White, black and full red of that matrix, as texture values (0..1)
    tex = lambda y, cb, cr: [(lo + y * span) / 255, (128 + cb * cspan) / 255, (128 + cr * cspan) / 255]
    assert apply(u, tex(1, 0, 0)) == pytest.approx([1, 1, 1], abs=2e-3)
    assert apply(u, tex(0, 0, 0)) == pytest.approx([0, 0, 0], abs=2e-3)
    # Pure red: Y' = Kr, Cb = -Kr / (2 (1 - Kb)), Cr = 1/2
    assert apply(u, tex(kr, -kr / 2 / (1 - kb), 0.5)) == pytest.approx([1, 0, 0], abs=2e-3)
    assert u["u_opaque"] == [1]


def test_pixel_aspect_ratio(play, any_video):
    out = play("video:" + str(any_video), "create",
               "caps:video/x-raw,format=I420,width=32,height=32,pixel-aspect-ratio=2/1", "sample:0", "render",
               "state").stdout
    assert kv(out)["aspect"] == "2.0000"


def test_sample_without_buffer_is_skipped(play, any_video):
    out = play("video:" + str(any_video), "create", "caps:video/x-raw,format=I420,width=8,height=8",
               "sample-nobuffer", "render", "state").stdout
    assert kv(out)["have_frame"] == "0" and uploads(out) == []


# ---- Covering the screen ----

def screen_map(u):
    """The u_map uniform as a function of the screen point (0..1, y down) to the frame"""
    m = u["u_map"]
    return lambda s, t: (m[0] * s + m[3] * t + m[6], m[1] * s + m[4] * t + m[7])


def test_covers_the_screen_like_a_wallpaper(play, media):
    video = make_video(media / "wide.mkv", "I420", 64, 48, 3)
    out = play("video:" + str(video), "size:720x1600", "create", "visible:1", "until:frame:5000").stdout
    f = screen_map(uniforms(out))
    # Full height, the middle 720/1600 of the frame's 4:3 shown across: cut off evenly
    k = (720 / 1600) / (64 / 48)
    assert f(0, 0) == pytest.approx((0.5 - k / 2, 0), abs=1e-5)
    assert f(1, 1) == pytest.approx((0.5 + k / 2, 1), abs=1e-5)
    # A screen wider than the frame: the full width, the middle rows
    out = play("video:" + str(video), "size:1600x720", "create", "visible:1", "until:frame:5000").stdout
    f = screen_map(uniforms(out))
    k = (64 / 48) / (1600 / 720)
    assert f(0, 0) == pytest.approx((0, 0.5 - k / 2), abs=1e-5)
    assert f(1, 1) == pytest.approx((1, 0.5 + k / 2), abs=1e-5)


def rotate_cw(grid):
    return [list(row) for row in zip(*grid[::-1])]


@pytest.mark.parametrize("tag,turns,flip", [
    ("rotate-0", 0, False), ("rotate-90", 1, False), ("rotate-180", 2, False), ("rotate-270", 3, False),
    ("flip-rotate-0", 0, True), ("flip-rotate-90", 1, True), ("flip-rotate-270", 3, True),
])
def test_orientation_tag(play, media, tag, turns, flip):
    # A square frame on a square screen: the corners of the screen show the frame's corners as
    # the tag turns (clockwise) and mirrors the picture
    video = make_video(media / "square.mkv", "I420", 32, 32, 3)
    out = play("video:" + str(video), "size:500x500", "create", "visible:1", "until:frame:5000",
               "tag:" + tag, "iterate:50", "tag-other", "iterate:50", "render", "state").stdout
    s = kv(out)
    assert (s["rotation"], s["flip"]) == (str(turns), str(int(flip)))
    labels = [["tl", "tr"], ["bl", "br"]]
    shown = [row[::-1] for row in labels] if flip else labels
    for _ in range(turns):
        shown = rotate_cw(shown)
    f = screen_map(uniforms(out))
    for t in (0, 1):
        for sx in (0, 1):
            u, w = f(sx, t)
            assert labels[round(w)][round(u)] == shown[t][sx], (sx, t, u, w)


# ---- Playing ----

def test_plays_only_while_seen(play, media):
    video = make_video(media / "seen.mkv", "I420", 16, 16, 30)
    out = play("video:" + str(video), "create", "iterate:300", "state").stdout
    # Not seen: no pipeline started, nothing drawn
    s = kv(out)
    assert (s["running"], s["have_frame"], s["renders"]) == ("0", "0", "0")
    assert gl(out) == []


def test_hidden_pauses_then_releases_and_goes_on(play, media):
    video = make_video(media / "long.mkv", "I420", 16, 16, 90)
    out = play("video:" + str(video), "create", "visible:1", "until:renders10:8000", "visible:0",
               "iterate:200", "state", "release", "state", "visible:1", "until:started:5000",
               "until:renders14:8000", "destroy").stdout
    states = [line for line in out.splitlines() if line.startswith(("running=", "resume_at=", "release-timer="))]
    # Hidden: paused (still running) with the release timer set; released: READY, the position kept
    assert states[:2] == ["running=1", "resume_at=-1"]
    assert states[2] == "release-timer=1"
    assert states[3] == "running=0"
    resume = int(states[4].split("=")[1])
    assert resume > 0
    cut = out.index("release-timer=")
    before = frames_shown(out[:cut], "I420", 16, 16, 90)
    after = frames_shown(out[cut:], "I420", 16, 16, 90)
    # Seen again: on from where it was (the seek is accurate), not from the start
    assert len(before) >= 10 and len(after) >= 4
    assert after == sorted(after) and after[0] >= max(before) - 1
    assert abs(after[0] - resume * FPS / 1e9) <= 2


def test_hidden_again_before_released(play, media):
    video = make_video(media / "long.mkv", "I420", 16, 16, 90)
    out = play("video:" + str(video), "create", "visible:1", "until:renders3:8000", "visible:0",
               "visible:1", "until:renders6:8000", "visible:0", "visible:0", "state").stdout
    # Seen again in time: the timer is cancelled, playback goes on (more frames), and a second
    # hide sets one timer
    s = kv(out)
    assert s["running"] == "1" and int(s["renders"]) >= 6


def test_seen_before_it_prerolled(play, media):
    video = make_video(media / "long.mkv", "I420", 16, 16, 90)
    # Shown, hidden and shown again before the first frame: plays once prerolled
    out = play("video:" + str(video), "create", "visible:1", "visible:0", "visible:1",
               "until:renders3:8000", "state").stdout
    assert kv(out)["started"] == "1" and "timeout" not in out


def test_loops_without_a_gap(play, media):
    # 10 frames at 30 per second: looped as a segment, the frames come round again
    video = make_video(media / "loop.mkv", "I420", 16, 16, 10)
    out = play("video:" + str(video), "create", "visible:1", "until:renders25:10000", "state").stdout
    shown = frames_shown(out, "I420", 16, 16, 10)
    assert kv(out)["ended"] == "0"
    wraps = sum(1 for a, b in zip(shown, shown[1:]) if b < a)
    assert wraps >= 1


def test_eos_loops_with_a_flushing_seek(play, media):
    video = make_video(media / "loop.mkv", "I420", 16, 16, 10)
    out = play("video:" + str(video), "create", "visible:1", "until:renders3:8000", "eos",
               "until:renders8:8000", "state").stdout
    assert kv(out)["ended"] == "0" and "timeout" not in out


def test_still_picture_is_not_looped(play, media):
    # One frame: each loop is over at once; after five in a row the last frame stays
    video = make_video(media / "still.mkv", "I420", 16, 16, 1)
    r = play("video:" + str(video), "create", "visible:1", "until:ended:8000", "state", "render", "destroy")
    s = kv(r.stdout)
    assert s["ended"] == "1" and s["running"] == "0" and s["have_frame"] == "1" and s["failed"] == "0"
    assert f"{video} is not looped (no time passes in it)" in r.stderr
    # Still drawn
    assert draws(r.stdout)
    # And not started again when hidden and seen
    out = play("video:" + str(video), "create", "visible:1", "until:ended:8000", "visible:0",
               "visible:1", "segment-done", "iterate:100", "state").stdout
    assert kv(out)["running"] == "0"


def test_cannot_seek_is_not_looped(play, media):
    video = make_video(media / "loop.mkv", "I420", 16, 16, 10)
    # The pipeline stopped behind its back (READY): the seek back fails, the last frame stays
    r = play("video:" + str(video), "create", "visible:1", "until:renders3:8000", "stop-pipeline",
             "segment-done", "iterate:100", "state")
    assert kv(r.stdout)["ended"] == "1"
    assert f"{video} is not looped (cannot seek)" in r.stderr


def test_loop_message_before_start_is_ignored(play, media):
    video = make_video(media / "loop.mkv", "I420", 16, 16, 10)
    out = play("video:" + str(video), "create", "visible:1", "segment-done", "eos",
               "until:renders3:8000", "state").stdout
    assert kv(out)["ended"] == "0" and "timeout" not in out


def test_audio_is_not_decoded(play, media):
    video = make_video(media / "with-audio.mkv", "I420", 16, 16, 10, audio=True)
    r = play("video:" + str(video), "create", "visible:1", "until:renders3:8000", "state")
    assert kv(r.stdout)["failed"] == "0" and "timeout" not in r.stdout


@pytest.mark.parametrize("caps,expected", [
    ("audio/mpeg", 0), ("text/x-raw", 0), ("subpicture/x-dvd", 0), ("closedcaption/x-cea-608", 0),
    ("application/x-ass", 0), ("application/x-ssa", 0), ("application/x-subtitle", 0),
    ("subtitle/x-kate", 0), ("video/x-h264", 1), ("video/x-raw", 1), ("EMPTY", 1), ("ANY", 1),
])
def test_autoplug_skips_audio_and_subtitles(play, media, caps, expected):
    out = play(f"autoplug:{caps}").stdout
    assert kv(out)["autoplug"] == str(expected)


def test_events_after_failing_change_nothing(play, media):
    video = make_video(media / "loop.mkv", "I420", 16, 16, 10)
    r = play("video:" + str(video), "create", "visible:1", "until:frame:8000", "error-bare",
             "until:failed:3000", "segment-done", "tag:rotate-90", "iterate:100", "render", "state")
    s = kv(r.stdout)
    assert s["failed"] == "1" and s["rotation"] == "0"
    assert f"Cannot play the video {video}: unknown error" in r.stderr
    # Phosh's background: the frame after the failure is a transparent clear
    out = r.stdout
    assert gl(out, "clear color")[-1] == "clear color 0 0 0 0"


def test_warnings_and_other_messages_are_only_logged(play, media):
    video = make_video(media / "loop.mkv", "I420", 16, 16, 10)
    out = play("video:" + str(video), "create", "visible:1", "until:frame:8000", "warning", "warning-bare",
               "iterate:100", "until:renders3:8000", "state").stdout
    assert kv(out)["failed"] == "0" and "timeout" not in out


# ---- Errors ----

def test_no_such_file(play, tmp_path):
    r = play(f"video:{tmp_path}/gone.mkv", "create", "visible:1", "render", "state", "destroy")
    assert f"Cannot play the video {tmp_path}/gone.mkv: no such file" in r.stderr
    s = kv(r.stdout)
    assert s["failed"] == "1" and s["news"] == "0"
    assert draws(r.stdout) == [] and gl(r.stdout, "clear color") == ["clear color 0 0 0 0"]


def test_no_video_set(play):
    out = play("create", "visible:1", "render", "state", "step", "destroy").stdout
    s = kv(out)
    assert s["failed"] == "1" and s["news"] == "0" and s["step"] == "0" and s["built"] == "0"


def test_warned_once_per_file(play, tmp_path):
    bad = tmp_path / "text.mkv"
    bad.write_text("this is no video\n")
    r = play(f"video:{bad}", "create", "visible:1", "until:failed:5000", "destroy",
             "create", "visible:1", "until:failed:5000", "state", "destroy")
    assert kv(r.stdout)["failed"] == "1"
    assert r.stderr.count(f"Cannot play the video {bad}") == 1
    # A wake for the frame without the video
    assert int(kv(r.stdout)["wakes"]) >= 1


def test_unreadable_file_fails_when_started(play, tmp_path):
    bad = tmp_path / "locked.mkv"
    bad.write_bytes(b"\0" * 64)
    bad.chmod(0)
    if os.access(bad, os.R_OK):
        pytest.skip("running as root: every file is readable")
    r = play(f"video:{bad}", "nowake", "create", "visible:1", "state", "visible:0", "destroy")
    assert kv(r.stdout)["failed"] == "1"
    assert f"Cannot play the video {bad}" in r.stderr


def test_no_decoder(play, media):
    # A codec with every decoder for it out of the ranks
    x264 = subprocess.run(["gst-inspect-1.0", "x264enc"], capture_output=True).returncode == 0
    if not x264:
        pytest.skip("no x264enc on the host")
    path = media / "h264.mkv"
    if not path.exists():
        subprocess.run([GST_LAUNCH, "-q", "videotestsrc", "num-buffers=5", "!", "video/x-raw,width=16,height=16",
                        "!", "x264enc", "!", "h264parse", "!", "matroskamux", "!", "filesink", f"location={path}"],
                       check=True, capture_output=True, env={**os.environ, "GST_REGISTRY": str(media / "registry.bin")})
    ranks = ",".join(f"{d}:0" for d in ("avdec_h264", "openh264dec", "vah264dec", "vaapih264dec", "nvh264dec",
                                         "v4l2h264dec", "nvh264sldec", "vulkanh264dec", "d3d11h264dec"))
    r = play(f"video:{path}", "create", "visible:1", "until:failed:8000", env={"GST_PLUGIN_FEATURE_RANK": ranks})
    assert "no decoder for video/x-h264" in r.stderr


def test_audio_only_file(play, media, tmp_path):
    path = media / "audio-only.mkv"
    if not path.exists():
        subprocess.run([GST_LAUNCH, "-q", "audiotestsrc", "num-buffers=5", "!", "matroskamux", "!", "filesink",
                        f"location={path}"], check=True, capture_output=True,
                       env={**os.environ, "GST_REGISTRY": str(media / "registry.bin")})
    r = play(f"video:{path}", "create", "visible:1", "until:failed:8000")
    assert "no video stream that can be decoded" in r.stderr


@pytest.mark.parametrize("caps,error", [("audio/mpeg", False), ("EMPTY", False), ("video/x-fancy", True)])
def test_unknown_type(play, media, caps, error):
    video = make_video(media / "loop.mkv", "I420", 16, 16, 10)
    r = play("video:" + str(video), "create", f"unknown:{caps}", "iterate:100", "state")
    assert kv(r.stdout)["failed"] == str(int(error))
    assert ("no decoder for video/x-fancy" in r.stderr) == error


@pytest.mark.parametrize("caps,linked,error", [
    ("video/x-raw,format=I420,width=16,height=16", 1, None),
    ("video/x-raw,format=NV12,width=8,height=8", 1, None),
    ("audio/x-raw", 0, "no video stream that can be decoded"),
    # Decoded, but in memory videoconvert cannot read
    ("video/x-raw(memory:DMABuf),format=I420,width=16,height=16", 0, "cannot link the decoded video"),
])
def test_decoded_pads(play, media, caps, linked, error):
    video = make_video(media / "loop.mkv", "I420", 16, 16, 10)
    # A video pad is linked (and no complaint at the end of the pads), others stay unlinked
    # and leave an error
    r = play("video:" + str(video), "create", f"pad:{caps}", "iterate:100", "state")
    assert kv(r.stdout)["linked"] == str(linked)
    assert kv(r.stdout)["failed"] == str(1 - linked)
    if error:
        assert error in r.stderr


def test_second_video_stream_stays_unlinked(play, media):
    video = make_video(media / "loop.mkv", "I420", 16, 16, 10)
    out = play("video:" + str(video), "create", "pad:video/x-raw,format=I420,width=16,height=16",
               "pad:video/x-raw,format=I420,width=16,height=16", "iterate:50", "state").stdout
    assert [line for line in out.splitlines() if line.startswith("linked=")] == ["linked=1", "linked=0"]


def test_frame_larger_than_the_gpu_takes(play, media):
    video = make_video(media / "big.mkv", "I420", 64, 48, 3)
    r = play("video:" + str(video), "create", "visible:1", "until:failed:5000", "render", "state",
             env={"FAKEGL_MAX_TEXTURE": "32"})
    assert kv(r.stdout)["failed"] == "1"
    assert "the frame is larger than the GPU's textures" in r.stderr


def test_gl_program_that_does_not_link(play, media):
    video = make_video(media / "loop.mkv", "I420", 16, 16, 10)
    r = play("video:" + str(video), "create", "visible:1", "until:renders3:5000", "state", "destroy",
             env={"FAKEGL_FAIL_COMPILE": "u_plane2, uv1).r"})
    assert "Video shader does not compile: fake compile error" in r.stderr
    assert "Video program does not link" in r.stderr
    out = r.stdout
    # Nothing uploaded or drawn; the programs made so far are freed with the effect
    assert uploads(out) == [] and draws(out) == []
    assert gl(out, "live") == ["live shaders 0 programs 0 buffers 0 textures 0 framebuffers 0"]


def test_shaders_are_glsl_es(play, media, tmp_path):
    video = make_video(media / "loop.mkv", "I420", 16, 16, 10)
    play("video:" + str(video), "create", "visible:1", "until:frame:5000")
    errors = glsl_errors(tmp_path / "dump")
    if errors is None:
        pytest.skip("no glslangValidator")
    assert errors == {} and len(list((tmp_path / "dump").glob("shader-*"))) == 6


def test_destroyed_while_playing(play, media):
    video = make_video(media / "long.mkv", "I420", 16, 16, 90)
    out = play("video:" + str(video), "create", "visible:1", "until:renders3:5000", "visible:0", "destroy").stdout
    assert gl(out, "live") == ["live shaders 0 programs 0 buffers 0 textures 0 framebuffers 0"]


# ---- GIFs ----

def gif(path, colors, durations, loop=0, size=(5, 4)):
    frames = [PIL.new("RGB", size, c) for c in colors]
    kw = {"save_all": True, "append_images": frames[1:], "duration": durations} if len(frames) > 1 else {}
    if len(frames) > 1 and loop is not None:
        kw["loop"] = loop
    frames[0].save(path, **kw)
    return path


def gif_colors(out):
    colors = []
    for tex, size, fmt, path in uploads(out):
        if path == "-":
            continue
        data = Path(path).read_bytes()
        assert fmt == "RGBA" and len(data) == 5 * 4 * 4
        colors.append(tuple(data[:3]))
        # One colour all over, opaque
        assert data == data[:4] * 20 and data[3] == 255
    return colors


def test_animated_gif_plays_its_frames(play, tmp_path):
    path = gif(tmp_path / "a.gif", [(255, 0, 0), (0, 255, 0), (0, 0, 255)], [60, 60, 60])
    r = play(f"video:{path}", "create", "visible:1", "until:renders7:5000", "state", "destroy")
    colors = gif_colors(r.stdout)
    # In their order, and round again
    assert colors[:4] == [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 0, 0)]
    assert f"Animated GIF {path}: 5×4" in r.stderr
    s = kv(r.stdout)
    assert s["layout"] == "0" and s["opaque"] == "0" and s["aspect"] == "1.2500"
    assert gl(r.stdout, "live") == ["live shaders 0 programs 0 buffers 0 textures 0 framebuffers 0"]


def test_gif_hidden_stands_still(play, tmp_path):
    path = gif(tmp_path / "a.gif", [(255, 0, 0), (0, 255, 0), (0, 0, 255)], [40, 40, 40])
    out = play(f"video:{path}", "create", "iterate:200", "state", "visible:1", "until:renders2:5000",
               "visible:0", "iterate:50", "state", "iterate:300", "state", "visible:1", "until:renders4:5000",
               "destroy").stdout
    renders = [int(line.split("=")[1]) for line in out.splitlines() if line.startswith("renders=")]
    # Nothing before it is seen; hidden, no frame comes (at most the one on its way)
    assert renders[0] == 0
    assert renders[2] - renders[1] <= 1


def test_gif_played_a_number_of_times_starts_again(play, tmp_path):
    # No loop count: the animation ends on its last frame; after a short pause it starts again
    path = gif(tmp_path / "once.gif", [(255, 0, 0), (0, 255, 0)], [40, 40], loop=None)
    out = play(f"video:{path}", "create", "visible:1", "until:renders5:5000", "destroy").stdout
    colors = gif_colors(out)
    assert colors[:3] == [(255, 0, 0), (0, 255, 0), (255, 0, 0)]


def test_still_gif_is_shown_once(play, tmp_path):
    path = gif(tmp_path / "still.gif", [(10, 20, 30)], [])
    out = play(f"video:{path}", "create", "visible:1", "until:renders1:5000", "iterate:300", "state",
               "destroy").stdout
    assert gif_colors(out) == [(10, 20, 30)]
    assert kv(out)["renders"] == "1"


def test_broken_gif(play, tmp_path):
    path = tmp_path / "broken.gif"
    path.write_bytes(b"GIF89a" + b"\xff" * 20)
    r = play(f"video:{path}", "create", "visible:1", "until:failed:5000", "render", "state", "destroy")
    assert kv(r.stdout)["failed"] == "1"
    assert f"Cannot play the video {path}" in r.stderr


def test_gif_frame_without_alpha_is_packed_and_opaque(play, tmp_path):
    path = gif(tmp_path / "a.gif", [(255, 0, 0), (0, 255, 0)], [40, 40])
    # 5 px of RGB: rows of 15 bytes padded to 16 by gdk-pixbuf, packed for GLES 2
    out = play(f"video:{path}", "create", "picture-rgb:5x3", "state", "destroy").stdout
    [(tex, size, fmt, dump)] = uploads(out)
    assert (size, fmt) == ("5x3", "RGB")
    assert Path(dump).read_bytes() == bytes((x * 7 + y * 13) & 0xFF for y in range(3) for x in range(15))
    assert kv(out)["opaque"] == "1"


def test_gif_frame_larger_than_the_gpu_takes(play, tmp_path):
    path = gif(tmp_path / "big.gif", [(255, 0, 0), (0, 255, 0)], [40, 40], size=(40, 4))
    r = play(f"video:{path}", "create", "visible:1", "until:failed:5000", "state",
             env={"FAKEGL_MAX_TEXTURE": "32"})
    assert kv(r.stdout)["failed"] == "1"
    assert "the picture is larger than the GPU's textures" in r.stderr


def test_gif_destroyed_while_hidden_and_seen(play, tmp_path):
    path = gif(tmp_path / "a.gif", [(255, 0, 0), (0, 255, 0)], [40, 40])
    # The thread waits for being seen, or for the next frame: both end with the effect
    play(f"video:{path}", "create", "iterate:50", "destroy", "sleep:100")
    play(f"video:{path}", "create", "visible:1", "until:renders1:5000", "destroy", "sleep:100")


# ---- Details ----

def test_frames_not_drawn_in_time_are_dropped(play, media):
    video = make_video(media / "long.mkv", "I420", 16, 16, 90)
    # Nobody draws for a while: only the newest frame waits, and that is the one drawn
    out = play("video:" + str(video), "nodraw", "create", "visible:1", "until:started:5000", "iterate:400",
               "render", "position").stdout
    [shown] = frames_shown(out, "I420", 16, 16, 90)
    assert shown >= 6


def test_hidden_drops_the_waiting_frame(play, media):
    video = make_video(media / "long.mkv", "I420", 16, 16, 90)
    out = play("video:" + str(video), "nodraw", "create", "visible:1", "until:started:5000", "iterate:200",
               "state", "visible:0", "state", "render").stdout
    news = [line for line in out.splitlines() if line.startswith("news=")]
    # A frame was waiting; hidden, it is let go (the texture keeps the last one drawn)
    assert news == ["news=1", "news=0"]
    assert uploads(out) == []


def test_hidden_before_ever_seen(play, any_video):
    out = play("video:" + str(any_video), "create", "visible:0", "release", "state").stdout
    assert kv(out)["release-timer"] == "0" and kv(out)["running"] == "0"


def test_destroyed_with_a_frame_waiting(play, any_video):
    out = play("video:" + str(any_video), "create", "caps:video/x-raw,format=I420,width=8,height=8",
               "sample:1", "destroy").stdout
    assert gl(out, "live") == ["live shaders 0 programs 0 buffers 0 textures 0 framebuffers 0"]


def test_frame_that_cannot_be_mapped(play, any_video):
    r = play("video:" + str(any_video), "create", "caps:video/x-raw,format=I420,width=16,height=16",
             "sample-short", "render", "state", env={"G_MESSAGES_DEBUG": "all"})
    assert "Cannot map a video frame" in r.stdout + r.stderr
    assert kv(r.stdout)["have_frame"] == "0" and kv(r.stdout)["failed"] == "0"


def test_failure_is_reported_once(play, any_video):
    r = play("nowake", "video:" + str(any_video), "create", "fail-twice", "state")
    assert "the first reason" in r.stderr and "the second reason" not in r.stderr
    assert kv(r.stdout)["failed"] == "1"


def test_sink_asks_for_the_video_meta(play):
    # The planes are read with their strides: plinius-vdec may hand over padded lines
    out = play("allocation").stdout
    assert kv(out)["proposed"] == "1" and kv(out)["video-meta"] == "1"


def test_decoded_by_a_real_decoder(play, media):
    if subprocess.run(["gst-inspect-1.0", "vp8enc"], capture_output=True).returncode != 0:
        pytest.skip("no vp8enc on the host")
    path = media / "vp8.mkv"
    if not path.exists():
        subprocess.run([GST_LAUNCH, "-q", "videotestsrc", "num-buffers=20", "!",
                        "video/x-raw,width=32,height=32,framerate=30/1", "!", "vp8enc", "!", "matroskamux", "!",
                        "filesink", f"location={path}"], check=True, capture_output=True,
                       env={**os.environ, "GST_REGISTRY": str(media / "registry.bin")})
    out = play(f"video:{path}", "create", "visible:1", "until:renders3:8000", "state").stdout
    s = kv(out)
    assert s["have_frame"] == "1" and s["layout"] == "2" and "timeout" not in out


def test_without_segment_seeks_it_still_plays(play, media):
    video = make_video(media / "loop.mkv", "I420", 16, 16, 10)
    # Prerolled while the pipeline cannot seek (stopped behind its back): logged, then played
    r = play("video:" + str(video), "create", "visible:1", "stop-pipeline", "async-done", "until:started:3000",
             "until:renders3:8000", "state", env={"G_MESSAGES_DEBUG": "all"})
    assert f"No segment seek in {video}: looped at its end instead" in r.stdout + r.stderr
    assert kv(r.stdout)["failed"] == "0" and "timeout=renders3" not in r.stdout


def test_without_the_gstreamer_plugins(play, media, tmp_path):
    # Only videoconvert and appsink there: uridecodebin is missing
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    found = 0
    for name in ("libgstvideoconvertscale.so", "libgstapp.so"):
        for d in ("/usr/lib/gstreamer-1.0", "/usr/lib64/gstreamer-1.0", "/usr/lib/x86_64-linux-gnu/gstreamer-1.0"):
            if Path(d, name).exists():
                (plugins / name).symlink_to(Path(d, name))
                found += 1
                break
    if found != 2:
        pytest.skip("videoconvertscale or app plugin not found")
    video = make_video(media / "loop.mkv", "I420", 16, 16, 10)
    r = play("video:" + str(video), "create", "visible:1", "render", "state",
             env={"GST_PLUGIN_SYSTEM_PATH": str(plugins), "GST_PLUGIN_PATH": "",
                  "GST_REGISTRY": str(tmp_path / "registry.bin")})
    assert "GStreamer lacks uridecodebin, videoconvert or appsink" in r.stderr
    assert kv(r.stdout)["failed"] == "1"
