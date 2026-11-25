/* Test program of one GNOME Control Center panel file (tests/gcc, see gcc_driver.h and
 * gcc_harness.py).
 *
 * Usage: <program> SCRIPT.json OUT.json
 *
 * The script is {"params": {...}, "steps": [{"op": ...}, ...]}. Steps before {"op": "setup"} run
 * before the panel is made (none: it is made first). OUT gets the dumps, the results of the steps
 * that return something, the panels' visibility set through the shell model, the application
 * actions activated and every warning or critical logged. A failing step ends the program with
 * exit code 3 and the reason on stderr, with the screen's state in OUT. */

#include <gio/gio.h>
#include <glib/gstdio.h>
#include <stdlib.h>
#include <string.h>

#include "gcc_driver.h"
#include "shell/cc-application.h"
#include "shell/cc-shell-model.h"

/* ----- Shell stand-ins ----- */

static JsonBuilder *visibility_log, *action_log;

struct _CcWindow {
    AdwApplicationWindow parent_instance;
    AdwNavigationView *navigation;
};

G_DEFINE_FINAL_TYPE (CcWindow, cc_window, ADW_TYPE_APPLICATION_WINDOW)

static void
cc_window_class_init (CcWindowClass *klass)
{
}

static void
cc_window_init (CcWindow *self)
{
    GtkWidget *nav = adw_navigation_view_new ();

    self->navigation = ADW_NAVIGATION_VIEW (nav);
    /* Pushes and pops finish at once */
    adw_navigation_view_set_animate_transitions (self->navigation, FALSE);
    adw_application_window_set_content (ADW_APPLICATION_WINDOW (self), nav);
}

AdwNavigationView *
cc_window_get_navigation_view (CcWindow *self)
{
    return self->navigation;
}

#ifndef GCC_REAL_SHELL_MODEL
/* (With -DGCC_REAL_SHELL_MODEL the test compiles GNOME's own model, see the stub header) */
struct _CcShellModel {
    GObject parent_instance;
};

G_DEFINE_FINAL_TYPE (CcShellModel, cc_shell_model, G_TYPE_OBJECT)

static void
cc_shell_model_class_init (CcShellModelClass *klass)
{
}

static void
cc_shell_model_init (CcShellModel *self)
{
}

void
cc_shell_model_set_panel_visibility (CcShellModel *self, const gchar *id, CcPanelVisibility visible)
{
    static const char *const names[] = { "hidden", "search", "visible" };

    json_builder_begin_object (visibility_log);
    json_builder_set_member_name (visibility_log, "id");
    json_builder_add_string_value (visibility_log, id);
    json_builder_set_member_name (visibility_log, "visibility");
    json_builder_add_string_value (visibility_log, visible <= CC_PANEL_VISIBLE ? names[visible] : "?");
    json_builder_end_object (visibility_log);
}
#endif

struct _CcApplication {
    AdwApplication parent_instance;
    CcShellModel *model;
};

G_DEFINE_FINAL_TYPE (CcApplication, cc_application, ADW_TYPE_APPLICATION)

static void
on_app_action (GSimpleAction *action, GVariant *parameter, gpointer user_data)
{
    g_autofree char *text = parameter ? g_variant_print (parameter, TRUE) : NULL;

    json_builder_begin_object (action_log);
    json_builder_set_member_name (action_log, "action");
    json_builder_add_string_value (action_log, g_action_get_name (G_ACTION (action)));
    json_builder_set_member_name (action_log, "parameter");
    if (text)
        json_builder_add_string_value (action_log, text);
    else
        json_builder_add_null_value (action_log);
    json_builder_end_object (action_log);
}

static void
cc_application_dispose (GObject *object)
{
    g_clear_object (&CC_APPLICATION (object)->model);
    G_OBJECT_CLASS (cc_application_parent_class)->dispose (object);
}

static void
cc_application_class_init (CcApplicationClass *klass)
{
    G_OBJECT_CLASS (klass)->dispose = cc_application_dispose;
}

static void
cc_application_init (CcApplication *self)
{
    static const GActionEntry entries[] = {
        { "launch-panel", on_app_action, "(sav)" },
        { "quit", on_app_action },
    };

    self->model = g_object_new (CC_TYPE_SHELL_MODEL, NULL);
    g_action_map_add_action_entries (G_ACTION_MAP (self), entries, G_N_ELEMENTS (entries), self);
}

CcShellModel *
cc_application_get_model (CcApplication *self)
{
    return self->model;
}

/* GNOME Settings' own file for the hub panels' titles; the real one when a test compiles it */
__attribute__ ((weak)) char *
cc_plinius_hub_panel_title (const char *panel_id)
{
    return NULL;
}

/* ----- The default test panel ----- */

#define GCC_TYPE_TEST_PANEL (gcc_test_panel_get_type ())
G_DECLARE_FINAL_TYPE (GccTestPanel, gcc_test_panel, GCC, TEST_PANEL, CcPanel)

struct _GccTestPanel {
    CcPanel parent_instance;
    AdwPreferencesPage *page;
};

CC_PANEL_REGISTER (GccTestPanel, gcc_test_panel)

static void
gcc_test_panel_class_init (GccTestPanelClass *klass)
{
}

static void
gcc_test_panel_init (GccTestPanel *self)
{
    GtkWidget *toolbar = adw_toolbar_view_new ();

    self->page = ADW_PREFERENCES_PAGE (adw_preferences_page_new ());
    adw_toolbar_view_add_top_bar (ADW_TOOLBAR_VIEW (toolbar), adw_header_bar_new ());
    adw_toolbar_view_set_content (ADW_TOOLBAR_VIEW (toolbar), GTK_WIDGET (self->page));
    adw_navigation_page_set_child (ADW_NAVIGATION_PAGE (self), toolbar);
    adw_navigation_page_set_title (ADW_NAVIGATION_PAGE (self), "Test Panel");
    adw_navigation_page_set_tag (ADW_NAVIGATION_PAGE (self), "test-panel");
}

__attribute__ ((weak)) CcPanel *
gcc_test_create_panel (GccTest *t)
{
    GccTestPanel *panel = g_object_new (GCC_TYPE_TEST_PANEL, "window", t->window, NULL);

    t->page = panel->page;
    return CC_PANEL (panel);
}

__attribute__ ((weak)) gboolean
gcc_test_call (GccTest *t, const char *name, JsonArray *args, JsonBuilder *result)
{
    return FALSE;
}

const char *
gcc_test_param (GccTest *t, const char *name)
{
    return t->params && json_object_has_member (t->params, name) ? json_object_get_string_member (t->params, name)
                                                                  : NULL;
}

void
gcc_test_add (GccTest *t, GtkWidget *widget)
{
    g_return_if_fail (t->page != NULL);
    if (!widget)
        return;
    if (ADW_IS_PREFERENCES_GROUP (widget)) {
        adw_preferences_page_add (t->page, ADW_PREFERENCES_GROUP (widget));
    } else {
        GtkWidget *group = adw_preferences_group_new ();

        adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), widget);
        adw_preferences_page_add (t->page, ADW_PREFERENCES_GROUP (group));
    }
}

/* ----- Log messages ----- */

static JsonBuilder *message_log;

/* Since GTK 4.20, widgets that show the same symbolic icon share its render node, and GSK's
 * broadway renderer then names that node several times in one frame; gtk4-broadwayd (4.24) aborts
 * on it ("append_node_ops: assertion failed: (!reused_node->consumed)") when the tree around it
 * changes, and every later test of the session finds no display. A filter on each surface's root
 * makes the renderer send the surface as one picture. The tests read widgets, not pixels. */
