# plinius-bootcheck: what raises the alarm about a changed boot chain, what is recorded silently,
# and what "accept" takes as the new reference. Also its session notification (notify).
#
# Both run as copies with /usr/lib/plinius-bootcheck.d, /var/lib/plinius-bootcheck,
# /run/plinius-bootcheck and /proc/bootconfig below tmp_path; partitions are files, the boot menu
# card is a directory read through a debugfs stand-in.

import subprocess

import pytest

from conftest import PKG
from release_sim import DEBUGFS, bootcheck_copy, copy_script

SRC = PKG / "plinius-bootcheck" / "plinius-bootcheck"
NOTIFY = PKG / "plinius-bootcheck" / "notify"


class Check:
    def __init__(self, tmp_path, fake_bin):
        self.root = tmp_path / "root"
        self.fake_bin = fake_bin
        self.lists = self.root / "usr/lib/plinius-bootcheck.d"
        self.state = self.root / "var/lib/plinius-bootcheck"
        self.run_dir = self.root / "run/plinius-bootcheck"
        self.parts = self.root / "dev/disk/by-partlabel"
        self.card_dev = self.root / "dev/mmcblk0p1"
        self.card = tmp_path / "card"
        for d in (self.lists, self.parts, self.root / "proc"):
            d.mkdir(parents=True)
        self.script = bootcheck_copy(SRC, tmp_path / "plinius-bootcheck", self.root)
        self.slot("_b")
        for name in ("lk_a", "lk_b", "boot_a", "boot_b", "vendor_boot_b", "dtbo_b"):
            (self.parts / name).write_bytes(name.encode() * 10)
        self.write_list([f"{self.parts}/lk@slot", f"{self.parts}/boot@slot",
                         f"{self.parts}/vendor_boot@slot", f"abm {self.card_dev}"])
        fake_bin.stub("debugfs", DEBUGFS)
        fake_bin.stub("id", '[[ $1 == -u ]] && echo "${FAKE_UID:-0}"')

    def slot(self, s):
        text = f'androidboot.slot_suffix = "{s}"\n' if s else ""
        (self.root / "proc/bootconfig").write_text("androidboot.hardware = mt6789\n" + text)

    def write_list(self, lines, name="device.list"):
        (self.lists / name).write_text("# boot chain\n\n" + "\n".join(lines) + "\n")

    def boot_menu(self):
        e = self.card / "db/entries"
        e.mkdir(parents=True)
        (e / "real.conf").write_text("title Arch Linux\nlinux real/zImage\ninitrd real/initrd\n"
                                     "logo real/logo.bin\n")
        (e / "plinius-previous.conf").write_text(
            "title previous\nlinux plinius-previous/zImage\ninitrd plinius-previous/initrd.cpio.gz\n"
            "dtb plinius-previous/dtb.dtb\nlogo real/logo.bin\nxpart real\n")
        (e / "arch-sd.conf").write_text("title Arch on SD\nlinux sd/zImage\nxpart 1:3\n")
        for f in ("real/zImage", "real/initrd", "real/logo.bin", "plinius-previous/zImage",
                  "plinius-previous/initrd.cpio.gz", "plinius-previous/dtb.dtb", "sd/zImage"):
            (self.card / f).parent.mkdir(parents=True, exist_ok=True)
            (self.card / f).write_bytes(f.encode())
        self.card_dev.write_text(str(self.card))

    def run(self, *args, uid=0):
        env = self.fake_bin.env(isolated=True, FAKE_UID=uid)
        return subprocess.run(["bash", str(self.script), *args], env=env, capture_output=True,
                              text=True, timeout=60)

    def reference(self):
        ref = self.state / "reference"
        if not ref.exists():
            return {}
        return {line.split("  ", 1)[1]: line.split("  ", 1)[0]
                for line in ref.read_text().splitlines()}

    def changed(self):
        p = self.run_dir / "changed"
        return p.read_text().split() if p.exists() else []


@pytest.fixture
def check(tmp_path, fake_bin):
    return Check(tmp_path, fake_bin)


def code(r, expected):
    assert r.returncode == expected, f"exit {r.returncode}\n{r.stdout}\n{r.stderr}"
    return r


