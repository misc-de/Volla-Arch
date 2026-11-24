# "Contribute to beaconDB" on the Location page (cc-plinius-beacondb.c): the switch shows the marker
# file of furios-gps-contribute (~/.config/furios-gps/contribute) and runs "furios-gps-contribute
# on|off", a fake in fake_bin that keeps the marker as the real tool does; the row below shows
# "status" (submitted=, queued=); the group follows the location switch.

import pytest

from gcc_harness import (build_panel, call, dump, gcc_session, gcc_world, set_active,  # noqa: F401
                         settle, wait_for)

GLUE = r'''
#include "gcc_driver.h"
#include "cc-plinius-beacondb.h"

static gboolean set_up;

void
gcc_test_setup (GccTest *t)
{
    GtkWidget *location = adw_preferences_group_new ();
    GtkWidget *apps = adw_preferences_group_new ();
    GtkWidget *row = adw_switch_row_new ();

    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), "Automatic Device Location");
    adw_switch_row_set_active (ADW_SWITCH_ROW (row), g_strcmp0 (gcc_test_param (t, "location"), "off") != 0);
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (location), row);
    adw_preferences_group_set_title (ADW_PREFERENCES_GROUP (apps), "Apps");
    adw_preferences_page_add (t->page, ADW_PREFERENCES_GROUP (location));
    adw_preferences_page_add (t->page, ADW_PREFERENCES_GROUP (apps));
    set_up = cc_plinius_beacondb_setup (t->page, ADW_SWITCH_ROW (row));
}

gboolean
gcc_test_call (GccTest *t, const char *name, JsonArray *args, JsonBuilder *result)
{
    if (!g_str_equal (name, "set_up"))
        return FALSE;
    json_builder_add_boolean_value (result, set_up);
    return TRUE;
}
'''

SUBTITLE = ("Sends the addresses and signal strengths of nearby Wi-Fi networks with the GPS position to the "
            "open beaconDB project, only over Wi-Fi. Never network names, hidden networks or networks whose "
            "name ends in _nomap.")


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    return build_panel(gcc_session, tmp_path_factory.mktemp("beacondb"), "beacondb",
                       ["cc-plinius-beacondb.c"], GLUE)


class Tool:
    def __init__(self, world):
        self.world = world
        self.marker = world.home / ".config/furios-gps/contribute"
        self.status_file = world.tmp / "status"
        self.code = world.tmp / "code"
        self.status("submitted=0\nqueued=0\n")
        world.fake_bin.stub("furios-gps-contribute", f"""
case "$1" in
status) cat "{self.status_file}" ;;
on|off)
    code=$(cat "{self.code}" 2>/dev/null || echo 0)
    [ "$code" = 0 ] || exit $code
    if [ "$1" = on ]; then mkdir -p "{self.marker.parent}"; touch "{self.marker}"; else rm -f "{self.marker}"; fi ;;
esac""")

    def on(self):
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        self.marker.write_text("")

    def status(self, text):
        self.status_file.write_text(text)

    def calls(self):
        return self.world.fake_bin.calls("furios-gps-contribute")


@pytest.fixture
def tool(gcc_world):
    return Tool(gcc_world)


def switch(screen):
    return screen.one(type="AdwSwitchRow", text="Contribute to beaconDB")


def contributions(screen):
    rows = screen.find(type="AdwActionRow", text="Contributions")
    if not rows:
        return None
    return [n["text"] for n in rows[0].walk() if n.get("type") == "GtkLabel"][0]


def group(screen):
    return [g for g in screen.find(type="AdwPreferencesGroup")
            if any(n.get("text") == "Contribute to beaconDB" for n in g.walk())][0]


def test_group_below_the_location_switch(program, gcc_world, tool):
    r = gcc_world.run(program, [call("set_up"), settle(200), dump("main")])
    assert r.value("set_up") is True
    s = r.dump("main")
    groups = s.find(type="AdwPreferencesGroup")
    assert groups[1] == group(s) and groups[2]["text"] == "Apps"
    row = switch(s)
    assert row["subtitle"] == SUBTITLE and row["active"] is False
    # Off and nothing collected: no row for it
    assert contributions(s) is None
    assert tool.calls() == [["status"]]
    assert not r.warnings()


