# plinius-health (self-test after boot, as root) and its session notification. loginctl,
# systemctl, runuser and the radio tools are stubs; /run, /proc and /sys paths are in tmp_path.

import pytest

from conftest import PKG
from system_helpers import rewritten_copy, run

SRC = PKG / "plinius-health"


class Health:
    def __init__(self, tmp_path, fake_bin):
        self.tmp = tmp_path
        self.bin = fake_bin
        self.run_dir = tmp_path / "run" / "plinius-health"
        self.proc = tmp_path / "proc"
        self.sys = tmp_path / "sys"
        (self.proc / "4242").mkdir(parents=True)
        (self.proc / "4242" / "maps").write_text("7f00-7f10 rw-s 0 00:05 12 /dev/mali0\n")
        (self.sys / "class/net/wlan0").mkdir(parents=True)
        (self.sys / "class/bluetooth/hci0").mkdir(parents=True)
        self.script = rewritten_copy(SRC / "plinius-health", tmp_path / "plinius-health", {
            "RUN=/run/plinius-health": f"RUN={self.run_dir}",
            '"/proc/$pid/maps"': f'"{self.proc}/$pid/maps"',
            "/sys/class/net/wlan0": self.sys / "class/net/wlan0",
            "'/sys/class/bluetooth/hci*'": f"'{self.sys}/class/bluetooth/hci*'",
        }, check_left=("/proc/", "/sys/", "RUN=/run"))
        fake_bin.stub("id", 'case $# in 1) echo "${FAKE_UID:-0}" ;; *) echo 1000 ;; esac')
        # sleep only counts; the stubs below look at how often it was called, so that the
        # session, the greeter and audio can come up "after a while"
        fake_bin.stub("sleep")
        slept = f'slept() {{ [ -e "{fake_bin.path}/sleep.log" ] && tr -cd "\\036" < "{fake_bin.path}/sleep.log" | wc -c || echo 0; }}\n'
        # c1: the user's Phosh session, there after SESSION_AFTER sleeps (never with NO_SESSION);
        # g1: greetd's greeter (GREETER=1) until then; 3: an SSH login
        fake_bin.stub("loginctl", slept + '''
user_up() { [ -z "${NO_SESSION:-}" ] && [ "$(slept)" -ge "${SESSION_AFTER:-0}" ]; }
case $1 in
list-sessions) if [ -n "${GREETER:-}" ] && ! user_up; then printf "g1 958 greeter seat0 tty1\\n"; fi
    if user_up; then printf "c1 1000 plinius seat0 tty7\\n"; fi
    printf "3 0 root - pts/0\\n" ;;
show-session) case "$2 $4" in
    "c1 Seat") echo seat0 ;; "c1 Type") echo wayland ;; "c1 Class") echo user ;;
    "c1 Name") echo plinius ;;
    "g1 Seat") echo seat0 ;; "g1 Type") echo wayland ;; "g1 Class") echo "${GREETER_CLASS:-greeter}" ;;
    "g1 Name") echo greeter ;;
    "3 Seat") echo ;; "3 Type") echo tty ;; "3 Class") echo user ;; "3 Name") echo root ;; esac ;;
esac''')
        # runuser -u USER -- CMD...: run CMD (here: env XDG_RUNTIME_DIR=... systemctl/pw-cli)
        fake_bin.stub("runuser", 'while [ "$1" != -- ]; do shift; done; shift; exec "$@"')
        fake_bin.stub("systemctl", 'case "$*" in *--user*) printf "%s" "${USER_FAILED:-}"; '
                      'exit ${USER_MANAGER_RC:-0} ;; *) printf "%s" "${FAILED:-}" ;; esac')
        fake_bin.stub("lxc-info", 'echo "${CONTAINER:-RUNNING}"')
        fake_bin.stub("pgrep", 'case "$*" in *phoc*) [ -n "${NO_PHOC:-}" ] || echo 4242 ;; '
                      '*phosh*) [ -z "${NO_PHOSH:-}" ] ;; esac')
        # The HAL sink after HAL_AFTER sleeps (audio-hal-bridge a little after the session)
        fake_bin.stub("pw-cli", slept + 'node=${HAL_NODE:-hal_output}; '
                      '[ "$(slept)" -ge "${HAL_AFTER:-0}" ] || node=other; '
                      'echo "  node.name = \\"$node\\""')
        fake_bin.stub("mmcli", '[ -n "${NO_MODEM:-}" ] || '
                      'echo "/org/freedesktop/ModemManager1/Modem/0 [MediaTek] ril_0"')

    def run(self, *args, **env):
        env.setdefault("JOURNAL_STREAM", "")
        return run([self.script, *args], self.bin.env(isolated=True, **env))

    def result(self):
        return (self.run_dir / "result").read_text().splitlines()

    def sleeps(self):
        return self.bin.calls("sleep")


