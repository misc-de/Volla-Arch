# The phone's options on the Display, Appearance and Search pages (cc-plinius-display.c): Rotation
# Speed (sensor-proxy-bridge's OrientationSettleTime), Wake Up (plinius-wake), Brightness and Screen
# Off (GNOME's power and session settings, shown as GNOME's Power page decides from the Shell's
# brightness control and the light sensor), Always-On Display and Flip Cover, the App Overview and
# Home Screen groups (phosh-plinius' org.plinius.display, Phosh's app filter).
#
# The settings come from schema directories of the test's own: the host's schemas, ours from
# pkgbuilds/, and stand-ins for the schemas the host lacks (gnome-settings-daemon's power plugin,
# Phosh's shell); a second directory has what an older phosh-plinius and Phosh install.
# panels/common/cc-number-row.c needs cc-number-row-enums.h, which meson makes with glib-mkenums
# (not on the host): it is written by hand in c/number-row/, and a test checks it against the
# enums of the tree's cc-number-row.h.

import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from conftest import PKG
from gcc_harness import (CDIR, build_panel, call, dump, gcc_session, gcc_world, interface_xml,  # noqa: F401
                         select, set_active, settle, wait, wait_for)

SENSORS = "net.hadess.SensorProxy"
SENSORS_PATH = "/net/hadess/SensorProxy"
SENSORS_XML = interface_xml(PKG / "sensor-proxy-bridge" / "sensor-proxy-bridge.c", "org.plinius.SensorProxyBridge")
WAKE = "org.plinius.Wake"
WAKE_PATH = "/org/plinius/Wake"
WAKE_XML = interface_xml(PKG / "plinius-wake" / "plinius-wake.c", WAKE)
# plinius-wake before 0.3
OLD_WAKE_XML = re.sub(r"<property name='PickUpBrighten'[^>]*/>", "", WAKE_XML)
BRIGHTNESS = "org.gnome.Shell.Brightness"
BRIGHTNESS_PATH = "/org/gnome/Shell/Brightness"
BRIGHTNESS_XML = """<node><interface name="org.gnome.Shell.Brightness">
 <property name="HasBrightnessControl" type="b" access="read"/>
</interface></node>"""
NUMBER_ROW = CDIR / "number-row"

GLUE = r'''
#include "gcc_driver.h"
#include "cc-plinius-display.h"

static gboolean display_any, overview, search;
static GtkWidget *rotation;

void
gcc_test_setup (GccTest *t)
{
    const char *what = gcc_test_param (t, "what");

    if (!g_strcmp0 (what, "appearance")) {
        /* Appearance: GNOME's own groups, ours goes on top */
        GtkWidget *style = adw_preferences_group_new ();

        adw_preferences_group_set_title (ADW_PREFERENCES_GROUP (style), "Style");
        adw_preferences_page_add (t->page, ADW_PREFERENCES_GROUP (style));
        overview = cc_plinius_display_setup_app_overview (t->page);
    } else if (!g_strcmp0 (what, "search")) {
        GtkWidget *apps = adw_preferences_group_new ();

        adw_preferences_group_set_title (ADW_PREFERENCES_GROUP (apps), "Search Results");
        adw_preferences_page_add (t->page, ADW_PREFERENCES_GROUP (apps));
        search = cc_plinius_display_setup_search (t->page);
    } else {
        /* Display: Auto Rotate with our row right below, then the box of the phone's groups */
        GtkWidget *orientation = adw_preferences_group_new ();
        GtkWidget *auto_rotate = adw_switch_row_new ();
        GtkWidget *box = gtk_box_new (GTK_ORIENTATION_VERTICAL, 24);

        adw_preferences_row_set_title (ADW_PREFERENCES_ROW (auto_rotate), "Auto Rotate");
        adw_preferences_group_add (ADW_PREFERENCES_GROUP (orientation), auto_rotate);
        rotation = cc_plinius_display_rotation_row_new ();
        if (rotation)
            adw_preferences_group_add (ADW_PREFERENCES_GROUP (orientation), rotation);
        adw_preferences_page_add (t->page, ADW_PREFERENCES_GROUP (orientation));
        display_any = cc_plinius_display_setup (GTK_BOX (box));
        gcc_test_add (t, box);
    }
}

gboolean
gcc_test_call (GccTest *t, const char *name, JsonArray *args, JsonBuilder *result)
{
    if (!g_str_equal (name, "set_up"))
        return FALSE;
    json_builder_begin_object (result);
    json_builder_set_member_name (result, "display");
    json_builder_add_boolean_value (result, display_any);
    json_builder_set_member_name (result, "rotation");
    json_builder_add_boolean_value (result, rotation != NULL);
    json_builder_set_member_name (result, "overview");
    json_builder_add_boolean_value (result, overview);
    json_builder_set_member_name (result, "search");
    json_builder_add_boolean_value (result, search);
    json_builder_end_object (result);
    return TRUE;
}
'''

