/*
 * cc-plinius-buttons.c
 *
 * Plinius: the assistant button on the Power page. plinius-assist (package plinius-assist) runs
 * the action chosen here for a short, a double and a long press (schema
 * org.plinius.assistant-button); "Custom" runs a command of the user's choice, typed in or
 * picked with a file dialog.
 * The group only shows when plinius-assist's schema is installed.
 * Our own strings use the gettext domain plinius-control-center (not GNOME's po/).
 *
 * SPDX-License-Identifier: MIT
 */

#undef G_LOG_DOMAIN
#define G_LOG_DOMAIN "cc-plinius-buttons"

#include "cc-plinius-buttons.h"

#define PLINIUS_DOMAIN "plinius-control-center"
#define P_(String) g_dgettext (PLINIUS_DOMAIN, String)

#define ASSIST_SCHEMA "org.plinius.assistant-button"

/* Actions in the order of the combo rows: the schema's enum nicks */
static const char *const actions[] = { "none", "torch", "screenshot", "camera", "silent", "lock", "custom" };

static gboolean
action_get_mapping (GValue *value, GVariant *variant, gpointer user_data)
{
    const char *action = g_variant_get_string (variant, NULL);

    for (guint i = 0; i < G_N_ELEMENTS (actions); i++) {
        if (g_str_equal (action, actions[i])) {
            g_value_set_uint (value, i);
            return TRUE;
        }
    }
    return FALSE;
}

static GVariant *
action_set_mapping (const GValue *value, const GVariantType *expected_type, gpointer user_data)
{
    guint i = g_value_get_uint (value);

    if (i >= G_N_ELEMENTS (actions))
        return NULL;
    return g_variant_new_string (actions[i]);
}

/* The command row only shows for "Custom" */
static gboolean
custom_visible_mapping (GValue *value, GVariant *variant, gpointer user_data)
{
    g_value_set_boolean (value, g_str_equal (g_variant_get_string (variant, NULL), "custom"));
    return TRUE;
}

static GtkStringList *
action_names (void)
{
    GtkStringList *names = gtk_string_list_new (NULL);

    /* Same order as actions[] */
    gtk_string_list_append (names, P_("Nothing"));
    gtk_string_list_append (names, P_("Torch"));
    gtk_string_list_append (names, P_("Screenshot"));
    gtk_string_list_append (names, P_("Camera"));
    gtk_string_list_append (names, P_("Silent Mode"));
    gtk_string_list_append (names, P_("Lock Screen"));
    gtk_string_list_append (names, P_("Custom"));
    return names;
}

/* An entry row writes its text on "apply" only, not on every key press */
static void
on_command_applied (AdwEntryRow *row, GSettings *settings)
{
    const char *key = g_object_get_data (G_OBJECT (row), "plinius-key");

    g_settings_set_string (settings, key, gtk_editable_get_text (GTK_EDITABLE (row)));
}

/*
 * The command for a picked file. plinius-assist splits the command like a shell, so the path is
 * quoted. A script without the executable bit cannot be started directly: it gets the
 * interpreter from its "#!" line, else sh.
 */
static char *
command_for_file (const char *path)
{
    g_autofree char *quoted = g_shell_quote (path);
    g_autoptr(GFile) file = NULL;
    g_autoptr(GFileInputStream) stream = NULL;
    g_autoptr(GDataInputStream) data = NULL;
    g_autofree char *first_line = NULL;

    if (g_file_test (path, G_FILE_TEST_IS_EXECUTABLE))
        return g_steal_pointer (&quoted);

    file = g_file_new_for_path (path);
    stream = g_file_read (file, NULL, NULL);
    if (stream) {
        data = g_data_input_stream_new (G_INPUT_STREAM (stream));
        first_line = g_data_input_stream_read_line_utf8 (data, NULL, NULL, NULL);
    }
    if (first_line && g_str_has_prefix (first_line, "#!") && *g_strstrip (first_line + 2))
        return g_strdup_printf ("%s %s", first_line + 2, quoted);
    return g_strdup_printf ("sh %s", quoted);
}

static void
on_file_chosen (GObject *source, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(AdwEntryRow) row = user_data;
    g_autoptr(GFile) file = gtk_file_dialog_open_finish (GTK_FILE_DIALOG (source), result, NULL);
    g_autofree char *path = NULL;
    g_autofree char *command = NULL;

    /* Cancelled, or not a local file */
    if (!file || !(path = g_file_get_path (file)))
        return;

    command = command_for_file (path);
    gtk_editable_set_text (GTK_EDITABLE (row), command);
    on_command_applied (row, g_object_get_data (G_OBJECT (row), "plinius-settings"));
}

