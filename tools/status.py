#!/usr/bin/env python3
"""Shows what is waiting in the maintenance of our packages (docs/PAKETIERUNG.md, section 4).

Three questions, one report:
  1. Upstream: is there a newer release (tag) of the project a package builds from, or has the
     branch we follow moved past the pinned commit? (git ls-remote, no clone) A pin on a release
     follows the stable branch of that release, if there is one: the default branch holds the next
     version, not an update. Also: point releases of further git sources that are pinned on a
     release (wlroots in phoc), newer versions and known vulnerabilities of vendored PyPI packages.
  2. ALARM: is a package we fork (provides=, e.g. gnome-control-center-plinius → gnome-control-center)
     newer on the mirrors than our pkgver? Has ALARM started shipping a package under our name?
  3. Approval: which coupled packages are newer on the mirrors than plinius-freigabe allows?
     Devices hold back every -Syu until they are approved; this is the trigger for tools/candidate.sh.

Usage: [ARCH=x86_64] tools/status.py [--upstream] [--alarm] [--waiting] [PACKAGE...]
  Without section options all three are shown; without packages all of pkgbuilds/.
  Exit status 1 if anything is waiting, so a timer or CI job can act on it.
"""
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import freigabe  # noqa: E402  (mirror databases, ARCH, paths)

PKGBUILDS = os.path.join(freigabe.ROOT, "pkgbuilds")

# Packages whose first source is not the project to watch, or whose default branch is the wrong
# one. Value: (git URL, branch) or None to skip the upstream check.
UPSTREAM = {
    # The UT kernel: the Halium branch, not the default branch of kernel-android-common
    "plinius-kmod": ("https://gitlab.com/ubports/porting/community-ports/android12/generic/"
                     "kernel-android-common.git", "android14-6.1-halium"),
}

# Own programs built on an upstream tree: compare upstream against the version of this package
SAME_UPSTREAM = {"plinius-progress": "buffybox"}

# Commit pins on a branch that is neither the default branch nor named after a release
FOLLOW_BRANCH = {"android-headers": "halium-14.0"}

# Tags that say nothing about our state: ofono2mm's 1.0.4.1 is from 2024, on a side branch, and
# sorts above the snapshot version of the current default branch
NO_RELEASES = {"ofono2mm"}

# Packages that follow one release series of a project whose repository also holds the next one:
# gtk3-plinius builds GTK 3.24, the tags of GTK 4 (and its 3.9x previews) are no update for it
RELEASE_SERIES = {"gtk3-plinius": "3.24"}

# Tags that are releases: digits separated by dots or underscores, optionally behind a
# prefix like "v", "release-" or "hostap_". Pre-releases (alpha, beta, rc, dev) are ignored.
RELEASE_TAG = re.compile(r"^(?:[A-Za-z]+[-_]?)?v?(\d+(?:[._]\d+)*)$")


def srcinfo(pkgdir):
    """pkgbase, pkgver, names, provides and sources of a PKGBUILD (makepkg --printsrcinfo)."""
    out = subprocess.run(["makepkg", "--printsrcinfo"], cwd=pkgdir, capture_output=True,
                         text=True, check=True).stdout
    info = {"pkgname": [], "provides": [], "source": []}
    for line in out.splitlines():
        m = re.match(r"^\s*(\w+?)(?:_\w+)? = (.*)$", line)
        if not m:
            continue
        key, value = m.groups()
        if key in ("pkgbase", "pkgver"):
            info[key] = value
        elif key in info and value not in info[key]:
            info[key].append(value)
    return info


def upstream_of(pkg, sources):
    """(git URL, pinned commit or tag, branch) of the first remote source, or None."""
    if pkg in UPSTREAM:
        if UPSTREAM[pkg] is None:
            return None
        url, branch = UPSTREAM[pkg]
        # The commit from the archive URL (.../-/archive/<commit>/...)
        pin = next((m.group(1) for s in sources
                    if (m := re.search(r"/-/archive/([0-9a-f]{7,40})/", s))), None)
        return url, pin, branch
    for s in sources:
        s = s.split("::", 1)[-1]
        if s.startswith("git+"):
            url, _, frag = s[4:].partition("#")
            pin = frag.split("=", 1)[1] if "=" in frag else None
            branch = frag.split("=", 1)[1] if frag.startswith("branch=") else None
            return url, None if branch else pin, branch
        m = re.match(r"(https://github\.com/[^/]+/[^/]+)/archive/refs/tags/(.+?)\.tar\.gz$", s)
        if m:
            return m.group(1) + ".git", m.group(2), None
        m = re.match(r"(https://[^/]+/.+?)/-/archive/([^/]+)/", s)
        if m:
            return m.group(1) + ".git", m.group(2), None
    return None


