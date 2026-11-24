# Device Security (cc-plinius-security.c): the checks of the panel's page from the files the
# system keeps (all of them in the program's root in tmp), the connections from a fake
# plinius-netlogd (org.plinius.NetLog1, interface from its C source), the known vulnerabilities
# from cve.json, the boot chain alarm with "Accept Changes" through a fake pkexec, and the
# access control page.

import getpass
import json
import time

import pytest

from conftest import PKG
from gcc_harness import (build_panel, click, dump, gcc_session, gcc_world, interface_xml,  # noqa: F401
                         push, select, set_active, settle)

NETLOG = "org.plinius.NetLog1"
NETLOG_PATH = "/org/plinius/NetLog1"
NETLOG_XML = interface_xml(PKG / "plinius-netlog" / "plinius-netlogd.c", NETLOG)
PORTAL = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
PORTAL_XML = """<node><interface name="org.freedesktop.portal.OpenURI">
 <method name="OpenURI"><arg type="s" name="parent_window" direction="in"/>
  <arg type="s" name="uri" direction="in"/><arg type="a{sv}" name="options" direction="in"/>
  <arg type="o" name="handle" direction="out"/></method>
 <property name="version" type="u" access="read"/>
</interface></node>"""

PATHS = ["/var/lib/plinius-security/lock", "/usr/bin/plinius-encrypt",
         "/var/lib/plinius-crypt/strength", "/run/plinius-bootcheck", "/usr/bin/plinius-bootcheck",
         "/usr/lib/security/pam_plinius_pinguard.so", "/etc/plinius-pinguard/enabled",
         "/var/lib/plinius-cve/cve.json", "/sys/kernel/security/lsm", "/sys/fs/selinux/enforce",
         "/sys/kernel/security/apparmor/profiles",
         "/sys/kernel/security/apparmor/policy/profiles",
         "/usr/share/plinius-apparmor/expected", "/etc/pam.d/phosh", "/etc/pacman.conf"]

GLUE = r'''
#include "gcc_driver.h"
#include "cc-plinius-security.h"

/* The pages of cc-security-panel.c the checks lead to, as stand-ins */
static void
add_stand_in (GccTest *t, const char *tag, const char *title)
{
    GtkWidget *label = gtk_label_new (title);

    cc_panel_add_subpage (t->panel, tag, adw_navigation_page_new_with_tag (label, title, tag));
}

void
gcc_test_setup (GccTest *t)
{
    add_stand_in (t, "lock", "Lock & Unlock");
    add_stand_in (t, "encryption", "Encryption Page");
    cc_plinius_security_setup_panel (t->panel);
}
'''


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    reps = [(f'"{p}', f'"{{root}}{p}') for p in PATHS]
    return build_panel(gcc_session, tmp_path_factory.mktemp("security"), "security",
                       ["cc-plinius-security.c"], GLUE,
                       replacements={"cc-plinius-security.c": reps})


class System:
    """The files the checks read, in the program's root."""

    def __init__(self, root):
        self.root = root

    def write(self, path, text="", mode=0o644):
        f = self.root / path.lstrip("/")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        f.chmod(mode)
        return f

    def remove(self, path):
        f = self.root / path.lstrip("/")
        if f.is_dir():
            import shutil
            shutil.rmtree(f)
        elif f.exists() or f.is_symlink():
            f.unlink()

    def clear(self):
        import shutil
        for d in list(self.root.iterdir()):
            shutil.rmtree(d) if d.is_dir() else d.unlink()

    def good(self):
        """Everything rated good."""
        self.clear()
        self.write(f"/var/lib/plinius-security/lock/{getpass.getuser()}", "level=good\n")
        self.write("/usr/lib/security/pam_plinius_pinguard.so")
        self.write("/etc/pam.d/phosh", "auth required pam_plinius_pinguard.so\n")
        self.write("/etc/plinius-pinguard/enabled")
        self.encryption("encrypted", "level=good\n")
        self.write("/usr/bin/plinius-bootcheck", "#!/bin/sh\n", 0o755)
        self.write("/run/plinius-bootcheck/done")
        self.write("/etc/pacman.conf", "[options]\nSigLevel = Required DatabaseOptional\n")
        self.write("/sys/kernel/security/lsm", "capability,landlock,apparmor\n")
        self.write("/sys/kernel/security/apparmor/profiles", "plinius.cupsd (enforce)\nplinius.ofono (enforce)\n")
        self.write("/usr/share/plinius-apparmor/expected", "plinius.cupsd\nplinius.ofono\n")
        self.write("/var/lib/plinius-cve/cve.json", json.dumps({"checked": 1767323040, "issues": []}))

    def encryption(self, state, strength=None):
        self.write("/usr/bin/plinius-encrypt", f"#!/bin/sh\n[ \"$1\" = --status ] && echo {state}\n", 0o755)
        if strength is None:
            self.remove("/var/lib/plinius-crypt/strength")
        else:
            self.write("/var/lib/plinius-crypt/strength", strength)


@pytest.fixture
def system(program):
    s = System(program.root)
    s.good()
    return s


def netlog(world, summary="(uint32 23, uint32 9, uint32 4, uint32 2)", query=None, own=True):
    replies = {"Summary": summary}
    if query is not None:
        replies["Query"] = query
    world.service("system", NETLOG, NETLOG_PATH, NETLOG_XML, replies=replies, own=own)


def checks(screen):
    group = screen.within(type="AdwPreferencesGroup", text="Checks")
    out = {}
    for row in group.find(type="AdwActionRow"):
        icons = [c for c in row.get("children", []) if c["type"] == "GtkImage"]
        out[row["text"]] = (row["subtitle"], icons[0]["icon"] if icons else None)
    return out


def overall(screen):
    row = screen.find(type="AdwActionRow")[0]
    return row["text"], row["subtitle"]


GOOD = "object-select-symbolic"
WARN = "dialog-warning-symbolic"
BAD = "dialog-error-symbolic"
INFO = "dialog-information-symbolic"


