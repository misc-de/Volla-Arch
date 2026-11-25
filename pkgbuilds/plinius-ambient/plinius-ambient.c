// SPDX-License-Identifier: MIT
// plinius-ambient: animated effects over the background of Phosh - falling snow, dust in a beam
// of light, fireflies, rain, or balls and water that roll and run as the phone is tilted.
//
// A wlr-layer-shell surface with the namespace "plinius-ambient", with an empty input region so
// that taps go through, drawn with OpenGL ES 2 and transparent apart from the effect. phoc-plinius
// draws it right after each of Phosh's backgrounds (under the windows, under the app grid and on
// the lock screen) and sends it frame callbacks only while it can be seen: under an app or with
// the display off the animation stops by itself, as it is paced by those callbacks.
//
// Power: the balls and the water stop drawing once they lie still (but for one frame a second).
// Each effect runs at its own frame rate (30-60 per second), the soft ones at half the panel's
// resolution.
//
// Settings: org.plinius.ambient (GNOME Settings > Appearance). The tilt for the balls and the
// water comes from sensor-proxy-bridge's gravity vector, the water's sloshing from its linear
// acceleration (Motion), the 360° photo's view from its game rotation vector (GameOrientation,
// bridge 0.9: the gyroscope alone follows a turn at once and keeps still on a table, where the
// rotation vector with the magnetometer lagged and crept on; with an older bridge Orientation);
// each is claimed only while an effect that follows it is on screen.
//
// Backgrounds: the 360° photo (panorama.c) or a video (video.c) instead of Phosh's picture, with
// the effect over it (layered.c). The video plays only while it can be seen: frame callbacks
// coming and the display on (MCE's display_status_ind, the always-on display counting as off).
#define G_LOG_DOMAIN "plinius-ambient"

#include "effects.h"
#include "layered.h"
#include "panorama.h"
#include "render.h"
#include "video.h"

#include <EGL/egl.h>
#include <GLES2/gl2.h>
#include <gio/gio.h>
#include <glib-unix.h>
#include <math.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <wayland-client.h>
#include <wayland-egl.h>

#include "viewporter-client-protocol.h"
#include "wlr-layer-shell-unstable-v1-client-protocol.h"

#define SETTINGS_SCHEMA "org.plinius.ambient"

#define GRAVITY_BUS_NAME "net.hadess.SensorProxy"
#define GRAVITY_PATH "/net/hadess/SensorProxy/Gravity"
#define GRAVITY_IFACE "org.plinius.SensorProxyBridge.Gravity"
#define STANDARD_GRAVITY 9.80665f
/* Share of each new sample in the smoothed gravity vector (at 50 Hz a time constant of 22 ms) */
#define GRAVITY_SMOOTHING 0.6f
/* Linear acceleration: one sample every 20 ms (sensor-proxy-bridge asks for 50 Hz). A single
 * knock (the phone put down on a table: up to 35 m/s², measured) counts no more than this. The
 * shake level is its square smoothed over SHAKE_TIME seconds. */
#define MOTION_PERIOD 0.02f
#define MOTION_CLIP 12.0f
#define SHAKE_TIME 0.8f

/* The display state (plinius-mce) */
#define MCE_BUS_NAME "com.nokia.mce"
#define MCE_REQUEST_PATH "/com/nokia/mce/request"
#define MCE_REQUEST_IFACE "com.nokia.mce.request"
#define MCE_SIGNAL_PATH "/com/nokia/mce/signal"
#define MCE_SIGNAL_IFACE "com.nokia.mce.signal"

/* Without a frame callback for this long after drawing, the effect is not on screen (under an
 * app, display off) */
#define STALL_MS 1000
/* An effect at rest is still drawn this often: the frame callbacks tell whether it is on screen
 * (else the tilt sensor would stay on under an app) */
#define RESTING_INTERVAL_MS 1000

static struct {
	GMainLoop *loop;

	/* Wayland */
	struct wl_display *display;
	struct wl_compositor *compositor;
	struct wl_output *output;
	struct zwlr_layer_shell_v1 *layer_shell;
	struct wp_viewporter *viewporter;
	int output_scale;                /* integer: rounded up from a fractional scale */
	int mode_width, mode_height;     /* the panel's pixels */
	enum wl_output_transform transform;

	/* The surface, while an effect is chosen */
	struct wl_surface *surface;
	struct zwlr_layer_surface_v1 *layer_surface;
	struct wp_viewport *viewport;
	struct wl_egl_window *egl_window;
	EGLSurface egl_surface;
	int width, height;               /* logical, from the compositor */
	int buffer_width, buffer_height; /* what is drawn */
	gboolean configured;

	/* EGL, set up with the first surface */
	EGLDisplay egl_display;
	EGLConfig egl_config;
	EGLContext egl_context;
	struct renderer renderer;

	/* The effect */
	GSettings *settings;
	const struct effect *effect;
	void *state;
	struct scene scene;

	/* Drawing */
	gboolean stalled;       /* no frame callback after the last frame: not on screen */
	gboolean resting;       /* the effect does not move: drawn once a second only */
	gboolean display_on;    /* MCE: "off" also with the always-on display */
	int told_visible;       /* what the effect was last told (set_visible), -1 nothing yet */
	struct wl_callback *frame_callback;
	guint draw_timer;
	guint stall_timer;
	gint64 last_draw, last_step; /* µs, monotonic */

