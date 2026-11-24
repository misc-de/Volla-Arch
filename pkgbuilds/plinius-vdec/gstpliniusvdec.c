/* SPDX-License-Identifier: MIT */
/*
 * Hardware video decoding on the Volla Phone Plinius (MediaTek Dimensity 7300).
 *
 * The vendor vcodec driver (mtk_vcodec_dec_v1) is a stateful V4L2 memory-to-memory decoder,
 * but GStreamer's v4l2 elements cannot use it:
 *  - it takes the bitstream address only from a dma-buf (vb2_plane.dbuf), so OUTPUT buffers
 *    must be V4L2_MEMORY_DMABUF;
 *  - for every CAPTURE buffer it reads the fd of a metadata dma-buf from v4l2_buffer.reserved2,
 *    -1 means none;
 *  - the firmware always writes MediaTek's compressed HyFBC format (N12H), whatever S_FMT asks
 *    for, so the frames have to go through the MDP to become NV12: plinius-vdec-mdpd does that
 *    (mdp-proto.h), the only one that may open the MDP.
 * Measured 2026-11-10: 720p H.264 decodes at about 360 fps, the MDP converts a 720p frame in
 * about 3 ms, and the result is bit-exact against FFmpeg's software decoder.
 *
 * One frame is one access unit (h264parse/h265parse with alignment=au). The decoder works in
 * the streaming thread: queueing input also collects the decoded pictures.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

#include <linux/dma-buf.h>
#include <linux/dma-heap.h>
#include <linux/videodev2.h>

#include <gio/gio.h>
#include <gst/allocators/allocators.h>
#include <gst/gst.h>
#include <gst/video/video.h>
#include <gst/video/gstvideodecoder.h>

#include "mdp.h"

#define PACKAGE "plinius-vdec"

GST_DEBUG_CATEGORY_STATIC(pv_debug);
#define GST_CAT_DEFAULT pv_debug

#define DEVICE_NAME     "mtk-vcodec-dec"
#define HEAP_PATH       "/dev/dma_heap/mtk_mm"
/* for buffers only the hardware touches: no cache maintenance when the MDP maps them */
#define HW_HEAP_PATH    "/dev/dma_heap/mtk_mm-uncached"
#define N_OUTPUT        8
#define OUTPUT_SIZE     (4 * 1024 * 1024)
#define EXTRA_CAPTURE   4
#define MAX_BUFFERS     32
#define DRAIN_TIMEOUT   3000 /* ms */
#define V4L2_PIX_FMT_N12H v4l2_fourcc('N', '1', '2', 'H')
/* the vendor driver names HEVC 'H265', not V4L2_PIX_FMT_HEVC */
#define V4L2_PIX_FMT_MTK_H265 v4l2_fourcc('H', '2', '6', '5')

typedef enum { CODEC_H264, CODEC_H265, CODEC_VP9 } Codec;

typedef struct {
	const char *element;
	const char *long_name;
	Codec codec;
	guint32 fourcc;
	const char *caps;
} CodecInfo;

static const CodecInfo codecs[] = {
	{ "pliniush264dec", "Plinius hardware H.264 decoder", CODEC_H264, V4L2_PIX_FMT_H264,
	  "video/x-h264, stream-format=byte-stream, alignment=au, "
	  "profile=(string){ constrained-baseline, baseline, main, high }, "
	  "width=(int)[16,4096], height=(int)[16,2176]" },
	{ "pliniush265dec", "Plinius hardware H.265 decoder", CODEC_H265, V4L2_PIX_FMT_MTK_H265,
	  "video/x-h265, stream-format=byte-stream, alignment=au, "
	  "profile=(string){ main, main-still-picture }, "
	  "width=(int)[16,4096], height=(int)[16,2176]" },
	{ "pliniusvp9dec", "Plinius hardware VP9 decoder", CODEC_VP9, V4L2_PIX_FMT_VP9,
	  "video/x-vp9, profile=(string)0, width=(int)[16,4096], height=(int)[16,2176]" },
};

typedef struct {
	int fd;
	void *map;
	gsize size;
	gboolean queued;
} PvBuffer;

typedef struct {
	GstVideoDecoder parent;

	GstVideoCodecState *input_state;
	int fd;
	int heap, hw_heap;
	PvBuffer out[N_OUTPUT];
	PvBuffer cap[MAX_BUFFERS];
	guint n_cap;
	gboolean out_on, cap_on, eos, error;
	guint coded_width, width, height;

	PvMdp *mdp;
	PvBuffer nv12; /* MDP target when downstream buffers are not ours */
	guint nv12_pitch;
	gboolean zero_copy;

	/* thread CPU time per step, in ns, for the debug log */
	gint64 cpu_mdp, cpu_copy, cpu_total;
	guint n_pictures;

	/* H.264/H.265 parameter sets, sent again after the device was reopened */
	GByteArray *headers;
	gboolean need_headers;
} GstPliniusVdec;

typedef struct {
	GstVideoDecoderClass parent_class;
	const CodecInfo *info;
} GstPliniusVdecClass;

#define PV(obj) ((GstPliniusVdec *)(obj))
#define PV_GET_CLASS(obj) ((GstPliniusVdecClass *)G_OBJECT_GET_CLASS(obj))

static GstElementClass *parent_class;

static int xioctl(int fd, unsigned long req, void *arg)
{
	int r;
	do
		r = ioctl(fd, req, arg);
	while (r < 0 && errno == EINTR);
	return r;
}

