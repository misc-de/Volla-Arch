# plinius-ambient, 360° background (panorama.c): the view for the phone's orientation (game
# rotation vector). The view is centred when the sensor is claimed, turns 1:1 and the right way
# with the phone, looks at the horizon with the phone held tilted back, keeps the horizon level
# in landscape, and the smoothing ends at rest as a proper rotation. No GL context is made; only
# the matrix code runs.

import functools
import math

import pytest

from dhw_harness import PKG, build_harness, kv, run, sandboxed_source

DIR = PKG / "plinius-ambient"
SRC = DIR / "panorama.c"

HARNESS = r"""
#include <stdio.h>
#include <stdlib.h>
#include "panorama.c"

/* Forward (-z of the camera) as longitude/elevation, the camera's up as its height, and how far
 * the matrix is from a rotation */
static void describe(const char *label, mat3 m)
{
	float fx = -m[0][2], fy = -m[1][2];
	double err = 0;
	for (int i = 0; i < 3; i++)
		for (int j = 0; j < 3; j++) {
			double dot = 0;
			for (int k = 0; k < 3; k++)
				dot += m[k][i] * m[k][j];
			err = fmax(err, fabs(dot - (i == j)));
		}
	double det = m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1]) - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0]) +
		     m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]);
	printf("%s=lon %.2f elev %.2f up %.3f ortho %.1e det %.4f\n", label, forward_longitude(m),
	       asinf(fmaxf(-1, fminf(1, fy))) * 180 / M_PI, m[1][1], err, det);
	(void)fx;
}

static void parse_q(const char *s, struct scene *sc)
{
	sscanf(s, "%f,%f,%f,%f,%d", &sc->orientation[0], &sc->orientation[1], &sc->orientation[2],
	       &sc->orientation[3], &sc->screen_angle);
	sc->has_orientation = true;
}

int main(int argc, char **argv)
{
	struct panorama p = {0};
	struct scene sc = {0};
	for (int i = 1; i < argc; i++) {
		char *a = argv[i];
		mat3 m;
		if (!strncmp(a, "claim:", 6)) {
			/* The sensor is claimed anew with this orientation */
			parse_q(a + 6, &sc);
			p.had_orientation = FALSE;
			target_view(&p, &sc, m);
			p.had_orientation = TRUE;
			describe("claim", m);
		} else if (!strncmp(a, "turn:", 5)) {
			parse_q(a + 5, &sc);
			target_view(&p, &sc, m);
			describe("view", m);
		} else if (!strcmp(a, "nosensor")) {
			sc.has_orientation = false;
			target_view(&p, &sc, m);
			describe("nosensor", m);
		} else if (!strncmp(a, "steps:", 6)) {
			/* steps:<n>:<dt>: smooth towards the current orientation */
			int n;
			float dt;
			sscanf(a + 6, "%d:%f", &n, &dt);
			p.loaded = TRUE;
			int moving = 0;
			for (int k = 0; k < n; k++)
				moving = panorama_step(&p, &sc, dt);
			mat3 target;
			target_view(&p, &sc, target);
			printf("steps: moving=%d diff=%.1e wake=%d\n", moving, difference(p.shown, target),
			       panorama_wake(&p, &sc));
			describe("shown", p.shown);
		} else if (!strncmp(a, "quat:", 5)) {
			float q[4];
			sscanf(a + 5, "%f,%f,%f,%f", &q[0], &q[1], &q[2], &q[3]);
			quaternion_matrix(q, m);
			describe("quat", m);
			printf("quat-y-axis=%.3f %.3f %.3f\n", m[0][1], m[1][1], m[2][1]);
		}
	}
	return 0;
}
"""


@pytest.fixture(scope="module")
def pano(tmp_path_factory):
    d = tmp_path_factory.mktemp("panorama")
    sandboxed_source(SRC, d, [])
    exe = build_harness(d, "panorama", HARNESS, flags=[f"-I{DIR}"], pkgs=("gdk-pixbuf-2.0", "glesv2"))
    return functools.partial(run, exe)


def qmul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz)


def axis(x, y, z, deg):
    s = math.sin(math.radians(deg) / 2)
    return (x * s, y * s, z * s, math.cos(math.radians(deg) / 2))


