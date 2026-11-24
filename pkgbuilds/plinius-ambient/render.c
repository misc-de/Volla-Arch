// SPDX-License-Identifier: MIT
// plinius-ambient: drawing one frame of an effect, see render.h.
#include "render.h"

#include <glib.h>
#include <stddef.h>
#include <stdlib.h>

/* The composite pass: one quad over the whole framebuffer, v_uv the texture coordinate */
static const char composite_vertex_shader[] =
	"attribute vec2 a_pos;\n"
	"varying vec2 v_uv;\n"
	"void main() {\n"
	"  v_uv = a_pos * 0.5 + 0.5;\n"
	"  gl_Position = vec4(a_pos, 0.0, 1.0);\n"
	"}\n";

static GLuint compile(GLenum type, const char *source)
{
	GLuint shader = glCreateShader(type);
	GLint ok = 0;
	glShaderSource(shader, 1, &source, NULL);
	glCompileShader(shader);
	glGetShaderiv(shader, GL_COMPILE_STATUS, &ok);
	if (!ok) {
		char log[1024] = "";
		glGetShaderInfoLog(shader, sizeof(log), NULL, log);
		g_warning("Shader does not compile: %s", log);
	}
	return shader;
}

/* Builds the program for a fragment shader once and keeps it until another one is needed */
static bool use_program(struct render_program *p, const char *vertex_shader, const char *fragment_shader,
			const char *name)
{
	if (p->id && p->fragment_shader == fragment_shader) {
		glUseProgram(p->id);
		return true;
	}
	if (p->id)
		glDeleteProgram(p->id);
	GLuint vs = compile(GL_VERTEX_SHADER, vertex_shader);
	GLuint fs = compile(GL_FRAGMENT_SHADER, fragment_shader);
	p->id = glCreateProgram();
	glAttachShader(p->id, vs);
	glAttachShader(p->id, fs);
	glBindAttribLocation(p->id, 0, "a_pos");
	glBindAttribLocation(p->id, 1, "a_uv");
	glBindAttribLocation(p->id, 2, "a_color");
	glBindAttribLocation(p->id, 3, "a_param");
	glLinkProgram(p->id);
	glDeleteShader(vs);
	glDeleteShader(fs);
	GLint ok = 0;
	glGetProgramiv(p->id, GL_LINK_STATUS, &ok);
	if (!ok) {
		char log[1024] = "";
		glGetProgramInfoLog(p->id, sizeof(log), NULL, log);
		g_warning("Program for %s does not link: %s", name, log);
		glDeleteProgram(p->id);
		p->id = 0;
		p->fragment_shader = NULL;
		return false;
	}
	p->fragment_shader = fragment_shader;
	p->u_size = glGetUniformLocation(p->id, "u_size");
	p->u_field = glGetUniformLocation(p->id, "u_field");
	p->u_texel = glGetUniformLocation(p->id, "u_texel");
	glUseProgram(p->id);
	return true;
}

/* The offscreen texture at the framebuffer's size (8 bits per channel: what every GLES 2 has).
 * Leaves the framebuffer bound that was: when it fails, the caller draws there without it. */
static bool ensure_field(struct renderer *r, int width, int height)
{
	GLint before = 0;

	if (r->fbo && r->field_width == width && r->field_height == height)
		return true;
	glGetIntegerv(GL_FRAMEBUFFER_BINDING, &before);
	if (!r->fbo) {
		glGenFramebuffers(1, &r->fbo);
		glGenTextures(1, &r->field);
	}
	glBindTexture(GL_TEXTURE_2D, r->field);
	glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, width, height, 0, GL_RGBA, GL_UNSIGNED_BYTE, NULL);
	glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
	glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
	glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
	glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
	glBindFramebuffer(GL_FRAMEBUFFER, r->fbo);
	glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, r->field, 0);
	GLenum status = glCheckFramebufferStatus(GL_FRAMEBUFFER);
	glBindFramebuffer(GL_FRAMEBUFFER, before);
	if (status != GL_FRAMEBUFFER_COMPLETE) {
		g_warning("Offscreen framebuffer incomplete (0x%x)", status);
		r->field_width = r->field_height = 0;
		return false;
	}
	r->field_width = width;
	r->field_height = height;
	return true;
}

