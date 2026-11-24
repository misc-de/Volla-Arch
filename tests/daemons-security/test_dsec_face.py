# plinius-face: the decisions of the face unlock service (plinius-faced.c through
# c/dsec_face_harness.c), without camera and NPU: which frames count, the series of matches that
# unlocks, the median of Verify, enrollment, the failure limit and when the face may unlock at
# all, the stored templates, and which D-Bus methods need polkit. Frames are scripted; the
# state directories live in tmp_path.

import struct

import pytest

from conftest import PKG
from dsec_helpers import CDIR, build, run_harness

FACE = PKG / "plinius-face"
UNLOCK = "calls org.freedesktop.login1 UnlockSession"
POLKIT = "calls org.freedesktop.PolicyKit1 CheckAuthorization"
NO_CALLS = "calls -"


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    return build(tmp_path_factory.mktemp("face"), "face-harness", [CDIR / "dsec_face_harness.c"],
                 flags=[f"-I{CDIR / 'fake-include'}", f"-I{FACE}", "-Wno-unused-parameter",
                        "-lm"],
                 pkgs=["gio-unix-2.0"])


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "root"
    (r / "state").mkdir(parents=True)
    (r / "run").mkdir()
    return r


def run(harness, root, *cmds, **env):
    out = run_harness(harness, "\n".join(cmds) + "\n", DSEC_ROOT=root, **env)
    assert len(out) == len(cmds), out
    return out


def enrolled(enabled=1, strict=1, uid=1001, templates=3):
    return [f"settings {uid} {enabled} {strict}", f"templates {uid} {templates}"]


# ---- Which frames count ----

@pytest.mark.parametrize("spec,hint", [
    ("s:0.7", "good"),
    ("n", "no-face"),
    ("u", "unclear"),
    ("f", "too-far"),
    ("t", "tilted"),
    ("raw:0.9:0.9:340:500", "good"),        # the limits themselves are good enough
    ("raw:0.89:0.95:400:500", "unclear"),
    ("raw:0.95:0.89:400:500", "unclear"),
    ("raw:0.95:0.95:339:500", "too-far"),
    ("raw:0.95:0.95:395:533", "good"),      # atan2(33, 95) = 0.334 rad, about 19°
    ("raw:0.95:0.95:390:540", "tilted"),
])
def test_check_frame(harness, root, spec, hint):
    assert run(harness, root, f"check {spec}") == [hint]


# ---- Unlocking ----

def unlock(harness, root, frames, *, setup=None, watch="1001 1 1 0"):
    cmds = (setup if setup is not None else enrolled()) + [f"watch s1 {watch}",
                                                            "frames " + " ".join(frames),
                                                            "unlock 1001 s1", "show s1"]
    return run(harness, root, *cmds)[-2:]


@pytest.mark.parametrize("frames,unlocks,failures", [
    (["s:0.7"] * 3, True, 0),
    (["s:0.6"] * 3, True, 0),                               # the strict threshold itself
    (["s:0.59"] * 3, False, 1),
    (["s:0.7", "s:0.7", "n", "s:0.7", "s:0.7"], False, 1),  # the series starts again
    (["s:0.7", "s:0.7", "s:0.2", "s:0.7", "s:0.7"], False, 1),
    (["s:0.7", "s:0.7", "u", "s:0.7", "s:0.7", "s:0.7"], True, 0),
    (["s:0.2", "s:0.7", "s:0.7", "s:0.7"], True, 0),
    (["n", "n", "n"], False, 0),                            # no face seen: no failed try
    (["u", "f", "t"], False, 0),
    (["e"], False, 0),
    (["c"], False, 0),
    ([], False, 0),
])
def test_unlock_series(harness, root, frames, unlocks, failures):
    calls, show = unlock(harness, root, frames)
    assert calls == (UNLOCK if unlocks else NO_CALLS)
    locked, pin_seen, by_face, fails = show.split()
    assert by_face == ("1" if unlocks else "0")
    assert int(fails) == failures


@pytest.mark.parametrize("strict,sim,unlocks", [(1, 0.55, False), (0, 0.55, True),
                                                (0, 0.49, False)])
