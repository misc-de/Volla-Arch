/*
 * Harness for the GStreamer element of plinius-vdec (gstpliniusvdec.c, a copy with its paths made
 * relative: the test runs it in a directory of its own with sys/class/video4linux, dev/videoN and
 * dev/dma_heap/ as plain files). The kernel side is a fake:
 *
 *  - the MediaTek decoder (open/ioctl/poll on dev/videoN): a stateful V4L2 m2m decoder that
 *    reports SOURCE_CHANGE after the first data, "decodes" every queued access unit into a
 *    picture of a fake HyFBC layout (Y lines then UV lines, both with the coded width as pitch)
 *    whose bytes follow from the access unit's last byte k, can hold pictures back like a
 *    decoded picture buffer, and drains on V4L2_DEC_CMD_STOP with an empty LAST buffer;
 *  - the dma-heaps: DMA_HEAP_IOCTL_ALLOC gives a memfd of the size asked for;
 *  - the MDP (mdp.h, instead of mdp-client.c): copies the fake HyFBC picture as NV12 into the
 *    destination with the pitch asked for.
 * Everything the element asks for is logged to FAKE_LOG. Faults: FAKE_FAIL="op:n[:errno],..."
 * makes the n-th call of op fail (n 0: every call); the other FAKE_* knobs are read below.
 *
 * Commands (one per line, from the file in argv[1]):
 *   element NAME | meta | caps CAPS | push FILE | pushunmappable FILE | eos | flush | pull |
 *   badalloc | state STATE | pipeline | pool
 * Output: "push <flow>", "frame ..." per picture with the visible bytes appended to FRAMES_OUT,
 * "error <text> | <debug>" per bus error.
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
#include <time.h>
#include <unistd.h>

#include <linux/dma-buf.h>
#include <linux/dma-heap.h>
#include <linux/videodev2.h>

#include <gio/gio.h>
#include <gst/allocators/allocators.h>
#include <gst/app/gstappsink.h>
#include <gst/app/gstappsrc.h>
#include <gst/check/gstharness.h>
#include <gst/gst.h>
#include <gst/video/video.h>
#include <gst/video/gstvideodecoder.h>

#include "mdp.h"
#include "mdp-proto.h"

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

#define FOURCC_N12H v4l2_fourcc('N', '1', '2', 'H')

typedef struct {
	guint64 number;
	guint8 k;
} Picture;

static struct {
	gboolean cap_on, source_changed, draining, last_sent;
	int cap_fd[32];
	gsize cap_len[32];
	gboolean cap_queued[32];
	int n_cap;
	int done[64], n_done;
	Picture pics[64];
	int n_pics;
	int events[16], n_events;
	gboolean bogus_sent, empty_sent;
} dec;

/* ---- Files: which fd is what ---- */

enum { FD_NONE, FD_DEV, FD_HEAP, FD_HW_HEAP, FD_BUF };
static unsigned char kinds[4096];
static int syncs;

