# Battery Condition and the charging options on the Power page (cc-plinius-battery.c): what the
# condition group shows for a battery's UPower facts (an UpDevice made in the glue with the
# properties UPower would report), and plinius-charge (org.plinius.Charge, interface from its C
# source) as a fake on the test's "system" bus.

import pytest

from conftest import PKG
from gcc_harness import build_panel, call, dump, gcc_session, gcc_world, interface_xml, settle  # noqa: F401

CHARGE = "org.plinius.Charge"
CHARGE_PATH = "/org/plinius/Charge"
CHARGE_XML = interface_xml(PKG / "plinius-charge" / "plinius-charge.c", CHARGE)

GLUE = r'''
#include "gcc_driver.h"
#include "cc-plinius-battery.h"

static GtkWidget *group;
static GDBusProxy *charge;

void
gcc_test_setup (GccTest *t)
{
    group = cc_plinius_battery_group_new ();
    gcc_test_add (t, group);
    gcc_test_add (t, cc_plinius_charge_fast_row_new ());
}

/* An UpDevice with the properties of a JSON object, as UPower would fill them */
static UpDevice *
device_from (JsonNode *node)
{
    UpDevice *device;
    JsonObject *props;
    g_autoptr(GList) names = NULL;

    if (node == NULL || JSON_NODE_HOLDS_NULL (node))
        return NULL;
    device = up_device_new ();
    /* UPower's "unknown" (the property's default; a new UpDevice holds 0 until set) */
    g_object_set (device, "charge-cycles", -1, NULL);
    props = json_node_get_object (node);
    names = json_object_get_members (props);
    for (GList *l = names; l; l = l->next) {
        GParamSpec *pspec = g_object_class_find_property (G_OBJECT_GET_CLASS (device), l->data);
        g_auto(GValue) value = G_VALUE_INIT;
        JsonNode *v = json_object_get_member (props, l->data);

        g_assert (pspec != NULL);
        g_value_init (&value, pspec->value_type);
        if (pspec->value_type == G_TYPE_DOUBLE)
            g_value_set_double (&value, json_node_get_double (v));
        else if (pspec->value_type == G_TYPE_INT)
            g_value_set_int (&value, json_node_get_int (v));
        else if (pspec->value_type == G_TYPE_UINT)
            g_value_set_uint (&value, json_node_get_int (v));
        else if (G_TYPE_IS_ENUM (pspec->value_type))
            g_value_set_enum (&value, json_node_get_int (v));
        else
            g_assert_not_reached ();
        g_object_set_property (G_OBJECT (device), l->data, &value);
    }
    return device;
}

gboolean
gcc_test_call (GccTest *t, const char *name, JsonArray *args, JsonBuilder *result)
{
    if (g_str_equal (name, "update")) {
        g_autoptr(UpDevice) device = device_from (json_array_get_element (args, 0));
        double *full;

        cc_plinius_battery_group_update (group, device);
        full = g_object_get_data (G_OBJECT (group), "energy-full-wh");
        if (full)
            json_builder_add_double_value (result, *full);
        else
            json_builder_add_null_value (result);
        return TRUE;
    }
    if (g_str_equal (name, "proxy")) {
        g_clear_object (&charge);
        charge = cc_plinius_charge_proxy_new ();
        json_builder_add_boolean_value (result, charge != NULL);
        return TRUE;
    }
    if (g_str_equal (name, "get")) {
        json_builder_add_boolean_value (result, cc_plinius_charge_get (charge, json_array_get_string_element (args, 0)));
        return TRUE;
    }
    if (g_str_equal (name, "level")) {
        json_builder_add_int_value (result, cc_plinius_charge_get_level (charge, json_array_get_string_element (args, 0)));
        return TRUE;
    }
    if (g_str_equal (name, "set")) {
        cc_plinius_charge_set (charge, json_array_get_string_element (args, 0), json_array_get_boolean_element (args, 1));
        json_builder_add_boolean_value (result, cc_plinius_charge_get (charge, json_array_get_string_element (args, 0)));
        return TRUE;
    }
    if (g_str_equal (name, "subtitle")) {
        g_autofree char *text = cc_plinius_charge_limit_subtitle (charge);

        json_builder_add_string_value (result, text);
        return TRUE;
    }
    return FALSE;
}
'''

