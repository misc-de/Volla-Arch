# plinius-ambient, 360° background (panorama.c): loading the photo and drawing it, against the
# stand-in for OpenGL ES 2 (ambient/fakegl.c). The photo is decoded on a thread and reaches the
# texture pixel for pixel (padding of odd rows dropped, an alpha channel kept, the embedded
# orientation applied, scaled down to what the GPU and the 8192 px limit take); until it is
# there and when it cannot be read nothing but a transparent clear is drawn. The quad gets the
# shown rotation and the field of view of the screen's shape; the photo's middle is what the
# view looks at without a sensor; destroying the effect frees its GL objects, also while the
# photo is still loading. The view's matrix maths is in test_dhw_ambient_panorama.py.

import math
import os

import pytest

from ambient_helpers import build, draws, gl, glsl_errors, uniforms
from dhw_harness import kv, run

PIL = pytest.importorskip("PIL.Image")

HARNESS = r"""
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include "panorama.c"

void fakegl_report(void);

static void wait_loaded(struct panorama *p)
{
	for (int i = 0; p->loader && i < 20000; i++) {
		g_mutex_lock(&p->loader->lock);
		gboolean done = p->loader->done;
		g_mutex_unlock(&p->loader->lock);
		if (done)
			return;
		usleep(1000);
	}
}

int main(int argc, char **argv)
{
	struct scene sc = {.width = 360, .height = 800};
	struct panorama *p = NULL;
	for (int i = 1; i < argc; i++) {
		char *a = argv[i];
		int w, h;
		if (!strncmp(a, "picture:", 8)) {
			sc.picture = a + 8;
		} else if (!strcmp(a, "create")) {
			p = panorama_effect.create(&sc);
		} else if (!strcmp(a, "wait")) {
			wait_loaded(p);
		} else if (!strncmp(a, "render:", 7)) {
			sscanf(a + 7, "%dx%d", &w, &h);
			panorama_effect.render(p, &sc, w, h);
			printf("loaded=%d\nfailed=%d\n", p->loaded, p->failed);
		} else if (!strncmp(a, "step:", 5)) {
			printf("step=%d\n", panorama_effect.step(p, &sc, strtof(a + 5, NULL)));
		} else if (!strcmp(a, "wake")) {
			printf("wake=%d\n", panorama_effect.wake(p, &sc));
		} else if (!strncmp(a, "orient:", 7)) {
			sscanf(a + 7, "%f,%f,%f,%f", &sc.orientation[0], &sc.orientation[1], &sc.orientation[2],
			       &sc.orientation[3]);
			sc.has_orientation = true;
		} else if (!strcmp(a, "nosensor")) {
			sc.has_orientation = false;
		} else if (!strcmp(a, "update")) {
			panorama_effect.update(p, &sc, 2, 0.5f);
		} else if (!strcmp(a, "build")) {
			struct vertex_buffer vb = {0};
			panorama_effect.build(p, &sc, &vb);
			printf("built=%zu\n", vb.len);
		} else if (!strcmp(a, "destroy")) {
			panorama_effect.destroy(p);
			p = NULL;
			fakegl_report();
		} else if (!strcmp(a, "linger")) {
			/* The loading thread finishes after the effect is gone */
			usleep(300000);
		}
	}
	return 0;
}
"""


@pytest.fixture(scope="module")
def exe(tmp_path_factory):
    d = tmp_path_factory.mktemp("ambient-panorama-gl")
    return build(d, "panorama-gl", HARNESS, [], included=["panorama.c"], pkgs=("glib-2.0", "gdk-pixbuf-2.0"))


@pytest.fixture
def pano(exe, tmp_path):
    dump = tmp_path / "dump"
    dump.mkdir()

    def call(*args, env=None):
        e = {"FAKEGL_DUMP": str(dump), "PATH": "/usr/bin:/bin"}
        e.update(env or {})
        return run(exe, *args, env=e, timeout=60)
    return call


