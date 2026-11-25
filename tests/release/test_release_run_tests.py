# tools/run-tests.sh around pytest: a test that fails among the others gets one more run on its
# own, and a full run feeds the test gate (tools/test-gate.py) of tools/publish.sh. The script
# runs as a copy in a small tree in tmp_path (tools/ with run-tests.sh, test-gate.py and
# test-shards.py, one package, a few real pytest files), so the tests it runs are these files.

import json
import os
import re
import shutil
import subprocess

import pytest

from conftest import TOOLS

OK = "def test_ok():\n    assert True\n"
# Fails the first time it runs in this tree, passes after
WOBBLY = """from pathlib import Path


def test_wobbly():
    seen = Path(__file__).with_name("seen")
    if not seen.exists():
        seen.write_text("1")
        assert False, "first run"
"""
BROKEN = "def test_broken():\n    assert False, 'always'\n"


class Tree:
    def __init__(self, tmp_path):
        self.root = tmp_path / "volla"
        (self.root / "tools").mkdir(parents=True)
        for s in ("run-tests.sh", "test-gate.py", "test-shards.py"):
            shutil.copy(TOOLS / s, self.root / "tools" / s)
        pkg = self.root / "pkgbuilds" / "foo"
        pkg.mkdir(parents=True)
        (pkg / "PKGBUILD").write_text("pkgname=foo\npkgver=1\npkgrel=1\n")
        (self.root / "tests").mkdir()
        self.result = self.root / "build" / "test-pass.json"
        self.tmp = tmp_path / "tmp"
        self.tmp.mkdir()

    def tests(self, **files):
        for name, text in files.items():
            (self.root / "tests" / f"test_{name}.py").write_text(text)

    def run(self, *args):
        # Neither the outer coverage measurement nor its gcov directory reaches the inner run:
        # it would measure the copies in tmp_path
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("COVERAGE_", "PLINIUS_", "PYTEST_"))}
        env.update(TMPDIR=str(self.tmp), GIT_CEILING_DIRECTORIES=str(self.root.parent))
        r = subprocess.run(["bash", str(self.root / "tools/run-tests.sh"), *args], env=env,
                           cwd=self.root, capture_output=True, text=True, timeout=300)
        # A red run with groups keeps its logs in /tmp for reading; the test reads r instead
        for logs in re.findall(r"Logs of the groups: (\S+)/\*\.log", r.stdout):
            assert logs.startswith("/tmp/plinius-tests.")
            shutil.rmtree(logs)
        return r

    def passed(self):
        return json.loads(self.result.read_text())


@pytest.fixture
def tree(tmp_path):
    if not shutil.which("nproc"):
        pytest.skip("nproc missing")
    return Tree(tmp_path)


def out(r):
    return f"exit {r.returncode}\n{r.stdout}\n{r.stderr}"


def test_green_full_run_records_the_packages(tree):
    tree.tests(ok=OK)
    r = tree.run("--jobs", "2")
    assert r.returncode == 0, out(r)
    p = tree.passed()
    assert list(p["packages"]) == ["foo"] and p["wobbly"] == []
    assert "test gate: 1 package directories passed the full run" in r.stderr
    assert list(tree.tmp.iterdir()) == []  # the snapshot is gone


def test_single_process_full_run_counts_too(tree):
    tree.tests(ok=OK)
    r = tree.run("--jobs", "1")
    assert r.returncode == 0, out(r)
    assert list(tree.passed()["packages"]) == ["foo"]


def test_a_wobbly_test_passes_when_run_again_alone(tree):
    tree.tests(ok=OK, wobbly=WOBBLY)
    r = tree.run("--jobs", "2")
    assert r.returncode == 0, out(r)
    assert "FAILED tests/test_wobbly.py::test_wobbly" in r.stdout
    assert "again alone: 1 passed" in r.stdout
    assert "WOBBLY (failed among the others, passed alone): tests/test_wobbly.py::test_wobbly" \
        in r.stdout
    assert tree.passed()["wobbly"] == ["tests/test_wobbly.py::test_wobbly"]


def test_a_broken_test_stays_red_and_takes_the_gate_back(tree):
    tree.tests(ok=OK)
    assert tree.run("--jobs", "2").returncode == 0
    tree.tests(broken=BROKEN)
    r = tree.run("--jobs", "2")
    assert r.returncode == 1, out(r)
    assert "again alone: 1 failed" in r.stdout
    assert "WOBBLY" not in r.stdout
    assert not tree.result.exists()
    assert "no package counts as tested" in r.stderr


def test_single_process_gets_no_second_run(tree):
    tree.tests(wobbly=WOBBLY)
    r = tree.run("--jobs", "1")
    assert r.returncode == 1, out(r)
    assert "again alone" not in r.stdout
    assert not tree.result.exists()


@pytest.mark.parametrize("args", [["tests/test_ok.py"], ["--quick"], ["-k", "ok"]])
def test_a_partial_run_leaves_the_gate_alone(tree, args):
    tree.tests(ok=OK, broken=BROKEN)
    tree.result.parent.mkdir()
    tree.result.write_text('{"earlier": true}')
    r = tree.run("--jobs", "2", *args)
    assert "test gate" not in r.stderr, out(r)
    assert tree.result.read_text() == '{"earlier": true}'
