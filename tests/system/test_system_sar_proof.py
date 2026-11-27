# The SAR proof: tools/sar-current.sh (on the phone: switches scenes during an upload, asks the
# modem for its transmit time per power level, logs the battery current) and tools/sar-analyse.py
# (on the PC: per phase the levels and the current, a verdict per scene). The script runs as a
# copy with its sysfs and log paths in tmp_path; binder-call, lxc-attach, mmcli, curl and
# plinius-sar are stubs.

import shutil
import struct

import pytest

from conftest import TOOLS, load_script
from system_helpers import rewritten_copy, run

CURRENT = TOOLS / "sar-current.sh"
ANALYSE = TOOLS / "sar-analyse.py"

MMCLI = r'''
case "$*" in
  -L) echo "    /org/freedesktop/ModemManager1/Modem/0 [ofono] binder" ;;
  "-m 0 -K") printf '%s\n' "generic.state                 : connected" \
       "generic.access-technologies.value[1] : lte" "generic.signal-quality.value  : 7" ;;
esac
'''

# What logcat would print: one activity line among others, and an ERFIDX line
LOGCAT = r'''
echo "  1763800000.100   247   376 I AT      : RECV MSGID=04a0 [NW_GET_ACTIVITY_INFO_CNF], TXID=037c, DUMP=84195424000000000100A0047C0340000000040000000000000114000000000000000000000000000000000000000000010104001F00000002010400421F000003010400C30700000401010005000000"
echo "  1763800000.200   247   397 I RILJ    : [0] something with an IMSI"
echo "  1763800000.300   247   407 I AT      : [0] AT> AT+ERFIDX=1,2 (RIL_CMD_READER_1)"
'''


def copy_current(tmp_path):
    sysfs = tmp_path / "sys"
    sysfs.mkdir()
    for name, value in (("carrier", "0"), ("current_now", "-512000"), ("voltage_now", "3900000"),
                        ("tx_bytes", "1000")):
        (sysfs / name).write_text(value + "\n")
    (tmp_path / "random").write_bytes(b"x" * 64)
    return rewritten_copy(CURRENT, tmp_path / "sar-current.sh", {
        "/root/sar-current.log": tmp_path / "run.log",
        "/root/sar-activity.log": tmp_path / "activity.log",
        "/sys/class/net/usb0/carrier": sysfs / "carrier",
        "/sys/class/power_supply/battery/current_now": sysfs / "current_now",
        "/sys/class/power_supply/battery/voltage_now": sysfs / "voltage_now",
        "/sys/class/net/$IFACE/statistics/tx_bytes": sysfs / "tx_bytes",
        "/dev/urandom": tmp_path / "random",
    })


def stubs(fake_bin, tmp_path, binder_call=True):
    real_sleep, real_seq = shutil.which("sleep"), shutil.which("seq")
    # The script's sleeps shortened: the 20 s settle to 1 s (time for a few uploads), the rest
    # (1 s per sample) to 0.05 s
    fake_bin.stub("sleep", f'[ "$1" = 20 ] && exec {real_sleep} 1; exec {real_sleep} 0.05')
    fake_bin.stub("seq", f'exec {real_seq} "$@"')
    fake_bin.stub("timeout", 'shift; exec "$@"')
    fake_bin.stub("mmcli", MMCLI)
    # Every upload sends 30 kB while the file "flow" exists
    tx, flow = tmp_path / "sys" / "tx_bytes", tmp_path / "flow"
    flow.touch()
    fake_bin.stub("curl", f'[ -e {flow} ] && echo $(($(cat {tx}) + 30000)) > {tx}; exec {real_sleep} 0.2')
    fake_bin.stub("lxc-attach", LOGCAT)
    fake_bin.stub("systemctl")
    fake_bin.stub("plinius-sar")
    fake_bin.stub("pkill")
    if binder_call:
        fake_bin.stub("binder-call")