# Stand-ins for what the host lacks: the keys our file uses, with the real schemas' types and defaults
POWER_SCHEMA = """<schemalist>
  <schema id="org.gnome.settings-daemon.plugins.power" path="/org/gnome/settings-daemon/plugins/power/">
    <key name="ambient-enabled" type="b"><default>true</default></key>
    <key name="idle-dim" type="b"><default>true</default></key>
  </schema>
</schemalist>
"""
PHOSH_SCHEMA = """<schemalist>
  <flags id="{id}.PhoshAppFilterModeFlags">
    <value nick="none" value="0"/>
    <value nick="adaptive" value="1"/>
  </flags>
  <schema id="{id}" path="/{path}/">
    <key name="app-filter-mode" flags="{id}.PhoshAppFilterModeFlags"><default>['adaptive']</default></key>
  </schema>
</schemalist>
"""


def schema_dir(base, name, *, power, phosh, display_keys, assist):
    """A compiled schema directory: the host's, ours (org.plinius.display with only display_keys,
    None: all), stand-ins."""
    d = base / name
    d.mkdir()
    for f in Path("/usr/share/glib-2.0/schemas").glob("*.xml"):
        shutil.copy(f, d / f.name)
    for f in PKG.glob("*/*.gschema.xml"):
        if f.name == "org.plinius.assistant-button.gschema.xml" and not assist:
            continue
        if f.name == "org.plinius.display.gschema.xml":
            if display_keys == ():
                continue
            if display_keys is not None:
                tree = ET.parse(f)
                schema = tree.getroot().find("schema")
                for key in list(schema.findall("key")):
                    if key.get("name") not in display_keys:
                        schema.remove(key)
                tree.write(d / f.name)
                continue
        shutil.copy(f, d / f.name)
    if power:
        (d / "test.power.gschema.xml").write_text(POWER_SCHEMA)
    if phosh:
        (d / "test.phosh.gschema.xml").write_text(PHOSH_SCHEMA.format(id=phosh, path=phosh.replace(".", "/")))
    r = subprocess.run(["glib-compile-schemas", str(d)], capture_output=True, text=True)
    if r.returncode != 0:
        # A host schema that does not compile alone (as in gcc_harness): ours and GNOME's suffice
        for f in d.glob("*.gschema.xml"):
            if not (f.name.startswith(("org.gnome.desktop.", "test.")) or any(PKG.glob(f"*/{f.name}"))):
                f.unlink()
        subprocess.run(["glib-compile-schemas", "--strict", str(d)], check=True, capture_output=True)
    return d


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    out = tmp_path_factory.mktemp("display")
    prog = build_panel(gcc_session, out, "display", ["cc-plinius-display.c"], GLUE,
                       upstream=("panels/common/cc-number-row.c", "panels/common/cc-util.c"),
                       sources=[NUMBER_ROW / "cc-number-row-enums.c"], flags=[f"-I{NUMBER_ROW}"])
    base = out / "schemas"
    base.mkdir()
    prog.schemas = {
        "full": schema_dir(base, "full", power=True, phosh="mobi.phosh.shell", display_keys=None, assist=True),
        # phosh-plinius before 0.57.0-24 and Phosh before 0.59; no power plugin, no plinius-assist
        "old": schema_dir(base, "old", power=False, phosh="sm.puri.phosh", assist=False,
                          display_keys=("app-grid-search", "app-grid-app-names", "app-grid-folder-dock")),
        # No phosh-plinius at all, no Phosh filter
        "bare": schema_dir(base, "bare", power=False, phosh=None, display_keys=(), assist=False),
        # phosh-plinius without the folder dock key yet: the overview group does not show
        "nodock": schema_dir(base, "nodock", power=True, phosh=None, assist=True,
                             display_keys=("app-grid-search", "app-grid-app-names")),
        "nophosh": schema_dir(base, "nophosh", power=False, phosh=None, display_keys=None, assist=False),
    }
    return prog


