# The custom accent color (cc-plinius-accent.c): a color wheel after GNOME's nine accent buttons
# on the Appearance page, GTK's color editor on a page of its own, the color in
# org.plinius.appearance accent-color with GNOME's key set to the nearest preset, a block in
# ~/.config/gtk-4.0/gtk.css for the GTK 4 apps. The glue builds the accent box as
# cc-background-panel.c does (nine toggle buttons that set GNOME's key when they get active,
# reloaded on a change of the key through cc_plinius_accent_sync()).

import shutil
import subprocess

import pytest

from gcc_harness import (back, build_panel, call, click, dump, enter, gcc_session, gcc_world,  # noqa: F401
                         set_text, settle)

GLUE = r'''
#include <gdesktop-enums.h>
#include "gcc_driver.h"
#include "cc-plinius-accent.h"

static const char *const names[] = { "Blue", "Teal", "Green", "Yellow", "Orange", "Red", "Pink", "Purple", "Slate" };
static GtkWidget *accent_box;
static GSettings *interface;
static gboolean set_up;
static gboolean box_gone;

static void
box_gone_cb (gpointer data, GObject *where)
{
    box_gone = TRUE;
}

/* cc-background-panel.c's on_accent_color_toggled_cb () */
static void
toggled_cb (GtkToggleButton *toggle)
{
    int color = GPOINTER_TO_INT (g_object_get_data (G_OBJECT (toggle), "accent-color"));

    if (!gtk_toggle_button_get_active (toggle))
        return;
    if (color == g_settings_get_enum (interface, "accent-color"))
        return;
    g_settings_set_enum (interface, "accent-color", color);
}

/* cc-background-panel.c's reload_accent_color_toggles () */
static void
reload (void)
{
    int color = g_settings_get_enum (interface, "accent-color");

    if (cc_plinius_accent_sync (accent_box))
        return;
    for (GtkWidget *c = gtk_widget_get_first_child (accent_box); c; c = gtk_widget_get_next_sibling (c))
        gtk_toggle_button_set_active (GTK_TOGGLE_BUTTON (c),
                                      GPOINTER_TO_INT (g_object_get_data (G_OBJECT (c), "accent-color")) == color);
}

void
gcc_test_setup (GccTest *t)
{
    GtkWidget *group = adw_preferences_group_new ();
    int color;

    adw_preferences_group_set_title (ADW_PREFERENCES_GROUP (group), "Accent Color");
    accent_box = gtk_box_new (GTK_ORIENTATION_HORIZONTAL, 2);
    gtk_widget_set_name (accent_box, "accent-box");
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), accent_box);
    adw_preferences_page_add (t->page, ADW_PREFERENCES_GROUP (group));
    if (gcc_test_param (t, "empty")) {
        set_up = cc_plinius_accent_setup (accent_box);
        return;
    }
    interface = g_settings_new ("org.gnome.desktop.interface");
    color = g_settings_get_enum (interface, "accent-color");
    for (int i = 0; i < 9; i++) {
        GtkWidget *button = gtk_toggle_button_new ();
        GtkWidget *first = gtk_widget_get_first_child (accent_box);

        gtk_widget_set_tooltip_text (button, names[i]);
        gtk_widget_add_css_class (button, "accent-button");
        g_object_set_data (G_OBJECT (button), "accent-color", GINT_TO_POINTER (i));
        g_signal_connect (button, "toggled", G_CALLBACK (toggled_cb), NULL);
        if (first)
            gtk_toggle_button_set_group (GTK_TOGGLE_BUTTON (button), GTK_TOGGLE_BUTTON (first));
        if (i == color)
            gtk_toggle_button_set_active (GTK_TOGGLE_BUTTON (button), TRUE);
        gtk_box_append (GTK_BOX (accent_box), button);
    }
    set_up = cc_plinius_accent_setup (accent_box);
    g_signal_connect (interface, "changed::accent-color", G_CALLBACK (reload), NULL);
}

gboolean
gcc_test_call (GccTest *t, const char *name, JsonArray *args, JsonBuilder *result)
{
    if (g_str_equal (name, "set_up")) {
        json_builder_add_boolean_value (result, set_up);
        return TRUE;
    }
    if (g_str_equal (name, "box_gone")) {
        json_builder_add_boolean_value (result, box_gone);
        return TRUE;
    }
    if (g_str_equal (name, "sync")) {
        json_builder_add_boolean_value (result, cc_plinius_accent_sync (accent_box));
        return TRUE;
    }
    if (g_str_equal (name, "hex")) {
        GdkRGBA rgba = { json_array_get_double_element (args, 0), json_array_get_double_element (args, 1),
                         json_array_get_double_element (args, 2), 1 };
        g_autofree char *hex = cc_plinius_color_to_hex (&rgba);

        json_builder_add_string_value (result, hex);
        return TRUE;
    }
    if (g_str_equal (name, "accent")) {
        /* The accent color Settings' own widgets get now */
        GtkWidget *probe = gtk_widget_get_first_child (accent_box);
        GdkRGBA rgba;
        g_autofree char *hex = NULL;

        G_GNUC_BEGIN_IGNORE_DEPRECATIONS
        {
            GtkStyleContext *context = gtk_widget_get_style_context (probe);

            if (!gtk_style_context_lookup_color (context, "accent_bg_color", &rgba))
                rgba = (GdkRGBA) { 0, 0, 0, 0 };
        }
        G_GNUC_END_IGNORE_DEPRECATIONS
        hex = cc_plinius_color_to_hex (&rgba);
        json_builder_add_string_value (result, hex);
        return TRUE;
    }
    if (g_str_equal (name, "drop_box")) {
        /* The page goes (another panel), its box with it */
        g_signal_handlers_disconnect_by_func (interface, reload, NULL);
        g_object_weak_ref (G_OBJECT (accent_box), box_gone_cb, NULL);
        adw_preferences_page_remove (t->page, ADW_PREFERENCES_GROUP (
            gtk_widget_get_ancestor (accent_box, ADW_TYPE_PREFERENCES_GROUP)));
        json_builder_add_null_value (result);
        return TRUE;
    }
    return FALSE;
}
'''

