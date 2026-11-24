// SPDX-License-Identifier: MIT
// plinius-ambient: the effects. Sizes and speeds are in logical pixels (360 wide on the Plinius)
// and scale with the screen width; particle counts scale with the screen area.
#include "effects.h"
#include "panorama.h"

#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

/* ---- Helpers ---- */

/* xorshift32: plenty for particles, and no shared state with other code */
static uint32_t rng_state = 0x9e3779b9;

static float rnd(void)
{
	rng_state ^= rng_state << 13;
	rng_state ^= rng_state >> 17;
	rng_state ^= rng_state << 5;
	return (rng_state >> 8) * (1.0f / 16777216.0f);
}

static float rnd_range(float lo, float hi)
{
	return lo + (hi - lo) * rnd();
}

static float clampf(float x, float lo, float hi)
{
	return x < lo ? lo : x > hi ? hi : x;
}

/* Particle count for the density, relative to a 360×800 screen */
static int count_for(const struct scene *s, const int counts[3])
{
	float area = s->width * s->height / (360.0f * 800.0f);
	int n = (int)(counts[s->density] * clampf(area, 0.25f, 4.0f));
	return n > 1 ? n : 1;
}

/* Logical pixels in units of a 360 pixel wide screen */
static float unit(const struct scene *s)
{
	float w = s->width < s->height ? s->width : s->height;
	return w > 0 ? w / 360.0f : 1.0f;
}

static void push(struct vertex_buffer *b, const struct vertex *v)
{
	if (b->len == b->size) {
		b->size = b->size ? b->size * 2 : 1024;
		b->data = realloc(b->data, b->size * sizeof(*b->data));
		if (!b->data)
			abort();
	}
	b->data[b->len++] = *v;
}

/* A quad around (cx, cy), half width hw and half height hh, turned by angle (radians,
 * clockwise on screen); colour premultiplied */
static void quad(struct vertex_buffer *b, float cx, float cy, float hw, float hh, float angle, const float c[4],
		 float p, float q)
{
	static const float corners[6][2] = {{-1, 1}, {-1, -1}, {1, -1}, {-1, 1}, {1, -1}, {1, 1}};
	float cs = cosf(angle), sn = sinf(angle);
	for (int i = 0; i < 6; i++) {
		float u = corners[i][0], v = corners[i][1];
		float lx = u * hw, ly = -v * hh;
		struct vertex vx = {
			cx + lx * cs - ly * sn, cy + lx * sn + ly * cs, u, v, c[0], c[1], c[2], c[3], p, q,
		};
		push(b, &vx);
	}
}

/* Depth of field. Particles have a depth z from 0 (far: small, slow, faint) to 1 (near), sharp
 * up to FOCUS_DEPTH and softer beyond; a few lie between 1 and FOREGROUND_MAX, big and blurred
 * right in front of the lens, and pass by fastest. */
#define FOCUS_DEPTH 0.45f
#define FOREGROUND_MAX 1.7f
#define FOREGROUND_SHARE 0.07f

static float random_depth(void)
{
	if (rnd() < FOREGROUND_SHARE)
		return rnd_range(1.0f, FOREGROUND_MAX);
	float z = rnd();
	return z * z; /* more far particles than near ones */
}

/* 0 sharp .. 1 fully out of focus */
static float blur_of(float z)
{
	return clampf((z - FOCUS_DEPTH) / (FOREGROUND_MAX - FOCUS_DEPTH) * 1.4f, 0, 1);
}

/* Opacity by depth: far ones faint, the blurred foreground ones see-through */
static float depth_alpha(float z)
{
	return z <= 1 ? 0.5f + 0.5f * z : 0.55f - 0.3f * (z - 1) / (FOREGROUND_MAX - 1);
}

/* ---- Shaders ---- */

const char effect_vertex_shader[] =
	"attribute vec2 a_pos;\n"
	"attribute vec2 a_uv;\n"
	"attribute vec4 a_color;\n"
	"attribute vec2 a_param;\n"
	"uniform vec2 u_size;\n"
	"varying vec2 v_uv;\n"
	"varying vec4 v_color;\n"
	"varying vec2 v_param;\n"
	"void main() {\n"
	"  v_uv = a_uv;\n"
	"  v_color = a_color;\n"
	"  v_param = a_param;\n"
	"  vec2 c = a_pos / u_size * 2.0 - 1.0;\n"
	"  gl_Position = vec4(c.x, -c.y, 0.0, 1.0);\n"
	"}\n";

#define FRAGMENT_HEADER                  \
	"precision mediump float;\n"     \
	"varying vec2 v_uv;\n"           \
	"varying vec4 v_color;\n"        \
	"varying vec2 v_param;\n"

/* Snow: p the blur (0 sharp .. 1 a soft, flat disc), q from a soft dot (0) to a six-armed crystal
 * (1); colour not premultiplied, a is the opacity. A faint dark halo keeps sharp flakes visible on
 * light pictures. */
static const char snow_shader[] = FRAGMENT_HEADER
	"void main() {\n"
	"  float r = length(v_uv);\n"
	"  if (r > 1.0) discard;\n"
	"  float a = atan(v_uv.y, v_uv.x);\n"
	/* Folded into one of the six sectors: distance from the arm in its middle */
	"  float k = mod(a, 1.0471976) - 0.5235988;\n"
	"  float arm = smoothstep(0.13, 0.04, r * abs(sin(k))) * smoothstep(0.98, 0.6, r);\n"
	/* Side branches: the arms once more, mirrored around a point half way out */
	"  vec2 m = vec2(r * cos(k) - 0.55, r * abs(sin(k)));\n"
	"  float branch = smoothstep(0.1, 0.03, abs(m.y - abs(m.x) * 1.2)) * smoothstep(0.3, 0.0, length(m));\n"
	"  float dot_ = exp(-r * r * 9.0);\n"
	"  float sharp = mix(dot_, max(max(arm, branch * 0.9), dot_ * 0.9), v_param.y);\n"
	"  float soft = smoothstep(1.0, 0.3, r) * 0.7;\n"
	"  float core = mix(sharp, soft, v_param.x);\n"
	"  float halo = smoothstep(1.0, 0.4, r) * 0.16 * (1.0 - v_param.x);\n"
	"  gl_FragColor = vec4(v_color.rgb * core, core + halo * (1.0 - core)) * v_color.a;\n"
	"}\n";

