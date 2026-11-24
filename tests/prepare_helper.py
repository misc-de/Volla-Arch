# Runs a PKGBUILD's prepare() the way makepkg would, from the source cache build/cache/sources
# (SRCDEST of tools/build.sh), in a scratch directory and without network: whether the patches
# still apply to the pinned upstream commit, without building anything.

import os
import shutil
import subprocess
from pathlib import Path

from conftest import PKG, REPO

SRCDEST = REPO / "build" / "cache" / "sources"
ARCHIVES = (".tar", ".tar.gz", ".tgz", ".tar.xz", ".tar.bz2", ".tar.zst", ".zip")
# A source bigger than this (the kernel, TensorFlow) is not unpacked for a test
MAX_ARCHIVE = 150 << 20

DUMP = r"""
set +eu
source ./PKGBUILD >/dev/null 2>&1 || exit 3
src=("${source[@]}" "${source_aarch64[@]}")
printf '%s\0' "${src[@]}"
printf '\036'
printf '%s\0' "${noextract[@]}"
printf '\036'
declare -F prepare >/dev/null && echo yes
"""


class Unavailable(Exception):
    """The cache lacks a source, or it is too big: the test is skipped, not failed."""


def _git(*args, cwd=None):
    # The cache belongs to the build's user namespace: allow it for this call only
    return subprocess.run(["git", "-c", "safe.directory=*", *args], cwd=cwd, check=True,
                          capture_output=True, text=True)


def read(name):
    r = subprocess.run(["bash", "-c", DUMP], cwd=PKG / name, capture_output=True,
                       env={"PATH": "/usr/bin:/bin", "CARCH": "aarch64", "HOME": "/nonexistent"})
    if r.returncode != 0:
        raise AssertionError(f"PKGBUILD of {name} cannot be sourced: {r.stderr.decode()}")
    sources, noextract, rest = r.stdout.decode().split("\036")
    return ([s for s in sources.split("\0") if s], [s for s in noextract.split("\0") if s],
            rest.strip() == "yes")


def has_prepare(name):
    return read(name)[2]


def parse(entry):
    """(file name in srcdir, kind, location, fragment) of a source entry."""
    name, _, url = entry.partition("::")
    if not url:
        name, url = "", entry
    fragment = ""
    if url.startswith("git+"):
        url, _, fragment = url[4:].partition("#")
        base = url.rstrip("/").rsplit("/", 1)[-1]
        base = base[:-4] if base.endswith(".git") else base
        return name or base, "git", url, fragment
    if "://" in url:
        return name or url.rsplit("/", 1)[-1], "download", url, ""
    return name or url, "local", url, ""


def populate(name, srcdir):
    """srcdir as makepkg leaves it before prepare(): local files linked, git sources checked
    out from the cache, archives unpacked."""
    sources, noextract, _ = read(name)
    srcdir.mkdir(parents=True, exist_ok=True)
    for entry in sources:
        fname, kind, loc, fragment = parse(entry)
        if kind == "local":
            os.symlink(PKG / name / loc, srcdir / Path(loc).name)
            continue
        cached = SRCDEST / fname
        if not cached.exists():
            raise Unavailable(f"{fname} is not in {SRCDEST}")
        if kind == "git":
            ref = "HEAD"
            if fragment:
                key, _, value = fragment.partition("=")
                ref = value if key in ("commit", "tag") else f"refs/heads/{value}"
            try:
                _git("rev-parse", "-q", "--verify", f"{ref}^{{commit}}", cwd=cached)
            except subprocess.CalledProcessError:
                raise Unavailable(f"{fname}: {ref} is not in the cache (built elsewhere?)")
            _git("clone", "-q", "-s", "--no-checkout", str(cached), str(srcdir / fname))
            _git("checkout", "-q", "--detach", ref, cwd=srcdir / fname)
            continue
        os.symlink(cached, srcdir / fname)
        if fname.endswith(ARCHIVES) and fname not in noextract:
            if cached.stat().st_size > MAX_ARCHIVE:
                raise Unavailable(f"{fname} is {cached.stat().st_size >> 20} MiB")
            subprocess.run(["bsdtar", "-xf", str(cached), "-C", str(srcdir)], check=True,
                           capture_output=True)


def run_prepare(name, work):
    """Runs prepare() in work/src without network; returns the CompletedProcess."""
    srcdir = work / "src"
    populate(name, srcdir)
    script = r"""
set -eE
cd "$startdir"
source ./PKGBUILD
cd "$srcdir"
prepare
"""
    env = {"PATH": "/usr/bin:/bin", "HOME": str(work), "CARCH": "aarch64",
           "LC_ALL": "C.UTF-8", "srcdir": str(srcdir), "pkgdir": str(work / "pkg"),
           "startdir": str(PKG / name), "SRCDEST": str(SRCDEST),
           "GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "safe.directory",
           "GIT_CONFIG_VALUE_0": "*",
           # In the namespace the checkout belongs to uid 0: the index must not compare owners
           "GIT_CONFIG_KEY_1": "core.checkStat", "GIT_CONFIG_VALUE_1": "minimal",
           # git apply / am in prepare() need an identity
           "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@invalid",
           "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@invalid"}
    # Fetching crates needs the network: prepare() may try, the patches before it are the test
    stubs = work / "stubs"
    stubs.mkdir(exist_ok=True)
    for tool, out in (("cargo", ""), ("rustc", "aarch64-unknown-linux-gnu")):
        (stubs / tool).write_text(f"#!/bin/sh\necho {out}\n")
        (stubs / tool).chmod(0o755)
    env["PATH"] = f"{stubs}:{env['PATH']}"
    unshare = ["unshare", "-rn"] if shutil.which("unshare") else []
    return subprocess.run([*unshare, "bash", "-c", script], env=env, capture_output=True,
                          text=True, timeout=300)
