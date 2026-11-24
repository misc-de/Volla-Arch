# plinius-face front camera (face-camera.c) without the phone: libhybris' camera layer, EGL and
# GL are stand-ins in the harness that record every call and deliver preview frames, texture
# updates and errors when a test says so, as the camera service does from its own threads.
# What is checked: which camera is chosen, how it is set up and torn down, that the GL thread
# updates the preview texture whenever asked (else the camera's queue stalls), that each frame is
# handed out once with its arrival time, timeouts, and every way opening can fail.

import pytest

from dhw_harness import FAKES, PKG, build_harness, run

FACE = PKG / "plinius-face"

HARNESS = r"""
#include <errno.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES2/gl2.h>
#include <GLES2/gl2ext.h>
#include <hybris/camera/camera_compatibility_layer.h>
#include <hybris/camera/camera_compatibility_layer_capabilities.h>

/* ---- Recorded calls ---- */

static pthread_mutex_t log_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t log_cond = PTHREAD_COND_INITIALIZER;
static int updates;

static void
logf_ (const char *fmt, ...)
{
	va_list args;

	pthread_mutex_lock (&log_lock);
	va_start (args, fmt);
	vprintf (fmt, args);
	va_end (args);
	putchar ('\n');
	pthread_mutex_unlock (&log_lock);
}

static struct {
	int devices;
	int facing[4], orientation[4], info_rc[4];
	int egl_init, egl_bind, egl_context, make_current, connect;
	int fail_malloc, fail_calloc;
} cfg = { 2, { 0, 1 }, { 90, 270 }, { 0, 0 }, 1, 1, 1, 1, 1, 0, 0 };

static struct CameraControlListener listener;
static int control_dummy;

/* ---- libhybris camera ---- */

int
android_camera_get_number_of_devices (void)
{
	return cfg.devices;
}

int
android_camera_get_device_info (int32_t id, int *facing, int *orientation)
{
	logf_ ("info %d", id);
	*facing = cfg.facing[id];
	*orientation = cfg.orientation[id];
	return cfg.info_rc[id];
}

struct CameraControl *
android_camera_connect_by_id (int32_t id, struct CameraControlListener *l)
{
	logf_ ("connect %d", id);
	if (!cfg.connect)
		return NULL;
	listener = *l;
	return (struct CameraControl *) &control_dummy;
}

void
android_camera_disconnect (struct CameraControl *c)
{
	logf_ ("disconnect");
}

void
android_camera_set_preview_texture (struct CameraControl *c, int texture)
{
	logf_ ("preview_texture %d", texture);
}

void
android_camera_update_preview_texture (struct CameraControl *c)
{
	pthread_mutex_lock (&log_lock);
	updates++;
	pthread_cond_broadcast (&log_cond);
	pthread_mutex_unlock (&log_lock);
}

void
android_camera_start_preview (struct CameraControl *c)
{
	logf_ ("start_preview");
}

void
android_camera_stop_preview (struct CameraControl *c)
{
	logf_ ("stop_preview");
}

int
android_camera_set_preview_callback_mode (struct CameraControl *c, PreviewCallbackMode mode)
{
	logf_ ("callback_mode %s", mode == PREVIEW_CALLBACK_ENABLED ? "enabled" : "disabled");
	return 0;
}

void
android_camera_set_preview_size (struct CameraControl *c, int w, int h)
{
	logf_ ("preview_size %dx%d", w, h);
}

void
android_camera_set_flash_mode (struct CameraControl *c, FlashMode mode)
{
	logf_ ("flash %s", mode == FLASH_MODE_OFF ? "off" : "on");
}

void
android_camera_set_auto_focus_mode (struct CameraControl *c, AutoFocusMode mode)
{
	logf_ ("focus %s", mode == AUTO_FOCUS_MODE_CONTINUOUS_VIDEO ? "continuous-video" : "other");
}

/* ---- EGL and GL ---- */

static int display_dummy, context_dummy;

EGLDisplay
eglGetDisplay (EGLNativeDisplayType id)
{
	logf_ ("egl_display %s", id == EGL_DEFAULT_DISPLAY ? "default" : "other");
	return (EGLDisplay) &display_dummy;
}

EGLBoolean
eglInitialize (EGLDisplay d, EGLint *major, EGLint *minor)
{
	return cfg.egl_init;
}

EGLBoolean
eglBindAPI (EGLenum api)
{
	logf_ ("egl_bind %s", api == EGL_OPENGL_ES_API ? "gles" : "other");
	return cfg.egl_bind;
}

EGLContext
eglCreateContext (EGLDisplay d, EGLConfig config, EGLContext share, const EGLint *attrs)
{
	logf_ ("egl_context config=%s version=%d", config == EGL_NO_CONFIG_KHR ? "none" : "some",
	       attrs[0] == EGL_CONTEXT_CLIENT_VERSION ? attrs[1] : -1);
	return cfg.egl_context ? (EGLContext) &context_dummy : EGL_NO_CONTEXT;
}

EGLBoolean
eglMakeCurrent (EGLDisplay d, EGLSurface draw, EGLSurface read, EGLContext ctx)
{
	logf_ ("egl_current %s", ctx == EGL_NO_CONTEXT ? "none" : "context");
	return ctx == EGL_NO_CONTEXT ? EGL_TRUE : cfg.make_current;
}

EGLBoolean
eglDestroyContext (EGLDisplay d, EGLContext ctx)
{
	logf_ ("egl_destroy");
	return EGL_TRUE;
}

void
glGenTextures (GLsizei n, GLuint *textures)
{
	*textures = 7;
}

void
glBindTexture (GLenum target, GLuint texture)
{
	logf_ ("bind %s %u", target == GL_TEXTURE_EXTERNAL_OES ? "external" : "other", texture);
}

/* Out of memory for the camera's state or its frame */
static void *
camera_malloc (size_t size)
{
	return cfg.fail_malloc ? NULL : malloc (size);
}

static void *
camera_calloc (size_t n, size_t size)
{
	return cfg.fail_calloc ? NULL : calloc (n, size);
}

#define malloc camera_malloc
#define calloc camera_calloc
#include "face-camera.c"
#undef malloc
#undef calloc

static int64_t
now_ns (void)
{
	struct timespec ts;

	clock_gettime (CLOCK_MONOTONIC, &ts);
	return (int64_t) ts.tv_sec * 1000000000 + ts.tv_nsec;
}

int
main (void)
{
	static char line[256];
	static uint8_t frame[FRAME_SIZE + 16], dst[FRAME_SIZE];
	FaceCamera *camera = NULL;

	setvbuf (stdout, NULL, _IOLBF, 0);
	while (fgets (line, sizeof line, stdin)) {
		char key[32];
		int n1, n2, n3, n4;

		line[strcspn (line, "\n")] = '\0';
		if (sscanf (line, "devices %d", &n1) == 1) {
			cfg.devices = n1;
		} else if (sscanf (line, "device %d %d %d %d", &n1, &n2, &n3, &n4) == 4) {
			cfg.info_rc[n1] = n4;
			cfg.facing[n1] = n2;
			cfg.orientation[n1] = n3;
		} else if (sscanf (line, "fail %31s", key) == 1) {
			if (!strcmp (key, "init"))
				cfg.egl_init = 0;
			else if (!strcmp (key, "bind"))
				cfg.egl_bind = 0;
			else if (!strcmp (key, "context"))
				cfg.egl_context = 0;
			else if (!strcmp (key, "current"))
				cfg.make_current = 0;
			else if (!strcmp (key, "connect"))
				cfg.connect = 0;
			else if (!strcmp (key, "malloc"))
				cfg.fail_malloc = 1;
			else if (!strcmp (key, "calloc"))
				cfg.fail_calloc = 1;
		} else if (!strcmp (line, "open")) {
			char *error = NULL;

			camera = face_camera_open (&error);
			if (camera)
				logf_ ("open ok");
			else
				logf_ ("open failed: %s", error);
			free (error);
		} else if (!strcmp (line, "open-quiet")) {
			camera = face_camera_open (NULL);
			logf_ ("open %s", camera ? "ok" : "failed");
		} else if (!strcmp (line, "format")) {
			int w, h, o;
			size_t bytes;

			face_camera_format (camera, &w, &h, &o, &bytes);
			logf_ ("format %dx%d orientation %d bytes %zu", w, h, o, bytes);
		} else if (sscanf (line, "frame %d %d", &n1, &n2) == 2) {
			/* size, first byte: as the camera service calls it, from its own thread */
			memset (frame, n2, sizeof frame);
			listener.on_preview_frame_cb (frame, (uint32_t) n1, listener.context);
		} else if (!strcmp (line, "texture")) {
			struct timespec until;
			int before, ok = 1;

			pthread_mutex_lock (&log_lock);
			before = updates;
			pthread_mutex_unlock (&log_lock);
			listener.on_preview_texture_needs_update_cb (listener.context);
			clock_gettime (CLOCK_REALTIME, &until);
			until.tv_sec += 5;
			pthread_mutex_lock (&log_lock);
			while (updates == before && ok)
				ok = pthread_cond_timedwait (&log_cond, &log_lock, &until) == 0;
			pthread_mutex_unlock (&log_lock);
			logf_ ("texture updated %d", updates - before);
		} else if (!strcmp (line, "error")) {
			listener.on_msg_error_cb (listener.context);
		} else if (sscanf (line, "next %d %31s", &n1, key) == 2) {
			int64_t start = now_ns (), ts = -1;
			int rc;

			memset (dst, 0, sizeof dst);
			rc = face_camera_next_frame (camera, dst, n1, strcmp (key, "ts") ? NULL : &ts);
			logf_ ("next %d byte %d last %d waited>=%d stamp %s", rc, dst[0], dst[FRAME_SIZE - 1],
			       (now_ns () - start) >= (int64_t) n1 * 1000000,
			       ts < 0 ? "-" : ts >= start - 10000000000LL && ts <= now_ns () ? "ok" : "bad");
		} else if (!strcmp (line, "close")) {
			face_camera_close (camera);
			camera = NULL;
			logf_ ("closed");
		} else if (!strcmp (line, "closenull")) {
			face_camera_close (NULL);
			logf_ ("closed");
		} else {
			logf_ ("<unknown command: %.60s>", line);
		}
	}
	return 0;
}
"""


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    d = tmp_path_factory.mktemp("face-camera")
    return build_harness(d, "face-camera", HARNESS, pkgs=(),
                         flags=[f"-I{FAKES / 'face-camera'}", f"-I{FACE}", "-lpthread"])


