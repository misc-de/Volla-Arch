# plinius-ambient, drawing a frame (render.c) and a background with an effect over it (layered.c),
# against the stand-in for OpenGL ES 2 (ambient/fakegl.c): the vertices that reach the GPU are
# the effect's, byte for byte; plain effects are drawn straight into the target, a composited one
# (water) first into an offscreen field with additive blending, then through its composite
# shader; programs are built once and kept, rebuilt for another shader, and a shader that does
# not compile or link draws nothing instead of garbage; an incomplete offscreen framebuffer falls
# back to drawing directly; render_release frees every GL object. The layered effect combines
# the needs of both parts, draws the background first, then the effect over it without clearing,
# and passes every call on to both. The effects' and the composite pass's shaders are checked as
# GLSL ES 1.00 with glslangValidator.

import functools
import struct

import pytest

from ambient_helpers import build, draws, gl, glsl_errors, uniforms
from dhw_harness import kv, run

HARNESS = r"""
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "effects.h"
#include "layered.h"
#include "render.h"

void fakegl_report(void);

/* ---- Effects of the test's own: known vertices, every call printed ---- */

static int n_quads = 1;
static bool moving = true, waking = false;

static void push(struct vertex_buffer *b, const struct vertex *v)
{
	if (b->len == b->size) {
		b->size = b->size ? b->size * 2 : 16;
		b->data = realloc(b->data, b->size * sizeof(*b->data));
	}
	b->data[b->len++] = *v;
}

struct tstate {
	const char *name;
};

static void *t_create(const struct scene *s)
{
	struct tstate *t = calloc(1, sizeof(*t));
	printf("create %.0fx%.0f\n", s->width, s->height);
	return t;
}

static void t_update(void *st, const struct scene *s, float sx, float sy)
{
	printf("update %.3g %.3g\n", sx, sy);
}

static bool t_step(void *st, const struct scene *s, float dt)
{
	printf("step %.3g\n", dt);
	return moving;
}

static bool t_wake(void *st, const struct scene *s)
{
	printf("wake\n");
	return waking;
}

/* Vertex k: every field a value the test computes as well */
static void t_build(void *st, const struct scene *s, struct vertex_buffer *out)
{
	for (int k = 0; k < 6 * n_quads; k++) {
		struct vertex v = {k * 1.5f, k * 2.25f, k % 2 ? 1 : -1, k % 3 ? 1 : -1, k / 100.0f, k / 200.0f,
				   k / 400.0f, 0.5f, k, -k};
		push(out, &v);
	}
}

static void t_destroy(void *st)
{
	printf("destroy\n");
	free(st);
}

static void t_render(void *st, const struct scene *s, int width, int height)
{
	printf("render %dx%d\n", width, height);
}

static void t_visible(void *st, bool visible)
{
	printf("visible %d\n", visible);
}

#define TEST_EFFECT(n, fs, cs) \
	{.name = n, .fps = 30, .fragment_shader = fs, .composite_shader = cs, .create = t_create, .update = t_update, \
	 .step = t_step, .wake = t_wake, .build = t_build, .destroy = t_destroy}

static const char plain_fs[] = "precision mediump float;\nvoid main() { gl_FragColor = vec4(1.0); }\n";
static const char other_fs[] = "precision mediump float;\nvoid main() { gl_FragColor = vec4(0.5); }\n";
static const char field_fs[] = "precision mediump float;\nvoid main() { gl_FragColor = vec4(0.25); }\n";
static const char composite_fs[] =
	"precision mediump float;\n"
	"uniform sampler2D u_field;\n"
	"uniform vec2 u_texel;\n"
	"uniform vec2 u_size;\n"
	"varying vec2 v_uv;\n"
	"void main() { gl_FragColor = texture2D(u_field, v_uv + u_texel) * u_size.x; }\n";

static struct effect plain = TEST_EFFECT("plain", plain_fs, NULL);
static struct effect other = TEST_EFFECT("other", other_fs, NULL);
static struct effect composite = TEST_EFFECT("composite", field_fs, composite_fs);
/* A background: draws itself, tells whether it is seen, wakes */
static struct effect background = {.name = "background", .fps = 60, .full_resolution = true,
				   .wants_orientation = true, .create = t_create, .update = t_update,
				   .step = t_step, .wake = t_wake, .build = t_build, .render = t_render,
				   .set_visible = t_visible, .destroy = t_destroy};
/* One without wake and set_visible */
static struct effect bare = {.name = "bare", .fps = 20, .create = t_create, .update = t_update, .step = t_step,
			     .build = t_build, .render = t_render, .destroy = t_destroy};
static struct effect overs[30];
static char over_shaders[30][160];

static const struct effect *find(const char *name)
{
	if (!strcmp(name, "plain"))
		return &plain;
	if (!strcmp(name, "other"))
		return &other;
	if (!strcmp(name, "composite"))
		return &composite;
	if (!strcmp(name, "background"))
		return &background;
	if (!strcmp(name, "bare"))
		return &bare;
	if (!strcmp(name, "null"))
		return NULL;
	if (!strncmp(name, "over", 4))
		return &overs[atoi(name + 4)];
	return effect_find(name);
}

int main(int argc, char **argv)
{
	struct renderer r = {0};
	struct scene sc = {.width = 360, .height = 800, .scale = 1, .density = 1, .gravity = {0, 9.81f}};
	const struct effect *e = NULL;
	void *st = NULL;
	for (int i = 0; i < 30; i++) {
		/* Each with a shader of its own, to see which one a layered state draws */
		snprintf(over_shaders[i], sizeof(over_shaders[i]),
			 "precision mediump float;\nvoid main() { gl_FragColor = vec4(%d.0 / 32.0); }\n", i);
		overs[i] = (struct effect)TEST_EFFECT("over", over_shaders[i], NULL);
		overs[i].fps = i;
		overs[i].wants_gravity = i == 1;
		overs[i].wants_motion = i == 2;
		overs[i].set_visible = t_visible;
	}
	overs[3].wake = NULL;
	for (int i = 1; i < argc; i++) {
		char *a = argv[i];
		int w, h;
		if (!strncmp(a, "effect:", 7)) {
			e = find(a + 7);
			st = e->create(&sc);
		} else if (!strncmp(a, "layered:", 8)) {
			char bg[32], over[32];
			sscanf(a + 8, "%31[^:]:%31s", bg, over);
			const struct effect *l = layered_with(find(bg), find(over));
			printf("layered=%s fps %d full %d gravity %d motion %d orientation %d is-background %d\n",
			       l->name, l->fps, l->full_resolution, l->wants_gravity, l->wants_motion,
			       l->wants_orientation, l == find(bg));
			printf("slot=%ld\n", (long)(intptr_t)l);
			e = l;
		} else if (!strcmp(a, "create")) {
			st = e->create(&sc);
		} else if (!strncmp(a, "size:", 5)) {
			sscanf(a + 5, "%dx%d", &w, &h);
			sc.width = w;
			sc.height = h;
		} else if (!strncmp(a, "quads:", 6)) {
			n_quads = atoi(a + 6);
		} else if (!strncmp(a, "moving:", 7)) {
			moving = atoi(a + 7);
		} else if (!strncmp(a, "waking:", 7)) {
			waking = atoi(a + 7);
		} else if (!strncmp(a, "bindfb:", 7)) {
			glBindFramebuffer(GL_FRAMEBUFFER, atoi(a + 7));
		} else if (!strncmp(a, "frame:", 6)) {
			sscanf(a + 6, "%dx%d", &w, &h);
			render_frame(&r, e, st, &sc, w, h, false);
		} else if (!strncmp(a, "tint:", 5)) {
			sscanf(a + 5, "%dx%d", &w, &h);
			render_frame(&r, e, st, &sc, w, h, true);
		} else if (!strncmp(a, "over:", 5)) {
			sscanf(a + 5, "%dx%d", &w, &h);
			render_frame_over(&r, e, st, &sc, w, h);
		} else if (!strncmp(a, "run:", 4)) {
			/* A real effect stepped for a while, so that it has particles */
			for (int k = 0; k < atoi(a + 4); k++)
				e->step(st, &sc, 1 / 60.0f);
		} else if (!strncmp(a, "step:", 5)) {
			printf("step=%d\n", e->step(st, &sc, strtof(a + 5, NULL)));
		} else if (!strcmp(a, "wake")) {
			printf("wake=%d\n", e->wake(st, &sc));
		} else if (!strncmp(a, "visible:", 8)) {
			e->set_visible(st, atoi(a + 8));
		} else if (!strncmp(a, "update:", 7)) {
			float sx, sy;
			sscanf(a + 7, "%f:%f", &sx, &sy);
			e->update(st, &sc, sx, sy);
		} else if (!strcmp(a, "destroy")) {
			e->destroy(st);
			st = NULL;
		} else if (!strcmp(a, "release")) {
			render_release(&r);
			struct renderer zero = {0};
			printf("released=%d\n", !memcmp(&r, &zero, sizeof(r)));
		} else if (!strcmp(a, "report")) {
			fakegl_report();
		} else if (!strcmp(a, "build")) {
			struct vertex_buffer vb = {0};
			e->build(st, &sc, &vb);
			printf("built=%zu\n", vb.len);
		} else if (!strcmp(a, "vertices")) {
			printf("vertices=%zu\n", r.vertices.len);
		}
	}
	return 0;
}
"""

