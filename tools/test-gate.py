#!/usr/bin/env python3
# The test gate in front of tools/publish.sh: a package reaches beta only when its sources passed
# a full green run of tools/run-tests.sh.
#
# Usage: tools/test-gate.py snapshot FILE        fingerprints of every package directory now
#        tools/test-gate.py pass FILE [ID ...]   after a green full run: keep those directories
#                                                that did not change during the run (FILE from
#                                                snapshot; IDs: tests that passed only when run
#                                                again alone)
#        tools/test-gate.py fail                 after a red full run: no package counts as tested
#        tools/test-gate.py check DB BETA_DIR    every package of the repo database DB that BETA_DIR
#                                                does not have yet (byte for byte) must have been
#                                                built from the PKGBUILD version in the tree, from
#                                                a directory unchanged since the last green run
#
# The result of the last full run is build/test-pass.json (PLINIUS_TEST_PASS overrides). A
# directory's fingerprint covers every file git versions or would version (ignored ones such as
# the bare source mirrors left out), with its path, executable bit and content.

import datetime
import hashlib
import json
import os
import stat
import subprocess
import sys
import tarfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKGBUILDS = os.path.join(ROOT, "pkgbuilds")


def pass_file():
    return os.environ.get("PLINIUS_TEST_PASS") or os.path.join(ROOT, "build", "test-pass.json")


def tree_files():
    """Paths below pkgbuilds/ relative to ROOT: from git when ROOT is a checkout, else every file."""
    r = subprocess.run(["git", "-C", ROOT, "ls-files", "-co", "--exclude-standard", "-z", "--",
                        "pkgbuilds"], capture_output=True)
    if r.returncode == 0:
        return sorted({f for f in r.stdout.decode("utf-8", "surrogateescape").split("\0") if f})
    files = []
    for dirpath, dirnames, names in os.walk(PKGBUILDS):
        dirnames.sort()
        for n in names:
            files.append(os.path.relpath(os.path.join(dirpath, n), ROOT))
    return sorted(files)


def fingerprints():
    """{package directory: sha256 of its files}"""
    per_pkg = {}
    for rel in tree_files():
        parts = rel.split("/")
        if len(parts) < 3:
            continue
        path = os.path.join(ROOT, rel)
        try:
            st = os.lstat(path)
        except FileNotFoundError:  # listed by git but deleted in the tree
            continue
        if stat.S_ISLNK(st.st_mode):
            kind, data = "l", os.readlink(path).encode("utf-8", "surrogateescape")
        elif stat.S_ISREG(st.st_mode):
            kind = "x" if st.st_mode & 0o111 else "f"
            with open(path, "rb") as f:
                data = f.read()
        else:
            continue
        h = per_pkg.setdefault(parts[1], hashlib.sha256())
        h.update("/".join(parts[2:]).encode("utf-8", "surrogateescape") + b"\0" + kind.encode()
                 + b"\0" + hashlib.sha256(data).digest())
    return {name: h.hexdigest() for name, h in sorted(per_pkg.items())}


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.part"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def head_commit():
    r = subprocess.run(["git", "-C", ROOT, "rev-parse", "HEAD"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def cmd_snapshot(out):
    write_json(out, {"packages": fingerprints()})
    return 0


def cmd_pass(snapshot, wobbly):
    with open(snapshot) as f:
        before = json.load(f)["packages"]
    now = fingerprints()
    kept = {n: h for n, h in now.items() if before.get(n) == h}
    changed = sorted((set(before) | set(now)) - set(kept))
    write_json(pass_file(), {
        "time": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commit": head_commit(),
        "packages": kept,
        "changed_during_run": changed,
        "wobbly": sorted(wobbly),
    })
    print(f"==> test gate: {len(kept)} package directories passed the full run", file=sys.stderr)
    if changed:
        print("==> changed while the tests ran, so not counted as tested: " + ", ".join(changed),
              file=sys.stderr)
    return 0


def cmd_fail():
    try:
        os.unlink(pass_file())
    except FileNotFoundError:
        pass
    print("==> test gate: the full run failed, no package counts as tested", file=sys.stderr)
    return 0


def db_entries(db):
    """[(file name, base, version)] from a pacman repo database"""
    entries = []
    with tarfile.open(db) as t:
        for m in t.getmembers():
            if not m.name.endswith("/desc"):
                continue
            fields, key = {}, None
            for line in t.extractfile(m).read().decode().splitlines():
                if line.startswith("%") and line.endswith("%"):
                    key = line.strip("%")
                elif line and key and key not in fields:
                    fields[key] = line
            entries.append((fields["FILENAME"], fields.get("BASE") or fields["NAME"],
                            fields["VERSION"]))
    return sorted(entries)


def same_file(a, b):
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
    except OSError:
        return False
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            x, y = fa.read(1 << 20), fb.read(1 << 20)
            if x != y:
                return False
            if not x:
                return True


def pkgbuild_version(name):
    d = os.path.join(PKGBUILDS, name)
    if not os.path.isfile(os.path.join(d, "PKGBUILD")):
        return None
    r = subprocess.run(["bash", "-c", 'source ./PKGBUILD >/dev/null 2>&1 || exit 1\n'
                        'printf "%s" "${epoch:+$epoch:}$pkgver-$pkgrel"'],
                       cwd=d, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return r.stdout if r.returncode == 0 else ""


def cmd_check(db, beta):
    repo = os.path.dirname(os.path.abspath(db))
    new = {}
    for fn, base, version in db_entries(db):
        if not same_file(os.path.join(repo, fn), os.path.join(beta, fn)):
            new.setdefault(base, set()).add(version)
    if not new:
        print("==> test gate: nothing new for beta", file=sys.stderr)
        return 0
    try:
        with open(pass_file()) as f:
            passed = json.load(f)
    except FileNotFoundError:
        passed = None
    now = fingerprints()
    problems = []
    for base, versions in sorted(new.items()):
        tree = pkgbuild_version(base)
        if tree is None:
            problems.append(f"{base}: no pkgbuilds/{base}/PKGBUILD")
            continue
        if versions != {tree}:
            problems.append(f"{base}: built as {', '.join(sorted(versions))}, the tree has "
                            f"{tree or 'a PKGBUILD that does not load'}: rebuild it from the tree")
            continue
        if passed is None:
            problems.append(f"{base}: no green full test run")
        elif base not in passed["packages"]:
            problems.append(f"{base}: not in the last green full test run ({passed['time']})")
        elif passed["packages"][base] != now.get(base):
            problems.append(f"{base}: changed since the last green full test run ({passed['time']})")
    for p in problems:
        print(f"Error: test gate: {p}", file=sys.stderr)
    if problems:
        print("==> tools/run-tests.sh without arguments makes a full run", file=sys.stderr)
        return 1
    print(f"==> test gate: {len(new)} new for beta, all from sources of the green full test run "
          f"of {passed['time']}", file=sys.stderr)
    for w in passed.get("wobbly", []):
        print(f"==> test gate: that run had a wobbly test (failed among the others, passed "
              f"alone): {w}", file=sys.stderr)
    return 0


def main(argv):
    usage = "usage: tools/test-gate.py snapshot FILE | pass FILE [ID ...] | fail | check DB BETA_DIR"
    if argv[:1] == ["snapshot"] and len(argv) == 2:
        return cmd_snapshot(argv[1])
    if argv[:1] == ["pass"] and len(argv) >= 2:
        return cmd_pass(argv[1], argv[2:])
    if argv == ["fail"]:
        return cmd_fail()
    if argv[:1] == ["check"] and len(argv) == 3:
        return cmd_check(argv[1], argv[2])
    print(usage, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
