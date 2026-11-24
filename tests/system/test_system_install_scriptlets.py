# pacman install scripts (*.install) that edit shared system files (PAM stacks, nsswitch.conf,
# resolv.conf, CUPS configuration) or start and stop units. Sourced from a copy whose paths are
# in tmp_path; systemctl and the other programs are stubs. The scripts of plinius-swap,
# plinius-ssh, plinius-pdf-printer, plinius-apparmor and device-volla-ansuz are tested next to
# their programs.

import os
import stat
from pathlib import Path

import pytest

from conftest import PKG, FakeBin
from system_helpers import call_scriptlet, scriptlet


def call(script, func, fake_bin, *args, **env):
    r = call_scriptlet(script, func, fake_bin.env(isolated=True, **env), *args)
    assert r.returncode == 0, r.stderr
    return r


# --- PAM: plinius-crypt, plinius-security (passwd), plinius-pinguard (sudo, su, su-l) ---

PASSWD = ("#%PAM-1.0\n"
          "#password\trequired\tpam_cracklib.so difok=2 minlen=8 dcredit=2 ocredit=2 retry=3\n"
          "#password\trequired\tpam_unix.so sha512 shadow use_authtok\n"
          "password\trequired\tpam_unix.so sha512 shadow nullok\n")
SUDO = ("#%PAM-1.0\nauth\t\tinclude\t\tsystem-auth\naccount\t\tinclude\t\tsystem-auth\n"
        "session\t\tinclude\t\tsystem-auth\n")
# /etc/pam.d/su and su-l as util-linux ships them
SU = ("#%PAM-1.0\n"
      "auth            sufficient      pam_rootok.so\n"
      "# Uncomment the following line to implicitly trust users in the \"wheel\" group.\n"
      "#auth           sufficient      pam_wheel.so trust use_uid\n"
      "auth            required        pam_unix.so\n"
      "account         required        pam_unix.so\n"
      "session         required        pam_unix.so\n")
CRYPT = "password\trequisite\tpam_plinius_crypt.so"
SECSTATE = "password\toptional\tpam_plinius_secstate.so"
PINGUARD = ("auth\t\t[success=done ignore=ignore authinfo_unavail=ignore module_unknown=ignore "
            "open_err=ignore symbol_err=ignore default=die]\tpam_plinius_pinguard.so")


@pytest.fixture
def pam(tmp_path):
    d = tmp_path / "pam.d"
    d.mkdir()
    (d / "passwd").write_text(PASSWD)
    (d / "sudo").write_text(SUDO)
    (d / "su").write_text(SU)
    (d / "su-l").write_text(SU)
    return d


def pam_scripts(tmp_path, pam):
    crypt = scriptlet("plinius-crypt", tmp_path, {"/etc/pam.d/passwd": pam / "passwd"})
    sec = scriptlet("plinius-security", tmp_path, {
        "/etc/pam.d/passwd": pam / "passwd",
        "d=/var/lib/plinius-security": f"d={tmp_path}/secstate"})
    guard = scriptlet("plinius-pinguard", tmp_path, {
        "/etc/pam.d/sudo": pam / "sudo",
        "/etc/pam.d/su-l": pam / "su-l",  # before su: it is a prefix
        "/etc/pam.d/su": pam / "su",
        "/etc/plinius-pinguard/enabled": tmp_path / "pinguard" / "enabled"})
    return crypt, sec, guard


def test_pam_lines_in_order_once_and_removed(tmp_path, fake_bin, pam):
    crypt, sec, guard = pam_scripts(tmp_path, pam)
    for script in (crypt, sec, guard):
        call(script, "post_install", fake_bin)
        call(script, "post_upgrade", fake_bin)  # a second time: still once
    assert (pam / "passwd").read_text() == (
        "#%PAM-1.0\n"
        "#password\trequired\tpam_cracklib.so difok=2 minlen=8 dcredit=2 ocredit=2 retry=3\n"
        "#password\trequired\tpam_unix.so sha512 shadow use_authtok\n"
        # Before the first module: it reads the new password for the ones after it
        f"{CRYPT}\npassword\trequired\tpam_unix.so sha512 shadow nullok\n"
        # Last: it rates a password the others have set
        f"{SECSTATE}\n")
    assert (pam / "sudo").read_text() == f"#%PAM-1.0\n{PINGUARD}\n" + SUDO.split("\n", 1)[1]
    for name in ("su", "su-l"):
        assert (pam / name).read_text() == SU.replace(
            "pam_rootok.so\n", "pam_rootok.so\n" + SU_ADDED, 1)
    for script in (crypt, sec, guard):
        call(script, "pre_remove", fake_bin)
    assert (pam / "passwd").read_text() == PASSWD
    assert (pam / "sudo").read_text() == SUDO
    assert (pam / "su").read_text() == SU
    assert (pam / "su-l").read_text() == SU


