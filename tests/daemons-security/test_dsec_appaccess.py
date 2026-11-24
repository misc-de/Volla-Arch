# plinius-appaccess: per app, whether it may use a USB camera, a security key and the network
# (cgroup BPF), the questions "allow once / always / never" and who may change what.
# plinius-appaccessd.c runs through c/dsec_appaccess_harness.c as a copy whose system paths are
# relative: the harness runs in tmp_path, which stands for / (a cgroup tree of plain
# directories, logind's user files, /proc entries of the callers, sysfs device classes, the
# state file). The BPF object, maps and ring buffer, the bus and the clocks are fakes; nothing
# is loaded into a kernel and nothing reaches a bus.
# plinius-appaccess-agent.c runs through c/dsec_appaccess_agent_harness.c against a recorded
# session bus (Phosh's access dialog, notifications) and system bus (the daemon); its count of
# the camera portal's refusals and its questions also on two private dbus-daemons
# (tests/daemons-hw/dhw_daemon_bus.py) with a fake portal, a fake Phosh, a fake
# plinius-appaccessd and a test connection as the app.

import ast
import configparser
import json
import re
import shutil
import stat
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from conftest import GCOV_DIR, PKG, REPO, load_script
from dsec_helpers import CDIR, build, san_env

APPACCESS = PKG / "plinius-appaccess"
DAEMON = APPACCESS / "plinius-appaccessd.c"
AGENT = APPACCESS / "plinius-appaccess-agent.c"

# Every path of the daemon into the running system, made relative to the harness's directory
RELATIVE = [
    '"/usr/lib/plinius-appaccess/appaccess.bpf.o"',
    '"/var/lib/plinius-appaccess/apps.ini"',
    '"/etc/plinius-appaccess/off"',
    '"/sys/fs/cgroup"',
    '"/run/systemd/users"',
    '"/sys/class/video4linux"',
    '"/sys/bus/media/devices"',
    '"/sys/class/hidraw"',
    '"/proc/%u/exe"',
    '"/proc/%u/cgroup"',
    '"/dev"',
    '"/run/plinius-appaccess"',
]
# Left as they are: compared with a caller's executable, never opened; the harness's own fds
KEPT = {"/usr/bin/gnome-control-center", "/usr/lib/plinius-appaccess/plinius-appaccess-agent",
        "/usr/bin/camera-hal-bridge", "/proc/self/fd/%d"}
SYSTEM = ("/sys", "/dev", "/proc", "/run", "/var", "/etc", "/usr", "/home", "/tmp", "/root",
          "/boot", "/lib", "/bin", "/opt", "/mnt", "/media", "/srv")
LITERAL = re.compile(r'"((?:[^"\\\n]|\\.)*)"')

DENIED = "error g-dbus-error-quark:9: Not allowed"
INVALID = "error g-dbus-error-quark:16: "

GCC = "/usr/bin/gnome-control-center"
AGENT_EXE = "/usr/lib/plinius-appaccess/plinius-appaccess-agent"
BRIDGE = "/usr/bin/camera-hal-bridge"

U1000 = "/user.slice/user-1000.slice/user@1000.service"
U1001 = "/user.slice/user-1001.slice/user@1001.service"
SLICE = U1000 + "/app.slice"
SLICE1 = U1001 + "/app.slice"
SNAP = SLICE + "/app-gnome-org.gnome.Snapshot-1234.scope"
SNAP1 = SLICE1 + "/app-gnome-org.gnome.Snapshot-99.scope"
FOX = SLICE + "/app-flatpak-org.mozilla.firefox-77.scope"
PORTAL = SLICE + "/xdg-desktop-portal.service"
SNAPSHOT = "org.gnome.Snapshot"
FIREFOX = "org.mozilla.firefox"
MONO, UNIX = 5000, 1800000000


def relative_copy(src, out_dir):
    """The daemon's source with its system paths relative; refuses a copy that still names one
    (other than the KEPT strings). Same line count, so gcov counts the lines for the original."""
    text = src.read_text()
    for lit in RELATIVE:
        assert lit in text, f"{lit} not in {src}: the source changed, update the test"
        text = text.replace(lit, '"' + lit[2:])
    for m in LITERAL.finditer(text):
        lit = m.group(1)
        if lit.startswith(SYSTEM) and lit not in KEPT:
            raise AssertionError(f"{src}: system path {lit!r} left in the test copy")
    assert text.count("\n") == src.read_text().count("\n")
    dst = Path(out_dir) / src.name
    dst.write_text(f'#line 1 "{src.resolve()}"\n' + text)
    return dst


@pytest.fixture(scope="module")
def daemon(tmp_path_factory):
    d = tmp_path_factory.mktemp("appaccessd")
    src = relative_copy(DAEMON, d)
    return build(d, "appaccessd-harness", [CDIR / "dsec_appaccess_harness.c"],
                 flags=[f"-I{APPACCESS}", "-Wno-unused-parameter", f'-DAA_DAEMON_SOURCE="{src}"'],
                 pkgs=["gio-unix-2.0", "libbpf"])


def harness_run(binary, cwd, script="", args=(), timeout=30, check=True, **env):
    r = subprocess.run([str(binary), *args], input=script, capture_output=True, text=True,
                       cwd=cwd, timeout=timeout * (5 if GCOV_DIR else 1), env=san_env(**env))
    if check:
        assert r.returncode == 0, f"harness exit {r.returncode}\n{r.stderr[-4000:]}"
    return r


class Phone:
    """tmp_path as the phone's /: a cgroup tree, logind's users, /proc of the callers"""

    def __init__(self, binary, root):
        self.binary = binary
        self.root = root
        self.cgroot = root / "sys/fs/cgroup"
        (self.cgroot / "user.slice").mkdir(parents=True)
        (root / "run/systemd/users").mkdir(parents=True)
        (root / "var/lib/plinius-appaccess").mkdir(parents=True)
        (root / "run/plinius-appaccess").mkdir(parents=True)
        (root / "proc").mkdir()
        self.state = root / "var/lib/plinius-appaccess/apps.ini"
        self.stderr = ""
        # The default caller of dsec_fakebus.h: uid 1001, in wheel (998), signed in, running
        # GNOME Settings as process 4242
        self.user(1000)
        self.user(1001)
        self.exe(4242, GCC)

    def cgroup(self, *paths):
        for p in paths:
            (self.cgroot / p.lstrip("/")).mkdir(parents=True, exist_ok=True)
        return self

    def ino(self, path):
        return (self.cgroot / path.lstrip("/")).stat().st_ino

    def user(self, uid, state="active"):
        (self.root / f"run/systemd/users/{uid}").write_text(
            f"# This is private data. Do not parse.\nNAME=u{uid}\nSTATE={state}\nRUNTIME=/run/user/{uid}\n")

    def exe(self, pid, target):
        d = self.root / f"proc/{pid}"
        d.mkdir(exist_ok=True)
        link = d / "exe"
        if link.is_symlink():
            link.unlink()
        link.symlink_to(target)

    def proc_cgroup(self, pid, text):
        d = self.root / f"proc/{pid}"
        d.mkdir(exist_ok=True)
        (d / "cgroup").write_text(text)

    def run(self, *cmds, **env):
        r = harness_run(self.binary, self.root, "\n".join(cmds) + "\n", **env)
        self.stderr = r.stderr
        out = r.stdout.splitlines()
        assert len(out) == len(cmds), out
        return out

    def last(self, *cmds, **env):
        return self.run(*cmds, **env)[-1]

    def main(self, *args, check=True, **env):
        r = harness_run(self.binary, self.root, "", args=("main", *args), **env)
        self.stderr = r.stderr
        return r.stdout.splitlines()


@pytest.fixture
def phone(daemon, tmp_path):
    return Phone(daemon, tmp_path)




def value(line):
    """The GVariant of an "ok ..." answer as Python values"""
    assert line.startswith("ok "), line
    return ast.literal_eval(re.sub(r"@[a-z(){}]+ ", "", line[3:]))


def cgroups(line):
    """'cgroups' output: {path: {app, uid, map, once}}"""
    if line == "-":
        return {}
    out = {}
    for entry in line.split(" | "):
        path, *fields = entry.split(" ")
        out[path] = dict(f.split("=", 1) for f in fields)
    return out


def app_rows(line):
    """'app APP' output: {resource: {policy, set, allowed, denied, last}}"""
    if line == "-":
        return None
    out = {}
    for entry in line.split(" | "):
        res, *fields = entry.split(" ")
        out[res] = dict(f.split("=", 1) for f in fields)
    return out


def verdicts(phone, *cmds):
    """The verdicts in the BPF map per cgroup path after the commands"""
    return {p: c["map"] for p, c in cgroups(phone.last(*cmds, "cgroups")).items()}


def keyfile(path):
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str
    cp.read(path)
    return {s: dict(cp[s]) for s in cp.sections()}


# ---- Application ids from cgroup names ----

@pytest.mark.parametrize("leaf,app", [
    ("app-gnome-org.gnome.Snapshot-1234.scope", "org.gnome.Snapshot"),
    ("app-flatpak-org.mozilla.firefox-77.scope", "org.mozilla.firefox"),
    ("app-org.gnome.Maps-5.scope", "org.gnome.Maps"),
    ("app-gnome-firefox-123.scope", "firefox"),
    ("app-firefox-123.scope", "firefox"),
    ("app-org.gnome.Weather@x1.service", "org.gnome.Weather"),
    ("app-org.gnome.Weather.service", "org.gnome.Weather"),
    ("app-gnome-org.gnome.Calls@autostart.service", "org.gnome.Calls"),
    ("dbus-:1.2-org.gnome.Weather@0.service", "org.gnome.Weather"),
    ("dbus-:1.2-org.gnome.Weather.service", "org.gnome.Weather"),
    # systemd escapes a dash inside the application id
    ("app-gnome-org.example.App\\x2dbeta-12.scope", "org.example.App-beta"),
    ("app-org.example.App\\x2dbeta-12.scope", "org.example.App-beta"),
    ("app-gnome-firefox\\x2desr-1234.scope", "firefox-esr"),
    # … also without a launcher: the name is split before it is unescaped, so the escaped dash
    # is no separator
    ("app-firefox\\x2desr-1234.scope", "firefox-esr"),
    ("app-firefox\\x2desr@x1.service", "firefox-esr"),
    ("app-firefox\\x2desr.service", "firefox-esr"),
    ("dbus-:1.2-org.example.App\\x2dbeta@0.service", "org.example.App-beta"),
    # Not an app: slices, user services, sessions, broken names
    ("app.slice", None),
    ("app-gnome.slice", None),
    ("xdg-desktop-portal.service", None),
    ("session-2.scope", None),
    ("dbus-:1.2.service", None),
    ("dbus-:1.2-.service", None),
    ("dbus-:1.2-bad\\x2fname@0.service", None),
    ("app-.scope", None),
    ("app-.service", None),
    ("app-gnome-a\\x2fb-1.scope", None),         # an escaped slash
    ("app-gnome-a b-1.scope", None),
    ("app-gnome-a\\x00b-1.scope", None),          # an escaped NUL ends the name
    ("app-gnome-" + "a" * 256 + "-1.scope", None),
    ("app-gnome-\\x4-1.scope", None),             # a broken escape stays: a backslash
])
def test_app_from_cgroup_name(phone, leaf, app):
    assert phone.last(f"leaf {leaf}") == (app or "-")


@pytest.mark.parametrize("app,ok", [
    ("org.gnome.Snapshot", True),
    ("a", True),
    ("A_b-c.9", True),
    ("a" * 255, True),
    ("a" * 256, False),
    ("", False),
    ("../etc", False),
    ("a/b", False),
    ("a b", False),
    ("a]b", False),                 # would end a group of the state file
    ("a=b", False),
    ("appé", False),
])
def test_application_id_check(phone, app, ok):
    assert phone.last(f"valid {app}") == ("yes" if ok else "no")


@pytest.mark.parametrize("name,res", [
    ("camera", 0), ("security-key", 1), ("network", 2), ("microphone", 3), ("Camera", -1), ("", -1),
    ("speaker", -1),
])
def test_resource_names(phone, name, res):
    assert phone.last(f"res {name}") == str(res)


def test_cgroup_level(phone):
    assert phone.run("level /user.slice", f"level {SLICE}", f"level {SNAP}") == ["1", "4", "5"]


# ---- The cgroup tree and the verdicts the programs see ----

def test_scan_writes_a_verdict_for_every_app_and_service(phone):
    maps = SLICE + "/dbus-:1.5-org.gnome.Maps@0.service"
    phone.cgroup(SNAP, FOX, PORTAL, maps,
                 U1000 + "/init.scope",                        # the user manager itself
                 U1000 + "/session.slice/pipewire.service",    # not below app.slice
                 "/user.slice/user-1000.slice/session-2.scope")  # the login session
    cg = cgroups(phone.last("scan /user.slice", "cgroups"))
    assert cg == {
        # Defaults: cameras and keys ask, the network is allowed
        SNAP: {"app": SNAPSHOT, "uid": "1000", "map": "3,3,1", "once": "0,0,0"},
        FOX: {"app": FIREFOX, "uid": "1000", "map": "3,3,1", "once": "0,0,0"},
        maps: {"app": "org.gnome.Maps", "uid": "1000", "map": "3,3,1", "once": "0,0,0"},
        # A user service (portal, camera-hal-bridge …) is always allowed
        PORTAL: {"app": "-", "uid": "1000", "map": "1,1,1", "once": "0,0,0"},
    }


def test_policies_of_the_state_file_decide_the_verdicts(phone):
    phone.state.write_text(
        "[org.gnome.Snapshot]\ncamera=allow\nsecurity-key=deny\nnetwork=deny\n"
        "[org.mozilla.firefox]\ncamera=ALLOW\nnetwork=sometimes\n"   # unknown words: the defaults
        "[bad app]\ncamera=allow\n[a/b]\ncamera=allow\n")
    phone.cgroup(SNAP, FOX)
    out = phone.run("load", "scan /user.slice", "cgroups", "apps")
    assert {p: c["map"] for p, c in cgroups(out[2]).items()} == {SNAP: "1,2,2", FOX: "3,3,1"}
    assert out[3] == f"{SNAPSHOT} {FIREFOX}"


def test_programs_are_attached_only_after_the_first_scan(phone):
    phone.cgroup(SNAP)
    out = phone.run("scan /user.slice", "links", "attach", "links")
    # Until every cgroup has its verdict, nothing is attached (an app would meet no verdict)
    assert out[1] == "to_attach 1"
    assert out[3] == f"{SLICE} 2 | to_attach 0"


