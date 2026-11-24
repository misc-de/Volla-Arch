# The USB panel (cc-usb-panel.c): what the phone offers a computer on the cable, as plinius-usb
# (org.plinius.Usb on the system bus, interface from its C source) reports it; switching a
# function, the power direction and the presets go back to the service, refusals show as a toast
# with the state the service holds. The service is a fake on the test's private system bus.

import re

import pytest

from conftest import PKG
from gcc_harness import (build_panel, call, dump, gcc_session, gcc_world, interface_xml,  # noqa: F401
                         select, set_active, settle, wait)

USB = "org.plinius.Usb"
USB_PATH = "/org/plinius/Usb"
USB_XML = interface_xml(PKG / "plinius-usb" / "plinius-usb.c", USB)
POLICY = "/usr/share/dbus-1/system.d/org.plinius.Usb.conf"
# plinius-usb before 0.4: no power properties
OLD_PROPS = ("PowerRole", "PowerRoleSwappable", "SupplyPowerOnConnect", "NetworkOnConnect")

GLUE = r'''
#include "gcc_driver.h"
#include "cc-usb-panel.h"

CcPanel *
gcc_test_create_panel (GccTest *t)
{
    return g_object_new (CC_TYPE_USB_PANEL, "window", t->window, NULL);
}

void
gcc_test_setup (GccTest *t)
{
}

gboolean
gcc_test_call (GccTest *t, const char *name, JsonArray *args, JsonBuilder *result)
{
    if (g_str_equal (name, "static_init")) {
        cc_usb_panel_static_init_func ();
        json_builder_add_null_value (result);
        return TRUE;
    }
    return FALSE;
}
'''

FUNCTIONS = {"mtp": "File Sharing", "ncm": "Network", "gateway": "Internet for the Computer",
             "webcam": "Webcam"}
DENIED = {"error": "org.freedesktop.DBus.Error.AccessDenied", "message": "The phone is locked"}


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    return build_panel(gcc_session, tmp_path_factory.mktemp("usb"), "usb", ["cc-usb-panel.c"],
                       GLUE, replacements={"cc-usb-panel.c": [(f'"{POLICY}"', f'"{{root}}{POLICY}"')]})


def strv(names):
    return "[" + ", ".join(repr(n) for n in names) + "]" if names else "@as []"


def service(world, connected=True, functions=(), available=("mtp", "ncm", "gateway"),
            network_on_connect=False, role="sink", swappable=True, supply=False, xml=USB_XML,
            own=True, **replies):
    props = {"Connected": "true" if connected else "false", "Functions": strv(functions),
             "AvailableFunctions": strv(available),
             "NetworkOnConnect": "true" if network_on_connect else "false",
             "PowerRole": repr(role), "PowerRoleSwappable": "true" if swappable else "false",
             "SupplyPowerOnConnect": "true" if supply else "false"}
    world.service("system", USB, USB_PATH, xml, properties=props, replies=replies, own=own)


def old_xml():
    """The interface without the properties of plinius-usb 0.4 (and without the preset)."""
    xml = USB_XML
    for p in OLD_PROPS:
        xml, n = re.subn(rf"<property name='{p}'[^>]*/>", "", xml)
        assert n == 1, p
    return xml


def status(screen):
    return screen.one(type="AdwActionRow", text="Computer")["subtitle"]


def function_rows(screen):
    """{title: (active, sensitive)} of the function rows shown."""
    group = screen.one(type="AdwPreferencesGroup", text="Use USB For")
    return {r["text"]: (r["active"], r.get("sensitive", True))
            for r in walk(group) if r.get("type") == "AdwSwitchRow"}


def walk(node):
    yield node
    for c in node.get("children", ()):
        yield from walk(c)


def row(screen, type, text, within=None):
    s = screen.within(type="AdwPreferencesGroup", text=within) if within else screen
    return s.one(type=type, text=text)


def power(screen):
    r = row(screen, "AdwSwitchRow", "Power the Connected Device")
    return r["active"], r.get("sensitive", True), r["subtitle"]


def preset(screen):
    r = row(screen, "AdwComboRow", "Power Supply", within="When Plugging In")
    return r["selected_text"], r.get("sensitive", True)


