# The Encryption page (cc-plinius-crypt.c): the state from "plinius-encrypt --status", encrypting
# at the next restart, withdrawing that, and changing the password, each through a fake pkexec
# that runs a fake plinius-encrypt in the program's root. The fake tool keeps its state in a file
# and answers with the exit code and message a test gives it; it records its arguments and stdin.

import pytest

from gcc_harness import (build_panel, call, click, dump, gcc_session, gcc_world, push,  # noqa: F401
                         set_text, settle, wait, wait_for)

TOOL = "/usr/bin/plinius-encrypt"

GLUE = r'''
#include "gcc_driver.h"
#include "cc-plinius-crypt.h"
#include "cc-list-row.h"

static gboolean have_page;

void
gcc_test_setup (GccTest *t)
{
    AdwNavigationPage *page = cc_plinius_crypt_page_new ();
    /* The row on Privacy & Security (a CcListRow there), hidden until set up */
    GtkWidget *row = g_object_new (CC_TYPE_LIST_ROW, NULL);

    gtk_widget_set_visible (row, FALSE);
    cc_plinius_crypt_row_setup (ADW_ACTION_ROW (row));
    gcc_test_add (t, row);
    have_page = page != NULL;
    if (page)
        cc_panel_add_subpage (t->panel, "encryption", page);
}

gboolean
gcc_test_call (GccTest *t, const char *name, JsonArray *args, JsonBuilder *result)
{
    if (!g_str_equal (name, "have_page"))
        return FALSE;
    json_builder_add_boolean_value (result, have_page);
    return TRUE;
}
'''


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    return build_panel(gcc_session, tmp_path_factory.mktemp("crypt"), "crypt", ["cc-plinius-crypt.c"], GLUE,
                       replacements={"cc-plinius-crypt.c": [(f'"{TOOL}"', f'"{{root}}{TOOL}"')]})


class Crypt:
    """The fake plinius-encrypt and pkexec."""

    def __init__(self, program, world):
        self.world = world
        self.dir = world.tmp / "crypt"
        self.dir.mkdir()
        self.tool = program.root / TOOL.lstrip("/")
        self.tool.parent.mkdir(parents=True, exist_ok=True)
        d = self.dir
        self.tool.write_text(f"""#!/bin/bash
d="{d}"
if [ "$1" = --status ]; then cat "$d/state" 2>/dev/null; exit 0; fi
printf '%s\\n' "$*" >> "$d/args"
cat >> "$d/stdin"
if [ -e "$d/wait" ]; then for i in $(seq 1 400); do [ -e "$d/go" ] && break; sleep 0.05; done; fi
code=$(cat "$d/code" 2>/dev/null || echo 0)
[ -e "$d/message" ] && cat "$d/message" >&2
if [ "$code" = 0 ]; then
    case "$1" in
    --request) echo requested > "$d/state" ;;
    --cancel) echo off > "$d/state" ;;
    esac
fi
exit $code
""")
        self.tool.chmod(0o755)
        world.fake_bin.stub("pkexec", 'exec "$@"')
        self.state("off")

    def state(self, text):
        (self.dir / "state").write_text(text + "\n" if text is not None else "")

    def answer(self, code, message=None, wait=False):
        (self.dir / "code").write_text(str(code))
        if message is not None:
            (self.dir / "message").write_text(message + "\n")
        if wait:
            (self.dir / "wait").write_text("")

    def go(self):
        return {"op": "write_file", "path": str(self.dir / "go"), "text": ""}

    def args(self):
        f = self.dir / "args"
        return f.read_text().splitlines() if f.exists() else []

    def stdin(self):
        f = self.dir / "stdin"
        return f.read_text() if f.exists() else ""


@pytest.fixture
def crypt(program, gcc_world):
    return Crypt(program, gcc_world)


def state_row(screen):
    rows = [r for r in screen.find(type="AdwActionRow") if any(
        c.get("type") == "GtkImage" for c in r.get("children", []))]
    assert len(rows) == 1, screen.outline()
    icon = [c for c in rows[0]["children"] if c["type"] == "GtkImage"][0]["icon"]
    return rows[0]["text"], rows[0]["subtitle"], icon


def buttons(screen):
    return [r["text"] for r in screen.find(type="AdwButtonRow")]