def script(harness, tmp_path, *cmds):
    """Runs the commands (on stdin, which run() has no argument for); returns the output lines."""
    f = tmp_path / "cmds"
    f.write_text("\n".join(cmds) + "\n")
    r = run("sh", "-c", 'exec "$0" < "$1"', harness, f)
    assert "<unknown command" not in r.stdout, r.stdout
    return r.stdout.splitlines()


FRAME = 1280 * 720 * 3 // 2

SETUP = [
    "bind external 7", "connect 1", "flash off", "focus continuous-video",
    "preview_size 1280x720", "preview_texture 7", "callback_mode enabled", "start_preview",
]
TEARDOWN = ["callback_mode disabled", "stop_preview", "disconnect", "egl_current none",
            "egl_destroy"]


def test_opens_the_front_camera_and_sets_up_the_preview(harness, tmp_path):
    out = script(harness, tmp_path, "open", "format", "close")
    assert out == ["info 0", "info 1", "egl_display default", "egl_bind gles",
                   "egl_context config=none version=2", "egl_current context", *SETUP,
                   "open ok", "format 1280x720 orientation 270 bytes 1382400", *TEARDOWN,
                   "closed"]


def test_first_front_camera_wins(harness, tmp_path):
    # Camera 0 does not answer, 1 is a back camera, 2 and 3 face the front
    out = script(harness, tmp_path, "devices 4", "device 0 1 0 -1", "device 1 0 90 0",
                 "device 2 1 180 0", "device 3 1 0 0", "open", "format", "close")
    assert [line for line in out if line.startswith("info")] == ["info 0", "info 1", "info 2"]
    assert "connect 2" in out
    assert "format 1280x720 orientation 180 bytes 1382400" in out


