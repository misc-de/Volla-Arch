# Form dialogs as pages (cc-plinius-dialog-pages.c): the file replaces adw_dialog_present(),
# adw_dialog_close() and adw_dialog_force_close() of the whole program and has
# cc_plinius_gtk_dialog_present_as_page() for GtkDialogs. The glue makes the dialogs Settings'
# panels make (a form with a header bar, a plain AdwDialog, alerts, a dialog without a child, a
# dialog with a navigation of its own, GtkDialogs with OK/Cancel) and presents them for widgets
# inside and outside the window's navigation; what the window shows and which signals the
# dialogs get is what the tests check.

import pytest

from gcc_harness import (back, build_panel, call, click, dump, gcc_session, gcc_world,  # noqa: F401
                         settle)

GLUE = r'''
#include "gcc_driver.h"

gboolean cc_plinius_gtk_dialog_present_as_page (GtkDialog *dialog, GtkWidget *parent);

/* A form dialog of a panel (its type name does not start with "Adw") */
#define TEST_TYPE_FORM (test_form_get_type ())
G_DECLARE_FINAL_TYPE (TestForm, test_form, TEST, FORM, AdwDialog)
struct _TestForm { AdwDialog parent_instance; };
G_DEFINE_FINAL_TYPE (TestForm, test_form, ADW_TYPE_DIALOG)
static void test_form_class_init (TestFormClass *klass) {}
static void test_form_init (TestForm *self) {}

static GPtrArray *dialogs;     /* AdwDialog or GtkDialog, owned */
static GPtrArray *events;      /* strings */
static GtkWidget *outside;     /* a widget in another window */
static AdwNavigationPage *held;

static void
event (const char *format, ...)
{
    va_list ap;
    char *text;

    va_start (ap, format);
    text = g_strdup_vprintf (format, ap);
    va_end (ap);
    /* stderr too: what happens while the window is destroyed is after the last step */
    g_printerr ("EVENT %s\n", text);
    g_ptr_array_add (events, text);
}

static int
index_of (gpointer dialog)
{
    for (guint i = 0; i < dialogs->len; i++)
        if (dialogs->pdata[i] == dialog)
            return i;
    return -1;
}

static void
closed_cb (AdwDialog *dialog, gpointer data)
{
    event ("closed %d", index_of (dialog));
    if (g_object_get_data (G_OBJECT (dialog), "close-in-closed")) {
        event ("close %d", adw_dialog_close (dialog));
        adw_dialog_force_close (dialog);
    }
    if (g_object_get_data (G_OBJECT (dialog), "present-again"))
        adw_dialog_present (dialog, GTK_WIDGET (data));
}

static void
close_attempt_cb (AdwDialog *dialog, gpointer data)
{
    event ("close-attempt %d", index_of (dialog));
}

static void
response_cb (GtkDialog *dialog, int response, gpointer data)
{
    event ("response %d %d", index_of (dialog), response);
}

static void
clicked_cb (GtkButton *button, gpointer data)
{
    event ("clicked %s", gtk_button_get_label (button));
}

static gboolean
has (JsonArray *args, const char *option)
{
    for (guint i = 1; i < json_array_get_length (args); i++)
        if (!g_strcmp0 (json_array_get_string_element (args, i), option))
            return TRUE;
    return FALSE;
}

static const char *
option_value (JsonArray *args, const char *prefix)
{
    for (guint i = 1; i < json_array_get_length (args); i++) {
        const char *s = json_array_get_string_element (args, i);

        if (g_str_has_prefix (s, prefix))
            return s + strlen (prefix);
    }
    return NULL;
}

static GtkWidget *
form_child (JsonArray *args, AdwDialog *dialog)
{
    GtkWidget *toolbar = adw_toolbar_view_new ();
    GtkWidget *header = adw_header_bar_new ();
    GtkWidget *box = gtk_box_new (GTK_ORIENTATION_VERTICAL, 6);
    GtkWidget *entry = gtk_entry_new ();
    GtkWidget *top = gtk_button_new_with_label ("Top");
    GtkWidget *save = gtk_button_new_with_label ("Save");
    GtkWidget *other = gtk_button_new_with_label ("Other");

    gtk_widget_set_name (entry, "form-entry");
    gtk_widget_set_name (top, "top");
    gtk_widget_set_name (other, "other");
    gtk_box_append (GTK_BOX (box), top);
    gtk_box_append (GTK_BOX (box), entry);
    gtk_box_append (GTK_BOX (box), other);
    g_signal_connect (save, "clicked", G_CALLBACK (clicked_cb), NULL);
    adw_header_bar_pack_end (ADW_HEADER_BAR (header), save);
    if (has (args, "cancel")) {
        GtkWidget *cancel = gtk_button_new_with_label ("Cancel");

        gtk_actionable_set_action_name (GTK_ACTIONABLE (cancel), "window.close");
        adw_header_bar_pack_start (ADW_HEADER_BAR (header), cancel);
    }
    if (has (args, "hidden-start")) {
        GtkWidget *hidden = gtk_button_new_with_label ("Hidden");

        gtk_widget_set_visible (hidden, FALSE);
        adw_header_bar_pack_start (ADW_HEADER_BAR (header), hidden);
    }
    if (has (args, "no-header"))
        g_object_ref_sink (header), g_object_unref (header);
    else
        adw_toolbar_view_add_top_bar (ADW_TOOLBAR_VIEW (toolbar), header);
    adw_toolbar_view_set_content (ADW_TOOLBAR_VIEW (toolbar), box);
    adw_dialog_set_default_widget (dialog, save);
    if (has (args, "focus-entry"))
        adw_dialog_set_focus (dialog, entry);
    if (has (args, "focus-button"))
        adw_dialog_set_focus (dialog, other);
    return toolbar;
}

/* The eSIM dialog: a navigation of its own, a first page and a subpage */
static GtkWidget *
inner_child (void)
{
    GtkWidget *nav = adw_navigation_view_new ();
    GtkWidget *first = gtk_label_new ("Profiles");
    GtkWidget *second = gtk_label_new ("Add Profile");

    gtk_widget_set_name (nav, "inner");
    adw_navigation_view_set_animate_transitions (ADW_NAVIGATION_VIEW (nav), FALSE);
    /* add() on an empty navigation pushes: the first page first */
    adw_navigation_view_push (ADW_NAVIGATION_VIEW (nav), adw_navigation_page_new_with_tag (first, "Profiles", "first"));
    adw_navigation_view_add (ADW_NAVIGATION_VIEW (nav), adw_navigation_page_new_with_tag (second, "Add", "add"));
    return nav;
}

static GtkWidget *
parent_of (GccTest *t, const char *where)
{
    if (!where || g_str_equal (where, "page"))
        return GTK_WIDGET (t->page);
    if (g_str_equal (where, "window"))
        return GTK_WIDGET (t->window);
    if (g_str_equal (where, "outside"))
        return outside;
    if (g_str_equal (where, "none"))
        return NULL;
    if (g_str_has_prefix (where, "dialog:"))
        return dialogs->pdata[atoi (where + 7)];
    /* A widget of the window outside the navigation view: its header bar's place */
    if (g_str_equal (where, "window-only")) {
        static GtkWidget *loose;

        if (!loose) {
            loose = gtk_label_new ("loose");
            g_object_ref_sink (loose);
        }
        return loose;
    }
    g_error ("no parent %s", where);
}

static AdwDialog *
new_dialog (JsonArray *args)
{
    const char *kind = json_array_get_string_element (args, 0);
    const char *title = option_value (args, "title=");
    AdwDialog *dialog;

    if (g_str_equal (kind, "form")) {
        dialog = g_object_new (TEST_TYPE_FORM, NULL);
        adw_dialog_set_child (dialog, form_child (args, dialog));
    } else if (g_str_equal (kind, "plain")) {
        dialog = adw_dialog_new ();
        adw_dialog_set_child (dialog, form_child (args, dialog));
    } else if (g_str_equal (kind, "empty")) {
        dialog = g_object_new (TEST_TYPE_FORM, NULL);
    } else if (g_str_equal (kind, "inner")) {
        dialog = g_object_new (TEST_TYPE_FORM, NULL);
        adw_dialog_set_child (dialog, inner_child ());
    } else if (g_str_equal (kind, "inner-boxed")) {
        GtkWidget *box = gtk_box_new (GTK_ORIENTATION_VERTICAL, 0);

        dialog = g_object_new (TEST_TYPE_FORM, NULL);
        gtk_box_append (GTK_BOX (box), gtk_label_new ("Above the profiles"));
        gtk_box_append (GTK_BOX (box), inner_child ());
        adw_dialog_set_child (dialog, box);
    } else if (g_str_equal (kind, "alert")) {
        dialog = adw_alert_dialog_new ("Discard changes?", NULL);
        adw_alert_dialog_add_response (ADW_ALERT_DIALOG (dialog), "cancel", "Keep");
    } else {
        g_error ("no dialog kind %s", kind);
    }
    adw_dialog_set_title (dialog, title ? title : "Form");
    if (has (args, "no-close"))
        adw_dialog_set_can_close (dialog, FALSE);
    if (has (args, "close-in-closed"))
        g_object_set_data (G_OBJECT (dialog), "close-in-closed", GINT_TO_POINTER (1));
    if (has (args, "present-again"))
        g_object_set_data (G_OBJECT (dialog), "present-again", GINT_TO_POINTER (1));
    g_signal_connect (dialog, "closed", G_CALLBACK (closed_cb), NULL);
    g_signal_connect (dialog, "close-attempt", G_CALLBACK (close_attempt_cb), NULL);
    return dialog;
}

static GtkDialog *
new_gtk_dialog (JsonArray *args)
{
    GtkWidget *dialog;
    GtkWidget *content, *ok;
    const char *title = option_value (args, "title=");

    G_GNUC_BEGIN_IGNORE_DEPRECATIONS
    dialog = gtk_dialog_new ();
    if (!has (args, "no-title"))
        gtk_window_set_title (GTK_WINDOW (dialog), title ? title : "Wi-Fi Network");
    content = gtk_dialog_get_content_area (GTK_DIALOG (dialog));
    gtk_box_append (GTK_BOX (content), gtk_label_new ("Network Name"));
    gtk_box_append (GTK_BOX (content), gtk_entry_new ());
    gtk_dialog_add_button (GTK_DIALOG (dialog), "_Cancel", GTK_RESPONSE_CANCEL);
    ok = gtk_dialog_add_button (GTK_DIALOG (dialog), "C_onnect", GTK_RESPONSE_OK);
    if (has (args, "insensitive"))
        gtk_widget_set_sensitive (ok, FALSE);
    if (has (args, "icon-apply")) {
        GtkWidget *apply = gtk_button_new_from_icon_name ("object-select-symbolic");

        gtk_dialog_add_action_widget (GTK_DIALOG (dialog), apply, GTK_RESPONSE_APPLY);
    }
    if (has (args, "yes"))
        gtk_dialog_add_button (GTK_DIALOG (dialog), "_Yes", GTK_RESPONSE_YES);
    G_GNUC_END_IGNORE_DEPRECATIONS
    if (has (args, "below"))
        g_object_set_data (G_OBJECT (dialog), "cc-plinius-confirm-below", GINT_TO_POINTER (1));
    g_signal_connect (dialog, "response", G_CALLBACK (response_cb), NULL);
    return GTK_DIALOG (dialog);
}

static AdwHeaderBar *
header_bar_in (GtkWidget *w)
{
    if (ADW_IS_HEADER_BAR (w))
        return ADW_HEADER_BAR (w);
    for (GtkWidget *c = gtk_widget_get_first_child (w); c; c = gtk_widget_get_next_sibling (c)) {
        AdwHeaderBar *bar = header_bar_in (c);

        if (bar)
            return bar;
    }
    return NULL;
}

static void
header_buttons (GtkWidget *w, JsonBuilder *b)
{
    if (GTK_IS_BUTTON (w) && gtk_button_get_label (GTK_BUTTON (w)) && gtk_widget_get_visible (w))
        json_builder_add_string_value (b, gtk_button_get_label (GTK_BUTTON (w)));
    for (GtkWidget *c = gtk_widget_get_first_child (w); c; c = gtk_widget_get_next_sibling (c))
        header_buttons (c, b);
}

static void
add_string (JsonBuilder *b, const char *s)
{
    if (s)
        json_builder_add_string_value (b, s);
    else
        json_builder_add_null_value (b);
}

void
gcc_test_setup (GccTest *t)
{
    GtkWidget *other = gtk_window_new ();

    dialogs = g_ptr_array_new ();
    events = g_ptr_array_new_with_free_func (g_free);
    outside = gtk_label_new ("Elsewhere");
    gtk_window_set_child (GTK_WINDOW (other), outside);
    adw_preferences_page_add (t->page, ADW_PREFERENCES_GROUP (adw_preferences_group_new ()));
}

gboolean
gcc_test_call (GccTest *t, const char *name, JsonArray *args, JsonBuilder *result)
{
    if (g_str_equal (name, "present")) {
        /* present KIND [parent=WHERE] [options]: the index of the dialog */
        AdwDialog *dialog = new_dialog (args);

        g_ptr_array_add (dialogs, g_object_ref_sink (dialog));
        adw_dialog_present (dialog, parent_of (t, option_value (args, "parent=")));
        json_builder_add_int_value (result, dialogs->len - 1);
        return TRUE;
    }
    if (g_str_equal (name, "present_again")) {
        adw_dialog_present (dialogs->pdata[json_array_get_int_element (args, 0)], GTK_WIDGET (t->page));
        json_builder_add_null_value (result);
        return TRUE;
    }
    if (g_str_equal (name, "close") || g_str_equal (name, "force_close")) {
        AdwDialog *dialog = dialogs->pdata[json_array_get_int_element (args, 0)];

        if (g_str_equal (name, "close"))
            json_builder_add_boolean_value (result, adw_dialog_close (dialog));
        else
            adw_dialog_force_close (dialog), json_builder_add_null_value (result);
        return TRUE;
    }
    if (g_str_equal (name, "gtk_present")) {
        /* gtk_present [parent=WHERE] [options]: whether it became a page */
        GtkDialog *dialog = new_gtk_dialog (args);

        /* Owned by GTK's list of windows, as libnma's are: gtk_window_destroy () frees it */
        g_ptr_array_add (dialogs, dialog);
        g_object_add_weak_pointer (G_OBJECT (dialog), &dialogs->pdata[dialogs->len - 1]);
        json_builder_begin_object (result);
        json_builder_set_member_name (result, "index");
        json_builder_add_int_value (result, dialogs->len - 1);
        json_builder_set_member_name (result, "page");
        json_builder_add_boolean_value (result,
            cc_plinius_gtk_dialog_present_as_page (dialog, parent_of (t, option_value (args, "parent="))));
        json_builder_end_object (result);
        return TRUE;
    }
    if (g_str_equal (name, "gtk_destroy")) {
        gtk_window_destroy (dialogs->pdata[json_array_get_int_element (args, 0)]);
        json_builder_add_null_value (result);
        return TRUE;
    }
    if (g_str_equal (name, "gtk_response")) {
        G_GNUC_BEGIN_IGNORE_DEPRECATIONS
        gtk_dialog_response (dialogs->pdata[json_array_get_int_element (args, 0)],
                             json_array_get_int_element (args, 1));
        G_GNUC_END_IGNORE_DEPRECATIONS
        json_builder_add_null_value (result);
        return TRUE;
    }
    if (g_str_equal (name, "set_title")) {
        adw_dialog_set_title (dialogs->pdata[json_array_get_int_element (args, 0)],
                              json_array_get_string_element (args, 1));
        json_builder_add_null_value (result);
        return TRUE;
    }
    if (g_str_equal (name, "set_can_close")) {
        adw_dialog_set_can_close (dialogs->pdata[json_array_get_int_element (args, 0)],
                                  json_array_get_boolean_element (args, 1));
        json_builder_add_null_value (result);
        return TRUE;
    }
    if (g_str_equal (name, "push_plain")) {
        /* Another page of the panel on top (a page that is no dialog) */
        GtkWidget *label = gtk_label_new ("Above");

        adw_navigation_view_push (t->nav, adw_navigation_page_new_with_tag (label, "Above", "above"));
        json_builder_add_null_value (result);
        return TRUE;
    }
    if (g_str_equal (name, "replace")) {
        /* Another panel replaces the stack; the visible page is held first if asked */
        AdwNavigationPage *pages[] = { ADW_NAVIGATION_PAGE (t->panel) };

        if (json_array_get_length (args) && json_array_get_boolean_element (args, 0))
            held = g_object_ref (adw_navigation_view_get_visible_page (t->nav));
        adw_navigation_view_replace (t->nav, pages, 1);
        json_builder_add_null_value (result);
        return TRUE;
    }
    if (g_str_equal (name, "drop_held")) {
        g_clear_object (&held);
        json_builder_add_null_value (result);
        return TRUE;
    }
    if (g_str_equal (name, "inner")) {
        /* The inner navigation: push/pop, and the phases of its gestures */
        GtkWidget *w = NULL;
        g_autoptr(GListModel) controllers = NULL;
        const char *what = json_array_get_string_element (args, 0);
        AdwNavigationPage *visible = adw_navigation_view_get_visible_page (t->nav);

        for (w = GTK_WIDGET (visible); w && !ADW_IS_NAVIGATION_VIEW (w) ; )
            w = gtk_widget_get_first_child (w);
        /* The first navigation view below the visible page's child */
        {
            GtkWidget *stack[64];
            int n = 0;

            w = NULL;
            stack[n++] = GTK_WIDGET (visible);
            while (n && !w) {
                GtkWidget *c = stack[--n];

                if (c != GTK_WIDGET (visible) && ADW_IS_NAVIGATION_VIEW (c)) {
                    w = c;
                    break;
                }
                for (GtkWidget *k = gtk_widget_get_first_child (c); k && n < 64; k = gtk_widget_get_next_sibling (k))
                    stack[n++] = k;
            }
        }
        g_assert (w != NULL);
        if (g_str_equal (what, "push"))
            adw_navigation_view_push_by_tag (ADW_NAVIGATION_VIEW (w), "add");
        else if (g_str_equal (what, "pop"))
            adw_navigation_view_pop (ADW_NAVIGATION_VIEW (w));
        controllers = gtk_widget_observe_controllers (w);
        json_builder_begin_array (result);
        for (guint i = 0; i < g_list_model_get_n_items (controllers); i++) {
            g_autoptr(GtkEventController) c = g_list_model_get_item (controllers, i);

            if (GTK_IS_GESTURE (c))
                json_builder_add_int_value (result, gtk_event_controller_get_propagation_phase (c));
        }
        json_builder_end_array (result);
        return TRUE;
    }
    if (g_str_equal (name, "state")) {
        /* The navigation stack's titles, the visible page's header bar, focus and default */
        g_autoptr(GListModel) stack = adw_navigation_view_get_navigation_stack (t->nav);
        AdwNavigationPage *visible = adw_navigation_view_get_visible_page (t->nav);
        GtkWidget *focus = gtk_root_get_focus (GTK_ROOT (t->window));
        GtkWidget *def = gtk_window_get_default_widget (GTK_WINDOW (t->window));
        AdwHeaderBar *bar = NULL;
        GtkWidget *child = visible ? adw_navigation_page_get_child (visible) : NULL;

        if (child)
            bar = header_bar_in (child);
        json_builder_begin_object (result);
        json_builder_set_member_name (result, "stack");
        json_builder_begin_array (result);
        for (guint i = 0; i < g_list_model_get_n_items (stack); i++) {
            g_autoptr(AdwNavigationPage) p = g_list_model_get_item (stack, i);

            json_builder_add_string_value (result, adw_navigation_page_get_title (p));
        }
        json_builder_end_array (result);
        json_builder_set_member_name (result, "can_pop");
        json_builder_add_boolean_value (result, visible && adw_navigation_page_get_can_pop (visible));
        json_builder_set_member_name (result, "back_button");
        if (bar)
            json_builder_add_boolean_value (result, adw_header_bar_get_show_back_button (bar));
        else
            json_builder_add_null_value (result);
        json_builder_set_member_name (result, "header_buttons");
        json_builder_begin_array (result);
        if (bar)
            header_buttons (GTK_WIDGET (bar), result);
        json_builder_end_array (result);
        json_builder_set_member_name (result, "focus");
        add_string (result, focus ? (gtk_widget_get_name (focus)) : NULL);
        json_builder_set_member_name (result, "focus_type");
        add_string (result, focus ? G_OBJECT_TYPE_NAME (focus) : NULL);
        json_builder_set_member_name (result, "default");
        add_string (result, GTK_IS_BUTTON (def) ? gtk_button_get_label (GTK_BUTTON (def)) : NULL);
        json_builder_set_member_name (result, "dialog");
        add_string (result, adw_application_window_get_visible_dialog (ADW_APPLICATION_WINDOW (t->window))
                                ? adw_dialog_get_title (adw_application_window_get_visible_dialog (
                                      ADW_APPLICATION_WINDOW (t->window)))
                                : NULL);
        json_builder_end_object (result);
        return TRUE;
    }
    if (g_str_equal (name, "has_child")) {
        json_builder_add_boolean_value (result,
            adw_dialog_get_child (dialogs->pdata[json_array_get_int_element (args, 0)]) != NULL);
        return TRUE;
    }
    if (g_str_equal (name, "events")) {
        json_builder_begin_array (result);
        for (guint i = 0; i < events->len; i++)
            json_builder_add_string_value (result, events->pdata[i]);
        json_builder_end_array (result);
        return TRUE;
    }
    return FALSE;
}
'''


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    return build_panel(gcc_session, tmp_path_factory.mktemp("dialog-pages"), "dialog-pages",
                       ["cc-plinius-dialog-pages.c"], GLUE, flags=["-ldl"])