# What pinguard puts right after pam_rootok in su and su-l: root goes on unasked, anyone else
# must be in wheel (refused before any PIN is tried), then the PIN with the shared count
SU_ADDED = ("# plinius-pinguard: only members of wheel may use su, and the PIN is counted "
            "(removed with the package)\n"
            "auth\t\trequisite\tpam_wheel.so use_uid\n"
            f"{PINGUARD}\n")


def test_pam_su_without_rootok_left_alone(tmp_path, fake_bin, pam):
    # Anything before a missing pam_rootok could ask root (services using su) for a password
    without = SU.replace("auth            sufficient      pam_rootok.so\n", "")
    (pam / "su").write_text(without)
    *_, guard = pam_scripts(tmp_path, pam)
    r = call(guard, "post_install", fake_bin)
    assert (pam / "su").read_text() == without
    assert "has no line" in r.stderr and str(pam / "su") in r.stderr
    assert SU_ADDED in (pam / "su-l").read_text()


def test_pam_su_rootok_with_arguments_and_first_one_only(tmp_path, fake_bin, pam):
    text = SU.replace("pam_rootok.so\n", "pam_rootok.so debug\n") + (
        "auth            sufficient      pam_rootok.so\n")
    (pam / "su").write_text(text)
    *_, guard = pam_scripts(tmp_path, pam)
    call(guard, "post_install", fake_bin)
    out = (pam / "su").read_text()
    assert out == text.replace("pam_rootok.so debug\n", "pam_rootok.so debug\n" + SU_ADDED, 1)
    assert out.count("pam_plinius_pinguard.so") == 1


def test_pam_su_remove_keeps_a_wheel_line_of_the_admin(tmp_path, fake_bin, pam):
    # Only the exact line pinguard adds goes; one with other arguments is the administrator's
    own = SU + "auth            required        pam_wheel.so use_uid group=admins\n"
    (pam / "su").write_text(own)
    *_, guard = pam_scripts(tmp_path, pam)
    call(guard, "post_install", fake_bin)
    call(guard, "pre_remove", fake_bin)
    assert (pam / "su").read_text() == own


def test_pinguard_wait_is_on_after_a_fresh_install(tmp_path, fake_bin, pam):
    *_, guard = pam_scripts(tmp_path, pam)
    marker = tmp_path / "pinguard" / "enabled"
    call(guard, "post_install", fake_bin)
    assert marker.is_file()
    # A second install (pacman -S again) leaves the marker as it is
    call(guard, "post_install", fake_bin)
    assert marker.is_file()


def test_pinguard_update_never_switches_the_wait_on(tmp_path, fake_bin, pam):
    # Switched off on the phone (plinius-pinguard-set off removed the marker): an update keeps it off
    *_, guard = pam_scripts(tmp_path, pam)
    call(guard, "post_upgrade", fake_bin)
    assert not (tmp_path / "pinguard" / "enabled").exists()


def test_pam_without_files(tmp_path, fake_bin, pam):
    (pam / "passwd").unlink()
    for name in ("sudo", "su", "su-l"):
        (pam / name).unlink()
    for script in pam_scripts(tmp_path, pam):
        call(script, "post_install", fake_bin)
        call(script, "pre_remove", fake_bin)
    assert list(pam.iterdir()) == []


def test_pam_stack_without_module_lines(tmp_path, fake_bin, pam):
    (pam / "passwd").write_text("#%PAM-1.0\n")
    (pam / "sudo").write_text("#%PAM-1.0\n")
    crypt, _, guard = pam_scripts(tmp_path, pam)
    call(crypt, "post_install", fake_bin)
    call(guard, "post_install", fake_bin)
    assert (pam / "passwd").read_text() == f"#%PAM-1.0\n{CRYPT}\n"
    # sudo without an auth line is left alone: nothing to go in front of
    assert (pam / "sudo").read_text() == "#%PAM-1.0\n"


