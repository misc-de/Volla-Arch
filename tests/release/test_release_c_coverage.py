# tools/c-coverage.py: the C line coverage of the host tests (tools/run-tests.sh --coverage). Real
# gcov counters from tiny programs built here with gcc --coverage, in a repository of its own in
# tmp_path: lines run in any of the builds count, the test programs themselves and files outside
# pkgbuilds/ do not, and versioned C files no build compiled are listed apart.

import json
import shutil
import subprocess
import sys

import pytest

from conftest import TOOLS, load_script

SCRIPT = TOOLS / "c-coverage.py"

# Line numbers matter: the assertions name them
LIB_C = """\
int pick(int x)
{
\tif (x > 0)
\t\treturn 1;
\treturn -1;
}

int never(void)
{
\treturn 42;
}
"""

MAIN_C = """\
#include <stdlib.h>
int pick(int x);
int main(int argc, char **argv)
{
\treturn pick(atoi(argv[1])) > 0 ? 0 : 3;
}
"""


@pytest.fixture
def cc(monkeypatch, tmp_path):
    for tool in ("gcc", "gcov", "git"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} missing on the host")
    mod = load_script(SCRIPT)
    root = tmp_path / "repo"
    (root / "pkgbuilds").mkdir(parents=True)
    monkeypatch.setattr(mod, "ROOT", root)
    monkeypatch.setattr(mod, "PKG", root / "pkgbuilds")
    return mod, root


def git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def build(out, *sources):
    subprocess.run(["gcc", "-O0", "--coverage", "-o", str(out), *map(str, sources)], check=True,
                   capture_output=True)


