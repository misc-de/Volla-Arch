#!/usr/bin/env python3
"""Checks our packages for file conflicts with ALARM (rule 2 in docs/PAKETIERUNG.md).

Usage: tools/check-conflicts.py DB-DIR PACKAGE.pkg.tar.* [...]
DB-DIR contains core/extra/alarm.files.tar.gz from an ALARM mirror (x86_64: core/extra).
Files of packages listed in conflict= of our package are replaced on purpose and not reported.
Exit status 1 if any conflict is found.
"""
import os
import sys
import tarfile


def declared_conflicts(t):
    """Packages from conflict = ... in .PKGINFO; we replace their files on purpose."""
    names = set()
    info = t.extractfile(".PKGINFO").read().decode()
    for line in info.splitlines():
        if line.startswith("conflict = "):
            dep = line.split(" = ", 1)[1]
            for op in "<>=":
                dep = dep.split(op)[0]
            names.add(dep)
    return names


def alarm_files(dbdir):
    """File path -> "repo/pkgname-ver-rel" for all files in the ALARM .files databases."""
    owner = {}
    for repo in ("core", "extra", "alarm"):
        # x86_64 (preview) has no alarm repo
        if not os.path.exists(f"{dbdir}/{repo}.files.tar.gz"):
            continue
        with tarfile.open(f"{dbdir}/{repo}.files.tar.gz") as t:
            for m in t.getmembers():
                if not m.name.endswith("/files"):
                    continue
                pkg = m.name.split("/")[0]
                for line in t.extractfile(m).read().decode(errors="replace").splitlines()[1:]:
                    if line and not line.endswith("/"):
                        owner[line] = f"{repo}/{pkg}"
    return owner


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    owner = alarm_files(sys.argv[1])
    found = 0
    for pkgfile in sys.argv[2:]:
        with tarfile.open(pkgfile) as t:
            replaced = declared_conflicts(t)
            for name in t.getnames():
                if name.startswith(".") or name.endswith("/"):
                    continue
                if name in owner and owner[name].split("/")[1].rsplit("-", 2)[0] not in replaced:
                    print(f"{pkgfile.rsplit('/', 1)[-1]}: {name}  <->  {owner[name]}")
                    found += 1
    print(f"{found} conflict(s)")
    sys.exit(1 if found else 0)


if __name__ == "__main__":
    main()
