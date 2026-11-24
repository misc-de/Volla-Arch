/* SPDX-License-Identifier: MIT */
/*
 * Hardware H.264 encoding on the Volla Phone Plinius (MediaTek Dimensity 7300): pliniush264enc.
 *
 * The vendor encoder (mtk-vcodec-enc, module mtk_vcodec_enc_v1) is a stateful V4L2
 * memory-to-memory encoder, but GStreamer's v4l2h264enc only gets the parameter sets out of it:
 * like the decoder, it takes the address of a raw frame only from a dma-buf. So the frames go
 * into OUTPUT buffers of V4L2_MEMORY_DMABUF from the mtk_mm heap; the bitstream comes in MMAP
 * CAPTURE buffers. Each v4l2_buffer carries the fd of a metadata dma-buf in reserved2, -1 for none.
 * Found on the device (2026-11-21):
 *  - NV12 and NV21 as two planes (NM12/NM21); the driver pads the lines (1080 wide: 1088), G_FMT
 *    tells the pitch;
 *  - with the parameter sets joined to the first picture (V4L2_CID_MPEG_VIDEO_HEADER_MODE), every
 *    picture is one CAPTURE buffer, and the OUTPUT buffer's timestamp comes back on it;
 *  - High profile up to level 5.1, 3840x2160 and 2160x3840 although it reports 2176 lines at
 *    most; 1080p at about 80 fps, 4K at about 44 fps, the copy of each frame included.
 * The bit rates by default are the ones of Android's camcorder profiles on this phone.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>

#include <linux/dma-buf.h>
#include <linux/dma-heap.h>
#include <linux/videodev2.h>

#include <gst/gst.h>
#include <gst/video/gstvideoencoder.h>
#include <gst/video/video.h>

#define PACKAGE "plinius-vdec"

GST_DEBUG_CATEGORY_STATIC(venc_debug);
#define GST_CAT_DEFAULT venc_debug

#define DEVICE_NAME "mtk-vcodec-enc"
#define HEAP_PATH   "/dev/dma_heap/mtk_mm"
#define N_INPUT     4
#define N_BITSTREAM 4
#define TIMEOUT_MS  2000
#define OUT_TYPE    V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE
#define CAP_TYPE    V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE

enum { PROP_0, PROP_BITRATE, PROP_KEY_INT_MAX };

/* A raw frame for the encoder: one dma-buf per plane, mapped for the copy */
typedef struct {
	int fd[2];
	guint8 *map[2];
	gsize size[2];
	gboolean queued;
} VencInput;

/* A CAPTURE buffer of the driver, mapped */
typedef struct {
	guint8 *map;
	gsize size;
} VencBitstream;

typedef struct {
	GstVideoEncoder parent;

	guint bitrate;	    /* kbit/s, 0: by size */
	guint key_int_max;  /* frames between key frames, 0: one second */

	int heap, fd;
	GstVideoCodecState *input_state;
	guint width, height, pitch[2];
	VencInput in[N_INPUT];
	guint n_in;
	VencBitstream bs[N_BITSTREAM];
	guint n_bs;
	gboolean streaming;
	guint pending; /* frames queued whose pictures have not come back */
} GstPliniusH264Enc;

typedef struct {
	GstVideoEncoderClass parent_class;
} GstPliniusH264EncClass;

#define PE(obj) ((GstPliniusH264Enc *)(obj))

G_DEFINE_TYPE(GstPliniusH264Enc, gst_plinius_h264_enc, GST_TYPE_VIDEO_ENCODER)

static int xioctl(int fd, unsigned long req, void *arg)
{
	int r;
	do
		r = ioctl(fd, req, arg);
	while (r < 0 && errno == EINTR);
	return r;
}

/* The encoder's device node, NULL without one */
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

/* Bit rate in bit/s: the property, or by the shorter side as Android records 720p, 1080p, 4K */
static guint bitrate_for(GstPliniusH264Enc *self)
{
	guint side = MIN(self->width, self->height);
	if (self->bitrate)
		return MIN(self->bitrate, 400000u) * 1000;
	return side <= 720 ? 9000000 : side <= 1088 ? 20000000 : 50000000;
}

