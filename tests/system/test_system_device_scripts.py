# The small shell scripts of device-volla-ansuz (boot helpers, pacman hook scripts, bash
# snippets) and its install script. Each runs as a copy whose /dev, /sys, /run, /usr, /etc and
# /android paths point into tmp_path; programs that change state are stubs.

import os
import socket
import stat

import pytest

from conftest import PKG
from system_helpers import call_scriptlet, rewritten_copy, run, run_bash, scriptlet

DEV = PKG / "device-volla-ansuz"


def copy(tmp_path, name, mapping, check_left=()):
    return rewritten_copy(DEV / name, tmp_path / name, mapping, check_left=check_left)


# --- droid-get-bt-address.sh ---

def test_bt_address_from_nvram(tmp_path, fake_bin):
    nvram = tmp_path / "BT_Addr"
    nvram.write_bytes(bytes([0x00, 0x0a, 0xf5, 0x12, 0x34, 0xfe, 0x99, 0x99]))
    bt = tmp_path / "bluetooth"
    script = copy(tmp_path, "droid-get-bt-address.sh", {
        "/mnt/vendor/nvdata/APCFG/APRDEB/BT_Addr": nvram,
        "/var/lib/bluetooth": bt,
    }, check_left=("/var/", "/mnt/"))
    fake_bin.stub("setprop")
    r = run(["sh", script], fake_bin.env(isolated=True))
    assert r.returncode == 0, r.stderr
    assert fake_bin.calls("setprop") == [["persist.service.bdroid.bdaddr", "00:0a:f5:12:34:fe"]]
    assert (bt / "board-address").read_text() == "00:0a:f5:12:34:fe\n"


def test_bt_address_without_nvram_hands_over_no_address(tmp_path, fake_bin):
    script = copy(tmp_path, "droid-get-bt-address.sh", {
        "/mnt/vendor/nvdata/APCFG/APRDEB/BT_Addr": tmp_path / "missing",
        "/var/lib/bluetooth": tmp_path / "bluetooth",
    })
    fake_bin.stub("setprop")
    r = run(["sh", script], fake_bin.env(isolated=True))
    # sh has no pipefail: od's error is lost and the address stays empty (the HAL then makes up
    # one), bluebinder still starts. Never a partial or garbage address.
    assert r.returncode == 0
    assert fake_bin.calls("setprop") == [["persist.service.bdroid.bdaddr", ""]]
    assert (tmp_path / "bluetooth" / "board-address").read_text() == "\n"


# --- keyring-prompter-wait ---

def prompter(tmp_path, fake_bin, owned_after):
    count = tmp_path / "count"
    fake_bin.stub("busctl", f'n=$(cat "{count}" 2>/dev/null || echo 0); n=$((n + 1)); '
                  f'echo $n > "{count}"; [ $n -gt {owned_after} ]')
    fake_bin.stub("sleep")
    gcr = fake_bin.stub("gcr-prompter")
    script = copy(tmp_path, "keyring-prompter-wait", {"/usr/lib/gcr-prompter": gcr})
    return run(["sh", script], fake_bin.env(isolated=True))


def test_prompter_waits_for_phosh(tmp_path, fake_bin):
    r = prompter(tmp_path, fake_bin, owned_after=5)
    assert r.returncode == 0
    assert len(fake_bin.calls("sleep")) == 5 and fake_bin.calls("gcr-prompter") == []
    assert fake_bin.calls("busctl")[0] == ["--user", "status", "org.gnome.keyring.SystemPrompter"]


def test_prompter_falls_back_to_gcr(tmp_path, fake_bin):
    r = prompter(tmp_path, fake_bin, owned_after=1000)
    assert r.returncode == 0
    assert len(fake_bin.calls("busctl")) == 90 and fake_bin.calls("gcr-prompter") == [[]]


# --- plinius-android-initrc ---

CALL = "    exec - system system -- /system/bin/vdc keymaster earlyBootEnded"


