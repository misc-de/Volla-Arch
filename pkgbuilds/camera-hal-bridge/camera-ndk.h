// SPDX-License-Identifier: MIT
// The Camera2 NDK and the image reader of Android's media NDK, for a glibc process: the libraries
// run inside the process through libhybris, so there is no linking against them. The types and
// calls are declared here as in the NDK's camera/Ndk*.h and media/NdkImage*.h, and looked up by
// camera_ndk_load.
#pragma once

#include <dlfcn.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>

#include <hybris/common/dlfcn.h>

typedef struct ACameraManager ACameraManager;
typedef struct ACameraMetadata ACameraMetadata;
typedef struct ACameraDevice ACameraDevice;
typedef struct ACaptureRequest ACaptureRequest;
typedef struct ACaptureSessionOutput ACaptureSessionOutput;
typedef struct ACaptureSessionOutputContainer ACaptureSessionOutputContainer;
typedef struct ACameraOutputTarget ACameraOutputTarget;
typedef struct ACameraCaptureSession ACameraCaptureSession;
typedef struct AImageReader AImageReader;
typedef struct AImage AImage;
typedef struct ANativeWindow ANativeWindow;

typedef struct {
	int numCameras;
	const char **cameraIds;
} ACameraIdList;

typedef struct {
	int32_t numerator;
	int32_t denominator;
} ACameraMetadata_rational;

typedef struct {
	uint32_t tag;
	uint8_t type;
	uint32_t count;
	union {
		const uint8_t *u8;
		const int32_t *i32;
		const float *f;
		const int64_t *i64;
		const double *d;
		const ACameraMetadata_rational *r;
	} data;
} ACameraMetadata_const_entry;

typedef struct {
	void *context;
	void (*onDisconnected)(void *context, ACameraDevice *device);
	void (*onError)(void *context, ACameraDevice *device, int error);
} ACameraDevice_StateCallbacks;

typedef struct {
	void *context;
	void (*onClosed)(void *context, ACameraCaptureSession *session);
	void (*onReady)(void *context, ACameraCaptureSession *session);
	void (*onActive)(void *context, ACameraCaptureSession *session);
} ACameraCaptureSession_stateCallbacks;

typedef struct {
	int64_t frameNumber;
	int reason;
	int sequenceId;
	int wasImageCaptured;
} ACameraCaptureFailure;

typedef struct {
	void *context;
	void (*onCaptureStarted)(void *context, ACameraCaptureSession *session,
				 const ACaptureRequest *request, int64_t timestamp);
	void (*onCaptureProgressed)(void *context, ACameraCaptureSession *session,
				    ACaptureRequest *request, const ACameraMetadata *result);
	void (*onCaptureCompleted)(void *context, ACameraCaptureSession *session,
				   ACaptureRequest *request, const ACameraMetadata *result);
	void (*onCaptureFailed)(void *context, ACameraCaptureSession *session,
				ACaptureRequest *request, ACameraCaptureFailure *failure);
	void (*onCaptureSequenceCompleted)(void *context, ACameraCaptureSession *session,
					   int sequenceId, int64_t frameNumber);
	void (*onCaptureSequenceAborted)(void *context, ACameraCaptureSession *session, int sequenceId);
	void (*onCaptureBufferLost)(void *context, ACameraCaptureSession *session,
				    ACaptureRequest *request, ANativeWindow *window, int64_t frameNumber);
} ACameraCaptureSession_captureCallbacks;

typedef struct {
	void *context;
	void (*onImageAvailable)(void *context, AImageReader *reader);
} AImageReader_ImageListener;

enum { TEMPLATE_PREVIEW = 1, TEMPLATE_STILL_CAPTURE = 2 };
enum { AIMAGE_FORMAT_YUV_420_888 = 0x23, AIMAGE_FORMAT_JPEG = 0x100 };
enum { METADATA_TYPE_BYTE = 0, METADATA_TYPE_INT32 = 1, METADATA_TYPE_RATIONAL = 5 };

/* Android's tags */
#define TAG_CONTROL_AE_EXPOSURE_COMPENSATION 0x00010001
#define TAG_CONTROL_AWB_MODE 0x0001000b
#define TAG_CONTROL_MODE 0x0001000f
#define TAG_CONTROL_SCENE_MODE 0x00010010
#define TAG_CONTROL_AE_COMPENSATION_RANGE 0x00010015
#define TAG_CONTROL_AE_COMPENSATION_STEP 0x00010016
#define TAG_CONTROL_AVAILABLE_SCENE_MODES 0x00010019
#define TAG_FLASH_MODE 0x00040002
#define TAG_JPEG_ORIENTATION 0x00070003
#define TAG_JPEG_QUALITY 0x00070004
#define TAG_LENS_FACING 0x00080005
#define TAG_SCALER_AVAILABLE_STREAM_CONFIGURATIONS 0x000d000a
#define TAG_SENSOR_ORIENTATION 0x000e000e
/* MediaTek's, as "dumpsys media.camera" lists them */
#define TAG_MTK_MFB_MODE 0x80040000
#define TAG_MTK_MFB_AVAILABLE 0x80040002

