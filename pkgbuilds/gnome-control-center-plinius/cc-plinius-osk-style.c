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
 * The keyboard switches at once, no restart. Nothing of this shows without stevia-plinius.
 * Our own strings use the gettext domain plinius-control-center (not GNOME's po/).
 *
 * SPDX-License-Identifier: MIT
 */

#undef G_LOG_DOMAIN
#define G_LOG_DOMAIN "cc-plinius-osk-style"

#include "cc-plinius-osk-style.h"

#define PLINIUS_DOMAIN "plinius-control-center"
#define P_(String) g_dgettext (PLINIUS_DOMAIN, String)
/* Marks a string for xgettext; translated later through P_() */
#define N_(String) (String)

#define KEYBOARD_SCHEMA "org.plinius.keyboard"
#define KEY_STYLE "style"

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
    return TRUE;
}
