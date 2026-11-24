# "Wait After Wrong PINs" on the Screen Lock page (cc-plinius-pinguard.c): the switch shows
# plinius-pinguard's marker file and runs "pkexec plinius-pinguard-set on|off"; pkexec is a stub
# in fake_bin that does what the tool does (the marker in the program's root) or answers like a
# dismissed or refused PIN prompt.

import pytest

from gcc_harness import build_panel, call, dump, gcc_session, gcc_world, set_active, settle, wait_for  # noqa: F401

TOOL = "/usr/lib/plinius-pinguard/plinius-pinguard-set"
MARKER = "/etc/plinius-pinguard/enabled"

GLUE = r'''
#include "gcc_driver.h"
#include "cc-plinius-pinguard.h"

static gboolean added;

void
gcc_test_setup (GccTest *t)
{
    GtkWidget *lock = adw_preferences_group_new ();

    adw_preferences_group_set_title (ADW_PREFERENCES_GROUP (lock), "Screen Lock");
    adw_preferences_page_add (t->page, ADW_PREFERENCES_GROUP (lock));
    added = cc_plinius_pinguard_setup (t->page);
}

gboolean
gcc_test_call (GccTest *t, const char *name, JsonArray *args, JsonBuilder *result)
{
    if (!g_str_equal (name, "added"))
        return FALSE;
    json_builder_add_boolean_value (result, added);
    return TRUE;
}
'''

SUBTITLE = ("After 5 wrong PINs, every further try has to wait longer: 30 seconds, 1 minute, 5 and 15 "
            "minutes, then 1 hour.")


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    return build_panel(gcc_session, tmp_path_factory.mktemp("pinguard"), "pinguard",
                       ["cc-plinius-pinguard.c"], GLUE,
                       replacements={"cc-plinius-pinguard.c": [(f'"{p}"', f'"{{root}}{p}"')
                                                                for p in (TOOL, MARKER)]})


@pytest.fixture
def system(program):
    tool = program.root / TOOL.lstrip("/")
    tool.parent.mkdir(parents=True, exist_ok=True)
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    marker = program.root / MARKER.lstrip("/")
    marker.parent.mkdir(parents=True, exist_ok=True)
    if marker.exists():
        marker.unlink()
    return program.root


def pkexec(world, root, exit_code=0, act=True):
    """pkexec running the tool: "on" makes the marker, "off" removes it (when act); it takes a
    moment, as a PIN prompt does."""
    marker = root / MARKER.lstrip("/")
    script = "sleep 0.2\n"
    if act:
        script += (f'[ "$2" = on ] && touch "{marker}"\n'
                   f'[ "$2" = off ] && rm -f "{marker}"\n')
    script += f"exit {exit_code}\n"
    world.fake_bin.stub("pkexec", script)


def switch(screen):
    r = screen.one(type="AdwSwitchRow", text="Wait After Wrong PINs")
    return r["active"], r.get("sensitive", True)


@pytest.mark.parametrize("on", [True, False])
def test_switch_shows_the_marker(program, gcc_world, system, on):
    if on:
        (system / MARKER.lstrip("/")).write_text("")
    pkexec(gcc_world, system)
    r = gcc_world.run(program, [settle(), call("added"), dump("page")])
    screen = r.dump("page")
    assert r.value("added") is True
    assert switch(screen) == (on, True)
    assert screen.one(type="AdwSwitchRow")["subtitle"] == SUBTITLE
    # Below GNOME's lock options
    groups = screen.find(type="AdwPreferencesGroup")
    assert groups[0]["text"] == "Screen Lock"
    assert any(c.get("text") == "Wait After Wrong PINs" for c in groups[1].walk())
    # Showing runs nothing
    assert gcc_world.fake_bin.calls("pkexec") == []


def test_nothing_without_plinius_pinguard(program, gcc_world, system):
    (system / TOOL.lstrip("/")).chmod(0o644)
    r = gcc_world.run(program, [settle(), call("added"), dump("page")])
    assert r.value("added") is False
    assert not r.dump("page").has(type="AdwSwitchRow")


@pytest.mark.parametrize("start, active, arg", [(False, True, "on"), (True, False, "off")])
def test_switching_runs_the_tool_through_pkexec(program, gcc_world, system, start, active, arg):
    if start:
        (system / MARKER.lstrip("/")).write_text("")
    pkexec(gcc_world, system)
    r = gcc_world.run(program, [settle(), set_active(active, type="AdwSwitchRow"), dump("busy"), settle(500),
                                dump("done")])
    assert gcc_world.fake_bin.calls("pkexec") == [[str(system) + TOOL, arg]]
    # One change at a time until pkexec is done
    assert switch(r.dump("busy")) == (active, False)
    assert switch(r.dump("done")) == (active, True)
    assert (system / MARKER.lstrip("/")).exists() is active
    assert not r.warnings()


@pytest.mark.parametrize("code", [126, 127])
def test_dismissed_or_refused_prompt_goes_back_quietly(program, gcc_world, system, code):
    (system / MARKER.lstrip("/")).write_text("")
    pkexec(gcc_world, system, exit_code=code, act=False)
    r = gcc_world.run(program, [settle(), set_active(False, type="AdwSwitchRow"), settle(500), dump("done")])
    # The daemon still waits after wrong PINs: the switch says so
    assert switch(r.dump("done")) == (True, True)
    assert not r.warnings()


def test_failing_tool_is_a_warning(program, gcc_world, system):
    pkexec(gcc_world, system, exit_code=1, act=False)
    r = gcc_world.run(program, [settle(), set_active(True, type="AdwSwitchRow"), settle(500), dump("done")])
    assert switch(r.dump("done")) == (False, True)
    assert [m["message"] for m in r.warnings()] == [f"{system}{TOOL} exited with 1"]


def test_killed_tool_is_a_warning(program, gcc_world, system):
    gcc_world.fake_bin.stub("pkexec", "kill -9 $$")
    r = gcc_world.run(program, [settle(), set_active(True, type="AdwSwitchRow"), settle(500), dump("done")])
    assert switch(r.dump("done")) == (False, True)
    assert [m["message"] for m in r.warnings()] == [f"{system}{TOOL} exited with -1"]


def test_without_pkexec_the_switch_stays(program, gcc_world, system):
    # fake_bin has no pkexec: nothing to run
    r = gcc_world.run(program, [settle(), set_active(True, type="AdwSwitchRow"), settle(), dump("done")])
    assert switch(r.dump("done")) == (False, True)
    assert r.warnings()
    assert all(m["message"].startswith(f"Cannot run pkexec {system}{TOOL}: ") for m in r.warnings())


def test_without_pkexec_it_is_tried_once(program, gcc_world, system):
    r = gcc_world.run(program, [settle(), set_active(True, type="AdwSwitchRow"), settle(), dump("done")])
    msgs = [m["message"] for m in r.warnings()]
    assert len(msgs) == 1 and msgs[0].startswith(f"Cannot run pkexec {system}{TOOL}: ")
