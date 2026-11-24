// SPDX-License-Identifier: MIT
// plinius-ambient: a video or an animated GIF as the background, looping and muted.
//
// Videos play with GStreamer. uridecodebin picks the decoders by rank, so H.264, H.265 and VP9 go
// to plinius-vdec's hardware decoders: they hand out NV12 in dma-bufs that the MDP wrote, without
// a copy as long as the sink reads the video meta (padded lines), which the appsink here says it
// does. Audio and subtitle streams are not decoded at all: their pads stay undecoded and
// unlinked. The appsink keeps time and drops late frames, and its QoS events let the decoder skip
// frames too; of what arrives, only the newest frame waits for the next draw. Its planes go into
// textures as they are (Y and the interleaved UV as luminance textures for NV12, three for I420,
// RGBA as it is) and the fragment shader turns them into RGB with the stream's own matrix and
// range: no colour conversion on the CPU (videoconvert only acts for other formats).
// Looping: a segment seek, again at each SEGMENT_DONE without a flush, so that playback runs on
// without a gap; at EOS (no segment seeks) a flushing seek back to the start.
//
// Animated GIFs: GStreamer on the phone has no GIF decoder (gst-libav is not installed, and
// gdkpixbufdec offers only the formats gdk-pixbuf still decodes itself), so they go through
// GdkPixbufAnimation (glycin), on a thread of their own that sleeps for each frame's delay.
//
// Power: frames are decoded only while the background is seen (set_visible: frame callbacks
// come and the display is on, the always-on display counting as off). Hidden, the pipeline
// pauses at once and goes to READY after RELEASE_S, which frees the decoder and its buffers;
// seen again, it goes on from where it was. The last frame stays in the texture meanwhile and
// shows at once. A frame is drawn only when a new one has been decoded (the effect itself is
// always at rest: each new frame wakes the client through scene->wake).
//
// Scaled like a wallpaper: the frame covers the surface, the overflow cut off evenly on both
// sides, with the pixel aspect ratio and the orientation tag of phone videos (rotate-90...)
// applied. The surface is laid out in the output's turned coordinates, so a rotation of the
// screen needs nothing but its new size.
//
// Errors (the file missing, no decoder, a broken stream): logged once per file, then nothing is
// drawn (Phosh's background picture shows) and nothing is tried again until the settings change.
#include "effects.h"
#include "video.h"

#include <GLES2/gl2.h>
#include <gdk-pixbuf/gdk-pixbuf.h>
#include <gst/app/gstappsink.h>
#include <gst/gst.h>
#include <gst/video/video.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Hidden this long, the pipeline frees the decoder (READY) */
#define RELEASE_S 10
/* Loops over sooner than this, SHORT_LOOPS times in a row, are a still picture or broken
 * timestamps: not looped any more (would decode as fast as it can). A few are normal: the first
 * SEGMENT_DONE comes as soon as the demuxer is through, while the queues still hold the frames. */
#define MIN_LOOP_US (200 * 1000)
#define SHORT_LOOPS 5
/* The shortest delay of a GIF frame (browsers treat shorter ones as 100 ms; this only guards
 * against 0) */
#define MIN_GIF_DELAY_MS 20
/* The last frame of a GIF that plays a number of times, before it starts again */
#define GIF_END_PAUSE_MS 100
/* A frame later than this is dropped by the sink */
#define MAX_LATENESS (20 * GST_MSECOND)
/* GST_TAG_IMAGE_ORIENTATION, without linking libgsttag for the name */
#define TAG_IMAGE_ORIENTATION "image-orientation"

/* How the frame lies in the textures: one RGBA texture, Y + interleaved UV, Y + U + V */
enum layout { LAYOUT_RGB, LAYOUT_NV12, LAYOUT_I420, N_LAYOUTS };

/* What the streaming thread (video) or the GIF thread hands to the main loop */
struct feed {
	gint refs;
	GMutex lock;
	GCond cond;
	GSource *notify;    /* wakes the main loop; NULL once the background is gone */
	GstSample *sample;  /* the newest video frame, until drawn */
	/* GIF */
	char *path;
	gboolean visible;
	GdkPixbuf *picture; /* the newest GIF frame, until drawn */
	char *error;        /* set by the thread when the GIF cannot be read */
};

struct program {
	GLuint id;
	GLint u_map, u_scale0, u_scale1, u_limit0, u_limit1, u_matrix, u_offset, u_opaque, u_plane[3];
};

struct video {
	const struct scene *scene;
	char *path;
	struct feed *feed;
	gboolean gif;
	gboolean failed;     /* nothing to show: Phosh's picture shows */
	gboolean visible;

	/* GStreamer */
	GstElement *pipeline;
	guint bus_watch;
	gboolean running;    /* PAUSED or PLAYING asked for */
	gboolean started;    /* prerolled and the segment seek sent */
	gboolean ended;      /* not looped (a still picture): the last frame stays */
	gint64 resume_at;    /* ns: where to go on after a release, -1 from the start */
	gint64 loop_began;   /* µs, monotonic */
	int short_loops;     /* in a row, see MIN_LOOP_US */
	guint release_timer;
	int rotation;        /* quarter turns clockwise, from the orientation tag */
	gboolean flip;       /* mirrored before the rotation */

	/* GL */
	gboolean gl_ready, gl_failed;
	struct program programs[N_LAYOUTS];
	GLuint textures[3], vbo;
	int tex_width[3], tex_height[3];
	GLenum tex_format[3];
	gboolean have_frame;
	enum layout layout;
	float aspect;        /* of the frame as displayed (pixel aspect ratio applied) */
	float scale[2][2];   /* texture coordinates of the frame's far edges: plane 0, planes 1-2 */
	float limit[2][2];   /* the same half a texel inside, against the lines' padding */
	float matrix[9], offset[3]; /* texture to RGB, column-major */
	float opaque;        /* 1: the texture's alpha is padding (RGBx), 0: it is the alpha */
};