def toast(screen, text):
    """A toast shows as a label of its own, outside the rows."""
    return screen.has(type="GtkLabel", text=text)


def sets(world):
    return [c["args"] for c in world.calls(USB, "Set")]


def test_connected_with_functions_on(program, gcc_world):
    service(gcc_world, functions=["mtp"], available=["mtp", "ncm", "gateway", "webcam"])
    r = gcc_world.run(program, [settle(), dump("page")])
    screen = r.dump("page")
    assert status(screen) == "Connected to a computer"
    assert function_rows(screen) == {"File Sharing": (True, True), "Network": (False, True),
                                     "Internet for the Computer": (False, True),
                                     "Webcam": (False, True)}
    subtitles = {n["text"]: n["subtitle"] for n in screen.find(type="AdwSwitchRow")}
    assert subtitles["File Sharing"] == "The computer sees the files of your home folder (MTP)"
    assert subtitles["Webcam"] == "The computer gets a picture of the phone's camera"
    assert power(screen) == (False, True, "This phone takes power from the cable")
    assert preset(screen) == ("Charge This Phone", True)
    assert row(screen, "AdwSwitchRow", "Network Right Away")["active"] is False
    assert [g["text"] for g in screen.find(type="AdwPreferencesGroup") if g.get("text")] == [
        "Use USB For", "Power Supply", "When Plugging In"]
    # Showing changes nothing
    assert not gcc_world.calls(USB, "SetFunctions") and not sets(gcc_world)
    assert not r.warnings()


def test_functions_the_service_does_not_offer_have_no_row(program, gcc_world):
    service(gcc_world, available=["ncm"])
    r = gcc_world.run(program, [settle(), dump("page")])
    assert function_rows(r.dump("page")) == {"Network": (False, True)}


def test_without_a_computer_only_charging(program, gcc_world):
    service(gcc_world, connected=False, role="", swappable=False)
    r = gcc_world.run(program, [settle(), dump("page")])
    screen = r.dump("page")
    assert status(screen) == "No computer connected, only charging"
    assert all(not sensitive for _a, sensitive in function_rows(screen).values())
    assert power(screen) == (False, False, "Nothing connected")


def test_gateway_keeps_the_network_on(program, gcc_world):
    service(gcc_world, functions=["ncm", "gateway"])
    r = gcc_world.run(program, [settle(), dump("page")])
    rows = function_rows(r.dump("page"))
    assert rows["Network"] == (True, False)
    assert rows["Internet for the Computer"] == (True, True)


@pytest.mark.parametrize("start, title, active, sent", [
    ([], "File Sharing", True, ["mtp"]),
    (["ncm"], "Internet for the Computer", True, ["ncm", "gateway"]),
    (["ncm"], "Network", False, []),
])
def test_switching_a_function_sends_the_whole_set(program, gcc_world, start, title, active, sent):
    service(gcc_world, functions=start)
    gcc_world.run(program, [settle(), set_active(active, type="AdwSwitchRow", text=title), settle()])
    assert [c["args"] for c in gcc_world.calls(USB, "SetFunctions")] == [[sent]]


def test_hidden_function_rows_are_not_sent(program, gcc_world):
    # Webcam is on in the service but not offered: no row, not in the set
    service(gcc_world, functions=["webcam"], available=["mtp"])
    gcc_world.run(program, [settle(), set_active(True, type="AdwSwitchRow", text="File Sharing"),
                            settle()])
    assert [c["args"] for c in gcc_world.calls(USB, "SetFunctions")] == [[["mtp"]]]


def test_refused_function_shows_why_and_the_real_state(program, gcc_world):
    service(gcc_world, SetFunctions=DENIED)
    r = gcc_world.run(program, [settle(), set_active(True, type="AdwSwitchRow", text="File Sharing"),
                                settle(), dump("page")])
    screen = r.dump("page")
    assert function_rows(screen)["File Sharing"] == (False, True)
    assert toast(screen, "The phone is locked")
    assert [m["message"] for m in r.warnings()] == [
        "Changing the USB functions failed: The phone is locked"]


