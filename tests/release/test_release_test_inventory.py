# tools/test-inventory.py: which packages have own code, patches, a check() and tests under tests/.
# A small repository of its own in tmp_path stands in for this one.

import re
import subprocess
import sys

import pytest

from conftest import TOOLS, load_script

SCRIPT = TOOLS / "test-inventory.py"


def put(root, path, text="", mode=None):
    p = root / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    if mode:
        p.chmod(mode)
    return p


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    crypt = "pkgbuilds/plinius-crypt/"
    put(root, crypt + "PKGBUILD", "pkgname=plinius-crypt\ncheck() {\n  true\n}\n")
    put(root, crypt + "crypt.c", "int a;\nint b;\nint c;\n")             # C 3
    put(root, crypt + "crypt.h", "int a;\n")                              # C 1
    put(root, crypt + "unlock", "#!/bin/bash\necho\n")                    # Shell 2 (no extension)
    put(root, crypt + "helper", "#!/usr/bin/env python3\nprint()\n")      # Python 2
    put(root, crypt + "data", "plain text\n")                             # no shebang: not code
    put(root, crypt + "tool.py", "x = 1\n")                               # Python 1
    put(root, crypt + "hook.hook", "[Trigger]\nType = Path\n")             # pacman hook: not code
    put(root, crypt + "mkinitcpio.hook", "run_hook() {\n  :\n}\n")        # initcpio hook: Shell 3
    put(root, crypt + "test-crypt.c", "int t;\n")                         # own test, not counted
    put(root, crypt + "README.md", "# docs\n")
    put(root, crypt + "0001-fix.patch", "")
    put(root, crypt + "0002-fix.patch", "")
    put(root, crypt + "deleted", "#!/bin/sh\n")
    put(root, "pkgbuilds/phosh-plinius/PKGBUILD", "pkgname=phosh-plinius\n")
    put(root, "pkgbuilds/phosh-plinius/0001-a.patch", "")
    put(root, "pkgbuilds/upstream-thing/PKGBUILD", "pkgname=upstream-thing\n")
    put(root, "pkgbuilds/README", "top level, no package\n")
    put(root, "tools/release.sh", "#!/bin/sh\necho 1\necho 2\n")          # tools: Shell 3
    put(root, "docs/x.py", "print()\n")                                   # neither pkgbuilds nor tools
    put(root, "tests/release/test_crypt.py",
        'from conftest import PKG\nSRC = PKG / "plinius-crypt"\n'
        "def test_one():\n    pass\n\nasync def test_two():\n    pass\n")
    put(root, "tests/release/test_tools.py",
        'P = "tools/release.sh"\n    def test_indented():\n        pass\n')
    put(root, "tests/release/test_other.py", "def test_unrelated():\n    pass\n")
    put(root, "tests/release/helper.py", "# names pkgbuilds/plinius-crypt but is no test file\n"
                                          "def test_not_counted():\n    pass\n")
    put(root, "tests/lint/test_lint.py", "# pkgbuilds/phosh-plinius\ndef test_lint():\n    pass\n")
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    (root / crypt / "deleted").unlink()  # versioned, gone from the working tree
    mod = load_script(SCRIPT)
    monkeypatch.setattr(mod, "ROOT", str(root))
    return mod


def row(out, name):
    """The text report's line of a package, split; "tests=   2" becomes "tests=2"."""
    line = next(l for l in out.splitlines() if l.strip().startswith(name + " "))
    return re.sub(r"= +", "=", line).split()


def run(mod, monkeypatch, capsys, *args):
    monkeypatch.setattr(mod.sys, "argv", ["test-inventory.py", *args])
    mod.main()
    return capsys.readouterr().out


def test_text_report(repo, monkeypatch, capsys):
    out = run(repo, monkeypatch, capsys)
    assert row(out, "plinius-crypt") == ["plinius-crypt", "12", "lines", "2", "patches", "check=yes",
                             "tests=2", "Shell", "5,", "C", "4,", "Python", "3"]
    # The lint test names it, but lint tests do not count
    assert row(out, "phosh-plinius")[:7] == ["phosh-plinius", "0", "lines", "1", "patches", "check=no",
                                 "tests=0"]
    assert row(out, "tools")[:7] == ["tools", "3", "lines", "0", "patches", "check=no", "tests=1"]
    # Categories in their order, only those with packages; "other" collects the rest
    assert re.findall(r"^== .*\[(\w+)\]$", out, re.M) == ["boot", "release", "ui", "other"]
    assert "upstream-thing" in out.split("[other]")[1]
    assert "  -> 100 % of 12 lines in packages with tests" in out
    assert "  -> 0 % of 0 lines in packages with tests" in out  # ui: phosh, patches only
    # 3 packages with code or patches, 2 of them tested; 5 test functions in test_*.py files (lint ones too)
    assert out.strip().splitlines()[-1] == (
        "Total: 2/3 packages with own code or patches have tests; 100 % of 15 own lines are in "
        "packages with tests; 5 test functions under tests/")


def test_markdown_report(repo, monkeypatch, capsys):
    out = run(repo, monkeypatch, capsys, "--markdown")
    assert "\n## Boot, encryption, kernel (phone does not start, data lost) [boot]\n" in out
    assert "| Package | Own code (lines) | Patches | check() | Tests under tests/ |" in out
    assert "| plinius-crypt | 12 (Shell 5, C 4, Python 3) | 2 | yes | 2 |" in out
    assert "| phosh-plinius | 0 (–) | 1 | – | – |" in out
    assert "\n100 % of 12 lines in packages with tests" in out


def test_empty_repository(tmp_path, monkeypatch, capsys):
    mod = load_script(SCRIPT)
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    monkeypatch.setattr(mod, "ROOT", str(tmp_path))
    out = run(mod, monkeypatch, capsys)
    assert out.strip() == ("Total: 0/0 packages with own code or patches have tests; 0 % of 0 own "
                           "lines are in packages with tests; 0 test functions under tests/")


def test_command_line_on_the_repository():
    r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert "== Updates and releases (what reaches the phone) [release]" in r.stdout
    assert re.search(r"^  tools +\d+ lines", r.stdout, re.M)
    assert r.stdout.strip().splitlines()[-1].startswith("Total: ")