/* The H.264 level for the picture size at up to 30 fps: 3.1, 4.1 or 5.1 */
static int level_for(guint width, guint height)
{
	guint64 area = (guint64)width * height;
	if (area <= 1280 * 720)
		return V4L2_MPEG_VIDEO_H264_LEVEL_3_1;
	if (area <= 2048 * 1088)
		return V4L2_MPEG_VIDEO_H264_LEVEL_4_1;
	return V4L2_MPEG_VIDEO_H264_LEVEL_5_1;
}

static void set_control(GstPliniusH264Enc *self, guint id, int value, const char *what)
{
	struct v4l2_control c = { .id = id, .value = value };
	if (xioctl(self->fd, VIDIOC_S_CTRL, &c) < 0)
		GST_WARNING_OBJECT(self, "%s %d: %s", what, value, g_strerror(errno));
}

static void device_close(GstPliniusH264Enc *self)
{
	if (self->fd < 0)
		return;
	if (self->streaming) {
		int t = OUT_TYPE;
		xioctl(self->fd, VIDIOC_STREAMOFF, &t);
		t = CAP_TYPE;
		xioctl(self->fd, VIDIOC_STREAMOFF, &t);
		self->streaming = FALSE;
	}
	for (guint i = 0; i < self->n_bs; i++)
		if (self->bs[i].map)
			munmap(self->bs[i].map, self->bs[i].size);
	for (guint i = 0; i < self->n_in; i++)
		for (guint p = 0; p < 2; p++) {
			if (self->in[i].map[p])
				munmap(self->in[i].map[p], self->in[i].size[p]);
			if (self->in[i].fd[p] > 0)
				close(self->in[i].fd[p]);
		}
	memset(self->bs, 0, sizeof(self->bs));
	memset(self->in, 0, sizeof(self->in));
	self->n_bs = self->n_in = 0;
	self->pending = 0;
	close(self->fd);
	self->fd = -1;
}

/* A dma-buf of the heap, mapped; FALSE if either fails */
static gboolean input_alloc(GstPliniusH264Enc *self, VencInput *in, guint plane, gsize size)
{
	struct dma_heap_allocation_data a = { .len = size, .fd_flags = O_RDWR | O_CLOEXEC };
	if (xioctl(self->heap, DMA_HEAP_IOCTL_ALLOC, &a) < 0) {
		GST_ERROR_OBJECT(self, "allocating %zu bytes: %s", size, g_strerror(errno));
		return FALSE;
	}
	in->fd[plane] = a.fd;
	in->size[plane] = size;
	in->map[plane] = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, a.fd, 0);
	if (in->map[plane] == MAP_FAILED) {
		in->map[plane] = NULL;
		GST_ERROR_OBJECT(self, "mapping a dma-buf: %s", g_strerror(errno));
		return FALSE;
	}
	return TRUE;
}

static gboolean queue_bitstream(GstPliniusH264Enc *self, guint index)
{
	struct v4l2_plane plane = { 0 };
	struct v4l2_buffer b = {
		.index = index, .type = CAP_TYPE, .memory = V4L2_MEMORY_MMAP, .m.planes = &plane, .length = 1,
		/* MediaTek: fd of a metadata dma-buf, none */
		.reserved2 = (guint32)-1,
	};
	if (xioctl(self->fd, VIDIOC_QBUF, &b) < 0) {
		GST_ERROR_OBJECT(self, "queueing bitstream buffer %u: %s", index, g_strerror(errno));
		return FALSE;
	}
	return TRUE;
}

