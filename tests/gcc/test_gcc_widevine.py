# Protected Content on the Privacy page (cc-plinius-widevine.c): the switch shows the marker file
# of plinius-widevine and the subtitle the installed version (the link "current"); switching on
# asks first and then runs "pkexec plinius-widevine-set on", switching off runs "... off" without
# asking; while it runs the row waits with a spinner, afterwards it shows what is true again.
#
# The files are in the program's root in tmp; pkexec is a stub that runs the tool, and the tool
# is a script in the root that changes the marker and the link as plinius-widevine-set would.

import pytest

from gcc_harness import (build_panel, click, dump, gcc_session, gcc_world, set_active,  # noqa: F401
                         settle, wait_for)

GLUE = r'''
#include "gcc_driver.h"
#include "cc-plinius-widevine.h"

void
gcc_test_setup (GccTest *t)
{
    cc_plinius_widevine_add (t->page);
}
'''

TOOL = "/usr/lib/plinius-widevine/plinius-widevine-set"
MARKER = "/etc/plinius-widevine/enabled"
CURRENT = "/var/lib/plinius-widevine/current"

OFF_TEXT = "For streaming services such as Netflix and Prime Video in Firefox"
ASK_BODY = ("Streaming services such as Netflix and Prime Video only play with Widevine, a "
            "closed-source module from Google. It may not be passed on, so the phone downloads it "
            "from Google itself (about 150 MB).\n\nOn Linux, services play at most in HD (720p). "
            "Firefox uses the module after a restart.")


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    reps = [(f'"{p}"', f'"{{root}}{p}"') for p in (TOOL, MARKER, CURRENT)]
    return build_panel(gcc_session, tmp_path_factory.mktemp("widevine"), "widevine",
                       ["cc-plinius-widevine.c"], GLUE,
                       replacements={"cc-plinius-widevine.c": reps})


class Widevine:
    """plinius-widevine in the program's root: the tool, the marker and the installed version."""

    def __init__(self, root, world):
        import shutil
        self.root = root
        self.world = world
        for d in list(root.iterdir()):
            shutil.rmtree(d) if d.is_dir() else d.unlink()
        self.go = root / "go"

    def path(self, p):
        return self.root / p.lstrip("/")

    def tool(self, on_code=0, off_code=0, on_installs="4.10.2830.0", wait=False, kill=False):
        """The tool: "on" sets the marker and, with on_installs, the version; "off" removes both.
        wait: it waits for the file "go" first (the download takes a while); kill: it dies of
        a signal."""
        marker, current = self.path(MARKER), self.path(CURRENT)
        versions = current.parent / "versions"
        lines = ["#!/bin/sh", f'echo "$@" >> "{self.root}/tool.log"']
        if wait:
            lines.append(f'while [ ! -e "{self.go}" ]; do sleep 0.02; done')
        if kill:
            lines.append("kill -9 $$")
        lines += ['case "$1" in',
                  f'on) mkdir -p "{marker.parent}"; touch "{marker}"']
        if on_installs:
            lines.append(f'    mkdir -p "{versions}/{on_installs}"; '
                         f'ln -sfn "{versions}/{on_installs}" "{current}"')
        lines += [f"    exit {on_code};;",
                  f'off) rm -f "{marker}" "{current}"; exit {off_code};;',
                  "esac"]
        f = self.path(TOOL)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("\n".join(lines) + "\n")
        f.chmod(0o755)

    def pkexec(self, then='exec "$@"'):
        """pkexec takes a moment (its prompt), so the waiting row shows, then does `then`."""
        self.world.fake_bin.stub("pkexec", f"sleep 0.3\n{then}")

    def installed(self, version):
        self.path(MARKER).parent.mkdir(parents=True, exist_ok=True)
        self.path(MARKER).touch()
        if version:
            target = self.path(CURRENT).parent / "versions" / version
            target.mkdir(parents=True, exist_ok=True)
            self.path(CURRENT).symlink_to(target)

    def tool_calls(self):
        log = self.root / "tool.log"
        return log.read_text().splitlines() if log.exists() else []

    def go_step(self):
        return {"op": "write_file", "path": str(self.go), "text": ""}


@pytest.fixture
def widevine(program, gcc_world):
    w = Widevine(program.root, gcc_world)
    w.tool()
    w.pkexec()
    return w


def row(screen):
    return screen.one(type="AdwSwitchRow", text="Play DRM Videos")