def test_run_switches_scenes_and_asks_the_modem(tmp_path, fake_bin):
    script = copy_current(tmp_path)
    stubs(fake_bin, tmp_path)
    env = fake_bin.env(isolated=True, PHASE_S="1", CYCLES="2", SCENES="2 0", SLOT="slot2")
    r = run(["bash", script], env, timeout=60)
    assert r.returncode == 0, r.stderr
    log = (tmp_path / "run.log").read_text().splitlines()

    # The service stops for the run and comes back, the modem ends at normal power
    assert fake_bin.calls("systemctl") == [["stop", "plinius-sar"], ["start", "plinius-sar"]]
    scenes = [c[1] for c in fake_bin.calls("plinius-sar")]
    assert scenes == ["-1", "2", "-1", "0", "-1", "2", "-1", "0", "-1"]

    # One query before the first phase and one after every phase, each with its own serial
    calls = fake_bin.calls("binder-call")
    assert len(calls) == 9
    assert calls[0] == ["-q", "-a", "-o", "-d", "/dev/binder",
                        "android.hardware.radio.modem.IRadioModem/slot2", "5", "2147418113"]
    assert [c[-1] for c in calls] == [str(2147418113 + i) for i in range(9)]
    marks = [line for line in log if line.startswith(("# activity", "# phase"))]
    assert marks[0].startswith("# activity t=")
    assert marks[1] == "# phase scene=-1 rc=0 tech=lte signal=7"
    assert [m.split()[1] for m in marks] == ["activity"] + ["phase", "activity"] * 8

    # Samples: time scene current voltage tx_bytes
    samples = [line.split() for line in log if line[:1].isdigit()]
    assert samples and all(s[1:4] in (["-1", "-512000", "3900000"], ["2", "-512000", "3900000"],
                                      ["0", "-512000", "3900000"]) for s in samples)
    sent = [int(s[4]) for s in samples]
    assert sent == sorted(sent) and sent[-1] > sent[0]

    # The radio log comes from the container, filtered: the IMSI line stays out
    assert fake_bin.calls("lxc-attach") == [["-n", "android", "--", "/system/bin/logcat", "-b",
                                             "radio", "-v", "epoch", "-T", "1"]]
    kept = (tmp_path / "activity.log").read_text()
    assert "NW_GET_ACTIVITY_INFO_CNF" in kept and "ERFIDX" in kept and "IMSI" not in kept
    assert log[-1].startswith("done ")


def test_run_needs_binder_call(tmp_path, fake_bin):
    script = copy_current(tmp_path)
    stubs(fake_bin, tmp_path, binder_call=False)
    r = run(["bash", script], fake_bin.env(isolated=True))
    assert r.returncode == 1
    assert "pacman -S libgbinder-tools" in (tmp_path / "run.log").read_text()
    assert fake_bin.calls("systemctl") == []


@pytest.mark.parametrize("binder_call, failed", [
    # A oneway call: binder-call has no reply to show and ends with 2, the query went out
    ('echo "NO REPLY"; exit 2', 0),
    ('echo "binder-call: No such service: $6" >&2; exit 1', 3),
])
def test_failed_query_is_marked(tmp_path, fake_bin, binder_call, failed):
    script = copy_current(tmp_path)
    stubs(fake_bin, tmp_path)
    fake_bin.stub("binder-call", binder_call)
    env = fake_bin.env(isolated=True, PHASE_S="0", CYCLES="1", SCENES="2")
    assert run(["bash", script], env, timeout=60).returncode == 0
    log = (tmp_path / "run.log").read_text()
    assert log.count("# activity failed") == failed
    if failed:
        assert "# activity failed: binder-call: No such service: " \
            "android.hardware.radio.modem.IRadioModem/slot1" in log


@pytest.mark.parametrize("route, iface", [
    ("default via 192.0.2.1 dev wlan0 proto dhcp metric 600\n"
     "default via 10.60.0.1 dev ccmni2 proto static metric 1050", "ccmni2"),
    ("default via 192.0.2.1 dev wlan0 proto dhcp metric 600", "ccmni1"),
])
def test_upload_goes_over_the_mobile_interface(tmp_path, fake_bin, route, iface):
    script = copy_current(tmp_path)
    stubs(fake_bin, tmp_path)
    fake_bin.stub("ip", f'[ "$*" = "route show default" ] && printf "{route}\\n"')
    env = fake_bin.env(isolated=True, PHASE_S="1", CYCLES="1", SCENES="2")
    assert run(["bash", script], env, timeout=60).returncode == 0
    uploads = fake_bin.calls("curl")
    assert uploads and all(c[c.index("--interface") + 1] == iface for c in uploads)
    assert all(c[c.index("-A") + 1] == "Plinius Arch Linux" for c in uploads)


# ---- sar-analyse.py ----

T0 = 1763800000.0


def phase_lines(scene, current_ma, upload_kib, rc=0, radio="tech=lte signal=7", samples=5):
    lines = [f"# phase scene={scene} rc={rc} {radio}".rstrip()]
    for i in range(samples):
        lines.append(f"12:00:{i:02d} {scene} {-current_ma * 1000} 3900000 {int(i * upload_kib * 1024)}")
    return lines


def tlv(kind, value):
    return struct.pack("<HH", kind, len(value)) + value