/* Opens and sets up the encoder for the input format; with an error posted when it fails */
static gboolean device_open(GstPliniusH264Enc *self)
{
	GstVideoInfo *info = &self->input_state->info;
	gboolean nv21 = GST_VIDEO_INFO_FORMAT(info) == GST_VIDEO_FORMAT_NV21;
	g_autofree char *path = find_device();
	const char *step = "open";

	self->width = GST_VIDEO_INFO_WIDTH(info);
	self->height = GST_VIDEO_INFO_HEIGHT(info);
	self->fd = path ? open(path, O_RDWR | O_NONBLOCK | O_CLOEXEC) : -1;
	if (self->fd < 0)
		goto fail;

	step = "VIDIOC_S_FMT (bitstream)";
	struct v4l2_format cf = { .type = CAP_TYPE };
	cf.fmt.pix_mp.pixelformat = V4L2_PIX_FMT_H264;
	cf.fmt.pix_mp.width = self->width;
	cf.fmt.pix_mp.height = self->height;
	cf.fmt.pix_mp.num_planes = 1;
	/* An I frame of 4K at 50 Mbit/s takes about 0.8 MB: room for much more */
	cf.fmt.pix_mp.plane_fmt[0].sizeimage = MAX(self->width * self->height, 4u << 20);
	if (xioctl(self->fd, VIDIOC_S_FMT, &cf) < 0)
		goto fail;

	step = "VIDIOC_S_FMT (frames)";
	struct v4l2_format of = { .type = OUT_TYPE };
	of.fmt.pix_mp.pixelformat = nv21 ? V4L2_PIX_FMT_NV21M : V4L2_PIX_FMT_NV12M;
	of.fmt.pix_mp.width = self->width;
	of.fmt.pix_mp.height = self->height;
	of.fmt.pix_mp.num_planes = 2;
	if (xioctl(self->fd, VIDIOC_S_FMT, &of) < 0)
		goto fail;
	step = "the frame layout";
	if (of.fmt.pix_mp.num_planes != 2 || of.fmt.pix_mp.plane_fmt[0].bytesperline < self->width ||
	    of.fmt.pix_mp.plane_fmt[1].bytesperline < self->width ||
	    of.fmt.pix_mp.plane_fmt[0].sizeimage < (gsize)of.fmt.pix_mp.plane_fmt[0].bytesperline * self->height ||
	    of.fmt.pix_mp.plane_fmt[1].sizeimage < (gsize)of.fmt.pix_mp.plane_fmt[1].bytesperline * (self->height / 2)) {
		errno = EINVAL;
		goto fail;
	}
	self->pitch[0] = of.fmt.pix_mp.plane_fmt[0].bytesperline;
	self->pitch[1] = of.fmt.pix_mp.plane_fmt[1].bytesperline;

	struct v4l2_streamparm parm = { .type = OUT_TYPE };
	int fps_n = GST_VIDEO_INFO_FPS_N(info), fps_d = GST_VIDEO_INFO_FPS_D(info);
	if (fps_n <= 0 || fps_d <= 0)
		fps_n = 30, fps_d = 1;
	parm.parm.output.timeperframe = (struct v4l2_fract){ fps_d, fps_n };
	if (xioctl(self->fd, VIDIOC_S_PARM, &parm) < 0)
		GST_WARNING_OBJECT(self, "frame rate %d/%d: %s", fps_n, fps_d, g_strerror(errno));
	set_control(self, V4L2_CID_MPEG_VIDEO_BITRATE, (int)bitrate_for(self), "bit rate");
	set_control(self, V4L2_CID_MPEG_VIDEO_H264_PROFILE, V4L2_MPEG_VIDEO_H264_PROFILE_HIGH, "profile");
	set_control(self, V4L2_CID_MPEG_VIDEO_H264_LEVEL, level_for(self->width, self->height), "level");
	set_control(self, V4L2_CID_MPEG_VIDEO_HEADER_MODE, V4L2_MPEG_VIDEO_HEADER_MODE_JOINED_WITH_1ST_FRAME,
		    "header mode");
	set_control(self, V4L2_CID_MPEG_VIDEO_GOP_SIZE,
		    self->key_int_max ? (int)self->key_int_max : MAX(1, (fps_n + fps_d - 1) / fps_d), "key frame interval");

	step = "VIDIOC_REQBUFS (frames)";
	struct v4l2_requestbuffers rq = { .count = N_INPUT, .type = OUT_TYPE, .memory = V4L2_MEMORY_DMABUF };
	if (xioctl(self->fd, VIDIOC_REQBUFS, &rq) < 0 || rq.count == 0)
		goto fail;
	self->n_in = MIN(rq.count, N_INPUT);
	step = "frame buffers";
	for (guint i = 0; i < self->n_in; i++)
		for (guint p = 0; p < 2; p++)
			if (!input_alloc(self, &self->in[i], p, of.fmt.pix_mp.plane_fmt[p].sizeimage))
				goto fail;

	step = "VIDIOC_REQBUFS (bitstream)";
	rq = (struct v4l2_requestbuffers){ .count = N_BITSTREAM, .type = CAP_TYPE, .memory = V4L2_MEMORY_MMAP };
	if (xioctl(self->fd, VIDIOC_REQBUFS, &rq) < 0 || rq.count == 0)
		goto fail;
	self->n_bs = MIN(rq.count, N_BITSTREAM);
	for (guint i = 0; i < self->n_bs; i++) {
		struct v4l2_plane plane = { 0 };
		struct v4l2_buffer b = { .index = i, .type = CAP_TYPE, .memory = V4L2_MEMORY_MMAP,
					 .m.planes = &plane, .length = 1 };
		step = "VIDIOC_QUERYBUF";
		if (xioctl(self->fd, VIDIOC_QUERYBUF, &b) < 0)
			goto fail;
		step = "mapping a bitstream buffer";
		self->bs[i].map = mmap(NULL, plane.length, PROT_READ, MAP_SHARED, self->fd, plane.m.mem_offset);
		if (self->bs[i].map == MAP_FAILED) {
			self->bs[i].map = NULL;
			goto fail;
		}
		self->bs[i].size = plane.length;
		step = "VIDIOC_QBUF (bitstream)";
		if (!queue_bitstream(self, i))
			goto fail;
	}

	step = "VIDIOC_STREAMON";
	int t = OUT_TYPE;
	if (xioctl(self->fd, VIDIOC_STREAMON, &t) < 0)
		goto fail;
	self->streaming = TRUE;
	t = CAP_TYPE;
	if (xioctl(self->fd, VIDIOC_STREAMON, &t) < 0)
		goto fail;
	GST_INFO_OBJECT(self, "%ux%u %s, pitch %u/%u, %u kbit/s", self->width, self->height, nv21 ? "NV21" : "NV12",
			self->pitch[0], self->pitch[1], bitrate_for(self) / 1000);
	return TRUE;

fail:
	GST_ELEMENT_ERROR(self, RESOURCE, OPEN_READ_WRITE, ("Cannot use the hardware video encoder"),
			  ("%s: %s", path ? step : "no " DEVICE_NAME " device", path ? g_strerror(errno) : "not found"));
	device_close(self);
	return FALSE;
}

