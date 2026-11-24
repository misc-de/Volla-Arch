// SPDX-License-Identifier: MIT
// The recognition chain of plinius-face, from a camera frame to a face embedding:
//   1. YuNet (OpenCV Zoo) finds the face and five points: eyes, nose tip, mouth corners,
//   2. Face Mesh (MediaPipe face landmarker v2) places 478 landmarks on it, for the liveness
//      checks,
//   3. YuNet's five points align the face to the 112×112 template of SFace (OpenCV Zoo),
//   4. SFace turns the aligned face into a 128-value embedding; two embeddings of the same person
//      have a high cosine similarity.
// All three models run through plinius-tflite, on the NPU where it can, else on the CPU.
//
// Coordinates are those of the upright frame (the camera frame turned by the sensor orientation),
// in pixels, x to the right and y down.
#ifndef FACE_PIPELINE_H
#define FACE_PIPELINE_H

#include <stdint.h>

#include <plinius-tflite.h>

#define FACE_MESH_POINTS 478
#define FACE_EMBEDDING_SIZE 128

typedef struct FacePipeline FacePipeline;

typedef struct {
	const uint8_t *nv21; /* Y plane, then interleaved V/U at half resolution */
	int width, height;   /* as the sensor delivers it */
	int orientation;     /* clockwise rotation that makes the frame upright: 0/90/180/270 */
} FaceFrame;

typedef struct {
	float x, y;
} FacePoint;

typedef struct {
	float score;                       /* detector confidence, 0..1 */
	float box[4];                      /* x0, y0, x1, y1 */
	FacePoint keypoints[5];            /* YuNet: eyes, nose tip, mouth corners, image-left first */
	float presence;                    /* Face Mesh: is it a face, 0..1 */
	FacePoint mesh[FACE_MESH_POINTS];  /* landmarks */
	float mesh_z[FACE_MESH_POINTS];    /* relative depth, in the same scale as x */
	float embedding[FACE_EMBEDDING_SIZE]; /* L2-normalized */
	double ms_detect, ms_mesh, ms_embed;  /* time per step */
} FaceResult;

/* Loads the three models from model_dir (see face-pipeline.c for the file names) */
FacePipeline *face_pipeline_new (const char *model_dir, const PlTflOptions *options, char **error);
void face_pipeline_free (FacePipeline *pipeline);

/* Describes on which unit each model runs, e.g. "detect 165/165 NPU, ..." (static buffer) */
const char *face_pipeline_describe (FacePipeline *pipeline);

/* Runs the chain on one frame. 1: a face with landmarks and embedding, 0: no face, -1: error */
int face_pipeline_run (FacePipeline *pipeline, const FaceFrame *frame, FaceResult *result);

/* Cosine similarity of two L2-normalized embeddings, -1..1 */
float face_similarity (const float *a, const float *b);

#endif