static gint64 thread_cpu_ns(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_THREAD_CPUTIME_ID, &ts);
	return ts.tv_sec * G_GINT64_CONSTANT(1000000000) + ts.tv_nsec;
}

/*
 * Output buffers as dma-bufs from the heap, so that the MDP writes the NV12 picture straight
 * into the buffer that goes downstream (saves a copy of about 2 ms per 1080p picture).
 * Lines are as long as the decoded ones (the MDP converts whole lines, see mdp.c), padded to 16
 * bytes; the video meta tells downstream.
 */
typedef struct {
	GstBufferPool parent;
	int heap;
	GstAllocator *allocator;
	GstVideoInfo info;
	guint min_pitch;  /* the decoded lines' width */
	guint pitch;
	gsize size;       /* one NV12 picture */
	gsize alloc_size; /* dma-buf, page aligned */
} PvPool;

typedef struct {
	GstBufferPoolClass parent_class;
} PvPoolClass;

static GType pv_pool_get_type(void);
G_DEFINE_TYPE(PvPool, pv_pool, GST_TYPE_BUFFER_POOL)

static const gchar **pv_pool_get_options(GstBufferPool *pool)
{
	static const gchar *options[] = { GST_BUFFER_POOL_OPTION_VIDEO_META, NULL };
	return options;
}

static gboolean pv_pool_set_config(GstBufferPool *bpool, GstStructure *config)
{
	PvPool *pool = (PvPool *)bpool;
	GstCaps *caps;
	guint size, min, max;

	if (!gst_buffer_pool_config_get_params(config, &caps, &size, &min, &max) || !caps ||
	    !gst_video_info_from_caps(&pool->info, caps) ||
	    GST_VIDEO_INFO_FORMAT(&pool->info) != GST_VIDEO_FORMAT_NV12)
		return FALSE;
	pool->pitch = GST_ROUND_UP_16(MAX(pool->min_pitch, (guint)GST_VIDEO_INFO_WIDTH(&pool->info)));
	pool->size = (gsize)pool->pitch * GST_VIDEO_INFO_HEIGHT(&pool->info) * 3 / 2;
	pool->alloc_size = GST_ROUND_UP_N(pool->size, 4096);
	gst_buffer_pool_config_set_params(config, caps, pool->size, min, max);
	return GST_BUFFER_POOL_CLASS(pv_pool_parent_class)->set_config(bpool, config);
}

static GstFlowReturn pv_pool_alloc_buffer(GstBufferPool *bpool, GstBuffer **buffer,
	GstBufferPoolAcquireParams *params)
{
	PvPool *pool = (PvPool *)bpool;
	struct dma_heap_allocation_data a = { .len = pool->alloc_size, .fd_flags = O_RDWR | O_CLOEXEC };
	guint width = GST_VIDEO_INFO_WIDTH(&pool->info), height = GST_VIDEO_INFO_HEIGHT(&pool->info);

	if (xioctl(pool->heap, DMA_HEAP_IOCTL_ALLOC, &a) < 0) {
		GST_ERROR_OBJECT(pool, "allocating %zu bytes: %s", pool->alloc_size, g_strerror(errno));
		return GST_FLOW_ERROR;
	}
	GstMemory *mem = gst_dmabuf_allocator_alloc(pool->allocator, a.fd, pool->alloc_size);
	if (!mem) {
		close(a.fd);
		return GST_FLOW_ERROR;
	}
	gst_memory_resize(mem, 0, pool->size);
	GstBuffer *buf = gst_buffer_new();
	gst_buffer_append_memory(buf, mem);
	gsize offsets[GST_VIDEO_MAX_PLANES] = { 0, (gsize)pool->pitch * height };
	gint strides[GST_VIDEO_MAX_PLANES] = { pool->pitch, pool->pitch };
	gst_buffer_add_video_meta_full(buf, GST_VIDEO_FRAME_FLAG_NONE, GST_VIDEO_FORMAT_NV12,
		width, height, 2, offsets, strides);
	*buffer = buf;
	return GST_FLOW_OK;
}

static void pv_pool_finalize(GObject *object)
{
	PvPool *pool = (PvPool *)object;
	if (pool->heap >= 0)
		close(pool->heap);
	gst_clear_object(&pool->allocator);
	G_OBJECT_CLASS(pv_pool_parent_class)->finalize(object);
}

static void pv_pool_class_init(PvPoolClass *klass)
{
	GstBufferPoolClass *pool_class = GST_BUFFER_POOL_CLASS(klass);
	G_OBJECT_CLASS(klass)->finalize = pv_pool_finalize;
	pool_class->get_options = pv_pool_get_options;
	pool_class->set_config = pv_pool_set_config;
	pool_class->alloc_buffer = pv_pool_alloc_buffer;
}

static void pv_pool_init(PvPool *pool)
{
	pool->heap = -1;
}

static GstBufferPool *pv_pool_new(int heap, guint min_pitch)
{
	PvPool *pool = g_object_new(pv_pool_get_type(), NULL);
	pool->heap = fcntl(heap, F_DUPFD_CLOEXEC, 0);
	pool->min_pitch = min_pitch;
	pool->allocator = gst_dmabuf_allocator_new();
	return GST_BUFFER_POOL(gst_object_ref_sink(pool));
}

