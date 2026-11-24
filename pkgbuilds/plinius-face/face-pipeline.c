// SPDX-License-Identifier: MIT
// plinius-face recognition chain, see face-pipeline.h.
//
// Each model gets its input as a patch cut from the camera frame by an affine map (patch pixel →
// upright frame), sampled straight from NV21: no full-frame conversion or rotation, and the
// rotations for Face Mesh and the alignment for SFace cost nothing extra.
//
// YuNet's five points align the face for SFace, as in OpenCV's FaceRecognizerSF, the pairing
// OpenCV Zoo measured (LFW 99.4 %). Face Mesh points in their place gave a noticeably different
// embedding (cosine 0.86 to OpenCV's on the same image); its landmarks are for the liveness
// checks.
#include "face-pipeline.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define DETECT_MODEL "face_detection_yunet_2023mar_float32.tflite"
#define MESH_MODEL "face_landmarks_detector.tflite"
#define EMBED_MODEL "face_recognition_sface_2021dec_float32.tflite"

#define DETECT_SIZE 640	      /* YuNet's input is fixed at 640×640 */
#define DETECT_MIN_SCORE 0.9f /* OpenCV's FaceDetectorYN default */
#define MESH_SIZE 256
#define MESH_ROI_SCALE 1.5f /* the landmark model wants some room around the detected box */
#define EMBED_SIZE 112

/* SFace's alignment template (as OpenCV's FaceRecognizerSF::alignCrop): eyes, nose tip and mouth
 * corners in the 112×112 crop, image-left point first, the order of YuNet's points */
static const FacePoint embed_template[5] = {
	{ 38.2946f, 51.6963f }, { 73.5318f, 51.5014f }, { 56.0252f, 71.7366f },
	{ 41.5493f, 92.3655f }, { 70.7299f, 92.2041f },
};

/* Patch pixel (i, j) → upright frame (u, v) = (a·i + b·j + tx, c·i + d·j + ty) */
typedef struct {
	float a, b, c, d, tx, ty;
} Affine;

struct FacePipeline {
	PlTfl *detect, *mesh, *embed;
	float patch[DETECT_SIZE * DETECT_SIZE * 3]; /* RGB 0..255, big enough for every model */
	char description[256];
};

static double
now_ms (void)
{
	struct timespec ts;

	clock_gettime (CLOCK_MONOTONIC, &ts);
	return ts.tv_sec * 1e3 + ts.tv_nsec / 1e6;
}

static float
sigmoid (float x)
{
	if (x < -100)
		x = -100;
	else if (x > 100)
		x = 100;
	return 1.0f / (1.0f + expf (-x));
}

static float
clamp01 (float x)
{
	return x < 0 ? 0 : x > 1 ? 1 : x;
}

static void
upright_size (const FaceFrame *frame, int *width, int *height)
{
	int quarter = frame->orientation == 90 || frame->orientation == 270;

	*width = quarter ? frame->height : frame->width;
	*height = quarter ? frame->width : frame->height;
}

/* Upright frame position → sensor frame position (inverse of the rotation) */
static void
upright_to_sensor (const FaceFrame *frame, float u, float v, float *x, float *y)
{
	float w1 = frame->width - 1, h1 = frame->height - 1;

	switch (frame->orientation) {
	case 90: *x = v; *y = h1 - u; break;
	case 180: *x = w1 - u; *y = h1 - v; break;
	case 270: *x = w1 - v; *y = u; break;
	default: *x = u; *y = v; break;
	}
}

static unsigned char
clamp_byte (float value)
{
	return value <= 0 ? 0 : value >= 255 ? 255 : (unsigned char) (value + 0.5f);
}

/* Cuts a size×size RGB patch (0..255) out of the frame; luma bilinear, chroma nearest. Outside
 * the frame stays black, as MediaPipe and OpenCV pad. */