def test_service_reports_the_new_functions(program, gcc_world):
    service(gcc_world)
    ctl = gcc_world.control("system")
    r = gcc_world.run(program, [settle(),
                                ctl.set_property(USB_PATH, USB, "Functions", "['mtp', 'ncm']"),
                                settle(), dump("on"),
                                ctl.set_property(USB_PATH, USB, "Connected", "false"),
                                ctl.set_property(USB_PATH, USB, "Functions", "@as []"),
                                settle(), dump("off")])
    assert function_rows(r.dump("on"))["File Sharing"] == (True, True)
    assert function_rows(r.dump("on"))["Network"] == (True, True)
    assert status(r.dump("off")) == "No computer connected, only charging"
    assert function_rows(r.dump("off"))["File Sharing"] == (False, False)
    # What the service says is not sent back
    assert not gcc_world.calls(USB, "SetFunctions")


def test_network_right_away_is_set(program, gcc_world):
    service(gcc_world)
    r = gcc_world.run(program, [settle(), set_active(True, type="AdwSwitchRow", text="Network Right Away"),
                                settle(), dump("page")])
    assert sets(gcc_world) == [[USB, "NetworkOnConnect", True]]
    assert row(r.dump("page"), "AdwSwitchRow", "Network Right Away")["active"] is True


def test_network_right_away_shown_on(program, gcc_world):
    service(gcc_world, network_on_connect=True)
    r = gcc_world.run(program, [settle(), dump("page")])
    assert row(r.dump("page"), "AdwSwitchRow", "Network Right Away")["active"] is True
    assert not sets(gcc_world)


def test_refused_preset_goes_back(program, gcc_world):
    service(gcc_world)
    gcc_world.services[-1]["set_errors"] = {
        "NetworkOnConnect": ("org.freedesktop.DBus.Error.AccessDenied", "Not allowed")}
    r = gcc_world.run(program, [settle(), set_active(True, type="AdwSwitchRow", text="Network Right Away"),
                                settle(), dump("page")])
    assert row(r.dump("page"), "AdwSwitchRow", "Network Right Away")["active"] is False
    assert toast(r.dump("page"), "Not allowed")
    assert [m["message"] for m in r.warnings()] == ["Changing the USB functions failed: Not allowed"]


@pytest.mark.parametrize("index, value", [(1, True), (0, False)])
def test_power_preset_is_set(program, gcc_world, index, value):
    service(gcc_world, supply=not value)
    r = gcc_world.run(program, [settle(), dump("before"),
                                select(index, type="AdwComboRow", text="Power Supply"), settle()])
    assert preset(r.dump("before"))[0] == ("Charge This Phone" if value else "Power the Connected Device")
    assert sets(gcc_world) == [[USB, "SupplyPowerOnConnect", value]]


@pytest.mark.parametrize("role, swappable, subtitle, sensitive", [
    ("source", True, "The phone supplies power to the connected device", True),
    ("source", False, "The connected device takes its power from the phone", False),
    ("sink", True, "This phone takes power from the cable", True),
    ("sink", False, "Only with devices that support USB Power Delivery", False),
    ("", True, "Nothing connected", False),
])
def test_power_row_says_where_the_power_goes(program, gcc_world, role, swappable, subtitle, sensitive):
    service(gcc_world, role=role, swappable=swappable)
    r = gcc_world.run(program, [settle(), dump("page")])
    assert power(r.dump("page")) == (role == "source", sensitive, subtitle)


@pytest.mark.parametrize("active, role", [(True, "source"), (False, "sink")])
def test_power_switch_asks_for_a_swap(program, gcc_world, active, role):
    service(gcc_world, role="source" if not active else "sink",
            SetPowerRole={"delay_ms": 300, "reply": "()"})
    ctl = gcc_world.control("system")
    r = gcc_world.run(program, [settle(),
                                set_active(active, type="AdwSwitchRow", text="Power the Connected Device"),
                                dump("busy"), wait(500), settle(), dump("refused-to-change"),
                                ctl.set_property(USB_PATH, USB, "PowerRole", repr(role)), settle(),
                                dump("done")])
    assert [c["args"] for c in gcc_world.calls(USB, "SetPowerRole")] == [[role]]
    # While the partner answers the row waits
    assert power(r.dump("busy"))[:2] == (active, False)
    # Answered but PowerRole unchanged: the real state shows again
    assert power(r.dump("refused-to-change"))[:2] == (not active, True)
    assert power(r.dump("done"))[:2] == (active, True)
    assert not r.warnings()