/* Glow for dust and fireflies: colour premultiplied (rgb above a for light), p the strength of
 * the wide halo, q the sharpness of the core. q < 0: a beam of light along v; p < 0: an out of
 * focus disc (bokeh) with a slightly brighter rim. */
static const char glow_shader[] = FRAGMENT_HEADER
	"void main() {\n"
	"  if (v_param.y < 0.0) {\n"
	"    float across = exp(-v_uv.x * v_uv.x * 2.2) * (1.0 - v_uv.x * v_uv.x);\n"
	"    float along = smoothstep(-1.0, 0.2, v_uv.y);\n"
	"    gl_FragColor = v_color * across * along;\n"
	"    return;\n"
	"  }\n"
	"  float r2 = dot(v_uv, v_uv);\n"
	"  if (r2 > 1.0) discard;\n"
	"  if (v_param.x < 0.0) {\n"
	"    float r = sqrt(r2);\n"
	"    gl_FragColor = v_color * smoothstep(1.0, 0.86, r) * (0.6 + 0.4 * smoothstep(0.45, 0.95, r));\n"
	"    return;\n"
	"  }\n"
	"  float f = exp(-r2 * mix(6.0, 60.0, v_param.y)) + exp(-r2 * 5.0) * v_param.x;\n"
	"  gl_FragColor = v_color * f * (1.0 - r2);\n"
	"}\n";

/* Rain: q 0 a streak (head at v = 1) with p its blur, q 1 a splash ring; colour premultiplied */
static const char rain_shader[] = FRAGMENT_HEADER
	"void main() {\n"
	"  if (v_param.y > 0.5) {\n"
	"    float r = length(v_uv);\n"
	"    gl_FragColor = v_color * smoothstep(0.25, 0.0, abs(r - 0.75));\n"
	"    return;\n"
	"  }\n"
	"  float x2 = v_uv.x * v_uv.x;\n"
	"  float across = exp(-x2 * mix(9.0, 1.6, v_param.x)) * (1.0 - x2);\n"
	"  float along = 0.5 + 0.5 * v_uv.y;\n"
	"  gl_FragColor = v_color * across * along * along;\n"
	"}\n";

/* Balls: p < 0 the soft shadow (a its opacity), else a ball of black lacquer lit by one lamp; rgb
 * the direction to the lamp from the ball's centre (uv axes: x right, y up, z out of the screen),
 * q the width of the antialiased edge in uv units. Blinn-Phong: a little diffuse light on the
 * black, a sharp highlight in a faint sheen, and a thin rim towards the lamp. */
static const char ball_shader[] = FRAGMENT_HEADER
	"void main() {\n"
	"  float r2 = dot(v_uv, v_uv);\n"
	"  if (r2 > 1.0) discard;\n"
	"  if (v_param.x < 0.0) {\n"
	"    float s = 1.0 - r2;\n"
	"    gl_FragColor = vec4(0.0, 0.0, 0.0, s * s * v_color.a);\n"
	"    return;\n"
	"  }\n"
	"  float r = sqrt(r2);\n"
	"  float edge = smoothstep(1.0, 1.0 - v_param.y, r);\n"
	"  vec3 n = vec3(v_uv, sqrt(1.0 - r2));\n"
	"  vec3 l = v_color.rgb;\n"
	"  vec3 h = normalize(l + vec3(0.0, 0.0, 1.0));\n"
	"  float diffuse = max(dot(n, l), 0.0);\n"
	"  float nh = max(dot(n, h), 0.0);\n"
	"  float spec = pow(nh, 110.0) * 0.95 + pow(nh, 10.0) * 0.08;\n"
	"  float rim = pow(1.0 - n.z, 3.0) * (0.03 + 0.16 * diffuse);\n"
	"  vec3 c = vec3(0.015 + 0.13 * diffuse * diffuse + spec + rim);\n"
	"  gl_FragColor = vec4(min(c, 1.0), 1.0) * edge;\n"
	"}\n";

/* Water, first pass: a soft blob, (1 - r²)³, added into the field; r and g of the colour are
 * what it adds at its centre to the coverage and to the height */
static const char water_blob_shader[] = FRAGMENT_HEADER
	"void main() {\n"
	"  float r2 = dot(v_uv, v_uv);\n"
	"  if (r2 > 1.0) discard;\n"
	"  float k = 1.0 - r2;\n"
	"  k = k * k * k;\n"
	"  gl_FragColor = vec4(v_color.rg * k, 0.0, 0.0);\n"
	"}\n";

/* Water, second pass: the surface from the field. Inside the threshold of the coverage it is
 * water; the slope of the field gives its normal (steep at the rim from the coverage, gentle in
 * the body from the height). A thin film: a faint dark tint, darker at the rim where the light is
 * bent away, and highlights towards the lamp at the upper left. Premultiplied, light above the
 * alpha adds rather than covers. */
static const char water_shader[] =
	"precision mediump float;\n"
	"varying vec2 v_uv;\n"
	"uniform sampler2D u_field;\n"
	"uniform vec2 u_texel;\n"
	"void main() {\n"
	"  vec2 f = texture2D(u_field, v_uv).rg;\n"
	"  float cover = smoothstep(0.42, 0.56, f.r);\n"
	"  if (cover <= 0.0) { gl_FragColor = vec4(0.0); return; }\n"
	/* Wide steps: the body's slope from the height, smooth over several blobs */
	"  vec2 dx = vec2(u_texel.x * 3.0, 0.0), dy = vec2(0.0, u_texel.y * 3.0);\n"
	"  vec2 fx = texture2D(u_field, v_uv + dx).rg - texture2D(u_field, v_uv - dx).rg;\n"
	"  vec2 fy = texture2D(u_field, v_uv + dy).rg - texture2D(u_field, v_uv - dy).rg;\n"
	"  vec2 slope = -(vec2(fx.x, fy.x) * 0.9 + vec2(fx.y, fy.y) * 2.5);\n"
	"  vec3 n = normalize(vec3(slope, 0.5));\n"
	"  vec3 l = normalize(vec3(-0.45, 0.6, 0.66));\n"
	"  vec3 h = normalize(l + vec3(0.0, 0.0, 1.0));\n"
	"  float spec = pow(max(dot(n, h), 0.0), 70.0);\n"
	/* The rim: where the field falls off. Towards the lamp it catches light, away from it the
	 * light is bent out of the eye: a thin dark line */
	"  float rim = 1.0 - smoothstep(0.48, 0.72, f.r);\n"
	"  float facing = dot(normalize(slope + vec2(1e-4)), l.xy);\n"
	"  float dark = rim * smoothstep(0.0, -0.6, facing);\n"
	"  float bright = rim * smoothstep(0.2, 0.9, facing);\n"
	"  float alpha = cover * (0.035 + 0.30 * dark);\n"
	"  vec3 light = vec3(spec * 0.75 + bright * 0.22);\n"
	"  gl_FragColor = vec4(vec3(0.0, 0.02, 0.04) * alpha + light * cover, alpha);\n"
	"}\n";

