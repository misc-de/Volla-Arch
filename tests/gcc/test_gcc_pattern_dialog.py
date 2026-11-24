# The unlock pattern dialog of Users (cc-pattern-dialog.c): first the PIN (checked by
# plinius-pinguard-check, or unix_chkpwd without it) or, with a pattern set, the current pattern
# (plinius-pattern check), then a new pattern drawn twice on the pattern pad and saved with
# plinius-pattern set; or the pattern removed. The account comes from a fake accounts daemon
# (org.freedesktop.Accounts, interfaces from the host's files), the two PIN checkers are scripts in
# the program's root, plinius-pattern a stand-in in fake_bin; the pad is drawn through its drag
# gesture.

import re
import shutil
import time
from pathlib import Path

import pytest

from gcc_harness import (build_panel, call, click, dump, drag, gcc_session, gcc_world,  # noqa: F401
                         select, set_text, settle, wait, wait_for)

ACCOUNTS = "org.freedesktop.Accounts"
ACCOUNTS_PATH = "/org/freedesktop/Accounts"
USER_PATH = "/org/freedesktop/Accounts/User1000"
INTERFACES = Path("/usr/share/dbus-1/interfaces")
USER = "alice"

CHKPWD = "/usr/bin/unix_chkpwd"
PINGUARD = "/usr/lib/plinius-pinguard/plinius-pinguard-check"

GLUE = r'''
#include "gcc_driver.h"
#include "cc-pattern-dialog.h"

static ActUser *user;

static void
present (GccTest *t)
{
    adw_dialog_present (ADW_DIALOG (cc_pattern_dialog_new (user)), GTK_WIDGET (t->window));
}

/* As the Users panel has it: the account of the user, loaded from the accounts daemon */
void
gcc_test_setup (GccTest *t)
{
    gint64 end = g_get_monotonic_time () + 5 * G_USEC_PER_SEC;

    user = g_object_ref (act_user_manager_get_user (act_user_manager_get_default (), gcc_test_param (t, "user")));
    while (!act_user_is_loaded (user) && g_get_monotonic_time () < end) {
        if (!g_main_context_iteration (NULL, FALSE))
            g_usleep (1000);
    }
    if (g_strcmp0 (gcc_test_param (t, "open"), "no") != 0)
        present (t);
}

gboolean
gcc_test_call (GccTest *t, const char *name, JsonArray *args, JsonBuilder *result)
{
    if (g_str_equal (name, "open")) {
        present (t);
        json_builder_add_null_value (result);
    } else if (g_str_equal (name, "available")) {
        json_builder_add_boolean_value (result, cc_pattern_dialog_is_available ());
    } else if (g_str_equal (name, "has_pattern")) {
        json_builder_add_boolean_value (result, cc_pattern_dialog_has_pattern ());
    } else if (g_str_equal (name, "user_name")) {
        json_builder_add_string_value (result, act_user_get_user_name (user));
    } else {
        return FALSE;
    }
    return TRUE;
}
'''


def _interface(name):
    """The host's introspection file of name, without its documentation elements."""
    f = INTERFACES / f"{name}.xml"
    if not f.exists():
        pytest.skip(f"no {f} on the host")
    text = f.read_text()
    text = re.sub(r"<!DOCTYPE[^>]*>", "", text)
    text = re.sub(r"<\?xml[^>]*\?>", "", text)
    text = re.sub(r"<doc:doc>.*?</doc:doc>", "", text, flags=re.S)
    text = re.sub(r'\s+xmlns:doc="[^"]*"', "", text)
    return text


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    reps = [(f'"{CHKPWD}"', f'"{{root}}{CHKPWD}"'), (f'"{PINGUARD}"', f'"{{root}}{PINGUARD}"')]
    return build_panel(gcc_session, tmp_path_factory.mktemp("pattern"), "pattern",
                       ["cc-pattern-dialog.c"], GLUE, pkgs=("accountsservice",),
                       replacements={"cc-pattern-dialog.c": reps})


