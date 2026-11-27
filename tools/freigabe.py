#!/usr/bin/env python3
"""Writes the approval (Freigabe) for ARCH into pkgbuilds/plinius-freigabe/PKGBUILD
(docs/PAKETIERUNG.md).

Coupled packages are
  - the packages of the libraries our compiled packages are linked against
    (ELF NEEDED of the packages in the local repo, mapped to a package via the .files database),
  - the core list pkgbuilds/plinius-freigabe/kern.list.
Each is approved at the version on the mirrors at the time of approval:
conflicts=("package>version"). pacman rejects newer versions on -Syu, and with them the whole
update; plinius-software then shows "Update waiting for approval".

Only what was built together is approved: for every linked library, the version on the mirrors
must be the one our package was built against (.BUILDINFO; tools/build.sh builds with
pacman -Syu against the current state). If the mirror has moved on since, or a package is
older, the script aborts and names the packages that need to be rebuilt.

Usage (after building and testing): [ARCH=x86_64] tools/freigabe.py [--check | --force]
  --check  write nothing; exit status 1 if the committed approval does not match the repo and
           the mirrors (tools/build.sh runs this after every build)
  --force  approve even if packages were built against a different state
"""
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARCH = os.environ.get("ARCH", "aarch64")
BUILD = os.path.join(ROOT, "build")
PKGBUILD = os.path.join(ROOT, "pkgbuilds", "plinius-freigabe", "PKGBUILD")
KERN = os.path.join(ROOT, "pkgbuilds", "plinius-freigabe", "kern.list")
if ARCH == "aarch64":
    REPO = f"{BUILD}/repo/local"
    REPOS = ("core", "extra", "alarm")
    MIRROR = "https://de3.mirror.archlinuxarm.org/aarch64/{repo}/{repo}"
elif ARCH == "x86_64":
    REPO = f"{BUILD}/repo/local-x86_64"
    REPOS = ("core", "extra")
    MIRROR = "https://geo.mirror.pkgbuild.com/{repo}/os/x86_64/{repo}"
else:
    sys.exit(f"ARCH={ARCH} not supported")


def db_entries(path, wanted):
    """Yields (entry name, {field: values}) from a pacman database; wanted: desc or files."""
    with tarfile.open(path) as t:
        for m in t.getmembers():
            if not m.name.endswith("/" + wanted):
                continue
            fields, key = {}, None
            for line in t.extractfile(m).read().decode(errors="replace").splitlines():
                if line.startswith("%") and line.endswith("%"):
                    key = line.strip("%")
                    fields[key] = []
                elif line and key:
                    fields[key].append(line)
            yield m.name.split("/")[0], fields


CACHE = f"{BUILD}/cache/files-{ARCH}"
USER_AGENT = "Plinius Arch Linux"  # every request says who asks


def fetch(repo, kind, max_age):
    """Database from the mirror (kind: db or files), cached in build/cache for max_age seconds."""
    os.makedirs(CACHE, exist_ok=True)
    suffix = ".db" if kind == "db" else ".files.tar.gz"
    path = f"{CACHE}/{repo}{suffix}"
    if not os.path.exists(path) or time.time() - os.path.getmtime(path) > max_age:
        request = urllib.request.Request(MIRROR.format(repo=repo) + suffix, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=120) as response, open(path + ".part", "wb") as out:
            shutil.copyfileobj(response, out)
        os.replace(path + ".part", path)
    return path


def versions():
    """Package → current version on the mirrors."""
    result = {}
    for repo in REPOS:
        for _entry, f in db_entries(fetch(repo, "db", 0), "desc"):
            # ALARM extra contains a broken entry without fields (findnewest)
            if f.get("NAME") and f.get("VERSION"):
                result.setdefault(f["NAME"][0], f["VERSION"][0])
    return result


def library_owners():
    """Library file name (libfoo.so.1) → package, from the .files databases."""
    owners = {}
    for repo in REPOS:
        for entry, f in db_entries(fetch(repo, "files", 86400), "files"):
            name = entry.rsplit("-", 2)[0]
            for file in f.get("FILES", []):
                # Only directly in /usr/lib: the linker does not search subdirectories (e.g. usr/lib/cef/)
                if file.startswith("usr/lib/") and file.count("/") == 2 and ".so" in file:
                    owners.setdefault(os.path.basename(file), name)
    return owners


