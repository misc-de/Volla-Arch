// SPDX-License-Identifier: MIT
// plinius-ambient: a 360° photo as the background, seen through the phone as through a window.
//
// The photo is equirectangular (the whole sphere unrolled onto a 2:1 rectangle, as 360° cameras
// and Muga's "Set as Background" deliver it) and lies around the viewer; turning the phone turns
// the view, 1:1. The orientation is sensor-proxy-bridge's game rotation vector (gyroscope and
// accelerometer, no magnetometer, so no north: the view is re-centred on the middle of the photo
// whenever the sensor is claimed anew, i.e. each time the background comes into sight).
//
// The virtual camera looks TILT_DEG above the back of the phone: held the usual way, tilted back,
// the horizon is in the middle of the screen rather than the ground.
//
// Drawn as one quad whose corners carry their ray directions (linear across the screen, exact
// for a perspective view); the fragment shader turns each ray into longitude and latitude. The
// photo is decoded on a thread and uploaded once; until then the surface stays transparent and
// Phosh's own background shows. At rest (the phone lying still) nothing is drawn.
//
// The photo is a background, not an effect: layered_with() (layered.c) puts any of the effects
// (snow, rain, balls...) over it, drawn into the same frame after the photo.
#include "effects.h"
#include "panorama.h"

#include <GLES2/gl2.h>
#include <gdk-pixbuf/gdk-pixbuf.h>
#include <math.h>
#include <string.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

/* Long edge of the texture: a 2400 px screen showing FOV_DEG of the sphere needs about 8600 px
 * round the circle to be drawn texel for pixel; 8192 is the limit of many mobile GPUs */
#define MAX_DIM 8192
/* Field of view across the long side of the screen */
#define FOV_DEG 95.0f
/* How far the view looks above the back of the phone */
#define TILT_DEG 40.0f
/* The drawn orientation follows the sensor with this time constant (s): smooths its 50 Hz
 * steps on a 60-120 Hz display. Less than one sample's time: at 0.05 the view trailed a turn
 * noticeably */
#define SMOOTHING_S 0.015f
/* Below this (largest change of a matrix element, ~0.01°) the view counts as at rest */
#define REST_EPSILON 2e-4f

typedef float mat3[3][3];

/* Loaded on a thread; shared with it until it is done */
struct loader {
	gint refs;
	char *path;
	GdkPixbuf *pixbuf; /* set by the thread, taken by the main loop */
	gboolean done;
	GMutex lock;
};

struct panorama {
	struct loader *loader;
	GLuint program, texture, vbo;
	GLint u_rotation, u_tan, u_picture;
	gboolean loaded, failed;
	mat3 shown;         /* camera to world, as drawn */
	gboolean have_shown;
	gboolean had_orientation;
	float yaw_offset;   /* degrees added to the sensor's heading */
};

/* ---- Rotations ---- */

static void mat_mul(mat3 a, mat3 b, mat3 out)
{
	mat3 r;
	for (int i = 0; i < 3; i++)
		for (int j = 0; j < 3; j++)
			r[i][j] = a[i][0] * b[0][j] + a[i][1] * b[1][j] + a[i][2] * b[2][j];
	memcpy(out, r, sizeof(r));
}

static void rot_x(float deg, mat3 out)
{
	float c = cosf(deg * (float)M_PI / 180), s = sinf(deg * (float)M_PI / 180);
	mat3 m = {{1, 0, 0}, {0, c, -s}, {0, s, c}};
	memcpy(out, m, sizeof(m));
}

/* Right-handed about the vertical: lowers the longitude by deg */
static void rot_y(float deg, mat3 out)
{
	float c = cosf(deg * (float)M_PI / 180), s = sinf(deg * (float)M_PI / 180);
	mat3 m = {{c, 0, s}, {0, 1, 0}, {-s, 0, c}};
	memcpy(out, m, sizeof(m));
}

static void rot_z(float deg, mat3 out)
{
	float c = cosf(deg * (float)M_PI / 180), s = sinf(deg * (float)M_PI / 180);
	mat3 m = {{c, -s, 0}, {s, c, 0}, {0, 0, 1}};
	memcpy(out, m, sizeof(m));
}

static void quaternion_matrix(const float q[4], mat3 out)
{
	float x = q[0], y = q[1], z = q[2], w = q[3];
	float n = sqrtf(x * x + y * y + z * z + w * w);
	if (n > 0) {
		x /= n;
		y /= n;
		z /= n;
		w /= n;
	}
	mat3 m = {{1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)},
		  {2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)},
		  {2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)}};
	memcpy(out, m, sizeof(m));
}