/* Takes what the encoder has done: frames it no longer reads, pictures it made. Waits up to
 * timeout ms for something when nothing is ready. */
static GstFlowReturn process(GstPliniusH264Enc *self, int timeout)
{
	GstVideoEncoder *enc = GST_VIDEO_ENCODER(self);
	struct pollfd p = { .fd = self->fd, .events = POLLIN | POLLOUT };
	GstFlowReturn ret = GST_FLOW_OK;

	if (poll(&p, 1, timeout) < 0 && errno != EINTR)
		return GST_FLOW_ERROR;
	if (p.revents & POLLERR) {
		GST_ELEMENT_ERROR(self, STREAM, ENCODE, ("The hardware video encoder failed"), (NULL));
		return GST_FLOW_ERROR;
	}
	for (;;) {
		struct v4l2_plane planes[2] = { 0 };
		struct v4l2_buffer b = { .type = OUT_TYPE, .memory = V4L2_MEMORY_DMABUF, .m.planes = planes,
					 .length = 2, .reserved2 = (guint32)-1 };
		if (xioctl(self->fd, VIDIOC_DQBUF, &b) < 0)
			break;
		if (b.index < self->n_in)
			self->in[b.index].queued = FALSE;
	}
	for (;;) {
		struct v4l2_plane plane = { 0 };
		struct v4l2_buffer b = { .type = CAP_TYPE, .memory = V4L2_MEMORY_MMAP, .m.planes = &plane,
					 .length = 1, .reserved2 = (guint32)-1 };
		if (xioctl(self->fd, VIDIOC_DQBUF, &b) < 0)
			break;
		if (b.index >= self->n_bs)
			continue;
		GstVideoCodecFrame *frame = gst_video_encoder_get_frame(enc, (int)b.timestamp.tv_sec);
		gsize offset = MIN(plane.data_offset, plane.bytesused);
		gsize used = MIN(plane.bytesused, self->bs[b.index].size) - MIN(offset, self->bs[b.index].size);
		if (frame && used > 0) {
			frame->output_buffer = gst_buffer_new_memdup(self->bs[b.index].map + offset, used);
			if (b.flags & V4L2_BUF_FLAG_KEYFRAME)
				GST_VIDEO_CODEC_FRAME_SET_SYNC_POINT(frame);
			else
				GST_VIDEO_CODEC_FRAME_UNSET_SYNC_POINT(frame);
			self->pending -= self->pending > 0;
			ret = gst_video_encoder_finish_frame(enc, frame);
		} else if (frame) {
			GST_WARNING_OBJECT(self, "empty picture for frame %ld", (long)b.timestamp.tv_sec);
			gst_video_codec_frame_unref(frame);
		} else {
			GST_DEBUG_OBJECT(self, "picture for unknown frame %ld", (long)b.timestamp.tv_sec);
		}
		if (!queue_bitstream(self, b.index))
			return GST_FLOW_ERROR;
		if (ret != GST_FLOW_OK)
			return ret;
	}
	return ret;
}