/* ---- Snow ---- */

struct flake {
	float x, y;
	float z;      /* depth, see random_depth() */
	float phase;  /* of the sideways sway */
	float sway;   /* px/s */
	float angle, spin;
};

struct snow {
	int n;
	float time;
	struct flake *flakes;
};

static void snow_place(struct flake *f, const struct scene *s, bool anywhere)
{
	f->z = random_depth();
	f->x = rnd() * s->width;
	f->y = anywhere ? rnd() * s->height : -60 * unit(s);
	f->phase = rnd() * 2 * M_PI;
	f->sway = rnd_range(4, 14) * (0.5f + f->z) * unit(s);
	f->angle = rnd() * 2 * M_PI;
	f->spin = rnd_range(-0.8f, 0.8f);
}

static void *snow_create(const struct scene *s)
{
	static const int counts[3] = {60, 120, 220};
	struct snow *st = calloc(1, sizeof(*st));
	st->n = count_for(s, counts);
	st->flakes = calloc(st->n, sizeof(*st->flakes));
	for (int i = 0; i < st->n; i++)
		snow_place(&st->flakes[i], s, true);
	return st;
}

/* The flake itself; out of focus it spreads over a bigger quad */
static float flake_radius(const struct flake *f, const struct scene *s)
{
	return (1.4f + 8.5f * f->z * f->z) * unit(s);
}

static float flake_extent(const struct flake *f, const struct scene *s)
{
	return flake_radius(f, s) * (1 + 0.8f * blur_of(f->z));
}

/* Stretched along: after a rotation the flakes cover the new shape as before (cut off by the
 * new size, the wider half would stay empty) */
static void snow_update(void *state, const struct scene *s, float sx, float sy)
{
	struct snow *st = state;
	for (int i = 0; i < st->n; i++) {
		st->flakes[i].x *= sx;
		st->flakes[i].y *= sy;
	}
}

static bool snow_step(void *state, const struct scene *s, float dt)
{
	struct snow *st = state;
	float u = unit(s);
	st->time += dt;
	/* Gusts: a slow wind that comes and goes, stronger for near flakes (parallax) */
	float wind = (sinf(st->time * 0.13f) * 9 + sinf(st->time * 0.37f + 1) * 4) * u;
	for (int i = 0; i < st->n; i++) {
		struct flake *f = &st->flakes[i];
		float r = flake_extent(f, s);
		f->y += (16 + 48 * f->z) * u * dt;
		f->x += (cosf(st->time * (0.5f + 0.6f * f->z) + f->phase) * f->sway + wind * (0.4f + f->z)) * dt;
		f->angle += f->spin * dt;
		if (f->y - r > s->height)
			snow_place(f, s, false);
		if (f->x < -r)
			f->x += s->width + 2 * r;
		else if (f->x > s->width + r)
			f->x -= s->width + 2 * r;
	}
	return true;
}

static void snow_build(void *state, const struct scene *s, struct vertex_buffer *out)
{
	struct snow *st = state;
	for (int i = 0; i < st->n; i++) {
		struct flake *f = &st->flakes[i];
		float blur = blur_of(f->z), r = flake_extent(f, s);
		float c[4] = {1.0f, 1.0f, 1.0f, depth_alpha(f->z)};
		/* Only sharp flakes big enough on screen show their crystal */
		float crystal = clampf((flake_radius(f, s) * s->scale - 9) / 8, 0, 1) * (1 - blur);
		quad(out, f->x, f->y, r, r, f->angle, c, blur, crystal);
	}
}

static void snow_destroy(void *state)
{
	struct snow *st = state;
	free(st->flakes);
	free(st);
}

/* ---- Dust in a beam of light ---- */

struct mote {
	float x, y, vx, vy;
	float z;    /* depth, see random_depth() */
	float size; /* radius of the mote; in the foreground of its out of focus disc */
	float phase, freq; /* twinkle */
};

struct dust {
	int n;
	float time;
	struct mote *motes;
};

/* The beam comes in from the upper left: a line through (0.2 w, 0) along this direction */
#define BEAM_DIR_X 0.42f
#define BEAM_DIR_Y 0.91f

static float beam_distance(float x, float y, const struct scene *s)
{
	float dx = x - 0.2f * s->width, dy = y;
	return fabsf(dx * BEAM_DIR_Y - dy * BEAM_DIR_X);
}

static void *dust_create(const struct scene *s)
{
	static const int counts[3] = {70, 140, 240};
	struct dust *st = calloc(1, sizeof(*st));
	st->n = count_for(s, counts);
	st->motes = calloc(st->n, sizeof(*st->motes));
	for (int i = 0; i < st->n; i++) {
		struct mote *m = &st->motes[i];
		m->x = rnd() * s->width;
		m->y = rnd() * s->height;
		m->z = random_depth();
		m->size = (m->z <= 1 ? 0.8f + 2.2f * m->z : 5 + 9 * (m->z - 1) / (FOREGROUND_MAX - 1)) * unit(s);
		m->phase = rnd() * 2 * M_PI;
		m->freq = rnd_range(0.6f, 2.0f);
	}
	return st;
}

static void dust_update(void *state, const struct scene *s, float sx, float sy)
{
	struct dust *st = state;
	for (int i = 0; i < st->n; i++) {
		st->motes[i].x *= sx;
		st->motes[i].y *= sy;
	}
}

static bool dust_step(void *state, const struct scene *s, float dt)
{
	struct dust *st = state;
	float u = unit(s);
	st->time += dt;
	for (int i = 0; i < st->n; i++) {
		struct mote *m = &st->motes[i];
		/* Brownian drift with a little warm air rising, damped so it stays a float; nearer
		 * motes seem to move faster (parallax) */
		float near = 0.5f + m->z;
		m->vx += rnd_range(-1, 1) * 18 * near * u * dt;
		m->vy += (rnd_range(-1, 1) * 18 - 1.5f) * near * u * dt;
		float damp = expf(-0.8f * dt);
		m->vx *= damp;
		m->vy *= damp;
		m->x += (m->vx + 2 * near * u) * dt;
		m->y += m->vy * dt;
		if (m->x < 0)
			m->x += s->width;
		else if (m->x > s->width)
			m->x -= s->width;
		if (m->y < 0)
			m->y += s->height;
		else if (m->y > s->height)
			m->y -= s->height;
	}
	return true;
}

