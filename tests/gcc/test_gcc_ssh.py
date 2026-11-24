# Allowed Networks on the Secure Shell page (cc-plinius-ssh.c): three switches that show
# /etc/plinius-ssh.conf (read as plinius-ssh-set reads it), each change runs
# "pkexec plinius-ssh-set allow|deny usb|local|all"; "All Networks" includes the other two; the
# group can only be used while GNOME's SSH switch is on.
#
# The files are in the program's root in tmp; pkexec is a stub that runs the tool, the tool a
# script in the root that writes the file the way plinius-ssh-set does (the last line of a key
# counts). How the real tool reads the file is checked against its own get() function.

import subprocess

import pytest

from conftest import PKG
from gcc_harness import (build_panel, call, dump, gcc_session, gcc_world, set_active,  # noqa: F401
                         settle, wait_for)

GLUE = r'''
#include "gcc_driver.h"
#include "cc-plinius-ssh.h"

static gboolean set_up;

void
gcc_test_setup (GccTest *t)
{
    /* GNOME's switch of the Secure Shell page, on unless the test says otherwise */
    GtkWidget *group = adw_preferences_group_new ();
    GtkWidget *row = adw_switch_row_new ();

    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), "Secure Shell");
    adw_switch_row_set_active (ADW_SWITCH_ROW (row), !gcc_test_param (t, "ssh_off"));
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), row);
    adw_preferences_page_add (t->page, ADW_PREFERENCES_GROUP (group));
    set_up = cc_plinius_ssh_setup (t->page, ADW_SWITCH_ROW (row));
}

gboolean
gcc_test_call (GccTest *t, const char *name, JsonArray *args, JsonBuilder *result)
{
    if (g_str_equal (name, "set_up")) {
        json_builder_add_boolean_value (result, set_up);
        return TRUE;
    }
    return FALSE;
}
'''

TOOL = "/usr/lib/plinius-ssh/plinius-ssh-set"
CONF = "/etc/plinius-ssh.conf"
REAL_TOOL = PKG / "plinius-ssh" / "plinius-ssh-set"

ROWS = ("USB Network", "Local Networks", "All Networks")
KEYS = ("usb", "local", "all")
DEFAULTS = ("yes", "no", "no")


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    reps = [(f'"{p}"', f'"{{root}}{p}"') for p in (TOOL, CONF)]
    return build_panel(gcc_session, tmp_path_factory.mktemp("ssh"), "ssh", ["cc-plinius-ssh.c"], GLUE,
                       replacements={"cc-plinius-ssh.c": reps})


class Ssh:
    def __init__(self, root, world):
        import shutil
        self.root = root
        self.world = world
        for d in list(root.iterdir()):
            shutil.rmtree(d) if d.is_dir() else d.unlink()
        self.go = root / "go"
        self.tool()
        self.pkexec()

    def path(self, p):
        return self.root / p.lstrip("/")

    def tool(self, code=0, wait=False, kill=False):
        """plinius-ssh-set: appends key=yes|no (the last line counts), exits with code."""
        conf = self.path(CONF)
        lines = ["#!/bin/sh", f'echo "$@" >> "{self.root}/tool.log"']
        if wait:
            lines.append(f'while [ ! -e "{self.go}" ]; do sleep 0.02; done')
        if kill:
            lines.append("kill -9 $$")
        lines += [f'mkdir -p "{conf.parent}"',
                  f'if [ "$1" = allow ]; then echo "$2=yes" >> "{conf}"; '
                  f'else echo "$2=no" >> "{conf}"; fi',
                  f"exit {code}"]
        f = self.path(TOOL)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("\n".join(lines) + "\n")
        f.chmod(0o755)

    def pkexec(self, then='exec "$@"'):
        """pkexec takes a moment (the PIN prompt), so the waiting rows show, then does `then`."""
        self.world.fake_bin.stub("pkexec", f"sleep 0.3\n{then}")

    def conf(self, text):
        f = self.path(CONF)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)

    def go_step(self):
        return {"op": "write_file", "path": str(self.go), "text": ""}


@pytest.fixture
def ssh(program, gcc_world):
    return Ssh(program.root, gcc_world)


def switches(screen):
    """{title: (active, sensitive)} of the three switches."""
    group = screen.within(type="AdwPreferencesGroup", text="Allowed Networks")
    return {r["text"]: (r["active"], r.get("sensitive", True)) for r in group.find(type="AdwSwitchRow")}


