# Change Username (cc-username-dialog.c): the dialog that schedules the renaming of one's own login
# name through pkexec plinius-rename-user (plinius-account). The user comes from a fake
# accountsservice (org.freedesktop.Accounts on the private system bus), GNOME's username check
# runs a fake usermod, pkexec is a fake that records what it was asked and writes or removes the
# request file in the program's root, the restart goes to a fake logind. The template is
# tests/gcc/ui/cc-username-dialog.ui, written by hand from the .blp (see test_gcc_blp_ui_match.py).

import os
import shutil
from pathlib import Path

import pytest

from gcc_harness import (UI, build_panel, call, click, compile_resources, dump, enter,  # noqa: F401
                         gcc_session, gcc_world, set_text, settle, wait, wait_for)

ACCOUNTS = "org.freedesktop.Accounts"
ACCOUNTS_PATH = "/org/freedesktop/Accounts"
IFACES = Path("/usr/share/dbus-1/interfaces")
LOGIN1 = "org.freedesktop.login1"
LOGIN1_PATH = "/org/freedesktop/login1"
LOGIN1_XML = """<node><interface name="org.freedesktop.login1.Manager">
 <method name="Reboot"><arg type="b" name="interactive" direction="in"/></method>
</interface></node>"""
HELPER = "/usr/bin/plinius-rename-user"
REQUEST = "/var/lib/plinius-account/rename"
RULE = ("Lower case letters, numbers, hyphens and underscores, starting with a letter, at most 32 "
        "characters.")

GLUE = r'''
#include <act/act.h>
#include "gcc_driver.h"
#include "cc-username-dialog.h"

static ActUser *user;
static AdwDialog *dialog;

/* The user as GNOME's Users panel has it: from accountsservice, once it is loaded */
static ActUser *
load_user (const char *name)
{
    ActUserManager *manager = act_user_manager_get_default ();
    ActUser *u = act_user_manager_get_user (manager, name);
    gint64 end = g_get_monotonic_time () + 10 * G_USEC_PER_SEC;

    while (!act_user_is_loaded (u) && g_get_monotonic_time () < end)
        if (!g_main_context_iteration (NULL, FALSE))
            g_usleep (1000);
    g_assert (act_user_is_loaded (u));
    return u;
}

void
gcc_test_setup (GccTest *t)
{
    user = load_user (gcc_test_param (t, "user"));
    if (g_strcmp0 (gcc_test_param (t, "dialog"), "no") != 0) {
        dialog = ADW_DIALOG (cc_username_dialog_new (user));
        adw_dialog_present (dialog, GTK_WIDGET (t->window));
    }
}

/* The titles of the toasts on screen (AdwToastWidget is libadwaita's own, its label the title) */
static void
collect_toasts (GtkWidget *w, gboolean inside, JsonBuilder *b)
{
    if (!gtk_widget_get_mapped (w))
        return;
    if (!inside && g_str_equal (G_OBJECT_TYPE_NAME (w), "AdwToastWidget"))
        inside = TRUE;
    else if (inside && GTK_IS_LABEL (w) && *gtk_label_get_text (GTK_LABEL (w)))
        json_builder_add_string_value (b, gtk_label_get_text (GTK_LABEL (w)));
    for (GtkWidget *c = gtk_widget_get_first_child (w); c; c = gtk_widget_get_next_sibling (c))
        collect_toasts (c, inside, b);
}

gboolean
gcc_test_call (GccTest *t, const char *name, JsonArray *args, JsonBuilder *result)
{
    if (g_str_equal (name, "wait_toasts")) {
        /* Until a toast shows (at most 10 s), without waiting for the main loop to get quiet: a
         * spinner elsewhere never lets it */
        gint64 end = g_get_monotonic_time () + 10 * G_USEC_PER_SEC;

        for (;;) {
            g_autoptr(JsonBuilder) probe = json_builder_new ();
            g_autoptr(JsonNode) node = NULL;

            json_builder_begin_array (probe);
            collect_toasts (GTK_WIDGET (t->window), FALSE, probe);
            json_builder_end_array (probe);
            node = json_builder_get_root (probe);
            if (json_array_get_length (json_node_get_array (node)) > 0 || g_get_monotonic_time () > end) {
                json_builder_add_value (result, g_steal_pointer (&node));
                return TRUE;
            }
            if (!g_main_context_iteration (NULL, FALSE))
                g_usleep (1000);
        }
    }
    if (g_str_equal (name, "toasts")) {
        json_builder_begin_array (result);
        collect_toasts (GTK_WIDGET (t->window), FALSE, result);
        json_builder_end_array (result);
        return TRUE;
    }
    if (g_str_equal (name, "available")) {
        json_builder_add_boolean_value (result, cc_username_dialog_available (user));
        return TRUE;
    }
    if (g_str_equal (name, "pending")) {
        g_autofree char *pending = cc_username_dialog_get_pending (user);

        if (pending)
            json_builder_add_string_value (result, pending);
        else
            json_builder_add_null_value (result);
        return TRUE;
    }
    if (g_str_equal (name, "close")) {
        adw_dialog_force_close (dialog);
        json_builder_add_null_value (result);
        return TRUE;
    }
    return FALSE;
}
'''


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    if not (IFACES / f"{ACCOUNTS}.xml").exists():
        pytest.skip("no accountsservice interface files on the host")
    out = tmp_path_factory.mktemp("username")
    resources = compile_resources(out, UI / "username.gresource.xml", "cc_test_username")
    # GNOME's username check runs /usr/sbin/usermod: here the fake from PATH
    utils = out / "user-utils.c"
    text = (gcc_session.tree.root / "panels/system/users/user-utils.c").read_text()
    assert '"/usr/sbin/usermod"' in text
    utils.write_text(text.replace('"/usr/sbin/usermod"', '"usermod"'))
    reps = [(f'"{HELPER}"', f'"{{root}}{HELPER}"'), (f'"{REQUEST}"', f'"{{root}}{REQUEST}"'),
            ('g_build_filename ("/home"', 'g_build_filename ("{root}/home"')]
    return build_panel(gcc_session, out, "username", ["cc-username-dialog.c"], GLUE,
                       replacements={"cc-username-dialog.c": reps},
                       upstream=["panels/system/users/cc-entry-feedback.c"],
                       sources=[resources, utils], pkgs=("accountsservice",),
                       flags=['-DGNOMELOCALEDIR="/usr/share/locale"'])


