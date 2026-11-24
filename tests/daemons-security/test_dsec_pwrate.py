# plinius-pwrate: the rating of the encryption password for the Device Security page (level from
# pwrate.h's estimate, "weak" for a dictionary word, the word behind common letter swaps), with
# the cracklib it loads at run time replaced by a stand-in (c/dsec_fakecrack.c) or missing, and
# once with the host's cracklib as the package's check() runs it.

import ctypes.util
import subprocess

import pytest

from dsec_helpers import CDIR, build, san_env


@pytest.fixture(scope="module")
def binaries(tmp_path_factory):
    d = tmp_path_factory.mktemp("pwrate")
    return {
        "tool": build(d, "pwrate-tool", [CDIR / "dsec_pwrate_harness.c"], flags=["-ldl", "-lm"]),
        # Libraries for dlopen: no sanitizer runtime of their own, no coverage counters
        "crack": build(d, "libfakecrack.so", [CDIR / "dsec_fakecrack.c"],
                       flags=["-shared", "-fPIC"], sanitize=False),
        "broken": build(d, "libbrokencrack.so", [CDIR / "dsec_fakecrack.c"],
                        flags=["-shared", "-fPIC", "-DNO_DICT_FUNC"], sanitize=False),
    }


def rate(binaries, password, lib="crack", args=(), **env):
    if lib in ("crack", "broken"):
        env["DSEC_LIBCRACK"] = binaries[lib]
    elif lib == "none":
        env["DSEC_LIBCRACK"] = "none"
    r = subprocess.run([str(binaries["tool"]), *args], input=password, capture_output=True,
                       timeout=60, env=san_env(**env))
    if r.returncode != 0:
        return r.returncode, r.stderr.decode()
    return 0, dict(l.split("=", 1) for l in r.stdout.decode().splitlines())


@pytest.mark.parametrize("password,level,bits", [
    (b"kX9#mQ2$vL7!\n", "good", "75"),        # 12 * log2(95) = 78.8, rounded down to 5
    (b"qwmzktbxplrv\n", "fair", "55"),        # 12 * log2(26) = 56.4
    (b"123456\n", "weak", "5"),             # a run: 3 * log2(10) = 9.97
    (b"kX9#mQ2$vL7!", "good", "75"),           # no newline at the end
])
def test_level_from_the_estimate(binaries, password, level, bits):
    rc, out = rate(binaries, password)
    assert rc == 0
    assert (out["level"], out["bits"], out["dictionary"]) == (level, bits, "checked")
    assert set(out) == {"level", "bits", "dictionary", "rated"}
    assert int(out["rated"]) > 1700000000


def test_dictionary_word_is_weak_however_long(binaries):
    rc, out = rate(binaries, b"Correcthorsebatterystaple\n",
                   DSEC_CRACK_WORDS="correcthorsebatterystaple")
    assert (out["level"], out["dictionary"]) == ("weak", "word")


@pytest.mark.parametrize("password,plain", [
    (b"Tr0ub4dor&3", "Troubador"),             # swaps undone, the "&3" at the end cut off
    (b"P@ssw0rd2026!", "Password"),
    (b"$1lv3r|+", "silver"),                   # $->s 1->i 3->e; the "|+" at the end cut off
    (b"5+|ck", "stlck"),                        # +->t |->l in the middle are swaps
    (b"2026", ""),                             # nothing but digits: an empty word
])
def test_word_behind_letter_swaps(binaries, tmp_path, password, plain):
    log = tmp_path / "asked"
    rc, out = rate(binaries, password + b"\n", DSEC_CRACK_WORDS=plain or "-",
                   DSEC_CRACK_LOG=log)
    asked = log.read_text().splitlines()
    # First as typed, then the plain word, both against cracklib's default dictionary
    assert asked == [f"{password.decode()} /fake/pw_dict", f"{plain} /fake/pw_dict"]
    assert out["dictionary"] == ("word" if plain else "checked")


def test_password_itself_is_not_asked_again_when_already_a_word(binaries, tmp_path):
    log = tmp_path / "asked"
    rc, out = rate(binaries, b"monkey\n", DSEC_CRACK_WORDS="monkey", DSEC_CRACK_LOG=log)
    assert out["dictionary"] == "word"
    assert log.read_text().splitlines() == ["monkey /fake/pw_dict"]


@pytest.mark.parametrize("lib", ["none", "broken"])
def test_without_a_usable_cracklib(binaries, lib):
    # The initramfs has no cracklib: rated by the estimate alone, and said so
    rc, out = rate(binaries, b"kX9#mQ2$vL7!\n", lib=lib)
    assert (out["level"], out["dictionary"]) == ("good", "unchecked")


def test_nothing_on_stdin(binaries):
    rc, err = rate(binaries, b"")
    assert rc == 2 and "No password on stdin" in err


def test_usage(binaries):
    rc, err = rate(binaries, b"x\n", args=["extra"])
    assert rc == 2 and "Usage: plinius-pwrate" in err


def test_password_is_not_printed(binaries):
    rc, out = rate(binaries, b"kX9#mQ2$vL7!\n")
    assert "kX9#mQ2$vL7!" not in "".join(f"{k}={v}" for k, v in out.items())


@pytest.mark.skipif(ctypes.util.find_library("crack") is None, reason="no cracklib on the host")
@pytest.mark.parametrize("password,level", [
    # The package's check() (PKGBUILD), with the host's cracklib
    (b"Tr0ub4dor&3\n", "weak"),
    (b"kX9#mQ2$vL7!\n", "good"),
    (b"qwmzktbxplrv\n", "fair"),
])
def test_package_check_with_the_real_cracklib(binaries, password, level):
    rc, out = rate(binaries, password, lib="host")
    assert rc == 0 and out["level"] == level
    assert out["dictionary"] in ("word", "checked")
