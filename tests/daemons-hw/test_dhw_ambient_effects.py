# plinius-ambient, the particle effects (effects.c): every effect is created, stepped through a
# few seconds of tilting and shaking, a screen rotation and a pause, and built into quads; all
# vertices must stay finite (a NaN would blank the background) and the balls and the water must
# stay on the screen. Runs under ASan/UBSan; no GL context is made.

import functools

import pytest

from dhw_harness import PKG, build_harness, kv, run, sandboxed_source

DIR = PKG / "plinius-ambient"
EFFECTS = ["snow", "dust", "fireflies", "rain", "balls", "water"]
CONTAINED = {"balls", "water"}

HARNESS = r"""
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "effects.h"

/* Steps the effect for <seconds> at <dt>, gravity turning once round and shaking in between */
static int simulate(const struct effect *e, void *st, struct scene *sc, float seconds, float dt, double *t)
{
	int moving = 0;
	for (float s = 0; s < seconds; s += dt, *t += dt) {
		float a = (float)(*t * 0.4);
		sc->gravity[0] = 9.81f * sinf(a);
		sc->gravity[1] = 9.81f * cosf(a);
		sc->shake = fmodf((float)*t, 7) < 1 ? 40 : 0;
		sc->impulse[0] = fmodf((float)*t, 5) < 0.05f ? 1.5f : 0;
		sc->impulse[1] = 0;
		moving = e->step(st, sc, dt);
	}
	return moving;
}

static void check(const char *label, const struct effect *e, void *st, const struct scene *sc, float margin)
{
	struct vertex_buffer vb = {0};
	e->build(st, sc, &vb);
	size_t bad = 0, outside = 0;
	for (size_t i = 0; i < vb.len; i++) {
		struct vertex *v = &vb.data[i];
		if (!isfinite(v->x) || !isfinite(v->y) || !isfinite(v->r) || !isfinite(v->a) || !isfinite(v->p))
			bad++;
		else if (v->x < -margin || v->x > sc->width + margin || v->y < -margin || v->y > sc->height + margin)
			outside++;
	}
	printf("%s=vertices %zu nonfinite %zu outside %zu\n", label, vb.len, bad, outside);
	free(vb.data);
}

int main(int argc, char **argv)
{
	const struct effect *e = effect_find(argv[1]);
	int density = atoi(argv[2]);
	float margin = strtof(argv[3], NULL);
	if (!e) {
		printf("missing\n");
		return 0;
	}
	struct scene sc = {.width = 360, .height = 800, .scale = 3, .density = density,
			   .gravity = {0, 9.81f}};
	double t = 0;
	void *st = e->create(&sc);
	simulate(e, st, &sc, 6, 1 / 60.0f, &t);
	check("portrait", e, st, &sc, margin);
	/* Turned to landscape */
	sc.width = 800;
	sc.height = 360;
	e->update(st, &sc, 800 / 360.0f, 360 / 800.0f);
	simulate(e, st, &sc, 6, 1 / 40.0f, &t);
	check("landscape", e, st, &sc, margin);
	/* The longest frame the client passes (it clamps dt to 0..0.1 s); with "zero" also an empty
	 * one, which the clamp allows (a draw in the same microsecond as the last step) */
	e->step(st, &sc, 0.1f);
	if (argc > 4 && !strcmp(argv[4], "zero"))
		e->step(st, &sc, 0.0f);
	check("long-frame", e, st, &sc, margin);
	/* Lying flat and still: things come to rest (or keep falling, for snow and rain) */
	sc.gravity[0] = sc.gravity[1] = 0;
	sc.shake = sc.impulse[0] = 0;
	int moving = 1, flat_steps = argc > 5 ? atoi(argv[5]) : 120;
	for (int i = 0; i < flat_steps && moving; i++)
		moving = e->step(st, &sc, 1 / 60.0f);
	check("flat", e, st, &sc, margin);
	/* At rest: nothing changed keeps it resting; a tilt (balls) or a shake (water) wakes it */
	int still = e->wake ? e->wake(st, &sc) : -1;
	if (e->wants_motion)
		sc.shake = 100;
	else
		sc.gravity[0] = argc > 6 ? strtof(argv[6], NULL) : 9.81f;
	printf("rest=%d still=%d woken=%d\n", !moving, still, e->wake ? e->wake(st, &sc) : -1);
	e->destroy(st);
	return 0;
}
"""


@pytest.fixture(scope="module")
def sim(tmp_path_factory):
    d = tmp_path_factory.mktemp("effects")
    effects = sandboxed_source(DIR / "effects.c", d, [])
    panorama = sandboxed_source(DIR / "panorama.c", d, [])
    exe = build_harness(d, "effects", HARNESS, flags=["-O2", f"-I{DIR}", str(effects), str(panorama)],
                        pkgs=("gdk-pixbuf-2.0", "glesv2"))
    return functools.partial(run, exe)


def check_phases(out, name):
    for phase in ("portrait", "landscape", "long-frame", "flat"):
        parts = out[phase].split()
        assert int(parts[1]) > 0, f"{phase}: nothing drawn"
        assert parts[3] == "0", f"{phase}: {out[phase]}"
        if name in CONTAINED:
            assert parts[5] == "0", f"{phase}: left the screen: {out[phase]}"


@pytest.mark.parametrize("density", [0, 2])
@pytest.mark.parametrize("name", EFFECTS)
def test_effect_stays_finite(sim, name, density):
    # Snow, rain and the glow particles are born outside and drift through: only finiteness
    check_phases(kv(sim(name, density, 60 if name in CONTAINED else 1e9).stdout), name)


@pytest.mark.parametrize("name", EFFECTS)
def test_zero_time_step(sim, name):
    check_phases(kv(sim(name, 1, 60 if name in CONTAINED else 1e9, "zero").stdout), name)


@pytest.mark.parametrize("name", sorted(CONTAINED))
def test_contained_effects_rest_and_wake(sim, name):
    out = kv(sim(name, 0, 60, "nozero", 30 * 60).stdout)
    assert out["rest"] == "1 still=0 woken=1"


def test_slight_tilt_wakes_balls(sim):
    # Half a degree of tilt (0.09 m/s²) sets resting balls rolling again
    out = kv(sim("balls", 0, 60, "nozero", 30 * 60, 0.09).stdout)
    assert out["rest"] == "1 still=0 woken=1"


def test_unknown_effect(sim):
    assert sim("fog", 0, 0).stdout.strip() == "missing"