static void
sample_patch (const FaceFrame *frame, const Affine *m, int size, float *out)
{
	const uint8_t *luma = frame->nv21;
	const uint8_t *chroma = frame->nv21 + (size_t) frame->width * frame->height;
	int w = frame->width, h = frame->height;

	for (int j = 0; j < size; j++) {
		for (int i = 0; i < size; i++, out += 3) {
			float u = m->a * i + m->b * j + m->tx;
			float v = m->c * i + m->d * j + m->ty;
			float x, y, fx, fy, luma_value, cr, cb;
			int x0, y0, x1, y1, chroma_x, chroma_y;
			const uint8_t *vu;

			upright_to_sensor (frame, u, v, &x, &y);
			if (x < 0 || y < 0 || x > w - 1 || y > h - 1) {
				out[0] = out[1] = out[2] = 0;
				continue;
			}
			x0 = (int) x;
			y0 = (int) y;
			x1 = x0 + 1 < w ? x0 + 1 : x0;
			y1 = y0 + 1 < h ? y0 + 1 : y0;
			fx = x - x0;
			fy = y - y0;
			luma_value = (luma[y0 * w + x0] * (1 - fx) + luma[y0 * w + x1] * fx) * (1 - fy) +
				     (luma[y1 * w + x0] * (1 - fx) + luma[y1 * w + x1] * fx) * fy;
			chroma_x = (int) (x + 0.5f) / 2;
			chroma_y = (int) (y + 0.5f) / 2;
			if (chroma_x >= w / 2)
				chroma_x = w / 2 - 1;
			if (chroma_y >= h / 2)
				chroma_y = h / 2 - 1;
			vu = chroma + ((size_t) chroma_y * (w / 2) + chroma_x) * 2;
			cr = vu[0] - 128.0f;
			cb = vu[1] - 128.0f;
			/* BT.601, full range as the camera HAL delivers NV21 */
			out[0] = clamp_byte (luma_value + 1.402f * cr);
			out[1] = clamp_byte (luma_value - 0.344136f * cb - 0.714136f * cr);
			out[2] = clamp_byte (luma_value + 1.772f * cb);
		}
	}
}

/* Writes the patch into the model's input as value · scale + offset, quantized if the model
 * wants; bgr swaps the channels for models trained on OpenCV's channel order */
static int
fill_input (PlTfl *model, const float *rgb, size_t count, float scale, float offset, int bgr)
{
	PlTflTensorInfo info;
	void *data = pltfl_input (model, 0, &info);

	if (!data)
		return -1;
	for (size_t k = 0; k < count; k++) {
		/* Channel k % 3 of the model comes from this channel of the patch */
		size_t from = bgr ? k - k % 3 + 2 - k % 3 : k;
		float value = rgb[from] * scale + offset;

		switch (info.type) {
		case PLTFL_FLOAT32:
			if (info.bytes != count * sizeof (float))
				return -1;
			((float *) data)[k] = value;
			break;
		case PLTFL_UINT8:
		case PLTFL_INT8: {
			float q;

			if (info.bytes != count || info.scale == 0)
				return -1;
			q = roundf (value / info.scale) + info.zero_point;
			if (info.type == PLTFL_UINT8)
				((uint8_t *) data)[k] = q < 0 ? 0 : q > 255 ? 255 : (uint8_t) q;
			else
				((int8_t *) data)[k] = q < -128 ? -128 : q > 127 ? 127 : (int8_t) q;
			break;
		}
		default:
			return -1;
		}
	}
	return 0;
}

/* Float output with exactly count values, or NULL */
static const float *
float_output (PlTfl *model, int index, size_t count)
{
	PlTflTensorInfo info;
	const void *data = pltfl_output (model, index, &info);

	if (!data || info.type != PLTFL_FLOAT32 || info.bytes != count * sizeof (float))
		return NULL;
	return data;
}

typedef struct {
	float score;
	float box[4];	  /* x0, y0, x1, y1 in detector input pixels */
	float points[10]; /* five (x, y) */
} Detection;

/* Runs YuNet on the square region (ox, oy, side) of the upright frame. Returns whether a face was
 * found; the best one goes to det in upright frame coordinates. Decoding as OpenCV's
 * FaceDetectorYN: per stride 8, 16, 32 a grid of cells, each with class and objectness score,
 * box offset and size (log), and five point offsets. */
