/*
 * Harness for the encoder of plinius-vdec (gstpliniusvenc.c, a copy with its paths made relative:
 * the test runs it in a directory of its own with sys/class/video4linux, dev/videoN and
 * dev/dma_heap/mtk_mm as plain files). The kernel side is a fake:
 *
 *  - the MediaTek encoder (open/ioctl/poll/mmap on dev/videoN): a stateful V4L2 m2m encoder that
 *    pads the lines of its frames to 16 bytes (or FAKE_PITCH), "encodes" each queued frame into a
 *    picture that tells what it read ("pic <frame> y=<sum> uv=<sum>", the sums over the visible
 *    bytes of each plane with the pitch it gave), key frames by its GOP size and on a forced key
 *    frame, returns the frame's timestamp on the picture and the frame's buffer right away;
 *  - the dma-heap: DMA_HEAP_IOCTL_ALLOC gives a memfd of the size asked for.
 * Everything the element asks for is logged to FAKE_LOG. Faults: FAKE_FAIL="op:n[:errno],..."
 * makes the n-th call of op fail (n 0: every call); the other FAKE_* knobs are read below.
 *
 * Commands (one per line, from the file in argv[1]):
 *   element [PROP=VALUE ...] | caps CAPS | push K [key] | pushshort | eos | flush | nodev | stop |
 *   start (the element alone to PAUSED)
 * Output: "push <flow>", "picture pts=<pts> key=<0|1> <bytes>" per buffer out, "eos <ok>",
 * "error <text> | <debug>" and "warning <text> | <debug>" per bus message.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

#include <linux/dma-buf.h>
#include <linux/dma-heap.h>
#include <linux/videodev2.h>

#include <gst/check/gstharness.h>
#include <gst/gst.h>
#include <gst/video/gstvideoencoder.h>
#include <gst/video/video.h>

/* ---- Log and faults ---- */

static void say(const char *fmt, ...) G_GNUC_PRINTF(1, 2);
static void say(const char *fmt, ...)
{
	const char *path = getenv("FAKE_LOG");
	if (!path)
		return;
	FILE *f = fopen(path, "a");
	va_list args;
	va_start(args, fmt);
	vfprintf(f, fmt, args);
	va_end(args);
	fputc('\n', f);
	fclose(f);
}

static int knob(const char *name, int def)
{
	const char *v = getenv(name);
	return v && *v ? atoi(v) : def;
}

/* errno for the call of @op that FAKE_FAIL names, else 0 */
static int fails(const char *op)
{
	static struct { char op[32]; int calls; } seen[64];
	int i;
	for (i = 0; i < 64 && seen[i].op[0] && strcmp(seen[i].op, op); i++)
		;
	if (i == 64)
		return 0;
	g_strlcpy(seen[i].op, op, sizeof seen[i].op);
	int call = ++seen[i].calls;
	const char *spec = getenv("FAKE_FAIL");
	if (!spec)
		return 0;
	g_auto(GStrv) items = g_strsplit(spec, ",", -1);
	for (char **it = items; *it; it++) {
		g_auto(GStrv) f = g_strsplit(*it, ":", 3);
		if (f[0] && f[1] && !strcmp(f[0], op) && (atoi(f[1]) == 0 || atoi(f[1]) == call))
			return f[2] ? atoi(f[2]) : EIO;
	}
	return 0;
}

/* ---- Files: which fd is what ---- */

enum { FD_NONE, FD_DEV, FD_HEAP, FD_BUF };
static unsigned char kinds[4096];
static int syncs;

#define MAX_BUFS 32
#define BS_OFFSET 0x100000 /* mmap offset of bitstream buffer i: i * BS_OFFSET */

typedef struct {
	char data[160];
	long frame;
	gboolean key;
	int polls; /* polls to go before it is ready */
} Picture;

static struct {
	unsigned width, height, pitch, sizeimage, gop;
	gboolean nv21, force_key;
	int n_in, n_bs;
	gboolean in_done[MAX_BUFS];
	gboolean bs_queued[MAX_BUFS];
	guint8 *bs_map[MAX_BUFS];
	Picture pics[64];
	int n_pics, frames;
	gboolean bad_index_sent;
} enc;

