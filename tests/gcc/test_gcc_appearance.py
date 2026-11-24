# The two rows on top of the Appearance page (cc-plinius-appearance.c): Style says the style and
# the accent color, Background what shows (picture, 360° photo, video or the background color)
# and the effect over it. Compiled with the real cc-plinius-ambient.c (the effect's name) and
# cc-plinius-panorama.c (the photo or video shown), all of them reading GSettings in memory.

import shutil
import subprocess

import pytest

from gcc_harness import build_panel, call, dump, gcc_session, gcc_world, settle  # noqa: F401

GLUE = r'''
#include "gcc_driver.h"
#include "cc-plinius-appearance.h"

static GtkWidget *group, *style, *background, *loose;

void
gcc_test_setup (GccTest *t)
{
    group = adw_preferences_group_new ();
    style = adw_action_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (style), "Style");
    background = adw_action_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (background), "Background");
    loose = adw_action_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (loose), "Not Set Up");
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), style);
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), background);
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), loose);
    adw_preferences_page_add (t->page, ADW_PREFERENCES_GROUP (group));
    cc_plinius_appearance_style_row_setup (ADW_ACTION_ROW (style));
    cc_plinius_appearance_background_row_setup (ADW_ACTION_ROW (background));
}

gboolean
gcc_test_call (GccTest *t, const char *name, JsonArray *args, JsonBuilder *result)
{
    if (g_str_equal (name, "set_uri")) {
        /* set_uri ROW URI|null */
        const char *uri = json_array_get_null_element (args, 1) ? NULL : json_array_get_string_element (args, 1);

        cc_plinius_appearance_background_row_set_uri (
            ADW_ACTION_ROW (g_str_equal (json_array_get_string_element (args, 0), "loose") ? loose : background), uri);
        json_builder_add_null_value (result);
        return TRUE;
    }
    if (g_str_equal (name, "drop")) {
        adw_preferences_page_remove (t->page, ADW_PREFERENCES_GROUP (group));
        json_builder_add_null_value (result);
        return TRUE;
    }
    return FALSE;
}
'''

IFACE = "org.gnome.desktop.interface"
APPEARANCE = "org.plinius.appearance"
AMBIENT = "org.plinius.ambient"
BG = "org.gnome.desktop.background"


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    return build_panel(gcc_session, tmp_path_factory.mktemp("appearance"), "appearance",
                       ["cc-plinius-appearance.c", "cc-plinius-ambient.c", "cc-plinius-panorama.c"], GLUE,
                       # "/usr/" is only compared with (the pictures that came with the system)
                       allow=("/usr/",),
                       pkgs=("gsettings-desktop-schemas", "gstreamer-1.0", "gdk-pixbuf-2.0"))


@pytest.fixture(scope="module")
def schemas_without(gcc_session, tmp_path_factory):
    """A schema directory without phosh-plinius' and plinius-ambient's schemas."""
    d = tmp_path_factory.mktemp("schemas-without-ours")
    for f in gcc_session.schemas.glob("*.xml"):
        if f.name not in (f"{APPEARANCE}.gschema.xml", f"{AMBIENT}.gschema.xml"):
            shutil.copy(f, d / f.name)
    subprocess.run(["glib-compile-schemas", str(d)], check=True, capture_output=True)
    return d


def gset(schema, key, value):
    return {"op": "gsettings_set", "schema": schema, "key": key, "value": value}


def subtitles(r, label="main"):
    s = r.dump(label)
    return s.one(type="AdwActionRow", text="Style").get("subtitle"), \
        s.one(type="AdwActionRow", text="Background").get("subtitle")


def run(world, program, before=(), after=(), uri="file:///usr/share/backgrounds/gnome/blobs-l.svg"):
    steps = [*before, {"op": "setup"}]
    if uri is not None:
        steps.append(call("set_uri", "background", uri))
    return world.run(program, [*steps, settle(), *after, dump("main")])