@pytest.fixture
def root(program):
    """The program's root, empty for each test, with an executable helper."""
    for d in list(program.root.iterdir()):
        shutil.rmtree(d) if d.is_dir() else d.unlink()
    helper = program.root / HELPER.lstrip("/")
    helper.parent.mkdir(parents=True)
    helper.write_text("#!/bin/sh\nexit 0\n")
    helper.chmod(0o755)
    return program.root


def accounts(world, name="alice", uid=None, local=True, homed=False):
    path = f"{ACCOUNTS_PATH}/User{uid if uid is not None else os.getuid()}"
    world.service("system", ACCOUNTS, ACCOUNTS_PATH, (IFACES / f"{ACCOUNTS}.xml").read_text(),
                  replies={"ListCachedUsers": f"([objectpath '{path}'],)",
                           "FindUserByName": f"(objectpath '{path}',)"},
                  properties={"DaemonVersion": "'23.13'"})
    world.service("system", ACCOUNTS, path, (IFACES / f"{ACCOUNTS}.User.xml").read_text(),
                  properties={"UserName": repr(name), "RealName": "'Alice'",
                              "Uid": f"uint64 {uid if uid is not None else os.getuid()}",
                              "HomeDirectory": f"'/home/{name}'",
                              "LocalAccount": str(local).lower(), "UsesHomed": str(homed).lower()})


def usermod(world, code=6, sleep=0):
    """GNOME's check: usermod --login NAME -- NAME; exit 6 = no such user (the name is free),
    0 = the user exists, 3 = a bad character."""
    world.fake_bin.stub("usermod", (f"sleep {sleep}\n" if sleep else "") + f"exit {code}")


def pkexec(world, root, code=0, stderr="", write=None, remove=False, script=""):
    """pkexec HELPER ARGS…: records the call, writes the request file (write: its text) or
    removes it, says stderr and exits with code."""
    request = root / REQUEST.lstrip("/")
    lines = [script]
    if write is not None:
        lines.append(f"mkdir -p '{request.parent}' && printf '%s\\n' '{write}' > '{request}'")
    if remove:
        lines.append(f"rm -f '{request}'")
    if stderr:
        lines.append(f"printf '%s' '{stderr}' >&2")
    lines.append(f"exit {code}")
    world.fake_bin.stub("pkexec", "\n".join(lines))


def request(root, text):
    f = root / REQUEST.lstrip("/")
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)


def run(world, steps, user="alice", **kw):
    return world.run_program(steps, user=user, **kw)


