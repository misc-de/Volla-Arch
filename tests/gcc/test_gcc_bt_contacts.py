# "Contacts" on the Bluetooth panel and on the Privacy page (cc-plinius-bt-contacts.c): the row
# shows only with bluez-obex-plinius' plinius-pbap-ask (in the program's root), its page lists the
# devices of $XDG_DATA_HOME/plinius/bluetooth-contacts named after BlueZ' devices (a fake
# org.bluez ObjectManager on the private system bus), and each switch writes its single entry
# into the file as it is at that moment.

import re
import stat

import pytest

from gcc_harness import (build_panel, click, dump, gcc_session, gcc_world, set_active,  # noqa: F401
                         settle, wait_for)

PBAP_ASK = "/usr/lib/bluetooth/plinius-pbap-ask"
BLUEZ = "org.bluez"
OM = "org.freedesktop.DBus.ObjectManager"
OM_XML = f"""<node><interface name="{OM}">
 <method name="GetManagedObjects"><arg type="a{{oa{{sa{{sv}}}}}}" name="objects" direction="out"/></method>
</interface></node>"""
DESCRIPTION = ("Devices switched on here may read your contacts over Bluetooth, a car for example "
               "to show who is calling. A device that is not listed asks first. Changes apply "
               "when the device connects again.")
EMPTY = "No device has asked for the contacts yet"

GLUE = r'''
#include "gcc_driver.h"
#include "cc-plinius-bt-contacts.h"

void
gcc_test_setup (GccTest *t)
{
    /* A page that can go on top of the contacts page (as a dialog page would) */
    GtkWidget *label = gtk_label_new ("Above");

    cc_panel_add_subpage (t->panel, "above", adw_navigation_page_new_with_tag (label, "Above", "above"));
    /* The Privacy page puts the row into a group of its own; the Bluetooth panel below its header
     * bar */
    if (!g_strcmp0 (gcc_test_param (t, "where"), "privacy")) {
        GtkWidget *row = cc_plinius_bt_contacts_row_new (t->panel, TRUE);

        gcc_test_add (t, row);
    } else {
        GtkWidget *toolbar = gtk_widget_get_ancestor (GTK_WIDGET (t->page), ADW_TYPE_TOOLBAR_VIEW);

        cc_plinius_bt_contacts_add (t->panel, ADW_TOOLBAR_VIEW (toolbar));
    }
}
'''


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    return build_panel(gcc_session, tmp_path_factory.mktemp("bt-contacts"), "bt_contacts",
                       ["cc-plinius-bt-contacts.c"], GLUE,
                       replacements={"cc-plinius-bt-contacts.c": [
                           (f'"{PBAP_ASK}"', f'"{{root}}{PBAP_ASK}"')]})


@pytest.fixture
def obex(program):
    """bluez-obex-plinius installed: its plinius-pbap-ask in the program's root."""
    f = program.root / PBAP_ASK.lstrip("/")
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("#!/bin/sh\n")
    f.chmod(0o755)
    yield f
    f.unlink()


def store(world):
    return world.home / ".local/share/plinius/bluetooth-contacts"


def write_store(world, text):
    f = store(world)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    return f


def bluez(world, devices=(), reply=None):
    """BlueZ' objects: devices [(address, alias)] (alias None: no Alias property)."""
    if reply is None:
        objects = []
        for i, (address, alias) in enumerate(devices):
            props = [f"'Address': <{address!r}>"]
            if alias is not None:
                props.append(f"'Alias': <{alias!r}>")
            objects.append(f"objectpath '/org/bluez/hci0/dev_{i}': "
                           f"{{'org.bluez.Device1': {{{', '.join(props)}}}}}")
        objects.append("objectpath '/org/bluez/hci0': {'org.bluez.Adapter1': {'Powered': <true>}}")
        reply = f"({{{', '.join(objects)}}},)"
    world.service("system", BLUEZ, "/", OM_XML, replies={"GetManagedObjects": reply})


