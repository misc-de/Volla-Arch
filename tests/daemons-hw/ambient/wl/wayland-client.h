// A stand-in for libwayland-client for the plinius-ambient client test: no compositor. Every
// request is a line "wl ..." on stdout (fakewl.c); the events come from the test (fakewl.h).
#pragma once
#include <stdint.h>

struct wl_display;
struct wl_registry;
struct wl_compositor;
struct wl_output;
struct wl_surface;
struct wl_region;
struct wl_callback;

struct wl_interface {
	const char *name;
};
extern const struct wl_interface wl_compositor_interface, wl_output_interface;

enum wl_output_transform {
	WL_OUTPUT_TRANSFORM_NORMAL = 0,
	WL_OUTPUT_TRANSFORM_90 = 1,
	WL_OUTPUT_TRANSFORM_180 = 2,
	WL_OUTPUT_TRANSFORM_270 = 3,
	WL_OUTPUT_TRANSFORM_FLIPPED = 4,
	WL_OUTPUT_TRANSFORM_FLIPPED_90 = 5,
	WL_OUTPUT_TRANSFORM_FLIPPED_180 = 6,
	WL_OUTPUT_TRANSFORM_FLIPPED_270 = 7,
};
enum wl_output_mode {
	WL_OUTPUT_MODE_CURRENT = 0x1,
	WL_OUTPUT_MODE_PREFERRED = 0x2,
};

struct wl_callback_listener {
	void (*done)(void *data, struct wl_callback *callback, uint32_t time);
};
struct wl_output_listener {
	void (*geometry)(void *data, struct wl_output *output, int32_t x, int32_t y, int32_t mm_width,
			 int32_t mm_height, int32_t subpixel, const char *make, const char *model, int32_t transform);
	void (*mode)(void *data, struct wl_output *output, uint32_t flags, int32_t width, int32_t height,
		     int32_t refresh);
	void (*done)(void *data, struct wl_output *output);
	void (*scale)(void *data, struct wl_output *output, int32_t factor);
	void (*name)(void *data, struct wl_output *output, const char *name);
	void (*description)(void *data, struct wl_output *output, const char *description);
};
struct wl_registry_listener {
	void (*global)(void *data, struct wl_registry *registry, uint32_t name, const char *interface, uint32_t version);
	void (*global_remove)(void *data, struct wl_registry *registry, uint32_t name);
};

struct wl_display *wl_display_connect(const char *name);
void wl_display_disconnect(struct wl_display *display);
struct wl_registry *wl_display_get_registry(struct wl_display *display);
int wl_display_roundtrip(struct wl_display *display);
int wl_display_get_fd(struct wl_display *display);
int wl_display_prepare_read(struct wl_display *display);
void wl_display_cancel_read(struct wl_display *display);
int wl_display_read_events(struct wl_display *display);
int wl_display_dispatch_pending(struct wl_display *display);
int wl_display_flush(struct wl_display *display);

int wl_registry_add_listener(struct wl_registry *registry, const struct wl_registry_listener *listener, void *data);
void *wl_registry_bind(struct wl_registry *registry, uint32_t name, const struct wl_interface *interface,
		       uint32_t version);
int wl_output_add_listener(struct wl_output *output, const struct wl_output_listener *listener, void *data);

struct wl_surface *wl_compositor_create_surface(struct wl_compositor *compositor);
struct wl_region *wl_compositor_create_region(struct wl_compositor *compositor);
void wl_region_destroy(struct wl_region *region);
void wl_surface_set_input_region(struct wl_surface *surface, struct wl_region *region);
void wl_surface_set_buffer_scale(struct wl_surface *surface, int32_t scale);
void wl_surface_commit(struct wl_surface *surface);
struct wl_callback *wl_surface_frame(struct wl_surface *surface);
void wl_surface_destroy(struct wl_surface *surface);
int wl_callback_add_listener(struct wl_callback *callback, const struct wl_callback_listener *listener, void *data);
void wl_callback_destroy(struct wl_callback *callback);