/* Copies a frame into a free input buffer and queues it */
static GstFlowReturn queue_frame(GstPliniusH264Enc *self, GstVideoCodecFrame *frame)
{
	VencInput *in = NULL;
	gint64 deadline = g_get_monotonic_time() + TIMEOUT_MS * 1000;
	GstVideoFrame vf;

	while (!in) {
		for (guint i = 0; i < self->n_in && !in; i++)
			if (!self->in[i].queued)
				in = &self->in[i];
		if (in)
			break;
		if (g_get_monotonic_time() > deadline) {
			GST_ELEMENT_ERROR(self, STREAM, ENCODE, ("The hardware video encoder stopped"),
					  ("no frame buffer free for %d ms", TIMEOUT_MS));
			return GST_FLOW_ERROR;
		}
		GstFlowReturn ret = process(self, 100);
		if (ret != GST_FLOW_OK)
			return ret;
	}
	if (!gst_video_frame_map(&vf, &self->input_state->info, frame->input_buffer, GST_MAP_READ)) {
		GST_ELEMENT_ERROR(self, STREAM, ENCODE, ("Cannot read the frame"), (NULL));
		return GST_FLOW_ERROR;
	}
	for (guint p = 0; p < 2; p++) {
		const guint8 *src = GST_VIDEO_FRAME_PLANE_DATA(&vf, p);
		gint stride = GST_VIDEO_FRAME_PLANE_STRIDE(&vf, p);
		guint rows = p ? self->height / 2 : self->height;
		struct dma_buf_sync sync = { DMA_BUF_SYNC_START | DMA_BUF_SYNC_WRITE };
		xioctl(in->fd[p], DMA_BUF_IOCTL_SYNC, &sync);
		for (guint y = 0; y < rows; y++)
			memcpy(in->map[p] + (gsize)y * self->pitch[p], src + (gsize)y * stride, self->width);
		sync.flags = DMA_BUF_SYNC_END | DMA_BUF_SYNC_WRITE;
		xioctl(in->fd[p], DMA_BUF_IOCTL_SYNC, &sync);
	}
	gst_video_frame_unmap(&vf);

	if (GST_VIDEO_CODEC_FRAME_IS_FORCE_KEYFRAME(frame))
		set_control(self, V4L2_CID_MPEG_VIDEO_FORCE_KEY_FRAME, 1, "key frame");
	struct v4l2_plane planes[2];
	for (guint p = 0; p < 2; p++)
		planes[p] = (struct v4l2_plane){ .bytesused = in->size[p], .length = in->size[p], .m.fd = in->fd[p] };
	struct v4l2_buffer b = {
		.index = in - self->in, .type = OUT_TYPE, .memory = V4L2_MEMORY_DMABUF, .m.planes = planes, .length = 2,
		/* the frame number comes back on its picture */
		.timestamp.tv_sec = frame->system_frame_number,
		.reserved2 = (guint32)-1,
	};
	if (xioctl(self->fd, VIDIOC_QBUF, &b) < 0) {
		GST_ELEMENT_ERROR(self, STREAM, ENCODE, ("The hardware video encoder refused a frame"),
				  ("VIDIOC_QBUF: %s", g_strerror(errno)));
		return GST_FLOW_ERROR;
	}
	in->queued = TRUE;
	self->pending++;
	return GST_FLOW_OK;
}

