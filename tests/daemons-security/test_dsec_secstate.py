# plinius-security: the password strength estimate (pwrate.h, shared by plinius-pwrate and the
# PAM module) and pam_plinius_secstate, which notes the strength of a new screen lock PIN for the
# Device Security page. The module runs in c/dsec_secstate_harness.c with its state directory
# moved into tmp_path (any other path aborts the harness) and fake shadow/user entries.

import math
import os
import stat
import subprocess

import pytest

from dsec_helpers import CDIR, build, san_env

PAM_UPDATE_AUTHTOK = 0x2000
PAM_PRELIM_CHECK = 0x4000


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    return build(tmp_path_factory.mktemp("secstate"), "secstate-harness",
                 [CDIR / "dsec_secstate_harness.c"], flags=["-lcrypt", "-lm"])


def rate(harness, *passwords):
    data = b"".join(p + b"\n" for p in passwords)
    r = subprocess.run([str(harness), "--rate"], input=data, capture_output=True, timeout=30,
                       env=san_env())
    assert r.returncode == 0, r.stderr
    return [tuple(line.split()) for line in r.stdout.decode().splitlines()]


L10, L26, L36, L95 = math.log2(10), math.log2(26), math.log2(36), math.log2(95)


@pytest.mark.parametrize("password,bits,length,digits", [
    (b"", 0, 0, 0),
    # A run counts from its second step on: 1 + 1 + 4 quarters
    (b"123456", 3 * L10, 6, 1),
    (b"654321", 3 * L10, 6, 1),
    (b"000000", 2.25 * L10, 6, 1),
    (b"135790", 6 * L10, 6, 1),           # steps of 2 are no run
    (b"112233", 3.75 * L10, 6, 1),
    (b"abc", 2.25 * L26, 3, 0),
    (b"abd", 3 * L26, 3, 0),
    (b"Aa1!", 4 * L95, 4, 0),
    (b"a1", 2 * L36, 2, 0),
    (b"\xc3\xa4", 1 * 6, 1, 0),           # one character, not two bytes
    (b"\xc3\xa4\xc3\xa4", 1.25 * 6, 2, 0),
    (b"\xff", 6, 1, 0),                   # invalid bytes count one each
    (b"\xe2\x82", 2 * 6, 2, 0),           # cut short at the end: no read past it
    (b"\xf0", 6, 1, 0),
    (b"\xf0\x9f\x98", 3 * 6, 3, 0),
    (b"\xf0\x9f\x98\x80", 6, 1, 0),
    (b"1\xc3", 2 * math.log2(74), 2, 0),
])
def test_estimate(harness, password, bits, length, digits):
    (b, l, d), = rate(harness, password)
    assert float(b) == pytest.approx(bits, abs=0.01)
    assert (int(l), int(d)) == (length, digits)


def test_six_digit_pin_reaches_the_lock_level(harness):
    # 6 * log2(10) = 19.93 must reach PWRATE_LOCK_GOOD (19.9)
    (b, _, _), = rate(harness, b"135790")
    assert float(b) >= 19.9


def test_long_input(harness):
    out = rate(harness, b"x" * 4000, bytes(range(0x80, 0x100)) * 10)
    assert len(out) == 2


# ---- The PAM module ----

def chauthtok(harness, tmp_path, user="alice", token="135790", current="135790",
              flags=PAM_UPDATE_AUTHTOK, **env):
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    e = san_env(DSEC_ROOT=root, **env)
    if current is not None:
        e["DSEC_CURRENT"] = current
    tok = token if token in ("-", "=") else token.encode().hex()
    r = subprocess.run([str(harness), user, tok, str(flags)],
                       capture_output=True, text=True, timeout=30, env=e,
                       preexec_fn=lambda: os.umask(0o077))
    assert r.returncode == 0, r.stderr
    return r.stdout.splitlines(), root


def rating(root, user="alice"):
    f = root / "lock" / user
    return dict(l.split("=", 1) for l in f.read_text().splitlines()) if f.exists() else None