def present(kind, *options, label=None):
    return call("present", kind, *options, label=label or "present")


def gtk_present(*options):
    return call("gtk_present", "gtk", *options, label="gtk")


def state(label="state"):
    return call("state", label=label)


def events(label="events"):
    return call("events", label=label)


def test_form_dialog_becomes_a_page(program, gcc_world):
    r = gcc_world.run(program, [present("form", "title=Edit Rule"), settle(), state(), dump("page")])
    assert r.value("present") == 0
    s = r.value("state")
    assert s["stack"] == ["Test Panel", "Edit Rule"]
    # No sheet: nothing over the window, the dialog's child is in the page
    assert s["dialog"] is None
    assert s["can_pop"] is True
    assert s["back_button"] is True
    # Enter in a field confirms, as in the dialog
    assert s["default"] == "Save"
    page = r.dump("page")
    assert page.one(type="AdwNavigationPage", tag=None, text="Edit Rule")
    assert page.has(type="GtkButton", text="Save")
    assert not r.warnings()


def test_plain_adw_dialog_is_a_form_too(program, gcc_world):
    r = gcc_world.run(program, [present("plain", "title=Password"), settle(), state()])
    assert r.value("state")["stack"] == ["Test Panel", "Password"]
    assert r.value("state")["dialog"] is None