@pytest.fixture
def world(gcc_world, program):
    """gcc_world with run_program: the program with the user's name as a parameter."""
    def run_program(steps, user="alice", dialog=True, **kw):
        params = {"user": user}
        if not dialog:
            params["dialog"] = "no"
        return gcc_world.run(program, steps, params=params, **kw)
    gcc_world.run_program = run_program
    return gcc_world


def feedback(screen):
    """The feedback below the entry (CcEntryFeedback, whose image and label stand in the group):
    (icon, text); no icon while GNOME's spinner turns."""
    group = next(g for g in screen.find(type="AdwPreferencesGroup")
                 if g.get("subtitle", "").startswith("The name you log in with"))
    kids = group.get("children", [])
    icons = [k.get("icon") for k in kids if k["type"] == "GtkImage"]
    texts = [k.get("text") for k in kids if k["type"] == "GtkLabel"]
    return (icons[0] if icons else None), (texts[0] if texts else None)


def rename_sensitive(screen):
    return screen.one(type="GtkButton", text="Rename").get("sensitive", True)


TYPE = "Lower case letters"


def test_shows_the_current_name_and_nothing_pending(world, root):
    accounts(world)
    r = run(world, [settle(), dump("dialog")])
    s = r.dump("dialog")
    assert s.one(type="CcUsernameDialog")["text"] == "Change Username"
    assert s.one(type="AdwActionRow", text="Current Username")["subtitle"] == "alice"
    assert not s.has(type="AdwActionRow", text="Scheduled for the Next Restart")
    assert rename_sensitive(s) is False
    assert feedback(s) == ("info-outline-symbolic", RULE)
    assert not r.warnings()


@pytest.mark.parametrize("name, message", [
    ("Alice2", RULE), ("9lives", RULE), ("a" * 33, RULE), ("has space", RULE),
    ("root", "A group with this name already exists."),
])
def test_names_refused_before_asking_gnome(world, root, name, message):
    accounts(world)
    usermod(world)
    r = run(world, [settle(), set_text(name, type="AdwEntryRow"), settle(), dump("typed")])
    s = r.dump("typed")
    assert feedback(s) == ("dialog-error-symbolic", message)
    assert rename_sensitive(s) is False
    assert not world.fake_bin.calls("usermod")


def test_a_home_folder_of_that_name_is_refused(world, root):
    accounts(world)
    usermod(world)
    (root / "home" / "bob").mkdir(parents=True)
    r = run(world, [settle(), set_text("bob", type="AdwEntryRow"), settle(), dump("typed")])
    assert feedback(r.dump("typed")) == ("dialog-error-symbolic",
                                         "A home folder with this name already exists.")
    assert not world.fake_bin.calls("usermod")


@pytest.mark.parametrize("text", ["alice", "  ", ""])
def test_own_name_or_nothing_shows_the_rule_again(world, root, text):
    accounts(world)
    usermod(world)
    r = run(world, [settle(), set_text("Bad", type="AdwEntryRow"), settle(),
                    set_text(text, type="AdwEntryRow"), settle(), dump("typed")])
    s = r.dump("typed")
    assert feedback(s) == ("info-outline-symbolic", RULE)
    assert rename_sensitive(s) is False
    assert not world.fake_bin.calls("usermod")


def test_a_free_name_can_be_renamed_to(world, root):
    accounts(world)
    usermod(world, 6, sleep=0.3)
    r = run(world, [settle(), set_text(" bob_2 ", type="AdwEntryRow"), wait(100), dump("checking"),
                    wait_for(type="GtkLabel", text="This username is available."), dump("free")])
    # While usermod runs: GNOME's spinner, no icon
    checking = r.dump("checking")
    assert feedback(checking)[1] == "Checking username…"
    assert rename_sensitive(checking) is False
    free = r.dump("free")
    assert feedback(free) == ("check-outlined-symbolic", "This username is available.")
    assert rename_sensitive(free) is not False
    assert world.fake_bin.calls("usermod") == [["--login", "bob_2", "--", "bob_2"]]


@pytest.mark.parametrize("code, tip", [
    (0, "Username is already in use — please choose another"),
    (3, "Usernames cannot include “2”"),
])
def test_gnome_check_refuses(world, root, code, tip):
    accounts(world)
    usermod(world, code)
    r = run(world, [settle(), set_text("bob2", type="AdwEntryRow"),
                    wait_for(type="GtkLabel", text=tip), dump("refused")])
    s = r.dump("refused")
    assert feedback(s) == ("dialog-error-symbolic", tip)
    assert rename_sensitive(s) is False