def device_rows(screen):
    group = screen.one(type="AdwPreferencesGroup", text="Devices")
    return [(r["text"], r.get("subtitle"), r.get("active"))
            for r in group.walk() if r.get("type") in ("AdwSwitchRow", "AdwActionRow")]


OPEN = [click(type="AdwActionRow", text="Contacts"), wait_for(type="AdwPreferencesGroup",
                                                               text="Devices"), settle()]


def test_row_below_the_header_bar_opens_the_page(program, gcc_world, obex):
    write_store(gcc_world, "[devices]\nAA:BB:CC:DD:EE:01=true\n11:22:33:44:55:66=false\n")
    bluez(gcc_world, [("AA:BB:CC:DD:EE:01", "Car Kit"), ("11:22:33:44:55:66", "Headset"),
                      ("99:99:99:99:99:99", "Unrelated")])
    r = gcc_world.run(program, [dump("panel"), *OPEN, dump("page")])
    panel = r.dump("panel")
    row = panel.one(type="AdwActionRow", text="Contacts")
    assert row["subtitle"] == "Which devices may read them"
    assert any(c.get("icon") == "x-office-address-book-symbolic" for c in row.walk())
    # Not in a group of the panel's page: a list of its own below the header bar
    assert not panel.has(type="AdwPreferencesGroup")
    page = r.dump("page")
    assert page.has(type="AdwNavigationPage", text="Contacts")
    group = page.one(type="AdwPreferencesGroup", text="Devices")
    assert group["subtitle"] == DESCRIPTION
    # Sorted by name; the address as the subtitle; devices only BlueZ knows are not listed
    assert device_rows(page) == [("Car Kit", "AA:BB:CC:DD:EE:01", True),
                                 ("Headset", "11:22:33:44:55:66", False)]
    assert [c["path"] for c in gcc_world.calls(BLUEZ, "GetManagedObjects")] == ["/"]
    assert not r.warnings()


def test_privacy_row_says_bluetooth(program, gcc_world, obex):
    bluez(gcc_world)
    r = gcc_world.run(program, [dump("panel"), *OPEN, dump("page")], params={"where": "privacy"})
    row = r.dump("panel").one(type="AdwActionRow", text="Contacts")
    assert row["subtitle"] == "Which Bluetooth devices may read them"
    # No file yet: nobody has asked
    page = r.dump("page")
    rows = device_rows(page)
    assert rows == [(EMPTY, None, None)]
    empty = page.one(type="AdwActionRow", text=EMPTY)
    assert "dim-label" in empty.get("classes", [])
    assert not store(gcc_world).exists()


@pytest.mark.parametrize("where", [None, "privacy"])
def test_without_bluez_obex_plinius_nothing_shows(program, gcc_world, where):
    bluez(gcc_world)
    params = {"where": where} if where else {}
    r = gcc_world.run(program, [dump("panel")], params=params)
    assert not r.dump("panel").has(type="AdwActionRow", text="Contacts")
    assert not gcc_world.calls(BLUEZ)


def test_a_pbap_ask_that_is_not_executable_counts_as_missing(program, gcc_world, obex):
    obex.chmod(0o644)
    r = gcc_world.run(program, [dump("panel")])
    assert not r.dump("panel").has(type="AdwActionRow", text="Contacts")


def test_entries_obexd_would_not_take_are_left_out(program, gcc_world, obex):
    write_store(gcc_world, "# decisions\n[devices]\n"
                           "aa:bb:cc:dd:ee:ff=true\n"       # lower case: obexd compares upper case
                           "AA:BB:CC:DD:EE=true\n"          # too short
                           "AA-BB-CC-DD-EE-FF=true\n"       # other separators
                           "AA:BB:CC:DD:EE:FG=true\n"       # no hex digit
                           "AA:BB:CC:DD:EE:F0=maybe\n"      # no boolean
                           "AA:BB:CC:DD:EE:F1=false\n"
                           "[other]\nAA:BB:CC:DD:EE:F2=true\n")
    bluez(gcc_world, [("AA:BB:CC:DD:EE:F1", "Watch")])
    r = gcc_world.run(program, [*OPEN, dump("page")])
    assert device_rows(r.dump("page")) == [("Watch", "AA:BB:CC:DD:EE:F1", False)]