class Checker:
    """A PIN checker script in the program's root: records its arguments and what it read,
    answers with the exit status set (0: right)."""

    def __init__(self, root, path):
        self.file = Path(root) / path.lstrip("/")

    def install(self, status=0, sleep=None, script=None):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        body = script or (
            f'{{ [ $# -eq 0 ] || printf "%s\\0" "$@"; printf "\\036"; }} >> "$0.args"\n'
            f'cat >> "$0.stdin"; printf "\\036" >> "$0.stdin"\n'
            + (f"sleep {sleep}\n" if sleep else "")
            + f"exit {status}\n")
        self.file.write_text("#!/bin/bash\n" + body)
        self.file.chmod(0o755)
        return self

    def calls(self):
        f = Path(f"{self.file}.args")
        if not f.exists():
            return []
        return [r.split("\0")[:-1] for r in f.read_text().split("\036")[:-1]]

    def inputs(self):
        f = Path(f"{self.file}.stdin")
        return f.read_bytes().split(b"\036")[:-1] if f.exists() else []


@pytest.fixture
def root(program):
    for d in list(program.root.iterdir()):
        shutil.rmtree(d) if d.is_dir() else d.unlink()
    return program.root


@pytest.fixture
def chkpwd(root):
    return Checker(root, CHKPWD)


@pytest.fixture
def pinguard(root):
    return Checker(root, PINGUARD)


def accounts(world, name=USER):
    world.service("system", ACCOUNTS, ACCOUNTS_PATH, _interface("org.freedesktop.Accounts"),
                  replies={"FindUserByName": f"(objectpath '{USER_PATH}',)"})
    world.service("system", ACCOUNTS, USER_PATH, _interface("org.freedesktop.Accounts.User"),
                  properties={"UserName": repr(name), "Uid": "uint64 1000",
                              "RealName": "'Alice'", "HomeDirectory": repr(str(world.home)),
                              "LocalAccount": "true"})


class PatternTool:
    """plinius-pattern in fake_bin: each command's stdin recorded, its exit status and a pause
    before it ends set per command."""

    def __init__(self, world):
        self.bin = world.fake_bin
        d = self.bin.path
        self.bin.stub("plinius-pattern",
                      f'd="{d}"\n'
                      f'cat >> "$d/pattern-$1.in"; printf "\\036" >> "$d/pattern-$1.in"\n'
                      f'[ -f "$d/pattern-$1.sleep" ] && sleep "$(cat "$d/pattern-$1.sleep")"\n'
                      f'[ -f "$d/pattern-$1.kill" ] && kill -9 $$\n'
                      f'[ -f "$d/pattern-$1.status" ] && exit "$(cat "$d/pattern-$1.status")"\n'
                      'exit 0')

    def answer(self, command, status=None, sleep=None, kill=False):
        if status is not None:
            (self.bin.path / f"pattern-{command}.status").write_text(str(status))
        if sleep is not None:
            (self.bin.path / f"pattern-{command}.sleep").write_text(str(sleep))
        if kill:
            (self.bin.path / f"pattern-{command}.kill").write_text("")

    def calls(self):
        return [c[0] if c else None for c in self.bin.calls("plinius-pattern")]

    def inputs(self, command):
        f = self.bin.path / f"pattern-{command}.in"
        return [x.rstrip("\n") for x in f.read_text().split("\036")[:-1]] if f.exists() else []


@pytest.fixture
def tool(gcc_world):
    return PatternTool(gcc_world)


def set_pattern(world, grid=4, text=None):
    f = world.home / ".config/plinius/unlock-pattern"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text if text is not None else f"{grid}\nhash\n")
    return f