/* ---- Feed ---- */

static struct feed *feed_new(const char *path)
{
	struct feed *f = g_new0(struct feed, 1);
	f->refs = 1;
	f->path = g_strdup(path);
	g_mutex_init(&f->lock);
	g_cond_init(&f->cond);
	return f;
}

static struct feed *feed_ref(struct feed *f)
{
	g_atomic_int_inc(&f->refs);
	return f;
}

static void feed_unref(struct feed *f)
{
	if (!g_atomic_int_dec_and_test(&f->refs))
		return;
	if (f->sample)
		gst_sample_unref(f->sample);
	g_clear_object(&f->picture);
	g_free(f->error);
	g_free(f->path);
	g_mutex_clear(&f->lock);
	g_cond_clear(&f->cond);
	g_free(f);
}

/* Wakes the main loop (lock held); from any thread */
static void feed_notify(struct feed *f)
{
	if (f->notify)
		g_source_set_ready_time(f->notify, 0);
}

static gboolean notify_dispatch(GSource *source, GSourceFunc callback, gpointer user_data)
{
	g_source_set_ready_time(source, -1);
	return callback ? callback(user_data) : G_SOURCE_CONTINUE;
}

static GSourceFuncs notify_funcs = {NULL, NULL, notify_dispatch, NULL, NULL, NULL};

/* ---- Errors ---- */

static void stop_pipeline(struct video *v);

/* Logs once per file: the effect is built anew on every change of the settings */
static void report(struct video *v, const char *what, const char *detail)
{
	static char *reported;
	if (g_strcmp0(reported, v->path)) {
		g_warning("Cannot play the video %s: %s%s%s", v->path, what, detail ? " - " : "", detail ? detail : "");
		g_free(reported);
		reported = g_strdup(v->path);
	} else {
		g_debug("Cannot play the video %s: %s", v->path, what);
	}
}

/* Gives up: nothing more is decoded, and the next frame is drawn without the video */
static void fail(struct video *v, const char *what, const char *detail)
{
	if (v->failed)
		return;
	report(v, what, detail);
	v->failed = TRUE;
	stop_pipeline(v);
	if (v->scene->wake)
		v->scene->wake();
}

static void fail_with_message(struct video *v, GstMessage *message)
{
	g_autoptr(GError) error = NULL;
	g_autofree char *debug = NULL;
	gst_message_parse_error(message, &error, &debug);
	g_debug("%s", debug ? debug : "no details");
	fail(v, error ? error->message : "unknown error", NULL);
}

/* From a streaming thread: the main loop gives up on the bus message */
static void post_error(GstElement *element, const char *message)
{
	GError *error = g_error_new_literal(GST_STREAM_ERROR, GST_STREAM_ERROR_CODEC_NOT_FOUND, message);
	gst_element_post_message(element, gst_message_new_error(GST_OBJECT(element), error, NULL));
	g_error_free(error);
}

/* ---- The GIF thread ---- */

G_GNUC_BEGIN_IGNORE_DEPRECATIONS /* GdkPixbufAnimation and GTimeVal: still the way to glycin's frames */

static GdkPixbufAnimationIter *gif_rewind(GdkPixbufAnimation *animation, gint64 *shown)
{
	GTimeVal start = {0, 0};
	*shown = 0;
	return gdk_pixbuf_animation_get_iter(animation, &start);
}

static gpointer gif_thread(gpointer data)
{
	struct feed *f = data;
	g_autoptr(GError) error = NULL;
	GdkPixbufAnimation *animation = gdk_pixbuf_animation_new_from_file(f->path, &error);
	g_mutex_lock(&f->lock);
	if (!animation) {
		f->error = g_strdup(error ? error->message : "unknown format");
		feed_notify(f);
		g_mutex_unlock(&f->lock);
		feed_unref(f);
		return NULL;
	}
	gboolean still = gdk_pixbuf_animation_is_static_image(animation);
	gint64 shown; /* ms of the animation since the iterator began */
	GdkPixbufAnimationIter *iter = gif_rewind(animation, &shown);
	while (f->notify) {
		if (!f->visible) {
			/* Hidden: nothing is decoded, the animation's clock stands still */
			g_cond_wait(&f->cond, &f->lock);
			continue;
		}
		g_mutex_unlock(&f->lock);
		/* The iterator's pixbuf changes as it advances */
		GdkPixbuf *current = gdk_pixbuf_animation_iter_get_pixbuf(iter);
		GdkPixbuf *frame = current ? gdk_pixbuf_copy(current) : NULL;
		int delay = still ? -1 : gdk_pixbuf_animation_iter_get_delay_time(iter);
		g_mutex_lock(&f->lock);
		if (frame) {
			g_clear_object(&f->picture); /* not drawn in time: dropped */
			f->picture = frame;
			feed_notify(f);
		}
		gboolean rewind = FALSE;
		if (delay < 0) {
			if (still || !shown) {
				/* Shown for good */
				while (f->notify)
					g_cond_wait(&f->cond, &f->lock);
				break;
			}
			/* The end of a GIF that plays a number of times: from the start again */
			rewind = TRUE;
			delay = GIF_END_PAUSE_MS;
		}
		delay = MAX(delay, MIN_GIF_DELAY_MS);
		gint64 end = g_get_monotonic_time() + delay * G_GINT64_CONSTANT(1000);
		while (f->notify && g_get_monotonic_time() < end)
			g_cond_wait_until(&f->cond, &f->lock, end);
		g_mutex_unlock(&f->lock);
		if (rewind) {
			g_object_unref(iter);
			iter = gif_rewind(animation, &shown);
		} else {
			shown += delay;
			GTimeVal now = {shown / 1000, (shown % 1000) * 1000};
			gdk_pixbuf_animation_iter_advance(iter, &now);
		}
		g_mutex_lock(&f->lock);
	}
	g_mutex_unlock(&f->lock);
	g_object_unref(iter);
	g_object_unref(animation);
	feed_unref(f);
	return NULL;
}