def test_row_on_the_privacy_page(program, gcc_world, crypt):
    r = gcc_world.run(program, [dump("main")])
    row = r.dump("main").one(type="CcListRow")
    assert (row["text"], row["subtitle"]) == ("Encryption", "Password at startup for the data on the phone")


def test_no_page_without_plinius_crypt(program, gcc_world, crypt):
    crypt.tool.chmod(0o644)
    r = gcc_world.run(program, [call("have_page")])
    assert r.value("have_page") is False


@pytest.mark.parametrize("state, row, offered", [
    ("encrypted", ("Encrypted", "The phone asks for the encryption password at every start. Without it, "
                   "nobody can read the data.", "security-high-symbolic"), ["Change Password…"]),
    ("requested", ("Encryption at the Next Restart", "At the next start the phone asks for a new encryption "
                   "password, then encrypts. This takes a while, keep the phone charging.",
                   "security-medium-symbolic"), ["Cancel Encryption"]),
    ("off", ("Not Encrypted", "Whoever has the phone in hand can read the data from its storage.",
             "security-low-symbolic"), ["Encrypt…"]),
    ("unsupported", ("Not Available", "This system does not start with the encryption support, so it cannot "
                     "be encrypted.", "dialog-information-symbolic"), []),
    (None, ("Not Available", "This system does not start with the encryption support, so it cannot "
            "be encrypted.", "dialog-information-symbolic"), []),
])
def test_state_and_what_it_offers(program, gcc_world, crypt, state, row, offered):
    crypt.state(state)
    r = gcc_world.run(program, [push("encryption"), dump("page")])
    s = r.dump("page")
    assert state_row(s) == row
    assert buttons(s) == offered
    assert not r.warnings()


def test_state_read_again_when_the_page_shows(program, gcc_world, crypt):
    r = gcc_world.run(program, [push("encryption"), dump("before"), {"op": "back"},
                                {"op": "write_file", "path": str(crypt.dir / "state"), "text": "encrypted\n"},
                                push("encryption"), dump("after")])
    assert state_row(r.dump("before"))[0] == "Not Encrypted"
    assert state_row(r.dump("after"))[0] == "Encrypted"


def test_encrypt_asks_then_requests(program, gcc_world, crypt):
    r = gcc_world.run(program, [push("encryption"), click(type="AdwButtonRow", text="Encrypt…"),
                                wait_for(type="AdwAlertDialog"), dump("asked"),
                                click(type="GtkButton", text="Encrypt at Restart"),
                                wait_for(type="AdwButtonRow", text="Cancel Encryption"), dump("done")])
    alert = r.dump("asked").one(type="AdwAlertDialog")
    assert alert["text"] == "Encrypt the Phone?"
    assert alert["subtitle"].startswith("At the next restart the phone asks twice for a new encryption password")
    assert alert["subtitle"].endswith("It must differ from the screen lock PIN.")
    assert crypt.args() == ["--request"]
    assert crypt.stdin() == ""
    assert state_row(r.dump("done"))[0] == "Encryption at the Next Restart"
    assert not r.dump("done").has(type="AdwAlertDialog")


def test_encrypt_cancelled_in_the_question(program, gcc_world, crypt):
    r = gcc_world.run(program, [push("encryption"), click(type="AdwButtonRow", text="Encrypt…"),
                                wait_for(type="AdwAlertDialog"), click(type="GtkButton", text="Cancel"),
                                settle(200), dump("done")])
    assert crypt.args() == []
    assert buttons(r.dump("done")) == ["Encrypt…"]


@pytest.mark.parametrize("code, message, heading, body", [
    (7, "This system cannot be encrypted.", "Encryption Not Possible", "This system cannot be encrypted."),
    (1, "Not enough free space.", "Encryption Failed", "Not enough free space."),
    (1, "", "Encryption Failed", None),
    (126, "Dismissed", None, None),
    (127, "Not authorized", None, None),
])
def test_encrypt_errors(program, gcc_world, crypt, code, message, heading, body):
    crypt.answer(code, message)
    r = gcc_world.run(program, [push("encryption"), click(type="AdwButtonRow", text="Encrypt…"),
                                wait_for(type="AdwAlertDialog"), click(type="GtkButton", text="Encrypt at Restart"),
                                wait_for(type="AdwAlertDialog", text="Encrypt the Phone?", gone=True),
                                settle(500), dump("done")])
    alerts = r.dump("done").find(type="AdwAlertDialog")
    if heading is None:
        assert alerts == []
    else:
        assert [(a["text"], a.get("subtitle")) for a in alerts] == [(heading, body)]
    assert buttons(r.dump("done")) == ["Encrypt…"]


