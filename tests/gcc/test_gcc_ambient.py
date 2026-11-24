# The group "Animation" on the Background page (cc-plinius-ambient.c): the effect over the
# background and its amount, both bound to org.plinius.ambient (plinius-ambient's schema, here
# in memory), and the effect's name for the Background row.

import shutil
import subprocess

import pytest

from gcc_harness import build_panel, call, dump, gcc_session, gcc_world, select, settle  # noqa: F401

GLUE = r'''
#include "gcc_driver.h"
#include "cc-plinius-ambient.h"

static gboolean made;

void
gcc_test_setup (GccTest *t)
{
    GtkWidget *group = cc_plinius_ambient_group_new ();

    made = group != NULL;
    gcc_test_add (t, group);
}

gboolean
gcc_test_call (GccTest *t, const char *name, JsonArray *args, JsonBuilder *result)
{
    if (g_str_equal (name, "made")) {
        json_builder_add_boolean_value (result, made);
        return TRUE;
    }
    if (g_str_equal (name, "label")) {
        const char *label = cc_plinius_ambient_effect_label ();

        if (label)
            json_builder_add_string_value (result, label);
        else
            json_builder_add_null_value (result);
        return TRUE;
    }
    return FALSE;
}
'''

AMBIENT = "org.plinius.ambient"
EFFECTS = ["None", "Snow", "Dust in the Light", "Fireflies", "Rain", "Rolling Balls", "Water"]
VALUES = ["none", "snow", "dust", "fireflies", "rain", "balls", "water"]


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    return build_panel(gcc_session, tmp_path_factory.mktemp("ambient"), "ambient",
                       ["cc-plinius-ambient.c"], GLUE)


def gset(key, value):
    return {"op": "gsettings_set", "schema": AMBIENT, "key": key, "value": value}


def gget(key, label=None):
    return {"op": "gsettings_get", "schema": AMBIENT, "key": key, "label": label or key}


def rows(screen):
    return {r["text"]: r for r in screen.find(type="AdwComboRow")}


def test_group_with_effect_and_amount(program, gcc_world):
    r = gcc_world.run(program, [settle(), dump("main"), call("made"), call("label"), gget("effect")])
    s = r.dump("main")
    assert s.one(type="AdwPreferencesGroup")["text"] == "Animation"
    effect, amount = rows(s)["Effect"], rows(s)["Amount"]
    assert effect["items"] == EFFECTS
    assert amount["items"] == ["Low", "Medium", "High"]
    # As the schema starts: no effect, so the amount waits
    assert r.value("effect") == "'none'"
    assert (effect["selected_text"], amount["selected_text"]) == ("None", "Medium")
    assert amount["sensitive"] is False
    assert r.value("label") is None
    assert r.value("made") is True
    assert not r.warnings()


@pytest.mark.parametrize("index", range(7))
def test_effect_choice_is_written(program, gcc_world, index):
    r = gcc_world.run(program, [settle(), select(index, type="AdwComboRow", text="Effect"), settle(),
                                gget("effect"), call("label"), dump("main")])
    assert r.value("effect") == f"'{VALUES[index]}'"
    assert r.value("label") == (None if index == 0 else EFFECTS[index])
    # The amount only with an effect
    assert rows(r.dump("main"))["Amount"].get("sensitive", True) is (index != 0)


@pytest.mark.parametrize("index, value", [(0, "low"), (1, "medium"), (2, "high")])
def test_amount_choice_is_written(program, gcc_world, index, value):
    r = gcc_world.run(program, [gset("effect", "'snow'"), {"op": "setup"}, settle(),
                                select(index, type="AdwComboRow", text="Amount"), settle(), gget("density")])
    assert r.value("density") == f"'{value}'"


def test_rows_follow_the_settings(program, gcc_world):
    r = gcc_world.run(program, [gset("effect", "'fireflies'"), gset("density", "'high'"), {"op": "setup"},
                                settle(), dump("a"), gset("effect", "'none'"), settle(), dump("b")])
    a, b = rows(r.dump("a")), rows(r.dump("b"))
    assert (a["Effect"]["selected_text"], a["Amount"]["selected_text"]) == ("Fireflies", "High")
    assert a["Amount"].get("sensitive", True) is True
    assert b["Effect"]["selected_text"] == "None"
    assert b["Amount"]["sensitive"] is False


def test_unknown_effect_shows_the_default_and_has_no_name(program, gcc_world):
    # plinius-ambient 0.3 wrote "panorama" as an effect: not one of the rows' (GSettings shows the
    # default then), the key stays for plinius-ambient to turn into panorama-enabled
    r = gcc_world.run(program, [gset("effect", "'rain'"), {"op": "setup"}, settle(), gset("effect", "'panorama'"),
                                settle(), dump("main"), call("label"), gget("effect")])
    assert rows(r.dump("main"))["Effect"]["selected_text"] == "None"
    assert r.value("label") is None
    assert r.value("effect") == "'panorama'"


@pytest.fixture(scope="module")
def schemas_without_ambient(gcc_session, tmp_path_factory):
    d = tmp_path_factory.mktemp("schemas-without-ambient")
    for f in gcc_session.schemas.glob("*.xml"):
        if f.name != f"{AMBIENT}.gschema.xml":
            shutil.copy(f, d / f.name)
    subprocess.run(["glib-compile-schemas", str(d)], check=True, capture_output=True)
    return d


def test_nothing_without_plinius_ambient(program, gcc_world, schemas_without_ambient):
    gcc_world.extra_env["GSETTINGS_SCHEMA_DIR"] = str(schemas_without_ambient)
    r = gcc_world.run(program, [settle(), dump("main"), call("made"), call("label"), call("label", label="again")])
    assert r.value("made") is False
    assert not r.dump("main").has(type="AdwComboRow")
    assert r.value("label") is None and r.value("again") is None
