#!/usr/bin/env python3
"""Paths the sandbox of a systemd unit needs that only the image itself can provide.

ReadWritePaths=, ReadOnlyPaths=, InaccessiblePaths=, ExecPaths=, NoExecPaths= and the sources of
BindPaths= and BindReadOnlyPaths= must exist when the service starts, or systemd stops it at the
NAMESPACE step (status 226) on every start. A path is fine without the image when it carries the
"-" prefix (may be missing), when the unit's own StateDirectory=, CacheDirectory=, LogsDirectory=
or ConfigurationDirectory= creates it, when tmpfiles.d creates it at boot, or when a condition of
the unit (ConditionPathIsDirectory= and the like) skips the service without it. What is left has to
be in the image: tools/image-check.sh looks each one up there. A directory that only another
service creates (bluebinder's /var/lib/bluetooth, made by bluetooth.service after it) is missing
on a newly installed phone.

Usage: tools/unit-paths.py ROOT
  ROOT holds usr/lib/systemd/{system,user}, etc/systemd/{system,user} and usr/lib/tmpfiles.d,
  etc/tmpfiles.d as in the image. Prints "UNIT PATH" for each path the image must have.
"""
import pathlib
import shlex
import sys

PATH_KEYS = ("ReadWritePaths", "ReadOnlyPaths", "InaccessiblePaths", "ExecPaths", "NoExecPaths")
BIND_KEYS = ("BindPaths", "BindReadOnlyPaths")
# The directories systemd makes for a system unit
MADE = {"StateDirectory": "/var/lib", "CacheDirectory": "/var/cache", "LogsDirectory": "/var/log",
        "ConfigurationDirectory": "/etc"}
# Only the persistent file system is the image's; /run, /tmp, /proc, /sys, /dev come at boot
PERSISTENT = ("/etc/", "/usr/", "/var/", "/opt/", "/srv/")
UNIT_DIRS = ("usr/lib/systemd/system", "etc/systemd/system", "usr/lib/systemd/user",
             "etc/systemd/user")
TMPFILES_DIRS = ("usr/lib/tmpfiles.d", "etc/tmpfiles.d")
# tmpfiles.d types that create their path
TMPFILES_CREATE = set("dDvqQCLfF")
# [Unit] conditions under which a missing path skips the service instead of failing it
CONDITIONS = ("ConditionPathExists", "ConditionPathIsDirectory", "ConditionPathIsReadWrite",
              "ConditionDirectoryNotEmpty", "AssertPathExists", "AssertPathIsDirectory")


def lines(path):
    """Logical lines of a unit file: continuations joined, comments and blanks dropped."""
    out, cur = [], ""
    for raw in path.read_text(errors="replace").splitlines():
        s = raw.strip()
        if not cur and (not s or s[0] in "#;"):
            continue
        if s.endswith("\\"):
            cur += s[:-1] + " "
            continue
        out.append(cur + s)
        cur = ""
    if cur:
        out.append(cur)
    return out


def settings(files, want="[Service]"):
    """{key: [values]} of one section (e.g. [Service]), later files after earlier ones; an empty
    assignment clears what came before."""
    conf = {}
    for f in files:
        section = None
        for line in lines(f):
            if line.startswith("["):
                section = line
                continue
            if section != want or "=" not in line:
                continue
            key, value = (p.strip() for p in line.split("=", 1))
            try:
                words = shlex.split(value)
            except ValueError:
                words = value.split()
            if not words:
                conf.pop(key, None)
            else:
                conf.setdefault(key, []).extend(words)
    return conf


def units(root):
    """{unit name: [unit file, drop-ins...]} of the system and user units. Symlinks (aliases,
    masks, .wants/) are left out: the unit they name is there as a file."""
    found = {}
    for d in UNIT_DIRS:
        base = root / d
        if not base.is_dir():
            continue
        for f in sorted(base.iterdir()):
            if f.is_file() and not f.is_symlink() and f.suffix == ".service":
                found.setdefault((d.endswith("user"), f.name), {})["unit"] = f
        for dropdir in sorted(base.glob("*.service.d")):
            for f in sorted(dropdir.glob("*.conf")):
                if f.is_file() and not f.is_symlink():
                    found.setdefault((d.endswith("user"), dropdir.name[:-2]), {}).setdefault(
                        "drop", {})[f.name] = f
    out = {}
    for (user, name), parts in sorted(found.items()):
        if "unit" not in parts:
            continue
        # Drop-ins in name order; one in /etc replaces the one of the same name in /usr/lib
        drops = [parts.get("drop", {})[n] for n in sorted(parts.get("drop", {}))]
        out[(user, name)] = [parts["unit"], *drops]
    return out


def tmpfiles_made(root):
    made = set()
    for d in TMPFILES_DIRS:
        base = root / d
        if not base.is_dir():
            continue
        for f in sorted(base.glob("*.conf")):
            for line in f.read_text(errors="replace").splitlines():
                parts = line.split()
                if len(parts) >= 2 and not parts[0].startswith("#") and \
                        parts[0][0] in TMPFILES_CREATE:
                    made.add(parts[1].rstrip("/") or "/")
    return made


def needed(conf):
    """The paths of one unit's sandbox that must exist, without the "-" prefix ones."""
    paths = [w for k in PATH_KEYS for w in conf.get(k, [])]
    paths += [w.split(":", 1)[0] for k in BIND_KEYS for w in conf.get(k, [])]
    out = []
    for p in paths:
        if p.startswith("-"):
            continue
        p = p.lstrip("+")
        if "%" in p or not p.startswith(PERSISTENT):
            continue
        out.append(p.rstrip("/"))
    return out


def made_by_unit(conf):
    made = []
    for key, base in MADE.items():
        for w in conf.get(key, []):
            made.append(f"{base}/{w.split(':', 1)[0]}".rstrip("/"))
    return made


def guarded(conf):
    """Paths a condition of [Unit] requires: without them the service is skipped. Negated ("!")
    and triggering ("|", any one of them is enough) conditions do not count."""
    return {w.rstrip("/") for k in CONDITIONS for w in conf.get(k, []) if w[:1] not in "!|"}


def missing(root):
    root = pathlib.Path(root)
    made = tmpfiles_made(root)
    out = []
    for (user, name), files in units(root).items():
        conf = settings(files)
        own = [] if user else made_by_unit(conf)
        skip = guarded(settings(files, "[Unit]"))
        for p in needed(conf):
            if p in made or p in skip or any(p == d or p.startswith(d + "/") for d in own):
                continue
            out.append((name, p))
    return sorted(set(out))


def main(argv):
    if len(argv) != 2:
        print(__doc__.strip().split("\n\n")[-1], file=sys.stderr)
        return 1
    for name, path in missing(argv[1]):
        print(name, path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