static GstFlowReturn gst_plinius_h264_enc_handle_frame(GstVideoEncoder *enc, GstVideoCodecFrame *frame)
{
	GstPliniusH264Enc *self = PE(enc);
	GstFlowReturn ret;

	if (self->fd < 0 && !device_open(self)) {
		gst_video_codec_frame_unref(frame);
		return GST_FLOW_ERROR;
	}
	ret = queue_frame(self, frame);
	gst_video_codec_frame_unref(frame);
	if (ret == GST_FLOW_OK)
		ret = process(self, 0);
	return ret;
}

/* Waits for the pictures of all frames queued */
static GstFlowReturn drain(GstPliniusH264Enc *self)
{
	gint64 deadline = g_get_monotonic_time() + TIMEOUT_MS * 1000;
	while (self->fd >= 0 && self->pending > 0) {
		if (g_get_monotonic_time() > deadline) {
			GST_WARNING_OBJECT(self, "%u pictures did not come", self->pending);
			break;
		}
		GstFlowReturn ret = process(self, 100);
		if (ret != GST_FLOW_OK)
			return ret;
	}
	return GST_FLOW_OK;
}

static GstFlowReturn gst_plinius_h264_enc_finish(GstVideoEncoder *enc)
{
	return drain(PE(enc));
}

static gboolean gst_plinius_h264_enc_set_format(GstVideoEncoder *enc, GstVideoCodecState *state)
{
	GstPliniusH264Enc *self = PE(enc);
	GstVideoCodecState *out;

	/* A new format: what is queued goes out first, the device opens again with the next frame */
	drain(self);
	device_close(self);
	g_clear_pointer(&self->input_state, gst_video_codec_state_unref);
	self->input_state = gst_video_codec_state_ref(state);
	out = gst_video_encoder_set_output_state(
		enc,
		gst_caps_from_string("video/x-h264, stream-format=(string)byte-stream, alignment=(string)au, "
				     "profile=(string)high"),
		state);
	gst_video_codec_state_unref(out);
	return gst_video_encoder_negotiate(enc);
}

static gboolean gst_plinius_h264_enc_flush(GstVideoEncoder *enc)
{
	device_close(PE(enc));
	return TRUE;
}

static gboolean gst_plinius_h264_enc_start(GstVideoEncoder *enc)
{
	GstPliniusH264Enc *self = PE(enc);
	self->heap = open(HEAP_PATH, O_RDONLY | O_CLOEXEC);
	if (self->heap < 0) {
		GST_ELEMENT_ERROR(self, RESOURCE, OPEN_READ, ("Cannot open %s", HEAP_PATH), ("%s", g_strerror(errno)));
		return FALSE;
	}
	return TRUE;
}

static gboolean gst_plinius_h264_enc_stop(GstVideoEncoder *enc)
{
	GstPliniusH264Enc *self = PE(enc);
	device_close(self);
	g_clear_pointer(&self->input_state, gst_video_codec_state_unref);
	if (self->heap >= 0)
		close(self->heap);
	self->heap = -1;
	return TRUE;
}

