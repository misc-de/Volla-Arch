/*
 * cc-plinius-ambient.c
 *
 * Plinius: the animated effect over the background (plinius-ambient), the group "Animation" at
 * the top of the Background page, apart from the sections of the background itself: the effect
 * (none, snow, dust in the light, fireflies, rain, balls rolling and water running with the tilt)
 * and how many particles it has. The effect is drawn over whatever the background is, the
 * picture, a 360° photo or a video, and stays when that changes. Both rows are bound to
 * org.plinius.ambient, which plinius-ambient follows at once.
 *
 * Nothing of this shows without the org.plinius.ambient schema (plinius-ambient not installed).
 * Our own strings use the gettext domain plinius-control-center (not GNOME's po/).
 *
 * SPDX-License-Identifier: MIT
 */

#undef G_LOG_DOMAIN
#define G_LOG_DOMAIN "cc-plinius-ambient"

#include "cc-plinius-ambient.h"

#define PLINIUS_DOMAIN "plinius-control-center"
#define P_(String) g_dgettext (PLINIUS_DOMAIN, String)
#define N_(String) (String)

#define AMBIENT_SCHEMA "org.plinius.ambient"

typedef struct {
    const char *value; /* in the schema */
    const char *label; /* translated when shown */
} Choice;

/* In the order of the rows; the values as in plinius-ambient's schema */
static const Choice effects[] = {
    { "none", N_("None") },
    { "snow", N_("Snow") },
    { "dust", N_("Dust in the Light") },
    { "fireflies", N_("Fireflies") },
    { "rain", N_("Rain") },
    { "balls", N_("Rolling Balls") },
    { "water", N_("Water") },
};

static const Choice densities[] = {
    { "low", N_("Low") },
    { "medium", N_("Medium") },
    { "high", N_("High") },
};

/* A table of choices for the mappings between the GSettings string and the row's index */
typedef struct {
    const Choice *choices;
    guint n_choices;
} ChoiceTable;

static const ChoiceTable effect_table = { effects, G_N_ELEMENTS (effects) };
static const ChoiceTable density_table = { densities, G_N_ELEMENTS (densities) };

static GSettingsSchema *
ambient_schema (void)
{
    GSettingsSchemaSource *source = g_settings_schema_source_get_default ();

    return source ? g_settings_schema_source_lookup (source, AMBIENT_SCHEMA, TRUE) : NULL;
}

static gboolean
choice_get_mapping (GValue *value, GVariant *variant, gpointer user_data)
{
    const ChoiceTable *table = user_data;
    const char *text = g_variant_get_string (variant, NULL);

    for (guint i = 0; i < table->n_choices; i++) {
        if (g_strcmp0 (text, table->choices[i].value) == 0) {
            g_value_set_uint (value, i);
            return TRUE;
        }
    }
    return FALSE;
}

static GVariant *
choice_set_mapping (const GValue *value, const GVariantType *type, gpointer user_data)
{
    const ChoiceTable *table = user_data;
    guint i = g_value_get_uint (value);

    if (i >= table->n_choices)
        return NULL;
    return g_variant_new_string (table->choices[i].value);
}

/* The amount only matters with an effect */
static gboolean
effect_chosen_mapping (GValue *value, GVariant *variant, gpointer user_data)
{
    g_value_set_boolean (value, g_strcmp0 (g_variant_get_string (variant, NULL), "none") != 0);
    return TRUE;
}

/* The translated name of an effect other than none; NULL for none and for values we do not list
 * ("panorama" of plinius-ambient 0.3, which it turns into panorama-enabled itself) */
static const char *
effect_label (const char *value)
{
    for (guint i = 1; i < G_N_ELEMENTS (effects); i++)
        if (g_strcmp0 (value, effects[i].value) == 0)
            return P_(effects[i].label);
    return NULL;
}

/**
 * cc_plinius_ambient_effect_label:
 *
 * The name of the effect over the background, %NULL without one (or without plinius-ambient).
 */
const char *
cc_plinius_ambient_effect_label (void)
{
    static GSettings *settings;
    static gboolean looked;
    g_autofree char *value = NULL;

    if (!looked) {
        g_autoptr(GSettingsSchema) schema = ambient_schema ();

        looked = TRUE;
        if (schema)
            settings = g_settings_new_full (schema, NULL, NULL);
    }
    if (!settings)
        return NULL;
    value = g_settings_get_string (settings, "effect");
    return effect_label (value);
}

static AdwComboRow *
new_choice_row (AdwPreferencesGroup *group, GSettings *settings, const char *key, const char *title,
                const ChoiceTable *table)
{
    g_autoptr(GtkStringList) labels = gtk_string_list_new (NULL);
    GtkWidget *row = adw_combo_row_new ();

    for (guint i = 0; i < table->n_choices; i++)
        gtk_string_list_append (labels, P_(table->choices[i].label));
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), title);
    adw_combo_row_set_model (ADW_COMBO_ROW (row), G_LIST_MODEL (labels));
    /* The binding keeps its own reference on the settings */
    g_settings_bind_with_mapping (settings, key, row, "selected", G_SETTINGS_BIND_DEFAULT,
                                  choice_get_mapping, choice_set_mapping, (gpointer) table, NULL);
    adw_preferences_group_add (group, row);
    return ADW_COMBO_ROW (row);
}

/**
 * cc_plinius_ambient_group_new:
 *
 * The group "Animation" with the effect and its amount, or %NULL without plinius-ambient.
 */
GtkWidget *
cc_plinius_ambient_group_new (void)
{
    g_autoptr(GSettingsSchema) schema = ambient_schema ();
    g_autoptr(GSettings) settings = NULL;
    AdwPreferencesGroup *group;
    AdwComboRow *amount;

    if (schema == NULL)
        return NULL;
    settings = g_settings_new_full (schema, NULL, NULL);

    group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_group_set_title (group, P_("Animation"));
    new_choice_row (group, settings, "effect", P_("Effect"), &effect_table);
    amount = new_choice_row (group, settings, "density", P_("Amount"), &density_table);
    g_settings_bind_with_mapping (settings, "effect", amount, "sensitive", G_SETTINGS_BIND_GET,
                                  effect_chosen_mapping, NULL, NULL, NULL);
    return GTK_WIDGET (group);
}