def test_a_new_session_is_attached_at_once_and_only_once(phone):
    phone.cgroup(SNAP)
    out = phone.run("scan /user.slice", "attach", f"mkdir sys/fs/cgroup{SNAP1}",
                    "scan /user.slice/user-1001.slice", "links", "scan /user.slice", "links", "cgroups")
    assert out[4] == out[6] == f"{SLICE} 2 | {SLICE1} 2 | to_attach 0"
    assert set(cgroups(out[7])) == {SNAP, SNAP1}


def test_logout_detaches_the_programs(phone):
    phone.cgroup(SNAP, PORTAL)
    out = phone.run("scan /user.slice", "attach", f"remove {SNAP}", f"remove {PORTAL}", f"remove {SLICE}",
                    "links", "destroyed", "cgroups", "maps")
    assert out[5:] == ["to_attach 0", "2", "-", "devices=0 cgroups=0 counts=0"]


def test_logout_before_the_apps_were_reported_gone_still_detaches(phone):
    phone.cgroup(SNAP)
    out = phone.run("scan /user.slice", "attach", f"count {SNAP} camera 1 0 4999 0", f"remove {SLICE}",
                    "links", "destroyed", f"remove {SNAP}", "maps")
    assert out[4:6] == ["to_attach 0", "2"]
    # The app's path is forgotten with its app.slice: removing it later is no error
    assert out[6] == "ok"


def test_without_inotify_the_scan_still_writes_verdicts(phone):
    phone.cgroup(SNAP)
    assert verdicts(phone, "noinotify", "scan /user.slice") == {SNAP: "3,3,1"}
    assert "Cannot watch sys/fs/cgroup/user.slice" in phone.stderr


def test_one_program_failing_to_attach_leaves_the_other(phone):
    phone.cgroup(SNAP)
    out = phone.run("bpf fail attach:appaccess_sock_create", "scan /user.slice", "attach", "links")
    assert out[3] == f"{SLICE} 1 | to_attach 0"
    assert "Cannot attach appaccess_sock_create to sys/fs/cgroup" + SLICE in phone.stderr


def test_an_app_slice_gone_before_the_attach_is_skipped(phone):
    phone.cgroup(SLICE)
    out = phone.run("scan /user.slice", f"rmdir sys/fs/cgroup{SLICE}", "attach", "links")
    assert out[2:] == ["ok", "to_attach 0"]
    assert "Cannot open sys/fs/cgroup" + SLICE in phone.stderr


def test_attach_without_a_session(phone):
    assert phone.run("scan /user.slice", "attach", "links") == ["ok", "ok", "to_attach 0"]
    assert "No session yet" in phone.stderr


@pytest.mark.parametrize("fail", ["map:events", "ringbuf"])
def test_attach_fails_without_the_ring_buffer(phone, fail):
    assert phone.run(f"bpf fail {fail}", "attach") == ["ok", "fail"]
    assert "Cannot open the ring buffer" in phone.stderr


def test_a_verdict_that_cannot_be_written_is_reported(phone):
    phone.cgroup(SNAP)
    assert verdicts(phone, "bpf fail update", "scan /user.slice") == {SNAP: "-"}
    assert "Cannot write the verdict of " + SNAP in phone.stderr


def test_cgroup_add_takes_only_directories_below_app_slice_once(phone):
    phone.cgroup(SNAP)
    (phone.cgroot / SLICE.lstrip("/") / "cgroup.procs").write_text("")
    out = phone.run(f"add {SNAP}", f"add {SNAP}", f"add {SLICE}/cgroup.procs", f"add {SLICE}/missing.scope",
                    "cgroups")
    assert list(cgroups(out[-1])) == [SNAP]


def test_cgroup_outside_a_user_manager_has_no_uid(phone):
    odd = "/user.slice/x.slice/y.service/app.slice/app-gnome-org.example.A-1.scope"
    phone.cgroup(odd)
    cg = cgroups(phone.last(f"scan {odd}", "cgroups"))
    assert cg[odd]["uid"] == "-1" and cg[odd]["app"] == "org.example.A"


def test_a_cgroup_below_an_app_scope_belongs_to_the_app(phone):
    # With Delegate= the user owns the scope's tree and can make cgroups in it: they are the
    # app's, whatever they are called, not user services (which get ALLOW for everything)
    worker = SNAP + "/worker"
    disguised = SNAP + "/app.slice/app-gnome-org.example.Other-1.scope"
    under_portal = PORTAL + "/app-gnome-org.example.Other-2.scope"
    phone.cgroup(worker, disguised, under_portal)
    cg = cgroups(phone.last("scan /user.slice", "cgroups"))
    assert cg[worker] == {"app": SNAPSHOT, "uid": "1000", "map": "3,3,1", "once": "0,0,0"}
    assert cg[disguised]["app"] == SNAPSHOT
    # Below a user service nothing becomes an app either
    assert cg[under_portal] == {"app": "-", "uid": "1000", "map": "1,1,1", "once": "0,0,0"}


# An app started by D-Bus activation (DBusActivatable=true), as the phone has it: systemd puts
# the instances of the template unit dbus-:1.2-org.gnome.TextEditor@.service into a slice
TEXT_SLICE = SLICE + "/app-dbus\\x2d:1.2\\x2dorg.gnome.TextEditor.slice"
TEXT = TEXT_SLICE + "/dbus-:1.2-org.gnome.TextEditor@0.service"
TEXT_EDITOR = "org.gnome.TextEditor"


def test_an_app_started_by_dbus_activation_gets_its_policy(phone):
    phone.state.write_text(f"[{TEXT_EDITOR}]\ncamera=deny\nsecurity-key=deny\nnetwork=deny\n")
    second = TEXT_SLICE + "/dbus-:1.2-org.gnome.TextEditor@1.service"
    worker = TEXT + "/worker"                    # Delegate=: the app's own tree
    disguised = TEXT + "/xdg-desktop-portal.service"
    phone.cgroup(second, worker, disguised)
    cg = cgroups(phone.last("load", "scan /user.slice", "cgroups"))
    for path in (TEXT, second, worker, disguised):
        assert cg[path] == {"app": TEXT_EDITOR, "uid": "1000", "map": "2,2,2", "once": "0,0,0"}, path
    # The slice itself is no app and no user service: no process of systemd's runs there, one
    # moved there gets what an app gets before it is asked, without a question
    assert cg[TEXT_SLICE] == {"app": "-", "uid": "1000", "map": "2,2,1", "once": "0,0,0"}


def test_an_app_in_a_slice_asks_and_its_answer_reaches_it(phone):
    phone.cgroup(TEXT)
    out = phone.run("scan /user.slice", f"event {phone.ino(TEXT)} camera 1 3", "signals",
                    "call SetPolicy ('org.gnome.TextEditor', 'camera', uint32 1)", "cgroups",
                    f"event {phone.ino(TEXT_SLICE)} camera 1 2", "signals")
    assert signals(out[2]) == [denied(TEXT_EDITOR, "camera")]
    cg = cgroups(out[4])
    assert cg[TEXT]["map"] == "1,3,1" and cg[TEXT_SLICE]["map"] == "2,2,1"
    # A refusal in the slice asks nobody: there is no app to ask for
    assert signals(out[6]) == [changed(TEXT_EDITOR)]


def test_user_services_in_slices_stay_user_services(phone):
    # Instances of a user service's template run in a slice too; a slice in a slice
    session = SLICE + "/app-gnome\\x2dsession\\x2dmanager.slice/gnome-session-manager@phosh.service"
    nested = SLICE + "/a.slice/b.slice/xdg-desktop-portal.service"
    app = SLICE + "/a.slice/b.slice/app-gnome-org.gnome.Maps-5.scope/sub.slice"
    phone.cgroup(session, nested, app)
    cg = cgroups(phone.last("scan /user.slice", "cgroups"))
    assert cg[session] == {"app": "-", "uid": "1000", "map": "1,1,1", "once": "0,0,0"}
    assert cg[nested]["app"] == "-" and cg[nested]["map"] == "1,1,1"
    # The first unit that is no slice decides; a "slice" the app made below it is the app's
    assert cg[app]["app"] == "org.gnome.Maps" and cg[app]["map"] == "3,3,1"
    for s in (SLICE + "/app-gnome\\x2dsession\\x2dmanager.slice", SLICE + "/a.slice", SLICE + "/a.slice/b.slice"):
        assert cg[s]["app"] == "-" and cg[s]["map"] == "2,2,1", s


def test_an_app_in_a_slice_appears_with_its_cgroups(phone):
    # inotify reports the app's slice, the scan finds the unit in it; a cgroup the app makes
    # two levels below its unit is still seen, as below an app's scope
    phone.cgroup(SNAP)
    a, b = TEXT + "/a", TEXT + "/a/b"
    out = phone.run("scan /user.slice", "attach", f"mkdir sys/fs/cgroup{TEXT}", "inotify",
                    f"mkdir sys/fs/cgroup{a}", "inotify", f"mkdir sys/fs/cgroup{b}", "inotify", "cgroups",
                    f"rmdir sys/fs/cgroup{b}", "inotify", "cgroups")
    cg = cgroups(out[8])
    assert {p: c["app"] for p, c in cg.items()} == {
        SNAP: SNAPSHOT, TEXT_SLICE: "-", TEXT: TEXT_EDITOR, a: TEXT_EDITOR, b: TEXT_EDITOR}
    assert b not in cgroups(out[11])


def test_the_process_of_an_app_in_a_slice_is_the_apps(phone):
    phone.cgroup(TEXT)
    phone.proc_cgroup(5000, f"0::{TEXT}\n")
    phone.proc_cgroup(5001, f"0::{TEXT_SLICE}\n")
    assert phone.run("pidapp 5000", "pidapp 5001") == [TEXT_EDITOR, "-"]


def test_an_app_slice_deeper_down_holds_no_apps(phone):
    # The programs run only below the user manager's own app.slice (level 4); an app.slice the
    # user made deeper in their tree names no app (and no program runs there to ask for it)
    odd = U1000 + "/session.slice/app.slice/app-gnome-org.gnome.Snapshot-1.scope"
    phone.cgroup(odd)
    out = phone.run("scan /user.slice", "attach", "cgroups", "links")
    cg = cgroups(out[2])
    assert cg[odd]["app"] == "-" and cg[odd]["map"] == "1,1,1"
    assert out[3] == "to_attach 0"


def test_a_policy_reaches_the_cgroups_below_an_app_scope(phone):
    worker = SNAP + "/worker"
    phone.cgroup(worker)
    assert verdicts(phone, "scan /user.slice", "call SetPolicy ('org.gnome.Snapshot', 'camera', uint32 2)") == {
        SNAP: "2,3,1", worker: "2,3,1"}


def test_a_cgroup_made_later_in_an_instance_allowed_once_is_allowed_too(phone):
    phone.cgroup(SNAP1)
    worker, other = SNAP1 + "/worker", SLICE1 + "/app-gnome-org.gnome.Maps-6.scope/worker"
    out = phone.run("scan /user.slice", CALLS["AllowOnce"], f"mkdir sys/fs/cgroup{worker}", f"scan {worker}",
                    f"mkdir sys/fs/cgroup{other}", f"scan {SLICE1}", "cgroups")
    cg = cgroups(out[-1])
    assert (cg[worker]["map"], cg[worker]["once"]) == ("1,3,1", "1,0,0")
    # Another app's instance does not get Snapshot's answer
    assert (cg[other]["app"], cg[other]["map"]) == ("org.gnome.Maps", "3,3,1")


def test_an_event_of_a_deep_cgroup_gets_the_apps_question(phone):
    # Deeper than the daemon watches: the programs report the unknown cgroup, the daemon adds it
    deep = SNAP + "/a/b/c"
    phone.cgroup(SNAP, deep)
    out = phone.run(f"scan {SNAP}", f"event {phone.ino(deep)} camera 2 0", "signals", "cgroups")
    assert cgroups(out[3])[deep]["map"] == "3,3,1"


def test_removing_a_cgroup_counts_its_last_attempts_and_drops_its_verdict(phone):
    phone.cgroup(SNAP, FOX)
    out = phone.run("scan /user.slice", f"count {SNAP} camera 2 1 4990 4995", f"remove {SNAP}", "cgroups",
                    "maps", f"app {SNAPSHOT}", f"remove {SLICE}/unknown.scope", "cgroups")
    assert list(cgroups(out[3])) == [FOX]
    assert out[4] == "devices=0 cgroups=1 counts=0"
    assert app_rows(out[5])["camera"] == {"policy": "0", "set": "0", "allowed": "2", "denied": "1",
                                          "last": f"{UNIX - 10},{UNIX - 5}"}
    assert list(cgroups(out[7])) == [FOX]


# ---- The attempts the programs counted ----

def test_harvest_adds_the_counts_and_keeps_the_latest_times(phone):
    phone.cgroup(SNAP)
    out = phone.run("scan /user.slice",
                    f"count {SNAP} camera 3 2 4990 4995", "harvest", "maps",
                    f"count {SNAP} camera 1 0 4980 0", "harvest", f"app {SNAPSHOT}")
    assert out[3] == "devices=0 cgroups=1 counts=0"     # taken out of the map
    rows = app_rows(out[-1])
    # Monotonic 4990 s with the monotonic clock at 5000 s: ten seconds ago
    assert rows["camera"] == {"policy": "0", "set": "0", "allowed": "4", "denied": "2",
                              "last": f"{UNIX - 10},{UNIX - 5}"}
    assert rows["security-key"]["allowed"] == "0"


def test_a_count_without_a_time_leaves_the_time(phone):
    phone.cgroup(SNAP)
    rows = app_rows(phone.last("scan /user.slice", f"count {SNAP} camera 1 1 0 0", "harvest", f"app {SNAPSHOT}"))
    # Not the boot time (monotonic 0), and not now
    assert rows["camera"] == {"policy": "0", "set": "0", "allowed": "1", "denied": "1", "last": "0,0"}


def test_harvest_of_one_cgroup_leaves_the_others(phone):
    phone.cgroup(SNAP, FOX)
    out = phone.run("scan /user.slice", f"count {SNAP} camera 1 0 4999 0",
                    f"count {FOX} security-key 0 4 0 4999", f"harvest {SNAP}", "maps", f"app {FIREFOX}",
                    f"app {SNAPSHOT}")
    assert out[4] == "devices=0 cgroups=2 counts=1"
    assert app_rows(out[5])["security-key"]["denied"] == "0"
    assert app_rows(out[6])["camera"]["allowed"] == "1"


