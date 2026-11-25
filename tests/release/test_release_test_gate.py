# tools/test-gate.py: which package directories passed a full green test run, and the check
# tools/publish.sh makes before a package goes to beta. The script runs against a tree in
# tmp_path (its ROOT moved there); the result file is in tmp_path too (PLINIUS_TEST_PASS).

import json
import os
import subprocess

import pytest

from conftest import TOOLS, load_script
from release_sim import write_db

SCRIPT = TOOLS / "test-gate.py"


class Tree:
    def __init__(self, tmp_path, monkeypatch):
        self.root = tmp_path / "volla"
        self.pkgbuilds = self.root / "pkgbuilds"
        self.pkgbuilds.mkdir(parents=True)
        self.result = tmp_path / "test-pass.json"
        self.repo = tmp_path / "repo"
        self.beta = tmp_path / "beta"
        self.repo.mkdir()
        self.beta.mkdir()
        self.mod = load_script(SCRIPT)
        monkeypatch.setattr(self.mod, "ROOT", str(self.root))
        monkeypatch.setattr(self.mod, "PKGBUILDS", str(self.pkgbuilds))
        monkeypatch.setenv("PLINIUS_TEST_PASS", str(self.result))
        # A tree that is no checkout must not find the repository around tmp_path either
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
        self.snap = tmp_path / "snap.json"

    def package(self, name, version="1.0-1", epoch=None, files=None):
        d = self.pkgbuilds / name
        d.mkdir(exist_ok=True)
        ver, rel = version.split("-")
        text = f"pkgname={name}\npkgver={ver}\npkgrel={rel}\n"
        if epoch:
            text += f"epoch={epoch}\n"
        (d / "PKGBUILD").write_text(text)
        for f, data in (files or {}).items():
            (d / f).parent.mkdir(parents=True, exist_ok=True)
            (d / f).write_text(data)
        return d

    def built(self, packages, in_beta=()):
        """packages: {file stem: (base, version)}; in_beta: stems beta already has identically."""
        entries = {}
        for stem, (base, version) in packages.items():
            fn = f"{stem}-aarch64.pkg.tar.zst"
            (self.repo / fn).write_bytes(f"package {stem}".encode())
            if stem in in_beta:
                (self.beta / fn).write_bytes(f"package {stem}".encode())
            entries[stem] = {"FILENAME": fn, "NAME": stem.rsplit("-", 2)[0], "BASE": base,
                             "VERSION": version}
        return write_db(self.repo / "plinius-local.db.tar.gz", entries)

    def main(self, *args):
        return self.mod.main(list(args))

    def green_run(self, *wobbly, change_during=None):
        assert self.main("snapshot", str(self.snap)) == 0
        if change_during:
            change_during()
        return self.main("pass", str(self.snap), *wobbly)

    def check(self):
        return self.main("check", str(self.repo / "plinius-local.db.tar.gz"), str(self.beta))

    def passed(self):
        return json.loads(self.result.read_text())


@pytest.fixture
def tree(tmp_path, monkeypatch):
    return Tree(tmp_path, monkeypatch)


def git(root, *args):
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.org",
                    *args], check=True, capture_output=True)


# --- fingerprints ---------------------------------------------------------------------------------

def test_fingerprint_follows_path_content_and_exec_bit(tree):
    d = tree.package("foo", files={"a.c": "int a;", "sub/b.sh": "echo"})
    tree.package("bar")
    first = tree.mod.fingerprints()
    assert sorted(first) == ["bar", "foo"]
    assert tree.mod.fingerprints() == first
    (d / "a.c").write_text("int a = 1;")
    content = tree.mod.fingerprints()
    assert content["foo"] != first["foo"] and content["bar"] == first["bar"]
    (d / "sub/b.sh").chmod(0o755)
    mode = tree.mod.fingerprints()["foo"]
    assert mode != content["foo"]
    (d / "sub/b.sh").rename(d / "b.sh")
    assert tree.mod.fingerprints()["foo"] != mode


def test_fingerprint_of_a_symlink_is_its_target(tree):
    d = tree.package("foo", files={"x": "1", "y": "2"})
    (d / "link").symlink_to("x")
    first = tree.mod.fingerprints()["foo"]
    (d / "x").write_text("changed")
    after_target = tree.mod.fingerprints()["foo"]
    (d / "link").unlink()
    (d / "link").symlink_to("y")
    assert len({first, after_target, tree.mod.fingerprints()["foo"]}) == 3


