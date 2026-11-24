// A stand-in for OpenGL ES 2, for the plinius-ambient tests: no GPU and no context, every call
// is written to stdout as one line starting with "gl " (in the order the program makes them),
// and the state a test asks about is kept: which program, buffer, texture and framebuffer are
// bound, the blend function, the uniforms by name, and what was uploaded.
//
// With FAKEGL_DUMP=<dir> every texture upload is written to <dir>/tex-<n>.raw (the rows as GL
// reads them, without the unpack padding), every buffer upload to <dir>/buf-<n>.raw and every
// shader source to <dir>/shader-<n>.vert or .frag (for glslangValidator), so a test compares
// them with what it computed itself.
//
// Failures on request: FAKEGL_FAIL_COMPILE=<text> (a shader whose source contains it does not
// compile), FAKEGL_FAIL_LINK=<text> (a program with such a shader does not link),
// FAKEGL_FB_STATUS=<hex> (what glCheckFramebufferStatus says), FAKEGL_MAX_TEXTURE=<n>
// (GL_MAX_TEXTURE_SIZE, 4096 without it).
#include <GLES2/gl2.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MAX_OBJECTS 65536
#define MAX_UNIFORMS 4096

enum kind { FREE, SHADER, PROGRAM, BUFFER, TEXTURE, FRAMEBUFFER };

struct object {
	enum kind kind;
	GLenum type;      /* shader: vertex or fragment */
	char *source;     /* shader; program: those of its shaders */
	int compiled;     /* shader */
	GLuint attached[8];
	int n_attached;
	int linked;       /* program */
	int width, height; /* texture */
	GLenum format;
	size_t size;      /* buffer */
};

static struct object objects[MAX_OBJECTS];
static GLuint next_name = 1;
static struct {
	GLuint program;
	char name[64];
} uniforms[MAX_UNIFORMS];
static int n_uniforms;
static GLuint program, array_buffer, framebuffer, textures[32];
static int unit, blend, unpack_alignment = 4, viewport[4];
static GLenum blend_src = GL_ONE, blend_dst = GL_ZERO;
static unsigned enabled_attribs;
static int dumps;

static const char *env(const char *name)
{
	const char *v = getenv(name);
	return v && *v ? v : NULL;
}

__attribute__((constructor)) static void fakegl_init(void)
{
	setvbuf(stdout, NULL, _IOLBF, 0);
}

static GLuint make(enum kind kind)
{
	if (next_name >= MAX_OBJECTS) {
		fprintf(stderr, "fakegl: out of names\n");
		abort();
	}
	GLuint name = next_name++;
	objects[name] = (struct object){.kind = kind};
	return name;
}

static struct object *get(GLuint name, enum kind kind)
{
	if (name == 0 || name >= MAX_OBJECTS || objects[name].kind != kind) {
		printf("gl error: %u is no live object of kind %d\n", name, kind);
		return NULL;
	}
	return &objects[name];
}

static void release(GLuint name, enum kind kind, const char *what)
{
	if (name == 0)
		return;
	struct object *o = get(name, kind);
	if (!o)
		return;
	free(o->source);
	*o = (struct object){.kind = FREE};
	printf("gl delete %s %u\n", what, name);
}