static int
detect_in (FacePipeline *self, const FaceFrame *frame, float ox, float oy, float side,
	   Detection *det)
{
	static const int strides[3] = { 8, 16, 32 };
	float k = side / DETECT_SIZE;
	Affine m = { k, 0, 0, k, ox + 0.5f * k - 0.5f, oy + 0.5f * k - 0.5f };
	Detection best = { 0 };

	sample_patch (frame, &m, DETECT_SIZE, self->patch);
	/* YuNet takes BGR 0..255 */
	if (fill_input (self->detect, self->patch, DETECT_SIZE * DETECT_SIZE * 3, 1, 0, 1) ||
	    pltfl_invoke (self->detect))
		return -1;

	for (int level = 0; level < 3; level++) {
		int stride = strides[level], cols = DETECT_SIZE / stride, cells = cols * cols;
		/* Outputs in the order of the ONNX model: cls, obj, bbox, kps, each for 8, 16, 32 */
		const float *cls = float_output (self->detect, level, cells);
		const float *obj = float_output (self->detect, 3 + level, cells);
		const float *bbox = float_output (self->detect, 6 + level, cells * 4);
		const float *kps = float_output (self->detect, 9 + level, cells * 10);

		if (!cls || !obj || !bbox || !kps)
			return -1;
		for (int cell = 0; cell < cells; cell++) {
			float score = sqrtf (clamp01 (cls[cell]) * clamp01 (obj[cell]));
			int col = cell % cols, row = cell / cols;
			const float *b = bbox + cell * 4, *p = kps + cell * 10;
			float cx, cy, w, h;

			if (score < DETECT_MIN_SCORE || score <= best.score)
				continue;
			cx = (col + b[0]) * stride;
			cy = (row + b[1]) * stride;
			w = expf (b[2]) * stride;
			h = expf (b[3]) * stride;
			best.score = score;
			best.box[0] = cx - w / 2;
			best.box[1] = cy - h / 2;
			best.box[2] = cx + w / 2;
			best.box[3] = cy + h / 2;
			for (int n = 0; n < 5; n++) {
				best.points[2 * n] = (p[2 * n] + col) * stride;
				best.points[2 * n + 1] = (p[2 * n + 1] + row) * stride;
			}
		}
	}
	if (best.score == 0)
		return 0;

	/* Detector input pixel → upright frame, the same map as the patch */
	det->score = best.score;
	for (int v = 0; v < 4; v++)
		det->box[v] = (v % 2 ? m.ty : m.tx) + best.box[v] * k;
	for (int v = 0; v < 10; v++)
		det->points[v] = (v % 2 ? m.ty : m.tx) + best.points[v] * k;
	return 1;
}

/* Least-squares similarity (rotation, scale, shift) taking src onto dst; returned inverted, as
 * the map dst → src that sample_patch() needs */
static Affine
similarity_inverse (const FacePoint *src, const FacePoint *dst, int n)
{
	float msx = 0, msy = 0, mdx = 0, mdy = 0, dot = 0, cross = 0, norm = 0, c, s, n2;
	Affine m;

	for (int k = 0; k < n; k++) {
		msx += src[k].x / n;
		msy += src[k].y / n;
		mdx += dst[k].x / n;
		mdy += dst[k].y / n;
	}
	for (int k = 0; k < n; k++) {
		float sx = src[k].x - msx, sy = src[k].y - msy;
		float dx = dst[k].x - mdx, dy = dst[k].y - mdy;

		dot += sx * dx + sy * dy;
		cross += sx * dy - sy * dx;
		norm += sx * sx + sy * sy;
	}
	/* Forward: dst = [c −s; s c] (src − ms) + md */
	c = norm > 0 ? dot / norm : 1;
	s = norm > 0 ? cross / norm : 0;
	n2 = c * c + s * s;
	if (n2 <= 0)
		n2 = 1;
	m.a = c / n2;
	m.b = s / n2;
	m.c = -s / n2;
	m.d = c / n2;
	m.tx = msx - (m.a * mdx + m.b * mdy);
	m.ty = msy - (m.c * mdx + m.d * mdy);
	return m;
}

FacePipeline *
face_pipeline_new (const char *model_dir, const PlTflOptions *options, char **error)
{
	FacePipeline *self = calloc (1, sizeof *self);
	const char *names[3] = { DETECT_MODEL, MESH_MODEL, EMBED_MODEL };
	PlTfl **models[3];
	char path[4096];

	if (!self) {
		if (error)
			*error = strdup ("out of memory");
		return NULL;
	}
	models[0] = &self->detect;
	models[1] = &self->mesh;
	models[2] = &self->embed;
	for (int k = 0; k < 3; k++) {
		snprintf (path, sizeof path, "%s/%s", model_dir, names[k]);
		*models[k] = pltfl_open (path, options, error);
		if (!*models[k]) {
			face_pipeline_free (self);
			return NULL;
		}
	}
	snprintf (self->description, sizeof self->description,
		  "detect %d/%d, mesh %d/%d, embed %d/%d operations on the NPU",
		  pltfl_npu_nodes (self->detect), pltfl_total_nodes (self->detect),
		  pltfl_npu_nodes (self->mesh), pltfl_total_nodes (self->mesh),
		  pltfl_npu_nodes (self->embed), pltfl_total_nodes (self->embed));
	return self;
}