static int fake_open(const char *path, int flags, ...)
{
	int kind = g_str_has_prefix(path, "dev/video") ? FD_DEV :
		   !strcmp(path, "dev/dma_heap/mtk_mm") ? FD_HEAP :
		   !strcmp(path, "dev/dma_heap/mtk_mm-uncached") ? FD_HW_HEAP : FD_NONE;
	int e = kind == FD_DEV ? fails("open") : 0;
	if (e) {
		errno = e;
		return -1;
	}
	int fd = open(path, flags);
	if (fd >= 0 && fd < 4096)
		kinds[fd] = kind;
	if (fd >= 0 && kind == FD_DEV) {
		/* a decoder of its own for every open */
		memset(&dec, 0, sizeof dec);
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

static int fake_fcntl(int fd, int cmd, ...)
{
	va_list args;
	va_start(args, cmd);
	int arg = va_arg(args, int); /* the element only duplicates fds */
	va_end(args);
	int r = fcntl(fd, cmd, arg);
	if (cmd == F_DUPFD_CLOEXEC && r >= 0 && r < 4096 && fd >= 0 && fd < 4096)
		kinds[r] = kinds[fd];
	return r;
}

/* ---- The decoder ---- */

static guint coded_width(void) { return knob("FAKE_CODED", 80); }
static guint stream_height(void) { return knob("FAKE_H", 64); }

/* The fake HyFBC picture of access unit k (the MDP fake reads it back as NV12) */
static guint8 luma(guint8 k, guint x, guint y) { return (k * 7 + x + 3 * y) & 255; }
static guint8 chroma(guint8 k, guint i, guint y) { return (k * 13 + 5 * i + 11 * y) & 255; }

static void decode_into(int fd, gsize len, guint8 k)
{
	guint pitch = coded_width(), h = stream_height();
	if ((gsize)pitch * (h + h / 2) > len)
		say("decoder: picture buffer of %zu bytes too small", len);
	guint8 *line = g_malloc(pitch);
	for (guint y = 0; y < h + h / 2; y++) {
		for (guint x = 0; x < pitch; x++)
			line[x] = y < h ? luma(k, x, y) : chroma(k, x, y - h);
		if (pwrite(fd, line, pitch, (off_t)y * pitch) != (ssize_t)pitch)
			say("decoder: short write");
	}
	g_free(line);
}

static void push_event(int type)
{
	dec.events[dec.n_events++] = type;
}

static gboolean deliverable(void)
{
	return dec.cap_on && (dec.n_pics > knob("FAKE_DELAY", 0) || (dec.draining && dec.n_pics) ||
			      (dec.draining && !dec.last_sent && !getenv("FAKE_NO_LAST")) ||
			      (getenv("FAKE_BOGUS") && !dec.bogus_sent && dec.n_pics) ||
			      (getenv("FAKE_EMPTY") && !dec.empty_sent && dec.n_pics));
}

static int free_capture(void)
{
	for (int i = 0; i < dec.n_cap; i++)
		if (dec.cap_queued[i])
			return i;
	return -1;
}

static int dev_ioctl(unsigned long req, void *arg)
{
	int e;
	switch (req) {
	case VIDIOC_S_FMT: {
		struct v4l2_format *f = arg;
		if ((e = fails("s_fmt")))
			return errno = e, -1;
		say("s_fmt output %.4s", (char *)&f->fmt.pix_mp.pixelformat);
		if (getenv("FAKE_OTHER_FORMAT"))
			f->fmt.pix_mp.pixelformat = V4L2_PIX_FMT_MPEG2;
		f->fmt.pix_mp.plane_fmt[0].sizeimage = knob("FAKE_OUT_SIZEIMAGE", 1 << 20);
		return 0;
	}
	case VIDIOC_SUBSCRIBE_EVENT:
		say("subscribe %u", ((struct v4l2_event_subscription *)arg)->type);
		return 0;
	case VIDIOC_REQBUFS: {
		struct v4l2_requestbuffers *r = arg;
		gboolean cap = r->type == V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
		if (r->memory != V4L2_MEMORY_DMABUF)
			return errno = EINVAL, -1;
		if (r->count && (e = fails(cap ? "reqbufs_cap" : "reqbufs_out")))
			return errno = e, -1;
		guint asked = r->count;
		if (cap && r->count)
			r->count = knob("FAKE_CAP_COUNT", r->count);
		say("reqbufs %s %u -> %u", cap ? "capture" : "output", asked, r->count);
		if (cap) {
			dec.n_cap = r->count;
			memset(dec.cap_queued, 0, sizeof dec.cap_queued);
		}
		return 0;
	}
	case VIDIOC_STREAMON:
	case VIDIOC_STREAMOFF: {
		gboolean on = req == VIDIOC_STREAMON;
		gboolean cap = *(int *)arg == V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
		if (on && (e = fails(cap ? "streamon_cap" : "streamon_out")))
			return errno = e, -1;
		say("stream%s %s", on ? "on" : "off", cap ? "capture" : "output");
		if (cap)
			dec.cap_on = on;
		return 0;
	}
	case VIDIOC_QBUF: {
		struct v4l2_buffer *b = arg;
		struct v4l2_plane *p = b->m.planes;
		if (b->memory != V4L2_MEMORY_DMABUF || b->length != 1 || b->reserved2 != (guint32)-1)
			return errno = EINVAL, -1;
		if (b->type == V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE) {
			if ((e = fails("qbuf_cap")))
				return errno = e, -1;
			if (b->index >= (guint)dec.n_cap)
				return errno = EINVAL, -1;
			dec.cap_fd[b->index] = p->m.fd;
			dec.cap_len[b->index] = p->length;
			dec.cap_queued[b->index] = TRUE;
			return 0;
		}
		if ((e = fails("qbuf_out")))
			return errno = e, -1;
		guint8 *data = g_malloc(p->bytesused + 1);
		ssize_t n = pread(p->m.fd, data, p->bytesused, 0);
		g_autoptr(GString) hex = g_string_new(NULL);
		for (ssize_t i = 0; i < n && i < 512; i++)
			g_string_append_printf(hex, "%02x", data[i]);
		say("decode %lu %u %s", (unsigned long)b->timestamp.tv_sec, p->bytesused, hex->str);
		if (p->bytesused && !getenv("FAKE_NO_SOURCE_CHANGE")) {
			if (!dec.source_changed)
				push_event(V4L2_EVENT_SOURCE_CHANGE);
			dec.source_changed = TRUE;
			dec.pics[dec.n_pics++] = (Picture){b->timestamp.tv_sec, data[p->bytesused - 1]};
		}
		g_free(data);
		if (!getenv("FAKE_HOLD_OUTPUT"))
			dec.done[dec.n_done++] = b->index;
		return 0;
	}
	case VIDIOC_DQBUF: {
		struct v4l2_buffer *b = arg;
		if (b->type == V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE) {
			if ((e = fails("dqbuf_out")))
				return errno = e, -1;
			if (!dec.n_done)
				return errno = EAGAIN, -1;
			b->index = getenv("FAKE_BAD_INDEX") ? 99 : dec.done[0];
			memmove(dec.done, dec.done + 1, --dec.n_done * sizeof(int));
			return 0;
		}
		if ((e = fails("dqbuf_cap")))
			return errno = e, -1;
		if (!dec.cap_on)
			return errno = EINVAL, -1;
		if (dec.last_sent)
			return errno = EPIPE, -1;
		int i = free_capture();
		if (i < 0 || !deliverable())
			return errno = EAGAIN, -1;
		dec.cap_queued[i] = FALSE;
		b->index = i;
		b->flags = 0;
		b->m.planes[0].bytesused = 0;
		if (getenv("FAKE_EMPTY") && !dec.empty_sent) {
			/* a buffer the decoder gives back without a picture */
			dec.empty_sent = TRUE;
		} else if (getenv("FAKE_BOGUS") && !dec.bogus_sent) {
			/* a picture of a frame the element does not know */
			dec.bogus_sent = TRUE;
			decode_into(dec.cap_fd[i], dec.cap_len[i], 0);
			b->m.planes[0].bytesused = dec.cap_len[i];
			b->timestamp.tv_sec = 4242;
		} else if (dec.n_pics) {
			Picture pic = dec.pics[0];
			memmove(dec.pics, dec.pics + 1, --dec.n_pics * sizeof(Picture));
			decode_into(dec.cap_fd[i], dec.cap_len[i], pic.k);
			b->m.planes[0].bytesused = dec.cap_len[i];
			b->timestamp.tv_sec = pic.number;
		} else {
			b->flags = V4L2_BUF_FLAG_LAST;
			dec.last_sent = TRUE;
		}
		return 0;
	}
	case VIDIOC_DQEVENT: {
		struct v4l2_event *ev = arg;
		if (!dec.n_events)
			return errno = ENOENT, -1;
		memset(ev, 0, sizeof *ev);
		ev->type = dec.events[0];
		memmove(dec.events, dec.events + 1, --dec.n_events * sizeof(int));
		return 0;
	}
	case VIDIOC_G_FMT: {
		struct v4l2_format *f = arg;
		if ((e = fails("g_fmt")))
			return errno = e, -1;
		f->fmt.pix_mp.pixelformat = getenv("FAKE_FOURCC") ? V4L2_PIX_FMT_P010 : FOURCC_N12H;
		f->fmt.pix_mp.width = coded_width();
		f->fmt.pix_mp.height = stream_height();
		f->fmt.pix_mp.plane_fmt[0].sizeimage = knob("FAKE_SIZEIMAGE", 0);
		return 0;
	}
	case VIDIOC_G_SELECTION: {
		struct v4l2_selection *s = arg;
		const char *crop = getenv("FAKE_CROP");
		if ((crop && !strcmp(crop, "none")) || s->target != V4L2_SEL_TGT_COMPOSE)
			return errno = EINVAL, -1;
		if (crop)
			sscanf(crop, "%ux%u", &s->r.width, &s->r.height);
		else
			s->r.width = coded_width(), s->r.height = stream_height();
		return 0;
	}
	case VIDIOC_G_CTRL: {
		struct v4l2_control *c = arg;
		if ((e = fails("g_ctrl")) || c->id != V4L2_CID_MIN_BUFFERS_FOR_CAPTURE)
			return errno = e ? e : EINVAL, -1;
		c->value = knob("FAKE_MIN_BUFFERS", 6);
		return 0;
	}
	case VIDIOC_DECODER_CMD: {
		struct v4l2_decoder_cmd *c = arg;
		if ((e = fails("decoder_cmd")))
			return errno = e, -1;
		say("decoder cmd %u", c->cmd);
		dec.draining = TRUE;
		push_event(V4L2_EVENT_EOS);
		return 0;
	}
	}
	say("unknown ioctl 0x%lx", req);
	return errno = ENOTTY, -1;
}

static int fake_ioctl(int fd, unsigned long req, ...)
{
	va_list args;
	va_start(args, req);
	void *arg = va_arg(args, void *);
	va_end(args);
	int kind = fd >= 0 && fd < 4096 ? kinds[fd] : FD_NONE;
	int e;
	if (kind == FD_DEV) {
		if ((e = fails("eintr")))
			return errno = EINTR, -1;
		return dev_ioctl(req, arg);
	}
	if ((kind == FD_HEAP || kind == FD_HW_HEAP) && req == DMA_HEAP_IOCTL_ALLOC) {
		struct dma_heap_allocation_data *a = arg;
		if ((e = fails("alloc")))
			return errno = e, -1;
		if (fails("unmappable")) {
			/* a buffer that cannot be mapped */
			int p[2];
			if (pipe2(p, O_CLOEXEC))
				return -1;
			close(p[1]);
			a->fd = p[0];
		} else {
			a->fd = memfd_create("dmabuf", MFD_CLOEXEC);
			if (ftruncate(a->fd, a->len))
				return -1;
		}
		kinds[a->fd] = FD_BUF;
		say("alloc %s %llu", kind == FD_HEAP ? "heap" : "hw-heap", (unsigned long long)a->len);
		return 0;
	}
	if (req == DMA_BUF_IOCTL_SYNC) {
		syncs++;
		return 0;
	}
	say("ioctl 0x%lx on fd of kind %d", req, kind);
	return errno = ENOTTY, -1;
}

static int fake_poll(struct pollfd *fds, nfds_t n, int timeout)
{
	int e;
	if ((e = fails("poll")))
		return errno = e, -1;
	struct pollfd *p = &fds[0];
	p->revents = 0;
	if (kinds[p->fd] != FD_DEV)
		return poll(fds, n, timeout);
	if (!dec.cap_on)
		p->revents |= POLLERR;
	if (dec.n_events)
		p->revents |= POLLPRI;
	if (dec.n_done)
		p->revents |= POLLOUT;
	if (free_capture() >= 0 && deliverable())
		p->revents |= POLLIN;
	if (!(p->revents & ~POLLERR) && timeout > 0)
		g_usleep(MIN(timeout, 20) * 1000);
	return p->revents ? 1 : 0;
}

/* ---- The MDP ---- */

struct PvMdp {
	int unused;
};

PvMdp *pv_mdp_new(GError **error)
{
	if (fails("mdp_new")) {
		g_set_error(error, G_IO_ERROR, G_IO_ERROR_FAILED, "no MDP service here");
		return NULL;
	}
	say("mdp new");
	return g_new0(PvMdp, 1);
}

void pv_mdp_free(PvMdp *mdp)
{
	if (mdp)
		say("mdp free");
	g_free(mdp);
}

guint pv_mdp_hyfbc_src_size(guint coded, guint height)
{
	return (guint)pv_mdp_src_bytes(coded, height);
}

gboolean pv_mdp_hyfbc_to_nv12(PvMdp *mdp, int src_fd, guint src_size, guint coded, guint width, guint height,
			      int dst_fd, guint dst_pitch, GError **error)
{
	struct stat src_st, dst_st;
	fstat(src_fd, &src_st);
	fstat(dst_fd, &dst_st);
	say("mdp %u (buffer %lld) coded %u %ux%u pitch %u into %lld bytes", src_size, (long long)src_st.st_size, coded,
	    width, height, dst_pitch, (long long)dst_st.st_size);
	if (fails("mdp")) {
		g_set_error(error, G_IO_ERROR, G_IO_ERROR_FAILED, "MDP said no");
		return FALSE;
	}
	/* As the real MDP (mdp.c): whole decoded lines, so the destination's lines hold them */
	if (dst_pitch < coded) {
		g_set_error(error, G_IO_ERROR, G_IO_ERROR_INVALID_ARGUMENT, "pitch %u < coded width %u", dst_pitch, coded);
		return FALSE;
	}
	guint h = stream_height();
	guint8 *line = g_malloc(coded);
	for (guint y = 0; y < height + height / 2; y++) {
		/* Y lines, then UV lines, of the fake HyFBC picture */
		off_t src_off = (off_t)(y < height ? y : h + y - height) * coded;
		if (pread(src_fd, line, coded, src_off) != (ssize_t)coded ||
		    pwrite(dst_fd, line, coded, (off_t)y * dst_pitch) != (ssize_t)coded)
			say("mdp: short read or write");
	}
	g_free(line);
	return TRUE;
}

#define open fake_open
#define close fake_close
#define fcntl fake_fcntl
#define ioctl fake_ioctl
#define poll fake_poll
#include "gstpliniusvdec.c"
#undef open
#undef close
#undef fcntl
#undef ioctl
#undef poll

/* ---- Memory that cannot be mapped (an allocator whose map fails) ---- */

typedef struct {
	GstAllocator parent;
} BadAllocator;
typedef struct {
	GstAllocatorClass parent_class;
} BadAllocatorClass;
static GType bad_allocator_get_type(void);
G_DEFINE_TYPE(BadAllocator, bad_allocator, GST_TYPE_ALLOCATOR)

static GstMemory *bad_alloc(GstAllocator *allocator, gsize size, GstAllocationParams *params)
{
	GstMemory *mem = g_new0(GstMemory, 1);
	gst_memory_init(mem, 0, allocator, NULL, size, 0, 0, size);
	return mem;
}

static void bad_free(GstAllocator *allocator, GstMemory *mem)
{
	g_free(mem);
}

static gpointer bad_map(GstMemory *mem, gsize maxsize, GstMapFlags flags)
{
	return NULL;
}

static void bad_unmap(GstMemory *mem)
{
}

static void bad_allocator_class_init(BadAllocatorClass *klass)
{
	GST_ALLOCATOR_CLASS(klass)->alloc = bad_alloc;
	GST_ALLOCATOR_CLASS(klass)->free = bad_free;
}

static void bad_allocator_init(BadAllocator *a)
{
	GstAllocator *alloc = GST_ALLOCATOR(a);
	alloc->mem_type = "unmappable";
	alloc->mem_map = bad_map;
	alloc->mem_unmap = bad_unmap;
}

/* ---- A downstream pool that takes no configuration ---- */

typedef struct {
	GstBufferPool parent;
} RefusingPool;
typedef struct {
	GstBufferPoolClass parent_class;
} RefusingPoolClass;
static GType refusing_pool_get_type(void);
G_DEFINE_TYPE(RefusingPool, refusing_pool, GST_TYPE_BUFFER_POOL)

static gboolean refusing_set_config(GstBufferPool *pool, GstStructure *config)
{
	return FALSE;
}

static void refusing_pool_class_init(RefusingPoolClass *klass)
{
	GST_BUFFER_POOL_CLASS(klass)->set_config = refusing_set_config;
}

static void refusing_pool_init(RefusingPool *p)
{
}

/* Downstream proposes a pool: none (only its minimum) or one that refuses every configuration */
static guint proposed_min;
static gboolean proposed_refusing;

static GstPadProbeReturn propose_pool(GstPad *pad, GstPadProbeInfo *info, gpointer data)
{
	GstQuery *q = GST_PAD_PROBE_INFO_QUERY(info);
	if (GST_QUERY_TYPE(q) == GST_QUERY_ALLOCATION) {
		GstBufferPool *pool = proposed_refusing ? g_object_new(refusing_pool_get_type(), NULL) : NULL;
		gst_query_add_allocation_pool(q, pool, 0, proposed_min, 0);
		if (pool)
			gst_object_unref(pool);
	}
	return GST_PAD_PROBE_OK;
}

/* ---- The driver ---- */

static FILE *frames_out;

static void show_messages(GstBus *bus)
{
	GstMessage *m;
	while (bus && (m = gst_bus_pop(bus))) {
		if (GST_MESSAGE_TYPE(m) == GST_MESSAGE_ERROR || GST_MESSAGE_TYPE(m) == GST_MESSAGE_WARNING) {
			g_autoptr(GError) err = NULL;
			g_autofree char *debug = NULL;
			if (GST_MESSAGE_TYPE(m) == GST_MESSAGE_ERROR)
				gst_message_parse_error(m, &err, &debug);
			else
				gst_message_parse_warning(m, &err, &debug);
			/* the debug text after the location line, if any */
			const char *nl = debug ? strchr(debug, '\n') : NULL;
			printf("%s %s | %s\n", GST_MESSAGE_TYPE(m) == GST_MESSAGE_ERROR ? "error" : "warning", err->message,
			       nl ? nl + 1 : "");
		}
		gst_message_unref(m);
	}
}

static void dump(GstBuffer *buf, GstCaps *caps)
{
	GstVideoInfo info;
	GstVideoFrame vf;
	gst_video_info_from_caps(&info, caps);
	GstVideoMeta *meta = gst_buffer_get_video_meta(buf);
	GstMemory *mem = gst_buffer_peek_memory(buf, 0);
	if (!gst_video_frame_map(&vf, &info, buf, GST_MAP_READ)) {
		printf("frame unmappable\n");
		return;
	}
	guint w = GST_VIDEO_INFO_WIDTH(&info), h = GST_VIDEO_INFO_HEIGHT(&info);
	printf("frame pts=%" G_GUINT64_FORMAT " %ux%u stride %d/%d dmabuf=%d meta=%d\n",
	       GST_BUFFER_PTS(buf) / GST_MSECOND, w, h, GST_VIDEO_FRAME_PLANE_STRIDE(&vf, 0),
	       GST_VIDEO_FRAME_PLANE_STRIDE(&vf, 1), gst_is_dmabuf_memory(mem), meta != NULL);
	for (guint plane = 0; plane < 2; plane++)
		for (guint y = 0; y < (plane ? h / 2 : h); y++)
			fwrite((guint8 *)GST_VIDEO_FRAME_PLANE_DATA(&vf, plane) + y * GST_VIDEO_FRAME_PLANE_STRIDE(&vf, plane),
			       1, w, frames_out);
	fflush(frames_out);
	gst_video_frame_unmap(&vf);
}

static GstBuffer *read_buffer(const char *path, gboolean mappable, int n)
{
	gchar *data;
	gsize size;
	if (!g_file_get_contents(path, &data, &size, NULL))
		g_error("cannot read %s", path);
	GstBuffer *buf = gst_buffer_new();
	if (mappable) {
		gst_buffer_append_memory(buf, gst_memory_new_wrapped(0, data, size, 0, size, data, g_free));
	} else {
		GstAllocator *a = g_object_new(bad_allocator_get_type(), NULL);
		gst_buffer_append_memory(buf, gst_allocator_alloc(a, size, NULL));
		gst_object_unref(a);
		g_free(data);
	}
	GST_BUFFER_PTS(buf) = n * 40 * GST_MSECOND;
	GST_BUFFER_DURATION(buf) = 40 * GST_MSECOND;
	return buf;
}

static void pull(GstHarness *h)
{
	GstBuffer *out;
	while ((out = gst_harness_try_pull(h))) {
		GstCaps *caps = gst_pad_get_current_caps(h->sinkpad);
		dump(out, caps);
		gst_caps_unref(caps);
		gst_buffer_unref(out);
	}
}

static void segment(GstHarness *h)
{
	GstSegment seg;
	gst_segment_init(&seg, GST_FORMAT_TIME);
	gst_harness_push_event(h, gst_event_new_segment(&seg));
}

/* appsrc ! ELEMENT ! appsink with the access units of the script (only "push" and "eos") */
static void run_pipeline(const char *element, const char *caps, char **lines)
{
	g_autofree char *desc = g_strdup_printf("appsrc name=src format=time caps=\"%s\" ! %s ! appsink name=sink sync=false",
						caps, element);
	g_autoptr(GError) error = NULL;
	GstElement *pipe = gst_parse_launch(desc, &error);
	if (!pipe) {
		printf("pipeline error %s\n", error->message);
		return;
	}
	GstElement *src = gst_bin_get_by_name(GST_BIN(pipe), "src"), *sink = gst_bin_get_by_name(GST_BIN(pipe), "sink");
	gst_element_set_state(pipe, GST_STATE_PLAYING);
	int n = 0;
	for (char **l = lines; *l; l++)
		if (g_str_has_prefix(*l, "push "))
			gst_app_src_push_buffer(GST_APP_SRC(src), read_buffer(*l + 5, TRUE, n++));
	gst_app_src_end_of_stream(GST_APP_SRC(src));
	GstSample *s;
	while ((s = gst_app_sink_try_pull_sample(GST_APP_SINK(sink), 10 * GST_SECOND))) {
		dump(gst_sample_get_buffer(s), gst_sample_get_caps(s));
		gst_sample_unref(s);
	}
	printf("eos %d\n", gst_app_sink_is_eos(GST_APP_SINK(sink)));
	GstBus *bus = gst_element_get_bus(pipe);
	show_messages(bus);
	gst_object_unref(bus);
	gst_element_set_state(pipe, GST_STATE_NULL);
	gst_object_unref(src);
	gst_object_unref(sink);
	gst_object_unref(pipe);
}

/* The pool on its own: only NV12, lines padded to 16 bytes */
static void pool_checks(void)
{
	int heap = fake_open("dev/dma_heap/mtk_mm", O_RDONLY | O_CLOEXEC);
	GstBufferPool *pool = pv_pool_new(heap, 0);
	fake_close(heap);
	const char *formats[] = {"I420", "NV12"};
	for (int i = 0; i < 2; i++) {
		GstStructure *config = gst_buffer_pool_get_config(pool);
		GstCaps *caps = gst_caps_new_simple("video/x-raw", "format", G_TYPE_STRING, formats[i], "width", G_TYPE_INT, 100,
						    "height", G_TYPE_INT, 50, NULL);
		gst_buffer_pool_config_set_params(config, caps, 0, 1, 2);
		gboolean ok = gst_buffer_pool_set_config(pool, config);
		guint size = 0;
		config = gst_buffer_pool_get_config(pool);
		gst_buffer_pool_config_get_params(config, NULL, &size, NULL, NULL);
		gst_structure_free(config);
		printf("pool %s accepted=%d size=%u\n", formats[i], ok, size);
		gst_caps_unref(caps);
	}
	printf("pool video meta %d\n", gst_buffer_pool_has_option(pool, GST_BUFFER_POOL_OPTION_VIDEO_META));
	gst_buffer_pool_set_active(pool, TRUE);
	GstBuffer *buf = NULL;
	gst_buffer_pool_acquire_buffer(pool, &buf, NULL);
	GstVideoMeta *meta = gst_buffer_get_video_meta(buf);
	printf("pool buffer %zu bytes, dmabuf %d, offsets %zu/%zu strides %d/%d\n", gst_buffer_get_size(buf),
	       gst_is_dmabuf_memory(gst_buffer_peek_memory(buf, 0)), meta->offset[0], meta->offset[1], meta->stride[0],
	       meta->stride[1]);
	gst_buffer_unref(buf);
	gst_buffer_pool_set_active(pool, FALSE);
	gst_object_unref(pool);
	/* decoded lines of 120 bytes for a picture 100 wide: the pool's lines hold them */
	heap = fake_open("dev/dma_heap/mtk_mm", O_RDONLY | O_CLOEXEC);
	pool = pv_pool_new(heap, 120);
	fake_close(heap);
	GstStructure *config = gst_buffer_pool_get_config(pool);
	GstCaps *caps = gst_caps_new_simple("video/x-raw", "format", G_TYPE_STRING, "NV12", "width", G_TYPE_INT, 100,
					    "height", G_TYPE_INT, 50, NULL);
	gst_buffer_pool_config_set_params(config, caps, 0, 1, 2);
	gst_buffer_pool_set_config(pool, config);
	gst_caps_unref(caps);
	gst_buffer_pool_set_active(pool, TRUE);
	gst_buffer_pool_acquire_buffer(pool, &buf, NULL);
	meta = gst_buffer_get_video_meta(buf);
	printf("pool wide lines %zu bytes, %ux%u strides %d/%d\n", gst_buffer_get_size(buf), meta->width, meta->height,
	       meta->stride[0], meta->stride[1]);
	gst_buffer_unref(buf);
	gst_buffer_pool_set_active(pool, FALSE);
	gst_object_unref(pool);
}

int main(int argc, char **argv)
{
	gst_init(&argc, &argv);
	/* as GST_PLUGIN_DEFINE made it (the registry would load the .so with the same description) */
	gst_plugin_pliniusvdec_register();
	frames_out = fopen(getenv("FRAMES_OUT") ? getenv("FRAMES_OUT") : "/dev/null", "wb");
	gchar *script;
	g_file_get_contents(argv[1], &script, NULL, NULL);
	g_auto(GStrv) lines = g_strsplit(script, "\n", -1);
	g_free(script);

	GstHarness *h = NULL;
	GstElement *element = NULL;
	GstBus *bus = gst_bus_new();
	int n = 0;
	for (char **l = lines; *l; l++) {
		char *line = *l;
		if (!*line)
			continue;
		if (g_str_has_prefix(line, "element ")) {
			element = gst_element_factory_make(line + 8, NULL);
			printf("element %s\n", element ? "made" : "unknown");
			if (!element)
				break;
			gst_element_set_bus(element, bus);
		} else if (!strcmp(line, "factories")) {
			const GstPluginDesc *d = gst_plugin_pliniusvdec_get_desc();
			printf("plugin %s %s %s %s\n", d->name, d->version, d->license, d->source);
			for (const char *const *f = (const char *const[]){"pliniush264dec", "pliniush265dec", "pliniusvp9dec", NULL};
			     *f; f++) {
				GstElementFactory *fac = gst_element_factory_find(*f);
				printf("factory %s %s", *f, fac ? "" : "missing\n");
				if (!fac)
					continue;
				printf("rank %u klass %s\n", gst_plugin_feature_get_rank(GST_PLUGIN_FEATURE(fac)),
				       gst_element_factory_get_metadata(fac, GST_ELEMENT_METADATA_KLASS));
				for (const GList *t = gst_element_factory_get_static_pad_templates(fac); t; t = t->next) {
					GstStaticPadTemplate *tpl = t->data;
					printf("template %s %s\n", tpl->name_template, tpl->static_caps.string);
				}
				gst_object_unref(fac);
			}
		} else if (g_str_has_prefix(line, "state ")) {
			/* Without a harness: set_state's own answer */
			GstState s = !strcmp(line + 6, "null") ? GST_STATE_NULL : GST_STATE_PAUSED;
			printf("state %s\n", gst_element_state_change_return_get_name(gst_element_set_state(element, s)));
		} else if (!strcmp(line, "harness")) {
			h = gst_harness_new_with_element(element, "sink", "src");
		} else if (g_str_has_prefix(line, "proposepool ")) {
			proposed_min = atoi(line + 12);
			proposed_refusing = strstr(line, "refusing") != NULL;
			gst_pad_add_probe(h->sinkpad, GST_PAD_PROBE_TYPE_QUERY_DOWNSTREAM, propose_pool, NULL, NULL);
		} else if (!strcmp(line, "meta")) {
			gst_harness_add_propose_allocation_meta(h, GST_VIDEO_META_API_TYPE, NULL);
		} else if (!strcmp(line, "badalloc")) {
			GstAllocator *a = g_object_new(bad_allocator_get_type(), NULL);
			gst_harness_set_propose_allocator(h, a, NULL);
		} else if (g_str_has_prefix(line, "caps ")) {
			gst_harness_set_src_caps_str(h, line + 5);
			/* pictures drained by the change, with the caps they were made for */
			pull(h);
		} else if (g_str_has_prefix(line, "push ") || g_str_has_prefix(line, "pushunmappable ")) {
			gboolean mappable = g_str_has_prefix(line, "push ");
			GstFlowReturn r = gst_harness_push(h, read_buffer(strchr(line, ' ') + 1, mappable, n++));
			printf("push %s\n", gst_flow_get_name(r));
			pull(h);
		} else if (!strcmp(line, "eos")) {
			gint64 t0 = g_get_monotonic_time();
			gboolean ok = gst_harness_push_event(h, gst_event_new_eos());
			printf("eos %d after %.1f s\n", ok, (g_get_monotonic_time() - t0) / 1e6);
			pull(h);
		} else if (!strcmp(line, "flush")) {
			gst_harness_push_event(h, gst_event_new_flush_start());
			gst_harness_push_event(h, gst_event_new_flush_stop(TRUE));
			segment(h);
			printf("flushed\n");
		} else if (!strcmp(line, "pool")) {
			pool_checks();
		} else if (g_str_has_prefix(line, "pipeline ")) {
			char **parts = g_strsplit(line + 9, " ", 2);
			run_pipeline(parts[0], parts[1], l + 1);
			g_strfreev(parts);
			break;
		}
		show_messages(bus);
	}
	if (h)
		gst_harness_teardown(h);
	else if (element) {
		gst_element_set_state(element, GST_STATE_NULL);
		gst_object_unref(element);
	}
	show_messages(bus);
	gst_object_unref(bus);
	printf("syncs %d\n", syncs);
	fclose(frames_out);
	return 0;
}
