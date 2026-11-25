# phoc-plinius as built (pinned phoc + wlroots, our patches, from the source cache): the doze
# preparation of wlroots' DRM backend (patches 0015 and 0039). MediaTek's driver skips the planes
# and the page-flip event of a modeset that reaches it with DOZE_ACTIVE set; wlroots then waited
# for that event forever and the always-on display froze. A modeset must therefore leave doze
# first and never enter it, and fail when the panel cannot leave it.
#
# drm_connector_doze_prepare() and drm_connector_doze_stop_idle() are taken from the patched
# drm.c and built with stand-ins for the connector, the timer and the doze commit, which prints
# every commit it is asked for.

import re
import shutil
import subprocess

import pytest

from conftest import compile_c
from prepare_helper import SRCDEST, Unavailable, run_prepare

pytestmark = pytest.mark.skipif(not SRCDEST.is_dir(), reason="no source cache")

FUNCTIONS = ("drm_connector_doze_stop_idle", "drm_connector_doze_prepare")

HARNESS = r"""
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

enum { WLR_ERROR = 1, WLR_INFO = 2, WLR_DEBUG = 3 };
struct wl_event_source { int unused; };
struct wlr_drm_crtc { struct { uint32_t doze_active; } props; };
struct wlr_drm_connector {
	struct wlr_drm_crtc *crtc;
	struct { bool enabled; } output;
	bool doze_pending, doze, doze_suspended;
	struct wl_event_source *doze_idle_timer;
};
struct wlr_drm_connector_state { bool active; bool doze; };

#define wlr_drm_conn_log(conn, level, fmt, ...) \
	printf("log %d " fmt "\n", level, ##__VA_ARGS__)

static unsigned commits, fail_mask;

static bool drm_atomic_doze_commit(struct wlr_drm_connector *conn, bool active, bool doze) {
	bool ok = !(fail_mask & (1u << commits++));
	printf("commit active=%d doze=%d %s\n", active, doze, ok ? "ok" : "failed");
	return ok;
}

static void wl_event_source_timer_update(struct wl_event_source *source, int ms) {
	printf("timer %d\n", ms);
}

@FUNCTIONS@

/* argv: doze suspended state.active state.doze enabled modeset test_only fail_mask has_prop */
int main(int argc, char **argv) {
	if (argc != 10) {
		return 2;
	}
	static struct wl_event_source timer;
	struct wlr_drm_crtc crtc = { .props = { .doze_active = atoi(argv[9]) ? 42 : 0 } };
	struct wlr_drm_connector conn = {
		.crtc = &crtc,
		.output = { .enabled = atoi(argv[5]) },
		.doze = atoi(argv[1]),
		.doze_suspended = atoi(argv[2]),
		.doze_idle_timer = &timer,
	};
	struct wlr_drm_connector_state state = { .active = atoi(argv[3]), .doze = atoi(argv[4]) };
	fail_mask = (unsigned)atoi(argv[8]);
	bool ok = drm_connector_doze_prepare(&conn, &state, atoi(argv[6]), atoi(argv[7]));
	printf("ret=%d doze=%d suspended=%d\n", ok, conn.doze, conn.doze_suspended);
	return 0;
}
"""


def extract(source, name):
    """The definition of the static function NAME, from its signature to its closing brace."""
    m = re.search(r"^static \w+ " + name + r"\(", source, re.M)
    assert m, f"{name} not in drm.c"
    depth, i = 0, source.index("{", m.end())
    for j in range(i, len(source)):
        depth += {"{": 1, "}": -1}.get(source[j], 0)
        if depth == 0:
            return source[m.start():j + 1]
    raise AssertionError(f"{name}: no closing brace")


@pytest.fixture(scope="module")
def drm_c(tmp_path_factory):
    work = tmp_path_factory.mktemp("phoc-doze")
    try:
        r = run_prepare("phoc-plinius", work)
    except Unavailable as e:
        shutil.rmtree(work, ignore_errors=True)
        pytest.skip(str(e))
    assert r.returncode == 0, r.stdout + r.stderr
    yield (work / "src/phoc/subprojects/wlroots-0.20.x/backend/drm/drm.c").read_text()
    shutil.rmtree(work, ignore_errors=True)


