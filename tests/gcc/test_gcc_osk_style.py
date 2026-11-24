# The look of the on-screen keyboard on the Keyboard page (cc-plinius-osk-style.c): a group right
# below the input sources with a combo row bound to org.plinius.keyboard style (stevia-plinius'
# schema, in memory here).

import shutil
import subprocess

import pytest

from gcc_harness import build_panel, call, dump, gcc_session, gcc_world, select, settle  # noqa: F401

GLUE = r'''
#include "gcc_driver.h"
#include "cc-plinius-osk-style.h"

static gboolean added;

static void
group (GccTest *t, const char *title)
{
    GtkWidget *g = adw_preferences_group_new ();

    adw_preferences_group_set_title (ADW_PREFERENCES_GROUP (g), title);
    adw_preferences_page_add (t->page, ADW_PREFERENCES_GROUP (g));
}

void
gcc_test_setup (GccTest *t)
{
    /* The Keyboard page as GNOME has it */
    group (t, "Input Sources");
    group (t, "Special Character Entry");
    added = cc_plinius_osk_style_setup (t->page);
}

gboolean
gcc_test_call (GccTest *t, const char *name, JsonArray *args, JsonBuilder *result)
{
    if (g_str_equal (name, "added")) {
        json_builder_add_boolean_value (result, added);
        return TRUE;
    }
    return FALSE;
}
'''

SCHEMA = "org.plinius.keyboard"


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    return build_panel(gcc_session, tmp_path_factory.mktemp("osk-style"), "osk-style",
                       ["cc-plinius-osk-style.c"], GLUE)


def schema_dir(session, d, change):
    for f in session.schemas.glob("*.xml"):
        if f.name == f"{SCHEMA}.gschema.xml":
            text = change(f.read_text())
            if text is not None:
                (d / f.name).write_text(text)
        else:
            shutil.copy(f, d / f.name)
    subprocess.run(["glib-compile-schemas", str(d)], check=True, capture_output=True)
    return d


@pytest.fixture(scope="module")
def without_stevia(gcc_session, tmp_path_factory):
    return schema_dir(gcc_session, tmp_path_factory.mktemp("schemas-without-stevia"), lambda text: None)


@pytest.fixture(scope="module")
def newer_stevia(gcc_session, tmp_path_factory):
    """A newer stevia-plinius with a look this Settings does not know."""
    def change(text):
        assert '<choice value="original"/>' in text
        return text.replace('<choice value="original"/>', '<choice value="original"/>\n        <choice value="pill"/>')
    return schema_dir(gcc_session, tmp_path_factory.mktemp("schemas-newer-stevia"), change)


def gset(value):
    return {"op": "gsettings_set", "schema": SCHEMA, "key": "style", "value": value}


def gget(label="style"):
    return {"op": "gsettings_get", "schema": SCHEMA, "key": "style", "label": label}


def test_group_right_below_the_input_sources(program, gcc_world):
    r = gcc_world.run(program, [settle(), dump("main"), call("added")])
    s = r.dump("main")
    assert [g["text"] for g in s.find(type="AdwPreferencesGroup")] == [
        "Input Sources", "On-Screen Keyboard", "Special Character Entry"]
    row = s.one(type="AdwComboRow")
    assert row["text"] == "Style"
    assert row["subtitle"] == ("Glass keys over the blurred wallpaper, as when the phone starts, or the "
                               "original dark keys")
    assert row["items"] == ["Glass", "Original"]
    # stevia's default
    assert row["selected_text"] == "Original"
    assert r.value("added") is True
    assert not r.warnings()


@pytest.mark.parametrize("value, shown", [("'glass'", "Glass"), ("'original'", "Original")])
def test_row_shows_the_setting(program, gcc_world, value, shown):
    r = gcc_world.run(program, [gset(value), {"op": "setup"}, settle(), dump("main")])
    assert r.dump("main").one(type="AdwComboRow")["selected_text"] == shown


def test_choice_is_written_and_follows_changes(program, gcc_world):
    r = gcc_world.run(program, [settle(), select(item="Glass", type="AdwComboRow"), settle(), gget("a"),
                                gset("'original'"), settle(), dump("b")])
    assert r.value("a") == "'glass'"
    assert r.dump("b").one(type="AdwComboRow")["selected_text"] == "Original"


def test_a_look_of_a_newer_stevia_shows_the_first(program, gcc_world, newer_stevia):
    gcc_world.extra_env["GSETTINGS_SCHEMA_DIR"] = str(newer_stevia)
    r = gcc_world.run(program, [gset("'pill'"), {"op": "setup"}, settle(), dump("main"), gget()])
    assert r.dump("main").one(type="AdwComboRow")["selected_text"] == "Glass"
    # Shown, not written: the keyboard keeps its look until one is chosen
    assert r.value("style") == "'pill'"
    assert not r.warnings()


def test_nothing_without_stevia(program, gcc_world, without_stevia):
    gcc_world.extra_env["GSETTINGS_SCHEMA_DIR"] = str(without_stevia)
    r = gcc_world.run(program, [settle(), dump("main"), call("added")])
    assert r.value("added") is False
    assert [g["text"] for g in r.dump("main").find(type="AdwPreferencesGroup")] == [
        "Input Sources", "Special Character Entry"]
