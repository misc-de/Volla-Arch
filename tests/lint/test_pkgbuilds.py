# Consistency of each PKGBUILD with the files next to it: every local source exists and matches
# its checksum, the checksum arrays are as long as the source arrays, and no versioned file in a
# package directory is forgotten (not in source=, not the install script).

import hashlib
import subprocess

import pytest

from conftest import PKG, REPO, git_files

ARCHES = ("", "_aarch64", "_x86_64")

# Files that are versioned next to a PKGBUILD on purpose without being a source
NOT_SOURCES = {
    "PKGBUILD", ".gitignore", "README.md", "LICENSE.upstream",
    "plinius-freigabe/kern.list",       # written and read by tools/freigabe.py
    "plinius-kernel/not-taken.txt",     # read by tools/publish-kernel-cves.sh
}

# Known findings, kept visible: the test fails again (strict xfail) once they are fixed
KNOWN = {}


def known(kind, name):
    reason = KNOWN.get((kind, name))
    return [pytest.mark.xfail(strict=True, reason=reason)] if reason else []


DUMP = r"""
set +eu
source ./PKGBUILD >/dev/null 2>&1 || exit 3
for v in source{,_aarch64,_x86_64} sha256sums{,_aarch64,_x86_64} b2sums{,_aarch64,_x86_64}; do
	declare -n a=$v
	printf '%s\0' "@$v" "${a[@]}"
	unset -n a
done
printf '%s\0' "@install" "${install:-}" "@changelog" "${changelog:-}"
"""


def packages():
    """Package directories whose PKGBUILD is versioned."""
    return sorted({f.parent.name for f in git_files(PKG) if f.name == "PKGBUILD"})


def params(kind):
    return [pytest.param(n, marks=known(kind, n)) for n in packages()]


def dump(name):
    r = subprocess.run(["bash", "-c", DUMP], cwd=PKG / name, capture_output=True,
                       env={"PATH": "/usr/bin:/bin", "CARCH": "aarch64", "HOME": "/nonexistent"})
    assert r.returncode == 0, f"PKGBUILD of {name} cannot be sourced: {r.stderr.decode()}"
    out, key = {}, None
    for item in r.stdout.decode().split("\0")[:-1]:
        if item.startswith("@"):
            key = item[1:]
            out[key] = []
        else:
            out[key].append(item)
    return out


def cached_name(entry):
    """The file name of a download with a name of its own ("NAME::URL"), else None. makepkg takes
    a file of that name next to the PKGBUILD instead of downloading it (its checksum still
    applies): plinius-software keeps GNOME Software's icons that way."""
    name, _, url = entry.partition("::")
    if url and "://" in url and not url.startswith(("git+", "svn+", "hg+", "bzr+")):
        return name
    return None


def local_name(entry):
    """File name of a local source entry, None for a download."""
    name, _, url = entry.partition("::")
    target = url or name
    if "://" in target or target.startswith(("git+", "svn+", "hg+", "bzr+")):
        return None
    return name if url else target.rsplit("/", 1)[-1]


# The checksum arrays makepkg checks; a package may use any of them (gtk3-plinius keeps Arch's
# b2sums), with the hash to compare local files against
SUMS = {"sha256sums": hashlib.sha256, "b2sums": hashlib.blake2b}


def checksums(d, arch):
    """(array name, checksums) of the checksum array that is set for arch, None for none."""
    found = [(kind, d[kind + arch]) for kind in SUMS if d[kind + arch]]
    assert len(found) <= 1, f"more than one checksum array for source{arch}: {found}"
    return found[0] if found else (None, [])


@pytest.fixture(scope="module")
def info():
    return {name: dump(name) for name in packages()}


@pytest.mark.parametrize("name", packages())
def test_checksums_as_many_as_sources(info, name):
    d = info[name]
    for arch in ARCHES:
        src = d["source" + arch]
        kind, sums = checksums(d, arch)
        assert len(src) == len(sums), (
            f"{name}: source{arch} has {len(src)} entries, {kind or 'sha256sums'}{arch} "
            f"{len(sums)}")


@pytest.mark.parametrize("name", params("sums"))
def test_local_sources_exist_and_match(info, name):
    d = info[name]
    wrong = []
    for arch in ARCHES:
        kind, sums = checksums(d, arch)
        for entry, want in zip(d["source" + arch], sums):
            local = local_name(entry)
            if local is None:
                # A versioned copy of a download must match it as well
                local = cached_name(entry)
                if local is None or not (PKG / name / local).is_file():
                    continue
            path = PKG / name / local
            assert path.is_file(), f"{name}: local source {local} is missing"
            if want == "SKIP":
                continue
            have = SUMS[kind](path.read_bytes()).hexdigest()
            if have != want:
                wrong.append(local)
    assert not wrong, f"{name}: checksums out of date for {', '.join(wrong)} (updpkgsums)"


@pytest.mark.parametrize("name", packages())
def test_no_forgotten_files(info, name):
    d = info[name]
    used = set(NOT_SOURCES)
    for arch in ARCHES:
        used |= {local_name(e) or cached_name(e) for e in d["source" + arch]}
    used |= set(d["install"]) | set(d["changelog"])
    base = PKG / name
    stray = []
    for f in git_files(base):
        rel = str(f.relative_to(base))
        # Translation templates stay next to the .po files
        if rel not in used and f"{name}/{rel}" not in used and not rel.endswith(".pot"):
            stray.append(rel)
    assert not stray, f"{name}: versioned but neither a source nor the install script: {stray}"


def test_repo_root():
    assert (REPO / "tools" / "build.sh").is_file()
