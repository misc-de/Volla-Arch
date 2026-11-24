# Face unlock (cc-face-dialog.c): the dialog and the row label from plinius-faced's answers
# (org.plinius.Face1 on the system bus, interface from its C source, a fake on the test's private
# bus): set up with progress hints and the result, try it, switch on, strictness, remove after a
# question; errors in the service's words.

import os

import pytest

from conftest import PKG
from gcc_harness import (build_panel, call, click, dump, gcc_session, gcc_world, interface_xml,  # noqa: F401
                         select, set_active, settle, wait)

FACE = "org.plinius.Face"
FACE_PATH = "/org/plinius/Face"
FACE_IFACE = "org.plinius.Face1"
FACE_XML = interface_xml(PKG / "plinius-face" / "plinius-faced.c", FACE_IFACE)
ACTIVATION = "/usr/share/dbus-1/system-services/org.plinius.Face.service"
UID = os.getuid()

GLUE = r'''
#include "gcc_driver.h"
#include "cc-face-dialog.h"

static CcListRow *row;
static AdwDialog *dialog;

void
gcc_test_setup (GccTest *t)
{
    GtkWidget *group = adw_preferences_group_new ();

    row = g_object_new (CC_TYPE_LIST_ROW, "title", "Face Unlock", "show-arrow", TRUE, NULL);
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), GTK_WIDGET (row));
    adw_preferences_page_add (t->page, ADW_PREFERENCES_GROUP (group));
    if (gcc_test_param (t, "dialog")) {
        dialog = ADW_DIALOG (cc_face_dialog_new ());
        adw_dialog_present (dialog, GTK_WIDGET (t->page));
    }
}

gboolean
gcc_test_call (GccTest *t, const char *name, JsonArray *args, JsonBuilder *result)
{
    if (g_str_equal (name, "close")) {
        json_builder_add_boolean_value (result, adw_dialog_close (dialog));
        return TRUE;
    }
    if (g_str_equal (name, "available")) {
        json_builder_add_boolean_value (result, cc_face_dialog_is_available ());
        return TRUE;
    }
    if (g_str_equal (name, "update_row")) {
        cc_face_dialog_update_row (row);
        json_builder_add_null_value (result);
        return TRUE;
    }
    if (g_str_equal (name, "row_label")) {
        g_autofree char *label = NULL;

        g_object_get (row, "secondary-label", &label, NULL);

        if (label && *label)
            json_builder_add_string_value (result, label);
        else
            json_builder_add_null_value (result);
        return TRUE;
    }
    return FALSE;
}
'''

DIALOG = {"dialog": "yes"}
NOT_AUTHORIZED = {"error": "org.freedesktop.DBus.Error.AccessDenied", "message": "not authorized"}
BUSY = {"error": "org.plinius.Face.Error.Busy", "message": "the camera is in use for another request"}


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    return build_panel(gcc_session, tmp_path_factory.mktemp("face"), "face", ["cc-face-dialog.c"], GLUE,
                       pkgs=("accountsservice",),
                       replacements={"cc-face-dialog.c": [(f'"{ACTIVATION}"', f'"{{root}}{ACTIVATION}"')]})


def user(enrolled=True, enabled=False, strictness="strict"):
    return f"({'true' if enrolled else 'false'}, {'true' if enabled else 'false'}, {strictness!r})"


def faced(world, own=True, **replies):
    replies.setdefault("GetUser", user())
    world.service("system", FACE, FACE_PATH, FACE_XML, replies=replies, own=own)


def view(screen):
    """What the dialog shows: rows by title with subtitle, the buttons and the remove group."""
    d = screen.within(type="CcFaceDialog")
    rows = {r["text"]: r.get("subtitle") for r in d.find(type=("AdwActionRow", "AdwSwitchRow", "AdwComboRow"))}
    buttons = {b["text"]: b.get("sensitive", True) for b in d.find(type="GtkButton") if b.get("text")}
    return rows, buttons