@pytest.mark.parametrize("kind", ["alert", "empty"])
def test_alerts_and_dialogs_without_a_child_stay_dialogs(program, gcc_world, kind):
    r = gcc_world.run(program, [present(kind, "title=Ask"), settle(), state()])
    s = r.value("state")
    assert s["stack"] == ["Test Panel"]
    assert s["dialog"] == "Ask"


@pytest.mark.parametrize("parent", ["outside", "none", "window-only"])
def test_dialog_for_a_widget_outside_the_navigation_stays_a_dialog(program, gcc_world, parent):
    steps = [present("form", "title=Elsewhere", f"parent={parent}"), settle(), state(), dump("screen")]
    r = gcc_world.run(program, steps)
    s = r.value("state")
    assert s["stack"] == ["Test Panel"]
    # libadwaita's own presentation: a sheet over the other window, or a window of its own
    assert r.dump("screen").has(type="TestForm", text="Elsewhere")


def test_dialog_for_the_window_is_a_page(program, gcc_world):
    r = gcc_world.run(program, [present("form", "title=Modem", "parent=window"), settle(), state()])
    assert r.value("state")["stack"] == ["Test Panel", "Modem"]


def test_cancel_in_the_header_bar_hides_the_back_button(program, gcc_world):
    r = gcc_world.run(program, [present("form", "cancel"), settle(), state()])
    assert r.value("state")["back_button"] is False


