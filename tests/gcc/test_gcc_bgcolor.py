# The background color on the Style page (cc-plinius-bgcolor.c): nine dark presets and a color
# wheel (GTK's color editor on a page of its own, from cc-plinius-accent.c), written as GNOME's
# primary-color with color-shading-type 'solid' of the background and of the lock screen.

import pytest

from gcc_harness import (build_panel, call, click, dump, enter, gcc_session, gcc_world,  # noqa: F401
                         set_text, settle)

GLUE = r'''
#include "gcc_driver.h"
#include "cc-plinius-bgcolor.h"
#include "cc-plinius-accent.h"

static GtkWidget *group, *box;

/* The accent box above, as on the Style page: GNOME's nine and the custom wheel */
static void
accent_group (GccTest *t)
{
    GtkWidget *g = adw_preferences_group_new ();
    GtkWidget *accent = gtk_box_new (GTK_ORIENTATION_HORIZONTAL, 2);

    gtk_widget_set_name (accent, "accent-box");
    for (int i = 0; i < 9; i++) {
        GtkWidget *button = gtk_toggle_button_new ();

        g_object_set_data (G_OBJECT (button), "accent-color", GINT_TO_POINTER (i));
        if (i)
            gtk_toggle_button_set_group (GTK_TOGGLE_BUTTON (button),
                                         GTK_TOGGLE_BUTTON (gtk_widget_get_first_child (accent)));
        gtk_box_append (GTK_BOX (accent), button);
    }
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (g), accent);
    adw_preferences_page_add (t->page, ADW_PREFERENCES_GROUP (g));
    cc_plinius_accent_setup (accent);
}

static void
bgcolor_group (GccTest *t)
{
    group = adw_preferences_group_new ();
    box = gtk_box_new (GTK_ORIENTATION_HORIZONTAL, 2);
    gtk_widget_set_name (box, "bgcolor-box");
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), box);
    adw_preferences_page_add (t->page, ADW_PREFERENCES_GROUP (group));
    cc_plinius_bgcolor_setup (ADW_PREFERENCES_GROUP (group), box);
}

void
gcc_test_setup (GccTest *t)
{
    if (gcc_test_param (t, "accent"))
        accent_group (t);
    bgcolor_group (t);
}

gboolean
gcc_test_call (GccTest *t, const char *name, JsonArray *args, JsonBuilder *result)
{
    if (g_str_equal (name, "drop")) {
        /* The Style page goes: its group, and the box's data with it */
        adw_preferences_page_remove (t->page, ADW_PREFERENCES_GROUP (group));
        json_builder_add_null_value (result);
        return TRUE;
    }
    if (g_str_equal (name, "again")) {
        /* The Style page made anew (opened again) */
        bgcolor_group (t);
        json_builder_add_null_value (result);
        return TRUE;
    }
    if (g_str_equal (name, "wheel_ring")) {
        /* The accent color the wheel's style gives it (its ring) */
        GtkWidget *wheel = gtk_widget_get_last_child (box);
        static GtkCssProvider *probe;
        GdkRGBA rgba;

        /* The variable the file sets, made visible as the wheel's text color */
        if (!probe) {
            probe = gtk_css_provider_new ();
            gtk_css_provider_load_from_string (probe, ".plinius-bgcolor-custom { color: var(--accent-bg-color); }");
            gtk_style_context_add_provider_for_display (gdk_display_get_default (), GTK_STYLE_PROVIDER (probe),
                                                        GTK_STYLE_PROVIDER_PRIORITY_USER);
        }
        gtk_widget_get_color (wheel, &rgba);
        json_builder_add_string_value (result, gdk_rgba_to_string (&rgba));
        return TRUE;
    }
    return FALSE;
}
'''

PRESETS = [("Black", "#000000"), ("Arch Blue", "#1793d1"), ("Midnight Blue", "#1b2a47"),
           ("Deep Teal", "#0e5a63"), ("Forest Green", "#2b5b3a"), ("Plum", "#4f2a5e"),
           ("Burgundy", "#6e1f2f"), ("Mocha", "#5b4033"), ("Slate Gray", "#3b4552")]