def tool_reads(conf_file):
    """What the real plinius-ssh-set reads from conf_file: yes/no for each key (its get())."""
    script = (f'conf="{conf_file}"\n'
              f'eval "$(sed -n "/^get() {{/,/^}}/p" "{REAL_TOOL}")"\n'
              + "".join(f'get {k} {d}\n' for k, d in zip(KEYS, DEFAULTS)))
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout
    return tuple(v == "yes" for v in out.split())


def test_without_plinius_ssh_nothing_shows(program, gcc_world, ssh):
    ssh.path(TOOL).chmod(0o644)
    r = gcc_world.run(program, [settle(), call("set_up"), dump("main")])
    assert r.value("set_up") is False
    assert not r.dump("main").has(text="Allowed Networks")


def test_without_a_file_the_defaults(program, gcc_world, ssh):
    r = gcc_world.run(program, [settle(), call("set_up"), dump("main")])
    assert r.value("set_up") is True
    s = r.dump("main")
    group = s.one(type="AdwPreferencesGroup", text="Allowed Networks")
    assert group["subtitle"] == ("Where logins over SSH are taken from. They need a key; allowing a "
                                 "network asks for the PIN.")
    assert s.rows()[1:] == [
        ("USB Network", "A computer on the USB cable, with Network on in the USB settings"),
        ("Local Networks", "Wi-Fi, hotspot and Ethernet, from local addresses only"),
        ("All Networks", "Also mobile data, VPN and the internet")]
    assert switches(s) == {"USB Network": (True, True), "Local Networks": (False, True),
                           "All Networks": (False, True)}
    assert tool_reads(ssh.path(CONF)) == (True, False, False)
    assert not r.warnings()


def test_all_networks_includes_the_others(program, gcc_world, ssh):
    ssh.conf("usb=no\nlocal=no\nall=yes\n")
    r = gcc_world.run(program, [settle(), dump("main")])
    # They show on and cannot be changed while "All Networks" is on
    assert switches(r.dump("main")) == {"USB Network": (True, False), "Local Networks": (True, False),
                                        "All Networks": (True, True)}


@pytest.mark.parametrize("text", [
    "usb=no\nlocal=yes\n",
    "usb=yes\nusb=no\n",                       # the last line of a key counts
    "  local = yes   # a comment\n",           # spaces around
    "# local=yes\nall=no\n",                   # a comment line is no choice
    "localx=yes\nall\nusb\n=yes\n",            # other keys, lines without "="
    "local=yes2\n",                            # what follows the word does not matter
])
def test_the_file_as_plinius_ssh_set_reads_it(program, gcc_world, ssh, text):
    ssh.conf(text)
    r = gcc_world.run(program, [settle(), dump("main")])
    shown = switches(r.dump("main"))
    assert tuple(shown[t][0] for t in ROWS) == tool_reads(ssh.path(CONF))


@pytest.mark.parametrize("text", [
    "local=yes\nlocal=maybe\n",
    "local=yes\nlocal=Yes\n",
    "local=yesterday\n",
    "local=yes\nlocal=\n",                     # an empty last line too
    "local=yes\nlocal = no-way\nlocal=YES\n",  # the word before the "-" counts, YES is none
    "usb=no\nusb=off\n",                       # back to the default: USB on
])
def test_an_odd_last_line_counts_as_the_tool_reads_it(program, gcc_world, ssh, text):
    ssh.conf(text)
    r = gcc_world.run(program, [settle(), dump("main")])
    shown = switches(r.dump("main"))
    assert tool_reads(ssh.path(CONF)) == (True, False, False)
    assert tuple(shown[t][0] for t in ROWS) == (True, False, False)


def test_the_group_follows_gnomes_ssh_switch(program, gcc_world, ssh):
    r = gcc_world.run(program, [settle(), dump("off"),
                                set_active(True, type="AdwSwitchRow", text="Secure Shell"), settle(),
                                dump("on")], params={"ssh_off": "1"})
    assert r.dump("off").one(type="AdwPreferencesGroup", text="Allowed Networks")["sensitive"] is False
    assert "sensitive" not in r.dump("on").one(type="AdwPreferencesGroup", text="Allowed Networks")