def progress(uid, percent, hint):
    return f"(uint32 {uid}, uint32 {percent}, {hint!r})"


def finished(uid, success, message=""):
    return f"(uint32 {uid}, {'true' if success else 'false'}, {message!r})"


def test_set_up_face_shows_everything(program, gcc_world):
    faced(gcc_world, GetUser=user(enrolled=True, enabled=True, strictness="balanced"))
    r = gcc_world.run(program, [settle(), dump("d")], params=DIALOG)
    screen = r.dump("d")
    rows, buttons = view(screen)
    assert rows["Face"] == "Set up"
    assert "Try It" in rows and "Setting Up" not in rows
    assert buttons == {"Set Up Again": True, "Try": True, "Remove Face": True}
    assert screen.one(type="AdwSwitchRow", text="Unlock the Screen")["active"] is True
    assert screen.one(type="AdwComboRow", text="Strictness")["selected_text"] == "Balanced"
    assert screen.one(type="CcFaceDialog")["text"] == "Face Unlock"
    # Showing what the service says changes nothing in it
    assert [c["member"] for c in gcc_world.calls(FACE)] == ["GetUser"]
    assert not r.warnings()


def test_no_face_yet(program, gcc_world):
    faced(gcc_world, GetUser=user(enrolled=False))
    r = gcc_world.run(program, [settle(), dump("d")], params=DIALOG)
    screen = r.dump("d")
    rows, buttons = view(screen)
    assert rows["Face"] == "Not set up"
    assert "Try It" not in rows
    assert buttons == {"Set Up": True}
    assert screen.one(type="AdwSwitchRow", text="Unlock the Screen").get("sensitive") is False
    assert screen.one(type="AdwComboRow", text="Strictness")["selected_text"] == "Strict"


@pytest.mark.parametrize("reply, shown", [
    (NOT_AUTHORIZED, "Not authorized"),
    (BUSY, "The camera is busy, try again in a moment"),
    ({"error": "org.plinius.Face.Error.Failed", "message": "no model"}, "no model"),
])
def test_service_errors_in_its_words(program, gcc_world, reply, shown):
    faced(gcc_world, GetUser=reply)
    r = gcc_world.run(program, [settle(), dump("d")], params=DIALOG)
    assert view(r.dump("d"))[0]["Face"] == shown


def test_without_a_system_bus(program, gcc_world):
    r = gcc_world.run(program, [settle(), dump("d"), click(type="GtkButton", text="Set Up"), settle()],
                      params=DIALOG, system_bus=False)
    assert view(r.dump("d"))[0]["Face"] == "Not set up"
    assert [m["message"] for m in r.warnings() if m["message"].startswith("system bus:")]


def test_setting_up_with_hints_and_success(program, gcc_world):
    # Enroll answers once the PIN is in (polkit)
    faced(gcc_world, GetUser=[user(enrolled=False), user(enrolled=True, enabled=False)],
          Enroll={"delay_ms": 300})
    ctl = gcc_world.control("system")
    steps = [settle(), click(type="GtkButton", text="Set Up"), dump("asking"), settle(400), dump("started")]
    hints = [("no-face", "Look straight at the phone"), ("too-far", "Hold the phone a little closer"),
             ("tilted", "Hold the phone upright"), ("unclear", "More light, please"),
             ("", "Keep looking at the phone")]
    for i, (hint, _) in enumerate(hints):
        steps += [ctl.emit(FACE_PATH, FACE_IFACE, "EnrollProgress", progress(UID, 10 * (i + 1), hint)),
                  settle(), dump(f"hint{i}")]
    steps += [ctl.emit(FACE_PATH, FACE_IFACE, "EnrollFinished", finished(UID, True)), settle(), dump("done")]
    r = gcc_world.run(program, steps, params=DIALOG)
    rows, buttons = view(r.dump("asking"))
    assert rows["Setting Up"] == "Enter your PIN, then look at the phone"
    assert buttons["Set Up"] is False
    assert view(r.dump("started"))[0]["Setting Up"] == "Keep looking at the phone"
    for i, (_, text) in enumerate(hints):
        assert view(r.dump(f"hint{i}"))[0]["Setting Up"] == f"{text} – {10 * (i + 1)} %"
    rows, buttons = view(r.dump("done"))
    assert rows["Face"] == "Set up" and "Setting Up" not in rows
    assert buttons == {"Set Up Again": True, "Try": True, "Remove Face": True}
    assert [c["member"] for c in gcc_world.calls(FACE)] == ["GetUser", "Enroll", "GetUser"]