@pytest.fixture
def health(tmp_path, fake_bin):
    return Health(tmp_path, fake_bin)


def test_all_checks_ok(health):
    r = health.run("check")
    assert r.returncode == 0, r.stdout + r.stderr
    result = health.result()
    assert [line.split(":")[0] for line in result] == [
        "ok units", "ok user_units", "ok container", "ok display", "ok audio", "ok modem",
        "ok wifi", "ok bluetooth"]
    assert "phoc on the GPU" in result[3]
    assert (health.run_dir / "done").exists()
    assert not (health.run_dir / "result.new").exists()


def test_failures_are_counted_and_named(health):
    r = health.run("check", FAILED="foo.service loaded failed failed Foo\nbar.service x\n",
                   CONTAINER="STOPPED", NO_MODEM=1, HAL_NODE="other")
    assert r.returncode == 2
    result = dict(line.split(":", 1) for line in health.result())
    assert result["fail units"] == " failed units: foo.service bar.service"
    assert "fail container" in result and "fail modem" in result and "fail audio" in result
    assert "ok wifi" in result


def test_phoc_without_gpu_fails(health):
    (health.proc / "4242" / "maps").write_text("7f00-7f10 r-xp 0 08:01 9 /usr/lib/libc.so\n")
    assert health.run("check").returncode == 2
    assert "fail display: phoc renders without the GPU" in "\n".join(health.result())


@pytest.mark.parametrize("env,message", [({"NO_PHOC": "1"}, "fail display: phoc not running"),
                                         ({"NO_PHOSH": "1"}, "fail display: phosh not running")])
def test_missing_compositor_or_shell(health, env, message):
    assert health.run("check", **env).returncode == 2
    assert message in health.result()


def test_unreachable_user_manager_is_a_failure_not_ok(health):
    # An empty list from a systemctl that could not ask must not read as "no failed units"
    assert health.run("check", USER_MANAGER_RC="1").returncode == 2
    assert "fail user_units: user manager of plinius not reachable" in health.result()


def test_failed_user_units(health):
    assert health.run("check", USER_FAILED="phosh.service x\n").returncode == 2
    assert "fail user_units: failed user units: phosh.service" in health.result()


def test_without_session(health):
    r = health.run("check", NO_SESSION="1")
    assert r.returncode == 2
    result = health.result()
    assert result[1] == "skip user_units: no graphical session"
    assert result[3].startswith("fail display: no graphical session")
    assert result[4] == "skip audio: no graphical session"


def test_the_greeter_is_no_session(health):
    # First start: greetd's greeter on seat0 runs phoc, but it is not the user's Phosh session
    r = health.run("check", GREETER="1", NO_SESSION="1")
    assert r.returncode == 2
    assert health.result()[3].startswith("fail display: no graphical session")
    assert health.sleeps() == []  # Check Again does not wait


