// SPDX-License-Identifier: MIT
// camera-hal-bridge: the Android cameras (CameraService in the container, reached through the
// Camera2 NDK running in this process by libhybris) as PipeWire camera nodes, which camera apps
// such as GNOME Snapshot find through the camera portal.
//
// One node per camera the HAL reports. A camera is only opened while its node is streaming. The
// frames come as YUV images through an image reader, go out as NV21 and drive the graph.
//
// Orientation: the sensors are mounted rotated and apps such as Snapshot do not rotate by
// themselves, so each frame is turned upright for the phone's natural (portrait) orientation by
// the sensor orientation of the HAL. While a camera streams, the accelerometer (iio-sensor-proxy
// on the system bus) tells how the phone is held; that rotation goes out as video transform
// metadata, which pipewiresrc turns into the image-orientation tag: the viewfinder rotates by it,
// photos get it as EXIF orientation and videos as rotation, so a landscape shot is landscape.
//
// Whether the front camera is on is told on the session bus (org.plinius.CameraBridge, property
// Active), so that the shell can show it: Phosh lights the notch while it runs (phosh-plinius).
// Not only our own streams count: other users of CameraService (face unlock, a root tool) open
// the sensor past the bridge, so the runtime power state of its CSI port in the kernel counts too.
//
// Photos: the nodes carry preview frames (1280x720), far below what the sensors give. A camera
// app takes a real photo through TakePicture on the same bus object: the streaming camera shoots
// a still through the HAL's capture pipeline at the sensor's native size and the bridge writes
// the JPEG where the app asked, which has to be in the user's pictures folder. MediaTek's multi-frame noise reduction runs on these stills
// (NoiseReduction), which takes several frames but keeps the preview running. The scene mode,
// white balance and exposure compensation (SceneMode, WhiteBalance, ExposureCompensation) apply
// to every camera, live to the one streaming.
//
// Uses per app: each time a camera starts streaming, the apps it streams to are told to
// plinius-appaccess (CountUse on the system bus), which counts them for the app's page in GNOME
// Settings. The app is the portal's app id of the consuming client (pipewire.access.portal.app_id,
// read by binding the client) or, without one, its process. Who may use the cameras at all is
// decided before: the camera portal per app and WirePlumber (find-plinius-camera-access.lua).
//
// A photo only for who gets the stream: TakePicture is refused unless the caller is a client the
// camera streams to, the same process or the same app. The caller's app is the application id of
// its cgroup (app-gnome-org.gnome.Snapshot-1234.scope, read as plinius-appaccessd reads it), the
// stream's app the portal's app id of a portal client, or the app of a plain client's process.
// As for plinius-appaccess, a native app can start a scope under any name and so pass for
// another one; a Flatpak app cannot.
#ifndef _GNU_SOURCE
#define _GNU_SOURCE /* renameat2 */
#endif
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/eventfd.h>
#include <time.h>
#include <unistd.h>

#include <pipewire/pipewire.h>
#include <spa/buffer/meta.h>
#include <spa/node/io.h>
#include <spa/param/video/format-utils.h>
#include <spa/utils/string.h>
#include <systemd/sd-bus.h>

#include "camera-ndk.h"

#define FPS 30
/* The stream sizes a camera can offer, as the sensor delivers them (landscape), from the
 * smallest; each one the HAL lists for YUV output is offered. The first is the default: apps that
 * take what comes get what they always got, a camera app picks a larger one for its videos. */
static const struct size {
	int width, height;
} stream_sizes[] = {{1280, 720}, {1920, 1080}, {3840, 2160}};
#define MAX_SIZES ((int)SPA_N_ELEMENTS(stream_sizes))
/* NV21: Y plane, then interleaved V/U */
#define NV21_SIZE(w, h) ((size_t)(w) * (h) * 3 / 2)
#define MAX_CAMERAS 8
/* How long to wait for the cameras at the start: 60 × 0.5 s */
#define CAMERA_WAIT_TRIES 60
#define CAMERA_WAIT_US 500000

#define SENSOR_PROXY "net.hadess.SensorProxy"
#define SENSOR_PROXY_PATH "/net/hadess/SensorProxy"

#define BRIDGE_NAME "org.plinius.CameraBridge"
#define BRIDGE_PATH "/org/plinius/CameraBridge"

#define JPEG_QUALITY 95
#define PREVIEW_IMAGES 4
#define STILL_IMAGES 2
#define CAPTURE_TIMEOUT_S 12 /* measured: 2.1 s for six frames of MFNR in the dark */
#define SESSION_CLOSE_TIMEOUT_S 5
#define OPEN_RETRIES_FAST 12 /* three seconds of tries before counting as really busy */
#define OPEN_RETRY_FAST_MS 250
#define OPEN_RETRY_SLOW_MS 1000

/* The CSI port of the front sensor (sensor1) on the Volla Phone Plinius: runtime "active" while
 * it streams, whoever opened it (measured: the back cameras use ports 0 and 3, nothing is active
 * without a camera). sysfs cannot be waited on for this, so it is read every FRONT_POLL_MS.
 * Missing elsewhere: own streams only. */
#define FRONT_PORT_STATUS                                                                      \
	"/sys/devices/platform/soc/1a00e000.seninf-top/1a00e000.seninf-top:seninf-csi-port-1/" \
	"power/runtime_status"
#define FRONT_POLL_MS 500

enum { FACING_BACK, FACING_FRONT };

/* A TakePicture call on its way: the bus thread asks, the camera thread shoots and answers */
enum capture_state { CAPTURE_IDLE, CAPTURE_REQUESTED, CAPTURE_TAKING, CAPTURE_DONE };

/* Everything that lives while a camera is open */
struct session {
	ACameraDevice *device;
	AImageReader *preview_reader, *still_reader;
	ACaptureSessionOutputContainer *outputs;
	ACaptureSessionOutput *preview_output, *still_output;
	ACameraOutputTarget *preview_target, *still_target;
	ACaptureRequest *preview_request, *still_request;
	ACameraCaptureSession *capture_session;
};

struct camera {
	struct bridge *bridge;
	char id[8]; /* Android camera id */
	int facing;
	char name[32];
	char description[32];
	struct pw_stream *stream;
	struct spa_io_position *position; /* graph clock, ours to keep while driving */
	int orientation; /* clockwise rotation that makes the sensor image upright: 0/90/180/270 */
	uint32_t width, height; /* node format, after the rotation */
	int sensor_width, sensor_height; /* the same as the sensor delivers it */
	struct size sizes[MAX_SIZES];	 /* what the node offers, from stream_sizes */
	int n_sizes;
	bool size_changed; /* a new format asks for another size while the camera is open */
	pthread_t thread;
	pthread_mutex_t lock;
	pthread_cond_t cond;
	bool active; /* node is streaming */
	bool quit;
	bool frame_ready;
	uint8_t *frame; /* latest preview frame */
	bool frame_format_logged;
	int picture_width, picture_height; /* still size */
	uint32_t scene_modes; /* bit per available Android scene mode */
	bool has_mfb; /* MediaTek's multi-frame modes are on offer */
	int ae_min, ae_max; /* exposure compensation range, in steps */
	double ae_step; /* EV per step, 0: no compensation */
	bool settings_dirty; /* a setting changed while open */
	bool device_lost; /* the device went away or the service died */
	bool session_closed;
	enum capture_state capture;
	sd_bus_message *capture_call; /* the TakePicture call to answer */
	char *capture_path;
	int capture_dir; /* the open folder of capture_path, until the call is answered */
	const char *capture_name; /* its name in there, part of capture_path */
	int capture_result; /* 0 or -errno, once CAPTURE_DONE */
	AImage *still; /* the still from the HAL, while CAPTURE_TAKING */
	bool still_failed;
	/* The NDK keeps pointers to these while the session lives */
	ACameraDevice_StateCallbacks device_callbacks;
	ACameraCaptureSession_stateCallbacks session_callbacks;
	ACameraCaptureSession_captureCallbacks still_callbacks;
	AImageReader_ImageListener preview_listener, still_listener;
};

/* What the registry says about the graph, as far as needed to find who a camera streams to */
enum watch_type { WATCH_NONE, WATCH_LINK, WATCH_NODE, WATCH_CLIENT };
#define MAX_WATCHED 512
#define APP_ID_SIZE 256
#define MAX_USES 16

struct watched {
	uint32_t id;
	enum watch_type type;
	uint32_t a, b;		  /* link: output node, input node; node: its client in a */
	uint32_t pid;		  /* client: pipewire.sec.pid */
	bool portal;		  /* client: opened by the portal for an app (pipewire.access=portal) */
	char app[APP_ID_SIZE];	  /* client: pipewire.access.portal.app_id, once its info came */
	struct pw_proxy *proxy;	  /* client of the camera portal, bound for its properties */
	struct spa_hook listener;
};

struct use {
	char app[APP_ID_SIZE];
	uint32_t pid;
};

struct consumers {
	struct pw_context *context;
	struct pw_core *core;
	struct pw_registry *registry;
	struct spa_hook registry_listener;
	struct watched items[MAX_WATCHED];
	/* Uses to tell plinius-appaccess: from the PipeWire thread to the reporting thread */
	pthread_mutex_t lock;
	pthread_cond_t cond;
	struct use queue[MAX_USES];
	int queued;
	bool quit;
	pthread_t thread;
};

struct bridge {
	struct pw_thread_loop *loop;
	struct consumers consumers;
	ACameraManager *manager;
	struct camera cameras[MAX_CAMERAS];
	int n_cameras;
	atomic_int streaming;	 /* number of streaming cameras */
	atomic_int front_streaming; /* of those, front cameras */
	atomic_bool front_on;	 /* what Active says: own front stream or the front port in use */
	atomic_int device_rotation; /* how the phone is turned from portrait, clockwise degrees */
	atomic_int scene_mode;	    /* SceneMode for all cameras */
	atomic_int white_balance;   /* WhiteBalance for all cameras */
	atomic_int exposure;	    /* ExposureCompensation for all cameras */
	atomic_int noise_reduction; /* NoiseReduction for all cameras */
	atomic_bool quit;
	int wake_fd; /* eventfd that wakes the orientation thread */
	pthread_t orientation_thread;
};

/* Rotates a w×h plane clockwise by degrees into dst, which is h×w for 90 and 270. Source pixel
 * (x, y) goes to dst[base + x * step_x + y * step_y]; tiles keep the strided side in cache. */