def test_nothing_without_the_tool(program, gcc_world):
    r = gcc_world.run(program, [call("set_up"), dump("main")])
    assert r.value("set_up") is False
    assert not r.dump("main").has(type="AdwSwitchRow", text="Contribute to beaconDB")


@pytest.mark.parametrize("on, status, shown", [
    (True, "submitted=0\nqueued=0\n", "Sent: 0, waiting: 0"),     # on: always worth the row
    (False, "submitted=12\nqueued=0\n", "Sent: 12, waiting: 0"),  # collected before
    (False, "submitted=0\nqueued=3\n", "Sent: 0, waiting: 3"),
    (True, "version=2\nsubmittedx=9\nsubmitted=5\nqueued=1\n", "Sent: 5, waiting: 1"),
    (True, "submitted=5\n", None),   # half an answer: nothing shown
    (True, "", None),
])
def test_marker_and_status(program, gcc_world, tool, on, status, shown):
    if on:
        tool.on()
    tool.status(status)
    r = gcc_world.run(program, [settle(300), dump("main")])
    assert switch(r.dump("main"))["active"] is on
    assert contributions(r.dump("main")) == shown


def test_group_follows_the_location_switch(program, gcc_world, tool):
    r = gcc_world.run(program, [settle(200), dump("on"),
                                set_active(False, type="AdwSwitchRow", text="Automatic Device Location"),
                                dump("off")])
    assert group(r.dump("on")).get("sensitive") is None
    assert group(r.dump("off")).get("sensitive") is False


def test_location_off_at_the_start(program, gcc_world, tool):
    r = gcc_world.run(program, [settle(200), dump("main")], params={"location": "off"})
    assert group(r.dump("main")).get("sensitive") is False


def test_switch_on_and_off_run_the_tool(program, gcc_world, tool):
    r = gcc_world.run(program, [settle(200), set_active(True, type="AdwSwitchRow", text="Contribute to beaconDB"),
                                wait_for(type="AdwActionRow", text="Contributions"), dump("on"),
                                set_active(False, type="AdwSwitchRow", text="Contribute to beaconDB"),
                                wait_for(type="AdwActionRow", text="Contributions", gone=True), dump("off")])
    assert [c for c in tool.calls() if c != ["status"]] == [["on"], ["off"]]
    assert switch(r.dump("on"))["active"] is True
    assert contributions(r.dump("on")) == "Sent: 0, waiting: 0"
    assert switch(r.dump("off"))["active"] is False
    assert not tool.marker.exists()
    assert not r.warnings()


def test_tool_failing_puts_the_switch_back(program, gcc_world, tool):
    tool.code.write_text("2")
    r = gcc_world.run(program, [settle(200), set_active(True, type="AdwSwitchRow", text="Contribute to beaconDB"),
                                settle(300), dump("back")])
    assert ["on"] in tool.calls()
    assert switch(r.dump("back"))["active"] is False
    assert [m["message"] for m in r.warnings()] == [
        "furios-gps-contribute failed: Child process exited with code 2"]


def test_tool_gone_after_the_start(program, gcc_world, tool):
    exe = gcc_world.fake_bin.path / "furios-gps-contribute"
    r = gcc_world.run(program, [settle(200), {"op": "remove_file", "path": str(exe)},
                                set_active(True, type="AdwSwitchRow", text="Contribute to beaconDB"),
                                settle(), dump("back")])
    assert switch(r.dump("back"))["active"] is False
    # Tried once: the switch goes back after GTK is done with the tap, not inside it
    msgs = [m["message"] for m in r.warnings()]
    assert len(msgs) == 1 and msgs[0].startswith("Cannot run furios-gps-contribute: ")


def test_marker_under_xdg_config_home(program, gcc_world, tool):
    # As the tool finds it: $XDG_CONFIG_HOME
    other = gcc_world.tmp / "config"
    (other / "furios-gps").mkdir(parents=True)
    (other / "furios-gps/contribute").write_text("")
    gcc_world.extra_env["XDG_CONFIG_HOME"] = str(other)
    r = gcc_world.run(program, [settle(200), dump("main")])
    assert switch(r.dump("main"))["active"] is True
