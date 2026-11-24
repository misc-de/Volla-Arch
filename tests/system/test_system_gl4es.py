# gl4es (pkgbuilds/gl4es/gl4es.sh, /usr/bin/gl4es): the starter puts gl4es' libGL and EGL wrapper
# in front of the program, points gl4es at the real EGL and GLES libraries and then becomes the
# program, keeping its arguments and exit code. The PKGBUILD installs what the starter expects.

import os
import re

from conftest import PKG
from system_helpers import run

STARTER = PKG / "gl4es" / "gl4es.sh"
PKGBUILD = (PKG / "gl4es" / "PKGBUILD").read_text()

# Prints the environment gl4es depends on, one variable per line, then the arguments
SHOW = ["sh", "-c", 'for v in LD_LIBRARY_PATH LIBGL_EGL LIBGL_GLES LIBGL_NOBANNER; do '
        'eval "echo $v=\\${$v-unset}"; done; printf "arg=%s\\n" "$@"', "show"]

ENV_KEYS = ("LD_LIBRARY_PATH", "LIBGL_EGL", "LIBGL_GLES", "LIBGL_NOBANNER", "GL4ES_DIR")


def env(**extra):
    e = {k: v for k, v in os.environ.items() if k not in ENV_KEYS}
    e.update(extra)
    return e


def shown(result):
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if not line.startswith("arg="))


def test_without_a_program_it_says_how_to_use_it():
    r = run([STARTER], env())
    assert r.returncode == 2
    assert "usage: gl4es <program>" in r.stderr and r.stdout == ""


def test_defaults_put_gl4es_first_and_point_it_at_the_real_libraries():
    r = run([STARTER, *SHOW], env())
    assert r.returncode == 0, r.stderr
    assert shown(r) == {
        "LD_LIBRARY_PATH": "/usr/lib/gl4es",
        "LIBGL_EGL": "/usr/lib/libEGL.so.1",
        "LIBGL_GLES": "/usr/lib/libGLESv2.so.2",
        "LIBGL_NOBANNER": "1",
    }


def test_an_existing_library_path_stays_behind_gl4es(tmp_path):
    r = run([STARTER, *SHOW], env(GL4ES_DIR=str(tmp_path), LD_LIBRARY_PATH="/opt/a:/opt/b"))
    assert shown(r)["LD_LIBRARY_PATH"] == f"{tmp_path}:/opt/a:/opt/b"


def test_settings_given_by_the_user_win():
    r = run([STARTER, *SHOW], env(LIBGL_EGL="/x/egl.so", LIBGL_GLES="/x/gles.so", LIBGL_NOBANNER="0"))
    s = shown(r)
    assert (s["LIBGL_EGL"], s["LIBGL_GLES"], s["LIBGL_NOBANNER"]) == ("/x/egl.so", "/x/gles.so", "0")


def test_the_program_keeps_its_arguments_and_exit_code():
    r = run([STARTER, *SHOW, "two words", "--flag", ""], env())
    assert [l for l in r.stdout.splitlines() if l.startswith("arg=")] == ["arg=two words", "arg=--flag", "arg="]
    assert run([STARTER, "sh", "-c", "exit 7"], env()).returncode == 7


def test_a_missing_program_fails_like_exec():
    r = run([STARTER, "/nonexistent/program"], env())
    assert r.returncode == 127 and "/nonexistent/program" in r.stderr


def test_the_package_installs_what_the_starter_expects():
    package = PKGBUILD.split("package() {", 1)[1]
    assert 'install -Dm755 gl4es.sh "$pkgdir/usr/bin/gl4es"' in package
    assert re.search(r'libGL\.so\.1 gl4es/lib/libEGL\.so\.1 -t "\$pkgdir/usr/lib/gl4es"', package)
    # glmark2 opens the unversioned names
    assert 'ln -s libGL.so.1 "$pkgdir/usr/lib/gl4es/libGL.so"' in package
    assert 'ln -s libEGL.so.1 "$pkgdir/usr/lib/gl4es/libEGL.so"' in package
    # The wrapper is what lets EGL programs get a desktop GL context
    assert "-DEGL_WRAPPER=ON" in PKGBUILD and "-DHYBRIS=ON" in PKGBUILD