@pytest.mark.parametrize("token,level,kind", [
    ("135790", "good", "pin"),
    ("123456", "good", "pin"),          # six digits are good on the lock screen by design
    ("12345", "weak", "pin"),
    ("1234", "weak", "pin"),
    ("12345a", "weak", "password"),
    ("correct horse", "good", "password"),
    ("Aa1!", "good", "password"),
    ("aaaaaaaa", "weak", "password"),
])
def test_rating_of_a_new_pin(harness, tmp_path, token, level, kind):
    out, root = chauthtok(harness, tmp_path, token=token, current=token)
    assert out[-1] == "rc IGNORE"      # never in the way of the change itself
    r = rating(root)
    assert (r["level"], r["kind"]) == (level, kind)
    assert set(r) == {"level", "kind", "rated"}
    # Neither the PIN nor its length is stored
    assert token not in (root / "lock" / "alice").read_text()


def test_file_modes_do_not_follow_the_umask(harness, tmp_path):
    out, root = chauthtok(harness, tmp_path)
    assert stat.S_IMODE(root.stat().st_mode) == 0o755
    assert stat.S_IMODE((root / "lock").stat().st_mode) == 0o755
    assert stat.S_IMODE((root / "lock" / "alice").stat().st_mode) == 0o640
    # root:<the user's group>, so only the user (and root) reads it
    assert "chown 0:1001" in out
    assert not (root / "lock" / "alice.tmp").exists()


def test_only_the_update_phase_rates(harness, tmp_path):
    for flags in (0, PAM_PRELIM_CHECK):
        out, root = chauthtok(harness, tmp_path, flags=flags)
        assert out[-1] == "rc IGNORE" and rating(root) is None


def test_refused_change_keeps_the_old_rating(harness, tmp_path):
    # An earlier module refused the new PIN: shadow still holds the old one
    chauthtok(harness, tmp_path, token="135790", current="135790")
    out, root = chauthtok(harness, tmp_path, token="1", current="135790")
    assert out[-1] == "rc IGNORE"
    assert rating(root)["level"] == "good"


def test_no_shadow_entry_no_rating(harness, tmp_path):
    out, root = chauthtok(harness, tmp_path, current=None)
    assert rating(root) is None


@pytest.mark.parametrize("user", ["../alice", "a/b", ".alice", "..", ".", "ghost", "/alice"])
def test_odd_user_names_write_nothing(harness, tmp_path, user):
    out, root = chauthtok(harness, tmp_path, user=user)
    assert out[-1] == "rc IGNORE"
    written = [p for p in root.rglob("*") if p.is_file()]
    assert written == []


def test_unknown_user_from_pam(harness, tmp_path):
    out, root = chauthtok(harness, tmp_path, user="-")
    assert out[-1] == "rc IGNORE" and not (root / "lock").exists()


def test_planted_link_is_not_followed(harness, tmp_path):
    root = tmp_path / "root"
    (root / "lock").mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.write_text("keep\n")
    (root / "lock" / "alice.tmp").symlink_to(victim)
    out, _ = chauthtok(harness, tmp_path)
    assert victim.read_text() == "keep\n"
    assert rating(root) is None


def test_failed_chown_leaves_no_file(harness, tmp_path):
    out, root = chauthtok(harness, tmp_path, DSEC_CHOWN_FAILS="1")
    assert rating(root) is None
    assert not (root / "lock" / "alice.tmp").exists()
    assert any(l.startswith("log cannot set the mode") for l in out)


def test_no_new_token_no_rating(harness, tmp_path):
    out, root = chauthtok(harness, tmp_path, token="-")
    assert out[-1] == "rc IGNORE" and rating(root) is None


def test_failed_rename_keeps_nothing_half_written(harness, tmp_path):
    root = tmp_path / "root"
    # The rating's place is taken by a directory
    (root / "lock" / "alice" / "x").mkdir(parents=True)
    out, _ = chauthtok(harness, tmp_path)
    assert out[-1] == "rc IGNORE"
    assert any(l.startswith(f"log cannot write {'/var/lib/plinius-security'}/lock/alice") for l in out)
    assert not (root / "lock" / "alice.tmp").exists()


def test_no_memory_for_the_file_writes_no_rating(harness, tmp_path):
    out, root = chauthtok(harness, tmp_path, DSEC_FDOPEN_FAILS="1")
    assert out[-1] == "rc IGNORE"
    assert rating(root) is None


def test_no_memory_for_the_file_leaves_no_temporary_file(harness, tmp_path):
    out, root = chauthtok(harness, tmp_path, DSEC_FDOPEN_FAILS="1")
    assert not (root / "lock" / "alice.tmp").exists()
    assert sorted(p.name for p in (root / "lock").iterdir()) == []
    assert any(l.startswith("log cannot write /var/lib/plinius-security/lock/alice.tmp") for l in out)
