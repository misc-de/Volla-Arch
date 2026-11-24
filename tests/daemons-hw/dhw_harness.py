# Helpers for the C daemon tests in tests/daemons-hw.
#
# A daemon's static functions are tested by a harness that renames its main and includes the
# source: `#define main real_main` + `#include "<copy>.c"`. The copy lives in tmp, with every
# path into the running system (sysfs, /dev, /proc, /run, /var, /etc) replaced by one in tmp;
# sandboxed_source() refuses a copy that still names one, so a test can never write to the
# phone's (or the host's) real files by accident.

import os
import re
import subprocess
from pathlib import Path

from conftest import GCOV_DIR, PKG, compile_c

HERE = Path(__file__).resolve().parent
FAKES = HERE / "fakes"

SANITIZE = ("-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-fno-sanitize-recover=undefined")
SYSTEM_PREFIXES = ("/sys/", "/dev/", "/proc/", "/run/", "/var/", "/etc/", "/usr/lib/", "/usr/share/")
STRING_LITERAL = re.compile(r'"((?:[^"\\\n]|\\.)*)"')


def sandboxed_source(src, out_dir, replacements, allow=()):
    """Copy src to out_dir with the given (old, new) text replacements, then check that no string
    literal still points into the system (except the prefixes in allow, which the test fakes)."""
    text = Path(src).read_text()
    for old, new in replacements:
        assert old in text, f"{old!r} not in {src}: the source changed, update the test"
        text = text.replace(old, str(new))
    out_dir = Path(out_dir)
    for m in STRING_LITERAL.finditer(text):
        lit = m.group(1)
        if lit.startswith(str(out_dir)):
            continue
        for p in SYSTEM_PREFIXES:
            if lit.startswith(p) and not any(lit.startswith(a) for a in allow):
                raise AssertionError(f"{src}: system path {lit!r} left in the test copy")
    dst = out_dir / Path(src).name
    # The copy's lines are the source's when no replacement added or removed a line: gcov (and
    # compiler messages) then name the file in pkgbuilds/, so its coverage counts for it
    if text.count("\n") == Path(src).read_text().count("\n"):
        text = f'#line 1 "{Path(src).resolve()}"\n' + text
    dst.write_text(text)
    return dst


def build_harness(out_dir, name, harness_code, *, flags=(), pkgs=("glib-2.0",), sanitize=True,
                  fakes=True):
    """Write and compile a harness; fakes/ comes first on the include path, so its gbinder.h
    (and any header placed there) shadows a real one."""
    out_dir = Path(out_dir)
    # Prefixed, so it never overwrites the copied source it includes
    c = out_dir / f"harness_{name}.c"
    c.write_text(harness_code)
    inc = [f"-I{FAKES}"] if fakes else []
    return compile_c(out_dir / name, [c], flags=[*inc, f"-I{out_dir}", *(SANITIZE if sanitize else ()),
                                                 "-lm", *flags], pkgs=pkgs)


def run(binary, *args, env=None, check=True, timeout=20, cwd=None):
    """Run a harness binary; sanitizer reports make it fail. Leak checking is off: the daemons
    keep their state in globals for the whole run."""
    e = dict(os.environ if env is None else env)
    e["ASAN_OPTIONS"] = "detect_leaks=0:abort_on_error=0:exitcode=86"
    e["UBSAN_OPTIONS"] = "print_stacktrace=1:halt_on_error=1:exitcode=87"
    e["G_DEBUG"] = ""
    e["LC_ALL"] = "C"
    # Counting every line (coverage builds, -O0) makes the busy harnesses several times slower
    r = subprocess.run([str(binary), *map(str, args)], capture_output=True, text=True, env=e,
                       timeout=timeout * (5 if GCOV_DIR else 1), cwd=cwd)
    if check:
        assert r.returncode == 0, f"exit {r.returncode}\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    return r


def kv(output):
    """Parse 'key=value' lines of a harness into a dict (later keys win)."""
    out = {}
    for line in output.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


__all__ = ["PKG", "sandboxed_source", "build_harness", "run", "kv", "FAKES"]