	/* Tilt */
	GDBusConnection *system_bus;
	gboolean gravity_claimed;
	float gravity_device[3]; /* smoothed, sensor axes */
	gboolean has_gravity;
	gboolean motion_claimed;
	gboolean orientation_claimed;
	/* "GameOrientation", or "Orientation" once the bridge turned that down */
	const char *orientation_sensor;
	char *picture; /* org.plinius.ambient panorama */
	char *video;   /* org.plinius.ambient video */
} app = {
	.output_scale = 1,
	.display_on = TRUE,
	.told_visible = -1,
	.scene.gravity = {0, STANDARD_GRAVITY},
	.orientation_sensor = "GameOrientation",
};

static void update_activity(void);
static void destroy_surface(void);

/* ---- Visibility ---- */

/* Whether the effect should be moving right now (phoc decides about apps over it) */
static gboolean active(void)
{
	return app.effect && app.configured && app.egl_surface != EGL_NO_SURFACE;
}

static void update_sensor_claims(void);

/* Whether the effect can be seen, told to it on every change (a video plays only then) */
static void update_visibility(void)
{
	update_sensor_claims();
	int visible = active() && !app.stalled && app.display_on;
	if (app.state && app.effect->set_visible && visible != app.told_visible) {
		app.told_visible = visible;
		g_debug("Background %s", visible ? "seen" : "hidden");
		app.effect->set_visible(app.state, visible);
	}
}

/* The effect's state, built anew: it has been told nothing yet */
static void create_state(void)
{
	app.state = app.effect->create(&app.scene);
	app.told_visible = -1;
}

static void set_display(const char *status)
{
	gboolean on = g_strcmp0(status, "off") != 0;
	if (on == app.display_on)
		return;
	app.display_on = on;
	g_debug("Display %s", status);
	update_visibility();
}

static void on_display_status(GDBusConnection *bus, const char *sender, const char *path, const char *iface,
			      const char *signal, GVariant *params, gpointer user_data)
{
	const char *status;
	if (g_variant_is_of_type(params, G_VARIANT_TYPE("(s)"))) {
		g_variant_get(params, "(&s)", &status);
		set_display(status);
	}
}

static void on_display_status_reply(GObject *source, GAsyncResult *result, gpointer user_data)
{
	g_autoptr(GError) error = NULL;
	g_autoptr(GVariant) reply = g_dbus_connection_call_finish(G_DBUS_CONNECTION(source), result, &error);
	const char *status;
	if (!reply) {
		/* No MCE: the display counts as on, the frame callbacks alone decide */
		g_debug("No display state: %s", error->message);
		return;
	}
	g_variant_get(reply, "(&s)", &status);
	set_display(status);
}

/* ---- Tilt ---- */

/* A vector in the sensor's axes (x right, y up) turned by the output transform into screen axes
 * (x right, y down) */
static void to_screen(float sensor_x, float sensor_y, float out[2])
{
	/* In panel coordinates */
	float x = sensor_x, y = -sensor_y;
	switch (app.transform & 3) {
	case WL_OUTPUT_TRANSFORM_90:
		out[0] = -y;
		out[1] = x;
		break;
	case WL_OUTPUT_TRANSFORM_180:
		out[0] = -x;
		out[1] = -y;
		break;
	case WL_OUTPUT_TRANSFORM_270:
		out[0] = y;
		out[1] = -x;
		break;
	default:
		out[0] = x;
		out[1] = y;
	}
}

/* The smoothed gravity vector in the screen plane; the sensor's vector points against gravity */
static void update_scene_gravity(void)
{
	if (!app.has_gravity) {
		app.scene.gravity[0] = 0;
		app.scene.gravity[1] = STANDARD_GRAVITY;
		return;
	}
	to_screen(-app.gravity_device[0], -app.gravity_device[1], app.scene.gravity);
}

static void schedule_draw(void);

/* An effect at rest moves again if the new sensor values are enough for it */
static void wake_effect(void)
{
	if (app.resting && app.effect && app.effect->wake && app.effect->wake(app.state, &app.scene)) {
		app.resting = FALSE;
		app.last_step = g_get_monotonic_time();
		/* The next frame at the full rate, not at the resting one */
		g_clear_handle_id(&app.draw_timer, g_source_remove);
		schedule_draw();
	}
}

static void on_gravity(GDBusConnection *bus, const char *sender, const char *path, const char *iface,
		       const char *signal, GVariant *params, gpointer user_data)
{
	double v[3];
	g_variant_get(params, "(ddd)", &v[0], &v[1], &v[2]);
	for (int i = 0; i < 3; i++)
		app.gravity_device[i] = app.has_gravity
						? app.gravity_device[i] + (v[i] - app.gravity_device[i]) * GRAVITY_SMOOTHING
						: v[i];
	app.has_gravity = TRUE;
	update_scene_gravity();
	wake_effect();
}

/* The linear acceleration (m/s², sensor axes, zero at rest): the shake level and the change of
 * speed since the last step */
static void on_motion(GDBusConnection *bus, const char *sender, const char *path, const char *iface,
		      const char *signal, GVariant *params, gpointer user_data)
{
	double v[3];
	g_variant_get(params, "(ddd)", &v[0], &v[1], &v[2]);
	if (!app.motion_claimed)
		return;
	float a = sqrtf(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]);
	float clip = a > MOTION_CLIP ? MOTION_CLIP / a : 1;
	a *= clip;
	app.scene.shake += (a * a - app.scene.shake) * (1 - expf(-MOTION_PERIOD / SHAKE_TIME));
	float in_plane[2];
	to_screen(v[0] * clip, v[1] * clip, in_plane);
	app.scene.impulse[0] += in_plane[0] * MOTION_PERIOD;
	app.scene.impulse[1] += in_plane[1] * MOTION_PERIOD;
	wake_effect();
}