def test_harvest_drops_counts_no_app_owns(phone):
    phone.cgroup(SNAP, PORTAL)
    out = phone.run("scan /user.slice",
                    "countid 777 camera 5 5 4999 4999",          # a cgroup the daemon does not know
                    f"count {PORTAL} camera 5 0 4999 0",          # a user service
                    f"count {SNAP} 7 5 5 4999 4999",              # no such resource
                    "harvest", "maps", f"app {SNAPSHOT}", "apps")
    assert out[5] == "devices=0 cgroups=2 counts=0"
    assert all(r["allowed"] == "0" and r["denied"] == "0" for r in app_rows(out[6]).values())
    assert out[7] == SNAPSHOT


def test_a_count_that_cannot_be_read_waits_for_the_next_harvest(phone):
    phone.cgroup(SNAP)
    out = phone.run("scan /user.slice", f"count {SNAP} camera 1 0 4999 0", "bpf fail lookup", "harvest",
                    "maps", "bpf fail -", "harvest", f"app {SNAPSHOT}")
    assert out[4] == "devices=0 cgroups=1 counts=1"
    assert app_rows(out[7])["camera"]["allowed"] == "1"


def test_timers_harvest_and_save_only_when_something_changed(phone):
    phone.cgroup(SNAP)
    out = phone.run("scan /user.slice", "timer save", f"count {SNAP} camera 1 0 4999 0", "timer harvest",
                    "timer save")
    assert out[1:] == ["dirty 0", "ok", "dirty 1", "dirty 0"]
    assert keyfile(phone.state) == {SNAPSHOT: {"camera-allowed": "1", "camera-last-allowed": str(UNIX - 1)}}


# ---- The state file ----

def test_state_file_round_trip(phone):
    phone.state.write_text(
        "[org.gnome.Snapshot]\ncamera=allow\ncamera-allowed=4\ncamera-last-allowed=1799990000\n"
        "security-key-denied=2\nsecurity-key-last-denied=1799990001\n"
        "[org.mozilla.firefox]\nnetwork=deny\n")
    phone.run("load", "save")
    assert keyfile(phone.state) == {
        SNAPSHOT: {"camera": "allow", "camera-allowed": "4", "camera-last-allowed": "1799990000",
                   "security-key-denied": "2", "security-key-last-denied": "1799990001"},
        FIREFOX: {"network": "deny"},
    }


def test_a_new_state_file_is_only_for_root(phone):
    assert phone.run("call SetPolicy ('org.gnome.Snapshot', 'camera', uint32 2)") == ["ok"]
    assert stat.S_IMODE(phone.state.stat().st_mode) == 0o600
    assert not [f for f in phone.state.parent.iterdir() if f != phone.state]   # no temporary file left


def test_state_file_values_that_do_not_parse_count_as_nothing(phone):
    phone.state.write_text("[org.gnome.Snapshot]\ncamera-allowed=lots\ncamera-last-allowed=x\n"
                           "camera=\nnetwork=deny\n")
    rows = app_rows(phone.last("load", f"app {SNAPSHOT}"))
    assert rows["camera"] == {"policy": "0", "set": "0", "allowed": "0", "denied": "0", "last": "0,0"}
    assert rows["network"]["policy"] == "2" and rows["network"]["set"] == "1"


@pytest.mark.parametrize("content", [None, b"", b"not a key file\n\x00\xff", b"[unclosed\n"])
def test_a_missing_or_broken_state_file_starts_empty(phone, content):
    if content is not None:
        phone.state.write_bytes(content)
    assert phone.run("load", "apps") == ["dirty 0", "-"]


def test_apps_without_a_policy_or_a_count_are_not_saved(phone):
    phone.cgroup(SNAP)
    phone.run("scan /user.slice", "save")
    assert phone.state.read_text().strip() == ""


def test_a_state_file_that_cannot_be_written_stays_dirty(phone):
    phone.cgroup(SNAP)
    out = phone.run("scan /user.slice", f"count {SNAP} camera 1 0 4999 0", "harvest", "save",
                    AA_STATE="missing-dir/apps.ini")
    assert out[-1] == "dirty 1"
    assert "Cannot save missing-dir/apps.ini" in phone.stderr


# ---- Cameras and security keys ----

FIDO_DESCRIPTOR = bytes.fromhex("06d0f10901a1010920150026ff007508958081020921150026ff00750895809102c0")
KEYBOARD_DESCRIPTOR = bytes.fromhex("05010906a101050719e029e71500250175019508810295017508810195067508"
                                    "150025650507190029658100c0")


def sysfs_device(phone, cls, name, dev, target, descriptor=None):
    """A node of a sysfs class: dev "MAJ:MIN", device -> target below sys/devices"""
    d = phone.root / cls / name
    d.mkdir(parents=True)
    if dev is not None:
        (d / "dev").write_text(dev + "\n")
    t = phone.root / "sys/devices" / target
    t.mkdir(parents=True, exist_ok=True)
    if descriptor is not None:
        (t / "report_descriptor").write_bytes(descriptor)
    (d / "device").symlink_to(t)
    return d


def devices_tree(phone):
    # The phone's host controller as sysfs has it (11201000.usb0 is also the gadget's controller)
    usb = "platform/soc/11201000.usb0/11200000.xhci0/usb1/1-1/1-1:1.0"
    sysfs_device(phone, "sys/class/video4linux", "video0", "81:0", usb + "/video4linux")
    sysfs_device(phone, "sys/class/video4linux", "video1", "81:1", "platform/soc/15000000.isp")  # built in
    # The phone's own webcam gadget (f_uvc): below the controller, but no device on USB
    sysfs_device(phone, "sys/class/video4linux", "video4", "81:4", "platform/soc/11201000.usb0/gadget.0")
    sysfs_device(phone, "sys/class/video4linux", "video2", None, usb + "/v2")                   # no dev
    sysfs_device(phone, "sys/class/video4linux", "video3", "garbage", usb + "/v3")
    sysfs_device(phone, "sys/class/video4linux", ".hidden", "81:9", usb + "/v9")
    sysfs_device(phone, "sys/bus/media/devices", "media0", "237:0", usb + "/media")
    key = sysfs_device(phone, "sys/class/hidraw", "hidraw0", "241:0", usb + "/0003:1050:0407.0001",
                       FIDO_DESCRIPTOR)
    sysfs_device(phone, "sys/class/hidraw", "hidraw1", "241:1", usb + "/0003:046D:C31C.0002",
                 KEYBOARD_DESCRIPTOR)
    sysfs_device(phone, "sys/class/hidraw", "hidraw2", "241:2", usb + "/0003:0000:0000.0003",
                 bytes.fromhex("06d0"))                       # the usage page cut off
    # The usage page in the descriptor's last three bytes still counts
    tail = sysfs_device(phone, "sys/class/hidraw", "hidraw3", "241:3", usb + "/0003:0000:0000.0004",
                        bytes.fromhex("0501") + bytes.fromhex("06d0f1"))
    sysfs_device(phone, "sys/class/hidraw", "hidraw4", "241:4", usb + "/0003:0000:0000.0005")  # none
    return key, tail


def test_devices_are_usb_cameras_and_fido_keys(phone):
    devices_tree(phone)
    assert phone.last("devices") == "237:0=camera 241:0=security-key 241:3=security-key 81:0=camera"


def test_devices_that_come_and_go_follow_in_the_map(phone):
    devices_tree(phone)
    out = phone.run("devices", "unlink sys/class/video4linux/video0/dev", "testdev 240:1=security-key",
                    "devices")
    assert out[3] == "237:0=camera 240:1=security-key 241:0=security-key 241:3=security-key"


def test_test_devices_are_cameras_or_keys_only(phone):
    out = phone.run("testdev 240:0=camera,240:1=security-key,240:2=network,junk,1:2=speaker", "devices",
                    "testdev -", "devices")
    assert out[1] == "240:0=camera 240:1=security-key"
    assert out[3] == "-"
    for bad in ("240:2=network", "junk", "1:2=speaker"):
        assert f"Ignoring --test-device {bad}" in phone.stderr


def test_keys_are_announced_again_but_cameras_are_not(phone):
    key, tail = devices_tree(phone)
    phone.run("testdev 240:1=security-key", "devices", "reannounce")
    assert (key / "uevent").read_text() == "add"
    assert (tail / "uevent").read_text() == "add"
    assert not (phone.root / "sys/class/video4linux/video0/uevent").exists()
    assert not (phone.root / "sys/class/hidraw/hidraw1/uevent").exists()


def test_a_key_that_cannot_be_announced_does_not_stop_the_others(phone):
    key, tail = devices_tree(phone)
    (key / "uevent").mkdir()
    phone.run("devices", "reannounce")
    assert (tail / "uevent").read_text() == "add"
    assert f"Cannot announce sys/class/hidraw/hidraw0 again" in phone.stderr


def test_a_new_device_node_rescans_the_devices(phone):
    (phone.root / "dev").mkdir()
    out = phone.run("devwatch dev", "devmap", "inotify", "devmap",
                    "testdev 240:0=camera", "touch dev/video9", "inotify", "devmap")
    assert out == ["ok", "-", "ok", "-", "ok", "ok", "ok", "240:0=camera"]


# ---- inotify ----

def test_apps_and_sessions_appear_and_go(phone):
    phone.cgroup(SNAP)
    maps = SLICE + "/app-gnome-org.gnome.Maps-5.scope"
    out = phone.run("scan /user.slice", "attach",
                    f"mkdir sys/fs/cgroup{maps}", f"touch sys/fs/cgroup{SLICE}/memory.max", "inotify", "cgroups",
                    f"mkdir sys/fs/cgroup{SNAP1}", "inotify", "cgroups", "links",
                    f"rmdir sys/fs/cgroup{maps}", "inotify", "cgroups")
    assert cgroups(out[5])[maps]["map"] == "3,3,1"
    assert set(cgroups(out[5])) == {SNAP, maps}
    # A new user's session: the whole tree at once, its app.slice attached right away
    assert cgroups(out[8])[SNAP1] == {"app": SNAPSHOT, "uid": "1001", "map": "3,3,1", "once": "0,0,0"}
    assert out[9] == f"{SLICE} 2 | {SLICE1} 2 | to_attach 0"
    assert set(cgroups(out[12])) == {SNAP, SNAP1}


# ---- Events of the programs ----

def signals(line):
    return [] if line == "-" else line.split(" | ")


def denied(app, res, uid=1000):
    return f"/org/plinius/AppAccess1 org.plinius.AppAccess1 AccessDenied ('{app}', '{res}', 0, {uid})"


def changed(app):
    return f"/org/plinius/AppAccess1 org.plinius.AppAccess1 Changed ('{app}',)"


def test_a_refused_ask_becomes_one_question_per_20_seconds(phone):
    phone.cgroup(SNAP)
    i = phone.ino(SNAP)
    out = phone.run("scan /user.slice",
                    f"event {i} camera 1 3", f"event {i} camera 1 3", "signals",
                    f"event {i} security-key 1 3", "signals",
                    "clock 5019 1800000019", f"event {i} camera 1 3", "signals",
                    "clock 5020 1800000020", f"event {i} camera 1 3", "signals")
    assert signals(out[3]) == [denied(SNAPSHOT, "camera")]
    assert signals(out[5]) == [denied(SNAPSHOT, "security-key")]
    assert signals(out[8]) == []
    assert signals(out[11]) == [denied(SNAPSHOT, "camera")]


@pytest.mark.parametrize("event", [
    "camera 1 2",          # DENY: refused without a question
    "camera 2 0",          # a cgroup without a verdict yet
    "7 1 3",               # no such resource
])
def test_events_without_a_question(phone, event):
    phone.cgroup(SNAP)
    assert phone.last("scan /user.slice", f"event {phone.ino(SNAP)} {event}", "signals") == "-"


def test_a_short_record_and_a_service_ask_nothing(phone):
    phone.cgroup(SNAP, PORTAL)
    out = phone.run("scan /user.slice", f"event {phone.ino(SNAP)} camera 1 3 20",
                    f"event {phone.ino(PORTAL)} camera 1 3", "signals")
    assert out[-1] == "-"


def test_an_event_of_an_unseen_cgroup_adds_it(phone):
    maps = SLICE + "/app-gnome-org.gnome.Maps-5.scope"
    phone.cgroup(SNAP, maps)
    out = phone.run(f"scan {SNAP}", f"event {phone.ino(maps)} camera 1 3", "signals", "cgroups",
                    "event 1 camera 1 3", "signals", "cgroups")
    assert signals(out[2]) == [denied("org.gnome.Maps", "camera")]
    assert set(cgroups(out[3])) == {SNAP, maps}
    assert out[5] == "-" and out[6] == out[3]


def test_events_come_through_the_ring_buffer(phone):
    phone.cgroup(SNAP)
    out = phone.run("scan /user.slice", "attach", f"ringbuf {phone.ino(SNAP)} camera 1 3", "signals")
    assert signals(out[-1]) == [denied(SNAPSHOT, "camera")]


def test_without_a_bus_no_signal_is_sent(phone):
    phone.cgroup(SNAP)
    out = phone.run("scan /user.slice", "busoff", f"event {phone.ino(SNAP)} camera 1 3", "signals")
    assert out[-1] == "-"


# ---- Who may call what ----

CALLS = {
    "Get": "call Get ('org.gnome.Snapshot',)",
    "List": "call List",
    "SetPolicy": "call SetPolicy ('org.gnome.Snapshot', 'camera', uint32 1)",
    "AllowOnce": "call AllowOnce ('org.gnome.Snapshot', 'camera')",
    "Forget": "call Forget ('org.gnome.Snapshot',)",
    "CountUse": "call CountUse ('org.gnome.Snapshot', uint32 0, 'camera')",
    # Process 5000 runs Snapshot in the account of the default caller (uid 1001)
    "CountRefused": "call CountRefused (uint32 5000, 'camera')",
    "CountAttempt": "call CountAttempt ('', uint32 5000, 'microphone', false)",
}
READ = {"Get", "List"}
OWN = READ | {"AllowOnce"}
ADMIN = OWN | {"SetPolicy", "Forget"}