static void
broadway_flat_surfaces (void)
{
    g_autoptr(GtkCssProvider) css = gtk_css_provider_new ();

    gtk_css_provider_load_from_string (css, "window, popover, tooltip { filter: blur(0.5px); }");
    gtk_style_context_add_provider_for_display (gdk_display_get_default (), GTK_STYLE_PROVIDER (css),
                                                GTK_STYLE_PROVIDER_PRIORITY_USER + 1);
}

static GLogWriterOutput
log_writer (GLogLevelFlags level, const GLogField *fields, gsize n_fields, gpointer user_data)
{
    const char *domain = NULL, *message = NULL;
    const char *name = NULL;

    if (level & G_LOG_LEVEL_ERROR)
        name = "error";
    else if (level & G_LOG_LEVEL_CRITICAL)
        name = "critical";
    else if (level & G_LOG_LEVEL_WARNING)
        name = "warning";
    if (name) {
        for (gsize i = 0; i < n_fields; i++) {
            if (g_str_equal (fields[i].key, "GLIB_DOMAIN"))
                domain = fields[i].value;
            else if (g_str_equal (fields[i].key, "MESSAGE"))
                message = fields[i].value;
        }
        json_builder_begin_object (message_log);
        json_builder_set_member_name (message_log, "level");
        json_builder_add_string_value (message_log, name);
        json_builder_set_member_name (message_log, "domain");
        json_builder_add_string_value (message_log, domain ? domain : "");
        json_builder_set_member_name (message_log, "message");
        json_builder_add_string_value (message_log, message ? message : "");
        json_builder_end_object (message_log);
    }
    return g_log_writer_standard_streams (level, fields, n_fields, user_data);
}

/* ----- What a widget shows ----- */

/* Text as shown: markup parsed, mnemonic underscores gone */
static char *
shown_text (const char *text, gboolean markup, gboolean underline)
{
    g_autofree char *plain = NULL;
    GString *out;

    if (!text)
        return NULL;
    if (markup && strchr (text, '<') == NULL && strchr (text, '&') == NULL)
        markup = FALSE;
    if (markup && !pango_parse_markup (text, -1, 0, NULL, &plain, NULL, NULL))
        plain = NULL;
    if (!plain)
        plain = g_strdup (text);
    if (!underline)
        return g_steal_pointer (&plain);
    out = g_string_new (NULL);
    for (const char *p = plain; *p; p++) {
        if (*p == '_') {
            if (p[1] == '_')
                g_string_append_c (out, '_');
            if (p[1] == '_')
                p++;
            continue;
        }
        g_string_append_c (out, *p);
    }
    return g_string_free (out, FALSE);
}

typedef struct {
    char *text;  /* title, label, heading: what a selector's "text" matches */
    char *subtitle;
} Shown;

static void
shown_clear (Shown *s)
{
    g_clear_pointer (&s->text, g_free);
    g_clear_pointer (&s->subtitle, g_free);
}

static void
shown_of (GtkWidget *w, Shown *s)
{
    memset (s, 0, sizeof *s);
    if (ADW_IS_PREFERENCES_ROW (w)) {
        AdwPreferencesRow *row = ADW_PREFERENCES_ROW (w);

        s->text = shown_text (adw_preferences_row_get_title (row), adw_preferences_row_get_use_markup (row),
                              adw_preferences_row_get_use_underline (row));
        if (ADW_IS_ACTION_ROW (w))
            s->subtitle = shown_text (adw_action_row_get_subtitle (ADW_ACTION_ROW (w)),
                                      adw_preferences_row_get_use_markup (row), FALSE);
        else if (ADW_IS_EXPANDER_ROW (w))
            s->subtitle = shown_text (adw_expander_row_get_subtitle (ADW_EXPANDER_ROW (w)),
                                      adw_preferences_row_get_use_markup (row), FALSE);
    } else if (ADW_IS_PREFERENCES_GROUP (w)) {
        s->text = shown_text (adw_preferences_group_get_title (ADW_PREFERENCES_GROUP (w)), TRUE, FALSE);
        s->subtitle = shown_text (adw_preferences_group_get_description (ADW_PREFERENCES_GROUP (w)), TRUE, FALSE);
    } else if (ADW_IS_NAVIGATION_PAGE (w)) {
        s->text = g_strdup (adw_navigation_page_get_title (ADW_NAVIGATION_PAGE (w)));
    } else if (ADW_IS_STATUS_PAGE (w)) {
        s->text = g_strdup (adw_status_page_get_title (ADW_STATUS_PAGE (w)));
        s->subtitle = shown_text (adw_status_page_get_description (ADW_STATUS_PAGE (w)), TRUE, FALSE);
    } else if (ADW_IS_ALERT_DIALOG (w)) {
        AdwAlertDialog *d = ADW_ALERT_DIALOG (w);

        s->text = shown_text (adw_alert_dialog_get_heading (d), adw_alert_dialog_get_heading_use_markup (d), FALSE);
        s->subtitle = shown_text (adw_alert_dialog_get_body (d), adw_alert_dialog_get_body_use_markup (d), FALSE);
    } else if (ADW_IS_DIALOG (w)) {
        s->text = g_strdup (adw_dialog_get_title (ADW_DIALOG (w)));
    } else if (ADW_IS_BANNER (w)) {
        s->text = shown_text (adw_banner_get_title (ADW_BANNER (w)), adw_banner_get_use_markup (ADW_BANNER (w)), FALSE);
    } else if (ADW_IS_WINDOW_TITLE (w)) {
        s->text = g_strdup (adw_window_title_get_title (ADW_WINDOW_TITLE (w)));
        s->subtitle = g_strdup (adw_window_title_get_subtitle (ADW_WINDOW_TITLE (w)));
    } else if (ADW_IS_BUTTON_CONTENT (w)) {
        s->text = shown_text (adw_button_content_get_label (ADW_BUTTON_CONTENT (w)), FALSE,
                              adw_button_content_get_use_underline (ADW_BUTTON_CONTENT (w)));
    } else if (GTK_IS_LABEL (w)) {
        s->text = g_strdup (gtk_label_get_text (GTK_LABEL (w)));
    } else if (GTK_IS_CHECK_BUTTON (w)) {
        s->text = shown_text (gtk_check_button_get_label (GTK_CHECK_BUTTON (w)), FALSE,
                              gtk_check_button_get_use_underline (GTK_CHECK_BUTTON (w)));
    } else if (GTK_IS_BUTTON (w)) {
        s->text = shown_text (gtk_button_get_label (GTK_BUTTON (w)), FALSE,
                              gtk_button_get_use_underline (GTK_BUTTON (w)));
    } else if (GTK_IS_MENU_BUTTON (w)) {
        s->text = shown_text (gtk_menu_button_get_label (GTK_MENU_BUTTON (w)), FALSE,
                              gtk_menu_button_get_use_underline (GTK_MENU_BUTTON (w)));
    } else if (GTK_IS_EDITABLE (w)) {
        s->text = g_strdup (gtk_editable_get_text (GTK_EDITABLE (w)));
    }
}