G_GNUC_END_IGNORE_DEPRECATIONS

static gboolean is_gif(const char *path)
{
	char magic[6];
	FILE *file = fopen(path, "rb");
	size_t n = file ? fread(magic, 1, sizeof(magic), file) : 0;
	if (file)
		fclose(file);
	return n == sizeof(magic) && (!memcmp(magic, "GIF87a", 6) || !memcmp(magic, "GIF89a", 6));
}

/* ---- The pipeline ---- */

static GstFlowReturn on_new_sample(GstAppSink *sink, gpointer user_data)
{
	struct feed *f = user_data;
	GstSample *sample = gst_app_sink_pull_sample(sink);
	if (!sample)
		return GST_FLOW_OK;
	g_mutex_lock(&f->lock);
	GstSample *old = f->sample; /* not drawn in time: dropped */
	f->sample = sample;
	feed_notify(f);
	g_mutex_unlock(&f->lock);
	if (old)
		gst_sample_unref(old);
	return GST_FLOW_OK;
}

/* The planes are read with their strides: plinius-vdec then lets the MDP write straight into
 * the buffers that come here (its lines are padded to 16 bytes) */
static gboolean on_propose_allocation(GstAppSink *sink, GstQuery *query, gpointer user_data)
{
	gst_query_add_allocation_meta(query, GST_VIDEO_META_API_TYPE, NULL);
	return TRUE;
}

/* Audio and subtitles are not decoded: their pads come out as they are and stay unlinked */
static gboolean on_autoplug_continue(GstElement *bin, GstPad *pad, GstCaps *caps, gpointer user_data)
{
	static const char *const skipped[] = {"audio/", "text/", "subpicture/", "closedcaption/", "subtitle/",
					      "application/x-ass", "application/x-ssa", "application/x-subtitle"};
	if (gst_caps_is_empty(caps) || gst_caps_is_any(caps))
		return TRUE;
	const char *name = gst_structure_get_name(gst_caps_get_structure(caps, 0));
	for (guint i = 0; i < G_N_ELEMENTS(skipped); i++)
		if (g_str_has_prefix(name, skipped[i]))
			return FALSE;
	return TRUE;
}

static void on_pad_added(GstElement *decode, GstPad *pad, gpointer user_data)
{
	GstElement *convert = user_data;
	g_autoptr(GstCaps) caps = gst_pad_get_current_caps(pad);
	if (!caps)
		caps = gst_pad_query_caps(pad, NULL);
	if (!caps || gst_caps_is_empty(caps) || gst_caps_is_any(caps) ||
	    !gst_structure_has_name(gst_caps_get_structure(caps, 0), "video/x-raw"))
		return;
	g_autoptr(GstPad) sink = gst_element_get_static_pad(convert, "sink");
	/* A second video stream stays unlinked */
	if (gst_pad_is_linked(sink))
		return;
	if (GST_PAD_LINK_FAILED(gst_pad_link(pad, sink)))
		post_error(decode, "cannot link the decoded video");
}

static void on_unknown_type(GstElement *decode, GstPad *pad, GstCaps *caps, gpointer user_data)
{
	if (gst_caps_is_empty(caps) || gst_caps_is_any(caps) ||
	    !g_str_has_prefix(gst_structure_get_name(gst_caps_get_structure(caps, 0)), "video/"))
		return;
	g_autofree char *description = gst_caps_to_string(caps);
	g_autofree char *message = g_strdup_printf("no decoder for %s", description);
	post_error(decode, message);
}

static void on_no_more_pads(GstElement *decode, gpointer user_data)
{
	GstElement *convert = user_data;
	g_autoptr(GstPad) sink = gst_element_get_static_pad(convert, "sink");
	if (!gst_pad_is_linked(sink))
		post_error(decode, "no video stream that can be decoded");
}

/* Prerolled: plays from the start (or from where it was released) as a segment */
static void on_prerolled(struct video *v)
{
	if (!v->running || v->started)
		return;
	v->started = TRUE;
	gint64 at = MAX(v->resume_at, 0);
	GstSeekFlags flags = GST_SEEK_FLAG_FLUSH | GST_SEEK_FLAG_SEGMENT;
	/* Exactly where it stopped, not at the key frame before it: the decoder runs through the
	 * frames in between without showing them, the texture keeps the last frame meanwhile, so
	 * the video goes on without a jump back */
	if (at)
		flags |= GST_SEEK_FLAG_ACCURATE;
	if (!gst_element_seek(v->pipeline, 1.0, GST_FORMAT_TIME, flags, GST_SEEK_TYPE_SET, at, GST_SEEK_TYPE_NONE,
			      GST_CLOCK_TIME_NONE))
		g_debug("No segment seek in %s: looped at its end instead", v->path);
	v->loop_began = g_get_monotonic_time();
	v->short_loops = 0;
	if (v->visible)
		gst_element_set_state(v->pipeline, GST_STATE_PLAYING);
}

/* The end of the video (SEGMENT_DONE, or EOS without segments): from the start again */
static void loop(struct video *v, gboolean eos)
{
	if (!v->started || v->ended)
		return;
	gint64 now = g_get_monotonic_time();
	v->short_loops = now - v->loop_began < MIN_LOOP_US ? v->short_loops + 1 : 0;
	v->loop_began = now;
	gboolean looped = FALSE;
	if (v->short_loops < SHORT_LOOPS) {
		GstSeekFlags flags = GST_SEEK_FLAG_SEGMENT | (eos ? GST_SEEK_FLAG_FLUSH : 0);
		looped = gst_element_seek(v->pipeline, 1.0, GST_FORMAT_TIME, flags, GST_SEEK_TYPE_SET, 0,
					  GST_SEEK_TYPE_NONE, GST_CLOCK_TIME_NONE);
	}
	if (looped)
		return;
	/* A still picture (or a stream that does not go back): its last frame stays */
	g_message("%s is not looped (%s)", v->path, v->short_loops >= SHORT_LOOPS ? "no time passes in it" : "cannot seek");
	v->ended = TRUE;
	stop_pipeline(v);
}