/* Gram-Schmidt on the columns: a blend of two rotations back to a rotation */
static void orthonormalize(mat3 m)
{
	float c0[3] = {m[0][0], m[1][0], m[2][0]}, c1[3] = {m[0][1], m[1][1], m[2][1]};
	float n = sqrtf(c0[0] * c0[0] + c0[1] * c0[1] + c0[2] * c0[2]);
	for (int i = 0; i < 3; i++)
		c0[i] /= n > 0 ? n : 1;
	float d = c0[0] * c1[0] + c0[1] * c1[1] + c0[2] * c1[2];
	for (int i = 0; i < 3; i++)
		c1[i] -= d * c0[i];
	n = sqrtf(c1[0] * c1[0] + c1[1] * c1[1] + c1[2] * c1[2]);
	for (int i = 0; i < 3; i++)
		c1[i] /= n > 0 ? n : 1;
	float c2[3] = {c0[1] * c1[2] - c0[2] * c1[1], c0[2] * c1[0] - c0[0] * c1[2], c0[0] * c1[1] - c0[1] * c1[0]};
	for (int i = 0; i < 3; i++) {
		m[i][0] = c0[i];
		m[i][1] = c1[i];
		m[i][2] = c2[i];
	}
}

/* Longitude of the camera's forward direction (-z), degrees right of the middle of the photo */
static float forward_longitude(mat3 m)
{
	return atan2f(-m[0][2], m[2][2]) * 180 / (float)M_PI;
}

/* Camera to world for the phone's orientation. Android's sensor world (x east, y north, z up)
 * turned into the sphere's (y up, -z the middle of the photo); the screen's axes onto the
 * device's for a rotated output; the camera tilted up from the back of the phone. */
static void device_view(const struct scene *scene, mat3 out)
{
	static mat3 android_to_sphere = {{1, 0, 0}, {0, 0, 1}, {0, -1, 0}};
	mat3 q, screen, tilt;
	quaternion_matrix(scene->orientation, q);
	rot_z(scene->screen_angle, screen);
	rot_x(TILT_DEG, tilt);
	mat_mul(android_to_sphere, q, out);
	mat_mul(out, screen, out);
	mat_mul(out, tilt, out);
}

/* Where the view should be now */
static void target_view(struct panorama *p, const struct scene *scene, mat3 out)
{
	if (!scene->has_orientation) {
		/* No sensor: straight at the middle of the photo */
		rot_x(0, out);
		return;
	}
	mat3 device, yaw;
	device_view(scene, device);
	if (!p->had_orientation) {
		/* Claimed anew: the phone's heading becomes the middle of the photo */
		p->yaw_offset = -forward_longitude(device);
		p->have_shown = FALSE;
	}
	rot_y(-p->yaw_offset, yaw);
	mat_mul(yaw, device, out);
}

static float difference(mat3 a, mat3 b)
{
	float d = 0;
	for (int i = 0; i < 3; i++)
		for (int j = 0; j < 3; j++)
			d = fmaxf(d, fabsf(a[i][j] - b[i][j]));
	return d;
}

/* ---- Loading ---- */

static void loader_unref(struct loader *l)
{
	if (!g_atomic_int_dec_and_test(&l->refs))
		return;
	g_clear_object(&l->pixbuf);
	g_free(l->path);
	g_mutex_clear(&l->lock);
	g_free(l);
}

static gpointer load_thread(gpointer data)
{
	struct loader *l = data;
	g_autoptr(GError) error = NULL;
	int width = 0, height = 0;
	GdkPixbuf *pixbuf = NULL;
	if (gdk_pixbuf_get_file_info(l->path, &width, &height)) {
		if (width > MAX_DIM || height > MAX_DIM)
			pixbuf = gdk_pixbuf_new_from_file_at_scale(l->path, MAX_DIM, MAX_DIM, TRUE, &error);
		else
			pixbuf = gdk_pixbuf_new_from_file(l->path, &error);
	}
	if (pixbuf) {
		GdkPixbuf *upright = gdk_pixbuf_apply_embedded_orientation(pixbuf);
		g_object_unref(pixbuf);
		pixbuf = upright;
	} else {
		g_warning("Cannot load the 360° photo %s: %s", l->path, error ? error->message : "unknown format");
	}
	g_mutex_lock(&l->lock);
	l->pixbuf = pixbuf;
	l->done = TRUE;
	g_mutex_unlock(&l->lock);
	loader_unref(l);
	return NULL;
}

/* ---- GL ---- */