def test_security_rating_modes_on_upgrade(tmp_path, fake_bin, pam):
    _, sec, _ = pam_scripts(tmp_path, pam)
    lock = tmp_path / "secstate" / "lock"
    lock.mkdir(parents=True)
    os.chmod(lock.parent, 0o700)
    os.chmod(lock, 0o700)
    (lock / "anna").write_text("weak")
    os.chmod(lock / "anna", 0o600)
    (lock / "ghost").write_text("x")  # no such user: left as it is
    os.chmod(lock / "ghost", 0o600)
    outside = tmp_path / "outside"
    outside.write_text("x")
    os.chmod(outside, 0o600)
    (lock / "link").symlink_to(outside)
    fake_bin.stub("id", '[ "$2" = anna ] && { echo anna; exit 0; }; [ "$2" = link ] && '
                  '{ echo link; exit 0; }; exit 1')
    fake_bin.stub("chown")
    call(sec, "post_upgrade", fake_bin)
    assert stat.S_IMODE(lock.stat().st_mode) == 0o755
    assert stat.S_IMODE(lock.parent.stat().st_mode) == 0o755
    assert stat.S_IMODE((lock / "anna").stat().st_mode) == 0o640
    assert stat.S_IMODE((lock / "ghost").stat().st_mode) == 0o600
    # Never through a link
    assert stat.S_IMODE(outside.stat().st_mode) == 0o600
    assert fake_bin.calls("chown") == [["root:anna", str(lock / "anna")]]


# --- cups-plinius: nsswitch.conf and the configuration saved by pacman ---

ARCH_HOSTS = "hosts: mymachines resolve [!UNAVAIL=return] files myhostname dns\n"


def cups(tmp_path, fake_bin, nsswitch=None, booted=False):
    etc = tmp_path / "etc"
    (etc / "cups").mkdir(parents=True)
    if nsswitch is not None:
        (etc / "nsswitch.conf").write_text(nsswitch)
    run_dir = tmp_path / "run-systemd"
    if booted:
        run_dir.mkdir()
    fake_bin.stub("chown")
    fake_bin.stub("systemctl")
    script = scriptlet("cups-plinius", tmp_path, {
        "/etc/nsswitch.conf": etc / "nsswitch.conf",
        "f=/etc/cups/$f": f"f={etc}/cups/$f",
        "/run/systemd/system": run_dir,
    }, check_left=("=/etc", "/run/"))
    return script, etc


@pytest.mark.parametrize("before,after", [
    (ARCH_HOSTS,
     "hosts: mymachines mdns_minimal [NOTFOUND=return] resolve [!UNAVAIL=return] files myhostname dns\n"),
    ("hosts: files dns\n", "hosts: files mdns_minimal [NOTFOUND=return] dns\n"),
    # Already there, also as mdns4_minimal or added by hand: unchanged
    ("hosts: files mdns4_minimal [NOTFOUND=return] dns\n",
     "hosts: files mdns4_minimal [NOTFOUND=return] dns\n"),
    ("hosts: mymachines mdns_minimal [NOTFOUND=return] resolve files\n",
     "hosts: mymachines mdns_minimal [NOTFOUND=return] resolve files\n"),
])
def test_cups_mdns_in_nsswitch(tmp_path, fake_bin, before, after):
    text = "passwd: files systemd\n" + before + "networks: files\n"
    script, etc = cups(tmp_path, fake_bin, text)
    call(script, "post_upgrade", fake_bin)
    call(script, "post_upgrade", fake_bin)
    assert (etc / "nsswitch.conf").read_text() == "passwd: files systemd\n" + after + \
        "networks: files\n"


def test_cups_mdns_after_files_at_the_end_of_the_line(tmp_path, fake_bin):
    script, etc = cups(tmp_path, fake_bin, "hosts:\tfiles\n")
    call(script, "post_upgrade", fake_bin)
    assert (etc / "nsswitch.conf").read_text().split() == [
        "hosts:", "files", "mdns_minimal", "[NOTFOUND=return]"]


def test_cups_without_nsswitch(tmp_path, fake_bin):
    script, etc = cups(tmp_path, fake_bin)
    call(script, "post_install", fake_bin)
    assert not (etc / "nsswitch.conf").exists()


