# The packages' own check() tests, run by the central runner too: test-dnsfilter.c,
# test-pinguard.c, test-pogo-port.c and the plinius-pwrate levels of plinius-security's check().
# Compiled for the host into tmp_path, run against state in tmp_path only.

import subprocess

import pytest

from conftest import PKG
from dsec_helpers import CDIR, build, san_env

DNSFILTER = PKG / "plinius-dnsfilter"
PINGUARD = PKG / "plinius-pinguard"
USB = PKG / "plinius-usb"
SECURITY = PKG / "plinius-security"

# As _cflags() in plinius-dnsfilter's PKGBUILD
DNSFILTER_CFLAGS = ["-std=gnu11", "-Wall", "-Wextra", "-Wno-unused-parameter",
                    "-Wno-missing-field-initializers", "-Wno-type-limits"]


def test_dnsfilter_package_test(tmp_path):
    exe = build(tmp_path, "test-dnsfilter",
                [DNSFILTER / f for f in ("test-dnsfilter.c", "appid.c", "dnsmsg.c", "filter.c")],
                flags=DNSFILTER_CFLAGS, pkgs=["gio-2.0"])
    # g_dir_make_tmp() honours TMPDIR: its files stay in tmp_path
    r = subprocess.run([str(exe)], cwd=tmp_path, capture_output=True, text=True, timeout=60,
                       env=san_env(TMPDIR=tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.slow
def test_pinguard_package_test(tmp_path):
    # The daemon as in build(), its syslog lines to stderr instead of the journal
    daemon = build(tmp_path, "plinius-pinguard", [PINGUARD / "plinius-pinguard.c"],
                   flags=["-include", str(CDIR / "dsec_nolog.h"), "-lcrypt"])
    test = build(tmp_path, "test-pinguard", [PINGUARD / "test-pinguard.c"])
    work = tmp_path / "test"
    work.mkdir()
    r = subprocess.run([str(test), str(daemon), str(work)], cwd=tmp_path, capture_output=True,
                       text=True, timeout=60, env=san_env())
    assert r.returncode == 0, r.stdout + r.stderr
    assert "all checks passed" in r.stdout


def test_pogo_port_package_test(tmp_path):
    exe = build(tmp_path, "test-pogo-port", [USB / "test-pogo-port.c"],
                flags=["-Wextra", f"-I{USB}"])
    r = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30, env=san_env())
    assert r.returncode == 0, r.stdout + r.stderr
    assert "All port decisions are as agreed" in r.stdout


@pytest.fixture(scope="module")
def pwrate(tmp_path_factory):
    return build(tmp_path_factory.mktemp("pwrate"), "plinius-pwrate",
                 [SECURITY / "plinius-pwrate.c"], flags=["-Wextra", "-lm"])


def rate(exe, password):
    r = subprocess.run([str(exe)], input=password + "\n", capture_output=True, text=True,
                       timeout=30, env=san_env())
    assert r.returncode == 0, r.stderr
    return dict(line.split("=", 1) for line in r.stdout.splitlines())


# The levels plinius-security's check() asserts (the Device Security page relies on them)
@pytest.mark.parametrize("password,level", [
    ("Tr0ub4dor&3", "weak"),
    ("kX9#mQ2$vL7!", "good"),
    ("qwmzktbxplrv", "fair"),
])
def test_pwrate_package_levels(pwrate, password, level):
    out = rate(pwrate, password)
    if password == "Tr0ub4dor&3" and out["dictionary"] == "unchecked":
        pytest.skip("cracklib (libcrack.so.2) missing on the host: the dictionary verdict decides")
    assert out["level"] == level