static void draw_quads(struct renderer *r, const struct scene *scene)
{
	glUniform2f(r->particles.u_size, scene->width, scene->height);
	if (!r->vbo)
		glGenBuffers(1, &r->vbo);
	glBindBuffer(GL_ARRAY_BUFFER, r->vbo);
	glBufferData(GL_ARRAY_BUFFER, r->vertices.len * sizeof(struct vertex), r->vertices.data, GL_STREAM_DRAW);
	glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, sizeof(struct vertex), (void *)offsetof(struct vertex, x));
	glVertexAttribPointer(1, 2, GL_FLOAT, GL_FALSE, sizeof(struct vertex), (void *)offsetof(struct vertex, u));
	glVertexAttribPointer(2, 4, GL_FLOAT, GL_FALSE, sizeof(struct vertex), (void *)offsetof(struct vertex, r));
	glVertexAttribPointer(3, 2, GL_FLOAT, GL_FALSE, sizeof(struct vertex), (void *)offsetof(struct vertex, p));
	for (int i = 0; i < 4; i++)
		glEnableVertexAttribArray(i);
	glDrawArrays(GL_TRIANGLES, 0, r->vertices.len);
}

static void draw_effect(struct renderer *r, const struct effect *effect, void *state, const struct scene *scene,
			int width, int height, bool clear, bool tint)
{
	GLint target = 0;
	glGetIntegerv(GL_FRAMEBUFFER_BINDING, &target);
	r->vertices.len = 0;
	effect->build(state, scene, &r->vertices);
	bool composited = effect->composite_shader && r->vertices.len && ensure_field(r, width, height);

	if (composited) {
		/* The field: every quad adds its blob */
		glBindFramebuffer(GL_FRAMEBUFFER, r->fbo);
		glViewport(0, 0, width, height);
		glClearColor(0, 0, 0, 0);
		glClear(GL_COLOR_BUFFER_BIT);
		if (use_program(&r->particles, effect_vertex_shader, effect->fragment_shader, effect->name)) {
			glEnable(GL_BLEND);
			glBlendFunc(GL_ONE, GL_ONE);
			draw_quads(r, scene);
		}
		glBindFramebuffer(GL_FRAMEBUFFER, target);
	}

	glViewport(0, 0, width, height);
	if (clear) {
		if (tint)
			glClearColor(0.3f, 0, 0, 0.3f);
		else
			glClearColor(0, 0, 0, 0);
		glClear(GL_COLOR_BUFFER_BIT);
	}
	if (!r->vertices.len)
		return;
	glEnable(GL_BLEND);
	glBlendFunc(GL_ONE, GL_ONE_MINUS_SRC_ALPHA);
	if (!composited) {
		if (use_program(&r->particles, effect_vertex_shader, effect->fragment_shader, effect->name))
			draw_quads(r, scene);
		return;
	}
	if (!use_program(&r->composite, composite_vertex_shader, effect->composite_shader, effect->name))
		return;
	static const float corners[] = {-1, -1, 1, -1, -1, 1, 1, 1};
	if (!r->quad_vbo) {
		glGenBuffers(1, &r->quad_vbo);
		glBindBuffer(GL_ARRAY_BUFFER, r->quad_vbo);
		glBufferData(GL_ARRAY_BUFFER, sizeof(corners), corners, GL_STATIC_DRAW);
	}
	glBindBuffer(GL_ARRAY_BUFFER, r->quad_vbo);
	for (int i = 1; i < 4; i++)
		glDisableVertexAttribArray(i);
	glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 0, NULL);
	glEnableVertexAttribArray(0);
	glActiveTexture(GL_TEXTURE0);
	glBindTexture(GL_TEXTURE_2D, r->field);
	glUniform1i(r->composite.u_field, 0);
	glUniform2f(r->composite.u_texel, 1.0f / width, 1.0f / height);
	glUniform2f(r->composite.u_size, scene->width, scene->height);
	glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
}

void render_frame(struct renderer *r, const struct effect *effect, void *state, const struct scene *scene,
		  int width, int height, bool tint)
{
	if (effect->render)
		effect->render(state, scene, width, height);
	else
		draw_effect(r, effect, state, scene, width, height, true, tint);
}

void render_frame_over(struct renderer *r, const struct effect *effect, void *state, const struct scene *scene,
		       int width, int height)
{
	draw_effect(r, effect, state, scene, width, height, false, false);
}

void render_release(struct renderer *r)
{
	if (r->particles.id)
		glDeleteProgram(r->particles.id);
	if (r->composite.id)
		glDeleteProgram(r->composite.id);
	if (r->vbo)
		glDeleteBuffers(1, &r->vbo);
	if (r->quad_vbo)
		glDeleteBuffers(1, &r->quad_vbo);
	if (r->fbo) {
		glDeleteFramebuffers(1, &r->fbo);
		glDeleteTextures(1, &r->field);
	}
	free(r->vertices.data);
	*r = (struct renderer){0};
}