@pytest.mark.parametrize("message, shown", [("cancelled", "Cancelled"),
                                             ("no face", "Not set up: the camera did not see the face "
                                                         "clearly enough")])
def test_setting_up_that_fails(program, gcc_world, message, shown):
    faced(gcc_world, GetUser=user(enrolled=False))
    ctl = gcc_world.control("system")
    r = gcc_world.run(program, [settle(), click(type="GtkButton", text="Set Up"), settle(),
                                ctl.emit(FACE_PATH, FACE_IFACE, "EnrollFinished", finished(UID, False, message)),
                                settle(), dump("d")], params=DIALOG)
    rows, buttons = view(r.dump("d"))
    assert rows["Face"] == shown
    assert "Setting Up" not in rows and buttons == {"Set Up": True}
    # No new question after a failure
    assert [c["member"] for c in gcc_world.calls(FACE)] == ["GetUser", "Enroll"]


def test_another_users_enrolling_is_not_ours(program, gcc_world):
    faced(gcc_world, GetUser=user(enrolled=False))
    ctl = gcc_world.control("system")
    r = gcc_world.run(program, [settle(),
                                # Progress while we do not enroll: nothing
                                ctl.emit(FACE_PATH, FACE_IFACE, "EnrollProgress", progress(UID, 5, "no-face")),
                                settle(), dump("idle"),
                                click(type="GtkButton", text="Set Up"), settle(),
                                ctl.emit(FACE_PATH, FACE_IFACE, "EnrollProgress", progress(UID + 1, 50, "tilted")),
                                ctl.emit(FACE_PATH, FACE_IFACE, "EnrollFinished", finished(UID + 1, True)),
                                settle(), dump("other")], params=DIALOG)
    assert "Setting Up" not in view(r.dump("idle"))[0]
    rows, _ = view(r.dump("other"))
    assert rows["Setting Up"] == "Keep looking at the phone"
    assert rows["Face"] == "Not set up"


def test_cancel_while_setting_up(program, gcc_world):
    faced(gcc_world, GetUser=user(enrolled=False))
    r = gcc_world.run(program, [settle(), click(type="GtkButton", text="Set Up"), settle(),
                                click(type="GtkButton", text="Cancel"), settle()], params=DIALOG)
    assert [c["member"] for c in gcc_world.calls(FACE)] == ["GetUser", "Enroll", "Cancel"]
    assert not r.warnings()


def test_refused_setting_up(program, gcc_world):
    faced(gcc_world, GetUser=user(enrolled=False), Enroll=NOT_AUTHORIZED)
    r = gcc_world.run(program, [settle(), click(type="GtkButton", text="Set Up"), settle(), dump("d")],
                      params=DIALOG)
    rows, buttons = view(r.dump("d"))
    assert rows["Face"] == "Not authorized"
    assert "Setting Up" not in rows and buttons == {"Set Up": True}


def test_closing_while_setting_up_stops_the_camera(program, gcc_world):
    faced(gcc_world, GetUser=user(enrolled=False), Enroll={"delay_ms": 2000})
    gcc_world.run(program, [settle(), click(type="GtkButton", text="Set Up"), settle(),
                            call("close"), settle(), wait(50)], params=DIALOG)
    assert [c["member"] for c in gcc_world.calls(FACE)] == ["GetUser", "Enroll", "Cancel"]