def photo(path, width, height, mode="RGB", **save):
    """Every pixel a colour of its own position, so that a test sees which one went where"""
    img = PIL.new(mode, (width, height))
    for y in range(height):
        for x in range(width):
            px = (x * 7 % 256, y * 11 % 256, (x + y) % 256)
            img.putpixel((x, y), px + ((x * y) % 256,) if mode == "RGBA" else px)
    img.save(path, **save)
    return img


def textures(out):
    """(size, format, dump file) of every texture upload"""
    return [(line.split()[3], line.split()[4], line.split()[5]) for line in gl(out, "tex image ")]


@pytest.mark.parametrize("width,mode,fmt", [(64, "RGB", "RGB"), (21, "RGB", "RGB"), (21, "RGBA", "RGBA")])
def test_photo_reaches_the_texture_pixel_for_pixel(pano, tmp_path, width, mode, fmt):
    # 21 px of RGB: gdk-pixbuf pads the rows to 64 bytes, GLES 2 takes them packed
    img = photo(tmp_path / "p.png", width, 32, mode)
    r = pano("picture:" + str(tmp_path / "p.png"), "create", "wait", "render:720x1600")
    assert kv(r.stdout)["loaded"] == "1"
    [(size, got_fmt, path)] = textures(r.stdout)
    assert (size, got_fmt) == (f"{width}x32", fmt)
    with open(path, "rb") as f:
        assert f.read() == img.tobytes()
    assert f"360° photo loaded at {width}×32" in r.stderr
    # Linear, no mipmaps, clamped: the longitude wraps in the shader
    params = gl(r.stdout, "tex parameter")
    assert sorted(p.split(maxsplit=3)[3] for p in params) == [
        "MAG_FILTER LINEAR", "MIN_FILTER LINEAR", "WRAP_S CLAMP_TO_EDGE", "WRAP_T CLAMP_TO_EDGE"]


def test_drawn_as_one_quad_with_the_view(pano, tmp_path):
    photo(tmp_path / "p.png", 64, 32)
    out = pano("picture:" + str(tmp_path / "p.png"), "create", "wait", "render:720x1600", "step:0.016",
               "render:720x1600", "render:1600x720").stdout
    d = draws(out)
    # Nothing until a view has been set by a step, then the whole screen, without blending
    assert len(d) == 2
    assert all(x["mode"] == "TRIANGLE_STRIP" and x["count"] == 4 and x["blend"] == "off" for x in d)
    assert d[0]["attribs"] == 0b0001 and d[0]["bytes"] == 32
    half = math.tan(math.radians(95 / 2))
    tans = [[float(v) for v in line.split()[2:]] for line in gl(out, "uniform u_tan")]
    # The field of view across the long side: portrait, then landscape
    assert tans[0] == pytest.approx([half * 720 / 1600, half], rel=1e-5)
    assert tans[1] == pytest.approx([half, half * 720 / 1600], rel=1e-5)
    # Without a sensor straight at the middle: the identity, column-major
    u = uniforms(out)
    assert u["u_rotation"] == pytest.approx([1, 0, 0, 0, 1, 0, 0, 0, 1], abs=1e-6)
    assert u["u_picture"] == [0]


def ray_texel(rotation, width, height):
    """The texel the middle of the screen shows, by the fragment shader's formula"""
    m = [[rotation[c * 3 + r] for c in range(3)] for r in range(3)]
    d = [-m[r][2] for r in range(3)]  # u_rotation * (0, 0, -1)
    n = math.sqrt(sum(v * v for v in d))
    d = [v / n for v in d]
    u = (0.5 + math.atan2(d[0], -d[2]) / (2 * math.pi)) % 1
    v = 0.5 - math.asin(max(-1, min(1, d[1]))) / math.pi
    return min(int(u * width), width - 1), min(int(v * height), height - 1)


def quaternion(axis, deg):
    s = math.sin(math.radians(deg) / 2)
    return [a * s for a in axis] + [math.cos(math.radians(deg) / 2)]