def use(world, program, schemas):
    world.extra_env["GSETTINGS_SCHEMA_DIR"] = str(program.schemas[schemas])


def sensors(world, settle_time=500, ambient=True, own=True, set_errors=None):
    props = {"HasAmbientLight": "true" if ambient else "false"}
    if settle_time is not None:
        props["OrientationSettleTime"] = f"uint32 {settle_time}"
    xml = SENSORS_XML
    if settle_time is None:
        # An older sensor-proxy-bridge without the property
        xml = re.sub(r"<property name='OrientationSettleTime'[^>]*/>", "", xml)
    world.service("system", SENSORS, SENSORS_PATH, xml, properties=props, own=own, set_errors=set_errors)


def wake(world, double_tap=True, tilt=True, has_double_tap=True, has_tilt=True, brighten=False, old=False,
         set_errors=None):
    props = {"DoubleTap": str(double_tap).lower(), "Tilt": str(tilt).lower(),
             "HasDoubleTap": str(has_double_tap).lower(), "HasTilt": str(has_tilt).lower()}
    if not old:
        props["PickUpBrighten"] = str(brighten).lower()
    world.service("system", WAKE, WAKE_PATH, OLD_WAKE_XML if old else WAKE_XML, properties=props,
                  set_errors=set_errors)


def brightness(world, control=True):
    world.service("session", BRIGHTNESS, BRIGHTNESS_PATH, BRIGHTNESS_XML,
                  properties={"HasBrightnessControl": str(control).lower()})


def groups(screen):
    return [g.get("text") for g in screen.find(type="AdwPreferencesGroup")]


def switches(screen, group=None):
    s = screen.within(type="AdwPreferencesGroup", text=group) if group else screen
    return [(n["text"], n["active"]) for n in s.find(type="AdwSwitchRow")]


def gs_get(schema, key, label=None):
    return {"op": "gsettings_get", "schema": schema, "key": key, "label": label or key}


def gs_set(schema, key, value):
    return {"op": "gsettings_set", "schema": schema, "key": key, "value": value}


# ----- The hand-written enums -----

def test_hand_written_enums_match_the_header(gcc_session):
    header = (gcc_session.tree.root / "panels/common/cc-number-row.h").read_text()
    enums = {m.group(2): re.findall(r"\b(CC_NUMBER_\w+)", m.group(1))
             for m in re.finditer(r"typedef enum\s*\{(.*?)\}\s*(\w+);", header, re.S)}
    assert set(enums) == {"CcNumberOrder", "CcNumberValueType", "CcNumberSortType"}
    ours = (NUMBER_ROW / "cc-number-row-enums.c").read_text()
    got = {m.group(1): re.findall(r'\{ (CC_NUMBER_\w+), "(CC_NUMBER_\w+)", "(\w+)" \}', m.group(2))
           for m in re.finditer(r'ENUM_TYPE \(\w+, "(\w+)",(.*?)\)\n', ours, re.S)}
    assert {name: [v[0] for v in values] for name, values in got.items()} == enums
    for name, values in got.items():
        prefix = re.sub(r"(?<!^)([A-Z])", r"_\1", name).upper() + "_"
        for value, value_name, nick in values:
            # As glib-mkenums names them: the value's name, the nick without the type's prefix
            assert value_name == value
            short = value[len("CC_NUMBER_"):]
            short = short.split("_", 1)[1] if name != "CcNumberValueType" else short[len("VALUE_"):]
            assert nick == short.lower().replace("_", "-"), (prefix, value)
    header_ours = (NUMBER_ROW / "cc-number-row-enums.h").read_text()
    for func, macro in (("cc_number_order_get_type", "CC_TYPE_NUMBER_ORDER"),
                        ("cc_number_value_type_get_type", "CC_TYPE_NUMBER_VALUE_TYPE"),
                        ("cc_number_sort_type_get_type", "CC_TYPE_NUMBER_SORT_TYPE")):
        assert f"#define {macro} ({func} ())" in header_ours
    # Every enum type cc-number-row.c names is one of those
    source = (gcc_session.tree.root / "panels/common/cc-number-row.c").read_text()
    assert set(re.findall(r"\bCC_TYPE_NUMBER_(?!OBJECT|ROW)\w+", source)) <= {
        "CC_TYPE_NUMBER_ORDER", "CC_TYPE_NUMBER_VALUE_TYPE", "CC_TYPE_NUMBER_SORT_TYPE"}


