# tools/status.py: what counts as "waiting" in maintenance (upstream releases, ALARM forks,
# packages held back by the approval). git ls-remote, PyPI and the mirror are replaced by data;
# pacman's vercmp is the real one (pure, no state).

import os
import shutil
import subprocess
import sys

import pytest

from conftest import TOOLS, load_script


@pytest.fixture
def st(monkeypatch):
    if not shutil.which("vercmp"):
        pytest.skip("pacman's vercmp missing on the host")
    monkeypatch.delenv("ARCH", raising=False)
    return load_script(TOOLS / "status.py")


def refs(**kw):
    """refs(tags={...}, heads={...}, head=commit)"""
    out = {}
    for t, c in kw.get("tags", {}).items():
        out[f"refs/tags/{t}"] = c
    for h, c in kw.get("heads", {}).items():
        out[f"refs/heads/{h}"] = c
    if "head" in kw:
        out["HEAD"] = kw["head"]
    return out


C1, C2, C3, C4 = ("1" * 40, "2" * 40, "3" * 40, "4" * 40)


# --- tags and versions ------------------------------------------------------------------------

@pytest.mark.parametrize("tag,version", [
    ("v1.2.3", "1.2.3"), ("1.10", "1.10"), ("release-2.0", "2.0"), ("hostap_2_11", "2.11"),
    ("libhandy1.8", "1.8"), ("2.0-rc1", None), ("v3.0beta", None), ("next-20230125.1517", "20230125.1517"),
])
def test_release_tag_pattern(st, tag, version):
    m = st.RELEASE_TAG.match(tag)
    assert (m.group(1).replace("_", ".") if m else None) == version


def test_newest_release_ignores_prereleases_and_dated_snapshots(st):
    r = refs(tags={"v1.2.3": C1, "1.10.0": C2, "1.9.0": C3, "2.0-rc1": C4,
                   "next-20230125.1517": C4}, heads={"main": C4})
    assert st.newest_release(r) == ("1.10.0", "1.10.0")
    assert st.newest_release(refs(heads={"main": C1})) == (None, None)


def test_our_version_from_a_pinned_release_tag(st):
    info = {"pkgver": "1.2.0.r5"}
    r = refs(tags={"v1.2.0": C1})
    assert st.our_version(info, "v1.2.0", r) == "1.2.0"
    assert st.our_version(info, "v9.9", r) == "1.2.0.r5"
    assert st.our_version(info, None, r) == "1.2.0.r5"


def test_release_of_commit_matches_a_short_id(st):
    r = refs(tags={"v1.2.0": C1, "nightly": C2})
    assert st.release_of_commit(r, C1[:12]) == ("v1.2.0", "1.2.0")
    assert st.release_of_commit(r, C2) == (None, None)


@pytest.mark.parametrize("heads,version,branch", [
    (["gnome-51", "main"], "51.1", "gnome-51"),
    (["1.6", "main"], "1.6.2", "1.6"),
    (["v0.10"], "0.10.1", "v0.10"),
    (["2_12"], "2.12.0", "2_12"),
    (["libhandy-1-8"], "1.8.3", "libhandy-1-8"),
    (["backport-5702-3-24", "gtk-3-24", "main"], "3.24.52", "gtk-3-24"),
    (["main", "1.7"], "1.6.2", None),
])
def test_stable_branch(st, heads, version, branch):
    assert st.stable_branch(refs(heads={h: C1 for h in heads}), version) == branch


# --- where a package comes from -----------------------------------------------------------------