def activity_line(t, tx, rx=100, sleep=0, idle=50, levels=5, result=0, skip=()):
    """The modem's answer as the MIPC RIL logs it (header, then TLVs)"""
    fields = {0x0: struct.pack("<I", result), 0x100: struct.pack(f"<{len(tx)}I", *tx),
              0x101: struct.pack("<I", rx), 0x102: struct.pack("<I", sleep),
              0x103: struct.pack("<I", idle), 0x104: bytes([levels])}
    body = b"".join(tlv(k, v) for k, v in fields.items() if k not in skip)
    header = bytes.fromhex("8419542400000000") + struct.pack("<HHHH", 1, 0x04a0, 0x037c, len(body))
    return (f"  {t:.3f}   247   376 I AT      : RECV MSGID=04a0 [NW_GET_ACTIVITY_INFO_CNF], "
            f"TXID=037c, DUMP={(header + body).hex().upper()}")


def build(phases, cumulative=True, delay=0.5, missing=()):
    """Main log and radio log for phases [(scene, tx per level, current, upload, kw)]"""
    run_log, radio_log = [], []
    total = [0] * 5
    t = T0
    for i in range(len(phases) + 1):
        run_log.append(f"# activity t={t:.3f} serial={2147418113 + i}")
        if i:
            tx = phases[i - 1][1]
            total = [a + b for a, b in zip(total, tx)] if cumulative else list(tx)
        if i not in missing:
            # Idle time grows with every phase when counted since boot
            radio_log.append(activity_line(t + delay, total, idle=1000 * (i if cumulative else 1)))
        if i < len(phases):
            scene, _, current, upload, kw = phases[i]
            run_log += phase_lines(scene, current, upload, **kw)
            t += 60
    return run_log, radio_log


FULL = (0, 0, 0, 10000, 50000)      # 83 % above 20 dBm
LOWER = (0, 0, 0, 55000, 5000)      # 8 % above 20 dBm
GOOD = (30000, 20000, 10000, 0, 0)  # good signal: nothing near the cap


def analyse(phases, **kw):
    sa = load_script(ANALYSE)
    run_log, radio_log = build(phases, **kw)
    return sa.analyse(run_log, radio_log)


def test_scene_that_lowers_the_power():
    out = analyse([(-1, FULL, 600, 100, {}), (2, LOWER, 480, 100, {}),
                   (-1, FULL, 610, 100, {}), (2, LOWER, 500, 100, {})])
    assert "counters: since boot" in out
    assert "radio: tech=lte signal=7" in out
    assert "scene 2: lowers power (2 valid pairs; >20 dBm points out: +75, +75; " \
           "current mA: -120, -110)" in out
    # Phase table: current, upload, transmit seconds and shares
    assert "   -1    600    100   60.0     0.0     0.0     0.0    16.7    83.3  " in out


def test_scene_without_back_off_and_counters_since_the_last_query():
    out = analyse([(-1, FULL, 600, 100, {}), (0, FULL, 610, 100, {}),
                   (-1, FULL, 600, 100, {}), (0, FULL, 590, 100, {})], cumulative=False)
    assert "counters: since the last query" in out
    assert "scene 0: no back-off above 20 dBm (2 valid pairs" in out


def test_good_signal_proves_nothing():
    # Without full power in the -1 phase a back-off cannot show
    out = analyse([(-1, GOOD, 400, 100, {}), (3, GOOD, 400, 100, {}),
                   (-1, GOOD, 400, 100, {}), (3, GOOD, 400, 100, {})])
    assert "scene 3: inconclusive (0 valid pairs; >20 dBm points out: -; current mA: -)" in out


def test_mixed_results_are_inconclusive():
    out = analyse([(-1, FULL, 600, 100, {}), (6, LOWER, 500, 100, {}),
                   (-1, FULL, 600, 100, {}), (6, FULL, 600, 100, {})])
    assert "scene 6: inconclusive (2 valid pairs; >20 dBm points out: +75, +0;" in out


def test_invalid_phases_do_not_count():
    out = analyse([(-1, FULL, 600, 100, {}), (2, LOWER, 480, 100, {"rc": 1}),
                   (-1, FULL, 600, 100, {}), (2, LOWER, 480, 20, {}),
                   (-1, FULL, 600, 100, {"radio": ""}), (2, LOWER, 480, 100, {}),
                   (-1, FULL, 600, 100, {}), (2, (0, 0, 0, 0, 0), 480, 100, {})])
    assert "scene refused (rc=1)" in out
    assert "upload 20 KiB/s" in out
    assert "no data connection" in out
    assert "no transmit time" in out
    assert "scene 2: inconclusive (0 valid pairs" in out