def test_cups_keeps_the_saved_configuration(tmp_path, fake_bin):
    script, etc = cups(tmp_path, fake_bin, ARCH_HOSTS)
    c = etc / "cups"
    # printers.conf: new file empty -> the saved one comes back
    (c / "printers.conf").write_text("")
    (c / "printers.conf.pacsave").write_text("<Printer PDF>\n")
    # cupsd.conf: new file is the default -> the saved one comes back
    (c / "cupsd.conf.default").write_text("default\n")
    (c / "cupsd.conf").write_text("default\n")
    (c / "cupsd.conf.pacsave").write_text("mine\n")
    # classes.conf: changed since -> stays, the .pacsave too
    (c / "classes.conf").write_text("new class\n")
    (c / "classes.conf.pacsave").write_text("old class\n")
    # snmp.conf: empty .pacsave -> nothing
    (c / "snmp.conf").write_text("")
    (c / "snmp.conf.pacsave").write_text("")
    r = call(script, "post_install", fake_bin)
    assert (c / "printers.conf").read_text() == "<Printer PDF>\n"
    assert (c / "cupsd.conf").read_text() == "mine\n"
    assert not (c / "printers.conf.pacsave").exists() and not (c / "cupsd.conf.pacsave").exists()
    assert (c / "classes.conf").read_text() == "new class\n"
    assert (c / "classes.conf.pacsave").exists() and (c / "snmp.conf.pacsave").exists()
    assert fake_bin.calls("chown") == [["root:cups", str(c / "printers.conf")],
                                       ["root:cups", str(c / "cupsd.conf")]]
    assert r.stdout.count("kept the configuration of cups") == 2
    assert fake_bin.calls("systemctl") == []  # not booted


def test_cups_old_pacsave_stays(tmp_path, fake_bin):
    script, etc = cups(tmp_path, fake_bin, ARCH_HOSTS, booted=True)
    c = etc / "cups"
    (c / "printers.conf.pacsave").write_text("<Printer old>\n")
    # find -cmin -30 finds nothing: the .pacsave is older than this transaction
    fake_bin.stub("find")
    call(script, "post_install", fake_bin)
    assert not (c / "printers.conf").exists()
    assert fake_bin.calls("systemctl") == [["try-restart", "cups.service"]]


# --- plinius-dnsfilter: /etc/resolv.conf ---

def dnsfilter(tmp_path, fake_bin):
    resolv = tmp_path / "resolv.conf"
    state = tmp_path / "dnsfilter-state"
    state.mkdir()
    (state / "log").write_text("x")
    os.chmod(state / "log", 0o644)
    fake_bin.stub("systemd-tmpfiles")
    fake_bin.stub("systemctl", 'case "$*" in *is-active*) exit "${ACTIVE:-3}" ;; esac')
    tool = fake_bin.stub("plinius-dnsfilter")
    script = scriptlet("plinius-dnsfilter", tmp_path, {
        "/etc/resolv.conf": resolv,
        "/var/lib/plinius-dnsfilter": state,
        "/usr/bin/plinius-dnsfilter": tool,
    }, check_left=("/etc/", "/var/"))
    return script, resolv, state


def test_dnsfilter_links_resolv_conf(tmp_path, fake_bin):
    script, resolv, state = dnsfilter(tmp_path, fake_bin)
    resolv.symlink_to("/run/NetworkManager/resolv.conf")
    call(script, "post_install", fake_bin)
    assert os.readlink(resolv) == "/run/plinius-dns/resolv.conf"
    call(script, "post_upgrade", fake_bin)
    assert os.readlink(resolv) == "/run/plinius-dns/resolv.conf"
    # The query log is no longer world-readable
    assert stat.S_IMODE((state / "log").stat().st_mode) == 0o600
    assert not [c for c in fake_bin.calls("systemctl") if "try-restart" in c]
    call(script, "pre_remove", fake_bin)
    assert os.readlink(resolv) == "/run/NetworkManager/resolv.conf"
    assert fake_bin.calls("plinius-dnsfilter") == [["--cleanup"]]


def test_dnsfilter_replaces_a_plain_file(tmp_path, fake_bin):
    script, resolv, _ = dnsfilter(tmp_path, fake_bin)
    resolv.write_text("nameserver 8.8.8.8\n")
    call(script, "post_install", fake_bin)
    assert os.readlink(resolv) == "/run/plinius-dns/resolv.conf"