static void read_orientation(struct video *v, GstMessage *message)
{
	GstTagList *tags = NULL;
	g_autofree char *value = NULL;
	gst_message_parse_tag(message, &tags);
	if (gst_tag_list_get_string(tags, TAG_IMAGE_ORIENTATION, &value)) {
		const char *degrees = strrchr(value, '-');
		v->flip = g_str_has_prefix(value, "flip-");
		v->rotation = (degrees ? atoi(degrees + 1) / 90 : 0) & 3;
		g_debug("Video orientation %s", value);
	}
	gst_tag_list_unref(tags);
}

static gboolean on_bus_message(GstBus *bus, GstMessage *message, gpointer user_data)
{
	struct video *v = user_data;
	if (v->failed)
		return G_SOURCE_CONTINUE;
	switch (GST_MESSAGE_TYPE(message)) {
	case GST_MESSAGE_ERROR:
		fail_with_message(v, message);
		break;
	case GST_MESSAGE_WARNING: {
		g_autoptr(GError) error = NULL;
		gst_message_parse_warning(message, &error, NULL);
		g_debug("Video %s: %s", v->path, error ? error->message : "warning");
		break;
	}
	case GST_MESSAGE_ASYNC_DONE:
		if (GST_MESSAGE_SRC(message) == GST_OBJECT(v->pipeline))
			on_prerolled(v);
		break;
	case GST_MESSAGE_SEGMENT_DONE:
		loop(v, FALSE);
		break;
	case GST_MESSAGE_EOS:
		loop(v, TRUE);
		break;
	case GST_MESSAGE_TAG:
		read_orientation(v, message);
		break;
	default:
		break;
	}
	return G_SOURCE_CONTINUE;
}

static gboolean build_pipeline(struct video *v)
{
	g_autoptr(GError) error = NULL;
	if (!gst_init_check(NULL, NULL, &error)) {
		fail(v, "GStreamer does not start", error ? error->message : NULL);
		return FALSE;
	}
	g_autofree char *uri = gst_filename_to_uri(v->path, &error);
	GstElement *decode = gst_element_factory_make("uridecodebin", NULL);
	GstElement *convert = gst_element_factory_make("videoconvert", NULL);
	GstElement *sink = gst_element_factory_make("appsink", NULL);
	if (!uri || !decode || !convert || !sink) {
		GstElement *made[] = {decode, convert, sink};
		for (guint i = 0; i < G_N_ELEMENTS(made); i++)
			if (made[i])
				gst_object_unref(gst_object_ref_sink(made[i]));
		fail(v, "GStreamer lacks uridecodebin, videoconvert or appsink", error ? error->message : NULL);
		return FALSE;
	}
	g_object_set(decode, "uri", uri, NULL);
	/* What the shader takes as it is */
	g_autoptr(GstCaps) caps =
		gst_caps_from_string("video/x-raw, format=(string){ NV12, I420, YV12, RGBA, BGRA, RGBx, BGRx }");
	/* Paced by the clock, late frames dropped (QoS: also by the decoder), only the newest frame
	 * kept, none held back for the last-sample property */
	g_object_set(sink, "caps", caps, "sync", TRUE, "qos", TRUE, "max-lateness", (gint64)MAX_LATENESS,
		     "max-buffers", 1u, "drop", TRUE, "enable-last-sample", FALSE, NULL);
	GstAppSinkCallbacks callbacks = {
		.new_sample = on_new_sample,
		.propose_allocation = on_propose_allocation,
	};
	gst_app_sink_set_callbacks(GST_APP_SINK(sink), &callbacks, v->feed, NULL);

	v->pipeline = gst_pipeline_new("plinius-ambient-video");
	gst_bin_add_many(GST_BIN(v->pipeline), decode, convert, sink, NULL);
	if (!gst_element_link(convert, sink)) {
		fail(v, "cannot link videoconvert to appsink", NULL);
		return FALSE;
	}
	g_signal_connect(decode, "autoplug-continue", G_CALLBACK(on_autoplug_continue), NULL);
	g_signal_connect(decode, "pad-added", G_CALLBACK(on_pad_added), convert);
	g_signal_connect(decode, "unknown-type", G_CALLBACK(on_unknown_type), NULL);
	g_signal_connect(decode, "no-more-pads", G_CALLBACK(on_no_more_pads), convert);
	g_autoptr(GstBus) bus = gst_element_get_bus(v->pipeline);
	v->bus_watch = gst_bus_add_watch(bus, on_bus_message, v);
	return TRUE;
}

/* NULL/READY → PAUSED: prerolls, then on_prerolled() seeks and plays */
static void start_pipeline(struct video *v)
{
	v->running = TRUE;
	v->started = FALSE;
	if (gst_element_set_state(v->pipeline, GST_STATE_PAUSED) != GST_STATE_CHANGE_FAILURE)
		return;
	/* The file cannot be opened, for instance: the reason is on the bus already */
	g_autoptr(GstBus) bus = gst_element_get_bus(v->pipeline);
	g_autoptr(GstMessage) message = gst_bus_pop_filtered(bus, GST_MESSAGE_ERROR);
	if (message)
		fail_with_message(v, message);
	else
		fail(v, "the pipeline does not start", NULL);
}

/* Frees the decoder, the file and the buffers; the textures keep the last frame */
static void release_pipeline(struct video *v)
{
	if (!v->pipeline || !v->running)
		return;
	gst_element_set_state(v->pipeline, GST_STATE_READY);
	v->running = v->started = FALSE;
}

static void stop_pipeline(struct video *v)
{
	g_clear_handle_id(&v->release_timer, g_source_remove);
	if (v->pipeline)
		gst_element_set_state(v->pipeline, GST_STATE_NULL);
	v->running = v->started = FALSE;
}