def test_strictness(harness, root, strict, sim, unlocks):
    calls, _ = unlock(harness, root, [f"s:{sim}"] * 3, setup=enrolled(strict=strict))
    assert calls == (UNLOCK if unlocks else NO_CALLS)


@pytest.mark.parametrize("setup", [enrolled(enabled=0), ["settings 1001 1 1"], []])
def test_no_unlock_without_face_unlock_switched_on_and_a_face(harness, root, setup):
    calls, show = unlock(harness, root, ["s:0.99"] * 5, setup=setup)
    assert calls == NO_CALLS
    assert show == "1 1 0 0"


def test_no_unlock_of_a_session_unlocked_meanwhile(harness, root):
    calls, show = unlock(harness, root, ["s:0.9"] * 3, watch="1001 0 1 0")
    assert calls == NO_CALLS and show == "0 1 0 0"


def test_job_for_another_session_changes_nothing(harness, root):
    out = run(harness, root, *enrolled(), "watch s1 1001 1 1 0", "frames s:0.1 s:0.1 s:0.1",
              "unlock 1001 other", "show s1")
    assert out[-2:] == [NO_CALLS, "1 1 0 0"]


def test_try_unlock_needs_screen_lock_pin_and_tries_left(harness, root):
    base = enrolled()
    # Screen off: never
    assert run(harness, root, *base, "watch s1 1001 1 1 0", "frames s:0.9 s:0.9 s:0.9",
               "try")[-1] == NO_CALLS
    # Screen on, locked, a PIN unlock seen since start, tries left: yes
    assert run(harness, root, *base, "watch s1 1001 1 1 0", "screen 1",
               "frames s:0.9 s:0.9 s:0.9", "try")[-1] == UNLOCK
    # No PIN unlock since the service started (e.g. right after boot): no
    assert run(harness, root, *base, "watch s1 1001 1 0 0", "screen 1",
               "frames s:0.9 s:0.9 s:0.9", "try")[-1] == NO_CALLS
    # Five failed tries: no more until the next PIN unlock
    assert run(harness, root, *base, "watch s1 1001 1 1 5", "screen 1",
               "frames s:0.9 s:0.9 s:0.9", "try")[-1] == NO_CALLS
    assert run(harness, root, *base, "watch s1 1001 1 1 4", "screen 1",
               "frames s:0.9 s:0.9 s:0.9", "try")[-1] == UNLOCK
    # Not locked: nothing to do
    assert run(harness, root, *base, "watch s1 1001 0 1 0", "screen 1",
               "frames s:0.9 s:0.9 s:0.9", "try")[-1] == NO_CALLS


def test_failures_add_up_to_the_limit(harness, root):
    cmds = [*enrolled(), "watch s1 1001 1 1 0", "screen 1"]
    for _ in range(6):
        cmds += ["frames s:0.1 s:0.1 s:0.1", "try"]
    cmds += ["show s1", "wants s1"]
    out = run(harness, root, *cmds)
    assert out[-2:] == ["1 1 0 5", "no"]


def test_pin_unlock_resets_and_face_unlock_does_not(harness, root):
    out = run(harness, root, *enrolled(), "watch s1 1001 1 1 3", "screen 1",
              "frames s:0.9 s:0.9 s:0.9", "try", "lockhint s1 0", "show s1",
              # The face's own unlock does not count as one with the PIN
              "lockhint s1 1", "lockhint s1 0", "show s1")
    assert out[-6] == UNLOCK
    assert out[-4] == "0 1 0 3"
    assert out[-1] == "0 1 0 0"


def test_pin_unlock_enables_the_face_after_boot(harness, root):
    out = run(harness, root, *enrolled(), "watch s1 1001 1 0 0", "wants s1", "lockhint s1 0",
              "lockhint s1 1", "wants s1", "show s1")
    assert out[-5] == "no"
    assert out[-2:] == ["yes", "1 1 0 0"]


# ---- Verify ----