VERTEX = struct.Struct("<10f")


@pytest.fixture(scope="module")
def exe(tmp_path_factory):
    d = tmp_path_factory.mktemp("ambient-render")
    return build(d, "render", HARNESS, ["render.c", "layered.c"], uncounted=["effects.c", "panorama.c"],
                 pkgs=("glib-2.0", "gdk-pixbuf-2.0"))


@pytest.fixture
def draw(exe, tmp_path):
    def call(*args, env=None, check=True):
        e = {"FAKEGL_DUMP": str(tmp_path), "PATH": "/usr/bin:/bin"}
        e.update(env or {})
        return run(exe, *args, env=e, check=check)
    return call


def expected_vertices(quads):
    data = b""
    for k in range(6 * quads):
        data += VERTEX.pack(k * 1.5, k * 2.25, 1 if k % 2 else -1, 1 if k % 3 else -1, k / 100, k / 200,
                            k / 400, 0.5, k, -k)
    return data


def buffer_dumps(out):
    """(buffer, bytes, dump file) of every glBufferData"""
    result = []
    for line in gl(out, "buffer data "):
        f = line.split()
        result.append((int(f[2]), int(f[3]), f[5]))
    return result


def test_plain_effect_draws_its_vertices_straight_into_the_target(draw):
    r = draw("effect:plain", "quads:3", "frame:720x1600")
    out = r.stdout
    # Cleared to transparent first
    assert gl(out, "clear color") == ["clear color 0 0 0 0"]
    assert gl(out, "clear ") == ["clear color 0 0 0 0", "clear framebuffer 0 viewport 720x1600"]
    [d] = draws(out)
    assert d["mode"] == "TRIANGLES" and (d["first"], d["count"]) == (0, 18)
    # Premultiplied colour over what is there; drawn into the framebuffer that was bound
    assert d["blend"] == "ONE,ONE_MINUS_SRC_ALPHA" and d["framebuffer"] == 0
    assert d["viewport"] == "720x1600" and d["attribs"] == 0b1111
    # The bytes on the GPU are the effect's vertices
    [(buf, size, path)] = buffer_dumps(out)
    assert d["buffer"] == buf and size == d["bytes"] == 18 * VERTEX.size
    with open(path, "rb") as f:
        assert f.read() == expected_vertices(3)
    # The attributes: position, uv, colour, parameters at their offsets in struct vertex
    attribs = [line.split() for line in gl(out, "attrib ")]
    assert [(int(a[1]), int(a[3]), int(a[5]), int(a[7])) for a in attribs] == [
        (0, 2, 40, 0), (1, 2, 40, 8), (2, 4, 40, 16), (3, 2, 40, 32)]
    # The logical size goes to the shader, the buffer size to the viewport
    assert uniforms(out)["u_size"] == [360, 800]
    # a_pos, a_uv, a_color, a_param at 0..3, as the shared vertex shader expects
    assert [line.split()[3:] for line in gl(out, "bind attrib")] == [
        ["0", "a_pos"], ["1", "a_uv"], ["2", "a_color"], ["3", "a_param"]]