/* A file dialog for "Custom", starting in the home folder */
static void
on_choose_clicked (GtkButton *button, AdwEntryRow *row)
{
    g_autoptr(GtkFileDialog) dialog = gtk_file_dialog_new ();
    g_autoptr(GFile) home = g_file_new_for_path (g_get_home_dir ());
    GtkRoot *root = gtk_widget_get_root (GTK_WIDGET (row));

    gtk_file_dialog_set_title (dialog, P_("Choose a Program or Script"));
    gtk_file_dialog_set_initial_folder (dialog, home);
    gtk_file_dialog_open (dialog, GTK_IS_WINDOW (root) ? GTK_WINDOW (root) : NULL, NULL,
                          on_file_chosen, g_object_ref (row));
}

static void
add_press_rows (AdwPreferencesGroup *group, GSettings *settings, const char *key, const char *title)
{
    g_autoptr(GtkStringList) names = action_names ();
    g_autofree char *command_key = g_strconcat (key, "-command", NULL);
    g_autofree char *command = g_settings_get_string (settings, command_key);
    GtkWidget *row = adw_combo_row_new ();
    GtkWidget *command_row = adw_entry_row_new ();
    GtkWidget *choose_button = gtk_button_new_from_icon_name ("document-open-symbolic");

    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), title);
    adw_combo_row_set_model (ADW_COMBO_ROW (row), G_LIST_MODEL (names));
    adw_preferences_group_add (group, row);

    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (command_row), P_("Command or Script Path"));
    adw_entry_row_set_show_apply_button (ADW_ENTRY_ROW (command_row), TRUE);
    gtk_editable_set_text (GTK_EDITABLE (command_row), command);
    gtk_widget_set_tooltip_text (choose_button, P_("Choose a Program or Script"));
    gtk_widget_set_valign (choose_button, GTK_ALIGN_CENTER);
    gtk_widget_add_css_class (choose_button, "flat");
    g_signal_connect (choose_button, "clicked", G_CALLBACK (on_choose_clicked), command_row);
    adw_entry_row_add_suffix (ADW_ENTRY_ROW (command_row), choose_button);
    adw_preferences_group_add (group, command_row);

    /* The bindings keep their own references on the settings */
    g_settings_bind_with_mapping (settings, key, row, "selected", G_SETTINGS_BIND_DEFAULT,
                                  action_get_mapping, action_set_mapping, NULL, NULL);
    g_settings_bind_with_mapping (settings, key, command_row, "visible", G_SETTINGS_BIND_GET,
                                  custom_visible_mapping, NULL, NULL, NULL);

    g_object_set_data_full (G_OBJECT (command_row), "plinius-key", g_steal_pointer (&command_key), g_free);
    g_object_set_data_full (G_OBJECT (command_row), "plinius-settings", g_object_ref (settings), g_object_unref);
    g_signal_connect (command_row, "apply", G_CALLBACK (on_command_applied), settings);
}

/**
 * cc_plinius_buttons_setup:
 * @page: the Power panel's general page
 *
 * Adds the assistant button group to @page. Returns %FALSE without plinius-assist.
 */
gboolean
cc_plinius_buttons_setup (AdwPreferencesPage *page)
{
    GSettingsSchemaSource *source = g_settings_schema_source_get_default ();
    g_autoptr(GSettingsSchema) schema = NULL;
    g_autoptr(GSettings) settings = NULL;
    GtkWidget *group;

    if (source)
        schema = g_settings_schema_source_lookup (source, ASSIST_SCHEMA, TRUE);
    if (schema == NULL)
        return FALSE;
    settings = g_settings_new_full (schema, NULL, NULL);

    /* No title: the panel's header bar already says "Assistant Button" */
    group = adw_preferences_group_new ();
    adw_preferences_group_set_description (ADW_PREFERENCES_GROUP (group),
                                           P_("“Custom” runs a program or script of your choice."));
    add_press_rows (ADW_PREFERENCES_GROUP (group), settings, "short-press", P_("Short Press"));
    add_press_rows (ADW_PREFERENCES_GROUP (group), settings, "double-press", P_("Double Press"));
    add_press_rows (ADW_PREFERENCES_GROUP (group), settings, "long-press", P_("Long Press"));
    adw_preferences_page_add (page, ADW_PREFERENCES_GROUP (group));
    return TRUE;
}