def test_a_greeter_session_without_class_is_known_by_its_user(health):
    r = health.run("check", GREETER="1", GREETER_CLASS="user", NO_SESSION="1")
    assert r.returncode == 2
    assert health.result()[1] == "skip user_units: no graphical session"


def test_boot_check_waits_while_the_greeter_is_up(health):
    # The PIN of a first start can take as long as it takes: no limit while the greeter is there,
    # far beyond SESSION_WAIT (30 looks)
    r = health.run("check", "--boot", GREETER="1", SESSION_AFTER="45")
    assert r.returncode == 0, r.stdout + r.stderr
    assert all(line.startswith("ok ") for line in health.result())
    assert health.sleeps() == [["10"]] * 45


def test_boot_check_without_any_session_gives_up_and_reports(health):
    r = health.run("check", "--boot", NO_SESSION="1")
    assert r.returncode == 2
    assert health.result()[3].startswith("fail display: no graphical session")
    # 300 s for a session, then the failure checked again for 180 s
    assert len(health.sleeps()) == 30 + 18


def test_boot_check_waits_for_a_late_session_without_greeter(health):
    assert health.run("check", "--boot", SESSION_AFTER="3").returncode == 0
    assert len(health.sleeps()) == 3


def test_boot_check_takes_a_failure_back_that_goes_away(health):
    # audio-hal-bridge's sink comes a little after the session: not a failure after boot
    r = health.run("check", "--boot", HAL_AFTER="2")
    assert r.returncode == 0, r.stdout
    assert "ok audio: PipeWire has the speaker of the audio HAL" in health.result()
    assert len(health.sleeps()) == 2
    assert not (health.run_dir / "result.new").exists()


def test_boot_check_reports_a_failure_that_stays(health):
    (health.sys / "class/bluetooth/hci0").rmdir()
    r = health.run("check", "--boot")
    assert r.returncode == 2
    assert health.result()[-1] == "fail bluetooth: no Bluetooth adapter (bluebinder)"
    assert len(health.sleeps()) == 18


def test_check_again_reports_at_once(health):
    # Settings' Check Again runs "check" alone: no waiting, no second look
    assert health.run("check", HAL_AFTER="2").returncode == 2
    assert health.sleeps() == []


def test_check_with_an_unknown_option(health):
    r = health.run("check", "--later")
    assert r.returncode == 1 and "usage" in r.stderr
    assert not health.run_dir.exists()


def test_the_service_runs_the_boot_check_without_holding_up_the_boot():
    unit = (SRC / "plinius-health.service").read_text().splitlines()
    assert "ExecStart=/usr/bin/plinius-health check --boot" in unit
    # The check waits for the first PIN: as a oneshot its start job (stopped after 90 s, or with
    # no limit kept open) left the boot unfinished until then
    assert "Type=exec" in unit and "Type=oneshot" not in unit
    assert not any(line.startswith("TimeoutStartSec=") for line in unit)


def test_missing_wifi_and_bluetooth(health):
    (health.sys / "class/net/wlan0").rmdir()
    (health.sys / "class/bluetooth/hci0").rmdir()
    assert health.run("check").returncode == 2
    assert health.result()[-2:] == [
        "fail wifi: no Wi-Fi interface wlan0 (plinius-wlan, Android container)",
        "fail bluetooth: no Bluetooth adapter (bluebinder)"]


def test_journal_priorities_under_systemd(health):
    r = health.run("check", JOURNAL_STREAM="8:123", NO_MODEM=1)
    lines = r.stdout.splitlines()
    assert "<4>fail modem: no modem in ModemManager (ofono, ofono2mm)" in lines
    assert "<6>ok wifi: Wi-Fi interface wlan0 present" in lines


def test_check_needs_root(health):
    r = health.run("check", FAKE_UID="1000")
    assert r.returncode == 1 and "root" in r.stderr
    assert not health.run_dir.exists()


