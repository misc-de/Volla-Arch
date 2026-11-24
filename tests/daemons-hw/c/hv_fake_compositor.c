/*
 * A compositor for the hybris-vulkan WSI tests (tests/daemons-hw/test_dhw_hybris_vulkan_wsi.py):
 * wl_compositor with wl_surface, and zwp_linux_dmabuf_v1 that turns params into wl_buffers.
 * It serves one client on the socket fd given as argv[1] and logs what it gets on stdout,
 * prefixed "server:". Behaviour from the further arguments:
 *   no-dmabuf      no linux-dmabuf global
 *   fail-params    answer every params create with failed
 *   no-release     never release a buffer
 *   no-frames      never answer frame callbacks
 *   die-after:N    exit after the N-th commit (the client loses the connection)
 *   die-at-create  exit on the first params create
 *   compositor:V   advertise wl_compositor version V (default 4)
 * By default a commit releases the buffer it replaces and answers the frame callbacks at once.
 */
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <wayland-server.h>

#include "linux-dmabuf-v1-server-protocol.h"

static bool no_dmabuf, fail_params, no_release, no_frames, die_at_create;
static int die_after, commits;
static uint32_t compositor_version = 4;

struct surface {
	struct wl_resource *resource, *pending, *current;
	bool attached;
	struct wl_list frames;
};

/* The surfaces' buffers, to forget a buffer the client destroys */
static struct surface *surfaces[8];
static int n_surfaces;

static void buffer_destroyed(struct wl_resource *buffer)
{
	for (int i = 0; i < n_surfaces; i++) {
		if (!surfaces[i])
			continue;
		if (surfaces[i]->current == buffer)
			surfaces[i]->current = NULL;
		if (surfaces[i]->pending == buffer)
			surfaces[i]->pending = NULL;
	}
}

static void destroy_request(struct wl_client *client, struct wl_resource *resource)
{
	wl_resource_destroy(resource);
}

static const struct wl_buffer_interface buffer_impl = {.destroy = destroy_request};

static void surface_attach(struct wl_client *c, struct wl_resource *r, struct wl_resource *buffer, int32_t x, int32_t y)
{
	struct surface *s = wl_resource_get_user_data(r);
	s->pending = buffer;
	s->attached = true;
}

static void surface_damage(struct wl_client *c, struct wl_resource *r, int32_t x, int32_t y, int32_t w, int32_t h)
{
	printf("server: damage %d %d %d %d\n", x, y, w, h);
}

static void surface_frame(struct wl_client *c, struct wl_resource *r, uint32_t id)
{
	struct surface *s = wl_resource_get_user_data(r);
	struct wl_resource *cb = wl_resource_create(c, &wl_callback_interface, 1, id);
	wl_resource_set_implementation(cb, NULL, NULL, NULL);
	wl_list_insert(s->frames.prev, wl_resource_get_link(cb));
	printf("server: frame\n");
}

static void surface_region(struct wl_client *c, struct wl_resource *r, struct wl_resource *region)
{
}

static void surface_commit(struct wl_client *c, struct wl_resource *r)
{
	struct surface *s = wl_resource_get_user_data(r);
	if (s->attached) {
		if (s->current && s->current != s->pending && !no_release)
			wl_buffer_send_release(s->current);
		s->current = s->pending;
		s->attached = false;
	}
	printf("server: commit %d buffer=%s\n", ++commits, s->current ? "yes" : "none");
	if (!no_frames) {
		struct wl_resource *cb, *tmp;
		wl_resource_for_each_safe(cb, tmp, &s->frames) {
			wl_callback_send_done(cb, 0);
			/* wl_resource_destroy leaves the link in our list */
			wl_list_remove(wl_resource_get_link(cb));
			wl_resource_destroy(cb);
		}
	}
	fflush(stdout);
	if (die_after && commits >= die_after)
		exit(0);
}

static void surface_int(struct wl_client *c, struct wl_resource *r, int32_t v)
{
}

static void surface_damage_buffer(struct wl_client *c, struct wl_resource *r, int32_t x, int32_t y, int32_t w, int32_t h)
{
	printf("server: damage_buffer %d %d %d %d\n", x, y, w, h);
}

static const struct wl_surface_interface surface_impl = {
	.destroy = destroy_request,
	.attach = surface_attach,
	.damage = surface_damage,
	.frame = surface_frame,
	.set_opaque_region = surface_region,
	.set_input_region = surface_region,
	.commit = surface_commit,
	.set_buffer_transform = surface_int,
	.set_buffer_scale = surface_int,
	.damage_buffer = surface_damage_buffer,
};

static void surface_gone(struct wl_resource *r)
{
	struct surface *s = wl_resource_get_user_data(r);
	struct wl_resource *cb, *tmp;
	wl_resource_for_each_safe(cb, tmp, &s->frames) {
		wl_list_remove(wl_resource_get_link(cb));
		wl_resource_destroy(cb);
	}
	for (int i = 0; i < n_surfaces; i++)
		if (surfaces[i] == s)
			surfaces[i] = NULL;
	free(s);
}