def test_dnsfilter_restarts_a_running_filter(tmp_path, fake_bin):
    script, _, _ = dnsfilter(tmp_path, fake_bin)
    call(script, "post_upgrade", fake_bin, ACTIVE=0)
    assert fake_bin.calls("systemctl")[-2:] == [["daemon-reload"],
                                                ["try-restart", "plinius-dnsfilter.service"]]



def dnsfilter_settings(state):
    return (state / "settings").read_text()


def test_dnsfilter_is_on_after_a_fresh_install(tmp_path, fake_bin):
    script, _, state = dnsfilter(tmp_path, fake_bin)
    call(script, "post_install", fake_bin)
    text = dnsfilter_settings(state)
    assert "[filter]\nenabled=true\n" in text
    # Private like everything the filter writes there
    assert stat.S_IMODE((state / "settings").stat().st_mode) == 0o600
    assert stat.S_IMODE(state.stat().st_mode) == 0o700


def test_dnsfilter_install_keeps_existing_settings(tmp_path, fake_bin):
    # Reinstalled over settings of its own (switched off before): never switched back on
    script, _, state = dnsfilter(tmp_path, fake_bin)
    (state / "settings").write_text("[filter]\nenabled=false\n")
    call(script, "post_install", fake_bin)
    assert dnsfilter_settings(state) == "[filter]\nenabled=false\n"


def test_dnsfilter_update_never_switches_it_on(tmp_path, fake_bin):
    # A phone that had the package before keeps the filter as it was, also without settings
    script, _, state = dnsfilter(tmp_path, fake_bin)
    call(script, "post_upgrade", fake_bin)
    assert not (state / "settings").exists()


def test_dnsfilter_creates_a_missing_state_directory(tmp_path, fake_bin):
    script, _, state = dnsfilter(tmp_path, fake_bin)
    (state / "log").unlink()
    state.rmdir()
    call(script, "post_install", fake_bin)
    assert "enabled=true" in dnsfilter_settings(state)
    assert stat.S_IMODE(state.stat().st_mode) == 0o700

# --- plinius-desktop: firewall chain on removal ---

def test_desktop_install_and_remove(tmp_path, fake_bin):
    marker = tmp_path / "plinius-desktop-firewall"
    marker.touch()
    tool = fake_bin.stub("plinius-desktop-set", "exit 1")
    script = scriptlet("plinius-desktop", tmp_path, {
        "/usr/lib/plinius-desktop/plinius-desktop-set": tool,
        "/run/plinius-desktop-firewall": marker,
    }, check_left=("/run/", "/usr/"))
    fake_bin.stub("iptables-legacy", "exit 1")
    fake_bin.stub("ip6tables-legacy", "exit 1")
    call(script, "post_upgrade", fake_bin)
    assert fake_bin.calls("plinius-desktop-set") == [["apply"]]
    call(script, "pre_remove", fake_bin)
    for t in ("iptables-legacy", "ip6tables-legacy"):
        assert fake_bin.calls(t) == [
            ["-w", "-D", "INPUT", "-p", "tcp", "--dport", "5900", "-j", "plinius-desktop"],
            ["-w", "-F", "plinius-desktop"], ["-w", "-X", "plinius-desktop"]]
    assert not marker.exists()


# --- plinius-keyring: pacman-key relative to the root pacman runs the script in ---

@pytest.mark.parametrize("initialized", [True, False])
def test_keyring_populates_only_an_initialized_keyring(tmp_path, initialized):
    keys = FakeBin(tmp_path / "usr" / "bin")
    keys.stub("pacman-key", f'[ "$1" = -l ] && exit {0 if initialized else 1}; exit 0')
    script = scriptlet("plinius-keyring", tmp_path, {})
    r = call_scriptlet(script, "post_install", keys.env(isolated=True), cwd=tmp_path)
    assert r.returncode == 0
    expected = [["-l"], ["--populate", "plinius"]] if initialized else [["-l"]]
    assert keys.calls("pacman-key") == expected


def test_keyring_without_pacman_key(tmp_path, fake_bin):
    script = scriptlet("plinius-keyring", tmp_path, {})
    r = call_scriptlet(script, "post_install", fake_bin.env(isolated=True), cwd=tmp_path)
    assert r.returncode == 0


# --- plinius-widevine: migration of the switch ---

