# The helpers of conftest.py themselves.

import subprocess

from conftest import bash_functions, load_script


def test_fake_bin_logs_calls(fake_bin):
    fake_bin.stub("cryptsetup", '[ "$1" = open ] && echo "opened $2"; exit 0')
    env = fake_bin.env(isolated=True)
    r = subprocess.run(["bash", "-c", 'cryptsetup open "/dev/a b" x; cryptsetup close x'],
                       env=env, capture_output=True, text=True)
    assert r.stdout == "opened /dev/a b\n"
    assert fake_bin.calls("cryptsetup") == [["open", "/dev/a b", "x"], ["close", "x"]]
    assert fake_bin.calls("never") == []


def test_isolated_path_has_no_system_programs(fake_bin):
    r = subprocess.run(["bash", "-c", "command -v pacman || echo none"],
                       env=fake_bin.env(isolated=True), capture_output=True, text=True)
    assert r.stdout.strip() == "none"


def test_bash_functions(tmp_path):
    script = tmp_path / "s"
    script.write_text('#!/bin/bash\nrm -rf /nothing\none() { echo 1; }\ntwo() {\n'
                      '\tif true; then\n\t\techo 2\n\tfi\n}\nexit 1\n')
    code = bash_functions(script, "one", "two") + "one; two"
    r = subprocess.run(["bash", "-c", code], capture_output=True, text=True)
    assert r.stdout == "1\n2\n"


def test_load_script_without_suffix(tmp_path):
    script = tmp_path / "tool-x"
    script.write_text("#!/usr/bin/env python3\nVALUE = 3\nif __name__ == '__main__':\n"
                      "    raise SystemExit(1)\n")
    assert load_script(script).VALUE == 3


def test_fake_bin_call_without_arguments(fake_bin):
    fake_bin.stub("sync")
    subprocess.run(["bash", "-c", "sync; sync ''"], env=fake_bin.env(isolated=True), check=True)
    assert fake_bin.calls("sync") == [[], [""]]
