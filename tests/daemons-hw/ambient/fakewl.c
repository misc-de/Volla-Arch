// The stand-ins for libwayland-client, wayland-egl and EGL in the plinius-ambient client test:
// requests are lines "wl ..." and "egl ..." on stdout, the compositor's events come from the
// test through fakewl.h. Objects are allocated and freed, so ASan sees a use after destroy.
//
// FAKEWL_NO_DISPLAY=1: no compositor. FAKEWL_GLOBALS: the globals announced on the first
// roundtrip (default "wl_compositor,wl_output,zwlr_layer_shell_v1,wp_viewporter").
// FAKEWL_OUTPUT="transform,width,height,scale": the output's events on the second roundtrip
// (default "0,1080,2400,3"). FAKEEGL_FAIL: comma list of display, init, config, context, surface,
// current, swap: those EGL calls fail.
#define _GNU_SOURCE
#include "fakewl.h"

#include <EGL/egl.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "viewporter-client-protocol.h"
#include "wayland-client.h"
#include "wayland-egl.h"
#include "wlr-layer-shell-unstable-v1-client-protocol.h"

const struct wl_interface wl_compositor_interface = {"wl_compositor"};
const struct wl_interface wl_output_interface = {"wl_output"};
const struct wl_interface wp_viewporter_interface = {"wp_viewporter"};
const struct wl_interface zwlr_layer_shell_v1_interface = {"zwlr_layer_shell_v1"};

enum kind { DISPLAY, REGISTRY, GLOBAL, SURFACE, REGION, CALLBACK, VIEWPORT, LAYER, EGL_WINDOW, N_KINDS };
static const char *const kind_names[N_KINDS] = {"display", "registry", "global", "surface", "region",
						"callback", "viewport", "layer", "egl_window"};

struct obj {
	enum kind kind;
	int id;
	const void *listener;
	void *data;
	const char *iface;
};

static int next_id = 1, live[N_KINDS], roundtrips;
static int pipe_fds[2] = {-1, -1};
static struct obj *registry, *output, *layer;
static struct obj *callbacks[64];
static int n_callbacks;
static uint32_t serial = 100;
int fakewl_queued, fakewl_read_error, fakewl_dispatch_error;

static struct obj *make(enum kind kind)
{
	struct obj *o = calloc(1, sizeof(*o));
	o->kind = kind;
	o->id = next_id++;
	live[kind]++;
	return o;
}

static void destroy(void *p, enum kind kind, const char *what)
{
	struct obj *o = p;
	if (!o)
		return;
	if (o->kind != kind) {
		printf("wl error: %s on a %s\n", what, kind_names[o->kind]);
		return;
	}
	printf("wl %s %d\n", what, o->id);
	live[kind]--;
	free(o);
}

static int failing(const char *what)
{
	const char *f = getenv("FAKEEGL_FAIL");
	if (!f)
		return 0;
	char list[256];
	snprintf(list, sizeof(list), ",%s,", f);
	char key[64];
	snprintf(key, sizeof(key), ",%s,", what);
	return strstr(list, key) != NULL;
}

/* ---- Display ---- */

struct wl_display *wl_display_connect(const char *name)
{
	if (getenv("FAKEWL_NO_DISPLAY"))
		return NULL;
	if (pipe2(pipe_fds, O_NONBLOCK | O_CLOEXEC) < 0)
		abort();
	printf("wl connect\n");
	return (struct wl_display *)make(DISPLAY);
}

void wl_display_disconnect(struct wl_display *display)
{
	destroy(display, DISPLAY, "disconnect");
}

struct wl_registry *wl_display_get_registry(struct wl_display *display)
{
	registry = make(REGISTRY);
	return (struct wl_registry *)registry;
}

int wl_registry_add_listener(struct wl_registry *r, const struct wl_registry_listener *listener, void *data)
{
	((struct obj *)r)->listener = listener;
	((struct obj *)r)->data = data;
	return 0;
}