def test_power_swap_refused_by_the_partner(program, gcc_world):
    service(gcc_world, SetPowerRole={"error": "org.plinius.Usb.Error.Refused",
                                     "message": "The device refused the power role swap"})
    r = gcc_world.run(program, [settle(),
                                set_active(True, type="AdwSwitchRow", text="Power the Connected Device"),
                                settle(), dump("page")])
    assert power(r.dump("page")) == (False, True, "This phone takes power from the cable")
    assert toast(r.dump("page"), "The device refused the power role swap")
    assert [m["message"] for m in r.warnings()] == [
        "Changing the power direction failed: The device refused the power role swap"]


def test_old_service_without_power_and_preset(program, gcc_world):
    service(gcc_world, xml=old_xml())
    r = gcc_world.run(program, [settle(), dump("page")])
    screen = r.dump("page")
    assert power(screen) == (False, False, "Nothing connected")
    assert preset(screen) == ("Charge This Phone", False)
    assert row(screen, "AdwSwitchRow", "Network Right Away").get("sensitive") is False
    assert status(screen) == "Connected to a computer"


def test_service_not_running(program, gcc_world):
    service(gcc_world, own=False)
    r = gcc_world.run(program, [settle(), dump("page")])
    screen = r.dump("page")
    assert status(screen) == "The USB service is not running"
    assert function_rows(screen) == {}
    assert power(screen) == (False, False, "Nothing connected")
    assert preset(screen)[1] is False
    assert not r.warnings()


def test_service_leaves_and_comes_back(program, gcc_world):
    service(gcc_world, functions=["mtp"])
    ctl = gcc_world.control("system")
    r = gcc_world.run(program, [settle(), ctl.release(USB), settle(), dump("gone"),
                                ctl.own(USB), settle(200), dump("back")])
    assert status(r.dump("gone")) == "The USB service is not running"
    assert function_rows(r.dump("gone")) == {}
    assert status(r.dump("back")) == "Connected to a computer"
    assert function_rows(r.dump("back"))["File Sharing"] == (True, True)
    # Coming back is not a change of the user's
    assert not gcc_world.calls(USB, "SetFunctions") and not sets(gcc_world)


def test_without_a_system_bus(program, gcc_world):
    service(gcc_world)
    r = gcc_world.run(program, [settle(), dump("page")], system_bus=False)
    screen = r.dump("page")
    # Nothing was answered: the rows stay as made, insensitive
    assert screen.one(type="AdwActionRow", text="Computer").get("subtitle") is None
    assert power(screen) == (False, False, "Nothing connected")
    assert [m["message"] for m in r.warnings()][0].startswith("Cannot reach plinius-usb: ")


def test_leaving_while_the_partner_answers(program, gcc_world):
    service(gcc_world, SetPowerRole={"delay_ms": 2000, "reply": "()"})
    r = gcc_world.run(program, [settle(),
                                set_active(True, type="AdwSwitchRow", text="Power the Connected Device"),
                                settle()])
    # The window went while SetPowerRole was waiting: no answer handled, no warning
    assert len(gcc_world.calls(USB, "SetPowerRole")) == 1
    assert not r.warnings()


@pytest.mark.parametrize("installed, shown", [(True, "visible"), (False, "hidden")])
def test_listed_only_with_plinius_usb(program, gcc_world, installed, shown):
    policy = program.root / POLICY.lstrip("/")
    if installed:
        policy.parent.mkdir(parents=True, exist_ok=True)
        policy.write_text("<busconfig/>\n")
    elif policy.exists():
        policy.unlink()
    service(gcc_world)
    r = gcc_world.run(program, [call("static_init")])
    assert r.visibility == {"usb": shown}