static gboolean on_release(gpointer user_data)
{
	struct video *v = user_data;
	gint64 position = -1;
	v->release_timer = 0;
	if (v->started && gst_element_query_position(v->pipeline, GST_FORMAT_TIME, &position))
		v->resume_at = position;
	g_debug("Hidden for %d s: video decoder released at %.2f s", RELEASE_S, MAX(v->resume_at, 0) / 1e9);
	release_pipeline(v);
	return G_SOURCE_REMOVE;
}

/* ---- GL ---- */

static const char vertex_shader[] =
	"attribute vec2 a_pos;\n"
	"uniform mat3 u_map;\n" /* the screen (0..1, y down) onto the frame (0..1, y down) */
	"uniform vec2 u_scale0, u_scale1;\n"
	"varying vec2 v_uv0, v_uv1;\n"
	"void main() {\n"
	"  vec2 uv = (u_map * vec3(a_pos.x * 0.5 + 0.5, 0.5 - a_pos.y * 0.5, 1.0)).xy;\n"
	"  v_uv0 = uv * u_scale0;\n"
	"  v_uv1 = uv * u_scale1;\n"
	"  gl_Position = vec4(a_pos, 0.0, 1.0);\n"
	"}\n";

/* highp: texture coordinates across 1920 texels need more than mediump's 10 bits. The limits
 * keep the linear filter off the padding at the end of the lines. */
#define FRAGMENT_SHADER(sample)                                                              \
	"#ifdef GL_FRAGMENT_PRECISION_HIGH\n"                                                \
	"precision highp float;\n"                                                           \
	"#else\n"                                                                            \
	"precision mediump float;\n"                                                         \
	"#endif\n"                                                                           \
	"varying vec2 v_uv0, v_uv1;\n"                                                       \
	"uniform vec2 u_limit0, u_limit1;\n"                                                 \
	"uniform sampler2D u_plane0, u_plane1, u_plane2;\n"                                  \
	"uniform mat3 u_matrix;\n"                                                           \
	"uniform vec3 u_offset;\n"                                                           \
	"uniform float u_opaque;\n"                                                          \
	"void main() {\n"                                                                    \
	"  vec2 uv0 = min(v_uv0, u_limit0), uv1 = min(v_uv1, u_limit1);\n"                   \
	"  vec4 c = " sample ";\n"                                                           \
	"  float a = max(c.a, u_opaque);\n"                                                  \
	"  gl_FragColor = vec4(clamp(u_matrix * c.rgb + u_offset, 0.0, 1.0) * a, a);\n"      \
	"}\n"

static const char *const fragment_shaders[N_LAYOUTS] = {
	[LAYOUT_RGB] = FRAGMENT_SHADER("texture2D(u_plane0, uv0)"),
	/* Luminance-alpha: U in .r, V in .a */
	[LAYOUT_NV12] = FRAGMENT_SHADER("vec4(texture2D(u_plane0, uv0).r, texture2D(u_plane1, uv1).ra, 1.0)"),
	[LAYOUT_I420] = FRAGMENT_SHADER("vec4(texture2D(u_plane0, uv0).r, texture2D(u_plane1, uv1).r, "
					"texture2D(u_plane2, uv1).r, 1.0)"),
};

static GLuint compile(GLenum type, const char *source)
{
	GLuint shader = glCreateShader(type);
	GLint ok = 0;
	glShaderSource(shader, 1, &source, NULL);
	glCompileShader(shader);
	glGetShaderiv(shader, GL_COMPILE_STATUS, &ok);
	if (!ok) {
		char log[1024] = "";
		glGetShaderInfoLog(shader, sizeof(log), NULL, log);
		g_warning("Video shader does not compile: %s", log);
	}
	return shader;
}

static gboolean build_program(struct program *p, const char *fragment_shader)
{
	GLuint vs = compile(GL_VERTEX_SHADER, vertex_shader), fs = compile(GL_FRAGMENT_SHADER, fragment_shader);
	GLint ok = 0;
	p->id = glCreateProgram();
	glAttachShader(p->id, vs);
	glAttachShader(p->id, fs);
	glBindAttribLocation(p->id, 0, "a_pos");
	glLinkProgram(p->id);
	glDeleteShader(vs);
	glDeleteShader(fs);
	glGetProgramiv(p->id, GL_LINK_STATUS, &ok);
	if (!ok) {
		g_warning("Video program does not link");
		return FALSE;
	}
	p->u_map = glGetUniformLocation(p->id, "u_map");
	p->u_scale0 = glGetUniformLocation(p->id, "u_scale0");
	p->u_scale1 = glGetUniformLocation(p->id, "u_scale1");
	p->u_limit0 = glGetUniformLocation(p->id, "u_limit0");
	p->u_limit1 = glGetUniformLocation(p->id, "u_limit1");
	p->u_matrix = glGetUniformLocation(p->id, "u_matrix");
	p->u_offset = glGetUniformLocation(p->id, "u_offset");
	p->u_opaque = glGetUniformLocation(p->id, "u_opaque");
	for (int i = 0; i < 3; i++) {
		char name[] = "u_plane0";
		name[7] = '0' + i;
		p->u_plane[i] = glGetUniformLocation(p->id, name);
	}
	return TRUE;
}

static gboolean setup_gl(struct video *v)
{
	for (int i = 0; i < N_LAYOUTS; i++)
		if (!build_program(&v->programs[i], fragment_shaders[i]))
			return FALSE;
	glGenTextures(3, v->textures);
	for (int i = 0; i < 3; i++) {
		glBindTexture(GL_TEXTURE_2D, v->textures[i]);
		glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
		glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
		glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
		glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
	}
	static const float corners[] = {-1, -1, 1, -1, -1, 1, 1, 1};
	glGenBuffers(1, &v->vbo);
	glBindBuffer(GL_ARRAY_BUFFER, v->vbo);
	glBufferData(GL_ARRAY_BUFFER, sizeof(corners), corners, GL_STATIC_DRAW);
	return TRUE;
}