@pytest.mark.parametrize("scheme, shown", [(None, "Default"), ("'prefer-dark'", "Dark"),
                                           ("'prefer-light'", "Light"), ("'default'", "Default")])
def test_style_says_the_style(program, gcc_world, scheme, shown):
    before = [gset(IFACE, "color-scheme", scheme)] if scheme else []
    r = run(gcc_world, program, before)
    assert subtitles(r)[0] == f"{shown} · Blue"
    assert not r.warnings()


@pytest.mark.parametrize("accent, shown", [("blue", "Blue"), ("teal", "Teal"), ("green", "Green"),
                                           ("yellow", "Yellow"), ("orange", "Orange"), ("red", "Red"),
                                           ("pink", "Pink"), ("purple", "Purple"), ("slate", "Slate")])
def test_style_says_gnomes_accent_color(program, gcc_world, accent, shown):
    r = run(gcc_world, program, [gset(IFACE, "accent-color", f"'{accent}'")])
    assert subtitles(r)[0] == f"Default · {shown}"


@pytest.mark.parametrize("custom, shown", [("#ff00ff", "Custom Color"), ("bogus", "Purple"), ("", "Purple")])
def test_style_says_custom_color(program, gcc_world, custom, shown):
    r = run(gcc_world, program, [gset(IFACE, "accent-color", "'purple'"),
                                 gset(APPEARANCE, "accent-color", repr(custom))])
    assert subtitles(r)[0] == f"Default · {shown}"


def test_style_follows_changes(program, gcc_world):
    r = gcc_world.run(program, [settle(), gset(IFACE, "color-scheme", "'prefer-dark'"), settle(), dump("a"),
                                gset(IFACE, "accent-color", "'red'"), settle(), dump("b"),
                                gset(APPEARANCE, "accent-color", "'#123456'"), settle(), dump("c")])
    assert [r.dump(x).one(type="AdwActionRow", text="Style")["subtitle"] for x in "abc"] == [
        "Dark · Blue", "Dark · Red", "Dark · Custom Color"]


def test_without_phosh_plinius_only_gnomes_colors(program, gcc_world, schemas_without):
    gcc_world.extra_env["GSETTINGS_SCHEMA_DIR"] = str(schemas_without)
    r = gcc_world.run(program, [gset(IFACE, "accent-color", "'green'"), {"op": "setup"},
                                call("set_uri", "background", "file:///home/user/a.jpg"), settle(), dump("a"),
                                gset(IFACE, "color-scheme", "'prefer-light'"), settle(), dump("main")])
    # No effect without plinius-ambient, no photo or video either
    assert subtitles(r, "a") == ("Default · Green", "Own Picture")
    assert subtitles(r) == ("Light · Green", "Own Picture")
    assert not r.warnings()


@pytest.mark.parametrize("uri, shown", [
    ("file:///usr/share/backgrounds/gnome/blobs-l.svg", "Default Picture"),
    ("file:///home/user/Pictures/cat.jpg", "Own Picture"),
    # Not a file: the system's
    ("resource:///org/gnome/shell/x.png", "Default Picture"),
    (None, "Color"), ("", "Color")])
def test_background_says_the_picture(program, gcc_world, uri, shown):
    r = gcc_world.run(program, [{"op": "setup"}, call("set_uri", "background", uri), settle(), dump("main")])
    assert subtitles(r)[1] == shown


def test_background_without_a_picture_yet_is_the_color(program, gcc_world):
    r = run(gcc_world, program, uri=None)
    assert subtitles(r)[1] == "Color"


def test_picture_options_none_shows_the_color(program, gcc_world):
    r = run(gcc_world, program, [gset(BG, "picture-options", "'none'")], uri="file:///home/user/cat.jpg")
    assert subtitles(r)[1] == "Color"


def test_picture_options_follow_changes(program, gcc_world):
    r = run(gcc_world, program, uri="file:///home/user/cat.jpg",
            after=[gset(BG, "picture-options", "'none'"), settle(), dump("none"),
                   gset(BG, "picture-options", "'zoom'"), settle()])
    assert subtitles(r, "none")[1] == "Color"
    assert subtitles(r)[1] == "Own Picture"