@pytest.mark.parametrize("setup,allowed", [
    (["bus caller 0", "pid -1"], ADMIN | {"CountUse", "CountRefused", "CountAttempt"}),  # root, whatever runs it
    (["bus caller -1"], set()),                                        # the bus does not know it
    ([], ADMIN),                                                       # an administrator in Settings
    (["exe 4242 " + AGENT_EXE], ADMIN | {"CountRefused", "CountAttempt"}),  # … or answering the agent
    # The agent of another account: not for 1001's app
    (["exe 4242 " + AGENT_EXE, "bus caller 1002", "bus groups 1002", "user 1002"], OWN),
    (["exe 4242 " + AGENT_EXE, "bus caller 975"], set()),              # a system account
    (["bus caller 1002", "bus groups 1002", "user 1002"], OWN),        # not in wheel
    (["bus wheel -1"], OWN),                                           # no group wheel
    (["bus groups -"], OWN),                                           # the bus gives no groups
    (["bus groups none"], OWN),
    (["creds fail"], OWN),                                             # no credentials: no admin
    (["exe 4242 /usr/bin/python3"], READ),                             # another program
    (["exe 4242 /usr/bin/gnome-control-center (deleted)"], READ),
    (["exe 4242 /usr/bin/gnome-control-center-x"], READ),
    (["pid -1"], READ),
    (["pid 0"], READ),
    (["pid 4243"], READ),                                              # gone from /proc
    (["nouser 1001"], set()),                                          # no session at logind
    (["user 1001 online"], ADMIN),
    (["user 1001 closing"], set()),
    (["bus caller 999", "user 999"], set()),                           # a system account
    (["bus caller 975", "exe 4242 " + BRIDGE], {"CountUse"}),          # camera-hal-bridge
    (["exe 4242 " + BRIDGE], READ | {"CountUse"}),
])
def test_who_may_call_what(phone, setup, allowed):
    cmds = []
    for s in setup:
        word, *rest = s.split(" ", 1)
        if word == "exe":
            pid, target = rest[0].split(" ", 1)
            phone.exe(int(pid), target)
        elif word == "user":
            uid, *state = rest[0].split()
            phone.user(int(uid), *state)
        elif word == "nouser":
            (phone.root / f"run/systemd/users/{rest[0]}").unlink()
        else:
            cmds.append(s)
    phone.cgroup(SNAP, SNAP1)
    phone.proc_cgroup(5000, f"0::{SNAP1}\n")
    out = phone.run("scan /user.slice", *cmds, *CALLS.values(), "signals")
    answers = dict(zip(CALLS, out[1 + len(cmds):]))
    assert {m for m, a in answers.items() if a != DENIED} == allowed, answers
    if not allowed & {"SetPolicy", "AllowOnce", "Forget", "CountUse", "CountRefused", "CountAttempt"}:
        # Refused calls change nothing
        assert out[-1] == "-"
        assert not phone.state.exists()


# ---- The methods ----

DEFAULT_ROWS = [("camera", 0, 0, 0, 0, 0), ("security-key", 0, 0, 0, 0, 0), ("network", 1, 0, 0, 0, 0),
                ("microphone", 0, 0, 0, 0, 0)]


def test_get_gives_the_defaults_of_an_unknown_app(phone):
    assert value(phone.last("call Get ('org.example.New',)")) == (DEFAULT_ROWS,)


@pytest.mark.parametrize("app", ["org.gnome.Settings", "org.plinius.Dictate1", "plinius-callfocus"])
def test_the_phones_own_recording_apps_have_the_microphone(phone, app):
    # Unknown to the daemon, known and forgotten: the microphone is allowed until set otherwise
    out = phone.run(f"call Get ('{app}',)", f"call SetPolicy ('{app}', 'microphone', uint32 2)",
                    f"call Get ('{app}',)", f"call Forget ('{app}',)", f"call Get ('{app}',)")
    own = DEFAULT_ROWS[:3] + [("microphone", 1, 0, 0, 0, 0)]
    assert value(out[0]) == value(out[4]) == (own,)
    assert value(out[2])[0][3] == ("microphone", 2, 0, 0, 0, 0)


def test_get_includes_attempts_not_yet_harvested(phone):
    phone.cgroup(SNAP)
    out = phone.run("scan /user.slice", f"count {SNAP} camera 2 1 4990 4995", CALLS["Get"])
    assert value(out[-1])[0][0] == ("camera", 0, 2, 1, UNIX - 10, UNIX - 5)


def test_list_gives_every_app_and_caps_the_counts(phone):
    phone.state.write_text("[org.gnome.Snapshot]\ncamera=allow\ncamera-allowed=5000000000\n"
                           "camera-last-allowed=1799999000\n[org.mozilla.firefox]\nnetwork=deny\n")
    rows = value(phone.last("load", "call List"))[0]
    assert sorted(rows) == sorted([
        (SNAPSHOT, "camera", 1, 2 ** 32 - 1, 0, 1799999000, 0),
        (SNAPSHOT, "security-key", 0, 0, 0, 0, 0),
        (SNAPSHOT, "network", 1, 0, 0, 0, 0),
        (SNAPSHOT, "microphone", 0, 0, 0, 0, 0),
        (FIREFOX, "camera", 0, 0, 0, 0, 0),
        (FIREFOX, "security-key", 0, 0, 0, 0, 0),
        (FIREFOX, "network", 2, 0, 0, 0, 0),
        (FIREFOX, "microphone", 0, 0, 0, 0, 0),
    ])


def test_get_of_an_unknown_app_does_not_add_it_to_list(phone):
    # Any user with a session may call Get: asking must not make the daemon keep the id
    out = phone.run("call Get ('org.example.Probe1',)", "call Get ('org.example.Probe2',)", "call List", "apps",
                    "save")
    assert value(out[0]) == value(out[1]) == (DEFAULT_ROWS,)
    assert value(out[2]) == ([],)
    assert out[3] == "-"


@pytest.mark.parametrize("call,message", [
    ("call Get ('bad/app',)", "Not an application id: bad/app"),
    ("call Get ('',)", "Not an application id: "),
    ("call Forget ('" + "a" * 256 + "',)", "Not an application id: " + "a" * 256),
    ("call SetPolicy ('org.x.Y', 'speaker', uint32 1)", "Unknown resource speaker"),
    ("call SetPolicy ('org.x.Y', 'camera', uint32 3)", "Unknown policy 3"),
    ("call SetPolicy ('org.x.Y', 'camera', uint32 4294967295)", "Unknown policy 4294967295"),
    ("call SetPolicy ('../x', 'camera', uint32 1)", "Not an application id: ../x"),
    ("call AllowOnce ('org.x.Y', 'gps')", "Unknown resource gps"),
    ("call CountUse ('org.x.Y', uint32 0, 'gps')", "Unknown resource gps"),
    ("call CountRefused (uint32 5000, 'gps')", "Unknown resource gps"),
    # The programs count refused keys and network themselves
    ("call CountRefused (uint32 5000, 'network')", "Refusals of network are counted by the programs"),
    ("call CountRefused (uint32 5000, 'security-key')", "Refusals of security-key are counted by the programs"),
])
def test_invalid_arguments_are_refused_and_change_nothing(phone, call, message):
    phone.cgroup(SNAP)
    phone.proc_cgroup(5000, f"0::{SNAP}\n")
    out = phone.run("scan /user.slice", "bus caller 0", call, "signals", "apps")
    assert out[2] == INVALID + message
    assert out[3] == "-"
    assert out[4] == SNAPSHOT
    assert not phone.state.exists()


def test_set_policy_reaches_every_running_instance_and_is_saved(phone):
    phone.cgroup(SNAP, SNAP1, FOX)
    out = phone.run("scan /user.slice", CALLS["SetPolicy"], "cgroups", "signals", f"app {SNAPSHOT}")
    assert out[1] == "ok"
    assert {p: c["map"] for p, c in cgroups(out[2]).items()} == {SNAP: "1,3,1", SNAP1: "1,3,1", FOX: "3,3,1"}
    assert signals(out[3]) == [changed(SNAPSHOT)]
    assert app_rows(out[4])["camera"]["set"] == "1"
    assert keyfile(phone.state) == {SNAPSHOT: {"camera": "allow"}}


def test_set_policy_of_an_app_that_is_not_running(phone):
    out = phone.run("call SetPolicy ('org.example.Later', 'network', uint32 2)")
    assert out == ["ok"]
    assert keyfile(phone.state) == {"org.example.Later": {"network": "deny"}}


def test_allow_once_reaches_only_the_callers_own_instances(phone):
    phone.cgroup(SNAP, SNAP1, FOX)
    later = SLICE1 + "/app-gnome-org.gnome.Snapshot-100.scope"
    out = phone.run("scan /user.slice", CALLS["AllowOnce"], "cgroups", "signals",
                    f"mkdir sys/fs/cgroup{later}", f"scan {later}", "cgroups")
    assert out[1] == "ok"
    cg = cgroups(out[2])
    # The caller is uid 1001: user 1000's Snapshot is still asked for
    assert (cg[SNAP1]["map"], cg[SNAP1]["once"]) == ("1,3,1", "1,0,0")
    assert (cg[SNAP]["map"], cg[SNAP]["once"]) == ("3,3,1", "0,0,0")
    assert cg[FOX]["map"] == "3,3,1"
    assert signals(out[3]) == [changed(SNAPSHOT)]
    # Once is not always: an instance started afterwards is asked again, nothing is saved
    assert cgroups(out[6])[later]["map"] == "3,3,1"
    assert keyfile(phone.state) == {}


def test_allow_once_by_root_reaches_every_account(phone):
    phone.cgroup(SNAP, SNAP1)
    assert verdicts(phone, "scan /user.slice", "bus caller 0", CALLS["AllowOnce"]) == {
        SNAP: "1,3,1", SNAP1: "1,3,1"}


def test_allow_once_ends_with_the_instance(phone):
    phone.cgroup(SNAP1)
    again = SLICE1 + "/app-gnome-org.gnome.Snapshot-101.scope"
    out = phone.run("scan /user.slice", CALLS["AllowOnce"], f"remove {SNAP1}", f"rmdir sys/fs/cgroup{SNAP1}",
                    f"mkdir sys/fs/cgroup{again}", f"scan {again}", "cgroups")
    assert {p: c["map"] for p, c in cgroups(out[-1]).items()} == {again: "3,3,1"}


def test_deny_ends_an_allow_once(phone):
    # Don't Allow in Settings closes the running instance at once, also after it is set back
    phone.cgroup(SNAP1)
    out = phone.run("scan /user.slice", CALLS["AllowOnce"], "call SetPolicy ('org.gnome.Snapshot', 'camera', uint32 2)",
                    "cgroups", "call SetPolicy ('org.gnome.Snapshot', 'camera', uint32 0)", "cgroups")
    assert out[2] == "ok"
    assert (cgroups(out[3])[SNAP1]["map"], cgroups(out[3])[SNAP1]["once"]) == ("2,3,1", "0,0,0")
    assert cgroups(out[5])[SNAP1]["map"] == "3,3,1"


@pytest.mark.parametrize("call", [
    "call SetPolicy ('org.gnome.Snapshot', 'camera', uint32 0)",   # Ask: the question comes again
    "call Forget ('org.gnome.Snapshot',)",
])
def test_ask_and_forget_end_an_allow_once(phone, call):
    phone.cgroup(SNAP1)
    out = phone.run("scan /user.slice", CALLS["AllowOnce"],
                    "call AllowOnce ('org.gnome.Snapshot', 'security-key')", call, "cgroups")
    c = cgroups(out[-1])[SNAP1]
    assert (c["map"], c["once"]) == (("3,1,1", "0,1,0") if "SetPolicy" in call else ("3,3,1", "0,0,0"))


def test_a_policy_for_one_resource_leaves_the_others_allowed_once(phone):
    phone.cgroup(SNAP1)
    assert verdicts(phone, "scan /user.slice", CALLS["AllowOnce"],
                    "call SetPolicy ('org.gnome.Snapshot', 'network', uint32 2)") == {SNAP1: "1,3,2"}


def test_allow_once_does_not_open_an_app_set_to_deny(phone):
    # A user who is not an administrator (and may not change policies, L15) must not open an
    # app an administrator set to Don't Allow; the agent only offers Allow Once for apps that ask
    snap2 = SLICE.replace("1000", "1002") + "/app-gnome-org.gnome.Snapshot-5.scope"
    phone.state.write_text("[org.gnome.Snapshot]\ncamera=deny\n")
    phone.cgroup(snap2)
    phone.user(1002)
    out = phone.run("load", "scan /user.slice", "bus caller 1002", "bus groups 1002", CALLS["AllowOnce"],
                    "cgroups", "bus caller 0", "call SetPolicy ('org.gnome.Snapshot', 'camera', uint32 0)",
                    "cgroups")
    assert out[4] == "ok"
    assert (cgroups(out[5])[snap2]["map"], cgroups(out[5])[snap2]["once"]) == ("2,3,1", "0,0,0")
    # Nothing was kept for later: set back to Ask, the app is asked again
    assert cgroups(out[8])[snap2]["map"] == "3,3,1"


@pytest.mark.parametrize("policy,verdict", [("deny", "2,3,1"), ("allow", "1,3,1")])
def test_allow_once_by_root_does_not_override_a_policy(phone, policy, verdict):
    phone.state.write_text(f"[org.gnome.Snapshot]\ncamera={policy}\n")
    phone.cgroup(SNAP)
    out = phone.run("load", "scan /user.slice", "bus caller 0", CALLS["AllowOnce"], "cgroups")
    assert (cgroups(out[-1])[SNAP]["map"], cgroups(out[-1])[SNAP]["once"]) == (verdict, "0,0,0")


def test_forget_resets_policies_and_counts(phone):
    phone.state.write_text("[org.gnome.Snapshot]\ncamera=allow\ncamera-allowed=4\ncamera-last-allowed=17\n"
                           "[org.mozilla.firefox]\nnetwork=deny\n")
    phone.cgroup(SNAP)
    out = phone.run("load", "scan /user.slice", f"count {SNAP} camera 3 0 4999 0", CALLS["Forget"],
                    "cgroups", f"app {SNAPSHOT}", "signals", "harvest", f"app {SNAPSHOT}")
    assert out[3] == "ok"
    assert cgroups(out[4])[SNAP]["map"] == "3,3,1"
    assert app_rows(out[5]) == app_rows(out[8]) == {
        r: {"policy": p, "set": "0", "allowed": "0", "denied": "0", "last": "0,0"}
        for r, p in (("camera", "0"), ("security-key", "0"), ("network", "1"), ("microphone", "0"))}
    assert signals(out[6]) == [changed(SNAPSHOT)]
    assert keyfile(phone.state) == {FIREFOX: {"network": "deny"}}


@pytest.mark.parametrize("call,announced", [
    ("call SetPolicy ('org.gnome.Snapshot', 'security-key', uint32 1)", True),
    ("call AllowOnce ('org.gnome.Snapshot', 'security-key')", True),
    ("call SetPolicy ('org.gnome.Snapshot', 'security-key', uint32 2)", False),
    ("call SetPolicy ('org.gnome.Snapshot', 'security-key', uint32 0)", False),
    ("call SetPolicy ('org.gnome.Snapshot', 'camera', uint32 1)", False),
    ("call AllowOnce ('org.gnome.Snapshot', 'camera')", False),
    ("call Forget ('org.gnome.Snapshot',)", False),
])
def test_an_allowed_key_is_announced_again(phone, call, announced):
    key, _ = devices_tree(phone)
    assert phone.run("devices", call)[1] == "ok"
    assert (key / "uevent").exists() == announced