/* The phone's orientation as a unit quaternion (x, y, z, w) */
static void on_orientation(GDBusConnection *bus, const char *sender, const char *path, const char *iface,
			   const char *signal, GVariant *params, gpointer user_data)
{
	double q[4];
	g_variant_get(params, "(dddd)", &q[0], &q[1], &q[2], &q[3]);
	/* Muga may claim the other one: its samples are not ours */
	if (!app.orientation_claimed || strcmp(signal, app.orientation_sensor))
		return;
	for (int i = 0; i < 4; i++)
		app.scene.orientation[i] = q[i];
	app.scene.has_orientation = TRUE;
	wake_effect();
}

static void call_sensor(const char *method);

static void on_claim_done(GObject *source, GAsyncResult *result, gpointer user_data)
{
	g_autoptr(GError) error = NULL;
	g_autoptr(GVariant) reply = g_dbus_connection_call_finish(G_DBUS_CONNECTION(source), result, &error);
	if (reply)
		return;
	g_message("Sensor not available (%s): %s", (const char *)user_data, error->message);
	/* A bridge before 0.9, or no game rotation vector: the rotation vector instead */
	if (!strcmp(user_data, "ClaimGameOrientation") && !strcmp(app.orientation_sensor, "GameOrientation")) {
		app.orientation_sensor = "Orientation";
		if (app.orientation_claimed)
			call_sensor("ClaimOrientation");
	}
}

/* Claim or Release with the orientation sensor in use */
static void call_orientation(gboolean claim)
{
	if (!strcmp(app.orientation_sensor, "GameOrientation"))
		call_sensor(claim ? "ClaimGameOrientation" : "ReleaseGameOrientation");
	else
		call_sensor(claim ? "ClaimOrientation" : "ReleaseOrientation");
}

static void call_sensor(const char *method)
{
	g_dbus_connection_call(app.system_bus, GRAVITY_BUS_NAME, GRAVITY_PATH, GRAVITY_IFACE, method, NULL, NULL,
			       G_DBUS_CALL_FLAGS_NONE, -1, NULL, on_claim_done, (gpointer)method);
}

static void reset_motion(void)
{
	app.scene.shake = 0;
	app.scene.impulse[0] = app.scene.impulse[1] = 0;
}

/* The sensors run only while an effect that follows them is on screen */
static void update_sensor_claims(void)
{
	gboolean on = app.system_bus && app.effect && active() && !app.stalled;
	gboolean want_gravity = on && app.effect->wants_gravity, want_motion = on && app.effect->wants_motion;
	gboolean want_orientation = on && app.effect->wants_orientation;
	if (want_gravity != app.gravity_claimed) {
		app.gravity_claimed = want_gravity;
		g_debug("Tilt sensor %s", want_gravity ? "claimed" : "released");
		call_sensor(want_gravity ? "ClaimGravity" : "ReleaseGravity");
		if (!want_gravity)
			app.has_gravity = FALSE;
	}
	if (want_motion != app.motion_claimed) {
		app.motion_claimed = want_motion;
		g_debug("Motion sensor %s", want_motion ? "claimed" : "released");
		call_sensor(want_motion ? "ClaimMotion" : "ReleaseMotion");
		reset_motion();
	}
	if (want_orientation != app.orientation_claimed) {
		app.orientation_claimed = want_orientation;
		g_debug("Orientation sensor %s", want_orientation ? "claimed" : "released");
		call_orientation(want_orientation);
		/* Claimed anew, the 360° photo re-centres on the first sample */
		app.scene.has_orientation = FALSE;
	}
}

/* sensor-proxy-bridge (re)started: claims of the old one are gone */
static void on_gravity_appeared(GDBusConnection *bus, const char *name, const char *owner, gpointer user_data)
{
	if (app.gravity_claimed)
		call_sensor("ClaimGravity");
	if (app.motion_claimed)
		call_sensor("ClaimMotion");
	if (app.orientation_claimed)
		call_orientation(TRUE);
}

/* ---- GL ---- */

static gboolean egl_init(void)
{
	static const EGLint config_attribs[] = {
		EGL_SURFACE_TYPE, EGL_WINDOW_BIT, EGL_RENDERABLE_TYPE, EGL_OPENGL_ES2_BIT,
		EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8, EGL_BLUE_SIZE, 8, EGL_ALPHA_SIZE, 8, EGL_NONE,
	};
	static const EGLint context_attribs[] = {EGL_CONTEXT_CLIENT_VERSION, 2, EGL_NONE};
	EGLint n = 0;
	if (app.egl_display != EGL_NO_DISPLAY)
		return TRUE;
	app.egl_display = eglGetDisplay((EGLNativeDisplayType)app.display);
	if (app.egl_display == EGL_NO_DISPLAY || !eglInitialize(app.egl_display, NULL, NULL)) {
		g_warning("Cannot initialize EGL (0x%x)", eglGetError());
		app.egl_display = EGL_NO_DISPLAY;
		return FALSE;
	}
	eglBindAPI(EGL_OPENGL_ES_API);
	if (!eglChooseConfig(app.egl_display, config_attribs, &app.egl_config, 1, &n) || n < 1) {
		g_warning("No EGL config with alpha");
		return FALSE;
	}
	app.egl_context = eglCreateContext(app.egl_display, app.egl_config, EGL_NO_CONTEXT, context_attribs);
	if (app.egl_context == EGL_NO_CONTEXT) {
		g_warning("Cannot create a GLES 2 context (0x%x)", eglGetError());
		return FALSE;
	}
	return TRUE;
}