def test_status(health):
    r = health.run("status")
    assert r.returncode == 0 and r.stdout == "not checked yet\n"
    assert health.run("check").returncode == 0
    r = health.run("status")
    assert r.returncode == 0 and len(r.stdout.splitlines()) == 8
    assert health.run("check", NO_MODEM=1).returncode == 2
    assert health.run("status").returncode == 2


def test_usage(health):
    r = health.run("bogus")
    assert r.returncode == 1 and "usage" in r.stderr


# --- notify (user unit) ---

SIG = "/org/freedesktop/Notifications: org.freedesktop.Notifications."


def sh(s):
    return "'" + s.replace("'", "'\\''") + "'"


class Notify:
    def __init__(self, tmp_path, fake_bin, result, done=True, timeout="0.2"):
        self.run_dir = tmp_path / "run"
        self.run_dir.mkdir()
        (self.run_dir / "result").write_text(result)
        if done:
            (self.run_dir / "done").touch()
        self.fake_bin = fake_bin
        # The wait for a tap: 5 s on the phone, a fraction here
        self.script = rewritten_copy(SRC / "notify", tmp_path / "notify", {
            "RUN=/run/plinius-health": f"RUN={self.run_dir}",
            "read -r -t 5 line": f"read -r -t {timeout} line",
        })
        fake_bin.stub("sleep")
        fake_bin.stub("gettext", 'printf "%s" "$1"')
        self.stub_gdbus([])

    def stub_gdbus(self, monitor_lines, reply="(uint32 7,)", stay=False, on_notify=""):
        # monitor: prints the signal lines, then stays (like the real one) or ends
        lines = "".join(f"printf '%s\\n' {sh(l)}\n" for l in monitor_lines)
        end = "exec /usr/bin/sleep 30" if stay else "exit 0"
        self.fake_bin.stub("gdbus", f"""
if [[ $1 == monitor ]]; then
{lines}{end}
fi
case "$*" in
*Notifications.Notify*) {on_notify} echo {sh(reply)} ;;
esac
exit 0""")

    def run(self):
        return run(["bash", str(self.script)], self.fake_bin.env(isolated=True))

    def calls(self):
        return [c for c in self.fake_bin.calls("gdbus") if c[0] != "monitor"]

    def body(self):
        call = self.calls()[0]
        return call[call.index("dialog-warning-symbolic") + 2]


def test_notify_names_the_failed_checks(tmp_path, fake_bin):
    n = Notify(tmp_path, fake_bin, "ok units: fine\nfail audio: no HAL\nok wifi: x\n"
               "fail modem: none\nfail user_units: failed user units: a.service\n")
    r = n.run()
    assert r.returncode == 0, r.stderr
    (call,) = n.calls()
    assert call[call.index("dialog-warning-symbolic") + 1] == "Phone self-test found a problem"
    # The names Settings shows, not the check's own
    assert n.body() == ("Not working after the start: Audio, Modem, Session services. "
                        "Tap for details and to take back the last update.")
    assert "['default', 'Show Details']" in call
    assert "{'urgency': <byte 1>, 'desktop-entry': <'org.gnome.Settings'>}" in call
    assert fake_bin.calls("sleep") == []


@pytest.mark.parametrize("check,name", [
    ("units", "System services"), ("container", "Android container"), ("display", "Display"),
    ("wifi", "Wi-Fi"), ("bluetooth", "Bluetooth"), ("new_check", "new_check"),
])
def test_notify_name_of_each_check(tmp_path, fake_bin, check, name):
    n = Notify(tmp_path, fake_bin, f"fail {check}: x\n")
    assert n.run().returncode == 0
    assert n.body().startswith(f"Not working after the start: {name}. ")


def test_notify_silent_when_all_ok(tmp_path, fake_bin):
    n = Notify(tmp_path, fake_bin, "ok units: fine\n")
    assert n.run().returncode == 0
    assert fake_bin.calls("gdbus") == []