def initrc(tmp_path, fake_bin, text, mounted=False):
    rc = tmp_path / "android" / "init.rc"
    rc.parent.mkdir()
    rc.write_text(text)
    out = tmp_path / "run" / "plinius" / "init.rc"
    script = copy(tmp_path, "plinius-android-initrc", {
        "/android/system/etc/init/hw/init.rc": rc,
        "/run/plinius/init.rc": out,
    }, check_left=("=/android", "=/run"))
    fake_bin.stub("findmnt", f"exit {0 if mounted else 1}")
    fake_bin.stub("mount")
    r = run(["sh", script], fake_bin.env(isolated=True))
    return r, rc, out


def test_initrc_drops_only_the_keymaster_call(tmp_path, fake_bin):
    text = ("on post-fs-data\n    mkdir /data/x\n" + CALL + "\n"
            "    exec - system system -- /system/bin/vdc keymaster other\n"
            "    # exec - system system -- /system/bin/vdc keymaster earlyBootEnded\n")
    r, rc, out = initrc(tmp_path, fake_bin, text)
    assert r.returncode == 0, r.stderr
    assert out.read_text() == text.replace(CALL + "\n", "")
    # init skips an init.rc writable by group or others
    assert stat.S_IMODE(out.stat().st_mode) == 0o644
    assert fake_bin.calls("mount") == [["--bind", str(out), str(rc)]]


def test_initrc_without_the_call_changes_nothing(tmp_path, fake_bin):
    r, _, out = initrc(tmp_path, fake_bin, "on boot\n    start x\n")
    assert r.returncode == 0 and not out.exists() and fake_bin.calls("mount") == []


def test_initrc_already_mounted(tmp_path, fake_bin):
    r, _, out = initrc(tmp_path, fake_bin, CALL + "\n", mounted=True)
    assert r.returncode == 0 and not out.exists() and fake_bin.calls("mount") == []


# --- plinius-dev-acl ---

@pytest.mark.parametrize("acl", [True, False])
def test_dev_acl(tmp_path, fake_bin, acl):
    dev = tmp_path / "dev"
    dev.mkdir()
    rule = tmp_path / "run" / "udev" / "rules.d" / "73-seat-late.rules"
    noacl = DEV / "73-plinius-seat-late-noacl.rules"
    script = copy(tmp_path, "plinius-dev-acl", {
        "/run/udev/rules.d/73-seat-late.rules": rule,
        "-p /dev": f"-p {dev}",
        "/usr/lib/plinius/udev/73-seat-late-noacl.rules": noacl,
    }, check_left=("=/run", "/usr/lib/plinius/udev", "-p /dev"))
    fake_bin.stub("setfacl", f"exit {0 if acl else 1}")
    if acl:
        rule.parent.mkdir(parents=True)
        rule.write_text("old")
    r = run(["sh", script], fake_bin.env(isolated=True))
    assert r.returncode == 0
    if acl:
        assert not rule.exists()
    else:
        assert rule.read_text() == noacl.read_text()
    assert list(dev.iterdir()) == []  # the probe file is gone
    (call,) = fake_bin.calls("setfacl")
    assert call[:2] == ["-m", "u:root:r"] and call[2].startswith(f"{dev}/.plinius-acl.")


# --- plinius-display-nfc, plinius-nfc-power ---

@pytest.mark.parametrize("state,powered", [("on", "true"), ("off", "false"), ("", "false")])
def test_display_nfc(fake_bin, state, powered):
    fake_bin.stub("busctl", "exit 1")  # nfcd not running yet: still no failure
    r = run(["sh", DEV / "plinius-display-nfc", state], fake_bin.env(isolated=True))
    assert r.returncode == 0
    assert fake_bin.calls("busctl") == [["set-property", "org.neard", "/nfc0",
                                         "org.neard.Adapter", "Powered", "b", powered]]