def ls_remote(url):
    """{ref: commit} of a remote repository; peeled tags (^{}) replace the tag object."""
    out = subprocess.run(["git", "ls-remote", url], capture_output=True, text=True, timeout=60,
                         env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, check=True).stdout
    refs = {}
    for line in out.splitlines():
        commit, ref = line.split("\t")
        refs[ref.removesuffix("^{}")] = commit
    return refs


def vercmp(a, b):
    """pacman's version comparison: <0, 0, >0."""
    return int(subprocess.run(["vercmp", a, b], capture_output=True, text=True).stdout)


def newest_release(refs, series=None):
    """(tag, version) of the highest release tag, or (None, None); only of one release series
    (version prefix like 3.24) if given."""
    best = (None, None)
    for ref in refs:
        if not ref.startswith("refs/tags/"):
            continue
        tag = ref[len("refs/tags/"):]
        m = RELEASE_TAG.match(tag)
        if not m:
            continue
        version = m.group(1).replace("_", ".")
        # Dated snapshot tags (next-20230125.1517) are not releases
        if len(version.split(".")[0]) >= 4:
            continue
        if series is not None and version != series and not version.startswith(series + "."):
            continue
        if best[1] is None or vercmp(version, best[1]) > 0:
            best = (tag, version)
    return best


def our_version(info, pin, refs):
    """The upstream version we build: that of a pinned release tag, else pkgver."""
    if pin and f"refs/tags/{pin}" in refs:
        m = RELEASE_TAG.match(pin)
        if m:
            return m.group(1).replace("_", ".")
    return info["pkgver"]


def release_of_commit(refs, commit):
    """(tag, version) of the release tag on a commit, or (None, None)."""
    for ref, target in refs.items():
        if ref.startswith("refs/tags/") and target.startswith(commit):
            tag = ref[len("refs/tags/"):]
            m = RELEASE_TAG.match(tag)
            if m:
                return tag, m.group(1).replace("_", ".")
    return None, None


def has_release(refs, version):
    """Whether a release tag of exactly this version exists."""
    for ref in refs:
        if ref.startswith("refs/tags/"):
            m = RELEASE_TAG.match(ref[len("refs/tags/"):])
            if m and m.group(1).replace("_", ".") == version:
                return True
    return False


def project_name(url):
    """Name of a repository from its URL: libhandy for https://gitlab.gnome.org/GNOME/libhandy.git."""
    name = url.rstrip("/").rsplit("/", 1)[-1]
    return name[:-len(".git")] if name.endswith(".git") else name


def stable_branch(refs, version, project=None):
    """The branch that receives fixes for a release version, by its name, or None.

    GNOME: gnome-51 for 51.1. Elsewhere major and minor: 1.6, v0.10, 2_12, libhandy-1-8.
    project is the repository's name: a branch named after it (libhandy-1-8) wins over other
    branches of the same series, like the shorter msvc-1-8 of a port.
    """
    major, _, rest = version.partition(".")
    minor = rest.split(".")[0]
    names = [f"refs/heads/gnome-{major}"]
    if minor:
        names += [f"refs/heads/{major}.{minor}", f"refs/heads/v{major}.{minor}",
                  f"refs/heads/{major}_{minor}"]
        if project:
            names.append(f"refs/heads/{project}-{major}-{minor}")
    for name in names:
        if name in refs:
            return name[len("refs/heads/"):]
    if minor:
        # The shortest name: gtk-3-24, not a feature branch like backport-5702-3-24
        found = sorted((ref[len("refs/heads/"):] for ref in refs
                        if ref.startswith("refs/heads/") and ref.endswith(f"-{major}-{minor}")),
                       key=lambda name: (len(name), name))
        if found:
            return found[0]
    return None