def test_first_check_records_the_reference(check):
    check.boot_menu()
    r = code(check.run("check"), 0)
    assert "no reference yet" in r.stdout
    ref = check.reference()
    p = str(check.parts)
    # Only the active slot, and only the card's own entries with the files they name
    assert set(ref) == {
        f"{p}/lk_b", f"{p}/boot_b", f"{p}/vendor_boot_b",
        "abm:/db/entries/real.conf", "abm:/real/zImage", "abm:/real/initrd", "abm:/real/logo.bin",
        "abm:/db/entries/plinius-previous.conf", "abm:/plinius-previous/zImage",
        "abm:/plinius-previous/initrd.cpio.gz", "abm:/plinius-previous/dtb.dtb"}
    import hashlib
    assert ref[f"{p}/boot_b"] == hashlib.sha256(b"boot_b" * 10).hexdigest()
    assert ref["abm:/real/zImage"] == hashlib.sha256(b"real/zImage").hexdigest()
    assert (check.run_dir / "done").exists() and check.changed() == []
    # Sorted by path, so the file stays stable
    lines = (check.state / "reference").read_text().splitlines()
    assert [l.split("  ", 1)[1] for l in lines] == sorted(ref)


def test_unchanged_boot_chain(check):
    check.boot_menu()
    code(check.run("check"), 0)
    r = code(check.run("check"), 0)
    assert "unchanged (11 entries)" in r.stdout
    assert code(check.run("status"), 0).stdout.strip() == "unchanged"


def test_changed_partition_raises_the_alarm_until_accepted(check):
    code(check.run("check"), 0)
    before = check.reference()
    boot = f"{check.parts}/boot_b"
    (check.parts / "boot_b").write_bytes(b"evil kernel")
    r = code(check.run("check"), 2)
    assert boot in r.stderr
    assert check.changed() == [boot]
    # The reference keeps the old value: the alarm comes again at every start
    assert check.reference() == before
    code(check.run("check"), 2)
    s = code(check.run("status"), 2)
    assert boot in s.stdout
    # Accepting it ends the alarm
    code(check.run("accept", boot), 0)
    assert check.changed() == []
    code(check.run("check"), 0)


def test_other_slot_is_not_judged(check):
    code(check.run("check"), 0)
    (check.parts / "boot_a").write_bytes(b"Volla OS update")
    code(check.run("check"), 0)


def test_new_list_entry_is_recorded_without_alarm(check):
    code(check.run("check"), 0)
    check.write_list([f"{check.parts}/dtbo@slot"], name="more.list")
    r = code(check.run("check"), 0)
    assert "new entries recorded" in r.stdout
    assert f"{check.parts}/dtbo_b" in check.reference()


def test_dropped_list_entry_goes_away_silently(check):
    code(check.run("check"), 0)
    check.write_list([f"{check.parts}/boot@slot"])
    code(check.run("check"), 0)
    assert list(check.reference()) == [f"{check.parts}/boot_b"]


def test_missing_path_and_glob_without_match(check):
    check.write_list([f"{check.parts}/boot@slot", f"{check.parts}/preloader_?",
                      f"{check.parts}/lk_?"])
    code(check.run("check"), 0)
    ref = check.reference()
    # A glob without matches stays as a literal path, recorded as missing
    assert ref[f"{check.parts}/preloader_?"] == "-"
    # A glob expands: both slots of lk here, as the line says
    assert f"{check.parts}/lk_a" in ref and f"{check.parts}/lk_b" in ref
    # The missing partition showing up is a change
    (check.parts / "preloader_b").write_bytes(b"x")
    check.write_list([f"{check.parts}/boot@slot", f"{check.parts}/preloader_b",
                      f"{check.parts}/lk_?"])
    code(check.run("check"), 0)  # a new path: recorded
    (check.parts / "boot_b").unlink()
    r = code(check.run("check"), 2)  # a partition gone is a change
    assert f"{check.parts}/boot_b" in r.stderr


def test_slot_unknown_stops_without_touching_the_reference(check):
    code(check.run("check"), 0)
    before = (check.state / "reference").read_text()
    (check.state / "slot").unlink()
    check.slot(None)
    r = code(check.run("check"), 1)
    assert "active slot unknown" in r.stderr
    assert (check.state / "reference").read_text() == before