def test_hidden_start_widgets_do_not_count(program, gcc_world):
    r = gcc_world.run(program, [present("form", "hidden-start"), settle(), state()])
    assert r.value("state")["back_button"] is True


def test_a_child_without_header_bar(program, gcc_world):
    r = gcc_world.run(program, [present("form", "no-header", "title=Bare"), settle(), state()])
    s = r.value("state")
    assert s["stack"] == ["Test Panel", "Bare"]
    assert s["back_button"] is None


@pytest.mark.parametrize("focus, focused", [(None, None), ("focus-button", "other"),
                                             # A text field is not focused: the keyboard is for
                                             # a field the user taps
                                             ("focus-entry", None)])
def test_focus_widget_is_focused_but_not_a_text_field(program, gcc_world, focus, focused):
    r = gcc_world.run(program, [present("form", *([focus] if focus else [])), settle(), state()])
    assert r.value("state")["focus"] == focused


def test_back_closes_the_dialog_and_gives_its_child_back(program, gcc_world):
    r = gcc_world.run(program, [present("form", "title=Edit"), settle(), back(), settle(),
                                state(), events(), call("has_child", 0)])
    assert r.value("state")["stack"] == ["Test Panel"]
    assert r.value("events") == ["closed 0"]
    # The default widget went with the page
    assert r.value("state")["default"] is None
    assert r.value("has_child") is True