#define ROTATE_TILE 32
#define DEFINE_ROTATE(name, type)                                                              \
	static void name(const type *src, type *dst, int w, int h, int degrees)                \
	{                                                                                      \
		long base, step_x, step_y;                                                     \
		switch (degrees) {                                                             \
		case 90: base = h - 1; step_x = h; step_y = -1; break;                         \
		case 180: base = (long)w * h - 1; step_x = -1; step_y = -w; break;             \
		case 270: base = (long)(w - 1) * h; step_x = -h; step_y = 1; break;            \
		default: memcpy(dst, src, sizeof(type) * w * h); return;                       \
		}                                                                              \
		for (int ty = 0; ty < h; ty += ROTATE_TILE)                                    \
			for (int tx = 0; tx < w; tx += ROTATE_TILE)                            \
				for (int y = ty; y < ty + ROTATE_TILE && y < h; y++)           \
					for (int x = tx; x < tx + ROTATE_TILE && x < w; x++)   \
						dst[base + x * step_x + y * step_y] =          \
							src[(long)y * w + x];                  \
	}
DEFINE_ROTATE(rotate_plane8, uint8_t)   /* Y */
DEFINE_ROTATE(rotate_plane16, uint16_t) /* interleaved V/U pairs */

/* Runs in the stream's data loop. A driving stream has to keep the graph clock itself, or it
 * keeps the audio defaults (1024/48000 per cycle) that have nothing to do with the frames: the
 * buffers are stamped with clock.nsec, but pipewiresrc's GStreamer clock counts clock.position,
 * so video sinks see every frame as far too late and drop all but the first. NO_RATE makes
 * followers derive their ticks from clock.nsec, so timestamps and clock are both monotonic time. */
static int do_trigger(struct spa_loop *loop, bool async, uint32_t seq, const void *data, size_t size,
		      void *user_data)
{
	struct camera *c = user_data;
	struct spa_io_position *p = c->position;
	if (p && pw_stream_is_driving(c->stream)) {
		uint64_t now = pw_stream_get_nsec(c->stream);
		p->clock.nsec = now;
		p->clock.next_nsec = now + SPA_NSEC_PER_SEC / FPS;
		p->clock.delay = 0;
		p->clock.rate_diff = 1.0;
		SPA_FLAG_SET(p->clock.flags, SPA_IO_CLOCK_FLAG_NO_RATE);
	}
	pw_stream_trigger_process(c->stream);
	return 0;
}

/* The settings of the bus, each with the values for the HAL: the first one is the default */
static const char *const scene_modes[] = {"auto", "action", "night", "party", "sunset", "hdr"};
static const uint8_t scene_mode_values[] = {0, 2, 5, 14, 10, 18}; /* CONTROL_SCENE_MODE_* */
static const char *const white_balances[] = {"auto", "daylight", "cloudy-daylight", "fluorescent",
					     "incandescent"};
static const uint8_t white_balance_values[] = {1, 5, 6, 3, 2}; /* CONTROL_AWB_MODE_* */
static const char *const exposures[] = {"0", "-2", "-1", "+1", "+2"};
static const int exposure_values[] = {0, -2, -1, 1, 2}; /* EV */
static const char *const noise_reductions[] = {"auto", "on", "off"};
static const int32_t noise_reduction_values[] = {255, 1, 0}; /* com.mediatek.mfnrfeature.mfbmode */

/* Turns an image of the HAL's YUV 4:2:0 into our NV21, whatever its strides. In practice the HAL
 * delivers NV21 with a stride of its own, then the chroma rows can be copied whole. */
static bool copy_image(struct camera *c, const AImage *image, uint8_t *dst)
{
	int32_t width = 0, height = 0, y_stride, u_stride, v_stride, y_pixel, chroma_pixel;
	uint8_t *y, *u, *v;
	int y_size, u_size, v_size;
	const int W = c->sensor_width, H = c->sensor_height;
	/* An image of the size before a format change is left out */
	if (AImage_getWidth(image, &width) != 0 || AImage_getHeight(image, &height) != 0 || width != W ||
	    height != H || AImage_getPlaneData(image, 0, &y, &y_size) != 0 ||
	    AImage_getPlaneData(image, 1, &u, &u_size) != 0 || AImage_getPlaneData(image, 2, &v, &v_size) != 0 ||
	    AImage_getPlaneRowStride(image, 0, &y_stride) != 0 || AImage_getPlaneRowStride(image, 1, &u_stride) != 0 ||
	    AImage_getPlaneRowStride(image, 2, &v_stride) != 0 || AImage_getPlanePixelStride(image, 0, &y_pixel) != 0 ||
	    AImage_getPlanePixelStride(image, 1, &chroma_pixel) != 0)
		return false;
	if (!c->frame_format_logged) {
		pw_log_info("%s: frames %dx%d, stride %d/%d/%d, chroma pixel stride %d, %s", c->name, width,
			    height, y_stride, u_stride, v_stride, chroma_pixel,
			    v + 1 == u ? "NV21" : "other layout");
		c->frame_format_logged = true;
	}
	/* A plane's reported size ends at its last pixel, so a whole stride may not fit in the last row */
	if (y_size < (long)y_stride * (H - 1) + W ||
	    u_size < (long)u_stride * (H / 2 - 1) + (W / 2 - 1) * chroma_pixel + 1 ||
	    v_size < (long)v_stride * (H / 2 - 1) + (W / 2 - 1) * chroma_pixel + 1)
		return false;
	for (int row = 0; row < H; row++)
		memcpy(dst + (long)row * W, y + (long)row * y_stride, W);
	dst += (long)W * H;
	for (int row = 0; row < H / 2; row++) {
		uint8_t *line = dst + (long)row * W;
		const uint8_t *u_line = u + (long)row * u_stride;
		const uint8_t *v_line = v + (long)row * v_stride;
		if (chroma_pixel == 2 && v_line + 1 == u_line) {
			memcpy(line, v_line, W);
			continue;
		}
		for (int x = 0; x < W / 2; x++) {
			line[2 * x] = v_line[x * chroma_pixel];
			line[2 * x + 1] = u_line[x * chroma_pixel];
		}
	}
	return true;
}

/* A preview frame, on the image reader's thread */
static void on_preview_image(void *context, AImageReader *reader)
{
	struct camera *c = context;
	AImage *image = NULL;
	if (AImageReader_acquireLatestImage(reader, &image) != 0 || !image)
		return;
	pthread_mutex_lock(&c->lock);
	bool copied = copy_image(c, image, c->frame);
	c->frame_ready = copied;
	pthread_mutex_unlock(&c->lock);
	AImage_delete(image);
	/* The stream drives its graph with each camera frame */
	if (copied)
		pw_loop_invoke(pw_stream_get_data_loop(c->stream), do_trigger, 0, NULL, 0, false, c);
}

/* The JPEG of a still, on the image reader's thread. One that comes after its call gave up is
 * only released. */
static void on_still_image(void *context, AImageReader *reader)
{
	struct camera *c = context;
	AImage *image = NULL;
	if (AImageReader_acquireLatestImage(reader, &image) != 0 || !image)
		return;
	pthread_mutex_lock(&c->lock);
	if (c->capture == CAPTURE_TAKING && !c->still) {
		c->still = image;
		image = NULL;
		pthread_cond_signal(&c->cond);
	}
	pthread_mutex_unlock(&c->lock);
	if (image)
		AImage_delete(image);
}

static void on_still_failed(void *context, ACameraCaptureSession *session, ACaptureRequest *request,
			    ACameraCaptureFailure *failure)
{
	struct camera *c = context;
	pw_log_warn("%s: still failed, reason %d", c->name, failure->reason);
	pthread_mutex_lock(&c->lock);
	c->still_failed = true;
	pthread_cond_signal(&c->cond);
	pthread_mutex_unlock(&c->lock);
}

static void device_gone(struct camera *c)
{
	pthread_mutex_lock(&c->lock);
	c->device_lost = true;
	pthread_cond_signal(&c->cond);
	pthread_mutex_unlock(&c->lock);
}

static void on_device_disconnected(void *context, ACameraDevice *device)
{
	struct camera *c = context;
	pw_log_warn("%s: camera disconnected", c->name);
	device_gone(c);
}

static void on_device_error(void *context, ACameraDevice *device, int error)
{
	struct camera *c = context;
	pw_log_warn("%s: camera error %d", c->name, error);
	device_gone(c);
}

static void on_session_closed(void *context, ACameraCaptureSession *session)
{
	struct camera *c = context;
	pthread_mutex_lock(&c->lock);
	c->session_closed = true;
	pthread_cond_signal(&c->cond);
	pthread_mutex_unlock(&c->lock);
}

static void on_session_state(void *context, ACameraCaptureSession *session) {}

static void wake_orientation_thread(struct bridge *b);

/* Puts the settings of the bus into a request. A scene the camera does not offer is auto. */
static void apply_settings(struct camera *c, ACaptureRequest *request)
{
	struct bridge *b = c->bridge;
	uint8_t scene = scene_mode_values[atomic_load(&b->scene_mode)];
	if (scene >= 32 || !(c->scene_modes & (1u << scene)))
		scene = 0;
	uint8_t mode = scene ? CONTROL_MODE_USE_SCENE_MODE : CONTROL_MODE_AUTO;
	ACaptureRequest_setEntry_u8(request, TAG_CONTROL_MODE, 1, &mode);
	ACaptureRequest_setEntry_u8(request, TAG_CONTROL_SCENE_MODE, 1, &scene);
	uint8_t white_balance = white_balance_values[atomic_load(&b->white_balance)];
	ACaptureRequest_setEntry_u8(request, TAG_CONTROL_AWB_MODE, 1, &white_balance);
	uint8_t flash = FLASH_MODE_OFF; /* the flash button uses the LED past the camera */
	ACaptureRequest_setEntry_u8(request, TAG_FLASH_MODE, 1, &flash);
	int32_t steps = 0;
	if (c->ae_step > 0) {
		double value = exposure_values[atomic_load(&b->exposure)] / c->ae_step;
		steps = (int32_t)(value + (value < 0 ? -0.5 : 0.5));
		steps = steps < c->ae_min ? c->ae_min : steps > c->ae_max ? c->ae_max : steps;
	}
	ACaptureRequest_setEntry_i32(request, TAG_CONTROL_AE_EXPOSURE_COMPENSATION, 1, &steps);
}

/* The JPEG rotation the HAL wants: the sensor's mounting plus how the phone is held, seen from the
 * front camera the other way round */
static int picture_rotation(struct camera *c)
{
	int device = atomic_load(&c->bridge->device_rotation);
	if (c->facing == FACING_FRONT)
		return (c->orientation - device + 360) % 360;
	return (c->orientation + device) % 360;
}