static void dust_build(void *state, const struct scene *s, struct vertex_buffer *out)
{
	struct dust *st = state;
	float beam_half = 0.32f * s->width;
	/* The beam itself: a long, faint, warm band */
	float len = hypotf(s->width, s->height);
	float breathe = 0.85f + 0.15f * sinf(st->time * 0.25f);
	float beam[4] = {0.17f * breathe, 0.145f * breathe, 0.10f * breathe, 0.0f};
	quad(out, 0.2f * s->width + BEAM_DIR_X * len * 0.5f, BEAM_DIR_Y * len * 0.5f, beam_half, len * 0.55f,
	     atan2f(BEAM_DIR_X, -BEAM_DIR_Y) + (float)M_PI, beam, 0, -1);
	for (int i = 0; i < st->n; i++) {
		struct mote *m = &st->motes[i];
		/* Lit inside the beam, barely visible outside of it */
		float lit = 0.18f + 0.82f * clampf(1.2f - beam_distance(m->x, m->y, s) / beam_half, 0, 1);
		float b = lit * (0.55f + 0.45f * sinf(st->time * m->freq + m->phase));
		if (m->z > 1) {
			/* In front of the lens: a faint disc of light */
			b *= 0.3f;
			float c[4] = {1.0f * b, 0.93f * b, 0.78f * b, 0.5f * b};
			quad(out, m->x, m->y, m->size, m->size, 0, c, -1, 0);
			continue;
		}
		/* Behind the focus sharp points, nearer ones softer */
		float c[4] = {1.0f * b, 0.93f * b, 0.78f * b, 0.55f * b};
		quad(out, m->x, m->y, m->size * 3, m->size * 3, 0, c, 0.25f, 0.6f - 0.5f * blur_of(m->z));
	}
}

static void dust_destroy(void *state)
{
	struct dust *st = state;
	free(st->motes);
	free(st);
}

/* ---- Fireflies ---- */

struct fly {
	float x, y;
	float heading, turn, speed; /* radians, radians/s, px/s */
	float phase, period;        /* of the flashing */
};

struct fireflies {
	int n;
	float time;
	struct fly *flies;
};

static void *fireflies_create(const struct scene *s)
{
	static const int counts[3] = {14, 24, 40};
	struct fireflies *st = calloc(1, sizeof(*st));
	st->n = count_for(s, counts);
	st->flies = calloc(st->n, sizeof(*st->flies));
	for (int i = 0; i < st->n; i++) {
		struct fly *f = &st->flies[i];
		f->x = rnd() * s->width;
		f->y = rnd_range(0.25f, 1.0f) * s->height;
		f->heading = rnd() * 2 * M_PI;
		f->speed = rnd_range(10, 26) * unit(s);
		f->phase = rnd() * 2 * M_PI;
		f->period = rnd_range(2.5f, 5.0f);
	}
	return st;
}

static void fireflies_update(void *state, const struct scene *s, float sx, float sy)
{
	struct fireflies *st = state;
	for (int i = 0; i < st->n; i++) {
		st->flies[i].x = clampf(st->flies[i].x * sx, 0, s->width);
		st->flies[i].y = clampf(st->flies[i].y * sy, 0, s->height);
	}
}

static bool fireflies_step(void *state, const struct scene *s, float dt)
{
	struct fireflies *st = state;
	float margin = 30 * unit(s);
	st->time += dt;
	for (int i = 0; i < st->n; i++) {
		struct fly *f = &st->flies[i];
		/* Wandering: the rate of turning itself drifts at random */
		f->turn = clampf(f->turn + rnd_range(-1, 1) * 4 * dt, -1.6f, 1.6f);
		f->heading += f->turn * dt;
		/* Near an edge, turn back towards the middle (they prefer the lower part) */
		if (f->x < margin || f->x > s->width - margin || f->y < 0.15f * s->height ||
		    f->y > s->height - margin) {
			float want = atan2f(0.6f * s->height - f->y, 0.5f * s->width - f->x);
			float diff = remainderf(want - f->heading, 2 * (float)M_PI);
			f->heading += clampf(diff, -2 * dt, 2 * dt);
		}
		f->x += cosf(f->heading) * f->speed * dt;
		f->y += sinf(f->heading) * f->speed * dt;
	}
	return true;
}

static void fireflies_build(void *state, const struct scene *s, struct vertex_buffer *out)
{
	struct fireflies *st = state;
	float r = 22 * unit(s);
	for (int i = 0; i < st->n; i++) {
		struct fly *f = &st->flies[i];
		/* Mostly dim, flashing up now and then */
		float pulse = powf(fmaxf(0, sinf(st->time * 2 * (float)M_PI / f->period + f->phase)), 4);
		float b = 0.08f + 0.92f * pulse;
		float c[4] = {0.80f * b * 1.3f, 1.0f * b * 1.3f, 0.35f * b * 1.3f, 0.35f * b};
		quad(out, f->x, f->y, r, r, 0, c, 0.45f, 1.0f);
	}
}

static void fireflies_destroy(void *state)
{
	struct fireflies *st = state;
	free(st->flies);
	free(st);
}

/* ---- Rain ---- */

struct drop {
	float x, y; /* head */
	float z;    /* depth, see random_depth() */
	float speed, len, width, land;
};

struct splash {
	float x, y, age;
};

#define SPLASHES 64
#define SPLASH_TIME 0.32f
#define RAIN_SLANT 0.12f

struct rain {
	int n;
	struct drop *drops;
	struct splash splashes[SPLASHES];
	int next_splash;
};

static void drop_place(struct drop *d, const struct scene *s, bool anywhere)
{
	float u = unit(s), z = random_depth();
	d->z = z;
	d->speed = (520 + 420 * z) * u;
	d->len = (12 + 22 * z) * u;
	d->width = (0.9f + 1.8f * z) * u;
	d->x = rnd_range(-0.1f, 1.0f) * s->width;
	d->y = anywhere ? rnd() * s->height : -rnd() * 0.3f * s->height - d->len;
	/* Near drops land low on the screen, far ones higher up; some fall past the bottom, and the
	 * ones in front of the lens always do */
	d->land = z <= 1 ? s->height * (0.55f + 0.5f * z) : s->height + d->len;
}

