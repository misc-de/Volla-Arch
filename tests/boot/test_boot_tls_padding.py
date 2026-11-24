# libhybris-tls-padding.so (pkgbuilds/libhybris/hybris-tls-padding.c): preloaded into every
# process, its TLS block must be the first one and cover exactly bionic's slots tp+16..tp+63
# (TLS_SLOT_APP..TLS_SLOT_ART_THREAD_SELF), which Mali's GL driver writes with inline code. The
# check it reports with HYBRIS_TLS_PADDING_DEBUG is tested where it can tell both cases apart: on
# aarch64 (cross compiler and qemu-user, if the host has them), with an executable without TLS
# of its own (the slots are covered) and with one that has some (its block comes first). On
# x86_64 the static TLS lies below the thread pointer, so there it always reports the slots as
# unprotected, at the offset of the first block.

import json
import os
import re
import shutil
import subprocess
import sys

import pytest

from conftest import PKG, TOOLS, compile_c

SRC = PKG / "libhybris" / "hybris-tls-padding.c"
SYSROOT = "/usr/aarch64-linux-gnu"

NO_TLS = "int main(void) { return 0; }\n"
OWN_TLS = "__thread long counter;\nint main(void) { return (int)counter; }\n"


def tls_segment(lib):
    """(memsz, align) of the PT_TLS program header"""
    out = subprocess.run(["readelf", "-lW", str(lib)], capture_output=True, text=True, check=True).stdout
    m = re.search(r"^\s*TLS\s+\S+\s+\S+\s+\S+\s+\S+\s+(0x[0-9a-f]+)\s+\S+\s+(0x[0-9a-f]+)", out, re.M)
    assert m, out
    return int(m.group(1), 16), int(m.group(2), 16)


@pytest.fixture(scope="module")
def aarch64(tmp_path_factory):
    cc = shutil.which("aarch64-linux-gnu-gcc")
    qemu = shutil.which("qemu-aarch64") or shutil.which("qemu-aarch64-static")
    if not cc or not qemu or not os.path.isdir(SYSROOT) or not shutil.which("readelf"):
        pytest.skip("no aarch64 cross compiler, qemu-user, sysroot or readelf on the host")
    d = tmp_path_factory.mktemp("tls-padding-aarch64")
    lib = compile_c(d / "libhybris-tls-padding.so", [SRC], flags=["-shared", "-fPIC"], cc=cc)
    exes = {}
    for name, code in (("plain", NO_TLS), ("own-tls", OWN_TLS)):
        (d / f"{name}.c").write_text(code)
        # The executables are the subject (their TLS layout): never with gcov's counters
        exes[name] = compile_c(d / name, [d / f"{name}.c"], cc=cc, counted=False)

    def run(name, debug=True):
        env = {k: v for k, v in os.environ.items() if k != "HYBRIS_TLS_PADDING_DEBUG"}
        if debug:
            env["HYBRIS_TLS_PADDING_DEBUG"] = "1"
        return subprocess.run([qemu, "-L", SYSROOT, "-E", f"LD_PRELOAD={lib}", str(exes[name])],
                              capture_output=True, text=True, env=env, timeout=60)
    run.lib = lib
    return run


def test_the_block_covers_exactly_the_six_slots(aarch64):
    # Slots 2..7, eight bytes each, 16-byte aligned like bionic's TLS_SLOT_APP
    assert tls_segment(aarch64.lib) == (6 * 8, 16)


def test_slots_reserved_when_preloaded(aarch64):
    r = aarch64("plain")
    assert r.returncode == 0
    assert r.stderr == "hybris-tls-padding: bionic TLS slots 2-7 reserved\n"


def test_executable_with_tls_of_its_own_is_reported(aarch64):
    # Its 8-byte block takes tp+16; ours follows at the next 16-byte boundary
    r = aarch64("own-tls")
    assert r.returncode == 0
    assert r.stderr == ("hybris-tls-padding: slots at offset 32 instead of 16, "
                        "bionic TLS slots are not protected\n")


@pytest.mark.parametrize("name", ["plain", "own-tls"])
def test_silent_without_debug(aarch64, name):
    # Loaded into every process: nothing on stderr unless asked
    r = aarch64(name, debug=False)
    assert r.returncode == 0 and r.stderr == ""


def test_host_layout_is_reported_as_unprotected(tmp_path):
    if not shutil.which("readelf"):
        pytest.skip("no readelf on the host")
    if os.uname().machine != "x86_64":
        pytest.skip("the offsets below are those of x86_64's TLS layout")
    lib = compile_c(tmp_path / "libhybris-tls-padding.so", [SRC], flags=["-shared", "-fPIC"])
    assert tls_segment(lib) == (48, 16)
    (tmp_path / "plain.c").write_text(NO_TLS)
    exe = compile_c(tmp_path / "plain", [tmp_path / "plain.c"], counted=False)
    env = dict(os.environ, HYBRIS_TLS_PADDING_DEBUG="1", LD_PRELOAD=str(lib))
    r = subprocess.run([str(exe)], capture_output=True, text=True, env=env, timeout=30)
    # The first block ends at the thread pointer: 48 bytes below it
    assert r.returncode == 0
    assert r.stderr == ("hybris-tls-padding: slots at offset -48 instead of 16, "
                        "bionic TLS slots are not protected\n")
    del env["HYBRIS_TLS_PADDING_DEBUG"]
    r = subprocess.run([str(exe)], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0 and r.stderr == ""


def test_cross_compiled_counters_are_read(tmp_path):
    # tools/c-coverage.py reads the counters of the aarch64 build too, though the cross compiler
    # is a minor version apart from the host's gcov (which then warns and exits with 3)
    cc = shutil.which("aarch64-linux-gnu-gcc")
    qemu = shutil.which("qemu-aarch64") or shutil.which("qemu-aarch64-static")
    if not cc or not qemu or not os.path.isdir(SYSROOT) or not shutil.which("gcov"):
        pytest.skip("no aarch64 cross compiler, qemu-user, sysroot or gcov on the host")
    lib = tmp_path / "libpad.so"
    subprocess.run([cc, "-O0", "--coverage", "-shared", "-fPIC", "-o", str(lib), str(SRC)], check=True)
    (tmp_path / "plain.c").write_text(NO_TLS)
    exe = tmp_path / "plain"
    subprocess.run([cc, "-o", str(exe), str(tmp_path / "plain.c")], check=True)
    env = dict(os.environ, HYBRIS_TLS_PADDING_DEBUG="1")
    subprocess.run([qemu, "-L", SYSROOT, "-E", f"LD_PRELOAD={lib}", str(exe)], env=env, check=True,
                   capture_output=True, timeout=60)
    builds = tmp_path / "gcov"
    builds.mkdir()
    (builds / "builds.txt").write_text(f"{lib}\n")
    report = tmp_path / "report.txt"
    r = subprocess.run([sys.executable, str(TOOLS / "c-coverage.py"), str(builds), str(report),
                        "--json", str(tmp_path / "c.json")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    data = json.loads((tmp_path / "c.json").read_text())["pkgbuilds/libhybris/hybris-tls-padding.c"]
    # Run once with the debug variable on aarch64: all but the early return and the warning
    assert data["missing"] == [29, 34]