static const char *enum_name(GLenum e)
{
	switch (e) {
	case GL_ZERO: return "ZERO";
	case GL_ONE: return "ONE";
	case GL_ONE_MINUS_SRC_ALPHA: return "ONE_MINUS_SRC_ALPHA";
	case GL_SRC_ALPHA: return "SRC_ALPHA";
	case GL_TRIANGLES: return "TRIANGLES";
	case GL_TRIANGLE_STRIP: return "TRIANGLE_STRIP";
	case GL_RGBA: return "RGBA";
	case GL_RGB: return "RGB";
	case GL_LUMINANCE: return "LUMINANCE";
	case GL_LUMINANCE_ALPHA: return "LUMINANCE_ALPHA";
	case GL_VERTEX_SHADER: return "VERTEX";
	case GL_FRAGMENT_SHADER: return "FRAGMENT";
	case GL_STATIC_DRAW: return "STATIC_DRAW";
	case GL_STREAM_DRAW: return "STREAM_DRAW";
	case GL_BLEND: return "BLEND";
	case GL_LINEAR: return "LINEAR";
	case GL_CLAMP_TO_EDGE: return "CLAMP_TO_EDGE";
	case GL_TEXTURE_MIN_FILTER: return "MIN_FILTER";
	case GL_TEXTURE_MAG_FILTER: return "MAG_FILTER";
	case GL_TEXTURE_WRAP_S: return "WRAP_S";
	case GL_TEXTURE_WRAP_T: return "WRAP_T";
	default: {
		static char buf[8][16];
		static int i;
		i = (i + 1) % 8;
		snprintf(buf[i], sizeof(buf[i]), "0x%x", e);
		return buf[i];
	}
	}
}

static int bytes_per_pixel(GLenum format)
{
	switch (format) {
	case GL_LUMINANCE: return 1;
	case GL_LUMINANCE_ALPHA: return 2;
	case GL_RGB: return 3;
	default: return 4;
	}
}

/* The rows as GL reads them: each one padded to the unpack alignment */
static char *dump_pixels(const void *pixels, int width, int height, GLenum format)
{
	static char path[4096];
	const char *dir = env("FAKEGL_DUMP");
	if (!dir || !pixels)
		return NULL;
	snprintf(path, sizeof(path), "%s/tex-%d.raw", dir, dumps++);
	FILE *f = fopen(path, "wb");
	if (!f) {
		perror(path);
		abort();
	}
	size_t row = (size_t)width * bytes_per_pixel(format);
	size_t stride = (row + unpack_alignment - 1) / unpack_alignment * unpack_alignment;
	for (int y = 0; y < height; y++)
		fwrite((const uint8_t *)pixels + y * stride, 1, row, f);
	fclose(f);
	return path;
}

/* ---- Shaders and programs ---- */

GLuint glCreateShader(GLenum type)
{
	GLuint s = make(SHADER);
	objects[s].type = type;
	printf("gl create shader %u %s\n", s, enum_name(type));
	return s;
}

void glShaderSource(GLuint shader, GLsizei count, const GLchar *const *string, const GLint *length)
{
	struct object *o = get(shader, SHADER);
	if (!o)
		return;
	size_t len = 0;
	for (int i = 0; i < count; i++)
		len += length && length[i] >= 0 ? (size_t)length[i] : strlen(string[i]);
	free(o->source);
	o->source = calloc(1, len + 1);
	for (int i = 0; i < count; i++)
		strncat(o->source, string[i], length && length[i] >= 0 ? (size_t)length[i] : strlen(string[i]));
	const char *dir = env("FAKEGL_DUMP");
	if (dir) {
		char path[4096];
		snprintf(path, sizeof(path), "%s/shader-%d.%s", dir, dumps++, o->type == GL_VERTEX_SHADER ? "vert" : "frag");
		FILE *f = fopen(path, "w");
		fputs(o->source, f);
		fclose(f);
		printf("gl shader source %u %s\n", shader, path);
	}
}

void glCompileShader(GLuint shader)
{
	struct object *o = get(shader, SHADER);
	if (!o)
		return;
	const char *fail = env("FAKEGL_FAIL_COMPILE");
	o->compiled = !(fail && o->source && strstr(o->source, fail));
	printf("gl compile shader %u %s\n", shader, o->compiled ? "ok" : "failed");
}

void glGetShaderiv(GLuint shader, GLenum pname, GLint *params)
{
	struct object *o = get(shader, SHADER);
	*params = o && pname == GL_COMPILE_STATUS ? o->compiled : 0;
}

void glGetShaderInfoLog(GLuint shader, GLsizei bufSize, GLsizei *length, GLchar *infoLog)
{
	snprintf(infoLog, bufSize, "fake compile error in shader %u", shader);
	if (length)
		*length = strlen(infoLog);
}