@pytest.mark.parametrize("devices", [["devices 0"], ["devices 1", "device 0 0 90 0"],
                                     ["devices 1", "device 0 1 270 -22"]])
def test_no_front_camera(harness, tmp_path, devices):
    out = script(harness, tmp_path, *devices, "open")
    assert out[-1] == "open failed: no front camera"
    assert not any(line.startswith(("egl", "connect")) for line in out)


@pytest.mark.parametrize("failure,message", [
    ("init", "no EGL"), ("bind", "no EGL"),
    ("malloc", "out of memory"), ("calloc", "out of memory"),
])
def test_open_fails_early(harness, tmp_path, failure, message):
    out = script(harness, tmp_path, f"fail {failure}", "open")
    assert out[-1] == f"open failed: {message}"
    assert not any(line.startswith(("egl_context", "connect")) for line in out)


@pytest.mark.parametrize("failure,steps", [
    # No GL context: never connects, nothing to make current or destroy afterwards
    ("context", ["egl_context config=none version=2"]),
    ("current", ["egl_context config=none version=2", "egl_current context", "egl_current none",
                 "egl_destroy"]),
    ("connect", ["egl_context config=none version=2", "egl_current context", "bind external 7",
                 "connect 1", "egl_current none", "egl_destroy"]),
])
def test_camera_busy_or_no_gl(harness, tmp_path, failure, steps):
    out = script(harness, tmp_path, f"fail {failure}", "open")
    assert out[-1] == "open failed: cannot open the front camera (busy?)"
    middle = out[out.index("egl_bind gles") + 1:-1]
    assert middle == steps
    assert "start_preview" not in out and "disconnect" not in out