# ----- Display page -----

def test_everything_on_the_display_page(program, gcc_world):
    use(gcc_world, program, "full")
    sensors(gcc_world, 500)
    wake(gcc_world, double_tap=True, tilt=False, brighten=True)
    brightness(gcc_world, True)
    r = gcc_world.run(program, [call("set_up"), settle(200), dump("page")])
    assert r.value("set_up")["display"] is True and r.value("set_up")["rotation"] is True
    s = r.dump("page")
    # Auto Rotate's group, the box's group, then the box: ours in the order of the page
    assert groups(s) == [None, None, "Brightness and Screen Off", None, "Wake Up", None]
    rotation = s.one(type="AdwComboRow", text="Rotation Speed")
    assert rotation["items"] == ["Fast", "Normal", "Slow"] and rotation["selected_text"] == "Normal"
    assert rotation["subtitle"] == "How long the phone must be held turned before the screen follows"
    bright = s.within(type="AdwPreferencesGroup", text="Brightness and Screen Off")
    assert switches(bright) == [("Automatic Screen Brightness", True), ("Dim Screen", True)]
    delay = bright.one(type="CcNumberRow", text="Blank Screen Delay")
    assert delay["items"] == ["1 minute", "2 minutes", "3 minutes", "4 minutes", "5 minutes", "8 minutes",
                              "10 minutes", "12 minutes", "15 minutes", "Never"]
    assert delay["selected_text"] == "5 minutes"
    assert delay["subtitle"] == "Period of inactivity until screen blanks"
    assert switches(s, "Wake Up") == [("Double Tap", True), ("Tilt", False), ("Brighten When Picked Up", True)]
    assert s.one(type="AdwPreferencesGroup", text="Wake Up")["subtitle"] == (
        "The screen also comes on without the power button. Not while the phone is in a pocket.")
    rows = [(n["text"], n.get("subtitle")) for n in s.find(type="AdwSwitchRow")]
    assert ("Always-On Display", "Time and notifications on the dark screen at low power, not during calls") in rows
    assert ("Flip Cover", "Closing the cover turns the screen off, opening it turns the screen on") in rows
    assert not r.warnings()


@pytest.mark.parametrize("stored, shown", [(100, "Fast"), (300, "Fast"), (600, "Normal"), (750, "Normal"),
                                           (800, "Slow"), (5000, "Slow")])
def test_rotation_speed_shows_the_nearest_step(program, gcc_world, stored, shown):
    use(gcc_world, program, "bare")
    sensors(gcc_world, stored)
    r = gcc_world.run(program, [dump("page")])
    assert r.dump("page").one(type="AdwComboRow", text="Rotation Speed")["selected_text"] == shown


def test_rotation_speed_is_set_on_the_bridge(program, gcc_world):
    use(gcc_world, program, "bare")
    sensors(gcc_world, 500)
    r = gcc_world.run(program, [select(item="Slow", type="AdwComboRow", text="Rotation Speed"), settle(200),
                                select(item="Fast", type="AdwComboRow", text="Rotation Speed"), settle(200)])
    sets = [c["args"] for c in gcc_world.calls(SENSORS, "Set")]
    assert sets == [["org.plinius.SensorProxyBridge", "OrientationSettleTime", 1000],
                    ["org.plinius.SensorProxyBridge", "OrientationSettleTime", 250]]
    assert not r.warnings()


def test_rotation_speed_refused(program, gcc_world):
    use(gcc_world, program, "bare")
    sensors(gcc_world, 500, set_errors={"OrientationSettleTime": ("org.freedesktop.DBus.Error.AccessDenied", "no")})
    r = gcc_world.run(program, [select(item="Slow", type="AdwComboRow", text="Rotation Speed"), settle(300)])
    warnings = [m["message"] for m in r.warnings()]
    assert len(warnings) == 1 and warnings[0].startswith("Changing OrientationSettleTime failed: ")


@pytest.mark.parametrize("how", ["gone", "old bridge", "no system bus"])
def test_no_rotation_row_without_the_bridge(program, gcc_world, how):
    use(gcc_world, program, "bare")
    if how == "gone":
        sensors(gcc_world, 500, own=False)
    elif how == "old bridge":
        sensors(gcc_world, None)
    r = gcc_world.run(program, [call("set_up"), dump("page")], system_bus=how != "no system bus")
    assert r.value("set_up")["rotation"] is False
    assert not r.dump("page").has(type="AdwComboRow", text="Rotation Speed")