def test_a_failing_check_leaves_it_to_the_helper(world, root):
    accounts(world)
    usermod(world, 1)
    r = run(world, [settle(), set_text("bob", type="AdwEntryRow"),
                    wait_for(type="GtkLabel", text="This username is available."), dump("free")])
    assert rename_sensitive(r.dump("free")) is not False
    assert [m["message"] for m in r.warnings()] == [
        "Could not check username by usermod: Child process exited with code 1"]


def test_typing_on_cancels_the_running_check(world, root):
    accounts(world)
    usermod(world, 6, sleep=0.4)
    r = run(world, [settle(), set_text("bob", type="AdwEntryRow"), wait(100),
                    set_text("Bad", type="AdwEntryRow"), wait(700), dump("after"), call("toasts")])
    # The first check's answer comes after the second name was refused: it changes nothing
    assert feedback(r.dump("after")) == ("dialog-error-symbolic", RULE)
    assert len(world.fake_bin.calls("usermod")) == 1


def ready(name="bob"):
    return [settle(), set_text(name, type="AdwEntryRow"),
            wait_for(type="GtkLabel", text="This username is available.")]


def test_rename_schedules_and_offers_a_restart(world, root):
    accounts(world)
    usermod(world)
    pkexec(world, root, write="alice bob")
    world.service("system", LOGIN1, LOGIN1_PATH, LOGIN1_XML)
    r = run(world, [*ready(), click(type="GtkButton", text="Rename"),
                    wait_for(type="AdwAlertDialog"), dump("asked"),
                    click(type="GtkButton", text="Restart"), settle(100), dump("after"), call("toasts")])
    assert world.fake_bin.calls("pkexec") == [[str(root) + HELPER, "alice", "bob"]]
    s = r.dump("asked")
    alert = s.one(type="AdwAlertDialog")
    assert alert["text"] == "Restart Now?"
    assert alert["subtitle"] == ("On restart, alice will be renamed to bob. After that, you log in "
                                 "as bob.")
    assert s.one(type="AdwActionRow", text="Scheduled for the Next Restart")["subtitle"] == \
        "alice → bob"
    # The entry is empty again
    assert s.one(type="AdwEntryRow")["value"] == ""
    assert [c["args"] for c in world.calls(LOGIN1, "Reboot")] == [[True]]
    assert not r.dump("after").has(type="AdwAlertDialog")
    assert not r.warnings()


def test_enter_in_the_entry_renames_too(world, root):
    accounts(world)
    usermod(world)
    pkexec(world, root, write="alice bob")
    run(world, [*ready(), enter(type="AdwEntryRow"), wait_for(type="AdwAlertDialog"),
                click(type="GtkButton", text="Later"), settle()])
    assert world.fake_bin.calls("pkexec") == [[str(root) + HELPER, "alice", "bob"]]


def test_enter_before_the_name_is_checked_does_nothing(world, root):
    accounts(world)
    usermod(world, 6, sleep=0.5)
    pkexec(world, root)
    run(world, [settle(), set_text("bob", type="AdwEntryRow"), wait(100),
                enter(type="AdwEntryRow"), settle()])
    assert not world.fake_bin.calls("pkexec")


def test_later_does_not_restart(world, root):
    accounts(world)
    usermod(world)
    pkexec(world, root, write="alice bob")
    world.service("system", LOGIN1, LOGIN1_PATH, LOGIN1_XML)
    r = run(world, [*ready(), click(type="GtkButton", text="Rename"),
                    wait_for(type="AdwAlertDialog"), click(type="GtkButton", text="Later"),
                    settle(100), dump("after"), call("toasts")])
    assert not world.calls(LOGIN1)
    assert not r.dump("after").has(type="AdwAlertDialog")
    # Still scheduled
    assert r.dump("after").has(type="AdwActionRow", text="Scheduled for the Next Restart")


def test_refused_restart_says_so(world, root):
    accounts(world)
    usermod(world)
    pkexec(world, root, write="alice bob")
    world.service("system", LOGIN1, LOGIN1_PATH, LOGIN1_XML, replies={
        "Reboot": {"error": "org.freedesktop.DBus.Error.AccessDenied", "message": "Other users"}})
    r = run(world, [*ready(), click(type="GtkButton", text="Rename"),
                    wait_for(type="AdwAlertDialog"), click(type="GtkButton", text="Restart"),
                    call("wait_toasts"), dump("after"), call("toasts")])
    assert r.value("wait_toasts") == ["Restarting failed"]
    assert [m["message"] for m in r.warnings()] == [
        "Could not restart: GDBus.Error:org.freedesktop.DBus.Error.AccessDenied: Other users"]