def test_all_good_is_protected(program, gcc_world, system):
    netlog(gcc_world)
    r = gcc_world.run(program, [settle(), dump("main")])
    s = r.dump("main")
    assert overall(s) == ("Protected", "All checks below are fine.")
    assert checks(s) == {
        "Screen Lock": ("PIN of at least 6 digits.", GOOD),
        "Encryption": ("Encrypted with a strong password.", GOOD),
        "Boot Chain": ("Bootloader, kernel and ramdisk are unchanged since the last accepted state.", GOOD),
        "Software Sources": ("Updates are only installed with a valid signature.", GOOD),
        "Access Control": ("AppArmor confines 2 services of the network and the radio.", GOOD),
        # Information only, it does not count
        "Bootloader": ("Unlocked, so that this system can start. Whoever has the phone in hand can "
                       "start a different system; the encryption keeps the data safe, the boot chain "
                       "check notices changes.", INFO),
    }
    assert s.one(type="AdwActionRow", text="Known Vulnerabilities")["subtitle"] == (
        "No known vulnerabilities in the installed packages.")
    assert s.one(type="AdwActionRow", text="Connections")["subtitle"] == (
        "Last hour, internet: 23 connections · 9 destinations · 4 apps")
    # The last hour (asked when the page is made and again when it shows)
    assert {tuple(c["args"]) for c in gcc_world.calls(NETLOG, "Summary")} == {(3600,)}
    assert not r.warnings()


@pytest.mark.parametrize("files, subtitle, icon", [
    ({"pattern": True}, "Unlock pattern of at least 6 points.", GOOD),
    ({"level": None}, "The PIN is not rated yet. Set it again to rate it.", WARN),
    ({"level": "weak"}, "The PIN is shorter than 6 digits. Choose a longer one.", BAD),
    ({"level": "good", "pinguard": False},
     "PIN of at least 6 digits. Wrong tries do not make the lock screen wait, a short PIN can be "
     "guessed by trying.", WARN),
    ({"level": "weak", "pinguard": False},
     "The PIN is shorter than 6 digits. Choose a longer one. Wrong tries do not make the lock "
     "screen wait, a short PIN can be guessed by trying.", BAD),
    ({"level": "good", "phosh": "auth required pam_unix.so\n"},
     "PIN of at least 6 digits. Wrong tries do not make the lock screen wait, a short PIN can be "
     "guessed by trying.", WARN),
    ({"level": "good", "enabled": False},
     "PIN of at least 6 digits. Wrong tries do not make the lock screen wait, a short PIN can be "
     "guessed by trying.", WARN),
])
def test_screen_lock(program, gcc_world, system, files, subtitle, icon):
    lock = f"/var/lib/plinius-security/lock/{getpass.getuser()}"
    if files.get("pattern"):
        (gcc_world.home / ".config/plinius").mkdir(parents=True)
        (gcc_world.home / ".config/plinius/unlock-pattern").write_text("x")
    if "level" in files:
        if files["level"] is None:
            system.remove(lock)
        else:
            system.write(lock, f"digits=6\nlevel={files['level']}\n")
    if files.get("pinguard") is False:
        system.remove("/usr/lib/security/pam_plinius_pinguard.so")
    if "phosh" in files:
        system.write("/etc/pam.d/phosh", files["phosh"])
    if files.get("enabled") is False:
        system.remove("/etc/plinius-pinguard/enabled")
    netlog(gcc_world)
    r = gcc_world.run(program, [settle(), dump("main")])
    assert checks(r.dump("main"))["Screen Lock"] == (subtitle, icon)


@pytest.mark.parametrize("state, strength, subtitle, icon", [
    (None, None, "Not available: plinius-crypt is not installed. Whoever has the phone in hand can "
     "read its data.", BAD),
    ("off", None, "Not encrypted. Whoever has the phone in hand can read its data.", BAD),
    ("requested", None, "Encryption at the next restart. Until then the data can be read.", WARN),
    ("unsupported", None, "Not encrypted, and this system cannot be encrypted: it does not start "
     "with the encryption support.", BAD),
    ("encrypted", None, "Encrypted. The password is not rated yet. Change it once to rate it.", WARN),
    ("encrypted", "level=fair\n", "Encrypted, but the password could be found with enough "
     "computing power. Choose 12 or more mixed characters or 5 random words.", WARN),
    ("encrypted", "level=weak\ndictionary=word\n", "Encrypted, but the password is a dictionary "
     "word or close to one. Change the password.", BAD),
    ("encrypted", "level=weak\n", "Encrypted, but the password is too short or too simple. Change "
     "the password.", BAD),
])
def test_encryption(program, gcc_world, system, state, strength, subtitle, icon):
    if state is None:
        system.remove("/usr/bin/plinius-encrypt")
    else:
        system.encryption(state, strength)
    netlog(gcc_world)
    r = gcc_world.run(program, [settle(), dump("main")])
    assert checks(r.dump("main"))["Encryption"] == (subtitle, icon)


@pytest.mark.parametrize("changed, subtitle, icon", [
    (None, "Not checked at this start yet.", WARN),
    ("\n  \n", "Not checked at this start yet.", WARN),
    ("/dev/disk/by-partlabel/boot_b\n/dev/disk/by-partlabel/lk_a\n",
     "Tampering detected: Kernel (boot_b), Bootloader (lk_a) changed since the last accepted state.", BAD),
    ("abm:/db/entries/plinius.conf\nabm:/kernel-b\n/dev/disk/by-partlabel/vbmeta_system_a\n"
     "/dev/disk/by-partlabel/init_boot\n/dev/disk/by-partlabel/userdata\n",
     "Tampering detected: Boot menu entry “plinius”, Boot menu card: /kernel-b, Boot Verification "
     "Data (vbmeta_system_a), Ramdisk (init_boot), /dev/disk/by-partlabel/userdata changed since "
     "the last accepted state.", BAD),
])
def test_boot_chain(program, gcc_world, system, changed, subtitle, icon):
    system.remove("/run/plinius-bootcheck/done")
    if changed is not None:
        system.write("/run/plinius-bootcheck/changed", changed)
    netlog(gcc_world)
    r = gcc_world.run(program, [settle(), dump("main")])
    assert checks(r.dump("main"))["Boot Chain"] == (subtitle, icon)


def test_no_boot_check_no_row(program, gcc_world, system):
    system.remove("/usr/bin/plinius-bootcheck")
    netlog(gcc_world)
    r = gcc_world.run(program, [settle(), dump("main")])
    assert "Boot Chain" not in checks(r.dump("main"))


