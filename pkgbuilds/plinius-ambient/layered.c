// SPDX-License-Identifier: MIT
// plinius-ambient: a background (the 360° photo, a video) with one of the effects (snow, rain,
// balls...) over it, drawn into the same frame after the background. The pair behaves as one
// effect: it moves while either part moves, follows the sensors either part follows, and is
// drawn at the faster of the two frame rates and at full resolution.
#include "layered.h"
#include "render.h"

#include <glib.h>

struct layered {
	const struct effect *background, *over;
	void *below, *effect;
	struct renderer renderer; /* the effect's own GL objects */
};

#define MAX_LAYERED 24
static struct effect layered_effects[MAX_LAYERED];
static const struct effect *layered_background[MAX_LAYERED];
static const struct effect *layered_over[MAX_LAYERED];

static void layered_update(void *state, const struct scene *scene, float sx, float sy)
{
	struct layered *l = state;
	l->background->update(l->below, scene, sx, sy);
	l->over->update(l->effect, scene, sx, sy);
}

static bool layered_step(void *state, const struct scene *scene, float dt)
{
	struct layered *l = state;
	bool below = l->background->step(l->below, scene, dt);
	bool effect = l->over->step(l->effect, scene, dt);
	return below || effect;
}

static bool layered_wake(void *state, const struct scene *scene)
{
	struct layered *l = state;
	if (l->background->wake && l->background->wake(l->below, scene))
		return true;
	return l->over->wake && l->over->wake(l->effect, scene);
}

static void layered_build(void *state, const struct scene *scene, struct vertex_buffer *out) {}

static void layered_render(void *state, const struct scene *scene, int width, int height)
{
	struct layered *l = state;
	l->background->render(l->below, scene, width, height);
	render_frame_over(&l->renderer, l->over, l->effect, scene, width, height);
}

static void layered_set_visible(void *state, bool visible)
{
	struct layered *l = state;
	if (l->background->set_visible)
		l->background->set_visible(l->below, visible);
	if (l->over->set_visible)
		l->over->set_visible(l->effect, visible);
}

static void layered_destroy(void *state)
{
	struct layered *l = state;
	/* The GL context is current whenever an effect is replaced (see plinius-ambient.c) */
	l->background->destroy(l->below);
	l->over->destroy(l->effect);
	render_release(&l->renderer);
	g_free(l);
}

/* One create per combination, so that the state knows which pair it is */
#define LAYERED_CREATE(i)                                                    \
	static void *layered_create_##i(const struct scene *scene)           \
	{                                                                    \
		struct layered *l = g_new0(struct layered, 1);               \
		l->background = layered_background[i];                       \
		l->over = layered_over[i];                                   \
		l->below = l->background->create(scene);                     \
		l->effect = l->over->create(scene);                          \
		return l;                                                    \
	}
LAYERED_CREATE(0) LAYERED_CREATE(1) LAYERED_CREATE(2) LAYERED_CREATE(3) LAYERED_CREATE(4)
LAYERED_CREATE(5) LAYERED_CREATE(6) LAYERED_CREATE(7) LAYERED_CREATE(8) LAYERED_CREATE(9)
LAYERED_CREATE(10) LAYERED_CREATE(11) LAYERED_CREATE(12) LAYERED_CREATE(13) LAYERED_CREATE(14)
LAYERED_CREATE(15) LAYERED_CREATE(16) LAYERED_CREATE(17) LAYERED_CREATE(18) LAYERED_CREATE(19)
LAYERED_CREATE(20) LAYERED_CREATE(21) LAYERED_CREATE(22) LAYERED_CREATE(23)
static void *(*const layered_creates[MAX_LAYERED])(const struct scene *) = {
	layered_create_0, layered_create_1, layered_create_2, layered_create_3, layered_create_4,
	layered_create_5, layered_create_6, layered_create_7, layered_create_8, layered_create_9,
	layered_create_10, layered_create_11, layered_create_12, layered_create_13, layered_create_14,
	layered_create_15, layered_create_16, layered_create_17, layered_create_18, layered_create_19,
	layered_create_20, layered_create_21, layered_create_22, layered_create_23,
};

const struct effect *layered_with(const struct effect *background, const struct effect *over)
{
	if (!over || over == background)
		return background;
	for (int i = 0; i < MAX_LAYERED; i++) {
		if (layered_background[i] == background && layered_over[i] == over)
			return &layered_effects[i];
		if (layered_background[i])
			continue;
		/* A new combination: the needs of both together */
		layered_background[i] = background;
		layered_over[i] = over;
		layered_effects[i] = (struct effect){
			.name = over->name,
			.fps = MAX(background->fps, over->fps),
			.full_resolution = true,
			.wants_gravity = background->wants_gravity || over->wants_gravity,
			.wants_motion = background->wants_motion || over->wants_motion,
			.wants_orientation = background->wants_orientation || over->wants_orientation,
			.create = layered_creates[i],
			.update = layered_update,
			.step = layered_step,
			.wake = layered_wake,
			.build = layered_build,
			.render = layered_render,
			.set_visible = layered_set_visible,
			.destroy = layered_destroy,
		};
		return &layered_effects[i];
	}
	return background;
}