SCHEMA = "org.plinius.appearance"
IFACE = "org.gnome.desktop.interface"
PRESETS = ["Blue", "Teal", "Green", "Yellow", "Orange", "Red", "Pink", "Purple", "Slate"]
ENUM = ["blue", "teal", "green", "yellow", "orange", "red", "pink", "purple", "slate"]
BEGIN = "/* plinius-accent begin: written by Settings (Appearance > Accent Color), do not edit */\n"
END = "/* plinius-accent end */\n"


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    return build_panel(gcc_session, tmp_path_factory.mktemp("accent"), "accent",
                       ["cc-plinius-accent.c"], GLUE, pkgs=("gsettings-desktop-schemas",))


# The color page is a sheet here (in Settings cc-plinius-dialog-pages.c makes it a page): too
# wide for the test's window, and the editor's icons have no theme to come from
SHEET_NOISE = (r"^AdwBottomSheet .* exceeds AdwBreakpointBin width",
               r"^gtk_snapshot_append_texture: assertion 'GDK_IS_TEXTURE \(texture\)' failed")


def gset(schema, key, value):
    return {"op": "gsettings_set", "schema": schema, "key": key, "value": value}


def gget(schema, key, label):
    return {"op": "gsettings_get", "schema": schema, "key": key, "label": label}


def custom(value):
    return gset(SCHEMA, "accent-color", repr(value))


def keys(label):
    return [gget(SCHEMA, "accent-color", f"{label}-custom"), gget(IFACE, "accent-color", f"{label}-gnome")]


def values(r, label):
    return r.value(f"{label}-custom"), r.value(f"{label}-gnome")


def buttons(screen):
    """[(tooltip, active)] of the accent box's buttons"""
    box = screen.within(type="GtkBox", name="accent-box")
    return [(b.get("tooltip"), b.get("active")) for b in box.find(type="GtkToggleButton")]


def active(screen):
    return [t for t, a in buttons(screen) if a]


def css(world):
    f = world.home / ".config/gtk-4.0/gtk.css"
    return f.read_text() if f.exists() else None


def block(bg, fg):
    return (BEGIN + f"@define-color accent_bg_color {bg};\n@define-color accent_fg_color {fg};\n"
            f":root {{\n  --accent-bg-color: {bg};\n  --accent-fg-color: {fg};\n}}\n" + END)


def editor_text(screen):
    """The color in the editor's field (GTK writes it in capitals)"""
    return screen.within(type="AdwDialog").one(type="GtkEntry")["text"].lower()