def test_the_middle_of_the_screen_turns_with_the_phone(pano, tmp_path):
    img = photo(tmp_path / "p.png", 64, 32)
    # Upright (90° about x) when claimed: the middle of the photo, 40° above the horizon
    upright = ",".join(f"{v:.7f}" for v in quaternion((1, 0, 0), 90))
    out = pano("picture:" + str(tmp_path / "p.png"), "create", "wait", "orient:" + upright,
               "render:720x1600", "step:0.016", "render:720x1600").stdout
    x, y = ray_texel(uniforms(out)["u_rotation"], 64, 32)
    assert x == 32
    assert y == int((0.5 - 40 / 180) * 32)
    assert img.getpixel((x, y)) == (32 * 7 % 256, y * 11 % 256, (32 + y) % 256)


def test_photo_larger_than_the_gpu_takes_is_scaled(pano, tmp_path):
    photo(tmp_path / "p.png", 64, 32)
    out = pano("picture:" + str(tmp_path / "p.png"), "create", "wait", "render:8x8",
               env={"FAKEGL_MAX_TEXTURE": "16"}).stdout
    assert textures(out)[0][:2] == ("16x8", "RGB")


def test_scaled_photo_with_padded_rows_is_packed(pano, tmp_path):
    # Scaled to 21 px of RGB, gdk-pixbuf pads each row of 63 bytes to 64: GLES 2 has no row
    # length, every row must start where the last one ended
    PIL.new("RGB", (42, 20), (10, 200, 30)).save(tmp_path / "p.png")
    out = pano("picture:" + str(tmp_path / "p.png"), "create", "wait", "render:8x8",
               env={"FAKEGL_MAX_TEXTURE": "21"}).stdout
    size, fmt, path = textures(out)[0]
    assert (size, fmt) == ("21x10", "RGB")
    with open(path, "rb") as f:
        assert f.read() == bytes((10, 200, 30)) * 21 * 10


def test_photo_beyond_8192_is_decoded_smaller(pano, tmp_path):
    img = PIL.new("RGB", (8200, 4), (10, 200, 30))
    img.save(tmp_path / "wide.png")
    out = pano("picture:" + str(tmp_path / "wide.png"), "create", "wait", "render:8x8",
               env={"FAKEGL_MAX_TEXTURE": "16384"}).stdout
    size, fmt, path = textures(out)[0]
    # The long side at 8192, the aspect kept (4 * 8192 / 8200 rounds to 4)
    assert size == "8192x4"
    with open(path, "rb") as f:
        assert set(f.read()[:300]) == {10, 200, 30}


def test_embedded_orientation_is_applied(pano, tmp_path):
    img = PIL.new("RGB", (40, 20), (255, 0, 0))
    exif = img.getexif()
    exif[0x0112] = 6  # rotated 90° clockwise to be shown upright
    img.save(tmp_path / "turned.jpg", exif=exif.tobytes())
    out = pano("picture:" + str(tmp_path / "turned.jpg"), "create", "wait", "render:8x8").stdout
    assert textures(out)[0][0] == "20x40"


@pytest.mark.parametrize("content", [None, b"not an image at all"])
def test_photo_that_cannot_be_read(pano, tmp_path, content):
    path = tmp_path / "broken.png"
    if content:
        path.write_bytes(content)
    r = pano("picture:" + str(path), "create", "wait", "render:8x8", "step:0.016", "wake")
    assert f"Cannot load the 360° photo {path}" in r.stderr
    out = r.stdout
    # Phosh's background shows: cleared to transparent, nothing drawn, at rest
    assert kv(out)["failed"] == "1" and kv(out)["step"] == "0"
    assert draws(out) == [] and gl(out, "clear color") == ["clear color 0 0 0 0"]
    assert gl(out, "tex image") == []


def test_without_a_photo(pano):
    out = pano("create", "step:0.016", "render:8x8", "destroy").stdout
    assert kv(out)["failed"] == "1" and kv(out)["step"] == "0"
    assert gl(out, "live") == ["live shaders 0 programs 0 buffers 0 textures 0 framebuffers 0"]