/* ---- Drawing ---- */

static void on_frame_done(void *data, struct wl_callback *callback, uint32_t time);
static const struct wl_callback_listener frame_listener = {on_frame_done};

static gboolean on_stall(gpointer user_data)
{
	app.stall_timer = 0;
	if (!app.frame_callback)
		return G_SOURCE_REMOVE;
	/* The compositor does not show the effect: an app over it, or the display off. The pending
	 * callback ends this. */
	app.stalled = TRUE;
	g_debug("No frame callback: not on screen");
	update_visibility();
	return G_SOURCE_REMOVE;
}

static void draw(void)
{
	gint64 now = g_get_monotonic_time();
	float dt = CLAMP((now - app.last_step) / 1e6f, 0.0f, 0.1f);
	app.last_step = now;
	app.last_draw = now;
	if (!eglMakeCurrent(app.egl_display, app.egl_surface, app.egl_surface, app.egl_context)) {
		g_warning("eglMakeCurrent failed (0x%x)", eglGetError());
		return;
	}
	gint64 t0 = g_get_monotonic_time();
	if (!app.effect->step(app.state, &app.scene, dt))
		app.resting = TRUE;
	/* Taken in by this step (or dropped at rest) */
	app.scene.impulse[0] = app.scene.impulse[1] = 0;
	gint64 t1 = g_get_monotonic_time();
	/* PLINIUS_AMBIENT_TINT=1: a faint red over everything, to see where the surface is */
	render_frame(&app.renderer, app.effect, app.state, &app.scene, app.buffer_width, app.buffer_height,
		     g_getenv("PLINIUS_AMBIENT_TINT") != NULL);
	gint64 t2 = g_get_monotonic_time();

	/* Paced by our own frame callbacks (swap interval 0 does not wait for them) */
	app.frame_callback = wl_surface_frame(app.surface);
	wl_callback_add_listener(app.frame_callback, &frame_listener, NULL);
	if (!eglSwapBuffers(app.egl_display, app.egl_surface))
		g_warning("eglSwapBuffers failed (0x%x)", eglGetError());

	/* G_MESSAGES_DEBUG=plinius-ambient: what a frame costs, every 5 s */
	static gint64 since, step_us, render_us, swap_us;
	static int frames;
	step_us += t1 - t0;
	render_us += t2 - t1;
	swap_us += g_get_monotonic_time() - t2;
	frames++;
	if (now - since >= 5 * G_USEC_PER_SEC) {
		if (since)
			g_debug("%d frames in %.1f s: step %.2f ms, drawing %.2f ms, swap %.2f ms per frame", frames,
				(now - since) / 1e6, step_us / 1e3 / frames, render_us / 1e3 / frames, swap_us / 1e3 / frames);
		since = now;
		step_us = render_us = swap_us = 0;
		frames = 0;
	}
	if (!app.stall_timer)
		app.stall_timer = g_timeout_add(STALL_MS, on_stall, NULL);
}

static gboolean on_draw_timer(gpointer user_data)
{
	app.draw_timer = 0;
	if (active() && !app.frame_callback)
		draw();
	return G_SOURCE_REMOVE;
}

/* The next frame, at the effect's frame rate after the last one (once a second at rest) */
static void schedule_draw(void)
{
	if (!active() || app.frame_callback || app.draw_timer)
		return;
	gint64 interval = app.resting ? RESTING_INTERVAL_MS * 1000 : G_USEC_PER_SEC / app.effect->fps;
	gint64 wait = app.last_draw + interval - g_get_monotonic_time();
	app.draw_timer = g_timeout_add(MAX(wait, 0) / 1000, on_draw_timer, NULL);
}

static void on_frame_done(void *data, struct wl_callback *callback, uint32_t time)
{
	wl_callback_destroy(callback);
	app.frame_callback = NULL;
	g_clear_handle_id(&app.stall_timer, g_source_remove);
	if (app.stalled) {
		/* On screen again: do not jump ahead by the time it was hidden */
		app.stalled = FALSE;
		app.last_step = g_get_monotonic_time();
		update_visibility();
	}
	schedule_draw();
}

/* Something changed that may start or stop the animation */
static void update_activity(void)
{
	static gboolean was_active;
	gboolean now_active = active();
	if (now_active && !was_active) {
		app.last_step = g_get_monotonic_time();
		app.resting = FALSE;
	}
	if (!now_active)
		g_clear_handle_id(&app.draw_timer, g_source_remove);
	if (now_active != was_active)
		g_debug("%s", now_active ? "Animating" : "Paused");
	was_active = now_active;
	update_visibility();
	schedule_draw();
}

/* scene.wake: an effect with a new frame of its own (the video), also at rest */
static void request_frame(void)
{
	if (!active())
		return;
	if (app.resting) {
		app.resting = FALSE;
		app.last_step = g_get_monotonic_time();
		/* The next frame at the full rate, not at the resting one */
		g_clear_handle_id(&app.draw_timer, g_source_remove);
	}
	schedule_draw();
}

/* ---- Surface ---- */

/* Buffer size for the logical size. With the viewporter: the panel's own pixels (a fractional
 * scale such as 2.27 would otherwise be drawn at 3), the soft effects at half of that. Without
 * it: the integer output scale. */