def test_fingerprint_in_a_checkout_leaves_ignored_files_out(tree):
    d = tree.package("foo", files={"a.c": "int a;"})
    (tree.root / ".gitignore").write_text("/pkgbuilds/foo/mirror/\n")
    git(tree.root, "init", "-q")
    git(tree.root, "add", "-A")
    git(tree.root, "commit", "-q", "-m", "x")
    first = tree.mod.fingerprints()
    # A bare source mirror next to the PKGBUILD changes nothing, a new untracked file does
    (d / "mirror").mkdir()
    (d / "mirror/HEAD").write_text("ref: refs/heads/main\n")
    assert tree.mod.fingerprints() == first
    (d / "new.patch").write_text("patch")
    assert tree.mod.fingerprints()["foo"] != first["foo"]
    # A versioned file deleted in the tree counts as gone, not as an error
    (d / "a.c").unlink()
    (d / "new.patch").unlink()
    assert tree.mod.fingerprints()["foo"] != first["foo"]


def test_only_files_inside_a_package_directory_count(tree):
    (tree.pkgbuilds / "README").write_text("top level")
    tree.package("foo")
    fifo = tree.pkgbuilds / "foo" / "fifo"
    os.mkfifo(fifo)
    assert list(tree.mod.fingerprints()) == ["foo"]


# --- pass / fail ------------------------------------------------------------------------------------

def test_green_run_records_the_directories(tree):
    tree.package("foo")
    tree.package("bar")
    assert tree.green_run() == 0
    p = tree.passed()
    assert p["packages"] == tree.mod.fingerprints()
    assert p["changed_during_run"] == [] and p["wobbly"] == []
    assert p["commit"] is None  # no checkout
    assert p["time"].endswith("Z")


def test_green_run_leaves_out_what_changed_while_it_ran(tree, capsys):
    tree.package("foo")
    bar = tree.package("bar")

    def change():
        (bar / "x.c").write_text("new")
        tree.package("baz")
        import shutil
        shutil.rmtree(tree.pkgbuilds / "foo")

    tree.green_run(change_during=change)
    p = tree.passed()
    assert p["packages"] == {}
    assert p["changed_during_run"] == ["bar", "baz", "foo"]
    assert "not counted as tested: bar, baz, foo" in capsys.readouterr().err


def test_green_run_names_its_wobbly_tests_and_the_commit(tree):
    tree.package("foo")
    git(tree.root, "init", "-q")
    git(tree.root, "add", "-A")
    git(tree.root, "commit", "-q", "-m", "x")
    tree.green_run("tests/b.py::t", "tests/a.py::t[1]")
    p = tree.passed()
    assert p["wobbly"] == ["tests/a.py::t[1]", "tests/b.py::t"]
    assert len(p["commit"]) == 40


def test_red_run_takes_everything_back(tree, capsys):
    tree.package("foo")
    tree.green_run()
    assert tree.main("fail") == 0
    assert not tree.result.exists()
    assert tree.main("fail") == 0  # nothing to take back
    assert "no package counts as tested" in capsys.readouterr().err


# --- check ------------------------------------------------------------------------------------------

def test_check_nothing_new_for_beta(tree, capsys):
    tree.package("foo")
    tree.built({"foo-1.0-1": ("foo", "1.0-1")}, in_beta={"foo-1.0-1"})
    assert tree.check() == 0  # without any test run: beta has it already
    assert "nothing new for beta" in capsys.readouterr().err


def test_check_without_a_green_run(tree, capsys):
    tree.package("foo")
    tree.built({"foo-1.0-1": ("foo", "1.0-1")})
    assert tree.check() == 1
    err = capsys.readouterr().err
    assert "Error: test gate: foo: no green full test run" in err
    assert "tools/run-tests.sh without arguments makes a full run" in err


def test_check_passes_what_the_green_run_saw(tree, capsys):
    tree.package("foo")
    tree.package("bar")
    tree.built({"foo-1.0-1": ("foo", "1.0-1"), "bar-1.0-1": ("bar", "1.0-1")},
               in_beta={"bar-1.0-1"})
    tree.green_run("tests/x.py::t")
    assert tree.check() == 0
    err = capsys.readouterr().err
    assert f"1 new for beta, all from sources of the green full test run of {tree.passed()['time']}" in err
    assert "wobbly test (failed among the others, passed alone): tests/x.py::t" in err


def test_check_a_package_beta_has_in_another_build(tree):
    tree.package("foo")
    tree.built({"foo-1.0-1": ("foo", "1.0-1")})
    (tree.beta / "foo-1.0-1-aarch64.pkg.tar.zst").write_bytes(b"package foo-1.0-X")  # same size
    assert tree.check() == 1
    (tree.beta / "foo-1.0-1-aarch64.pkg.tar.zst").write_bytes(b"other size")
    assert tree.check() == 1
    (tree.beta / "foo-1.0-1-aarch64.pkg.tar.zst").write_bytes(b"package foo-1.0-1")
    assert tree.check() == 0


