# Remote Access (cc-desktop-vnc-panel.c): the overview with Secure Shell and Desktop over VNC, each
# saying On or Off; the Secure Shell page (GNOME's switch through systemd and polkit, the login
# command from hostnamed, our Allowed Networks of cc-plinius-ssh.c, tested in test_gcc_ssh.py);
# the VNC page: the switch enables the user's plinius-desktop.socket (systemctl --user), the status
# of a connection from the session's client file, the addresses a viewer can reach, the networks
# (pkexec plinius-desktop-set allow|deny usb|local, a fake tool in the program's root writing
# /etc/plinius-desktop.conf there) and the taskbar's edge (org.plinius.desktop).
#
# systemd1, hostname1 and polkit are fakes on the private system bus; systemctl and pkexec fake
# programs; the phone's network interfaces come from a getifaddrs () of the glue, so the address
# row never depends on the host's network.

import shutil

import pytest

from fake_programs import Programs
from gcc_harness import (build_panel, call, click, dump, gcc_session, gcc_world, push,  # noqa: F401
                         select, set_active, settle, wait, wait_for)

SET_TOOL = "/usr/lib/plinius-desktop/plinius-desktop-set"
CONF = "/etc/plinius-desktop.conf"
SSH_TOOL = "/usr/lib/plinius-ssh/plinius-ssh-set"
SSH_CONF = "/etc/plinius-ssh.conf"
SOCKET = "plinius-desktop.socket"

SYSTEMD = "org.freedesktop.systemd1"
SYSTEMD_PATH = "/org/freedesktop/systemd1"
SYSTEMD_XML = """<node><interface name="org.freedesktop.systemd1.Manager">
 <method name="GetUnitFileState"><arg type="s" name="file" direction="in"/><arg type="s" name="state" direction="out"/></method>
 <method name="StartUnit"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="o" direction="out"/></method>
 <method name="StopUnit"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="o" direction="out"/></method>
 <method name="EnableUnitFiles"><arg type="as" direction="in"/><arg type="b" direction="in"/><arg type="b" direction="in"/>
  <arg type="b" direction="out"/><arg type="a(sss)" direction="out"/></method>
 <method name="DisableUnitFiles"><arg type="as" direction="in"/><arg type="b" direction="in"/>
  <arg type="a(sss)" direction="out"/></method>
</interface></node>"""
HOSTNAME = "org.freedesktop.hostname1"
HOSTNAME_PATH = "/org/freedesktop/hostname1"
HOSTNAME_XML = """<node><interface name="org.freedesktop.hostname1">
 <property name="Hostname" type="s" access="read"/>
 <property name="StaticHostname" type="s" access="read"/>
 <property name="PrettyHostname" type="s" access="read"/>
</interface></node>"""
POLKIT = "org.freedesktop.PolicyKit1"
POLKIT_PATH = "/org/freedesktop/PolicyKit1/Authority"
POLKIT_XML = """<node><interface name="org.freedesktop.PolicyKit1.Authority">
 <method name="CheckAuthorization">
  <arg type="(sa{sv})" name="subject" direction="in"/><arg type="s" name="action_id" direction="in"/>
  <arg type="a{ss}" name="details" direction="in"/><arg type="u" name="flags" direction="in"/>
  <arg type="s" name="cancellation_id" direction="in"/>
  <arg type="(bba{ss})" name="result" direction="out"/></method>
 <method name="CancelCheckAuthorization"><arg type="s" name="cancellation_id" direction="in"/></method>
 <signal name="Changed"/>
 <property name="BackendName" type="s" access="read"/>
 <property name="BackendVersion" type="s" access="read"/>
 <property name="BackendFeatures" type="u" access="read"/>
</interface></node>"""

