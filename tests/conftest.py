# Shared helpers for the host-side tests (run with tools/run-tests.sh, see tests/README.md).
#
# Nothing here touches the running system: scripts get their programs from stubs in a temporary
# directory, and paths they write to are pointed into tmp_path.

import importlib.machinery
import importlib.util
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.dont_write_bytecode = True

# Nothing a test starts or imports may reach the desktop or the buses of the person running the
# tests: GTK imported in this process would otherwise open their display, and a GDBus call
# their session or system bus. Tests that need a display or a bus start their own (Broadway,
# a private dbus-daemon) and set these themselves; a pytest run inside such a test (tests/apps/
# test_apps_software_ui.py) says so with PLINIUS_TEST_OWN_DISPLAY and keeps what it was given.
if not os.environ.get("PLINIUS_TEST_OWN_DISPLAY"):
    for _name in ("DISPLAY", "WAYLAND_DISPLAY", "GDK_BACKEND", "XAUTHORITY"):
        os.environ.pop(_name, None)
    os.environ["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/nonexistent/plinius-tests/session-bus"
    os.environ["DBUS_SYSTEM_BUS_ADDRESS"] = "unix:path=/nonexistent/plinius-tests/system-bus"

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "pkgbuilds"
TOOLS = REPO / "tools"


def load_script(path, name=None):
    """Import a Python script (also one without .py) as a module, without running its main."""
    path = Path(path)
    name = name or "script_" + path.name.replace("-", "_").replace(".", "_")
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    module.__file__ = str(path)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


def git_files(*paths):
    """Versioned files below the given repository paths."""
    out = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z", "--", *map(str, paths)],
                         check=True, capture_output=True).stdout
    return [REPO / p for p in out.decode().split("\0") if p]


class FakeBin:
    """A directory of stand-in programs that log their arguments.

    stub(name, script) writes a shell stub; every call appends its arguments (each ended by NUL,
    the call by \\036) to <dir>/<name>.log. env() is os.environ with the stubs first in PATH, or
    with only the stubs and the basic tools (isolated=True, so nothing real is reached by
    accident).
    """

    BASIC = ("sh", "bash", "cat", "printf", "echo", "sed", "awk", "grep", "tr", "cut", "head",
             "tail", "sort", "uniq", "wc", "mkdir", "rm", "mv", "cp", "ln", "ls", "stat", "test",
             "basename", "dirname", "readlink", "realpath", "touch", "chmod", "date", "env",
             "find", "xargs", "tee", "id", "sleep", "true", "false", "cmp", "diff", "od", "dd",
             "truncate", "mktemp", "seq", "expr", "install", "sha256sum", "gzip", "zstd", "lz4",
             "python3", "tac", "rev", "paste", "join", "comm", "fold", "nproc", "getopt",
             "flock", "timeout", "sync", "base64", "xxd", "cpio", "tar", "gettext")

    def __init__(self, path):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)

    def stub(self, name, script="exit 0"):
        f = self.path / name
        log = self.path / (name + ".log")
        f.write_text("#!/bin/bash\n"
                     f'{{ [ $# -eq 0 ] || printf "%s\\0" "$@"; printf "\\036"; }} >> "{log}"\n'
                     + script + "\n")
        f.chmod(f.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return f

    def calls(self, name):
        log = self.path / (name + ".log")
        if not log.exists():
            return []
        records = log.read_text().split("\036")[:-1]
        return [r.split("\0")[:-1] for r in records]

    def env(self, isolated=False, **extra):
        env = dict(os.environ)
        if isolated:
            real = self.path / ".real"
            real.mkdir(exist_ok=True)
            for tool in self.BASIC:
                found = shutil.which(tool)
                if found and not (real / tool).exists() and not (self.path / tool).exists():
                    (real / tool).symlink_to(found)
            env["PATH"] = f"{self.path}:{real}"
        else:
            env["PATH"] = f"{self.path}:{env.get('PATH', '')}"
        env["LC_ALL"] = "C.UTF-8"
        env.pop("LANGUAGE", None)
        env.update({k: str(v) for k, v in extra.items()})
        return env


@pytest.fixture(autouse=True)
def _keep_locale():
    """Some programs call locale.setlocale() when imported: no test may leave its locale (a
    German decimal comma, say) to the next one."""
    import locale
    saved = locale.setlocale(locale.LC_ALL)
    yield
    locale.setlocale(locale.LC_ALL, saved)


@pytest.fixture
def fake_bin(tmp_path):
    return FakeBin(tmp_path / "bin")


# tools/run-tests.sh --coverage: C programs are built with gcov's counters (and -O0, so every line
# keeps its own counter); the list of builds goes to this directory, tools/c-coverage.py reads the
# counters once the tests are done.
GCOV_DIR = os.environ.get("PLINIUS_TEST_GCOV")


def compile_c(out, sources, *, flags=(), pkgs=(), cc=None, counted=True):
    """Compile C sources for the host into out; skips the test when a library is missing.
    counted=False: never with gcov's counters, for a program that is itself the subject of a test
    (its machine code must stay as the compiler normally makes it)."""
    cc = cc or os.environ.get("CC", "cc")
    if shutil.which(cc) is None:
        pytest.skip(f"no C compiler ({cc})")
    pc_flags = []
    if pkgs:
        if shutil.which("pkg-config") is None:
            pytest.skip("no pkg-config")
        r = subprocess.run(["pkg-config", "--cflags", "--libs", *pkgs], capture_output=True,
                           text=True)
        if r.returncode != 0:
            pytest.skip(f"missing on the host: {' '.join(pkgs)}")
        pc_flags = r.stdout.split()
    gcov = GCOV_DIR and counted
    opt = ["-O0", "--coverage", "-fprofile-update=atomic"] if gcov else ["-O1"]
    cmd = [cc, "-std=gnu11", *opt, "-g", "-Wall", "-Wno-unused-function", "-o", str(out),
           *map(str, sources), *flags, *pc_flags]
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0, f"{' '.join(cmd)}\n{r.stderr}"
    if gcov:
        with open(os.path.join(GCOV_DIR, "builds.txt"), "a") as f:
            f.write(f"{os.path.abspath(out)}\n")
    return Path(out)


def bash_functions(script, *names):
    """Source only the named function definitions of a bash script (for unit tests of scripts
    whose top level does real work). Returns bash code to put before the test commands."""
    text = Path(script).read_text()
    lines = text.splitlines()
    out = []
    for name in names:
        start = None
        for i, line in enumerate(lines):
            s = line.strip()
            if s.startswith((f"{name}()", f"{name} ()", f"function {name}")):
                start = i
                break
        assert start is not None, f"{name} not found in {script}"
        if lines[start].rstrip().endswith("}") and "{" in lines[start]:
            out.append(lines[start])  # one-line function
            continue
        for j in range(start, len(lines)):
            out.append(lines[j])
            # Function bodies in this repository close with a "}" at the start of a line
            if j > start and lines[j].startswith("}"):
                break
        else:
            raise AssertionError(f"end of {name} not found in {script}")
    return "\n".join(out) + "\n"