def check_bundled(pkg, sources):
    """Report lines for further git sources pinned on a release: a newer point release of it.

    A new minor version would mean porting (wlroots 0.21 for phoc), that one comes with the
    package itself. Sources not pinned on a release (lvgl, gvdb) have no meaningful comparison.
    """
    lines = []
    git_sources = [s.split("::", 1)[-1] for s in sources if "git+" in s]
    for s in git_sources[1:]:
        url, _, frag = s[4:].partition("#")
        if not frag.startswith("commit="):
            continue
        try:
            refs = ls_remote(url)
        except (subprocess.SubprocessError, OSError) as e:
            lines.append(f"{pkg}: {url} not reachable ({e.__class__.__name__})")
            continue
        _, ours = release_of_commit(refs, frag.split("=", 1)[1])
        if not ours:
            continue
        # 1.2.3 → "1.2."; a tag of two parts (0.18) is the series itself: "0.18.", or every
        # minor version (0.19) would count as one of its point releases
        parts = ours.split(".")
        series = ".".join(parts[:-1] if len(parts) > 2 else parts) + "."
        same_series = {r: c for r, c in refs.items()
                       if (m := RELEASE_TAG.match(r[len("refs/tags/"):]))
                       and r.startswith("refs/tags/")
                       and m.group(1).replace("_", ".").startswith(series)}
        tag, version = newest_release(same_series)
        if version and vercmp(version, ours) > 0:
            name = url.rsplit("/", 1)[-1].removesuffix(".git")
            lines.append(f"{pkg}: bundled {name} {tag} (we have {ours})")
    return lines


def pypi(path):
    """JSON of a PyPI project or release (path: 'name' or 'name/version')."""
    with urllib.request.urlopen(f"https://pypi.org/pypi/{path}/json", timeout=30) as response:
        return json.load(response)


def check_pypi(pkg, sources):
    """Report lines for vendored PyPI packages: known vulnerabilities and newer versions."""
    lines = []
    for s in sources:
        m = re.match(r".*files\.pythonhosted\.org/packages/source/./[^/]+/(.+)-([^-/]+)\.tar\.gz$", s)
        if not m:
            continue
        name, ours = m.groups()
        try:
            latest = pypi(name)["info"]["version"]
            vulns = pypi(f"{name}/{ours}").get("vulnerabilities", [])
        except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
            lines.append(f"{pkg}: PyPI not reachable for {name} ({e.__class__.__name__})")
            continue
        if vulns:
            ids = ", ".join(v["id"] for v in vulns)
            lines.append(f"{pkg}: vendored {name} {ours} HAS KNOWN VULNERABILITIES: {ids}")
        if vercmp(latest, ours) > 0:
            lines.append(f"{pkg}: vendored {name} {latest} (we have {ours})")
    return lines


def check_upstream(pkg, info, infos):
    """All report lines for the upstream state of a package."""
    line = check_main_source(pkg, info, infos)
    return ([line] if line else []) + check_bundled(pkg, info["source"]) + \
        check_pypi(pkg, info["source"])