def points(grid, *picked):
    """The centres of grid points (numbered row by row from 0) as fractions of the pad."""
    return [((p % grid + 0.5) / grid, (p // grid + 0.5) / grid) for p in picked]


def draw(grid, *picked):
    return drag(points(grid, *picked), type="CcPatternPad")


PAD = wait_for(type="CcPatternPad")
# The pad has the lock screen's size on the display (wider than the test's window), and GTK's
# text field misses its focus-out when the dialog closes in a headless run: not the dialog's
IGNORED = (r"exceeds AdwBreakpointBin", r"did not receive a focus-out event")
GOOD = (0, 1, 2, 3, 7, 11)  # an L on the top row and down the right side of a 4x4 grid


def run(world, program, steps, **params):
    return world.run(program, [wait_for(type="AdwDialog"), *steps],
                     params={"user": USER, **params})


def dialog(screen):
    return screen.one(type="CcPatternDialog")


def labels(screen):
    # Not the dialog's title in its header bar
    return [n for n in screen.within(type="CcPatternDialog").all()
            if n["type"] == "GtkLabel" and n.get("text") != "Unlock Pattern"]


def label_texts(screen):
    return [(n["text"], "error" in n.get("classes", [])) for n in labels(screen)]


def has_dialog(screen):
    return screen.has(type="CcPatternDialog")


# ----- The PIN first -----

def test_without_a_pattern_the_pin_comes_first(program, gcc_world, root, tool):
    accounts(gcc_world)
    r = run(gcc_world, program, [settle(), dump("pin"), call("available"), call("has_pattern"),
                                 call("user_name")])
    s = r.dump("pin")
    assert dialog(s)["text"] == "Unlock Pattern"
    assert label_texts(s) == [
        ("With an unlock pattern, the lock screen takes the pattern or a fingerprint instead of "
         "the PIN. Enter your PIN to continue.", False)]
    assert s.has(type="AdwPasswordEntryRow", text="PIN")
    assert s.has(type="GtkButton", text="Continue")
    assert not s.has(type="CcPatternPad")
    assert (r.value("available"), r.value("has_pattern"), r.value("user_name")) == (True, False, USER)
    assert not r.warnings(ignore=IGNORED)


def test_not_available_without_plinius_pattern(program, gcc_world, root):
    accounts(gcc_world)
    r = run(gcc_world, program, [call("available")])
    assert r.value("available") is False


def test_empty_pin_does_nothing(program, gcc_world, root, chkpwd):
    accounts(gcc_world)
    chkpwd.install(0)
    r = run(gcc_world, program, [click(type="GtkButton", text="Continue"), settle(), dump("pin")])
    assert not chkpwd.calls()
    assert r.dump("pin").has(type="AdwPasswordEntryRow", text="PIN")


def test_right_pin_through_unix_chkpwd(program, gcc_world, root, chkpwd, tool):
    accounts(gcc_world)
    chkpwd.install(0)
    r = run(gcc_world, program, [set_text("123456", type="AdwPasswordEntryRow"),
                                 click(type="GtkButton", text="Continue"), PAD, dump("draw")])
    # The caller's own password with its NUL, as pam_unix sends it
    assert chkpwd.calls() == [[USER, "nullok"]]
    assert chkpwd.inputs() == [b"123456\0"]
    s = r.dump("draw")
    assert label_texts(s) == [("Draw an unlock pattern. Connect at least 6 points.", False)]
    group = s.one(type="AdwToggleGroup")
    assert group["toggles"] == ["4×4", "5×5", "6×6"] and group["active_name"] == "4"
    # Nothing to remove yet, and no PIN needed any more
    assert not s.has(type="GtkButton", text="Remove Pattern")
    assert not s.has(type="GtkButton", text="Use PIN Instead")


def test_enter_in_the_pin_field_continues(program, gcc_world, root, chkpwd, tool):
    accounts(gcc_world)
    chkpwd.install(0)
    run(gcc_world, program, [set_text("2468", type="AdwPasswordEntryRow"),
                             {"op": "enter", "target": {"type": "AdwPasswordEntryRow"}}, PAD])
    assert chkpwd.inputs() == [b"2468\0"]


def test_wrong_pin_waits_two_seconds(program, gcc_world, root, chkpwd, tool):
    accounts(gcc_world)
    chkpwd.install(1)
    r = run(gcc_world, program, [
        set_text("0000", type="AdwPasswordEntryRow"), click(type="GtkButton", text="Continue"),
        wait_for(type="GtkLabel", text="Wrong PIN. Try again."), dump("wrong"),
        # Another try while the wait runs is not taken
        set_text("1111", type="AdwPasswordEntryRow"),
        {"op": "enter", "target": {"type": "AdwPasswordEntryRow"}}, settle(),
        wait(2300), dump("again")])
    wrong = r.dump("wrong")
    assert ("Wrong PIN. Try again.", True) in label_texts(wrong)
    assert wrong.one(type="AdwPasswordEntryRow")["value"] == ""
    assert wrong.one(type="AdwPasswordEntryRow").get("sensitive") is False
    assert wrong.one(type="GtkButton", text="Continue").get("sensitive") is False
    again = r.dump("again")
    assert again.one(type="AdwPasswordEntryRow").get("sensitive", True) is True
    assert again.one(type="GtkButton", text="Continue").get("sensitive", True) is True
    assert chkpwd.inputs() == [b"0000\0"]


def test_pin_checked_by_pinguard_when_installed(program, gcc_world, root, chkpwd, pinguard, tool):
    accounts(gcc_world)
    chkpwd.install(1)
    pinguard.install(0)
    run(gcc_world, program, [set_text("123456", type="AdwPasswordEntryRow"),
                             click(type="GtkButton", text="Continue"), PAD])
    assert pinguard.calls() == [[]] and pinguard.inputs() == [b"123456\0"]
    assert not chkpwd.calls()


@pytest.mark.parametrize("status, text", [(1, "Wrong PIN. Try again."),
                                          (2, "Too many wrong tries. Try again later.")])
def test_pinguard_refuses(program, gcc_world, root, chkpwd, pinguard, status, text):
    accounts(gcc_world)
    chkpwd.install(0)
    pinguard.install(status)
    r = run(gcc_world, program, [set_text("123456", type="AdwPasswordEntryRow"),
                                 click(type="GtkButton", text="Continue"),
                                 wait_for(type="GtkLabel", text=text), dump("wrong")])
    assert (text, True) in label_texts(r.dump("wrong"))
    # Counted by plinius-pinguard: unix_chkpwd is not asked behind its back
    assert not chkpwd.calls()


@pytest.mark.parametrize("how", ["not running", "cannot start"])
def test_pinguard_not_there_falls_back_to_unix_chkpwd(program, gcc_world, root, chkpwd, pinguard,
                                                      tool, how):
    accounts(gcc_world)
    chkpwd.install(0)
    if how == "not running":
        pinguard.install(3)
    else:
        # Executable, but its interpreter is missing: it never starts
        pinguard.file.parent.mkdir(parents=True, exist_ok=True)
        pinguard.file.write_text("#!/nonexistent/interpreter\n")
        pinguard.file.chmod(0o755)
    run(gcc_world, program, [set_text("123456", type="AdwPasswordEntryRow"),
                             click(type="GtkButton", text="Continue"), PAD])
    assert chkpwd.calls() == [[USER, "nullok"]] and chkpwd.inputs() == [b"123456\0"]


def test_without_unix_chkpwd_the_pin_can_be_tried_again(program, gcc_world, root):
    accounts(gcc_world)
    r = run(gcc_world, program, [set_text("123456", type="AdwPasswordEntryRow"),
                                 click(type="GtkButton", text="Continue"), settle(), dump("pin")])
    s = r.dump("pin")
    assert s.one(type="AdwPasswordEntryRow").get("sensitive", True) is True
    assert s.one(type="GtkButton", text="Continue").get("sensitive", True) is True
    assert [m["message"] for m in r.warnings(ignore=IGNORED)] == [
        f"Running {root}{CHKPWD} failed: Failed to execute child process “{root}{CHKPWD}” "
        "(No such file or directory)"]


def test_checker_killed_counts_as_wrong(program, gcc_world, root, chkpwd):
    accounts(gcc_world)
    chkpwd.install(script='cat > /dev/null; kill -9 $$\n')
    r = run(gcc_world, program, [set_text("123456", type="AdwPasswordEntryRow"),
                                 click(type="GtkButton", text="Continue"),
                                 wait_for(type="GtkLabel", text="Wrong PIN. Try again.")])
    assert r.returncode == 0


# ----- Drawing the new pattern -----

def pin_ok(chkpwd):
    chkpwd.install(0)
    return [set_text("123456", type="AdwPasswordEntryRow"), click(type="GtkButton", text="Continue"),
            PAD]


def test_new_pattern_drawn_twice_is_saved(program, gcc_world, root, chkpwd, tool):
    accounts(gcc_world)
    r = run(gcc_world, program, [*pin_ok(chkpwd), draw(4, *GOOD), settle(), dump("confirm"),
                                 draw(4, *GOOD), settle(), dump("done")])
    assert label_texts(r.dump("confirm")) == [("Draw the pattern again to confirm.", False)]
    assert tool.calls() == ["set"]
    assert tool.inputs("set") == ["4:0.1.2.3.7.11"]
    assert not has_dialog(r.dump("done"))


def test_points_jumped_over_are_taken(program, gcc_world, root, chkpwd, tool):
    accounts(gcc_world)
    # Corner to corner across the diagonal, then along the bottom row and up the left side:
    # every grid point on a line counts, in order
    run(gcc_world, program, [*pin_ok(chkpwd), draw(4, 0, 15, 12, 4), settle(),
                             draw(4, 0, 15, 12, 4), settle()])
    assert tool.inputs("set") == ["4:0.5.10.15.14.13.12.8.4"]


def test_too_few_points(program, gcc_world, root, chkpwd, tool):
    accounts(gcc_world)
    r = run(gcc_world, program, [*pin_ok(chkpwd), draw(4, 0, 1, 2, 3, 7), settle(), dump("short")])
    assert label_texts(r.dump("short")) == [("Connect at least 6 points.", True)]
    assert not tool.calls()


def test_a_drag_that_meets_no_point_is_nothing(program, gcc_world, root, chkpwd, tool):
    accounts(gcc_world)
    # Between the points, where none is hit
    r = run(gcc_world, program, [*pin_ok(chkpwd), drag([(0.25, 0.25), (0.5, 0.5 - 0.125)],
                                                       type="CcPatternPad"), settle(),
                                 dump("draw")])
    assert label_texts(r.dump("draw")) == [("Draw an unlock pattern. Connect at least 6 points.", False)]


def test_patterns_that_do_not_match_start_over(program, gcc_world, root, chkpwd, tool):
    accounts(gcc_world)
    r = run(gcc_world, program, [*pin_ok(chkpwd), draw(4, *GOOD), settle(),
                                 draw(4, 0, 1, 2, 3, 7, 6), settle(), dump("mismatch"),
                                 # The first one counts no more
                                 draw(4, *GOOD), settle(), dump("first")])
    assert label_texts(r.dump("mismatch")) == [("The patterns do not match. Draw a new one.", True)]
    assert label_texts(r.dump("first")) == [("Draw the pattern again to confirm.", False)]
    assert not tool.calls()


def test_pattern_not_saved(program, gcc_world, root, chkpwd, tool):
    accounts(gcc_world)
    tool.answer("set", status=1)
    r = run(gcc_world, program, [*pin_ok(chkpwd), draw(4, *GOOD), settle(), draw(4, *GOOD),
                                 settle(), dump("failed")])
    s = r.dump("failed")
    assert has_dialog(s)
    assert label_texts(s) == [("The pattern could not be saved.", True)]
    assert [m["message"] for m in r.warnings(ignore=IGNORED)] == [
        "plinius-pattern set failed: exit status"]


def test_pattern_not_saved_without_the_tool(program, gcc_world, root, chkpwd):
    accounts(gcc_world)
    r = run(gcc_world, program, [*pin_ok(chkpwd), draw(4, *GOOD), settle(), draw(4, *GOOD),
                                 settle(), dump("failed")])
    assert label_texts(r.dump("failed")) == [("The pattern could not be saved.", True)]
    assert [m["message"] for m in r.warnings(ignore=IGNORED)] == [
        "plinius-pattern set failed: Failed to execute child process “plinius-pattern” "
        "(No such file or directory)"]


@pytest.mark.parametrize("grid, picked, saved", [
    ("5", (0, 1, 2, 3, 4, 9), "5:0.1.2.3.4.9"),
    ("6", (0, 1, 2, 3, 4, 5), "6:0.1.2.3.4.5"),
])
def test_another_grid_size(program, gcc_world, root, chkpwd, tool, grid, picked, saved):
    accounts(gcc_world)
    n = int(grid)
    run(gcc_world, program, [*pin_ok(chkpwd), select(name=grid, type="AdwToggleGroup"), settle(),
                             draw(n, *picked), settle(), draw(n, *picked), settle()])
    assert tool.inputs("set") == [saved]


def test_another_grid_size_starts_over(program, gcc_world, root, chkpwd, tool):
    accounts(gcc_world)
    r = run(gcc_world, program, [*pin_ok(chkpwd), draw(4, *GOOD), settle(),
                                 select(name="5", type="AdwToggleGroup"), settle(), dump("over")])
    assert label_texts(r.dump("over")) == [("Draw an unlock pattern. Connect at least 6 points.", False)]


# ----- With a pattern set -----

def test_with_a_pattern_the_current_one_comes_first(program, gcc_world, root, tool):
    accounts(gcc_world)
    set_pattern(gcc_world, 5)
    r = run(gcc_world, program, [PAD, dump("current"), call("has_pattern")])
    s = r.dump("current")
    assert label_texts(s) == [("Draw your current pattern.", False)]
    assert not s.has(type="AdwToggleGroup")
    assert s.has(type="GtkButton", text="Use PIN Instead")
    assert not s.has(type="GtkButton", text="Remove Pattern")
    assert r.value("has_pattern") is True


def test_right_current_pattern_leads_to_a_new_one(program, gcc_world, root, tool):
    accounts(gcc_world)
    set_pattern(gcc_world, 5)
    r = run(gcc_world, program, [PAD, draw(5, 0, 1, 2, 3), wait_for(
        type="GtkLabel", text="Draw an unlock pattern. Connect at least 6 points."), dump("first")])
    # Checked on its own grid, the pattern on stdin
    assert tool.inputs("check") == ["5:0.1.2.3"]
    s = r.dump("first")
    # Its grid preselected for the new one, which can also replace it by nothing
    assert s.one(type="AdwToggleGroup")["active_name"] == "5"
    assert s.has(type="GtkButton", text="Remove Pattern")
    assert not s.has(type="GtkButton", text="Use PIN Instead")


def test_a_tap_is_no_current_pattern(program, gcc_world, root, tool):
    accounts(gcc_world)
    set_pattern(gcc_world)
    r = run(gcc_world, program, [PAD, draw(4, 5), settle(), dump("current")])
    assert not tool.calls()
    assert label_texts(r.dump("current")) == [("Draw your current pattern.", False)]


@pytest.mark.parametrize("status, text", [(1, "Wrong pattern. Try again."),
                                          (3, "Too many wrong tries. Try again later.")])
def test_wrong_current_pattern(program, gcc_world, root, tool, status, text):
    accounts(gcc_world)
    set_pattern(gcc_world)
    tool.answer("check", status=status)
    r = run(gcc_world, program, [PAD, draw(4, 0, 1, 2), wait_for(type="GtkLabel", text=text),
                                 dump("wrong"), wait(2300), dump("later"),
                                 draw(4, 3, 2, 1), settle()])
    assert label_texts(r.dump("wrong")) == [(text, True)]
    assert r.dump("wrong").one(type="CcPatternPad").get("sensitive") is False
    # Shown in a dump only while insensitive
    assert not r.dump("later").has(type="CcPatternPad", sensitive=False)
    assert tool.inputs("check") == ["4:0.1.2", "4:3.2.1"]


def test_checker_killed_is_a_wrong_pattern(program, gcc_world, root, tool):
    accounts(gcc_world)
    set_pattern(gcc_world)
    tool.answer("check", kill=True)
    run(gcc_world, program, [PAD, draw(4, 0, 1, 2),
                             wait_for(type="GtkLabel", text="Wrong pattern. Try again.")])


def test_current_pattern_without_the_tool(program, gcc_world, root):
    accounts(gcc_world)
    set_pattern(gcc_world)
    r = run(gcc_world, program, [PAD, draw(4, 0, 1, 2), settle(), dump("current")])
    s = r.dump("current")
    assert not s.has(type="CcPatternPad", sensitive=False)
    assert label_texts(s) == [("Draw your current pattern.", False)]
    assert [m["message"] for m in r.warnings(ignore=IGNORED)] == [
        "Running plinius-pattern failed: Failed to execute child process “plinius-pattern” "
        "(No such file or directory)"]


def test_forgotten_pattern_the_pin_instead(program, gcc_world, root, chkpwd, tool):
    accounts(gcc_world)
    set_pattern(gcc_world)
    r = run(gcc_world, program, [PAD, click(type="GtkButton", text="Use PIN Instead"), settle(),
                                 dump("pin"), *pin_ok(chkpwd), dump("first")])
    assert r.dump("pin").has(type="AdwPasswordEntryRow", text="PIN")
    assert r.dump("first").has(type="GtkButton", text="Remove Pattern")
    assert chkpwd.inputs() == [b"123456\0"]


def test_remove_pattern(program, gcc_world, root, tool):
    accounts(gcc_world)
    set_pattern(gcc_world)
    r = run(gcc_world, program, [PAD, draw(4, 0, 1, 2),
                                 wait_for(type="GtkButton", text="Remove Pattern"),
                                 click(type="GtkButton", text="Remove Pattern"), settle(),
                                 dump("gone")])
    assert tool.calls() == ["check", "remove"]
    assert tool.inputs("remove") == [""]
    assert not has_dialog(r.dump("gone"))


def test_pattern_not_removed(program, gcc_world, root, tool):
    accounts(gcc_world)
    set_pattern(gcc_world)
    tool.answer("remove", status=1)
    r = run(gcc_world, program, [PAD, draw(4, 0, 1, 2),
                                 wait_for(type="GtkButton", text="Remove Pattern"),
                                 click(type="GtkButton", text="Remove Pattern"), settle(),
                                 dump("kept")])
    assert label_texts(r.dump("kept")) == [("The pattern could not be removed.", True)]


def test_old_3x3_pattern_is_checked_on_3x3_and_replaced_on_4x4(program, gcc_world, root, tool):
    accounts(gcc_world)
    set_pattern(gcc_world, 3)
    r = run(gcc_world, program, [PAD, draw(3, 0, 1, 2, 5, 8),
                                 wait_for(type="GtkButton", text="Remove Pattern"), dump("first"),
                                 draw(4, *GOOD), settle(), draw(4, *GOOD), settle()])
    assert tool.inputs("check") == ["3:0.1.2.5.8"]
    # 3x3 is no longer offered for a new pattern
    assert r.dump("first").one(type="AdwToggleGroup")["active_name"] == "4"
    assert tool.inputs("set") == ["4:0.1.2.3.7.11"]


@pytest.mark.parametrize("text", ["9\nhash\n", "5hash\n", ""])
def test_pattern_file_of_unknown_form_uses_the_defaults(program, gcc_world, root, tool, text):
    accounts(gcc_world)
    set_pattern(gcc_world, text=text)
    r = run(gcc_world, program, [PAD, draw(4, 0, 1, 2), settle()])
    # Still a pattern set: it is asked for, on the 4x4 grid
    assert tool.inputs("check") == ["4:0.1.2"]
    assert r.returncode == 0


def test_closing_while_the_check_runs(program, gcc_world, root, tool):
    accounts(gcc_world)
    set_pattern(gcc_world)
    tool.answer("check", sleep=0.5)
    r = run(gcc_world, program, [PAD, draw(4, 0, 1, 2), {"op": "close_dialog"}, wait(900),
                                 dump("closed")])
    assert not has_dialog(r.dump("closed"))
    assert tool.calls() == ["check"]