static void *rain_create(const struct scene *s)
{
	static const int counts[3] = {70, 140, 240};
	struct rain *st = calloc(1, sizeof(*st));
	st->n = count_for(s, counts);
	st->drops = calloc(st->n, sizeof(*st->drops));
	for (int i = 0; i < st->n; i++)
		drop_place(&st->drops[i], s, true);
	for (int i = 0; i < SPLASHES; i++)
		st->splashes[i].age = SPLASH_TIME;
	return st;
}

static void rain_update(void *state, const struct scene *s, float sx, float sy)
{
	struct rain *st = state;
	for (int i = 0; i < st->n; i++)
		drop_place(&st->drops[i], s, true);
}

static bool rain_step(void *state, const struct scene *s, float dt)
{
	struct rain *st = state;
	for (int i = 0; i < SPLASHES; i++)
		st->splashes[i].age += dt;
	for (int i = 0; i < st->n; i++) {
		struct drop *d = &st->drops[i];
		d->y += d->speed * dt;
		d->x += d->speed * RAIN_SLANT * dt;
		if (d->y < d->land)
			continue;
		if (d->land < s->height) {
			struct splash *sp = &st->splashes[st->next_splash];
			st->next_splash = (st->next_splash + 1) % SPLASHES;
			*sp = (struct splash){d->x, d->land, 0};
		}
		drop_place(d, s, false);
	}
	return true;
}

static void rain_build(void *state, const struct scene *s, struct vertex_buffer *out)
{
	struct rain *st = state;
	float u = unit(s);
	float angle = atan2f(RAIN_SLANT, -1.0f);
	for (int i = 0; i < st->n; i++) {
		struct drop *d = &st->drops[i];
		float blur = blur_of(d->z), a = 0.42f * depth_alpha(d->z);
		float c[4] = {0.82f * a, 0.88f * a, 1.0f * a, a};
		/* The quad's centre half a length behind the head; out of focus a wider, softer streak */
		float cx = d->x - RAIN_SLANT * d->len * 0.5f, cy = d->y - d->len * 0.5f;
		quad(out, cx, cy, d->width * (1 + 1.5f * blur), d->len * 0.5f, angle, c, blur, 0);
	}
	for (int i = 0; i < SPLASHES; i++) {
		struct splash *sp = &st->splashes[i];
		if (sp->age >= SPLASH_TIME)
			continue;
		float t = sp->age / SPLASH_TIME, r = (2 + 8 * t) * u, a = 0.35f * (1 - t);
		float c[4] = {0.85f * a, 0.9f * a, 1.0f * a, a};
		quad(out, sp->x, sp->y, r, r * 0.35f, 0, c, 0, 1);
	}
}

static void rain_destroy(void *state)
{
	struct rain *st = state;
	free(st->drops);
	free(st);
}

/* ---- Balls ---- */

struct ball {
	float x, y, vx, vy;
	float r, mass;
};

/* Pixels per metre: slower than a real marble on a phone-sized tray, which crosses the screen
 * in a fifth of a second */
#define PX_PER_M 1100.0f
/* A rolling solid ball accelerates at 5/7 of the slope's pull */
#define ROLLING (5.0f / 7.0f)
#define ROLL_DAMPING 0.35f /* 1/s */
#define WALL_BOUNCE 0.45f
#define BALL_BOUNCE 0.75f
#define MAX_SUBSTEP (1.0f / 240)
/* At rest: every ball slower than this (px/s) for REST_TIME; woken by a change of the tilt */
#define REST_SPEED 6.0f
#define REST_TIME 0.6f
#define WAKE_GRAVITY 0.35f /* m/s² */

struct balls {
	int n;
	struct ball *balls;
	float still_for;
	bool resting;
	float rest_gravity[2];
};

static void *balls_create(const struct scene *s)
{
	static const int counts[3] = {6, 10, 16};
	struct balls *st = calloc(1, sizeof(*st));
	float u = unit(s);
	st->n = counts[s->density];
	st->balls = calloc(st->n, sizeof(*st->balls));
	for (int i = 0; i < st->n; i++) {
		struct ball *b = &st->balls[i];
		b->r = rnd_range(17, 30) * u;
		b->mass = b->r * b->r;
		/* Somewhere free if possible */
		for (int tries = 0; tries < 50; tries++) {
			b->x = rnd_range(b->r, s->width - b->r);
			b->y = rnd_range(b->r, s->height * 0.6f);
			bool free_spot = true;
			for (int j = 0; j < i; j++)
				if (hypotf(b->x - st->balls[j].x, b->y - st->balls[j].y) < b->r + st->balls[j].r)
					free_spot = false;
			if (free_spot)
				break;
		}
	}
	return st;
}

static void balls_update(void *state, const struct scene *s, float sx, float sy)
{
	struct balls *st = state;
	for (int i = 0; i < st->n; i++) {
		struct ball *b = &st->balls[i];
		b->x = clampf(b->x * sx, b->r, s->width - b->r);
		b->y = clampf(b->y * sy, b->r, s->height - b->r);
	}
	st->resting = false;
	st->still_for = 0;
}

static void balls_substep(struct balls *st, const struct scene *s, float h)
{
	float ax = s->gravity[0] * PX_PER_M * ROLLING, ay = s->gravity[1] * PX_PER_M * ROLLING;
	float damp = expf(-ROLL_DAMPING * h);
	for (int i = 0; i < st->n; i++) {
		struct ball *b = &st->balls[i];
		b->vx = (b->vx + ax * h) * damp;
		b->vy = (b->vy + ay * h) * damp;
		b->x += b->vx * h;
		b->y += b->vy * h;
		if (b->x < b->r) {
			b->x = b->r;
			b->vx = fabsf(b->vx) * WALL_BOUNCE;
		} else if (b->x > s->width - b->r) {
			b->x = s->width - b->r;
			b->vx = -fabsf(b->vx) * WALL_BOUNCE;
		}
		if (b->y < b->r) {
			b->y = b->r;
			b->vy = fabsf(b->vy) * WALL_BOUNCE;
		} else if (b->y > s->height - b->r) {
			b->y = s->height - b->r;
			b->vy = -fabsf(b->vy) * WALL_BOUNCE;
		}
	}
	/* Collisions: push overlapping balls apart (by mass) and exchange momentum along the line
	 * between their centres; twice, so stacks settle */
	for (int pass = 0; pass < 2; pass++)
		for (int i = 0; i < st->n; i++)
			for (int j = i + 1; j < st->n; j++) {
				struct ball *a = &st->balls[i], *b = &st->balls[j];
				float dx = b->x - a->x, dy = b->y - a->y, d = hypotf(dx, dy), min = a->r + b->r;
				if (d >= min)
					continue;
				float nx = d > 1e-4f ? dx / d : 1, ny = d > 1e-4f ? dy / d : 0;
				float overlap = min - d, total = a->mass + b->mass;
				a->x -= nx * overlap * b->mass / total;
				a->y -= ny * overlap * b->mass / total;
				b->x += nx * overlap * a->mass / total;
				b->y += ny * overlap * a->mass / total;
				float vn = (b->vx - a->vx) * nx + (b->vy - a->vy) * ny;
				if (vn >= 0)
					continue;
				float impulse = -(1 + BALL_BOUNCE) * vn / (1 / a->mass + 1 / b->mass);
				a->vx -= impulse / a->mass * nx;
				a->vy -= impulse / a->mass * ny;
				b->vx += impulse / b->mass * nx;
				b->vy += impulse / b->mass * ny;
			}
}