void
face_pipeline_free (FacePipeline *self)
{
	if (!self)
		return;
	pltfl_close (self->detect);
	pltfl_close (self->mesh);
	pltfl_close (self->embed);
	free (self);
}

const char *
face_pipeline_describe (FacePipeline *self)
{
	return self->description;
}

int
face_pipeline_run (FacePipeline *self, const FaceFrame *frame, FaceResult *result)
{
	int width, height, found;
	float side, size, k, cx, cy, angle, cs, sn, norm = 0;
	const float *landmarks, *presence, *embedding;
	Detection det;
	Affine m;
	double start;

	memset (result, 0, sizeof *result);
	upright_size (frame, &width, &height);

	/* 1. Find the face: first in the middle square of the frame (a face in front of the phone
	 *    fills it well), else in the whole frame with padding */
	start = now_ms ();
	side = width < height ? width : height;
	found = detect_in (self, frame, (width - side) / 2, (height - side) / 2, side, &det);
	if (found == 0) {
		side = width > height ? width : height;
		found = detect_in (self, frame, (width - side) / 2, (height - side) / 2, side, &det);
	}
	result->ms_detect = now_ms () - start;
	if (found <= 0)
		return found;
	result->score = det.score;
	memcpy (result->box, det.box, sizeof result->box);
	for (int p = 0; p < 5; p++)
		result->keypoints[p] = (FacePoint) { det.points[2 * p], det.points[2 * p + 1] };

	/* 2. Landmarks: a square around the box, turned so that the eyes are level */
	start = now_ms ();
	cx = (det.box[0] + det.box[2]) / 2;
	cy = (det.box[1] + det.box[3]) / 2;
	size = fmaxf (det.box[2] - det.box[0], det.box[3] - det.box[1]) * MESH_ROI_SCALE;
	angle = atan2f (result->keypoints[1].y - result->keypoints[0].y,
			result->keypoints[1].x - result->keypoints[0].x);
	k = size / MESH_SIZE;
	cs = cosf (angle) * k;
	sn = sinf (angle) * k;
	m = (Affine) { cs, -sn, sn, cs, 0, 0 };
	m.tx = cx + (m.a + m.b) * (0.5f - MESH_SIZE / 2.0f);
	m.ty = cy + (m.c + m.d) * (0.5f - MESH_SIZE / 2.0f);
	sample_patch (frame, &m, MESH_SIZE, self->patch);
	/* Face Mesh wants RGB 0..1 */
	if (fill_input (self->mesh, self->patch, MESH_SIZE * MESH_SIZE * 3, 1.0f / 255, 0, 0) ||
	    pltfl_invoke (self->mesh))
		return -1;
	landmarks = float_output (self->mesh, 0, FACE_MESH_POINTS * 3);
	presence = float_output (self->mesh, 1, 1);
	if (!landmarks || !presence)
		return -1;
	result->presence = sigmoid (presence[0]);
	for (int p = 0; p < FACE_MESH_POINTS; p++) {
		/* Model output is in patch pixels with the edge at 0, the map takes pixel centers */
		float i = landmarks[3 * p] - 0.5f, j = landmarks[3 * p + 1] - 0.5f;

		result->mesh[p].x = m.a * i + m.b * j + m.tx;
		result->mesh[p].y = m.c * i + m.d * j + m.ty;
		result->mesh_z[p] = landmarks[3 * p + 2] * k;
	}
	result->ms_mesh = now_ms () - start;

	/* 3. Align YuNet's five points to SFace's template */
	start = now_ms ();
	m = similarity_inverse (result->keypoints, embed_template, 5);
	sample_patch (frame, &m, EMBED_SIZE, self->patch);

	/* 4. Embedding; SFace takes RGB 0..255 */
	if (fill_input (self->embed, self->patch, EMBED_SIZE * EMBED_SIZE * 3, 1, 0, 0) ||
	    pltfl_invoke (self->embed))
		return -1;
	embedding = float_output (self->embed, 0, FACE_EMBEDDING_SIZE);
	if (!embedding)
		return -1;
	for (int v = 0; v < FACE_EMBEDDING_SIZE; v++)
		norm += embedding[v] * embedding[v];
	norm = norm > 0 ? sqrtf (norm) : 1;
	for (int v = 0; v < FACE_EMBEDDING_SIZE; v++)
		result->embedding[v] = embedding[v] / norm;
	result->ms_embed = now_ms () - start;
	return 1;
}

float
face_similarity (const float *a, const float *b)
{
	float dot = 0;

	for (int v = 0; v < FACE_EMBEDDING_SIZE; v++)
		dot += a[v] * b[v];
	return dot;
}
