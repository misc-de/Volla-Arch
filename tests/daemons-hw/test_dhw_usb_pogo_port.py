# plinius-usb's pogo-port.h: the package's own check (test-pogo-port.c, run by its PKGBUILD's
# check()) built and run here as there, and shown to catch a wrong decision.

import subprocess

from conftest import compile_c
from dhw_usb_phone import USB


def test_the_package_check_passes(tmp_path):
    # Not named like the source: gcc would name its counters test-pogo-port.gcda, which
    # tools/c-coverage.py does not look for (<output>-<source>.gcda)
    exe = compile_c(tmp_path / "pogo-check", [USB / "test-pogo-port.c"], flags=["-Wextra"])
    r = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
    assert (r.returncode, r.stdout) == (0, "All port decisions are as agreed\n")


def test_the_package_check_catches_a_wrong_decision(tmp_path):
    # USB-C first and the user takes the pogo pins: the driver must get "1", not "2"
    header = (USB / "pogo-port.h").read_text()
    wrong = header.replace('return port == POGO_PORT_POGO ? "1\\n" : "2\\n";',
                           'return port == POGO_PORT_POGO ? "2\\n" : "1\\n";')
    assert wrong != header
    (tmp_path / "pogo-port.h").write_text(wrong)
    # The wrong header first: its include guard makes the check's own #include a no-op
    exe = compile_c(tmp_path / "pogo-check", [USB / "test-pogo-port.c"],
                    flags=["-include", str(tmp_path / "pogo-port.h")])
    r = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
    assert r.returncode == 1
    assert "FAILED: USB-C first, take pogo\n" in r.stdout
    assert "FAILED: stick, take USB-C\n" in r.stdout