static void update_buffer_size(void)
{
	float scale = app.output_scale;
	int panel_width = app.transform & 1 ? app.mode_height : app.mode_width;
	int panel_height = app.transform & 1 ? app.mode_width : app.mode_height;
	gboolean panel_known = app.viewporter && panel_width > 0;
	if (panel_known) {
		scale = (float)panel_width / app.width;
		if (!app.effect->full_resolution)
			scale = MAX(1.0f, scale * 0.5f);
	}
	app.scene.scale = scale;
	if (panel_known && app.effect->full_resolution) {
		/* Exactly the panel: rounding the scaled size can give a row more (2401 for 2400) */
		app.buffer_width = panel_width;
		app.buffer_height = panel_height;
	} else {
		app.buffer_width = (int)lroundf(app.width * scale);
		app.buffer_height = (int)lroundf(app.height * scale);
	}
	if (app.viewport) {
		wl_surface_set_buffer_scale(app.surface, 1);
		wp_viewport_set_destination(app.viewport, app.width, app.height);
	} else {
		wl_surface_set_buffer_scale(app.surface, app.output_scale);
	}
	if (app.egl_window)
		wl_egl_window_resize(app.egl_window, app.buffer_width, app.buffer_height, 0, 0);
}

static void on_layer_configure(void *data, struct zwlr_layer_surface_v1 *layer_surface, uint32_t serial,
			       uint32_t width, uint32_t height)
{
	zwlr_layer_surface_v1_ack_configure(layer_surface, serial);
	if (!width || !height)
		return;
	gboolean resized = (int)width != app.width || (int)height != app.height;
	float sx = app.width ? (float)width / app.width : 1, sy = app.height ? (float)height / app.height : 1;
	app.width = width;
	app.height = height;
	app.scene.width = width;
	app.scene.height = height;
	update_buffer_size();
	g_debug("Configured %d×%d, drawn at %d×%d", app.width, app.height, app.buffer_width, app.buffer_height);
	if (!app.egl_window) {
		app.egl_window = wl_egl_window_create(app.surface, app.buffer_width, app.buffer_height);
		app.egl_surface = eglCreateWindowSurface(app.egl_display, app.egl_config,
							 (EGLNativeWindowType)app.egl_window, NULL);
		if (app.egl_surface == EGL_NO_SURFACE) {
			g_warning("Cannot create the EGL surface (0x%x)", eglGetError());
			return;
		}
		eglMakeCurrent(app.egl_display, app.egl_surface, app.egl_surface, app.egl_context);
		eglSwapInterval(app.egl_display, 0);
	}
	if (!app.state)
		create_state();
	else if (resized)
		app.effect->update(app.state, &app.scene, sx, sy);
	app.configured = TRUE;
	app.resting = FALSE;
	/* A frame now, even while waiting for a frame callback: the old buffer may be the wrong size,
	 * and after a rotation phoc keeps its shield up until every layer surface has committed a
	 * buffer for its configure (3 s; behind an app the callback would not come at all) */
	g_clear_pointer(&app.frame_callback, wl_callback_destroy);
	draw();
	update_activity();
}

static void on_layer_closed(void *data, struct zwlr_layer_surface_v1 *layer_surface)
{
	destroy_surface();
	update_activity();
}

static const struct zwlr_layer_surface_v1_listener layer_listener = {on_layer_configure, on_layer_closed};

static void create_surface(void)
{
	if (app.surface || !egl_init())
		return;
	app.surface = wl_compositor_create_surface(app.compositor);
	/* No input: taps go to what is below */
	struct wl_region *empty = wl_compositor_create_region(app.compositor);
	wl_surface_set_input_region(app.surface, empty);
	wl_region_destroy(empty);
	if (app.viewporter)
		app.viewport = wp_viewporter_get_viewport(app.viewporter, app.surface);
	/* The layer hardly matters: phoc-plinius draws the surface by its namespace, with Phosh's
	 * background. Bottom keeps it under the windows with any other compositor. */
	app.layer_surface = zwlr_layer_shell_v1_get_layer_surface(app.layer_shell, app.surface, NULL,
								  ZWLR_LAYER_SHELL_V1_LAYER_BOTTOM, "plinius-ambient");
	zwlr_layer_surface_v1_set_anchor(app.layer_surface, ZWLR_LAYER_SURFACE_V1_ANCHOR_TOP |
								     ZWLR_LAYER_SURFACE_V1_ANCHOR_BOTTOM |
								     ZWLR_LAYER_SURFACE_V1_ANCHOR_LEFT |
								     ZWLR_LAYER_SURFACE_V1_ANCHOR_RIGHT);
	/* The whole screen, also behind the panels */
	zwlr_layer_surface_v1_set_exclusive_zone(app.layer_surface, -1);
	zwlr_layer_surface_v1_set_size(app.layer_surface, 0, 0);
	zwlr_layer_surface_v1_add_listener(app.layer_surface, &layer_listener, NULL);
	wl_surface_commit(app.surface);
}

