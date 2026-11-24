# Helpers shared by the tests in tests/system (imported by name, so the file name is unique).
#
# The scripts here hardcode absolute paths (/etc, /run, /proc, /home ...). They are only ever run
# as a copy whose paths were rewritten into tmp_path, never in place.

import subprocess
from pathlib import Path

from conftest import PKG


# Paths of persistent state: a copy that still names one of them outside a comment would change
# the build host's real files (as the pinguard test did with /etc/pam.d/su, 2026-11-19). Checked in
# every copy, in addition to check_left; allow_left names the occurrences that are known to only
# be read or printed.
STATE_PREFIXES = ("/etc/", "/var/", "/home/", "/root/", "/boot/", "/srv/", "/userdata/")


def _code_lines(text):
    """The lines of a shell/Python/Lua script without full-line comments."""
    return [line for line in text.splitlines()
            if not line.lstrip().startswith(("#", "--"))]


def rewritten_copy(src, dst, mapping, *, check_left=(), allow_left=()):
    """Copy script src to dst with each key of mapping replaced by its value (in the given order).

    Every key must occur, so a renamed path in the script makes the test fail instead of silently
    pointing at the real system. check_left lists prefixes that must not remain afterwards;
    STATE_PREFIXES are checked on top, except for the strings in allow_left (each must occur).
    """
    src = Path(src)
    text = src.read_text()
    # Placeholders first: tmp_path itself may contain a prefix of check_left (/tmp/.../proc/...)
    marks = {}
    for i, old in enumerate(mapping):
        assert old in text, f"{old!r} not found in {src}"
        marks[f"\0{i}\0"] = str(mapping[old])
        text = text.replace(old, f"\0{i}\0")
    for prefix in check_left:
        assert prefix not in text, f"{prefix!r} still in the copy of {src}"
    code = "\n".join(_code_lines(text))
    for allowed in allow_left:
        assert allowed in code, f"allow_left {allowed!r} not found in {src}"
        code = code.replace(allowed, "")
    left = sorted({line.strip() for line in code.splitlines()
                   if any(p in line for p in STATE_PREFIXES)})
    assert not left, f"system paths still in the copy of {src}: {left}"
    for mark, new in marks.items():
        text = text.replace(mark, new)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(text)
    dst.chmod(0o755)
    return dst


def run(cmd, env, **kw):
    kw.setdefault("timeout", 20)
    return subprocess.run([str(c) for c in cmd], env=env, capture_output=True, text=True, **kw)


def run_bash(code, env, **kw):
    return run(["bash", "-c", code], env, **kw)


def scriptlet(pkg, tmp_path, mapping, check_left=(), allow_left=()):
    """A copy of pkgbuilds/<pkg>/<pkg>.install with its paths rewritten, ready to be sourced."""
    return rewritten_copy(PKG / pkg / f"{pkg}.install", tmp_path / f"{pkg}.install", mapping,
                          check_left=check_left, allow_left=allow_left)


def call_scriptlet(script, func, env, *args, cwd=None):
    """Source a pacman install script and run one of its functions, as pacman does (bash)."""
    q = " ".join("'" + a.replace("'", "'\\''") + "'" for a in args)
    return run_bash(f'. "{script}"; {func} {q}', env, cwd=cwd)
