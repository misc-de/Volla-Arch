# When the phones learn of new vulnerabilities: the project server builds the kernel lists four
# times a day (tools/kernel-cves/plinius-kernel-cves.timer), each phone fetches them an hour later
# (plinius-security's plinius-cve-update.timer), and the NetworkManager dispatcher
# (90-plinius-cve) catches up after a connection when a run was missed.
#
# The dispatcher runs as it is, with its state directory moved into tmp_path and systemctl,
# uname replaced by stand-ins that only record what they were asked.

import configparser
import os
import re
import subprocess
import time

import pytest

from conftest import PKG, REPO

PHONE_TIMER = PKG / "plinius-security" / "plinius-cve-update.timer"
SERVER_TIMER = REPO / "tools" / "kernel-cves" / "plinius-kernel-cves.timer"
DISPATCHER = PKG / "plinius-security" / "90-plinius-cve"
# How long the server needs for one run after its timer fired (measured 24.11.2026: under a minute)
SERVER_RUN = 15 * 60


def timer(path):
    """(start times in seconds after midnight UTC, RandomizedDelaySec in seconds) of a timer unit.

    Both timers are in UTC: in local time a server and a phone in different zones, or one of them
    changing to daylight saving time, would move the phone's run before the server's."""
    c = configparser.ConfigParser(strict=False)
    c.read_string(path.read_text())
    cal = c["Timer"]["OnCalendar"]
    m = re.fullmatch(r"\*-\*-\* ([\d,]+):(\d\d) UTC", cal)
    assert m, f"{path.name}: OnCalendar {cal!r} is not of the form *-*-* H,H:MM UTC"
    starts = sorted(int(h) * 3600 + int(m.group(2)) * 60 for h in m.group(1).split(","))
    delay = c["Timer"].get("RandomizedDelaySec", "0")
    d = re.fullmatch(r"(\d+)(min|h|s)?", delay)
    return starts, int(d.group(1)) * {"min": 60, "h": 3600, "s": 1, None: 1}[d.group(2)]


def test_server_and_phone_run_four_times_a_day():
    assert len(timer(SERVER_TIMER)[0]) == 4
    assert len(timer(PHONE_TIMER)[0]) == 4


def test_each_phone_run_comes_after_a_finished_server_run_and_soon():
    server, server_delay = timer(SERVER_TIMER)
    phone, _ = timer(PHONE_TIMER)
    for p in phone:
        # The latest server run that has certainly finished by the earliest start of this phone run
        done = [s for s in server if s + server_delay + SERVER_RUN <= p]
        assert done, f"phone run at {p // 3600:02}:{p % 3600 // 60:02} comes before any server run"
        # and no new server run starts in between, which the phone would then miss for hours
        assert p - max(done) <= 2 * 3600, "phone run more than two hours after its server run"


def test_phone_timer_catches_up_after_being_off():
    c = configparser.ConfigParser(strict=False)
    c.read_string(PHONE_TIMER.read_text())
    assert c["Timer"]["Persistent"] == "true"


@pytest.fixture
def dispatcher(tmp_path):
    """Runs the dispatcher: state in tmp_path/state, systemctl calls recorded in tmp_path/calls."""
    state = tmp_path / "state"
    state.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    (bin_dir / "systemctl").write_text(f'#!/bin/sh\necho "$*" >> {calls}\n')
    (bin_dir / "uname").write_text('#!/bin/sh\necho "${FAKE_RELEASE:-6.1.192-plinius23}"\n')
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    script = tmp_path / "90-plinius-cve"
    script.write_text(DISPATCHER.read_text().replace("/var/lib/plinius-cve", str(state)))

    def run(action="connectivity-change", connectivity="FULL", release=None):
        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "CONNECTIVITY_STATE": connectivity}
        if release:
            env["FAKE_RELEASE"] = release
        calls.unlink(missing_ok=True)
        subprocess.run(["sh", str(script), "wlan0", action], env=env, check=True)
        return calls.read_text().splitlines() if calls.exists() else []

    run.state = state
    return run


def fresh_kernel_list(state, release="6.1.192-plinius23"):
    (state / "kernel-cves.json").write_text(f'{{"release":"{release}","issues":[]}}')


def downloaded(state, hours_ago):
    f = state / "arch-issues.json"
    f.write_text("{}")
    t = time.time() - hours_ago * 3600
    os.utime(f, (t, t))


START = ["start --no-block plinius-cve-update.service"]


def test_dispatcher_leaves_a_download_of_the_last_six_hours_alone(dispatcher):
    fresh_kernel_list(dispatcher.state)
    downloaded(dispatcher.state, 5)
    assert dispatcher() == []


def test_dispatcher_catches_up_a_run_missed_while_offline(dispatcher):
    # Older than the six hours between two timer runs: the phone missed one
    fresh_kernel_list(dispatcher.state)
    downloaded(dispatcher.state, 7)
    assert dispatcher() == START


def test_dispatcher_loads_at_once_without_any_download(dispatcher):
    fresh_kernel_list(dispatcher.state)
    assert dispatcher() == START


def test_dispatcher_loads_the_list_of_a_new_kernel_at_once(dispatcher):
    fresh_kernel_list(dispatcher.state, "6.1.192-plinius22")
    downloaded(dispatcher.state, 0)
    assert dispatcher(release="6.1.192-plinius23") == START


def test_dispatcher_ignores_other_events(dispatcher):
    assert dispatcher(action="down", connectivity="NONE") == []
    assert dispatcher(action="connectivity-change", connectivity="LIMITED") == []