@pytest.mark.parametrize("sources,expected", [
    (["phoc::git+https://gitlab.gnome.org/World/Phosh/phoc.git#tag=v0.58.0", "x.patch"],
     ("https://gitlab.gnome.org/World/Phosh/phoc.git", "v0.58.0", None)),
    (["git+https://github.com/a/b.git#commit=" + C1], ("https://github.com/a/b.git", C1, None)),
    (["git+https://github.com/a/b.git#branch=devel"], ("https://github.com/a/b.git", None, "devel")),
    (["git+https://github.com/a/b.git"], ("https://github.com/a/b.git", None, None)),
    (["b-1.2.tar.gz::https://github.com/a/b/archive/refs/tags/v1.2.tar.gz"],
     ("https://github.com/a/b.git", "v1.2", None)),
    (["https://gitlab.com/g/sub/p/-/archive/v2.0/p-v2.0.tar.gz"],
     ("https://gitlab.com/g/sub/p.git", "v2.0", None)),
    (["https://example.org/x-1.0.tar.xz", "local.patch"], None),
])
def test_upstream_of(st, sources, expected):
    assert st.upstream_of("pkg", sources) == expected


def test_upstream_override_takes_the_commit_from_the_archive_url(st):
    src = ["https://gitlab.com/ubports/x/kernel/-/archive/abcdef1234/kernel-abcdef1234.tar.gz"]
    url, pin, branch = st.upstream_of("plinius-kmod", src)
    assert pin == "abcdef1234" and branch == "android14-6.1-halium"
    assert url.endswith("kernel-android-common.git")


def test_ls_remote_parses_and_peels_tags(st, fake_bin, monkeypatch):
    fake_bin.stub("git", f'''[[ $1 == ls-remote ]] || exit 1
printf '%s\\tHEAD\\n{C1}\\trefs/heads/main\\n{C2}\\trefs/tags/v1.0\\n{C3}\\trefs/tags/v1.0^{{}}\\n' {C1}''')
    monkeypatch.setenv("PATH", f"{fake_bin.path}:{os.environ['PATH']}")
    assert st.ls_remote("https://x") == {"HEAD": C1, "refs/heads/main": C1, "refs/tags/v1.0": C3}
    assert fake_bin.calls("git") == [["ls-remote", "https://x"]]


def test_srcinfo_reads_names_provides_and_architecture_sources(st, fake_bin, monkeypatch, tmp_path):
    fake_bin.stub("makepkg", '''cat <<'EOF'
pkgbase = gnome-control-center-plinius
	pkgdesc = Settings
	pkgver = 51.1
	pkgrel = 83
	source = git+https://gitlab.gnome.org/GNOME/gnome-control-center.git#tag=51.1
	source = 0001.patch
	source_aarch64 = arm.patch
	sha256sums = SKIP

pkgname = gnome-control-center-plinius
	provides = gnome-control-center=51.1
	provides = gnome-control-center=51.1

pkgname = gnome-control-center-plinius-debug
EOF''')
    monkeypatch.setenv("PATH", f"{fake_bin.path}:{os.environ['PATH']}")
    info = st.srcinfo(str(tmp_path))
    assert info["pkgbase"] == "gnome-control-center-plinius" and info["pkgver"] == "51.1"
    assert info["pkgname"] == ["gnome-control-center-plinius", "gnome-control-center-plinius-debug"]
    assert info["provides"] == ["gnome-control-center=51.1"]
    assert info["source"][1:] == ["0001.patch", "arm.patch"]


# --- upstream report ------------------------------------------------------------------------------

def upstream(st, monkeypatch, pkg, sources, r, pkgver="1.2.0", infos=None):
    def ls(url):
        if r is None:
            raise subprocess.CalledProcessError(128, ["git"])
        return r
    monkeypatch.setattr(st, "ls_remote", ls)
    return st.check_main_source(pkg, {"pkgver": pkgver, "source": sources}, infos or {})


def test_newer_release_is_reported(st, monkeypatch):
    r = refs(tags={"v1.2.0": C1, "v1.3.0": C2}, head=C3)
    line = upstream(st, monkeypatch, "phoc", ["git+https://h/phoc.git#tag=v1.2.0"], r)
    assert line == "phoc: release v1.3.0 (we have 1.2.0)"


def test_release_pin_reports_its_moved_stable_branch(st, monkeypatch):
    r = refs(tags={"v1.2.0": C1}, heads={"1.2": C2, "main": C3}, head=C3)
    line = upstream(st, monkeypatch, "p", ["git+https://h/p.git#tag=v1.2.0"], r)
    assert line == f"p: 1.2 {C2[:10]} past pin {C1[:10]}"