void fakewl_output_events(int transform, int width, int height, int scale)
{
	if (!output || !output->listener)
		return;
	const struct wl_output_listener *l = output->listener;
	l->geometry(output->data, (struct wl_output *)output, 0, 0, 68, 151, 0, "fake", "panel", transform);
	l->mode(output->data, (struct wl_output *)output, WL_OUTPUT_MODE_PREFERRED, 640, 480, 60000);
	l->mode(output->data, (struct wl_output *)output, WL_OUTPUT_MODE_CURRENT | WL_OUTPUT_MODE_PREFERRED, width,
		height, 60000);
	l->scale(output->data, (struct wl_output *)output, scale);
	l->done(output->data, (struct wl_output *)output);
}

int wl_display_roundtrip(struct wl_display *display)
{
	roundtrips++;
	if (roundtrips == 1 && registry && registry->listener) {
		const struct wl_registry_listener *l = registry->listener;
		const char *g = getenv("FAKEWL_GLOBALS");
		char list[512];
		snprintf(list, sizeof(list), "%s", g ? g : "wl_compositor,wl_output,zwlr_layer_shell_v1,wp_viewporter");
		uint32_t name = 1;
		for (char *s = strtok(list, ","); s; s = strtok(NULL, ","))
			l->global(registry->data, (struct wl_registry *)registry, name++, s, 4);
		l->global_remove(registry->data, (struct wl_registry *)registry, 99);
	} else if (roundtrips == 2) {
		int t = 0, w = 1080, h = 2400, s = 3;
		const char *o = getenv("FAKEWL_OUTPUT");
		if (o)
			sscanf(o, "%d,%d,%d,%d", &t, &w, &h, &s);
		fakewl_output_events(t, w, h, s);
	}
	return 0;
}

int wl_display_get_fd(struct wl_display *display)
{
	return pipe_fds[0];
}

void fakewl_readable(void)
{
	if (write(pipe_fds[1], "x", 1) != 1)
		abort();
}

void fakewl_hangup(void)
{
	close(pipe_fds[1]);
	pipe_fds[1] = -1;
}

int wl_display_prepare_read(struct wl_display *display)
{
	if (fakewl_queued) {
		fakewl_queued = 0;
		printf("wl events queued\n");
		return -1;
	}
	return 0;
}

void wl_display_cancel_read(struct wl_display *display)
{
}

int wl_display_read_events(struct wl_display *display)
{
	char c;
	while (read(pipe_fds[0], &c, 1) == 1)
		;
	printf("wl read events\n");
	/* As in libwayland: the error sticks to the display, dispatching fails from then on */
	if (fakewl_read_error)
		fakewl_dispatch_error = 1;
	return fakewl_read_error ? -1 : 0;
}

int wl_display_dispatch_pending(struct wl_display *display)
{
	printf("wl dispatch\n");
	return fakewl_dispatch_error ? -1 : 0;
}

int wl_display_flush(struct wl_display *display)
{
	return 0;
}

void *wl_registry_bind(struct wl_registry *r, uint32_t name, const struct wl_interface *interface, uint32_t version)
{
	struct obj *o = make(GLOBAL);
	o->iface = interface->name;
	printf("wl bind %s %u\n", interface->name, version);
	if (interface == &wl_output_interface)
		output = o;
	return o;
}

int wl_output_add_listener(struct wl_output *o, const struct wl_output_listener *listener, void *data)
{
	((struct obj *)o)->listener = listener;
	((struct obj *)o)->data = data;
	return 0;
}

/* ---- Surfaces ---- */

struct wl_surface *wl_compositor_create_surface(struct wl_compositor *compositor)
{
	struct obj *o = make(SURFACE);
	printf("wl create surface %d\n", o->id);
	return (struct wl_surface *)o;
}

struct wl_region *wl_compositor_create_region(struct wl_compositor *compositor)
{
	return (struct wl_region *)make(REGION);
}

void wl_region_destroy(struct wl_region *region)
{
	destroy(region, REGION, "destroy region");
}

void wl_surface_set_input_region(struct wl_surface *surface, struct wl_region *region)
{
	printf("wl input region empty\n");
}

void wl_surface_set_buffer_scale(struct wl_surface *surface, int32_t scale)
{
	printf("wl buffer scale %d\n", scale);
}

void wl_surface_commit(struct wl_surface *surface)
{
	printf("wl commit\n");
}

struct wl_callback *wl_surface_frame(struct wl_surface *surface)
{
	struct obj *o = make(CALLBACK);
	if (n_callbacks < 64)
		callbacks[n_callbacks++] = o;
	printf("wl frame %d\n", o->id);
	return (struct wl_callback *)o;
}