static char *find_device(void)
{
	GDir *dir = g_dir_open("/sys/class/video4linux", 0, NULL);
	const char *entry;
	char *found = NULL;
	if (!dir)
		return NULL;
	while (!found && (entry = g_dir_read_name(dir))) {
		g_autofree char *path = g_build_filename("/sys/class/video4linux", entry, "name", NULL);
		g_autofree char *name = NULL;
		if (g_file_get_contents(path, &name, NULL, NULL) && g_str_equal(g_strstrip(name), DEVICE_NAME))
			found = g_build_filename("/dev", entry, NULL);
	}
	g_dir_close(dir);
	return found;
}

static gboolean buffer_alloc(GstPliniusVdec *self, int heap, PvBuffer *buf, gsize size, gboolean map)
{
	struct dma_heap_allocation_data a = { .len = size, .fd_flags = O_RDWR | O_CLOEXEC };
	if (xioctl(heap, DMA_HEAP_IOCTL_ALLOC, &a) < 0) {
		GST_ERROR_OBJECT(self, "allocating %zu bytes: %s", size, g_strerror(errno));
		return FALSE;
	}
	buf->fd = a.fd;
	buf->size = size;
	buf->queued = FALSE;
	buf->map = NULL;
	if (map) {
		buf->map = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, a.fd, 0);
		if (buf->map == MAP_FAILED) {
			buf->map = NULL;
			GST_ERROR_OBJECT(self, "mapping a dma-buf: %s", g_strerror(errno));
			return FALSE;
		}
	}
	return TRUE;
}

static void buffer_free(PvBuffer *buf)
{
	if (buf->map)
		munmap(buf->map, buf->size);
	if (buf->fd > 0)
		close(buf->fd);
	memset(buf, 0, sizeof(*buf));
}

static void dmabuf_sync(int fd, guint64 flags)
{
	struct dma_buf_sync s = { flags };
	xioctl(fd, DMA_BUF_IOCTL_SYNC, &s);
}

static int reqbufs(GstPliniusVdec *self, guint type, guint count)
{
	struct v4l2_requestbuffers r = { .count = count, .type = type, .memory = V4L2_MEMORY_DMABUF };
	if (xioctl(self->fd, VIDIOC_REQBUFS, &r) < 0)
		return -1;
	return r.count;
}

static gboolean stream(GstPliniusVdec *self, guint type, gboolean on)
{
	int t = type;
	return xioctl(self->fd, on ? VIDIOC_STREAMON : VIDIOC_STREAMOFF, &t) == 0;
}

static gboolean queue(GstPliniusVdec *self, guint type, guint index, PvBuffer *buf, guint used,
	guint64 frame_number)
{
	struct v4l2_plane plane = { .bytesused = used, .length = buf->size, .m.fd = buf->fd };
	struct v4l2_buffer b = {
		.index = index, .type = type, .memory = V4L2_MEMORY_DMABUF,
		.m.planes = &plane, .length = 1,
		.timestamp.tv_sec = frame_number,
		/* MediaTek: fd of a metadata dma-buf for each picture, none here */
		.reserved2 = (guint32)-1,
	};
	if (xioctl(self->fd, VIDIOC_QBUF, &b) < 0) {
		GST_ERROR_OBJECT(self, "VIDIOC_QBUF type %u index %u: %s", type, index, g_strerror(errno));
		return FALSE;
	}
	buf->queued = TRUE;
	return TRUE;
}

/* Index of a dequeued buffer, or -1 if none is ready */
static int dequeue(GstPliniusVdec *self, guint type, guint *used, guint64 *frame_number, guint32 *flags)
{
	struct v4l2_plane plane = { 0 };
	struct v4l2_buffer b = {
		.type = type, .memory = V4L2_MEMORY_DMABUF, .m.planes = &plane, .length = 1,
		.reserved2 = (guint32)-1,
	};
	if (xioctl(self->fd, VIDIOC_DQBUF, &b) < 0) {
		if (errno != EAGAIN && errno != EPIPE)
			GST_WARNING_OBJECT(self, "VIDIOC_DQBUF type %u: %s", type, g_strerror(errno));
		return -1;
	}
	if (used)
		*used = plane.bytesused;
	if (frame_number)
		*frame_number = b.timestamp.tv_sec;
	if (flags)
		*flags = b.flags;
	return b.index;
}

static void release_capture(GstPliniusVdec *self)
{
	if (self->cap_on)
		stream(self, V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE, FALSE);
	self->cap_on = FALSE;
	if (self->n_cap)
		reqbufs(self, V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE, 0);
	for (guint i = 0; i < self->n_cap; i++)
		buffer_free(&self->cap[i]);
	self->n_cap = 0;
	buffer_free(&self->nv12);
}

static void device_close(GstPliniusVdec *self)
{
	if (self->fd < 0)
		return;
	if (self->n_pictures)
		GST_INFO_OBJECT(self, "%u pictures (%s), CPU per picture: %.2f ms in all, MDP %.2f ms, copy %.2f ms",
			self->n_pictures, self->zero_copy ? "zero copy" : "copied",
			self->cpu_total / 1e6 / self->n_pictures, self->cpu_mdp / 1e6 / self->n_pictures,
			self->cpu_copy / 1e6 / self->n_pictures);
	self->cpu_total = self->cpu_mdp = self->cpu_copy = 0;
	self->n_pictures = 0;
	release_capture(self);
	if (self->out_on)
		stream(self, V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE, FALSE);
	self->out_on = FALSE;
	reqbufs(self, V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE, 0);
	for (guint i = 0; i < N_OUTPUT; i++)
		buffer_free(&self->out[i]);
	close(self->fd);
	self->fd = -1;
	self->eos = FALSE;
	self->need_headers = TRUE;
}