@pytest.mark.parametrize("keys, shown", [
    ({"video-enabled": "true", "video": "'/home/user/Videos/sea.mp4'"}, "Video"),
    ({"panorama-enabled": "true", "panorama": "'/home/user/pano.jpg'"}, "360° Photo"),
    # Both (plinius-ambient keeps only one): the video counts
    ({"video-enabled": "true", "video": "'/v.mp4'", "panorama-enabled": "true", "panorama": "'/p.jpg'"}, "Video"),
    # Switched on without a file: the picture
    ({"video-enabled": "true"}, "Own Picture"),
    ({"panorama-enabled": "false", "panorama": "'/home/user/pano.jpg'"}, "Own Picture")])
def test_background_says_photo_or_video(program, gcc_world, keys, shown):
    before = [gset(AMBIENT, k, v) for k, v in keys.items()]
    r = run(gcc_world, program, before, uri="file:///home/user/cat.jpg")
    assert subtitles(r)[1] == shown


@pytest.mark.parametrize("effect, shown", [
    ("snow", "Own Picture · Snow"), ("dust", "Own Picture · Dust in the Light"),
    ("fireflies", "Own Picture · Fireflies"), ("rain", "Own Picture · Rain"),
    ("balls", "Own Picture · Rolling Balls"), ("water", "Own Picture · Water"),
    ("none", "Own Picture"),
    # plinius-ambient 0.3's value, which it turns into panorama-enabled itself
    ("panorama", "Own Picture")])
def test_background_says_the_effect(program, gcc_world, effect, shown):
    r = run(gcc_world, program, [gset(AMBIENT, "effect", repr(effect))], uri="file:///home/user/cat.jpg")
    assert subtitles(r)[1] == shown


def test_background_follows_the_ambient_settings(program, gcc_world):
    r = run(gcc_world, program, uri="file:///home/user/cat.jpg",
            after=[gset(AMBIENT, "effect", "'rain'"), settle(), dump("rain"),
                   gset(AMBIENT, "panorama", "'/p.jpg'"), gset(AMBIENT, "panorama-enabled", "true"), settle(),
                   dump("photo"), gset(AMBIENT, "panorama-enabled", "false"),
                   gset(AMBIENT, "video", "'/v.mp4'"), gset(AMBIENT, "video-enabled", "true"), settle()])
    assert subtitles(r, "rain")[1] == "Own Picture · Rain"
    assert subtitles(r, "photo")[1] == "360° Photo · Rain"
    assert subtitles(r)[1] == "Video · Rain"


def test_set_uri_on_a_row_without_setup_does_nothing(program, gcc_world):
    r = gcc_world.run(program, [settle(), call("set_uri", "loose", "file:///home/user/cat.jpg"), dump("main")])
    assert r.dump("main").one(type="AdwActionRow", text="Not Set Up").get("subtitle") is None
    assert not r.warnings()


def test_rows_gone_let_go_of_the_settings(program, gcc_world):
    # Changes after the page went reach no freed row
    r = gcc_world.run(program, [settle(), call("drop"), settle(),
                                gset(IFACE, "color-scheme", "'prefer-dark'"), gset(APPEARANCE, "accent-color", "'#000000'"),
                                gset(AMBIENT, "effect", "'snow'"), gset(BG, "picture-options", "'none'"), settle(),
                                dump("main")])
    assert not r.dump("main").has(type="AdwActionRow")
    assert not r.warnings()


def test_rows_gone_without_our_schemas(program, gcc_world, schemas_without):
    gcc_world.extra_env["GSETTINGS_SCHEMA_DIR"] = str(schemas_without)
    r = gcc_world.run(program, [settle(), call("drop"), settle(), gset(IFACE, "color-scheme", "'prefer-dark'"),
                                settle(), dump("main")])
    assert not r.dump("main").has(type="AdwActionRow")
    assert not r.warnings()