def put(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_lines_run_by_any_build_count_and_unbuilt_files_are_listed(cc, tmp_path, capsys):
    mod, root = cc
    lib = put(root / "pkgbuilds/demo/lib.c", LIB_C)
    put(root / "pkgbuilds/demo/unbuilt.c", "int a;\nint b;\nint c;\n")
    put(root / "pkgbuilds/demo/README", "not C\n")
    git(root, "init", "-q")
    git(root, "add", ".")
    harness = put(tmp_path / "harness/main.c", MAIN_C)  # outside pkgbuilds/: not counted
    builds = tmp_path / "builds"
    builds.mkdir()
    # Two programs from the same source: one takes the positive branch, the other the negative
    build(builds / "pos", lib, harness)
    build(builds / "neg", lib, harness)
    assert subprocess.run([builds / "pos", "5"]).returncode == 0
    assert subprocess.run([builds / "neg", "-5"]).returncode == 3
    gdir = tmp_path / "gcov"
    gdir.mkdir()
    # A build listed twice counts once; one whose directory is gone has no counters
    (gdir / "builds.txt").write_text(f"{builds / 'pos'}\n{builds / 'neg'}\n{builds / 'pos'}\n"
                                     f"{tmp_path / 'gone/prog'}\n")
    report, js = tmp_path / "report.txt", tmp_path / "c.json"

    mod.main(["c-coverage.py", str(gdir), str(report), "--json", str(js)])

    data = json.loads(js.read_text())
    assert list(data) == ["pkgbuilds/demo/lib.c"]
    # Lines 1-5 run in one or the other build; never() (lines 8-10) in none
    assert data["pkgbuilds/demo/lib.c"]["missing"] == [8, 10]
    lines = data["pkgbuilds/demo/lib.c"]["lines"]
    assert data["pkgbuilds/demo/lib.c"]["covered"] == lines - 2
    text = report.read_text().splitlines()
    assert text[0].split() == ["File", "Lines", "Miss", "Cover"]
    row = next(l.split() for l in text if l.startswith("pkgbuilds/demo/lib.c"))
    assert row == ["pkgbuilds/demo/lib.c", str(lines), "2", f"{(lines - 2) / lines:.0%}"]
    total = next(l for l in text if l.startswith("TOTAL"))
    assert total.split()[-3:] == [str(lines), "2", f"{(lines - 2) / lines:.0%}"]
    # The versioned C file no program compiled, with its number of source lines
    assert text[text.index("C files no test compiles (source lines):") + 1].split() == \
        ["pkgbuilds/demo/unbuilt.c", "3"]
    assert "main.c" not in report.read_text()
    out = capsys.readouterr().out
    assert out.strip() == (f"C: {lines - 2}/{lines} lines ({(lines - 2) / lines:.0%}) in 1 files "
                           "compiled by the tests, 1 C files not compiled by any test")


def test_a_build_that_never_ran_counts_as_compiled_and_not_covered(cc, tmp_path):
    mod, root = cc
    lib = put(root / "pkgbuilds/demo/lib.c", LIB_C)
    git(root, "init", "-q")
    git(root, "add", ".")
    harness = put(tmp_path / "main.c", MAIN_C)
    build(tmp_path / "idle", lib, harness)  # only the .gcno files, the program never runs
    gdir = tmp_path / "gcov"
    gdir.mkdir()
    (gdir / "builds.txt").write_text(f"{tmp_path / 'idle'}\n")
    report = tmp_path / "report.txt"
    mod.main(["c-coverage.py", str(gdir), str(report)])
    text = report.read_text()
    assert "C files no test compiles" not in text
    row = next(l.split() for l in text.splitlines() if l.startswith("pkgbuilds/demo/lib.c"))
    assert row[2] == row[1] and row[3] == "0%"


def test_unreadable_counters_are_skipped(cc, tmp_path):
    mod, root = cc
    git(root, "init", "-q")
    (tmp_path / "b").mkdir()
    (tmp_path / "b/prog-x.gcda").write_bytes(b"not a gcda file")
    assert mod.counters(tmp_path / "b/prog-x.gcda") == {}
    assert mod.gcda_files(tmp_path / "b/prog") == [tmp_path / "b/prog-x.gcda"]


def test_no_builds_and_no_c_files(cc, tmp_path, capsys):
    mod, root = cc
    put(root / "pkgbuilds/demo/PKGBUILD", "pkgname=demo\n")
    git(root, "init", "-q")
    git(root, "add", ".")
    gdir = tmp_path / "gcov"
    gdir.mkdir()
    report = tmp_path / "report.txt"
    mod.main(["c-coverage.py", str(gdir), str(report)])
    text = report.read_text()
    assert text.splitlines()[-1].split()[-3:] == ["0", "0", "100%"]
    assert "C files no test compiles" not in text
    assert capsys.readouterr().out.strip() == (
        "C: 0/0 lines (100%) in 0 files compiled by the tests, 0 C files not compiled by any test")


def test_command_line_on_the_repository(tmp_path):
    # As tools/run-tests.sh runs it: without builds every versioned C file is listed as not
    # compiled (the repository is only read)
    gdir = tmp_path / "gcov"
    gdir.mkdir()
    report = tmp_path / "report.txt"
    r = subprocess.run([sys.executable, str(SCRIPT), str(gdir), str(report)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("C: 0/0 lines (100%) in 0 files compiled by the tests, ")
    assert "\nC files no test compiles (source lines):\n  pkgbuilds/" in report.read_text()


def test_a_program_named_like_its_only_source_is_found(cc, tmp_path):
    # gcc names the counters <output>.gcda, not <output>-<source>.gcda, when the program has the
    # name of its single source file (gcc -o tool tool.c)
    mod, root = cc
    src = put(root / "pkgbuilds/demo/tool.c", "int main(void)\n{\n\treturn 0;\n}\n")
    git(root, "init", "-q")
    git(root, "add", ".")
    work = tmp_path / "w"
    work.mkdir()
    shutil.copy(src, work / "tool.c")
    # The copy maps back to the versioned file, as sandboxed copies do
    (work / "tool.c").write_text(f'#line 1 "{src}"\n' + src.read_text())
    subprocess.run(["gcc", "-O0", "--coverage", "-o", "tool", "tool.c"], cwd=work, check=True,
                   capture_output=True)
    subprocess.run([str(work / "tool")], check=True)
    assert mod.gcda_files(work / "tool") == [work / "tool.gcda"]
    gdir = tmp_path / "gcov"
    gdir.mkdir()
    (gdir / "builds.txt").write_text(f"{work / 'tool'}\n")
    report = tmp_path / "report.txt"
    mod.main(["c-coverage.py", str(gdir), str(report)])
    text = report.read_text()
    assert "C files no test compiles" not in text
    row = next(l.split() for l in text.splitlines() if l.startswith("pkgbuilds/demo/tool.c"))
    assert row[2] == "0" and row[3] == "100%"