@pytest.mark.parametrize("conf, subtitle, icon", [
    (None, "The package settings cannot be read.", WARN),
    ("[options]\nSigLevel = Never\n", "Packages without a valid signature are accepted (SigLevel in "
     "/etc/pacman.conf).", BAD),
    ("[core]\n  SigLevel = PackageOptional\n", "Packages without a valid signature are accepted "
     "(SigLevel in /etc/pacman.conf).", BAD),
    ("[options]\nSigLevel = Required DatabaseOptional\n# SigLevel = Never\n",
     "Updates are only installed with a valid signature.", GOOD),
])
def test_software_sources(program, gcc_world, system, conf, subtitle, icon):
    if conf is None:
        system.remove("/etc/pacman.conf")
    else:
        system.write("/etc/pacman.conf", conf)
    netlog(gcc_world)
    r = gcc_world.run(program, [settle(), dump("main")])
    assert checks(r.dump("main"))["Software Sources"] == (subtitle, icon)


def test_a_commented_never_is_no_weak_siglevel(program, gcc_world, system):
    system.write("/etc/pacman.conf", "[options]\n#SigLevel = Never\nSigLevel = Required\n"
                                     "LocalFileSigLevel = Optional\n")
    netlog(gcc_world)
    r = gcc_world.run(program, [settle(), dump("main")])
    assert checks(r.dump("main"))["Software Sources"][1] == GOOD


@pytest.mark.parametrize("siglevel", ["PackageOptional DatabaseOptional", "Optional DatabaseOptional"])
def test_optional_package_signatures_are_weak_even_with_optional_databases(program, gcc_world, system,
                                                                          siglevel):
    system.write("/etc/pacman.conf", f"[options]\nSigLevel = {siglevel}\n")
    netlog(gcc_world)
    r = gcc_world.run(program, [settle(), dump("main")])
    assert checks(r.dump("main"))["Software Sources"][1] == BAD


# Each word for packages and databases or, with Package/Database in front, for one of them; the
# last word for packages counts (as in pacman)
@pytest.mark.parametrize("line, icon", [
    ("SigLevel = DatabaseNever", GOOD),
    ("SigLevel = PackageNever DatabaseRequired", BAD),
    ("SigLevel = Optional PackageRequired", GOOD),
    ("SigLevel = PackageRequired Never", BAD),
    ("SigLevel=Never", BAD),
    ("SigLevel =\tRequired\tPackageOptional", BAD),
    ("SigLevel = Required TrustAll", GOOD),
    ("SigLevel", GOOD),
])
def test_siglevel_words(program, gcc_world, system, line, icon):
    system.write("/etc/pacman.conf", f"[options]\n{line}\n")
    netlog(gcc_world)
    r = gcc_world.run(program, [settle(), dump("main")])
    assert checks(r.dump("main"))["Software Sources"][1] == icon


def access(system, lsm=None, profiles=None, expected=None, enforce=None, policy=None):
    for p in ("/sys/kernel/security/lsm", "/sys/kernel/security/apparmor/profiles",
              "/usr/share/plinius-apparmor/expected", "/sys/fs/selinux/enforce",
              "/sys/kernel/security/apparmor/policy/profiles"):
        system.remove(p)
    if lsm is not None:
        system.write("/sys/kernel/security/lsm", lsm)
    if profiles is not None:
        system.write("/sys/kernel/security/apparmor/profiles", profiles)
    if expected is not None:
        system.write("/usr/share/plinius-apparmor/expected", expected)
    if enforce is not None:
        system.write("/sys/fs/selinux/enforce", enforce)
    for i, (name, mode) in enumerate(policy or []):
        if name is not None:
            system.write(f"/sys/kernel/security/apparmor/policy/profiles/{name}.{i}/name", f"{name}\n")
        if mode is not None:
            system.write(f"/sys/kernel/security/apparmor/policy/profiles/{name}.{i}/mode", f"{mode}\n")


@pytest.mark.parametrize("kw, subtitle, icon", [
    ({}, "No access control. A program that is taken over can reach everything its user can.", BAD),
    ({"lsm": "capability,selinux\n", "enforce": "1"}, "SELinux enforces its policy.", GOOD),
    ({"lsm": "selinux,apparmor\n", "enforce": "0"}, "SELinux is on but only logs.", WARN),
    ({"lsm": "selinux\n"}, "SELinux is on but only logs.", WARN),
    ({"lsm": "apparmor\n", "profiles": "", "expected": "plinius.a\n"},
     "AppArmor is on, but no program is confined yet.", WARN),
    ({"lsm": "apparmor\n", "profiles": "plinius.a (enforce)\nplinius.b (complain)\n",
      "expected": "plinius.a\nplinius.b\n"},
     "AppArmor confines 1 service; more only log what they do.", WARN),
    ({"lsm": "apparmor\n", "profiles": "plinius.a (enforce)\nplinius.b (enforce)\n",
      "expected": "plinius.a\nplinius.b\nplinius.c\n"},
     "AppArmor confines 2 services; more only log what they do.", WARN),
    ({"lsm": "apparmor\n", "profiles": "plinius.b (complain)\n", "expected": "plinius.b\n"},
     "AppArmor watches 1 service without blocking it yet.", WARN),
    ({"lsm": "apparmor\n", "profiles": "plinius.a (complain)\nplinius.b (complain)\n(nameless)\nbroken\n",
      "expected": "plinius.a\n\nplinius.b\n"},
     "AppArmor watches 2 services without blocking them yet.", WARN),
    ({"lsm": "apparmor\n", "profiles": "plinius.a (enforce)\n", "expected": "plinius.a\n"},
     "AppArmor confines 1 service of the network and the radio.", GOOD),
    # As a user: the profiles directory instead of the root-only file
    ({"lsm": "apparmor\n", "expected": "plinius.a\nplinius.b\n",
      "policy": [("plinius.a", "enforce"), ("plinius.b", "enforce"), ("other", None), (None, "x")]},
     "AppArmor confines 2 services of the network and the radio.", GOOD),
])
def test_access_control(program, gcc_world, system, kw, subtitle, icon):
    access(system, **kw)
    netlog(gcc_world)
    r = gcc_world.run(program, [settle(), dump("main")])
    assert checks(r.dump("main"))["Access Control"] == (subtitle, icon)


def test_access_control_page_lists_the_profiles(program, gcc_world, system):
    access(system, lsm="apparmor\n", expected="plinius.cupsd\nplinius.ofono\nplinius.mm\nother\n",
           profiles="plinius.cupsd (enforce)\nplinius.ofono (complain)\nother (kill)\n")
    netlog(gcc_world)
    r = gcc_world.run(program, [settle(), click(type="AdwActionRow", text="Access Control"),
                                settle(), dump("mac")])
    page = r.dump("mac").within(type="AdwNavigationPage", tag="plinius-access-control")
    assert page.one(type="AdwActionRow", text="In the Kernel")["subtitle"] == "AppArmor"
    profiles = page.within(type="AdwPreferencesGroup", text="Profiles")
    assert [(x["text"], x["subtitle"]) for x in profiles.find(type="AdwActionRow")] == [
        ("cupsd", "Enforced"), ("ofono", "Logging only"), ("mm", "Not loaded"), ("other", "kill")]