BG = "org.gnome.desktop.background"
LOCK = "org.gnome.desktop.screensaver"
SHEET_NOISE = (r"^AdwBottomSheet .* exceeds AdwBreakpointBin width",
               r"^gtk_snapshot_append_texture: assertion 'GDK_IS_TEXTURE \(texture\)' failed")


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    return build_panel(gcc_session, tmp_path_factory.mktemp("bgcolor"), "bgcolor",
                       ["cc-plinius-bgcolor.c", "cc-plinius-accent.c"], GLUE,
                       pkgs=("gsettings-desktop-schemas",))


def gset(schema, key, value):
    return {"op": "gsettings_set", "schema": schema, "key": key, "value": value}


def keys(label):
    return [{"op": "gsettings_get", "schema": s, "key": k, "label": f"{label}-{s}-{k}"}
            for s in (BG, LOCK) for k in ("primary-color", "color-shading-type")]


def values(r, label):
    return {(s, k): r.value(f"{label}-{s}-{k}") for s in (BG, LOCK) for k in ("primary-color", "color-shading-type")}


def solid(color):
    return {(s, k): (f"'{color}'" if k == "primary-color" else "'solid'")
            for s in (BG, LOCK) for k in ("primary-color", "color-shading-type")}


def buttons(screen):
    box = screen.within(type="GtkBox", name="bgcolor-box")
    return [(b.get("tooltip"), b.get("active")) for b in box.find(type="GtkToggleButton")]


def active(screen):
    return [t for t, a in buttons(screen) if a]


def pick(hex_color):
    return [set_text(hex_color, type="GtkEntry", within={"type": "AdwDialog"}),
            enter(type="GtkEntry", within={"type": "AdwDialog"}), settle(),
            click(type="GtkButton", text="Select"), settle()]


def test_presets_and_wheel(program, gcc_world):
    r = gcc_world.run(program, [settle(), dump("main"), call("wheel_ring")])
    s = r.dump("main")
    group = s.one(type="AdwPreferencesGroup")
    assert (group["text"], group["subtitle"]) == ("Background Color", "Shown when no background is selected")
    # GNOME's default blue is none of the presets: the wheel is the active one, its ring in it
    assert buttons(s) == [(n, False) for n, _c in PRESETS] + [("Custom Color", True)]
    preset = s.one(type="GtkToggleButton", tooltip="Plum")
    assert preset["classes"] == ["plinius-bgcolor", "plinius-bgcolor-5"]
    wheel = s.one(type="GtkToggleButton", tooltip="Custom Color")
    assert wheel["classes"] == ["plinius-custom", "plinius-bgcolor-custom"]
    assert r.value("wheel_ring") == "rgb(2,60,136)"
    assert not r.warnings()


@pytest.mark.parametrize("value, shown", [("'#1793d1'", ["Arch Blue"]), ("'#1793D1'", ["Arch Blue"]),
                                          ("'rgb(0,0,0)'", ["Black"]), ("'#123456'", ["Custom Color"]),
                                          # Unreadable: GNOME's default, none of the presets
                                          ("'bogus'", ["Custom Color"])])
def test_active_button_follows_the_color(program, gcc_world, value, shown):
    r = gcc_world.run(program, [gset(BG, "primary-color", value), {"op": "setup"}, settle(), dump("main"),
                                call("wheel_ring")])
    assert active(r.dump("main")) == shown
    if value == "'bogus'":
        assert r.value("wheel_ring") == "rgb(2,60,136)"


def test_preset_sets_both_screens_in_one_color(program, gcc_world):
    r = gcc_world.run(program, [settle(), click(type="GtkToggleButton", tooltip="Deep Teal"), settle(),
                                dump("after"), *keys("k")])
    assert values(r, "k") == solid("#0e5a63")
    assert active(r.dump("after")) == ["Deep Teal"]