def test_allow_once_of_a_denied_key_announces_nothing(phone):
    key, _ = devices_tree(phone)
    phone.state.write_text("[org.gnome.Snapshot]\nsecurity-key=deny\n")
    assert phone.run("load", "devices", "call AllowOnce ('org.gnome.Snapshot', 'security-key')")[2] == "ok"
    assert not (key / "uevent").exists()


def test_count_use_for_a_named_app(phone):
    out = phone.run("bus caller 0", "call CountUse ('org.gnome.Snapshot', uint32 0, 'camera')",
                    f"app {SNAPSHOT}", "signals", "timer save")
    assert value(out[1]) == (SNAPSHOT,)
    assert app_rows(out[2])["camera"] == {"policy": "0", "set": "0", "allowed": "1", "denied": "0",
                                          "last": f"{UNIX},0"}
    assert signals(out[3]) == [changed(SNAPSHOT)]
    assert out[4] == "dirty 0" and keyfile(phone.state)[SNAPSHOT]["camera-allowed"] == "1"


def test_count_use_for_the_app_of_a_process(phone):
    maps = SLICE + "/app-gnome-org.gnome.Maps-5.scope"
    phone.cgroup(SNAP, PORTAL, maps)
    phone.proc_cgroup(5000, f"0::{SNAP}\n")
    phone.proc_cgroup(5001, f"0::{maps}\n")               # not seen by the daemon yet
    phone.proc_cgroup(5002, f"0::{PORTAL}\n")             # a user service
    phone.proc_cgroup(5003, "0::/user.slice/user-1000.slice/session-2.scope\n")
    phone.proc_cgroup(5005, f"1:name=systemd:{SNAP}\n")   # only the v2 line counts
    phone.proc_cgroup(5006, f"0::{SLICE}/app-gnome-org.example.Gone-1.scope\n")
    out = phone.run(f"scan {SNAP}", "bus caller 0",
                    *(f"call CountUse ('', uint32 {pid}, 'camera')" for pid in
                      (5000, 5001, 5002, 5003, 5004, 0, 5005, 5006)),
                    "call CountUse ('bad/app', uint32 5000, 'camera')",   # no app id: the process's
                    f"app {SNAPSHOT}", "app org.gnome.Maps", "signals", "cgroups")
    assert [value(o)[0] for o in out[2:11]] == [SNAPSHOT, "org.gnome.Maps", "", "", "", "", "", "",
                                                 SNAPSHOT]
    assert app_rows(out[11])["camera"]["allowed"] == "2"
    assert app_rows(out[12])["camera"]["allowed"] == "1"
    assert signals(out[13]) == [changed(SNAPSHOT), changed("org.gnome.Maps"), changed(SNAPSHOT)]
    # Cgroups the daemon had not seen are looked at (and get their verdicts) on the way
    assert set(cgroups(out[14])) == {SNAP, maps, PORTAL}


def test_count_refused_counts_a_denied_attempt_without_a_question(phone):
    # The agent of uid 1000 saw the camera portal refuse a camera to its Snapshot
    phone.cgroup(SNAP)
    phone.proc_cgroup(5000, f"0::{SNAP}\n")
    phone.exe(4242, AGENT_EXE)
    out = phone.run("scan /user.slice", "bus caller 1000", "call CountRefused (uint32 5000, 'camera')",
                    "signals", "clock 5100 1800000100", "call CountRefused (uint32 5000, 'camera')", "signals",
                    CALLS["Get"], "timer save")
    assert value(out[2]) == value(out[5]) == (SNAPSHOT,)
    # Changed for Settings, no AccessDenied: the portal has answered the app, the agent asks nothing
    assert signals(out[3]) == signals(out[6]) == [changed(SNAPSHOT)]
    assert value(out[7])[0][0] == ("camera", 0, 0, 2, 0, UNIX + 100)
    assert out[8] == "dirty 0"
    assert keyfile(phone.state)[SNAPSHOT] == {"camera-denied": "2", "camera-last-denied": str(UNIX + 100)}


def test_count_refused_only_for_the_callers_own_apps(phone):
    # The agent of uid 1001 for an app of uid 1000: refused, nothing counted; root may
    phone.cgroup(SNAP)
    phone.proc_cgroup(5000, f"0::{SNAP}\n")
    phone.exe(4242, AGENT_EXE)
    out = phone.run("scan /user.slice", "call CountRefused (uint32 5000, 'camera')", "signals",
                    f"app {SNAPSHOT}", "bus caller 0", "call CountRefused (uint32 5000, 'camera')",
                    f"app {SNAPSHOT}")
    assert out[1] == DENIED
    assert out[2] == "-"
    assert app_rows(out[3])["camera"]["denied"] == "0"
    assert value(out[5]) == (SNAPSHOT,)
    assert app_rows(out[6])["camera"]["denied"] == "1"


def test_count_refused_for_a_process_without_an_app(phone):
    maps = SLICE + "/app-gnome-org.gnome.Maps-5.scope"
    phone.cgroup(PORTAL, maps)
    phone.proc_cgroup(5000, f"0::{PORTAL}\n")                                    # a user service
    phone.proc_cgroup(5001, "0::/user.slice/user-1000.slice/session-2.scope\n")  # a shell
    phone.proc_cgroup(5002, f"0::{maps}\n")                                      # not seen yet
    out = phone.run("bus caller 0", *(f"call CountRefused (uint32 {pid}, 'camera')"
                                      for pid in (5000, 5001, 5003, 0, 5002)),
                    "signals", "apps")
    assert [value(o)[0] for o in out[1:6]] == ["", "", "", "", "org.gnome.Maps"]
    assert signals(out[6]) == [changed("org.gnome.Maps")]
    assert out[7] == "org.gnome.Maps"


def test_pid_of_an_app(phone):
    phone.cgroup(SNAP)
    phone.proc_cgroup(5000, f"12:pids:/x\n0::{SNAP}\n")
    assert phone.run("pidapp 5000", "pidapp 0", "pidapp 6000") == [SNAPSHOT, "-", "-"]


# ---- The daemon's main() ----

def bpf_object(phone):
    f = phone.root / "usr/lib/plinius-appaccess/appaccess.bpf.o"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"\x7fELF")
    return f


def test_check_lists_devices_verdicts_and_attachments(phone):
    bpf_object(phone)
    (phone.root / "dev").mkdir()
    phone.state.write_text("[org.gnome.Snapshot]\ncamera=allow\n")
    phone.cgroup(SNAP, PORTAL)
    out = phone.main("--check", "--test-device", "240:0=camera")
    assert sorted(out) == sorted([
        "device 240:0 camera",
        f"app          camera=1 security-key=3 network=1 microphone=3 {SNAPSHOT} {SNAP}",
        f"service      camera=1 security-key=1 network=1 microphone=1 - {PORTAL}",
        f"attached to {SLICE}: 2 programs",
        "exit 0",
    ])


def test_check_with_other_files(phone):
    (phone.root / "my.bpf.o").write_bytes(b"")
    (phone.root / "my.ini").write_text("[org.gnome.Snapshot]\nnetwork=deny\n")
    phone.cgroup(SNAP)
    out = phone.main("--check", "--bpf", "my.bpf.o", "--state", "my.ini")
    assert f"app          camera=3 security-key=3 network=2 microphone=3 {SNAPSHOT} {SNAP}" in out


def test_the_off_file_stops_everything(phone):
    off = phone.root / "etc/plinius-appaccess/off"
    off.parent.mkdir(parents=True)
    off.write_text("")
    assert phone.main() == ["etc/plinius-appaccess/off exists: nothing is decided", "exit 0"]


@pytest.mark.parametrize("bpf,args,env,message", [
    (True, ["--bogus"], {}, "Unknown option --bogus"),
    (False, [], {}, "Cannot open usr/lib/plinius-appaccess/appaccess.bpf.o"),
    (True, ["--bpf", "nothing.o"], {}, "Cannot open nothing.o"),
    (True, [], {"AA_BPF_FAIL": "load"}, "Cannot load the BPF programs of usr/lib/plinius-appaccess/appaccess.bpf.o"),
    (True, [], {"AA_BPF_FAIL": "map:counts"}, "No map counts in the BPF object"),
    (True, [], {"AA_BPF_FAIL": "map:events"}, "Cannot open the ring buffer"),
])
def test_main_refuses_to_start_without_its_programs(phone, bpf, args, env, message):
    if bpf:
        bpf_object(phone)
    assert phone.main(*args, **env) == ["exit 2" if args == ["--bogus"] else "exit 1"]
    assert message in phone.stderr
    assert not phone.state.exists()


def test_main_needs_the_cgroup_tree(phone):
    bpf_object(phone)
    shutil.rmtree(phone.cgroot)
    assert phone.main()[-1] == "exit 1"
    assert "Cannot open sys/fs/cgroup or inotify" in phone.stderr


def test_main_runs_until_sigterm_and_saves(phone):
    bpf_object(phone)
    phone.cgroup(SNAP)
    out = phone.main("--no-bus", AA_TERM_MS=300)
    assert out == ["exit 0"]
    assert phone.state.exists()


def test_main_takes_the_bus_name_and_exports_the_object(phone):
    bpf_object(phone)
    out = phone.main(AA_TERM_MS=300)
    assert out == ["own system org.plinius.AppAccess1",
                   "register /org/plinius/AppAccess1 org.plinius.AppAccess1 "
                   "Get,List,SetPolicy,AllowOnce,Forget,CountUse,CountAttempt,CountRefused handler",
                   "exit 0"]


def test_main_keeps_running_when_the_object_cannot_be_exported(phone):
    bpf_object(phone)
    assert phone.main(AA_TERM_MS=300, AA_REGISTER="fail")[-1] == "exit 0"
    assert "Cannot export /org/plinius/AppAccess1: fake: already exported" in phone.stderr


def test_main_ends_when_the_bus_name_is_lost(phone):
    bpf_object(phone)
    assert phone.main(AA_OWN="lost")[-1] == "exit 0"
    assert "Lost the bus name org.plinius.AppAccess1" in phone.stderr


# ---- plinius-appaccess-agent: the question in the session ----

# ---- The microphone: the verdicts published for WirePlumber, the attempts it saw ----

MICDIR = "run/plinius-appaccess"
CALLFOCUS = SLICE + "/app-gnome-plinius\\x2dcallfocus-5039.scope"
DICTATE = SLICE + "/app-dbus\\x2d:1.2\\x2dorg.plinius.Dictate1.slice/dbus-:1.2-org.plinius.Dictate1@0.service"
BRIDGE_SVC = SLICE + "/audio-hal-bridge.service"
ONLY_SLICES = SLICE + "/app-gnome.slice"
OWN_APPS = {"org.gnome.Settings": "allow", "org.plinius.Dictate1": "allow", "plinius-callfocus": "allow"}


def mic_file(phone, uid=1000):
    return phone.root / MICDIR / f"microphone-{uid}.conf"


def mic(phone, uid=1000):
    return json.loads(mic_file(phone, uid).read_text())


def test_the_microphone_is_published_per_account_for_its_group(phone):
    phone.state.write_text("[org.mozilla.firefox]\nmicrophone=deny\n")
    phone.cgroup(SNAP, FOX, PORTAL, BRIDGE_SVC, CALLFOCUS, DICTATE, ONLY_SLICES, SNAP1)
    out = phone.run("load", "scan /user.slice", "attach", "nopasswd 1001", "mic")
    assert out[-1] == "1000 gid=101000"
    assert not mic_file(phone, 1001).exists()
    assert "User 1001 has no group: WirePlumber decides nothing for it" in phone.stderr
    assert stat.S_IMODE(mic_file(phone).stat().st_mode) == 0o640
    assert mic(phone) == {
        "default": "ask",
        "apps": {**OWN_APPS, SNAPSHOT: "ask", FIREFOX: "deny"},
        "cgroups": {SNAP: "ask", FOX: "deny", PORTAL: "service", BRIDGE_SVC: "service", CALLFOCUS: "allow",
                    DICTATE: "allow", DICTATE.rsplit("/", 1)[0]: "deny", ONLY_SLICES: "deny"},
    }
    # Every account with a session has its own, with only its own cgroups
    out = phone.run("load", "scan /user.slice", "attach", "mic")
    assert out[-1].startswith("1000 1001 gid=")
    assert mic(phone, 1001)["cgroups"] == {SNAP1: "ask"}


def test_a_session_without_apps_is_published_and_the_json_is_escaped(phone):
    odd = SNAP + '/a "quoted" \\ name\x01'
    phone.cgroup(SLICE, odd)
    os.mkdir(os.path.join(os.fsencode(phone.cgroot / SNAP.lstrip("/")), b"no-utf8-\xff"))
    phone.run("scan /user.slice", "attach", "mic")
    cgs = mic(phone)["cgroups"]
    # The odd name comes back as it is; a name that is no UTF-8 is left out (its parent decides)
    assert cgs[odd] == "ask"
    assert not any("no-utf8" in p for p in cgs)
    shutil.rmtree(phone.cgroot / "user.slice")
    phone.cgroup(SLICE)
    phone.run("scan /user.slice", "attach", "mic")
    assert mic(phone) == {"default": "ask", "apps": OWN_APPS, "cgroups": {}}


def test_answers_and_policies_reach_the_published_verdicts(phone):
    # The default caller (uid 1001, an administrator in Settings) allows its own Snapshot once
    phone.cgroup(SNAP, SNAP1)
    out = phone.run("scan /user.slice", "attach", "call AllowOnce ('org.gnome.Snapshot', 'microphone')", "mic")
    assert out[2] == "ok"
    assert mic(phone, 1001)["cgroups"] == {SNAP1: "allow"}
    assert mic(phone, 1001)["apps"][SNAPSHOT] == "allow"
    assert mic(phone, 1000)["cgroups"] == {SNAP: "ask"}
    assert mic(phone, 1000)["apps"][SNAPSHOT] == "ask"
    phone.run("scan /user.slice", "attach", "call SetPolicy ('org.gnome.Snapshot', 'microphone', uint32 1)", "mic")
    assert mic(phone, 1000)["cgroups"] == {SNAP: "allow"}
    assert mic(phone, 1000)["apps"][SNAPSHOT] == "allow"
    # An app that does not run: only its entry in "apps"
    phone.run("scan /user.slice", "attach", "call SetPolicy ('org.example.Recorder', 'microphone', uint32 2)", "mic")
    assert mic(phone, 1000)["apps"]["org.example.Recorder"] == "deny"