/* Labels (and texts) inside a row, group, page or dialog that only show its own title */
static gboolean
own_text_of_ancestor (GtkWidget *w, const char *text)
{
    int depth = 0;

    if (!text)
        return FALSE;
    for (GtkWidget *a = gtk_widget_get_parent (w); a && depth < 8; a = gtk_widget_get_parent (a), depth++) {
        Shown s;
        gboolean same;

        if (!(ADW_IS_PREFERENCES_ROW (a) || ADW_IS_PREFERENCES_GROUP (a) || ADW_IS_STATUS_PAGE (a) ||
              ADW_IS_DIALOG (a) || ADW_IS_BANNER (a) || ADW_IS_WINDOW_TITLE (a) || ADW_IS_BUTTON_CONTENT (a) ||
              GTK_IS_BUTTON (a) || GTK_IS_CHECK_BUTTON (a) || GTK_IS_MENU_BUTTON (a) || GTK_IS_EDITABLE (a)))
            continue;
        shown_of (a, &s);
        same = (s.text && g_str_equal (s.text, text)) || (s.subtitle && g_str_equal (s.subtitle, text));
        shown_clear (&s);
        if (same)
            return TRUE;
        /* A label's own row ends the search: a suffix label of a row is its own text */
        if (ADW_IS_PREFERENCES_ROW (a) || ADW_IS_PREFERENCES_GROUP (a))
            return FALSE;
    }
    return FALSE;
}

static const char *const notable_classes[] = {
    "error", "warning", "success", "accent", "destructive-action", "suggested-action", "dim-label", "dimmed",
    "property", "monospace", "numeric", NULL,
};

static void
add_string (JsonBuilder *b, const char *name, const char *value)
{
    if (!value || !*value)
        return;
    json_builder_set_member_name (b, name);
    json_builder_add_string_value (b, value);
}

static void
add_bool (JsonBuilder *b, const char *name, gboolean value)
{
    json_builder_set_member_name (b, name);
    json_builder_add_boolean_value (b, value);
}

static char *
combo_item_text (AdwComboRow *row, guint position)
{
    g_autoptr(GObject) item = NULL;
    GListModel *model = adw_combo_row_get_model (row);
    GtkExpression *expr = adw_combo_row_get_expression (row);

    if (!model || position == GTK_INVALID_LIST_POSITION)
        return NULL;
    item = g_list_model_get_item (model, position);
    if (!item)
        return NULL;
    if (GTK_IS_STRING_OBJECT (item))
        return g_strdup (gtk_string_object_get_string (GTK_STRING_OBJECT (item)));
    if (expr) {
        g_auto(GValue) value = G_VALUE_INIT;

        if (gtk_expression_evaluate (expr, item, &value) && G_VALUE_HOLDS_STRING (&value))
            return g_value_dup_string (&value);
    }
    return NULL;
}

/* The widget's facts as members of the open object; TRUE if any is worth a node of its own */
static gboolean
describe (GtkWidget *w, JsonBuilder *b)
{
    if (GTK_IS_WINDOW_CONTROLS (w))
        return FALSE;

    gboolean notable = FALSE;
    Shown s;
    const char *name = gtk_widget_get_name (w);
    const char *tooltip = gtk_widget_get_tooltip_text (w);
    g_auto(GStrv) classes = gtk_widget_get_css_classes (w);

    json_builder_set_member_name (b, "type");
    json_builder_add_string_value (b, G_OBJECT_TYPE_NAME (w));
    if (name && !g_str_equal (name, G_OBJECT_TYPE_NAME (w))) {
        add_string (b, "name", name);
        notable = TRUE;
    }
    shown_of (w, &s);
    if (s.text && *s.text) {
        if ((GTK_IS_LABEL (w) || GTK_IS_TEXT (w)) && own_text_of_ancestor (w, s.text)) {
            /* Shown by its row or group: not a node of its own */
            shown_clear (&s);
            return FALSE;
        }
        add_string (b, "text", s.text);
        notable = TRUE;
    }
    if (s.subtitle && *s.subtitle) {
        add_string (b, "subtitle", s.subtitle);
        notable = TRUE;
    }
    shown_clear (&s);
    if (tooltip && *tooltip) {
        add_string (b, "tooltip", tooltip);
        notable = TRUE;
    }
    if (!gtk_widget_is_sensitive (w)) {
        add_bool (b, "sensitive", FALSE);
        notable = TRUE;
    }
    if (classes) {
        gboolean any = FALSE;

        for (int i = 0; classes[i]; i++) {
            if (!g_str_has_prefix (classes[i], "plinius") && !g_strv_contains (notable_classes, classes[i]))
                continue;
            if (!any) {
                json_builder_set_member_name (b, "classes");
                json_builder_begin_array (b);
                any = TRUE;
            }
            json_builder_add_string_value (b, classes[i]);
        }
        if (any) {
            json_builder_end_array (b);
            notable = TRUE;
        }
    }

    if (ADW_IS_NAVIGATION_PAGE (w)) {
        add_string (b, "tag", adw_navigation_page_get_tag (ADW_NAVIGATION_PAGE (w)));
        notable = TRUE;
    }
    if (ADW_IS_PREFERENCES_ROW (w) || ADW_IS_PREFERENCES_GROUP (w) || ADW_IS_STATUS_PAGE (w) || ADW_IS_DIALOG (w) ||
        ADW_IS_TOGGLE_GROUP (w))
        notable = TRUE;
    if (ADW_IS_SWITCH_ROW (w)) {
        add_bool (b, "active", adw_switch_row_get_active (ADW_SWITCH_ROW (w)));
    } else if (ADW_IS_COMBO_ROW (w)) {
        guint sel = adw_combo_row_get_selected (ADW_COMBO_ROW (w));
        g_autofree char *item = combo_item_text (ADW_COMBO_ROW (w), sel);
        GListModel *model = adw_combo_row_get_model (ADW_COMBO_ROW (w));

        json_builder_set_member_name (b, "selected");
        json_builder_add_int_value (b, sel == GTK_INVALID_LIST_POSITION ? -1 : (gint64) sel);
        add_string (b, "selected_text", item);
        if (model) {
            json_builder_set_member_name (b, "items");
            json_builder_begin_array (b);
            for (guint i = 0; i < g_list_model_get_n_items (model); i++) {
                g_autofree char *text = combo_item_text (ADW_COMBO_ROW (w), i);

                json_builder_add_string_value (b, text ? text : "");
            }
            json_builder_end_array (b);
        }
    } else if (ADW_IS_SPIN_ROW (w)) {
        json_builder_set_member_name (b, "value");
        json_builder_add_double_value (b, adw_spin_row_get_value (ADW_SPIN_ROW (w)));
    } else if (ADW_IS_EXPANDER_ROW (w)) {
        add_bool (b, "expanded", adw_expander_row_get_expanded (ADW_EXPANDER_ROW (w)));
    } else if (ADW_IS_ENTRY_ROW (w)) {
        json_builder_set_member_name (b, "value");
        json_builder_add_string_value (b, gtk_editable_get_text (GTK_EDITABLE (w)));
    } else if (GTK_IS_CHECK_BUTTON (w)) {
        add_bool (b, "active", gtk_check_button_get_active (GTK_CHECK_BUTTON (w)));
        notable = TRUE;
    } else if (GTK_IS_TOGGLE_BUTTON (w)) {
        add_bool (b, "active", gtk_toggle_button_get_active (GTK_TOGGLE_BUTTON (w)));
        notable = TRUE;
    } else if (GTK_IS_SWITCH (w)) {
        /* A switch row's own switch: the row says it */
        if (gtk_widget_get_ancestor (w, ADW_TYPE_SWITCH_ROW))
            return notable;
        add_bool (b, "active", gtk_switch_get_active (GTK_SWITCH (w)));
        notable = TRUE;
    } else if (ADW_IS_TOGGLE_GROUP (w)) {
        AdwToggleGroup *g = ADW_TOGGLE_GROUP (w);

        add_string (b, "active_name", adw_toggle_group_get_active_name (g));
        json_builder_set_member_name (b, "active");
        json_builder_add_int_value (b, adw_toggle_group_get_active (g) == GTK_INVALID_LIST_POSITION
                                           ? -1 : (gint64) adw_toggle_group_get_active (g));
        json_builder_set_member_name (b, "toggles");
        json_builder_begin_array (b);
        for (guint i = 0; i < adw_toggle_group_get_n_toggles (g); i++) {
            AdwToggle *toggle = adw_toggle_group_get_toggle (g, i);

            json_builder_add_string_value (b, adw_toggle_get_label (toggle) ? adw_toggle_get_label (toggle)
                                                                            : adw_toggle_get_name (toggle));
        }
        json_builder_end_array (b);
    } else if (GTK_IS_IMAGE (w)) {
        const char *icon = gtk_image_get_icon_name (GTK_IMAGE (w));

        if (icon && !g_str_has_prefix (icon, "go-next") && !g_str_has_prefix (icon, "pan-")) {
            add_string (b, "icon", icon);
            notable = TRUE;
        }
    } else if (GTK_IS_PROGRESS_BAR (w)) {
        json_builder_set_member_name (b, "fraction");
        json_builder_add_double_value (b, gtk_progress_bar_get_fraction (GTK_PROGRESS_BAR (w)));
        notable = TRUE;
    } else if (GTK_IS_LEVEL_BAR (w)) {
        json_builder_set_member_name (b, "value");
        json_builder_add_double_value (b, gtk_level_bar_get_value (GTK_LEVEL_BAR (w)));
        notable = TRUE;
    } else if (GTK_IS_SPINNER (w) || ADW_IS_SPINNER (w)) {
        notable = TRUE;
    } else if (GTK_IS_DRAWING_AREA (w)) {
        notable = TRUE;
    }
    if (GTK_IS_BUTTON (w)) {
        const char *icon = gtk_button_get_icon_name (GTK_BUTTON (w));

        add_string (b, "icon", icon);
        if (icon)
            notable = TRUE;
        if (GTK_IS_LINK_BUTTON (w))
            add_string (b, "uri", gtk_link_button_get_uri (GTK_LINK_BUTTON (w)));
    }
    if (GTK_IS_ACTIONABLE (w) && gtk_actionable_get_action_name (GTK_ACTIONABLE (w))) {
        GVariant *target = gtk_actionable_get_action_target_value (GTK_ACTIONABLE (w));
        g_autofree char *text = target ? g_variant_print (target, FALSE) : NULL;

        add_string (b, "action", gtk_actionable_get_action_name (GTK_ACTIONABLE (w)));
        add_string (b, "target", text);
    }
    return notable;
}