void wl_surface_destroy(struct wl_surface *surface)
{
	destroy(surface, SURFACE, "destroy surface");
}

int wl_callback_add_listener(struct wl_callback *c, const struct wl_callback_listener *listener, void *data)
{
	((struct obj *)c)->listener = listener;
	((struct obj *)c)->data = data;
	return 0;
}

void wl_callback_destroy(struct wl_callback *c)
{
	for (int i = 0; i < n_callbacks; i++)
		if (callbacks[i] == (struct obj *)c) {
			memmove(&callbacks[i], &callbacks[i + 1], (n_callbacks - i - 1) * sizeof(callbacks[0]));
			n_callbacks--;
			break;
		}
	destroy(c, CALLBACK, "destroy callback");
}

/* The compositor shows the frame: the oldest pending callback is done */
int fakewl_frame_done(void)
{
	if (!n_callbacks)
		return 0;
	struct obj *o = callbacks[0];
	const struct wl_callback_listener *l = o->listener;
	l->done(o->data, (struct wl_callback *)o, 1234);
	return 1;
}

int fakewl_pending_frames(void)
{
	return n_callbacks;
}

struct wp_viewport *wp_viewporter_get_viewport(struct wp_viewporter *viewporter, struct wl_surface *surface)
{
	return (struct wp_viewport *)make(VIEWPORT);
}

void wp_viewport_set_destination(struct wp_viewport *viewport, int32_t width, int32_t height)
{
	printf("wl viewport destination %dx%d\n", width, height);
}

void wp_viewport_destroy(struct wp_viewport *viewport)
{
	destroy(viewport, VIEWPORT, "destroy viewport");
}

struct zwlr_layer_surface_v1 *zwlr_layer_shell_v1_get_layer_surface(struct zwlr_layer_shell_v1 *shell,
								    struct wl_surface *surface, struct wl_output *output,
								    uint32_t lay, const char *namespace_)
{
	layer = make(LAYER);
	printf("wl layer surface %d layer %u namespace %s output %s\n", layer->id, lay, namespace_,
	       output ? "set" : "any");
	return (struct zwlr_layer_surface_v1 *)layer;
}

void zwlr_layer_surface_v1_set_anchor(struct zwlr_layer_surface_v1 *surface, uint32_t anchor)
{
	printf("wl anchor %u\n", anchor);
}

void zwlr_layer_surface_v1_set_exclusive_zone(struct zwlr_layer_surface_v1 *surface, int32_t zone)
{
	printf("wl exclusive zone %d\n", zone);
}

void zwlr_layer_surface_v1_set_size(struct zwlr_layer_surface_v1 *surface, uint32_t width, uint32_t height)
{
	printf("wl size %ux%u\n", width, height);
}

int zwlr_layer_surface_v1_add_listener(struct zwlr_layer_surface_v1 *surface,
				       const struct zwlr_layer_surface_v1_listener *listener, void *data)
{
	((struct obj *)surface)->listener = listener;
	((struct obj *)surface)->data = data;
	return 0;
}

void zwlr_layer_surface_v1_ack_configure(struct zwlr_layer_surface_v1 *surface, uint32_t s)
{
	printf("wl ack configure %u\n", s);
}

void zwlr_layer_surface_v1_destroy(struct zwlr_layer_surface_v1 *surface)
{
	if ((struct obj *)surface == layer)
		layer = NULL;
	destroy(surface, LAYER, "destroy layer surface");
}

int fakewl_configure(int width, int height)
{
	if (!layer)
		return 0;
	const struct zwlr_layer_surface_v1_listener *l = layer->listener;
	l->configure(layer->data, (struct zwlr_layer_surface_v1 *)layer, ++serial, width, height);
	return 1;
}

int fakewl_closed(void)
{
	if (!layer)
		return 0;
	const struct zwlr_layer_surface_v1_listener *l = layer->listener;
	l->closed(layer->data, (struct zwlr_layer_surface_v1 *)layer);
	return 1;
}

struct wl_egl_window *wl_egl_window_create(struct wl_surface *surface, int width, int height)
{
	struct obj *o = make(EGL_WINDOW);
	printf("wl egl window %dx%d\n", width, height);
	return (struct wl_egl_window *)o;
}

