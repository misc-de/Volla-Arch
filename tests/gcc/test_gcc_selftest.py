# Self-Test in System (cc-plinius-selftest.c): what plinius-health found after the start
# (/run/plinius-health/result, in the program's root in tmp), the row on the System page that leads
# there, Check Again (pkexec plinius-health check), Copy, and after a failed check what the last
# update changed with Take Back Update (plinius-rollback summary, pkexec plinius-rollback run).
#
# The glue stands in for the System panel: a hidden "Self-Test" row with the action
# navigation.push, set up only when the page was added (as cc-system-panel.c does). The
# template's row has the action but no target until the file sets one: GTK says so once
# (TEMPLATE_WARNING), the same in GNOME Settings, it is not this file's.

import pytest

from gcc_harness import (Screen, back, build_panel, call, click, dump, gcc_session, gcc_world,  # noqa: F401
                         settle, wait_for)

GLUE = r'''
#include "gcc_driver.h"
#include "cc-list-row.h"
#include "cc-plinius-selftest.h"

static gboolean added;

void
gcc_test_setup (GccTest *t)
{
    GtkWidget *group = adw_preferences_group_new ();
    /* As in the System panel's template: hidden, the action set, the target ours */
    GtkWidget *row = g_object_new (CC_TYPE_LIST_ROW, "title", "Unset Row", "show-arrow", TRUE,
                                   "action-name", "navigation.push", "visible", FALSE, NULL);

    adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), row);
    adw_preferences_page_add (t->page, ADW_PREFERENCES_GROUP (group));

    added = cc_plinius_selftest_add_page (t->panel);
    if (added)
        cc_plinius_selftest_row_setup (ADW_ACTION_ROW (row));
}

gboolean
gcc_test_call (GccTest *t, const char *name, JsonArray *args, JsonBuilder *result)
{
    if (g_str_equal (name, "added")) {
        json_builder_add_boolean_value (result, added);
        return TRUE;
    }
    if (g_str_equal (name, "clipboard")) {
        GdkContentProvider *content = gdk_clipboard_get_content (gtk_widget_get_clipboard (GTK_WIDGET (t->window)));
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

TEMPLATE_WARNING = r"^actionhelper: action navigation.push can't be activated due to parameter type"

RUN = "/run/plinius-health"
HEALTH = "/usr/bin/plinius-health"
ROLLBACK = "/usr/bin/plinius-rollback"
TAG = "plinius-self-test"

ALL_OK = """\
ok units: no failed system units
ok user_units: no failed user units
ok container: Android container running
ok display: phoc and phosh running, phoc on the GPU
ok audio: PipeWire has the speaker of the audio HAL
ok modem: 2 modem(s) in ModemManager
ok wifi: Wi-Fi interface wlan0 present
ok bluetooth: Bluetooth adapter present
"""

# As on the phone on 2026-11-20: netlogd did not start
NETLOG_FAILED = ALL_OK.replace("ok units: no failed system units",
                               "fail units: failed units: plinius-netlogd.service")

SUMMARY = "when=2026-11-19T22:05:00+0100\npackages=12\nmissing=0\n"


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    reps = [(f'"{p}"', f'"{{root}}{p}"') for p in (RUN, HEALTH, ROLLBACK)]
    return build_panel(gcc_session, tmp_path_factory.mktemp("selftest"), "selftest",
                       ["cc-plinius-selftest.c"], GLUE,
                       replacements={"cc-plinius-selftest.c": reps})


class System:
    def __init__(self, root):
        self.root = root
        import shutil
        for d in list(root.iterdir()):
            shutil.rmtree(d) if d.is_dir() else d.unlink()

    def path(self, p):
        return self.root / p.lstrip("/")

    def write(self, p, text, mode=0o644):
        f = self.path(p)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        f.chmod(mode)
        return f

    def health(self, result=None):
        """plinius-health installed; with a result, the check has run"""
        self.write(HEALTH, "#!/bin/sh\n", 0o755)
        if result is not None:
            self.write(RUN + "/result", result)
            self.write(RUN + "/done", "")

    def rollback(self, out=SUMMARY, code=0):
        """plinius-rollback installed, answering summary with out and code"""
        log = self.path("rollback.log")
        self.write(ROLLBACK, f'#!/bin/sh\necho "$@" >> "{log}"\nprintf "%s" "{out}"\nexit {code}\n', 0o755)
        return log


@pytest.fixture
def system(program):
    return System(program.root)


def open_page():
    return [settle(), click(type="CcListRow", text="Self-Test"), wait_for(type="AdwNavigationPage", tag=TAG),
            settle(200)]


def page_of(screen):
    return screen.within(type="AdwNavigationPage", tag=TAG)


def groups(screen):
    """[(group title, description, [(row title, subtitle)], [button rows])] of the page"""
    out = []
    for g in page_of(screen).find(type="AdwPreferencesGroup"):
        s = Screen(g.get("children", []))
        out.append((g.get("text"), g.get("subtitle"),
                    [(r.get("text"), r.get("subtitle")) for r in s.find(type="AdwActionRow")],
                    [r.get("text") for r in s.find(type="AdwButtonRow")]))
    return out


def checks(screen):
    return {title: subtitle for t, _, rows, _ in groups(screen) if t == "Checks" for title, subtitle in rows}


def status(screen):
    return groups(screen)[0][2][0]


def last_update(screen):
    (g,) = [g for g in page_of(screen).find(type="AdwPreferencesGroup") if g.get("text") == "Last Update"]
    return g


# ----- The row -----

def test_without_plinius_health_no_row_and_no_page(program, gcc_world, system):
    r = gcc_world.run(program, [settle(), call("added"), dump("main")])
    assert r.value("added") is False
    assert not r.dump("main").has(text="Self-Test")
    assert not r.dump("main").has(text="Unset Row")
    assert not r.warnings(ignore=[TEMPLATE_WARNING])


def test_a_health_tool_that_cannot_run_is_no_reason_for_the_row(program, gcc_world, system):
    system.write(HEALTH, "#!/bin/sh\n", 0o644)
    r = gcc_world.run(program, [settle(), call("added")])
    assert r.value("added") is False


def test_row_leads_to_the_page(program, gcc_world, system):
    system.health()
    r = gcc_world.run(program, [settle(), call("added"), dump("main"), *open_page(), dump("page")])
    assert r.value("added") is True
    row = r.dump("main").one(type="CcListRow", text="Self-Test")
    assert row["subtitle"] == "Whether the phone's parts work after the start"
    assert row["action"] == "navigation.push" and row["target"] == f"'{TAG}'"
    assert r.dump("page").one(type="AdwNavigationPage", tag=TAG)["text"] == "Self-Test"


# ----- What the check found -----

def test_not_checked_yet(program, gcc_world, system):
    system.health()
    system.rollback()
    r = gcc_world.run(program, [*open_page(), dump("page")])
    s = r.dump("page")
    assert groups(s) == [
        (None, None, [("Not Checked Yet", "The self-test runs a few minutes after the phone starts.")], []),
        (None, None, [], ["Check Again"]),
    ]
    # Nothing to copy, nothing to take back
    assert not system.path("rollback.log").exists()
    assert not r.warnings()


def test_a_result_without_done_is_a_check_still_running(program, gcc_world, system):
    system.health()
    system.write(RUN + "/result", NETLOG_FAILED)
    r = gcc_world.run(program, [*open_page(), dump("page")])
    assert status(r.dump("page"))[0] == "Not Checked Yet"


def test_everything_works(program, gcc_world, system):
    system.health(ALL_OK)
    system.rollback()
    r = gcc_world.run(program, [*open_page(), dump("page")])
    s = r.dump("page")
    assert status(s) == ("Everything Works", "All checks after the start were fine.")
    assert checks(s) == {
        "System Services": "Working", "Session Services": "Working", "Android Container": "Working",
        "Display": "Working", "Audio": "Working", "Modem": "Working", "Wi-Fi": "Working",
        "Bluetooth": "Working",
    }
    assert groups(s)[2][3] == ["Check Again", "Copy"]
    # Without a failure no update is suspected
    assert not [g for g in groups(s) if g[0] == "Last Update"]
    assert not system.path("rollback.log").exists()
    assert not r.warnings()


def test_failed_services_are_named(program, gcc_world, system):
    system.health(NETLOG_FAILED.replace(
        "ok user_units: no failed user units",
        "fail user_units: failed user units: a.service b.service"))
    r = gcc_world.run(program, [*open_page(), dump("page")])
    s = r.dump("page")
    assert status(s) == ("Problem Found", "2 checks failed.")
    c = checks(s)
    assert c["System Services"] == "Not started: plinius-netlogd.service"
    assert c["Session Services"] == "Not started: a.service, b.service"
    row = page_of(s).one(type="AdwActionRow", text="System Services")
    assert Screen(row.get("children", [])).has(type="GtkImage", icon="dialog-error-symbolic")


@pytest.mark.parametrize("line, title, subtitle", [
    ("fail container: Android container not running (lxc-android-config.service)", "Android Container",
     "The Android container is not running. Without it there is no modem, Bluetooth, camera or sensors."),
    ("fail display: phoc renders without the GPU (no /dev/mali0 mapped)", "Display",
     "The display does not run on the graphics chip, or not at all."),
    ("fail audio: no audio HAL sink in PipeWire (audio-hal-bridge)", "Audio",
     "The speaker and microphone of the phone are missing."),
    ("fail modem: no modem in ModemManager (ofono, ofono2mm)", "Modem",
     "No modem: no calls, messages or mobile data."),
    ("fail wifi: no Wi-Fi interface wlan0 (plinius-wlan, Android container)", "Wi-Fi",
     "The Wi-Fi adapter is missing."),
    ("fail bluetooth: no Bluetooth adapter (bluebinder)", "Bluetooth", "The Bluetooth adapter is missing."),
    # Without a unit list: the check's own words for people
    ("fail user_units: user manager of plinius not reachable", "Session Services",
     "A service of your session did not start."),
    # A check of a newer plinius-health: its name and message as they are
    ("fail thermal: too hot", "thermal", "too hot"),
    ("skip audio: no graphical session", "Audio", "Not checked: nobody was logged in."),
])
def test_each_check_in_words_of_its_own(program, gcc_world, system, line, title, subtitle):
    system.health(line + "\n")
    r = gcc_world.run(program, [*open_page(), dump("page")])
    assert checks(r.dump("page")) == {title: subtitle}


def test_lines_that_are_no_check_are_left_out(program, gcc_world, system):
    system.health("garbage\nok :empty name\nfail nocolon\nok wifi: present\n\n")
    r = gcc_world.run(program, [*open_page(), dump("page")])
    assert checks(r.dump("page")) == {"Wi-Fi": "Working"}
    assert status(r.dump("page"))[0] == "Everything Works"


def test_one_failure_in_the_singular(program, gcc_world, system):
    system.health("fail modem: none\nok wifi: x\n")
    r = gcc_world.run(program, [*open_page(), dump("page")])
    assert status(r.dump("page")) == ("Problem Found", "One check failed.")


def test_copy_puts_the_original_lines_on_the_clipboard(program, gcc_world, system):
    system.health(NETLOG_FAILED)
    r = gcc_world.run(program, [*open_page(), click(type="AdwButtonRow", text="Copy"),
                                wait_for(type="GtkLabel", text="Copied to clipboard"), call("clipboard")])
    assert r.value("clipboard") == NETLOG_FAILED


def test_an_unreadable_result_warns(program, gcc_world, system):
    system.health(ALL_OK)
    system.path(RUN + "/result").unlink()
    system.path(RUN + "/result").mkdir()
    r = gcc_world.run(program, [*open_page(), dump("page")])
    assert status(r.dump("page"))[0] == "Not Checked Yet"
    assert [m["message"] for m in r.warnings()] == [
        f"Could not read {program.root}{RUN}/result: Error reading file “{program.root}{RUN}/result”: Is a directory"]


def test_the_result_is_read_again_on_every_showing(program, gcc_world, system):
    system.health()
    system.path(RUN).mkdir(parents=True)
    result = system.path(RUN + "/result")
    done = system.path(RUN + "/done")
    r = gcc_world.run(program, [
        *open_page(), dump("before"), back(), settle(),
        {"op": "write_file", "path": str(result), "text": NETLOG_FAILED},
        {"op": "write_file", "path": str(done), "text": ""},
        click(type="CcListRow", text="Self-Test"), settle(), dump("after"),
    ])
    assert status(r.dump("before"))[0] == "Not Checked Yet"
    assert status(r.dump("after"))[0] == "Problem Found"


# ----- Check Again -----

def test_check_again_runs_the_check_and_shows_the_new_result(program, gcc_world, system):
    system.health(NETLOG_FAILED)
    result = system.path(RUN + "/result")
    gcc_world.fake_bin.stub("pkexec", f"sleep 0.3; printf '%s' '{ALL_OK}' > '{result}'; exit 0")
    r = gcc_world.run(program, [*open_page(), click(type="AdwButtonRow", text="Check Again"), settle(),
                                dump("running"), settle(800), dump("after")])
    assert gcc_world.fake_bin.calls("pkexec") == [[str(program.root / HEALTH.lstrip("/")), "check"]]
    # While it runs, it cannot be pressed again
    row = page_of(r.dump("running")).one(type="AdwButtonRow", text="Checking…")
    assert row.get("sensitive") is False
    s = r.dump("after")
    assert status(s)[0] == "Everything Works"
    assert page_of(s).one(type="AdwButtonRow", text="Check Again").get("sensitive", True)
    assert not page_of(s).has(type="GtkLabel", contains="could not run")


@pytest.mark.parametrize("code, toast", [
    (2, None),        # a check failed: the check itself ran
    (126, None),      # the question dismissed
    (127, "Not checked: not authorized"),
    (1, "The self-test could not run"),
])
def test_check_again_answers(program, gcc_world, system, code, toast):
    system.health(NETLOG_FAILED)
    gcc_world.fake_bin.stub("pkexec", f"exit {code}")
    r = gcc_world.run(program, [*open_page(), click(type="AdwButtonRow", text="Check Again"), settle(400),
                                dump("after")])
    s = r.dump("after")
    for text in ("Not checked: not authorized", "The self-test could not run"):
        assert s.has(type="GtkLabel", text=text) == (text == toast)


# ----- Take Back Update -----

def test_after_a_failure_the_last_update_can_be_taken_back(program, gcc_world, system):
    system.health(NETLOG_FAILED)
    log = system.rollback()
    r = gcc_world.run(program, [*open_page(), dump("page")])
    g = last_update(r.dump("page"))
    assert g["subtitle"].startswith("The update of ")
    assert g["subtitle"].endswith(" changed 12 packages. Their earlier versions are still on the phone. "
                                  "If the problem began with this update, take it back. Asks for your PIN.")
    # The time as the viewer reads it, not pacman.log's
    assert "2026-11-19T22:05" not in g["subtitle"]
    assert take_back_shown(g)
    assert log.read_text() == "summary\n"


@pytest.mark.parametrize("out, code, text", [
    ("when=2026-11-19T22:05:00+0100\npackages=1\nmissing=0\n", 0, " changed one package. Its earlier version"),
    ("when=2026-11-19T22:05:00+0100\npackages=3\nmissing=2\n", 3,
     " cannot be taken back: the earlier versions of 2 packages are no longer on the phone."),
    ("when=2026-11-19T22:05:00+0100\npackages=3\nmissing=1\n", 3,
     " cannot be taken back: the earlier version of one package is no longer on the phone."),
    ("", 4, "There is no update to take back."),
    ("", 1, "The last update could not be read."),
    # No packages to take back although the summary said fine: nothing to press
    ("when=2026-11-19T22:05:00+0100\npackages=0\nmissing=0\n", 0, "The last update could not be read."),
])
def test_what_the_summary_says(program, gcc_world, system, out, code, text):
    system.health(NETLOG_FAILED)
    system.rollback(out, code)
    r = gcc_world.run(program, [*open_page(), dump("page")])
    g = last_update(r.dump("page"))
    assert text in g["subtitle"]
    assert take_back_shown(g) == (code == 0 and "packages=0" not in out)


def test_without_plinius_rollback_no_group(program, gcc_world, system):
    system.health(NETLOG_FAILED)
    r = gcc_world.run(program, [*open_page(), dump("page")])
    assert not [g for g in groups(r.dump("page")) if g[0] == "Last Update"]


def take_back_shown(group):
    # A hidden widget is not in the dump
    return Screen(group.get("children", [])).has(type="GtkButton", text="Take Back Update")


def take_back(more=()):
    return [*open_page(), click(type="GtkButton", text="Take Back Update"), wait_for(type="AdwAlertDialog"),
            dump("asked"), *more]


def test_take_back_asks_first(program, gcc_world, system):
    system.health(NETLOG_FAILED)
    system.rollback()
    gcc_world.fake_bin.stub("pkexec", "exit 0")
    r = gcc_world.run(program, take_back([click(type="GtkButton", text="Cancel"), settle(), dump("after")]))
    alert = r.dump("asked").one(type="AdwAlertDialog")
    assert alert["text"] == "Take Back the Last Update?"
    assert alert["subtitle"].startswith("The update of ")
    assert alert["subtitle"].endswith(" goes back: 12 packages return to their earlier versions. "
                                      "Restart the phone afterwards.")
    assert gcc_world.fake_bin.calls("pkexec") == []
    assert not r.dump("after").has(type="AdwAlertDialog")


def test_take_back_runs_the_rollback_as_a_unit(program, gcc_world, system):
    system.health(NETLOG_FAILED)
    system.rollback()
    gcc_world.fake_bin.stub("pkexec", "sleep 0.4; exit 0")
    r = gcc_world.run(program, take_back([click(type="GtkButton", text="Take Back"), settle(),
                                          dump("running"), settle(900), dump("after")]))
    assert gcc_world.fake_bin.calls("pkexec") == [[
        str(program.root / ROLLBACK.lstrip("/")), "run", "--noconfirm", "--unit"]]
    g = last_update(r.dump("running"))
    assert g["subtitle"] == "Taking back the update… Do not switch off the phone."
    assert not take_back_shown(g)
    g = last_update(r.dump("after"))
    assert g["subtitle"] == "The update was taken back. Restart the phone to load the earlier versions."
    assert not take_back_shown(g)
    assert not r.warnings()


@pytest.mark.parametrize("code, text", [
    (127, "Not taken back: not authorized."),
    (3, "Not taken back: the rollback stopped with status 3."),
])
def test_take_back_refused(program, gcc_world, system, code, text):
    system.health(NETLOG_FAILED)
    system.rollback()
    gcc_world.fake_bin.stub("pkexec", f"exit {code}")
    r = gcc_world.run(program, take_back([click(type="GtkButton", text="Take Back"), settle(400), dump("after")]))
    assert last_update(r.dump("after"))["subtitle"] == text


def test_take_back_dismissed_asks_the_summary_again(program, gcc_world, system):
    system.health(NETLOG_FAILED)
    log = system.rollback()
    gcc_world.fake_bin.stub("pkexec", "exit 126")
    r = gcc_world.run(program, take_back([click(type="GtkButton", text="Take Back"), settle(400), dump("after")]))
    g = last_update(r.dump("after"))
    assert g["subtitle"].endswith("Asks for your PIN.")
    assert take_back_shown(g)
    assert log.read_text() == "summary\nsummary\n"


def test_the_outcome_stays_after_a_check_that_finds_nothing(program, gcc_world, system):
    # After the rollback the user checks again before restarting: fine now, the outcome still says
    # to restart
    system.health(NETLOG_FAILED)
    system.rollback()
    result = system.path(RUN + "/result")
    gcc_world.fake_bin.stub("pkexec", f'case "$2" in check) printf "%s" "{ALL_OK}" > "{result}";; esac; exit 0')
    r = gcc_world.run(program, take_back([click(type="GtkButton", text="Take Back"), settle(400),
                                          click(type="AdwButtonRow", text="Check Again"), settle(400),
                                          dump("after")]))
    s = r.dump("after")
    assert status(s)[0] == "Everything Works"
    assert last_update(s)["subtitle"] == "The update was taken back. Restart the phone to load the earlier versions."


def test_leaving_while_the_summary_runs(program, gcc_world, system):
    system.health(NETLOG_FAILED)
    system.rollback(out=SUMMARY)
    # A slow summary: the page is left and shown again before it answers
    system.write(ROLLBACK, f'#!/bin/sh\nsleep 0.5\nprintf "%s" "{SUMMARY}"\n', 0o755)
    r = gcc_world.run(program, [*open_page(), back(), settle(), click(type="CcListRow", text="Self-Test"),
                                settle(1200), dump("after")])
    g = last_update(r.dump("after"))
    assert g["subtitle"].endswith("Asks for your PIN.")
    assert not r.warnings()


# ----- What cannot run, and closing while something runs -----

NO_INTERPRETER = "#!/nonexistent/interpreter\n"


def unrunnable_pkexec(gcc_world):
    """A pkexec in PATH that cannot be started (its interpreter is missing)"""
    f = gcc_world.fake_bin.path / "pkexec"
    f.write_text(NO_INTERPRETER)
    f.chmod(0o755)


def test_check_again_when_pkexec_cannot_start(program, gcc_world, system):
    system.health(NETLOG_FAILED)
    unrunnable_pkexec(gcc_world)
    r = gcc_world.run(program, [*open_page(), click(type="AdwButtonRow", text="Check Again"), settle(300),
                                dump("after")])
    s = r.dump("after")
    assert s.has(type="GtkLabel", text="The self-test could not run")
    # Not stuck in "Checking…": it can be pressed again
    assert page_of(s).one(type="AdwButtonRow", text="Check Again").get("sensitive", True)
    (w,) = r.warnings(ignore=[TEMPLATE_WARNING])
    assert w["message"].startswith(f"Could not run pkexec {program.root}{HEALTH} check: ")


def test_check_again_ended_by_a_signal(program, gcc_world, system):
    system.health(NETLOG_FAILED)
    gcc_world.fake_bin.stub("pkexec", "kill -KILL $$")
    r = gcc_world.run(program, [*open_page(), click(type="AdwButtonRow", text="Check Again"), settle(400),
                                dump("after")])
    assert r.dump("after").has(type="GtkLabel", text="The self-test could not run")


def test_closing_while_the_check_runs(program, gcc_world, system):
    # Settings is closed while pkexec still runs: the wait is cancelled with the page, nothing
    # touches the freed page afterwards (the window is destroyed at the end of the run)
    system.health(NETLOG_FAILED)
    gcc_world.fake_bin.stub("pkexec", "sleep 1")
    r = gcc_world.run(program, [*open_page(), click(type="AdwButtonRow", text="Check Again"), settle(100),
                                dump("running")])
    assert page_of(r.dump("running")).one(type="AdwButtonRow", text="Checking…").get("sensitive") is False
    assert not r.warnings(ignore=[TEMPLATE_WARNING])


def test_take_back_when_pkexec_cannot_start(program, gcc_world, system):
    system.health(NETLOG_FAILED)
    system.rollback()
    unrunnable_pkexec(gcc_world)
    r = gcc_world.run(program, take_back([click(type="GtkButton", text="Take Back"), settle(300), dump("after")]))
    g = last_update(r.dump("after"))
    assert g["subtitle"].startswith("Not taken back: ")
    assert not take_back_shown(g)
    (w,) = r.warnings(ignore=[TEMPLATE_WARNING])
    assert w["message"].startswith(f"Could not run pkexec {program.root}{ROLLBACK} run: ")


def test_closing_while_the_rollback_runs(program, gcc_world, system):
    system.health(NETLOG_FAILED)
    system.rollback()
    gcc_world.fake_bin.stub("pkexec", "sleep 1")
    r = gcc_world.run(program, take_back([click(type="GtkButton", text="Take Back"), settle(100),
                                          dump("running")]))
    assert last_update(r.dump("running"))["subtitle"] == "Taking back the update… Do not switch off the phone."
    assert not r.warnings(ignore=[TEMPLATE_WARNING])


def test_closing_while_the_summary_runs(program, gcc_world, system):
    system.health(NETLOG_FAILED)
    system.write(ROLLBACK, f'#!/bin/sh\nsleep 1\nprintf "%s" "{SUMMARY}"\n', 0o755)
    r = gcc_world.run(program, [*open_page(), dump("page")])
    assert last_update(r.dump("page"))["subtitle"] == "Looking for the last update…"
    assert not r.warnings(ignore=[TEMPLATE_WARNING])


def test_a_rollback_tool_that_cannot_start(program, gcc_world, system):
    system.health(NETLOG_FAILED)
    system.write(ROLLBACK, NO_INTERPRETER, 0o755)
    r = gcc_world.run(program, [*open_page(), dump("page")])
    g = last_update(r.dump("page"))
    assert g["subtitle"] == "The last update could not be read."
    assert not take_back_shown(g)
    (w,) = r.warnings(ignore=[TEMPLATE_WARNING])
    assert w["message"].startswith(f"Could not run {program.root}{ROLLBACK} summary: ")