def test_names_from_bluez_or_the_address(program, gcc_world, obex):
    write_store(gcc_world, "[devices]\nAA:00:00:00:00:01=true\nAA:00:00:00:00:02=true\n"
                           "AA:00:00:00:00:03=false\nAA:00:00:00:00:04=true\n")
    # An empty alias and a device without alias give the address, the first of two objects
    # with the same address names it; markup in a name is shown as it is
    bluez(gcc_world, [("AA:00:00:00:00:01", ""), ("AA:00:00:00:00:02", None),
                      ("AA:00:00:00:00:03", "<b>Bold</b> & Co"),
                      ("AA:00:00:00:00:04", "Zebra"), ("AA:00:00:00:00:04", "Again")])
    r = gcc_world.run(program, [*OPEN, dump("page")])
    assert device_rows(r.dump("page")) == [
        ("<b>Bold</b> & Co", "AA:00:00:00:00:03", False),
        ("AA:00:00:00:00:01", None, True),
        ("AA:00:00:00:00:02", None, True),
        ("Zebra", "AA:00:00:00:00:04", True),
    ]
    assert not r.warnings()


def test_without_bluez_the_addresses_show(program, gcc_world, obex):
    write_store(gcc_world, "[devices]\nAA:BB:CC:DD:EE:01=true\n")
    r = gcc_world.run(program, [*OPEN, dump("page")])
    assert device_rows(r.dump("page")) == [("AA:BB:CC:DD:EE:01", None, True)]


def test_bluez_with_an_error_shows_the_addresses(program, gcc_world, obex):
    write_store(gcc_world, "[devices]\nAA:BB:CC:DD:EE:01=false\n")
    bluez(gcc_world, reply={"error": "org.freedesktop.DBus.Error.AccessDenied", "message": "no"})
    r = gcc_world.run(program, [*OPEN, dump("page")])
    assert device_rows(r.dump("page")) == [("AA:BB:CC:DD:EE:01", None, False)]
    assert len(gcc_world.calls(BLUEZ, "GetManagedObjects")) == 1


def test_without_a_system_bus_the_file_shows(program, gcc_world, obex):
    write_store(gcc_world, "[devices]\nAA:BB:CC:DD:EE:01=true\n")
    r = gcc_world.run(program, [*OPEN, dump("page")], system_bus=False)
    assert device_rows(r.dump("page")) == [("AA:BB:CC:DD:EE:01", None, True)]


def test_a_switch_writes_its_entry_into_the_file_as_it_is_now(program, gcc_world, obex):
    f = write_store(gcc_world, "# kept\n[devices]\nAA:BB:CC:DD:EE:01=false\n")
    bluez(gcc_world, [("AA:BB:CC:DD:EE:01", "Car Kit")])
    answered = "# kept\n[devices]\nAA:BB:CC:DD:EE:01=false\n11:22:33:44:55:66=true\n"
    r = gcc_world.run(program, [
        *OPEN,
        # plinius-pbap-ask answers a notification meanwhile
        {"op": "write_file", "path": str(f), "text": answered},
        set_active(True, type="AdwSwitchRow", text="Car Kit"), settle(), dump("page")])
    text = f.read_text()
    assert "# kept" in text
    lines = set(text.splitlines())
    assert {"AA:BB:CC:DD:EE:01=true", "11:22:33:44:55:66=true"} <= lines
    assert device_rows(r.dump("page")) == [("Car Kit", "AA:BB:CC:DD:EE:01", True)]
    assert not r.warnings()


