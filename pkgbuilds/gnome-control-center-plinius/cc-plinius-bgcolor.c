/*
 * cc-plinius-bgcolor.c
 *
 * Plinius: the background color on the Style page (Appearance > Style), below the accent color
 * and in the same look: a row of round buttons, nine colors and a color wheel for a color of
 * one's own (GTK's color editor on a page of its own, as for the custom accent color, see
 * cc-plinius-accent.c).
 *
 * The color is GNOME's primary-color (with color-shading-type 'solid') of the background and
 * of the lock screen. Phosh fills the screen with it where no picture is: while picture-options
 * is 'none', which a tap on the background shown sets (Appearance > Background, see
 * cc-background-chooser.c and cc-plinius-panorama.c). Choosing a picture keeps the color
 * (cc-background-panel.c no longer writes the colors of GNOME's wallpapers), and plinius-ambient
 * keeps a 360° photo or a video when only the color changes.
 *
 * The presets are dark enough for Phosh's white clock and labels on them, and differ from the
 * row of accent colors above them. A color that is none of them makes the wheel the active
 * button, with a ring in that color.
 *
 * Our own strings use the gettext domain plinius-control-center (not GNOME's po/).
 *
 * SPDX-License-Identifier: MIT
 */

#undef G_LOG_DOMAIN
#define G_LOG_DOMAIN "cc-plinius-bgcolor"

#include <gdesktop-enums.h>

#include "cc-plinius-accent.h"
#include "cc-plinius-bgcolor.h"

#define PLINIUS_DOMAIN "plinius-control-center"
#define P_(String) g_dgettext (PLINIUS_DOMAIN, String)
#define N_(String) (String)

#define COLOR_KEY "primary-color"
#define SHADING_KEY "color-shading-type"

/* Style classes: every preset, each preset by its number, the wheel */
#define BUTTON_CLASS "plinius-bgcolor"
#define CUSTOM_CLASS "plinius-bgcolor-custom"

/* The background and the lock screen, as the panel sets a picture for both */
static const char * const schemas[] = { "org.gnome.desktop.background", "org.gnome.desktop.screensaver" };

static const struct {
    const char *color;
    const char *name;
} presets[] = {
    { "#000000", N_("Black") },
    { "#1793d1", N_("Arch Blue") },
    { "#1b2a47", N_("Midnight Blue") },
    { "#0e5a63", N_("Deep Teal") },
    { "#2b5b3a", N_("Forest Green") },
    { "#4f2a5e", N_("Plum") },
    { "#6e1f2f", N_("Burgundy") },
    { "#5b4033", N_("Mocha") },
    { "#3b4552", N_("Slate Gray") },
};

typedef struct {
    GtkWidget *box; /* owns this */
    GtkWidget *custom;
    GSettings *background;
    GtkCssProvider *custom_provider; /* the wheel's ring in the custom color */
} BgColor;

/* The color set now; GNOME's default if it cannot be read */
static void
get_color (GSettings *background, GdkRGBA *rgba)
{
    g_autofree char *value = g_settings_get_string (background, COLOR_KEY);

    if (!gdk_rgba_parse (rgba, value))
        gdk_rgba_parse (rgba, "#023c88");
}

/* The preset of the color set now, -1 if it is none of them */
static int
current_preset (GSettings *background, GdkRGBA *rgba)
{
    get_color (background, rgba);
    for (guint i = 0; i < G_N_ELEMENTS (presets); i++) {
        GdkRGBA preset;

        gdk_rgba_parse (&preset, presets[i].color);
        if (gdk_rgba_equal (&preset, rgba))
            return (int) i;
    }
    return -1;
}

/* Both schemas at once, each in one write */
static void
set_color (const GdkRGBA *rgba)
{
    g_autofree char *hex = cc_plinius_color_to_hex (rgba);

    for (guint i = 0; i < G_N_ELEMENTS (schemas); i++) {
        g_autoptr(GSettings) settings = g_settings_new (schemas[i]);
        g_autoptr(GSettingsSchema) schema = NULL;

        g_object_get (settings, "settings-schema", &schema, NULL);
        g_settings_delay (settings);
        if (g_settings_schema_has_key (schema, COLOR_KEY))
            g_settings_set_string (settings, COLOR_KEY, hex);
        if (g_settings_schema_has_key (schema, SHADING_KEY))
            g_settings_set_enum (settings, SHADING_KEY, G_DESKTOP_BACKGROUND_SHADING_SOLID);
        g_settings_apply (settings);
    }
}

/* The presets' colors as GNOME's accent buttons take them; a faint edge, so that a dark color
 * stays visible on a dark row */
static void
ensure_style (void)
{
    static GtkCssProvider *provider;
    g_autoptr(GString) css = NULL;

    if (provider)
        return;

    css = g_string_new (".accent-button." BUTTON_CLASS " {\n"
                        "  outline: 1px solid color-mix(in srgb, currentColor 25%, transparent);\n"
                        "  outline-offset: -4px;\n"
                        "}\n");
    for (guint i = 0; i < G_N_ELEMENTS (presets); i++)
        g_string_append_printf (css, ".accent-button." BUTTON_CLASS "-%u { --accent-bg-color: %s; --accent-color: %s; }\n",
                                i, presets[i].color, presets[i].color);

    provider = gtk_css_provider_new ();
    gtk_css_provider_load_from_string (provider, css->str);
    gtk_style_context_add_provider_for_display (gdk_display_get_default (), GTK_STYLE_PROVIDER (provider),
                                                GTK_STYLE_PROVIDER_PRIORITY_APPLICATION);
}