static void destroy_surface(void)
{
	if (!app.surface)
		return;
	g_clear_handle_id(&app.draw_timer, g_source_remove);
	g_clear_handle_id(&app.stall_timer, g_source_remove);
	g_clear_pointer(&app.frame_callback, wl_callback_destroy);
	if (app.egl_surface != EGL_NO_SURFACE) {
		eglMakeCurrent(app.egl_display, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);
		eglDestroySurface(app.egl_display, app.egl_surface);
		app.egl_surface = EGL_NO_SURFACE;
	}
	g_clear_pointer(&app.egl_window, wl_egl_window_destroy);
	g_clear_pointer(&app.viewport, wp_viewport_destroy);
	g_clear_pointer(&app.layer_surface, zwlr_layer_surface_v1_destroy);
	g_clear_pointer(&app.surface, wl_surface_destroy);
	app.configured = FALSE;
	app.width = app.height = 0;
	wl_display_flush(app.display);
}

/* ---- Settings ---- */

static void apply_settings(void)
{
	static const char *const densities[] = {"low", "medium", "high"};
	g_autofree char *name = g_settings_get_string(app.settings, "effect");
	g_autofree char *density = g_settings_get_string(app.settings, "density");
	g_autofree char *picture = g_settings_get_string(app.settings, "panorama");
	g_autofree char *video = g_settings_get_string(app.settings, "video");
	/* Both read every time: GSettings sends "changed" only for keys that have been read */
	gboolean panorama_enabled = g_settings_get_boolean(app.settings, "panorama-enabled");
	gboolean video_enabled = g_settings_get_boolean(app.settings, "video-enabled");
	if (!strcmp(name, "panorama")) {
		/* Up to 0.3 the photo was an effect of its own: now it is the background under any
		 * effect (the change comes back here with the new keys) */
		g_settings_set_boolean(app.settings, "panorama-enabled", TRUE);
		g_settings_set_string(app.settings, "effect", "none");
		return;
	}
	const struct effect *effect = effect_find(name);
	/* The video or the 360° photo under the effect (never both: see on_settings_changed) */
	gboolean use_video = *video && video_enabled;
	gboolean use_panorama = !use_video && *picture && panorama_enabled;
	if (use_video)
		effect = layered_with(&video_effect, effect);
	else if (use_panorama)
		effect = layered_with(&panorama_effect, effect);
	int level = 1;
	for (int i = 0; i < 3; i++)
		if (!strcmp(density, densities[i]))
			level = i;
	/* A new 360° photo or video needs the effect built anew */
	gboolean picture_changed = g_strcmp0(picture, app.picture) != 0;
	gboolean video_changed = g_strcmp0(video, app.video) != 0;
	g_free(app.picture);
	app.picture = g_steal_pointer(&picture);
	app.scene.picture = app.picture;
	g_free(app.video);
	app.video = g_steal_pointer(&video);
	app.scene.video = app.video;
	if (effect == app.effect && level == app.scene.density && !(picture_changed && use_panorama) &&
	    !(video_changed && use_video))
		return;
	g_message("Effect %s, density %s%s", effect ? effect->name : "none", densities[level],
		  use_video ? ", over the video" : use_panorama ? ", over the 360° photo" : "");
	/* An effect's GL objects go with the context */
	if (app.state && app.egl_surface != EGL_NO_SURFACE)
		eglMakeCurrent(app.egl_display, app.egl_surface, app.egl_surface, app.egl_context);
	if (app.state) {
		app.effect->destroy(app.state);
		app.state = NULL;
	}
	gboolean resolution_changed = app.effect && effect && app.effect->full_resolution != effect->full_resolution;
	app.effect = effect;
	app.scene.density = level;
	app.resting = FALSE;
	if (!effect) {
		destroy_surface();
	} else if (!app.surface) {
		create_surface();
	} else if (app.configured) {
		if (resolution_changed)
			update_buffer_size();
		create_state();
	}
	update_activity();
}

/* The video and the 360° photo exclude each other: the one switched on last wins (GNOME
 * Settings switches the other off itself, this is for everyone else) */
static void exclude_backgrounds(const char *key)
{
	if (!strcmp(key, "video-enabled") && g_settings_get_boolean(app.settings, "video-enabled") &&
	    g_settings_get_boolean(app.settings, "panorama-enabled"))
		g_settings_set_boolean(app.settings, "panorama-enabled", FALSE);
	else if (!strcmp(key, "panorama-enabled") && g_settings_get_boolean(app.settings, "panorama-enabled") &&
		 g_settings_get_boolean(app.settings, "video-enabled"))
		g_settings_set_boolean(app.settings, "video-enabled", FALSE);
}

static void on_settings_changed(GSettings *settings, const char *key, gpointer user_data)
{
	exclude_backgrounds(key);
	apply_settings();
}

/* A background picture chosen in GNOME Settings is meant to be seen: the 360° photo or the video,
 * which cover it completely, step aside (the effect over them stays). A picture is chosen when
 * picture-uri or picture-uri-dark changes, or picture-options becomes anything but 'none' (the
 * picture set already, chosen again after it was taken away). Not for picture-options 'none'
 * (no picture: the background color shows where nothing covers it) nor for primary-color: that
 * color is what shows when there is no background, a 360° photo or video stays over it. */
static void on_background_changed(GSettings *settings, const char *key, gpointer user_data)
{
	if (!strcmp(key, "picture-options")) {
		g_autofree char *options = g_settings_get_string(settings, "picture-options");
		if (!strcmp(options, "none"))
			return;
	} else if (strcmp(key, "picture-uri") && strcmp(key, "picture-uri-dark"))
		return;
	gboolean panorama = g_settings_get_boolean(app.settings, "panorama-enabled");
	gboolean video = g_settings_get_boolean(app.settings, "video-enabled");
	if (panorama || video) {
		/* The effect stays, over the new picture */
		g_message("A new background picture: the %s makes way for it", video ? "video" : "360° photo");
		if (panorama)
			g_settings_set_boolean(app.settings, "panorama-enabled", FALSE);
		if (video)
			g_settings_set_boolean(app.settings, "video-enabled", FALSE);
	}
}