def test_nothing_without_any_service_or_schema(program, gcc_world):
    use(gcc_world, program, "bare")
    r = gcc_world.run(program, [call("set_up"), dump("page")])
    assert r.value("set_up") == {"display": False, "rotation": False, "overview": False, "search": False}
    assert groups(r.dump("page")) == [None, None]
    assert not r.dump("page").has(type="AdwSwitchRow", text="Double Tap")


# ----- Wake up -----

def test_wake_switches_set_the_service(program, gcc_world):
    use(gcc_world, program, "bare")
    wake(gcc_world, double_tap=False, tilt=True, brighten=False)
    r = gcc_world.run(program, [set_active(True, type="AdwSwitchRow", text="Double Tap"),
                                set_active(False, type="AdwSwitchRow", text="Tilt"),
                                set_active(True, type="AdwSwitchRow", text="Brighten When Picked Up"), settle(200)])
    assert [c["args"] for c in gcc_world.calls(WAKE, "Set")] == [
        [WAKE, "DoubleTap", True], [WAKE, "Tilt", False], [WAKE, "PickUpBrighten", True]]
    assert not r.warnings()


def test_wake_rows_follow_the_service(program, gcc_world):
    use(gcc_world, program, "bare")
    wake(gcc_world, double_tap=False, tilt=False)
    ctl = gcc_world.control()
    r = gcc_world.run(program, [ctl.set_property(WAKE_PATH, WAKE, "DoubleTap", "true"),
                                ctl.set_property(WAKE_PATH, WAKE, "PickUpBrighten", "true"), settle(200),
                                dump("after")])
    assert switches(r.dump("after"), "Wake Up") == [("Double Tap", True), ("Tilt", False),
                                                    ("Brighten When Picked Up", True)]
    # Shown, not set back
    assert gcc_world.calls(WAKE, "Set") == []


def test_wake_refused(program, gcc_world):
    use(gcc_world, program, "bare")
    wake(gcc_world, set_errors={"Tilt": ("org.freedesktop.DBus.Error.AccessDenied", "no")})
    r = gcc_world.run(program, [set_active(False, type="AdwSwitchRow", text="Tilt"), settle(300)])
    warnings = [m["message"] for m in r.warnings()]
    assert len(warnings) == 1 and warnings[0].startswith("Changing Tilt failed: ")


@pytest.mark.parametrize("has_double_tap, has_tilt, old, rows", [
    (True, False, False, ["Double Tap"]),
    (False, True, False, ["Tilt", "Brighten When Picked Up"]),
    (False, True, True, ["Tilt"]),
    (False, False, False, []),
])
def test_wake_rows_only_for_what_the_phone_has(program, gcc_world, has_double_tap, has_tilt, old, rows):
    use(gcc_world, program, "bare")
    wake(gcc_world, has_double_tap=has_double_tap, has_tilt=has_tilt, old=old)
    r = gcc_world.run(program, [call("set_up"), dump("page")])
    s = r.dump("page")
    assert [n["text"] for n in s.find(type="AdwSwitchRow") if n["text"] != "Auto Rotate"] == rows
    assert s.has(type="AdwPreferencesGroup", text="Wake Up") is bool(rows)
    # The group is there (hidden): the setup has something
    assert r.value("set_up")["display"] is True


# ----- Brightness and screen off -----

@pytest.mark.parametrize("control, ambient, rows", [
    (True, True, ["Automatic Screen Brightness", "Dim Screen"]),
    (True, False, ["Dim Screen"]),
    (False, True, []),
    (None, True, []),
])
def test_brightness_rows_as_on_the_power_page(program, gcc_world, control, ambient, rows):
    use(gcc_world, program, "full")
    sensors(gcc_world, 500, ambient=ambient)
    if control is not None:
        brightness(gcc_world, control)
    r = gcc_world.run(program, [settle(200), dump("page")])
    group = r.dump("page").within(type="AdwPreferencesGroup", text="Brightness and Screen Off")
    assert [n["text"] for n in group.find(type="AdwSwitchRow")] == rows
    assert group.has(type="CcNumberRow", text="Blank Screen Delay")