/* Values of the tags above */
enum { LENS_FACING_FRONT = 0, LENS_FACING_BACK = 1 };
enum { CONTROL_MODE_AUTO = 1, CONTROL_MODE_USE_SCENE_MODE = 2 };
enum { FLASH_MODE_OFF = 0 };
#define STREAM_FORMAT_BLOB 0x21
#define STREAM_FORMAT_YUV_420_888 0x23

static ACameraManager *(*ACameraManager_create)(void);
static int (*ACameraManager_getCameraIdList)(ACameraManager *, ACameraIdList **);
static void (*ACameraManager_deleteCameraIdList)(ACameraIdList *);
static int (*ACameraManager_getCameraCharacteristics)(ACameraManager *, const char *, ACameraMetadata **);
static int (*ACameraManager_openCamera)(ACameraManager *, const char *, ACameraDevice_StateCallbacks *,
					ACameraDevice **);
static int (*ACameraMetadata_getConstEntry)(const ACameraMetadata *, uint32_t, ACameraMetadata_const_entry *);
static void (*ACameraMetadata_free)(ACameraMetadata *);
static int (*ACameraDevice_close)(ACameraDevice *);
static int (*ACameraDevice_createCaptureRequest)(const ACameraDevice *, int, ACaptureRequest **);
static int (*ACameraDevice_createCaptureSession)(ACameraDevice *, const ACaptureSessionOutputContainer *,
						 const ACameraCaptureSession_stateCallbacks *,
						 ACameraCaptureSession **);
static int (*ACaptureSessionOutputContainer_create)(ACaptureSessionOutputContainer **);
static void (*ACaptureSessionOutputContainer_free)(ACaptureSessionOutputContainer *);
static int (*ACaptureSessionOutputContainer_add)(ACaptureSessionOutputContainer *, const ACaptureSessionOutput *);
static int (*ACaptureSessionOutput_create)(ANativeWindow *, ACaptureSessionOutput **);
static void (*ACaptureSessionOutput_free)(ACaptureSessionOutput *);
static int (*ACameraOutputTarget_create)(ANativeWindow *, ACameraOutputTarget **);
static void (*ACameraOutputTarget_free)(ACameraOutputTarget *);
static int (*ACaptureRequest_addTarget)(ACaptureRequest *, const ACameraOutputTarget *);
static int (*ACaptureRequest_setEntry_i32)(ACaptureRequest *, uint32_t, uint32_t, const int32_t *);
static int (*ACaptureRequest_setEntry_u8)(ACaptureRequest *, uint32_t, uint32_t, const uint8_t *);
static void (*ACaptureRequest_free)(ACaptureRequest *);
static int (*ACameraCaptureSession_setRepeatingRequest)(ACameraCaptureSession *,
							ACameraCaptureSession_captureCallbacks *, int,
							ACaptureRequest **, int *);
static int (*ACameraCaptureSession_stopRepeating)(ACameraCaptureSession *);
static int (*ACameraCaptureSession_capture)(ACameraCaptureSession *, ACameraCaptureSession_captureCallbacks *,
					    int, ACaptureRequest **, int *);
static void (*ACameraCaptureSession_close)(ACameraCaptureSession *);

static int (*AImageReader_new)(int32_t, int32_t, int32_t, int32_t, AImageReader **);
static void (*AImageReader_delete)(AImageReader *);
static int (*AImageReader_getWindow)(AImageReader *, ANativeWindow **);
static int (*AImageReader_setImageListener)(AImageReader *, AImageReader_ImageListener *);
static int (*AImageReader_acquireLatestImage)(AImageReader *, AImage **);
static int (*AImage_getWidth)(const AImage *, int32_t *);
static int (*AImage_getHeight)(const AImage *, int32_t *);
static int (*AImage_getPlaneData)(const AImage *, int, uint8_t **, int *);
static int (*AImage_getPlaneRowStride)(const AImage *, int, int32_t *);
static int (*AImage_getPlanePixelStride)(const AImage *, int, int32_t *);
static void (*AImage_delete)(AImage *);