@pytest.mark.parametrize("kw, what", [({"lsm": "selinux\n", "enforce": "1"}, "SELinux, enforcing"),
                                      ({"lsm": "selinux\n", "enforce": "0"}, "SELinux, only logging"),
                                      ({}, "None")])
def test_access_control_page_without_apparmor(program, gcc_world, system, kw, what):
    access(system, **kw)
    netlog(gcc_world)
    r = gcc_world.run(program, [settle(), click(type="AdwActionRow", text="Access Control"),
                                settle(), dump("mac")])
    page = r.dump("mac").within(type="AdwNavigationPage", tag="plinius-access-control")
    assert page.one(type="AdwActionRow", text="In the Kernel")["subtitle"] == what
    assert not page.has(type="AdwPreferencesGroup", text="Profiles")


@pytest.mark.parametrize("row, page", [("Screen Lock", "Lock & Unlock"),
                                       ("Encryption", "Encryption Page")])
def test_checks_lead_to_their_pages(program, gcc_world, system, row, page):
    netlog(gcc_world)
    r = gcc_world.run(program, [settle(), click(type="AdwActionRow", text=row), settle(), dump("page")])
    assert r.dump("page").has(type="AdwNavigationPage", text=page)


def test_rows_without_a_page_do_nothing(program, gcc_world, system):
    netlog(gcc_world)
    r = gcc_world.run(program, [settle(), click(type="AdwActionRow", text="Bootloader"), settle(),
                                dump("page")])
    assert r.dump("page").has(type="GccTestPanel", tag="test-panel")


def test_the_worst_check_decides(program, gcc_world, system):
    system.remove(f"/var/lib/plinius-security/lock/{getpass.getuser()}")
    netlog(gcc_world)
    r = gcc_world.run(program, [settle(), dump("warn")])
    assert overall(r.dump("warn")) == (
        "Needs Attention", "Nothing is open, but some of the checks below could be better.")
    system.encryption("off")
    r = gcc_world.run(program, [settle(), dump("bad")])
    assert overall(r.dump("bad")) == (
        "At Risk", "Some of the checks below leave the phone's data open. The red rows say what to "
        "change.")


def test_checks_are_read_again_on_showing(program, gcc_world, system):
    netlog(gcc_world)
    lock = program.root / "var/lib/plinius-security/lock" / getpass.getuser()
    r = gcc_world.run(program, [settle(), click(type="AdwActionRow", text="Access Control"), settle(),
                                {"op": "write_file", "path": str(lock), "text": "level=weak\n"},
                                {"op": "back"}, settle(), dump("again")])
    assert checks(r.dump("again"))["Screen Lock"][1] == BAD
    # Made, shown, shown again
    assert len(gcc_world.calls(NETLOG, "Summary")) == 3


def test_without_the_recording_service(program, gcc_world, system):
    netlog(gcc_world, own=False)
    r = gcc_world.run(program, [settle(), dump("main"), push("plinius-connections"), settle(),
                                dump("conn")])
    assert r.dump("main").one(type="AdwActionRow", text="Connections")["subtitle"] == (
        "Not recorded: the recording service is not running.")
    page = r.dump("conn").within(type="AdwNavigationPage", tag="plinius-connections")
    assert page.has(type="AdwActionRow",
                    text="Connections are not recorded: the recording service is not running.")


def test_summary_in_the_singular(program, gcc_world, system):
    netlog(gcc_world, summary="(uint32 1, uint32 1, uint32 1, uint32 0)")
    r = gcc_world.run(program, [settle(), dump("main")])
    assert r.dump("main").one(type="AdwActionRow", text="Connections")["subtitle"] == (
        "Last hour, internet: 1 connection · 1 destination · 1 app")


# ----- Connections -----

def conn(app, exe, proto, port, addr, host, scope, count, last=1767323040):
    return (f"({app!r}, {exe!r}, uint32 {proto}, uint16 {port}, {addr!r}, {host!r}, uint32 {scope}, "
            f"uint32 {count}, int64 {last - 60}, int64 {last})")


def test_connections_by_app_internet_and_local(program, gcc_world, system):
    (gcc_world.data_dir / "applications" / "org.mozilla.firefox.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Firefox\nExec=/bin/true\nIcon=firefox\n")
    rows = [conn("org.mozilla.firefox", "/usr/lib/firefox/firefox", 6, 443, "192.0.2.10", "example.org", 2, 5),
            conn("org.mozilla.firefox", "/usr/lib/firefox/firefox", 17, 443, "192.0.2.11", "", 2, 1),
            conn("service:NetworkManager", "/usr/bin/NetworkManager", 17, 53, "192.168.1.1", "", 1, 9),
            conn("session", "/usr/bin/ssh", 6, 22, "192.0.2.20", "host.example", 2, 1),
            conn("session", "/usr/bin/curl", 1, 0, "192.0.2.21", "", 2, 1),
            conn("android", "/vendor/bin/x", 58, 0, "fe80::1", "", 1, 2),
            conn("unknown", "/opt/y", 99, 1, "192.0.2.22", "", 2, 1),
            conn("org.Example.Gone", "/usr/bin/gone", 6, 80, "192.0.2.23", "", 2, 1)]
    netlog(gcc_world, query=f"([{', '.join(rows)}],)")
    r = gcc_world.run(program, [settle(), push("plinius-connections"), settle(), dump("conn")])
    page = r.dump("conn").within(type="AdwNavigationPage", tag="plinius-connections")
    internet = page.within(type="AdwPreferencesGroup", text="Internet")
    groups = [(g["text"], g["subtitle"]) for g in internet.find(type="AdwExpanderRow")]
    assert groups == [
        ("Firefox", "6 connections · 2 destinations"),
        ("/opt/y", "Program · 1 connection · 1 destination"),
        ("/usr/bin/curl", "Program · 1 connection · 1 destination"),
        ("/usr/bin/ssh", "Program · 1 connection · 1 destination"),
        ("org.Example.Gone", "1 connection · 1 destination"),
    ]
    lan = page.within(type="AdwPreferencesGroup", text="Local Network")
    assert [(g["text"], g["subtitle"]) for g in lan.find(type="AdwExpanderRow")] == [
        ("NetworkManager", "Service · 9 connections · 1 destination"),
        ("Android Container", "Vendor services of the phone · 2 connections · 1 destination")]
    assert [c["args"] for c in gcc_world.calls(NETLOG, "Query")] == [[3600]]


