// Stand-in for wayland-egl (see wayland-client.h here)
#pragma once
struct wl_surface;
struct wl_egl_window;
struct wl_egl_window *wl_egl_window_create(struct wl_surface *surface, int width, int height);
void wl_egl_window_resize(struct wl_egl_window *window, int width, int height, int dx, int dy);
void wl_egl_window_destroy(struct wl_egl_window *window);