/* Internals of libadwaita's own widgets that are never what a test means */
static gboolean
skip_children (GtkWidget *w)
{
    return GTK_IS_WINDOW_CONTROLS (w) || GTK_IS_SPINNER (w) || ADW_IS_SPINNER (w) || GTK_IS_DRAWING_AREA (w) || GTK_IS_IMAGE (w) ||
           GTK_IS_PROGRESS_BAR (w) || GTK_IS_LEVEL_BAR (w) || GTK_IS_SWITCH (w) || GTK_IS_TEXT (w);
}

static void dump_children (GtkWidget *w, JsonBuilder *b);

static void
dump_widget (GtkWidget *w, JsonBuilder *b)
{
    g_autoptr(JsonBuilder) node = json_builder_new ();
    g_autoptr(JsonBuilder) kids = json_builder_new ();
    g_autoptr(JsonNode) kids_node = NULL;
    g_autoptr(JsonNode) node_node = NULL;
    gboolean notable;

    if (!gtk_widget_get_mapped (w))
        return;
    json_builder_begin_object (node);
    notable = describe (w, node);
    json_builder_begin_array (kids);
    if (!skip_children (w))
        dump_children (w, kids);
    json_builder_end_array (kids);
    kids_node = json_builder_get_root (kids);
    if (!notable) {
        /* Its children take its place */
        JsonArray *a = json_node_get_array (kids_node);

        for (guint i = 0; i < json_array_get_length (a); i++)
            json_builder_add_value (b, json_node_copy (json_array_get_element (a, i)));
        json_builder_end_object (node);
        return;
    }
    if (json_array_get_length (json_node_get_array (kids_node))) {
        json_builder_set_member_name (node, "children");
        json_builder_add_value (node, json_node_copy (kids_node));
    }
    json_builder_end_object (node);
    node_node = json_builder_get_root (node);
    json_builder_add_value (b, g_steal_pointer (&node_node));
}

static void
dump_children (GtkWidget *w, JsonBuilder *b)
{
    for (GtkWidget *c = gtk_widget_get_first_child (w); c; c = gtk_widget_get_next_sibling (c))
        dump_widget (c, b);
}

static void
dump_screen (JsonBuilder *b)
{
    GListModel *toplevels = gtk_window_get_toplevels ();

    json_builder_begin_array (b);
    for (guint i = 0; i < g_list_model_get_n_items (toplevels); i++) {
        g_autoptr(GtkWidget) w = g_list_model_get_item (toplevels, i);

        dump_widget (w, b);
    }
    json_builder_end_array (b);
}

/* ----- Finding widgets ----- */

static gboolean matches (GtkWidget *w, JsonObject *sel);

static gboolean
text_matches (const char *have, JsonObject *sel, const char *key, gboolean contains)
{
    const char *want;

    if (!json_object_has_member (sel, key))
        return TRUE;
    want = json_object_get_string_member (sel, key);
    if (!have)
        return FALSE;
    return contains ? strstr (have, want) != NULL : g_str_equal (have, want);
}

static gboolean
matches (GtkWidget *w, JsonObject *sel)
{
    Shown s;
    gboolean ok = TRUE;

    if (json_object_has_member (sel, "type")) {
        GType type = g_type_from_name (json_object_get_string_member (sel, "type"));

        if (!type || !g_type_is_a (G_OBJECT_TYPE (w), type))
            return FALSE;
    }
    if (json_object_has_member (sel, "name") &&
        g_strcmp0 (gtk_widget_get_name (w), json_object_get_string_member (sel, "name")))
        return FALSE;
    if (json_object_has_member (sel, "tag") &&
        (!ADW_IS_NAVIGATION_PAGE (w) ||
         g_strcmp0 (adw_navigation_page_get_tag (ADW_NAVIGATION_PAGE (w)), json_object_get_string_member (sel, "tag"))))
        return FALSE;
    if (json_object_has_member (sel, "css") && !gtk_widget_has_css_class (w, json_object_get_string_member (sel, "css")))
        return FALSE;
    if (json_object_has_member (sel, "icon")) {
        const char *icon = GTK_IS_BUTTON (w) ? gtk_button_get_icon_name (GTK_BUTTON (w))
                           : GTK_IS_IMAGE (w) ? gtk_image_get_icon_name (GTK_IMAGE (w))
                                              : NULL;

        if (g_strcmp0 (icon, json_object_get_string_member (sel, "icon")))
            return FALSE;
    }
    if (json_object_has_member (sel, "tooltip")) {
        const char *tooltip = gtk_widget_get_tooltip_text (w);

        if (g_strcmp0 (tooltip, json_object_get_string_member (sel, "tooltip")))
            return FALSE;
    }
    /* "sensitive": whether it can be used now (its own state and its parents') */
    if (json_object_has_member (sel, "sensitive") &&
        !gtk_widget_is_sensitive (w) != !json_object_get_boolean_member (sel, "sensitive"))
        return FALSE;
    shown_of (w, &s);
    ok = text_matches (s.text, sel, "text", FALSE) && text_matches (s.text, sel, "contains", TRUE) &&
         text_matches (s.subtitle, sel, "subtitle", FALSE) &&
         text_matches (s.subtitle, sel, "subtitle_contains", TRUE);
    shown_clear (&s);
    if (!ok)
        return FALSE;
    if (json_object_has_member (sel, "within")) {
        JsonObject *within = json_object_get_object_member (sel, "within");

        for (GtkWidget *a = gtk_widget_get_parent (w); a; a = gtk_widget_get_parent (a))
            if (matches (a, within))
                return TRUE;
        return FALSE;
    }
    return TRUE;
}

