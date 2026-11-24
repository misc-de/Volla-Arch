# Focus moves nobody asked for stay out of text fields (cc-plinius-text-focus.c): the file
# replaces GtkText's "focus" virtual function. The glue makes a page with a button, a spin row
# and an entry and moves the focus the ways Settings and the user do: GTK's own focus chain (a
# page opening), a tap (grab), and Tab on a real keyboard ("move-focus").

import pytest

from gcc_harness import build_panel, call, gcc_session, gcc_world, settle  # noqa: F401

GLUE = r'''
#include "gcc_driver.h"
#include "cc-plinius-text-focus.h"

static GtkWidget *button, *spin, *entry;

void
gcc_test_setup (GccTest *t)
{
    GtkWidget *group = adw_preferences_group_new ();

    if (!gcc_test_param (t, "off")) {
        cc_plinius_text_focus_init ();
        /* A second call (another panel) changes nothing */
        cc_plinius_text_focus_init ();
    }
    button = gtk_button_new_with_label ("Button");
    gtk_widget_set_name (button, "button");
    spin = adw_spin_row_new_with_range (0, 10, 1);
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (spin), "Count");
    entry = gtk_entry_new ();
    gtk_widget_set_name (entry, "entry");
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), button);
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), spin);
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), entry);
    adw_preferences_page_add (t->page, ADW_PREFERENCES_GROUP (group));
}

static const char *
focus_name (GccTest *t)
{
    GtkWidget *focus = gtk_root_get_focus (GTK_ROOT (t->window));

    if (!focus)
        return "none";
    if (GTK_IS_TEXT (focus))
        return gtk_widget_get_ancestor (focus, ADW_TYPE_SPIN_ROW) ? "spin-text" : "entry-text";
    return G_OBJECT_TYPE_NAME (focus);
}

gboolean
gcc_test_call (GccTest *t, const char *name, JsonArray *args, JsonBuilder *result)
{
    if (g_str_equal (name, "chain")) {
        /* GTK's own focus chain, n steps forward from the button (a page that opens) */
        gint64 n = json_array_get_int_element (args, 0);

        json_builder_begin_array (result);
        gtk_widget_grab_focus (button);
        for (gint64 i = 0; i < n; i++) {
            gtk_widget_child_focus (GTK_WIDGET (t->window), GTK_DIR_TAB_FORWARD);
            json_builder_add_string_value (result, focus_name (t));
        }
        json_builder_end_array (result);
        return TRUE;
    }
    if (g_str_equal (name, "tab")) {
        /* Tab on a keyboard, n times from the button */
        gint64 n = json_array_get_int_element (args, 0);

        json_builder_begin_array (result);
        gtk_widget_grab_focus (button);
        for (gint64 i = 0; i < n; i++) {
            g_signal_emit_by_name (t->window, "move-focus", GTK_DIR_TAB_FORWARD);
            json_builder_add_string_value (result, focus_name (t));
        }
        json_builder_end_array (result);
        return TRUE;
    }
    if (g_str_equal (name, "tap")) {
        gtk_widget_grab_focus (entry);
        json_builder_add_string_value (result, focus_name (t));
        return TRUE;
    }
    if (g_str_equal (name, "focus")) {
        json_builder_add_string_value (result, focus_name (t));
        return TRUE;
    }
    return FALSE;
}
'''


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    return build_panel(gcc_session, tmp_path_factory.mktemp("text-focus"), "text-focus",
                       ["cc-plinius-text-focus.c"], GLUE)


def texts(names):
    return [n for n in names if n.endswith("-text")]


def test_without_the_file_gtk_moves_into_text_fields(program, gcc_world):
    # What the file is for: GTK's own chain ends in the spin row's and the entry's text
    r = gcc_world.run(program, [settle(), call("chain", 6)], params={"off": "1"})
    assert texts(r.value("chain"))


def test_focus_chain_skips_text_fields(program, gcc_world):
    r = gcc_world.run(program, [settle(), call("chain", 6)])
    assert texts(r.value("chain")) == []
    assert not r.warnings()


def test_a_tap_still_focuses_the_field(program, gcc_world):
    r = gcc_world.run(program, [settle(), call("tap")])
    assert r.value("tap") == "entry-text"


def test_tab_on_a_keyboard_reaches_the_fields(program, gcc_world):
    r = gcc_world.run(program, [settle(), call("tab", 6), settle(), call("focus")])
    assert "spin-text" in r.value("tab")
    assert "entry-text" in r.value("tab")


def test_after_a_key_press_the_chain_is_closed_again(program, gcc_world):
    # The key's permission ends with its emission (an idle of high priority)
    r = gcc_world.run(program, [settle(), call("tab", 1), settle(), call("chain", 6)])
    assert texts(r.value("chain")) == []