/* Resolves a symbol into the function pointer of the same name */
#define CAMERA_NDK_RESOLVE(lib, name)                                                          \
	do {                                                                                   \
		*(void **)&name = hybris_dlsym(lib, #name);                                    \
		if (!name) {                                                                   \
			fprintf(stderr, "camera-hal-bridge: no symbol %s\n", #name);           \
			return false;                                                          \
		}                                                                              \
	} while (0)

/* Loads the libraries and starts the binder thread pool. Android apps get that pool from Zygote;
 * a plain process has to start its own. The BufferQueue of an image reader lives in this process
 * and the camera service calls into it for every buffer, and results and the news of a dying
 * service come in the same way. Without the pool no frame ever arrives, and the flush on closing
 * hangs until the service's watchdog aborts the whole service. */
static bool camera_ndk_load(void)
{
	void *camera = hybris_dlopen("libcamera2ndk.so", RTLD_NOW);
	void *media = hybris_dlopen("libmediandk.so", RTLD_NOW);
	void *binder = hybris_dlopen("libbinder_ndk.so", RTLD_NOW);
	if (!camera || !media || !binder) {
		fprintf(stderr, "camera-hal-bridge: cannot load the NDK: %s\n", hybris_dlerror());
		return false;
	}
	CAMERA_NDK_RESOLVE(camera, ACameraManager_create);
	CAMERA_NDK_RESOLVE(camera, ACameraManager_getCameraIdList);
	CAMERA_NDK_RESOLVE(camera, ACameraManager_deleteCameraIdList);
	CAMERA_NDK_RESOLVE(camera, ACameraManager_getCameraCharacteristics);
	CAMERA_NDK_RESOLVE(camera, ACameraManager_openCamera);
	CAMERA_NDK_RESOLVE(camera, ACameraMetadata_getConstEntry);
	CAMERA_NDK_RESOLVE(camera, ACameraMetadata_free);
	CAMERA_NDK_RESOLVE(camera, ACameraDevice_close);
	CAMERA_NDK_RESOLVE(camera, ACameraDevice_createCaptureRequest);
	CAMERA_NDK_RESOLVE(camera, ACameraDevice_createCaptureSession);
	CAMERA_NDK_RESOLVE(camera, ACaptureSessionOutputContainer_create);
	CAMERA_NDK_RESOLVE(camera, ACaptureSessionOutputContainer_free);
	CAMERA_NDK_RESOLVE(camera, ACaptureSessionOutputContainer_add);
	CAMERA_NDK_RESOLVE(camera, ACaptureSessionOutput_create);
	CAMERA_NDK_RESOLVE(camera, ACaptureSessionOutput_free);
	CAMERA_NDK_RESOLVE(camera, ACameraOutputTarget_create);
	CAMERA_NDK_RESOLVE(camera, ACameraOutputTarget_free);
	CAMERA_NDK_RESOLVE(camera, ACaptureRequest_addTarget);
	CAMERA_NDK_RESOLVE(camera, ACaptureRequest_setEntry_i32);
	CAMERA_NDK_RESOLVE(camera, ACaptureRequest_setEntry_u8);
	CAMERA_NDK_RESOLVE(camera, ACaptureRequest_free);
	CAMERA_NDK_RESOLVE(camera, ACameraCaptureSession_setRepeatingRequest);
	CAMERA_NDK_RESOLVE(camera, ACameraCaptureSession_stopRepeating);
	CAMERA_NDK_RESOLVE(camera, ACameraCaptureSession_capture);
	CAMERA_NDK_RESOLVE(camera, ACameraCaptureSession_close);
	CAMERA_NDK_RESOLVE(media, AImageReader_new);
	CAMERA_NDK_RESOLVE(media, AImageReader_delete);
	CAMERA_NDK_RESOLVE(media, AImageReader_getWindow);
	CAMERA_NDK_RESOLVE(media, AImageReader_setImageListener);
	CAMERA_NDK_RESOLVE(media, AImageReader_acquireLatestImage);
	CAMERA_NDK_RESOLVE(media, AImage_getWidth);
	CAMERA_NDK_RESOLVE(media, AImage_getHeight);
	CAMERA_NDK_RESOLVE(media, AImage_getPlaneData);
	CAMERA_NDK_RESOLVE(media, AImage_getPlaneRowStride);
	CAMERA_NDK_RESOLVE(media, AImage_getPlanePixelStride);
	CAMERA_NDK_RESOLVE(media, AImage_delete);

	void (*start_thread_pool)(void);
	*(void **)&start_thread_pool = hybris_dlsym(binder, "ABinderProcess_startThreadPool");
	if (!start_thread_pool) {
		fprintf(stderr, "camera-hal-bridge: no symbol ABinderProcess_startThreadPool\n");
		return false;
	}
	start_thread_pool();
	return true;
}