def phone(heading=0.0, pitch=90.0, roll=0.0, screen_angle=0):
    """Device to world (x east, y north, z up): upright is 90 degrees about x; heading turns it
    clockwise seen from above; roll turns it about the screen normal (-90: left edge up)."""
    q = qmul(axis(0, 0, 1, -heading), qmul(axis(1, 0, 0, pitch), axis(0, 0, 1, roll)))
    return ",".join(f"{v:.7f}" for v in q) + f",{screen_angle}"


def view(out, label):
    f = kv(out)[label].split()
    return {"lon": float(f[1]), "elev": float(f[3]), "up": float(f[5]), "ortho": float(f[7]),
            "det": float(f[9])}


def test_quaternion_matrix(pano):
    # Upright: the device's y axis (the top) points up in the world
    out = pano("quat:" + phone()[:-2]).stdout
    assert [float(v) for v in kv(out)["quat-y-axis"].split()] == pytest.approx([0, 0, 1], abs=1e-3)
    # Not normalized: normalized first
    out = pano("quat:0,0,0,2").stdout
    assert view(out, "quat")["det"] == pytest.approx(1, abs=1e-4)


@pytest.mark.parametrize("start", [0, 77, 200])
def test_centred_when_claimed(pano, start):
    v = view(pano("claim:" + phone(heading=start)).stdout, "claim")
    assert v["lon"] == pytest.approx(0, abs=0.05)


@pytest.mark.parametrize("turn", [30, -45, 90, 170])
def test_turns_one_to_one_the_right_way(pano, turn):
    out = pano("claim:" + phone(heading=10), "turn:" + phone(heading=10 + turn)).stdout
    # Turning the phone right shows what is right of the middle of the photo
    assert view(out, "view")["lon"] == pytest.approx(turn, abs=0.1)


@pytest.mark.parametrize("pitch,elevation", [
    (90 - 40, 0),     # held the usual way, tilted back 40 degrees: the horizon in the middle
    (90, 40),         # upright: looking 40 degrees up
    (90 - 60, -20),
])
def test_elevation(pano, pitch, elevation):
    v = view(pano("claim:" + phone(pitch=pitch)).stdout, "claim")
    assert v["elev"] == pytest.approx(elevation, abs=0.1)


def test_landscape_keeps_the_horizon_level(pano):
    # Left edge up, the output turned by 90 degrees: the camera's up is the world's up (tilted
    # back by the 40 degrees look-up, so its height is cos 40)
    portrait = view(pano("claim:" + phone()).stdout, "claim")
    landscape = view(pano("claim:" + phone(roll=-90, screen_angle=90)).stdout, "claim")
    assert portrait["up"] == pytest.approx(math.cos(math.radians(40)), abs=1e-3)
    assert landscape["up"] == pytest.approx(math.cos(math.radians(40)), abs=1e-3)
    assert landscape["elev"] == pytest.approx(40, abs=0.1)
    # Without telling it the screen turned, the horizon would stand on end
    wrong = view(pano("claim:" + phone(roll=-90, screen_angle=0)).stdout, "claim")
    assert abs(wrong["up"]) < 0.1


def test_without_sensor_straight_at_the_middle(pano):
    v = view(pano("nosensor").stdout, "nosensor")
    assert (v["lon"], v["elev"], v["up"]) == pytest.approx((0, 0, 1), abs=1e-4)


def test_smoothing_comes_to_rest_as_a_rotation(pano):
    out = pano("claim:" + phone(heading=10), "steps:1:0.016",
               "turn:" + phone(heading=60, pitch=70), "steps:3:0.008", "steps:200:0.016").stdout
    lines = [line for line in out.splitlines() if line.startswith("steps:")]
    # Moving while it follows, at rest (and not woken) once it got there
    assert lines[1].startswith("steps: moving=1")
    assert lines[2].startswith("steps: moving=0") and lines[2].endswith("wake=0")
    shown = [line for line in out.splitlines() if line.startswith("shown=")][-1]
    v = view(shown, "shown")
    assert v["lon"] == pytest.approx(50, abs=0.1)
    assert v["ortho"] < 1e-5 and v["det"] == pytest.approx(1, abs=1e-4)


def test_wakes_when_turned_at_rest(pano):
    out = pano("claim:" + phone(), "steps:200:0.016", "turn:" + phone(heading=5), "steps:0:0.016").stdout
    assert [line for line in out.splitlines() if line.startswith("steps:")][-1].endswith("wake=1")
