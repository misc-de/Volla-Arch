# Helpers for the plinius-ambient tests: the sources compiled against the stand-in for OpenGL ES 2
# (ambient/fakegl.c, every GL call a line "gl ..." on stdout), and that log read back.

import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from dhw_harness import PKG, SANITIZE, sandboxed_source
from conftest import compile_c

DIR = PKG / "plinius-ambient"
HERE = Path(__file__).resolve().parent
AMBIENT_FAKES = HERE / "ambient"


def build(out_dir, name, harness_code, sources, *, included=(), uncounted=(), pkgs=("glib-2.0",), flags=(),
          sanitize=True, extra=()):
    """Compiles the harness with copies of the named plinius-ambient sources (each with its #line,
    so gcov counts them for pkgbuilds/) and the fake GL; the copies of `included` sit next to the
    harness for its #include, the C files in `extra` are compiled with it. The sources in
    `uncounted` are only needed to link (the effects under a layered background, say): copied
    without the #line, their counters do not dilute those of the test made for them. No
    libGLESv2 is linked: the fake is the only GL there is."""
    if not Path("/usr/include/GLES2/gl2.h").exists():
        pytest.skip("no GLES2 headers on the host")
    out_dir = Path(out_dir)
    copies = [sandboxed_source(DIR / s, out_dir, []) for s in sources]
    for s in included:
        sandboxed_source(DIR / s, out_dir, [])
    for s in uncounted:
        copy = sandboxed_source(DIR / s, out_dir, [])
        lines = copy.read_text().split("\n", 1)
        if lines[0].startswith("#line 1 "):
            copy.write_text(lines[1])
        copies.append(copy)
    c = out_dir / f"harness_{name}.c"
    c.write_text(harness_code)
    return compile_c(out_dir / name, [c, *copies, *extra, AMBIENT_FAKES / "fakegl.c"],
                     flags=[f"-I{DIR}", f"-I{AMBIENT_FAKES}", *(SANITIZE if sanitize else ()), "-lm",
                            "-Wno-unused-parameter", *flags], pkgs=pkgs)


def gl(out, prefix=""):
    """The fake GL's lines (without "gl "), those starting with prefix"""
    lines = [line[3:] for line in out.splitlines() if line.startswith("gl ")]
    return [line for line in lines if line.startswith(prefix)]


def draws(out):
    """Every glDrawArrays as a dict"""
    result = []
    for line in gl(out, "draw "):
        f = line.split()
        result.append({"mode": f[1], "first": int(f[2]), "count": int(f[3]), "program": int(f[5]),
                       "framebuffer": int(f[7]), "viewport": f[9], "blend": f[11],
                       "attribs": int(f[13], 16), "buffer": int(f[15]), "bytes": int(f[16]),
                       "textures": [int(t) for t in f[18:21]]})
    return result


def uniforms(out):
    """The uniforms as last set: name -> list of numbers"""
    u = {}
    for line in gl(out, "uniform "):
        f = line.split()
        if f[1] != "-1":
            u[f[1]] = [float(v) for v in f[2:]]
    return u


def floats(path):
    data = Path(path).read_bytes()
    return list(struct.unpack(f"<{len(data) // 4}f", data))


def glsl_errors(dump_dir):
    """Every shader the program handed to GL, checked as GLSL ES 1.00 by glslangValidator (the
    phone's driver takes the same language): file name -> messages, None without the validator"""
    validator = shutil.which("glslangValidator")
    errors = {}
    if not validator:
        return None
    checked_dir = Path(dump_dir) / "checked"
    checked_dir.mkdir(exist_ok=True)
    for src in sorted(Path(dump_dir).glob("shader-*")):
        # glslang wants the version line; GLSL ES 1.00 is what "no #version" means for GLES 2
        checked = checked_dir / src.name
        checked.write_text("#version 100\n" + src.read_text())
        r = subprocess.run([validator, "-S", src.suffix[1:], str(checked)], capture_output=True, text=True)
        if r.returncode != 0:
            errors[src.name] = r.stdout + r.stderr
    return errors