static gboolean device_open(GstPliniusVdec *self)
{
	const CodecInfo *info = PV_GET_CLASS(self)->info;
	g_autofree char *dev = find_device();
	if (!dev) {
		GST_ELEMENT_ERROR(self, RESOURCE, NOT_FOUND, ("No MediaTek video decoder found"), (NULL));
		return FALSE;
	}
	self->fd = open(dev, O_RDWR | O_NONBLOCK | O_CLOEXEC);
	if (self->fd < 0) {
		GST_ELEMENT_ERROR(self, RESOURCE, OPEN_READ_WRITE, ("Cannot open %s", dev),
			("%s", g_strerror(errno)));
		return FALSE;
	}
	struct v4l2_format f = { .type = V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE };
	f.fmt.pix_mp.pixelformat = info->fourcc;
	f.fmt.pix_mp.num_planes = 1;
	f.fmt.pix_mp.plane_fmt[0].sizeimage = OUTPUT_SIZE;
	errno = 0;
	if (xioctl(self->fd, VIDIOC_S_FMT, &f) < 0 || f.fmt.pix_mp.pixelformat != info->fourcc) {
		GST_ELEMENT_ERROR(self, RESOURCE, SETTINGS, ("The decoder refuses %s",
			info->long_name), ("%s", errno ? g_strerror(errno) : "other format chosen"));
		goto fail;
	}
	guint size = MAX(f.fmt.pix_mp.plane_fmt[0].sizeimage, OUTPUT_SIZE);
	static const guint events[] = { V4L2_EVENT_SOURCE_CHANGE, V4L2_EVENT_EOS };
	for (guint i = 0; i < G_N_ELEMENTS(events); i++) {
		struct v4l2_event_subscription sub = { .type = events[i] };
		xioctl(self->fd, VIDIOC_SUBSCRIBE_EVENT, &sub);
	}
	if (reqbufs(self, V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE, N_OUTPUT) != N_OUTPUT) {
		GST_ELEMENT_ERROR(self, RESOURCE, SETTINGS, ("Cannot get decoder input buffers"),
			("%s", g_strerror(errno)));
		goto fail;
	}
	for (guint i = 0; i < N_OUTPUT; i++)
		if (!buffer_alloc(self, self->heap, &self->out[i], size, TRUE)) {
			GST_ELEMENT_ERROR(self, RESOURCE, NO_SPACE_LEFT, ("Cannot allocate decoder buffers"), (NULL));
			goto fail;
		}
	if (!stream(self, V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE, TRUE)) {
		GST_ELEMENT_ERROR(self, RESOURCE, FAILED, ("Cannot start the decoder"), ("%s", g_strerror(errno)));
		goto fail;
	}
	self->out_on = TRUE;
	GST_INFO_OBJECT(self, "opened %s for %s", dev, info->element);
	return TRUE;
fail:
	device_close(self);
	return FALSE;
}

/* After SOURCE_CHANGE: buffers for the decoded pictures, output caps, MDP target */
static gboolean setup_capture(GstPliniusVdec *self)
{
	GstVideoDecoder *dec = GST_VIDEO_DECODER(self);
	struct v4l2_format f = { .type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE };
	struct v4l2_selection sel = { .type = V4L2_BUF_TYPE_VIDEO_CAPTURE, .target = V4L2_SEL_TGT_COMPOSE };
	struct v4l2_control min = { .id = V4L2_CID_MIN_BUFFERS_FOR_CAPTURE };

	release_capture(self);
	if (xioctl(self->fd, VIDIOC_G_FMT, &f) < 0)
		return FALSE;
	guint32 fourcc = f.fmt.pix_mp.pixelformat;
	if (fourcc != V4L2_PIX_FMT_N12H) {
		GST_ELEMENT_ERROR(self, STREAM, FORMAT, ("Unsupported picture format from the decoder"),
			("%" GST_FOURCC_FORMAT " (10-bit video is not supported yet)", GST_FOURCC_ARGS(fourcc)));
		return FALSE;
	}
	self->coded_width = f.fmt.pix_mp.width;
	self->width = f.fmt.pix_mp.width;
	self->height = f.fmt.pix_mp.height;
	if (xioctl(self->fd, VIDIOC_G_SELECTION, &sel) == 0 && sel.r.width && sel.r.height) {
		self->width = sel.r.width;
		self->height = sel.r.height;
	} else if (self->input_state && GST_VIDEO_INFO_WIDTH(&self->input_state->info)) {
		self->width = GST_VIDEO_INFO_WIDTH(&self->input_state->info);
		self->height = GST_VIDEO_INFO_HEIGHT(&self->input_state->info);
	}
	self->width &= ~1u;
	self->height &= ~1u;

	guint n = 4;
	if (xioctl(self->fd, VIDIOC_G_CTRL, &min) == 0 && min.value > 0)
		n = min.value;
	n = MIN(n + EXTRA_CAPTURE, MAX_BUFFERS);
	int got = reqbufs(self, V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE, n);
	if (got <= 0)
		return FALSE;
	self->n_cap = MIN((guint)got, MAX_BUFFERS);
	gsize size = MAX(f.fmt.pix_mp.plane_fmt[0].sizeimage,
		pv_mdp_hyfbc_src_size(self->coded_width, self->height));
	size = GST_ROUND_UP_N(size, 4096);
	for (guint i = 0; i < self->n_cap; i++) {
		if (!buffer_alloc(self, self->hw_heap >= 0 ? self->hw_heap : self->heap, &self->cap[i], size, FALSE) ||
		    !queue(self, V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE, i, &self->cap[i], 0, 0))
			return FALSE;
	}
	self->nv12_pitch = GST_ROUND_UP_16(MAX(self->coded_width, self->width));
	if (!buffer_alloc(self, self->heap, &self->nv12, self->nv12_pitch * self->height * 3 / 2, TRUE))
		return FALSE;
	if (!stream(self, V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE, TRUE))
		return FALSE;
	self->cap_on = TRUE;
	GST_INFO_OBJECT(self, "pictures %ux%u (coded width %u), %u buffers of %zu bytes",
		self->width, self->height, self->coded_width, self->n_cap, size);

	GstVideoCodecState *out = gst_video_decoder_set_output_state(dec, GST_VIDEO_FORMAT_NV12,
		self->width, self->height, self->input_state);
	gst_video_codec_state_unref(out);
	return gst_video_decoder_negotiate(dec);
}