static void
collect (GtkWidget *w, JsonObject *sel, gboolean hidden, GPtrArray *out)
{
    if (!hidden && !gtk_widget_get_mapped (w))
        return;
    if (matches (w, sel))
        g_ptr_array_add (out, w);
    for (GtkWidget *c = gtk_widget_get_first_child (w); c; c = gtk_widget_get_next_sibling (c))
        collect (c, sel, hidden, out);
}

static GPtrArray *
find_all (JsonObject *sel)
{
    GPtrArray *out = g_ptr_array_new ();
    GListModel *toplevels = gtk_window_get_toplevels ();
    gboolean hidden = json_object_has_member (sel, "hidden") && json_object_get_boolean_member (sel, "hidden");

    for (guint i = 0; i < g_list_model_get_n_items (toplevels); i++) {
        g_autoptr(GtkWidget) w = g_list_model_get_item (toplevels, i);

        collect (w, sel, hidden, out);
    }
    return out;
}

/* ----- Running the script ----- */

static GccTest test;
static JsonBuilder *results;
static char *failure;
static gboolean set_up;

static void G_GNUC_PRINTF (1, 2)
fail (const char *format, ...)
{
    va_list ap;

    if (failure)
        return;
    va_start (ap, format);
    failure = g_strdup_vprintf (format, ap);
    va_end (ap);
}

static char *
selector_text (JsonObject *sel)
{
    g_autoptr(JsonNode) node = json_node_new (JSON_NODE_OBJECT);

    json_node_set_object (node, sel);
    return json_to_string (node, FALSE);
}

static GtkWidget *
find_one (JsonObject *step, const char *key)
{
    JsonObject *sel;
    g_autoptr(GPtrArray) all = NULL;
    g_autofree char *text = NULL;
    gint64 index = -1;

    if (!json_object_has_member (step, key)) {
        fail ("step without \"%s\"", key);
        return NULL;
    }
    sel = json_object_get_object_member (step, key);
    if (json_object_has_member (sel, "index"))
        index = json_object_get_int_member (sel, "index");
    all = find_all (sel);
    text = selector_text (sel);
    if (all->len == 0) {
        fail ("no widget on screen matches %s", text);
        return NULL;
    }
    if (index < 0 && all->len > 1) {
        fail ("%u widgets match %s (give an index or \"within\")", all->len, text);
        return NULL;
    }
    if (index < 0)
        index = 0;
    if (index >= all->len) {
        fail ("only %u widgets match %s", all->len, text);
        return NULL;
    }
    return all->pdata[index];
}

static void
iterate (void)
{
    while (g_main_context_pending (NULL))
        g_main_context_iteration (NULL, FALSE);
}

static void
wait_ms (guint ms)
{
    gint64 end = g_get_monotonic_time () + ms * 1000;

    while (g_get_monotonic_time () < end) {
        if (!g_main_context_iteration (NULL, FALSE))
            g_usleep (1000);
    }
}

/* Until nothing has happened for a while: D-Bus answers, idle callbacks, threads' results */
static void
settle (guint quiet_ms)
{
    gint64 deadline = g_get_monotonic_time () + 10 * G_USEC_PER_SEC;
    gint64 quiet_since = g_get_monotonic_time ();

    while (g_get_monotonic_time () < deadline) {
        if (g_main_context_iteration (NULL, FALSE)) {
            quiet_since = g_get_monotonic_time ();
            continue;
        }
        if (g_get_monotonic_time () - quiet_since >= quiet_ms * 1000)
            return;
        g_usleep (1000);
    }
}

/* What GNOME Settings' window does with a panel's subpages when it shows the panel */
static void
load_subpages (void)
{
    GList *subpages = cc_panel_get_subpages (test.panel);

    for (GList *l = subpages; l; l = l->next) {
        const char *tag = adw_navigation_page_get_tag (l->data);
        AdwNavigationPage *existing = tag ? adw_navigation_view_find_page (test.nav, tag) : NULL;

        if (existing && existing != l->data)
            adw_navigation_view_remove (test.nav, existing);
        if (gtk_widget_get_parent (l->data) != GTK_WIDGET (test.nav))
            adw_navigation_view_add (test.nav, l->data);
    }
    g_list_free (subpages);
}

static void
do_setup (void)
{
    GPtrArray *stack;

    if (set_up)
        return;
    set_up = TRUE;
    test.panel = gcc_test_create_panel (&test);
    gcc_test_setup (&test);
    stack = cc_panel_get_navigation_stack (test.panel);
    adw_navigation_view_replace (test.nav, (AdwNavigationPage **) stack->pdata, stack->len);
    load_subpages ();
    settle (50);
}

static GVariant *
parse_variant (JsonObject *step, const char *key, const char *type_key)
{
    const char *text = json_object_get_string_member (step, key);
    const char *type = json_object_has_member (step, type_key) ? json_object_get_string_member (step, type_key)
                                                                : NULL;
    g_autoptr(GError) error = NULL;
    GVariant *v = g_variant_parse (type ? G_VARIANT_TYPE (type) : NULL, text, NULL, NULL, &error);

    if (!v)
        fail ("cannot parse %s %s: %s", key, text, error->message);
    return v;
}

static void
result_begin (JsonObject *step)
{
    json_builder_begin_object (results);
    json_builder_set_member_name (results, "label");
    json_builder_add_string_value (results, json_object_has_member (step, "label")
                                                ? json_object_get_string_member (step, "label")
                                                : json_object_get_string_member (step, "op"));
    json_builder_set_member_name (results, "value");
}

static void
op_click (JsonObject *step)
{
    GtkWidget *w = find_one (step, "target");

    if (!w)
        return;
    if (!gtk_widget_is_sensitive (w)) {
        fail ("%s is not sensitive", G_OBJECT_TYPE_NAME (w));
        return;
    }
    if (GTK_IS_BUTTON (w))
        g_signal_emit_by_name (w, "clicked");
    else if (GTK_IS_MENU_BUTTON (w))
        gtk_menu_button_popup (GTK_MENU_BUTTON (w));
    else if (ADW_IS_EXPANDER_ROW (w))
        adw_expander_row_set_expanded (ADW_EXPANDER_ROW (w), !adw_expander_row_get_expanded (ADW_EXPANDER_ROW (w)));
    else if (!gtk_widget_activate (w))
        fail ("%s cannot be activated", G_OBJECT_TYPE_NAME (w));
}