def test_slot_is_remembered_for_starts_without_bootconfig(check):
    code(check.run("check"), 0)
    assert (check.state / "slot").read_text().strip() == "_b"
    check.slot(None)
    code(check.run("check"), 0)


def test_slot_is_not_written_by_a_user(check):
    r = code(check.run("status", uid=1000), 0)
    assert r.stdout.strip() == "not checked yet"
    check.run("check", uid=1000)
    assert not (check.state / "slot").exists()


def test_new_own_card_entry_raises_the_alarm(check):
    check.boot_menu()
    code(check.run("check"), 0)
    e = check.card / "db/entries/evil.conf"
    e.write_text("title Arch Linux\nlinux evil/zImage\n")
    (check.card / "evil").mkdir()
    (check.card / "evil/zImage").write_bytes(b"keylogger")
    r = code(check.run("check"), 2)
    assert set(check.changed()) == {"abm:/db/entries/evil.conf", "abm:/evil/zImage"}
    # It stays out of the reference, so the alarm stays until it is accepted
    assert "abm:/db/entries/evil.conf" not in check.reference()
    code(check.run("check"), 2)
    assert "abm:/evil/zImage" in r.stderr


def test_entries_for_card_systems_are_left_alone(check):
    check.boot_menu()
    code(check.run("check"), 0)
    (check.card / "sd/zImage").write_bytes(b"new sd kernel")
    (check.card / "db/entries/other-sd.conf").write_text("linux x/zImage\nxpart 1:5\n")
    code(check.run("check"), 0)


def test_changed_kernel_of_a_card_entry_raises_the_alarm(check):
    check.boot_menu()
    code(check.run("check"), 0)
    (check.card / "plinius-previous/zImage").write_bytes(b"swapped")
    code(check.run("check"), 2)
    assert check.changed() == ["abm:/plinius-previous/zImage"]


def test_card_taken_out_is_no_change(check):
    check.boot_menu()
    code(check.run("check"), 0)
    before = check.reference()
    check.card_dev.unlink()
    code(check.run("check"), 0)
    assert check.reference() == before  # the card entries keep their reference
    check.card_dev.write_text(str(check.card))
    code(check.run("check"), 0)


def test_card_back_with_a_changed_entry_raises_the_alarm(check):
    check.boot_menu()
    code(check.run("check"), 0)
    check.card_dev.unlink()
    code(check.run("check"), 0)
    (check.card / "real/initrd").write_bytes(b"swapped while out")
    check.card_dev.write_text(str(check.card))
    code(check.run("check"), 2)
    assert check.changed() == ["abm:/real/initrd"]


def test_first_card_seen_after_the_reference_is_recorded(check):
    # The reference had no card yet: its entries are new list entries, not an alarm
    code(check.run("check"), 0)
    check.boot_menu()
    code(check.run("check"), 0)
    assert "abm:/db/entries/real.conf" in check.reference()


def test_accept_needs_root(check):
    code(check.run("check"), 0)
    r = code(check.run("accept", uid=1000), 1)
    assert "must run as root" in r.stderr


def test_accept_without_paths_takes_everything(check):
    check.boot_menu()
    code(check.run("check"), 0)
    (check.parts / "lk_b").write_bytes(b"new lk")
    (check.card / "real/zImage").write_bytes(b"new")
    code(check.run("check"), 2)
    r = code(check.run("accept"), 0)
    assert "accepted 11 entries" in r.stdout
    assert check.changed() == []
    code(check.run("check"), 0)


def test_accept_of_a_path_outside_the_lists_stops(check):
    code(check.run("check"), 0)
    r = code(check.run("accept", "/etc/passwd"), 1)
    assert "not in the lists" in r.stderr


def test_accept_of_a_card_entry_takes_its_files(check):
    check.boot_menu()
    code(check.run("check"), 0)
    (check.card / "plinius-previous/zImage").write_bytes(b"kernel 2")
    (check.card / "plinius-previous/dtb.dtb").write_bytes(b"dtb 2")
    (check.parts / "boot_b").write_bytes(b"boot 2")
    code(check.run("check"), 2)
    code(check.run("accept", "abm-entry:plinius-previous"), 0)
    # Only boot_b is left as an alarm
    assert check.changed() == [f"{check.parts}/boot_b"]
    r = code(check.run("check"), 2)
    assert "plinius-previous" not in r.stderr