/* Copies the MDP's NV12 picture into a downstream buffer that is not one of ours. */
static gboolean copy_picture(GstPliniusVdec *self, GstBuffer *buffer)
{
	GstVideoCodecState *state = gst_video_decoder_get_output_state(GST_VIDEO_DECODER(self));
	GstVideoFrame vf;
	gboolean ok = gst_video_frame_map(&vf, &state->info, buffer, GST_MAP_WRITE);
	if (ok) {
		dmabuf_sync(self->nv12.fd, DMA_BUF_SYNC_START | DMA_BUF_SYNC_READ);
		const guint8 *src = self->nv12.map;
		for (guint plane = 0; plane < 2; plane++) {
			guint8 *dst = GST_VIDEO_FRAME_PLANE_DATA(&vf, plane);
			gint stride = GST_VIDEO_FRAME_PLANE_STRIDE(&vf, plane);
			guint rows = plane ? self->height / 2 : self->height;
			for (guint y = 0; y < rows; y++)
				memcpy(dst + (gsize)y * stride, src + (gsize)y * self->nv12_pitch, self->width);
			src += (gsize)self->nv12_pitch * self->height;
		}
		dmabuf_sync(self->nv12.fd, DMA_BUF_SYNC_END | DMA_BUF_SYNC_READ);
		gst_video_frame_unmap(&vf);
	}
	gst_video_codec_state_unref(state);
	return ok;
}

static GstFlowReturn output_picture(GstPliniusVdec *self, guint index, guint64 number)
{
	GstVideoDecoder *dec = GST_VIDEO_DECODER(self);
	GstVideoCodecFrame *frame = gst_video_decoder_get_frame(dec, (int)number);
	g_autoptr(GError) error = NULL;

	if (!frame) {
		GST_DEBUG_OBJECT(self, "picture for unknown frame %" G_GUINT64_FORMAT, number);
		return GST_FLOW_OK;
	}
	GstFlowReturn ret = gst_video_decoder_allocate_output_frame(dec, frame);
	if (ret != GST_FLOW_OK) {
		gst_video_decoder_release_frame(dec, frame);
		return ret;
	}
	GstMemory *mem = gst_buffer_n_memory(frame->output_buffer) == 1 ?
		gst_buffer_peek_memory(frame->output_buffer, 0) : NULL;
	GstVideoMeta *meta = gst_buffer_get_video_meta(frame->output_buffer);
	/* A buffer of a pool from before the decoded size was known may have lines too short */
	gboolean direct = self->zero_copy && mem && gst_is_dmabuf_memory(mem) && meta &&
		(guint)meta->stride[0] >= self->coded_width;
	int dst_fd = direct ? gst_dmabuf_memory_get_fd(mem) : self->nv12.fd;
	guint dst_pitch = direct ? (guint)meta->stride[0] : self->nv12_pitch;

	gint64 t0 = thread_cpu_ns();
	gboolean converted = pv_mdp_hyfbc_to_nv12(self->mdp, self->cap[index].fd, self->cap[index].size,
		self->coded_width, self->width, self->height, dst_fd, dst_pitch, &error);
	gint64 t1 = thread_cpu_ns();
	self->cpu_mdp += t1 - t0;
	if (!converted) {
		GST_ELEMENT_ERROR(self, STREAM, DECODE, ("Cannot convert the decoded picture"),
			("%s", error->message));
		gst_video_decoder_release_frame(dec, frame);
		return GST_FLOW_ERROR;
	}
	if (direct) {
		/* the CPU must not see stale cache lines of the previous picture */
		dmabuf_sync(dst_fd, DMA_BUF_SYNC_START | DMA_BUF_SYNC_READ);
		dmabuf_sync(dst_fd, DMA_BUF_SYNC_END | DMA_BUF_SYNC_READ);
	} else if (!copy_picture(self, frame->output_buffer)) {
		gst_video_decoder_release_frame(dec, frame);
		return GST_FLOW_ERROR;
	}
	self->cpu_copy += thread_cpu_ns() - t1;
	self->n_pictures++;
	return gst_video_decoder_finish_frame(dec, frame);
}