@pytest.mark.parametrize("reply, shown", [("(true, 0.874)", "Recognized (87 % alike)"),
                                           ("(false, 0.3)", "Not recognized (30 % alike)"),
                                           (BUSY, "The camera is busy, try again in a moment")])
def test_trying_the_face(program, gcc_world, reply, shown):
    faced(gcc_world, Verify={"delay_ms": 200, "reply": reply} if isinstance(reply, str) else reply)
    r = gcc_world.run(program, [settle(), click(type="GtkButton", text="Try"), dump("looking"), settle(300),
                                dump("d")], params=DIALOG)
    rows, buttons = view(r.dump("looking"))
    if isinstance(reply, str):
        assert rows["Try It"] == "Look at the phone"
        assert buttons["Try"] is False and buttons["Set Up Again"] is False
    rows, buttons = view(r.dump("d"))
    assert rows["Try It"] == shown
    assert buttons["Try"] is True and buttons["Set Up Again"] is True


@pytest.mark.parametrize("active", [True, False])
def test_unlock_switch_follows_the_service(program, gcc_world, active):
    faced(gcc_world, GetUser=[user(enabled=not active), user(enabled=active)])
    r = gcc_world.run(program, [settle(), set_active(active, type="AdwSwitchRow", text="Unlock the Screen"),
                                settle(), dump("d")], params=DIALOG)
    assert [c["args"] for c in gcc_world.calls(FACE, "SetEnabled")] == [[active]]
    assert r.dump("d").one(type="AdwSwitchRow", text="Unlock the Screen")["active"] is active
    # Read again: the switch shows the service's answer, it is not sent back
    assert len(gcc_world.calls(FACE, "GetUser")) == 2


def test_refused_switch_goes_back(program, gcc_world):
    faced(gcc_world, SetEnabled=NOT_AUTHORIZED)
    r = gcc_world.run(program, [settle(), set_active(True, type="AdwSwitchRow", text="Unlock the Screen"),
                                settle(), dump("d")], params=DIALOG)
    screen = r.dump("d")
    assert screen.one(type="AdwSwitchRow", text="Unlock the Screen")["active"] is False
    # The service's answer to GetUser leaves the reason on the face row
    assert view(screen)[0]["Face"] == "Not authorized"
    assert len(gcc_world.calls(FACE, "SetEnabled")) == 1


def test_a_reason_stays_until_the_next_step(program, gcc_world):
    faced(gcc_world, GetUser=[user(), user(), user(strictness="balanced")], Delete=NOT_AUTHORIZED)
    r = gcc_world.run(program, [settle(), click(type="GtkButton", text="Remove Face"), settle(),
                                click(type="GtkButton", text="Remove"), settle(), dump("refused"),
                                select(1, type="AdwComboRow", text="Strictness"), settle(), dump("next")],
                      params=DIALOG)
    rows, buttons = view(r.dump("refused"))
    assert rows["Face"] == "Not authorized" and buttons["Remove Face"] is True
    assert view(r.dump("next"))[0]["Face"] == "Set up"
    assert [c["member"] for c in gcc_world.calls(FACE)] == ["GetUser", "Delete", "GetUser", "SetStrictness",
                                                           "GetUser"]


@pytest.mark.parametrize("index, sent", [(1, "balanced"), (0, "strict")])
def test_strictness(program, gcc_world, index, sent):
    start = "strict" if index == 1 else "balanced"
    faced(gcc_world, GetUser=[user(strictness=start), user(strictness=sent)])
    r = gcc_world.run(program, [settle(), select(index, type="AdwComboRow", text="Strictness"), settle(),
                                dump("d")], params=DIALOG)
    assert [c["args"] for c in gcc_world.calls(FACE, "SetStrictness")] == [[sent]]
    assert r.dump("d").one(type="AdwComboRow")["selected"] == index