def test_tint_clears_to_faint_red(draw):
    out = draw("effect:plain", "tint:100x100").stdout
    assert gl(out, "clear color") == ["clear color 0.3 0 0 0.3"]


def test_program_is_built_once_and_rebuilt_for_another_shader(draw):
    out = draw("effect:plain", "frame:10x10", "frame:10x10", "frame:10x10",
               "effect:other", "frame:10x10", "report").stdout
    created = gl(out, "create program")
    assert len(created) == 2
    first = int(created[0].split()[2])
    # The first program is deleted when another shader needs one, the shaders after linking
    assert f"delete program {first}" in gl(out, "delete program")
    assert [d["program"] for d in draws(out)] == [first] * 3 + [int(created[1].split()[2])]
    assert gl(out, "live") == ["live shaders 0 programs 1 buffers 1 textures 0 framebuffers 0"]
    # One vertex buffer for all frames
    assert len(gl(out, "gen buffer")) == 1


def test_nothing_to_draw_only_clears(draw):
    out = draw("effect:plain", "quads:0", "frame:10x10", "vertices").stdout
    assert draws(out) == [] and gl(out, "clear framebuffer") == ["clear framebuffer 0 viewport 10x10"]
    # Nothing built either
    assert gl(out, "create program") == []


def test_shader_that_does_not_compile_draws_nothing(draw):
    r = draw("effect:plain", "frame:10x10", "frame:10x10", env={"FAKEGL_FAIL_COMPILE": "vec4(1.0)"})
    assert "Shader does not compile: fake compile error" in r.stderr
    assert "Program for plain does not link: fake link error" in r.stderr
    assert draws(r.stdout) == []
    # Tried again for the next frame (nothing is kept of the failed program), and the failed
    # programs are deleted
    assert len(gl(r.stdout, "create program")) == 2
    assert len(gl(r.stdout, "delete program")) == 2