def nfc_power(tmp_path, fake_bin, dpms, fails):
    dpms_file = tmp_path / "dpms"
    dpms_file.write_text(dpms + "\n")
    count = tmp_path / "count"
    fake_bin.stub("busctl", f'n=$(cat "{count}" 2>/dev/null || echo 0); n=$((n + 1)); '
                  f'echo $n > "{count}"; [ $n -gt {fails} ]')
    fake_bin.stub("sleep")
    script = copy(tmp_path, "plinius-nfc-power", {"/sys/class/drm/card0-DSI-1/dpms": dpms_file})
    return run(["sh", script], fake_bin.env(isolated=True))


def test_nfc_power_display_off(tmp_path, fake_bin):
    assert nfc_power(tmp_path, fake_bin, "Off", 0).returncode == 0
    assert fake_bin.calls("busctl") == []


def test_nfc_power_retries_until_nfcd_has_the_adapter(tmp_path, fake_bin):
    assert nfc_power(tmp_path, fake_bin, "On", 3).returncode == 0
    assert len(fake_bin.calls("busctl")) == 4 and len(fake_bin.calls("sleep")) == 3


def test_nfc_power_gives_up(tmp_path, fake_bin):
    r = nfc_power(tmp_path, fake_bin, "On", 1000)
    assert r.returncode == 1 and "no NFC adapter" in r.stderr
    assert len(fake_bin.calls("busctl")) == 31


# --- plinius-drop-setuid, plinius-polkit-helper (pacman hook scripts) ---

def test_drop_setuid(tmp_path, fake_bin):
    names = ["ksu", "mount.cifs", "lxc-user-nic", "chfn", "chsh"]
    files = {n: tmp_path / n for n in names}
    for n in ("ksu", "chfn", "lxc-user-nic"):
        files[n].write_text("")
        files[n].chmod(0o6755)
    target = tmp_path / "real-chsh"
    target.write_text("")
    target.chmod(0o4755)
    files["chsh"].symlink_to(target)  # a link is left alone
    script = copy(tmp_path, "plinius-drop-setuid", {
        "/usr/bin/ksu": files["ksu"], "/usr/bin/mount.cifs": files["mount.cifs"],
        "/usr/lib/lxc/lxc-user-nic": files["lxc-user-nic"], "/usr/bin/chfn": files["chfn"],
        "/usr/bin/chsh": files["chsh"],
    }, check_left=(" /usr/",))
    r = run(["sh", script], fake_bin.env(isolated=True))
    assert r.returncode == 0, r.stderr
    for n in ("ksu", "chfn", "lxc-user-nic"):
        assert stat.S_IMODE(files[n].stat().st_mode) == 0o755
    assert stat.S_IMODE(target.stat().st_mode) == 0o4755


@pytest.mark.parametrize("booted", [True, False])
def test_polkit_helper(tmp_path, fake_bin, booted):
    helper = tmp_path / "polkit-agent-helper-1"
    helper.write_text("")
    helper.chmod(0o755)
    sock = tmp_path / "agent-helper.socket"
    sock.write_text("")
    run_dir = tmp_path / "run-systemd"
    if booted:
        run_dir.mkdir()
    script = copy(tmp_path, "plinius-polkit-helper", {
        "/usr/lib/polkit-1/polkit-agent-helper-1": helper,
        "/run/systemd/system": run_dir,
        "/run/polkit/agent-helper.socket": sock,
    }, check_left=("=/usr", "/run/"))
    fake_bin.stub("systemctl", "exit 5")
    r = run(["sh", script], fake_bin.env(isolated=True))
    assert r.returncode == 0
    assert stat.S_IMODE(helper.stat().st_mode) == 0o4755
    assert sock.exists() != booted
    assert fake_bin.calls("systemctl") == ([["stop", "polkit-agent-helper.socket"]]
                                           if booted else [])


# --- plinius-hide-apps ---