def test_still_loading(pano, tmp_path):
    # A FIFO nobody writes to: the loading thread waits in it for good, so the photo is
    # certainly not there yet
    fifo = tmp_path / "slow.png"
    os.mkfifo(fifo)
    out = pano("picture:" + str(fifo), "create", "step:0.016", "wake", "render:8x8").stdout
    # Keeps looking (moving), wakes, and draws nothing but a transparent clear meanwhile
    assert kv(out)["step"] == "1" and kv(out)["wake"] == "1"
    assert kv(out)["loaded"] == "0" and kv(out)["failed"] == "0"
    assert draws(out) == [] and gl(out, "clear color") == ["clear color 0 0 0 0"]
    assert gl(out, "create program") == []


def test_destroyed_while_loading_and_after(pano, tmp_path):
    photo(tmp_path / "p.png", 64, 32)
    # The thread holds its own reference: the effect may go first (ASan watches)
    out = pano("picture:" + str(tmp_path / "p.png"), "create", "destroy", "linger").stdout
    assert gl(out, "live") == ["live shaders 0 programs 0 buffers 0 textures 0 framebuffers 0"]
    out = pano("picture:" + str(tmp_path / "p.png"), "create", "wait", "render:8x8", "destroy").stdout
    assert len(gl(out, "delete program")) == 1 and len(gl(out, "delete texture")) == 1
    assert len(gl(out, "delete buffer")) == 1
    assert gl(out, "live") == ["live shaders 0 programs 0 buffers 0 textures 0 framebuffers 0"]


def test_wake_and_rest(pano, tmp_path):
    photo(tmp_path / "p.png", 64, 32)
    up = ",".join(f"{v:.7f}" for v in quaternion((1, 0, 0), 90))
    out = pano("picture:" + str(tmp_path / "p.png"), "create", "wait", "render:8x8",
               "orient:" + up, "step:0.016", "step:0.016", "wake",
               "nosensor", "wake").stdout.splitlines()
    # The first step shows the view at once, the next is at rest; the sensor going away wakes it
    assert [line for line in out if line.startswith(("step=", "wake="))] == [
        "step=1", "step=0", "wake=0", "wake=1"]


def test_neither_update_nor_build_change_anything(pano, tmp_path):
    photo(tmp_path / "p.png", 64, 32)
    out = pano("picture:" + str(tmp_path / "p.png"), "create", "wait", "render:8x8", "step:0.016",
               "update", "build", "render:8x8").stdout
    # Drawn by its own render: no quads; a new size needs nothing (the quad is the screen)
    assert kv(out)["built"] == "0"
    assert len(textures(out)) == 1 and len(draws(out)) == 1


def test_shader_that_does_not_compile_or_link(pano, tmp_path):
    photo(tmp_path / "p.png", 64, 32)
    r = pano("picture:" + str(tmp_path / "p.png"), "create", "wait", "render:8x8", "step:0.016",
             "render:8x8", "destroy", env={"FAKEGL_FAIL_COMPILE": "u_picture"})
    assert "360° shader does not compile: fake compile error" in r.stderr
    assert "360° program does not link" in r.stderr
    # Given up: no texture, nothing drawn, at rest; the program is freed with the effect
    assert kv(r.stdout)["failed"] == "1" and kv(r.stdout)["step"] == "0"
    assert textures(r.stdout) == [] and draws(r.stdout) == []
    assert gl(r.stdout, "live") == ["live shaders 0 programs 0 buffers 0 textures 0 framebuffers 0"]


def test_shaders_are_glsl_es(pano, tmp_path):
    photo(tmp_path / "p.png", 64, 32)
    pano("picture:" + str(tmp_path / "p.png"), "create", "wait", "render:8x8")
    errors = glsl_errors(tmp_path / "dump")
    if errors is None:
        pytest.skip("no glslangValidator")
    assert errors == {} and len(list((tmp_path / "dump").glob("shader-*"))) == 2
