// Stand-in for wayland-scanner's viewporter header (see wayland-client.h here)
#pragma once
#include <wayland-client.h>
struct wp_viewporter;
struct wp_viewport;
extern const struct wl_interface wp_viewporter_interface;
struct wp_viewport *wp_viewporter_get_viewport(struct wp_viewporter *viewporter, struct wl_surface *surface);
void wp_viewport_set_destination(struct wp_viewport *viewport, int32_t width, int32_t height);
void wp_viewport_destroy(struct wp_viewport *viewport);