def test_release_pin_without_stable_branch_ignores_the_default_branch(st, monkeypatch):
    r = refs(tags={"v1.2.0": C1}, heads={"main": C3}, head=C3)
    assert upstream(st, monkeypatch, "p", ["git+https://h/p.git#tag=v1.2.0"], r) is None


def test_commit_pin_on_the_default_branch_that_moved(st, monkeypatch):
    r = refs(heads={"main": C2}, head=C2)
    line = upstream(st, monkeypatch, "p", [f"git+https://h/p.git#commit={C1}"], r, pkgver="0.1.r40")
    assert line == f"p: HEAD {C2[:10]} past pin {C1[:10]}"
    r = refs(heads={"main": C1}, head=C1)
    assert upstream(st, monkeypatch, "p", [f"git+https://h/p.git#commit={C1[:12]}"], r) is None


def test_follow_branch_for_commit_pins_on_a_side_branch(st, monkeypatch):
    r = refs(heads={"halium-14.0": C2, "main": C3}, head=C3)
    line = upstream(st, monkeypatch, "android-headers", [f"git+https://h/a.git#commit={C1}"], r)
    assert line == f"android-headers: halium-14.0 {C2[:10]} past pin {C1[:10]}"


def test_no_releases_for_packages_whose_tags_mean_nothing(st, monkeypatch):
    r = refs(tags={"1.0.4.1": C2}, heads={"main": C1}, head=C1)
    line = upstream(st, monkeypatch, "ofono2mm", [f"git+https://h/o.git#commit={C1}"], r,
                    pkgver="1.0.4.r20")
    assert line is None


def test_unreachable_upstream(st, monkeypatch):
    line = upstream(st, monkeypatch, "p", ["git+https://h/p.git#tag=v1"], None)
    assert line == "p: https://h/p.git not reachable (CalledProcessError)"


def test_same_upstream_compares_against_the_other_package(st, monkeypatch):
    r = refs(tags={"3.2.0": C1, "3.3.0": C2}, head=C2)
    line = upstream(st, monkeypatch, "plinius-progress", ["git+https://h/buffybox.git#commit=" + C1],
                    r, pkgver="0.1", infos={"buffybox": {"pkgver": "3.2.0"}})
    assert line == "plinius-progress: release 3.3.0 (we have 3.2.0)"


def bundled(st, monkeypatch, r, pin):
    monkeypatch.setattr(st, "ls_remote", lambda url: r)
    return st.check_bundled("phoc", ["git+https://h/phoc.git#tag=v0.58.0",
                                     f"git+https://gitlab.freedesktop.org/wlroots/wlroots.git#commit={pin}"])


def test_bundled_source_newer_point_release(st, monkeypatch):
    r = refs(tags={"0.18.2": C1, "0.18.3": C2, "0.19.0": C3})
    assert bundled(st, monkeypatch, r, C1) == ["phoc: bundled wlroots 0.18.3 (we have 0.18.2)"]
    assert bundled(st, monkeypatch, r, C2) == []  # 0.19 is a port, comes with phoc itself


def test_bundled_source_two_component_tag_reports_no_minor_version(st, monkeypatch):
    r = refs(tags={"0.18": C1, "0.19.0": C3})
    assert bundled(st, monkeypatch, r, C1) == []


def test_bundled_source_not_on_a_release_is_not_compared(st, monkeypatch):
    assert bundled(st, monkeypatch, refs(tags={"0.18.3": C2}), C4) == []