class Widevine:
    def __init__(self, tmp_path, fake_bin):
        self.bin = fake_bin
        self.var = tmp_path / "var-widevine"
        self.etc = tmp_path / "etc-widevine"
        self.run_dir = tmp_path / "run-systemd"
        fake_bin.stub("systemd-sysusers")
        fake_bin.stub("systemctl")
        # vercmp A B: -1, 0 or 1 like pacman's
        fake_bin.stub("vercmp", '[ "$1" = "$2" ] && { echo 0; exit; }; '
                      'first=$(printf "%s\\n%s\\n" "$1" "$2" | sort -V | head -n1); '
                      '[ "$first" = "$1" ] && echo -1 || echo 1')
        self.script = scriptlet("plinius-widevine", tmp_path, {
            "/var/lib/plinius-widevine": self.var,
            "/etc/plinius-widevine": self.etc,
            "/run/systemd/system": self.run_dir,
        }, check_left=("/var/", "/etc/", "/run/"))

    def call(self, func, *args):
        return call(self.script, func, self.bin, *args)


@pytest.fixture
def widevine(tmp_path, fake_bin):
    return Widevine(tmp_path, fake_bin)


def test_widevine_upgrade_from_01_keeps_a_fetched_cdm_on(widevine):
    widevine.var.mkdir()
    (widevine.var / "current").touch()
    widevine.call("post_upgrade", "0.3-1", "0.1-2")
    assert (widevine.etc / "enabled").exists()


@pytest.mark.parametrize("old,cdm", [("0.1-2", False), ("0.2-1", True), ("0.3-1", True)])
def test_widevine_upgrade_does_not_switch_on_by_itself(widevine, old, cdm):
    if cdm:
        widevine.var.mkdir()
        (widevine.var / "current").touch()
    widevine.call("post_upgrade", "0.3-2", old)
    assert not (widevine.etc / "enabled").exists()


def test_widevine_upgrade_to_03_fetches_now_when_switched_on(widevine):
    widevine.run_dir.mkdir()
    widevine.etc.mkdir()
    (widevine.etc / "enabled").touch()
    widevine.call("post_upgrade", "0.3-1", "0.2-3")
    assert ["start", "--no-block", "plinius-widevine-update.service"] in \
        widevine.bin.calls("systemctl")
    n = len(widevine.bin.calls("systemctl"))
    widevine.call("post_upgrade", "0.3-2", "0.3-1")
    assert ["start", "--no-block", "plinius-widevine-update.service"] not in \
        widevine.bin.calls("systemctl")[n:]


def test_widevine_remove_deletes_cdm_and_switch(widevine):
    widevine.var.mkdir()
    (widevine.var / "current").touch()
    widevine.etc.mkdir()
    (widevine.etc / "enabled").touch()
    widevine.call("pre_remove")
    widevine.call("post_remove")
    assert not widevine.var.exists() and not widevine.etc.exists()
    assert widevine.bin.calls("systemctl") == []  # not booted


# --- plinius-vdec: the MDP nodes for the new group ---

@pytest.mark.parametrize("group", [True, False])
def test_vdec_gives_the_nodes_to_its_group(tmp_path, fake_bin, group):
    dev = tmp_path / "dev"
    dev.mkdir()
    # Links to /dev/null pass the -c test; chgrp and chmod are stubs, nothing reaches it
    (dev / "mtk_mdp").symlink_to("/dev/null")
    script = scriptlet("plinius-vdec", tmp_path, {
        "/dev/mtk_mdp /dev/mdp_sync": f"{dev}/mtk_mdp {dev}/mdp_sync",
    }, check_left=("dev in /dev",))
    for tool in ("systemd-sysusers", "chgrp", "chmod", "systemctl"):
        fake_bin.stub(tool)
    fake_bin.stub("getent", f"exit {0 if group else 2}")
    call(script, "post_install", fake_bin)
    if group:
        assert fake_bin.calls("chgrp") == [["plinius-vdec", str(dev / "mtk_mdp")]]
        assert fake_bin.calls("chmod") == [["0660", str(dev / "mtk_mdp")]]
    else:
        assert fake_bin.calls("chgrp") == [] and fake_bin.calls("chmod") == []
    assert fake_bin.calls("systemctl")[-1] == ["start", "plinius-vdec-mdpd.socket"]