# UpDeviceState and UpDeviceTechnology as upower-glib numbers them
CHARGING, DISCHARGING, EMPTY, FULLY_CHARGED, PENDING_CHARGE = 1, 2, 3, 4, 5
LI_ION, LI_POLYMER, LI_IRON_PHOSPHATE, LEAD_ACID = 1, 2, 3, 4


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    return build_panel(gcc_session, tmp_path_factory.mktemp("battery"), "battery",
                       ["cc-plinius-battery.c"], GLUE, pkgs=("upower-glib",))


def condition(screen):
    """{row title: shown value} of the Battery Condition group (rows that show only)."""
    group = screen.within(type="AdwPreferencesGroup", text="Battery Condition")
    out = {}
    for row in group.find(type="AdwActionRow"):
        labels = [c["text"] for c in row.get("children", []) if c["type"] == "GtkLabel"]
        out[row["text"]] = labels[0] if labels else ""
    return out


def charge(world, own=True, **props):
    world.service("system", CHARGE, CHARGE_PATH, CHARGE_XML, own=own,
                  properties={k: v for k, v in props.items() if k != "set_errors"},
                  set_errors=props.get("set_errors"))


def test_without_battery_nothing_shows(program, gcc_world):
    r = gcc_world.run(program, [call("update", None), dump("page")])
    s = r.dump("page")
    assert not s.has(type="AdwPreferencesGroup", text="Battery Condition")
    # The fast charging row of the charging options is there on its own
    assert s.one(type="AdwSwitchRow", text="Fast Charging")["subtitle"] == (
        "Charges faster with a suitable charger. The battery gets warmer.")
    assert r.value("update") is None
    assert not r.warnings()


def test_all_facts_of_a_charging_battery(program, gcc_world):
    r = gcc_world.run(program, [
        call("update", {"capacity": 91.4, "energy-full": 16.04, "energy-full-design": 17.5,
                        "charge-cycles": 312, "temperature": 31.6, "voltage": 4.123,
                        "energy-rate": 7.25, "state": CHARGING, "technology": LI_ION}),
        dump("page")])
    s = r.dump("page")
    assert condition(s) == {
        "Health": "91 %",
        "Full Charge": "16.0 of 17.5 Wh",
        "Charge Cycles": "312",
        "Temperature": "32 °C",
        "Voltage": "4.12 V",
        "Power": "7.2 W, Charging",
        "Battery Type": "Lithium-ion",
    }
    assert s.one(type="AdwActionRow", text="Health")["subtitle"] == (
        "Capacity compared to when the battery was new")
    # The full energy for the Battery Usage page
    assert r.value("update") == pytest.approx(16.04)


@pytest.mark.parametrize("state, technology, power, kind", [
    (DISCHARGING, LI_POLYMER, "Discharging", "Lithium polymer"),
    (FULLY_CHARGED, LI_IRON_PHOSPHATE, "Fully charged", "Lithium iron phosphate"),
    (PENDING_CHARGE, LEAD_ACID, "Charging paused", None),
    (EMPTY, 0, None, None),
])
def test_state_without_rate_and_technologies(program, gcc_world, state, technology, power, kind):
    r = gcc_world.run(program, [
        call("update", {"capacity": 80.0, "state": state, "technology": technology}), dump("page")])
    shown = condition(r.dump("page"))
    assert shown.get("Power") == power
    assert shown.get("Battery Type") == kind


def test_facts_the_battery_does_not_report_are_left_out(program, gcc_world):
    # No capacity, no design energy, no cycle count (-1), no temperature, no voltage
    r = gcc_world.run(program, [
        call("update", {"capacity": 0.0, "energy-full": 12.0, "energy-full-design": 0.0,
                        "charge-cycles": -1, "temperature": 0.0, "voltage": 0.0,
                        "energy-rate": 3.0, "state": 0, "technology": 0}),
        dump("page")])
    assert condition(r.dump("page")) == {}
    # The group itself shows: there is a battery
    assert r.dump("page").has(type="AdwPreferencesGroup", text="Battery Condition")
    assert r.value("update") == pytest.approx(12.0)


