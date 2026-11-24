# The assistant button's actions (cc-plinius-buttons.c): one combo row each for a short, a double
# and a long press, bound to plinius-assist's schema org.plinius.assistant-button, and for
# "Custom" a command row written on apply or filled from a file picked in the file chooser
# portal (fake_file_chooser.py), quoted, with the script's interpreter when it cannot run itself.

import ast
import os

import pytest

from fake_file_chooser import FileChooserPortal
from fake_schemas import no_schemas, schemas_without
from gcc_harness import (apply, build_panel, call, click, dump, gcc_session, gcc_world,  # noqa: F401
                         select, set_text, settle, wait, wait_for)

GLUE = r'''
#include "gcc_driver.h"
#include "cc-plinius-buttons.h"

static gboolean added;

void
gcc_test_setup (GccTest *t)
{
    added = cc_plinius_buttons_setup (t->page);
}

gboolean
gcc_test_call (GccTest *t, const char *name, JsonArray *args, JsonBuilder *result)
{
    if (g_str_equal (name, "added")) {
        json_builder_add_boolean_value (result, added);
        return TRUE;
    }
    return FALSE;
}
'''

SCHEMA = "org.plinius.assistant-button"
PRESSES = ("Short Press", "Double Press", "Long Press")
KEYS = ("short-press", "double-press", "long-press")
NAMES = ["Nothing", "Torch", "Screenshot", "Camera", "Silent Mode", "Lock Screen", "Custom"]
NICKS = ["none", "torch", "screenshot", "camera", "silent", "lock", "custom"]
COMMAND = "Command or Script Path"


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    return build_panel(gcc_session, tmp_path_factory.mktemp("buttons"), "buttons",
                       ["cc-plinius-buttons.c"], GLUE)


def gset(key, value):
    return {"op": "gsettings_set", "schema": SCHEMA, "key": key, "value": value}


def gget(key):
    return {"op": "gsettings_get", "schema": SCHEMA, "key": key, "label": key}


def combos(screen):
    return {r["text"]: r["selected_text"] for r in screen.find(type="AdwComboRow")}


def command_rows(screen):
    return [r["value"] for r in screen.find(type="AdwEntryRow", text=COMMAND)]


def test_rows_for_the_three_presses(program, gcc_world):
    r = gcc_world.run(program, [settle(), call("added"), dump("main")])
    assert r.value("added") is True
    s = r.dump("main")
    group = s.one(type="AdwPreferencesGroup", subtitle="“Custom” runs a program or script of your choice.")
    # No title of its own: the panel's header says it
    assert "text" not in group
    assert [r["text"] for r in s.find(type="AdwComboRow")] == list(PRESSES)
    assert all(r["items"] == NAMES for r in s.find(type="AdwComboRow"))
    assert combos(s) == dict.fromkeys(PRESSES, "Nothing")
    # The command rows only show for "Custom"
    assert command_rows(s) == []
    assert not r.warnings()


@pytest.mark.parametrize("index", range(7))
def test_a_choice_is_written_as_the_schemas_nick(program, gcc_world, index):
    r = gcc_world.run(program, [settle(), select(index=index, type="AdwComboRow", text="Long Press"),
                                settle(), *map(gget, KEYS)])
    assert r.value("long-press") == f"'{NICKS[index]}'"
    # The others stay
    assert r.value("short-press") == r.value("double-press") == "'none'"


@pytest.mark.parametrize("index", range(7))
def test_the_setting_shows_in_its_row(program, gcc_world, index):
    r = gcc_world.run(program, [gset("double-press", f"'{NICKS[index]}'"), {"op": "setup"}, settle(),
                                dump("main")])
    assert combos(r.dump("main")) == {"Short Press": "Nothing", "Double Press": NAMES[index],
                                      "Long Press": "Nothing"}


def test_custom_shows_the_command_row_with_its_command(program, gcc_world):
    r = gcc_world.run(program, [
        gset("short-press-command", "'flashlight --toggle'"),
        gset("long-press-command", "'other'"),
        {"op": "setup"}, settle(),
        select(item="Custom", type="AdwComboRow", text="Short Press"), settle(), dump("custom"),
        select(item="Torch", type="AdwComboRow", text="Short Press"), settle(), dump("torch")])
    s = r.dump("custom")
    assert command_rows(s) == ["flashlight --toggle"]
    # Right after its press's row, with the file button
    rows = [n.get("text") for n in s.all() if n.get("type") in ("AdwComboRow", "AdwEntryRow")]
    assert rows == ["Short Press", COMMAND, "Double Press", "Long Press"]
    button = s.within(type="AdwEntryRow", text=COMMAND).one(type="GtkButton")
    assert button["icon"] == "document-open-symbolic"
    assert button["tooltip"] == "Choose a Program or Script"
    assert command_rows(r.dump("torch")) == []