def test_close_pops_the_page(program, gcc_world):
    r = gcc_world.run(program, [present("form"), settle(), call("close", 0), settle(), state(),
                                events(), call("close", 0, label="again")])
    assert r.value("close") is True
    assert r.value("state")["stack"] == ["Test Panel"]
    assert r.value("events") == ["closed 0"]
    # The dialog is an ordinary one again: libadwaita answers (it is not presented)
    assert r.value("again") is False


def test_a_dialog_that_may_not_close_only_hears_the_attempt(program, gcc_world):
    r = gcc_world.run(program, [present("form", "no-close"), settle(), state("before"),
                                call("close", 0), settle(), state("after"), events("attempt"),
                                call("force_close", 0), settle(), state("forced"), events()])
    assert r.value("before")["can_pop"] is False
    assert r.value("close") is False
    assert r.value("after")["stack"] == ["Test Panel", "Form"]
    assert r.value("attempt") == ["close-attempt 0"]
    assert r.value("forced")["stack"] == ["Test Panel"]
    assert r.value("events") == ["close-attempt 0", "closed 0"]


def test_can_close_follows_the_dialog(program, gcc_world):
    r = gcc_world.run(program, [present("form"), settle(), call("set_can_close", 0, False), state(),
                                call("set_title", 0, "Renamed"), state("renamed")])
    assert r.value("state")["can_pop"] is False
    assert r.value("renamed")["stack"] == ["Test Panel", "Renamed"]