def verify(harness, root, sims, setup=None, **env):
    frames = " ".join(s if isinstance(s, str) else f"s:{s}" for s in sims)
    return run(harness, root, *(enrolled() if setup is None else setup), f"frames {frames}",
               "verify 1001", **env)[-1]


def parse_verify(out):
    assert out.startswith("ok (")
    ok, sim = out[4:-1].split(", ")
    return ok == "true", float(sim)


@pytest.mark.parametrize("sims,recognized,median", [
    ([0.9, 0.1, 0.1, 0.9, 0.9], True, 0.9),
    ([0.9, 0.9, 0.1, 0.1, 0.1], False, 0.1),
    ([0.99, 0.3, 0.3, 0.3, 0.3], False, 0.3),     # one lucky frame does not decide
    ([0.61, 0.61, 0.61, 0.61, 0.61], True, 0.61),
    ([0.7, "n", "u", 0.7, 0.1], True, 0.7),       # only good frames count, median of three
    ([0.7, 0.1, 0.1], False, 0.1),                # time ran out after three good frames
    ([0.7, 0.1], False, 0.1),                     # two good frames: the worse one
])
def test_verify_takes_the_median(harness, root, sims, recognized, median):
    ok, sim = parse_verify(verify(harness, root, sims))
    assert ok == recognized
    assert sim == pytest.approx(median, abs=1e-5)


def test_verify_errors(harness, root):
    assert verify(harness, root, [0.9] * 5, setup=["settings 1001 1 1"]) == (
        "error org.plinius.Face.Error.Failed: no face enrolled")
    assert verify(harness, root, ["n", "n"]) == (
        "error org.plinius.Face.Error.Failed: no clear picture of a face")
    assert verify(harness, root, [0.9] * 5, DSEC_NO_CAMERA="1") == (
        "error org.plinius.Face.Error.Failed: camera: no camera here")
    assert verify(harness, root, ["e"]) == "error org.plinius.Face.Error.Failed: recognition failed"


# ---- Enrolling ----

def test_enroll_stores_mean_and_spread(harness, root):
    out = run(harness, root, "frames " + " ".join(["s:1.0"] * 30), "enroll 1001", "load 1001")
    assert out[1].startswith("signals ")
    # The mean (normalized) and every third of the 30 frames
    assert out[2] == "templates 11 enabled 0 strict 1 norm 1.000"


def test_enroll_needs_thirty_clear_frames(harness, root):
    out = run(harness, root, "frames " + " ".join(["s:1.0"] * 29 + ["n"] * 5), "enroll 1001",
              "load 1001")
    assert out[2] == "templates 0 enabled 0 strict 1 norm 0.000"


# ---- Stored templates and settings ----

def templates_file(count, n=None, magic=b"PLFACE1\0", extra=b""):
    n = count if n is None else n
    return magic + struct.pack("<I", count) + struct.pack("<f", 1.0) * (128 * n) + extra


@pytest.mark.parametrize("data,count", [
    (templates_file(1), 1),
    (templates_file(16), 16),
    (templates_file(0), 0),
    (templates_file(17), 0),
    (templates_file(0xffffffff, 1), 0),
    (templates_file(2, 1), 0),
    (templates_file(1, extra=b"x"), 0),
    (templates_file(1)[:-1], 0),
    (templates_file(1, magic=b"PLFACE2\0"), 0),
    (b"", 0),
    (b"PLFACE1", 0),
])
def test_template_file_checks(harness, root, data, count):
    (root / "state" / "1001.emb").write_bytes(data)
    assert run(harness, root, "load 1001")[0].startswith(f"templates {count} ")


@pytest.mark.parametrize("conf,enabled,strict", [
    ("[face]\nenabled=true\nstrictness=balanced\n", 1, 0),
    ("[face]\nenabled=true\nstrictness=strict\n", 1, 1),
    ("[face]\nenabled=true\nstrictness=Balanced\n", 1, 1),   # anything else is strict
    ("[face]\nenabled=true\nstrictness=\n", 1, 1),
    ("[face]\nenabled=yes\n", 0, 1),
    ("garbage", 0, 1),
    ("", 0, 1),
])
def test_settings_file(harness, root, conf, enabled, strict):
    (root / "state" / "1001.conf").write_text(conf)
    out, = run(harness, root, "load 1001")
    assert f" enabled {enabled} strict {strict} " in out