void glDeleteShader(GLuint shader)
{
	release(shader, SHADER, "shader");
}

GLuint glCreateProgram(void)
{
	GLuint p = make(PROGRAM);
	printf("gl create program %u\n", p);
	return p;
}

void glAttachShader(GLuint prog, GLuint shader)
{
	struct object *o = get(prog, PROGRAM);
	if (o && get(shader, SHADER) && o->n_attached < 8)
		o->attached[o->n_attached++] = shader;
}

void glBindAttribLocation(GLuint prog, GLuint index, const GLchar *name)
{
	printf("gl bind attrib %u %u %s\n", prog, index, name);
}

void glLinkProgram(GLuint prog)
{
	struct object *o = get(prog, PROGRAM);
	if (!o)
		return;
	const char *fail = env("FAKEGL_FAIL_LINK");
	o->linked = 1;
	/* The program keeps its shaders' sources: they may be deleted once it is linked */
	free(o->source);
	o->source = strdup("");
	for (int i = 0; i < o->n_attached; i++) {
		struct object *s = &objects[o->attached[i]];
		if (s->kind != SHADER || !s->compiled || (fail && s->source && strstr(s->source, fail)))
			o->linked = 0;
		if (s->kind == SHADER && s->source) {
			o->source = realloc(o->source, strlen(o->source) + strlen(s->source) + 1);
			strcat(o->source, s->source);
		}
	}
	printf("gl link program %u %s\n", prog, o->linked ? "ok" : "failed");
}

void glGetProgramiv(GLuint prog, GLenum pname, GLint *params)
{
	struct object *o = get(prog, PROGRAM);
	*params = o && pname == GL_LINK_STATUS ? o->linked : 0;
}

void glGetProgramInfoLog(GLuint prog, GLsizei bufSize, GLsizei *length, GLchar *infoLog)
{
	snprintf(infoLog, bufSize, "fake link error in program %u", prog);
	if (length)
		*length = strlen(infoLog);
}

void glDeleteProgram(GLuint prog)
{
	if (prog == program)
		program = 0;
	release(prog, PROGRAM, "program");
}

void glUseProgram(GLuint prog)
{
	program = prog;
	printf("gl use program %u\n", prog);
}

/* -1 for a name no attached shader declares, as GL does for inactive uniforms */
GLint glGetUniformLocation(GLuint prog, const GLchar *name)
{
	struct object *o = get(prog, PROGRAM);
	if (!o || !o->linked)
		return -1;
	if (!strstr(o->source, name) || n_uniforms >= MAX_UNIFORMS)
		return -1;
	for (int i = 0; i < n_uniforms; i++)
		if (uniforms[i].program == prog && !strcmp(uniforms[i].name, name))
			return i;
	uniforms[n_uniforms].program = prog;
	snprintf(uniforms[n_uniforms].name, sizeof(uniforms[n_uniforms].name), "%s", name);
	return n_uniforms++;
}

static void uniform(GLint location, int n, const GLfloat *v, int is_int, GLint iv)
{
	if (location < 0) {
		printf("gl uniform -1\n");
		return;
	}
	if (location >= n_uniforms || uniforms[location].program != program) {
		printf("gl error: uniform %d set without its program in use\n", location);
		return;
	}
	printf("gl uniform %s", uniforms[location].name);
	if (is_int)
		printf(" %d", iv);
	for (int i = 0; i < n; i++)
		printf(" %.6g", v[i]);
	printf("\n");
}

void glUniform1f(GLint location, GLfloat v0)
{
	uniform(location, 1, &v0, 0, 0);
}

void glUniform1i(GLint location, GLint v0)
{
	uniform(location, 0, NULL, 1, v0);
}

void glUniform2f(GLint location, GLfloat v0, GLfloat v1)
{
	GLfloat v[] = {v0, v1};
	uniform(location, 2, v, 0, 0);
}

void glUniform2fv(GLint location, GLsizei count, const GLfloat *value)
{
	uniform(location, 2 * count, value, 0, 0);
}