def pick(hex_color):
    """On the color page: type the color into the editor's field and tap Select."""
    return [set_text(hex_color, type="GtkEntry", within={"type": "AdwDialog"}),
            enter(type="GtkEntry", within={"type": "AdwDialog"}), settle(),
            click(type="GtkButton", text="Select"), settle()]


def test_wheel_after_gnomes_colors(program, gcc_world):
    r = gcc_world.run(program, [settle(), dump("main"), call("set_up"), call("accent"), *keys("k")])
    s = r.dump("main")
    assert buttons(s) == [(n, n == "Blue") for n in PRESETS] + [("Custom Color", False)]
    wheel = s.one(type="GtkToggleButton", tooltip="Custom Color")
    assert wheel["classes"] == ["plinius-custom"]
    assert r.value("set_up") is True
    # GNOME's color: no block in gtk.css, Settings in GNOME's blue
    assert css(gcc_world) is None
    assert r.value("accent") == "#3584e4"
    assert values(r, "k") == ("''", "'blue'")
    assert not r.warnings()


def test_custom_color_set_makes_the_wheel_active_and_writes_gtk_css(program, gcc_world):
    r = gcc_world.run(program, [custom("#ff00ff"), {"op": "setup"}, settle(), dump("main"), call("accent")])
    assert active(r.dump("main")) == ["Custom Color"]
    assert css(gcc_world) == block("#ff00ff", "#ffffff")
    assert r.value("accent") == "#ff00ff"


@pytest.mark.parametrize("value", ["nonsense", ""])
def test_invalid_custom_color_is_gnomes(program, gcc_world, value):
    r = gcc_world.run(program, [custom(value), gset(IFACE, "accent-color", "'green'"), {"op": "setup"},
                                settle(), dump("main"), call("accent")])
    assert active(r.dump("main")) == ["Green"]
    assert css(gcc_world) is None
    assert r.value("accent") == "#3a944a"


@pytest.mark.parametrize("color, fg", [("#ffff00", "rgba(0, 0, 0, 0.8)"), ("#c88800", "#ffffff"),
                                       ("#202020", "#ffffff")])
def test_text_on_light_colors_is_dark(program, gcc_world, color, fg):
    gcc_world.run(program, [custom(color), {"op": "setup"}, settle()])
    assert css(gcc_world) == block(color, fg)


def test_page_with_the_editor_and_select(program, gcc_world):
    r = gcc_world.run(program, [settle(), click(type="GtkToggleButton", tooltip="Custom Color"), settle(),
                                dump("page"), *pick("#9141ac"), dump("after"), *keys("k"), call("accent")])
    page = r.dump("page")
    dialog = page.one(type="AdwDialog")
    assert dialog["text"] == "Custom Color"
    assert page.has(type="GtkButton", text="Select")
    # Until a color is selected GNOME's stays active
    assert active(page) == ["Blue"]
    assert values(r, "k") == ("'#9141ac'", "'purple'")
    assert active(r.dump("after")) == ["Custom Color"]
    assert not r.dump("after").has(type="AdwDialog")
    assert css(gcc_world) == block("#9141ac", "#ffffff")
    assert r.value("accent") == "#9141ac"


def test_page_starts_with_the_color_set(program, gcc_world):
    r = gcc_world.run(program, [custom("#123456"), {"op": "setup"}, settle(),
                                click(type="GtkToggleButton", tooltip="Custom Color"), settle(), dump("page")])
    assert editor_text(r.dump("page")) == "#123456"


def test_page_starts_with_gnomes_color(program, gcc_world):
    r = gcc_world.run(program, [gset(IFACE, "accent-color", "'teal'"), {"op": "setup"}, settle(),
                                click(type="GtkToggleButton", tooltip="Custom Color"), settle(), dump("page")])
    assert editor_text(r.dump("page")) == "#2190a4"


def test_leaving_the_page_changes_nothing(program, gcc_world):
    r = gcc_world.run(program, [gset(IFACE, "accent-color", "'red'"), {"op": "setup"}, settle(),
                                click(type="GtkToggleButton", tooltip="Custom Color"), settle(),
                                {"op": "close_dialog"}, settle(), dump("after"), *keys("k")])
    assert active(r.dump("after")) == ["Red"]
    assert values(r, "k") == ("''", "'red'")
    assert css(gcc_world) is None