def test_composited_effect_goes_through_the_field(draw):
    out = draw("bindfb:7", "effect:composite", "quads:2", "frame:300x500", "frame:300x500").stdout
    d = draws(out)
    assert len(d) == 4
    field, picture = d[0], d[1]
    fbo = int(gl(out, "gen framebuffer")[0].split()[2])
    tex = int(gl(out, "gen texture")[0].split()[2])
    # The blobs add up in the offscreen field, at the target's size
    assert field["framebuffer"] == fbo and field["blend"] == "ONE,ONE" and field["count"] == 12
    assert gl(out, f"tex image {tex}") == [f"tex image {tex} 300x500 RGBA -"]
    assert gl(out, "framebuffer texture") == [f"framebuffer texture {fbo} {tex}"]
    # The field is cleared, then the target that was bound (7) gets the composite: one quad
    # over the whole framebuffer, the field on unit 0
    assert gl(out, "clear framebuffer")[:2] == [f"clear framebuffer {fbo} viewport 300x500",
                                               "clear framebuffer 7 viewport 300x500"]
    assert picture["framebuffer"] == 7 and picture["mode"] == "TRIANGLE_STRIP"
    assert picture["count"] == 4 and picture["blend"] == "ONE,ONE_MINUS_SRC_ALPHA"
    assert picture["attribs"] == 0b0001 and picture["textures"][0] == tex
    assert picture["program"] != field["program"]
    u = uniforms(out)
    assert u["u_field"] == [0]
    assert u["u_texel"] == pytest.approx([1 / 300, 1 / 500], rel=1e-5)
    assert u["u_size"] == [360, 800]
    # The full-screen quad's corners, uploaded once
    quads = [b for b in buffer_dumps(out) if b[1] == 32]
    assert len(quads) == 1
    with open(quads[0][2], "rb") as f:
        assert struct.unpack("<8f", f.read()) == (-1, -1, 1, -1, -1, 1, 1, 1)
    # Same size again: the field is kept
    assert len(gl(out, "tex image")) == 1 and len(gl(out, "gen framebuffer")) == 1


def test_field_follows_the_size(draw):
    out = draw("effect:composite", "frame:300x500", "frame:500x300", "release", "report").stdout
    assert [line.split()[3] for line in gl(out, "tex image")] == ["300x500", "500x300"]
    assert len(gl(out, "gen framebuffer")) == 1
    # Everything freed, the renderer back to zero
    assert gl(out, "live") == ["live shaders 0 programs 0 buffers 0 textures 0 framebuffers 0"]
    assert kv(out)["released"] == "1"


