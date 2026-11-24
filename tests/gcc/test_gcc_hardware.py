# Hardware in System (cc-plinius-hardware.c): the page of the phone's chips and firmware versions,
# made from the key file the device package collects at boot (/run/plinius-hardware, in the
# program's root in tmp), the row on the System page that leads there, and "Copy".
#
# The glue stands in for the System panel: a hidden "Hardware" row with the action
# navigation.push, set up only when the page was added (as cc-system-panel.c does).
# The template's row has the action but no target until the file sets one: GTK says so once
# (TEMPLATE_WARNING), the same in GNOME Settings, it is not this file's.

import pytest

from gcc_harness import (Screen, back, build_panel, call, click, dump, gcc_session, gcc_world,  # noqa: F401
                         settle, wait_for)

GLUE = r'''
#include "gcc_driver.h"
#include "cc-list-row.h"
#include "cc-plinius-hardware.h"

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

    added = cc_plinius_hardware_add_page (t->panel);
    if (added)
        cc_plinius_hardware_row_setup (ADW_ACTION_ROW (row));
}

gboolean
gcc_test_call (GccTest *t, const char *name, JsonArray *args, JsonBuilder *result)
{
    if (g_str_equal (name, "added")) {
        json_builder_add_boolean_value (result, added);
        return TRUE;
    }
    if (g_str_equal (name, "clipboard")) {
        /* What "Copy" put there (the program's own clipboard: its content is local) */
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

FILE = "/run/plinius-hardware"
COLLECTOR = "/usr/lib/plinius/plinius-hardware-info"

FULL = """\
[soc]
Name=MediaTek Dimensity 6080

[gpu]
Name=Mali-G57 MC2
Version=r49p0

[storage]
Name=UFS 2.2
Firmware=0300

[touch]
Name=Focaltech FT3683G
Firmware=0x22

[preloader]
Version=1.0
Date=2024-05-17

[bootloader]
Version=lk-2.0
State=unlocked
Slot=b

[verified-boot]
State=orange

[modem]
Name=MediaTek MT6835
Version=MOLY.NR17.R1.MP1
Date=2024-06-02

[vendor]
Version=13
Patch=2024-07-05

[unknown-chip]
Name=Not Shown

[nfc]
Comment=no key this page knows
"""

# The text "Copy" puts on the clipboard: every row as "title: subtitle", sections in order
FULL_TEXT = """\
System on Chip: MediaTek Dimensity 6080
Graphics: Mali-G57 MC2 · r49p0
Storage: UFS 2.2 · Firmware 0300
Touchscreen: Focaltech FT3683G · Firmware 0x22
Preloader: 1.0 · 2024-05-17
Bootloader: lk-2.0 · Bootloader unlocked · Slot b
Verified Boot: orange
Modem: MediaTek MT6835 · MOLY.NR17.R1.MP1 · 2024-06-02
Vendor: 13 · Security patch 2024-07-05
"""


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    reps = [(f'"{FILE}"', f'"{{root}}{FILE}"'), (f'"{COLLECTOR}"', f'"{{root}}{COLLECTOR}"')]
    return build_panel(gcc_session, tmp_path_factory.mktemp("hardware"), "hardware",
                       ["cc-plinius-hardware.c"], GLUE,
                       replacements={"cc-plinius-hardware.c": reps})


class System:
    def __init__(self, root):
        self.root = root
        self.clear()

    def path(self, p):
        return self.root / p.lstrip("/")

    def clear(self):
        import shutil
        for d in list(self.root.iterdir()):
            shutil.rmtree(d) if d.is_dir() else d.unlink()

    def write(self, p, text, mode=0o644):
        f = self.path(p)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        f.chmod(mode)
        return f


@pytest.fixture
def system(program):
    return System(program.root)


def groups(screen):
    """{group title: (description, [(row title, subtitle)])} of the Hardware page."""
    page = screen.within(type="AdwNavigationPage", tag="plinius-hardware")
    out = {}
    for g in page.find(type="AdwPreferencesGroup"):
        rows = [(r.get("text"), r.get("subtitle")) for r in Screen(g.get("children", [])).find(type="AdwActionRow")]
        out[g.get("text")] = (g.get("subtitle"), rows)
    return out


def open_page():
    return [settle(), click(type="CcListRow", text="Hardware"),
            wait_for(type="AdwNavigationPage", tag="plinius-hardware")]


def test_without_file_or_collector_no_row_and_no_page(program, gcc_world, system):
    r = gcc_world.run(program, [settle(), call("added"), dump("main")])
    assert r.value("added") is False
    s = r.dump("main")
    # The row stays hidden and unset: nothing leads to a page that is not there
    assert not s.has(text="Hardware")
    assert not s.has(text="Unset Row")
    assert not r.warnings(ignore=[TEMPLATE_WARNING])


def test_a_collector_that_cannot_run_is_no_reason_for_the_row(program, gcc_world, system):
    system.write(COLLECTOR, "#!/bin/sh\n", 0o644)
    r = gcc_world.run(program, [settle(), call("added")])
    assert r.value("added") is False


def test_collector_without_file_yet_says_not_collected(program, gcc_world, system):
    system.write(COLLECTOR, "#!/bin/sh\n", 0o755)
    r = gcc_world.run(program, [settle(), call("added"), dump("main"), *open_page(), dump("page")])
    assert r.value("added") is True
    row = r.dump("main").one(type="CcListRow", text="Hardware")
    assert row["subtitle"] == "Chips and firmware versions"
    assert row["action"] == "navigation.push" and row["target"] == "'plinius-hardware'"
    s = r.dump("page")
    assert s.one(type="AdwNavigationPage", tag="plinius-hardware")["text"] == "Hardware"
    assert groups(s) == {None: (None, [("Not collected yet",
                                        "The versions are read once after the phone starts.")])}
    # Nothing to copy
    assert not s.has(type="AdwButtonRow")
    # A missing file is no warning: the collector has not run yet
    assert not r.warnings()


def test_versions_by_section_in_the_order_of_the_table(program, gcc_world, system):
    system.write(FILE, FULL)
    r = gcc_world.run(program, [*open_page(), dump("page")])
    s = r.dump("page")
    g = groups(s)
    assert list(g) == ["Components", "Boot and Security", "Radios", "Android Base", None]
    assert g["Components"] == (None, [
        ("System on Chip", "MediaTek Dimensity 6080"),
        ("Graphics", "Mali-G57 MC2 · r49p0"),
        ("Storage", "UFS 2.2 · Firmware 0300"),
        ("Touchscreen", "Focaltech FT3683G · Firmware 0x22"),
    ])
    assert g["Boot and Security"] == ("Firmware that starts before Linux", [
        ("Preloader", "1.0 · 2024-05-17"),
        ("Bootloader", "lk-2.0 · Bootloader unlocked · Slot b"),
        ("Verified Boot", "orange"),
    ])
    assert g["Radios"] == (None, [("Modem", "MediaTek MT6835 · MOLY.NR17.R1.MP1 · 2024-06-02")])
    assert g["Android Base"] == ("Vendor drivers that Linux uses through the Android container",
                                 [("Vendor", "13 · Security patch 2024-07-05")])
    # Groups the page does not know, and known groups without a known key, are left out
    assert not s.has(contains="Not Shown")
    assert not s.has(type="AdwActionRow", text="NFC")
    # The rows are facts: property style, nothing to press
    row = s.one(type="AdwActionRow", text="Graphics")
    assert "property" in row.get("classes", [])
    assert s.one(type="AdwButtonRow", text="Copy")
    assert not r.warnings()


@pytest.mark.parametrize("keys, subtitle", [
    ("State=locked", "Bootloader locked"),
    ("State=unlocked", "Bootloader unlocked"),
    ("State=green", "green"),
    ("Slot=a", "Slot a"),
    ("Firmware=17", "Firmware 17"),
    ("Patch=2025-01-01", "Security patch 2025-01-01"),
    ("Date=2025-02-03", "2025-02-03"),
    # Every key, in the order the subtitle joins them, whatever the file's order
    ("Patch=P\nDate=D\nSlot=S\nState=locked\nFirmware=F\nVersion=V\nName=N",
     "N · V · Firmware F · Bootloader locked · Slot S · D · Security patch P"),
])
def test_subtitle_of_each_key(program, gcc_world, system, keys, subtitle):
    system.write(FILE, f"[bootloader]\n{keys}\n")
    r = gcc_world.run(program, [*open_page(), dump("page")])
    assert groups(r.dump("page"))["Boot and Security"][1] == [("Bootloader", subtitle)]


def test_both_trusted_os_groups_and_every_known_entry(program, gcc_world, system):
    names = ["soc", "gpu", "storage", "connectivity", "touch", "nfc", "wireless-charging", "audio",
             "preloader", "bootloader", "atf", "tee", "trusty", "hypervisor", "spm",
             "verified-boot", "modem", "wifi", "bluetooth", "vendor", "bsp", "vendor-modules",
             "gpu-driver", "audio-params", "system"]
    # The file's order is not the page's
    system.write(FILE, "".join(f"[{n}]\nName={n}\n" for n in reversed(names)))
    r = gcc_world.run(program, [*open_page(), dump("page")])
    g = groups(r.dump("page"))
    assert [t for t, _ in g["Components"][1]] == [
        "System on Chip", "Graphics", "Storage", "Wi-Fi and Bluetooth", "Touchscreen", "NFC",
        "Wireless Charging", "Audio"]
    assert g["Boot and Security"][1] == [
        ("Preloader", "preloader"), ("Bootloader", "bootloader"), ("Trusted Firmware-A", "atf"),
        ("Trusted OS", "tee"), ("Trusted OS", "trusty"), ("Hypervisor", "hypervisor"),
        ("Power Management", "spm"), ("Verified Boot", "verified-boot")]
    assert [t for t, _ in g["Radios"][1]] == ["Modem", "Wi-Fi", "Bluetooth"]
    assert [t for t, _ in g["Android Base"][1]] == [
        "Vendor", "MediaTek Software", "Vendor Kernel Modules", "Graphics Driver",
        "Audio Parameters", "Android System"]


def test_copy_puts_every_row_on_the_clipboard(program, gcc_world, system):
    system.write(FILE, FULL)
    r = gcc_world.run(program, [*open_page(), click(type="AdwButtonRow", text="Copy"),
                                wait_for(type="GtkLabel", text="Copied to clipboard"),
                                call("clipboard")])
    assert r.value("clipboard") == FULL_TEXT
    assert not r.warnings()


def test_a_file_without_known_groups_shows_nothing_to_copy(program, gcc_world, system):
    system.write(FILE, "[unknown]\nName=x\n")
    r = gcc_world.run(program, [*open_page(), dump("page")])
    s = r.dump("page")
    assert groups(s) == {}
    assert not s.has(type="AdwButtonRow")


def test_a_broken_file_warns_and_says_not_collected(program, gcc_world, system):
    system.write(FILE, "this is no key file\n")
    r = gcc_world.run(program, [*open_page(), dump("page")])
    assert groups(r.dump("page")) == {None: (None, [(
        "Not collected yet", "The versions are read once after the phone starts.")])}
    assert [m["message"] for m in r.warnings()] == [
        f"Could not read {program.root}{FILE}: Key file contains line “this is no key file” which "
        "is not a key-value pair, group, or comment"]


def test_the_file_is_read_again_on_every_showing(program, gcc_world, system):
    f = system.write(COLLECTOR, "#!/bin/sh\n", 0o755)
    hw = program.root / FILE.lstrip("/")
    hw.parent.mkdir(parents=True)
    r = gcc_world.run(program, [
        *open_page(), dump("before"), back(), settle(),
        # The collector finished while Settings was open
        {"op": "write_file", "path": str(hw), "text": "[soc]\nName=First\n"},
        click(type="CcListRow", text="Hardware"), settle(), dump("first"), back(), settle(),
        {"op": "write_file", "path": str(hw), "text": "[gpu]\nName=Second\n"},
        click(type="CcListRow", text="Hardware"), settle(), dump("second"),
    ])
    assert f.exists()
    assert list(groups(r.dump("before"))) == [None]
    assert groups(r.dump("first")) == {"Components": (None, [("System on Chip", "First")]),
                                       None: (None, [])}
    # The rows of before are gone, not added to
    assert groups(r.dump("second")) == {"Components": (None, [("Graphics", "Second")]),
                                        None: (None, [])}


def test_copy_after_a_new_showing_has_only_the_new_text(program, gcc_world, system):
    hw = system.write(FILE, "[soc]\nName=Old\n")
    r = gcc_world.run(program, [
        *open_page(), back(), settle(),
        {"op": "write_file", "path": str(hw), "text": "[gpu]\nName=New\n"},
        click(type="CcListRow", text="Hardware"), settle(),
        click(type="AdwButtonRow", text="Copy"), settle(), call("clipboard"),
    ])
    assert r.value("clipboard") == "Graphics: New\n"