static void gst_plinius_h264_enc_set_property(GObject *object, guint id, const GValue *value, GParamSpec *pspec)
{
	GstPliniusH264Enc *self = PE(object);
	switch (id) {
	case PROP_BITRATE:
		self->bitrate = g_value_get_uint(value);
		break;
	case PROP_KEY_INT_MAX:
		self->key_int_max = g_value_get_uint(value);
		break;
	default:
		G_OBJECT_WARN_INVALID_PROPERTY_ID(object, id, pspec);
	}
}

static void gst_plinius_h264_enc_get_property(GObject *object, guint id, GValue *value, GParamSpec *pspec)
{
	GstPliniusH264Enc *self = PE(object);
	switch (id) {
	case PROP_BITRATE:
		g_value_set_uint(value, self->bitrate);
		break;
	case PROP_KEY_INT_MAX:
		g_value_set_uint(value, self->key_int_max);
		break;
	default:
		G_OBJECT_WARN_INVALID_PROPERTY_ID(object, id, pspec);
	}
}

static void gst_plinius_h264_enc_init(GstPliniusH264Enc *self)
{
	self->fd = -1;
	self->heap = -1;
}

static void gst_plinius_h264_enc_class_init(GstPliniusH264EncClass *klass)
{
	GObjectClass *object_class = G_OBJECT_CLASS(klass);
	GstElementClass *element_class = GST_ELEMENT_CLASS(klass);
	GstVideoEncoderClass *enc_class = GST_VIDEO_ENCODER_CLASS(klass);

	object_class->set_property = gst_plinius_h264_enc_set_property;
	object_class->get_property = gst_plinius_h264_enc_get_property;
	g_object_class_install_property(object_class, PROP_BITRATE,
		g_param_spec_uint("bitrate", "Bit rate", "Bit rate in kbit/s, 0: 9, 20 or 50 Mbit/s by size",
				  0, 400000, 0, G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS));
	g_object_class_install_property(object_class, PROP_KEY_INT_MAX,
		g_param_spec_uint("key-int-max", "Key frame interval", "Frames from one key frame to the next, 0: one second",
				  0, 65535, 0, G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS));
	gst_element_class_set_static_metadata(element_class, "Plinius hardware H.264 encoder",
					      "Codec/Encoder/Video/Hardware",
					      "H.264 with the MediaTek video encoder of the Volla Phone Plinius", "misc-de");
	gst_element_class_add_pad_template(element_class, gst_pad_template_new("sink", GST_PAD_SINK, GST_PAD_ALWAYS,
		gst_caps_from_string("video/x-raw, format=(string){ NV12, NV21 }, width=(int)[160,3840], "
				     "height=(int)[128,3840], framerate=(fraction)[0/1,120/1]")));
	gst_element_class_add_pad_template(element_class, gst_pad_template_new("src", GST_PAD_SRC, GST_PAD_ALWAYS,
		gst_caps_from_string("video/x-h264, stream-format=(string)byte-stream, alignment=(string)au, "
				     "profile=(string)high")));
	enc_class->start = gst_plinius_h264_enc_start;
	enc_class->stop = gst_plinius_h264_enc_stop;
	enc_class->set_format = gst_plinius_h264_enc_set_format;
	enc_class->handle_frame = gst_plinius_h264_enc_handle_frame;
	enc_class->finish = gst_plinius_h264_enc_finish;
	enc_class->flush = gst_plinius_h264_enc_flush;
}

static gboolean plugin_init(GstPlugin *plugin)
{
	GST_DEBUG_CATEGORY_INIT(venc_debug, "pliniusvenc", 0, "Plinius hardware video encoder");
	/* only where the encoder is: elsewhere the plugin registers nothing */
	g_autofree char *device = find_device();
	if (!device)
		return TRUE;
	/* above the software encoders, so that encodebin picks the hardware */
	return gst_element_register(plugin, "pliniush264enc", GST_RANK_PRIMARY + 2, gst_plinius_h264_enc_get_type());
}

GST_PLUGIN_DEFINE(GST_VERSION_MAJOR, GST_VERSION_MINOR, pliniusvenc,
	"Hardware video encoding on the Volla Phone Plinius", plugin_init, "0.1", "MIT",
	"plinius-vdec", "https://github.com/misc-de/Volla-Arch")