def test_incomplete_field_is_reported_and_tried_again(draw):
    r = draw("effect:composite", "frame:300x500", "frame:300x500", env={"FAKEGL_FB_STATUS": "8cd6"})
    assert "Offscreen framebuffer incomplete (0x8cd6)" in r.stderr
    # Every frame the quads directly, without the composite pass
    assert [(x["mode"], x["blend"]) for x in draws(r.stdout)] == [("TRIANGLES", "ONE,ONE_MINUS_SRC_ALPHA")] * 2
    # The field is set up again for the next frame (it may work at another size)
    assert len(gl(r.stdout, "tex image")) == 2


def test_incomplete_field_draws_into_the_target(draw):
    r = draw("bindfb:7", "effect:composite", "frame:300x500", "frame:300x500", env={"FAKEGL_FB_STATUS": "8cd6"})
    assert [x["framebuffer"] for x in draws(r.stdout)] == [7, 7]
    assert gl(r.stdout, "clear framebuffer") == ["clear framebuffer 7 viewport 300x500"] * 2


def test_complete_field_still_ends_on_the_target(draw):
    r = draw("bindfb:7", "effect:composite", "frame:300x500", "frame:300x500")
    # The field in its own framebuffer, the picture from it on the target, every frame
    fbs = [x["framebuffer"] for x in draws(r.stdout)]
    assert len(fbs) == 4 and fbs[1::2] == [7, 7] and 7 not in fbs[0::2]


def test_composite_shader_that_does_not_link(draw):
    r = draw("effect:composite", "frame:300x500", env={"FAKEGL_FAIL_LINK": "u_field"})
    d = draws(r.stdout)
    # The field is drawn, the picture from it is not
    assert len(d) == 1 and d[0]["blend"] == "ONE,ONE"
    assert "Program for composite does not link" in r.stderr


def test_field_shader_that_does_not_link(draw):
    r = draw("effect:composite", "frame:300x500", env={"FAKEGL_FAIL_LINK": "vec4(0.25)"})
    d = draws(r.stdout)
    # No blobs, but the composite pass still runs (over a cleared field)
    assert [x["mode"] for x in d] == ["TRIANGLE_STRIP"]


def test_effect_with_its_own_render(draw):
    out = draw("effect:background", "frame:64x32").stdout
    assert "render 64x32" in out.splitlines()
    assert gl(out) == []


def test_frame_over_does_not_clear(draw):
    out = draw("effect:plain", "over:64x32").stdout
    assert gl(out, "clear") == [] and len(draws(out)) == 1


def test_release_of_an_unused_renderer(draw):
    out = draw("release", "report").stdout
    assert kv(out)["released"] == "1"
    assert gl(out, "delete") == []


@pytest.mark.parametrize("name", ["snow", "dust", "fireflies", "rain", "balls", "water"])
def test_real_effects(draw, tmp_path, name):
    out = draw(f"effect:{name}", "run:30", "frame:720x1600", "vertices", "release", "report").stdout
    n = int(kv(out)["vertices"])
    assert n > 0 and n % 6 == 0
    d = draws(out)
    if name == "water":
        assert [x["blend"] for x in d] == ["ONE,ONE", "ONE,ONE_MINUS_SRC_ALPHA"]
        assert d[0]["count"] == n
    else:
        assert len(d) == 1 and d[0]["count"] == n
    assert gl(out, "live") == ["live shaders 0 programs 0 buffers 0 textures 0 framebuffers 0"]
    errors = glsl_errors(tmp_path)
    if errors is None:
        pytest.skip("no glslangValidator")
    assert errors == {}
    assert len(list(tmp_path.glob("shader-*.vert"))) == len(d)


# ---- layered.c ----

def test_layered_pairs(draw):
    out = draw("layered:background:over1", "layered:background:over2", "layered:bare:over25").stdout
    lines = [line for line in out.splitlines() if line.startswith("layered=")]
    # The effect's name, the faster frame rate, always full resolution, the sensors of both
    assert lines[0] == "layered=over fps 60 full 1 gravity 1 motion 0 orientation 1 is-background 0"
    assert lines[1] == "layered=over fps 60 full 1 gravity 0 motion 1 orientation 1 is-background 0"
    assert lines[2] == "layered=over fps 25 full 1 gravity 0 motion 0 orientation 0 is-background 0"


