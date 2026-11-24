// SPDX-License-Identifier: MIT
// plinius-face front camera, see face-camera.h. Same way as camera-hal-bridge: libhybris'
// camera compatibility layer needs a preview texture in a thread that owns a GL context and
// updates it whenever asked, else the camera's buffer queue stalls; the frames themselves come
// as NV21 copies through the preview callback.
#include "face-camera.h"

#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES2/gl2.h>
#include <GLES2/gl2ext.h>
#include <hybris/camera/camera_compatibility_layer.h>
#include <hybris/camera/camera_compatibility_layer_capabilities.h>

#include <errno.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define WIDTH 1280 /* preview size as the sensor delivers it, as in camera-hal-bridge */
#define HEIGHT 720
#define FRAME_SIZE (WIDTH * HEIGHT * 3 / 2)

/* Android's CAMERA_FACING_FRONT */
#define FACING_FRONT 1

struct FaceCamera {
	int id;
	int orientation;
	EGLDisplay display;
	pthread_t thread;
	pthread_mutex_t lock;
	pthread_cond_t cond;
	bool started;	   /* the thread has opened the camera, or failed to */
	bool failed;
	bool quit;
	int textures_pending;
	uint8_t *frame;    /* latest preview frame */
	uint64_t sequence; /* counts frames; next_frame hands out each one once */
	uint64_t taken;
	int64_t timestamp_ns;
};

static int64_t
monotonic_ns (void)
{
	struct timespec ts;

	clock_gettime (CLOCK_MONOTONIC, &ts);
	return (int64_t) ts.tv_sec * 1000000000 + ts.tv_nsec;
}

static void
set_error (char **error, const char *message)
{
	if (error)
		*error = strdup (message);
}

static void
on_frame (void *data, uint32_t size, void *context)
{
	FaceCamera *self = context;

	if (size != FRAME_SIZE)
		return;
	pthread_mutex_lock (&self->lock);
	memcpy (self->frame, data, FRAME_SIZE);
	self->sequence++;
	self->timestamp_ns = monotonic_ns ();
	pthread_cond_broadcast (&self->cond);
	pthread_mutex_unlock (&self->lock);
}

static void
on_texture (void *context)
{
	FaceCamera *self = context;

	pthread_mutex_lock (&self->lock);
	self->textures_pending++;
	pthread_cond_broadcast (&self->cond);
	pthread_mutex_unlock (&self->lock);
}

static void
on_error (void *context)
{
	FaceCamera *self = context;

	fprintf (stderr, "plinius-face: camera error\n");
	pthread_mutex_lock (&self->lock);
	self->failed = true;
	pthread_cond_broadcast (&self->cond);
	pthread_mutex_unlock (&self->lock);
}

static void *
camera_thread (void *data)
{
	FaceCamera *self = data;
	EGLint attrs[] = { EGL_CONTEXT_CLIENT_VERSION, 2, EGL_NONE };
	EGLContext context = eglCreateContext (self->display, EGL_NO_CONFIG_KHR, EGL_NO_CONTEXT, attrs);
	struct CameraControlListener listener = { 0 };
	struct CameraControl *control = NULL;
	GLuint texture = 0;

	if (context != EGL_NO_CONTEXT &&
	    eglMakeCurrent (self->display, EGL_NO_SURFACE, EGL_NO_SURFACE, context)) {
		glGenTextures (1, &texture);
		glBindTexture (GL_TEXTURE_EXTERNAL_OES, texture);
		listener.on_msg_error_cb = on_error;
		listener.on_preview_texture_needs_update_cb = on_texture;
		listener.on_preview_frame_cb = on_frame;
		listener.context = self;
		control = android_camera_connect_by_id (self->id, &listener);
	}
	if (control) {
		android_camera_set_flash_mode (control, FLASH_MODE_OFF);
		android_camera_set_auto_focus_mode (control, AUTO_FOCUS_MODE_CONTINUOUS_VIDEO);
		android_camera_set_preview_size (control, WIDTH, HEIGHT);
		android_camera_set_preview_texture (control, texture);
		android_camera_set_preview_callback_mode (control, PREVIEW_CALLBACK_ENABLED);
		android_camera_start_preview (control);
	}

	pthread_mutex_lock (&self->lock);
	self->started = true;
	self->failed = !control;
	pthread_cond_broadcast (&self->cond);
	while (control && !self->quit) {
		if (self->textures_pending) {
			self->textures_pending = 0;
			pthread_mutex_unlock (&self->lock);
			android_camera_update_preview_texture (control);
			pthread_mutex_lock (&self->lock);
			continue;
		}
		pthread_cond_wait (&self->cond, &self->lock);
	}
	pthread_mutex_unlock (&self->lock);

	if (control) {
		android_camera_set_preview_callback_mode (control, PREVIEW_CALLBACK_DISABLED);
		android_camera_stop_preview (control);
		android_camera_disconnect (control);
	}
	if (context != EGL_NO_CONTEXT) {
		eglMakeCurrent (self->display, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);
		eglDestroyContext (self->display, context);
	}
	return NULL;
}