def test_files_are_private(harness, root):
    run(harness, root, *enrolled())
    for name in ("1001.conf", "1001.emb"):
        assert (root / "state" / name).stat().st_mode & 0o777 == 0o600


# ---- What survives the re-exec ----

def test_restore_watch(harness, root):
    (root / "run" / "sessions").write_text("s1 1 3\ns2 0 0\ngarbage\n" + "x" * 80 + " 1 1\n")
    out = run(harness, root, "restore s1", "restore s2", "restore s9", "restore " + "x" * 80)
    # A session the old process did not know waits for a PIN: the safe side
    assert out == ["1 3", "0 0", "0 0", "0 0"]


def test_sessions_round_trip(harness, root):
    out = run(harness, root, "watch s1 1001 1 1 2", "watch s2 1002 0 0 0", "save",
              "restore s1", "restore s2")
    assert out[-2:] == ["1 2", "0 0"]
    assert (root / "run" / "sessions").stat().st_mode & 0o777 == 0o600


# ---- Who may do what (D-Bus) ----

@pytest.mark.parametrize("method,arg,polkit", [
    ("Enroll", "", True),
    ("SetEnabled", "true", True),        # switching on asks for the PIN
    ("SetStrictness", "balanced", True), # loosening asks for the PIN
    ("SetEnabled", "false", False),      # taking rights away needs nothing
    ("SetStrictness", "strict", False),
    ("Delete", "", False),
    ("Cancel", "", False),
    ("GetUser", "", False),
])
def test_which_methods_need_polkit(harness, root, method, arg, polkit):
    out = run(harness, root, *enrolled(enabled=0, strict=1), f"method {method} {arg}".strip(),
              "calls")
    if polkit:
        assert out[-2:] == ["pending", POLKIT]
    else:
        assert out[-2].startswith("ok") and out[-1] == NO_CALLS


def test_settings_unchanged_until_polkit_says_yes(harness, root):
    out = run(harness, root, *enrolled(enabled=0, strict=1), "method SetEnabled true",
              "method SetStrictness balanced", "load 1001")
    assert out[-1] == "templates 3 enabled 0 strict 1 norm 1.000"


@pytest.mark.parametrize("value", ["loose", "Balanced", "", "balanced.", "strict,balanced"])
def test_strictness_values(harness, root, value):
    out = run(harness, root, *enrolled(), f"method SetStrictness {value}".rstrip(), "calls")
    # Never asks polkit and never loosens: refused right away
    assert out[-2].startswith("error org.plinius.Face.Error.Invalid")
    assert out[-1] == NO_CALLS


def test_unknown_caller_is_denied(harness, root):
    out = run(harness, root, "bus caller -1", "method GetUser", "method Delete", "calls")
    assert out[1:] == ["error org.plinius.Face.Error.Denied: unknown caller"] * 2 + [NO_CALLS]


def test_each_user_has_their_own_face(harness, root):
    out = run(harness, root, *enrolled(uid=1001), "bus caller 1002", "method GetUser",
              "method Delete", "load 1001", "bus caller 1001", "method GetUser")
    assert out[-5] == "ok (false, false, 'strict')"
    assert out[-3] == "templates 3 enabled 1 strict 1 norm 1.000"
    assert out[-1] == "ok (true, true, 'strict')"


def test_switching_off_and_deleting(harness, root):
    out = run(harness, root, *enrolled(), "method SetEnabled false", "load 1001",
              "method Delete", "load 1001")
    assert out[-3] == "templates 3 enabled 0 strict 1 norm 1.000"
    assert out[-1] == "templates 0 enabled 0 strict 1 norm 0.000"


def test_verify_over_the_bus(harness, root):
    out = run(harness, root, *enrolled(), "frames " + " ".join(["s:0.8"] * 5), "method Verify")
    assert out[-1].startswith("ok (true, 0.8")