def test_without_pkexec_the_error_says_why(program, gcc_world, crypt):
    (gcc_world.fake_bin.path / "pkexec").unlink()
    r = gcc_world.run(program, [push("encryption"), click(type="AdwButtonRow", text="Encrypt…"),
                                wait_for(type="AdwAlertDialog"), click(type="GtkButton", text="Encrypt at Restart"),
                                wait_for(type="AdwAlertDialog", text="Encryption Failed"), dump("done")])
    alert = r.dump("done").one(type="AdwAlertDialog")
    assert "pkexec" in alert["subtitle"]


def test_cancel_encryption(program, gcc_world, crypt):
    crypt.state("requested")
    r = gcc_world.run(program, [push("encryption"), click(type="AdwButtonRow", text="Cancel Encryption"),
                                wait_for(type="AdwButtonRow", text="Encrypt…"), dump("done")])
    assert crypt.args() == ["--cancel"]
    assert state_row(r.dump("done"))[0] == "Not Encrypted"


def test_cancel_encryption_failing(program, gcc_world, crypt):
    crypt.state("requested")
    crypt.answer(2, "Busy.")
    r = gcc_world.run(program, [push("encryption"), click(type="AdwButtonRow", text="Cancel Encryption"),
                                wait_for(type="AdwAlertDialog"), dump("done")])
    alert = r.dump("done").one(type="AdwAlertDialog")
    assert (alert["text"], alert["subtitle"]) == ("Cancelling Failed", "Busy.")
    assert buttons(r.dump("done")) == ["Cancel Encryption"]


def test_cancel_encryption_dismissed_says_nothing(program, gcc_world, crypt):
    crypt.state("requested")
    crypt.answer(126)
    r = gcc_world.run(program, [push("encryption"), click(type="AdwButtonRow", text="Cancel Encryption"),
                                settle(500), dump("done")])
    assert not r.dump("done").has(type="AdwAlertDialog")


# ----- Change password -----

def open_dialog():
    return [push("encryption"), click(type="AdwButtonRow", text="Change Password…"),
            wait_for(type="AdwDialog", text="Change Password")]


def fill(current="old secret", new="new secret", repeat=None):
    return [set_text(current, type="AdwPasswordEntryRow", text="Current Password"),
            set_text(new, type="AdwPasswordEntryRow", text="New Password"),
            set_text(new if repeat is None else repeat, type="AdwPasswordEntryRow", text="Repeat New Password")]


def change_button(screen):
    return screen.one(type="GtkButton", text="Change")


def entry_errors(screen):
    return [r["text"] for r in screen.find(type="AdwPasswordEntryRow") if "error" in (r.get("classes") or [])]


def error_label(screen):
    labels = [n for n in screen.within(type="AdwDialog").all()
              if n.get("type") == "GtkLabel" and "error" in (n.get("classes") or [])]
    return labels[0]["text"] if labels else None


def test_dialog_and_change_only_when_all_filled_and_matching(program, gcc_world, crypt):
    crypt.state("encrypted")
    r = gcc_world.run(program, [*open_dialog(), dump("empty"),
                                set_text("old", type="AdwPasswordEntryRow", text="Current Password"),
                                set_text("new1", type="AdwPasswordEntryRow", text="New Password"), dump("two"),
                                set_text("new2", type="AdwPasswordEntryRow", text="Repeat New Password"),
                                dump("mismatch"),
                                set_text("new1", type="AdwPasswordEntryRow", text="Repeat New Password"),
                                dump("ready")])
    empty = r.dump("empty")
    group = empty.one(type="AdwPreferencesGroup", subtitle=(
        "The password is asked at every start, on the keyboard shown there. It must differ from the screen "
        "lock PIN."))
    assert [n["text"] for n in group.walk() if n.get("type") == "AdwPasswordEntryRow"] == [
        "Current Password", "New Password", "Repeat New Password"]
    assert change_button(empty).get("sensitive") is False
    assert change_button(r.dump("two")).get("sensitive") is False
    mismatch = r.dump("mismatch")
    assert change_button(mismatch).get("sensitive") is False
    assert error_label(mismatch) == "The new passwords do not match."
    assert entry_errors(mismatch) == ["Repeat New Password"]
    ready = r.dump("ready")
    assert change_button(ready).get("sensitive") is None
    assert error_label(ready) is None
    assert entry_errors(ready) == []