def test_wheel_opens_the_editor_and_select_sets_the_color(program, gcc_world):
    r = gcc_world.run(program, [gset(BG, "primary-color", "'#5b4033'"), {"op": "setup"}, settle(),
                                click(type="GtkToggleButton", tooltip="Custom Color"), settle(), dump("page"),
                                *pick("#204060"), dump("after"), *keys("k"), call("wheel_ring")])
    page = r.dump("page")
    dialog = page.one(type="AdwDialog")
    assert dialog["text"] == "Background Color"
    # The editor starts with the color set; until Select the preset stays active
    assert page.within(type="AdwDialog").one(type="GtkEntry")["text"].lower() == "#5b4033"
    assert active(page) == ["Mocha"]
    assert values(r, "k") == solid("#204060")
    assert active(r.dump("after")) == ["Custom Color"]
    assert r.value("wheel_ring") == "rgb(32,64,96)"
    assert not r.warnings(ignore=SHEET_NOISE)


def test_leaving_the_editor_changes_nothing(program, gcc_world):
    r = gcc_world.run(program, [gset(BG, "primary-color", "'#000000'"), {"op": "setup"}, settle(),
                                click(type="GtkToggleButton", tooltip="Custom Color"), settle(),
                                {"op": "close_dialog"}, settle(), dump("after"), *keys("k")])
    assert active(r.dump("after")) == ["Black"]
    assert values(r, "k")[(BG, "primary-color")] == "'#000000'"
    assert values(r, "k")[(LOCK, "primary-color")] == "'#023c88'"


def test_selecting_a_preset_color_in_the_editor_makes_it_the_preset(program, gcc_world):
    r = gcc_world.run(program, [settle(), click(type="GtkToggleButton", tooltip="Custom Color"), settle(),
                                *pick("#4f2a5e"), dump("after")])
    assert active(r.dump("after")) == ["Plum"]


def test_changes_by_others_show_at_once(program, gcc_world):
    r = gcc_world.run(program, [settle(), gset(BG, "primary-color", "'#6e1f2f'"), settle(), dump("a"),
                                gset(BG, "primary-color", "'#abcdef'"), settle(), dump("b"), call("wheel_ring")])
    assert active(r.dump("a")) == ["Burgundy"]
    assert active(r.dump("b")) == ["Custom Color"]
    assert r.value("wheel_ring") == "rgb(171,205,239)"


def test_page_gone_takes_its_style_and_handlers(program, gcc_world):
    # Changes after the Style page went reach nothing that is gone; another page starts anew
    r = gcc_world.run(program, [settle(), call("drop"), settle(), gset(BG, "primary-color", "'#000000'"),
                                settle(), dump("after")])
    assert not r.dump("after").has(type="GtkToggleButton")
    assert not r.warnings()


def test_editor_after_the_page_went_still_sets_the_color(program, gcc_world):
    r = gcc_world.run(program, [settle(), click(type="GtkToggleButton", tooltip="Custom Color"), settle(),
                                call("drop"), settle(), *pick("#102030"), *keys("k")])
    assert values(r, "k") == solid("#102030")
    assert not r.warnings(ignore=SHEET_NOISE)


def test_style_page_with_both_wheels(program, gcc_world):
    r = gcc_world.run(program, [settle(), dump("main")], params={"accent": "1"})
    s = r.dump("main")
    wheels = s.find(type="GtkToggleButton", tooltip="Custom Color")
    assert [w["classes"] for w in wheels] == [["plinius-custom"], ["plinius-custom", "plinius-bgcolor-custom"]]
    assert not r.warnings()


def test_style_page_opened_again(program, gcc_world):
    r = gcc_world.run(program, [settle(), call("drop"), settle(), call("again"), settle(),
                                gset(BG, "primary-color", "'#3b4552'"), settle(), dump("main"), call("wheel_ring")])
    assert active(r.dump("main")) == ["Slate Gray"]
    assert r.value("wheel_ring") == "rgb(59,69,82)"
    assert not r.warnings()