static int fake_open(const char *path, int flags, ...)
{
	int kind = g_str_has_prefix(path, "dev/video") ? FD_DEV : !strcmp(path, "dev/dma_heap/mtk_mm") ? FD_HEAP : FD_NONE;
	int e = kind == FD_DEV ? fails("open") : 0;
	if (e) {
		errno = e;
		return -1;
	}
	int fd = open(path, flags);
	if (fd >= 0 && fd < 4096)
		kinds[fd] = kind;
	if (fd >= 0 && kind == FD_DEV) {
		/* an encoder of its own for every open */
		memset(&enc, 0, sizeof enc);
		say("open %s", path);
	}
	return fd;
}

static int fake_close(int fd)
{
	if (fd >= 0 && fd < 4096) {
		if (kinds[fd] == FD_DEV)
			say("close device");
		kinds[fd] = FD_NONE;
	}
	return close(fd);
}

static void *fake_mmap(void *addr, size_t len, int prot, int flags, int fd, off_t offset)
{
	if (fd >= 0 && fd < 4096 && kinds[fd] == FD_DEV) {
		int e = fails("mmap_bitstream");
		if (e) {
			errno = e;
			return MAP_FAILED;
		}
		void *p = mmap(NULL, len, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
		long index = offset / BS_OFFSET;
		if (p != MAP_FAILED && index >= 0 && index < MAX_BUFS)
			enc.bs_map[index] = p;
		return p;
	}
	int e = fails("mmap_frame");
	if (e) {
		errno = e;
		return MAP_FAILED;
	}
	return mmap(addr, len, prot, flags, fd, offset);
}

/* ---- The encoder ---- */

static const char *control_name(unsigned id)
{
	switch (id) {
	case V4L2_CID_MPEG_VIDEO_BITRATE: return "bitrate";
	case V4L2_CID_MPEG_VIDEO_H264_PROFILE: return "profile";
	case V4L2_CID_MPEG_VIDEO_H264_LEVEL: return "level";
	case V4L2_CID_MPEG_VIDEO_HEADER_MODE: return "header";
	case V4L2_CID_MPEG_VIDEO_GOP_SIZE: return "gop";
	case V4L2_CID_MPEG_VIDEO_FORCE_KEY_FRAME: return "force-key";
	default: return "?";
	}
}

/* Sum of the visible bytes of a plane, read with the encoder's pitch */
static unsigned long plane_sum(int fd, unsigned rows, unsigned long *first)
{
	size_t len = (size_t)enc.pitch * rows;
	guint8 *m = mmap(NULL, len, PROT_READ, MAP_SHARED, fd, 0);
	unsigned long sum = 0;
	if (m == MAP_FAILED)
		return 0;
	for (unsigned y = 0; y < rows; y++)
		for (unsigned x = 0; x < enc.width; x++)
			sum += m[(size_t)y * enc.pitch + x];
	*first = m[0];
	munmap(m, len);
	return sum;
}

static int dev_ioctl(unsigned long req, void *arg)
{
	int e;
	switch (req) {
	case VIDIOC_S_FMT: {
		struct v4l2_format *f = arg;
		if (f->type == V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE) {
			if ((e = fails("s_fmt_bitstream")))
				return errno = e, -1;
			enc.sizeimage = f->fmt.pix_mp.plane_fmt[0].sizeimage;
			say("s_fmt bitstream %c%c%c%c %ux%u size %u", f->fmt.pix_mp.pixelformat & 0xff,
			    (f->fmt.pix_mp.pixelformat >> 8) & 0xff, (f->fmt.pix_mp.pixelformat >> 16) & 0xff,
			    f->fmt.pix_mp.pixelformat >> 24, f->fmt.pix_mp.width, f->fmt.pix_mp.height, enc.sizeimage);
			return 0;
		}
		if ((e = fails("s_fmt_frames")))
			return errno = e, -1;
		enc.width = f->fmt.pix_mp.width;
		enc.height = f->fmt.pix_mp.height;
		enc.nv21 = f->fmt.pix_mp.pixelformat == V4L2_PIX_FMT_NV21M;
		enc.pitch = knob("FAKE_PITCH", (enc.width + 15) & ~15u);
		f->fmt.pix_mp.num_planes = knob("FAKE_PLANES", 2);
		f->fmt.pix_mp.plane_fmt[0].bytesperline = enc.pitch;
		f->fmt.pix_mp.plane_fmt[1].bytesperline = knob("FAKE_UV_PITCH", enc.pitch);
		f->fmt.pix_mp.plane_fmt[0].sizeimage = enc.pitch * enc.height - knob("FAKE_Y_SHORT", 0);
		f->fmt.pix_mp.plane_fmt[1].sizeimage = enc.pitch * (enc.height / 2) - knob("FAKE_UV_SHORT", 0);
		say("s_fmt frames %s %ux%u pitch %u", enc.nv21 ? "NM21" : "NM12", enc.width, enc.height, enc.pitch);
		return 0;
	}
	case VIDIOC_S_PARM: {
		struct v4l2_streamparm *p = arg;
		if ((e = fails("s_parm")))
			return errno = e, -1;
		say("s_parm %u/%u", p->parm.output.timeperframe.denominator, p->parm.output.timeperframe.numerator);
		return 0;
	}
	case VIDIOC_S_CTRL: {
		struct v4l2_control *c = arg;
		if ((e = fails("s_ctrl")))
			return errno = e, -1;
		say("ctrl %s %d", control_name(c->id), c->value);
		if (c->id == V4L2_CID_MPEG_VIDEO_GOP_SIZE)
			enc.gop = c->value;
		if (c->id == V4L2_CID_MPEG_VIDEO_FORCE_KEY_FRAME)
			enc.force_key = TRUE;
		return 0;
	}
	case VIDIOC_REQBUFS: {
		struct v4l2_requestbuffers *r = arg;
		gboolean frames = r->type == V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE;
		if ((e = fails(frames ? "reqbufs_frames" : "reqbufs_bitstream")))
			return errno = e, -1;
		say("reqbufs %s %u %s", frames ? "frames" : "bitstream", r->count,
		    r->memory == V4L2_MEMORY_DMABUF ? "dmabuf" : "mmap");
		if (r->count)
			r->count = MIN(r->count, (unsigned)knob(frames ? "FAKE_FRAMES" : "FAKE_BITSTREAMS", 4));
		*(frames ? &enc.n_in : &enc.n_bs) = r->count;
		return 0;
	}
	case VIDIOC_QUERYBUF: {
		struct v4l2_buffer *b = arg;
		if ((e = fails("querybuf")))
			return errno = e, -1;
		b->m.planes[0].length = enc.sizeimage;
		b->m.planes[0].m.mem_offset = b->index * BS_OFFSET;
		return 0;
	}
	case VIDIOC_QBUF: {
		struct v4l2_buffer *b = arg;
		if (b->reserved2 != (guint32)-1)
			say("qbuf without reserved2 = -1");
		if (b->type == V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE) {
			if ((e = fails("qbuf_bitstream")))
				return errno = e, -1;
			enc.bs_queued[b->index] = TRUE;
			return 0;
		}
		if ((e = fails("qbuf_frame")))
			return errno = e, -1;
		unsigned long y0 = 0, uv0 = 0;
		unsigned long ysum = plane_sum(b->m.planes[0].m.fd, enc.height, &y0);
		unsigned long uvsum = plane_sum(b->m.planes[1].m.fd, enc.height / 2, &uv0);
		Picture *p = &enc.pics[enc.n_pics++];
		p->frame = b->timestamp.tv_sec + (knob("FAKE_STRANGER", -1) == enc.frames ? 1000 : 0);
		p->key = enc.force_key || (enc.gop && enc.frames % enc.gop == 0);
		p->polls = knob("FAKE_LAZY", 0);
		if (knob("FAKE_EMPTY", -1) == enc.frames)
			p->data[0] = '\0';
		else
			snprintf(p->data, sizeof p->data, "pic %ld y=%lu uv=%lu first=%lu,%lu bytesused=%u/%u", p->frame,
				 ysum, uvsum, y0, uv0, b->m.planes[0].bytesused, b->m.planes[1].bytesused);
		enc.force_key = FALSE;
		enc.frames++;
		if (!knob("FAKE_HOLD", 0))
			enc.in_done[b->index] = TRUE;
		return 0;
	}
	case VIDIOC_DQBUF: {
		struct v4l2_buffer *b = arg;
		if (b->type == V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE) {
			for (int i = 0; i < enc.n_in; i++)
				if (enc.in_done[i]) {
					enc.in_done[i] = FALSE;
					b->index = i;
					return 0;
				}
			return errno = EAGAIN, -1;
		}
		if (knob("FAKE_BAD_INDEX", 0) && !enc.bad_index_sent) {
			enc.bad_index_sent = TRUE;
			b->index = 99;
			return 0;
		}
		int i;
		for (i = 0; i < enc.n_bs && !enc.bs_queued[i]; i++)
			;
		if (enc.n_pics == 0 || i == enc.n_bs || enc.pics[0].polls > 0)
			return errno = EAGAIN, -1;
		Picture p = enc.pics[0];
		memmove(enc.pics, enc.pics + 1, sizeof(Picture) * --enc.n_pics);
		unsigned offset = knob("FAKE_OFFSET", 0), len = strlen(p.data);
		memset(enc.bs_map[i], 'x', offset);
		memcpy(enc.bs_map[i] + offset, p.data, len);
		b->index = i;
		b->m.planes[0].bytesused = len ? offset + len : 0;
		b->m.planes[0].data_offset = offset;
		b->flags = p.key ? V4L2_BUF_FLAG_KEYFRAME : 0;
		b->timestamp.tv_sec = p.frame;
		enc.bs_queued[i] = FALSE;
		return 0;
	}
	case VIDIOC_STREAMON:
	case VIDIOC_STREAMOFF: {
		int *t = arg;
		gboolean on = req == VIDIOC_STREAMON;
		if (on && (e = fails(*t == V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE ? "streamon_frames" : "streamon_bitstream")))
			return errno = e, -1;
		say("stream%s %s", on ? "on" : "off", *t == V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE ? "frames" : "bitstream");
		return 0;
	}
	}
	say("unknown ioctl %#lx", req);
	return errno = ENOTTY, -1;
}

static int fake_ioctl(int fd, unsigned long req, ...)
{
	va_list args;
	va_start(args, req);
	void *arg = va_arg(args, void *);
	va_end(args);
	int kind = fd >= 0 && fd < 4096 ? kinds[fd] : FD_NONE;
	if (kind == FD_HEAP && req == DMA_HEAP_IOCTL_ALLOC) {
		struct dma_heap_allocation_data *a = arg;
		int e = fails("alloc");
		if (e)
			return errno = e, -1;
		int buf = memfd_create("dmabuf", MFD_CLOEXEC);
		if (ftruncate(buf, a->len) < 0)
			return -1;
		if (buf < 4096)
			kinds[buf] = FD_BUF;
		a->fd = buf;
		say("alloc %llu", (unsigned long long)a->len);
		return 0;
	}
	if (kind == FD_BUF && req == DMA_BUF_IOCTL_SYNC) {
		syncs++;
		return 0;
	}
	if (kind == FD_DEV)
		return dev_ioctl(req, arg);
	return ioctl(fd, req, arg);
}

static int fake_poll(struct pollfd *fds, nfds_t n, int timeout)
{
	struct pollfd *p = &fds[0];
	int e = fails("poll");
	if (e)
		return errno = e, -1;
	gboolean bs_free = FALSE, in_done = FALSE;
	for (int i = 0; i < enc.n_bs; i++)
		bs_free |= enc.bs_queued[i];
	for (int i = 0; i < enc.n_in; i++)
		in_done |= enc.in_done[i];
	if (enc.n_pics && enc.pics[0].polls > 0)
		enc.pics[0].polls--;
	p->revents = (knob("FAKE_POLLERR", 0) ? POLLERR : 0) |
		     (enc.n_pics && enc.pics[0].polls == 0 && bs_free ? POLLIN : 0) | (in_done ? POLLOUT : 0);
	if (!p->revents && timeout > 0)
		g_usleep(MIN(timeout, 5) * 1000);
	return p->revents ? 1 : 0;
}

#define open fake_open
#define close fake_close
#define ioctl fake_ioctl
#define poll fake_poll
#define mmap fake_mmap
#include "gstpliniusvenc.c"
#undef open
#undef close
#undef ioctl
#undef poll
#undef mmap

/* ---- The script ---- */

static void show_messages(GstBus *bus)
{
	GstMessage *m;
	while (bus && (m = gst_bus_pop(bus))) {
		GError *error = NULL;
		gchar *debug = NULL;
		/* The debug text after the source location GStreamer puts before it */
		if (GST_MESSAGE_TYPE(m) == GST_MESSAGE_ERROR) {
			gst_message_parse_error(m, &error, &debug);
			const char *text = debug && strrchr(debug, '\n') ? strrchr(debug, '\n') + 1 : "";
			printf("error %s | %s\n", error->message, text);
		} else if (GST_MESSAGE_TYPE(m) == GST_MESSAGE_WARNING) {
			gst_message_parse_warning(m, &error, &debug);
			const char *text = debug && strrchr(debug, '\n') ? strrchr(debug, '\n') + 1 : "";
			printf("warning %s | %s\n", error->message, text);
		}
		g_clear_error(&error);
		g_free(debug);
		gst_message_unref(m);
	}
}

static void pull(GstHarness *h)
{
	GstBuffer *b;
	while (h && (b = gst_harness_try_pull(h))) {
		GstMapInfo map;
		gst_buffer_map(b, &map, GST_MAP_READ);
		printf("picture pts=%" G_GUINT64_FORMAT " key=%d %.*s\n", GST_BUFFER_PTS(b),
		       !GST_BUFFER_FLAG_IS_SET(b, GST_BUFFER_FLAG_DELTA_UNIT), (int)map.size, map.data);
		gst_buffer_unmap(b, &map);
		gst_buffer_unref(b);
	}
}

/* A frame of the current caps: Y = k + x + y, U/V bytes = k + 2x + y (mod 256), in the layout of
 * a GstVideoInfo (lines of 4-byte multiples) */
static GstBuffer *frame(GstVideoInfo *info, int k, guint64 pts, gboolean whole)
{
	GstBuffer *b = gst_buffer_new_allocate(NULL, whole ? GST_VIDEO_INFO_SIZE(info) : 16, NULL);
	if (whole) {
		GstMapInfo map;
		gst_buffer_map(b, &map, GST_MAP_WRITE);
		for (int p = 0; p < 2; p++) {
			guint8 *plane = map.data + GST_VIDEO_INFO_PLANE_OFFSET(info, p);
			int stride = GST_VIDEO_INFO_PLANE_STRIDE(info, p);
			int rows = p ? GST_VIDEO_INFO_HEIGHT(info) / 2 : GST_VIDEO_INFO_HEIGHT(info);
			for (int y = 0; y < rows; y++)
				for (int x = 0; x < GST_VIDEO_INFO_WIDTH(info); x++)
					plane[y * stride + x] = p ? k + 2 * x + y : k + x + y;
		}
		gst_buffer_unmap(b, &map);
	}
	GST_BUFFER_PTS(b) = pts;
	GST_BUFFER_DURATION(b) = GST_SECOND / 30;
	return b;
}

int main(int argc, char **argv)
{
	gst_init(&argc, &argv);
	/* as GST_PLUGIN_DEFINE made it (the registry would load the .so with the same description) */
	gst_plugin_pliniusvenc_register();
	GstElementFactory *factory = gst_element_factory_find("pliniush264enc");
	printf("registered %d\n", factory != NULL);
	if (factory) {
		printf("rank %u\n", gst_plugin_feature_get_rank(GST_PLUGIN_FEATURE(factory)));
		gst_object_unref(factory);
	}
	gchar *script;
	g_file_get_contents(argv[1], &script, NULL, NULL);
	g_auto(GStrv) lines = g_strsplit(script, "\n", -1);
	g_free(script);

	GstHarness *h = NULL;
	GstBus *bus = gst_bus_new();
	GstVideoInfo info;
	gst_video_info_init(&info);
	guint64 pts = 0;
	for (char **l = lines; *l; l++) {
		char *line = *l;
		if (!*line)
			continue;
		if (g_str_has_prefix(line, "element")) {
			GstElement *element = gst_element_factory_make("pliniush264enc", NULL);
			g_auto(GStrv) props = g_strsplit(line, " ", -1);
			for (char **p = props + 1; element && *p; p++) {
				g_auto(GStrv) kv = g_strsplit(*p, "=", 2);
				gst_util_set_object_arg(G_OBJECT(element), kv[0], kv[1]);
			}
			printf("element %s\n", element ? "made" : "unknown");
			if (!element)
				break;
			guint bitrate, key_int_max;
			g_object_get(element, "bitrate", &bitrate, "key-int-max", &key_int_max, NULL);
			printf("properties bitrate=%u key-int-max=%u\n", bitrate, key_int_max);
			gst_element_set_bus(element, bus);
			h = gst_harness_new_with_element(element, "sink", "src");
			gst_object_unref(element);
		} else if (g_str_has_prefix(line, "caps ")) {
			GstCaps *caps = gst_caps_from_string(line + 5);
			gst_video_info_from_caps(&info, caps);
			gst_caps_unref(caps);
			gst_harness_set_src_caps_str(h, line + 5);
			pull(h);
		} else if (g_str_has_prefix(line, "push ")) {
			g_auto(GStrv) a = g_strsplit(line, " ", -1);
			if (a[2] && !strcmp(a[2], "key"))
				gst_harness_push_event(h, gst_video_event_new_downstream_force_key_unit(
								  GST_CLOCK_TIME_NONE, GST_CLOCK_TIME_NONE, GST_CLOCK_TIME_NONE,
								  FALSE, 0));
			GstFlowReturn r = gst_harness_push(h, frame(&info, atoi(a[1]), pts, TRUE));
			pts += GST_SECOND / 30;
			printf("push %s\n", gst_flow_get_name(r));
			pull(h);
		} else if (!strcmp(line, "pushshort")) {
			GstFlowReturn r = gst_harness_push(h, frame(&info, 0, pts, FALSE));
			printf("push %s\n", gst_flow_get_name(r));
		} else if (!strcmp(line, "eos")) {
			printf("eos %d\n", gst_harness_push_event(h, gst_event_new_eos()));
			pull(h);
		} else if (!strcmp(line, "flush")) {
			gst_harness_push_event(h, gst_event_new_flush_start());
			gst_harness_push_event(h, gst_event_new_flush_stop(TRUE));
			pull(h);
		} else if (!strcmp(line, "start")) {
			/* the element alone to PAUSED: what start says */
			GstElement *element = gst_element_factory_make("pliniush264enc", NULL);
			gst_element_set_bus(element, bus);
			printf("start %s\n", gst_element_state_change_return_get_name(
						      gst_element_set_state(element, GST_STATE_PAUSED)));
			gst_element_set_state(element, GST_STATE_NULL);
			gst_object_unref(element);
		} else if (!strcmp(line, "nodev")) {
			/* the encoder's sysfs entry goes */
			printf("nodev %d\n", rename("sys/class/video4linux/video3/name", "sys/class/video4linux/video3/gone"));
		} else if (!strcmp(line, "stop")) {
			gst_harness_teardown(h);
			h = NULL;
		}
		show_messages(bus);
	}
	if (h)
		gst_harness_teardown(h);
	show_messages(bus);
	printf("syncs %d\n", syncs);
	gst_object_unref(bus);
	return 0;
}