def test_close_of_a_page_under_another_takes_both(program, gcc_world):
    r = gcc_world.run(program, [present("form", "title=Lower"), settle(), call("push_plain"), settle(),
                                state("two"), call("close", 0), settle(), state(), events()])
    assert r.value("two")["stack"] == ["Test Panel", "Lower", "Above"]
    assert r.value("state")["stack"] == ["Test Panel"]
    assert r.value("events") == ["closed 0"]


def test_pages_above_popped_alone_leave_the_dialog(program, gcc_world):
    r = gcc_world.run(program, [present("form", "title=Lower"), settle(), call("push_plain"), settle(),
                                back(), settle(), state(), events()])
    assert r.value("state")["stack"] == ["Test Panel", "Lower"]
    assert r.value("events") == []


def test_window_close_action_closes_the_dialog_not_settings(program, gcc_world):
    r = gcc_world.run(program, [present("form", "cancel"), settle(), click(type="GtkButton", text="Cancel"),
                                settle(), state(), events()])
    assert r.value("state")["stack"] == ["Test Panel"]
    assert r.value("events") == ["closed 0"]
    assert not [a for a in r.app_actions if a["action"] == "quit"]


def test_presented_again_while_a_page_does_nothing(program, gcc_world):
    r = gcc_world.run(program, [present("form"), settle(), call("present_again", 0), settle(), state()])
    assert r.value("state")["stack"] == ["Test Panel", "Form"]
    assert not r.warnings()


