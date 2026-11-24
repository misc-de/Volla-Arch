# The Assistant Button panel (cc-assistant-panel.c): listed only when plinius-assist's schema
# org.plinius.assistant-button is installed (the shell model's visibility), its page titled
# "Assistant Button" with a header bar and the actions of cc-plinius-buttons.c (compiled along).

import pytest

from fake_schemas import schemas_without
from gcc_harness import build_panel, call, dump, gcc_session, gcc_world, select, settle  # noqa: F401

GLUE = r'''
#include "gcc_driver.h"
#include "cc-assistant-panel.h"

CcPanel *
gcc_test_create_panel (GccTest *t)
{
    return g_object_new (CC_TYPE_ASSISTANT_PANEL, "window", t->window, NULL);
}

void
gcc_test_setup (GccTest *t)
{
}

gboolean
gcc_test_call (GccTest *t, const char *name, JsonArray *args, JsonBuilder *result)
{
    if (g_str_equal (name, "static_init")) {
        cc_assistant_panel_static_init_func ();
        json_builder_add_null_value (result);
        return TRUE;
    }
    return FALSE;
}
'''

SCHEMA = "org.plinius.assistant-button"


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    return build_panel(gcc_session, tmp_path_factory.mktemp("assistant"), "assistant",
                       ["cc-assistant-panel.c", "cc-plinius-buttons.c"], GLUE)


def without_schema(world, tmp_path):
    world.extra_env["GSETTINGS_SCHEMA_DIR"] = str(schemas_without(world.session, tmp_path / "s", SCHEMA))


@pytest.mark.parametrize("installed, visibility", [(True, "visible"), (False, "hidden")])
def test_listed_only_with_plinius_assist(program, gcc_world, tmp_path, installed, visibility):
    if not installed:
        without_schema(gcc_world, tmp_path)
    r = gcc_world.run(program, [call("static_init")])
    assert r.visibility == {"assistant": visibility}


def test_the_page_holds_the_three_presses(program, gcc_world):
    r = gcc_world.run(program, [settle(), dump("main"),
                                select(item="Screenshot", type="AdwComboRow", text="Double Press"),
                                settle(), {"op": "gsettings_get", "schema": SCHEMA, "key": "double-press"}])
    s = r.dump("main")
    page = s.one(type="CcAssistantPanel")
    assert page["text"] == "Assistant Button"
    # The header bar says what the page is
    assert s.has(type="GtkLabel", text="Assistant Button")
    assert [n["text"] for n in s.find(type="AdwComboRow")] == ["Short Press", "Double Press", "Long Press"]
    assert r.value("gsettings_get") == "'screenshot'"
    assert not r.warnings()


def test_without_plinius_assist_the_page_is_empty(program, gcc_world, tmp_path):
    without_schema(gcc_world, tmp_path)
    r = gcc_world.run(program, [settle(), dump("main")])
    s = r.dump("main")
    assert s.one(type="CcAssistantPanel")["text"] == "Assistant Button"
    assert not s.has(type="AdwComboRow")
    assert not s.has(type="AdwPreferencesGroup")