def test_change_sends_the_passwords_on_stdin_and_closes(program, gcc_world, crypt):
    crypt.state("encrypted")
    crypt.answer(0, wait=True)
    r = gcc_world.run(program, [*open_dialog(), *fill(), click(type="GtkButton", text="Change"), settle(),
                                dump("busy"), crypt.go(),
                                wait_for(type="AdwDialog", text="Change Password", gone=True), dump("done")])
    busy = r.dump("busy")
    assert busy.has(type="GtkLabel", text="Changing the password. This takes a few seconds, do not switch the "
                                          "phone off.")
    assert change_button(busy).get("sensitive") is False
    assert busy.one(type="GtkButton", text="Cancel").get("sensitive") is False
    assert busy.one(type="AdwPasswordEntryRow", text="Current Password").get("sensitive") is False
    assert crypt.args() == ["--change-password --stdin"]
    # Never on the command line
    assert crypt.stdin() == "old secret\nnew secret\n"
    assert state_row(r.dump("done"))[0] == "Encrypted"


def test_busy_dialog_cannot_be_closed(program, gcc_world, crypt):
    crypt.state("encrypted")
    crypt.answer(0, wait=True)
    r = gcc_world.run(program, [*open_dialog(), *fill(), click(type="GtkButton", text="Change"), settle(),
                                {"op": "close_dialog"}, settle(), dump("still"), crypt.go(),
                                wait_for(type="AdwDialog", text="Change Password", gone=True)])
    assert r.dump("still").has(type="AdwDialog", text="Change Password")


@pytest.mark.parametrize("code, message, shown, marked", [
    (3, "", "The current password is wrong.", ["Current Password"]),
    (4, "The keyboard at startup has no “€”.", "The keyboard at startup has no “€”.", ["New Password"]),
    (5, "", "The new password is the screen lock PIN. Choose a different one, so that the PIN alone does not "
            "open the phone.", ["New Password"]),
    (2, "No space left.", "No space left.", []),
    (2, "", "Changing the password failed.", []),
    (126, "", None, []),
    (127, "", None, []),
])
def test_change_errors_stay_in_the_dialog(program, gcc_world, crypt, code, message, shown, marked):
    crypt.state("encrypted")
    crypt.answer(code, message)
    r = gcc_world.run(program, [*open_dialog(), *fill(), click(type="GtkButton", text="Change"),
                                settle(), wait(300), settle(), dump("done")])
    s = r.dump("done")
    assert s.has(type="AdwDialog", text="Change Password")
    assert error_label(s) == shown
    assert entry_errors(s) == marked
    # Unlocked again for another try
    assert change_button(s).get("sensitive") is None
    assert s.one(type="GtkButton", text="Cancel").get("sensitive") is None


def test_cancel_closes_the_dialog(program, gcc_world, crypt):
    crypt.state("encrypted")
    r = gcc_world.run(program, [*open_dialog(), *fill(), click(type="GtkButton", text="Cancel"),
                                wait_for(type="AdwDialog", text="Change Password", gone=True), settle(),
                                dump("done")])
    assert crypt.args() == []
    assert not r.dump("done").has(type="AdwDialog")


def test_page_gone_while_the_tool_runs(program, gcc_world, crypt):
    # The window closes while plinius-encrypt still works: its answer comes to nobody, no crash
    crypt.state("encrypted")
    crypt.answer(0, wait=True)
    r = gcc_world.run(program, [*open_dialog(), *fill(), click(type="GtkButton", text="Change"), settle()])
    (crypt.dir / "go").write_text("")
    assert r.returncode == 0
    assert not r.warnings()