static bool balls_step(void *state, const struct scene *s, float dt)
{
	struct balls *st = state;
	if (st->resting)
		return false;
	int steps = (int)ceilf(dt / MAX_SUBSTEP);
	for (int i = 0; i < steps; i++)
		balls_substep(st, s, dt / steps);
	float fastest = 0;
	for (int i = 0; i < st->n; i++)
		fastest = fmaxf(fastest, hypotf(st->balls[i].vx, st->balls[i].vy));
	st->still_for = fastest < REST_SPEED * unit(s) ? st->still_for + dt : 0;
	if (st->still_for >= REST_TIME) {
		st->resting = true;
		memcpy(st->rest_gravity, s->gravity, sizeof(s->gravity));
		for (int i = 0; i < st->n; i++)
			st->balls[i].vx = st->balls[i].vy = 0;
	}
	return true; /* the frame of coming to rest is still drawn */
}

static bool balls_wake(void *state, const struct scene *s)
{
	struct balls *st = state;
	if (!st->resting)
		return true;
	if (hypotf(s->gravity[0] - st->rest_gravity[0], s->gravity[1] - st->rest_gravity[1]) < WAKE_GRAVITY)
		return false;
	st->resting = false;
	st->still_for = 0;
	return true;
}

/* The one lamp: above the screen plane, beyond its upper left corner (relative to the screen size) */
#define LAMP_X 0.15f
#define LAMP_Y -0.15f
#define LAMP_HEIGHT 0.35f

static void balls_build(void *state, const struct scene *s, struct vertex_buffer *out)
{
	struct balls *st = state;
	float lamp_x = LAMP_X * s->width, lamp_y = LAMP_Y * s->height, lamp_z = LAMP_HEIGHT * s->height;
	/* All shadows first, cast away from the lamp, then the balls over them */
	for (int i = 0; i < st->n; i++) {
		struct ball *b = &st->balls[i];
		float dx = b->x - lamp_x, dy = b->y - lamp_y, d = hypotf(dx, dy);
		/* The further from the lamp, the longer the shadow, up to half a radius */
		float shift = fminf(d * b->r / lamp_z * 0.5f, 0.5f * b->r) / fmaxf(d, 1);
		float c[4] = {0, 0, 0, 0.32f};
		quad(out, b->x + dx * shift, b->y + dy * shift, b->r * 1.12f, b->r * 1.12f, 0, c, -1, 0);
	}
	for (int i = 0; i < st->n; i++) {
		struct ball *b = &st->balls[i];
		/* Towards the lamp from the ball's centre, which lies one radius above the screen;
		 * normalized here, its length squared would overflow mediump in the shader */
		float lx = lamp_x - b->x, ly = b->y - lamp_y, lz = lamp_z - b->r, len = sqrtf(lx * lx + ly * ly + lz * lz);
		float c[4] = {lx / len, ly / len, lz / len, 1};
		quad(out, b->x, b->y, b->r, b->r, 0, c, 1, 1.5f / (b->r * s->scale));
	}
}

static void balls_destroy(void *state)
{
	struct balls *st = state;
	free(st->balls);
	free(st);
}

/* ---- Water ----
 *
 * A little water on the glass: a few hundred particles that hold together and keep their
 * distance (double density relaxation, Clavet et al. 2005, "Particle-based viscoelastic fluid
 * simulation"), in a thin pool along the edge gravity points to. Tilting the phone lets it run
 * to the other side; it sticks to the glass below a small slope, so a phone lying (nearly) flat
 * keeps it where it is. Moving the phone makes it slosh against the movement.
 *
 * Drawn in two passes: every particle adds a soft blob to a field (red: coverage, green: a
 * gentler height for the shading), and the composite shader turns the field into one surface
 * with a darker rim, a slight tint and highlights from a lamp at the upper left. */

/* Interaction radius (px on a 360 wide screen) */
#define WATER_RADIUS 11.0f
/* Relaxation in units of the radius per substep: rest density, stiffness, near stiffness */
#define WATER_REST_DENSITY 3.0f
#define WATER_STIFFNESS 0.06f
#define WATER_STIFFNESS_NEAR 0.12f
/* Viscosity (linear and quadratic, per px/s of approaching speed) */
#define WATER_VISCOSITY 0.04f
#define WATER_VISCOSITY_QUAD 0.0005f
/* Substeps: at least WATER_MIN_RATE a second, more while the water is fast, so that no particle
 * moves more than WATER_MAX_MOVE radii in one; at most WATER_MAX_SUBSTEPS per frame. Below 200 a
 * second this thin water bursts apart now and then for no reason (measured in the preview). On
 * the Plinius about 1 ms per substep for 264 particles, the bulk of the cost; mostly in the loops
 * over the pairs, not in finding them. */
#define WATER_MIN_RATE 200.0f
#define WATER_MAX_MOVE 0.4f
#define WATER_MAX_SUBSTEPS 8
/* Pull of 1 g in the screen plane, px/s² on a 360 wide screen: water on glass runs slower than
 * a free fall, but crosses a tilted screen in about a third of a second */