def test_destinations_of_an_app(program, gcc_world, system):
    rows = [conn("org.mozilla.firefox", "/usr/lib/firefox/firefox", 6, 443, "192.0.2.10", "example.org", 2, 5),
            conn("org.mozilla.firefox", "/usr/lib/firefox/firefox", 17, 443, "192.0.2.11", "", 2, 1)]
    netlog(gcc_world, query=f"([{', '.join(rows)}],)")
    r = gcc_world.run(program, [settle(), push("plinius-connections"), settle(),
                                set_active(True, type="AdwExpanderRow", text="org.mozilla.firefox"),
                                settle(), dump("open")])
    page = r.dump("open").within(type="AdwNavigationPage", tag="plinius-connections")
    dest = [(x["text"], x["subtitle"]) for x in page.find(type="AdwActionRow")
            if x["text"] != "org.mozilla.firefox"]
    assert dest == [
        ("example.org", "192.0.2.10 · TCP 443 · /usr/lib/firefox/firefox · 5 times, last 01/02/26 03:04"),
        ("192.0.2.11", "UDP 443 · /usr/lib/firefox/firefox · 1 time, last 01/02/26 03:04")]


def test_no_connections_in_the_range(program, gcc_world, system):
    netlog(gcc_world, query="(@a(ssuqssuuxx) [],)")
    r = gcc_world.run(program, [settle(), push("plinius-connections"), settle(),
                                select(name="1w", type="AdwToggleGroup"), settle(), dump("conn")])
    page = r.dump("conn").within(type="AdwNavigationPage", tag="plinius-connections")
    assert page.has(type="AdwActionRow", text="No connections in this time.")
    assert [c["args"] for c in gcc_world.calls(NETLOG, "Query")] == [[3600], [604800]]
    assert page.one(type="AdwToggleGroup")["toggles"] == ["15 min", "30 min", "1 h", "Day", "Week"]


# ----- Known vulnerabilities -----

def cve_file(system, data):
    system.write("/var/lib/plinius-cve/cve.json", json.dumps(data) if not isinstance(data, str) else data)


def issue(cve, severity="Medium", status=None, package="openssl", installed="3.0-1", group="AVG-1",
          type_="arbitrary code execution", fixed=None, affected=None, note=None):
    i = {"cve": cve, "severity": severity, "package": package, "installed": installed,
         "group": group, "type": type_}
    if fixed:
        i["fixed"] = fixed
    if affected:
        i["affected"] = affected
    if status:
        i["assessment"] = {"status": status, **({"note": note} if note else {})}
    return i


@pytest.mark.parametrize("data, subtitle, icon", [
    (None, "Not checked yet. The list is loaded four times a day.", WARN),
    ("not json", "Not checked yet. The list is loaded four times a day.", WARN),
    ({"issues": []}, "No known vulnerabilities in the installed packages.", GOOD),
    ({"issues": [issue("CVE-1", "High", "affected"), issue("CVE-2"), issue("CVE-3", status="not-affected")]},
     "3 known vulnerabilities in installed packages: 1 affects this system, 1 not assessed", BAD),
    ({"issues": [issue("CVE-1", "Low", "mitigated")]},
     "1 known vulnerability in installed packages: 1 affects this system, 0 not assessed", WARN),
    ({"issues": [issue("CVE-3", status="fixed")]},
     "1 known vulnerability in installed packages: 0 affect this system, 0 not assessed", GOOD),
    ({"issues": [], "kernel": {"cves": {"issues": [issue("CVE-9", "High", "affected")]}}},
     "1 known vulnerability (0 in installed packages, 1 in the kernel): 1 affects this system, 0 not "
     "assessed",
     WARN),
])
def test_vulnerabilities_row(program, gcc_world, system, data, subtitle, icon):
    if data is None:
        system.remove("/var/lib/plinius-cve/cve.json")
    else:
        cve_file(system, data)
    netlog(gcc_world)
    r = gcc_world.run(program, [settle(), dump("main")])
    row = r.dump("main").one(type="AdwActionRow", text="Known Vulnerabilities")
    icons = [c["icon"] for c in row["children"] if c["type"] == "GtkImage"]
    assert (row["subtitle"], icons[0]) == (subtitle, icon)


def test_vulnerabilities_page_sorted_by_assessment_and_severity(program, gcc_world, system):
    cve_file(system, {"checked": 1767323040, "downloaded": 1767323040, "issues": [
        issue("CVE-2026-0001", "Low", "affected", fixed="3.1-1", note="Reachable from the network."),
        issue("CVE-2026-0002", "Critical", "mitigated", affected="3.0-1"),
        issue("CVE-2026-0003", "Unknown", type_="unknown", group="", installed=None),
        issue("CVE-2026-0004", "High", "not-affected"),
        issue("CVE-2026-0005", "High", "fixed"),
        issue("XSA-7", "Medium")]})
    netlog(gcc_world)
    r = gcc_world.run(program, [settle(), push("plinius-vulnerabilities"), settle(), dump("vuln")])
    page = r.dump("vuln").within(type="AdwNavigationPage", tag="plinius-vulnerabilities")
    assert page.one(type="AdwActionRow", text="Last Checked")["subtitle"] == (
        "01/02/26 03:04, list of the tracker from 01/02/26 03:04")
    sections = {}
    for title in ("Affect This System", "Not Assessed", "Do Not Affect This System"):
        g = page.within(type="AdwPreferencesGroup", text=title)
        sections[title] = [(x["text"], x["subtitle"]) for x in g.find(type="AdwExpanderRow")]
    assert sections == {
        "Affect This System": [
            ("CVE-2026-0002", "openssl 3.0-1 · Critical · arbitrary code execution"),
            ("CVE-2026-0001", "openssl 3.0-1 · Low · arbitrary code execution")],
        "Not Assessed": [("XSA-7", "openssl 3.0-1 · Medium · arbitrary code execution"),
                         ("CVE-2026-0003", "openssl")],
        "Do Not Affect This System": [
            ("CVE-2026-0005", "openssl 3.0-1 · High · arbitrary code execution"),
            ("CVE-2026-0004", "openssl 3.0-1 · High · arbitrary code execution")],
    }
    labels = {x["text"]: x.get("classes") for x in page.find(type="GtkLabel")
              if x["text"] in ("Affects this system", "Mitigated", "Not assessed", "Fixed here",
                               "Does not affect this system")}
    assert labels == {"Affects this system": ["error"], "Mitigated": ["warning"],
                      "Not assessed": ["dimmed"], "Fixed here": ["success"],
                      "Does not affect this system": ["success"]}


