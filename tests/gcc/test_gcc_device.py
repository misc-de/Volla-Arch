# Device facts and phone settings (cc-plinius-device.c): the processor name and the scale base
# from the device package's /usr/share/plinius/device.conf, the installed RAM from the device
# tree's memory nodes (/proc/device-tree), the virtualization row About leaves out, the scale
# steps of the built-in panel with their labels, and the "Notch" row bound to phosh-plinius'
# org.plinius.display. Every path is in the program's root in tmp.

import pytest

from fake_schemas import no_schemas, schemas_without
from gcc_harness import (build_panel, call, dump, gcc_session, gcc_world, select,  # noqa: F401
                         settle)

GLUE = r'''
#include "gcc_driver.h"
#include "cc-plinius-device.h"

static GtkWidget *notch_row;
static gboolean has_notch;

void
gcc_test_setup (GccTest *t)
{
    GtkWidget *group = adw_preferences_group_new ();

    /* As the Display page: a combo row, hidden unless the file fills it */
    notch_row = adw_combo_row_new ();
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), notch_row);
    adw_preferences_page_add (t->page, ADW_PREFERENCES_GROUP (group));
    has_notch = cc_plinius_device_setup_notch_row (ADW_COMBO_ROW (notch_row));
    gtk_widget_set_visible (notch_row, has_notch);
}

static void
add_string_or_null (JsonBuilder *result, char *text)
{
    if (text)
        json_builder_add_string_value (result, text);
    else
        json_builder_add_null_value (result);
    g_free (text);
}

gboolean
gcc_test_call (GccTest *t, const char *name, JsonArray *args, JsonBuilder *result)
{
    if (g_str_equal (name, "has_notch")) {
        json_builder_add_boolean_value (result, has_notch);
        return TRUE;
    }
    if (g_str_equal (name, "processor")) {
        add_string_or_null (result, cc_plinius_device_get_processor ());
        return TRUE;
    }
    if (g_str_equal (name, "ram")) {
        json_builder_add_int_value (result, (gint64) cc_plinius_device_get_installed_ram ());
        return TRUE;
    }
    if (g_str_equal (name, "virt_hidden")) {
        const char *virt = json_array_get_null_element (args, 0) ? NULL : json_array_get_string_element (args, 0);

        json_builder_add_boolean_value (result, cc_plinius_device_is_virtualization_hidden (virt));
        return TRUE;
    }
    if (g_str_equal (name, "scales")) {
        GArray *scales = cc_plinius_device_get_scales (json_array_get_int_element (args, 0),
                                                       json_array_get_int_element (args, 1));

        if (!scales) {
            json_builder_add_null_value (result);
            return TRUE;
        }
        json_builder_begin_array (result);
        for (guint i = 0; i < scales->len; i++)
            json_builder_add_double_value (result, g_array_index (scales, double, i));
        json_builder_end_array (result);
        g_array_unref (scales);
        return TRUE;
    }
    if (g_str_equal (name, "scale_string")) {
        add_string_or_null (result, cc_plinius_device_make_scale_string (json_array_get_double_element (args, 0)));
        return TRUE;
    }
    return FALSE;
}
'''

CONF = "/usr/share/plinius/device.conf"
TREE = "/proc/device-tree"

ANSUZ = """\
[Device]
Processor=MediaTek Dimensity 7300 (4× Cortex-A78, 4× Cortex-A55)

[Display]
ScaleBase=1.5
"""


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    reps = [(f'"{CONF}"', f'"{{root}}{CONF}"'), (f'"{TREE}"', f'"{{root}}{TREE}"')]
    return build_panel(gcc_session, tmp_path_factory.mktemp("device"), "device",
                       ["cc-plinius-device.c"], GLUE,
                       replacements={"cc-plinius-device.c": reps})