static const char vertex_shader[] =
	"attribute vec2 a_pos;\n"
	"varying vec3 v_ray;\n"
	"uniform mat3 u_rotation;\n"
	"uniform vec2 u_tan;\n"
	"void main() {\n"
	"  v_ray = u_rotation * vec3(a_pos * u_tan, -1.0);\n"
	"  gl_Position = vec4(a_pos, 0.0, 1.0);\n"
	"}\n";

/* highp: atan and asin in mediump band visibly near the poles */
static const char fragment_shader[] =
	"#ifdef GL_FRAGMENT_PRECISION_HIGH\n"
	"precision highp float;\n"
	"#else\n"
	"precision mediump float;\n"
	"#endif\n"
	"varying vec3 v_ray;\n"
	"uniform sampler2D u_picture;\n"
	"const float PI = 3.14159265358979;\n"
	"void main() {\n"
	"  vec3 d = normalize(v_ray);\n"
	"  vec2 uv = vec2(fract(0.5 + atan(d.x, -d.z) / (2.0 * PI)),\n"
	"                 0.5 - asin(clamp(d.y, -1.0, 1.0)) / PI);\n"
	"  gl_FragColor = vec4(texture2D(u_picture, uv).rgb, 1.0);\n"
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
		g_warning("360° shader does not compile: %s", log);
	}
	return shader;
}

static gboolean setup_gl(struct panorama *p)
{
	GLuint vs = compile(GL_VERTEX_SHADER, vertex_shader), fs = compile(GL_FRAGMENT_SHADER, fragment_shader);
	GLint ok = 0;
	p->program = glCreateProgram();
	glAttachShader(p->program, vs);
	glAttachShader(p->program, fs);
	glBindAttribLocation(p->program, 0, "a_pos");
	glLinkProgram(p->program);
	glDeleteShader(vs);
	glDeleteShader(fs);
	glGetProgramiv(p->program, GL_LINK_STATUS, &ok);
	if (!ok) {
		g_warning("360° program does not link");
		return FALSE;
	}
	p->u_rotation = glGetUniformLocation(p->program, "u_rotation");
	p->u_tan = glGetUniformLocation(p->program, "u_tan");
	p->u_picture = glGetUniformLocation(p->program, "u_picture");
	static const float corners[] = {-1, -1, 1, -1, -1, 1, 1, 1};
	glGenBuffers(1, &p->vbo);
	glBindBuffer(GL_ARRAY_BUFFER, p->vbo);
	glBufferData(GL_ARRAY_BUFFER, sizeof(corners), corners, GL_STATIC_DRAW);
	return TRUE;
}

/* The decoded photo into the texture, once (context current) */
static void upload(struct panorama *p)
{
	struct loader *l = p->loader;
	g_mutex_lock(&l->lock);
	gboolean done = l->done;
	GdkPixbuf *pixbuf = g_steal_pointer(&l->pixbuf);
	g_mutex_unlock(&l->lock);
	if (!done)
		return;
	if (!pixbuf) {
		p->failed = TRUE;
		return;
	}
	if (!setup_gl(p)) {
		g_object_unref(pixbuf);
		p->failed = TRUE;
		return;
	}
	GLint limit = 0;
	glGetIntegerv(GL_MAX_TEXTURE_SIZE, &limit);
	int width = gdk_pixbuf_get_width(pixbuf), height = gdk_pixbuf_get_height(pixbuf);
	if (limit > 0 && MAX(width, height) > limit) {
		double scale = (double)limit / MAX(width, height);
		GdkPixbuf *smaller = gdk_pixbuf_scale_simple(pixbuf, MAX(1, (int)(width * scale)),
							     MAX(1, (int)(height * scale)), GDK_INTERP_BILINEAR);
		g_object_unref(pixbuf);
		pixbuf = smaller;
		width = gdk_pixbuf_get_width(pixbuf);
		height = gdk_pixbuf_get_height(pixbuf);
	}
	int channels = gdk_pixbuf_get_n_channels(pixbuf), stride = gdk_pixbuf_get_rowstride(pixbuf);
	GLenum format = channels == 4 ? GL_RGBA : GL_RGB;
	const guint8 *pixels = gdk_pixbuf_read_pixels(pixbuf);
	guint8 *packed = NULL;
	if (stride != width * channels) {
		/* GLES 2 has no row length: drop the padding */
		packed = g_malloc((gsize)width * channels * height);
		for (int y = 0; y < height; y++)
			memcpy(packed + (gsize)y * width * channels, pixels + (gsize)y * stride, (gsize)width * channels);
		pixels = packed;
	}
	glGenTextures(1, &p->texture);
	glBindTexture(GL_TEXTURE_2D, p->texture);
	glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
	/* No mipmaps: their level choice jumps at the seam where the longitude wraps (a line down
	 * the photo); no repeat: GLES 2 allows it only for power-of-two sizes, fract() does it */
	glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
	glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
	glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
	glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
	glTexImage2D(GL_TEXTURE_2D, 0, format, width, height, 0, format, GL_UNSIGNED_BYTE, pixels);
	g_free(packed);
	g_object_unref(pixbuf);
	p->loaded = TRUE;
	g_message("360° photo loaded at %d×%d", width, height);
}