def test_an_issue_opened(program, gcc_world, system):
    cve_file(system, {"checked": 1767323040, "issues": [
        issue("CVE-2026-0001", "Low", "affected", fixed="3.1-1", note="Reachable from the network."),
        issue("CVE-2026-0002", "Low", "mitigated", affected="3.0-1", group="X-1"),
        issue("CVE-2026-0003", "Low")]})
    netlog(gcc_world)
    gcc_world.service("session", PORTAL, PORTAL_PATH, PORTAL_XML, properties={"version": "uint32 5"})
    r = gcc_world.run(program, [settle(), push("plinius-vulnerabilities"), settle(),
                                set_active(True, type="AdwExpanderRow", text="CVE-2026-0001"),
                                set_active(True, type="AdwExpanderRow", text="CVE-2026-0002"),
                                set_active(True, type="AdwExpanderRow", text="CVE-2026-0003"),
                                settle(), dump("open"),
                                click(type="AdwActionRow", text="CVE Record",
                                      within={"type": "AdwExpanderRow", "text": "CVE-2026-0001"}),
                                settle(),
                                click(type="AdwActionRow", text="Arch Linux Security Tracker",
                                      within={"type": "AdwExpanderRow", "text": "CVE-2026-0001"}),
                                settle()])
    page = r.dump("open").within(type="AdwNavigationPage", tag="plinius-vulnerabilities")

    def inner(cve):
        rows = page.within(type="AdwExpanderRow", text=cve).find(type="AdwActionRow")
        return [(x["text"], x["subtitle"]) for x in rows if x["text"] != cve]

    assert inner("CVE-2026-0001") == [
        ("Affects this system", "Reachable from the network."), ("Fix", "Fixed in 3.1-1; an update is due."),
        ("CVE Record", "https://www.cve.org/CVERecord?id=CVE-2026-0001"),
        ("Arch Linux Security Tracker", "https://security.archlinux.org/AVG-1")]
    assert inner("CVE-2026-0002") == [
        ("Mitigated", "Nobody has checked yet whether and how this issue affects this system."),
        ("Fix", "Affected since 3.0-1, no fixed version known."),
        ("CVE Record", "https://www.cve.org/CVERecord?id=CVE-2026-0002")]
    assert inner("CVE-2026-0003")[1] == ("Fix", "No fixed version known.")
    uris = [c["args"][1] for c in gcc_world.calls(PORTAL, "OpenURI")]
    assert uris == ["https://www.cve.org/CVERecord?id=CVE-2026-0001",
                    "https://security.archlinux.org/AVG-1"]


def test_vulnerabilities_page_without_a_list(program, gcc_world, system):
    system.remove("/var/lib/plinius-cve/cve.json")
    netlog(gcc_world)
    r = gcc_world.run(program, [settle(), push("plinius-vulnerabilities"), settle(), dump("vuln")])
    page = r.dump("vuln").within(type="AdwNavigationPage", tag="plinius-vulnerabilities")
    assert page.one(type="AdwActionRow", text="Last Checked")["subtitle"] == (
        "Not yet. The list is loaded four times a day while online.")


def test_vulnerabilities_page_with_an_error_and_no_issues(program, gcc_world, system):
    cve_file(system, {"checked": 1767323040, "error": "tracker not reachable", "issues": []})
    netlog(gcc_world)
    r = gcc_world.run(program, [settle(), push("plinius-vulnerabilities"), settle(), dump("vuln")])
    page = r.dump("vuln").within(type="AdwNavigationPage", tag="plinius-vulnerabilities")
    assert page.one(type="AdwActionRow", text="Last Checked")["subtitle"] == (
        "01/02/26 03:04. Last download failed: tracker not reachable")
    assert page.has(type="AdwActionRow", text="No known vulnerabilities in the installed packages.")


# ----- Boot chain -----

CHANGED = "/dev/disk/by-partlabel/boot_b\nabm:/db/entries/plinius.conf\n"


def boot_alarm(system):
    system.remove("/run/plinius-bootcheck/done")
    system.write("/run/plinius-bootcheck/changed", CHANGED)


def test_boot_chain_page_explains_the_alarm(program, gcc_world, system):
    boot_alarm(system)
    netlog(gcc_world)
    r = gcc_world.run(program, [settle(), click(type="AdwActionRow", text="Boot Chain"), settle(),
                                dump("boot")])
    page = r.dump("boot").within(type="AdwNavigationPage", tag="plinius-boot-chain")
    assert page.find(type="AdwActionRow")[0]["text"] == "Tampering Detected"
    parts = page.within(type="AdwPreferencesGroup", text="Changed Parts")
    assert [(x["text"], x["subtitle"]) for x in parts.find(type="AdwActionRow")] == [
        ("Kernel (boot_b)", "/dev/disk/by-partlabel/boot_b"),
        ("Boot menu entry “plinius”", "abm:/db/entries/plinius.conf")]
    todo = page.within(type="AdwPreferencesGroup", text="What to Do")
    assert [x["text"] for x in todo.find(type="AdwActionRow")] == ["You Changed It", "You Did Not"]
    assert page.has(type="GtkButton", text="Accept Changes")


def test_boot_chain_page_without_changes(program, gcc_world, system):
    netlog(gcc_world)
    r = gcc_world.run(program, [settle(), push("plinius-boot-chain"), settle(), dump("boot")])
    page = r.dump("boot").within(type="AdwNavigationPage", tag="plinius-boot-chain")
    assert [x["text"] for x in page.find(type="AdwActionRow")] == ["No Changes"]
    assert not page.has(type="GtkButton", text="Accept Changes")