static void
op_set_text (JsonObject *step)
{
    GtkWidget *w = find_one (step, "target");

    if (!w)
        return;
    if (!GTK_IS_EDITABLE (w)) {
        fail ("%s is no text field", G_OBJECT_TYPE_NAME (w));
        return;
    }
    gtk_editable_set_text (GTK_EDITABLE (w), json_object_get_string_member (step, "text"));
}

static GtkWidget *
first_text (GtkWidget *w)
{
    if (GTK_IS_TEXT (w))
        return w;
    for (GtkWidget *c = gtk_widget_get_first_child (w); c; c = gtk_widget_get_next_sibling (c)) {
        GtkWidget *t = first_text (c);

        if (t)
            return t;
    }
    return NULL;
}

/* Enter in a text field */
static void
op_enter (JsonObject *step)
{
    GtkWidget *w = find_one (step, "target");
    GtkWidget *text;

    if (!w)
        return;
    text = first_text (w);
    if (!text) {
        fail ("%s has no text field", G_OBJECT_TYPE_NAME (w));
        return;
    }
    g_signal_emit_by_name (text, "activate");
}

/* The apply button of an entry row */
static void
op_apply (JsonObject *step)
{
    GtkWidget *w = find_one (step, "target");

    if (!w)
        return;
    if (!ADW_IS_ENTRY_ROW (w)) {
        fail ("%s is no entry row", G_OBJECT_TYPE_NAME (w));
        return;
    }
    g_signal_emit_by_name (w, "apply");
}

static void
op_set_active (JsonObject *step)
{
    GtkWidget *w = find_one (step, "target");
    gboolean active = json_object_get_boolean_member (step, "active");

    if (!w)
        return;
    if (!gtk_widget_is_sensitive (w)) {
        fail ("%s is not sensitive", G_OBJECT_TYPE_NAME (w));
        return;
    }
    if (ADW_IS_SWITCH_ROW (w))
        adw_switch_row_set_active (ADW_SWITCH_ROW (w), active);
    else if (ADW_IS_EXPANDER_ROW (w))
        adw_expander_row_set_expanded (ADW_EXPANDER_ROW (w), active);
    else if (GTK_IS_CHECK_BUTTON (w))
        gtk_check_button_set_active (GTK_CHECK_BUTTON (w), active);
    else if (GTK_IS_TOGGLE_BUTTON (w))
        gtk_toggle_button_set_active (GTK_TOGGLE_BUTTON (w), active);
    else if (GTK_IS_SWITCH (w))
        gtk_switch_set_active (GTK_SWITCH (w), active);
    else
        fail ("%s cannot be switched", G_OBJECT_TYPE_NAME (w));
}

static void
op_select (JsonObject *step)
{
    GtkWidget *w = find_one (step, "target");

    if (!w)
        return;
    if (!gtk_widget_is_sensitive (w)) {
        fail ("%s is not sensitive", G_OBJECT_TYPE_NAME (w));
        return;
    }
    if (ADW_IS_COMBO_ROW (w)) {
        AdwComboRow *row = ADW_COMBO_ROW (w);
        GListModel *model = adw_combo_row_get_model (row);

        if (json_object_has_member (step, "item")) {
            const char *want = json_object_get_string_member (step, "item");

            for (guint i = 0; model && i < g_list_model_get_n_items (model); i++) {
                g_autofree char *text = combo_item_text (row, i);

                if (!g_strcmp0 (text, want)) {
                    adw_combo_row_set_selected (row, i);
                    return;
                }
            }
            fail ("no item %s in the combo row", want);
            return;
        }
        if (!model || json_object_get_int_member (step, "index") >= g_list_model_get_n_items (model)) {
            fail ("no item %" G_GINT64_FORMAT " in the combo row", json_object_get_int_member (step, "index"));
            return;
        }
        adw_combo_row_set_selected (row, json_object_get_int_member (step, "index"));
    } else if (ADW_IS_TOGGLE_GROUP (w)) {
        AdwToggleGroup *g = ADW_TOGGLE_GROUP (w);

        if (json_object_has_member (step, "name")) {
            adw_toggle_group_set_active_name (g, json_object_get_string_member (step, "name"));
            return;
        }
        if (json_object_has_member (step, "item")) {
            const char *want = json_object_get_string_member (step, "item");

            for (guint i = 0; i < adw_toggle_group_get_n_toggles (g); i++)
                if (!g_strcmp0 (adw_toggle_get_label (adw_toggle_group_get_toggle (g, i)), want)) {
                    adw_toggle_group_set_active (g, i);
                    return;
                }
            fail ("no toggle %s", want);
            return;
        }
        adw_toggle_group_set_active (g, json_object_get_int_member (step, "index"));
    } else if (GTK_IS_DROP_DOWN (w)) {
        gtk_drop_down_set_selected (GTK_DROP_DOWN (w), json_object_get_int_member (step, "index"));
    } else {
        fail ("%s has nothing to select", G_OBJECT_TYPE_NAME (w));
    }
}

static void
op_set_value (JsonObject *step)
{
    GtkWidget *w = find_one (step, "target");
    double value = json_object_get_double_member (step, "value");

    if (!w)
        return;
    if (ADW_IS_SPIN_ROW (w))
        adw_spin_row_set_value (ADW_SPIN_ROW (w), value);
    else if (GTK_IS_SPIN_BUTTON (w))
        gtk_spin_button_set_value (GTK_SPIN_BUTTON (w), value);
    else if (GTK_IS_RANGE (w))
        gtk_range_set_value (GTK_RANGE (w), value);
    else
        fail ("%s has no value", G_OBJECT_TYPE_NAME (w));
}

/* A finger drawn over the target ({"points": [[fx, fy], ...]}, fractions of the widget's width
 * and height): drag-begin at the first point, drag-update at each further one and drag-end at
 * the last, on the widget's first GtkGestureDrag; the main loop runs between the points, so the
 * widget draws what it shows while the finger moves */
static void
op_drag (JsonObject *step)
{
    GtkWidget *w = find_one (step, "target");
    JsonArray *points = json_object_has_member (step, "points") ? json_object_get_array_member (step, "points")
                                                                : NULL;
    g_autoptr(GListModel) controllers = NULL;
    GtkGesture *drag = NULL;
    double x0 = 0, y0 = 0, dx = 0, dy = 0;
    int width, height;

    if (!w)
        return;
    if (!gtk_widget_is_sensitive (w)) {
        fail ("%s is not sensitive", G_OBJECT_TYPE_NAME (w));
        return;
    }
    if (!points || json_array_get_length (points) == 0) {
        fail ("drag without points");
        return;
    }
    controllers = gtk_widget_observe_controllers (w);
    for (guint i = 0; !drag && i < g_list_model_get_n_items (controllers); i++) {
        g_autoptr(GObject) controller = g_list_model_get_item (controllers, i);

        if (GTK_IS_GESTURE_DRAG (controller))
            drag = GTK_GESTURE (controller);
    }
    if (!drag) {
        fail ("%s has no drag gesture", G_OBJECT_TYPE_NAME (w));
        return;
    }
    width = gtk_widget_get_width (w);
    height = gtk_widget_get_height (w);
    for (guint i = 0; i < json_array_get_length (points); i++) {
        JsonArray *point = json_array_get_array_element (points, i);
        double x = json_array_get_double_element (point, 0) * width;
        double y = json_array_get_double_element (point, 1) * height;

        if (i == 0) {
            x0 = x;
            y0 = y;
            g_signal_emit_by_name (drag, "drag-begin", x, y);
        } else {
            dx = x - x0;
            dy = y - y0;
            g_signal_emit_by_name (drag, "drag-update", dx, dy);
        }
        wait_ms (20);
    }
    g_signal_emit_by_name (drag, "drag-end", dx, dy);
}