class System:
    def __init__(self, root):
        self.root = root
        import shutil
        for d in list(root.iterdir()):
            shutil.rmtree(d) if d.is_dir() else d.unlink()

    def write(self, path, data):
        f = self.root / path.lstrip("/")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data.encode() if isinstance(data, str) else data)
        return f

    def conf(self, text):
        return self.write(CONF, text)

    def node(self, name, device_type=b"memory\0", reg=None):
        """A node of the device tree: device_type as the kernel shows it (with its NUL), reg."""
        if device_type is not None:
            self.write(f"{TREE}/{name}/device_type", device_type)
        else:
            (self.root / f"{TREE}/{name}".lstrip("/")).mkdir(parents=True, exist_ok=True)
        if reg is not None:
            self.write(f"{TREE}/{name}/reg", reg)


@pytest.fixture
def system(program):
    return System(program.root)


def cells(*values, width=1):
    """Big-endian cells: each value as `width` 32-bit cells."""
    out = b""
    for v in values:
        out += v.to_bytes(4 * width, "big")
    return out


GiB = 1 << 30


# ----- Processor -----

def test_processor_from_the_device_package(program, gcc_world, system):
    system.conf(ANSUZ)
    r = gcc_world.run(program, [call("processor")])
    assert r.value("processor") == "MediaTek Dimensity 7300 (4× Cortex-A78, 4× Cortex-A55)"
    assert not r.warnings()


@pytest.mark.parametrize("text", [None, "[Display]\nScaleBase=1.5\n"])
def test_no_processor_without_file_or_key(program, gcc_world, system, text):
    if text is not None:
        system.conf(text)
    r = gcc_world.run(program, [call("processor")])
    # About falls back to GNOME's own way then
    assert r.value("processor") is None
    assert not r.warnings()


def test_a_broken_file_warns_once_and_counts_as_none(program, gcc_world, system):
    system.conf("no key file\n")
    r = gcc_world.run(program, [call("processor"), call("scales", 1080, 2400, label="scales"),
                                call("scale_string", 3.0)])
    assert r.value("processor") is None
    assert r.value("scales") is None
    assert r.value("scale_string") is None
    # Read once for the whole program, so said once
    assert [m["message"] for m in r.warnings()] == [
        f"Could not read {program.root}{CONF}: Key file contains line “no key file” which is not "
        "a key-value pair, group, or comment"]


def test_the_file_is_read_once(program, gcc_world, system):
    f = system.conf(ANSUZ)
    r = gcc_world.run(program, [call("processor", label="first"),
                                {"op": "remove_file", "path": str(f)},
                                call("processor", label="second")])
    assert r.value("first") == r.value("second") == ANSUZ.splitlines()[1].split("=", 1)[1]


# ----- Installed RAM -----

def test_no_device_tree_no_ram(program, gcc_world, system):
    r = gcc_world.run(program, [call("ram")])
    # About shows MemTotal then
    assert r.value("ram") == 0


def test_ram_with_the_default_cells(program, gcc_world, system):
    # Without #address-cells / #size-cells: 2 and 1, as the device tree specification says
    system.node("memory@40000000", reg=cells(0, 0x40000000, 2 * GiB) + cells(1, 0, GiB))
    r = gcc_world.run(program, [call("ram")])
    assert r.value("ram") == 3 * GiB