def test_accept_changes_ends_the_alarm(program, gcc_world, system):
    boot_alarm(system)
    done = program.root / "run/plinius-bootcheck/done"
    changed = program.root / "run/plinius-bootcheck/changed"
    gcc_world.fake_bin.stub("pkexec", f'rm -f "{changed}"; touch "{done}"; exit 0')
    netlog(gcc_world)
    r = gcc_world.run(program, [settle(), click(type="AdwActionRow", text="Boot Chain"), settle(),
                                click(type="GtkButton", text="Accept Changes"), settle(300),
                                dump("after")])
    assert gcc_world.fake_bin.calls("pkexec") == [[
        str(program.root / "usr/bin/plinius-bootcheck"), "accept",
        "/dev/disk/by-partlabel/boot_b", "abm:/db/entries/plinius.conf"]]
    s = r.dump("after")
    # Back on the checks, which say so
    assert s.has(type="GccTestPanel", tag="test-panel")
    assert checks(s)["Boot Chain"][1] == GOOD


@pytest.mark.parametrize("exit, stderr, message", [
    (126, "", None),
    (127, "", "Not accepted: not authorized."),
    (1, "accept: cannot write the state", "Not accepted: accept: cannot write the state"),
    (2, "", "Not accepted: ?"),
])
def test_accept_refused(program, gcc_world, system, exit, stderr, message):
    boot_alarm(system)
    gcc_world.fake_bin.stub("pkexec", f'echo "{stderr}" >&2; exit {exit}')
    netlog(gcc_world)
    r = gcc_world.run(program, [settle(), click(type="AdwActionRow", text="Boot Chain"), settle(),
                                click(type="GtkButton", text="Accept Changes"), settle(300),
                                dump("after")])
    page = r.dump("after").within(type="AdwNavigationPage", tag="plinius-boot-chain")
    group = [g for g in page.find(type="AdwPreferencesGroup") if g.get("subtitle", "").startswith(
        ("Takes the current", "Not accepted"))]
    expected = message or "Takes the current state as the accepted one and ends the alarm. Asks for your PIN."
    assert [g["subtitle"] for g in group] == [expected]
    # The button can be pressed again
    assert page.one(type="GtkButton", text="Accept Changes").get("sensitive", True)


def test_accept_without_pkexec(program, gcc_world, system):
    boot_alarm(system)
    netlog(gcc_world)
    r = gcc_world.run(program, [settle(), click(type="AdwActionRow", text="Boot Chain"), settle(),
                                click(type="GtkButton", text="Accept Changes"), settle(),
                                dump("after")])
    page = r.dump("after").within(type="AdwNavigationPage", tag="plinius-boot-chain")
    subtitles = [g.get("subtitle", "") for g in page.find(type="AdwPreferencesGroup")]
    assert any(s.startswith("Not accepted: ") and "pkexec" in s for s in subtitles)


# ----- The kernel's vulnerabilities -----

def kissue(cve, score=None, title=None, type_=None, fixed=None, status=None):
    i = {"cve": cve}
    if score is not None:
        i["score"] = score
    if title:
        i["title"] = title
    if type_:
        i["type"] = type_
    if fixed:
        i["fixed"] = fixed
    if status:
        i["assessment"] = {"status": status}
    return i


def kernel_data(release="6.1.192-g1234", **cves):
    k = {"release": release}
    if cves:
        k["cves"] = cves
    return k


def vuln_page(program, world, system, data, env=None):
    cve_file(system, {"checked": 1767323040, "issues": [], **data})
    netlog(world)
    if env:
        world.extra_env.update(env)
    r = world.run(program, [settle(), push("plinius-vulnerabilities"), settle(), dump("vuln")])
    return r.dump("vuln").within(type="AdwNavigationPage", tag="plinius-vulnerabilities")


def props(page, group=None):
    g = page.within(type="AdwPreferencesGroup", text=group) if group else page
    return {x["text"]: x.get("subtitle") for x in g.find(type=("AdwActionRow", "AdwExpanderRow"))}


def test_kernel_of_the_phone_maker(program, gcc_world, system):
    page = vuln_page(program, gcc_world, system, {"kernel": {
        "release": "4.19.191-vendor", "vendor_security_patch": "2025-06-05"}})
    p = props(page)
    assert p["Kernel"] == "4.19.191-vendor, from the phone maker"
    assert p["Vendor System"] == ("Security patch level 2025-06-05, firmware and drivers from the "
                                  "phone maker")
    # No list of the kernel's own issues: no section
    assert not page.has(type="AdwPreferencesGroup", text="Kernel")


@pytest.fixture(scope="module")
def shim(tmp_path_factory):
    from gcc_harness import uname_shim
    return uname_shim(tmp_path_factory.mktemp("uname"))


@pytest.mark.parametrize("version, built", [
    ("#1 SMP PREEMPT Sat Nov  8 19:40:00 CET 2026", ", own build of 11/08/26"),
    ("#1 SMP PREEMPT Sat Nov 18 19:40:00 CET 2026", ", own build of 11/18/26"),
    ("#1 SMP PREEMPT Sat Nov", ", own build"),
    ("#1 SMP PREEMPT Sat Nov 99 19:40:00 CET 2026", ", own build"),
    ("#1 SMP PREEMPT 2026", ", own build"),
])
def test_own_kernel_with_the_day_it_was_built(program, gcc_world, system, shim, version, built):
    release = "6.1.192-plinius20"
    page = vuln_page(program, gcc_world, system,
                     {"kernel": {"release": release, "security_patch": "2025-06-05"}},
                     env={"LD_PRELOAD": str(shim), "PLINIUS_TEST_UNAME_RELEASE": release,
                          "PLINIUS_TEST_UNAME_VERSION": version})
    p = props(page)
    assert p["Kernel"] == release + built
    assert p["Vendor System"].startswith("Security patch level 2025-06-05")


def test_own_kernel_that_no_longer_runs(program, gcc_world, system, shim):
    page = vuln_page(program, gcc_world, system, {"kernel": {"release": "6.1.192-plinius20"}},
                     env={"LD_PRELOAD": str(shim), "PLINIUS_TEST_UNAME_RELEASE": "6.1.192-plinius21",
                          "PLINIUS_TEST_UNAME_VERSION": "#1 SMP PREEMPT Sat Nov 8 19:40:00 CET 2026"})
    assert props(page)["Kernel"] == "6.1.192-plinius20, own build"


def test_kernel_list_not_loaded(program, gcc_world, system):
    page = vuln_page(program, gcc_world, system, {"kernel": {"release": "6.1.192-x",
                                                             "cves_error": "HTTP 503"}})
    assert props(page, "Kernel") == {"Kernel List": "Not loaded yet: HTTP 503"}