@pytest.mark.parametrize("row, active, args, after", [
    ("Local Networks", True, ["allow", "local"], (True, True, False)),
    ("USB Network", False, ["deny", "usb"], (False, False, False)),
    ("All Networks", True, ["allow", "all"], (True, True, True)),
])
def test_a_switch_runs_the_tool(program, gcc_world, ssh, row, active, args, after):
    ssh.tool(wait=True)
    r = gcc_world.run(program, [
        settle(), set_active(active, type="AdwSwitchRow", text=row), settle(), dump("busy"),
        ssh.go_step(), wait_for(type="AdwSwitchRow", text="All Networks", sensitive=True), settle(),
        dump("done")])
    assert gcc_world.fake_bin.calls("pkexec") == [[str(ssh.path(TOOL)), *args]]
    # One change at a time while pkexec (and its PIN prompt) runs
    assert all(not sensitive for _a, sensitive in switches(r.dump("busy")).values())
    done = switches(r.dump("done"))
    assert tuple(done[t][0] for t in ROWS) == after
    assert done["All Networks"][1] is True
    assert done["USB Network"][1] is done["Local Networks"][1] is (not after[2])
    assert not r.warnings()


def test_all_off_gives_the_others_back(program, gcc_world, ssh):
    ssh.conf("usb=yes\nlocal=no\nall=yes\n")
    r = gcc_world.run(program, [
        settle(), set_active(False, type="AdwSwitchRow", text="All Networks"), settle(),
        wait_for(type="AdwSwitchRow", text="Local Networks", sensitive=True), settle(), dump("done")])
    assert gcc_world.fake_bin.calls("pkexec") == [[str(ssh.path(TOOL)), "deny", "all"]]
    assert switches(r.dump("done")) == {"USB Network": (True, True), "Local Networks": (False, True),
                                        "All Networks": (False, True)}


@pytest.mark.parametrize("code", [126, 127])
def test_a_dismissed_prompt_puts_the_switch_back(program, gcc_world, ssh, code):
    ssh.pkexec(f"exit {code}")
    r = gcc_world.run(program, [
        settle(), set_active(True, type="AdwSwitchRow", text="Local Networks"), settle(), dump("busy"),
        wait_for(type="AdwSwitchRow", text="All Networks", sensitive=True), settle(), dump("done")])
    assert switches(r.dump("busy"))["Local Networks"] == (True, False)
    assert switches(r.dump("done"))["Local Networks"] == (False, True)
    assert not r.warnings()


def test_a_failing_tool_warns_and_shows_the_file(program, gcc_world, ssh):
    ssh.tool(code=2)
    r = gcc_world.run(program, [
        settle(), set_active(True, type="AdwSwitchRow", text="Local Networks"), settle(),
        wait_for(type="AdwSwitchRow", text="All Networks", sensitive=True), settle(), dump("done")])
    # The tool wrote the file before it failed: what the file says shows
    assert switches(r.dump("done"))["Local Networks"] == (True, True)
    assert [m["message"] for m in r.warnings()] == [f"{ssh.path(TOOL)} exited with 2"]


def test_a_tool_killed_by_a_signal_warns(program, gcc_world, ssh):
    ssh.tool(kill=True)
    r = gcc_world.run(program, [
        settle(), set_active(True, type="AdwSwitchRow", text="All Networks"), settle(),
        wait_for(type="AdwSwitchRow", text="All Networks", sensitive=True), settle(), dump("done")])
    assert switches(r.dump("done"))["All Networks"] == (False, True)
    assert [m["message"] for m in r.warnings()] == [f"{ssh.path(TOOL)} exited with -1"]


def test_without_pkexec_it_warns_and_shows_the_file(program, gcc_world, ssh):
    (gcc_world.fake_bin.path / "pkexec").unlink()
    r = gcc_world.run(program, [settle(), set_active(False, type="AdwSwitchRow", text="USB Network"),
                                settle(), dump("done")])
    assert switches(r.dump("done")) == {"USB Network": (True, True), "Local Networks": (False, True),
                                        "All Networks": (False, True)}
    # Said once: the switch is put back after its own change, so it does not try again
    assert [m["message"] for m in r.warnings()] == [
        f"Cannot run pkexec {ssh.path(TOOL)}: Failed to execute child process “pkexec” "
        "(No such file or directory)"]
    assert not (ssh.root / "tool.log").exists()