/* ---- The effect ---- */

static void *panorama_create(const struct scene *scene)
{
	struct panorama *p = g_new0(struct panorama, 1);
	if (!scene->picture || !*scene->picture) {
		p->failed = TRUE;
		return p;
	}
	struct loader *l = g_new0(struct loader, 1);
	l->refs = 2; /* the effect and the thread */
	l->path = g_strdup(scene->picture);
	g_mutex_init(&l->lock);
	p->loader = l;
	g_thread_unref(g_thread_new("panorama-load", load_thread, l));
	return p;
}

static void panorama_update(void *state, const struct scene *scene, float sx, float sy) {}

static bool panorama_step(void *state, const struct scene *scene, float dt)
{
	struct panorama *p = state;
	if (!p->loaded)
		/* Keep looking for the decoded photo (cheap: an empty frame) */
		return !p->failed;
	mat3 target;
	target_view(p, scene, target);
	p->had_orientation = scene->has_orientation;
	if (!p->have_shown) {
		memcpy(p->shown, target, sizeof(mat3));
		p->have_shown = TRUE;
		return true;
	}
	float before = difference(p->shown, target);
	float k = 1 - expf(-dt / SMOOTHING_S);
	for (int i = 0; i < 3; i++)
		for (int j = 0; j < 3; j++)
			p->shown[i][j] += (target[i][j] - p->shown[i][j]) * k;
	orthonormalize(p->shown);
	return before > REST_EPSILON;
}

static bool panorama_wake(void *state, const struct scene *scene)
{
	struct panorama *p = state;
	if (!p->loaded || scene->has_orientation != p->had_orientation)
		return true;
	mat3 target;
	target_view(p, scene, target);
	return difference(p->shown, target) > REST_EPSILON;
}

static void panorama_build(void *state, const struct scene *scene, struct vertex_buffer *out) {}

static void panorama_render(void *state, const struct scene *scene, int width, int height)
{
	struct panorama *p = state;
	if (!p->loaded && !p->failed)
		upload(p);
	glViewport(0, 0, width, height);
	glDisable(GL_BLEND);
	if (!p->loaded || !p->have_shown) {
		/* Phosh's own background shows until the photo is there */
		glClearColor(0, 0, 0, 0);
		glClear(GL_COLOR_BUFFER_BIT);
		return;
	}
	float half = tanf(FOV_DEG * (float)M_PI / 360);
	float tan_x = width >= height ? half : half * width / height;
	float tan_y = width >= height ? half * height / width : half;
	float columns[9];
	for (int c = 0; c < 3; c++)
		for (int r = 0; r < 3; r++)
			columns[c * 3 + r] = p->shown[r][c];
	glUseProgram(p->program);
	glUniformMatrix3fv(p->u_rotation, 1, GL_FALSE, columns);
	glUniform2f(p->u_tan, tan_x, tan_y);
	glActiveTexture(GL_TEXTURE0);
	glBindTexture(GL_TEXTURE_2D, p->texture);
	glUniform1i(p->u_picture, 0);
	glBindBuffer(GL_ARRAY_BUFFER, p->vbo);
	for (int i = 1; i < 4; i++)
		glDisableVertexAttribArray(i);
	glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 0, NULL);
	glEnableVertexAttribArray(0);
	glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
}

static void panorama_destroy(void *state)
{
	struct panorama *p = state;
	/* The GL context is current whenever an effect is replaced (see plinius-ambient.c) */
	if (p->texture)
		glDeleteTextures(1, &p->texture);
	if (p->vbo)
		glDeleteBuffers(1, &p->vbo);
	if (p->program)
		glDeleteProgram(p->program);
	if (p->loader)
		loader_unref(p->loader);
	g_free(p);
}

const struct effect panorama_effect = {
	.name = "panorama",
	.fps = 60,
	.full_resolution = true,
	.wants_orientation = true,
	.create = panorama_create,
	.update = panorama_update,
	.step = panorama_step,
	.wake = panorama_wake,
	.build = panorama_build,
	.render = panorama_render,
	.destroy = panorama_destroy,
};
