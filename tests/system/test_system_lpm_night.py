# tools/lpm-night.sh with its logger (runs on the phone) and its report: the night measurement of
# why the SoC never reaches its deep sleep. The logger runs here with stand-ins for dmesg,
# rfkill and bluetoothctl and a sysfs tree in tmp_path; lpm-night.sh talks to a fake ssh.

import datetime
import subprocess
import time

import pytest

from conftest import TOOLS, load_script

LOGGER = TOOLS / "lpm-night-logger.sh"
CONTROL = TOOLS / "lpm-night.sh"
report = load_script(TOOLS / "lpm-night-report.py", "lpm_night_report")

DMESG_LINES = """\
2026-11-25T01:00:01,000001+01:00 [name:spm&][SPM] System LPM is blocked by conn
2026-11-25T01:00:01,000002+01:00 [SPM] 26M_off_pct = 0, vcore_off_pct = 0
2026-11-25T01:00:02,000000+01:00 PM: suspend entry (s2idle)
2026-11-25T01:00:03,000000+01:00 [SPM] system_vcore wake up by R12_SYSTIMER_EVENT_B
2026-11-25T01:00:04,000000+01:00 usb 1-1: new device
"""


@pytest.fixture
def phone(tmp_path, fake_bin):
    sys_root = tmp_path / "sys"
    for rel, value in {"class/power_supply/battery/capacity": "79",
                       "class/power_supply/battery/current_now": "-104000",
                       "class/power_supply/battery/status": "Not charging",
                       "class/backlight/plinius-backlight/brightness": "0",
                       "power/suspend_stats/success": "1281",
                       "power/suspend_stats/fail": "97"}.items():
        (sys_root / rel).parent.mkdir(parents=True, exist_ok=True)
        (sys_root / rel).write_text(value + "\n")
    lines = tmp_path / "dmesg.txt"
    lines.write_text(DMESG_LINES)
    fake_bin.stub("dmesg", f'cat "{lines}"; case " $* " in *" -W "*) exec sleep 30;; esac')
    # As on the phone: German unless the logger asks for the C locale
    fake_bin.stub("rfkill", '[ "$LC_ALL" = C ] && printf "wlan unblocked\\nbluetooth blocked\\n" '
                  '|| printf "wlan entsperrt\\nbluetooth gesperrt\\n"')
    fake_bin.stub("bluetoothctl",
                  "printf 'Device 00:11:22:33:44:55 Headphones\\nDevice 00:11:22:33:44:66 Car\\n'")
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    return {"sys": sys_root, "dir": log_dir, "bin": fake_bin}


def run_logger(phone, until, **extra):
    (phone["dir"] / "until").write_text(f"{until}\n")
    env = phone["bin"].env(isolated=True, LPMLOG_DIR=phone["dir"], LPMLOG_SYS=phone["sys"],
                           **extra)
    return subprocess.run(["bash", str(LOGGER)], env=env, capture_output=True, text=True,
                          timeout=20)


def logs(phone):
    return [p.read_text() for p in sorted(phone["dir"].glob("*.txt"))]


def test_logger_once_writes_status_and_only_the_spm_lines(phone):
    r = run_logger(phone, int(time.time()) + 3600, LPMLOG_ONCE=1)
    assert r.returncode == 0, r.stderr
    [text] = logs(phone)
    status = [l for l in text.splitlines() if l.startswith("S ")]
    assert len(status) == 1
    fields = status[0].split()[2:]
    assert fields == ["cap=79", "cur=-104000", "st=Not_charging", "bl=0", "wlan=unblocked",
                      "bt=blocked", "btconn=2", "susp=1281/97"]
    kernel = [l for l in text.splitlines() if l.startswith("K ")]
    assert len(kernel) == 3
    assert "blocked by conn" in kernel[0] and "26M_off_pct" in kernel[1]
    assert "suspend entry" in kernel[2]
    # The count of connected devices only, never an address or a name
    assert "00:11:22" not in text and "Headphones" not in text


def test_logger_does_nothing_once_its_time_is_up(phone):
    r = run_logger(phone, int(time.time()) - 1)
    assert r.returncode == 0
    assert "nothing to do" in r.stdout
    assert logs(phone) == []