@pytest.mark.parametrize("stderr, toast", [
    ("plinius-rename-user: bob is logged in\n", "plinius-rename-user: bob is logged in"),
    ("", "Renaming failed"),
])
def test_failed_rename_says_why_and_checks_again(world, root, stderr, toast):
    accounts(world)
    usermod(world)
    pkexec(world, root, code=1, stderr=stderr)
    r = run(world, [*ready(), click(type="GtkButton", text="Rename"),
                    call("wait_toasts"),
                    wait_for(type="GtkLabel", text="This username is available."), dump("after"), call("toasts")])
    s = r.dump("after")
    assert r.value("wait_toasts") == [toast]
    assert not s.has(type="AdwAlertDialog")
    assert not s.has(type="AdwActionRow", text="Scheduled for the Next Restart")
    # The name was checked again and can be tried once more
    assert len(world.fake_bin.calls("usermod")) == 2
    assert rename_sensitive(s) is not False
    assert s.one(type="AdwEntryRow")["value"] == "bob"


@pytest.mark.parametrize("code", [126, 127])
def test_dismissed_password_prompt_is_silent(world, root, code):
    accounts(world)
    usermod(world)
    pkexec(world, root, code=code, stderr="Error executing command as another user")
    r = run(world, [*ready(), click(type="GtkButton", text="Rename"),
                    wait_for(type="GtkLabel", text="This username is available."), settle(100),
                    dump("after"), call("toasts")])
    assert not r.value("toasts")
    assert not r.dump("after").has(type="AdwAlertDialog")


def test_rename_button_is_off_while_the_helper_runs(world, root):
    accounts(world)
    usermod(world)
    pkexec(world, root, write="alice bob", script="sleep 0.5")
    r = run(world, [*ready(), click(type="GtkButton", text="Rename"), settle(30), dump("running"),
                    wait_for(type="AdwAlertDialog")])
    assert rename_sensitive(r.dump("running")) is False


def test_helper_killed_by_a_signal_says_renaming_failed(world, root):
    accounts(world)
    usermod(world)
    pkexec(world, root, script="kill -9 $$")
    r = run(world, [*ready(), click(type="GtkButton", text="Rename"),
                    call("wait_toasts"), dump("after"), call("toasts")])
    assert r.value("wait_toasts") == ["Renaming failed"]
    assert not r.warnings()


def test_without_pkexec_nothing_runs(world, root):
    accounts(world)
    usermod(world)
    r = run(world, [*ready(), click(type="GtkButton", text="Rename"), settle(100), dump("after"), call("toasts")])
    assert [m["message"] for m in r.warnings()] == [
        f"Could not run {root}{HELPER}: Failed to execute child process “pkexec” (No such file "
        "or directory)"]
    # Nothing in flight: the button stays
    assert rename_sensitive(r.dump("after")) is not False


def test_unreadable_helper_output_checks_again(world, root):
    accounts(world)
    usermod(world)
    pkexec(world, root, script=r"printf '\377\376' >&2")
    r = run(world, [*ready(), click(type="GtkButton", text="Rename"), settle(300),
                    wait_for(type="GtkLabel", text="This username is available."), dump("after"), call("toasts")])
    msgs = [m["message"] for m in r.warnings()]
    assert len(msgs) == 1 and msgs[0].startswith(f"Could not run {root}{HELPER}: ")
    assert len(world.fake_bin.calls("usermod")) == 2
    assert not r.dump("after").has(type="AdwAlertDialog")


def test_pending_rename_of_this_user_is_shown_and_undone(world, root):
    accounts(world)
    request(root, "alice carol\n")
    pkexec(world, root, remove=True)
    r = run(world, [settle(), dump("before"), click(type="GtkButton", text="Undo"),
                    wait_for(type="AdwActionRow", text="Scheduled for the Next Restart", gone=True),
                    dump("after"), call("toasts")])
    assert r.dump("before").one(type="AdwActionRow", text="Scheduled for the Next Restart")[
        "subtitle"] == "alice → carol"
    assert world.fake_bin.calls("pkexec") == [[str(root) + HELPER, "--cancel"]]
    assert not r.value("toasts")