#define WATER_GRAVITY 2800.0f
/* Below this slope (in-plane gravity, m/s²) the water sticks to the glass */
#define WATER_STICK 1.3f
#define WATER_DAMPING 0.4f /* 1/s, friction on the glass */
/* Sloshing against the phone's change of speed: px/s per m/s */
#define WATER_INERTIA 300.0f
/* The blob of one particle: radius in units of the interaction radius, and what it adds to the
 * field (one alone is just about visible as a droplet) */
#define WATER_BLOB 1.35f
#define WATER_COVER 0.55f
#define WATER_HEIGHT 0.06f
/* At rest: every particle slower than this (px/s) for WATER_REST_TIME */
#define WATER_REST_SPEED 4.0f
#define WATER_REST_TIME 0.8f
#define WATER_WAKE_GRAVITY 0.3f /* m/s² */
#define WATER_WAKE_SHAKE 0.8f   /* m²/s⁴ */

struct water_particle {
	float x, y, px, py, vx, vy;
};

struct water_pair {
	int i, j;
	float q, nx, ny; /* distance in units of the radius, direction from i to j */
};

struct water {
	int n;
	struct water_particle *p;
	struct water_pair *pairs;
	int n_pairs, max_pairs;
	int *cell_head, *next;
	int cols, rows;
	float *density, *density_near;
	float still_for;
	bool resting;
	float rest_gravity[2];
};

static float water_h(const struct scene *s)
{
	return WATER_RADIUS * unit(s);
}

/* A pool along the lower edge, as if it had been lying there */
static void water_place(struct water *st, const struct scene *s)
{
	float h = water_h(s), spacing = 0.5f * h;
	int per_row = (int)((s->width - h) / spacing);
	if (per_row < 1)
		per_row = 1;
	for (int i = 0; i < st->n; i++) {
		struct water_particle *p = &st->p[i];
		p->x = 0.5f * h + (i % per_row + rnd_range(-0.1f, 0.1f)) * spacing;
		p->y = s->height - 0.5f * h - (i / per_row) * spacing * 0.87f;
		p->px = p->x;
		p->py = p->y;
		p->vx = p->vy = 0;
	}
}

static void water_grid(struct water *st, const struct scene *s)
{
	float h = water_h(s);
	int cols = (int)(s->width / h) + 1, rows = (int)(s->height / h) + 1;
	if (cols != st->cols || rows != st->rows) {
		free(st->cell_head);
		st->cols = cols;
		st->rows = rows;
		st->cell_head = malloc(sizeof(int) * cols * rows);
	}
}

static void *water_create(const struct scene *s)
{
	static const int counts[3] = {200, 360, 560};
	struct water *st = calloc(1, sizeof(*st));
	/* Scales with the width: the pool keeps its depth on a wider screen */
	st->n = (int)(counts[s->density] * clampf(s->width / 360.0f, 0.5f, 3.0f));
	st->p = calloc(st->n, sizeof(*st->p));
	st->next = calloc(st->n, sizeof(int));
	st->density = calloc(st->n, sizeof(float));
	st->density_near = calloc(st->n, sizeof(float));
	st->max_pairs = st->n * 24;
	st->pairs = calloc(st->max_pairs, sizeof(*st->pairs));
	water_place(st, s);
	return st;
}

static void water_update(void *state, const struct scene *s, float sx, float sy)
{
	struct water *st = state;
	for (int i = 0; i < st->n; i++) {
		struct water_particle *p = &st->p[i];
		p->x *= sx;
		p->y *= sy;
		p->px = p->x;
		p->py = p->y;
	}
	st->resting = false;
	st->still_for = 0;
}

/* All pairs closer than the radius, through a grid of cells one radius wide */
static void water_find_pairs(struct water *st, const struct scene *s)
{
	float h = water_h(s);
	water_grid(st, s);
	for (int c = 0; c < st->cols * st->rows; c++)
		st->cell_head[c] = -1;
	for (int i = 0; i < st->n; i++) {
		int cx = (int)clampf(st->p[i].x / h, 0, st->cols - 1), cy = (int)clampf(st->p[i].y / h, 0, st->rows - 1);
		st->next[i] = st->cell_head[cy * st->cols + cx];
		st->cell_head[cy * st->cols + cx] = i;
	}
	st->n_pairs = 0;
	for (int i = 0; i < st->n; i++) {
		struct water_particle *a = &st->p[i];
		int cx = (int)clampf(a->x / h, 0, st->cols - 1), cy = (int)clampf(a->y / h, 0, st->rows - 1);
		for (int y = cy - 1; y <= cy + 1; y++) {
			if (y < 0 || y >= st->rows)
				continue;
			for (int x = cx - 1; x <= cx + 1; x++) {
				if (x < 0 || x >= st->cols)
					continue;
				for (int j = st->cell_head[y * st->cols + x]; j >= 0; j = st->next[j]) {
					if (j <= i)
						continue;
					float dx = st->p[j].x - a->x, dy = st->p[j].y - a->y, d2 = dx * dx + dy * dy;
					if (d2 >= h * h || st->n_pairs == st->max_pairs)
						continue;
					float d = sqrtf(d2);
					struct water_pair *pr = &st->pairs[st->n_pairs++];
					pr->i = i;
					pr->j = j;
					pr->q = d / h;
					/* Two in the same spot: push apart in some direction */
					pr->nx = d > 1e-4f ? dx / d : 1;
					pr->ny = d > 1e-4f ? dy / d : 0;
				}
			}
		}
	}
}