def test_logger_without_an_until_file_does_nothing(phone):
    env = phone["bin"].env(isolated=True, LPMLOG_DIR=phone["dir"], LPMLOG_SYS=phone["sys"])
    r = subprocess.run(["bash", str(LOGGER)], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and logs(phone) == []


def test_logger_follows_the_kernel_and_ends_by_itself(phone):
    start = time.time()
    r = run_logger(phone, int(start) + 3, LPMLOG_INTERVAL=1)
    assert r.returncode == 0, r.stderr
    assert "done" in r.stdout
    assert time.time() - start < 15
    [text] = logs(phone)
    assert text.count("S ") >= 2
    assert sum(l.startswith("K ") for l in text.splitlines()) == 3
    assert ["-W", "--time-format", "iso"] in phone["bin"].calls("dmesg")


def test_logger_with_unreadable_values(phone):
    for f in phone["sys"].rglob("*"):
        if f.is_file():
            f.unlink()
    phone["bin"].stub("rfkill", "exit 1")
    phone["bin"].stub("bluetoothctl", "exit 1")
    r = run_logger(phone, int(time.time()) + 3600, LPMLOG_ONCE=1)
    assert r.returncode == 0, r.stderr
    [text] = logs(phone)
    assert "cap=? cur=? st=? bl=? wlan= bt= btconn=0 susp=?/?" in text


# --- the report ---

def epoch(hhmm, day=25):
    return int(datetime.datetime(2026, 11, day, *map(int, hhmm.split(":"))).timestamp())


def iso(hhmm, day=25):
    """As dmesg --time-format iso writes it: 2026-11-26T01:00:00,000000+01:00"""
    t = datetime.datetime(2026, 11, day, *map(int, hhmm.split(":"))).astimezone()
    return t.isoformat(timespec="microseconds").replace(".", ",")


def status_line(hhmm, cap, cur=-100000, bl=0, wlan="unblocked", bt="unblocked", conn=0,
                st="Not_charging"):
    return (f"S {epoch(hhmm)} cap={cap} cur={cur} st={st} bl={bl} wlan={wlan} bt={bt} "
            f"btconn={conn} susp=1/0\n")


NIGHT = (
    [status_line("23:00", 80, bl=120, st="Charging")]
    + [status_line("23:30", 80, cur=-100000), status_line("01:30", 76, cur=-110000),
       status_line("03:30", 72, cur=-90000)]
    + [status_line("04:00", 72, bt="blocked", cur=-60000),
       status_line("06:00", 70, bt="blocked", cur=-50000)]
    + [f"K {iso('00:00', 26)} [SPM] System LPM is blocked by conn\n",
       f"K {iso('00:00', 26)} [SPM] 26M_off_pct = 0, vcore_off_pct = 0\n",
       f"K {iso('00:01', 26)} PM: suspend entry (s2idle)\n",
       f"K {iso('01:00', 26)} [SPM] System LPM is blocked by md\n",
       f"K {iso('05:00', 26)} [SPM] 26M_off_pct = 63, vcore_off_pct = 40\n",
       f"K {iso('05:01', 26)} PM: suspend entry (s2idle)\n",
       "K not-a-time something\n", "garbage line\n"]
)


def night_lines():
    # status lines after midnight belong to the 26th
    out = []
    for line in NIGHT:
        if line.startswith("S "):
            t = int(line.split()[1])
            if datetime.datetime.fromtimestamp(t).hour < 12:
                t += 86400
            line = "S " + str(t) + " " + line.split(" ", 2)[2]
        out.append(line)
    return out


def test_report_splits_the_night_by_state():
    status, kernel = report.parse(night_lines())
    rows = report.summary(status, kernel)
    assert [r["state"] for r in rows] == [
        "display on, wlan unblocked, bt unblocked, charging",
        "display off, wlan unblocked, bt unblocked, battery",
        "display off, wlan unblocked, bt blocked, battery",
    ]
    night, no_bt = rows[1], rows[2]
    assert night["hours"] == pytest.approx(4.0)
    assert night["rate"] == pytest.approx(2.0)
    assert night["current_ma"] == pytest.approx(100)
    assert night["blocked"] == {"conn": 1, "md": 1}
    assert night["off"][("26M_off_pct", False)] == 1
    assert night["suspends"] == 1
    assert no_bt["rate"] == pytest.approx(1.0)
    assert no_bt["off"][("26M_off_pct", True)] == 1 and no_bt["blocked"] == {}
    text = report.render(rows)
    assert "low-power state blocked by: conn 1, md 1" in text
    assert "26M_off_pct: > 0 in 1 of 1 reports" in text
    assert "battery 2.0 %/h" in text and "median current 100 mA" in text


def test_report_connected_bluetooth_and_missing_values():
    lines = [status_line("01:00", 50, conn=1), f"S {epoch('02:00')} cap=? cur=? st=? bl=? "
             "wlan= bt= btconn=? susp=?/?\n"]
    status, kernel = report.parse(lines)
    rows = report.summary(status, kernel)
    assert "bt connected" in rows[0]["state"]
    assert rows[1]["rate"] is None and rows[1]["current_ma"] is None
    assert "battery" not in report.render(rows[1:]).split("\n", 1)[1]


def test_report_main(tmp_path, capsys):
    f = tmp_path / "night.txt"
    f.write_text("".join(night_lines()))
    assert report.main(["report", str(f)]) == 0
    assert "blocked by: conn 1, md 1" in capsys.readouterr().out
    empty = tmp_path / "empty.txt"
    empty.write_text("K x y\n")
    assert report.main(["report", str(empty)]) == 1
    assert report.main(["report"]) == 2


# --- lpm-night.sh against a fake ssh ---

FAKE_SSH = r"""
cmd=${@: -1}
case $cmd in
	*"ls *.txt"*) printf '20261125-220000.txt\n' ;;
	"cat "*/20261125-220000.txt) cat "$NIGHT_FILE" ;;
	"sha256sum < "*) sha256sum < "$NIGHT_FILE" ;;
	"systemctl is-active -q plinius-lpmlog.service") exit "${ACTIVE_RC:-3}" ;;
	*"systemctl restart"*) cat > /dev/null; echo active ;;
	*) cat > /dev/null 2>&1 || true ;;
esac
"""


def control(fake_bin, tmp_path, *args, **extra):
    env = fake_bin.env(LPM_LOCAL_DIR=tmp_path / "fetched", **extra)
    return subprocess.run(["bash", str(CONTROL), *args], env=env, capture_output=True,
                          text=True, stdin=subprocess.DEVNULL, timeout=30)


def ssh_commands(fake_bin):
    return [c[-1] for c in fake_bin.calls("ssh")]


def test_control_fetch_copies_checks_removes_and_reports(tmp_path, fake_bin):
    night = tmp_path / "night.txt"
    night.write_text("".join(night_lines()))
    fake_bin.stub("ssh", FAKE_SSH)
    r = control(fake_bin, tmp_path, "fetch", NIGHT_FILE=night)
    assert r.returncode == 0, r.stderr
    copied = tmp_path / "fetched" / "20261125-220000.txt"
    assert copied.read_text() == night.read_text()
    assert "copied and removed from the phone" in r.stderr
    assert "low-power state blocked by: conn 1, md 1" in r.stdout
    cmds = ssh_commands(fake_bin)
    assert "rm -f /var/tmp/plinius-lpmlog/20261125-220000.txt" in cmds
    assert any("rmdir /var/tmp/plinius-lpmlog" in c for c in cmds)


def test_control_fetch_leaves_a_log_still_written(tmp_path, fake_bin):
    night = tmp_path / "night.txt"
    night.write_text("".join(night_lines()))
    fake_bin.stub("ssh", FAKE_SSH)
    r = control(fake_bin, tmp_path, "fetch", NIGHT_FILE=night, ACTIVE_RC=0)
    assert r.returncode == 0, r.stderr
    assert "it stays on the phone" in r.stderr
    assert not any(c.startswith("rm -f") for c in ssh_commands(fake_bin))


def test_control_fetch_keeps_a_copy_that_differs(tmp_path, fake_bin):
    night = tmp_path / "night.txt"
    night.write_text("".join(night_lines()))
    fake_bin.stub("ssh", FAKE_SSH.replace('sha256sum < "$NIGHT_FILE"', "echo 0000 -"))
    r = control(fake_bin, tmp_path, "fetch", NIGHT_FILE=night)
    assert r.returncode == 1
    assert "copy differs, left on the phone" in r.stderr
    assert not any(c.startswith("rm -f") for c in ssh_commands(fake_bin))


def test_control_fetch_without_a_log(tmp_path, fake_bin):
    fake_bin.stub("ssh", "exit 0")
    r = control(fake_bin, tmp_path, "fetch")
    assert r.returncode == 1 and "no log on the phone" in r.stderr


def test_control_start_installs_a_unit_that_survives_a_restart(tmp_path, fake_bin):
    fake_bin.stub("ssh", FAKE_SSH)
    r = control(fake_bin, tmp_path, "start", "12")
    assert r.returncode == 0, r.stderr
    install, unit = ssh_commands(fake_bin)
    assert "cat > /var/tmp/plinius-lpmlog/lpmlog.sh" in install
    assert "+ 12 * 3600" in unit
    assert "WantedBy=multi-user.target" in unit and "systemctl enable -q" in unit
    assert "systemctl restart plinius-lpmlog.service" in unit  # a changed logger takes over
    assert "ExecStart=/var/tmp/plinius-lpmlog/lpmlog.sh" in unit


@pytest.mark.parametrize("hours", ["0", "97", "x"])
def test_control_start_refuses_odd_hours(tmp_path, fake_bin, hours):
    fake_bin.stub("ssh")
    r = control(fake_bin, tmp_path, "start", hours)
    assert r.returncode == 1 and "HOURS must be 1 to 96" in r.stderr
    assert fake_bin.calls("ssh") == []


def test_control_stop_and_status_and_usage(tmp_path, fake_bin):
    fake_bin.stub("ssh")
    assert control(fake_bin, tmp_path, "stop").returncode == 0
    [stop] = ssh_commands(fake_bin)
    assert "systemctl disable --now plinius-lpmlog.service" in stop
    assert "rm -f /etc/systemd/system/plinius-lpmlog.service" in stop
    assert control(fake_bin, tmp_path, "status").returncode == 0
    r = control(fake_bin, tmp_path)
    assert r.returncode == 2 and "Usage" in r.stderr