def test_a_file_gone_meanwhile_is_made_again_for_the_user_only(program, gcc_world, obex):
    f = write_store(gcc_world, "[devices]\nAA:BB:CC:DD:EE:01=true\n11:22:33:44:55:66=true\n")
    bluez(gcc_world)
    r = gcc_world.run(program, [
        *OPEN, {"op": "remove_file", "path": str(f)},
        set_active(False, type="AdwSwitchRow", text="11:22:33:44:55:66"), settle(),
        dump("page")])
    # Only the changed entry: the other one went with the file
    assert f.read_text() == "[devices]\n11:22:33:44:55:66=false\n"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert not r.warnings()


def test_page_shown_again_reads_the_file_again(program, gcc_world, obex):
    f = write_store(gcc_world, "[devices]\nAA:BB:CC:DD:EE:01=false\n")
    bluez(gcc_world, [("AA:BB:CC:DD:EE:01", "Car Kit"), ("11:22:33:44:55:66", "Watch")])
    r = gcc_world.run(program, [
        *OPEN, dump("first"),
        {"op": "back"}, settle(),
        {"op": "write_file", "path": str(f),
         "text": "[devices]\nAA:BB:CC:DD:EE:01=true\n11:22:33:44:55:66=false\n"},
        *OPEN, dump("second")])
    assert device_rows(r.dump("first")) == [("Car Kit", "AA:BB:CC:DD:EE:01", False)]
    assert device_rows(r.dump("second")) == [("Car Kit", "AA:BB:CC:DD:EE:01", True),
                                             ("Watch", "11:22:33:44:55:66", False)]
    assert len(gcc_world.calls(BLUEZ, "GetManagedObjects")) == 2


def test_coming_back_to_the_page_shows_answers_given_meanwhile(program, gcc_world, obex):
    f = write_store(gcc_world, "[devices]\nAA:BB:CC:DD:EE:01=false\n")
    bluez(gcc_world, [("AA:BB:CC:DD:EE:01", "Car Kit"), ("11:22:33:44:55:66", "Watch")])
    r = gcc_world.run(program, [
        *OPEN, dump("before"),
        {"op": "push", "tag": "above"}, settle(),
        {"op": "write_file", "path": str(f),
         "text": "[devices]\nAA:BB:CC:DD:EE:01=false\n11:22:33:44:55:66=true\n"},
        {"op": "back"}, wait_for(type="AdwSwitchRow", text="Watch"), dump("after")])
    assert device_rows(r.dump("before")) == [("Car Kit", "AA:BB:CC:DD:EE:01", False)]
    # The old rows went, each device once
    assert device_rows(r.dump("after")) == [("Car Kit", "AA:BB:CC:DD:EE:01", False),
                                            ("Watch", "11:22:33:44:55:66", True)]
    assert len(gcc_world.calls(BLUEZ, "GetManagedObjects")) == 2


# A change that cannot be kept: the switch goes back to what the file still says, once (it is set
# back after GTK is done with the user's change; inside its own notify::active handler AdwSwitchRow
# notified again and again and GNOME Settings hung at 100 % CPU)


def run_failing_change(world, program, steps):
    """The program's stderr; the program must end by itself."""
    return world.run(program, [*steps, settle()], timeout=10).stderr


def first_warning(stderr):
    lines = [line for line in stderr.splitlines() if "cc-plinius-bt-contacts-WARNING" in line]
    assert lines, stderr[-2000:]
    # "(process:N): cc-plinius-bt-contacts-WARNING **: HH:MM:SS.mmm: message"
    return re.sub(r"^.*?-WARNING \*\*: [0-9:.]+: ", "", lines[0])


SWITCH_OFF = set_active(False, type="AdwSwitchRow", text="AA:BB:CC:DD:EE:01")