static void
op_action (JsonObject *step)
{
    GtkWidget *w = json_object_has_member (step, "target") ? find_one (step, "target")
                                                           : GTK_WIDGET (adw_navigation_view_get_visible_page (test.nav));
    g_autoptr(GVariant) target = NULL;

    if (!w)
        return;
    if (json_object_has_member (step, "value")) {
        target = parse_variant (step, "value", "value_type");
        if (!target)
            return;
    }
    if (!gtk_widget_activate_action_variant (w, json_object_get_string_member (step, "name"), target))
        fail ("no action %s for %s", json_object_get_string_member (step, "name"), G_OBJECT_TYPE_NAME (w));
}

static void
op_dbus (JsonObject *step)
{
    const char *bus_name = json_object_has_member (step, "bus") ? json_object_get_string_member (step, "bus")
                                                                : "system";
    g_autoptr(GError) error = NULL;
    g_autoptr(GDBusConnection) bus = g_bus_get_sync (g_str_equal (bus_name, "session") ? G_BUS_TYPE_SESSION
                                                                                       : G_BUS_TYPE_SYSTEM,
                                                     NULL, &error);
    g_autoptr(GVariant) args = NULL;
    g_autoptr(GVariant) reply = NULL;

    if (!bus) {
        fail ("no %s bus: %s", bus_name, error->message);
        return;
    }
    if (json_object_has_member (step, "args")) {
        args = parse_variant (step, "args", "signature");
        if (!args)
            return;
    }
    reply = g_dbus_connection_call_sync (bus, json_object_get_string_member (step, "dest"),
                                         json_object_get_string_member (step, "path"),
                                         json_object_get_string_member (step, "iface"),
                                         json_object_get_string_member (step, "method"), g_steal_pointer (&args), NULL,
                                         G_DBUS_CALL_FLAGS_NONE, 10000, NULL, &error);
    result_begin (step);
    if (reply) {
        g_autofree char *text = g_variant_print (reply, TRUE);

        json_builder_add_string_value (results, text);
    } else {
        json_builder_add_string_value (results, error->message);
    }
    json_builder_end_object (results);
}

static GSettings *
settings_of (JsonObject *step)
{
    GSettingsSchemaSource *source = g_settings_schema_source_get_default ();
    const char *id = json_object_get_string_member (step, "schema");
    g_autoptr(GSettingsSchema) schema = source ? g_settings_schema_source_lookup (source, id, TRUE) : NULL;

    if (!schema) {
        fail ("no schema %s", id);
        return NULL;
    }
    return g_settings_new (id);
}

static void
op_gsettings_set (JsonObject *step)
{
    g_autoptr(GSettings) settings = settings_of (step);
    g_autoptr(GSettingsSchema) schema = NULL;
    g_autoptr(GSettingsSchemaKey) key = NULL;
    g_autoptr(GVariant) value = NULL;
    g_autoptr(GError) error = NULL;

    if (!settings)
        return;
    g_object_get (settings, "settings-schema", &schema, NULL);
    key = g_settings_schema_get_key (schema, json_object_get_string_member (step, "key"));
    value = g_variant_parse (g_settings_schema_key_get_value_type (key), json_object_get_string_member (step, "value"),
                             NULL, NULL, &error);
    if (!value) {
        fail ("bad value: %s", error->message);
        return;
    }
    g_settings_set_value (settings, json_object_get_string_member (step, "key"), value);
}

static void
op_gsettings_get (JsonObject *step)
{
    g_autoptr(GSettings) settings = settings_of (step);
    g_autoptr(GVariant) value = NULL;
    g_autofree char *text = NULL;

    if (!settings)
        return;
    value = g_settings_get_value (settings, json_object_get_string_member (step, "key"));
    text = g_variant_print (value, FALSE);
    result_begin (step);
    json_builder_add_string_value (results, text);
    json_builder_end_object (results);
}

static void
op_call (JsonObject *step)
{
    const char *name = json_object_get_string_member (step, "name");
    JsonArray *args = json_object_has_member (step, "args") ? json_object_get_array_member (step, "args") : NULL;
    g_autoptr(JsonArray) none = json_array_new ();

    result_begin (step);
    if (!gcc_test_call (&test, name, args ? args : none, results)) {
        json_builder_add_null_value (results);
        fail ("the glue knows no call %s", name);
    }
    json_builder_end_object (results);
}

static void
op_dump (JsonObject *step)
{
    result_begin (step);
    dump_screen (results);
    json_builder_end_object (results);
}

/* For writing a test: every widget below the target, mapped or not, with its type and text */
static void
tree_of (GtkWidget *w, GString *out, int depth)
{
    Shown s;

    shown_of (w, &s);
    g_string_append_printf (out, "%*s%s%s%s%s%s\n", depth * 2, "", G_OBJECT_TYPE_NAME (w),
                            gtk_widget_get_mapped (w) ? "" : " (unmapped)",
                            gtk_widget_get_visible (w) ? "" : " (hidden)", s.text ? " " : "", s.text ? s.text : "");
    shown_clear (&s);
    for (GtkWidget *c = gtk_widget_get_first_child (w); c; c = gtk_widget_get_next_sibling (c))
        tree_of (c, out, depth + 1);
}

static void
op_tree (JsonObject *step)
{
    GtkWidget *w = find_one (step, "target");
    g_autoptr(GString) out = g_string_new (NULL);

    if (!w)
        return;
    tree_of (w, out, 0);
    result_begin (step);
    json_builder_add_string_value (results, out->str);
    json_builder_end_object (results);
}

static void
op_wait_for (JsonObject *step)
{
    JsonObject *sel = json_object_get_object_member (step, "target");
    gboolean gone = json_object_has_member (step, "gone") && json_object_get_boolean_member (step, "gone");
    gint64 timeout = json_object_has_member (step, "timeout") ? json_object_get_int_member (step, "timeout") : 5000;
    gint64 end = g_get_monotonic_time () + timeout * 1000;

    while (g_get_monotonic_time () < end) {
        g_autoptr(GPtrArray) all = find_all (sel);

        if (gone ? all->len == 0 : all->len > 0) {
            settle (20);
            return;
        }
        if (!g_main_context_iteration (NULL, FALSE))
            g_usleep (1000);
    }
    {
        g_autofree char *text = selector_text (sel);

        fail ("timeout waiting for %s%s", gone ? "the end of " : "", text);
    }
}

static void
op_close_dialog (void)
{
    GListModel *toplevels = gtk_window_get_toplevels ();

    for (guint i = 0; i < g_list_model_get_n_items (toplevels); i++) {
        g_autoptr(GtkWidget) w = g_list_model_get_item (toplevels, i);

        if (ADW_IS_APPLICATION_WINDOW (w) || ADW_IS_WINDOW (w)) {
            AdwDialog *dialog = ADW_IS_APPLICATION_WINDOW (w)
                                    ? adw_application_window_get_visible_dialog (ADW_APPLICATION_WINDOW (w))
                                    : adw_window_get_visible_dialog (ADW_WINDOW (w));

            if (dialog) {
                adw_dialog_close (dialog);
                return;
            }
        }
    }
    fail ("no dialog open");
}