def test_notify_silent_when_never_checked(tmp_path, fake_bin):
    n = Notify(tmp_path, fake_bin, "fail audio: x\n", done=False)
    assert n.run().returncode == 0
    assert fake_bin.calls("gdbus") == []
    assert len(fake_bin.calls("sleep")) == 900


def test_notify_percent_in_a_name_is_not_a_format(tmp_path, fake_bin):
    # The check names are printf arguments, not part of the format
    n = Notify(tmp_path, fake_bin, "fail a%sb: x\n")
    assert n.run().returncode == 0
    assert "a%sb" in n.body()


def test_notify_tap_opens_the_self_test_page(tmp_path, fake_bin):
    n = Notify(tmp_path, fake_bin, "fail audio: no HAL\n")
    # Another notification's tap first: not ours
    n.stub_gdbus([SIG + "ActionInvoked (uint32 8, 'default')",
                  SIG + "ActionInvoked (uint32 7, 'default')"], stay=True)
    r = n.run()
    assert r.returncode == 0, r.stderr
    notify, activate = n.calls()
    assert "org.freedesktop.Notifications.Notify" in notify
    assert "org.freedesktop.Application.ActivateAction" in activate
    assert "[<('system', [<'plinius-self-test'>])>]" in activate


def test_notify_closed_by_the_user_ends_quietly(tmp_path, fake_bin):
    n = Notify(tmp_path, fake_bin, "fail audio: no HAL\n")
    n.stub_gdbus([SIG + "NotificationClosed (uint32 7, uint32 2)"], stay=True)
    assert n.run().returncode == 0
    assert len(n.calls()) == 1  # only Notify


def test_notify_ends_when_phosh_goes(tmp_path, fake_bin):
    n = Notify(tmp_path, fake_bin, "fail audio: no HAL\n")
    n.stub_gdbus(["The name org.freedesktop.Notifications does not have an owner"], stay=True)
    assert n.run().returncode == 0
    assert len(n.calls()) == 1


def test_notify_removed_once_a_new_check_finds_nothing(tmp_path, fake_bin):
    n = Notify(tmp_path, fake_bin, "fail units: failed units: a.service\n")
    result = n.run_dir / "result"
    # "Check Again" in Settings right after the notification went out, now fine
    n.stub_gdbus([], stay=True, on_notify=f"echo 'ok units: fine' > {sh(str(result))};")
    r = n.run()
    assert r.returncode == 0, r.stderr
    close = n.calls()[-1]
    assert "org.freedesktop.Notifications.CloseNotification" in close and close[-1] == "7"


def test_notify_stays_while_a_new_check_runs(tmp_path, fake_bin):
    n = Notify(tmp_path, fake_bin, "fail units: failed units: a.service\n")
    done, result = n.run_dir / "done", n.run_dir / "result"
    flag = tmp_path / "waited"
    # Check Again has started (done gone, result already fine): not finished, the notification
    # stays; the check then ends with the same failure, and the user closes it
    n.fake_bin.stub("gdbus", f"""
if [[ $1 == monitor ]]; then
    for _ in $(seq 300); do [[ -e {sh(str(flag))} ]] && break; /usr/bin/sleep 0.01; done
    printf '%s\\n' {sh(SIG + "NotificationClosed (uint32 7, uint32 2)")}
    exec /usr/bin/sleep 30
fi
case "$*" in
*Notifications.Notify*) rm -f {sh(str(done))}; echo 'ok units: fine' > {sh(str(result))};
    (/usr/bin/sleep 0.7; touch {sh(str(flag))}) >/dev/null 2>&1 & echo '(uint32 7,)' ;;
esac
exit 0""")
    assert n.run().returncode == 0
    assert len(n.calls()) == 1


def test_notify_stops_without_a_notification_id(tmp_path, fake_bin):
    n = Notify(tmp_path, fake_bin, "fail audio: x\n")
    n.stub_gdbus([], reply="Error: no server")
    assert n.run().returncode == 1