# --- mmsd4ofono, hybris-tls-relocate ---

@pytest.mark.parametrize("active", [True, False])
def test_mmsd_routectld_socket(tmp_path, fake_bin, active):
    fake_bin.stub("systemctl", f'case "$*" in *is-active*) exit {0 if active else 3} ;; esac')
    script = scriptlet("mmsd4ofono", tmp_path, {})
    call(script, "post_upgrade", fake_bin)
    calls = fake_bin.calls("systemctl")
    # A routectld started by its socket may be routing an MMS: only an old daemon is stopped
    assert (["stop", "routectld.service"] in calls) == (not active)
    assert calls[-1] == ["start", "routectld.socket"]
    call(script, "pre_remove", fake_bin)
    assert fake_bin.calls("systemctl")[-1] == ["stop", "routectld.socket", "routectld.service"]


def test_hybris_tls_relocate_patches_installed_browsers(tmp_path, fake_bin):
    firefox = tmp_path / "firefox"
    firefox.write_text("")
    tool = fake_bin.stub("hybris-tls-relocate")
    script = scriptlet("hybris-tls-relocate", tmp_path, {
        "/usr/lib/firefox/firefox": firefox,
        "/usr/lib/thunderbird/thunderbird": tmp_path / "thunderbird",
        "/usr/bin/hybris-tls-relocate": tool,
    }, check_left=("/usr/",))
    call(script, "post_upgrade", fake_bin)
    assert fake_bin.calls("hybris-tls-relocate") == [[str(firefox)]]


# --- plinius-account: groups of second accounts on the upgrade to 0.6-2 ---

PASSWD_DB = ("root:x:0:0::/root:/bin/bash\n"
             "plinius:x:32011:32011::/home/plinius:/bin/bash\n"   # the phone's first user
             "anna:x:32012:32012::/home/anna:/bin/bash\n"         # second account
             "admin2:x:32013:32013::/home/admin2:/bin/bash\n"     # second account in wheel
             "last:x:32999:32999::/home/last:/bin/bash\n"         # highest local UID
             "other:x:33000:33000::/home/other:/bin/bash\n")      # not from plinius-add-user
GROUPS = {"plinius": "plinius wheel video audio radio", "anna": "anna video audio radio users",
          "admin2": "admin2 wheel video audio", "last": "last audio", "other": "other video",
          "root": "root"}


def account(tmp_path, fake_bin):
    fake_bin.stub("getent", f'[ "$1" = passwd ] && printf %s "{PASSWD_DB}"')
    cases = "\n".join(f'{n}) echo "{g}";;' for n, g in GROUPS.items())
    fake_bin.stub("id", f'[ "$1" = -nG ] && [ "$2" = -- ] || exit 2\ncase $3 in\n{cases}\n'
                        '*) exit 1;;\nesac')
    fake_bin.stub("gpasswd")
    fake_bin.stub("vercmp", 'python3 -c "import sys; a, b = sys.argv[1:]; '
                  'k = lambda v: [int(x) for x in v.replace(\'-\', \'.\').split(\'.\')]; '
                  'print((k(a) > k(b)) - (k(a) < k(b)))" "$1" "$2"')
    return scriptlet("plinius-account", tmp_path, {})


@pytest.mark.parametrize("old", ["0.5-3", "0.6-1"])
def test_account_upgrade_drops_device_groups_of_second_accounts(tmp_path, fake_bin, old):
    script = account(tmp_path, fake_bin)
    r = call(script, "post_upgrade", fake_bin, "0.6-2", old)
    # Only the second accounts outside wheel, only the groups they are in
    assert fake_bin.calls("gpasswd") == [["-d", "anna", "video"], ["-d", "anna", "audio"],
                                         ["-d", "anna", "radio"], ["-d", "last", "audio"]]
    assert "anna is no longer in video" in r.stdout


@pytest.mark.parametrize("old", ["0.6-2", "0.6-3", "0.7-1"])
def test_account_later_upgrades_leave_groups_alone(tmp_path, fake_bin, old):
    # Once only: groups an administrator gives back later stay
    script = account(tmp_path, fake_bin)
    call(script, "post_upgrade", fake_bin, "0.7-2", old)
    assert fake_bin.calls("gpasswd") == []