#define PART_SUFFIX ".part"

/* Writes the JPEG as name.part in the folder dir first, so the app never sees half a file. Never
 * onto an existing file, nor through a symlink: both the part and the name must be new. */
static int write_jpeg(int dir, const char *name, const uint8_t *data, size_t size)
{
	char tmp[NAME_MAX + 1];
	if (snprintf(tmp, sizeof(tmp), "%s" PART_SUFFIX, name) >= (int)sizeof(tmp))
		return -ENAMETOOLONG;
	int fd = openat(dir, tmp, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0644);
	if (fd < 0)
		return -errno;
	size_t done = 0;
	while (done < size) {
		ssize_t n = write(fd, data + done, size - done);
		if (n < 0 && errno == EINTR)
			continue;
		if (n <= 0) {
			int res = n < 0 ? -errno : -EIO;
			close(fd);
			unlinkat(dir, tmp, 0);
			return res;
		}
		done += n;
	}
	if (close(fd) < 0 || renameat2(dir, tmp, dir, name, RENAME_NOREPLACE) < 0) {
		int res = -errno;
		unlinkat(dir, tmp, 0);
		return res;
	}
	return 0;
}

/* The user's pictures folder as GLib reads it, so where Snapshot stores its photos: XDG_PICTURES_DIR
 * in $XDG_CONFIG_HOME/user-dirs.dirs ("$HOME/..." or an absolute path, the last line counts), else
 * ~/Pictures; without trailing slashes. False without a home, or for a folder that is the home or /
 * itself: no photo goes anywhere then. */
static bool pictures_dir(char *dir, size_t size)
{
	const char *home = getenv("HOME"), *config = getenv("XDG_CONFIG_HOME");
	if (!home || home[0] != '/')
		return false;
	char file[PATH_MAX];
	if (config && config[0] == '/')
		snprintf(file, sizeof(file), "%s/user-dirs.dirs", config);
	else
		snprintf(file, sizeof(file), "%s/.config/user-dirs.dirs", home);
	bool ok = snprintf(dir, size, "%s/Pictures", home) < (int)size;
	FILE *f = fopen(file, "re");
	char *line = NULL;
	size_t room = 0;
	while (f && getline(&line, &room, f) > 0) {
		char *p = line + strspn(line, " \t"), *end;
		if (strncmp(p, "XDG_PICTURES_DIR", 16) != 0)
			continue;
		p += 16;
		p += strspn(p, " \t");
		if (*p != '=')
			continue;
		p++;
		p += strspn(p, " \t");
		if (*p != '"' || !(end = strrchr(++p, '"')))
			continue;
		*end = '\0';
		if (strncmp(p, "$HOME", 5) == 0)
			ok = snprintf(dir, size, "%s%s%s", home, p[5] == '/' ? "" : "/", p + 5) < (int)size;
		else if (p[0] == '/')
			ok = snprintf(dir, size, "%s", p) < (int)size;
	}
	free(line);
	if (f)
		fclose(f);
	size_t n = strlen(dir), h = strlen(home);
	while (n > 1 && dir[n - 1] == '/')
		dir[--n] = '\0';
	while (h > 1 && home[h - 1] == '/')
		h--;
	return ok && strcmp(dir, "/") != 0 && !(n == h && strncmp(dir, home, h) == 0);
}

/* Where TakePicture may put a photo: a JPEG (.jpg, .jpeg) in the user's pictures folder or a folder
 * below it, as Snapshot stores its photos (Pictures/Camera). The folders below the pictures folder
 * are opened one by one without following a symlink, so a caller that may write there (an app with
 * access to the pictures only) cannot lead the photo elsewhere, ~/.ssh say; the pictures folder
 * itself is the user's choice and may be a symlink (onto the memory card). Gives the open folder
 * and where the name starts in path, or -errno: -EACCES for any other path. */