@pytest.mark.parametrize("color, nearest", [
    ("#3584e4", "blue"), ("#2a0a40", "purple"), ("#ffe000", "yellow"), ("#00ff00", "green"),
    ("#ff0000", "red"), ("#ff80c0", "pink"), ("#808080", "slate"), ("#00a0b0", "teal"),
    ("#ff7000", "orange")])
def test_gnomes_key_gets_the_nearest_preset(program, gcc_world, color, nearest):
    r = gcc_world.run(program, [settle(), click(type="GtkToggleButton", tooltip="Custom Color"), settle(),
                                *pick(color), *keys("k")])
    assert values(r, "k") == (repr(color), repr(nearest))


def test_preset_tap_drops_the_custom_color(program, gcc_world):
    r = gcc_world.run(program, [custom("#ff00ff"), {"op": "setup"}, settle(),
                                click(type="GtkToggleButton", tooltip="Green"), settle(), dump("after"),
                                *keys("k"), call("accent")])
    assert values(r, "k") == ("''", "'green'")
    assert active(r.dump("after")) == ["Green"]
    # Nothing else was in the file: it goes
    assert css(gcc_world) is None
    assert not (gcc_world.home / ".config/gtk-4.0/gtk.css").exists()
    assert r.value("accent") == "#3a944a"


def test_preset_tap_on_the_active_preset_without_custom_color(program, gcc_world):
    r = gcc_world.run(program, [settle(), click(type="GtkToggleButton", tooltip="Blue"), settle(), *keys("k")])
    assert values(r, "k") == ("''", "'blue'")


def test_preset_tap_on_the_nearest_preset_drops_the_custom_color(program, gcc_world):
    # GNOME's key is already purple: only the custom key changes
    r = gcc_world.run(program, [custom("#9141ac"), gset(IFACE, "accent-color", "'purple'"), {"op": "setup"},
                                settle(), click(type="GtkToggleButton", tooltip="Purple"), settle(),
                                dump("after"), *keys("k")])
    assert values(r, "k") == ("''", "'purple'")
    assert active(r.dump("after")) == ["Purple"]
    assert css(gcc_world) is None


def test_the_users_own_css_stays(program, gcc_world):
    f = gcc_world.home / ".config/gtk-4.0/gtk.css"
    f.parent.mkdir(parents=True)
    f.write_text("window { opacity: 1; }")
    gcc_world.run(program, [custom("#ff00ff"), {"op": "setup"}, settle()])
    assert css(gcc_world) == "window { opacity: 1; }\n" + block("#ff00ff", "#ffffff")
    # Another color: the block is replaced, not added
    gcc_world.run(program, [custom("#00ff00"), {"op": "setup"}, settle()])
    assert css(gcc_world) == "window { opacity: 1; }\n" + block("#00ff00", "rgba(0, 0, 0, 0.8)")
    # Back to GNOME's color: the user's part stays
    gcc_world.run(program, [settle()])
    assert css(gcc_world) == "window { opacity: 1; }\n"


def test_unchanged_block_is_not_written_again(program, gcc_world):
    f = gcc_world.home / ".config/gtk-4.0/gtk.css"
    f.parent.mkdir(parents=True)
    f.write_text(block("#ff00ff", "#ffffff"))
    # A write would fail in a directory that cannot be written
    f.parent.chmod(0o500)
    try:
        r = gcc_world.run(program, [custom("#ff00ff"), {"op": "setup"}, settle()])
    finally:
        f.parent.chmod(0o700)
    assert not r.warnings()
    assert css(gcc_world) == block("#ff00ff", "#ffffff")


def test_a_block_without_its_end_is_left_and_a_new_one_added(program, gcc_world):
    f = gcc_world.home / ".config/gtk-4.0/gtk.css"
    f.parent.mkdir(parents=True)
    f.write_text(BEGIN + "@define-color accent_bg_color #000000;\n")
    gcc_world.run(program, [custom("#ff00ff"), {"op": "setup"}, settle()])
    assert css(gcc_world) == BEGIN + "@define-color accent_bg_color #000000;\n" + block("#ff00ff", "#ffffff")