def test_a_command_is_written_on_apply_only(program, gcc_world):
    r = gcc_world.run(program, [
        gset("double-press", "'custom'"), {"op": "setup"}, settle(),
        set_text("/home/user/bin/hello --loud", type="AdwEntryRow", text=COMMAND), settle(),
        {**gget("double-press-command"), "label": "typed"},
        apply(type="AdwEntryRow", text=COMMAND), settle(),
        {**gget("double-press-command"), "label": "applied"}, *map(gget, ("short-press-command",
                                                                          "long-press-command"))])
    assert r.value("typed") == "''"
    assert r.value("applied") == "'/home/user/bin/hello --loud'"
    assert r.value("short-press-command") == r.value("long-press-command") == "''"


def pick(world, program, key, path, steps_after=()):
    """Custom for key's press, then the file button; the portal answers with path."""
    FileChooserPortal(world, [{"uris": [path if "://" in str(path) else path.as_uri()]}]).start()
    return world.run(program, [
        gset(key, "'custom'"), {"op": "setup"}, settle(),
        click(type="GtkButton", tooltip="Choose a Program or Script"), wait(400), settle(),
        dump("picked"), gget(f"{key}-command"), *steps_after])


def script(world, name, text, mode):
    f = world.home / name
    f.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        f.write_bytes(text)
    else:
        f.write_text(text)
    f.chmod(mode)
    return f


def test_the_file_dialog_starts_in_the_home_folder(program, gcc_world):
    f = script(gcc_world, "bin/torch", "#!/bin/sh\n", 0o755)
    r = pick(gcc_world, program, "short-press", f)
    calls = gcc_world.calls("org.freedesktop.portal.Desktop", "OpenFile")
    assert len(calls) == 1
    _parent, title, options = calls[0]["args"]
    assert title == "Choose a Program or Script"
    assert bytes(options["current_folder"]).rstrip(b"\0") == os.fsencode(gcc_world.home)
    # An executable runs itself: its path, quoted for plinius-assist's shell-like split
    assert ast.literal_eval(r.value("short-press-command")) == f"'{f}'"
    assert command_rows(r.dump("picked")) == [f"'{f}'"]
    assert not r.warnings()


@pytest.mark.parametrize("text, mode, command", [
    ("#!/bin/sh\necho\n", 0o755, "'{f}'"),
    ("#!/usr/bin/python3\nprint()\n", 0o644, "/usr/bin/python3 '{f}'"),
    ("#! /usr/bin/env bash -e  \n", 0o644, "/usr/bin/env bash -e '{f}'"),
    ("#!   \necho\n", 0o644, "sh '{f}'"),
    ("echo no interpreter\n", 0o644, "sh '{f}'"),
    ("", 0o644, "sh '{f}'"),
    (b"#!/bin/\xff\xfe\n", 0o644, "sh '{f}'"),
    ("#!/bin/sh\n", 0o000, "sh '{f}'"),
])
def test_the_command_for_a_picked_file(program, gcc_world, text, mode, command):
    f = script(gcc_world, "my scripts/it's mine", text, mode)
    r = pick(gcc_world, program, "long-press", f)
    quoted = "'" + str(f).replace("'", "'\\''") + "'"
    want = command.replace("'{f}'", quoted)
    assert command_rows(r.dump("picked")) == [want]
    # Stored as the row shows it (GSettings prints the string in its own quoting)
    assert ast.literal_eval(r.value("long-press-command")) == want


def test_a_file_that_is_no_local_file_is_left_out(program, gcc_world):
    r = pick(gcc_world, program, "short-press", "https://example.invalid/run.sh")
    assert r.value("short-press-command") == "''"
    assert command_rows(r.dump("picked")) == [""]


# A dismissed dialog is not tested: GTK 4.22.5 on the host aborts ("double free or corruption")
# in its own handler of the portal's Response when the answer is not "chosen" (response 1 or 2),
# before the file's callback runs. The callback's way out without a file is the same line as for
# a file that is no local file (above).


def test_without_plinius_assist_no_group(program, gcc_world, tmp_path):
    gcc_world.extra_env["GSETTINGS_SCHEMA_DIR"] = str(
        schemas_without(gcc_world.session, tmp_path / "schemas", SCHEMA))
    r = gcc_world.run(program, [settle(), call("added"), dump("main")])
    assert r.value("added") is False
    assert not r.dump("main").has(type="AdwComboRow")


def test_without_any_schema_no_group(program, gcc_world, tmp_path):
    gcc_world.extra_env["GSETTINGS_SCHEMA_DIR"] = str(no_schemas(tmp_path / "schemas"))
    r = gcc_world.run(program, [settle(), call("added")])
    assert r.value("added") is False