static void create_surface(struct wl_client *c, struct wl_resource *r, uint32_t id)
{
	struct surface *s = calloc(1, sizeof *s);
	wl_list_init(&s->frames);
	s->resource = wl_resource_create(c, &wl_surface_interface, wl_resource_get_version(r), id);
	wl_resource_set_implementation(s->resource, &surface_impl, s, surface_gone);
	if (n_surfaces < 8)
		surfaces[n_surfaces++] = s;
}

static void create_region(struct wl_client *c, struct wl_resource *r, uint32_t id)
{
}

static const struct wl_compositor_interface compositor_impl = {
	.create_surface = create_surface,
	.create_region = create_region,
};

static void bind_compositor(struct wl_client *c, void *data, uint32_t version, uint32_t id)
{
	struct wl_resource *r = wl_resource_create(c, &wl_compositor_interface, version, id);
	wl_resource_set_implementation(r, &compositor_impl, NULL, NULL);
}

/* linux-dmabuf */

static void params_add(struct wl_client *c, struct wl_resource *r, int32_t fd, uint32_t plane, uint32_t offset,
		       uint32_t stride, uint32_t mod_hi, uint32_t mod_lo)
{
	printf("server: add plane=%u offset=%u stride=%u modifier=%u:%u\n", plane, offset, stride, mod_hi, mod_lo);
	close(fd);
}

static void params_create(struct wl_client *c, struct wl_resource *r, int32_t w, int32_t h, uint32_t format,
			  uint32_t flags)
{
	printf("server: create %dx%d format=%.4s flags=%u\n", w, h, (const char *)&format, flags);
	fflush(stdout);
	if (die_at_create)
		exit(0);
	if (fail_params) {
		zwp_linux_buffer_params_v1_send_failed(r);
		return;
	}
	struct wl_resource *buffer = wl_resource_create(c, &wl_buffer_interface, 1, 0);
	wl_resource_set_implementation(buffer, &buffer_impl, NULL, buffer_destroyed);
	zwp_linux_buffer_params_v1_send_created(r, buffer);
}

static void params_create_immed(struct wl_client *c, struct wl_resource *r, uint32_t id, int32_t w, int32_t h,
				uint32_t format, uint32_t flags)
{
	printf("server: create_immed (unexpected)\n");
}

static const struct zwp_linux_buffer_params_v1_interface params_impl = {
	.destroy = destroy_request,
	.add = params_add,
	.create = params_create,
	.create_immed = params_create_immed,
};

static void dmabuf_create_params(struct wl_client *c, struct wl_resource *r, uint32_t id)
{
	struct wl_resource *p = wl_resource_create(c, &zwp_linux_buffer_params_v1_interface, wl_resource_get_version(r), id);
	wl_resource_set_implementation(p, &params_impl, NULL, NULL);
}

static void dmabuf_unused(struct wl_client *c, struct wl_resource *r, uint32_t id)
{
}

static void dmabuf_unused_surface(struct wl_client *c, struct wl_resource *r, uint32_t id, struct wl_resource *s)
{
}

static const struct zwp_linux_dmabuf_v1_interface dmabuf_impl = {
	.destroy = destroy_request,
	.create_params = dmabuf_create_params,
	.get_default_feedback = dmabuf_unused,
	.get_surface_feedback = dmabuf_unused_surface,
};

static void bind_dmabuf(struct wl_client *c, void *data, uint32_t version, uint32_t id)
{
	printf("server: bind linux-dmabuf version %u\n", version);
	struct wl_resource *r = wl_resource_create(c, &zwp_linux_dmabuf_v1_interface, version, id);
	wl_resource_set_implementation(r, &dmabuf_impl, NULL, NULL);
}

static void client_gone(struct wl_listener *l, void *data)
{
	fflush(stdout);
	exit(0);
}

int main(int argc, char **argv)
{
	setvbuf(stdout, NULL, _IOLBF, 0);
	for (int i = 2; i < argc; i++) {
		const char *a = argv[i];
		no_dmabuf |= !strcmp(a, "no-dmabuf");
		fail_params |= !strcmp(a, "fail-params");
		no_release |= !strcmp(a, "no-release");
		no_frames |= !strcmp(a, "no-frames");
		die_at_create |= !strcmp(a, "die-at-create");
		if (!strncmp(a, "die-after:", 10))
			die_after = atoi(a + 10);
		if (!strncmp(a, "compositor:", 11))
			compositor_version = atoi(a + 11);
	}
	struct wl_display *display = wl_display_create();
	wl_global_create(display, &wl_compositor_interface, compositor_version, NULL, bind_compositor);
	if (!no_dmabuf)
		wl_global_create(display, &zwp_linux_dmabuf_v1_interface, 4, NULL, bind_dmabuf);
	struct wl_client *client = wl_client_create(display, atoi(argv[1]));
	static struct wl_listener gone = {.notify = client_gone};
	wl_client_add_destroy_listener(client, &gone);
	wl_display_run(display);
	return 0;
}
