# phosh-plinius: the phoc.ini a session starts with. Phosh saves mode and scale from Settings with
# a copy of the whole system phoc.ini (0009); phoc-ini-merge takes the current system file and
# only mode and scale of each output from that copy, so a device update of the system file (the
# light sensor's box for phoc-plinius 0034) reaches the user too. phosh-session's part (0104) runs
# here as it is in the patch, with the merge script from the package folder.

import os
import subprocess

import pytest

from conftest import PKG

PHOSH = PKG / "phosh-plinius"
MERGE = PHOSH / "phoc-ini-merge"
PATCH = next(PHOSH.glob("0104-phosh-session-*.patch"))

SYSTEM = """\
# phoc on the Plinius
[output:DSI-1]
scale = 2.265625
# The light sensor
light-sensor = 608,0,134,150
light-sensor-gain = 0.88
"""

# What phosh saved on 19.11.: the system file of that day plus its mode
USER = """\
# phoc on the Plinius
[output:DSI-1]
scale=2.265625
mode=1080x2400@60.000Hz
"""


def merge(tmp_path, system, user):
    (tmp_path / "system.ini").write_text(system)
    (tmp_path / "user.ini").write_text(user)
    r = subprocess.run([str(MERGE), str(tmp_path / "system.ini"), str(tmp_path / "user.ini")],
                       capture_output=True, text=True, check=True)
    return r.stdout


def test_device_update_reaches_saved_modes(tmp_path):
    # The phone's case: the new keys from the system file, mode and scale from the user's copy
    assert merge(tmp_path, SYSTEM, USER) == """\
# phoc on the Plinius
[output:DSI-1]
scale=2.265625
# The light sensor
light-sensor = 608,0,134,150
light-sensor-gain = 0.88
mode=1080x2400@60.000Hz
"""


def test_user_values_replace_system_ones_in_place(tmp_path):
    out = merge(tmp_path, "[output:DSI-1]\nscale = 2\nmode = 1x1\nx = 0\n",
                "[output:DSI-1]\nmode=1080x2400@120.000Hz\nscale=3\n")
    assert out == "[output:DSI-1]\nscale=3\nmode=1080x2400@120.000Hz\nx = 0\n"


def test_only_mode_and_scale_of_outputs_come_from_the_copy(tmp_path):
    # Everything else in the copy is an old system file: left out
    out = merge(tmp_path, "[core]\nxwayland = true\n[output:DSI-1]\nscale = 2\n",
                "[core]\nxwayland=false\nscale=9\n"
                "[output:DSI-1]\nlight-sensor=1,1,1,1\nphys_width=1\nscale=3\n")
    assert out == "[core]\nxwayland = true\n[output:DSI-1]\nscale=3\n"


def test_output_only_in_the_copy_gets_its_section(tmp_path):
    # A second screen picked once, or a system file without the panel
    out = merge(tmp_path, "[output:DSI-1]\nscale = 2\n",
                "[ output:HDMI-A-1 ]\nmode=1920x1080@60.000Hz\n[output:DSI-1]\nscale=3\n")
    assert out == "[output:DSI-1]\nscale=3\n\n[output:HDMI-A-1]\nmode=1920x1080@60.000Hz\n"


def test_sections_match_with_spaces_and_keys_once(tmp_path):
    # Spaces in the header and around "=" do not matter; a key twice in either file comes out once
    out = merge(tmp_path, "[ output:DSI-1 ]\nscale = 2\nscale = 2.5\n",
                "[output:DSI-1]\n scale = 3 \nscale=4\n")
    assert out == "[ output:DSI-1 ]\nscale=4\n"


def test_without_mode_or_scale_the_system_file_as_it_is(tmp_path):
    assert merge(tmp_path, SYSTEM, "# nothing saved\n") == SYSTEM
    assert merge(tmp_path, SYSTEM, "") == SYSTEM


def test_usage_and_unreadable_files(tmp_path):
    r = subprocess.run([str(MERGE)], capture_output=True, text=True)
    assert r.returncode == 2 and "usage" in r.stderr
    (tmp_path / "user.ini").write_text(USER)
    r = subprocess.run([str(MERGE), str(tmp_path / "none.ini"), str(tmp_path / "user.ini")],
                       capture_output=True, text=True)
    assert r.returncode == 1 and "none.ini" in r.stderr
    r = subprocess.run([str(MERGE), str(tmp_path / "user.ini"), str(tmp_path / "none.ini")],
                       capture_output=True, text=True)
    assert r.returncode == 1 and "none.ini" in r.stderr


def session_block():
    """phosh-session's lines from 0104, with the merge script from the package folder"""
    lines = [line[1:] for line in PATCH.read_text().splitlines()
             if line.startswith("+") and not line.startswith("+++")]
    # The block's closing "fi" is a context line of the patch
    block = "\n".join(lines) + "\nfi"
    assert "@libexecdir@/plinius/phoc-ini-merge" in block
    return block.replace("@libexecdir@/plinius/phoc-ini-merge", str(MERGE))


def run_session(tmp_path, runtime, merge_ok=True):
    home = tmp_path / "home"
    (home / ".config/phosh").mkdir(parents=True, exist_ok=True)
    script = f'PHOC_INI="{tmp_path}/system.ini"\n{session_block()}\necho "$PHOC_INI"\n'
    if not merge_ok:
        script = script.replace(str(MERGE), "false")
    env = {"PATH": os.environ["PATH"], "HOME": str(home)}
    if runtime is not None:
        env["XDG_RUNTIME_DIR"] = str(runtime)
    r = subprocess.run(["sh", "-c", script], capture_output=True, text=True, env=env, check=True)
    return r.stdout.strip(), home / ".config/phosh/phoc.ini"


def test_session_without_saved_modes_uses_the_system_file(tmp_path):
    (tmp_path / "system.ini").write_text(SYSTEM)
    run = tmp_path / "run"
    run.mkdir()
    ini, _ = run_session(tmp_path, run)
    assert ini == f"{tmp_path}/system.ini"
    assert not (run / "phoc.ini").exists()


def test_session_merges_into_the_runtime_directory(tmp_path):
    (tmp_path / "system.ini").write_text(SYSTEM)
    run = tmp_path / "run"
    run.mkdir()
    (tmp_path / "home/.config/phosh").mkdir(parents=True)
    (tmp_path / "home/.config/phosh/phoc.ini").write_text(USER)
    ini, _ = run_session(tmp_path, run)
    assert ini == f"{run}/phoc.ini"
    assert "light-sensor = 608,0,134,150" in (run / "phoc.ini").read_text()
    assert "mode=1080x2400@60.000Hz" in (run / "phoc.ini").read_text()
    assert not (run / "phoc.ini.new").exists()


@pytest.mark.parametrize("case", ["no-runtime-dir", "runtime-dir-missing", "merge-fails"])
def test_session_falls_back_to_the_copy(tmp_path, case):
    (tmp_path / "system.ini").write_text(SYSTEM)
    (tmp_path / "home/.config/phosh").mkdir(parents=True)
    (tmp_path / "home/.config/phosh/phoc.ini").write_text(USER)
    run = tmp_path / "run"
    if case != "runtime-dir-missing":
        run.mkdir()
    ini, user = run_session(tmp_path, None if case == "no-runtime-dir" else run,
                            merge_ok=case != "merge-fails")
    assert ini == str(user)
    if run.exists():
        assert not (run / "phoc.ini.new").exists()
        assert not (run / "phoc.ini").exists()