FaceCamera *
face_camera_open (char **error)
{
	FaceCamera *self;
	pthread_condattr_t attr;
	int count, id = -1, orientation = 0;

	count = android_camera_get_number_of_devices ();
	for (int k = 0; k < count && id < 0; k++) {
		int facing, o;

		if (android_camera_get_device_info (k, &facing, &o) == 0 && facing == FACING_FRONT) {
			id = k;
			orientation = o;
		}
	}
	if (id < 0) {
		set_error (error, "no front camera");
		return NULL;
	}

	self = calloc (1, sizeof *self);
	if (!self || !(self->frame = malloc (FRAME_SIZE))) {
		free (self);
		set_error (error, "out of memory");
		return NULL;
	}
	self->id = id;
	self->orientation = orientation;
	self->display = eglGetDisplay (EGL_DEFAULT_DISPLAY);
	if (!eglInitialize (self->display, NULL, NULL) || !eglBindAPI (EGL_OPENGL_ES_API)) {
		free (self->frame);
		free (self);
		set_error (error, "no EGL");
		return NULL;
	}
	pthread_mutex_init (&self->lock, NULL);
	pthread_condattr_init (&attr);
	pthread_condattr_setclock (&attr, CLOCK_MONOTONIC);
	pthread_cond_init (&self->cond, &attr);
	pthread_condattr_destroy (&attr);

	pthread_create (&self->thread, NULL, camera_thread, self);
	pthread_mutex_lock (&self->lock);
	while (!self->started)
		pthread_cond_wait (&self->cond, &self->lock);
	pthread_mutex_unlock (&self->lock);
	if (self->failed) {
		face_camera_close (self);
		set_error (error, "cannot open the front camera (busy?)");
		return NULL;
	}
	return self;
}

void
face_camera_close (FaceCamera *self)
{
	if (!self)
		return;
	pthread_mutex_lock (&self->lock);
	self->quit = true;
	pthread_cond_broadcast (&self->cond);
	pthread_mutex_unlock (&self->lock);
	pthread_join (self->thread, NULL);
	pthread_cond_destroy (&self->cond);
	pthread_mutex_destroy (&self->lock);
	free (self->frame);
	free (self);
}

void
face_camera_format (FaceCamera *self, int *width, int *height, int *orientation, size_t *bytes)
{
	*width = WIDTH;
	*height = HEIGHT;
	*orientation = self->orientation;
	*bytes = FRAME_SIZE;
}

int
face_camera_next_frame (FaceCamera *self, uint8_t *dst, int timeout_ms, int64_t *timestamp_ns)
{
	struct timespec until;
	int result = 1;

	clock_gettime (CLOCK_MONOTONIC, &until);
	until.tv_sec += timeout_ms / 1000;
	until.tv_nsec += (long) (timeout_ms % 1000) * 1000000;
	if (until.tv_nsec >= 1000000000) {
		until.tv_sec++;
		until.tv_nsec -= 1000000000;
	}
	pthread_mutex_lock (&self->lock);
	while (self->sequence == self->taken && !self->failed) {
		if (pthread_cond_timedwait (&self->cond, &self->lock, &until) == ETIMEDOUT) {
			result = 0;
			break;
		}
	}
	if (self->failed)
		result = -1;
	else if (result == 1) {
		memcpy (dst, self->frame, FRAME_SIZE);
		self->taken = self->sequence;
		if (timestamp_ns)
			*timestamp_ns = self->timestamp_ns;
	}
	pthread_mutex_unlock (&self->lock);
	return result;
}
