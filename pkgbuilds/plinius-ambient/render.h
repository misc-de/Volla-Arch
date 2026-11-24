// SPDX-License-Identifier: MIT
// plinius-ambient: drawing one frame of an effect with OpenGL ES 2, shared by the client and the
// preview tool. The effect's quads go straight into the current framebuffer, or, for an effect
// with a composite shader (water), first into a texture that the composite shader then turns
// into the picture (a field of soft blobs that becomes one surface).
#pragma once

#include "effects.h"

#include <GLES2/gl2.h>
#include <stdbool.h>

struct render_program {
	const char *fragment_shader; /* what it was built from */
	GLuint id;
	GLint u_size, u_field, u_texel;
};

struct renderer {
	struct render_program particles, composite;
	GLuint vbo, quad_vbo;
	GLuint fbo, field;   /* the offscreen texture for composited effects */
	int field_width, field_height;
	struct vertex_buffer vertices;
};

/* Draws the effect's current state into the bound framebuffer of width × height pixels, cleared
 * to transparent (or to a faint red with tint, to see where the surface is). Needs the GL
 * context current. */
void render_frame(struct renderer *r, const struct effect *effect, void *state, const struct scene *scene,
		  int width, int height, bool tint);

/* The same over what the framebuffer holds already (the 360° photo), without clearing it */
void render_frame_over(struct renderer *r, const struct effect *effect, void *state, const struct scene *scene,
		       int width, int height);

/* Frees the GL objects (context current) and the vertex memory */
void render_release(struct renderer *r);