def test_hide_apps(tmp_path, fake_bin):
    apps = tmp_path / "usr/share/applications"
    apps.mkdir(parents=True)
    (apps / "a.desktop").write_text("[Desktop Entry]\nName=A\nNoDisplay=false\n\n"
                                    "[Desktop Action new]\nName=New\n")
    (apps / "b.desktop").write_text("[Desktop Entry]\nName=B\n")
    script = copy(tmp_path, "plinius-hide-apps", {'f="/$target"': f'f="{tmp_path}/$target"'})
    r = run(["sh", script], fake_bin.env(isolated=True),
            input="usr/share/applications/a.desktop\nusr/share/applications/b.desktop\n"
                  "usr/share/applications/missing.desktop\n")
    assert r.returncode == 0, r.stderr
    # Right below [Desktop Entry]: a key further down would land in the action's group
    assert (apps / "a.desktop").read_text() == ("[Desktop Entry]\nNoDisplay=true\nName=A\n\n"
                                                "[Desktop Action new]\nName=New\n")
    assert (apps / "b.desktop").read_text() == "[Desktop Entry]\nNoDisplay=true\nName=B\n"
    assert not (apps / "missing.desktop").exists()
    # A second run (the next upgrade) gives the same file
    run(["sh", script], fake_bin.env(isolated=True), input="usr/share/applications/b.desktop\n")
    assert (apps / "b.desktop").read_text() == "[Desktop Entry]\nNoDisplay=true\nName=B\n"


# --- plinius-udev-reload ---

class UdevReload:
    def __init__(self, tmp_path, fake_bin):
        self.tmp = tmp_path
        self.bin = fake_bin
        self.control = tmp_path / "control"
        self.no_trigger = tmp_path / "do-not-udevadm-trigger-on-update"
        self.udevadm = fake_bin.stub("udevadm")
        self.script = copy(tmp_path, "plinius-udev-reload", {
            "/run/udev/control": self.control,
            "/usr/bin/udevadm": self.udevadm,
            "/etc/systemd/do-not-udevadm-trigger-on-update": self.no_trigger,
        }, check_left=("/run/", "/usr/bin", "/etc/"))
        fake_bin.stub("systemd-detect-virt", "exit 1")
        fake_bin.stub("systemd-notify", "exit 0")

    def socket(self):
        s = socket.socket(socket.AF_UNIX)
        s.bind(str(self.control))
        s.close()

    def run(self):
        return run(["sh", "-e", self.script], self.bin.env(isolated=True))


@pytest.fixture
def udev(tmp_path, fake_bin):
    return UdevReload(tmp_path, fake_bin)


def test_udev_reload_triggers_without_power_supply(udev):
    udev.socket()
    r = udev.run()
    assert r.returncode == 0, r.stderr
    assert udev.bin.calls("udevadm") == [["control", "--reload"],
                                         ["trigger", "-c", "change",
                                          "--subsystem-nomatch=power_supply"], ["settle"]]


def test_udev_reload_failures_still_fail(udev):
    udev.socket()
    udev.bin.stub("udevadm", '[ "$1" = trigger ] && exit 1; exit 0')
    assert udev.run().returncode == 1


def test_udev_reload_without_trigger_switch(udev):
    udev.socket()
    udev.no_trigger.touch()
    assert udev.run().returncode == 0
    assert udev.bin.calls("udevadm") == [["control", "--reload"]]


@pytest.mark.parametrize("case", ["chroot", "not booted", "no udevd"])
def test_udev_reload_skips(udev, case):
    if case == "chroot":
        udev.socket()
        udev.bin.stub("systemd-detect-virt", "exit 0")
    elif case == "not booted":
        udev.socket()
        udev.bin.stub("systemd-notify", "exit 1")
    r = udev.run()
    assert r.returncode == 0 and "Skipped" in r.stderr
    assert udev.bin.calls("udevadm") == []


# --- plinius-user-sessions-generator ---