def test_pypi(st, monkeypatch):
    data = {"jeepney": {"info": {"version": "0.9.0"}},
            "jeepney/0.8.0": {"vulnerabilities": [{"id": "PYSEC-1"}, {"id": "GHSA-2"}]}}

    def pypi(path):
        if path not in data:
            raise st.urllib.error.URLError("offline")
        return data[path]
    monkeypatch.setattr(st, "pypi", pypi)
    src = ["https://files.pythonhosted.org/packages/source/j/jeepney/jeepney-0.8.0.tar.gz",
           "https://files.pythonhosted.org/packages/source/o/other/other-1.0.tar.gz", "x.patch"]
    assert st.check_pypi("p", src) == [
        "p: vendored jeepney 0.8.0 HAS KNOWN VULNERABILITIES: PYSEC-1, GHSA-2",
        "p: vendored jeepney 0.9.0 (we have 0.8.0)",
        "p: PyPI not reachable for other (URLError)"]


# --- ALARM and approval ---------------------------------------------------------------------------

def test_alarm_fork_behind_and_ours_shipped_by_alarm(st):
    info = {"pkgver": "51.1", "pkgname": ["gnome-control-center-plinius", "plinius-x-debug"],
            "provides": ["gnome-control-center=51.1", "libfoo=1:2.0-1", "virtual"]}
    mirror = {"gnome-control-center": "51.2-1", "libfoo": "1:2.0-3", "virtual": "60-1",
              "plinius-x-debug": "1-1"}
    assert st.check_alarm("gcc", info, mirror) == [
        "gcc: ALARM has gnome-control-center 51.2-1, our fork is at 51.1",
        "gcc: ALARM has virtual 60-1, our fork is at 51.1"]
    mirror["gnome-control-center-plinius"] = "1-1"
    assert st.check_alarm("gcc", info, mirror)[0].startswith(
        "gnome-control-center-plinius: ALARM now ships gnome-control-center-plinius 1-1 itself")


def test_waiting_for_approval(st, monkeypatch, tmp_path):
    pkgbuild = tmp_path / "PKGBUILD"
    pkgbuild.write_text('pkgname=plinius-freigabe\nconflicts_aarch64=(\n'
                        '\t"glibc>2.42-1"  # core list\n\t"avahi>1:0.9rc5-2"  # linked\n'
                        '\t"mesa>26.0-1"  # core list\n)\nconflicts_x86_64=(\n\t"zlib>1-1"\n)\n')
    monkeypatch.setattr(st.freigabe, "PKGBUILD", str(pkgbuild))
    mirror = {"glibc": "2.42-2", "avahi": "1:0.9rc5-2", "mesa": "25.9-1", "zlib": "9-1"}
    assert st.check_waiting(mirror) == ["glibc: approved 2.42-1, mirror 2.42-2"]


def test_main_exit_status_and_sections(st, monkeypatch, tmp_path, capsys):
    (tmp_path / "pkgs/foo").mkdir(parents=True)
    (tmp_path / "pkgs/foo/PKGBUILD").write_text("")
    monkeypatch.setattr(st, "PKGBUILDS", str(tmp_path / "pkgs"))
    monkeypatch.setattr(st, "srcinfo", lambda d: {"pkgver": "1", "pkgname": ["foo"],
                                                   "provides": [], "source": []})
    monkeypatch.setattr(st.freigabe, "versions", lambda: {})
    monkeypatch.setattr(st, "check_waiting", lambda m: [])
    monkeypatch.setattr(st.sys, "argv", ["status.py", "--alarm", "--waiting"])
    with pytest.raises(SystemExit) as e:
        st.main()
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "== ALARM aarch64 (0)" in out and "== Upstream" not in out
    monkeypatch.setattr(st, "check_waiting", lambda m: ["glibc: approved 1, mirror 2"])
    with pytest.raises(SystemExit) as e:
        st.main()
    assert e.value.code == 1
    assert "tools/candidate.sh" in capsys.readouterr().out
    monkeypatch.setattr(st.sys, "argv", ["status.py", "--bogus"])
    with pytest.raises(SystemExit) as e:
        st.main()
    assert "Usage" in str(e.value.code)


# --- remaining paths ---------------------------------------------------------------------------

def test_upstream_override_none_skips_the_package(st, monkeypatch):
    monkeypatch.setitem(st.UPSTREAM, "local-only", None)
    assert st.upstream_of("local-only", ["git+https://h/p.git#tag=v1"]) is None


