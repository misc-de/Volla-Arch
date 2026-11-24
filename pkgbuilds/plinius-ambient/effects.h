// SPDX-License-Identifier: MIT
// plinius-ambient: the effects. Each one moves its particles on the CPU and hands them over as
// textured quads; a fragment shader of its own shapes them (snowflake, glow, raindrop, ball).
// Water draws soft blobs into a field first, which a second shader turns into one surface (see
// render.h).
#pragma once

#include <stdbool.h>
#include <stddef.h>

/* One corner of a particle quad */
struct vertex {
	float x, y;       /* logical pixels, y down */
	float u, v;       /* -1..1 across the quad, v pointing up */
	float r, g, b, a; /* premultiplied colour; rgb above a adds light rather than covering */
	float p, q;       /* parameters for the effect's fragment shader */
};

struct vertex_buffer {
	struct vertex *data;
	size_t len, size;
};

/* What an effect sees of the world */
struct scene {
	float width, height; /* logical pixels */
	float scale;         /* buffer pixels per logical pixel */
	float gravity[2];    /* in the screen plane, m/s², y down; straight down without a sensor */
	/* How hard the phone is being moved: the square of its acceleration without gravity
	 * (m²/s⁴), smoothed over about a second; 0 without the sensor */
	float shake;
	/* The phone's change of speed in the screen plane since the last step (m/s, y down) */
	float impulse[2];
	int density;         /* 0 low, 1 medium, 2 high */
	/* The phone's orientation (game rotation vector): a unit quaternion x, y, z, w turning the
	 * device's axes into a world with z up; has_orientation false without the sensor */
	float orientation[4];
	bool has_orientation;
	/* Degrees the screen's axes are turned against the device's (the output transform: 90 when
	 * the device's left edge is up) */
	int screen_angle;
	const char *picture; /* the 360° photo (org.plinius.ambient panorama), NULL if none */
	const char *video;   /* the video or animated GIF (org.plinius.ambient video), NULL if none */
	/* Asks for a frame soon, also at rest: for an effect with news of its own (a decoded video
	 * frame). Only from the main loop; NULL in a client that draws all the time. */
	void (*wake)(void);
};

struct effect {
	const char *name;          /* as in the GSettings key org.plinius.ambient effect */
	int fps;                   /* frames per second it is drawn at */
	bool full_resolution;      /* sharp edges: drawn at the output scale, else at half of it */
	bool wants_gravity;        /* follows the tilt of the phone */
	bool wants_motion;         /* follows the phone being moved and shaken (shake, impulse) */
	const char *fragment_shader;
	/* NULL: the quads are the picture. Else they are added up into a texture first (u_field,
	 * u_texel the size of one of its pixels, v_uv the position in it) and this shader draws the
	 * picture from that field. */
	const char *composite_shader;
	void *(*create)(const struct scene *scene);
	/* The size changed, by sx and sy against the old one (both differ after a rotation) */
	void (*update)(void *state, const struct scene *scene, float sx, float sy);
	/* Advances by dt seconds; false when everything has come to rest (no need to draw) */
	bool (*step)(void *state, const struct scene *scene, float dt);
	/* At rest: whether the scene changed enough to move again (optional) */
	bool (*wake)(void *state, const struct scene *scene);
	void (*build)(void *state, const struct scene *scene, struct vertex_buffer *out);
	void (*destroy)(void *state);
	bool wants_orientation;    /* follows the phone's orientation (scene->orientation) */
	/* Optional: draws the frame itself instead of handing over quads (build is not called) */
	void (*render)(void *state, const struct scene *scene, int width, int height);
	/* Optional: whether the effect can be seen (on screen and the display on). Told after
	 * create and on every change; until then it counts as hidden. The video plays only while
	 * it is seen. */
	void (*set_visible)(void *state, bool visible);
};

/* NULL for unknown names and "none" */
const struct effect *effect_find(const char *name);

/* The vertex shader all effects share */
extern const char effect_vertex_shader[];