def check_main_source(pkg, info, infos):
    """One report line for the project a package builds from, or None if there is nothing to say."""
    if pkg in SAME_UPSTREAM:
        base = SAME_UPSTREAM[pkg]
        base_info = infos.get(base) or srcinfo(os.path.join(PKGBUILDS, base))
        info = {**info, "pkgver": base_info["pkgver"]}
    src = upstream_of(pkg, info["source"])
    if not src:
        return None
    url, pin, branch = src
    try:
        refs = ls_remote(url)
    except (subprocess.SubprocessError, OSError) as e:
        return f"{pkg}: {url} not reachable ({e.__class__.__name__})"
    notes = []
    tag, version = (None, None) if pkg in NO_RELEASES else \
        newest_release(refs, RELEASE_SERIES.get(pkg))
    ours = our_version(info, pin, refs)
    # Dated snapshots (1.4.0.20260508) count as past the release they start with
    if version and vercmp(version, ours) > 0:
        notes.append(f"release {tag} (we have {ours})")
    pinned = refs.get(f"refs/tags/{pin}", pin) if pin else None
    branch = branch or FOLLOW_BRANCH.get(pkg)
    if not branch and pinned:
        # Pinned on a release: only its stable branch is an update, the default branch is the
        # next version. Without a stable branch, only a newer release (above) is news.
        _, pinned_release = release_of_commit(refs, pinned)
        if pinned_release:
            branch = stable_branch(refs, pinned_release, project_name(url))
            if not branch:
                return f"{pkg}: " + "; ".join(notes) if notes else None
        elif has_release(refs, info["pkgver"]):
            # A commit after the release pkgver names (translations taken over from gnome-51
            # after 51.1): it sits on that release's stable branch, if there is one
            branch = stable_branch(refs, info["pkgver"], project_name(url))
    head = refs.get(f"refs/heads/{branch}") if branch else refs.get("HEAD")
    if head and pinned and not head.startswith(pinned) and not notes:
        # Only without a newer release: the branch we follow has moved on
        notes.append(f"{branch or 'HEAD'} {head[:10]} past pin {pinned[:10]}")
    return f"{pkg}: " + "; ".join(notes) if notes else None


def check_alarm(pkg, info, mirror):
    """Report lines for forks behind ALARM and for packages ALARM now ships under our name."""
    lines = []
    for name in info["pkgname"]:
        if name in mirror and not name.endswith("-debug"):
            lines.append(f"{name}: ALARM now ships {name} {mirror[name]} itself (we have "
                         f"{info['pkgver']}), drop ours?")
    for p in info["provides"]:
        name, _, version = p.partition("=")
        if name not in mirror or name in info["pkgname"]:
            continue
        ours = (version.rsplit("-", 1)[0] if version else info["pkgver"]).split(":")[-1]
        theirs = mirror[name].split(":")[-1].rsplit("-", 1)[0]
        if vercmp(theirs, ours) > 0:
            lines.append(f"{pkg}: ALARM has {name} {mirror[name]}, our fork is at {ours}")
    return lines


def check_waiting(mirror):
    """Coupled packages newer on the mirrors than the approval allows: 'name approved → mirror'."""
    text = open(freigabe.PKGBUILD).read()
    block = re.search(rf"^conflicts_{freigabe.ARCH}=\((.*?)^\)", text, re.S | re.M)
    lines = []
    for name, limit in re.findall(r'"([^">]+)>([^"]+)"', block.group(1) if block else ""):
        if name in mirror and vercmp(mirror[name], limit) > 0:
            lines.append(f"{name}: approved {limit}, mirror {mirror[name]}")
    return lines


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    opts = {a for a in sys.argv[1:] if a.startswith("--")}
    if opts - {"--upstream", "--alarm", "--waiting"}:
        sys.exit(__doc__)
    sections = opts or {"--upstream", "--alarm", "--waiting"}
    pkgs = args or sorted(d for d in os.listdir(PKGBUILDS)
                          if os.path.isfile(os.path.join(PKGBUILDS, d, "PKGBUILD")))
    infos = {p: srcinfo(os.path.join(PKGBUILDS, p)) for p in pkgs}
    mirror = freigabe.versions() if sections & {"--alarm", "--waiting"} else {}
    pending = 0

    if "--upstream" in sections:
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            lines = [l for ls in pool.map(lambda p: check_upstream(p, infos[p], infos), pkgs)
                     for l in ls]
        print(f"== Upstream ({len(lines)})")
        print("\n".join(lines) or "nothing new")
        pending += len(lines)
    if "--alarm" in sections:
        lines = [l for p in pkgs for l in check_alarm(p, infos[p], mirror)]
        print(f"== ALARM {freigabe.ARCH} ({len(lines)})")
        print("\n".join(lines) or "all forks at the ALARM version")
        pending += len(lines)
    if "--waiting" in sections:
        lines = check_waiting(mirror)
        print(f"== Waiting for approval, {freigabe.ARCH} ({len(lines)})")
        print("\n".join(lines) or "nothing, devices update freely")
        if lines:
            print("→ tools/candidate.sh checks whether our packages work with the new state")
        pending += len(lines)
    sys.exit(1 if pending else 0)


if __name__ == "__main__":
    main()