def test_missing_activity_line():
    out = analyse([(-1, FULL, 600, 100, {}), (2, LOWER, 480, 100, {})], missing={1})
    lines = out.splitlines()
    # Without the query between them, neither phase has numbers of its own
    assert sum("no activity numbers" in line for line in lines) == 2


def test_late_activity_line_is_not_taken():
    sa = load_script(ANALYSE)
    run_log, radio_log = build([(-1, FULL, 600, 100, {})], delay=sa.MATCH_S + 1)
    assert "no activity numbers" in sa.analyse(run_log, radio_log)


@pytest.mark.parametrize("kw", [{"levels": 4}, {"result": 1}, {"tx": (1, 2, 3, 4)},
                                {"skip": (0x102,)}, {"skip": (0x0,)}])
def test_unexpected_activity_answer(kw):
    sa = load_script(ANALYSE)
    kw = {"tx": (1, 2, 3, 4, 5), **kw}
    with pytest.raises(ValueError, match="unexpected activity answer"):
        sa.parse_activity([activity_line(T0, **kw)])


def test_answer_from_the_phone():
    # Logged on the Plinius 2026-11-22, 10 s after the previous query, without upload
    sa = load_script(ANALYSE)
    line = ("         1795381689.050   247   376 I AT      : RECV MSGID=04a0 "
            "[NW_GET_ACTIVITY_INFO_CNF], TXID=037c, DUMP=84195424000000000100A0047C0340000000040000"
            "000000000114000000000000000000000000000000000000000000010104001F00000002010400421F0000"
            "03010400C30700000401010005000000")
    assert sa.parse_activity([line]) == [(1795381689.05, [0, 0, 0, 0, 0, 31, 8002, 1987])]


def test_short_phase_has_no_upload():
    sa = load_script(ANALYSE)
    phases, _ = sa.parse_run(phase_lines(-1, 600, 100, samples=1))
    assert phases[0].upload_kib == 0.0
    assert sa.Phase(-1, 0, "").current_ma is None


def test_main(tmp_path, capsys):
    sa = load_script(ANALYSE)
    run_log, radio_log = build([(-1, FULL, 600, 100, {}), (2, LOWER, 480, 100, {})])
    (tmp_path / "run.log").write_text("\n".join(["start 12:00:00", *run_log, "done"]) + "\n")
    # The radio log also holds the scene commands, which the evaluation passes over
    erfidx = f"  {T0:.3f}   247   407 I AT      : [0] AT> AT+ERFIDX=1,2 (RIL_CMD_READER_1)"
    (tmp_path / "activity.log").write_text("\n".join([erfidx, *radio_log]) + "\n")
    assert sa.main(["sar-analyse.py", tmp_path / "run.log", tmp_path / "activity.log"]) == 0
    assert "scene 2: inconclusive (1 valid pairs" in capsys.readouterr().out
    assert sa.main(["sar-analyse.py"]) == 2
    assert "Usage: tools/sar-analyse.py" in capsys.readouterr().err


def test_no_upload_while_settling_stops_the_run(tmp_path, fake_bin):
    script = copy_current(tmp_path)
    stubs(fake_bin, tmp_path)
    (tmp_path / "flow").unlink()
    env = fake_bin.env(isolated=True, PHASE_S="1", CYCLES="1", SCENES="2")
    assert run(["bash", script], env, timeout=60).returncode == 0
    log = (tmp_path / "run.log").read_text()
    assert "abort: 0 bytes uploaded over ccmni1 while settling tech=lte signal=7" in log
    assert "# phase" not in log and fake_bin.calls("binder-call") == []
    # Normal power and the service come back all the same
    assert fake_bin.calls("plinius-sar") == [["--scene", "-1"]]
    assert fake_bin.calls("systemctl")[-1] == ["start", "plinius-sar"]
    assert log.splitlines()[-1].startswith("done ")


def test_two_phases_without_upload_stop_the_run(tmp_path, fake_bin):
    script = copy_current(tmp_path)
    stubs(fake_bin, tmp_path)
    # The connection goes when scene 2 is set; scene 2 and the -1 after it upload nothing
    fake_bin.stub("plinius-sar", f'[ "$2" = 2 ] && rm -f {tmp_path / "flow"}; exit 0')
    env = fake_bin.env(isolated=True, PHASE_S="1", CYCLES="2", SCENES="2 0")
    assert run(["bash", script], env, timeout=60).returncode == 0
    log = (tmp_path / "run.log").read_text()
    assert "abort: two phases without upload over ccmni1" in log
    assert [c[1] for c in fake_bin.calls("plinius-sar")] == ["-1", "2", "-1", "-1"]