/* ---- Output ---- */

static void on_output_geometry(void *data, struct wl_output *output, int32_t x, int32_t y, int32_t mm_width,
			       int32_t mm_height, int32_t subpixel, const char *make, const char *model,
			       int32_t transform)
{
	app.transform = transform;
	/* TRANSFORM_90: the device's left edge is up (see to_screen) */
	app.scene.screen_angle = (transform & 3) * 90;
	update_scene_gravity();
}

static void on_output_mode(void *data, struct wl_output *output, uint32_t flags, int32_t width, int32_t height,
			   int32_t refresh)
{
	if (flags & WL_OUTPUT_MODE_CURRENT) {
		app.mode_width = width;
		app.mode_height = height;
	}
}

static void on_output_done(void *data, struct wl_output *output)
{
	if (app.surface && app.configured) {
		update_buffer_size();
		app.resting = FALSE;
		schedule_draw();
	}
}

static void on_output_scale(void *data, struct wl_output *output, int32_t factor)
{
	app.output_scale = MAX(factor, 1);
}

static const struct wl_output_listener output_listener = {
	.geometry = on_output_geometry,
	.mode = on_output_mode,
	.done = on_output_done,
	.scale = on_output_scale,
};

/* ---- Wayland ---- */

static void on_global(void *data, struct wl_registry *registry, uint32_t name, const char *iface, uint32_t version)
{
	if (!strcmp(iface, wl_compositor_interface.name)) {
		app.compositor = wl_registry_bind(registry, name, &wl_compositor_interface, MIN(version, 4));
	} else if (!strcmp(iface, wl_output_interface.name) && !app.output) {
		/* The phone has one screen */
		app.output = wl_registry_bind(registry, name, &wl_output_interface, MIN(version, 2));
		wl_output_add_listener(app.output, &output_listener, NULL);
	} else if (!strcmp(iface, zwlr_layer_shell_v1_interface.name)) {
		app.layer_shell = wl_registry_bind(registry, name, &zwlr_layer_shell_v1_interface, MIN(version, 2));
	} else if (!strcmp(iface, wp_viewporter_interface.name)) {
		app.viewporter = wl_registry_bind(registry, name, &wp_viewporter_interface, 1);
	}
}

static void on_global_remove(void *data, struct wl_registry *registry, uint32_t name) {}

static const struct wl_registry_listener registry_listener = {on_global, on_global_remove};

/* Wayland events in the GLib main loop. The read intent is never held across iterations: EGL
 * reads its own queue inside eglSwapBuffers and would wait for us forever. */
struct wayland_source {
	GSource source;
	gpointer fd_tag;
};

static gboolean wayland_prepare(GSource *source, gint *timeout)
{
	*timeout = -1;
	/* Events already queued (read by EGL, for instance): dispatch them first */
	if (wl_display_prepare_read(app.display) != 0)
		return TRUE;
	wl_display_cancel_read(app.display);
	wl_display_flush(app.display);
	return FALSE;
}

static gboolean wayland_check(GSource *source)
{
	struct wayland_source *ws = (struct wayland_source *)source;
	GIOCondition revents = g_source_query_unix_fd(source, ws->fd_tag);
	if (revents & G_IO_IN && wl_display_prepare_read(app.display) == 0 &&
	    wl_display_read_events(app.display) < 0)
		return TRUE;
	return (revents & (G_IO_IN | G_IO_ERR | G_IO_HUP)) != 0;
}

static gboolean wayland_dispatch(GSource *source, GSourceFunc callback, gpointer user_data)
{
	struct wayland_source *ws = (struct wayland_source *)source;
	if (g_source_query_unix_fd(source, ws->fd_tag) & (G_IO_ERR | G_IO_HUP) ||
	    wl_display_dispatch_pending(app.display) < 0) {
		g_warning("Lost the compositor");
		g_main_loop_quit(app.loop);
		return G_SOURCE_REMOVE;
	}
	return G_SOURCE_CONTINUE;
}

static GSourceFuncs wayland_source_funcs = {wayland_prepare, wayland_check, wayland_dispatch, NULL, NULL, NULL};

static gboolean on_signal(gpointer user_data)
{
	g_main_loop_quit(app.loop);
	return G_SOURCE_REMOVE;
}

