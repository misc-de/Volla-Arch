// SPDX-License-Identifier: MIT
// The front camera of the Plinius for plinius-face, straight from CameraService through
// libhybris, not from the session's PipeWire: no other program can slip frames in.
#ifndef FACE_CAMERA_H
#define FACE_CAMERA_H

#include <stddef.h>
#include <stdint.h>

typedef struct FaceCamera FaceCamera;

/* Opens the front camera and starts its preview. NULL and *error (free with free()) when it is
 * missing or busy. */
FaceCamera *face_camera_open (char **error);
void face_camera_close (FaceCamera *camera);

/* Frame size in bytes (NV21), sensor size and the clockwise rotation that makes it upright */
void face_camera_format (FaceCamera *camera, int *width, int *height, int *orientation,
			 size_t *bytes);

/* Waits up to timeout_ms for a frame newer than the last one taken and copies it to dst.
 * 1: frame, 0: timeout, -1: camera error. *timestamp_ns (CLOCK_MONOTONIC) of its arrival. */
int face_camera_next_frame (FaceCamera *camera, uint8_t *dst, int timeout_ms,
			    int64_t *timestamp_ns);

#endif