def test_user_sessions_generator(tmp_path, fake_bin):
    unit = tmp_path / "systemd-user-sessions.service"
    unit.write_text("[Unit]\nDescription=Permit User Sessions\n"
                    "After=remote-fs.target nss-user-lookup.target network.target home.mount\n"
                    "After=network.target\n[Service]\nType=oneshot\n"
                    "ExecStart=/usr/lib/systemd/systemd-user-sessions start # network.target\n")
    out = tmp_path / "gen"
    out.mkdir()
    script = copy(tmp_path, "plinius-user-sessions-generator",
                  {"/usr/lib/systemd/system/systemd-user-sessions.service": unit})
    r = run(["sh", script, out, tmp_path / "early", tmp_path / "late"],
            fake_bin.env(isolated=True))
    assert r.returncode == 0, r.stderr
    assert (out / "systemd-user-sessions.service").read_text() == (
        "[Unit]\nDescription=Permit User Sessions\n"
        "After=remote-fs.target nss-user-lookup.target home.mount\nAfter=\n[Service]\n"
        "Type=oneshot\n"
        "ExecStart=/usr/lib/systemd/systemd-user-sessions start # network.target\n")


def test_user_sessions_generator_nothing_to_do(tmp_path, fake_bin):
    unit = tmp_path / "u.service"
    unit.write_text("[Unit]\nAfter=remote-fs.target\n")
    out = tmp_path / "gen"
    out.mkdir()
    script = copy(tmp_path, "plinius-user-sessions-generator",
                  {"/usr/lib/systemd/system/systemd-user-sessions.service": unit})
    assert run(["sh", script, out, out, out], fake_bin.env(isolated=True)).returncode == 0
    assert list(out.iterdir()) == []


# --- plinius-wlan ---

class Wlan:
    def __init__(self, tmp_path, fake_bin):
        self.tmp = tmp_path
        self.bin = fake_bin
        self.props = tmp_path / "props"
        self.props.mkdir()
        self.modules = tmp_path / "modules"
        self.modules.write_text("cfg80211 1234 1 mac80211, Live 0x0\n")
        self.wmt = tmp_path / "wmtWifi"
        self.dbg = tmp_path / "wlan" / "dbgLevel"
        self.dbg.parent.mkdir()
        self.script = copy(tmp_path, "plinius-wlan", {
            "MODULES=/android/vendor_dlkm/lib/modules": "MODULES=/vendor-modules",
            "/proc/modules": self.modules,
            "> /dev/wmtWifi": f'>> "{self.wmt}"',
            "/proc/net/wlan/dbgLevel": self.dbg,
        }, check_left=("/proc/", "> /dev/", "=/android"))
        # getprop NAME: the value in props/NAME; props/NAME.after: the value from the n-th call on
        fake_bin.stub("getprop", f'''
f="{self.props}/$1"; c="$f.calls"; n=$(cat "$c" 2>/dev/null || echo 0); n=$((n + 1)); echo $n > "$c"
if [ -e "$f.after" ] && [ $n -ge "$(cut -d' ' -f1 "$f.after")" ]; then cut -d' ' -f2 "$f.after"; else cat "$f" 2>/dev/null; fi''')
        fake_bin.stub("setprop")
        fake_bin.stub("insmod")
        fake_bin.stub("sleep")
        fake_bin.stub("nmcli", 'case $2 in GENERAL.STATE) echo "${NM_STATE:-100 (connected)}" ;; '
                      'esac')

    def prop(self, name, value, after=None):
        (self.props / name).write_text(value + "\n")
        if after:
            (self.props / f"{name}.after").write_text(f"{after[0]} {after[1]}\n")

    def run(self, **env):
        return run(["sh", self.script], self.bin.env(isolated=True, **env))


@pytest.fixture
def wlan(tmp_path, fake_bin):
    return Wlan(tmp_path, fake_bin)