def test_package_without_a_remote_source_has_nothing_to_say(st, monkeypatch):
    monkeypatch.setattr(st, "ls_remote", lambda url: pytest.fail("no remote to ask"))
    assert st.check_main_source("p", {"pkgver": "1", "source": ["local.c"]}, {}) is None


def test_our_version_of_a_pinned_tag_that_is_no_release(st):
    assert st.our_version({"pkgver": "0.3.r7"}, "nightly", refs(tags={"nightly": C1})) == "0.3.r7"


def test_stable_branch_of_a_version_without_minor(st):
    assert st.stable_branch(refs(heads={"gnome-51": C1}), "51") == "gnome-51"
    # Without a minor version only gnome-<major> is a candidate, no "-<major>-<minor>" search
    assert st.stable_branch(refs(heads={"lib-51-": C1, "51": C2}), "51") is None


def test_bundled_sources_not_pinned_on_a_commit_or_unreachable(st, monkeypatch):
    asked = []

    def ls(url):
        asked.append(url)
        raise subprocess.TimeoutExpired(["git"], 60)
    monkeypatch.setattr(st, "ls_remote", ls)
    lines = st.check_bundled("phoc", [
        "git+https://h/phoc.git#tag=v0.58.0",
        "git+https://h/lvgl.git#branch=master",           # not pinned on a commit: not compared
        f"git+https://gitlab.freedesktop.org/wlroots/wlroots.git#commit={C1}"])
    assert asked == ["https://gitlab.freedesktop.org/wlroots/wlroots.git"]
    assert lines == ["phoc: https://gitlab.freedesktop.org/wlroots/wlroots.git not reachable "
                     "(TimeoutExpired)"]


def test_pypi_reads_the_json_api(st, monkeypatch):
    import io
    import json as jsonlib
    opened = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()

    def urlopen(url, timeout):
        opened.append((url, timeout))
        return Response(jsonlib.dumps({"info": {"version": "2.0"}}).encode())
    monkeypatch.setattr(st.urllib.request, "urlopen", urlopen)
    assert st.pypi("jeepney/0.8.0") == {"info": {"version": "2.0"}}
    assert opened == [("https://pypi.org/pypi/jeepney/0.8.0/json", 30)]


def test_pypi_current_and_without_vulnerabilities(st, monkeypatch):
    data = {"jeepney": {"info": {"version": "0.8.0"}}, "jeepney/0.8.0": {"vulnerabilities": []}}
    monkeypatch.setattr(st, "pypi", lambda path: data[path])
    src = ["https://files.pythonhosted.org/packages/source/j/jeepney/jeepney-0.8.0.tar.gz"]
    assert st.check_pypi("p", src) == []


def test_check_upstream_collects_all_three_kinds(st, monkeypatch):
    remote = {"https://h/phoc.git": refs(tags={"v0.58.0": C1, "v0.59.0": C2}, head=C2),
              "https://h/wlroots.git": refs(tags={"0.18.2": C3, "0.18.3": C4})}
    monkeypatch.setattr(st, "ls_remote", lambda url: remote[url])
    monkeypatch.setattr(st, "pypi", lambda path: {"info": {"version": "1.1"}} if "/" not in path
                        else {"vulnerabilities": []})
    info = {"pkgver": "0.58.0", "source": [
        "git+https://h/phoc.git#tag=v0.58.0", f"git+https://h/wlroots.git#commit={C3}",
        "https://files.pythonhosted.org/packages/source/x/xpkg/xpkg-1.0.tar.gz"]}
    assert st.check_upstream("phoc", info, {}) == [
        "phoc: release v0.59.0 (we have 0.58.0)",
        "phoc: bundled wlroots 0.18.3 (we have 0.18.2)",
        "phoc: vendored xpkg 1.1 (we have 1.0)"]
    remote["https://h/phoc.git"] = refs(tags={"v0.58.0": C1}, head=C2)  # nothing new upstream
    assert st.check_upstream("phoc", info, {})[0] == "phoc: bundled wlroots 0.18.3 (we have 0.18.2)"


