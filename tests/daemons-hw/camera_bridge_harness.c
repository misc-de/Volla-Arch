/* Test harness for camera-hal-bridge (tests/daemons-hw/test_dhw_camera_*.py).
 *
 * The bridge runs as on the phone, with three things around it faked:
 * - the Camera2 and media NDK: fakes/hybris/common/dlfcn.h hands out the fakes below for
 *   hybris_dlsym; they print what the bridge asks of them ("ndk ..."), fail as HARNESS_FAIL
 *   says, and fire the NDK's callbacks when the test tells them to (a frame, a still, a lost
 *   device);
 * - PipeWire: the stream, context, core and registry functions are wrapped (-Wl,--wrap) and
 *   print what the bridge hands over ("pw ..."); the test fires the stream's and the registry's
 *   events;
 * - the system services: iio-sensor-proxy and plinius-appaccess are served by this process on
 *   the private bus of the test (HARNESS_BUS), never on a real one.
 *
 * "daemon" runs the bridge's main with commands from stdin (one per line, each answered by
 * "ok <command>"); other arguments are single checks of the bridge's functions.
 *
 * HARNESS_FAIL: comma separated rules, "name" fails every call, "name*N" the first N calls,
 * "name@N" only the Nth. Names are the NDK functions, "dlopen:<lib>", "dlsym:<symbol>", the
 * wrapped PipeWire functions and "write_eintr", "write_zero", "read_error", "strdup",
 * "sd_bus_creds_get_pid".
 * The caller of a bus method is process 4321 (the test's fake /proc tells its cgroup), or the one
 * "caller <pid>" names; "caller real" lets the bus tell.
 * HARNESS_CAMERAS: "id:facing:orientation[:options]", facing b(ack) or f(ront); options:
 * p plain (no optional characteristics), n no multi-frame noise reduction, z exposure step 0,
 * s exposure range -6..6 in steps of 0.3 EV, a every stream size (4K too; others offer 720p and
 * 1080p). */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/eventfd.h>
#include <sys/resource.h>
#include <unistd.h>

static void harness_exit(int code) __attribute__((noreturn));
#define main real_main
/* The bridge leaves with _exit (no Android destructors); the harness leaves with exit, so that
 * the sanitizers and gcov's counters get their say */
#define _exit harness_exit
#include "camera-hal-bridge.c"

/* The default stream size (stream_sizes[0]) and room for the largest one */
#define WIDTH 1280
#define HEIGHT 720
#define FRAME_SIZE NV21_SIZE(WIDTH, HEIGHT)
#define MAX_FRAME_SIZE NV21_SIZE(3840, 2160)
#undef main
#undef _exit

#include <spa/pod/parser.h>

/* ----- output ----- */

static pthread_mutex_t say_lock = PTHREAD_MUTEX_INITIALIZER;