/* One plane into texture i. GLES 2 has no row length: the texture is as wide as the stride
 * (the padding cut off by the texture coordinates), the plane repacked only if its stride is no
 * whole number of texels. False if it is larger than the GPU takes. */
static gboolean upload_plane(struct video *v, int i, GLenum format, int bpp, int width, int height, int stride,
			     const guint8 *data, float scale[2], float limit[2])
{
	static GLint max_size;
	if (!max_size)
		glGetIntegerv(GL_MAX_TEXTURE_SIZE, &max_size);
	int tex_width = stride % bpp == 0 && stride / bpp >= width ? stride / bpp : width;
	if (tex_width > max_size || height > max_size)
		return FALSE;
	guint8 *packed = NULL;
	if (tex_width * bpp != stride) {
		packed = g_malloc((gsize)width * bpp * height);
		for (int y = 0; y < height; y++)
			memcpy(packed + (gsize)y * width * bpp, data + (gsize)y * stride, (gsize)width * bpp);
		data = packed;
	}
	glBindTexture(GL_TEXTURE_2D, v->textures[i]);
	if (v->tex_width[i] != tex_width || v->tex_height[i] != height || v->tex_format[i] != format) {
		glTexImage2D(GL_TEXTURE_2D, 0, format, tex_width, height, 0, format, GL_UNSIGNED_BYTE, data);
		v->tex_width[i] = tex_width;
		v->tex_height[i] = height;
		v->tex_format[i] = format;
	} else {
		glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, tex_width, height, format, GL_UNSIGNED_BYTE, data);
	}
	g_free(packed);
	scale[0] = (float)width / tex_width;
	scale[1] = 1;
	limit[0] = (width - 0.5f) / tex_width;
	limit[1] = (height - 0.5f) / height;
	return TRUE;
}

static void set_matrix(struct video *v, const float rows[3][3], const float offset[3])
{
	for (int c = 0; c < 3; c++)
		for (int r = 0; r < 3; r++)
			v->matrix[c * 3 + r] = rows[r][c];
	memcpy(v->offset, offset, sizeof(v->offset));
}

/* Y'CbCr from the textures (0..1) to R'G'B' with the stream's matrix and range */
static void set_yuv_matrix(struct video *v, const GstVideoInfo *info)
{
	gdouble kr, kb;
	if (!gst_video_color_matrix_get_Kr_Kb(info->colorimetry.matrix, &kr, &kb)) {
		kr = 0.2126; /* BT.709 */
		kb = 0.0722;
	}
	gint offsets[GST_VIDEO_MAX_COMPONENTS], scales[GST_VIDEO_MAX_COMPONENTS];
	gst_video_color_range_offsets(info->colorimetry.range, info->finfo, offsets, scales);
	double kg = 1 - kr - kb;
	/* Y' 0..1 and Cb, Cr -0.5..0.5 to R'G'B' */
	double a[3][3] = {{1, 0, 2 * (1 - kr)},
			  {1, -2 * kb * (1 - kb) / kg, -2 * kr * (1 - kr) / kg},
			  {1, 2 * (1 - kb), 0}};
	/* The textures' 0..1 to Y' and Cb, Cr: t * 255 / scale - offset / scale */
	double d[3], o[3];
	for (int i = 0; i < 3; i++) {
		d[i] = 255.0 / scales[i];
		o[i] = -(double)offsets[i] / scales[i];
	}
	float rows[3][3], offset[3];
	for (int r = 0; r < 3; r++) {
		offset[r] = 0;
		for (int c = 0; c < 3; c++) {
			rows[r][c] = a[r][c] * d[c];
			offset[r] += a[r][c] * o[c];
		}
	}
	set_matrix(v, rows, offset);
}

static void set_rgb_matrix(struct video *v, gboolean bgr, gboolean opaque)
{
	static const float zero[3] = {0, 0, 0};
	static const float rgb[3][3] = {{1, 0, 0}, {0, 1, 0}, {0, 0, 1}}, swapped[3][3] = {{0, 0, 1}, {0, 1, 0}, {1, 0, 0}};
	set_matrix(v, bgr ? swapped : rgb, zero);
	v->opaque = opaque ? 1 : 0;
}