static void water_substep(struct water *st, const struct scene *s, float dt, float gx, float gy)
{
	float h = water_h(s);
	float damp = expf(-WATER_DAMPING * dt);
	for (int i = 0; i < st->n; i++) {
		struct water_particle *p = &st->p[i];
		p->vx = (p->vx + gx * dt) * damp;
		p->vy = (p->vy + gy * dt) * damp;
		p->px = p->x;
		p->py = p->y;
		p->x += p->vx * dt;
		p->y += p->vy * dt;
	}

	/* Double density relaxation: too dense pushes apart, too thin pulls together (the near
	 * density only pushes, so particles never clump into one spot) */
	water_find_pairs(st, s);
	memset(st->density, 0, sizeof(float) * st->n);
	memset(st->density_near, 0, sizeof(float) * st->n);
	for (int k = 0; k < st->n_pairs; k++) {
		struct water_pair *pr = &st->pairs[k];
		float w = 1 - pr->q, w2 = w * w, w3 = w2 * w;
		st->density[pr->i] += w2;
		st->density[pr->j] += w2;
		st->density_near[pr->i] += w3;
		st->density_near[pr->j] += w3;
	}
	for (int k = 0; k < st->n_pairs; k++) {
		struct water_pair *pr = &st->pairs[k];
		float w = 1 - pr->q;
		float pressure = WATER_STIFFNESS * (st->density[pr->i] + st->density[pr->j] - 2 * WATER_REST_DENSITY) * 0.5f;
		float near = WATER_STIFFNESS_NEAR * (st->density_near[pr->i] + st->density_near[pr->j]) * 0.5f;
		float d = (pressure * w + near * w * w) * h * 0.5f;
		st->p[pr->i].x -= pr->nx * d;
		st->p[pr->i].y -= pr->ny * d;
		st->p[pr->j].x += pr->nx * d;
		st->p[pr->j].y += pr->ny * d;
	}

	/* The edges of the screen hold the water in */
	float m = 0.3f * h;
	for (int i = 0; i < st->n; i++) {
		struct water_particle *p = &st->p[i];
		p->x = clampf(p->x, m, s->width - m);
		p->y = clampf(p->y, m, s->height - m);
		p->vx = (p->x - p->px) / dt;
		p->vy = (p->y - p->py) / dt;
	}

	/* Viscosity: neighbours that approach each other share their speed */
	for (int k = 0; k < st->n_pairs; k++) {
		struct water_pair *pr = &st->pairs[k];
		struct water_particle *a = &st->p[pr->i], *b = &st->p[pr->j];
		float in = (a->vx - b->vx) * pr->nx + (a->vy - b->vy) * pr->ny;
		if (in <= 0)
			continue;
		float impulse = fminf((1 - pr->q) * (WATER_VISCOSITY * in + WATER_VISCOSITY_QUAD * in * in), in) * 0.5f;
		a->vx -= pr->nx * impulse;
		a->vy -= pr->ny * impulse;
		b->vx += pr->nx * impulse;
		b->vy += pr->ny * impulse;
	}
}

static bool water_step(void *state, const struct scene *s, float dt)
{
	struct water *st = state;
	if (st->resting)
		return false;
	/* No time passed (a frame in the same microsecond as the last step, which draw() allows):
	 * the velocities come from the position change divided by the step */
	if (dt <= 0)
		return true;
	float u = unit(s);
	/* Only the slope beyond what the glass holds pulls the water */
	float g = hypotf(s->gravity[0], s->gravity[1]);
	float pull = g > WATER_STICK ? (g - WATER_STICK) / g : 0;
	float gx = s->gravity[0] / 9.81f * WATER_GRAVITY * u * pull, gy = s->gravity[1] / 9.81f * WATER_GRAVITY * u * pull;
	/* The water stays where it was while the phone moves under it */
	for (int i = 0; i < st->n; i++) {
		st->p[i].vx -= s->impulse[0] * WATER_INERTIA * u;
		st->p[i].vy -= s->impulse[1] * WATER_INERTIA * u;
	}
	float fastest = 0;
	for (int i = 0; i < st->n; i++)
		fastest = fmaxf(fastest, hypotf(st->p[i].vx, st->p[i].vy));
	int steps = (int)ceilf(fmaxf(dt * WATER_MIN_RATE, dt * fastest / (WATER_MAX_MOVE * water_h(s))) - 0.01f);
	steps = steps < 1 ? 1 : steps > WATER_MAX_SUBSTEPS ? WATER_MAX_SUBSTEPS : steps;
	for (int i = 0; i < steps; i++)
		water_substep(st, s, dt / steps, gx, gy);
	fastest = 0;
	for (int i = 0; i < st->n; i++)
		fastest = fmaxf(fastest, hypotf(st->p[i].vx, st->p[i].vy));
	st->still_for = fastest < WATER_REST_SPEED * u ? st->still_for + dt : 0;
	if (st->still_for >= WATER_REST_TIME) {
		st->resting = true;
		memcpy(st->rest_gravity, s->gravity, sizeof(s->gravity));
		for (int i = 0; i < st->n; i++)
			st->p[i].vx = st->p[i].vy = 0;
	}
	return true;
}

static bool water_wake(void *state, const struct scene *s)
{
	struct water *st = state;
	if (!st->resting)
		return true;
	if (s->shake < WATER_WAKE_SHAKE &&
	    hypotf(s->gravity[0] - st->rest_gravity[0], s->gravity[1] - st->rest_gravity[1]) < WATER_WAKE_GRAVITY)
		return false;
	st->resting = false;
	st->still_for = 0;
	return true;
}

static void water_build(void *state, const struct scene *s, struct vertex_buffer *out)
{
	struct water *st = state;
	float r = WATER_BLOB * water_h(s);
	float c[4] = {WATER_COVER, WATER_HEIGHT, 0, 0};
	for (int i = 0; i < st->n; i++)
		quad(out, st->p[i].x, st->p[i].y, r, r, 0, c, 0, 0);
}

static void water_destroy(void *state)
{
	struct water *st = state;
	free(st->p);
	free(st->pairs);
	free(st->cell_head);
	free(st->next);
	free(st->density);
	free(st->density_near);
	free(st);
}

/* ---- Registry ---- */

static const struct effect effects[] = {
	{"snow", 30, false, false, false, snow_shader, NULL, snow_create, snow_update, snow_step, NULL, snow_build,
	 snow_destroy, false, NULL, NULL},
	{"dust", 30, false, false, false, glow_shader, NULL, dust_create, dust_update, dust_step, NULL, dust_build,
	 dust_destroy, false, NULL, NULL},
	{"fireflies", 30, false, false, false, glow_shader, NULL, fireflies_create, fireflies_update, fireflies_step, NULL,
	 fireflies_build, fireflies_destroy, false, NULL, NULL},
	{"rain", 40, false, false, false, rain_shader, NULL, rain_create, rain_update, rain_step, NULL, rain_build,
	 rain_destroy, false, NULL, NULL},
	{"balls", 60, true, true, false, ball_shader, NULL, balls_create, balls_update, balls_step, balls_wake, balls_build,
	 balls_destroy, false, NULL, NULL},
	{"water", 40, false, true, true, water_blob_shader, water_shader, water_create, water_update, water_step,
	 water_wake, water_build, water_destroy, false, NULL, NULL},
};

const struct effect *effect_find(const char *name)
{
	if (!strcmp(name, panorama_effect.name))
		return &panorama_effect;
	for (size_t i = 0; i < sizeof(effects) / sizeof(effects[0]); i++)
		if (!strcmp(effects[i].name, name))
			return &effects[i];
	return NULL;
}