def test_ram_adds_every_memory_node_with_64_bit_sizes(program, gcc_world, system):
    system.write(f"{TREE}/#address-cells", cells(2))
    system.write(f"{TREE}/#size-cells", cells(2))
    system.node("memory@40000000", reg=cells(0x40000000, 6 * GiB, width=2))
    # Two ranges in one node
    system.node("memory@200000000", reg=cells(0x200000000, GiB, 0x300000000, GiB // 2, width=2))
    r = gcc_world.run(program, [call("ram")])
    assert r.value("ram") == 7 * GiB + GiB // 2


def test_ram_leaves_out_what_is_no_memory(program, gcc_world, system):
    system.write(f"{TREE}/#address-cells", cells(1))
    system.write(f"{TREE}/#size-cells", cells(1))
    system.node("memory@40000000", reg=cells(0x40000000, GiB))
    # Not named memory*, though of that type
    system.node("reserved-memory", reg=cells(0x50000000, GiB))
    # Named memory* but of another type, or without a type
    system.node("memory-controller", device_type=b"controller\0", reg=cells(0, GiB))
    system.node("memory@0", device_type=None, reg=cells(0, GiB))
    # A memory node without reg
    system.node("memory@80000000")
    # A trailing part shorter than an entry
    system.node("memory@90000000", reg=cells(0x90000000, GiB // 4) + b"\0\0")
    # A plain file in the tree
    system.write(f"{TREE}/memory-name", "x")
    r = gcc_world.run(program, [call("ram")])
    assert r.value("ram") == GiB + GiB // 4


def test_cells_of_the_wrong_size_fall_back_to_the_defaults(program, gcc_world, system):
    # Three bytes are no cell: 2 address and 1 size cells stand
    system.write(f"{TREE}/#address-cells", b"\0\0\1")
    system.write(f"{TREE}/#size-cells", b"\0\0\0\2\0")
    system.node("memory@40000000", reg=cells(0, 0x40000000, 4 * GiB - 4096))
    r = gcc_world.run(program, [call("ram")])
    assert r.value("ram") == 4 * GiB - 4096


# ----- Virtualization -----

@pytest.mark.parametrize("virt, hidden", [("vm-other", True), ("kvm", False), ("none", False),
                                          ("", False), (None, False)])
def test_only_vm_other_is_hidden(program, gcc_world, system, virt, hidden):
    r = gcc_world.run(program, [call("virt_hidden", virt)])
    assert r.value("virt_hidden") is hidden


# ----- Scales -----

def whole_pixels(width, height, scale):
    return abs(width / scale - round(width / scale)) < 1e-9 and \
        abs(height / scale - round(height / scale)) < 1e-9


def test_scale_steps_give_whole_logical_pixels(program, gcc_world, system):
    system.conf(ANSUZ)
    r = gcc_world.run(program, [call("scales", 1080, 2400), call("scale_string", 1.5, label="100"),
                                call("scale_string", 1080 / 477, label="150"),
                                call("scale_string", 1080 / 414, label="175"),
                                call("scale_string", 3.0, label="200")])
    scales = r.value("scales")
    # 100 % … 200 % of 1.5: 1.5, 1.875 and 3 are whole already; 2.25 (480 px) and 2.625 move to the
    # nearest width whose height is whole too (477 and 414 logical pixels wide)
    assert scales == pytest.approx([1.5, 1.875, 1080 / 477, 1080 / 414, 3.0])
    assert all(whole_pixels(1080, 2400, s) for s in scales)
    assert [r.value(n) for n in ("100", "150", "175", "200")] == ["100 %", "150 %", "175 %", "200 %"]


def test_a_step_without_whole_pixels_nearby_stays_as_it_is(program, gcc_world, system):
    system.conf("[Display]\nScaleBase=2\n")
    # 1081 = 23 × 47: no logical width near any step gives a whole height
    r = gcc_world.run(program, [call("scales", 1081, 2400)])
    assert r.value("scales") == pytest.approx([2.0, 2.5, 3.0, 3.5, 4.0])


def test_the_larger_width_wins_a_tie(program, gcc_world, system):
    system.conf("[Display]\nScaleBase=1\n")
    # 10×15: whole heights at even logical widths; 100 % is 10 px; 125 % is 8 px; 150 % (6.67 → 7 px)
    # has 6 and 8 equally near, 8 comes first; 175 % (5.7 → 6 px) and 200 % (5 px → 4 or 6) likewise
    r = gcc_world.run(program, [call("scales", 10, 15)])
    assert r.value("scales") == pytest.approx([1.0, 1.25, 1.25, 10 / 6, 10 / 6])


def test_tiny_panels_never_divide_by_zero(program, gcc_world, system):
    system.conf("[Display]\nScaleBase=3\n")
    # 1 px wide: every step is more than twice the width, rounds to 0 logical pixels
    r = gcc_world.run(program, [call("scales", 1, 1)])
    assert r.value("scales") == pytest.approx([1.0] * 5)


@pytest.mark.parametrize("text", [None, "[Device]\nProcessor=x\n", "[Display]\nScaleBase=0\n",
                                  "[Display]\nScaleBase=-1.5\n", "[Display]\nScaleBase=big\n"])
def test_no_steps_and_no_labels_without_a_base(program, gcc_world, system, text):
    if text is not None:
        system.conf(text)
    r = gcc_world.run(program, [call("scales", 1080, 2400), call("scale_string", 1.5)])
    # The compositor's own steps and labels apply
    assert r.value("scales") is None
    assert r.value("scale_string") is None


@pytest.mark.parametrize("size", [(0, 2400), (1080, 0), (-1, 2400)])
def test_no_steps_for_a_mode_without_size(program, gcc_world, system, size):
    system.conf(ANSUZ)
    r = gcc_world.run(program, [call("scales", *size)])
    assert r.value("scales") is None


def test_labels_round_to_5_percent(program, gcc_world, system):
    system.conf(ANSUZ)
    r = gcc_world.run(program, [call("scale_string", 1.5 * 1.124, label="a"),
                                call("scale_string", 1.5 * 1.126, label="b"),
                                call("scale_string", 1.5 * 0.5, label="c")])
    assert [r.value(n) for n in "abc"] == ["110 %", "115 %", "50 %"]


# ----- Notch -----

def notch(screen):
    return screen.one(type="AdwComboRow", text="Notch")


def test_notch_row_follows_the_setting(program, gcc_world, system):
    schema = {"op": "gsettings_get", "schema": "org.plinius.display", "key": "notch"}
    r = gcc_world.run(program, [
        settle(), call("has_notch"), dump("start"),
        select(index=3, type="AdwComboRow"), settle(), {**schema, "label": "after select"},
        {"op": "gsettings_set", "schema": "org.plinius.display", "key": "notch", "value": "'right'"},
        settle(), dump("after set"),
    ])
    assert r.value("has_notch") is True
    row = notch(r.dump("start"))
    assert row["subtitle"] == "Top bar icons make room for the camera"
    assert row["items"] == ["Automatic", "None", "Left", "Center", "Right"]
    # The default: the device's panel data
    assert row["selected_text"] == "Automatic"
    assert r.value("after select") == "'center'"
    assert notch(r.dump("after set"))["selected_text"] == "Right"
    assert not r.warnings()


@pytest.mark.parametrize("value, shown", [("'none'", "None"), ("'left'", "Left"), ("'auto'", "Automatic")])
def test_notch_row_shows_each_value(program, gcc_world, system, value, shown):
    r = gcc_world.run(program, [
        {"op": "gsettings_set", "schema": "org.plinius.display", "key": "notch", "value": value},
        {"op": "setup"}, settle(), dump("main")])
    assert notch(r.dump("main"))["selected_text"] == shown


@pytest.mark.parametrize("index, value", [(0, "'auto'"), (1, "'none'"), (2, "'left'"), (4, "'right'")])
def test_notch_row_writes_each_value(program, gcc_world, system, index, value):
    r = gcc_world.run(program, [
        settle(), select(index=index, type="AdwComboRow"), settle(),
        {"op": "gsettings_get", "schema": "org.plinius.display", "key": "notch"}])
    assert r.value("gsettings_get") == value


def test_without_phosh_plinius_no_notch_row(program, gcc_world, system, tmp_path):
    gcc_world.extra_env["GSETTINGS_SCHEMA_DIR"] = str(
        schemas_without(gcc_world.session, tmp_path / "schemas", "org.plinius.display"))
    r = gcc_world.run(program, [settle(), call("has_notch"), dump("main")])
    assert r.value("has_notch") is False
    assert not r.dump("main").has(type="AdwComboRow")
    assert not r.warnings()


def test_without_any_schema_no_notch_row(program, gcc_world, system, tmp_path):
    gcc_world.extra_env["GSETTINGS_SCHEMA_DIR"] = str(no_schemas(tmp_path / "schemas"))
    r = gcc_world.run(program, [settle(), call("has_notch")])
    assert r.value("has_notch") is False