/* The video frame into the textures */
static void upload_sample(struct video *v, GstSample *sample)
{
	GstCaps *caps = gst_sample_get_caps(sample);
	GstBuffer *buffer = gst_sample_get_buffer(sample);
	GstVideoInfo info;
	GstVideoFrame frame;
	if (!caps || !buffer || !gst_video_info_from_caps(&info, caps))
		return;
	if (!gst_video_frame_map(&frame, &info, buffer, GST_MAP_READ)) {
		g_debug("Cannot map a video frame");
		return;
	}
	GstVideoFormat format = GST_VIDEO_INFO_FORMAT(&info);
	gboolean ok = TRUE;
	glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
	if (format == GST_VIDEO_FORMAT_NV12 || format == GST_VIDEO_FORMAT_I420 || format == GST_VIDEO_FORMAT_YV12) {
		/* By component, not by plane: YV12 has V before U */
		gboolean nv12 = format == GST_VIDEO_FORMAT_NV12;
		v->layout = nv12 ? LAYOUT_NV12 : LAYOUT_I420;
		ok = upload_plane(v, 0, GL_LUMINANCE, 1, GST_VIDEO_FRAME_COMP_WIDTH(&frame, 0),
				  GST_VIDEO_FRAME_COMP_HEIGHT(&frame, 0), GST_VIDEO_FRAME_COMP_STRIDE(&frame, 0),
				  GST_VIDEO_FRAME_COMP_DATA(&frame, 0), v->scale[0], v->limit[0]);
		for (int c = 1; ok && c <= (nv12 ? 1 : 2); c++)
			ok = upload_plane(v, c, nv12 ? GL_LUMINANCE_ALPHA : GL_LUMINANCE, nv12 ? 2 : 1,
					  GST_VIDEO_FRAME_COMP_WIDTH(&frame, c), GST_VIDEO_FRAME_COMP_HEIGHT(&frame, c),
					  GST_VIDEO_FRAME_COMP_STRIDE(&frame, c), GST_VIDEO_FRAME_COMP_DATA(&frame, c),
					  v->scale[1], v->limit[1]);
		set_yuv_matrix(v, &info);
		v->opaque = 1;
	} else {
		/* RGBA, BGRA, RGBx, BGRx (the appsink's caps) */
		v->layout = LAYOUT_RGB;
		ok = upload_plane(v, 0, GL_RGBA, 4, GST_VIDEO_FRAME_WIDTH(&frame), GST_VIDEO_FRAME_HEIGHT(&frame),
				  GST_VIDEO_FRAME_PLANE_STRIDE(&frame, 0), GST_VIDEO_FRAME_PLANE_DATA(&frame, 0),
				  v->scale[0], v->limit[0]);
		set_rgb_matrix(v, format == GST_VIDEO_FORMAT_BGRA || format == GST_VIDEO_FORMAT_BGRx,
			       format == GST_VIDEO_FORMAT_RGBx || format == GST_VIDEO_FORMAT_BGRx);
	}
	gst_video_frame_unmap(&frame);
	if (!ok) {
		fail(v, "the frame is larger than the GPU's textures", NULL);
		return;
	}
	if (!v->have_frame)
		g_message("Video %s: %d×%d %s", v->path, GST_VIDEO_INFO_WIDTH(&info), GST_VIDEO_INFO_HEIGHT(&info),
			  gst_video_format_to_string(format));
	v->aspect = (float)GST_VIDEO_INFO_WIDTH(&info) * GST_VIDEO_INFO_PAR_N(&info) /
		    ((float)GST_VIDEO_INFO_HEIGHT(&info) * MAX(GST_VIDEO_INFO_PAR_D(&info), 1));
	v->have_frame = TRUE;
}

/* The GIF frame into the texture */
static void upload_picture(struct video *v, GdkPixbuf *picture)
{
	int channels = gdk_pixbuf_get_n_channels(picture);
	glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
	v->layout = LAYOUT_RGB;
	if (!upload_plane(v, 0, channels == 4 ? GL_RGBA : GL_RGB, channels, gdk_pixbuf_get_width(picture),
			  gdk_pixbuf_get_height(picture), gdk_pixbuf_get_rowstride(picture),
			  gdk_pixbuf_read_pixels(picture), v->scale[0], v->limit[0])) {
		fail(v, "the picture is larger than the GPU's textures", NULL);
		return;
	}
	set_rgb_matrix(v, FALSE, channels != 4);
	if (!v->have_frame)
		g_message("Animated GIF %s: %d×%d", v->path, gdk_pixbuf_get_width(picture), gdk_pixbuf_get_height(picture));
	v->aspect = (float)gdk_pixbuf_get_width(picture) / gdk_pixbuf_get_height(picture);
	v->have_frame = TRUE;
}

/* What the threads handed over since the last frame (context current) */
static void take_news(struct video *v)
{
	if (!v->feed)
		return;
	g_mutex_lock(&v->feed->lock);
	GstSample *sample = g_steal_pointer(&v->feed->sample);
	GdkPixbuf *picture = g_steal_pointer(&v->feed->picture);
	char *error = g_steal_pointer(&v->feed->error);
	g_mutex_unlock(&v->feed->lock);
	if (error)
		fail(v, error, NULL);
	if (!v->failed && !v->gl_ready && !v->gl_failed && (sample || picture)) {
		v->gl_ready = setup_gl(v);
		v->gl_failed = !v->gl_ready;
	}
	if (!v->failed && v->gl_ready) {
		if (sample)
			upload_sample(v, sample);
		if (picture)
			upload_picture(v, picture);
	}
	if (sample)
		gst_sample_unref(sample);
	g_clear_object(&picture);
	g_free(error);
}

/* Where a point of the screen (0..1, y down) lies in the frame (0..1, y down): the frame's
 * middle covering the screen, turned and mirrored as its orientation tag says */
static void map_point(const struct video *v, float aspect, float s, float t, float out[2])
{
	float shown = v->rotation & 1 ? 1 / v->aspect : v->aspect;
	float kx = shown > aspect ? aspect / shown : 1, ky = shown > aspect ? 1 : shown / aspect;
	float x = 0.5f + (s - 0.5f) * kx, y = 0.5f + (t - 0.5f) * ky, u, w;
	/* The picture turned clockwise by the tag: back to the frame */
	switch (v->rotation) {
	case 1:
		u = y;
		w = 1 - x;
		break;
	case 2:
		u = 1 - x;
		w = 1 - y;
		break;
	case 3:
		u = 1 - y;
		w = x;
		break;
	default:
		u = x;
		w = y;
	}
	out[0] = v->flip ? 1 - u : u;
	out[1] = w;
}

/* ---- The effect ---- */

static gboolean on_news(gpointer user_data)
{
	struct video *v = user_data;
	if (v->scene->wake)
		v->scene->wake();
	return G_SOURCE_CONTINUE;
}

static void *video_create(const struct scene *scene)
{
	struct video *v = g_new0(struct video, 1);
	v->scene = scene;
	v->resume_at = -1;
	v->path = g_strdup(scene->video ? scene->video : "");
	if (!*v->path) {
		v->failed = TRUE;
		return v;
	}
	if (!g_file_test(v->path, G_FILE_TEST_IS_REGULAR)) {
		report(v, "no such file", NULL);
		v->failed = TRUE;
		return v;
	}
	v->feed = feed_new(v->path);
	v->feed->notify = g_source_new(&notify_funcs, sizeof(GSource));
	g_source_set_callback(v->feed->notify, on_news, v, NULL);
	g_source_set_name(v->feed->notify, "plinius-ambient video frame");
	g_source_attach(v->feed->notify, NULL);
	v->gif = is_gif(v->path);
	if (v->gif)
		g_thread_unref(g_thread_new("ambient-gif", gif_thread, feed_ref(v->feed)));
	else
		build_pipeline(v);
	return v;
}