int main(void)
{
	app.display = wl_display_connect(NULL);
	if (!app.display) {
		g_printerr("No Wayland compositor\n");
		return 1;
	}
	app.egl_display = EGL_NO_DISPLAY;
	app.egl_surface = EGL_NO_SURFACE;
	struct wl_registry *registry = wl_display_get_registry(app.display);
	wl_registry_add_listener(registry, &registry_listener, NULL);
	wl_display_roundtrip(app.display);
	wl_display_roundtrip(app.display); /* the output's properties */
	if (!app.compositor || !app.layer_shell) {
		g_printerr("The compositor has no wlr-layer-shell\n");
		return 1;
	}

	app.loop = g_main_loop_new(NULL, FALSE);
	struct wayland_source *ws =
		(struct wayland_source *)g_source_new(&wayland_source_funcs, sizeof(struct wayland_source));
	ws->fd_tag = g_source_add_unix_fd(&ws->source, wl_display_get_fd(app.display), G_IO_IN | G_IO_ERR | G_IO_HUP);
	g_source_attach(&ws->source, NULL);

	app.system_bus = g_bus_get_sync(G_BUS_TYPE_SYSTEM, NULL, NULL);
	if (app.system_bus) {
		g_dbus_connection_signal_subscribe(app.system_bus, GRAVITY_BUS_NAME, GRAVITY_IFACE, "Gravity",
						   GRAVITY_PATH, NULL, G_DBUS_SIGNAL_FLAGS_NONE, on_gravity, NULL, NULL);
		g_dbus_connection_signal_subscribe(app.system_bus, GRAVITY_BUS_NAME, GRAVITY_IFACE, "Motion",
						   GRAVITY_PATH, NULL, G_DBUS_SIGNAL_FLAGS_NONE, on_motion, NULL, NULL);
		g_dbus_connection_signal_subscribe(app.system_bus, GRAVITY_BUS_NAME, GRAVITY_IFACE, "Orientation",
						   GRAVITY_PATH, NULL, G_DBUS_SIGNAL_FLAGS_NONE, on_orientation, NULL, NULL);
		g_dbus_connection_signal_subscribe(app.system_bus, GRAVITY_BUS_NAME, GRAVITY_IFACE, "GameOrientation",
						   GRAVITY_PATH, NULL, G_DBUS_SIGNAL_FLAGS_NONE, on_orientation, NULL, NULL);
		g_bus_watch_name_on_connection(app.system_bus, GRAVITY_BUS_NAME, G_BUS_NAME_WATCHER_FLAGS_NONE,
					       on_gravity_appeared, NULL, NULL, NULL);
		/* The display: the video pauses while it is off (or always-on) */
		g_dbus_connection_signal_subscribe(app.system_bus, MCE_BUS_NAME, MCE_SIGNAL_IFACE, "display_status_ind",
						   MCE_SIGNAL_PATH, NULL, G_DBUS_SIGNAL_FLAGS_NONE, on_display_status, NULL,
						   NULL);
		g_dbus_connection_call(app.system_bus, MCE_BUS_NAME, MCE_REQUEST_PATH, MCE_REQUEST_IFACE,
				       "get_display_status", NULL, G_VARIANT_TYPE("(s)"), G_DBUS_CALL_FLAGS_NO_AUTO_START,
				       -1, NULL, on_display_status_reply, NULL);
	}

	app.settings = g_settings_new(SETTINGS_SCHEMA);
	g_signal_connect(app.settings, "changed", G_CALLBACK(on_settings_changed), NULL);
	GSettingsSchemaSource *schemas = g_settings_schema_source_get_default();
	g_autoptr(GSettingsSchema) background_schema =
		schemas ? g_settings_schema_source_lookup(schemas, "org.gnome.desktop.background", TRUE) : NULL;
	if (background_schema) {
		GSettings *background = g_settings_new_full(background_schema, NULL, NULL);
		g_signal_connect(background, "changed", G_CALLBACK(on_background_changed), NULL);
		/* GSettings sends "changed" only for keys that have been read */
		g_free(g_settings_get_string(background, "picture-uri"));
		g_free(g_settings_get_string(background, "picture-uri-dark"));
		g_free(g_settings_get_string(background, "picture-options"));
	}
	/* PLINIUS_AMBIENT_ORIENTATION="x,y,z,w": a fixed orientation instead of the sensor, to try
	 * the 360° photo without one (with no system bus, nothing replaces it) */
	const char *fixed = g_getenv("PLINIUS_AMBIENT_ORIENTATION");
	if (fixed && sscanf(fixed, "%f,%f,%f,%f", &app.scene.orientation[0], &app.scene.orientation[1],
			    &app.scene.orientation[2], &app.scene.orientation[3]) == 4)
		app.scene.has_orientation = TRUE;
	app.scene.density = -1; /* so that the first apply_settings() takes it */
	app.scene.wake = request_frame;
	/* Both switched on (written by hand): the video, the newer of the two, wins */
	exclude_backgrounds("video-enabled");
	apply_settings();

	g_unix_signal_add(SIGTERM, on_signal, NULL);
	g_unix_signal_add(SIGINT, on_signal, NULL);
	g_main_loop_run(app.loop);

	if (app.gravity_claimed)
		g_dbus_connection_call_sync(app.system_bus, GRAVITY_BUS_NAME, GRAVITY_PATH, GRAVITY_IFACE,
					    "ReleaseGravity", NULL, NULL, G_DBUS_CALL_FLAGS_NONE, 1000, NULL, NULL);
	if (app.motion_claimed)
		g_dbus_connection_call_sync(app.system_bus, GRAVITY_BUS_NAME, GRAVITY_PATH, GRAVITY_IFACE,
					    "ReleaseMotion", NULL, NULL, G_DBUS_CALL_FLAGS_NONE, 1000, NULL, NULL);
	if (app.orientation_claimed) {
		/* The sensor claimed: the game rotation vector, or the rotation vector instead */
		g_autofree char *release = g_strconcat("Release", app.orientation_sensor, NULL);

		g_dbus_connection_call_sync(app.system_bus, GRAVITY_BUS_NAME, GRAVITY_PATH, GRAVITY_IFACE,
					    release, NULL, NULL, G_DBUS_CALL_FLAGS_NONE, 1000, NULL, NULL);
	}
	destroy_surface();
	if (app.egl_display != EGL_NO_DISPLAY)
		eglTerminate(app.egl_display);
	wl_display_disconnect(app.display);
	return 0;
}
