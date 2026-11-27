/*
 * cc-plinius-osk-style.c
 *
 * Plinius: "On-Screen Keyboard" on the Keyboard page, a group right below the input sources
 * with the look of the keyboard (stevia-plinius, setting org.plinius.keyboard style):
 *
 *   Original  Stevia's own dark keys (the default)
 *   Glass     the look of the password prompt at startup: glass keys over the blurred
 *             wallpaper, wider character keys
 *
 * Below it the word suggestions (org.plinius.keyboard word-suggestions, stevia-plinius 0015):
 * a switch, and a row that forgets the words the keyboard learned. Those are files in
 * $XDG_DATA_HOME/stevia/words (<language>.learned); raising forget-learned-words makes a running
 * keyboard drop the ones it holds. Both rows only with a stevia-plinius that has the keys.
 *
 * The keyboard switches at once, no restart. Nothing of this shows without stevia-plinius.
 * Our own strings use the gettext domain plinius-control-center (not GNOME's po/).
 *
 * SPDX-License-Identifier: MIT
 */

#undef G_LOG_DOMAIN
#define G_LOG_DOMAIN "cc-plinius-osk-style"

#include "cc-plinius-osk-style.h"

#include <errno.h>
#include <glib/gstdio.h>

#define PLINIUS_DOMAIN "plinius-control-center"
#define P_(String) g_dgettext (PLINIUS_DOMAIN, String)
/* Marks a string for xgettext; translated later through P_() */
#define N_(String) (String)

#define KEYBOARD_SCHEMA "org.plinius.keyboard"
#define KEY_STYLE "style"
#define KEY_WORD_SUGGESTIONS "word-suggestions"
#define KEY_FORGET "forget-learned-words"

/* Values of the setting, in the order of the combo row */
static const struct {
    const char *value;
    const char *name;
} styles[] = {
    { "glass",    N_("Glass") },
    { "original", N_("Original") },
};

/* Setting → position in the combo row; an unknown value shows the first one */
static gboolean
get_mapping (GValue *value, GVariant *variant, gpointer user_data)
{
    const char *style = g_variant_get_string (variant, NULL);

    for (guint i = 0; i < G_N_ELEMENTS (styles); i++) {
        if (g_strcmp0 (style, styles[i].value) == 0) {
            g_value_set_uint (value, i);
            return TRUE;
        }
    }
    g_value_set_uint (value, 0);
    return TRUE;
}

/* Position in the combo row → setting */
static GVariant *
set_mapping (const GValue *value, const GVariantType *expected_type, gpointer user_data)
{
    guint i = g_value_get_uint (value);

    if (i >= G_N_ELEMENTS (styles))
        return NULL;
    return g_variant_new_string (styles[i].value);
}

/* Removes the learned words and tells a running keyboard to drop them too */
static void
on_forget_activated (AdwButtonRow *row, GSettings *settings)
{
    g_autofree char *dir = g_build_filename (g_get_user_data_dir (), "stevia", "words", NULL);
    g_autoptr(GDir) d = g_dir_open (dir, 0, NULL);
    const char *name;

    while (d && (name = g_dir_read_name (d))) {
        g_autofree char *path = NULL;

        if (!g_str_has_suffix (name, ".learned"))
            continue;
        path = g_build_filename (dir, name, NULL);
        if (g_unlink (path) != 0)
            g_warning ("Removing %s failed: %s", path, g_strerror (errno));
    }
    g_settings_set_uint (settings, KEY_FORGET, g_settings_get_uint (settings, KEY_FORGET) + 1);

    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_("Learned Words Forgotten"));
    gtk_widget_set_sensitive (GTK_WIDGET (row), FALSE);
}


/* The word suggestions' rows, with a stevia-plinius that has them */
static void
add_word_suggestions (AdwPreferencesGroup *group, GSettingsSchema *schema)
{
    g_autoptr(GSettings) settings = NULL;
    GtkWidget *row;

    if (!g_settings_schema_has_key (schema, KEY_WORD_SUGGESTIONS) ||
        !g_settings_schema_has_key (schema, KEY_FORGET))
        return;

    settings = g_settings_new (KEYBOARD_SCHEMA);

    row = adw_switch_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_("Word Suggestions"));
    adw_action_row_set_subtitle (ADW_ACTION_ROW (row),
                                 P_("Suggests words while typing: the most common ones and those you "
                                    "type often"));
    adw_preferences_group_add (group, row);
    g_settings_bind (settings, KEY_WORD_SUGGESTIONS, row, "active", G_SETTINGS_BIND_DEFAULT);

    row = adw_button_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_("Forget Learned Words"));
    gtk_widget_add_css_class (row, "destructive-action");
    adw_preferences_group_add (group, row);
    g_signal_connect_data (row, "activated", G_CALLBACK (on_forget_activated),
                           g_object_ref (settings), (GClosureNotify) (void (*) (void)) g_object_unref,
                           0);
}


/**
 * cc_plinius_osk_style_setup:
 * @page: the Keyboard page
 *
 * Adds the "On-Screen Keyboard" group right below the input sources. Returns %FALSE without
 * stevia-plinius.
 */
gboolean
cc_plinius_osk_style_setup (AdwPreferencesPage *page)
{
    GSettingsSchemaSource *source = g_settings_schema_source_get_default ();
    g_autoptr(GSettingsSchema) schema = NULL;
    g_autoptr(GtkStringList) names = NULL;
    g_autoptr(GSettings) settings = NULL;
    AdwPreferencesGroup *group;
    GtkWidget *row;

    if (source)
        schema = g_settings_schema_source_lookup (source, KEYBOARD_SCHEMA, TRUE);
    if (schema == NULL)
        return FALSE;

    group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_group_set_title (group, P_("On-Screen Keyboard"));
    adw_preferences_page_insert (page, group, 1);

    names = gtk_string_list_new (NULL);
    for (guint i = 0; i < G_N_ELEMENTS (styles); i++)
        gtk_string_list_append (names, P_(styles[i].name));

    row = adw_combo_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_("Style"));
    adw_action_row_set_subtitle (ADW_ACTION_ROW (row),
                                 P_("Glass keys over the blurred wallpaper, as when the phone starts, "
                                    "or the original dark keys"));
    adw_combo_row_set_model (ADW_COMBO_ROW (row), G_LIST_MODEL (names));
    adw_preferences_group_add (group, row);

    /* The binding holds the settings object as long as the row lives */
    settings = g_settings_new (KEYBOARD_SCHEMA);
    g_settings_bind_with_mapping (settings, KEY_STYLE, row, "selected", G_SETTINGS_BIND_DEFAULT,
                                  get_mapping, set_mapping, NULL, NULL);

    add_word_suggestions (group, schema);
    return TRUE;
}