/* Which button is active, after a change by us or by anyone else */
static void
sync_buttons (BgColor *c)
{
    GdkRGBA rgba;
    int preset = current_preset (c->background, &rgba);
    g_autofree char *hex = cc_plinius_color_to_hex (&rgba);
    g_autofree char *css = g_strdup_printf (".accent-button." CUSTOM_CLASS " { --accent-bg-color: %s; --accent-color: %s; }\n",
                                            hex, hex);
    GtkWidget *child;

    gtk_css_provider_load_from_string (c->custom_provider, css);

    for (child = gtk_widget_get_first_child (c->box); child; child = gtk_widget_get_next_sibling (child)) {
        gboolean active;

        if (child == c->custom)
            active = preset < 0;
        else
            active = GPOINTER_TO_INT (g_object_get_data (G_OBJECT (child), "plinius-bgcolor")) == preset;
        gtk_toggle_button_set_active (GTK_TOGGLE_BUTTON (child), active);
    }
}

static void
preset_clicked_cb (GtkWidget *button)
{
    GdkRGBA rgba;

    gdk_rgba_parse (&rgba, presets[GPOINTER_TO_INT (g_object_get_data (G_OBJECT (button), "plinius-bgcolor"))].color);
    set_color (&rgba);
}

static void
custom_selected_cb (const GdkRGBA *rgba, gpointer data)
{
    set_color (rgba);
}

/* The tap made the wheel active; it stays so only once a color of its own is selected */
static void
custom_clicked_cb (BgColor *c)
{
    GdkRGBA rgba;

    sync_buttons (c);
    get_color (c->background, &rgba);
    cc_plinius_color_page_present (c->box, P_("Background Color"), &rgba, custom_selected_cb, NULL, NULL);
}

static void
bgcolor_free (gpointer data)
{
    BgColor *c = data;

    g_signal_handlers_disconnect_by_data (c->background, c);
    g_object_unref (c->background);
    gtk_style_context_remove_provider_for_display (gdk_display_get_default (),
                                                   GTK_STYLE_PROVIDER (c->custom_provider));
    g_object_unref (c->custom_provider);
    g_free (c);
}

/**
 * cc_plinius_bgcolor_setup:
 * @group: the group of the background color on the Style page (gets its title here)
 * @box: the empty box in it, for the buttons
 */
void
cc_plinius_bgcolor_setup (AdwPreferencesGroup *group, GtkWidget *box)
{
    BgColor *c = g_new0 (BgColor, 1);
    GtkWidget *first = NULL;

    adw_preferences_group_set_title (group, P_("Background Color"));
    adw_preferences_group_set_description (group, P_("Shown when no background is selected"));

    c->box = box;
    c->background = g_settings_new (schemas[0]);
    c->custom_provider = gtk_css_provider_new ();
    gtk_style_context_add_provider_for_display (gdk_display_get_default (), GTK_STYLE_PROVIDER (c->custom_provider),
                                                GTK_STYLE_PROVIDER_PRIORITY_APPLICATION);
    ensure_style ();

    for (guint i = 0; i < G_N_ELEMENTS (presets); i++) {
        GtkWidget *button = gtk_toggle_button_new ();
        g_autofree char *class = g_strdup_printf (BUTTON_CLASS "-%u", i);

        gtk_widget_set_tooltip_text (button, P_(presets[i].name));
        gtk_widget_add_css_class (button, "accent-button");
        gtk_widget_add_css_class (button, BUTTON_CLASS);
        gtk_widget_add_css_class (button, class);
        g_object_set_data (G_OBJECT (button), "plinius-bgcolor", GINT_TO_POINTER (i));
        if (first)
            gtk_toggle_button_set_group (GTK_TOGGLE_BUTTON (button), GTK_TOGGLE_BUTTON (first));
        else
            first = button;
        /* "clicked" is a tap only; "toggled" also comes from syncing */
        g_signal_connect (button, "clicked", G_CALLBACK (preset_clicked_cb), NULL);
        gtk_box_append (GTK_BOX (box), button);
    }

    c->custom = gtk_toggle_button_new ();
    gtk_widget_set_tooltip_text (c->custom, P_("Custom Color"));
    cc_plinius_color_wheel_style (c->custom);
    gtk_widget_add_css_class (c->custom, CUSTOM_CLASS);
    gtk_toggle_button_set_group (GTK_TOGGLE_BUTTON (c->custom), GTK_TOGGLE_BUTTON (first));
    g_signal_connect_swapped (c->custom, "clicked", G_CALLBACK (custom_clicked_cb), c);
    gtk_box_append (GTK_BOX (box), c->custom);

    g_object_set_data_full (G_OBJECT (box), "plinius-bgcolor-data", c, bgcolor_free);

    /* sync_buttons() reads the key, so that GSettings sends its changes */
    g_signal_connect_swapped (c->background, "changed::" COLOR_KEY, G_CALLBACK (sync_buttons), c);
    sync_buttons (c);
}