static void
run_step (JsonObject *step)
{
    const char *op = json_object_get_string_member (step, "op");

    if (!op) {
        fail ("step without op");
        return;
    }
    if (g_str_equal (op, "setup"))
        do_setup ();
    else if (g_str_equal (op, "click"))
        op_click (step);
    else if (g_str_equal (op, "set_text"))
        op_set_text (step);
    else if (g_str_equal (op, "enter"))
        op_enter (step);
    else if (g_str_equal (op, "apply"))
        op_apply (step);
    else if (g_str_equal (op, "set_active"))
        op_set_active (step);
    else if (g_str_equal (op, "select"))
        op_select (step);
    else if (g_str_equal (op, "set_value"))
        op_set_value (step);
    else if (g_str_equal (op, "drag"))
        op_drag (step);
    else if (g_str_equal (op, "action"))
        op_action (step);
    else if (g_str_equal (op, "push"))
        adw_navigation_view_push_by_tag (test.nav, json_object_get_string_member (step, "tag"));
    else if (g_str_equal (op, "back"))
        adw_navigation_view_pop (test.nav);
    else if (g_str_equal (op, "pop_to")) {
        /* Several pages back at once (a long press on the back button) */
        if (!adw_navigation_view_pop_to_tag (test.nav, json_object_get_string_member (step, "tag")))
            fail ("no page %s to go back to", json_object_get_string_member (step, "tag"));
    }
    else if (g_str_equal (op, "close_dialog"))
        op_close_dialog ();
    else if (g_str_equal (op, "wait"))
        wait_ms (json_object_get_int_member (step, "ms"));
    else if (g_str_equal (op, "settle"))
        settle (json_object_has_member (step, "ms") ? json_object_get_int_member (step, "ms") : 50);
    else if (g_str_equal (op, "wait_for"))
        op_wait_for (step);
    else if (g_str_equal (op, "dump"))
        op_dump (step);
    else if (g_str_equal (op, "tree"))
        op_tree (step);
    else if (g_str_equal (op, "dbus"))
        op_dbus (step);
    else if (g_str_equal (op, "gsettings_set"))
        op_gsettings_set (step);
    else if (g_str_equal (op, "gsettings_get"))
        op_gsettings_get (step);
    else if (g_str_equal (op, "call"))
        op_call (step);
    else if (g_str_equal (op, "write_file")) {
        g_autoptr(GError) error = NULL;

        if (!g_file_set_contents (json_object_get_string_member (step, "path"),
                                  json_object_get_string_member (step, "text"), -1, &error))
            fail ("%s", error->message);
    } else if (g_str_equal (op, "remove_file"))
        g_unlink (json_object_get_string_member (step, "path"));
    else if (g_str_equal (op, "subpages"))
        load_subpages ();
    else
        fail ("unknown op %s", op);
    /* A real user is slower than this: what the step started gets its turn */
    if (!failure && set_up && !g_str_equal (op, "wait") && !g_str_equal (op, "dump"))
        iterate ();
}

static gboolean
on_timeout (gpointer data)
{
    g_printerr ("gcc_driver: the whole run took too long\n");
    exit (4);
}

static void
write_out (const char *path)
{
    g_autoptr(JsonBuilder) out = json_builder_new ();
    g_autoptr(JsonGenerator) gen = json_generator_new ();
    g_autoptr(JsonNode) root = NULL;
    g_autoptr(GError) error = NULL;

    json_builder_begin_object (out);
    json_builder_set_member_name (out, "results");
    json_builder_end_array (results);
    json_builder_add_value (out, json_builder_get_root (results));
    json_builder_set_member_name (out, "visibility");
    json_builder_end_array (visibility_log);
    json_builder_add_value (out, json_builder_get_root (visibility_log));
    json_builder_set_member_name (out, "app_actions");
    json_builder_end_array (action_log);
    json_builder_add_value (out, json_builder_get_root (action_log));
    json_builder_set_member_name (out, "messages");
    json_builder_end_array (message_log);
    json_builder_add_value (out, json_builder_get_root (message_log));
    json_builder_set_member_name (out, "failure");
    if (failure)
        json_builder_add_string_value (out, failure);
    else
        json_builder_add_null_value (out);
    json_builder_set_member_name (out, "screen");
    {
        g_autoptr(JsonBuilder) screen = json_builder_new ();

        dump_screen (screen);
        json_builder_add_value (out, json_builder_get_root (screen));
    }
    json_builder_end_object (out);
    root = json_builder_get_root (out);
    json_generator_set_root (gen, root);
    json_generator_set_pretty (gen, TRUE);
    if (!json_generator_to_file (gen, path, &error)) {
        g_printerr ("gcc_driver: %s\n", error->message);
        exit (2);
    }
}

int
main (int argc, char **argv)
{
    g_autoptr(JsonParser) parser = json_parser_new ();
    g_autoptr(GError) error = NULL;
    g_autoptr(CcApplication) app = NULL;
    JsonObject *script;
    JsonArray *steps;
    gboolean explicit_setup = FALSE;

    if (argc != 3) {
        g_printerr ("usage: %s SCRIPT.json OUT.json\n", argv[0]);
        return 2;
    }
    results = json_builder_new ();
    visibility_log = json_builder_new ();
    action_log = json_builder_new ();
    message_log = json_builder_new ();
    json_builder_begin_array (results);
    json_builder_begin_array (visibility_log);
    json_builder_begin_array (action_log);
    json_builder_begin_array (message_log);
    g_log_set_writer_func (log_writer, NULL, NULL);

    if (!json_parser_load_from_file (parser, argv[1], &error)) {
        g_printerr ("gcc_driver: %s\n", error->message);
        return 2;
    }
    script = json_node_get_object (json_parser_get_root (parser));
    steps = json_object_get_array_member (script, "steps");
    test.params = json_object_has_member (script, "params") ? json_object_get_object_member (script, "params")
                                                             : NULL;
    for (guint i = 0; i < json_array_get_length (steps); i++)
        if (!g_strcmp0 (json_object_get_string_member (json_array_get_object_element (steps, i), "op"), "setup"))
            explicit_setup = TRUE;

    adw_init ();
    g_object_set (gtk_settings_get_default (), "gtk-enable-animations", FALSE, NULL);
    broadway_flat_surfaces ();
    g_timeout_add_seconds (json_object_has_member (script, "timeout")
                               ? json_object_get_int_member (script, "timeout") : 60,
                           on_timeout, NULL);

    app = g_object_new (CC_TYPE_APPLICATION, "application-id", "org.gnome.Settings",
                        "flags", G_APPLICATION_NON_UNIQUE, NULL);
    /* Without a session bus (a test of that) the application stays unregistered */
    if (!g_application_register (G_APPLICATION (app), NULL, &error)) {
        g_printerr ("gcc_driver: %s\n", error->message);
        g_clear_error (&error);
    }
    test.window = g_object_new (CC_TYPE_WINDOW, "application", app, "default-width", 360, "default-height", 720,
                                NULL);
    test.nav = cc_window_get_navigation_view (test.window);
    gtk_window_present (GTK_WINDOW (test.window));
    settle (20);

    if (!explicit_setup)
        do_setup ();
    for (guint i = 0; i < json_array_get_length (steps) && !failure; i++)
        run_step (json_array_get_object_element (steps, i));

    write_out (argv[2]);
    if (failure) {
        g_printerr ("gcc_driver: %s\n", failure);
        return 3;
    }
    /* The window goes, and with it the panel and its pages: what they free on the way must not
     * crash (a use after free ends the test with a signal) */
    gtk_window_destroy (GTK_WINDOW (test.window));
    settle (20);
    return 0;
}