GLUE = r'''
#include <arpa/inet.h>
#include <ifaddrs.h>
#include <net/if.h>
#include <stdlib.h>
#include <string.h>
#include "gcc_driver.h"
#include "cc-desktop-vnc-panel.h"
#include "shell/cc-object-storage.h"

/* The phone's interfaces instead of the host's: PLINIUS_TEST_IFADDRS="name=address[,down];...",
 * address "-" for an entry without one, "v6" for an IPv6 one; "!" fails */
typedef struct {
    struct ifaddrs ifa;
    struct sockaddr_in in;
    struct sockaddr_in6 in6;
    char name[IFNAMSIZ];
} FakeIfaddr;

int
getifaddrs (struct ifaddrs **list)
{
    const char *spec = g_getenv ("PLINIUS_TEST_IFADDRS");
    g_auto(GStrv) entries = g_strsplit (spec ? spec : "", ";", -1);
    struct ifaddrs *first = NULL, **next = &first;

    if (!g_strcmp0 (spec, "!"))
        return -1;
    for (char **e = entries; *e; e++) {
        g_auto(GStrv) kv = g_strsplit (*e, "=", 2);
        g_auto(GStrv) parts = NULL;
        FakeIfaddr *f;

        if (!kv[0] || !kv[1])
            continue;
        parts = g_strsplit (kv[1], ",", 2);
        f = calloc (1, sizeof *f);
        g_strlcpy (f->name, kv[0], sizeof f->name);
        f->ifa.ifa_name = f->name;
        f->ifa.ifa_flags = parts[1] && g_str_equal (parts[1], "down") ? 0 : IFF_UP;
        if (g_str_equal (parts[0], "v6")) {
            f->in6.sin6_family = AF_INET6;
            f->ifa.ifa_addr = (struct sockaddr *) &f->in6;
        } else if (!g_str_equal (parts[0], "-")) {
            f->in.sin_family = AF_INET;
            inet_pton (AF_INET, parts[0], &f->in.sin_addr);
            f->ifa.ifa_addr = (struct sockaddr *) &f->in;
        }
        *next = &f->ifa;
        next = &f->ifa.ifa_next;
    }
    *list = first;
    return 0;
}

void
freeifaddrs (struct ifaddrs *list)
{
    while (list) {
        struct ifaddrs *next = list->ifa_next;

        free (list);
        list = next;
    }
}

CcPanel *
gcc_test_create_panel (GccTest *t)
{
    /* As GNOME Settings' main () does */
    cc_object_storage_initialize ();
    return g_object_new (CC_TYPE_DESKTOP_VNC_PANEL, "window", t->window, NULL);
}

void
gcc_test_setup (GccTest *t)
{
}

gboolean
gcc_test_call (GccTest *t, const char *name, JsonArray *args, JsonBuilder *result)
{
    if (g_str_equal (name, "static_init")) {
        cc_desktop_vnc_panel_static_init_func ();
        json_builder_add_null_value (result);
        return TRUE;
    }
    if (g_str_equal (name, "clipboard")) {
        /* What the copy button put on the clipboard */
        GdkClipboard *clipboard = gtk_widget_get_clipboard (GTK_WIDGET (t->panel));
        GdkContentProvider *content = gdk_clipboard_get_content (clipboard);
        g_auto(GValue) value = G_VALUE_INIT;

        g_value_init (&value, G_TYPE_STRING);
        if (content && gdk_content_provider_get_value (content, &value, NULL))
            json_builder_add_string_value (result, g_value_get_string (&value));
        else
            json_builder_add_null_value (result);
        return TRUE;
    }
    return FALSE;
}
'''


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    vnc = [(f'"{SET_TOOL}"', f'"{{root}}{SET_TOOL}"'), (f'"{CONF}"', f'"{{root}}{CONF}"')]
    ssh = [(f'"{SSH_TOOL}"', f'"{{root}}{SSH_TOOL}"'), (f'"{SSH_CONF}"', f'"{{root}}{SSH_CONF}"')]
    return build_panel(gcc_session, tmp_path_factory.mktemp("vnc"), "vnc", ["cc-desktop-vnc-panel.c", "cc-plinius-ssh.c"],
                       GLUE, replacements={"cc-desktop-vnc-panel.c": vnc, "cc-plinius-ssh.c": ssh},
                       upstream=("panels/system/secure-shell/cc-secure-shell.c", "panels/system/cc-systemd-service.c",
                                 "panels/common/cc-hostname.c", "panels/common/hostname-helper.c",
                                 "shell/cc-object-storage.c"),
                       pkgs=("polkit-gobject-1",))