def test_file_that_cannot_be_written_is_a_warning(program, gcc_world):
    # gtk-4.0 is a file: no directory for gtk.css
    (gcc_world.home / ".config/gtk-4.0").write_text("")
    r = gcc_world.run(program, [custom("#ff00ff"), {"op": "setup"}, settle(), dump("main")])
    assert [m["message"] for m in r.warnings()] == [
        f"Could not write {gcc_world.home}/.config/gtk-4.0/gtk.css: Not a directory"]
    # Settings shows it all the same
    assert active(r.dump("main")) == ["Custom Color"]


def test_file_that_cannot_be_removed_is_a_warning(program, gcc_world):
    d = gcc_world.home / ".config/gtk-4.0"
    d.mkdir()
    (d / "gtk.css").write_text(block("#ff00ff", "#ffffff"))
    d.chmod(0o500)
    try:
        r = gcc_world.run(program, [settle()])
    finally:
        d.chmod(0o700)
    assert [m["message"] for m in r.warnings()] == [
        f"Could not remove {d}/gtk.css: Permission denied"]


def test_changes_by_others_show_at_once(program, gcc_world):
    r = gcc_world.run(program, [settle(), custom("#e62d42"), settle(), dump("custom"), call("accent", label="a1"),
                                custom(""), settle(), gset(IFACE, "accent-color", "'orange'"), settle(),
                                dump("orange"), call("accent", label="a2")])
    assert active(r.dump("custom")) == ["Custom Color"]
    assert r.value("a1") == "#e62d42"
    assert active(r.dump("orange")) == ["Orange"]
    assert r.value("a2") == "#ed5b00"
    assert css(gcc_world) is None


@pytest.fixture(scope="module")
def asan_program(gcc_session, tmp_path_factory):
    """The same program with AddressSanitizer: a use after free ends it with an error."""
    program = build_panel(gcc_session, tmp_path_factory.mktemp("accent-asan"), "accent-asan",
                          ["cc-plinius-accent.c"], GLUE, pkgs=("gsettings-desktop-schemas",),
                          flags=["-fsanitize=address", "-fno-omit-frame-pointer", "-fno-profile-arcs"])
    # Not counted: AddressSanitizer makes lines of its own (declarations) count for gcov
    for f in program.path.parent.glob("*.gcno"):
        f.unlink()
    return program


def test_the_page_outlives_the_appearance_page(asan_program, gcc_world):
    # The accent page holds its own settings: Select after the box went still sets the color
    gcc_world.extra_env["ASAN_OPTIONS"] = "detect_leaks=0"
    r = gcc_world.run(asan_program, [settle(), click(type="GtkToggleButton", tooltip="Custom Color"), settle(),
                                     call("drop_box"), settle(), call("box_gone"), *pick("#3a944a"),
                                     *keys("k")])
    assert r.value("box_gone") is True
    assert values(r, "k") == ("'#3a944a'", "'green'")
    assert "AddressSanitizer" not in r.stderr


def test_hex_of_colors_out_of_range(program, gcc_world):
    r = gcc_world.run(program, [call("hex", -0.5, 0.5, 1.5, label="a"), call("hex", 0.2, 0.4, 0.6, label="b")])
    assert r.value("a") == "#0080ff"
    assert r.value("b") == "#336699"


def test_box_without_gnomes_colors_gets_nothing(program, gcc_world):
    r = gcc_world.run(program, [settle(), call("set_up"), call("sync"), dump("main")], params={"empty": "1"})
    assert r.value("set_up") is False
    assert r.value("sync") is False


@pytest.fixture(scope="module")
def schemas_without_ours(gcc_session, tmp_path_factory):
    d = tmp_path_factory.mktemp("schemas-without-appearance")
    for f in gcc_session.schemas.glob("*.xml"):
        if f.name != f"{SCHEMA}.gschema.xml":
            shutil.copy(f, d / f.name)
    subprocess.run(["glib-compile-schemas", str(d)], check=True, capture_output=True)
    return d


def test_nothing_without_the_schema(program, gcc_world, schemas_without_ours):
    gcc_world.extra_env["GSETTINGS_SCHEMA_DIR"] = str(schemas_without_ours)
    r = gcc_world.run(program, [settle(), dump("main"), call("set_up"), call("sync")])
    assert r.value("set_up") is False
    assert r.value("sync") is False
    assert [t for t, _a in buttons(r.dump("main"))] == PRESETS
    assert css(gcc_world) is None