/* Handles what the device has ready; waits up to timeout ms for something. */
static GstFlowReturn process(GstPliniusVdec *self, int timeout)
{
	struct pollfd p = { .fd = self->fd, .events = POLLIN | POLLOUT | POLLPRI };
	GstFlowReturn ret = GST_FLOW_OK;

	if (poll(&p, 1, timeout) < 0 && errno != EINTR)
		return GST_FLOW_ERROR;
	/* before CAPTURE streams, poll reports POLLERR at once: do not spin */
	if ((p.revents & (POLLERR | POLLPRI | POLLIN | POLLOUT)) == POLLERR && timeout > 0)
		g_usleep(MIN(timeout, 5) * 1000);
	if (p.revents & POLLPRI) {
		struct v4l2_event ev;
		while (xioctl(self->fd, VIDIOC_DQEVENT, &ev) == 0) {
			if (ev.type == V4L2_EVENT_SOURCE_CHANGE) {
				GST_DEBUG_OBJECT(self, "source change");
				if (!setup_capture(self))
					return GST_FLOW_NOT_NEGOTIATED;
			} else if (ev.type == V4L2_EVENT_EOS) {
				GST_DEBUG_OBJECT(self, "end of stream event");
			}
		}
	}
	int i;
	while ((i = dequeue(self, V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE, NULL, NULL, NULL)) >= 0)
		if (i < N_OUTPUT)
			self->out[i].queued = FALSE;
	while (self->cap_on && ret == GST_FLOW_OK) {
		guint used;
		guint64 number;
		guint32 flags;
		i = dequeue(self, V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE, &used, &number, &flags);
		if (i < 0 || (guint)i >= self->n_cap)
			break;
		self->cap[i].queued = FALSE;
		if (used)
			ret = output_picture(self, i, number);
		if (flags & V4L2_BUF_FLAG_LAST) {
			self->eos = TRUE;
			break;
		}
		if (!queue(self, V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE, i, &self->cap[i], 0, 0))
			return GST_FLOW_ERROR;
	}
	return ret;
}

/* Remembers SPS/PPS (and VPS) so that a reopened decoder can start at the next keyframe. */
static void scan_headers(GstPliniusVdec *self, const guint8 *data, gsize size, gboolean *has_headers)
{
	Codec codec = PV_GET_CLASS(self)->info->codec;
	gsize i = 0;
	gboolean fresh = FALSE;

	*has_headers = FALSE;
	if (codec == CODEC_VP9)
		return;
	while (i + 3 < size) {
		if (data[i] || data[i + 1] || data[i + 2] != 1) {
			i++;
			continue;
		}
		gsize start = i > 0 && data[i - 1] == 0 ? i - 1 : i;
		gsize nal = i + 3, end = nal;
		while (end + 3 < size && !(data[end] == 0 && data[end + 1] == 0 && data[end + 2] == 1))
			end++;
		if (end + 3 >= size)
			end = size;
		else if (end > nal && data[end - 1] == 0)
			end--;
		guint type = codec == CODEC_H264 ? (data[nal] & 0x1f) : ((data[nal] >> 1) & 0x3f);
		gboolean header = codec == CODEC_H264 ? (type == 7 || type == 8) : (type >= 32 && type <= 34);
		if (header) {
			if (!fresh) {
				g_byte_array_set_size(self->headers, 0);
				fresh = TRUE;
			}
			g_byte_array_append(self->headers, data + start, end - start);
			*has_headers = TRUE;
		}
		i = end;
	}
}

static GstFlowReturn gst_plinius_vdec_handle_frame(GstVideoDecoder *dec, GstVideoCodecFrame *frame)
{
	GstPliniusVdec *self = PV(dec);
	GstFlowReturn ret = GST_FLOW_OK;
	GstMapInfo map;
	gboolean has_headers;
	gint64 t_start = thread_cpu_ns();

	if (self->fd < 0 && !device_open(self)) {
		gst_video_decoder_release_frame(dec, frame);
		return GST_FLOW_ERROR;
	}
	if (!gst_buffer_map(frame->input_buffer, &map, GST_MAP_READ)) {
		gst_video_decoder_release_frame(dec, frame);
		return GST_FLOW_ERROR;
	}
	scan_headers(self, map.data, map.size, &has_headers);
	const guint8 *prefix = NULL;
	gsize prefix_size = 0;
	if (self->need_headers && !has_headers && self->headers->len) {
		prefix = self->headers->data;
		prefix_size = self->headers->len;
	}

	int slot = -1;
	gint64 deadline = g_get_monotonic_time() + 5 * G_USEC_PER_SEC;
	while (slot < 0 && g_get_monotonic_time() < deadline) {
		for (int i = 0; i < N_OUTPUT; i++)
			if (!self->out[i].queued) {
				slot = i;
				break;
			}
		if (slot < 0 && (ret = process(self, 100)) != GST_FLOW_OK)
			goto out;
	}
	if (slot < 0) {
		GST_ELEMENT_ERROR(self, STREAM, DECODE, ("The decoder does not take more data"), (NULL));
		ret = GST_FLOW_ERROR;
		goto out;
	}
	PvBuffer *buf = &self->out[slot];
	if (prefix_size + map.size > buf->size) {
		GST_ELEMENT_ERROR(self, STREAM, DECODE, ("Frame of %zu bytes is too large", map.size), (NULL));
		ret = GST_FLOW_ERROR;
		goto out;
	}
	dmabuf_sync(buf->fd, DMA_BUF_SYNC_START | DMA_BUF_SYNC_WRITE);
	if (prefix_size)
		memcpy(buf->map, prefix, prefix_size);
	memcpy((guint8 *)buf->map + prefix_size, map.data, map.size);
	dmabuf_sync(buf->fd, DMA_BUF_SYNC_END | DMA_BUF_SYNC_WRITE);
	if (!queue(self, V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE, slot, buf, prefix_size + map.size,
	    frame->system_frame_number)) {
		GST_ELEMENT_ERROR(self, STREAM, DECODE, ("Cannot queue data for the decoder"), (NULL));
		ret = GST_FLOW_ERROR;
		goto out;
	}
	self->need_headers = FALSE;
	ret = process(self, 0);
out:
	gst_buffer_unmap(frame->input_buffer, &map);
	gst_video_codec_frame_unref(frame);
	self->cpu_total += thread_cpu_ns() - t_start;
	return ret;
}