def test_alarm_ignores_provides_of_our_own_names(st):
    info = {"pkgver": "1.0", "pkgname": ["foo", "foo-libs"], "provides": ["foo-libs=1.0"]}
    assert st.check_alarm("foo", info, {"foo-libs-x": "9-1"}) == []
    assert st.check_alarm("foo", info, {"foo-libs": "9-1"}) == [
        "foo-libs: ALARM now ships foo-libs 9-1 itself (we have 1.0), drop ours?"]


def test_main_upstream_section(st, monkeypatch, tmp_path, capsys):
    infos = {"a": {"pkgver": "1"}, "b": {"pkgver": "2"}}
    monkeypatch.setattr(st, "srcinfo", lambda d: infos[os.path.basename(d)])
    monkeypatch.setattr(st.freigabe, "versions", lambda: pytest.fail("no mirror for --upstream"))
    seen = []

    def check(pkg, info, all_infos):
        seen.append((pkg, info, all_infos))
        return [f"{pkg}: release 9"] if pkg == "b" else []
    monkeypatch.setattr(st, "check_upstream", check)
    monkeypatch.setattr(st.sys, "argv", ["status.py", "--upstream", "a", "b"])
    with pytest.raises(SystemExit) as e:
        st.main()
    assert e.value.code == 1
    assert capsys.readouterr().out == "== Upstream (1)\nb: release 9\n"
    assert sorted(p for p, _, _ in seen) == ["a", "b"] and seen[0][2] == infos
    monkeypatch.setattr(st, "check_upstream", lambda *a: [])
    with pytest.raises(SystemExit) as e:
        st.main()
    assert e.value.code == 0
    assert capsys.readouterr().out == "== Upstream (0)\nnothing new\n"


def test_command_line_usage():
    r = subprocess.run([sys.executable, str(TOOLS / "status.py"), "--bogus"], capture_output=True,
                       text=True, timeout=30)
    assert r.returncode == 1 and r.stderr.startswith("Shows what is waiting")


def test_commit_pin_after_a_release_follows_its_stable_branch(st, monkeypatch):
    # Pinned on gnome-51 after the tag 51.1 (translations): main is the next version, no update
    r = refs(tags={"51.1": C1}, heads={"gnome-51": C2, "main": C3}, head=C3)
    src = [f"git+https://h/g.git#commit={C2}"]
    assert upstream(st, monkeypatch, "g", src, r, pkgver="51.1") is None
    r = refs(tags={"51.1": C1}, heads={"gnome-51": C4, "main": C3}, head=C3)
    line = upstream(st, monkeypatch, "g", src, r, pkgver="51.1")
    assert line == f"g: gnome-51 {C4[:10]} past pin {C2[:10]}"


def test_commit_pin_after_a_release_without_stable_branch_compares_the_default_branch(
        st, monkeypatch):
    r = refs(tags={"1.2.0": C1}, heads={"main": C3}, head=C3)
    line = upstream(st, monkeypatch, "p", [f"git+https://h/p.git#commit={C2}"], r)
    assert line == f"p: HEAD {C3[:10]} past pin {C2[:10]}"


def test_release_series_ignores_the_next_major_version(st, monkeypatch):
    r = refs(tags={"3.24.52": C1, "3.24.53": C2, "3.99.5.1": C4, "4.25.1": C3},
             heads={"main": C3}, head=C3)
    src = ["git+https://h/gtk.git#tag=3.24.52"]
    line = upstream(st, monkeypatch, "gtk3-plinius", src, r, pkgver="3.24.52")
    assert line == "gtk3-plinius: release 3.24.53 (we have 3.24.52)"
    r = refs(tags={"3.24.52": C1, "4.25.1": C3}, heads={"main": C3}, head=C3)
    assert upstream(st, monkeypatch, "gtk3-plinius", src, r, pkgver="3.24.52") is None