def test_accept_of_a_missing_card_entry_stops(check):
    check.boot_menu()
    code(check.run("check"), 0)
    r = code(check.run("accept", "abm-entry:nope"), 1)
    assert "no entry nope" in r.stderr
    check.card_dev.unlink()
    r = code(check.run("accept", "abm-entry:real"), 1)
    assert "no boot menu card" in r.stderr


def test_accept_written_leaves_an_already_reported_change_to_the_user(check):
    code(check.run("check"), 0)
    boot = f"{check.parts}/boot_b"
    lk = f"{check.parts}/lk_b"
    (check.parts / "boot_b").write_bytes(b"swapped before this start")
    code(check.run("check"), 2)
    # A package now writes both: boot_b was reported at this start, so the change that ran
    # stays for the user to confirm; lk_b is taken
    old_boot = check.reference()[boot]
    (check.parts / "lk_b").write_bytes(b"lk from a package")
    r = code(check.run("accept", "--written", boot, lk), 0)
    assert "left for the user" in r.stderr
    assert check.reference()[boot] == old_boot
    assert check.changed() == [boot]
    code(check.run("check"), 2)


def test_accept_written_of_only_reported_paths_changes_nothing(check):
    code(check.run("check"), 0)
    boot = f"{check.parts}/boot_b"
    (check.parts / "boot_b").write_bytes(b"x")
    code(check.run("check"), 2)
    before = (check.state / "reference").read_text()
    code(check.run("accept", "--written", boot), 0)
    assert (check.state / "reference").read_text() == before


def test_no_lists_nothing_to_check(check):
    (check.lists / "device.list").unlink()
    r = code(check.run("check"), 0)
    assert "nothing to check" in r.stdout
    assert (check.run_dir / "done").exists()


@pytest.mark.parametrize("args", [["check", "extra"], ["status", "x"], ["frob"], []])
def test_usage(check, args):
    r = code(check.run(*args), 1)
    assert "usage" in r.stderr


# --- notify: the session notification ----------------------------------------------------------

class Notify:
    def __init__(self, tmp_path, fake_bin, timeout="0.2"):
        self.run_dir = tmp_path / "run"
        self.run_dir.mkdir()
        self.fake_bin = fake_bin
        # The wait for a tap: 5 s on the phone, a fraction here
        self.script = copy_script(NOTIFY, tmp_path / "notify", {
            "RUN=/run/plinius-bootcheck": "RUN=@ROOT@",
            "read -r -t 5 line": f"read -r -t {timeout} line",
        }, self.run_dir)

    def stub_gdbus(self, monitor_lines, reply="(uint32 7,)", stay=True, close_removes=None):
        # monitor: prints the signal lines, then stays (like the real one) or ends
        lines = "".join(f"printf '%s\\n' {sh(l)}\n" for l in monitor_lines)
        end = "exec sleep 30" if stay else "exit 0"
        self.fake_bin.stub("gdbus", f'''
if [[ $1 == monitor ]]; then
{lines}{end}
fi
case "$*" in
*Notifications.Notify*) echo {sh(reply)} ;;
esac
exit 0''')

    def run(self):
        env = self.fake_bin.env(isolated=True)
        return subprocess.run(["bash", str(self.script)], env=env, capture_output=True, text=True,
                              timeout=30)

    def calls(self):
        return [c for c in self.fake_bin.calls("gdbus") if c[0] != "monitor"]


def sh(s):
    return "'" + s.replace("'", "'\\''") + "'"


SIG = "/org/freedesktop/Notifications: org.freedesktop.Notifications."


def test_notify_without_changes_shows_nothing(tmp_path, fake_bin):
    n = Notify(tmp_path, fake_bin)
    (n.run_dir / "done").touch()
    n.stub_gdbus([])
    r = n.run()
    assert r.returncode == 0
    assert fake_bin.calls("gdbus") == []