def test_presented_again_while_the_page_goes_is_refused(program, gcc_world):
    # A panel opening the dialog again from its "closed" handler
    r = gcc_world.run(program, [present("form", "present-again"), settle(), back(), settle(), state()])
    assert r.value("state")["stack"] == ["Test Panel"]
    assert [m["message"] for m in r.warnings()] == [
        m["message"] for m in r.messages if "Cannot present TestForm" in m["message"]]
    assert len(r.warnings()) == 1


def test_alert_for_a_page_dialog_is_presented_for_its_page(program, gcc_world):
    r = gcc_world.run(program, [present("form", "title=Edit"), settle(),
                                present("alert", "parent=dialog:0", label="alert"), settle(), state()])
    s = r.value("state")
    assert s["stack"] == ["Test Panel", "Edit"]
    assert s["dialog"] is not None
    assert not r.warnings()


def test_form_for_an_ordinary_dialog_stays_a_dialog(program, gcc_world):
    # The parent is a real (sheet) dialog: no page of ours to stand in for it
    r = gcc_world.run(program, [present("alert"), settle(), present("form", "parent=dialog:0", "title=Inner",
                                                                    label="inner"),
                                settle(), state()], check=False)
    assert r.value("state")["stack"] == ["Test Panel"]


def test_stack_replaced_while_the_page_is_held(program, gcc_world):
    r = gcc_world.run(program, [present("form"), settle(), call("replace", True), settle(),
                                call("close", 0), settle(), events("closed"), call("drop_held"), settle(),
                                events(), call("has_child", 0)])
    # Not on the stack any more and nothing above: closing is decided at once
    assert r.value("close") is True
    assert r.value("closed") == ["closed 0"]
    assert r.value("events") == ["closed 0"]
    assert r.value("has_child") is True


def test_stack_replaced_drops_the_page_and_closes_the_dialog(program, gcc_world):
    r = gcc_world.run(program, [present("form"), settle(), call("replace"), settle(), events(),
                                call("close", 0, label="after")])
    assert r.value("events") == ["closed 0"]
    assert r.value("after") is False


def test_window_destroyed_with_the_page_says_nothing(program, gcc_world):
    r = gcc_world.run(program, [present("form"), settle()])
    assert "EVENT closed" not in r.stderr


@pytest.mark.parametrize("kind", ["inner", "inner-boxed"])
def test_inner_navigation_gives_up_its_swipe_on_its_first_page(program, gcc_world, kind):
    r = gcc_world.run(program, [present(kind, "title=eSIM"), settle(),
                                call("inner", "look", label="first"),
                                call("inner", "push", label="sub"), settle(),
                                call("inner", "look", label="sub-look"),
                                call("inner", "pop", label="back"), settle(),
                                call("inner", "look", label="back-look")])
    none = 0  # GTK_PHASE_NONE
    first = r.value("first")
    assert first and all(p == none for p in first)
    assert all(p != none for p in r.value("sub-look"))
    assert all(p == none for p in r.value("back-look"))


def test_switched_off_everything_stays_a_dialog(program, gcc_world):
    gcc_world.extra_env["PLINIUS_DIALOG_PAGES"] = "0"
    r = gcc_world.run(program, [present("form", "title=Sheet"), settle(), state(),
                                gtk_present()])
    assert r.value("state")["stack"] == ["Test Panel"]
    assert r.value("state")["dialog"] == "Sheet"
    assert r.value("gtk")["page"] is False


# ----- GtkDialog as a page -----

def test_gtk_dialog_becomes_a_page_with_its_confirming_button(program, gcc_world):
    r = gcc_world.run(program, [gtk_present("title=Hidden Network"), settle(),
                                state(), dump("page")])
    assert r.value("gtk") == {"index": 0, "page": True}
    assert r.value("state")["stack"] == ["Test Panel", "Hidden Network"]
    page = r.dump("page")
    ok = page.one(type="GtkButton", text="Connect")
    assert "suggested-action" in ok["classes"]
    assert r.value("state")["header_buttons"] == ["C_onnect"]
    # Cancel is the back button's job
    assert not page.has(type="GtkButton", text="Cancel")
    assert page.has(text="Network Name")