def our_packages():
    """Newest package per name in the local repo: name → (path, architecture, pkgbase)."""
    latest = {}
    for entry, f in db_entries(f"{REPO}/plinius-local.db", "desc"):
        latest[f["NAME"][0]] = (f"{REPO}/{f['FILENAME'][0]}", f["ARCH"][0], f.get("BASE", f["NAME"])[0])
    return latest


def needed(pkgfile):
    """sonames the ELF files of a package are linked against."""
    result = set()
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["bsdtar", "-xf", pkgfile, "-C", tmp], check=True)
        for dirpath, _dirs, files in os.walk(tmp):
            for name in files:
                path = os.path.join(dirpath, name)
                if os.path.islink(path) or not os.path.isfile(path):
                    continue
                with open(path, "rb") as fh:
                    if fh.read(4) != b"\x7fELF":
                        continue
                out = subprocess.run(["readelf", "-d", path], capture_output=True, text=True).stdout
                result |= set(re.findall(r"\(NEEDED\)\s+Shared library: \[(.+?)\]", out))
    return result


def built_against(pkgfile):
    """Package → version (pkgver-pkgrel) in the build environment, from .BUILDINFO."""
    out = subprocess.run(["bsdtar", "-xOf", pkgfile, ".BUILDINFO"], capture_output=True, text=True).stdout
    result = {}
    for line in out.splitlines():
        if line.startswith("installed = "):
            # name-pkgver-pkgrel-arch; names may contain hyphens
            name, ver, rel, _arch = line[len("installed = "):].rsplit("-", 3)
            result[name] = f"{ver}-{rel}"
    return result


def main():
    """Collects the coupled packages, then checks (--check) or rewrites the conflicts_ARCH block."""
    check = "--check" in sys.argv[1:]
    force = "--force" in sys.argv[1:]
    ours = our_packages()
    owners = library_owners()
    coupled = {}  # package → reason (emitted as comment)
    linked_by = {}  # linked package → [(our package, version at build time)]
    for name, (path, arch, base) in sorted(ours.items()):
        if arch == "any" or name.endswith("-debug"):
            continue
        built = built_against(path)
        for soname in needed(path):
            owner = owners.get(soname)
            # Only what was installed at build time: otherwise the library did not come from the
            # mirror (built in the source tree, or a test program against an optional library)
            if owner and owner not in ours and owner in built:
                coupled.setdefault(owner, f"linked by {name}")
                linked_by.setdefault(owner, []).append((base, built.get(owner)))
    with open(KERN) as f:
        for line in f:
            pkg = line.split("#")[0].strip()
            if pkg:
                coupled.setdefault(pkg, "core list")

    have = versions()
    lines, stale = [], {}
    for pkg in sorted(coupled):
        if pkg not in have:
            print(f"skipped, not in {ARCH}: {pkg}", file=sys.stderr)
            continue
        lines.append(f'\t"{pkg}>{have[pkg]}"  # {coupled[pkg]}')
        for name, version in linked_by.get(pkg, []):
            if version != have[pkg]:
                stale.setdefault(name, []).append(f"{pkg} {version or '?'} → {have[pkg]}")

    # Replace the block conflicts_ARCH=( … ) line by line: up to the first line that is just ")"
    old = open(PKGBUILD).read().split("\n")
    start = old.index(f"conflicts_{ARCH}=(")
    end = old.index(")", start)
    if check:
        # Compare only the limits, not the comments
        def limits(block):
            return {l.split("#")[0].strip() for l in block if l.strip()}
        current = limits(old[start + 1:end])
        if current == limits(lines) and not stale:
            return
        for l in sorted(limits(lines) - current):
            print(f"approval {ARCH}: missing {l}", file=sys.stderr)
        for l in sorted(current - limits(lines)):
            print(f"approval {ARCH}: outdated {l}", file=sys.stderr)
        for name, why in sorted(stale.items()):
            print(f"approval {ARCH}: {name} was built against a different state ({', '.join(why)})",
                  file=sys.stderr)
        sys.exit(1)
    if stale:
        print("Built against a different state than on the mirrors, rebuild first:", file=sys.stderr)
        for name, why in sorted(stale.items()):
            print(f"  tools/build.sh {name}   # {', '.join(why)}", file=sys.stderr)
        if not force:
            sys.exit("No approval written (--force approves anyway).")
    new = old[:start + 1] + lines + old[end:]
    new = [f"pkgver={time.strftime('%Y%m%d.%H%M')}" if l.startswith("pkgver=") else l for l in new]
    open(PKGBUILD, "w").write("\n".join(new))
    print(f"{len(lines)} coupled packages approved for {ARCH}:")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