def test_brightness_rows_follow_the_shell(program, gcc_world):
    use(gcc_world, program, "full")
    sensors(gcc_world, 500, ambient=True)
    brightness(gcc_world, False)
    ctl = gcc_world.control("session")
    r = gcc_world.run(program, [settle(200), dump("before"),
                                ctl.set_property(BRIGHTNESS_PATH, BRIGHTNESS, "HasBrightnessControl", "true"),
                                settle(300), dump("after")])
    assert switches(r.dump("before"), "Brightness and Screen Off") == []
    assert [t for t, _ in switches(r.dump("after"), "Brightness and Screen Off")] == [
        "Automatic Screen Brightness", "Dim Screen"]


def test_brightness_without_a_session_bus(program, gcc_world):
    use(gcc_world, program, "full")
    r = gcc_world.run(program, [settle(200), dump("page")], session_bus=False, system_bus=False)
    group = r.dump("page").within(type="AdwPreferencesGroup", text="Brightness and Screen Off")
    assert switches(group) == []
    assert group.has(type="CcNumberRow")


def test_brightness_and_delay_are_gnome_s_settings(program, gcc_world):
    use(gcc_world, program, "full")
    sensors(gcc_world, 500, ambient=True)
    brightness(gcc_world, True)
    power = "org.gnome.settings-daemon.plugins.power"
    session = "org.gnome.desktop.session"
    r = gcc_world.run(program, [settle(200), set_active(False, type="AdwSwitchRow", text="Automatic Screen Brightness"),
                                set_active(False, type="AdwSwitchRow", text="Dim Screen"),
                                select(item="Never", type="CcNumberRow", text="Blank Screen Delay"),
                                gs_get(power, "ambient-enabled"), gs_get(power, "idle-dim"),
                                gs_get(session, "idle-delay"), gs_set(session, "idle-delay", "uint32 120"),
                                gs_set(power, "idle-dim", "true"), dump("after")])
    assert r.value("ambient-enabled") == "false" and r.value("idle-dim") == "false"
    assert r.value("idle-delay") == "0"
    group = r.dump("after").within(type="AdwPreferencesGroup", text="Brightness and Screen Off")
    assert group.one(type="CcNumberRow")["selected_text"] == "2 minutes"
    assert ("Dim Screen", True) in switches(group)


# ----- Always-on display and flip cover -----

def test_always_on_and_cover_are_bound(program, gcc_world):
    use(gcc_world, program, "full")
    r = gcc_world.run(program, [dump("before"), set_active(True, type="AdwSwitchRow", text="Always-On Display"),
                                set_active(False, type="AdwSwitchRow", text="Flip Cover"),
                                gs_get("org.plinius.display", "always-on-display", "aod"),
                                gs_get("org.plinius.assistant-button", "cover")])
    assert ("Always-On Display", False) in switches(r.dump("before"))
    assert ("Flip Cover", True) in switches(r.dump("before"))
    assert r.value("aod") == "true" and r.value("cover") == "false"


def test_older_phosh_plinius_and_no_plinius_assist(program, gcc_world):
    use(gcc_world, program, "old")
    r = gcc_world.run(program, [call("set_up"), dump("page")])
    s = r.dump("page")
    assert r.value("set_up")["display"] is False
    assert not s.has(type="AdwSwitchRow", text="Always-On Display")
    assert not s.has(type="AdwSwitchRow", text="Flip Cover")
    assert not s.has(type="AdwPreferencesGroup", text="Brightness and Screen Off")


# ----- Appearance: the app overview -----

def test_app_overview_on_top_of_appearance(program, gcc_world):
    use(gcc_world, program, "full")
    r = gcc_world.run(program, [call("set_up"), dump("page")], params={"what": "appearance"})
    assert r.value("set_up")["overview"] is True
    s = r.dump("page")
    assert groups(s) == ["App Overview", "Style"]
    assert [(n["text"], n["subtitle"], n["active"]) for n in s.find(type="AdwSwitchRow")] == [
        ("App Names", "Names under the app icons", True),
        ("Folders at the Bottom", "Folders in one row at the bottom edge, scrolling sideways", False),
        ("Open Apps at the Bottom", "The cards of the open apps below the app icons", False),
        ("Mobile Apps Button", "Button that shows only the apps made for phones", True)]