def test_publishing_waits_for_the_first_scan_and_then_for_the_idle_loop(phone):
    phone.cgroup(SNAP)
    out = phone.run("scan /user.slice", "micidle", "attach", "micidle", "micidle",
                    "call SetPolicy ('org.gnome.Snapshot', 'microphone', uint32 2)", "micidle")
    assert out[1] == "idle 0"
    assert out[3] == "idle 1"
    assert out[4] == "idle 0"
    assert out[6] == "idle 1"
    assert mic(phone)["cgroups"] == {SNAP: "deny"}


def test_apps_that_come_and_go_and_an_account_that_logs_out(phone):
    phone.cgroup(SLICE)
    out = phone.run("scan /user.slice", "attach", "micidle", f"mkdir sys/fs/cgroup{SNAP}", f"add {SNAP}",
                    "micidle", f"rmdir sys/fs/cgroup{SNAP}", f"remove {SNAP}", "micidle")
    assert out[5] == out[8] == "idle 1"
    assert mic(phone)["cgroups"] == {}
    out = phone.run("scan /user.slice", "attach", "mic", f"rmdir sys/fs/cgroup{SLICE}", f"remove {SLICE}",
                    "micidle")
    assert out[2] == "1000 gid=101000"
    assert out[-1] == "idle 1"
    assert not mic_file(phone).exists()
    # A new session: its file at once, before any app runs
    out = phone.run("scan /user.slice", "attach", f"mkdir sys/fs/cgroup{SLICE}", f"add {SLICE}", "micidle")
    assert out[-1] == "idle 1"
    assert mic(phone)["cgroups"] == {}


@pytest.mark.parametrize("fail", ["chown", "nodir"])
def test_a_file_that_cannot_be_published_is_reported(phone, fail):
    phone.cgroup(SNAP)
    if fail == "nodir":
        (phone.root / MICDIR).rmdir()
    out = phone.run("scan /user.slice", "attach", *(["bpf fail chown"] if fail == "chown" else []), "mic")
    assert out[-1] == "- gid=0"
    assert "Cannot publish run/plinius-appaccess/microphone-1000.conf" in phone.stderr
    if fail == "chown":
        assert os.listdir(phone.root / MICDIR) == []


def test_main_publishes_once_it_runs(phone):
    bpf_object(phone)
    phone.cgroup(SNAP)
    assert phone.main("--no-bus", AA_TERM_MS=300) == ["exit 0"]
    assert mic(phone)["cgroups"] == {SNAP: "ask"}


def attempt(app="", pid=5000, res="microphone", allowed=False):
    return f"call CountAttempt ('{app}', uint32 {pid}, '{res}', {'true' if allowed else 'false'})"


@pytest.fixture
def agent_of_1000(phone):
    """The agent of uid 1000; process 5000 runs Snapshot, 5001 Firefox (a Flatpak app) there"""
    phone.cgroup(SNAP, FOX, PORTAL, SNAP1)
    phone.proc_cgroup(5000, f"0::{SNAP}\n")
    phone.proc_cgroup(5001, f"0::{FOX}\n")
    phone.proc_cgroup(5002, f"0::{PORTAL}\n")
    phone.proc_cgroup(5003, f"0::{SNAP1}\n")
    phone.exe(4242, AGENT_EXE)
    return ["scan /user.slice", "bus caller 1000"]


def test_a_refused_recording_asks_once_per_20_seconds_and_counts(phone, agent_of_1000):
    out = phone.run(*agent_of_1000, attempt(), "signals", attempt(), "signals",
                    "clock 5020 1800000020", attempt(), "signals", f"app {SNAPSHOT}")
    assert value(out[2]) == value(out[4]) == value(out[7]) == (SNAPSHOT,)
    assert signals(out[3]) == signals(out[8]) == [denied(SNAPSHOT, "microphone"), changed(SNAPSHOT)]
    assert signals(out[5]) == [changed(SNAPSHOT)]
    assert app_rows(out[9])["microphone"] == {"policy": "0", "set": "0", "allowed": "0", "denied": "3",
                                               "last": f"0,{UNIX + 20}"}


def test_a_recording_that_was_allowed_counts_as_a_use(phone, agent_of_1000):
    out = phone.run(*agent_of_1000, attempt(allowed=True), "signals", f"app {SNAPSHOT}")
    assert value(out[2]) == (SNAPSHOT,)
    assert signals(out[3]) == [changed(SNAPSHOT)]
    assert app_rows(out[4])["microphone"]["allowed"] == "1"


@pytest.mark.parametrize("setup", [
    "call SetPolicy ('org.gnome.Snapshot', 'microphone', uint32 2)",   # Don't Allow: no question
    "call AllowOnce ('org.gnome.Snapshot', 'microphone')",              # allowed once meanwhile
])
def test_a_refusal_without_a_question(phone, agent_of_1000, setup):
    out = phone.run(*agent_of_1000, "bus caller 0", setup, "signals", "bus caller 1000", attempt(), "signals")
    assert signals(out[-1]) == [changed(SNAPSHOT)]


def test_an_app_named_by_wireplumber(phone, agent_of_1000):
    # A Flatpak app through pipewire-pulse: its instance in the caller's account counts
    out = phone.run(*agent_of_1000, attempt(FIREFOX, 0), "signals", attempt("org.example.Gone", 0),
                    "bus caller 1001", attempt(FIREFOX, 0), attempt(SNAPSHOT, 0), "bus caller 0",
                    attempt(SNAPSHOT, 0, allowed=True))
    assert value(out[2]) == (FIREFOX,)
    assert signals(out[3]) == [denied(FIREFOX, "microphone"), changed(FIREFOX)]
    assert value(out[4]) == ("",)          # not running: nothing to count
    assert out[6] == DENIED                # Firefox runs only in 1000's account
    assert value(out[7]) == (SNAPSHOT,)    # 1001 has a Snapshot of its own
    assert value(out[9]) == (SNAPSHOT,)    # root: any account


def test_attempts_of_processes_that_are_no_app_or_another_accounts(phone, agent_of_1000):
    out = phone.run(*agent_of_1000, attempt(pid=5002), attempt(pid=4999), attempt(pid=5003), "signals",
                    "bus caller 0", attempt(pid=5003))
    assert value(out[2]) == value(out[3]) == ("",)
    assert out[4] == DENIED
    assert out[5] == "-"
    assert value(out[7]) == (SNAPSHOT,)


@pytest.mark.parametrize("res,message", [
    ("camera", "Attempts of camera are counted by others"),
    ("speaker", "Unknown resource speaker"),
])
def test_attempts_only_of_the_microphone(phone, agent_of_1000, res, message):
    assert phone.last(*agent_of_1000, attempt(res=res)) == INVALID + message


@pytest.fixture(scope="module")
def agent(tmp_path_factory):
    return build(tmp_path_factory.mktemp("appaccess-agent"), "appaccess-agent-harness",
                 [CDIR / "dsec_appaccess_agent_harness.c"],
                 flags=[f"-I{APPACCESS}", "-Wno-unused-parameter"], pkgs=["gio-unix-2.0"])


class Session:
    """The agent in a session whose desktop files, home and catalogs are in tmp_path"""

    def __init__(self, binary, root):
        self.binary = binary
        self.root = root
        apps = root / "share/applications"
        apps.mkdir(parents=True)
        # GLib takes a desktop file only if its program exists
        exe = root / "bin/snapshot"
        exe.parent.mkdir()
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        (apps / "org.gnome.Snapshot.desktop").write_text(
            f"[Desktop Entry]\nType=Application\nName=Camera\nName[de]=Kamera\nExec={exe}\n")
        for d in ("home", "data-home", "config", "cache", "runtime", "state"):
            (root / d).mkdir()
        (root / "runtime").chmod(0o700)
        self.env = {"XDG_DATA_DIRS": root / "share", "XDG_DATA_HOME": root / "data-home",
                    "XDG_CONFIG_HOME": root / "config", "XDG_CONFIG_DIRS": root / "config",
                    "XDG_CACHE_HOME": root / "cache", "XDG_RUNTIME_DIR": root / "runtime",
                    "XDG_STATE_HOME": root / "state",
                    "HOME": root / "home", "DBUS_SESSION_BUS_ADDRESS": "disabled:",
                    "DBUS_SYSTEM_BUS_ADDRESS": "disabled:", "GIO_USE_VFS": "local"}
        self.stderr = ""

    def run(self, *cmds, **env):
        r = harness_run(self.binary, self.root, "\n".join(cmds) + "\n", **{**self.env, **env})
        self.stderr = r.stderr
        out = r.stdout.splitlines()
        assert len(out) == len(cmds), out
        return out

    def last(self, *cmds, **env):
        return self.run(*cmds, **env)[-1]

    def main(self, **env):
        r = harness_run(self.binary, self.root, "", args=("main",), **{**self.env, **env})
        self.stderr = r.stderr
        return r.stdout.splitlines()


@pytest.fixture
def session(agent, tmp_path):
    return Session(agent, tmp_path)


def calls(line):
    return [] if line == "-" else line.split(" | ")


NOTIFY = "session org.freedesktop.Notifications /org/freedesktop/Notifications org.freedesktop.Notifications Notify "
DAEMON_CALL = "system org.plinius.AppAccess1 /org/plinius/AppAccess1 org.plinius.AppAccess1 "
STORE = ("session org.freedesktop.impl.portal.PermissionStore /org/freedesktop/impl/portal/PermissionStore "
         "org.freedesktop.impl.portal.PermissionStore SetPermission ")
BODY = "Access was blocked. If you allow it, try again in the app."
ADMIN_ACTIONS = "once,Allow Once,always,Always Allow,never,Don't Allow"
USER_ACTIONS = "once,Allow Once,dismiss,Don't Allow"


DIALOG = ("session mobi.phosh.Shell.Portal /org/freedesktop/portal/desktop org.freedesktop.impl.portal.Access "
          "AccessDialog ")
ADMIN_BUTTONS = "['once', 'always', 'never']"
USER_BUTTONS = "['once', 'never']"


def dialog(app, title, icon, buttons=ADMIN_BUTTONS, n=1, body=BODY):
    return (DIALOG + f"('/org/plinius/AppAccessAgent/dialog/{n}', '{app}', '', '{title}', '', '{body}', "
            f"{{'icon': <'{icon}'>, 'plinius-once': <true>, 'plinius-buttons': <{buttons}>}})")


SNAP_CAMERA = dialog("org.gnome.Snapshot", "Camera wants to use a camera", "camera-web-symbolic")


def notify(title, icon, actions, replaces=0, desktop=None):
    hints = "{'urgency': <byte 0x01>, 'category': <'device'>"
    hints += f", 'desktop-entry': <'{desktop}'>}}" if desktop else "}"
    return (NOTIFY + f"app=plinius-appaccess replaces={replaces} icon={icon} title={title} body={BODY} "
            f"actions={actions} hints={hints} expire=-1 timeout=2000")


@pytest.mark.parametrize("res,title,icon", [
    ("camera", "Camera wants to use a camera", "camera-web-symbolic"),
    ("security-key", "Camera wants to use a security key", "security-high-symbolic"),
    ("network", "Camera wants to use the network", "network-workgroup-symbolic"),
    ("microphone", "Camera wants to use the microphone", "audio-input-microphone-symbolic"),
])
def test_question_titles_and_icons(session, res, title, icon):
    assert session.run(f"title {res} Camera", f"icon {res}") == [title, icon]


@pytest.mark.parametrize("ids,wheel,admin", [
    ("1000 1000 1000,998", 998, True),      # in wheel
    ("1000 998 none", 998, True),           # wheel as the primary group
    ("1000 1000 1000,20", 998, False),
    ("1000 1000 none", 998, False),
    ("1000 1000 fail", 998, False),         # getgroups() fails: no
    ("1000 1000 1000,998", -1, False),      # no group wheel on the phone
])
def test_who_counts_as_administrator(session, ids, wheel, admin):
    assert session.last(f"ids {ids}", f"bus wheel {wheel}", "admin") == ("yes" if admin else "no")


def test_app_names_come_from_the_desktop_file(session):
    assert session.run("name org.gnome.Snapshot", "name org.example.Tool") == [
        "Camera desktop=1", "org.example.Tool desktop=0"]


def test_an_administrator_is_asked_with_three_answers(session):
    out = session.run("denied org.gnome.Snapshot camera 0 1000", "log", "questions")
    # Without Phosh's dialog: a notification
    assert calls(out[1]) == [SNAP_CAMERA, notify("Camera wants to use a camera", "camera-web-symbolic",
                                                 ADMIN_ACTIONS, desktop="org.gnome.Snapshot")]
    assert out[2] == "7 org.gnome.Snapshot camera"
    assert "No dialog from Phosh, a notification instead: fake: no dialog" in session.stderr


def test_another_user_may_only_allow_once(session):
    out = session.run("ids 1000 1000 1000", "denied org.example.Tool network 0 1000", "log")
    # No desktop file: the id as name and no app for the notification to open
    assert calls(out[2]) == [
        dialog("org.example.Tool", "org.example.Tool wants to use the network", "network-workgroup-symbolic",
               USER_BUTTONS),
        notify("org.example.Tool wants to use the network", "network-workgroup-symbolic", USER_ACTIONS)]


@pytest.mark.parametrize("signal", [
    "denied org.gnome.Snapshot camera 0 1001",              # another account's app
    "denied org.gnome.Snapshot camera 1 1000",              # allowed: nothing to ask
    "denied org.gnome.Snapshot camera 2 1000",              # denied: no more questions
    "signal Changed ('org.gnome.Snapshot',)",
    "signal AccessDenied ('org.gnome.Snapshot', 'camera')",  # not the signal's type
])
def test_signals_that_ask_nothing(session, signal):
    assert session.run(signal, "log", "questions")[1:] == ["-", "-"]


def test_the_same_question_replaces_the_open_one(session):
    out = session.run("denied org.gnome.Snapshot camera 0 1000", "notify 8",
                      "denied org.gnome.Snapshot camera 0 1000", "notify 9",
                      "denied org.gnome.Snapshot security-key 0 1000", "log", "questions")
    c = calls(out[5])
    # The open notification is shown again, Phosh's dialog is tried only for the new question
    assert [x.split()[4] for x in c] == ["AccessDialog", "Notify", "Notify", "AccessDialog", "Notify"]
    assert [re.search(r"replaces=(\d+)", x).group(1) for x in c if " Notify " in x] == ["0", "7", "0"]
    assert out[6] == "8 org.gnome.Snapshot camera | 9 org.gnome.Snapshot security-key"