static void video_update(void *state, const struct scene *scene, float sx, float sy) {}

/* Always at rest: each new frame wakes the client (on_news), which draws it */
static bool video_step(void *state, const struct scene *scene, float dt)
{
	return false;
}

static bool video_wake(void *state, const struct scene *scene)
{
	struct video *v = state;
	if (!v->feed)
		return false;
	g_mutex_lock(&v->feed->lock);
	bool news = v->feed->sample || v->feed->picture || v->feed->error;
	g_mutex_unlock(&v->feed->lock);
	return news;
}

static void video_build(void *state, const struct scene *scene, struct vertex_buffer *out) {}

static void video_set_visible(void *state, bool visible)
{
	struct video *v = state;
	v->visible = visible;
	if (v->failed || !v->feed)
		return;
	if (v->gif) {
		g_mutex_lock(&v->feed->lock);
		v->feed->visible = visible;
		g_cond_signal(&v->feed->cond);
		g_mutex_unlock(&v->feed->lock);
		return;
	}
	if (!v->pipeline || v->ended)
		return;
	if (visible) {
		g_clear_handle_id(&v->release_timer, g_source_remove);
		if (!v->running)
			start_pipeline(v);
		else if (v->started)
			gst_element_set_state(v->pipeline, GST_STATE_PLAYING);
		/* else on_prerolled() plays it */
		return;
	}
	if (!v->running)
		return;
	gst_element_set_state(v->pipeline, GST_STATE_PAUSED);
	/* No frame held for later: the texture has the last one drawn */
	g_mutex_lock(&v->feed->lock);
	GstSample *sample = g_steal_pointer(&v->feed->sample);
	g_mutex_unlock(&v->feed->lock);
	if (sample)
		gst_sample_unref(sample);
	if (!v->release_timer)
		v->release_timer = g_timeout_add_seconds(RELEASE_S, on_release, v);
}

static void video_render(void *state, const struct scene *scene, int width, int height)
{
	struct video *v = state;
	take_news(v);
	glViewport(0, 0, width, height);
	glDisable(GL_BLEND);
	if (v->failed || !v->have_frame || !v->gl_ready) {
		/* Phosh's own background shows */
		glClearColor(0, 0, 0, 0);
		glClear(GL_COLOR_BUFFER_BIT);
		return;
	}
	/* The screen's corners in the frame: an affine map */
	float aspect = (float)width / MAX(height, 1), o[2], x[2], y[2];
	map_point(v, aspect, 0, 0, o);
	map_point(v, aspect, 1, 0, x);
	map_point(v, aspect, 0, 1, y);
	float map[9] = {x[0] - o[0], x[1] - o[1], 0, y[0] - o[0], y[1] - o[1], 0, o[0], o[1], 1};
	struct program *p = &v->programs[v->layout];
	int planes = v->layout == LAYOUT_RGB ? 1 : v->layout == LAYOUT_NV12 ? 2 : 3;
	glUseProgram(p->id);
	glUniformMatrix3fv(p->u_map, 1, GL_FALSE, map);
	glUniform2fv(p->u_scale0, 1, v->scale[0]);
	glUniform2fv(p->u_scale1, 1, v->scale[planes > 1 ? 1 : 0]);
	glUniform2fv(p->u_limit0, 1, v->limit[0]);
	glUniform2fv(p->u_limit1, 1, v->limit[planes > 1 ? 1 : 0]);
	glUniformMatrix3fv(p->u_matrix, 1, GL_FALSE, v->matrix);
	glUniform3fv(p->u_offset, 1, v->offset);
	glUniform1f(p->u_opaque, v->opaque);
	for (int i = planes - 1; i >= 0; i--) {
		/* Unit 0 last: the effects drawn over it expect it active */
		glActiveTexture(GL_TEXTURE0 + i);
		glBindTexture(GL_TEXTURE_2D, v->textures[i]);
		glUniform1i(p->u_plane[i], i);
	}
	glBindBuffer(GL_ARRAY_BUFFER, v->vbo);
	for (int i = 1; i < 4; i++)
		glDisableVertexAttribArray(i);
	glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 0, NULL);
	glEnableVertexAttribArray(0);
	glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
}

static void video_destroy(void *state)
{
	struct video *v = state;
	g_clear_handle_id(&v->release_timer, g_source_remove);
	if (v->feed) {
		/* The threads see that it is gone (and the GIF thread ends) */
		g_mutex_lock(&v->feed->lock);
		GSource *notify = g_steal_pointer(&v->feed->notify);
		g_cond_signal(&v->feed->cond);
		g_mutex_unlock(&v->feed->lock);
		g_source_destroy(notify);
		g_source_unref(notify);
	}
	if (v->pipeline) {
		gst_element_set_state(v->pipeline, GST_STATE_NULL);
		g_clear_handle_id(&v->bus_watch, g_source_remove);
		gst_object_unref(v->pipeline);
	}
	if (v->feed)
		feed_unref(v->feed);
	/* The GL context is current whenever an effect is replaced (see plinius-ambient.c) */
	if (v->gl_ready || v->gl_failed) {
		for (int i = 0; i < N_LAYOUTS; i++)
			if (v->programs[i].id)
				glDeleteProgram(v->programs[i].id);
		if (v->textures[0])
			glDeleteTextures(3, v->textures);
		if (v->vbo)
			glDeleteBuffers(1, &v->vbo);
	}
	g_free(v->path);
	g_free(v);
}

const struct effect video_effect = {
	.name = "video",
	.fps = 60, /* at most: drawn once per decoded frame */
	.full_resolution = true,
	.create = video_create,
	.update = video_update,
	.step = video_step,
	.wake = video_wake,
	.build = video_build,
	.render = video_render,
	.set_visible = video_set_visible,
	.destroy = video_destroy,
};