def test_remove_asks_first(program, gcc_world):
    faced(gcc_world, GetUser=[user(), user(enrolled=False)])
    r = gcc_world.run(program, [settle(), click(type="GtkButton", text="Remove Face"), settle(), dump("ask"),
                                click(type="GtkButton", text="Cancel", within={"type": "AdwAlertDialog"}),
                                settle(), click(type="GtkButton", text="Remove Face", label="again"), settle(),
                                click(type="GtkButton", text="Remove"), settle(), dump("d")], params=DIALOG)
    ask = r.dump("ask").one(type="AdwAlertDialog")
    assert (ask["text"], ask["subtitle"]) == ("Remove Face?", "Face unlock stops working until the face is set "
                                                              "up again.")
    assert [c["member"] for c in gcc_world.calls(FACE)] == ["GetUser", "Delete", "GetUser"]
    rows, buttons = view(r.dump("d"))
    assert rows["Face"] == "Not set up" and buttons == {"Set Up": True}


def test_refused_remove_says_why(program, gcc_world):
    faced(gcc_world, GetUser=[user(), {"error": "org.plinius.Face.Error.Failed", "message": "gone"}],
          Delete=NOT_AUTHORIZED)
    r = gcc_world.run(program, [settle(), click(type="GtkButton", text="Remove Face"), settle(),
                                click(type="GtkButton", text="Remove"), settle(), dump("d")], params=DIALOG)
    assert view(r.dump("d"))[0]["Face"] == "gone"


@pytest.mark.parametrize("pending", ["GetUser", "SetEnabled", "Verify"])
def test_closing_before_the_answer_is_quiet(program, gcc_world, pending):
    delayed = {"delay_ms": 300, "reply": user()} if pending == "GetUser" else {"delay_ms": 300}
    faced(gcc_world, **{pending: delayed})
    steps = [settle(100)] if pending == "GetUser" else [settle()]
    if pending == "SetEnabled":
        steps.append(set_active(True, type="AdwSwitchRow", text="Unlock the Screen"))
    if pending == "Verify":
        steps.append(click(type="GtkButton", text="Try"))
    r = gcc_world.run(program, [*steps, call("close"), settle(), wait(400), settle()], params=DIALOG)
    # The answer comes to a dialog that is gone: cancelled, nothing touched
    assert not r.warnings()
    assert gcc_world.calls(FACE, pending)


# ----- Availability and the row -----

@pytest.mark.parametrize("installed", [True, False])
def test_available_when_the_service_can_start(program, gcc_world, installed):
    f = program.root / ACTIVATION.lstrip("/")
    if installed:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("[D-BUS Service]\n")
    elif f.exists():
        f.unlink()
    r = gcc_world.run(program, [call("available")])
    assert r.value("available") is installed


@pytest.mark.parametrize("reply, label", [(user(enrolled=False), "Not Set Up"), (user(enabled=True), "On"),
                                          (user(enabled=False), "Off"), (NOT_AUTHORIZED, None)])
def test_row_label(program, gcc_world, reply, label):
    faced(gcc_world, GetUser=reply)
    r = gcc_world.run(program, [call("update_row"), settle(), call("row_label")])
    assert r.value("row_label") == label


def test_row_without_a_system_bus(program, gcc_world):
    r = gcc_world.run(program, [call("update_row"), settle(), call("row_label")], system_bus=False)
    assert r.value("row_label") is None


def test_failed_setting_up_ends_the_progress(program, gcc_world):
    # What does show after a failed setup (the reason: test_setting_up_that_fails)
    faced(gcc_world, GetUser=user(enrolled=False))
    ctl = gcc_world.control("system")
    r = gcc_world.run(program, [settle(), click(type="GtkButton", text="Set Up"), settle(),
                                ctl.emit(FACE_PATH, FACE_IFACE, "EnrollFinished", finished(UID, False, "x")),
                                settle(), dump("d")], params=DIALOG)
    rows, buttons = view(r.dump("d"))
    assert "Setting Up" not in rows and buttons == {"Set Up": True}
    assert [c["member"] for c in gcc_world.calls(FACE)] == ["GetUser", "Enroll"]