static int picture_target(const char *path, int *dir, size_t *name)
{
	char base[PATH_MAX];
	if (!pictures_dir(base, sizeof(base)))
		return -EACCES;
	size_t n = strlen(base);
	if (strncmp(path, base, n) != 0 || path[n] != '/')
		return -EACCES;
	const char *file = strrchr(path, '/') + 1;
	size_t len = strlen(file);
	if (!(len > 4 && strcasecmp(file + len - 4, ".jpg") == 0) && !(len > 5 && strcasecmp(file + len - 5, ".jpeg") == 0))
		return -EACCES;
	if (len + strlen(PART_SUFFIX) > NAME_MAX)
		return -ENAMETOOLONG;
	int fd = open(base, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (fd < 0)
		return -errno;
	for (const char *p = path + n + 1; p < file;) {
		const char *end = strchr(p, '/');
		size_t l = (size_t)(end - p);
		char part[NAME_MAX + 1];
		if (l == 0 || l > NAME_MAX || strncmp(p, ".", l) == 0 || strncmp(p, "..", l) == 0) {
			close(fd);
			return -EACCES;
		}
		memcpy(part, p, l);
		part[l] = '\0';
		int next = openat(fd, part, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
		int res = -errno;
		close(fd);
		if (next < 0)
			return res;
		fd = next;
		p = end + 1;
	}
	*dir = fd;
	*name = (size_t)(file - path);
	return 0;
}

/* Shoots a still for the pending TakePicture call, called and returning with c->lock held. The
 * preview keeps running on the HAL's side and the still request feeds it too. */
static int take_picture(struct camera *c, struct session *s)
{
	struct bridge *b = c->bridge;
	c->capture = CAPTURE_TAKING;
	c->still = NULL;
	c->still_failed = false;
	pthread_mutex_unlock(&c->lock);

	struct timespec started;
	clock_gettime(CLOCK_MONOTONIC, &started);
	apply_settings(c, s->still_request);
	uint8_t quality = JPEG_QUALITY;
	ACaptureRequest_setEntry_u8(s->still_request, TAG_JPEG_QUALITY, 1, &quality);
	int32_t rotation = picture_rotation(c);
	ACaptureRequest_setEntry_i32(s->still_request, TAG_JPEG_ORIENTATION, 1, &rotation);
	if (c->has_mfb) {
		int32_t mfb = noise_reduction_values[atomic_load(&b->noise_reduction)];
		ACaptureRequest_setEntry_i32(s->still_request, TAG_MTK_MFB_MODE, 1, &mfb);
	}
	int res = ACameraCaptureSession_capture(s->capture_session, &c->still_callbacks, 1, &s->still_request, NULL);

	pthread_mutex_lock(&c->lock);
	if (res != 0) {
		pw_log_warn("%s: cannot request a still: %d", c->name, res);
		c->still_failed = true;
	}
	struct timespec until;
	clock_gettime(CLOCK_REALTIME, &until);
	until.tv_sec += CAPTURE_TIMEOUT_S;
	int wait = 0;
	while (!c->still && !c->still_failed && wait != ETIMEDOUT && c->active && !c->quit && !c->device_lost)
		wait = pthread_cond_timedwait(&c->cond, &c->lock, &until);
	AImage *image = c->still;
	c->still = NULL;
	pthread_mutex_unlock(&c->lock);

	int result;
	uint8_t *data = NULL;
	int size = 0;
	if (image && AImage_getPlaneData(image, 0, &data, &size) == 0 && size > 0) {
		result = write_jpeg(c->capture_dir, c->capture_name, data, size);
		struct timespec now;
		clock_gettime(CLOCK_MONOTONIC, &now);
		pw_log_info("%s: picture of %d bytes in %ld ms to %s: %s", c->name, size,
			    (now.tv_sec - started.tv_sec) * 1000 + (now.tv_nsec - started.tv_nsec) / 1000000,
			    c->capture_path, result ? strerror(-result) : "ok");
	} else {
		result = wait == ETIMEDOUT ? -ETIMEDOUT : image ? -EIO : -ECANCELED;
		pw_log_warn("%s: no picture from the HAL: %s", c->name, strerror(-result));
	}
	if (image)
		AImage_delete(image);
	pthread_mutex_lock(&c->lock);
	return result;
}

/* Hands a finished capture to the bus thread, with c->lock held */
static void finish_capture(struct camera *c, int result)
{
	c->capture_result = result;
	c->capture = CAPTURE_DONE;
	wake_orientation_thread(c->bridge);
}

/* Closes what open_session got, in the order the HAL needs: the session stops and drains while the
 * device is there, the device goes while the readers can still take its last buffers. Called
 * without c->lock. */
static void close_session(struct camera *c, struct session *s)
{
	if (s->capture_session) {
		ACameraCaptureSession_stopRepeating(s->capture_session);
		pthread_mutex_lock(&c->lock);
		c->session_closed = false;
		pthread_mutex_unlock(&c->lock);
		ACameraCaptureSession_close(s->capture_session);
		struct timespec until;
		clock_gettime(CLOCK_REALTIME, &until);
		until.tv_sec += SESSION_CLOSE_TIMEOUT_S;
		pthread_mutex_lock(&c->lock);
		int wait = 0;
		while (!c->session_closed && !c->device_lost && wait != ETIMEDOUT)
			wait = pthread_cond_timedwait(&c->cond, &c->lock, &until);
		pthread_mutex_unlock(&c->lock);
		if (wait == ETIMEDOUT)
			pw_log_warn("%s: the session did not close in time", c->name);
	}
	if (s->device)
		ACameraDevice_close(s->device);
	if (s->still_request)
		ACaptureRequest_free(s->still_request);
	if (s->preview_request)
		ACaptureRequest_free(s->preview_request);
	if (s->still_target)
		ACameraOutputTarget_free(s->still_target);
	if (s->preview_target)
		ACameraOutputTarget_free(s->preview_target);
	if (s->still_output)
		ACaptureSessionOutput_free(s->still_output);
	if (s->preview_output)
		ACaptureSessionOutput_free(s->preview_output);
	if (s->outputs)
		ACaptureSessionOutputContainer_free(s->outputs);
	if (s->still_reader)
		AImageReader_delete(s->still_reader);
	if (s->preview_reader)
		AImageReader_delete(s->preview_reader);
	memset(s, 0, sizeof(*s));
}

/* Opens the camera with a preview and a still stream and starts the preview. The still stream is
 * there from the start: the HAL picks its sensor mode and capture pipeline when the session is
 * configured, and adding it later would mean a new session for each photo. */
static bool open_session(struct camera *c, struct session *s)
{
	ANativeWindow *preview_window, *still_window;
	const char *step;
#define STEP(call)                                                                             \
	do {                                                                                   \
		step = #call;                                                                  \
		int res_ = (call);                                                             \
		if (res_ != 0) {                                                               \
			pw_log_info("%s: %s: %d", c->name, step, res_);                        \
			return false;                                                          \
		}                                                                              \
	} while (0)
	STEP(ACameraManager_openCamera(c->bridge->manager, c->id, &c->device_callbacks, &s->device));
	pthread_mutex_lock(&c->lock);
	int width = c->sensor_width, height = c->sensor_height;
	c->size_changed = false;
	pthread_mutex_unlock(&c->lock);
	STEP(AImageReader_new(width, height, AIMAGE_FORMAT_YUV_420_888, PREVIEW_IMAGES, &s->preview_reader));
	STEP(AImageReader_new(c->picture_width, c->picture_height, AIMAGE_FORMAT_JPEG, STILL_IMAGES, &s->still_reader));
	STEP(AImageReader_setImageListener(s->preview_reader, &c->preview_listener));
	STEP(AImageReader_setImageListener(s->still_reader, &c->still_listener));
	STEP(AImageReader_getWindow(s->preview_reader, &preview_window));
	STEP(AImageReader_getWindow(s->still_reader, &still_window));
	STEP(ACaptureSessionOutputContainer_create(&s->outputs));
	STEP(ACaptureSessionOutput_create(preview_window, &s->preview_output));
	STEP(ACaptureSessionOutput_create(still_window, &s->still_output));
	STEP(ACaptureSessionOutputContainer_add(s->outputs, s->preview_output));
	STEP(ACaptureSessionOutputContainer_add(s->outputs, s->still_output));
	STEP(ACameraOutputTarget_create(preview_window, &s->preview_target));
	STEP(ACameraOutputTarget_create(still_window, &s->still_target));
	STEP(ACameraDevice_createCaptureRequest(s->device, TEMPLATE_PREVIEW, &s->preview_request));
	STEP(ACameraDevice_createCaptureRequest(s->device, TEMPLATE_STILL_CAPTURE, &s->still_request));
	STEP(ACaptureRequest_addTarget(s->preview_request, s->preview_target));
	STEP(ACaptureRequest_addTarget(s->still_request, s->preview_target));
	STEP(ACaptureRequest_addTarget(s->still_request, s->still_target));
	apply_settings(c, s->preview_request);
	STEP(ACameraDevice_createCaptureSession(s->device, s->outputs, &c->session_callbacks, &s->capture_session));
	STEP(ACameraCaptureSession_setRepeatingRequest(s->capture_session, NULL, 1, &s->preview_request, NULL));
#undef STEP
	return true;
}

/* Keeps the camera open while the node streams, and does what is asked of it meanwhile */
static void *camera_thread(void *data)
{
	struct camera *c = data;
	c->device_callbacks = (ACameraDevice_StateCallbacks){c, on_device_disconnected, on_device_error};
	c->session_callbacks = (ACameraCaptureSession_stateCallbacks){c, on_session_closed, on_session_state,
								      on_session_state};
	c->still_callbacks = (ACameraCaptureSession_captureCallbacks){.context = c, .onCaptureFailed = on_still_failed};
	c->preview_listener = (AImageReader_ImageListener){c, on_preview_image};
	c->still_listener = (AImageReader_ImageListener){c, on_still_image};

	int attempts = 0; /* failed openings in a row */
	pthread_mutex_lock(&c->lock);
	while (!c->quit) {
		if (!c->active) {
			attempts = 0;
			pthread_cond_wait(&c->cond, &c->lock);
			continue;
		}
		c->device_lost = false;
		c->settings_dirty = false;
		pthread_mutex_unlock(&c->lock);
		struct session s = {0};
		bool open = open_session(c, &s);
		if (open) {
			attempts = 0;
			pw_log_info("%s: open", c->name);
		} else if (attempts++ == OPEN_RETRIES_FAST) {
			/* Not when switching cameras: the last one is still closing for the first tries */
			pw_log_warn("%s: cannot open the camera, trying on", c->name);
		}
		pthread_mutex_lock(&c->lock);
		while (open && c->active && !c->quit && !c->device_lost && !c->size_changed) {
			if (c->capture == CAPTURE_REQUESTED) {
				finish_capture(c, take_picture(c, &s));
				continue;
			}
			if (c->settings_dirty) {
				c->settings_dirty = false;
				pthread_mutex_unlock(&c->lock);
				apply_settings(c, s.preview_request);
				ACameraCaptureSession_setRepeatingRequest(s.capture_session, NULL, 1,
									  &s.preview_request, NULL);
				pthread_mutex_lock(&c->lock);
				continue;
			}
			pthread_cond_wait(&c->cond, &c->lock);
		}
		/* A photo asked for while the camera closes, or that has waited long enough for it to open.
		 * One asked for between two tries is taken once the camera is there. */
		if (c->capture == CAPTURE_REQUESTED &&
		    (!c->active || c->quit || attempts > OPEN_RETRIES_FAST))
			finish_capture(c, -ENODEV);
		bool lost = c->device_lost, resized = c->size_changed;
		pthread_mutex_unlock(&c->lock);
		close_session(c, &s);
		if (open)
			pw_log_info("%s: closed%s", c->name, resized ? " for another size" : "");
		pthread_mutex_lock(&c->lock);
		if ((!open || lost) && c->active && !c->quit) {
			/* Busy (the last camera still closing, face unlock) or the service restarting: try
			 * again soon, and less often if that goes on */
			long delay_ms = attempts <= OPEN_RETRIES_FAST ? OPEN_RETRY_FAST_MS : OPEN_RETRY_SLOW_MS;
			struct timespec until;
			clock_gettime(CLOCK_REALTIME, &until);
			until.tv_nsec += delay_ms * 1000000;
			until.tv_sec += until.tv_nsec / 1000000000;
			until.tv_nsec %= 1000000000;
			pthread_cond_timedwait(&c->cond, &c->lock, &until);
		}
	}
	pthread_mutex_unlock(&c->lock);
	return NULL;
}

/* How far the upright portrait frame has to be turned clockwise for the way the phone is held.
 * Turning the phone turns the scene the other way in its frames; seen from the front camera,
 * which looks the opposite way, the same turn goes the other direction. */
static enum spa_meta_videotransform_value frame_transform(struct camera *c)
{
	int device = atomic_load(&c->bridge->device_rotation);
	int degrees = c->facing == FACING_FRONT ? (360 - device) % 360 : device;
	/* pipewiresrc maps TRANSFORMATION_90 to image-orientation rotate-90, a clockwise turn */
	switch (degrees) {
	case 90: return SPA_META_TRANSFORMATION_90;
	case 180: return SPA_META_TRANSFORMATION_180;
	case 270: return SPA_META_TRANSFORMATION_270;
	default: return SPA_META_TRANSFORMATION_None;
	}
}

static void on_process(void *data)
{
	struct camera *c = data;
	struct pw_buffer *b = pw_stream_dequeue_buffer(c->stream);
	if (!b)
		return;
	struct spa_data *d = &b->buffer->datas[0];
	pthread_mutex_lock(&c->lock);
	const int w = c->sensor_width, h = c->sensor_height;
	const size_t size = NV21_SIZE(w, h);
	/* A buffer of the size before a format change is left as it is */
	if (d->data && d->maxsize >= size) {
		uint8_t *dst = d->data;
		rotate_plane8(c->frame, dst, w, h, c->orientation);
		rotate_plane16((const uint16_t *)(c->frame + (size_t)w * h), (uint16_t *)(dst + (size_t)w * h), w / 2,
			       h / 2, c->orientation);
		c->frame_ready = false;
		d->chunk->offset = 0;
		d->chunk->stride = c->width;
		d->chunk->size = size;
	}
	pthread_mutex_unlock(&c->lock);
	struct spa_meta_videotransform *vt =
		spa_buffer_find_meta_data(b->buffer, SPA_META_VideoTransform, sizeof(*vt));
	if (vt)
		vt->transform = frame_transform(c);
	pw_stream_queue_buffer(c->stream, b);
}

static void wake_orientation_thread(struct bridge *b)
{
	uint64_t one = 1;
	if (write(b->wake_fd, &one, sizeof(one)) < 0)
		pw_log_warn("cannot wake the orientation thread: %m");
}

/* ----- Who a camera streams to, counted per app by plinius-appaccess ----- */

#define APPACCESS_NAME "org.plinius.AppAccess1"
#define APPACCESS_PATH "/org/plinius/AppAccess1"

static struct watched *watched_find(struct consumers *w, uint32_t id, enum watch_type type)
{
	for (int i = 0; i < MAX_WATCHED; i++)
		if (w->items[i].type == type && w->items[i].id == id)
			return &w->items[i];
	return NULL;
}

static struct watched *watched_new(struct consumers *w, uint32_t id, enum watch_type type)
{
	for (int i = 0; i < MAX_WATCHED; i++)
		if (w->items[i].type == WATCH_NONE) {
			struct watched *it = &w->items[i];
			memset(it, 0, sizeof(*it));
			it->id = id;
			it->type = type;
			return it;
		}
	return NULL;
}

static void on_client_info(void *data, const struct pw_client_info *info)
{
	struct watched *it = data;
	const char *app = info->props ? spa_dict_lookup(info->props, "pipewire.access.portal.app_id") : NULL;
	if (app)
		snprintf(it->app, sizeof(it->app), "%s", app);
}

static const struct pw_client_events client_events = {
	PW_VERSION_CLIENT_EVENTS,
	.info = on_client_info,
};

static void on_global(void *data, uint32_t id, uint32_t permissions, const char *type, uint32_t version,
		      const struct spa_dict *props)
{
	struct consumers *w = data;
	struct watched *it;
	if (!props)
		return;
	if (spa_streq(type, PW_TYPE_INTERFACE_Link)) {
		const char *out = spa_dict_lookup(props, PW_KEY_LINK_OUTPUT_NODE);
		const char *in = spa_dict_lookup(props, PW_KEY_LINK_INPUT_NODE);
		if (out && in && (it = watched_new(w, id, WATCH_LINK))) {
			it->a = strtoul(out, NULL, 10);
			it->b = strtoul(in, NULL, 10);
		}
	} else if (spa_streq(type, PW_TYPE_INTERFACE_Node)) {
		const char *client = spa_dict_lookup(props, PW_KEY_CLIENT_ID);
		if (client && (it = watched_new(w, id, WATCH_NODE)))
			it->a = strtoul(client, NULL, 10);
	} else if (spa_streq(type, PW_TYPE_INTERFACE_Client)) {
		const char *pid = spa_dict_lookup(props, PW_KEY_SEC_PID);
		const char *access = spa_dict_lookup(props, PW_KEY_ACCESS);
		if (!(it = watched_new(w, id, WATCH_CLIENT)))
			return;
		it->pid = pid ? strtoul(pid, NULL, 10) : 0;
		/* A portal client is opened by the portal for the app: its process is the portal's,
		 * the app is in the client's own properties */
		it->portal = spa_streq(access, "portal");
		if (it->portal) {
			it->proxy = pw_registry_bind(w->registry, id, PW_TYPE_INTERFACE_Client, PW_VERSION_CLIENT, 0);
			if (it->proxy)
				pw_proxy_add_object_listener(it->proxy, &it->listener, &client_events, it);
		}
	}
}

static void on_global_remove(void *data, uint32_t id)
{
	struct consumers *w = data;
	for (int i = 0; i < MAX_WATCHED; i++) {
		struct watched *it = &w->items[i];
		if (it->type == WATCH_NONE || it->id != id)
			continue;
		if (it->proxy) {
			spa_hook_remove(&it->listener);
			pw_proxy_destroy(it->proxy);
		}
		it->type = WATCH_NONE;
		it->proxy = NULL;
	}
}

static const struct pw_registry_events registry_events = {
	PW_VERSION_REGISTRY_EVENTS,
	.global = on_global,
	.global_remove = on_global_remove,
};

/* The client that item @i (a link from @node) streams to, or NULL */
static struct watched *consumer_of(struct consumers *w, int i, uint32_t node)
{
	struct watched *link = &w->items[i], *peer;
	if (link->type != WATCH_LINK || link->a != node)
		return NULL;
	peer = watched_find(w, link->b, WATCH_NODE);
	return peer ? watched_find(w, peer->a, WATCH_CLIENT) : NULL;
}

/* In the PipeWire thread, when camera @c starts streaming: queue each app it streams to */
static void report_consumers(struct camera *c)
{
	struct consumers *w = &c->bridge->consumers;
	uint32_t node = pw_stream_get_node_id(c->stream);
	if (!w->registry || node == SPA_ID_INVALID)
		return;
	for (int i = 0; i < MAX_WATCHED; i++) {
		struct watched *client = consumer_of(w, i, node);
		if (!client)
			continue;
		pthread_mutex_lock(&w->lock);
		if (w->queued < MAX_USES) {
			struct use *u = &w->queue[w->queued++];
			snprintf(u->app, sizeof(u->app), "%s", client->app);
			u->pid = client->pid;
			pthread_cond_signal(&w->cond);
		}
		pthread_mutex_unlock(&w->lock);
	}
}

/* Tells plinius-appaccess the queued uses; a bus call must not hold up the PipeWire thread */
static void *report_thread(void *data)
{
	struct consumers *w = data;
	sd_bus *bus = NULL;
	pthread_mutex_lock(&w->lock);
	while (!w->quit) {
		if (w->queued == 0) {
			pthread_cond_wait(&w->cond, &w->lock);
			continue;
		}
		struct use u = w->queue[0];
		memmove(&w->queue[0], &w->queue[1], (size_t)(w->queued - 1) * sizeof(w->queue[0]));
		w->queued--;
		pthread_mutex_unlock(&w->lock);
		sd_bus_error error = SD_BUS_ERROR_NULL;
		if (!bus && sd_bus_open_system(&bus) < 0)
			bus = NULL;
		/* Without plinius-appaccess nothing is counted, nothing else changes */
		if (bus && sd_bus_call_method(bus, APPACCESS_NAME, APPACCESS_PATH, APPACCESS_NAME, "CountUse", &error,
					      NULL, "sus", u.app, u.pid, "camera") < 0) {
			pw_log_debug("not counted (%s, %u): %s", u.app, u.pid, error.message);
			bus = sd_bus_unref(bus);
		}
		sd_bus_error_free(&error);
		pthread_mutex_lock(&w->lock);
	}
	pthread_mutex_unlock(&w->lock);
	sd_bus_unref(bus);
	return NULL;
}

/* With the thread loop locked; without it the cameras work the same, only nothing is counted */
static void consumers_start(struct bridge *b)
{
	struct consumers *w = &b->consumers;
	pthread_mutex_init(&w->lock, NULL);
	pthread_cond_init(&w->cond, NULL);
	w->context = pw_context_new(pw_thread_loop_get_loop(b->loop), NULL, 0);
	w->core = w->context ? pw_context_connect(w->context, NULL, 0) : NULL;
	w->registry = w->core ? pw_core_get_registry(w->core, PW_VERSION_REGISTRY, 0) : NULL;
	if (!w->registry) {
		pw_log_warn("no registry, camera uses are not counted per app");
		return;
	}
	pw_registry_add_listener(w->registry, &w->registry_listener, &registry_events, w);
	pthread_create(&w->thread, NULL, report_thread, w);
}

/* With the thread loop locked */
static void consumers_stop(struct bridge *b)
{
	struct consumers *w = &b->consumers;
	if (!w->registry)
		return;
	pthread_mutex_lock(&w->lock);
	w->quit = true;
	pthread_cond_signal(&w->cond);
	pthread_mutex_unlock(&w->lock);
	for (int i = 0; i < MAX_WATCHED; i++)
		if (w->items[i].proxy) {
			spa_hook_remove(&w->items[i].listener);
			pw_proxy_destroy(w->items[i].proxy);
			w->items[i].proxy = NULL;
		}
	spa_hook_remove(&w->registry_listener);
	pw_proxy_destroy((struct pw_proxy *)w->registry);
	pw_core_disconnect(w->core);
	pw_context_destroy(w->context);
	w->registry = NULL;
}

static void on_state_changed(void *data, enum pw_stream_state old, enum pw_stream_state state, const char *error)
{
	struct camera *c = data;
	bool active = state == PW_STREAM_STATE_STREAMING;
	pthread_mutex_lock(&c->lock);
	bool changed = c->active != active;
	c->active = active;
	pthread_cond_signal(&c->cond);
	pthread_mutex_unlock(&c->lock);
	if (changed && active)
		report_consumers(c);
	if (changed) {
		/* The accelerometer only runs while some camera streams */
		atomic_fetch_add(&c->bridge->streaming, active ? 1 : -1);
		if (c->facing == FACING_FRONT)
			atomic_fetch_add(&c->bridge->front_streaming, active ? 1 : -1);
		wake_orientation_thread(c->bridge);
	}
}

static void on_param_changed(void *data, uint32_t id, const struct spa_pod *param)
{
	struct camera *c = data;
	if (id != SPA_PARAM_Format || !param)
		return;
	/* The size the consumer picked; without one (or one not offered) the size stays */
	struct spa_video_info_raw info = {0};
	bool quarter_turn = c->orientation == 90 || c->orientation == 270;
	spa_format_video_raw_parse(param, &info);
	pthread_mutex_lock(&c->lock);
	for (int i = 0; i < c->n_sizes; i++) {
		struct size s = c->sizes[i];
		uint32_t w = quarter_turn ? s.height : s.width, h = quarter_turn ? s.width : s.height;
		if (info.size.width != w || info.size.height != h)
			continue;
		if (s.width != c->sensor_width || s.height != c->sensor_height) {
			c->sensor_width = s.width;
			c->sensor_height = s.height;
			c->width = w;
			c->height = h;
			c->size_changed = true;
			c->frame_ready = false;
			pthread_cond_signal(&c->cond);
			pw_log_info("%s: %ux%u", c->name, w, h);
		}
	}
	const int frame_size = (int)NV21_SIZE(c->sensor_width, c->sensor_height), stride = (int)c->width;
	pthread_mutex_unlock(&c->lock);
	/* One buffer holds a whole NV21 frame; the transform rides along with each buffer */
	uint8_t buffer[512];
	struct spa_pod_builder b = SPA_POD_BUILDER_INIT(buffer, sizeof(buffer));
	const struct spa_pod *params[2];
	params[0] = spa_pod_builder_add_object(
		&b, SPA_TYPE_OBJECT_ParamBuffers, SPA_PARAM_Buffers,
		SPA_PARAM_BUFFERS_buffers, SPA_POD_CHOICE_RANGE_Int(4, 2, 8),
		SPA_PARAM_BUFFERS_blocks, SPA_POD_Int(1),
		SPA_PARAM_BUFFERS_size, SPA_POD_Int(frame_size),
		SPA_PARAM_BUFFERS_stride, SPA_POD_Int(stride));
	params[1] = spa_pod_builder_add_object(
		&b, SPA_TYPE_OBJECT_ParamMeta, SPA_PARAM_Meta,
		SPA_PARAM_META_type, SPA_POD_Id(SPA_META_VideoTransform),
		SPA_PARAM_META_size, SPA_POD_Int(sizeof(struct spa_meta_videotransform)));
	pw_stream_update_params(c->stream, params, 2);
}

static void on_io_changed(void *data, uint32_t id, void *area, uint32_t size)
{
	struct camera *c = data;
	if (id == SPA_IO_Position)
		c->position = area;
}

static const struct pw_stream_events stream_events = {
	PW_VERSION_STREAM_EVENTS,
	.state_changed = on_state_changed,
	.io_changed = on_io_changed,
	.param_changed = on_param_changed,
	.process = on_process,
};

static int connect_camera(struct bridge *b, struct camera *c)
{
	bool quarter_turn = c->orientation == 90 || c->orientation == 270;
	c->bridge = b;
	if (c->n_sizes == 0) /* characteristics never read */
		c->sizes[c->n_sizes++] = stream_sizes[0];
	/* The smallest offered is the default; the frame has room for the largest */
	struct size first = c->sizes[0], largest = c->sizes[c->n_sizes - 1];
	c->sensor_width = first.width;
	c->sensor_height = first.height;
	c->width = quarter_turn ? first.height : first.width;
	c->height = quarter_turn ? first.width : first.height;
	char offered[64] = "";
	for (int i = 0; i < c->n_sizes; i++)
		snprintf(offered + strlen(offered), sizeof(offered) - strlen(offered), " %dx%d", c->sizes[i].width,
			 c->sizes[i].height);
	pw_log_info("%s: camera %s, sensor orientation %d, sizes%s", c->name, c->id, c->orientation, offered);
	c->frame = calloc(1, NV21_SIZE(largest.width, largest.height));
	pthread_mutex_init(&c->lock, NULL);
	pthread_cond_init(&c->cond, NULL);
	struct pw_properties *props = pw_properties_new(
		PW_KEY_MEDIA_CLASS, "Video/Source",
		PW_KEY_MEDIA_ROLE, "Camera",
		PW_KEY_NODE_NAME, c->name,
		PW_KEY_NODE_DESCRIPTION, c->description,
		/* Apps tell front from back by libcamera's property (Snapshot mirrors the front one) */
		"api.libcamera.location", c->facing == FACING_FRONT ? "front" : "back",
		NULL);
	c->stream = pw_stream_new_simple(pw_thread_loop_get_loop(b->loop), c->name, props, &stream_events, c);
	if (!c->stream || !c->frame)
		return -1;

	/* One format per size, the default first: as an enum of sizes, GStreamer would get a list of
	 * widths and one of heights, and with them sizes that do not exist */
	uint8_t buffer[1024];
	struct spa_pod_builder builder = SPA_POD_BUILDER_INIT(buffer, sizeof(buffer));
	const struct spa_pod *params[MAX_SIZES];
	for (int i = 0; i < c->n_sizes; i++)
		params[i] = spa_pod_builder_add_object(
			&builder, SPA_TYPE_OBJECT_Format, SPA_PARAM_EnumFormat,
			SPA_FORMAT_mediaType, SPA_POD_Id(SPA_MEDIA_TYPE_video),
			SPA_FORMAT_mediaSubtype, SPA_POD_Id(SPA_MEDIA_SUBTYPE_raw),
			SPA_FORMAT_VIDEO_format, SPA_POD_Id(SPA_VIDEO_FORMAT_NV21),
			SPA_FORMAT_VIDEO_size,
			SPA_POD_Rectangle(&SPA_RECTANGLE(quarter_turn ? c->sizes[i].height : c->sizes[i].width,
							 quarter_turn ? c->sizes[i].width : c->sizes[i].height)),
			SPA_FORMAT_VIDEO_framerate, SPA_POD_Fraction(&SPA_FRACTION(FPS, 1)));
	return pw_stream_connect(c->stream, PW_DIRECTION_OUTPUT, PW_ID_ANY,
				 PW_STREAM_FLAG_MAP_BUFFERS | PW_STREAM_FLAG_DRIVER, params, (uint32_t)c->n_sizes);
}

/* The Volla phone's HAL IDs, measured by the sensor that streams (ov64b40, gc32e1, gc08a3,
 * gc02m1 macro): the name shown in the apps, and the still size. Every sensor mode reads out
 * less than the largest JPEG the HAL offers (the 64MP camera reads 4624x3472, the 32MP front
 * camera 3264x2448, the macro camera 1408x1056), and anything larger is only scaled up, so
 * photos take the largest offered size up to the readout. IDs without an entry are named by
 * facing and shoot at the largest size offered. */
static const struct {
	const char *label;
	int picture_width, picture_height;
} camera_models[] = {
	[0] = {"Back Camera 64MP", 4608, 3456},
	[1] = {"Front Camera 32MP", 3264, 2448},
	[2] = {"Back Camera 8MP", 3264, 2448},
	[3] = {"Back Camera 2MP Macro", 1280, 960},
};

/* Reads what the bridge needs to know about a camera from its characteristics */
static void read_characteristics(struct camera *c, const ACameraMetadata *meta)
{
	ACameraMetadata_const_entry e;
	if (ACameraMetadata_getConstEntry(meta, TAG_LENS_FACING, &e) == 0 && e.count > 0)
		c->facing = e.data.u8[0] == LENS_FACING_FRONT ? FACING_FRONT : FACING_BACK;
	if (ACameraMetadata_getConstEntry(meta, TAG_SENSOR_ORIENTATION, &e) == 0 && e.count > 0)
		c->orientation = e.data.i32[0];
	if (ACameraMetadata_getConstEntry(meta, TAG_CONTROL_AVAILABLE_SCENE_MODES, &e) == 0)
		for (uint32_t i = 0; i < e.count; i++)
			if (e.data.u8[i] < 32)
				c->scene_modes |= 1u << e.data.u8[i];
	c->has_mfb = ACameraMetadata_getConstEntry(meta, TAG_MTK_MFB_AVAILABLE, &e) == 0 && e.count > 1;
	if (ACameraMetadata_getConstEntry(meta, TAG_CONTROL_AE_COMPENSATION_RANGE, &e) == 0 && e.count == 2) {
		c->ae_min = e.data.i32[0];
		c->ae_max = e.data.i32[1];
	}
	if (ACameraMetadata_getConstEntry(meta, TAG_CONTROL_AE_COMPENSATION_STEP, &e) == 0 && e.count == 1 &&
	    e.data.r[0].denominator != 0)
		c->ae_step = (double)e.data.r[0].numerator / e.data.r[0].denominator;
	/* The largest JPEG on offer, for cameras without an entry in camera_models, and the stream
	 * sizes the HAL gives as YUV */
	bool yuv[MAX_SIZES] = {false};
	if (ACameraMetadata_getConstEntry(meta, TAG_SCALER_AVAILABLE_STREAM_CONFIGURATIONS, &e) == 0)
		for (uint32_t i = 0; i + 4 <= e.count; i += 4) {
			const int32_t *config = e.data.i32 + i; /* format, width, height, is input */
			if (config[3])
				continue;
			if (config[0] == STREAM_FORMAT_BLOB &&
			    (long)config[1] * config[2] > (long)c->picture_width * c->picture_height) {
				c->picture_width = config[1];
				c->picture_height = config[2];
			}
			for (int k = 0; k < MAX_SIZES; k++)
				yuv[k] |= config[0] == STREAM_FORMAT_YUV_420_888 && config[1] == stream_sizes[k].width &&
					  config[2] == stream_sizes[k].height;
		}
	c->n_sizes = 0;
	for (int k = 0; k < MAX_SIZES; k++)
		if (yuv[k])
			c->sizes[c->n_sizes++] = stream_sizes[k];
	/* A HAL that lists none of them still gets the default */
	if (c->n_sizes == 0)
		c->sizes[c->n_sizes++] = stream_sizes[0];
}

/* One entry per camera the HAL reports, named by camera_models or by facing: "Back Camera",
 * "Back Camera 2" ... */
static void find_cameras(struct bridge *b)
{
	ACameraIdList *ids = NULL;
	int res = ACameraManager_getCameraIdList(b->manager, &ids);
	if (res != 0 || !ids) {
		fprintf(stderr, "camera-hal-bridge: no camera list: %d\n", res);
		return;
	}
	int count[2] = {0, 0};
	for (int i = 0; i < ids->numCameras && b->n_cameras < MAX_CAMERAS; i++) {
		struct camera *c = &b->cameras[b->n_cameras];
		const char *id = ids->cameraIds[i];
		ACameraMetadata *meta = NULL;
		res = ACameraManager_getCameraCharacteristics(b->manager, id, &meta);
		if (res != 0) {
			/* Characteristics over 16 KB come as ashmem, see 73-camera-hal-bridge.rules */
			fprintf(stderr, "camera-hal-bridge: camera %s: no characteristics: %d\n", id, res);
			continue;
		}
		read_characteristics(c, meta);
		ACameraMetadata_free(meta);
		bool front = c->facing == FACING_FRONT;
		int nth = ++count[front];
		snprintf(c->id, sizeof(c->id), "%s", id);
		snprintf(c->name, sizeof(c->name), "hal_camera_%s", id);
		int model = atoi(id);
		if (model >= 0 && model < (int)SPA_N_ELEMENTS(camera_models) && camera_models[model].label) {
			snprintf(c->description, sizeof(c->description), "%s", camera_models[model].label);
			c->picture_width = camera_models[model].picture_width;
			c->picture_height = camera_models[model].picture_height;
		} else if (nth == 1) {
			snprintf(c->description, sizeof(c->description), "%s Camera", front ? "Front" : "Back");
		} else {
			snprintf(c->description, sizeof(c->description), "%s Camera %d", front ? "Front" : "Back", nth);
		}
		b->n_cameras++;
	}
	ACameraManager_deleteCameraIdList(ids);
}

/* Maps iio-sensor-proxy's AccelerometerOrientation to the clockwise turn from portrait; the
 * name says which edge of the phone points up */
static void read_orientation(struct bridge *b, sd_bus *bus)
{
	static const struct {
		const char *name;
		int degrees;
	} map[] = {{"normal", 0}, {"left-up", 90}, {"bottom-up", 180}, {"right-up", 270}};
	char *value = NULL;
	if (sd_bus_get_property_string(bus, SENSOR_PROXY, SENSOR_PROXY_PATH, SENSOR_PROXY,
				       "AccelerometerOrientation", NULL, &value) < 0)
		return;
	for (size_t i = 0; i < SPA_N_ELEMENTS(map); i++)
		if (strcmp(value, map[i].name) == 0)
			atomic_store(&b->device_rotation, map[i].degrees);
	/* "undefined" (lying flat) keeps the last orientation */
	free(value);
}

static int on_properties_changed(sd_bus_message *m, void *userdata, sd_bus_error *error)
{
	bool *dirty = userdata;
	*dirty = true;
	return 0;
}

static int get_active(sd_bus *bus, const char *path, const char *interface, const char *property,
		      sd_bus_message *reply, void *userdata, sd_bus_error *error)
{
	struct bridge *b = userdata;
	return sd_bus_message_append(reply, "b", atomic_load(&b->front_on));
}

static atomic_int *setting_of(struct bridge *b, const char *property, const char *const **names, size_t *n)
{
	if (strcmp(property, "SceneMode") == 0) {
		*names = scene_modes;
		*n = SPA_N_ELEMENTS(scene_modes);
		return &b->scene_mode;
	}
	if (strcmp(property, "ExposureCompensation") == 0) {
		*names = exposures;
		*n = SPA_N_ELEMENTS(exposures);
		return &b->exposure;
	}
	if (strcmp(property, "NoiseReduction") == 0) {
		*names = noise_reductions;
		*n = SPA_N_ELEMENTS(noise_reductions);
		return &b->noise_reduction;
	}
	*names = white_balances;
	*n = SPA_N_ELEMENTS(white_balances);
	return &b->white_balance;
}

static int get_setting(sd_bus *bus, const char *path, const char *interface, const char *property,
		       sd_bus_message *reply, void *userdata, sd_bus_error *error)
{
	const char *const *names;
	size_t n;
	atomic_int *value = setting_of(userdata, property, &names, &n);
	return sd_bus_message_append(reply, "s", names[atomic_load(value)]);
}

static int set_setting(sd_bus *bus, const char *path, const char *interface, const char *property,
		       sd_bus_message *value, void *userdata, sd_bus_error *error)
{
	struct bridge *b = userdata;
	const char *const *names;
	size_t n;
	atomic_int *setting = setting_of(b, property, &names, &n);
	const char *name;
	int res = sd_bus_message_read(value, "s", &name);
	if (res < 0)
		return res;
	size_t i = 0;
	while (i < n && strcmp(name, names[i]) != 0)
		i++;
	if (i == n)
		return sd_bus_error_setf(error, SD_BUS_ERROR_INVALID_ARGS, "unknown %s \"%s\"", property, name);
	if (atomic_exchange(setting, (int)i) == (int)i)
		return 0;
	for (int j = 0; j < b->n_cameras; j++) {
		struct camera *c = &b->cameras[j];
		pthread_mutex_lock(&c->lock);
		c->settings_dirty = true;
		pthread_cond_signal(&c->cond);
		pthread_mutex_unlock(&c->lock);
	}
	return sd_bus_emit_properties_changed(bus, BRIDGE_PATH, BRIDGE_NAME, property, NULL);
}

/* ----- Who may take a photo: who gets the camera's stream ----- */

#define PROC_DIR "/proc/"
/* cgroup v2 level of app.slice: /user.slice /user-<uid>.slice /user@<uid>.service /app.slice */
#define APP_SLICE_LEVEL 4

/* As plinius-appaccessd: letters, digits, '.', '_' and '-', 1 to 255 of them */
static bool app_id_valid(const char *app)
{
	size_t n = strlen(app);
	if (n == 0 || n >= APP_ID_SIZE)
		return false;
	for (const char *p = app; *p; p++)
		if (!(*p >= 'a' && *p <= 'z') && !(*p >= 'A' && *p <= 'Z') && !(*p >= '0' && *p <= '9') &&
		    *p != '.' && *p != '_' && *p != '-')
			return false;
	return true;
}

static int hex_value(char c)
{
	return c >= '0' && c <= '9' ? c - '0' : c >= 'a' && c <= 'f' ? c - 'a' + 10 : c >= 'A' && c <= 'F' ? c - 'A' + 10 : -1;
}

/* systemd's \xNN escapes of @s undone into @out; false for an escaped NUL or no room */
static bool unit_unescape(const char *s, char *out, size_t size)
{
	size_t n = 0;
	while (*s) {
		char c = *s++;
		if (c == '\\' && s[0] == 'x' && hex_value(s[1]) >= 0 && hex_value(s[2]) >= 0) {
			c = (char)(hex_value(s[1]) << 4 | hex_value(s[2]));
			if (c == '\0')
				return false;
			s += 3;
		}
		if (n + 1 >= size)
			return false;
		out[n++] = c;
	}
	out[n] = '\0';
	return true;
}

static bool ends_with(const char *s, const char *suffix)
{
	size_t n = strlen(s), k = strlen(suffix);
	return n >= k && strcmp(s + n - k, suffix) == 0;
}

/* The application id of an app's unit, the cgroup right below app.slice, as plinius-appaccessd
 * reads it: app-[<launcher>-]<ApplicationID>-<RANDOM>.scope,
 * app-[<launcher>-]<ApplicationID>[@<RANDOM>].service, dbus-:<bus id>-<ApplicationID>@<n>.service.
 * Split while still escaped: systemd writes a dash inside the application id as \x2d. */
static bool app_of_unit(const char *unit, char *app, size_t size)
{
	char leaf[1024], *id, *p;
	if (snprintf(leaf, sizeof(leaf), "%s", unit) >= (int)sizeof(leaf))
		return false;
	if (strncmp(leaf, "dbus-:", 6) == 0 && ends_with(leaf, ".service")) {
		if (!(id = strchr(leaf + 6, '-')))
			return false;
		id++;
		if ((p = strchr(id, '@')) || (p = strstr(id, ".service")))
			*p = '\0';
	} else if (strncmp(leaf, "app-", 4) == 0) {
		id = leaf + 4;
		if (ends_with(id, ".scope")) {
			id[strlen(id) - 6] = '\0';
			if ((p = strrchr(id, '-')))
				*p = '\0';
		} else if (ends_with(id, ".service")) {
			id[strlen(id) - 8] = '\0';
			if ((p = strchr(id, '@')))
				*p = '\0';
		} else {
			return false; /* a slice */
		}
		/* A launcher prefix ("gnome-", "flatpak-") has no dots, an application id usually has */
		p = strchr(id, '-');
		if (p && !memchr(id, '.', (size_t)(p - id)) && p[1])
			id = p + 1;
	} else {
		return false;
	}
	return unit_unescape(id, app, size) && app_id_valid(app);
}

/* The application id of process @pid from its cgroup, the first unit below app.slice that is no
 * slice; false for none (a service, a shell) */
static bool app_of_pid(uint32_t pid, char *app, size_t size)
{
	char file[64];
	snprintf(file, sizeof(file), PROC_DIR "%u/cgroup", pid);
	FILE *f = pid ? fopen(file, "re") : NULL;
	if (!f)
		return false;
	char *line = NULL, *path = NULL;
	size_t room = 0;
	while (!path && getline(&line, &room, f) > 0)
		if (strncmp(line, "0::", 3) == 0)
			path = line + 3;
	fclose(f);
	bool found = false;
	if (path) {
		path[strcspn(path, "\n")] = '\0';
		char *save = NULL, *part = strtok_r(path, "/", &save);
		for (int level = 1; part && level < APP_SLICE_LEVEL; level++)
			part = strtok_r(NULL, "/", &save);
		/* An app started by D-Bus activation runs one level deeper, in a slice of its own:
		 * app-dbus\x2d:1.2\x2dorg.gnome.Snapshot.slice/dbus-:1.2-org.gnome.Snapshot@0.service */
		if (part && strcmp(part, "app.slice") == 0) {
			while ((part = strtok_r(NULL, "/", &save)) && ends_with(part, ".slice"))
				;
			found = part && app_of_unit(part, app, size);
		}
	}
	free(line);
	return found;
}

/* Whether the process @pid gets @c's stream: it is a client the camera streams to or of the same
 * app as one (a portal client by the app id the portal gave it). Takes the thread loop's lock. */
static bool gets_stream(struct camera *c, uint32_t pid)
{
	struct bridge *b = c->bridge;
	struct consumers *w = &b->consumers;
	char caller[APP_ID_SIZE], app[APP_ID_SIZE];
	bool known = app_of_pid(pid, caller, sizeof(caller)), yes = false;
	pw_thread_loop_lock(b->loop);
	uint32_t node = pw_stream_get_node_id(c->stream);
	for (int i = 0; w->registry && node != SPA_ID_INVALID && i < MAX_WATCHED && !yes; i++) {
		struct watched *client = consumer_of(w, i, node);
		if (!client)
			continue;
		if (client->portal)
			yes = known && strcmp(client->app, caller) == 0;
		else
			yes = client->pid == pid ||
			      (known && app_of_pid(client->pid, app, sizeof(app)) && strcmp(app, caller) == 0);
	}
	pw_thread_loop_unlock(b->loop);
	return yes;
}

/* The process that sent @m, 0 if the bus does not tell */
static uint32_t sender_pid(sd_bus_message *m)
{
	sd_bus_creds *creds = NULL;
	pid_t pid = 0;
	if (sd_bus_query_sender_creds(m, SD_BUS_CREDS_PID, &creds) < 0 || sd_bus_creds_get_pid(creds, &pid) < 0)
		pid = 0;
	sd_bus_creds_unref(creds);
	return pid > 0 ? (uint32_t)pid : 0;
}

/* TakePicture(s node, s path): a still from the streaming camera node (hal_camera_<id>) as a JPEG
 * at path, a new file in the pictures folder (picture_target). Answered once the file is written,
 * by answer_captures in this thread. */
static int take_picture_call(sd_bus_message *m, void *userdata, sd_bus_error *error)
{
	struct bridge *b = userdata;
	const char *node, *path;
	int res = sd_bus_message_read(m, "ss", &node, &path);
	if (res < 0)
		return res;
	if (path[0] != '/')
		return sd_bus_error_setf(error, SD_BUS_ERROR_INVALID_ARGS, "not an absolute path: %s", path);
	struct camera *c = NULL;
	for (int i = 0; i < b->n_cameras && !c; i++)
		if (strcmp(b->cameras[i].name, node) == 0)
			c = &b->cameras[i];
	if (!c)
		return sd_bus_error_setf(error, SD_BUS_ERROR_INVALID_ARGS, "no camera node %s", node);
	pthread_mutex_lock(&c->lock);
	bool active = c->active;
	pthread_mutex_unlock(&c->lock);
	if (!active)
		return sd_bus_error_setf(error, SD_BUS_ERROR_NOT_SUPPORTED, "%s is not streaming", node);
	/* Not with c->lock held: the PipeWire thread takes it with the thread loop's lock */
	uint32_t pid = sender_pid(m);
	if (!pid || !gets_stream(c, pid)) {
		pw_log_warn("%s: picture refused to process %u, which does not get the stream", node, pid);
		return sd_bus_error_setf(error, SD_BUS_ERROR_ACCESS_DENIED, "%s: only for who gets its stream", node);
	}
	/* Only to whom it may see the stream: whether a folder is there is no business of others */
	int dir;
	size_t name;
	res = picture_target(path, &dir, &name);
	if (res == -EACCES) {
		pw_log_warn("%s: picture refused at %s, no JPEG in the pictures folder", node, path);
		return sd_bus_error_setf(error, SD_BUS_ERROR_ACCESS_DENIED,
					 "%s: a photo only as a new .jpg or .jpeg in the pictures folder", node);
	}
	if (res < 0)
		return sd_bus_error_set_errnof(error, -res, "%s: no picture at %s: %s", node, path, strerror(-res));
	char *copy = strdup(path);
	if (!copy) {
		close(dir);
		return -ENOMEM;
	}
	pthread_mutex_lock(&c->lock);
	if (!c->active || c->capture != CAPTURE_IDLE) {
		bool busy = c->active;
		pthread_mutex_unlock(&c->lock);
		close(dir);
		free(copy);
		return sd_bus_error_setf(error, busy ? SD_BUS_ERROR_LIMITS_EXCEEDED : SD_BUS_ERROR_NOT_SUPPORTED,
					 busy ? "%s is taking a picture already" : "%s is not streaming", node);
	}
	c->capture = CAPTURE_REQUESTED;
	c->capture_call = sd_bus_message_ref(m);
	c->capture_path = copy;
	c->capture_dir = dir;
	c->capture_name = copy + name;
	pthread_cond_signal(&c->cond);
	pthread_mutex_unlock(&c->lock);
	return 1; /* answered later */
}

/* Answers the TakePicture calls whose camera thread is done */
static void answer_captures(struct bridge *b)
{
	for (int i = 0; i < b->n_cameras; i++) {
		struct camera *c = &b->cameras[i];
		pthread_mutex_lock(&c->lock);
		if (c->capture != CAPTURE_DONE) {
			pthread_mutex_unlock(&c->lock);
			continue;
		}
		sd_bus_message *call = c->capture_call;
		int result = c->capture_result;
		free(c->capture_path);
		c->capture_path = NULL;
		c->capture_name = NULL;
		close(c->capture_dir);
		c->capture_dir = -1;
		c->capture_call = NULL;
		c->capture = CAPTURE_IDLE;
		pthread_mutex_unlock(&c->lock);
		if (result == 0)
			sd_bus_reply_method_return(call, "");
		else
			sd_bus_reply_method_errnof(call, -result, "%s: no picture: %s", c->name, strerror(-result));
		sd_bus_message_unref(call);
	}
}

static const sd_bus_vtable bridge_vtable[] = {
	SD_BUS_VTABLE_START(0),
	/* true while the front camera streams, through the bridge or not */
	SD_BUS_PROPERTY("Active", "b", get_active, 0, SD_BUS_VTABLE_PROPERTY_EMITS_CHANGE),
	/* For all cameras: the scene mode (auto, action, night, party, sunset, hdr; what a camera does
	 * not offer counts as auto), white balance (auto, daylight, cloudy-daylight, fluorescent,
	 * incandescent), exposure compensation in EV (0, -2, -1, +1, +2) and MediaTek's multi-frame
	 * noise reduction for photos (auto, on, off) */
	SD_BUS_WRITABLE_PROPERTY("SceneMode", "s", get_setting, set_setting, 0,
				 SD_BUS_VTABLE_PROPERTY_EMITS_CHANGE),
	SD_BUS_WRITABLE_PROPERTY("WhiteBalance", "s", get_setting, set_setting, 0,
				 SD_BUS_VTABLE_PROPERTY_EMITS_CHANGE),
	SD_BUS_WRITABLE_PROPERTY("ExposureCompensation", "s", get_setting, set_setting, 0,
				 SD_BUS_VTABLE_PROPERTY_EMITS_CHANGE),
	SD_BUS_WRITABLE_PROPERTY("NoiseReduction", "s", get_setting, set_setting, 0,
				 SD_BUS_VTABLE_PROPERTY_EMITS_CHANGE),
	SD_BUS_METHOD_WITH_ARGS("TakePicture", SD_BUS_ARGS("s", node, "s", path), SD_BUS_NO_RESULT,
				take_picture_call, SD_BUS_VTABLE_UNPRIVILEGED),
	SD_BUS_VTABLE_END,
};

/* The session bus with org.plinius.CameraBridge, or NULL (then the shell just shows nothing) */
static sd_bus *open_session_bus(struct bridge *b)
{
	sd_bus *bus = NULL;
	if (sd_bus_open_user(&bus) < 0 ||
	    sd_bus_add_object_vtable(bus, NULL, BRIDGE_PATH, BRIDGE_NAME, bridge_vtable, b) < 0 ||
	    sd_bus_request_name(bus, BRIDGE_NAME, 0) < 0) {
		pw_log_warn("no session bus, the shell cannot show the camera in use");
		sd_bus_unref(bus);
		return NULL;
	}
	return bus;
}

/* Whether the kernel has the front port powered, from the open runtime_status file (fd -1: not watched) */
static bool front_port_active(int fd)
{
	char state[16];
	ssize_t n = fd < 0 ? -1 : pread(fd, state, sizeof(state) - 1, 0);
	if (n <= 0)
		return false;
	state[n] = '\0';
	return strncmp(state, "active", 6) == 0;
}

/* Follows the phone's orientation on the system bus while any camera streams, and tells the
 * session bus whether the front camera is on */
static void *orientation_thread(void *data)
{
	struct bridge *b = data;
	sd_bus *bus = NULL;
	sd_bus *session = open_session_bus(b);
	bool claimed = false, dirty = false, announced = false;
	/* Only worth watching when someone listens */
	int port_status = session ? open(FRONT_PORT_STATUS, O_RDONLY | O_CLOEXEC) : -1;
	if (sd_bus_open_system(&bus) < 0 ||
	    sd_bus_match_signal(bus, NULL, SENSOR_PROXY, SENSOR_PROXY_PATH, "org.freedesktop.DBus.Properties",
				"PropertiesChanged", on_properties_changed, &dirty) < 0) {
		pw_log_warn("no system bus, the camera orientation stays portrait");
		sd_bus_unref(bus);
		bus = NULL;
	}
	if (!bus && !session)
		return NULL;
	while (!atomic_load(&b->quit)) {
		bool want = atomic_load(&b->streaming) > 0;
		bool on = atomic_load(&b->front_streaming) > 0 || front_port_active(port_status);
		atomic_store(&b->front_on, on);
		if (session && on != announced) {
			sd_bus_emit_properties_changed(session, BRIDGE_PATH, BRIDGE_NAME, "Active", NULL);
			announced = on;
		}
		if (session) {
			answer_captures(b);
			while (sd_bus_process(session, NULL) > 0)
				;
		}
		if (!bus) {
			struct pollfd fds[2] = {
				{.fd = sd_bus_get_fd(session), .events = sd_bus_get_events(session)},
				{.fd = b->wake_fd, .events = POLLIN},
			};
			if (poll(fds, 2, port_status >= 0 ? FRONT_POLL_MS : -1) > 0 && (fds[1].revents & POLLIN)) {
				uint64_t count;
				if (read(b->wake_fd, &count, sizeof(count)) < 0)
					pw_log_warn("cannot read the wakeup: %m");
			}
			continue;
		}
		if (want != claimed) {
			int res = sd_bus_call_method(bus, SENSOR_PROXY, SENSOR_PROXY_PATH, SENSOR_PROXY,
						     want ? "ClaimAccelerometer" : "ReleaseAccelerometer", NULL, NULL, "");
			if (res < 0)
				pw_log_warn("accelerometer %s: %s", want ? "claim" : "release", strerror(-res));
			claimed = want;
			dirty = claimed;
			if (!claimed)
				atomic_store(&b->device_rotation, 0);
		}
		while (sd_bus_process(bus, NULL) > 0)
			;
		if (dirty) {
			dirty = false;
			read_orientation(b, bus);
		}
		struct pollfd fds[3] = {
			{.fd = sd_bus_get_fd(bus), .events = sd_bus_get_events(bus)},
			{.fd = b->wake_fd, .events = POLLIN},
			{.fd = session ? sd_bus_get_fd(session) : -1,
			 .events = session ? sd_bus_get_events(session) : 0},
		};
		uint64_t until;
		int timeout = -1;
		if (sd_bus_get_timeout(bus, &until) >= 0 && until != UINT64_MAX) {
			struct timespec now;
			clock_gettime(CLOCK_MONOTONIC, &now);
			uint64_t now_us = now.tv_sec * 1000000ULL + now.tv_nsec / 1000;
			timeout = until > now_us ? (int)((until - now_us + 999) / 1000) : 0;
		}
		if (port_status >= 0 && (timeout < 0 || timeout > FRONT_POLL_MS))
			timeout = FRONT_POLL_MS;
		if (poll(fds, 3, timeout) > 0 && (fds[1].revents & POLLIN)) {
			uint64_t count;
			if (read(b->wake_fd, &count, sizeof(count)) < 0)
				pw_log_warn("cannot read the wakeup: %m");
		}
	}
	if (port_status >= 0)
		close(port_status);
	sd_bus_flush_close_unref(bus);
	sd_bus_flush_close_unref(session);
	return NULL;
}

int main(int argc, char *argv[])
{
	static struct bridge b;
	sigset_t signals;
	sigemptyset(&signals);
	sigaddset(&signals, SIGINT);
	sigaddset(&signals, SIGTERM);
	pthread_sigmask(SIG_BLOCK, &signals, NULL);

	if (!camera_ndk_load())
		return 1;
	b.manager = ACameraManager_create();
	/* At boot this starts before the provider in the Android container has listed its cameras,
	 * which takes it a few more seconds */
	find_cameras(&b);
	for (int i = 0; b.n_cameras == 0 && i < CAMERA_WAIT_TRIES; i++) {
		usleep(CAMERA_WAIT_US);
		find_cameras(&b);
	}
	if (b.n_cameras == 0) {
		fprintf(stderr, "camera-hal-bridge: no cameras\n");
		return 1;
	}
	b.wake_fd = eventfd(0, EFD_CLOEXEC);
	if (b.wake_fd < 0) {
		perror("camera-hal-bridge: eventfd");
		return 1;
	}
	pw_init(&argc, &argv);
	b.loop = pw_thread_loop_new("camera-hal-bridge", NULL);
	pw_thread_loop_lock(b.loop);
	int res = pw_thread_loop_start(b.loop);
	for (int i = 0; res == 0 && i < b.n_cameras; i++)
		res = connect_camera(&b, &b.cameras[i]);
	if (res == 0)
		consumers_start(&b);
	pw_thread_loop_unlock(b.loop);
	if (res) {
		fprintf(stderr, "camera-hal-bridge: cannot connect to PipeWire\n");
		return 1;
	}
	for (int i = 0; i < b.n_cameras; i++)
		pthread_create(&b.cameras[i].thread, NULL, camera_thread, &b.cameras[i]);
	pthread_create(&b.orientation_thread, NULL, orientation_thread, &b);

	int sig;
	sigwait(&signals, &sig);

	atomic_store(&b.quit, true);
	wake_orientation_thread(&b);
	pthread_join(b.orientation_thread, NULL);
	for (int i = 0; i < b.n_cameras; i++) {
		struct camera *c = &b.cameras[i];
		pthread_mutex_lock(&c->lock);
		c->quit = true;
		pthread_cond_signal(&c->cond);
		pthread_mutex_unlock(&c->lock);
		pthread_join(c->thread, NULL);
	}
	pw_thread_loop_lock(b.loop);
	consumers_stop(&b);
	for (int i = 0; i < b.n_cameras; i++)
		pw_stream_destroy(b.cameras[i].stream);
	pw_thread_loop_unlock(b.loop);
	if (b.consumers.thread)
		pthread_join(b.consumers.thread, NULL);
	pw_thread_loop_stop(b.loop);
	pw_thread_loop_destroy(b.loop);
	pw_deinit();
	close(b.wake_fd);
	/* Skips the destructors of the Android libraries, which are not meant to run in a glibc exit */
	fflush(NULL);
	_exit(0);
}