def test_check_large_identical_packages_are_compared_in_full(tree):
    tree.package("foo")
    tree.built({"foo-1.0-1": ("foo", "1.0-1")})
    data = os.urandom(3 << 20)
    (tree.repo / "foo-1.0-1-aarch64.pkg.tar.zst").write_bytes(data)
    (tree.beta / "foo-1.0-1-aarch64.pkg.tar.zst").write_bytes(data)
    assert tree.check() == 0
    (tree.beta / "foo-1.0-1-aarch64.pkg.tar.zst").write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    assert tree.check() == 1


def test_check_a_directory_changed_after_the_run(tree, capsys):
    d = tree.package("foo")
    tree.built({"foo-1.0-1": ("foo", "1.0-1")})
    tree.green_run()
    (d / "fix.patch").write_text("later")
    assert tree.check() == 1
    assert f"foo: changed since the last green full test run ({tree.passed()['time']})" in \
        capsys.readouterr().err


def test_check_a_package_the_green_run_did_not_have(tree, capsys):
    tree.green_run()
    tree.package("foo")
    tree.built({"foo-1.0-1": ("foo", "1.0-1")})
    assert tree.check() == 1
    assert "foo: not in the last green full test run" in capsys.readouterr().err


def test_check_a_package_built_from_another_version(tree, capsys):
    tree.package("foo", "1.0-2")
    tree.built({"foo-1.0-1": ("foo", "1.0-1")})
    tree.green_run()
    assert tree.check() == 1
    assert "foo: built as 1.0-1, the tree has 1.0-2: rebuild it from the tree" in \
        capsys.readouterr().err


def test_check_split_packages_and_epoch(tree, capsys):
    tree.package("biomd", "0.3-4", epoch=1)
    tree.built({"biomd-1:0.3-4": ("biomd", "1:0.3-4"), "biomd-tools-1:0.3-4": ("biomd", "1:0.3-4")})
    tree.green_run()
    assert tree.check() == 0
    assert "1 new for beta" in capsys.readouterr().err


def test_check_split_packages_from_two_versions(tree, capsys):
    tree.package("biomd", "0.3-4")
    tree.built({"biomd-0.3-4": ("biomd", "0.3-4"), "biomd-tools-0.3-3": ("biomd", "0.3-3")})
    tree.green_run()
    assert tree.check() == 1
    assert "biomd: built as 0.3-3, 0.3-4, the tree has 0.3-4" in capsys.readouterr().err


def test_check_without_a_pkgbuild_or_with_a_broken_one(tree, capsys):
    tree.package("broken")
    (tree.pkgbuilds / "broken" / "PKGBUILD").write_text("pkgver=1\nexit 1\n")
    tree.built({"gone-1-1": ("gone", "1-1"), "broken-1-1": ("broken", "1-1")})
    tree.green_run()
    assert tree.check() == 1
    err = capsys.readouterr().err
    assert "gone: no pkgbuilds/gone/PKGBUILD" in err
    assert "broken: built as 1-1, the tree has a PKGBUILD that does not load" in err


def test_check_database_without_base_takes_the_name(tree):
    tree.package("foo")
    fn = "foo-1.0-1-aarch64.pkg.tar.zst"
    (tree.repo / fn).write_bytes(b"x")
    write_db(tree.repo / "plinius-local.db.tar.gz",
             {"foo-1.0-1": {"FILENAME": fn, "NAME": "foo", "VERSION": "1.0-1",
                            "DEPENDS": ["glib2", "bash"]}})
    tree.green_run()
    assert tree.check() == 0


# --- command line -----------------------------------------------------------------------------------

@pytest.mark.parametrize("args", [[], ["snapshot"], ["pass"], ["fail", "x"], ["check", "db"],
                                  ["other"]])
def test_usage(tree, args, capsys):
    assert tree.main(*args) == 2
    assert "usage: tools/test-gate.py" in capsys.readouterr().err


def test_command_line_on_the_repository(tmp_path):
    """The script as publish.sh and run-tests.sh call it, on this checkout (only reads it)."""
    snap = tmp_path / "snap.json"
    env = dict(os.environ, PLINIUS_TEST_PASS=str(tmp_path / "pass.json"))
    r = subprocess.run(["python3", str(SCRIPT), "snapshot", str(snap)], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "plinius-keyring" in json.loads(snap.read_text())["packages"]
    r = subprocess.run(["python3", str(SCRIPT)], env=env, capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 2