def test_a_file_that_does_not_parse_is_not_overwritten(program, gcc_world, obex):
    f = write_store(gcc_world, "[devices]\nAA:BB:CC:DD:EE:01=true\n")
    bluez(gcc_world)
    broken = "[devices\nAA:BB:CC:DD:EE:01=false\n"
    stderr = run_failing_change(gcc_world, program, [
        *OPEN, {"op": "write_file", "path": str(f), "text": broken}, SWITCH_OFF])
    # It may hold decisions
    assert f.read_text() == broken
    assert first_warning(stderr).startswith(
        "Could not keep the contacts permission of AA:BB:CC:DD:EE:01: Key file contains line")


def test_a_directory_that_cannot_be_made_is_said(program, gcc_world, obex):
    # The data directory is a link to a link to the real one; the middle link goes while the
    # page shows: the file is gone and its directory cannot be made
    real = gcc_world.tmp / "real-data"
    middle = gcc_world.tmp / "middle"
    data = gcc_world.tmp / "data"
    (real / "plinius").mkdir(parents=True)
    (real / "plinius/bluetooth-contacts").write_text("[devices]\nAA:BB:CC:DD:EE:01=true\n")
    middle.symlink_to(real)
    data.symlink_to(middle)
    gcc_world.extra_env["XDG_DATA_HOME"] = str(data)
    bluez(gcc_world)
    stderr = run_failing_change(gcc_world, program, [
        *OPEN, {"op": "remove_file", "path": str(middle)}, SWITCH_OFF])
    assert first_warning(stderr) == (
        f"Could not keep the contacts permission of AA:BB:CC:DD:EE:01: {data}/plinius: "
        "No such file or directory")
    assert (real / "plinius/bluetooth-contacts").read_text() == "[devices]\nAA:BB:CC:DD:EE:01=true\n"


def test_a_file_that_cannot_be_written_is_said(program, gcc_world, obex):
    f = write_store(gcc_world, "[devices]\nAA:BB:CC:DD:EE:01=true\n")
    bluez(gcc_world)
    f.parent.chmod(0o500)
    try:
        stderr = run_failing_change(gcc_world, program, [*OPEN, SWITCH_OFF])
    finally:
        f.parent.chmod(0o700)
    assert f.read_text() == "[devices]\nAA:BB:CC:DD:EE:01=true\n"
    warning = first_warning(stderr)
    assert warning.startswith("Could not keep the contacts permission of AA:BB:CC:DD:EE:01: ")
    assert warning.endswith("Permission denied")


def test_a_change_that_cannot_be_kept_sets_the_switch_back_once(program, gcc_world, obex):
    f = write_store(gcc_world, "[devices]\nAA:BB:CC:DD:EE:01=true\n")
    bluez(gcc_world)
    f.parent.chmod(0o500)
    try:
        r = gcc_world.run(program, [*OPEN, SWITCH_OFF, settle(), dump("page")], timeout=10)
    finally:
        f.parent.chmod(0o700)
    assert device_rows(r.dump("page")) == [("AA:BB:CC:DD:EE:01", None, True)]
    assert len(r.warnings()) == 1
    assert f.read_text() == "[devices]\nAA:BB:CC:DD:EE:01=true\n"


def test_after_a_change_that_could_not_be_kept_the_next_one_is_kept(program, gcc_world, obex):
    # The switch was set back without a write; once the file reads again a change is kept
    f = write_store(gcc_world, "[devices]\nAA:BB:CC:DD:EE:01=true\n")
    bluez(gcc_world)
    good = "[devices]\nAA:BB:CC:DD:EE:01=true\n"
    r = gcc_world.run(program, [*OPEN, {"op": "write_file", "path": str(f), "text": "[devices\n"},
                                SWITCH_OFF, settle(), dump("back"),
                                {"op": "write_file", "path": str(f), "text": good},
                                SWITCH_OFF, settle(), dump("kept")], timeout=10)
    assert device_rows(r.dump("back")) == [("AA:BB:CC:DD:EE:01", None, True)]
    assert device_rows(r.dump("kept")) == [("AA:BB:CC:DD:EE:01", None, False)]
    assert "AA:BB:CC:DD:EE:01=false" in f.read_text()
    assert len(r.warnings()) == 1