def test_no_notification_server_leaves_no_question(session):
    out = session.run("notify fail", "denied org.gnome.Snapshot camera 0 1000", "questions")
    assert out[-1] == "-"
    assert "Cannot show the notification: fake: no notification server" in session.stderr


@pytest.mark.parametrize("res,key,expected", [
    ("camera", "once", [DAEMON_CALL + "AllowOnce ('org.gnome.Snapshot', 'camera')"]),
    ("camera", "always", [DAEMON_CALL + "SetPolicy ('org.gnome.Snapshot', 'camera', 1)",
                          STORE + "('devices', true, 'camera', 'org.gnome.Snapshot', ['yes'])"]),
    ("camera", "never", [DAEMON_CALL + "SetPolicy ('org.gnome.Snapshot', 'camera', 2)",
                         STORE + "('devices', true, 'camera', 'org.gnome.Snapshot', ['no'])"]),
    # The camera portal knows no security keys or network
    ("security-key", "always", [DAEMON_CALL + "SetPolicy ('org.gnome.Snapshot', 'security-key', 1)"]),
    ("network", "never", [DAEMON_CALL + "SetPolicy ('org.gnome.Snapshot', 'network', 2)"]),
    # "Don't Allow" of a user who is not an administrator: closed, nothing set
    ("camera", "dismiss", []),
    ("camera", "default", []),
])
def test_each_answer_and_what_it_calls(session, res, key, expected):
    out = session.run(f"denied org.gnome.Snapshot {res} 0 1000", "log", f"action 7 {key}", "log", "questions")
    assert calls(out[3]) == expected
    assert out[4] == "-"


def test_answers_to_other_notifications_are_ignored(session):
    out = session.run("denied org.gnome.Snapshot camera 0 1000", "log", "action 99 always", "closed 98",
                      "notifysig ActivationToken (uint32 7, 'token')", "log", "questions")
    assert out[5:] == ["-", "7 org.gnome.Snapshot camera"]


def test_a_closed_question_is_forgotten(session):
    out = session.run("denied org.gnome.Snapshot camera 0 1000", "closed 7", "questions", "action 7 always", "log")
    assert out[2] == "-"
    assert calls(out[4]) == [SNAP_CAMERA, notify("Camera wants to use a camera", "camera-web-symbolic",
                                                 ADMIN_ACTIONS, desktop="org.gnome.Snapshot")]


def test_a_refused_answer_is_reported(session):
    session.run("answer fail", "denied org.gnome.Snapshot camera 0 1000", "action 7 once")
    assert "The answer did not count: fake: Not allowed" in session.stderr


def test_agent_main_listens_to_the_daemon_and_the_notifications(session):
    out = session.main(AA_BUSES="ok", AA_TERM_MS=200)
    # No session bus to watch the camera portal on: the questions still come
    assert "Refused cameras are not counted: " in session.stderr
    assert out == [
        # Only the daemon's own signals (sender: its bus name) count
        "subscribe system sender=org.plinius.AppAccess1 iface=org.plinius.AppAccess1 member=AccessDenied "
        "path=/org/plinius/AppAccess1 arg0=*",
        "subscribe session sender=org.freedesktop.Notifications iface=org.freedesktop.Notifications member=* "
        "path=/org/freedesktop/Notifications arg0=*",
        "exit 0",
    ]


@pytest.mark.parametrize("buses,message", [("nosession", "No session bus"), ("nosystem", "No system bus")])
def test_agent_main_needs_both_buses(session, buses, message):
    assert session.main(AA_BUSES=buses) == ["exit 1"]
    assert message in session.stderr


def test_the_question_in_german(session):
    if shutil.which("msgfmt") is None:
        pytest.skip("msgfmt missing")
    if "de_DE.utf8" not in subprocess.run(["locale", "-a"], capture_output=True, text=True).stdout:
        pytest.skip("no German locale on the host")
    mo = session.root / "locale/de/LC_MESSAGES/plinius-appaccess.mo"
    mo.parent.mkdir(parents=True)
    subprocess.run(["msgfmt", "--check", "-o", str(mo), str(APPACCESS / "de.po")], check=True)
    out = session.run("denied org.gnome.Snapshot security-key 0 1000", "log", LC_ALL="de_DE.UTF-8",
                      LANGUAGE="de")
    d, c = calls(out[1])
    # Phosh translates its own buttons
    assert d == dialog("org.gnome.Snapshot", "Kamera möchte einen Sicherheitsschlüssel verwenden",
                       "security-high-symbolic",
                       body="Der Zugriff wurde blockiert. Wenn du ihn erlaubst, versuche es in der App noch einmal.")
    assert "title=Kamera möchte einen Sicherheitsschlüssel verwenden " in c
    assert "body=Der Zugriff wurde blockiert. Wenn du ihn erlaubst, versuche es in der App noch einmal. " in c
    assert "actions=once,Einmal erlauben,always,Immer erlauben,never,Nicht erlauben " in c


# ---- plinius-appaccess-agent: the microphone's attempts WirePlumber's hook saw ----

REPORTS = "state/wireplumber/plinius-appaccess-microphone"


def reports(entries):
    """The hook's state file as WirePlumber's State writes it, for the harness's "file" """
    text = "[plinius-appaccess-microphone]\\n" + "".join(f"{k}={v}\\n" for k, v in entries.items())
    return f"file {REPORTS} {text}"


def attempt_call(app, pid, allowed):
    return DAEMON_CALL + f"CountAttempt ('{app}', {pid}, 'microphone', {'true' if allowed else 'false'})"


def test_reports_from_before_the_start_are_old_new_ones_are_passed_on_once(session):
    (session.root / REPORTS).parent.mkdir(parents=True)
    (session.root / REPORTS).write_text("[plinius-appaccess-microphone]\n101=refused 5000 -\n")
    out = session.run("mic start", "log",
                      reports({"101": "refused 5000 -", "105": "used 5001 -", "107": "refused 0 org.example.Flat",
                               "108": "bogus 1 -", "109": "refused", "110": "refused x y"}),
                      "mic read", "log", "mic read", "log")
    assert out[:2] == ["mic yes", "-"]
    # Only the new ones that make sense: a use of process 5001, a refusal of a Flatpak app by name
    assert calls(out[4]) == [attempt_call("", 5001, True), attempt_call("org.example.Flat", 0, False)]
    assert out[6] == "-"


def test_the_file_may_come_later_and_go(session):
    out = session.run("mic start", reports({"7": "refused 5000 -"}), "mic event 2", "log",
                      "mic event 3", "log", "file " + REPORTS + " junk", "mic event 1", "log")
    assert out[0] == "mic yes"
    assert out[3] == "-"                       # deleted: nothing to read
    assert calls(out[5]) == [attempt_call("", 5000, False)]
    assert out[8] == "-"                       # not a key file: no reports


def test_the_microphone_question_in_german(session):
    if shutil.which("msgfmt") is None:
        pytest.skip("msgfmt missing")
    if "de_DE.utf8" not in subprocess.run(["locale", "-a"], capture_output=True, text=True).stdout:
        pytest.skip("no German locale on the host")
    mo = session.root / "locale/de/LC_MESSAGES/plinius-appaccess.mo"
    mo.parent.mkdir(parents=True)
    subprocess.run(["msgfmt", "--check", "-o", str(mo), str(APPACCESS / "de.po")], check=True)
    assert session.last("title microphone Kamera", LC_ALL="de_DE.UTF-8", LANGUAGE="de") == \
        "Kamera möchte das Mikrofon verwenden"


# ---- plinius-appaccess-agent: the question in Phosh's access dialog ----

def test_phosh_asks_with_the_apps_name_and_three_buttons(session):
    out = session.run("dialog wait", "denied org.gnome.Snapshot camera 0 1000", "log", "dialogs", "questions")
    assert calls(out[2]) == [SNAP_CAMERA]
    assert out[3:] == ["org.gnome.Snapshot camera", "-"]


def test_phosh_shows_the_apps_icon(session):
    apps = session.root / "share/applications"
    (apps / "org.example.Cam.desktop").write_text(
        f"[Desktop Entry]\nType=Application\nName=Cam\nIcon=org.example.Cam\nExec={session.root}/bin/snapshot\n")
    out = session.run("dialog wait", "denied org.example.Cam camera 0 1000", "log")
    assert calls(out[2]) == [dialog("org.example.Cam", "Cam wants to use a camera", "org.example.Cam")]


def test_another_user_gets_once_and_dont_allow_from_phosh(session):
    out = session.run("ids 1000 1000 1000", "dialog wait", "denied org.gnome.Snapshot camera 0 1000", "log")
    assert calls(out[3]) == [dialog("org.gnome.Snapshot", "Camera wants to use a camera", "camera-web-symbolic",
                                    USER_BUTTONS)]


@pytest.mark.parametrize("ids,choice,expected", [
    ("1000 1000 1000,998", "once", [DAEMON_CALL + "AllowOnce ('org.gnome.Snapshot', 'camera')"]),
    ("1000 1000 1000,998", "always", [DAEMON_CALL + "SetPolicy ('org.gnome.Snapshot', 'camera', 1)",
                                      STORE + "('devices', true, 'camera', 'org.gnome.Snapshot', ['yes'])"]),
    ("1000 1000 1000,998", "never", [DAEMON_CALL + "SetPolicy ('org.gnome.Snapshot', 'camera', 2)",
                                     STORE + "('devices', true, 'camera', 'org.gnome.Snapshot', ['no'])"]),
    ("1000 1000 1000,998", "cancel", []),                       # swiped away: asked again next time
    # Another user's Don't Allow only closes it; Always Allow is not theirs to give
    ("1000 1000 1000", "never", []),
    ("1000 1000 1000", "always", []),
    ("1000 1000 1000", "once", [DAEMON_CALL + "AllowOnce ('org.gnome.Snapshot', 'camera')"]),
    # A Phosh without the three buttons: granted is once, denied nothing
    ("1000 1000 1000,998", "granted", [DAEMON_CALL + "AllowOnce ('org.gnome.Snapshot', 'camera')"]),
    ("1000 1000 1000,998", "denied", []),
])
def test_each_answer_in_phosh_and_what_it_calls(session, ids, choice, expected):
    out = session.run(f"ids {ids}", "dialog wait", "denied org.gnome.Snapshot camera 0 1000", "log",
                      f"choose {choice}", "log", "dialogs", "questions")
    assert calls(out[5]) == expected
    assert out[6:] == ["-", "-"]


def test_the_same_question_is_not_asked_twice_and_others_wait(session):
    out = session.run("dialog wait", "denied org.gnome.Snapshot camera 0 1000",
                      "denied org.gnome.Snapshot camera 0 1000",           # the same: nothing
                      "denied org.gnome.Snapshot network 0 1000",          # another: waits
                      "denied org.gnome.Snapshot network 0 1000",          # waiting already
                      "denied org.example.Tool camera 0 1000",
                      "log", "dialogs",
                      "choose once", "log", "dialogs",
                      "choose never", "log", "dialogs",
                      "choose cancel", "log", "dialogs", "choose once", "log")
    assert calls(out[6]) == [SNAP_CAMERA]
    assert out[7] == "org.gnome.Snapshot camera | wait org.gnome.Snapshot network | wait org.example.Tool camera"
    assert calls(out[9]) == [DAEMON_CALL + "AllowOnce ('org.gnome.Snapshot', 'camera')",
                             dialog("org.gnome.Snapshot", "Camera wants to use the network",
                                    "network-workgroup-symbolic", n=2)]
    assert out[10] == "org.gnome.Snapshot network | wait org.example.Tool camera"
    assert calls(out[12]) == [DAEMON_CALL + "SetPolicy ('org.gnome.Snapshot', 'network', 2)",
                              dialog("org.example.Tool", "org.example.Tool wants to use a camera",
                                     "camera-web-symbolic", n=3)]
    assert out[13] == "org.example.Tool camera"
    assert calls(out[15]) == []
    assert out[16] == "-"
    # Nothing left to answer
    assert calls(out[18]) == []


def test_a_dialog_phosh_cannot_show_becomes_a_notification(session):
    # Phosh's dialog busy with a portal's question, or Phosh gone while it was open
    out = session.run("dialog wait", "denied org.gnome.Snapshot camera 0 1000",
                      "denied org.gnome.Snapshot network 0 1000", "log",
                      "choose error", "log", "dialogs", "questions")
    assert calls(out[3]) == [SNAP_CAMERA]
    assert calls(out[5]) == [notify("Camera wants to use a camera", "camera-web-symbolic", ADMIN_ACTIONS,
                                    desktop="org.gnome.Snapshot"),
                             dialog("org.gnome.Snapshot", "Camera wants to use the network",
                                    "network-workgroup-symbolic", n=2)]
    assert out[6:] == ["org.gnome.Snapshot network", "7 org.gnome.Snapshot camera"]
    assert "No dialog from Phosh, a notification instead: fake: no dialog" in session.stderr


# ---- plinius-appaccess-agent: refusals of the camera portal ----

REQUEST = "/org/freedesktop/portal/desktop/request/"
APP = ":1.42"
PID_CALL = ("session org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus "
            "GetConnectionUnixProcessID (':1.42',)")


def refused(pid=4242):
    return DAEMON_CALL + f"CountRefused ({pid}, 'camera')"


def cam_call(sender=APP, token=None, body=None):
    if body is None:
        body = f"({{'handle_token': <'{token}'>}},)" if token else "(@a{sv} {},)"
    return (f"msg call {sender} org.freedesktop.portal.Desktop /org/freedesktop/portal/desktop "
            f"org.freedesktop.portal.Camera AccessCamera {body}")


def response(path, code, dest=APP, iface="org.freedesktop.portal.Request", member="Response", body=None):
    body = body or f"(uint32 {code}, @a{{sv}} {{}})"
    return f"msg signal :1.5 {dest} {path} {iface} {member} {body}"


@pytest.mark.parametrize("token", ["t1", None])
@pytest.mark.parametrize("code,counted", [(0, False), (1, True), (2, True)])
def test_a_refused_portal_request_is_counted(session, token, code, counted):
    # 1: the user said no, 2: the permission store said no; 0 the app got the camera
    path = REQUEST + "1_42/" + (token or "generated7")
    out = session.run(cam_call(token=token), "log", "requests", response(path, code), "log", "requests")
    assert out[0] == out[3] == "dropped"
    assert calls(out[1]) == [PID_CALL]
    assert out[2] == f"{APP} {REQUEST}1_42/{token or '*'} pid=4242 looking=0 refused=0"
    assert calls(out[4]) == ([refused()] if counted else [])
    assert out[5] == "-"