def test_gtk_dialog_confirm_below_the_form(program, gcc_world):
    r = gcc_world.run(program, [gtk_present("below"), settle(), dump("page"),
                                call("state")])
    page = r.dump("page")
    # Below the form, where the thumb is, not in the header bar
    assert r.value("state")["header_buttons"] == []
    texts = [n.get("text") for n in page.all() if n.get("type") in ("GtkButton", "GtkEntry", "GtkLabel")]
    assert texts.index("Connect") > texts.index("Network Name")


def test_gtk_dialog_button_follows_the_original(program, gcc_world):
    r = gcc_world.run(program, [gtk_present("insensitive"), settle(), dump("page")])
    assert r.dump("page").one(type="GtkButton", text="Connect").get("sensitive") is False


def test_gtk_dialog_icon_button_is_not_repeated_and_yes_is(program, gcc_world):
    r = gcc_world.run(program, [gtk_present("icon-apply", "yes"), settle(), dump("page")])
    page = r.dump("page")
    assert page.has(type="GtkButton", text="Yes")
    assert not page.has(type="GtkButton", icon="object-select-symbolic")


def test_gtk_dialog_confirm_answers_and_the_page_goes(program, gcc_world):
    r = gcc_world.run(program, [gtk_present(), settle(),
                                click(type="GtkButton", text="Connect"), settle(), state(), events()])
    assert r.value("state")["stack"] == ["Test Panel"]
    # GTK_RESPONSE_OK = -5, and no "Cancel" after it
    assert r.value("events") == ["response 0 -5"]


def test_gtk_dialog_back_answers_cancel(program, gcc_world):
    r = gcc_world.run(program, [gtk_present("no-title"), settle(), state("open"),
                                back(), settle(), state(), events()])
    assert r.value("open")["stack"] == ["Test Panel", ""]
    assert r.value("state")["stack"] == ["Test Panel"]
    assert r.value("events") == ["response 0 -6"]


def test_gtk_dialog_destroyed_by_its_owner_takes_the_page(program, gcc_world):
    r = gcc_world.run(program, [gtk_present(), settle(), call("gtk_destroy", 0), settle(),
                                state(), events()])
    assert r.value("state")["stack"] == ["Test Panel"]
    assert r.value("events") == []


def test_gtk_dialog_pages_above_popped_alone_leave_it(program, gcc_world):
    r = gcc_world.run(program, [gtk_present(), settle(), call("push_plain"), settle(),
                                back(), settle(), state(), events()])
    assert r.value("state")["stack"] == ["Test Panel", "Wi-Fi Network"]
    assert r.value("events") == []


def test_gtk_dialog_answered_by_its_owner_after_a_replace(program, gcc_world):
    # The page is off the stack: nothing to pop, the answer still reaches the owner
    r = gcc_world.run(program, [gtk_present(), settle(), call("replace", True), settle(),
                                call("gtk_response", 0, -5), settle(), state(), events(), call("drop_held")])
    assert r.value("state")["stack"] == ["Test Panel"]
    assert r.value("events") == ["response 0 -5"]


@pytest.mark.parametrize("parent", ["outside", "none"])
def test_gtk_dialog_outside_the_navigation_is_not_a_page(program, gcc_world, parent):
    r = gcc_world.run(program, [gtk_present(f"parent={parent}"), settle(), state()])
    assert r.value("gtk")["page"] is False
    assert r.value("state")["stack"] == ["Test Panel"]


def test_closing_again_from_the_closed_handler_does_nothing(program, gcc_world):
    r = gcc_world.run(program, [present("form", "close-in-closed"), settle(), back(), settle(), state(),
                                events()])
    assert r.value("state")["stack"] == ["Test Panel"]
    # Once: adw_dialog_close () answers FALSE, the forced close is nothing more
    assert r.value("events") == ["closed 0", "close 0"]
    assert not r.warnings()


def test_force_close_of_an_ordinary_dialog_is_libadwaitas(program, gcc_world):
    r = gcc_world.run(program, [present("alert", "title=Ask"), settle(), state("open"),
                                call("force_close", 0), settle(), state(), events()])
    assert r.value("open")["dialog"] == "Ask"
    assert r.value("state")["dialog"] is None
    assert r.value("events") == ["closed 0"]


def test_gtk_dialog_answered_after_its_page_was_dropped(program, gcc_world):
    # Another panel replaced the stack and the page is gone: the answer still reaches the owner
    r = gcc_world.run(program, [gtk_present(), settle(), call("replace"), settle(),
                                call("gtk_response", 0, -5), settle(), state(), events()])
    assert r.value("state")["stack"] == ["Test Panel"]
    assert r.value("events") == ["response 0 -5"]
    assert not r.warnings()