def state(screen):
    r = row(screen)
    return r["active"], r["subtitle"], r.get("sensitive", True), screen.within(
        type="AdwSwitchRow", text="Play DRM Videos").has(type="AdwSpinner")


SWITCH = dict(type="AdwSwitchRow", text="Play DRM Videos")


def test_without_plinius_widevine_nothing_shows(program, gcc_world, widevine):
    widevine.path(TOOL).unlink()
    r = gcc_world.run(program, [settle(), dump("main")])
    assert not r.dump("main").has(text="Protected Content")
    assert not r.dump("main").has(type="AdwSwitchRow")


def test_a_tool_that_cannot_run_is_no_plinius_widevine(program, gcc_world, widevine):
    widevine.path(TOOL).chmod(0o644)
    r = gcc_world.run(program, [settle(), dump("main")])
    assert not r.dump("main").has(type="AdwSwitchRow")


def test_off_by_default(program, gcc_world, widevine):
    r = gcc_world.run(program, [settle(), dump("main")])
    s = r.dump("main")
    assert s.one(type="AdwPreferencesGroup", text="Protected Content")
    assert state(s) == (False, OFF_TEXT, True, False)
    assert not r.warnings()


@pytest.mark.parametrize("version, subtitle", [
    ("4.10.2830.0", "Widevine 4.10.2830.0 from Google"),
    (None, "Not downloaded yet, tries again later"),
])
def test_on_shows_what_is_installed(program, gcc_world, widevine, version, subtitle):
    widevine.installed(version)
    r = gcc_world.run(program, [settle(), dump("main")])
    assert state(r.dump("main")) == (True, subtitle, True, False)


def test_a_version_without_the_marker_counts_as_off(program, gcc_world, widevine):
    widevine.installed("4.10.2830.0")
    widevine.path(MARKER).unlink()
    r = gcc_world.run(program, [settle(), dump("main")])
    assert state(r.dump("main")) == (False, OFF_TEXT, True, False)


def test_switching_on_asks_first(program, gcc_world, widevine):
    r = gcc_world.run(program, [settle(), set_active(True, **SWITCH), wait_for(type="AdwAlertDialog"),
                                dump("asked")])
    alert = r.dump("asked").one(type="AdwAlertDialog")
    assert alert["text"] == "Turn On Protected Content?"
    assert alert["subtitle"] == ASK_BODY
    buttons = {b["text"]: b.get("classes", []) for b in
               r.dump("asked").within(type="AdwAlertDialog").find(type="GtkButton")}
    assert set(buttons) == {"Cancel", "Turn On"}
    assert "suggested-action" in buttons["Turn On"]
    # Nothing runs before the answer
    assert widevine.tool_calls() == []
    assert gcc_world.fake_bin.calls("pkexec") == []


@pytest.mark.parametrize("answer", [
    click(type="GtkButton", text="Cancel", within={"type": "AdwAlertDialog"}),
    {"op": "close_dialog"},
])
def test_cancel_puts_the_switch_back(program, gcc_world, widevine, answer):
    r = gcc_world.run(program, [settle(), set_active(True, **SWITCH), wait_for(type="AdwAlertDialog"),
                                answer, wait_for(type="AdwAlertDialog", gone=True), settle(),
                                dump("after")])
    assert state(r.dump("after")) == (False, OFF_TEXT, True, False)
    assert gcc_world.fake_bin.calls("pkexec") == []
    assert not r.warnings()


def test_turn_on_downloads_and_shows_the_version(program, gcc_world, widevine):
    widevine.tool(wait=True)
    r = gcc_world.run(program, [
        settle(), set_active(True, **SWITCH), wait_for(type="AdwAlertDialog"),
        click(type="GtkButton", text="Turn On", within={"type": "AdwAlertDialog"}),
        wait_for(type="AdwSpinner"), dump("busy"),
        widevine.go_step(),
        wait_for(type="AdwSpinner", gone=True), settle(), dump("done")])
    # One change at a time while pkexec (its prompt, the download) runs
    assert state(r.dump("busy")) == (True, "Downloading from Google…", False, True)
    assert gcc_world.fake_bin.calls("pkexec") == [[str(widevine.path(TOOL)), "on"]]
    assert state(r.dump("done")) == (True, "Widevine 4.10.2830.0 from Google", True, False)
    assert not r.warnings()


