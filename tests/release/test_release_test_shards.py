# tools/test-shards.py: splits the test files into groups of about the same running time for
# tools/run-tests.sh --jobs, and keeps the files' times from pytest's JUnit reports.

import json
import subprocess
import sys

import pytest

from conftest import TOOLS, load_script

SCRIPT = TOOLS / "test-shards.py"


@pytest.fixture
def mod(tmp_path, monkeypatch):
    m = load_script(SCRIPT)
    monkeypatch.setattr(m, "ROOT", tmp_path)
    monkeypatch.setattr(m, "DURATIONS", tmp_path / "build" / "test-durations.json")
    return m


def put(path, size=10):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x" * size)
    return path


def test_files_under_paths_only_test_modules(mod, tmp_path):
    put(tmp_path / "tests/a/test_one.py")
    put(tmp_path / "tests/a/helper.py")
    put(tmp_path / "tests/b/c/test_two.py")
    single = put(tmp_path / "other/test_three.py")
    found = mod.test_files(["tests", str(single), "missing"])
    assert [f.relative_to(tmp_path).as_posix() for f in found] == [
        "tests/a/test_one.py", "tests/b/c/test_two.py", "other/test_three.py"]


def test_weights_measured_time_before_size(mod, tmp_path):
    a = put(tmp_path / "tests/test_a.py", 4000)
    b = put(tmp_path / "tests/test_b.py", 4000)
    w = mod.weights([a, b], {"tests/test_a.py": 30.0})
    assert w == {"tests/test_a.py": 30.0, "tests/test_b.py": 4000 * mod.SECONDS_PER_BYTE}


def test_shards_longest_first_into_the_lightest_group(mod):
    w = {"a": 10, "b": 7, "c": 6, "d": 3, "e": 1}
    groups = mod.shards(2, w)
    sums = sorted(sum(w[n] for n in g) for g in groups)
    # 10+3 | 7+6+1 (LPT); every file exactly once, each group sorted
    assert sums == [13, 14]
    assert sorted(n for g in groups for n in g) == sorted(w)
    assert all(g == sorted(g) for g in groups)


def test_shards_no_empty_groups(mod):
    assert mod.shards(4, {"only": 1.0}) == [["only"]]


@pytest.mark.parametrize("text", ["", "not json", "[1, 2]"])
def test_unreadable_durations_count_as_none(mod, tmp_path, text):
    put(tmp_path / "build/test-durations.json").write_text(text)
    assert mod.load_durations() == {}


def test_durations_without_numbers_are_dropped(mod, tmp_path):
    put(tmp_path / "build/test-durations.json").write_text(json.dumps({"a": 1, "b": "x", "c": 2.5}))
    assert mod.load_durations() == {"a": 1.0, "c": 2.5}


JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest">
<testcase classname="tests.gcc.test_gcc_usb" name="test_a" time="1.25"/>
<testcase classname="tests.gcc.test_gcc_usb" name="test_b" time="0.75"/>
<testcase classname="tests.daemons-hw.test_dhw_x.TestGroup" name="test_c" time="2"/>
<testcase classname="" name="collect" time="5"/>
<testcase classname="tests.conftest" name="setup" time="9"/>
</testsuite></testsuites>
"""


def test_record_sums_times_per_file_and_keeps_others(mod, tmp_path):
    put(tmp_path / "build/test-durations.json").write_text(
        json.dumps({"tests/gcc/test_gcc_usb.py": 99.0, "tests/old/test_kept.py": 4.0}))
    report = put(tmp_path / "r.xml")
    report.write_text(JUNIT)
    broken = put(tmp_path / "broken.xml")
    broken.write_text("<not closed")
    mod.record([str(report), str(broken), str(tmp_path / "missing.xml")])
    assert json.loads((tmp_path / "build/test-durations.json").read_text()) == {
        "tests/daemons-hw/test_dhw_x.py": 2.0, "tests/gcc/test_gcc_usb.py": 2.0,
        "tests/old/test_kept.py": 4.0}


def test_record_without_times_writes_nothing(mod, tmp_path):
    mod.record([str(tmp_path / "missing.xml")])
    assert not (tmp_path / "build/test-durations.json").exists()


def test_main_prints_groups_and_records(mod, tmp_path, capsys):
    put(tmp_path / "tests/test_a.py", 6000)
    put(tmp_path / "tests/test_b.py", 4000)
    put(tmp_path / "tests/test_c.py", 2000)
    mod.main(["test-shards.py", "2"])
    assert capsys.readouterr().out.splitlines() == ["tests/test_a.py", "tests/test_b.py tests/test_c.py"]
    report = put(tmp_path / "r.xml")
    report.write_text(JUNIT)
    mod.main(["test-shards.py", "--record", str(report)])
    assert "tests/gcc/test_gcc_usb.py" in json.loads((tmp_path / "build/test-durations.json").read_text())
    with pytest.raises(SystemExit, match="at least 1"):
        mod.main(["test-shards.py", "0"])


def test_runs_as_a_program():
    r = subprocess.run([sys.executable, str(SCRIPT), "1", "tests/release/test_release_test_shards.py"],
                       cwd=TOOLS.parent, capture_output=True, text=True, check=True)
    assert r.stdout == "tests/release/test_release_test_shards.py\n"