/* Lets the decoder hand out all pictures it still holds, then closes it. */
static GstFlowReturn drain_and_close(GstPliniusVdec *self)
{
	GstFlowReturn ret = GST_FLOW_OK;
	if (self->fd < 0)
		return GST_FLOW_OK;
	if (self->cap_on) {
		struct v4l2_decoder_cmd cmd = { .cmd = V4L2_DEC_CMD_STOP };
		if (xioctl(self->fd, VIDIOC_DECODER_CMD, &cmd) == 0) {
			gint64 end = g_get_monotonic_time() + DRAIN_TIMEOUT * 1000;
			while (!self->eos && ret == GST_FLOW_OK && g_get_monotonic_time() < end)
				ret = process(self, 100);
		}
	}
	device_close(self);
	/* pictures the decoder never returned (e.g. skipped frames) */
	GList *frames = gst_video_decoder_get_frames(GST_VIDEO_DECODER(self));
	for (GList *l = frames; l; l = l->next)
		gst_video_decoder_release_frame(GST_VIDEO_DECODER(self), gst_video_codec_frame_ref(l->data));
	g_list_free_full(frames, (GDestroyNotify)gst_video_codec_frame_unref);
	return ret;
}

static GstFlowReturn gst_plinius_vdec_drain(GstVideoDecoder *dec)
{
	return drain_and_close(PV(dec));
}

static GstFlowReturn gst_plinius_vdec_finish(GstVideoDecoder *dec)
{
	return drain_and_close(PV(dec));
}

static gboolean gst_plinius_vdec_flush(GstVideoDecoder *dec)
{
	/* reopening is simpler and safer with this driver than restarting the queues */
	device_close(PV(dec));
	return TRUE;
}

static gboolean gst_plinius_vdec_decide_allocation(GstVideoDecoder *dec, GstQuery *query)
{
	GstPliniusVdec *self = PV(dec);
	GstCaps *caps;
	GstVideoInfo info;

	gst_query_parse_allocation(query, &caps, NULL);
	if (caps && gst_video_info_from_caps(&info, caps)) {
		guint pitch = GST_ROUND_UP_16(MAX(self->coded_width, (guint)GST_VIDEO_INFO_WIDTH(&info)));
		/* our buffers fit when downstream reads the video meta or needs no padding */
		gboolean fits = gst_query_find_allocation_meta(query, GST_VIDEO_META_API_TYPE, NULL) ||
			((guint)GST_VIDEO_INFO_PLANE_STRIDE(&info, 0) == pitch &&
			 GST_VIDEO_INFO_PLANE_OFFSET(&info, 1) == (gsize)pitch * GST_VIDEO_INFO_HEIGHT(&info));
		if (fits) {
			GstBufferPool *pool = pv_pool_new(self->heap, self->coded_width);
			guint size = 0, min = 0, max = 0;
			if (gst_query_get_n_allocation_pools(query) > 0) {
				gst_query_parse_nth_allocation_pool(query, 0, NULL, &size, &min, &max);
				gst_query_set_nth_allocation_pool(query, 0, pool, size, MAX(min, 4), 0);
			} else
				gst_query_add_allocation_pool(query, pool, size, 4, 0);
			gst_object_unref(pool);
		}
	}
	if (!GST_VIDEO_DECODER_CLASS(parent_class)->decide_allocation(dec, query))
		return FALSE;
	GstBufferPool *used = NULL;
	if (gst_query_get_n_allocation_pools(query) > 0)
		gst_query_parse_nth_allocation_pool(query, 0, &used, NULL, NULL, NULL);
	self->zero_copy = used && G_TYPE_CHECK_INSTANCE_TYPE(used, pv_pool_get_type());
	GST_DEBUG_OBJECT(self, "%s", self->zero_copy ? "MDP writes into the output buffers" : "pictures are copied");
	gst_clear_object(&used);
	return TRUE;
}

static gboolean gst_plinius_vdec_set_format(GstVideoDecoder *dec, GstVideoCodecState *state)
{
	GstPliniusVdec *self = PV(dec);
	if (self->input_state) {
		if (gst_caps_is_equal(self->input_state->caps, state->caps))
			return TRUE;
		drain_and_close(self);
		gst_video_codec_state_unref(self->input_state);
	}
	self->input_state = gst_video_codec_state_ref(state);
	return TRUE;
}