def test_failure_without_an_error_pointer(harness, tmp_path):
    out = script(harness, tmp_path, "devices 0", "open-quiet")
    assert out == ["open failed"]


def test_each_frame_is_handed_out_once(harness, tmp_path):
    out = script(harness, tmp_path, "open", f"frame {FRAME} 42", "next 1000 ts", "next 50 ts",
                 f"frame {FRAME} 43", f"frame {FRAME} 44", "next 1000 -", "close")
    nexts = [line for line in out if line.startswith("next")]
    assert nexts == [
        "next 1 byte 42 last 42 waited>=0 stamp ok",
        # Nothing new: waits out the timeout
        "next 0 byte 0 last 0 waited>=1 stamp -",
        # Two frames since: the newest one, once
        "next 1 byte 44 last 44 waited>=0 stamp -",
    ]


def test_frames_of_another_size_are_ignored(harness, tmp_path):
    out = script(harness, tmp_path, "open", f"frame {FRAME - 1} 9", f"frame {FRAME + 1} 9",
                 "next 30 ts", "close")
    assert "next 0 byte 0 last 0 waited>=1 stamp -" in out


@pytest.mark.parametrize("timeout", [999, 1500])
def test_timeouts_across_a_second(harness, tmp_path, timeout):
    # The deadline's nanoseconds carry into the seconds
    out = script(harness, tmp_path, "open", f"next {timeout} ts", "close")
    assert "next 0 byte 0 last 0 waited>=1 stamp -" in out


def test_texture_updates_happen_on_the_gl_thread(harness, tmp_path):
    out = script(harness, tmp_path, "open", "texture", "texture", "texture", "close")
    assert out.count("texture updated 1") == 3


def test_camera_error_ends_the_frames(harness, tmp_path):
    out = script(harness, tmp_path, "open", f"frame {FRAME} 5", "error", "next 1000 ts",
                 "next 1000 ts", "close")
    nexts = [line for line in out if line.startswith("next")]
    # Even a frame not yet taken: the camera failed
    assert nexts == ["next -1 byte 0 last 0 waited>=0 stamp -"] * 2
    assert out[-len(TEARDOWN) - 1:] == [*TEARDOWN, "closed"]


def test_close_of_nothing(harness, tmp_path):
    assert script(harness, tmp_path, "closenull") == ["closed"]