@pytest.mark.parametrize("other", [
    response(REQUEST + "1_42/t1", 2, dest=":1.43"),                         # another app's answer
    response(REQUEST + "1_42/t9", 2),                                       # another request of the app
    response(REQUEST + "1_42/t1", 2, dest="-"),                             # not sent to the app
    response(REQUEST + "1_42/t1", 2, iface="org.freedesktop.portal.Other"),
    response(REQUEST + "1_42/t1", 2, member="Closed"),
    response(REQUEST + "1_42/t1", 2, body="(uint32 2,)"),                   # not a Response's body
    response(REQUEST + "1_42/t1", 2, body="-"),
])
def test_other_answers_leave_the_request_waiting(session, other):
    out = session.run(cam_call(token="t1"), "log", other, "log", "requests",
                      response(REQUEST + "1_42/t1", 2), "log")
    assert out[2] == "dropped"
    assert out[3] == "-"
    assert out[4].startswith(f"{APP} {REQUEST}1_42/t1 ")
    assert calls(out[6]) == [refused()]


def test_without_a_token_the_first_answer_below_the_apps_requests_counts(session):
    # The prefix ends with '/': 1_420 is another app (":1.420")
    out = session.run(cam_call(), cam_call(), "log", "requests",
                      response(REQUEST + "1_420/t1", 2), response(REQUEST + "1_42/a", 2), "log", "requests",
                      response(REQUEST + "1_42/b", 0), "log", "requests")
    assert out[3] == " | ".join([f"{APP} {REQUEST}1_42/* pid=4242 looking=0 refused=0"] * 2)
    assert calls(out[6]) == [refused()]
    assert out[7] == f"{APP} {REQUEST}1_42/* pid=4242 looking=0 refused=0"
    assert calls(out[9]) == []
    assert out[10] == "-"


def test_a_sender_without_a_colon_and_tokens_per_request(session):
    out = session.run(cam_call(sender="org.example.App", token="x"), cam_call(token="t1"), cam_call(token="t2"),
                      "requests", response(REQUEST + "1_42/t2", 2), "requests")
    assert out[3] == (f"org.example.App {REQUEST}org_example_App/x pid=4242 looking=0 refused=0 | "
                      f"{APP} {REQUEST}1_42/t1 pid=4242 looking=0 refused=0 | "
                      f"{APP} {REQUEST}1_42/t2 pid=4242 looking=0 refused=0")
    assert out[5] == (f"org.example.App {REQUEST}org_example_App/x pid=4242 looking=0 refused=0 | "
                      f"{APP} {REQUEST}1_42/t1 pid=4242 looking=0 refused=0")


@pytest.mark.parametrize("answer,code,counted", [
    ("pid 5000", 2, True),     # refused before the bus said whose it is: counted with the answer
    ("pid 5000", 0, False),
    ("pid fail", 2, False),    # the app was gone already
])
def test_a_refusal_before_the_pid_is_known(session, answer, code, counted):
    out = session.run("pid defer", cam_call(token="t1"), "requests", response(REQUEST + "1_42/t1", code),
                      "requests", "log", answer, "flush", "log")
    assert out[2] == f"{APP} {REQUEST}1_42/t1 pid=0 looking=1 refused=0"
    assert out[4] == "-"
    assert calls(out[5]) == [PID_CALL]
    assert calls(out[8]) == ([refused(5000)] if counted else [])


def test_no_pid_from_the_bus_counts_nothing(session):
    out = session.run("pid fail", cam_call(token="t1"), "requests", response(REQUEST + "1_42/t1", 2), "log")
    assert out[2] == f"{APP} {REQUEST}1_42/t1 pid=0 looking=0 refused=0"
    assert calls(out[4]) == [PID_CALL]


@pytest.mark.parametrize("later,left", [(599, True), (601, False)])
def test_unanswered_requests_are_forgotten_after_ten_minutes(session, later, left):
    out = session.run(cam_call(token="t1"), f"clock +{later}", cam_call(token="t2"), "requests",
                      response(REQUEST + "1_42/t1", 2), "log")
    assert out[3].count(" | ") == (1 if left else 0)
    assert calls(out[5]) == [PID_CALL, PID_CALL] + ([refused()] if left else [])


def test_an_answer_after_ten_minutes_counts_nothing(session):
    out = session.run(cam_call(token="t1"), "clock +601", response(REQUEST + "1_42/t1", 2), "log", "requests")
    assert calls(out[3]) == [PID_CALL]
    assert out[4] == "-"


@pytest.mark.parametrize("msg,kept", [
    # Somebody else's calls and signals never reach GDBus; replies (BecomeMonitor's) do
    ("msg call :1.7 org.freedesktop.Notifications /org/freedesktop/Notifications "
     "org.freedesktop.Notifications Notify ('x',)", False),
    ("msg signal org.freedesktop.DBus :1.9 /org/freedesktop/DBus org.freedesktop.DBus NameAcquired (':1.9',)",
     False),
    (cam_call(sender="-"), False),                                    # no sender
    (cam_call(body="-"), False),                                      # no body
    (cam_call(body="('t1',)"), False),                                # not AccessCamera's body
    (cam_call(body="(@a{sv} {}, 'x')"), False),
    ("msg call :1.42 org.freedesktop.portal.Desktop /org/freedesktop/portal/desktop "
     "org.freedesktop.portal.Camera IsCameraPresent -", False),
    ("msg return org.freedesktop.DBus :1.9 - - - ()", True),
    ("outgoing call :1.9 org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus.Monitoring "
     "BecomeMonitor (@as [], uint32 0)", True),
])
def test_the_monitor_filter(session, msg, kept):
    out = session.run(msg, "requests", "log")
    assert out == ["kept" if kept else "dropped", "-", "-"]


def test_a_count_the_daemon_refuses_is_reported(session):
    session.run("answer fail", cam_call(token="t1"), response(REQUEST + "1_42/t1", 2))
    assert "An attempt did not count: fake: Not allowed" in session.stderr


def test_bad_monitor_lines(session):
    assert session.run("msg call :1.42", "monitor") == ["<bad message>", "monitor no"]
    assert "Refused cameras are not counted: " in session.stderr


# On real buses: the agent's main() with both buses private dbus-daemons of the test

DHW = REPO / "tests/daemons-hw/dhw_daemon_bus.py"
AA_XML = """<node><interface name='org.plinius.AppAccess1'>
  <method name='CountRefused'><arg direction='in' type='u'/><arg direction='in' type='s'/>
    <arg direction='out' type='s'/></method>
  <method name='AllowOnce'><arg direction='in' type='s'/><arg direction='in' type='s'/></method>
  <method name='SetPolicy'><arg direction='in' type='s'/><arg direction='in' type='s'/>
    <arg direction='in' type='u'/></method>
</interface></node>"""
# Phosh's access dialog as phosh-plinius 0109 serves it (data/phosh-shell.portal, src/dbus)
PHOSH_XML = """<node><interface name='org.freedesktop.impl.portal.Access'>
  <method name='AccessDialog'><arg direction='in' type='o'/><arg direction='in' type='s'/>
    <arg direction='in' type='s'/><arg direction='in' type='s'/><arg direction='in' type='s'/>
    <arg direction='in' type='s'/><arg direction='in' type='a{sv}'/>
    <arg direction='out' type='u'/><arg direction='out' type='a{sv}'/></method>
</interface></node>"""
PORTAL_XML = """<node><interface name='org.freedesktop.portal.Camera'>
  <method name='AccessCamera'><arg direction='in' type='a{sv}'/><arg direction='out' type='o'/></method>
</interface></node>"""
COUNTING = "Counting the refusals of the camera portal"


@pytest.fixture
def dhw():
    pytest.importorskip("gi")
    return load_script(DHW, "dsec_dhw_daemon_bus")


class RealSession:
    """A session bus with a camera portal, a system bus with plinius-appaccessd, the agent"""

    def __init__(self, dhw, session, root, deny_monitor=False, monkeypatch=None, phosh_choice=None):
        from gi.repository import GLib
        self.GLib = GLib
        self.dhw = dhw
        if deny_monitor:
            monkeypatch.setattr(dhw, "BUS_CONFIG", dhw.BUS_CONFIG.replace(
                "</policy>", '<deny send_destination="org.freedesktop.DBus" '
                             'send_interface="org.freedesktop.DBus.Monitoring"/></policy>'))
        for d in ("sbus", "ybus"):
            (root / d).mkdir()
        self.session_bus = dhw.PrivateBus(root / "sbus")
        self.system_bus = dhw.PrivateBus(root / "ybus")
        self.daemon = dhw.FakeService(self.system_bus, "org.plinius.AppAccess1", "/org/plinius/AppAccess1",
                                      AA_XML, {"CountRefused": lambda p: GLib.Variant("(s)", ("org.example.App",)),
                                               "AllowOnce": lambda p: None, "SetPolicy": lambda p: None})
        self.phosh = None
        if phosh_choice:
            self.phosh = dhw.FakeService(
                self.session_bus, "mobi.phosh.Shell.Portal", "/org/freedesktop/portal/desktop", PHOSH_XML,
                {"AccessDialog": lambda p: GLib.Variant(
                    "(ua{sv})", (0, {"plinius-choice": GLib.Variant("s", phosh_choice)}))})
        self.portal = dhw.FakeService(self.session_bus, "org.freedesktop.portal.Desktop",
                                      "/org/freedesktop/portal/desktop", PORTAL_XML,
                                      {"AccessCamera": lambda p: GLib.Variant("(o)", (REQUEST + "x/y",))})
        env = san_env(**{**session.env, "AA_BUSES": "real", "G_MESSAGES_DEBUG": "all",
                         "DBUS_SESSION_BUS_ADDRESS": self.session_bus.address,
                         "DBUS_SYSTEM_BUS_ADDRESS": self.system_bus.address})
        self.agent = dhw.Daemon([session.binary, "main"], env, cwd=root)
        self.apps = []

    def app(self):
        c = self.session_bus.connect()
        self.apps.append(c)
        return c

    def access_camera(self, app, token=None):
        GLib = self.GLib
        options = {"handle_token": GLib.Variant("s", token)} if token else {}
        app.call_sync("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
                      "org.freedesktop.portal.Camera", "AccessCamera", GLib.Variant("(a{sv})", (options,)),
                      GLib.VariantType("(o)"), 0, 5000, None)

    def respond(self, dest, path, code):
        """The portal's Response, sent to @dest alone (None: to everyone)"""
        done = threading.Event()

        def emit():
            self.portal.conn.emit_signal(dest, path, "org.freedesktop.portal.Request", "Response",
                                         self.GLib.Variant("(ua{sv})", (code, {})))
            self.portal.conn.flush_sync(None)
            done.set()
            return False

        self.portal.ctx.invoke_full(0, emit)
        assert done.wait(10)

    def counted(self):
        return [c for c in self.daemon.calls if c[0] == "CountRefused"]

    def close(self):
        result = self.agent.stop()
        for c in self.apps:
            c.close_sync(None)
        self.portal.close()
        self.daemon.close()
        if self.phosh:
            self.phosh.close()
        self.session_bus.close()
        self.system_bus.close()
        return result


def request_path(app, token):
    return REQUEST + app.get_unique_name()[1:].replace(".", "_") + "/" + token


def test_on_real_buses_the_portals_refusals_are_counted(dhw, session, tmp_path):
    real = RealSession(dhw, session, tmp_path)
    try:
        dhw.poll(lambda: any(COUNTING in line for line in real.agent.out), what="the monitor")
        app, other = real.app(), real.app()
        pid = os.getpid()
        # Refused by the permission store, with a handle_token
        real.access_camera(app, "t1")
        real.respond(app.get_unique_name(), request_path(app, "t1"), 2)
        dhw.poll(lambda: len(real.counted()) == 1, what="the first count")
        # Answers that are not the request's, then the request's own: granted
        real.access_camera(app, "t2")
        real.respond(other.get_unique_name(), request_path(other, "t2"), 2)   # an app that asked nothing
        real.respond(app.get_unique_name(), request_path(app, "t9"), 2)       # another request of the app
        real.respond(None, request_path(app, "t2"), 2)                          # to everyone
        real.respond(app.get_unique_name(), request_path(app, "t2"), 0)
        # Without a token: the portal's own path below the app's requests; the user said no
        real.access_camera(app)
        real.respond(app.get_unique_name(), request_path(app, "generated1"), 1)
        dhw.poll(lambda: len(real.counted()) == 2, what="the second count")
        time.sleep(0.3)
        assert real.counted() == [("CountRefused", (pid, "camera"))] * 2
    finally:
        code, stderr, stdout = real.close()
    assert code == 0 and "exit 0" in stdout, stderr
    assert "not counted" not in stderr and "did not count" not in stderr


def test_on_real_buses_a_refused_monitor_keeps_the_questions(dhw, session, tmp_path, monkeypatch):
    real = RealSession(dhw, session, tmp_path, deny_monitor=True, monkeypatch=monkeypatch)
    try:
        real.agent.wait_for("Refused cameras are not counted: ")
        app = real.app()
        real.access_camera(app, "t1")
        real.respond(app.get_unique_name(), request_path(app, "t1"), 2)
        time.sleep(0.3)
        assert real.counted() == []
    finally:
        code, stderr, stdout = real.close()
    assert code == 0 and "exit 0" in stdout, stderr
    assert "AccessDenied" in stdout     # still listening to the daemon


def test_on_real_buses_the_question_is_phoshs_dialog(dhw, session, tmp_path):
    from gi.repository import GLib
    real = RealSession(dhw, session, tmp_path, phosh_choice="once")
    try:
        dhw.poll(lambda: any(COUNTING in line for line in real.agent.out), what="the monitor")
        real.daemon.emit("/org/plinius/AppAccess1", "org.plinius.AppAccess1", "AccessDenied",
                         GLib.Variant("(ssuu)", ("org.gnome.Snapshot", "camera", 0, 1000)))
        dhw.poll(lambda: any(c[0] == "AllowOnce" for c in real.daemon.calls), what="the answer")
        (method, args), = real.phosh.calls
        assert method == "AccessDialog"
        assert args[:6] == ("/org/plinius/AppAccessAgent/dialog/1", "org.gnome.Snapshot", "",
                            "Camera wants to use a camera", "", BODY)
        assert args[6] == {"icon": "camera-web-symbolic", "plinius-once": True,
                           "plinius-buttons": ["once", "always", "never"]}
        assert [c for c in real.daemon.calls if c[0] != "CountRefused"] == [
            ("AllowOnce", ("org.gnome.Snapshot", "camera"))]
    finally:
        code, stderr, stdout = real.close()
    assert code == 0 and "exit 0" in stdout, stderr
    assert "a notification instead" not in stderr