def test_a_temperature_rounding_to_zero_and_zero_cycles_show(program, gcc_world):
    # UPower reports no temperature as 0 (its property does not go below); 0.4 °C is one
    r = gcc_world.run(program, [
        call("update", {"charge-cycles": 0, "temperature": 0.4}), dump("page")])
    assert condition(r.dump("page")) == {"Charge Cycles": "0", "Temperature": "0 °C"}


def test_update_hides_what_went_and_the_group_with_the_battery(program, gcc_world):
    r = gcc_world.run(program, [
        call("update", {"capacity": 90.0, "voltage": 4.0}, label="first"), dump("first shown"),
        call("update", {"capacity": 89.0}, label="second"), dump("second shown"),
        call("update", None, label="gone"), dump("gone shown")])
    assert condition(r.dump("first shown")) == {"Health": "90 %", "Voltage": "4.00 V"}
    assert condition(r.dump("second shown")) == {"Health": "89 %"}
    assert not r.dump("gone shown").has(type="AdwPreferencesGroup", text="Battery Condition")
    # The full energy of the last battery stays for the usage page
    assert r.value("second") == 0


def test_charge_options_from_the_service(program, gcc_world):
    charge(gcc_world, HasLimit="true", Limit="true", FastCharge="false", LimitStop="uint32 80",
           LimitStart="uint32 75")
    r = gcc_world.run(program, [
        call("proxy"), call("get", "Limit", label="limit"), call("get", "FastCharge", label="fast"),
        call("level", "LimitStop", label="stop"), call("subtitle")])
    assert r.value("proxy") is True
    assert r.value("limit") is True and r.value("fast") is False
    assert r.value("stop") == 80
    assert r.value("subtitle") == "Charging stops at 80 % and starts again at 75 %"


def test_values_of_another_type_count_as_unset(program, gcc_world):
    charge(gcc_world, HasLimit="true", LimitStop="uint32 80")
    r = gcc_world.run(program, [call("proxy"), call("get", "LimitStop", label="bool"),
                                call("level", "HasLimit", label="level"),
                                call("get", "NoSuchProperty", label="missing")])
    assert r.value("bool") is False and r.value("level") == 0 and r.value("missing") is False


def test_without_the_service_there_are_no_options(program, gcc_world):
    charge(gcc_world, own=False, HasLimit="true")
    r = gcc_world.run(program, [call("proxy"), call("get", "Limit"), call("level", "LimitStop"),
                                call("subtitle")])
    assert r.value("proxy") is False
    assert r.value("get") is False and r.value("level") == 0
    assert r.value("subtitle") == "Charging stops at 0 % and starts again at 0 %"


def test_without_a_system_bus_there_are_no_options(program, gcc_world):
    r = gcc_world.run(program, [call("proxy")], system_bus=False)
    assert r.value("proxy") is False


def test_setting_an_option_goes_to_the_service_and_shows_at_once(program, gcc_world):
    charge(gcc_world, HasLimit="true", FastCharge="false")
    r = gcc_world.run(program, [call("proxy"), call("set", "FastCharge", True), settle()])
    assert r.value("set") is True
    assert [c["args"] for c in gcc_world.calls(CHARGE, "Set")] == [[CHARGE, "FastCharge", True]]
    assert not r.warnings()


def test_a_refused_option_stays_as_it_was(program, gcc_world):
    charge(gcc_world, HasLimit="true", Limit="false",
           set_errors={"Limit": ["org.freedesktop.DBus.Error.AccessDenied", "Not allowed"]})
    r = gcc_world.run(program, [call("proxy"), call("set", "Limit", True)])
    assert r.value("set") is False
    assert [m["message"] for m in r.warnings()] == [
        "Setting Limit failed: GDBus.Error:org.freedesktop.DBus.Error.AccessDenied: Not allowed"]