def test_wlan_powers_on(wlan):
    wlan.prop("vendor.service.nvram_init", "Ready")
    wlan.prop("vendor.mtk.nvram.ready", "1")
    r = wlan.run()
    assert r.returncode == 0, r.stderr
    # Only the modules not loaded yet, from the vendor partition
    assert wlan.bin.calls("insmod") == [["/vendor-modules/mac80211.ko"],
                                        ["/vendor-modules/wlan_drv_gen4m_6878.ko"]]
    assert wlan.wmt.read_text() == "C\nP\n"
    assert wlan.dbg.read_text() == "0xff:0x0f\n"
    assert wlan.bin.calls("setprop") == []
    assert ["device", "set", "wlan0", "managed", "no"] not in wlan.bin.calls("nmcli")


def test_wlan_restarts_wlan_assistant_once(wlan):
    wlan.prop("vendor.service.nvram_init", "Ready")
    wlan.prop("vendor.mtk.nvram.ready", "0", after=(20, "1"))
    r = wlan.run()
    assert r.returncode == 0, r.stderr
    assert wlan.bin.calls("setprop") == [["ctl.restart", "wlan_assistant"]]
    assert wlan.wmt.read_text() == "C\nP\n"


def test_wlan_gives_up_without_nvram(wlan):
    wlan.prop("vendor.service.nvram_init", "Ready")
    wlan.prop("vendor.mtk.nvram.ready", "0")
    r = wlan.run()
    assert r.returncode == 1 and "NVRAM not handed" in r.stderr
    assert not wlan.wmt.exists()
    # 3 s and then 30 s, five checks a second
    assert len(wlan.bin.calls("sleep")) == 15 + 150


def test_wlan_waits_for_the_nvram_daemon(wlan):
    wlan.prop("vendor.service.nvram_init", "", after=(4, "Ready"))
    wlan.prop("vendor.mtk.nvram.ready", "1")
    assert wlan.run().returncode == 0
    assert wlan.bin.calls("sleep")[:3] == [["0.2"]] * 3


def test_wlan_hands_an_unavailable_wlan0_over_again(wlan):
    wlan.prop("vendor.service.nvram_init", "Ready")
    wlan.prop("vendor.mtk.nvram.ready", "1")
    assert wlan.run(NM_STATE="20 (unavailable)").returncode == 0
    assert wlan.bin.calls("nmcli")[1:] == [["device", "set", "wlan0", "managed", "no"],
                                           ["device", "set", "wlan0", "managed", "yes"]]


def test_wlan_without_debug_level_file(wlan):
    wlan.dbg.parent.rmdir()
    wlan.prop("vendor.service.nvram_init", "Ready")
    wlan.prop("vendor.mtk.nvram.ready", "1")
    assert wlan.run().returncode == 0


# --- bash snippets: plinius-prompt.sh, plinius-aliases.sh ---

def prompt(fake_bin, custom=None, named=None, ps1="[\\u@\\h \\W]\\$ ", term="xterm"):
    if custom is not None or named is not None:
        fake_bin.stub("gsettings", f'case $3 in accent-color) case $2 in '
                      f'org.plinius.appearance) echo "{custom or ""}" ;; '
                      f'*) echo "{named or ""}" ;; esac ;; esac')
    code = (f"PS1='{ps1}'; . '{DEV / 'plinius-prompt.sh'}'; "
            'for c in "${PROMPT_COMMAND[@]}"; do $c; done; printf "%s" "$PS1"')
    r = run_bash(code, fake_bin.env(isolated=True, TERM=term, HOME="/nonexistent"))
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_prompt_custom_accent(fake_bin):
    assert prompt(fake_bin, custom="'#ff8000'") == "\\[\\e[1;38;2;255;128;0m\\]\\u\\[\\e[0m\\]@\\h:\\w\\$ "


def test_prompt_named_accent_when_custom_is_invalid(fake_bin):
    assert ";33;144;164m" in prompt(fake_bin, custom="'#zz0000'", named="'teal'")
    # GNOME's default blue for an unknown name
    assert ";53;132;228m" in prompt(fake_bin, custom="''", named="'nope'")


