/*
 * Stand-in for libhybris' camera_compatibility_layer.h (not on the build host): the part
 * plinius-face's face-camera.c uses, with the same names, members and order as libhybris.
 * The functions are implemented by the test harness.
 */
#pragma once

#include <stdint.h>
#include <unistd.h>

typedef enum {
	PREVIEW_CALLBACK_DISABLED,
	PREVIEW_CALLBACK_ENABLED
} PreviewCallbackMode;

struct CameraControl;

typedef void (*on_msg_error) (void *context);
typedef void (*on_msg_shutter) (void *context);
typedef void (*on_msg_focus) (void *context);
typedef void (*on_msg_zoom) (void *context, int32_t new_zoom_level);
typedef void (*on_data_raw_image) (void *data, uint32_t data_size, void *context);
typedef void (*on_data_compressed_image) (void *data, uint32_t data_size, void *context);
typedef void (*on_preview_texture_needs_update) (void *context);
typedef void (*on_preview_frame) (void *data, uint32_t data_size, void *context);

struct CameraControlListener {
	on_msg_error on_msg_error_cb;
	on_msg_shutter on_msg_shutter_cb;
	on_msg_focus on_msg_focus_cb;
	on_msg_zoom on_msg_zoom_cb;
	on_data_raw_image on_data_raw_image_cb;
	on_data_compressed_image on_data_compressed_image_cb;
	on_preview_texture_needs_update on_preview_texture_needs_update_cb;
	void *context;
	on_preview_frame on_preview_frame_cb;
};

struct CameraControl *android_camera_connect_by_id (int32_t camera_id,
						    struct CameraControlListener *listener);
void android_camera_disconnect (struct CameraControl *control);
void android_camera_set_preview_texture (struct CameraControl *control, int texture_id);
void android_camera_update_preview_texture (struct CameraControl *control);
void android_camera_start_preview (struct CameraControl *control);
void android_camera_stop_preview (struct CameraControl *control);
int android_camera_set_preview_callback_mode (struct CameraControl *control, PreviewCallbackMode mode);
