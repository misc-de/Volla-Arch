// Stand-in for wayland-scanner's wlr-layer-shell header (see wayland-client.h here)
#pragma once
#include <wayland-client.h>
struct zwlr_layer_shell_v1;
struct zwlr_layer_surface_v1;
extern const struct wl_interface zwlr_layer_shell_v1_interface;
enum zwlr_layer_shell_v1_layer {
	ZWLR_LAYER_SHELL_V1_LAYER_BACKGROUND = 0,
	ZWLR_LAYER_SHELL_V1_LAYER_BOTTOM = 1,
	ZWLR_LAYER_SHELL_V1_LAYER_TOP = 2,
	ZWLR_LAYER_SHELL_V1_LAYER_OVERLAY = 3,
};
enum zwlr_layer_surface_v1_anchor {
	ZWLR_LAYER_SURFACE_V1_ANCHOR_TOP = 1,
	ZWLR_LAYER_SURFACE_V1_ANCHOR_BOTTOM = 2,
	ZWLR_LAYER_SURFACE_V1_ANCHOR_LEFT = 4,
	ZWLR_LAYER_SURFACE_V1_ANCHOR_RIGHT = 8,
};
struct zwlr_layer_surface_v1_listener {
	void (*configure)(void *data, struct zwlr_layer_surface_v1 *surface, uint32_t serial, uint32_t width,
			  uint32_t height);
	void (*closed)(void *data, struct zwlr_layer_surface_v1 *surface);
};
struct zwlr_layer_surface_v1 *zwlr_layer_shell_v1_get_layer_surface(struct zwlr_layer_shell_v1 *shell,
								    struct wl_surface *surface, struct wl_output *output,
								    uint32_t layer, const char *namespace_);
void zwlr_layer_surface_v1_set_anchor(struct zwlr_layer_surface_v1 *surface, uint32_t anchor);
void zwlr_layer_surface_v1_set_exclusive_zone(struct zwlr_layer_surface_v1 *surface, int32_t zone);
void zwlr_layer_surface_v1_set_size(struct zwlr_layer_surface_v1 *surface, uint32_t width, uint32_t height);
int zwlr_layer_surface_v1_add_listener(struct zwlr_layer_surface_v1 *surface,
				       const struct zwlr_layer_surface_v1_listener *listener, void *data);
void zwlr_layer_surface_v1_ack_configure(struct zwlr_layer_surface_v1 *surface, uint32_t serial);
void zwlr_layer_surface_v1_destroy(struct zwlr_layer_surface_v1 *surface);