def test_prompt_without_gsettings_or_dumb_terminal(fake_bin):
    assert prompt(fake_bin) == "\\u@\\h:\\w\\$ "
    assert prompt(fake_bin, custom="'#ff8000'", term="dumb") == "\\u@\\h:\\w\\$ "


def test_prompt_own_prompt_stays(fake_bin):
    assert prompt(fake_bin, custom="'#ff8000'", ps1="mine> ") == "mine> "


@pytest.mark.parametrize("vim", [True, False])
def test_aliases(fake_bin, vim):
    if vim:
        fake_bin.stub("vim")
    r = run_bash(f". '{DEV / 'plinius-aliases.sh'}'; alias", fake_bin.env(isolated=True))
    assert "alias rm='rm -i'" in r.stdout and "alias ll='ls -l --color=auto'" in r.stdout
    assert ("alias vi='vim'" in r.stdout) == vim


# --- device-volla-ansuz.install ---

class DeviceInstall:
    def __init__(self, tmp_path, fake_bin):
        self.tmp = tmp_path
        self.bin = fake_bin
        self.sudoers = tmp_path / "sudoers.d" / "wheel"
        self.sudoers.parent.mkdir()
        self.bashrc = tmp_path / "bash.bashrc"
        self.sys = tmp_path / "sys"
        self.dev = tmp_path / "dev"
        for d in ("class/video4linux", "bus/media/devices", "devices/gadget/video9",
                  "devices/platform/video0"):
            (self.sys / d).mkdir(parents=True)
        (self.sys / "class/video4linux/video0").symlink_to(self.sys / "devices/platform/video0")
        (self.sys / "class/video4linux/video9").symlink_to(self.sys / "devices/gadget/video9")
        self.dev.mkdir()
        # Character devices cannot be made without root: links to /dev/null pass the -c test,
        # and setfacl is a stub, so nothing reaches /dev/null itself
        (self.dev / "video0").symlink_to("/dev/null")
        (self.dev / "video9").symlink_to("/dev/null")
        self.firewall = fake_bin.stub("mobile-firewall")
        self.script = scriptlet("device-volla-ansuz", tmp_path, {
            "/etc/sudoers.d/wheel": self.sudoers,
            "/usr/lib/NetworkManager/dispatcher.d/10-plinius-mobile-firewall": self.firewall,
            "/sys/class/video4linux/* /sys/bus/media/devices/*":
                f"{self.sys}/class/video4linux/* {self.sys}/bus/media/devices/*",
            "/dev/${node##*/}": f"{self.dev}/${{node##*/}}",
            "/etc/bash.bashrc": self.bashrc,
        }, check_left=("/etc/", "/sys/", " /dev/", '"/dev/'))
        fake_bin.stub("setfacl")
        fake_bin.stub("gpasswd")
        # The files in tmp_path belong to the test user; stat answers "root" for the owner
        fake_bin.stub("stat", '[ "$1 $2" = "-c %u" ] && { echo "${OWNER:-0}"; exit; }; '
                      'exec /usr/bin/stat "$@"')
        fake_bin.stub("id", 'echo "${GROUPS_OF_PLINIUS:-plinius wheel}"')

    def call(self, func, **env):
        r = call_scriptlet(self.script, func, self.bin.env(isolated=True, **env))
        assert r.returncode == 0, r.stderr
        return r


@pytest.fixture
def device(tmp_path, fake_bin):
    return DeviceInstall(tmp_path, fake_bin)


def test_install_bashrc_lines_once_and_removed(device):
    device.bashrc.write_text("# system bashrc\n")
    device.call("post_install")
    device.call("post_upgrade")
    text = device.bashrc.read_text()
    assert text.count("/usr/lib/plinius/bash-prompt.sh") == 2  # "[[ -r f ]] && . f" once
    assert text.count("# device-volla-ansuz") == 2
    assert "[[ -r /usr/lib/plinius/bash-aliases.sh ]] && . /usr/lib/plinius/bash-aliases.sh" in text
    device.call("post_remove")
    assert device.bashrc.read_text().strip() == "# system bashrc"