@pytest.mark.parametrize("stderr, toast", [("busy", "busy"), ("", "Undoing failed")])
def test_failed_undo_says_why_and_stays(world, root, stderr, toast):
    accounts(world)
    request(root, "alice carol\n")
    pkexec(world, root, code=1, stderr=stderr)
    r = run(world, [settle(), click(type="GtkButton", text="Undo"),
                    call("wait_toasts"), dump("after"), call("toasts")])
    s = r.dump("after")
    assert r.value("wait_toasts") == [toast]
    assert s.has(type="AdwActionRow", text="Scheduled for the Next Restart")


def test_undo_dismissed_is_silent(world, root):
    accounts(world)
    request(root, "alice carol\n")
    pkexec(world, root, code=126)
    r = run(world, [settle(), click(type="GtkButton", text="Undo"), settle(300), dump("after"), call("toasts")])
    assert not r.value("toasts")
    assert r.dump("after").has(type="AdwActionRow", text="Scheduled for the Next Restart")


def test_undo_without_pkexec_only_warns(world, root):
    accounts(world)
    request(root, "alice carol\n")
    r = run(world, [settle(), click(type="GtkButton", text="Undo"), settle(100), dump("after"), call("toasts")])
    assert [m["message"] for m in r.warnings()] == [
        f"Could not run {root}{HELPER}: Failed to execute child process “pkexec” (No such file "
        "or directory)"]
    assert r.dump("after").has(type="AdwActionRow", text="Scheduled for the Next Restart")


def test_undo_with_unreadable_output_only_warns(world, root):
    accounts(world)
    request(root, "alice carol\n")
    pkexec(world, root, remove=True, script=r"printf '\377' >&2")
    r = run(world, [settle(), click(type="GtkButton", text="Undo"), settle(300), dump("after"), call("toasts")])
    msgs = [m["message"] for m in r.warnings()]
    assert len(msgs) == 1 and msgs[0].startswith(f"Could not run {root}{HELPER}: ")
    # The request is gone, but the dialog was not told: it still shows it
    assert r.dump("after").has(type="AdwActionRow", text="Scheduled for the Next Restart")


@pytest.mark.parametrize("text", ["bob carol\n", "alice\n", "alice carol dave\n", ""])
def test_other_or_broken_requests_are_not_shown(world, root, text):
    accounts(world)
    request(root, text)
    r = run(world, [settle(), dump("dialog"), call("pending")])
    assert not r.dump("dialog").has(type="AdwActionRow", text="Scheduled for the Next Restart")
    assert r.value("pending") is None


def test_pending_name_for_the_users_page(world, root):
    accounts(world)
    request(root, "  alice\tcarol  \n")
    r = run(world, [call("pending")], dialog=False)
    assert r.value("pending") == "carol"


def test_no_request_no_pending_name(world, root):
    accounts(world)
    r = run(world, [call("pending")], dialog=False)
    assert r.value("pending") is None


@pytest.mark.parametrize("kw, helper, expected", [
    ({}, True, True),
    ({"uid": 4242}, True, False),
    ({"local": False}, True, False),
    ({"homed": True}, True, False),
    ({}, False, False),
])
def test_available_only_for_ones_own_local_account(world, root, kw, helper, expected):
    accounts(world, **kw)
    if not helper:
        (root / HELPER.lstrip("/")).chmod(0o644)
    r = run(world, [call("available")], dialog=False)
    assert r.value("available") is expected


def test_cancel_closes_the_dialog(world, root):
    accounts(world)
    r = run(world, [settle(), click(type="GtkButton", text="Cancel"), settle(), dump("after"), call("toasts")])
    assert not r.dump("after").has(type="CcUsernameDialog")


def test_closing_while_checking_and_renaming(world, root):
    accounts(world)
    usermod(world, 6, sleep=0.3)
    pkexec(world, root, write="alice bob", script="sleep 0.3")
    r = run(world, [*ready(), click(type="GtkButton", text="Rename"), call("close"),
                    wait(600), dump("after"), call("toasts")])
    # The helper's answer still arrives (the dialog is kept until then) and offers the restart
    assert world.fake_bin.calls("pkexec") == [[str(root) + HELPER, "alice", "bob"]]
    assert not r.dump("after").has(type="CcUsernameDialog")


def test_closing_while_the_name_is_checked(world, root):
    accounts(world)
    usermod(world, 6, sleep=0.3)
    r = run(world, [settle(), set_text("bob", type="AdwEntryRow"), wait(100), call("close"),
                    wait(600), dump("after"), call("toasts")])
    assert not r.dump("after").has(type="CcUsernameDialog")
    assert not r.warnings()