def test_account_failed_gpasswd_is_not_reported_as_done(tmp_path, fake_bin):
    script = account(tmp_path, fake_bin)
    fake_bin.stub("gpasswd", "exit 3")
    r = call(script, "post_upgrade", fake_bin, "0.6-2", "0.6-1")
    assert "no longer" not in r.stdout
    assert len(fake_bin.calls("gpasswd")) == 4  # one failure does not stop the others


# --- plinius-games, plinius-dos-installer: emulator launchers hidden, back on removal ---

LAUNCHERS = {
    "plinius-games": ["com.libretro.RetroArch.desktop"],
    "plinius-dos-installer": ["org.scummvm.scummvm.desktop", "dosbox.desktop"],
}


def launcher_script(pkg, tmp_path, fake_bin):
    hide = fake_bin.stub("plinius-hide-apps", "cat > \"$0.stdin\"")
    script = scriptlet(pkg, tmp_path, {
        "/usr/share/libalpm/scripts/plinius-hide-apps": hide,
        '"/$f"': f'"{tmp_path}/root/$f"',
    }, check_left=('"/$f"', " /usr/"))
    return script, hide


@pytest.mark.parametrize("pkg", LAUNCHERS)
def test_launchers_hidden_on_install_and_upgrade(tmp_path, fake_bin, pkg):
    script, hide = launcher_script(pkg, tmp_path, fake_bin)
    for func in ("post_install", "post_upgrade"):
        call(script, func, fake_bin)
        assert Path(f"{hide}.stdin").read_text().split() == [
            f"usr/share/applications/{name}" for name in LAUNCHERS[pkg]]
    assert len(fake_bin.calls("plinius-hide-apps")) == 2


@pytest.mark.parametrize("pkg", LAUNCHERS)
def test_launchers_shown_again_on_removal(tmp_path, fake_bin, pkg):
    script, _ = launcher_script(pkg, tmp_path, fake_bin)
    apps = tmp_path / "root/usr/share/applications"
    apps.mkdir(parents=True)
    first, *rest = LAUNCHERS[pkg]
    entry = "[Desktop Entry]\nName=X\nNoDisplay=true\nExec=x\n"
    (apps / first).write_text(entry)
    # The others are missing (their app was removed first): no error
    call(script, "post_remove", fake_bin)
    assert (apps / first).read_text() == "[Desktop Entry]\nName=X\nExec=x\n"
    assert [p.name for p in apps.iterdir()] == [first]


# --- plinius-epo: first download right away, EPO cleared on removal ---

def test_epo_units(tmp_path, fake_bin):
    tool = fake_bin.stub("plinius-epo")
    script = scriptlet("plinius-epo", tmp_path, {"/usr/lib/plinius-epo/plinius-epo": tool})
    fake_bin.stub("systemctl")
    call(script, "post_install", fake_bin)
    assert fake_bin.calls("systemctl") == [
        ["daemon-reload"], ["start", "--no-block", "plinius-epo.timer", "plinius-epo.service"]]
    call(script, "post_upgrade", fake_bin)
    assert fake_bin.calls("systemctl")[2:] == [["daemon-reload"]]
    call(script, "pre_remove", fake_bin)
    assert fake_bin.calls("systemctl")[3:] == [["stop", "plinius-epo.timer"]]
    assert fake_bin.calls("plinius-epo") == [["clear"]]


def test_epo_image_build_without_systemd(tmp_path, fake_bin):
    # In the image build systemctl fails and the tool may be missing: the scriptlet never does
    script = scriptlet("plinius-epo", tmp_path, {
        "/usr/lib/plinius-epo/plinius-epo": tmp_path / "missing"})
    fake_bin.stub("systemctl", "exit 1")
    for func in ("post_install", "post_upgrade", "pre_remove"):
        call(script, func, fake_bin)


def test_every_install_script_is_tested():
    tested = {"cups-plinius", "device-volla-ansuz", "hybris-tls-relocate", "mmsd4ofono",
              "plinius-apparmor", "plinius-crypt", "plinius-desktop", "plinius-dnsfilter",
              "plinius-keyring", "plinius-pdf-printer", "plinius-pinguard", "plinius-security",
              "plinius-ssh", "plinius-swap", "plinius-vdec", "plinius-widevine",
              "plinius-account", "plinius-games", "plinius-dos-installer", "plinius-epo"}
    found = {p.stem for p in PKG.glob("*/*.install")}
    assert found - tested == set()