void glUniform3fv(GLint location, GLsizei count, const GLfloat *value)
{
	uniform(location, 3 * count, value, 0, 0);
}

void glUniformMatrix3fv(GLint location, GLsizei count, GLboolean transpose, const GLfloat *value)
{
	if (transpose)
		printf("gl error: GLES 2 takes no transposed matrices\n");
	uniform(location, 9 * count, value, 0, 0);
}

/* ---- Buffers ---- */

void glGenBuffers(GLsizei n, GLuint *buffers)
{
	for (int i = 0; i < n; i++) {
		buffers[i] = make(BUFFER);
		printf("gl gen buffer %u\n", buffers[i]);
	}
}

void glBindBuffer(GLenum target, GLuint buffer)
{
	if (target == GL_ARRAY_BUFFER)
		array_buffer = buffer;
}

void glBufferData(GLenum target, GLsizeiptr size, const void *data, GLenum usage)
{
	struct object *o = get(array_buffer, BUFFER);
	if (!o)
		return;
	o->size = size;
	const char *dir = env("FAKEGL_DUMP");
	char path[4096] = "-";
	if (dir && data) {
		snprintf(path, sizeof(path), "%s/buf-%d.raw", dir, dumps++);
		FILE *f = fopen(path, "wb");
		fwrite(data, 1, size, f);
		fclose(f);
	}
	printf("gl buffer data %u %zd %s %s\n", array_buffer, (ssize_t)size, enum_name(usage), path);
}

void glDeleteBuffers(GLsizei n, const GLuint *buffers)
{
	for (int i = 0; i < n; i++) {
		if (buffers[i] == array_buffer)
			array_buffer = 0;
		release(buffers[i], BUFFER, "buffer");
	}
}

void glVertexAttribPointer(GLuint index, GLint size, GLenum type, GLboolean normalized, GLsizei stride,
			   const void *pointer)
{
	printf("gl attrib %u size %d stride %d offset %zu buffer %u\n", index, size, stride, (size_t)pointer,
	       array_buffer);
}

void glEnableVertexAttribArray(GLuint index)
{
	enabled_attribs |= 1u << index;
}

void glDisableVertexAttribArray(GLuint index)
{
	enabled_attribs &= ~(1u << index);
}

/* ---- Textures ---- */

void glGenTextures(GLsizei n, GLuint *names)
{
	for (int i = 0; i < n; i++) {
		names[i] = make(TEXTURE);
		printf("gl gen texture %u\n", names[i]);
	}
}

void glActiveTexture(GLenum texture)
{
	unit = texture - GL_TEXTURE0;
}

void glBindTexture(GLenum target, GLuint texture)
{
	textures[unit] = texture;
}

void glTexParameteri(GLenum target, GLenum pname, GLint param)
{
	printf("gl tex parameter %u %s %s\n", textures[unit], enum_name(pname), enum_name(param));
}

void glPixelStorei(GLenum pname, GLint param)
{
	if (pname == GL_UNPACK_ALIGNMENT)
		unpack_alignment = param;
}

void glTexImage2D(GLenum target, GLint level, GLint internalformat, GLsizei width, GLsizei height, GLint border,
		  GLenum format, GLenum type, const void *pixels)
{
	struct object *o = get(textures[unit], TEXTURE);
	if (!o)
		return;
	if (internalformat != (GLint)format)
		printf("gl error: GLES 2 needs internalformat == format\n");
	o->width = width;
	o->height = height;
	o->format = format;
	char *path = dump_pixels(pixels, width, height, format);
	printf("gl tex image %u %dx%d %s %s\n", textures[unit], width, height, enum_name(format), path ? path : "-");
}

void glTexSubImage2D(GLenum target, GLint level, GLint xoffset, GLint yoffset, GLsizei width, GLsizei height,
		     GLenum format, GLenum type, const void *pixels)
{
	struct object *o = get(textures[unit], TEXTURE);
	if (!o)
		return;
	if (xoffset + width > o->width || yoffset + height > o->height || format != o->format)
		printf("gl error: sub image outside the texture or in another format\n");
	char *path = dump_pixels(pixels, width, height, format);
	printf("gl tex sub image %u %d,%d %dx%d %s %s\n", textures[unit], xoffset, yoffset, width, height,
	       enum_name(format), path ? path : "-");
}