def test_layered_same_pair_same_effect(draw):
    out = draw("layered:background:over1", "layered:background:over2", "layered:background:over1").stdout
    slots = [line for line in out.splitlines() if line.startswith("slot=")]
    assert slots[0] == slots[2] != slots[1]


def test_layered_without_effect_is_the_background(draw):
    out = draw("layered:background:null", "layered:background:background").stdout
    assert [line.split()[-1] for line in out.splitlines() if line.startswith("layered=")] == ["1", "1"]


def test_layered_table_full_falls_back_to_the_background(draw):
    args = [f"layered:background:over{i}" for i in range(25)]
    out = draw(*args).stdout
    flags = [line.split()[-1] for line in out.splitlines() if line.startswith("layered=")]
    # 24 combinations fit; the 25th shows the background alone rather than nothing
    assert flags == ["0"] * 24 + ["1"]


def test_layered_runs_both_parts(draw):
    out = draw("layered:background:over1", "create", "update:2:0.5", "moving:0", "step:0.016",
               "visible:1", "frame:720x1600", "destroy", "release", "report").stdout
    lines = out.splitlines()
    # Both created, both updated, both stepped
    assert lines.count("create 360x800") == 2
    assert lines.count("update 2 0.5") == 2
    assert lines.count("step 0.016") == 2 and kv(out)["step"] == "0"
    # Both told (the effect over it has set_visible too)
    assert lines.count("visible 1") == 2
    # The background draws itself, the effect is drawn over it without clearing
    i = lines.index("render 720x1600")
    assert [line for line in lines[i:] if line.startswith("gl clear")] == []
    assert len(draws("\n".join(lines[i:]))) == 1
    # Both destroyed, the effect's GL objects freed
    assert lines.count("destroy") == 2
    assert gl(out, "live") == ["live shaders 0 programs 0 buffers 0 textures 0 framebuffers 0"]


@pytest.mark.parametrize("moving,expected", [(1, "1"), (0, "0")])
def test_layered_moves_while_a_part_moves(draw, moving, expected):
    out = draw("layered:background:over1", "create", f"moving:{moving}", "step:0.02").stdout
    assert kv(out)["step"] == expected


def test_layered_wake(draw):
    # Neither part wakes
    out = draw("layered:background:over1", "create", "wake").stdout
    assert kv(out)["wake"] == "0" and out.splitlines().count("wake") == 2
    # The background wakes: the effect is not asked
    out = draw("layered:background:over1", "create", "waking:1", "wake").stdout
    assert kv(out)["wake"] == "1" and out.splitlines().count("wake") == 1
    # Neither has a wake: at rest
    out = draw("layered:bare:over3", "create", "wake").stdout
    assert kv(out)["wake"] == "0" and out.splitlines().count("wake") == 0
    # Only the effect has one
    out = draw("layered:bare:over1", "create", "waking:1", "wake").stdout
    assert kv(out)["wake"] == "1" and out.splitlines().count("wake") == 1


def test_layered_visible_without_set_visible(draw):
    # Neither part has set_visible: nothing is called
    out = draw("layered:bare:plain", "create", "visible:1").stdout
    assert "visible 1" not in out.splitlines()


def test_layered_hands_over_no_quads_of_its_own(draw):
    # Both parts draw in render; build (which the client never calls for it) adds nothing
    out = draw("layered:background:over1", "create", "build").stdout
    assert kv(out)["built"] == "0"


def test_every_layered_slot_creates_its_own_pair(draw, tmp_path):
    # Each of the 24 combinations is a state of its own pair: its frame draws the effect it was
    # made for, over the background
    args = []
    for i in range(24):
        args += [f"layered:background:over{i}", "create", "frame:8x8", "destroy"]
    out = draw(*args).stdout
    drawn = []
    for line in gl(out, "shader source"):
        path = line.split()[3]
        if path.endswith(".frag"):
            drawn.append(open(path).read())
    assert [f"vec4({i}.0 / 32.0)" in text for i, text in enumerate(drawn)] == [True] * 24
    assert out.splitlines().count("render 8x8") == 24