void wl_egl_window_resize(struct wl_egl_window *window, int width, int height, int dx, int dy)
{
	printf("wl egl resize %dx%d\n", width, height);
}

void wl_egl_window_destroy(struct wl_egl_window *window)
{
	destroy(window, EGL_WINDOW, "destroy egl window");
}

void fakewl_report(void)
{
	printf("wl live");
	for (int k = SURFACE; k < N_KINDS; k++)
		printf(" %s %d", kind_names[k], live[k]);
	printf("\n");
}

/* ---- EGL ---- */

static int egl_error = EGL_SUCCESS, swaps;

EGLint eglGetError(void)
{
	return egl_error;
}

EGLDisplay eglGetDisplay(EGLNativeDisplayType display_id)
{
	printf("egl get display\n");
	if (failing("display")) {
		egl_error = EGL_BAD_DISPLAY;
		return EGL_NO_DISPLAY;
	}
	return (EGLDisplay)0x1;
}

EGLBoolean eglInitialize(EGLDisplay dpy, EGLint *major, EGLint *minor)
{
	if (failing("init")) {
		egl_error = EGL_NOT_INITIALIZED;
		return EGL_FALSE;
	}
	return EGL_TRUE;
}

EGLBoolean eglBindAPI(EGLenum api)
{
	printf("egl bind api 0x%x\n", api);
	return EGL_TRUE;
}

EGLBoolean eglChooseConfig(EGLDisplay dpy, const EGLint *attrib_list, EGLConfig *configs, EGLint config_size,
			   EGLint *num_config)
{
	int alpha = 0;
	for (const EGLint *a = attrib_list; *a != EGL_NONE; a += 2)
		if (a[0] == EGL_ALPHA_SIZE)
			alpha = a[1];
	printf("egl choose config alpha %d\n", alpha);
	*num_config = failing("config") ? 0 : 1;
	configs[0] = (EGLConfig)0x2;
	return EGL_TRUE;
}

EGLContext eglCreateContext(EGLDisplay dpy, EGLConfig config, EGLContext share_context, const EGLint *attrib_list)
{
	int version = 0;
	for (const EGLint *a = attrib_list; *a != EGL_NONE; a += 2)
		if (a[0] == EGL_CONTEXT_CLIENT_VERSION)
			version = a[1];
	printf("egl create context version %d\n", version);
	if (failing("context")) {
		egl_error = EGL_BAD_CONFIG;
		return EGL_NO_CONTEXT;
	}
	return (EGLContext)0x3;
}

EGLSurface eglCreateWindowSurface(EGLDisplay dpy, EGLConfig config, EGLNativeWindowType win,
				  const EGLint *attrib_list)
{
	printf("egl create surface\n");
	if (failing("surface")) {
		egl_error = EGL_BAD_NATIVE_WINDOW;
		return EGL_NO_SURFACE;
	}
	return (EGLSurface)0x4;
}

EGLBoolean eglDestroySurface(EGLDisplay dpy, EGLSurface surface)
{
	printf("egl destroy surface\n");
	return EGL_TRUE;
}

EGLBoolean eglMakeCurrent(EGLDisplay dpy, EGLSurface draw, EGLSurface read, EGLContext ctx)
{
	printf("egl make current %s\n", draw == EGL_NO_SURFACE ? "none" : "surface");
	if (draw != EGL_NO_SURFACE && failing("current")) {
		egl_error = EGL_CONTEXT_LOST;
		return EGL_FALSE;
	}
	return EGL_TRUE;
}

EGLBoolean eglSwapInterval(EGLDisplay dpy, EGLint interval)
{
	printf("egl swap interval %d\n", interval);
	return EGL_TRUE;
}

EGLBoolean eglSwapBuffers(EGLDisplay dpy, EGLSurface surface)
{
	printf("egl swap %d\n", ++swaps);
	if (failing("swap")) {
		egl_error = EGL_BAD_SURFACE;
		return EGL_FALSE;
	}
	return EGL_TRUE;
}

EGLBoolean eglTerminate(EGLDisplay dpy)
{
	printf("egl terminate\n");
	return EGL_TRUE;
}