@pytest.fixture(scope="module")
def prepare(drm_c, tmp_path_factory):
    out = tmp_path_factory.mktemp("phoc-doze-bin")
    src = out / "doze_harness.c"
    src.write_text(HARNESS.replace("@FUNCTIONS@", "\n\n".join(extract(drm_c, f)
                                                              for f in FUNCTIONS)))
    exe = compile_c(out / "doze_harness", [src])

    def run(*, doze=0, suspended=0, active=1, want=1, enabled=1, modeset=0, test_only=0,
            fail=0, prop=1):
        args = [doze, suspended, active, want, enabled, modeset, test_only, fail, prop]
        r = subprocess.run([str(exe), *map(str, args)], capture_output=True, text=True,
                           check=True)
        lines = r.stdout.splitlines()
        return [l for l in lines if l.startswith("commit")], lines[-1], r.stdout
    return run


def test_a_modeset_right_after_doze_on_does_not_enter_doze(prepare):
    # The freeze of 18.11.: a new refresh rate 0.1 s after "Doze on with the next commit"
    commits, result, _ = prepare(doze=0, want=1, modeset=1)
    assert commits == []
    assert result == "ret=1 doze=0 suspended=0"


def test_the_next_ordinary_frame_enters_doze(prepare):
    commits, result, _ = prepare(doze=0, want=1, modeset=0)
    assert commits == ["commit active=1 doze=1 ok"]
    assert result == "ret=1 doze=1 suspended=0"


def test_a_dozing_panel_leaves_doze_before_a_modeset(prepare):
    commits, result, out = prepare(doze=1, want=1, modeset=1)
    assert commits == ["commit active=1 doze=0 ok"]
    assert result == "ret=1 doze=0 suspended=0"
    assert "leaves its low-power mode for a modeset" in out
    assert "timer 0" in out  # no doze suspend after the modeset


def test_a_suspended_dozing_panel_comes_on_then_leaves_doze_for_a_modeset(prepare):
    commits, result, _ = prepare(doze=1, suspended=1, want=1, modeset=1)
    assert commits == ["commit active=1 doze=1 ok", "commit active=1 doze=0 ok"]
    assert result == "ret=1 doze=0 suspended=0"


def test_the_modeset_fails_when_the_panel_cannot_leave_doze(prepare):
    commits, result, out = prepare(doze=1, want=1, modeset=1, fail=0b1)
    assert commits == ["commit active=1 doze=0 failed"]
    assert result == "ret=0 doze=1 suspended=0"
    assert "cannot leave its low-power mode for a modeset" in out


def test_the_modeset_fails_when_the_crtc_does_not_come_on_again(prepare):
    commits, result, _ = prepare(doze=1, suspended=1, modeset=1, fail=0b1)
    assert commits == ["commit active=1 doze=1 failed"]
    assert result == "ret=0 doze=1 suspended=1"


def test_an_output_going_off_takes_doze_off_with_its_own_commit(prepare):
    # atomic_connector_add() clears DOZE_ACTIVE in the commit that switches the CRTC off
    commits, result, _ = prepare(doze=1, active=0, want=0, modeset=1)
    assert commits == []
    assert result == "ret=1 doze=1 suspended=0"


def test_a_frame_leaves_doze_as_before(prepare):
    commits, result, _ = prepare(doze=1, want=0, modeset=0)
    assert commits == ["commit active=1 doze=0 ok"]
    assert result == "ret=1 doze=0 suspended=0"


@pytest.mark.parametrize("case", [dict(test_only=1), dict(prop=0)])
def test_nothing_without_a_doze_property_or_for_a_test_commit(prepare, case):
    commits, result, _ = prepare(doze=1, want=1, modeset=1, **case)
    assert commits == []
    assert result == "ret=1 doze=1 suspended=0"


def test_both_commit_paths_pass_the_modeset_and_fail_with_the_preparation(drm_c):
    assert re.search(r"if \(!drm_connector_doze_prepare\(conn, &pending, pending_dev\.modeset, "
                     r"test_only\)\) \{\s*goto out;", drm_c)
    assert re.search(r"if \(!drm_connector_doze_prepare\(conn, conn_state,\s*"
                     r"output_state->base\.allow_reconfiguration, test_only\)\) \{\s*goto out;",
                     drm_c)
    assert drm_c.count("drm_connector_doze_prepare(") == 3  # definition and the two calls