static gboolean gst_plinius_vdec_start(GstVideoDecoder *dec)
{
	GstPliniusVdec *self = PV(dec);
	g_autoptr(GError) error = NULL;

	self->heap = open(HEAP_PATH, O_RDONLY | O_CLOEXEC);
	if (self->heap < 0) {
		GST_ELEMENT_ERROR(self, RESOURCE, OPEN_READ, ("Cannot open %s", HEAP_PATH),
			("%s", g_strerror(errno)));
		return FALSE;
	}
	self->hw_heap = open(HW_HEAP_PATH, O_RDONLY | O_CLOEXEC);
	self->mdp = pv_mdp_new(&error);
	if (!self->mdp) {
		GST_ELEMENT_ERROR(self, LIBRARY, INIT, ("Cannot use the MDP for colour conversion"),
			("%s", error->message));
		close(self->heap);
		self->heap = -1;
		if (self->hw_heap >= 0)
			close(self->hw_heap);
		self->hw_heap = -1;
		return FALSE;
	}
	self->need_headers = TRUE;
	return TRUE;
}

static gboolean gst_plinius_vdec_stop(GstVideoDecoder *dec)
{
	GstPliniusVdec *self = PV(dec);
	device_close(self);
	pv_mdp_free(self->mdp);
	self->mdp = NULL;
	if (self->heap >= 0)
		close(self->heap);
	self->heap = -1;
	if (self->hw_heap >= 0)
		close(self->hw_heap);
	self->hw_heap = -1;
	g_clear_pointer(&self->input_state, gst_video_codec_state_unref);
	g_byte_array_set_size(self->headers, 0);
	return TRUE;
}

static void gst_plinius_vdec_init(GTypeInstance *instance, gpointer klass)
{
	GstPliniusVdec *self = PV(instance);
	self->fd = -1;
	self->heap = -1;
	self->hw_heap = -1;
	self->headers = g_byte_array_new();
	gst_video_decoder_set_packetized(GST_VIDEO_DECODER(self), TRUE);
	gst_video_decoder_set_needs_format(GST_VIDEO_DECODER(self), TRUE);
}

static void gst_plinius_vdec_finalize(GObject *object)
{
	g_byte_array_unref(PV(object)->headers);
	G_OBJECT_CLASS(parent_class)->finalize(object);
}

static void gst_plinius_vdec_class_init(gpointer klass, gpointer data)
{
	GstPliniusVdecClass *pclass = klass;
	GstElementClass *element_class = klass;
	GstVideoDecoderClass *dec_class = klass;
	const CodecInfo *info = data;

	parent_class = g_type_class_peek_parent(klass);
	pclass->info = info;
	G_OBJECT_CLASS(klass)->finalize = gst_plinius_vdec_finalize;
	gst_element_class_set_static_metadata(element_class, info->long_name,
		"Codec/Decoder/Video/Hardware",
		"Decodes video with the MediaTek decoder and converts it with the MDP",
		"misc-de <misc-de@users.noreply.github.com>");
	gst_element_class_add_pad_template(element_class, gst_pad_template_new("sink",
		GST_PAD_SINK, GST_PAD_ALWAYS, gst_caps_from_string(info->caps)));
	gst_element_class_add_pad_template(element_class, gst_pad_template_new("src",
		GST_PAD_SRC, GST_PAD_ALWAYS, gst_caps_from_string("video/x-raw, format=NV12, "
		"width=(int)[16,4096], height=(int)[16,2176]")));
	dec_class->start = gst_plinius_vdec_start;
	dec_class->stop = gst_plinius_vdec_stop;
	dec_class->set_format = gst_plinius_vdec_set_format;
	dec_class->handle_frame = gst_plinius_vdec_handle_frame;
	dec_class->drain = gst_plinius_vdec_drain;
	dec_class->finish = gst_plinius_vdec_finish;
	dec_class->flush = gst_plinius_vdec_flush;
	dec_class->decide_allocation = gst_plinius_vdec_decide_allocation;
}

static gboolean plugin_init(GstPlugin *plugin)
{
	GST_DEBUG_CATEGORY_INIT(pv_debug, "pliniusvdec", 0, "Plinius hardware video decoder");
	/* only on the phone: elsewhere the plugin registers nothing */
	if (!g_file_test("/vendor/lib64/mt6878/libdpframework.so", G_FILE_TEST_EXISTS))
		return TRUE;
	gst_plugin_add_dependency_simple(plugin, NULL, "/vendor/lib64/mt6878", "libdpframework.so",
		GST_PLUGIN_DEPENDENCY_FLAG_NONE);
	for (guint i = 0; i < G_N_ELEMENTS(codecs); i++) {
		GTypeInfo type_info = {
			.class_size = sizeof(GstPliniusVdecClass),
			.class_init = gst_plinius_vdec_class_init,
			.class_data = &codecs[i],
			.instance_size = sizeof(GstPliniusVdec),
			.instance_init = gst_plinius_vdec_init,
		};
		g_autofree char *type_name = g_strdup_printf("GstPlinius%s", codecs[i].element + 7);
		GType type = g_type_register_static(GST_TYPE_VIDEO_DECODER, type_name, &type_info, 0);
		/* above the software decoders, so that playbin picks the hardware */
		if (!gst_element_register(plugin, codecs[i].element, GST_RANK_PRIMARY + 2, type))
			return FALSE;
	}
	return TRUE;
}

GST_PLUGIN_DEFINE(GST_VERSION_MAJOR, GST_VERSION_MINOR, pliniusvdec,
	"Hardware video decoding on the Volla Phone Plinius", plugin_init, "0.1", "MIT",
	"plinius-vdec", "https://github.com/misc-de/Volla-Arch")