@pytest.mark.parametrize("code, installs, after", [
    # The prompt dismissed or refused: the user's answer, the switch goes back
    (126, None, (False, OFF_TEXT)),
    (127, None, (False, OFF_TEXT)),
    # The download failed: on, the service tries again later
    (3, None, (True, "Not downloaded yet, tries again later")),
])
def test_turn_on_answers_that_are_no_failure(program, gcc_world, widevine, code, installs, after):
    widevine.tool(on_code=code, on_installs=installs)
    if code in (126, 127):
        # pkexec itself answers, the tool never runs
        widevine.pkexec(f"exit {code}")
    r = gcc_world.run(program, [
        settle(), set_active(True, **SWITCH), wait_for(type="AdwAlertDialog"),
        click(type="GtkButton", text="Turn On", within={"type": "AdwAlertDialog"}),
        wait_for(type="AdwSpinner"), wait_for(type="AdwSpinner", gone=True), settle(), dump("done")])
    assert state(r.dump("done")) == (*after, True, False)
    assert not r.warnings()


def test_turn_on_failing_warns_and_shows_what_is_true(program, gcc_world, widevine):
    widevine.pkexec("exit 1")
    r = gcc_world.run(program, [
        settle(), set_active(True, **SWITCH), wait_for(type="AdwAlertDialog"),
        click(type="GtkButton", text="Turn On", within={"type": "AdwAlertDialog"}),
        wait_for(type="AdwSpinner"), wait_for(type="AdwSpinner", gone=True), settle(), dump("done")])
    assert state(r.dump("done")) == (False, OFF_TEXT, True, False)
    assert [m["message"] for m in r.warnings()] == [f"{widevine.path(TOOL)} exited with 1"]


def test_a_tool_killed_by_a_signal_warns(program, gcc_world, widevine):
    widevine.tool(kill=True)
    r = gcc_world.run(program, [
        settle(), set_active(True, **SWITCH), wait_for(type="AdwAlertDialog"),
        click(type="GtkButton", text="Turn On", within={"type": "AdwAlertDialog"}),
        wait_for(type="AdwSpinner"), wait_for(type="AdwSpinner", gone=True), settle(), dump("done")])
    assert state(r.dump("done")) == (False, OFF_TEXT, True, False)
    assert [m["message"] for m in r.warnings()] == [f"{widevine.path(TOOL)} exited with -1"]


def test_turn_off_removes_without_asking(program, gcc_world, widevine):
    widevine.installed("4.10.2830.0")
    widevine.tool(wait=True)
    r = gcc_world.run(program, [
        settle(), set_active(False, **SWITCH), wait_for(type="AdwSpinner"), dump("busy"),
        widevine.go_step(), wait_for(type="AdwSpinner", gone=True), settle(), dump("done")])
    assert not r.dump("busy").has(type="AdwAlertDialog")
    assert state(r.dump("busy")) == (False, "Removing…", False, True)
    assert gcc_world.fake_bin.calls("pkexec") == [[str(widevine.path(TOOL)), "off"]]
    assert state(r.dump("done")) == (False, OFF_TEXT, True, False)
    assert not widevine.path(MARKER).exists()
    assert not r.warnings()


def test_turn_off_refused_keeps_it_on(program, gcc_world, widevine):
    widevine.installed("4.10.2830.0")
    widevine.pkexec("exit 126")
    r = gcc_world.run(program, [
        settle(), set_active(False, **SWITCH), wait_for(type="AdwSpinner"),
        wait_for(type="AdwSpinner", gone=True), settle(), dump("done")])
    assert state(r.dump("done")) == (True, "Widevine 4.10.2830.0 from Google", True, False)


@pytest.mark.parametrize("on", [True, False])
def test_without_pkexec_it_warns_and_shows_what_is_true(program, gcc_world, widevine, on):
    (gcc_world.fake_bin.path / "pkexec").unlink()
    if not on:
        widevine.installed("4.10.2830.0")
    steps = [settle(), set_active(on, **SWITCH)]
    if on:
        steps += [wait_for(type="AdwAlertDialog"),
                  click(type="GtkButton", text="Turn On", within={"type": "AdwAlertDialog"}),
                  wait_for(type="AdwAlertDialog", gone=True)]
    r = gcc_world.run(program, [*steps, settle(), dump("done")])
    after = (False, OFF_TEXT) if on else (True, "Widevine 4.10.2830.0 from Google")
    assert state(r.dump("done")) == (*after, True, False)
    assert [m["message"] for m in r.warnings()] == [
        f"Cannot run pkexec {widevine.path(TOOL)}: Failed to execute child process “pkexec” "
        "(No such file or directory)"]