class Phone:
    def __init__(self, program, world):
        self.root = program.root
        for d in list(self.root.iterdir()):
            shutil.rmtree(d) if d.is_dir() else d.unlink()
        self.world = world
        self.progs = Programs(world.tmp / "programs")
        self.link = world.home / ".config/systemd/user/graphical-session.target.wants" / SOCKET
        self.client = world.runtime / "plinius-desktop" / "client"
        self.progs.program(world.fake_bin.path / "systemctl")
        self.progs.rule("systemctl", ["--user", "enable", "--now", SOCKET], write={str(self.link): ""})
        self.progs.rule("systemctl", ["--user", "disable", "--now", SOCKET], remove=[str(self.link)])
        self.progs.program(world.fake_bin.path / "pkexec")
        self.progs.rule("pkexec", exec=True)
        self.log = world.tmp / "set-args"
        self.code = world.tmp / "set-code"
        self.tool = self.path(SET_TOOL)
        self.tool.parent.mkdir(parents=True)
        # As plinius-desktop-set: a line key=yes|no, the last one of a key counts
        self.tool.write_text(f"""#!/bin/bash
echo "$*" >> "{self.log}"
code=$(cat "{self.code}" 2>/dev/null || echo 0)
[ "$code" = 0 ] || exit $code
case "$1" in allow) v=yes ;; deny) v=no ;; esac
mkdir -p "{self.path('/etc')}"
echo "$2=$v" >> "{self.path(CONF)}"
""")
        self.tool.chmod(0o755)
        self.interfaces()

    def path(self, p):
        return self.root / p.lstrip("/")

    def interfaces(self, spec="lo=127.0.0.1;usb0=10.15.19.82;wlan0=192.168.1.20"):
        self.world.extra_env["PLINIUS_TEST_IFADDRS"] = spec

    def enabled(self, on=True):
        if on:
            self.link.parent.mkdir(parents=True, exist_ok=True)
            self.link.write_text("")
        elif self.link.exists():
            self.link.unlink()

    def connected(self, who):
        self.client.parent.mkdir(parents=True, exist_ok=True)
        self.client.write_text(who)

    def conf(self, text):
        self.path(CONF).parent.mkdir(parents=True, exist_ok=True)
        self.path(CONF).write_text(text)

    def args(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def system(self, sshd="disabled", hostname="volla", allowed=True, polkit_own=True):
        w = self.world
        w.service("system", SYSTEMD, SYSTEMD_PATH, SYSTEMD_XML,
                  replies={"GetUnitFileState": sshd if isinstance(sshd, (dict, list)) else f"('{sshd}',)"})
        w.service("system", HOSTNAME, HOSTNAME_PATH, HOSTNAME_XML,
                  properties={"Hostname": f"'{hostname}'", "StaticHostname": f"'{hostname}'",
                              "PrettyHostname": "'Volla Phone'"})
        w.service("system", POLKIT, POLKIT_PATH, POLKIT_XML,
                  properties={"BackendName": "'js'", "BackendVersion": "'127'", "BackendFeatures": "uint32 1"},
                  replies={"CheckAuthorization": f"(({'true' if allowed else 'false'}, false, @a{{ss}} {{}}),)"},
                  own=polkit_own)


@pytest.fixture
def phone(program, gcc_world):
    return Phone(program, gcc_world)


VNC = {"type": "AdwNavigationPage", "tag": "plinius-vnc"}
SSH = {"type": "AdwNavigationPage", "tag": "plinius-ssh"}


def nav_rows(screen):
    """[(title, subtitle, On/Off)] of the overview"""
    out = []
    for r in screen.find(type="CcListRow"):
        labels = [n.get("text") for n in r.walk() if n.get("type") == "GtkLabel"]
        out.append((r["text"], r.get("subtitle"), labels[-1] if labels else None))
    return out


def vnc_rows(screen):
    return dict(screen.within(**VNC).rows())


def switch(screen, title, page=VNC):
    return screen.within(**page).one(type="AdwSwitchRow", text=title)


SHOW_VNC = [push("plinius-vnc"), wait_for(**VNC)]


# ----- The overview -----

def test_overview_says_what_is_on(program, gcc_world, phone):
    phone.system(sshd="enabled")
    phone.enabled(False)
    r = gcc_world.run(program, [call("static_init"), dump("overview")])
    s = r.dump("overview")
    assert s.one(type="CcDesktopVncPanel")["text"] == "Remote Access"
    assert nav_rows(s) == [
        ("Secure Shell", "Access this device using Secure Shell (SSH)", "On"),
        ("Desktop over VNC", "A VNC viewer on another computer gets a desktop with this phone's apps", "Off")]
    rows = s.find(type="CcListRow")
    assert [(r["action"], r["target"]) for r in rows] == [("navigation.push", "'plinius-ssh'"),
                                                           ("navigation.push", "'plinius-vnc'")]
    assert gcc_world.calls(SYSTEMD, "GetUnitFileState")[0]["args"] == ["sshd.service"]
    assert not r.warnings()


def test_overview_without_plinius_desktop(program, gcc_world, phone):
    phone.system()
    phone.tool.chmod(0o644)
    r = gcc_world.run(program, [dump("overview")])
    assert [row[0] for row in nav_rows(r.dump("overview"))] == ["Secure Shell"]


def test_overview_reads_the_socket_again_when_it_shows(program, gcc_world, phone):
    phone.system()
    phone.enabled(True)
    r = gcc_world.run(program, [dump("on"), {"op": "remove_file", "path": str(phone.link)}, *SHOW_VNC,
                                {"op": "back"}, wait_for(type="CcDesktopVncPanel"), dump("off")])
    assert nav_rows(r.dump("on"))[1][2] == "On"
    assert nav_rows(r.dump("off"))[1][2] == "Off"


# ----- Desktop over VNC -----

def test_vnc_page_while_off(program, gcc_world, phone):
    phone.system()
    r = gcc_world.run(program, [*SHOW_VNC, dump("page")])
    s = r.dump("page")
    assert s.one(**VNC)["text"] == "Desktop over VNC"
    assert switch(s, "Desktop over VNC")["active"] is False
    # Status and address only while on; the networks only usable then
    assert "Status" not in vnc_rows(s) and "Address" not in vnc_rows(s)
    nets = s.within(**VNC).one(type="AdwPreferencesGroup", text="Allowed Networks")
    assert nets.get("sensitive") is False
    assert nets["subtitle"] == ("Where connections are taken from. Never mobile data or the internet; allowing a "
                                "network asks for the PIN.")
    # Defaults as plinius-desktop-set has them: USB yes, local no
    assert [(n["text"], n["active"]) for n in Screen_rows(nets)] == [("USB Network", True), ("Local Networks", False)]
    taskbar = s.within(**VNC).one(type="AdwComboRow", text="Taskbar")
    assert taskbar["items"] == ["Top", "Bottom"] and taskbar["selected_text"] == "Top"
    assert taskbar["subtitle"] == "Apps button and open windows, at the top or bottom edge"


def Screen_rows(node):
    return [n for n in node.walk() if n.get("type") == "AdwSwitchRow"]


def test_vnc_page_while_waiting_for_a_connection(program, gcc_world, phone):
    phone.system()
    phone.enabled(True)
    phone.conf("usb=yes\nlocal = yes\n")
    phone.interfaces("lo=127.0.0.1;usb0=10.15.19.82;wlan0=192.168.1.20;ap0=10.42.0.1;eth0=192.168.2.3;"
                     "enx001122=192.168.3.4;rmnet0=100.64.1.2;wlan1=192.168.9.9,down;wlan2=v6;wlan3=-")
    r = gcc_world.run(program, [*SHOW_VNC, dump("page")])
    s = r.dump("page")
    rows = vnc_rows(s)
    assert rows["Status"] == "Waiting for a connection"
    assert rows["Address"] == ("10.15.19.82 (USB), 192.168.1.20 (Wi-Fi), 10.42.0.1 (Hotspot), 192.168.2.3 (Ethernet), "
                               "192.168.3.4 (Ethernet), port 5900")
    assert not s.within(**VNC).has(type="GtkButton", text="Disconnect")
    assert s.within(**VNC).one(type="AdwPreferencesGroup", text="Allowed Networks").get("sensitive") is not False
    status = s.within(**VNC).one(type="AdwActionRow", text="Status")
    assert "property" in status["classes"]


@pytest.mark.parametrize("conf, spec, address", [
    ("", "usb0=10.15.19.82;wlan0=192.168.1.20", "10.15.19.82 (USB), port 5900"),
    ("usb=no\nlocal=yes\n", "usb0=10.15.19.82;wlan0=192.168.1.20", "192.168.1.20 (Wi-Fi), port 5900"),
    ("usb=no\nusb=yes\nlocal=yesplease\nbroken\n", "usb0=10.15.19.82;wlan0=192.168.1.20",
     "10.15.19.82 (USB), 192.168.1.20 (Wi-Fi), port 5900"),
    ("usb=no\n", "usb0=10.15.19.82", "Not connected to an allowed network"),
    ("usb=maybe\n", "usb0=10.15.19.82", "10.15.19.82 (USB), port 5900"),
    ("", "!", "Not connected to an allowed network"),
])
def test_address_with_the_allowed_networks(program, gcc_world, phone, conf, spec, address):
    phone.system()
    phone.enabled(True)
    if conf:
        phone.conf(conf)
    phone.interfaces(spec)
    r = gcc_world.run(program, [*SHOW_VNC, dump("page")])
    assert vnc_rows(r.dump("page"))["Address"] == address


def test_connected_and_disconnect(program, gcc_world, phone):
    phone.system()
    phone.enabled(True)
    phone.connected("192.168.1.50")
    r = gcc_world.run(program, [*SHOW_VNC, dump("connected"), click(type="GtkButton", text="Disconnect"),
                                {"op": "remove_file", "path": str(phone.client)}, wait(2500), dump("gone")])
    assert vnc_rows(r.dump("connected"))["Status"] == "Connected: 192.168.1.50"
    assert phone.progs.calls("systemctl") == [["--user", "stop", "plinius-desktop.service"]]
    # The status follows every two seconds while the page shows
    assert vnc_rows(r.dump("gone"))["Status"] == "Waiting for a connection"
    assert not r.dump("gone").within(**VNC).has(type="GtkButton", text="Disconnect")
    assert not r.warnings()


def test_switch_enables_and_disables_the_socket(program, gcc_world, phone):
    phone.system()
    r = gcc_world.run(program, [*SHOW_VNC, set_active(True, type="AdwSwitchRow", text="Desktop over VNC"),
                                wait(800), dump("on"), set_active(False, type="AdwSwitchRow", text="Desktop over VNC"),
                                wait(800), dump("off"), {"op": "back"}, wait_for(type="CcDesktopVncPanel"),
                                dump("overview")])
    assert phone.progs.calls("systemctl") == [["--user", "enable", "--now", SOCKET],
                                              ["--user", "disable", "--now", SOCKET]]
    on = r.dump("on")
    assert switch(on, "Desktop over VNC")["active"] is True and switch(on, "Desktop over VNC").get("sensitive") is not False
    assert vnc_rows(on)["Status"] == "Waiting for a connection"
    assert switch(r.dump("off"), "Desktop over VNC")["active"] is False
    assert not phone.link.exists()
    assert nav_rows(r.dump("overview"))[1][2] == "Off"
    assert not r.warnings()


def test_switch_waits_for_systemctl(program, gcc_world, phone):
    phone.system()
    phone.progs.rule("systemctl", ["--user", "enable", "--now", SOCKET], first=True, sleep=1,
                     write={str(phone.link): ""})
    r = gcc_world.run(program, [*SHOW_VNC, set_active(True, type="AdwSwitchRow", text="Desktop over VNC"),
                                wait(300), dump("busy"), wait(1500), dump("done")])
    assert switch(r.dump("busy"), "Desktop over VNC").get("sensitive") is False
    assert switch(r.dump("done"), "Desktop over VNC").get("sensitive") is not False


def test_switch_that_systemctl_refuses_goes_back(program, gcc_world, phone):
    phone.system()
    phone.progs.rule("systemctl", ["--user", "enable", "--now", SOCKET], first=True, code=1)
    r = gcc_world.run(program, [*SHOW_VNC, set_active(True, type="AdwSwitchRow", text="Desktop over VNC"),
                                wait(800), dump("after")])
    assert switch(r.dump("after"), "Desktop over VNC")["active"] is False
    warnings = [m["message"] for m in r.warnings()]
    assert len(warnings) == 1 and warnings[0].startswith("systemctl failed: ")


def test_without_systemctl(program, gcc_world, phone):
    phone.system()
    (gcc_world.fake_bin.path / "systemctl").unlink()
    r = gcc_world.run(program, [*SHOW_VNC, set_active(True, type="AdwSwitchRow", text="Desktop over VNC"),
                                wait(300), dump("after")])
    assert switch(r.dump("after"), "Desktop over VNC")["active"] is False
    assert {m["message"].split(":")[0] for m in r.warnings()} == {"Cannot run systemctl"}


def test_networks_run_the_tool(program, gcc_world, phone):
    phone.system()
    phone.enabled(True)
    r = gcc_world.run(program, [*SHOW_VNC, set_active(True, type="AdwSwitchRow", text="Local Networks"), wait(600),
                                set_active(False, type="AdwSwitchRow", text="USB Network"), wait(600), dump("after")])
    assert phone.args() == ["allow local", "deny usb"]
    s = r.dump("after")
    assert switch(s, "Local Networks")["active"] is True and switch(s, "USB Network")["active"] is False
    assert vnc_rows(s)["Address"] == "192.168.1.20 (Wi-Fi), port 5900"
    assert not r.warnings()


def test_networks_wait_for_the_pin(program, gcc_world, phone):
    phone.system()
    phone.enabled(True)
    phone.progs.rule("pkexec", first=True, sleep=1, exec=True)
    r = gcc_world.run(program, [*SHOW_VNC, set_active(True, type="AdwSwitchRow", text="Local Networks"), wait(300),
                                dump("busy"), wait(1500), dump("done")])
    assert switch(r.dump("busy"), "USB Network").get("sensitive") is False
    assert switch(r.dump("busy"), "Local Networks").get("sensitive") is False
    assert switch(r.dump("done"), "USB Network").get("sensitive") is not False


@pytest.mark.parametrize("code, warned", [(126, False), (127, False), (2, True)])
def test_networks_refused_go_back(program, gcc_world, phone, code, warned):
    phone.system()
    phone.enabled(True)
    phone.code.write_text(str(code))
    r = gcc_world.run(program, [*SHOW_VNC, set_active(True, type="AdwSwitchRow", text="Local Networks"), wait(600),
                                dump("after")])
    assert switch(r.dump("after"), "Local Networks")["active"] is False
    warnings = [m["message"] for m in r.warnings()]
    assert warnings == ([f"{phone.tool} exited with {code}"] if warned else [])


def test_networks_without_pkexec(program, gcc_world, phone):
    phone.system()
    phone.enabled(True)
    (gcc_world.fake_bin.path / "pkexec").unlink()
    r = gcc_world.run(program, [*SHOW_VNC, set_active(True, type="AdwSwitchRow", text="Local Networks"), wait(300),
                                dump("after")])
    assert switch(r.dump("after"), "Local Networks")["active"] is False
    assert {m["message"].split(":")[0] for m in r.warnings()} == {f"Cannot run pkexec {phone.tool}"}


def test_taskbar_edge(program, gcc_world, phone):
    phone.system()
    r = gcc_world.run(program, [*SHOW_VNC, select(item="Bottom", type="AdwComboRow", text="Taskbar"),
                                {"op": "gsettings_get", "schema": "org.plinius.desktop", "key": "bar-position",
                                 "label": "after"},
                                {"op": "gsettings_set", "schema": "org.plinius.desktop", "key": "bar-position",
                                 "value": "'top'"}, dump("top")])
    assert r.value("after") == "'bottom'"
    assert r.dump("top").within(**VNC).one(type="AdwComboRow", text="Taskbar")["selected_text"] == "Top"


# ----- Secure Shell -----

def test_secure_shell_page(program, gcc_world, phone):
    phone.system(sshd="enabled", hostname="volla-f7")
    r = gcc_world.run(program, [push("plinius-ssh"), wait_for(**SSH), dump("page"),
                                click(type="GtkButton", tooltip="Copy"), call("clipboard"), dump("copied")])
    s = r.dump("page")
    assert s.one(**SSH)["text"] == "Secure Shell"
    row = switch(s, "Secure Shell", SSH)
    assert row["active"] is True and row["subtitle"] == "Access this device using Secure Shell (SSH)"
    command = s.within(**SSH).one(type="AdwActionRow", text="SSH Login Command")
    assert command["subtitle"] == "ssh volla-f7"
    assert r.value("clipboard") == "ssh volla-f7"
    assert "Command copied to clipboard" in r.dump("copied").texts()


@pytest.mark.parametrize("allowed, state", [(True, "disabled"), (False, "disabled")])
def test_secure_shell_switch(program, gcc_world, phone, allowed, state):
    phone.system(sshd=state, allowed=allowed)
    r = gcc_world.run(program, [push("plinius-ssh"), wait_for(**SSH),
                                set_active(True, type="AdwSwitchRow", text="Secure Shell"), wait(800), dump("after"),
                                {"op": "back"}, wait_for(type="CcDesktopVncPanel"), dump("overview")])
    started = [c["args"] for c in gcc_world.calls(SYSTEMD, "StartUnit")]
    enabled = [c["args"] for c in gcc_world.calls(SYSTEMD, "EnableUnitFiles")]
    if allowed:
        assert started == [["sshd.service", "replace"]] and enabled == [[["sshd.service"], False, False]]
        assert nav_rows(r.dump("overview"))[0][2] == "On"
    else:
        assert started == [] and enabled == []
        # Read again from systemd: still off
        assert switch(r.dump("after"), "Secure Shell", SSH)["active"] is False
        # GNOME's warning when polkit says no
        assert r.warnings()[0]["message"].startswith(
            "Cannot acquire 'org.gnome.controlcenter.remote-login-helper' permission: ")


def test_secure_shell_switched_off(program, gcc_world, phone):
    phone.system(sshd="enabled")
    gcc_world.run(program, [push("plinius-ssh"), wait_for(**SSH),
                            set_active(False, type="AdwSwitchRow", text="Secure Shell"), wait(800)])
    assert [c["args"] for c in gcc_world.calls(SYSTEMD, "StopUnit")] == [["sshd.service", "replace"]]
    assert [c["args"] for c in gcc_world.calls(SYSTEMD, "DisableUnitFiles")] == [[["sshd.service"], False]]


def test_secure_shell_networks_follow_the_switch(program, gcc_world, phone):
    phone.system(sshd="disabled")
    tool = phone.path(SSH_TOOL)
    tool.parent.mkdir(parents=True)
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(0o755)
    r = gcc_world.run(program, [push("plinius-ssh"), wait_for(**SSH), dump("page")])
    group = r.dump("page").within(**SSH).one(type="AdwPreferencesGroup", text="Allowed Networks")
    assert group.get("sensitive") is False
    assert [n["text"] for n in Screen_rows(group)] == ["USB Network", "Local Networks", "All Networks"]


def test_secure_shell_without_hostnamed_or_systemd(program, gcc_world, phone):
    # Nothing on the system bus: GNOME's functions say why
    r = gcc_world.run(program, [push("plinius-ssh"), wait_for(**SSH), dump("page")], system_bus=False)
    s = r.dump("page")
    assert switch(s, "Secure Shell", SSH)["active"] is False
    messages = [m["message"] for m in r.messages]
    assert any(m.startswith("Couldn't connect to hostnamed") for m in messages)
    assert any(m.startswith("Failed connecting to D-Bus system bus") for m in messages)


def test_no_taskbar_row_with_a_plinius_desktop_without_it(program, gcc_world, phone, tmp_path):
    import subprocess
    from pathlib import Path
    schemas = tmp_path / "schemas"
    schemas.mkdir()
    # GNOME's own schemas only: no org.plinius.desktop
    for f in Path("/usr/share/glib-2.0/schemas").glob("org.gnome.desktop.*.xml"):
        shutil.copy(f, schemas / f.name)
    subprocess.run(["glib-compile-schemas", str(schemas)], check=True, capture_output=True)
    gcc_world.extra_env["GSETTINGS_SCHEMA_DIR"] = str(schemas)
    phone.system()
    r = gcc_world.run(program, [*SHOW_VNC, dump("page")])
    assert not r.dump("page").within(**VNC).has(type="AdwComboRow", text="Taskbar")
    assert switch(r.dump("page"), "Desktop over VNC")