def test_kernel_section_without_high_rated_issues(program, gcc_world, system):
    page = vuln_page(program, gcc_world, system, {"kernel": {
        "release": "6.1.192-plinius20", "cves_error": "timeout",
        "cves": {"generated": 1767323040, "built": 120, "listed": 0, "other": 120, "issues": []}}})
    assert props(page, "Kernel") == {
        "Kernel List": "01/02/26 03:04, from the Linux kernel CNA. Last download failed: timeout",
        "Known Issues": "120 in the own-build kernel 6.1.192",
        "Issues with a High Rating": "0 with a CVSS score of 7 or more; 120 more with a lower or no score",
    }


def test_kernel_issues_built_when_opened(program, gcc_world, system):
    cve_file(system, {"checked": 1767323040, "issues": [], "kernel": {
        "release": "6.1.192-g1", "cves": {
            "generated": 1767323040, "built": 3, "listed": 3, "other": 9, "issues": [
                kissue("CVE-2026-1000", 7.1, "net: fix a leak", "memory leak", "6.1.193"),
                kissue("CVE-2026-1001", 9.8, title="bt: fix overflow", fixed=None, status="affected"),
                kissue("CVE-2026-1002", 7.1, status="mitigated"),
                kissue("GHSA-x")]}}})
    netlog(gcc_world)
    gcc_world.service("session", PORTAL, PORTAL_PATH, PORTAL_XML, properties={"version": "uint32 5"})
    r = gcc_world.run(program, [settle(), dump("main"), push("plinius-vulnerabilities"), settle(),
                                dump("closed"),
                                set_active(True, type="AdwExpanderRow", text="Issues with a High Rating"),
                                settle(), dump("open"),
                                click(type="AdwActionRow", text="CVE-2026-1001"), settle()])
    assert r.dump("main").one(type="AdwActionRow", text="Known Vulnerabilities")["subtitle"] == (
        "4 known vulnerabilities (0 in installed packages, 4 in the kernel): 2 affect this system, "
        "2 not assessed")
    page = r.dump("open").within(type="AdwNavigationPage", tag="plinius-vulnerabilities")
    assert props(page, "Kernel")["Known Issues"] == "3 in kernel 6.1.192"
    rows = page.within(type="AdwExpanderRow", text="Issues with a High Rating").find(type="AdwActionRow")
    got = [(x["text"], x["subtitle"]) for x in rows if x["text"] != "Issues with a High Rating"]
    # Highest score first, the newest CVE first at the same score
    assert got == [("CVE-2026-1001", "bt: fix overflow\nCVSS 9.8 · no fix yet"),
                   ("CVE-2026-1002", "CVSS 7.1 · no fix yet"),
                   ("CVE-2026-1000", "net: fix a leak\nCVSS 7.1 · memory leak · fixed in 6.1.193"),
                   ("GHSA-x", "CVSS 0.0 · no fix yet")]
    labels = [(x["text"], x["classes"]) for x in page.find(type="GtkLabel")
              if x["text"] in ("Affects this system", "Mitigated")]
    assert labels == [("Affects this system", ["error"]), ("Mitigated", ["warning"])]
    assert [c["args"][1] for c in gcc_world.calls(PORTAL, "OpenURI")] == [
        "https://www.cve.org/CVERecord?id=CVE-2026-1001"]
    # Before it was opened, no row of an issue existed
    closed = r.dump("closed").within(type="AdwNavigationPage", tag="plinius-vulnerabilities")
    assert not closed.has(type="AdwActionRow", text="CVE-2026-1001")


# ----- Edges -----

def test_without_a_system_bus_no_connections_are_asked(program, gcc_world, system):
    netlog(gcc_world)
    r = gcc_world.run(program, [settle(), dump("main"), push("plinius-connections"), settle()],
                      system_bus=False)
    assert "subtitle" not in r.dump("main").one(type="AdwActionRow", text="Connections")
    assert not gcc_world.calls(NETLOG)


def test_answers_still_on_their_way_when_the_panel_goes(program, gcc_world, system):
    netlog(gcc_world, summary={"delay_ms": 3000, "reply": "(uint32 1, uint32 1, uint32 1, uint32 0)"},
           query={"delay_ms": 3000, "reply": "(@a(ssuqssuuxx) [],)"})
    r = gcc_world.run(program, [settle(), push("plinius-connections"), settle()])
    assert not r.warnings()


def test_accept_still_running_when_the_panel_goes(program, gcc_world, system):
    boot_alarm(system)
    gcc_world.fake_bin.stub("pkexec", "sleep 3")
    netlog(gcc_world)
    r = gcc_world.run(program, [settle(), click(type="AdwActionRow", text="Boot Chain"), settle(),
                                click(type="GtkButton", text="Accept Changes"), settle(), dump("busy")])
    page = r.dump("busy").within(type="AdwNavigationPage", tag="plinius-boot-chain")
    # Pressed once: not again until pkexec is done
    assert page.one(type="GtkButton", text="Accept Changes")["sensitive"] is False


def test_accept_after_the_alarm_ended_elsewhere(program, gcc_world, system):
    boot_alarm(system)
    netlog(gcc_world)
    changed = program.root / "run/plinius-bootcheck/changed"
    gcc_world.run(program, [settle(), click(type="AdwActionRow", text="Boot Chain"), settle(),
                            {"op": "remove_file", "path": str(changed)},
                            click(type="GtkButton", text="Accept Changes"), settle()])
    assert gcc_world.fake_bin.calls("pkexec") == []


def test_times_of_today_and_out_of_range(program, gcc_world, system):
    now = int(time.time())
    if 86400 - now % 86400 < 10:
        time.sleep(11)
        now = int(time.time())
    rows = [conn("org.a", "/a", 6, 1, "192.0.2.1", "", 2, 1, last=now),
            conn("org.b", "/b", 6, 1, "192.0.2.2", "", 2, 1, last=2**40)]
    netlog(gcc_world, query=f"([{', '.join(rows)}],)")
    r = gcc_world.run(program, [settle(), push("plinius-connections"), settle(),
                                set_active(True, type="AdwExpanderRow", text="org.a"),
                                set_active(True, type="AdwExpanderRow", text="org.b"), settle(),
                                dump("conn")])
    page = r.dump("conn").within(type="AdwNavigationPage", tag="plinius-connections")
    subs = {x["text"]: x["subtitle"] for x in page.find(type="AdwActionRow")}
    assert subs["192.0.2.1"] == f"TCP 1 · /a · 1 time, last {time.strftime('%H:%M', time.gmtime(now))}"
    assert subs["192.0.2.2"] == "TCP 1 · /b · 1 time, last "