static void say(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void say(const char *fmt, ...)
{
	va_list ap;
	va_start(ap, fmt);
	pthread_mutex_lock(&say_lock);
	vprintf(fmt, ap);
	putchar('\n');
	fflush(stdout);
	pthread_mutex_unlock(&say_lock);
	va_end(ap);
}

static void harness_exit(int code)
{
	say("exit %d", code);
	exit(code);
}

/* ----- scripted failures ----- */

#define FAIL_CODE (-10001)

struct rule {
	char name[96];
	int mode; /* 0 always, 1 the first n calls, 2 only the nth */
	int n;
};
struct counter {
	char name[96];
	int n;
};
static struct rule rules[64];
static int n_rules;
static struct counter counters[256];
static int n_counters;
static pthread_mutex_t script_lock = PTHREAD_MUTEX_INITIALIZER;

static void add_rule(const char *spec)
{
	pthread_mutex_lock(&script_lock);
	struct rule *r = &rules[n_rules++];
	memset(r, 0, sizeof(*r));
	snprintf(r->name, sizeof(r->name), "%s", spec);
	char *mark = strpbrk(r->name, "*@");
	if (mark) {
		r->mode = *mark == '*' ? 1 : 2;
		r->n = atoi(mark + 1);
		*mark = '\0';
	}
	pthread_mutex_unlock(&script_lock);
}

static void add_rules(const char *spec)
{
	char buf[1024];
	if (!spec)
		return;
	snprintf(buf, sizeof(buf), "%s", spec);
	for (char *save, *tok = strtok_r(buf, ",", &save); tok; tok = strtok_r(NULL, ",", &save))
		add_rule(tok);
}

static void drop_rule(const char *name)
{
	pthread_mutex_lock(&script_lock);
	for (int i = 0; i < n_rules; i++)
		if (strcmp(rules[i].name, name) == 0)
			rules[i].name[0] = '\0';
	pthread_mutex_unlock(&script_lock);
}

static int count_of(const char *name)
{
	pthread_mutex_lock(&script_lock);
	int n = 0;
	for (int i = 0; i < n_counters; i++)
		if (strcmp(counters[i].name, name) == 0)
			n = counters[i].n;
	pthread_mutex_unlock(&script_lock);
	return n;
}

/* Counts a call of name; true when a rule says it fails */
static bool fails(const char *name)
{
	pthread_mutex_lock(&script_lock);
	struct counter *c = NULL;
	for (int i = 0; i < n_counters && !c; i++)
		if (strcmp(counters[i].name, name) == 0)
			c = &counters[i];
	if (!c) {
		c = &counters[n_counters++];
		snprintf(c->name, sizeof(c->name), "%s", name);
		c->n = 0;
	}
	int n = ++c->n;
	bool fail = false;
	for (int i = 0; i < n_rules; i++)
		if (strcmp(rules[i].name, name) == 0)
			fail |= rules[i].mode == 0 || (rules[i].mode == 1 && n <= rules[i].n) ||
				(rules[i].mode == 2 && n == rules[i].n);
	pthread_mutex_unlock(&script_lock);
	return fail;
}

/* ----- wrapped libc calls, for the errors a host does not make on its own ----- */

ssize_t __real_write(int fd, const void *buf, size_t n);
ssize_t __wrap_write(int fd, const void *buf, size_t n)
{
	if (fails("write_eintr")) {
		errno = EINTR;
		return -1;
	}
	if (fails("write_zero"))
		return 0;
	return __real_write(fd, buf, n);
}

ssize_t __real_read(int fd, void *buf, size_t n);
ssize_t __wrap_read(int fd, void *buf, size_t n)
{
	if (fails("read_error")) {
		errno = EBADF;
		return -1;
	}
	return __real_read(fd, buf, n);
}

char *__real_strdup(const char *s);
char *__wrap_strdup(const char *s)
{
	if (fails("strdup")) {
		errno = ENOMEM;
		return NULL;
	}
	return __real_strdup(s);
}

/* The process the bridge sees as the sender of a bus call, 0: the one the bus says */
static atomic_uint caller = 4321;

int __real_sd_bus_creds_get_pid(sd_bus_creds *c, pid_t *pid);
int __wrap_sd_bus_creds_get_pid(sd_bus_creds *c, pid_t *pid)
{
	if (fails("sd_bus_creds_get_pid"))
		return -ENODATA;
	if (atomic_load(&caller)) {
		*pid = (pid_t)atomic_load(&caller);
		return 0;
	}
	return __real_sd_bus_creds_get_pid(c, pid);
}

/* ----- the fake HAL: cameras and their characteristics ----- */

struct ACameraManager {
	int unused;
};
struct ACameraMetadata {
	struct fake_camera *fc;
};
struct ACameraDevice {
	struct fake_camera *fc;
	ACameraDevice_StateCallbacks callbacks;
};
struct ANativeWindow {
	AImageReader *reader;
};
struct AImageReader {
	int32_t width, height, format, max_images;
	AImageReader_ImageListener listener;
	struct ANativeWindow window;
	struct fake_camera *fc;
	AImage *pending; /* what acquireLatestImage hands out next */
};
struct ACaptureSessionOutputContainer {
	int n;
};
struct ACaptureSessionOutput {
	ANativeWindow *window;
};
struct ACameraOutputTarget {
	ANativeWindow *window;
};
struct ACaptureRequest {
	struct fake_camera *fc;
	int template_id;
};
struct ACameraCaptureSession {
	struct fake_camera *fc;
	ACameraCaptureSession_stateCallbacks callbacks;
};
struct AImage {
	int32_t width, height;
	uint8_t *data[3];
	int size[3];
	int32_t row_stride[3], pixel_stride[3];
};

struct fake_camera {
	char id[8];
	uint8_t lens_facing;
	int32_t orientation;
	bool plain, no_mfb, zero_step, small_range, all_sizes;
	/* live objects, to fire their callbacks */
	ACameraDevice *device;
	AImageReader *preview, *still;
	ACameraCaptureSession *session;
	ACameraCaptureSession_captureCallbacks capture_callbacks;
	bool capturing;
};

#define MAX_FAKE_CAMERAS 12
static struct fake_camera fake_cameras[MAX_FAKE_CAMERAS];
static int n_fake_cameras;
static pthread_mutex_t ndk_lock = PTHREAD_MUTEX_INITIALIZER;

/* Objects the bridge holds: created minus freed, per kind */
enum { OBJ_LIST, OBJ_META, OBJ_DEVICE, OBJ_READER, OBJ_CONTAINER, OBJ_OUTPUT, OBJ_TARGET, OBJ_REQUEST,
       OBJ_SESSION, OBJ_IMAGE, OBJ_KINDS };
static const char *const obj_names[] = {"list", "metadata", "device", "reader", "container", "output",
					 "target", "request", "session", "image"};
static atomic_int live[OBJ_KINDS];

static void parse_cameras(const char *spec)
{
	char buf[512];
	snprintf(buf, sizeof(buf), "%s", spec ? spec : "0:b:90,1:f:270");
	for (char *save, *tok = strtok_r(buf, ",", &save); tok && n_fake_cameras < MAX_FAKE_CAMERAS;
	     tok = strtok_r(NULL, ",", &save)) {
		struct fake_camera *fc = &fake_cameras[n_fake_cameras++];
		char *parts[4] = {0};
		int n = 0;
		for (char *s2, *p = strtok_r(tok, ":", &s2); p && n < 4; p = strtok_r(NULL, ":", &s2))
			parts[n++] = p;
		snprintf(fc->id, sizeof(fc->id), "%s", parts[0]);
		fc->lens_facing = parts[1] && parts[1][0] == 'f' ? LENS_FACING_FRONT : LENS_FACING_BACK;
		fc->orientation = parts[2] ? atoi(parts[2]) : 0;
		const char *opt = parts[3] ? parts[3] : "";
		fc->plain = strchr(opt, 'p');
		fc->no_mfb = strchr(opt, 'n');
		fc->zero_step = strchr(opt, 'z');
		fc->small_range = strchr(opt, 's');
		fc->all_sizes = strchr(opt, 'a');
	}
}

static struct fake_camera *fake_camera(const char *id)
{
	for (int i = 0; i < n_fake_cameras; i++)
		if (strcmp(fake_cameras[i].id, id) == 0)
			return &fake_cameras[i];
	return NULL;
}

static const char *tag_name(uint32_t tag)
{
	switch (tag) {
	case TAG_CONTROL_AE_EXPOSURE_COMPENSATION: return "AE_EXPOSURE_COMPENSATION";
	case TAG_CONTROL_AWB_MODE: return "AWB_MODE";
	case TAG_CONTROL_MODE: return "CONTROL_MODE";
	case TAG_CONTROL_SCENE_MODE: return "SCENE_MODE";
	case TAG_FLASH_MODE: return "FLASH_MODE";
	case TAG_JPEG_ORIENTATION: return "JPEG_ORIENTATION";
	case TAG_JPEG_QUALITY: return "JPEG_QUALITY";
	case TAG_MTK_MFB_MODE: return "MTK_MFB_MODE";
	}
	return "?";
}

static const char *template_name(int t)
{
	return t == TEMPLATE_PREVIEW ? "preview" : t == TEMPLATE_STILL_CAPTURE ? "still" : "?";
}

static const char *format_name(int32_t f)
{
	return f == AIMAGE_FORMAT_JPEG ? "jpeg" : f == AIMAGE_FORMAT_YUV_420_888 ? "yuv" : "?";
}

/* ----- the fake NDK ----- */

static ACameraManager *f_manager_create(void)
{
	static ACameraManager manager;
	say("ndk ACameraManager_create");
	return &manager;
}

static int f_get_id_list(ACameraManager *m, ACameraIdList **out)
{
	if (fails("ACameraManager_getCameraIdList")) {
		say("ndk ACameraManager_getCameraIdList failed");
		return FAIL_CODE;
	}
	ACameraIdList *l = calloc(1, sizeof(*l));
	l->numCameras = n_fake_cameras;
	l->cameraIds = calloc(n_fake_cameras + 1, sizeof(char *));
	for (int i = 0; i < n_fake_cameras; i++)
		l->cameraIds[i] = fake_cameras[i].id;
	live[OBJ_LIST]++;
	say("ndk ACameraManager_getCameraIdList %d", l->numCameras);
	*out = l;
	return 0;
}

static void f_delete_id_list(ACameraIdList *l)
{
	free(l->cameraIds);
	free(l);
	live[OBJ_LIST]--;
}

static int f_get_characteristics(ACameraManager *m, const char *id, ACameraMetadata **out)
{
	if (fails("ACameraManager_getCameraCharacteristics"))
		return FAIL_CODE;
	ACameraMetadata *meta = calloc(1, sizeof(*meta));
	meta->fc = fake_camera(id);
	live[OBJ_META]++;
	*out = meta;
	return 0;
}

static void f_metadata_free(ACameraMetadata *m)
{
	free(m);
	live[OBJ_META]--;
}

static int f_get_const_entry(const ACameraMetadata *m, uint32_t tag, ACameraMetadata_const_entry *e)
{
	/* option a: every stream size, 4K included */
	static const int32_t all_sizes[] = {
		STREAM_FORMAT_BLOB, 4000, 3000, 0, 0x23, 3840, 2160, 0, 0x23, 1920, 1080, 0, 0x23, 1280, 720, 0,
	};
	static const uint8_t scenes[] = {0, 2, 5, 40}; /* auto, action, night; 40 is beyond the mask */
	static const uint8_t mfb[] = {0, 1};
	static const int32_t range[] = {-12, 12}, small_range[] = {-6, 6};
	static const ACameraMetadata_rational step = {1, 6}, odd = {3, 10}, zero = {1, 0};
	static const int32_t configs[] = {
		STREAM_FORMAT_BLOB, 4000, 3000, 0, /* the largest output JPEG */
		STREAM_FORMAT_BLOB, 8000, 6000, 1, /* an input */
		0x23, 9000, 9000, 0,		   /* not a JPEG, not a stream size */
		STREAM_FORMAT_BLOB, 1920, 1080, 0,
		0x23, 1920, 1080, 0,		   /* stream sizes: 1080p and 720p, not 4K */
		0x23, 3840, 2160, 1,		   /* an input */
		0x23, 1280, 720, 0,
	};
	const struct fake_camera *fc = m->fc;
	memset(e, 0, sizeof(*e));
	e->tag = tag;
	switch (tag) {
	case TAG_LENS_FACING: e->count = 1; e->data.u8 = &fc->lens_facing; return 0;
	case TAG_SENSOR_ORIENTATION: e->count = 1; e->data.i32 = &fc->orientation; return 0;
	}
	if (fc->plain)
		return -1;
	switch (tag) {
	case TAG_CONTROL_AVAILABLE_SCENE_MODES: e->count = 4; e->data.u8 = scenes; return 0;
	case TAG_MTK_MFB_AVAILABLE: e->count = fc->no_mfb ? 1 : 2; e->data.u8 = mfb; return 0;
	case TAG_CONTROL_AE_COMPENSATION_RANGE:
		e->count = 2;
		e->data.i32 = fc->small_range ? small_range : range;
		return 0;
	case TAG_CONTROL_AE_COMPENSATION_STEP:
		e->count = 1;
		e->data.r = fc->zero_step ? &zero : fc->small_range ? &odd : &step;
		return 0;
	case TAG_SCALER_AVAILABLE_STREAM_CONFIGURATIONS:
		e->count = fc->all_sizes ? 16 : SPA_N_ELEMENTS(configs);
		e->data.i32 = fc->all_sizes ? all_sizes : configs;
		return 0;
	}
	return -1;
}

static int f_open_camera(ACameraManager *m, const char *id, ACameraDevice_StateCallbacks *cb, ACameraDevice **out)
{
	if (fails("ACameraManager_openCamera")) {
		say("ndk ACameraManager_openCamera %s failed", id);
		return FAIL_CODE;
	}
	ACameraDevice *d = calloc(1, sizeof(*d));
	d->fc = fake_camera(id);
	d->callbacks = *cb;
	pthread_mutex_lock(&ndk_lock);
	d->fc->device = d;
	pthread_mutex_unlock(&ndk_lock);
	live[OBJ_DEVICE]++;
	say("ndk ACameraManager_openCamera %s", id);
	*out = d;
	return 0;
}

static int f_device_close(ACameraDevice *d)
{
	say("ndk ACameraDevice_close %s", d->fc->id);
	pthread_mutex_lock(&ndk_lock);
	if (d->fc->device == d)
		d->fc->device = NULL;
	pthread_mutex_unlock(&ndk_lock);
	free(d);
	live[OBJ_DEVICE]--;
	return 0;
}

static int f_create_request(const ACameraDevice *d, int template_id, ACaptureRequest **out)
{
	if (fails("ACameraDevice_createCaptureRequest"))
		return FAIL_CODE;
	ACaptureRequest *r = calloc(1, sizeof(*r));
	r->fc = d->fc;
	r->template_id = template_id;
	live[OBJ_REQUEST]++;
	say("ndk ACameraDevice_createCaptureRequest %s %s", d->fc->id, template_name(template_id));
	*out = r;
	return 0;
}

static int f_create_session(ACameraDevice *d, const ACaptureSessionOutputContainer *outputs,
			    const ACameraCaptureSession_stateCallbacks *cb, ACameraCaptureSession **out)
{
	if (fails("ACameraDevice_createCaptureSession"))
		return FAIL_CODE;
	ACameraCaptureSession *s = calloc(1, sizeof(*s));
	s->fc = d->fc;
	s->callbacks = *cb;
	pthread_mutex_lock(&ndk_lock);
	d->fc->session = s;
	pthread_mutex_unlock(&ndk_lock);
	live[OBJ_SESSION]++;
	say("ndk ACameraDevice_createCaptureSession %s outputs=%d", d->fc->id, outputs->n);
	*out = s;
	return 0;
}

static int f_container_create(ACaptureSessionOutputContainer **out)
{
	if (fails("ACaptureSessionOutputContainer_create"))
		return FAIL_CODE;
	*out = calloc(1, sizeof(**out));
	live[OBJ_CONTAINER]++;
	return 0;
}

static void f_container_free(ACaptureSessionOutputContainer *c)
{
	free(c);
	live[OBJ_CONTAINER]--;
}

static int f_container_add(ACaptureSessionOutputContainer *c, const ACaptureSessionOutput *o)
{
	if (fails("ACaptureSessionOutputContainer_add"))
		return FAIL_CODE;
	c->n++;
	say("ndk ACaptureSessionOutputContainer_add %s", format_name(o->window->reader->format));
	return 0;
}

static int f_output_create(ANativeWindow *w, ACaptureSessionOutput **out)
{
	if (fails("ACaptureSessionOutput_create"))
		return FAIL_CODE;
	*out = calloc(1, sizeof(**out));
	(*out)->window = w;
	live[OBJ_OUTPUT]++;
	return 0;
}

static void f_output_free(ACaptureSessionOutput *o)
{
	free(o);
	live[OBJ_OUTPUT]--;
}

static int f_target_create(ANativeWindow *w, ACameraOutputTarget **out)
{
	if (fails("ACameraOutputTarget_create"))
		return FAIL_CODE;
	*out = calloc(1, sizeof(**out));
	(*out)->window = w;
	live[OBJ_TARGET]++;
	return 0;
}

static void f_target_free(ACameraOutputTarget *t)
{
	free(t);
	live[OBJ_TARGET]--;
}

static int f_add_target(ACaptureRequest *r, const ACameraOutputTarget *t)
{
	if (fails("ACaptureRequest_addTarget"))
		return FAIL_CODE;
	say("ndk ACaptureRequest_addTarget %s %s %s", r->fc->id, template_name(r->template_id),
	    format_name(t->window->reader->format));
	return 0;
}

static int f_set_i32(ACaptureRequest *r, uint32_t tag, uint32_t count, const int32_t *v)
{
	say("ndk set %s %s %s=%d", r->fc->id, template_name(r->template_id), tag_name(tag), count ? v[0] : -1);
	return 0;
}

static int f_set_u8(ACaptureRequest *r, uint32_t tag, uint32_t count, const uint8_t *v)
{
	say("ndk set %s %s %s=%d", r->fc->id, template_name(r->template_id), tag_name(tag), count ? v[0] : -1);
	return 0;
}

static void f_request_free(ACaptureRequest *r)
{
	free(r);
	live[OBJ_REQUEST]--;
}

static int f_set_repeating(ACameraCaptureSession *s, ACameraCaptureSession_captureCallbacks *cb, int n,
			   ACaptureRequest **requests, int *seq)
{
	if (fails("ACameraCaptureSession_setRepeatingRequest")) {
		say("ndk ACameraCaptureSession_setRepeatingRequest %s failed", s->fc->id);
		return FAIL_CODE;
	}
	say("ndk ACameraCaptureSession_setRepeatingRequest %s %s n=%d", s->fc->id,
	    template_name(requests[0]->template_id), n);
	s->callbacks.onActive(s->callbacks.context, s);
	return 0;
}

static int f_stop_repeating(ACameraCaptureSession *s)
{
	say("ndk ACameraCaptureSession_stopRepeating %s", s->fc->id);
	s->callbacks.onReady(s->callbacks.context, s);
	return 0;
}

static int f_capture(ACameraCaptureSession *s, ACameraCaptureSession_captureCallbacks *cb, int n,
		     ACaptureRequest **requests, int *seq)
{
	if (fails("ACameraCaptureSession_capture")) {
		say("ndk ACameraCaptureSession_capture %s failed", s->fc->id);
		return FAIL_CODE;
	}
	pthread_mutex_lock(&ndk_lock);
	s->fc->capture_callbacks = *cb;
	s->fc->capturing = true;
	pthread_mutex_unlock(&ndk_lock);
	say("ndk ACameraCaptureSession_capture %s %s n=%d", s->fc->id, template_name(requests[0]->template_id), n);
	return 0;
}

static void f_session_close(ACameraCaptureSession *s)
{
	say("ndk ACameraCaptureSession_close %s", s->fc->id);
	pthread_mutex_lock(&ndk_lock);
	if (s->fc->session == s)
		s->fc->session = NULL;
	s->fc->capturing = false;
	pthread_mutex_unlock(&ndk_lock);
	/* The HAL tells of the closed session (late, from its own thread, on the phone) */
	if (!getenv("HARNESS_NO_SESSION_CLOSED"))
		s->callbacks.onClosed(s->callbacks.context, s);
	free(s);
	live[OBJ_SESSION]--;
}

static int f_reader_new(int32_t w, int32_t h, int32_t format, int32_t max, AImageReader **out)
{
	if (fails("AImageReader_new"))
		return FAIL_CODE;
	AImageReader *r = calloc(1, sizeof(*r));
	r->width = w;
	r->height = h;
	r->format = format;
	r->max_images = max;
	r->window.reader = r;
	live[OBJ_READER]++;
	say("ndk AImageReader_new %dx%d %s max=%d", w, h, format_name(format), max);
	*out = r;
	return 0;
}

static void f_reader_delete(AImageReader *r)
{
	say("ndk AImageReader_delete %s", format_name(r->format));
	pthread_mutex_lock(&ndk_lock);
	if (r->fc && r->fc->preview == r)
		r->fc->preview = NULL;
	if (r->fc && r->fc->still == r)
		r->fc->still = NULL;
	pthread_mutex_unlock(&ndk_lock);
	free(r);
	live[OBJ_READER]--;
}

static int f_reader_window(AImageReader *r, ANativeWindow **w)
{
	if (fails("AImageReader_getWindow"))
		return FAIL_CODE;
	*w = &r->window;
	return 0;
}

static int f_reader_listener(AImageReader *r, AImageReader_ImageListener *l)
{
	if (fails("AImageReader_setImageListener"))
		return FAIL_CODE;
	r->listener = *l;
	/* The context is the bridge's camera: that tells which fake camera the reader is of */
	struct camera *c = l->context;
	pthread_mutex_lock(&ndk_lock);
	r->fc = fake_camera(c->id);
	if (r->format == AIMAGE_FORMAT_JPEG)
		r->fc->still = r;
	else
		r->fc->preview = r;
	pthread_mutex_unlock(&ndk_lock);
	return 0;
}

static int f_acquire(AImageReader *r, AImage **out)
{
	if (fails("AImageReader_acquireLatestImage")) {
		*out = NULL;
		return FAIL_CODE;
	}
	*out = r->pending;
	r->pending = NULL;
	return 0;
}

static int f_image_width(const AImage *i, int32_t *v)
{
	if (fails("AImage_getWidth"))
		return FAIL_CODE;
	*v = i->width;
	return 0;
}

static int f_image_height(const AImage *i, int32_t *v)
{
	*v = i->height;
	return 0;
}

static int f_image_data(const AImage *i, int p, uint8_t **d, int *n)
{
	if (fails("AImage_getPlaneData"))
		return FAIL_CODE;
	*d = i->data[p];
	*n = i->size[p];
	return 0;
}

static int f_image_row(const AImage *i, int p, int32_t *v)
{
	*v = i->row_stride[p];
	return 0;
}

static int f_image_pixel(const AImage *i, int p, int32_t *v)
{
	*v = i->pixel_stride[p];
	return 0;
}

static void drop_image(AImage *i)
{
	free(i->data[0]);
	if (i->data[2] && i->data[1] != i->data[2] + 1)
		free(i->data[1]);
	free(i->data[2]);
	free(i);
	live[OBJ_IMAGE]--;
}

static void f_image_delete(AImage *i)
{
	say("ndk AImage_delete %dx%d", i->width, i->height);
	free(i->data[0]);
	if (i->data[2] && i->data[1] != i->data[2] + 1)
		free(i->data[1]);
	free(i->data[2]);
	free(i);
	live[OBJ_IMAGE]--;
}

static void f_start_thread_pool(void)
{
	say("ndk ABinderProcess_startThreadPool");
}

/* A preview frame as the HAL gives it: NV21, Y = x + y, V = x + 3y, U = 2x + y (mod 256) */
static AImage *preview_image(int width, int height)
{
	AImage *im = calloc(1, sizeof(*im));
	int w = width > WIDTH ? width : WIDTH; /* a narrower one keeps the default's stride */
	im->width = width;
	im->height = height;
	im->row_stride[0] = im->row_stride[1] = im->row_stride[2] = w;
	im->pixel_stride[0] = 1;
	im->pixel_stride[1] = im->pixel_stride[2] = 2;
	im->data[0] = malloc((size_t)w * height);
	im->size[0] = w * height;
	for (int y = 0; y < height; y++)
		for (int x = 0; x < w; x++)
			im->data[0][y * w + x] = x + y;
	uint8_t *vu = malloc((size_t)w * height / 2);
	for (int y = 0; y < height / 2; y++)
		for (int x = 0; x < w / 2; x++) {
			vu[y * w + 2 * x] = x + 3 * y;
			vu[y * w + 2 * x + 1] = 2 * x + y;
		}
	im->data[2] = vu;
	im->data[1] = vu + 1;
	im->size[1] = im->size[2] = w * height / 2 - 1;
	live[OBJ_IMAGE]++;
	return im;
}

/* A JPEG as the HAL gives it: one plane with the file */
static AImage *still_image(const char *id, bool empty)
{
	AImage *im = calloc(1, sizeof(*im));
	im->width = 4000;
	im->height = 3000;
	im->data[0] = malloc(8);
	memcpy(im->data[0], (const uint8_t[]){0xff, 0xd8, 'J', 'P', 'G', (uint8_t)id[0], 0xff, 0xd9}, 8);
	im->size[0] = empty ? 0 : 8;
	live[OBJ_IMAGE]++;
	return im;
}

static const struct {
	const char *name;
	void *fn;
} ndk_symbols[] = {
	{"ACameraManager_create", f_manager_create},
	{"ACameraManager_getCameraIdList", f_get_id_list},
	{"ACameraManager_deleteCameraIdList", f_delete_id_list},
	{"ACameraManager_getCameraCharacteristics", f_get_characteristics},
	{"ACameraManager_openCamera", f_open_camera},
	{"ACameraMetadata_getConstEntry", f_get_const_entry},
	{"ACameraMetadata_free", f_metadata_free},
	{"ACameraDevice_close", f_device_close},
	{"ACameraDevice_createCaptureRequest", f_create_request},
	{"ACameraDevice_createCaptureSession", f_create_session},
	{"ACaptureSessionOutputContainer_create", f_container_create},
	{"ACaptureSessionOutputContainer_free", f_container_free},
	{"ACaptureSessionOutputContainer_add", f_container_add},
	{"ACaptureSessionOutput_create", f_output_create},
	{"ACaptureSessionOutput_free", f_output_free},
	{"ACameraOutputTarget_create", f_target_create},
	{"ACameraOutputTarget_free", f_target_free},
	{"ACaptureRequest_addTarget", f_add_target},
	{"ACaptureRequest_setEntry_i32", f_set_i32},
	{"ACaptureRequest_setEntry_u8", f_set_u8},
	{"ACaptureRequest_free", f_request_free},
	{"ACameraCaptureSession_setRepeatingRequest", f_set_repeating},
	{"ACameraCaptureSession_stopRepeating", f_stop_repeating},
	{"ACameraCaptureSession_capture", f_capture},
	{"ACameraCaptureSession_close", f_session_close},
	{"AImageReader_new", f_reader_new},
	{"AImageReader_delete", f_reader_delete},
	{"AImageReader_getWindow", f_reader_window},
	{"AImageReader_setImageListener", f_reader_listener},
	{"AImageReader_acquireLatestImage", f_acquire},
	{"AImage_getWidth", f_image_width},
	{"AImage_getHeight", f_image_height},
	{"AImage_getPlaneData", f_image_data},
	{"AImage_getPlaneRowStride", f_image_row},
	{"AImage_getPlanePixelStride", f_image_pixel},
	{"AImage_delete", f_image_delete},
	{"ABinderProcess_startThreadPool", f_start_thread_pool},
};

void *fake_hybris_dlopen(const char *name, int flags)
{
	char rule[128];
	snprintf(rule, sizeof(rule), "dlopen:%s", name);
	say("hybris dlopen %s", name);
	return fails(rule) ? NULL : (void *)name;
}

void *fake_hybris_dlsym(void *handle, const char *name)
{
	char rule[128];
	snprintf(rule, sizeof(rule), "dlsym:%s", name);
	if (fails(rule))
		return NULL;
	for (size_t i = 0; i < SPA_N_ELEMENTS(ndk_symbols); i++)
		if (strcmp(ndk_symbols[i].name, name) == 0)
			return ndk_symbols[i].fn;
	say("hybris unknown symbol %s", name);
	return NULL;
}

const char *fake_hybris_dlerror(void)
{
	return "fake: library not found";
}

/* ----- fake PipeWire ----- */

struct fake_stream {
	const struct pw_stream_events *events;
	void *data;
	char name[64];
	uint32_t node_id;
	bool driving;
	char buffer_mode[16]; /* next dequeue: normal, none, small, nodata, nometa */
	uint8_t *mem;
	struct pw_buffer pwb;
	struct spa_buffer sb;
	struct spa_data d;
	struct spa_chunk chunk;
	struct spa_meta meta;
	struct spa_meta_videotransform vt;
	struct spa_io_position position;
};

#define MAX_STREAMS 16
static struct fake_stream *streams[MAX_STREAMS];
static int n_streams;

static struct fake_stream *stream_named(const char *name)
{
	for (int i = 0; i < n_streams; i++)
		if (streams[i] && strcmp(streams[i]->name, name) == 0)
			return streams[i];
	say("no stream %s", name);
	return NULL;
}

struct pw_stream *__wrap_pw_stream_new_simple(struct pw_loop *loop, const char *name, struct pw_properties *props,
					      const struct pw_stream_events *events, void *data)
{
	say("pw stream_new %s media.class=%s media.role=%s node.name=%s node.description=%s location=%s", name,
	    pw_properties_get(props, PW_KEY_MEDIA_CLASS), pw_properties_get(props, PW_KEY_MEDIA_ROLE),
	    pw_properties_get(props, PW_KEY_NODE_NAME), pw_properties_get(props, PW_KEY_NODE_DESCRIPTION),
	    pw_properties_get(props, "api.libcamera.location"));
	pw_properties_free(props);
	if (fails("pw_stream_new_simple"))
		return NULL;
	struct fake_stream *s = calloc(1, sizeof(*s));
	s->events = events;
	s->data = data;
	snprintf(s->name, sizeof(s->name), "%s", name);
	s->node_id = 100 + n_streams;
	s->driving = true;
	snprintf(s->buffer_mode, sizeof(s->buffer_mode), "normal");
	s->mem = calloc(1, MAX_FRAME_SIZE);
	streams[n_streams++] = s;
	return (struct pw_stream *)s;
}

int __wrap_pw_stream_connect(struct pw_stream *stream, enum pw_direction direction, uint32_t target,
			     enum pw_stream_flags flags, const struct spa_pod **params, uint32_t n)
{
	struct fake_stream *s = (struct fake_stream *)stream;
	struct spa_video_info_raw info = {0};
	uint32_t media_type = 0, media_subtype = 0;
	spa_format_parse(params[0], &media_type, &media_subtype);
	spa_format_video_raw_parse(params[0], &info);
	/* The sizes of the formats after the first */
	char offered[96] = "";
	for (uint32_t i = 1; i < n; i++) {
		struct spa_video_info_raw other = {0};
		spa_format_video_raw_parse(params[i], &other);
		snprintf(offered + strlen(offered), sizeof(offered) - strlen(offered), "%s%ux%u", i > 1 ? "," : " then ",
			 other.size.width, other.size.height);
	}
	say("pw stream_connect %s output=%d any=%d map=%d driver=%d params=%u video=%d raw=%d nv21=%d size=%ux%u%s rate=%u/%u",
	    s->name, direction == PW_DIRECTION_OUTPUT, target == PW_ID_ANY, !!(flags & PW_STREAM_FLAG_MAP_BUFFERS),
	    !!(flags & PW_STREAM_FLAG_DRIVER), n, media_type == SPA_MEDIA_TYPE_video,
	    media_subtype == SPA_MEDIA_SUBTYPE_raw, info.format == SPA_VIDEO_FORMAT_NV21, info.size.width,
	    info.size.height, offered, info.framerate.num, info.framerate.denom);
	return fails("pw_stream_connect") ? -EIO : 0;
}

int __wrap_pw_stream_update_params(struct pw_stream *stream, const struct spa_pod **params, uint32_t n)
{
	struct fake_stream *s = (struct fake_stream *)stream;
	struct spa_pod *buffers = NULL;
	int32_t blocks = 0, size = 0, stride = 0, meta_size = 0;
	uint32_t meta_type = 0, n_values = 0, choice = 0;
	spa_pod_parse_object(params[0], SPA_TYPE_OBJECT_ParamBuffers, NULL, SPA_PARAM_BUFFERS_buffers,
			     SPA_POD_Pod(&buffers), SPA_PARAM_BUFFERS_blocks, SPA_POD_Int(&blocks),
			     SPA_PARAM_BUFFERS_size, SPA_POD_Int(&size), SPA_PARAM_BUFFERS_stride, SPA_POD_Int(&stride));
	spa_pod_parse_object(params[1], SPA_TYPE_OBJECT_ParamMeta, NULL, SPA_PARAM_META_type, SPA_POD_Id(&meta_type),
			     SPA_PARAM_META_size, SPA_POD_Int(&meta_size));
	/* The parser hands out a choice's default only: the choice from the object itself */
	const struct spa_pod_prop *prop = spa_pod_find_prop(params[0], NULL, SPA_PARAM_BUFFERS_buffers);
	const int32_t *values = prop ? SPA_POD_BODY(spa_pod_get_values(&prop->value, &n_values, &choice)) : NULL;
	say("pw update_params %s n=%u buffers=%d(%d..%d) range=%d blocks=%d size=%d stride=%d meta=%s meta_size=%d",
	    s->name, n, values ? values[0] : -1, values && n_values > 1 ? values[1] : -1,
	    values && n_values > 2 ? values[2] : -1, choice == SPA_CHOICE_Range, blocks, size, stride,
	    meta_type == SPA_META_VideoTransform ? "VideoTransform" : "?", meta_size);
	return 0;
}

void __wrap_pw_stream_destroy(struct pw_stream *stream)
{
	struct fake_stream *s = (struct fake_stream *)stream;
	say("pw stream_destroy %s", s->name);
	for (int i = 0; i < n_streams; i++)
		if (streams[i] == s)
			streams[i] = NULL;
	free(s->mem);
	free(s);
}

uint32_t __wrap_pw_stream_get_node_id(struct pw_stream *stream)
{
	return ((struct fake_stream *)stream)->node_id;
}

bool __wrap_pw_stream_is_driving(struct pw_stream *stream)
{
	return ((struct fake_stream *)stream)->driving;
}

uint64_t __wrap_pw_stream_get_nsec(struct pw_stream *stream)
{
	return 5000000000ULL;
}

int __wrap_pw_stream_trigger_process(struct pw_stream *stream)
{
	struct fake_stream *s = (struct fake_stream *)stream;
	struct camera *c = s->data;
	struct spa_io_position *p = c->position;
	if (p)
		say("pw trigger %s nsec=%" PRIu64 " next=%" PRIu64 " delay=%" PRIi64 " rate_diff=%.1f no_rate=%d",
		    s->name, p->clock.nsec, p->clock.next_nsec, p->clock.delay, p->clock.rate_diff,
		    !!(p->clock.flags & SPA_IO_CLOCK_FLAG_NO_RATE));
	else
		say("pw trigger %s without position", s->name);
	return 0;
}

/* The data loop: invoke runs the function at once */
static struct spa_loop fake_spa_loop;
static int fake_invoke(void *object, spa_invoke_func_t func, uint32_t seq, const void *data, size_t size,
		       bool block, void *user_data)
{
	return func(&fake_spa_loop, false, seq, data, size, user_data);
}
static const struct spa_loop_methods fake_loop_methods = {SPA_VERSION_LOOP_METHODS, .invoke = fake_invoke};
static struct spa_loop fake_spa_loop = {
	SPA_INTERFACE_INIT(SPA_TYPE_INTERFACE_Loop, SPA_VERSION_LOOP, &fake_loop_methods, NULL)};
static struct pw_loop fake_data_loop = {.loop = &fake_spa_loop, .name = "fake-data-loop"};

struct pw_loop *__wrap_pw_stream_get_data_loop(struct pw_stream *stream)
{
	return &fake_data_loop;
}

struct pw_buffer *__wrap_pw_stream_dequeue_buffer(struct pw_stream *stream)
{
	struct fake_stream *s = (struct fake_stream *)stream;
	const char *mode = s->buffer_mode;
	if (strcmp(mode, "none") == 0)
		return NULL;
	struct camera *c = s->data;
	memset(s->mem, 0xee, NV21_SIZE(c->sensor_width, c->sensor_height));
	s->sb = (struct spa_buffer){.n_metas = strcmp(mode, "nometa") ? 1 : 0, .metas = &s->meta,
				    .n_datas = 1, .datas = &s->d};
	s->meta = (struct spa_meta){.type = SPA_META_VideoTransform, .size = sizeof(s->vt), .data = &s->vt};
	s->vt.transform = 99;
	s->chunk = (struct spa_chunk){0};
	s->d = (struct spa_data){.type = SPA_DATA_MemPtr, .data = strcmp(mode, "nodata") ? s->mem : NULL,
				 .maxsize = strcmp(mode, "small") ? MAX_FRAME_SIZE : FRAME_SIZE - 1, .chunk = &s->chunk};
	s->pwb.buffer = &s->sb;
	return &s->pwb;
}

int __wrap_pw_stream_queue_buffer(struct pw_stream *stream, struct pw_buffer *b)
{
	struct fake_stream *s = (struct fake_stream *)stream;
	struct camera *c = s->data;
	uint32_t w = c->width, h = c->height;
	const uint8_t *m = s->mem;
	const uint8_t *vu = m + w * h;
	if (s->chunk.size)
		say("pw queue %s offset=%u stride=%d size=%u transform=%u y=%d,%d,%d,%d vu=%d/%d,%d/%d,%d/%d", s->name,
		    s->chunk.offset, s->chunk.stride, s->chunk.size, s->vt.transform, m[0], m[1], m[w],
		    m[w * h - 1], vu[0], vu[1], vu[2], vu[3], vu[w], vu[w + 1]);
	else
		say("pw queue %s empty transform=%u first=%d", s->name, s->vt.transform, m[0]);
	return 0;
}

/* Context, core and registry */
static int fake_context_token, fake_core_destroyed;
static struct spa_hook_list registry_hooks;

struct fake_proxy {
	uint32_t id;
	struct spa_hook_list hooks;
};
static struct fake_proxy *proxies[64];

static struct pw_registry *fake_get_registry(void *object, uint32_t version, size_t size);
static void *fake_registry_bind(void *object, uint32_t id, const char *type, uint32_t version, size_t size);
static int fake_registry_add_listener(void *object, struct spa_hook *listener, const struct pw_registry_events *events,
				      void *data);

static const struct pw_core_methods fake_core_methods = {PW_VERSION_CORE_METHODS, .get_registry = fake_get_registry};
static struct spa_interface fake_core =
	SPA_INTERFACE_INIT(PW_TYPE_INTERFACE_Core, PW_VERSION_CORE, &fake_core_methods, NULL);
static const struct pw_registry_methods fake_registry_methods = {
	PW_VERSION_REGISTRY_METHODS, .add_listener = fake_registry_add_listener, .bind = fake_registry_bind};
static struct spa_interface fake_registry =
	SPA_INTERFACE_INIT(PW_TYPE_INTERFACE_Registry, PW_VERSION_REGISTRY, &fake_registry_methods, NULL);

struct pw_context *__wrap_pw_context_new(struct pw_loop *loop, struct pw_properties *props, size_t size)
{
	say("pw context_new");
	if (props)
		pw_properties_free(props);
	return fails("pw_context_new") ? NULL : (struct pw_context *)&fake_context_token;
}

struct pw_core *__wrap_pw_context_connect(struct pw_context *context, struct pw_properties *props, size_t size)
{
	say("pw context_connect");
	if (props)
		pw_properties_free(props);
	return fails("pw_context_connect") ? NULL : (struct pw_core *)&fake_core;
}

static struct pw_registry *fake_get_registry(void *object, uint32_t version, size_t size)
{
	say("pw get_registry");
	return fails("pw_core_get_registry") ? NULL : (struct pw_registry *)&fake_registry;
}

static int fake_registry_add_listener(void *object, struct spa_hook *listener, const struct pw_registry_events *events,
				      void *data)
{
	say("pw registry_add_listener");
	spa_hook_list_append(&registry_hooks, listener, events, data);
	return 0;
}

static void *fake_registry_bind(void *object, uint32_t id, const char *type, uint32_t version, size_t size)
{
	say("pw bind %u %s", id, type);
	if (fails("pw_registry_bind"))
		return NULL;
	for (size_t i = 0; i < SPA_N_ELEMENTS(proxies); i++)
		if (!proxies[i]) {
			struct fake_proxy *p = calloc(1, sizeof(*p));
			p->id = id;
			spa_hook_list_init(&p->hooks);
			proxies[i] = p;
			return p;
		}
	return NULL;
}

void __wrap_pw_proxy_add_object_listener(struct pw_proxy *proxy, struct spa_hook *listener, const void *funcs,
					 void *data)
{
	struct fake_proxy *p = (struct fake_proxy *)proxy;
	say("pw proxy_listener %u", p->id);
	spa_hook_list_append(&p->hooks, listener, funcs, data);
}

void __wrap_pw_proxy_destroy(struct pw_proxy *proxy)
{
	if ((void *)proxy == (void *)&fake_registry) {
		say("pw registry_destroy listeners=%d", spa_list_is_empty(&registry_hooks.list) ? 0 : 1);
		return;
	}
	struct fake_proxy *p = (struct fake_proxy *)proxy;
	say("pw proxy_destroy %u listeners=%d", p->id, spa_list_is_empty(&p->hooks.list) ? 0 : 1);
	for (size_t i = 0; i < SPA_N_ELEMENTS(proxies); i++)
		if (proxies[i] == p)
			proxies[i] = NULL;
	free(p);
}

int __wrap_pw_core_disconnect(struct pw_core *core)
{
	say("pw core_disconnect");
	fake_core_destroyed = 1;
	return 0;
}

void __wrap_pw_context_destroy(struct pw_context *context)
{
	say("pw context_destroy");
}

static struct fake_proxy *proxy_of(uint32_t id)
{
	for (size_t i = 0; i < SPA_N_ELEMENTS(proxies); i++)
		if (proxies[i] && proxies[i]->id == id)
			return proxies[i];
	return NULL;
}

/* ----- the system services on the test's private bus ----- */

static sd_bus *svc_bus;
static char accel_orientation[32] = "normal";
static int svc_wake = -1;
static pthread_mutex_t svc_lock = PTHREAD_MUTEX_INITIALIZER;
static bool svc_emit;

static int svc_get_orientation(sd_bus *bus, const char *path, const char *interface, const char *property,
			       sd_bus_message *reply, void *userdata, sd_bus_error *error)
{
	pthread_mutex_lock(&svc_lock);
	say("sensorproxy get %s", accel_orientation);
	int r = sd_bus_message_append(reply, "s", accel_orientation);
	pthread_mutex_unlock(&svc_lock);
	return r;
}

static int svc_accelerometer(sd_bus_message *m, void *userdata, sd_bus_error *error)
{
	say("sensorproxy %s", sd_bus_message_get_member(m));
	return sd_bus_reply_method_return(m, "");
}

static int svc_count_use(sd_bus_message *m, void *userdata, sd_bus_error *error)
{
	const char *app, *what;
	uint32_t pid;
	int r = sd_bus_message_read(m, "sus", &app, &pid, &what);
	if (r < 0)
		return r;
	say("appaccess CountUse app=%s pid=%u what=%s", app, pid, what);
	if (getenv("HARNESS_APPACCESS_FAIL"))
		return sd_bus_error_set(error, SD_BUS_ERROR_ACCESS_DENIED, "refused by the test");
	return sd_bus_reply_method_return(m, "");
}

/* What the bridge says on the session bus: PropertiesChanged of org.plinius.CameraBridge */
static int svc_bridge_changed(sd_bus_message *m, void *userdata, sd_bus_error *error)
{
	const char *iface, *name;
	char line[256] = "";
	if (sd_bus_message_read(m, "s", &iface) < 0 || sd_bus_message_enter_container(m, 'a', "{sv}") < 0)
		return 0;
	while (sd_bus_message_enter_container(m, 'e', "sv") > 0) {
		char type;
		const char *contents;
		sd_bus_message_read(m, "s", &name);
		sd_bus_message_peek_type(m, &type, &contents);
		size_t len = strlen(line);
		if (contents && strcmp(contents, "b") == 0) {
			int b;
			sd_bus_message_read(m, "v", "b", &b);
			snprintf(line + len, sizeof(line) - len, " %s=%s", name, b ? "true" : "false");
		} else {
			const char *s;
			sd_bus_message_read(m, "v", "s", &s);
			snprintf(line + len, sizeof(line) - len, " %s=%s", name, s);
		}
		sd_bus_message_exit_container(m);
	}
	say("bridge-changed%s", line);
	return 0;
}

static const sd_bus_vtable sensor_proxy_vtable[] = {
	SD_BUS_VTABLE_START(0),
	SD_BUS_PROPERTY("AccelerometerOrientation", "s", svc_get_orientation, 0, SD_BUS_VTABLE_PROPERTY_EMITS_CHANGE),
	SD_BUS_METHOD("ClaimAccelerometer", "", "", svc_accelerometer, 0),
	SD_BUS_METHOD("ReleaseAccelerometer", "", "", svc_accelerometer, 0),
	SD_BUS_VTABLE_END,
};

static const sd_bus_vtable appaccess_vtable[] = {
	SD_BUS_VTABLE_START(0),
	SD_BUS_METHOD("CountUse", "sus", "", svc_count_use, 0),
	SD_BUS_VTABLE_END,
};

static void *svc_thread(void *data)
{
	for (;;) {
		pthread_mutex_lock(&svc_lock);
		bool emit = svc_emit;
		svc_emit = false;
		pthread_mutex_unlock(&svc_lock);
		if (emit)
			sd_bus_emit_properties_changed(svc_bus, SENSOR_PROXY_PATH, SENSOR_PROXY,
						       "AccelerometerOrientation", NULL);
		while (sd_bus_process(svc_bus, NULL) > 0)
			;
		/* What to wait for, once everything read is handled */
		struct pollfd fds[2] = {{.fd = sd_bus_get_fd(svc_bus), .events = sd_bus_get_events(svc_bus)},
					{.fd = svc_wake, .events = POLLIN}};
		uint64_t until;
		int timeout = -1;
		if (sd_bus_get_timeout(svc_bus, &until) >= 0 && until != UINT64_MAX)
			timeout = 10;
		if (poll(fds, 2, timeout) > 0 && (fds[1].revents & POLLIN)) {
			uint64_t n;
			if (__real_read(svc_wake, &n, sizeof(n)) < 0)
				break;
		}
	}
	return NULL;
}

/* Connects to the test's bus and serves what the environment asks for */
static void svc_start(void)
{
	const char *address = getenv("HARNESS_BUS");
	if (!address)
		return;
	svc_wake = eventfd(0, EFD_CLOEXEC);
	if (sd_bus_new(&svc_bus) < 0 || sd_bus_set_address(svc_bus, address) < 0 ||
	    sd_bus_set_bus_client(svc_bus, 1) < 0 || sd_bus_start(svc_bus) < 0) {
		say("no test bus at %s", address);
		exit(2);
	}
	if (!getenv("HARNESS_NO_SENSORPROXY")) {
		sd_bus_add_object_vtable(svc_bus, NULL, SENSOR_PROXY_PATH, SENSOR_PROXY, sensor_proxy_vtable, NULL);
		sd_bus_request_name(svc_bus, SENSOR_PROXY, 0);
	}
	if (!getenv("HARNESS_NO_APPACCESS")) {
		sd_bus_add_object_vtable(svc_bus, NULL, APPACCESS_PATH, APPACCESS_NAME, appaccess_vtable, NULL);
		sd_bus_request_name(svc_bus, APPACCESS_NAME, 0);
	}
	/* Someone else has the bridge's name already */
	if (getenv("HARNESS_TAKE_BRIDGE_NAME"))
		sd_bus_request_name(svc_bus, BRIDGE_NAME, 0);
	sd_bus_match_signal(svc_bus, NULL, NULL, BRIDGE_PATH, "org.freedesktop.DBus.Properties", "PropertiesChanged",
			    svc_bridge_changed, NULL);
	pthread_t t;
	pthread_create(&t, NULL, svc_thread, NULL);
	pthread_detach(t);
}

static void svc_set_orientation(const char *value)
{
	pthread_mutex_lock(&svc_lock);
	snprintf(accel_orientation, sizeof(accel_orientation), "%s", value);
	svc_emit = true;
	pthread_mutex_unlock(&svc_lock);
	uint64_t one = 1;
	if (__real_write(svc_wake, &one, sizeof(one)) < 0)
		say("cannot wake the service thread");
}

/* ----- commands of a running bridge ----- */

static struct camera *camera_of_stream(const char *name)
{
	struct fake_stream *s = stream_named(name);
	return s ? s->data : NULL;
}

static enum pw_stream_state state_named(const char *name)
{
	if (strcmp(name, "streaming") == 0)
		return PW_STREAM_STATE_STREAMING;
	if (strcmp(name, "paused") == 0)
		return PW_STREAM_STATE_PAUSED;
	if (strcmp(name, "error") == 0)
		return PW_STREAM_STATE_ERROR;
	return PW_STREAM_STATE_UNCONNECTED;
}

/* global <id> <link|node|client|port> [key=value ...]: a global of the registry, "-" for no props */
static void fire_global(char **a, int n)
{
	static const struct {
		const char *word, *type;
	} types[] = {{"link", PW_TYPE_INTERFACE_Link},
		     {"node", PW_TYPE_INTERFACE_Node},
		     {"client", PW_TYPE_INTERFACE_Client},
		     {"port", PW_TYPE_INTERFACE_Port}};
	const char *type = "?";
	for (size_t i = 0; i < SPA_N_ELEMENTS(types); i++)
		if (strcmp(a[2], types[i].word) == 0)
			type = types[i].type;
	struct spa_dict_item items[8];
	int k = 0;
	bool none = n > 3 && strcmp(a[3], "-") == 0;
	for (int i = 3; !none && i < n && k < 8; i++) {
		char *eq = strchr(a[i], '=');
		if (!eq)
			continue;
		*eq = '\0';
		const char *key = a[i];
		if (strcmp(key, "out") == 0)
			key = PW_KEY_LINK_OUTPUT_NODE;
		else if (strcmp(key, "in") == 0)
			key = PW_KEY_LINK_INPUT_NODE;
		else if (strcmp(key, "client") == 0)
			key = PW_KEY_CLIENT_ID;
		else if (strcmp(key, "pid") == 0)
			key = PW_KEY_SEC_PID;
		else if (strcmp(key, "access") == 0)
			key = PW_KEY_ACCESS;
		items[k++] = SPA_DICT_ITEM_INIT(key, eq + 1);
	}
	struct spa_dict dict = SPA_DICT_INIT(items, k);
	spa_hook_list_call(&registry_hooks, struct pw_registry_events, global, 0, (uint32_t)atoi(a[1]), 0x1ff, type, 3,
			   none ? NULL : &dict);
}

static void fire_frame(const char *id, int width, int height)
{
	struct fake_camera *fc = fake_camera(id);
	pthread_mutex_lock(&ndk_lock);
	AImageReader *r = fc ? fc->preview : NULL;
	if (r)
		r->pending = preview_image(width, height);
	pthread_mutex_unlock(&ndk_lock);
	if (!r) {
		say("no preview reader for %s", id);
		return;
	}
	r->listener.onImageAvailable(r->listener.context, r);
	/* An image the bridge did not take (the reader failed to hand it out): gone silently */
	if (r->pending) {
		drop_image(r->pending);
		r->pending = NULL;
	}
}

static void fire_still(const char *id, bool empty)
{
	struct fake_camera *fc = fake_camera(id);
	pthread_mutex_lock(&ndk_lock);
	AImageReader *r = fc ? fc->still : NULL;
	if (r)
		r->pending = still_image(id, empty);
	pthread_mutex_unlock(&ndk_lock);
	if (!r) {
		say("no still reader for %s", id);
		return;
	}
	r->listener.onImageAvailable(r->listener.context, r);
	if (r->pending) {
		drop_image(r->pending);
		r->pending = NULL;
	}
}

static void command(char *line)
{
	char *a[16] = {0};
	int n = 0;
	char copy[512];
	snprintf(copy, sizeof(copy), "%s", line);
	for (char *save, *t = strtok_r(copy, " ", &save); t && n < 16; t = strtok_r(NULL, " ", &save))
		a[n++] = t;
	if (n == 0)
		return;
	struct fake_stream *s;
	struct camera *c;
	struct fake_camera *fc;
	if (strcmp(a[0], "stream") == 0 && n == 3 && (s = stream_named(a[1]))) {
		s->events->state_changed(s->data, PW_STREAM_STATE_PAUSED, state_named(a[2]), NULL);
	} else if (strcmp(a[0], "io") == 0 && n >= 2 && (s = stream_named(a[1]))) {
		/* io <stream> [clock]: the position area, or an area the bridge does not use */
		if (n > 2)
			s->events->io_changed(s->data, SPA_IO_Clock, &s->position.clock, sizeof(s->position.clock));
		else
			s->events->io_changed(s->data, SPA_IO_Position, &s->position, sizeof(s->position));
	} else if (strcmp(a[0], "param") == 0 && (n == 3 || n == 4) && (s = stream_named(a[1]))) {
		/* param <stream> format|null|props [<width>x<height>]: the format the consumer picked */
		uint8_t buffer[256];
		struct spa_pod_builder b = SPA_POD_BUILDER_INIT(buffer, sizeof(buffer));
		unsigned fw = 0, fh = 0;
		const struct spa_pod *format =
			n == 4 && sscanf(a[3], "%ux%u", &fw, &fh) == 2
				? spa_pod_builder_add_object(
					  &b, SPA_TYPE_OBJECT_Format, SPA_PARAM_Format, SPA_FORMAT_mediaType,
					  SPA_POD_Id(SPA_MEDIA_TYPE_video), SPA_FORMAT_mediaSubtype,
					  SPA_POD_Id(SPA_MEDIA_SUBTYPE_raw), SPA_FORMAT_VIDEO_format,
					  SPA_POD_Id(SPA_VIDEO_FORMAT_NV21), SPA_FORMAT_VIDEO_size,
					  SPA_POD_Rectangle(&SPA_RECTANGLE(fw, fh)))
				: spa_pod_builder_add_object(
					  &b, SPA_TYPE_OBJECT_Format, SPA_PARAM_Format, SPA_FORMAT_mediaType,
					  SPA_POD_Id(SPA_MEDIA_TYPE_video), SPA_FORMAT_mediaSubtype,
					  SPA_POD_Id(SPA_MEDIA_SUBTYPE_raw), SPA_FORMAT_VIDEO_format,
					  SPA_POD_Id(SPA_VIDEO_FORMAT_NV21));
		if (strcmp(a[2], "format") == 0)
			s->events->param_changed(s->data, SPA_PARAM_Format, format);
		else if (strcmp(a[2], "null") == 0)
			s->events->param_changed(s->data, SPA_PARAM_Format, NULL);
		else
			s->events->param_changed(s->data, SPA_PARAM_Props, format);
	} else if (strcmp(a[0], "driving") == 0 && n == 3 && (s = stream_named(a[1]))) {
		s->driving = atoi(a[2]);
	} else if (strcmp(a[0], "nodeid") == 0 && n == 3 && (s = stream_named(a[1]))) {
		s->node_id = strcmp(a[2], "invalid") == 0 ? SPA_ID_INVALID : (uint32_t)atoi(a[2]);
	} else if (strcmp(a[0], "buffer") == 0 && n == 3 && (s = stream_named(a[1]))) {
		snprintf(s->buffer_mode, sizeof(s->buffer_mode), "%s", a[2]);
	} else if (strcmp(a[0], "process") == 0 && n == 2 && (s = stream_named(a[1]))) {
		s->events->process(s->data);
	} else if (strcmp(a[0], "frame") == 0 && n >= 2) {
		/* frame <camera> [<width> [<height>]] */
		fire_frame(a[1], n > 2 ? atoi(a[2]) : WIDTH, n > 3 ? atoi(a[3]) : HEIGHT);
	} else if (strcmp(a[0], "still") == 0 && n >= 2) {
		fire_still(a[1], n > 2 && strcmp(a[2], "empty") == 0);
	} else if (strcmp(a[0], "stillfail") == 0 && n == 2 && (fc = fake_camera(a[1]))) {
		pthread_mutex_lock(&ndk_lock);
		ACameraCaptureSession_captureCallbacks cb = fc->capture_callbacks;
		ACameraCaptureSession *session = fc->session;
		pthread_mutex_unlock(&ndk_lock);
		ACameraCaptureFailure failure = {.frameNumber = 7, .reason = 1, .sequenceId = 1};
		cb.onCaptureFailed(cb.context, session, NULL, &failure);
	} else if ((strcmp(a[0], "disconnect") == 0 || strcmp(a[0], "error") == 0) && n == 2 &&
		   (fc = fake_camera(a[1]))) {
		pthread_mutex_lock(&ndk_lock);
		ACameraDevice *d = fc->device;
		ACameraDevice_StateCallbacks cb = d ? d->callbacks : (ACameraDevice_StateCallbacks){0};
		pthread_mutex_unlock(&ndk_lock);
		if (!d)
			say("no device %s", a[1]);
		else if (a[0][0] == 'd')
			cb.onDisconnected(cb.context, d);
		else
			cb.onError(cb.context, d, 4);
	} else if (strcmp(a[0], "caller") == 0 && n == 2) {
		atomic_store(&caller, strcmp(a[1], "real") == 0 ? 0 : (unsigned)atoi(a[1]));
	} else if (strcmp(a[0], "global") == 0 && n >= 3) {
		fire_global(a, n);
	} else if (strcmp(a[0], "globals") == 0 && n == 3) {
		/* globals <first id> <count>: nodes of a client nobody streams to */
		for (int i = 0; i < atoi(a[2]); i++) {
			char id[16], client[] = "client=999999";
			snprintf(id, sizeof(id), "%d", atoi(a[1]) + i);
			char *g[] = {"global", id, "node", client};
			fire_global(g, 4);
		}
	} else if (strcmp(a[0], "remove") == 0 && n == 2) {
		spa_hook_list_call(&registry_hooks, struct pw_registry_events, global_remove, 0, (uint32_t)atoi(a[1]));
	} else if (strcmp(a[0], "clientinfo") == 0 && n == 3) {
		/* clientinfo <id> <app id|-|none>: - without properties, none without the app id */
		struct fake_proxy *p = proxy_of(atoi(a[1]));
		struct spa_dict_item item = SPA_DICT_ITEM_INIT(strcmp(a[2], "none") ? "pipewire.access.portal.app_id"
										       : "application.name",
							       a[2]);
		struct spa_dict dict = SPA_DICT_INIT(&item, 1);
		struct pw_client_info info = {.id = atoi(a[1]), .change_mask = PW_CLIENT_CHANGE_MASK_PROPS,
					      .props = strcmp(a[2], "-") ? &dict : NULL};
		if (!p)
			say("no proxy %s", a[1]);
		else
			spa_hook_list_call(&p->hooks, struct pw_client_events, info, 0, &info);
	} else if (strcmp(a[0], "orient") == 0 && n == 2) {
		svc_set_orientation(a[1]);
	} else if (strcmp(a[0], "fail") == 0 && n == 2) {
		add_rule(a[1]);
	} else if (strcmp(a[0], "unfail") == 0 && n == 2) {
		drop_rule(a[1]);
	} else if (strcmp(a[0], "count") == 0 && n == 2) {
		say("count %s=%d", a[1], count_of(a[1]));
	} else if (strcmp(a[0], "objects") == 0) {
		char out[256] = "";
		for (int i = 0; i < OBJ_KINDS; i++) {
			size_t len = strlen(out);
			snprintf(out + len, sizeof(out) - len, " %s=%d", obj_names[i], atomic_load(&live[i]));
		}
		say("objects%s", out);
	} else if (strcmp(a[0], "rotation") == 0 && n == 2 && (c = camera_of_stream(a[1]))) {
		say("rotation %s device=%d front_on=%d streaming=%d", a[1], atomic_load(&c->bridge->device_rotation),
		    atomic_load(&c->bridge->front_on), atomic_load(&c->bridge->streaming));
	} else if (strcmp(a[0], "capture") == 0 && n == 2 && (c = camera_of_stream(a[1]))) {
		pthread_mutex_lock(&c->lock);
		say("capture %s state=%d", a[1], c->capture);
		pthread_mutex_unlock(&c->lock);
	} else if (strcmp(a[0], "quit") == 0) {
		say("ok %s", line);
		kill(getpid(), SIGTERM);
		return;
	} else {
		say("bad command %s", line);
	}
	say("ok %s", line);
}

/* Reads stdin by its descriptor: a thread waiting in stdio would hold stdin's lock, on which
 * exit() waits */
static void *control_thread(void *data)
{
	char line[512];
	size_t len = 0;
	for (;;) {
		char ch;
		ssize_t n = __real_read(0, &ch, 1);
		if (n <= 0)
			break;
		if (ch != '\n') {
			if (len < sizeof(line) - 1)
				line[len++] = ch;
			continue;
		}
		line[len] = '\0';
		len = 0;
		command(line);
	}
	return NULL;
}

/* ----- single checks without the daemon ----- */

static struct bridge hb;

static struct camera *unit_camera(const char *id, int facing, int orientation)
{
	struct camera *c = &hb.cameras[hb.n_cameras++];
	snprintf(c->id, sizeof(c->id), "%s", id);
	snprintf(c->name, sizeof(c->name), "hal_camera_%s", id);
	snprintf(c->description, sizeof(c->description), "test");
	c->facing = facing;
	c->orientation = orientation;
	connect_camera(&hb, c);
	return c;
}

static int unit(char *arg)
{
	char *a[8] = {0};
	int n = 0;
	if (strncmp(arg, "unit:", 5) == 0) {
		/* unit:<cgroup leaf> (which may hold colons): the application id the bridge reads from it */
		char app[APP_ID_SIZE];
		say("app=%s", app_of_unit(arg + 5, app, sizeof(app)) ? app : "-");
		return 0;
	}
	for (char *save, *t = strtok_r(arg, ":", &save); t && n < 8; t = strtok_r(NULL, ":", &save))
		a[n++] = t;
	if (strcmp(a[0], "jpeg") == 0) {
		/* jpeg:<path>: write_jpeg of a small file into the folder of path */
		static const uint8_t data[] = {0xff, 0xd8, 1, 2, 3, 0xff, 0xd9};
		char *slash = strrchr(a[1], '/');
		*slash = '\0';
		int dir = open(a[1], O_RDONLY | O_DIRECTORY | O_CLOEXEC);
		say("jpeg=%d", dir < 0 ? -errno : write_jpeg(dir, slash + 1, data, sizeof(data)));
		if (dir >= 0)
			close(dir);
	} else if (strcmp(a[0], "longjpeg") == 0) {
		char name[300];
		memset(name, 'x', sizeof(name) - 1);
		name[sizeof(name) - 1] = '\0';
		say("jpeg=%d", write_jpeg(AT_FDCWD, name, (const uint8_t *)"x", 1));
	} else if (strcmp(a[0], "pictures") == 0) {
		/* pictures: the pictures folder as the bridge finds it ("-": none) */
		char dir[PATH_MAX];
		say("pictures=%s", pictures_dir(dir, sizeof(dir)) ? dir : "-");
	} else if (strcmp(a[0], "target") == 0) {
		/* target:<path>: picture_target, the name it found and what the folder is */
		int dir = -1;
		size_t name = 0;
		int res = picture_target(a[1], &dir, &name);
		char link[64], folder[PATH_MAX] = "-";
		snprintf(link, sizeof(link), "/proc/self/fd/%d", dir);
		ssize_t len = res == 0 ? readlink(link, folder, sizeof(folder) - 1) : -1;
		if (len >= 0)
			folder[len] = '\0';
		say("target=%d", res);
		say("name=%s", res == 0 ? a[1] + name : "-");
		say("folder=%s", folder);
		if (dir >= 0)
			close(dir);
	} else if (strcmp(a[0], "wake") == 0) {
		/* the orientation thread's eventfd is gone */
		hb.wake_fd = -1;
		wake_orientation_thread(&hb);
		say("woken");
	} else if (strcmp(a[0], "queue") == 0) {
		/* queue:<links>: uses of one camera's stream when nobody takes them off the queue */
		struct camera *c = unit_camera("0", FACING_BACK, 90);
		struct consumers *w = &hb.consumers;
		pthread_mutex_init(&w->lock, NULL);
		pthread_cond_init(&w->cond, NULL);
		w->registry = (struct pw_registry *)&fake_registry;
		for (int i = 0; i < atoi(a[1]); i++) {
			char client[16], node[16], link[16], pid[16], client_id[16], out[16], in[16];
			snprintf(client, sizeof(client), "%d", 1000 + i);
			snprintf(node, sizeof(node), "%d", 2000 + i);
			snprintf(link, sizeof(link), "%d", 3000 + i);
			snprintf(pid, sizeof(pid), "%d", 500 + i);
			snprintf(client_id, sizeof(client_id), "%d", 1000 + i);
			snprintf(out, sizeof(out), "%u", pw_stream_get_node_id(c->stream));
			snprintf(in, sizeof(in), "%d", 2000 + i);
			struct spa_dict_item ci[] = {SPA_DICT_ITEM_INIT(PW_KEY_SEC_PID, pid)};
			struct spa_dict_item ni[] = {SPA_DICT_ITEM_INIT(PW_KEY_CLIENT_ID, client_id)};
			struct spa_dict_item li[] = {SPA_DICT_ITEM_INIT(PW_KEY_LINK_OUTPUT_NODE, out),
						     SPA_DICT_ITEM_INIT(PW_KEY_LINK_INPUT_NODE, in)};
			on_global(w, atoi(client), 0, PW_TYPE_INTERFACE_Client, 3, &SPA_DICT_INIT_ARRAY(ci));
			on_global(w, atoi(node), 0, PW_TYPE_INTERFACE_Node, 3, &SPA_DICT_INIT_ARRAY(ni));
			on_global(w, atoi(link), 0, PW_TYPE_INTERFACE_Link, 3, &SPA_DICT_INIT_ARRAY(li));
		}
		report_consumers(c);
		say("queued=%d first=%u last=%u", w->queued, w->queue[0].pid, w->queue[w->queued - 1].pid);
	} else if (strcmp(a[0], "badmsg") == 0) {
		/* A method call or property value that does not carry what the vtable says */
		sd_bus *bus = NULL;
		sd_bus_message *m = NULL;
		sd_bus_error error = SD_BUS_ERROR_NULL;
		unit_camera("0", FACING_BACK, 90);
		if (sd_bus_open_user(&bus) < 0 ||
		    sd_bus_message_new_method_call(bus, &m, BRIDGE_NAME, BRIDGE_PATH, BRIDGE_NAME, "TakePicture") < 0 ||
		    sd_bus_message_append(m, "i", 5) < 0 || sd_bus_message_seal(m, 1, 0) < 0) {
			say("no test bus");
			return 1;
		}
		sd_bus_message_rewind(m, true);
		say("set=%d", set_setting(bus, BRIDGE_PATH, BRIDGE_NAME, "SceneMode", m, &hb, &error));
		sd_bus_message_rewind(m, true);
		say("take=%d", take_picture_call(m, &hb, &error));
		sd_bus_message_unref(m);
		sd_bus_unref(bus);
	} else {
		say("bad check %s", a[0]);
		return 1;
	}
	return 0;
}

int main(int argc, char **argv)
{
	spa_hook_list_init(&registry_hooks);
	add_rules(getenv("HARNESS_FAIL"));
	parse_cameras(getenv("HARNESS_CAMERAS"));
	if (argc > 1 && strcmp(argv[1], "daemon") == 0) {
		/* The bridge waits for its signals in sigwait: no other thread may take them */
		sigset_t signals;
		sigemptyset(&signals);
		sigaddset(&signals, SIGINT);
		sigaddset(&signals, SIGTERM);
		pthread_sigmask(SIG_BLOCK, &signals, NULL);
		svc_start();
		pthread_t t;
		pthread_create(&t, NULL, control_thread, NULL);
		pthread_detach(t);
		struct rlimit fds;
		getrlimit(RLIMIT_NOFILE, &fds);
		if (getenv("HARNESS_NO_FDS")) {
			/* Not one more file descriptor (until the bridge gave up: gcov needs some) */
			int next = dup(0);
			close(next);
			struct rlimit none = {next, fds.rlim_max};
			setrlimit(RLIMIT_NOFILE, &none);
		}
		int res = real_main(argc - 1, argv + 1);
		setrlimit(RLIMIT_NOFILE, &fds);
		say("exit %d", res);
		return res;
	}
	pw_init(NULL, NULL);
	hb.loop = pw_thread_loop_new("harness", NULL);
	hb.wake_fd = eventfd(0, EFD_CLOEXEC);
	for (int i = 1; i < argc; i++)
		if (unit(argv[i]))
			return 1;
	return 0;
}
