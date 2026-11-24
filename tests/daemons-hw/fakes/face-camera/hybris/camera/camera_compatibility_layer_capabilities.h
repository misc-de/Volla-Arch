/*
 * Stand-in for libhybris' camera_compatibility_layer_capabilities.h (not on the build host):
 * the enums and functions face-camera.c uses, values as in libhybris.
 */
#pragma once

#include <stdint.h>

struct CameraControl;

typedef enum {
	FLASH_MODE_OFF,
	FLASH_MODE_AUTO,
	FLASH_MODE_ON,
	FLASH_MODE_TORCH,
	FLASH_MODE_RED_EYE
} FlashMode;

typedef enum {
	AUTO_FOCUS_MODE_OFF,
	AUTO_FOCUS_MODE_CONTINUOUS_VIDEO,
	AUTO_FOCUS_MODE_AUTO,
	AUTO_FOCUS_MODE_MACRO,
	AUTO_FOCUS_MODE_CONTINUOUS_PICTURE,
	AUTO_FOCUS_MODE_INFINITY
} AutoFocusMode;

int android_camera_get_number_of_devices (void);
int android_camera_get_device_info (int32_t camera_id, int *facing, int *orientation);
void android_camera_set_preview_size (struct CameraControl *control, int width, int height);
void android_camera_set_flash_mode (struct CameraControl *control, FlashMode mode);
void android_camera_set_auto_focus_mode (struct CameraControl *control, AutoFocusMode mode);