def test_notify_tap_opens_the_boot_chain_page(tmp_path, fake_bin):
    n = Notify(tmp_path, fake_bin)
    (n.run_dir / "done").touch()
    (n.run_dir / "changed").write_text("/dev/disk/by-partlabel/boot_b\nabm:/real/zImage\n")
    n.stub_gdbus([SIG + "ActionInvoked (uint32 8, 'default')",
                  SIG + "ActionInvoked (uint32 7, 'default')"])
    r = n.run()
    assert r.returncode == 0, r.stderr
    notify, activate = n.calls()
    assert "org.freedesktop.Notifications.Notify" in notify
    body = notify[notify.index("dialog-warning-symbolic") + 2]
    assert "boot_b, zImage." in body
    assert "{'urgency': <byte 2>, 'desktop-entry': <'org.gnome.Settings'>}" in notify
    assert "org.freedesktop.Application.ActivateAction" in activate
    assert "[<('security', [<'plinius-boot-chain'>])>]" in activate


def test_notify_closed_by_the_user_ends_quietly(tmp_path, fake_bin):
    n = Notify(tmp_path, fake_bin)
    (n.run_dir / "done").touch()
    (n.run_dir / "changed").write_text("/dev/disk/by-partlabel/boot_b\n")
    n.stub_gdbus([SIG + "NotificationClosed (uint32 7, uint32 2)"])
    assert n.run().returncode == 0
    assert len(n.calls()) == 1  # only Notify


def test_notify_removed_once_the_change_is_accepted(tmp_path, fake_bin):
    n = Notify(tmp_path, fake_bin)
    (n.run_dir / "done").touch()
    changed = n.run_dir / "changed"
    changed.write_text("/dev/disk/by-partlabel/boot_b\n")
    # Accepted in Settings right after the notification went out
    fake_bin.stub("gdbus", f'''
if [[ $1 == monitor ]]; then exec sleep 30; fi
case "$*" in
*Notifications.Notify*) : > {sh(str(changed))}; echo '(uint32 7,)' ;;
esac
exit 0''')
    r = n.run()
    assert r.returncode == 0, r.stderr
    close = n.calls()[-1]
    assert "org.freedesktop.Notifications.CloseNotification" in close and close[-1] == "7"


def test_notify_ends_when_the_monitor_ends_during_the_wait(tmp_path, fake_bin):
    n = Notify(tmp_path, fake_bin, timeout="5")
    (n.run_dir / "done").touch()
    (n.run_dir / "changed").write_text("x\n")
    # The bus goes while the script waits in "read" for a signal line: the monitor ends once
    # the script sleeps in poll/select (its wchan), after the notification went out
    sent = tmp_path / "sent"
    fake_bin.stub("gdbus", f'''
if [[ $1 == monitor ]]; then
	for _ in $(seq 500); do
		[[ -e {sh(str(sent))} ]] && grep -qE "poll|select" /proc/$PPID/wchan 2>/dev/null && break
		sleep 0.01
	done
	exit 0
fi
case "$*" in
*Notifications.Notify*) touch {sh(str(sent))}; echo '(uint32 7,)' ;;
esac
exit 0''')
    r = n.run()
    assert r.returncode == 0, r.stderr


def test_notify_ends_quietly_when_the_monitor_is_already_gone(tmp_path, fake_bin):
    n = Notify(tmp_path, fake_bin)
    (n.run_dir / "done").touch()
    (n.run_dir / "changed").write_text("x\n")
    pid = tmp_path / "monitor.pid"
    # Notify answers only once the monitor has ended and was reaped
    fake_bin.stub("gdbus", f'''
if [[ $1 == monitor ]]; then echo $$ > {sh(str(pid))}; exit 0; fi
case "$*" in
*Notifications.Notify*)
	for _ in $(seq 300); do
		[[ -s {sh(str(pid))} ]] && ! kill -0 "$(cat {sh(str(pid))})" 2>/dev/null && break
		sleep 0.01
	done
	echo '(uint32 7,)' ;;
esac
exit 0''')
    r = n.run()
    assert r.returncode == 0, r.stderr


def test_notify_stops_without_a_notification_id(tmp_path, fake_bin):
    n = Notify(tmp_path, fake_bin)
    (n.run_dir / "done").touch()
    (n.run_dir / "changed").write_text("x\n")
    n.stub_gdbus([], reply="Error: no server")
    assert n.run().returncode == 1