def test_app_overview_switches_write_the_settings(program, gcc_world):
    use(gcc_world, program, "full")
    display = "org.plinius.display"
    r = gcc_world.run(program, [set_active(False, type="AdwSwitchRow", text="App Names"),
                                set_active(True, type="AdwSwitchRow", text="Folders at the Bottom"),
                                set_active(True, type="AdwSwitchRow", text="Open Apps at the Bottom"),
                                set_active(False, type="AdwSwitchRow", text="Mobile Apps Button"),
                                gs_get(display, "app-grid-app-names"), gs_get(display, "app-grid-folder-dock"),
                                gs_get(display, "overview-running-apps", "running"),
                                gs_get("mobi.phosh.shell", "app-filter-mode", "filter"),
                                set_active(False, type="AdwSwitchRow", text="Open Apps at the Bottom"),
                                set_active(True, type="AdwSwitchRow", text="Mobile Apps Button"),
                                gs_get(display, "overview-running-apps", "running again"),
                                gs_get("mobi.phosh.shell", "app-filter-mode", "filter again"),
                                gs_set(display, "overview-running-apps", "'bottom'"),
                                gs_set("mobi.phosh.shell", "app-filter-mode", "@as []"), dump("after")],
                      params={"what": "appearance"})
    assert r.value("app-grid-app-names") == "false" and r.value("app-grid-folder-dock") == "true"
    assert r.value("running") == "'bottom'" and r.value("filter") == "[]"
    assert r.value("running again") == "'top'" and r.value("filter again") == "['adaptive']"
    after = dict(switches(r.dump("after")))
    assert after["Open Apps at the Bottom"] is True and after["Mobile Apps Button"] is False


def test_app_overview_of_an_older_phosh_plinius(program, gcc_world):
    use(gcc_world, program, "old")
    r = gcc_world.run(program, [dump("page"), set_active(False, type="AdwSwitchRow", text="Mobile Apps Button"),
                                gs_get("sm.puri.phosh", "app-filter-mode", "filter")],
                      params={"what": "appearance"})
    # No running apps key; Phosh's button from the old schema
    assert [n["text"] for n in r.dump("page").find(type="AdwSwitchRow")] == [
        "App Names", "Folders at the Bottom", "Mobile Apps Button"]
    assert r.value("filter") == "[]"


@pytest.mark.parametrize("schemas", ["bare", "nodock"])
def test_no_app_overview_without_phosh_plinius(program, gcc_world, schemas):
    use(gcc_world, program, schemas)
    r = gcc_world.run(program, [call("set_up"), dump("page")], params={"what": "appearance"})
    assert r.value("set_up")["overview"] is False
    assert groups(r.dump("page")) == ["Style"]


def test_app_overview_without_phosh_s_filter(program, gcc_world):
    use(gcc_world, program, "nophosh")
    r = gcc_world.run(program, [dump("page")], params={"what": "appearance"})
    assert [n["text"] for n in r.dump("page").find(type="AdwSwitchRow")] == [
        "App Names", "Folders at the Bottom", "Open Apps at the Bottom"]


# ----- Search page -----

def test_home_screen_searches_on_top_of_search(program, gcc_world):
    use(gcc_world, program, "full")
    r = gcc_world.run(program, [call("set_up"), dump("page"),
                                set_active(False, type="AdwSwitchRow", text="Search from the Notch"),
                                set_active(False, type="AdwSwitchRow", text="Search in the App Overview"),
                                gs_get("org.plinius.display", "notch-search"),
                                gs_get("org.plinius.display", "app-grid-search")], params={"what": "search"})
    assert r.value("set_up")["search"] is True
    s = r.dump("page")
    assert groups(s) == ["Home Screen", "Search Results"]
    assert [(n["text"], n["subtitle"], n["active"]) for n in s.find(type="AdwSwitchRow")] == [
        ("Search from the Notch", "Tap the camera notch to search apps, settings, contacts and more", True),
        ("Search in the App Overview", "Search field above the apps", True)]
    assert r.value("notch-search") == "false" and r.value("app-grid-search") == "false"


def test_home_screen_of_an_older_phosh_plinius(program, gcc_world):
    use(gcc_world, program, "old")
    r = gcc_world.run(program, [dump("page")], params={"what": "search"})
    assert [n["text"] for n in r.dump("page").find(type="AdwSwitchRow")] == ["Search in the App Overview"]


def test_no_home_screen_without_phosh_plinius(program, gcc_world):
    use(gcc_world, program, "bare")
    r = gcc_world.run(program, [call("set_up"), dump("page")], params={"what": "search"})
    assert r.value("set_up")["search"] is False
    assert groups(r.dump("page")) == ["Search Results"]