void glDeleteTextures(GLsizei n, const GLuint *names)
{
	for (int i = 0; i < n; i++)
		release(names[i], TEXTURE, "texture");
}

/* ---- Framebuffers ---- */

void glGenFramebuffers(GLsizei n, GLuint *names)
{
	for (int i = 0; i < n; i++) {
		names[i] = make(FRAMEBUFFER);
		printf("gl gen framebuffer %u\n", names[i]);
	}
}

void glBindFramebuffer(GLenum target, GLuint fb)
{
	framebuffer = fb;
	printf("gl bind framebuffer %u\n", fb);
}

void glFramebufferTexture2D(GLenum target, GLenum attachment, GLenum textarget, GLuint texture, GLint level)
{
	printf("gl framebuffer texture %u %u\n", framebuffer, texture);
}

GLenum glCheckFramebufferStatus(GLenum target)
{
	const char *status = env("FAKEGL_FB_STATUS");
	return status ? (GLenum)strtoul(status, NULL, 16) : GL_FRAMEBUFFER_COMPLETE;
}

void glDeleteFramebuffers(GLsizei n, const GLuint *names)
{
	for (int i = 0; i < n; i++)
		release(names[i], FRAMEBUFFER, "framebuffer");
}

/* ---- State and drawing ---- */

void glGetIntegerv(GLenum pname, GLint *data)
{
	if (pname == GL_FRAMEBUFFER_BINDING)
		*data = framebuffer;
	else if (pname == GL_MAX_TEXTURE_SIZE)
		*data = env("FAKEGL_MAX_TEXTURE") ? atoi(env("FAKEGL_MAX_TEXTURE")) : 4096;
	else
		*data = 0;
}

void glEnable(GLenum cap)
{
	if (cap == GL_BLEND)
		blend = 1;
}

void glDisable(GLenum cap)
{
	if (cap == GL_BLEND)
		blend = 0;
}

void glBlendFunc(GLenum sfactor, GLenum dfactor)
{
	blend_src = sfactor;
	blend_dst = dfactor;
}

void glViewport(GLint x, GLint y, GLsizei width, GLsizei height)
{
	viewport[0] = x;
	viewport[1] = y;
	viewport[2] = width;
	viewport[3] = height;
}

void glClearColor(GLfloat red, GLfloat green, GLfloat blue, GLfloat alpha)
{
	printf("gl clear color %.3g %.3g %.3g %.3g\n", red, green, blue, alpha);
}

void glClear(GLbitfield mask)
{
	printf("gl clear framebuffer %u viewport %dx%d\n", framebuffer, viewport[2], viewport[3]);
}

void glDrawArrays(GLenum mode, GLint first, GLsizei count)
{
	struct object *b = array_buffer ? get(array_buffer, BUFFER) : NULL;
	char blending[64] = "off";
	if (blend)
		snprintf(blending, sizeof(blending), "%s,%s", enum_name(blend_src), enum_name(blend_dst));
	printf("gl draw %s %d %d program %u framebuffer %u viewport %dx%d blend %s attribs 0x%x buffer %u %zu"
	       " textures %u %u %u\n",
	       enum_name(mode), first, count, program, framebuffer, viewport[2], viewport[3], blending,
	       enabled_attribs, array_buffer, b ? b->size : 0, textures[0], textures[1], textures[2]);
}

/* Objects still alive: a test checks that destroying an effect frees what it made */
void fakegl_report(void)
{
	int counts[6] = {0};
	for (GLuint i = 1; i < next_name; i++)
		counts[objects[i].kind]++;
	printf("gl live shaders %d programs %d buffers %d textures %d framebuffers %d\n", counts[SHADER],
	       counts[PROGRAM], counts[BUFFER], counts[TEXTURE], counts[FRAMEBUFFER]);
}