def test_install_without_bashrc(device):
    device.call("post_install")
    device.call("post_remove")
    assert not device.bashrc.exists()


def test_install_sudoers_mode(device):
    device.sudoers.write_text("%wheel ALL=(ALL:ALL) ALL\n")
    device.sudoers.chmod(0o644)
    device.call("post_install")
    assert stat.S_IMODE(device.sudoers.stat().st_mode) == 0o440


def test_install_sudoers_not_touched_when_not_ours(device):
    device.sudoers.write_text("x\n")
    device.sudoers.chmod(0o644)
    device.call("post_install", OWNER="1000")
    assert stat.S_IMODE(device.sudoers.stat().st_mode) == 0o644
    # Never through a link
    target = device.tmp / "elsewhere"
    target.write_text("x\n")
    target.chmod(0o644)
    device.sudoers.unlink()
    device.sudoers.symlink_to(target)
    device.call("post_install")
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_install_applies_firewall_and_clears_camera_acls(device):
    device.call("post_install")
    assert device.bin.calls("mobile-firewall") == [["apply"]]
    # The webcam gadget's node keeps its ACL
    assert device.bin.calls("setfacl") == [["-b", f"{device.dev}/video0"]]


def test_upgrade_takes_plinius_out_of_input(device):
    r = device.call("post_upgrade", GROUPS_OF_PLINIUS="plinius wheel input video")
    assert device.bin.calls("gpasswd") == [["-d", "plinius", "input"]]
    assert "no longer in the group input" in r.stdout
    device.call("post_upgrade", GROUPS_OF_PLINIUS="plinius wheel inputx")
    assert len(device.bin.calls("gpasswd")) == 1


# --- schwupp launcher ---

def test_schwupp_launcher(tmp_path, fake_bin):
    py = fake_bin.stub("python3", 'printf "%s\\n" "$PYTHONPATH"')
    script = rewritten_copy(PKG / "schwupp" / "schwupp.sh", tmp_path / "schwupp",
                            {"/usr/bin/python3": py})
    env = fake_bin.env(isolated=True)
    env.pop("PYTHONPATH", None)
    r = run(["sh", script, "--tv", "a b"], env)
    assert r.stdout == "/usr/lib/schwupp:/usr/lib/schwupp/vendor\n"
    # -P: the current directory is not searched for "app"
    assert fake_bin.calls("python3") == [["-P", "-m", "app", "--tv", "a b"]]
    env["PYTHONPATH"] = "/x"
    r = run(["sh", script], env)
    assert r.stdout == "/usr/lib/schwupp:/usr/lib/schwupp/vendor:/x\n"


def test_all_device_shell_scripts_are_covered():
    # A new script in the package should get a test here (or a reason in this list)
    covered = {"droid-get-bt-address.sh", "keyring-prompter-wait", "plinius-android-initrc",
               "plinius-dev-acl", "plinius-display-nfc", "plinius-drop-setuid", "plinius-hide-apps",
               "plinius-nfc-power", "plinius-polkit-helper", "plinius-udev-reload",
               "plinius-user-sessions-generator", "plinius-wlan", "plinius-prompt.sh",
               "plinius-aliases.sh", "plinius-mobile-firewall", "plinius-hardware-info",
               # Only exports of constant variables
               "plinius-hybris.sh"}
    found = set()
    for f in DEV.iterdir():
        if not f.is_file() or f.is_symlink():
            continue
        head = f.read_bytes()[:64]
        if f.suffix == ".sh" or (head.startswith(b"#!") and b"sh" in head.split(b"\n")[0]):
            found.add(f.name)
    assert found - covered == set()
    assert os.path.exists(DEV / "plinius-hybris.sh")
