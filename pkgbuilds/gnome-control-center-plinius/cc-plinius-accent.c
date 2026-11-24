/*
 * cc-plinius-accent.c
 *
 * Plinius: a custom accent color on the Appearance page, as the last button after GNOME's nine.
 *
 * GNOME stores the accent color as one of nine names (org.gnome.desktop.interface accent-color),
 * and libadwaita snaps every color to one of them. The custom color therefore lives in a key of
 * its own, org.plinius.appearance accent-color ("#rrggbb", empty for GNOME's color; the schema
 * comes with phosh-plinius), and reaches the system on several ways:
 * - GNOME's key gets the preset nearest to it, for everything that only knows the nine colors.
 * - Phosh and phoc read the custom key themselves (phosh-plinius, phoc-plinius).
 * - GTK 4 apps (libadwaita) get it from a block in ~/.config/gtk-4.0/gtk.css that overrides the
 *   accent variables; GTK reads that file when an app starts, so apps that are already running
 *   keep the old color until they are started again.
 * - Settings itself shows it at once, through a style provider of its own.
 *
 * The custom button always shows a color wheel; with a custom color it is the active one, with
 * a ring in that color. Tapping it opens a page with GTK's color editor; "Select" sets the
 * color. Tapping one of GNOME's colors drops the custom one. The page and the wheel are also
 * used for the background color (cc-plinius-bgcolor.c).
 *
 * Nothing of this shows without the org.plinius.appearance schema.
 * Our own strings use the gettext domain plinius-control-center (not GNOME's po/).
 *
 * SPDX-License-Identifier: MIT
 */

#undef G_LOG_DOMAIN
#define G_LOG_DOMAIN "cc-plinius-accent"

#include <errno.h>
#include <math.h>
#include <string.h>

#include <glib/gstdio.h>
#include <gdesktop-enums.h>

#include "cc-plinius-accent.h"

#define PLINIUS_DOMAIN "plinius-control-center"
#define P_(String) g_dgettext (PLINIUS_DOMAIN, String)

#define APPEARANCE_SCHEMA "org.plinius.appearance"
#define CUSTOM_KEY "accent-color"
#define INTERFACE_SCHEMA "org.gnome.desktop.interface"
#define INTERFACE_ACCENT_KEY "accent-color"

/* Markers of our block in gtk.css; whatever else is in the file stays */
#define CSS_BEGIN "/* plinius-accent begin: written by Settings (Appearance > Accent Color), do not edit */\n"
#define CSS_END "/* plinius-accent end */\n"

/* Style class of the custom button */
#define CUSTOM_CLASS "plinius-custom"

/* GNOME's accent colors (libadwaita, gnome-shell), in the order of GDesktopAccentColor */
static const char * const preset_colors[] = {
    "#3584e4", "#2190a4", "#3a944a", "#c88800", "#ed5b00", "#e62d42", "#d56199", "#9141ac", "#6f8396",
};

typedef struct {
    GtkWidget *accent_box; /* owns this */
    GtkWidget *button;
    GSettings *appearance;
    GSettings *interface;
    guint update_id;
} Accent;

/* ----- Colors ----- */

/* The custom color, if one is set and valid */
static gboolean
get_custom (GSettings *appearance, GdkRGBA *rgba)
{
    g_autofree char *value = g_settings_get_string (appearance, CUSTOM_KEY);

    return value[0] != '\0' && gdk_rgba_parse (rgba, value);
}

static GDesktopAccentColor
get_preset (GSettings *interface)
{
    guint preset = g_settings_get_enum (interface, INTERFACE_ACCENT_KEY);

    return preset < G_N_ELEMENTS (preset_colors) ? preset : G_DESKTOP_ACCENT_COLOR_BLUE;
}

/* "#rrggbb", as the keys hold colors */
char *
cc_plinius_color_to_hex (const GdkRGBA *rgba)
{
    return g_strdup_printf ("#%02x%02x%02x",
                            (guint) lround (CLAMP (rgba->red, 0, 1) * 255),
                            (guint) lround (CLAMP (rgba->green, 0, 1) * 255),
                            (guint) lround (CLAMP (rgba->blue, 0, 1) * 255));
}

static double
linear_channel (double c)
{
    return c <= 0.04045 ? c / 12.92 : pow ((c + 0.055) / 1.055, 2.4);
}

/* Text on the accent color: white as in libadwaita, unless the color is too light for it.
 * GNOME's yellow, the lightest of its colors, has a contrast of 3.0 to white. */
static const char *
foreground_for (const GdkRGBA *rgba)
{
    double luminance = 0.2126 * linear_channel (rgba->red) +
                       0.7152 * linear_channel (rgba->green) +
                       0.0722 * linear_channel (rgba->blue);

    return 1.05 / (luminance + 0.05) >= 2.9 ? "#ffffff" : "rgba(0, 0, 0, 0.8)";
}

/* @rgba in OKLab, a space where distances match how different colors look */
static void
rgba_to_oklab (const GdkRGBA *rgba, double lab[3])
{
    double r = linear_channel (rgba->red), g = linear_channel (rgba->green), b = linear_channel (rgba->blue);
    double l = cbrt (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
    double m = cbrt (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
    double s = cbrt (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);

    lab[0] = 0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s;
    lab[1] = 1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s;
    lab[2] = 0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s;
}

/* GNOME's color closest to @rgba. Its colors are all about equally light, so the hue counts
 * more than the lightness: a dark purple is purple, not the green that is nearer in RGB. */
static GDesktopAccentColor
nearest_preset (const GdkRGBA *rgba)
{
    GDesktopAccentColor nearest = G_DESKTOP_ACCENT_COLOR_BLUE;
    double best = G_MAXDOUBLE;
    double lab[3];
    guint i;

    rgba_to_oklab (rgba, lab);

    for (i = 0; i < G_N_ELEMENTS (preset_colors); i++) {
        GdkRGBA preset;
        double preset_lab[3];
        double distance;

        gdk_rgba_parse (&preset, preset_colors[i]);
        rgba_to_oklab (&preset, preset_lab);
        distance = 0.1 * (preset_lab[0] - lab[0]) * (preset_lab[0] - lab[0]) +
                   (preset_lab[1] - lab[1]) * (preset_lab[1] - lab[1]) +
                   (preset_lab[2] - lab[2]) * (preset_lab[2] - lab[2]);
        if (distance < best) {
            best = distance;
            nearest = i;
        }
    }

    return nearest;
}

/* Sets the custom color, and GNOME's key to the preset nearest to it */
static void
set_custom (GSettings *appearance, GSettings *interface, const GdkRGBA *rgba)
{
    g_autofree char *hex = cc_plinius_color_to_hex (rgba);
    GDesktopAccentColor nearest = nearest_preset (rgba);

    /* The custom key first: whoever reacts to GNOME's key finds the custom color already set */
    g_settings_set_string (appearance, CUSTOM_KEY, hex);
    if (get_preset (interface) != nearest)
        g_settings_set_enum (interface, INTERFACE_ACCENT_KEY, nearest);
}

/* ----- Applying the color ----- */

/* Our block in ~/.config/gtk-4.0/gtk.css: written with a custom color, removed without one */
static void
write_gtk_css (const char *background, const char *foreground)
{
    g_autofree char *dir = g_build_filename (g_get_user_config_dir (), "gtk-4.0", NULL);
    g_autofree char *path = g_build_filename (dir, "gtk.css", NULL);
    g_autofree char *old = NULL;
    g_autofree char *rest = NULL;
    g_autoptr(GString) css = NULL;
    g_autoptr(GError) error = NULL;
    const char *begin, *end;

    if (!g_file_get_contents (path, &old, NULL, NULL))
        g_clear_pointer (&old, g_free);

    /* The file without our block */
    css = g_string_new (old);
    begin = old ? strstr (old, CSS_BEGIN) : NULL;
    end = begin ? strstr (begin, CSS_END) : NULL;
    if (end)
        g_string_erase (css, begin - old, end + strlen (CSS_END) - begin);

    if (background) {
        if (css->len > 0 && css->str[css->len - 1] != '\n')
            g_string_append_c (css, '\n');
        g_string_append_printf (css,
                                CSS_BEGIN
                                "@define-color accent_bg_color %s;\n"
                                "@define-color accent_fg_color %s;\n"
                                ":root {\n"
                                "  --accent-bg-color: %s;\n"
                                "  --accent-fg-color: %s;\n"
                                "}\n"
                                CSS_END,
                                background, foreground, background, foreground);
    }

    if (g_strcmp0 (css->str, old ? old : "") == 0)
        return;

    /* Nothing else was in the file: no empty file stays behind */
    rest = g_strstrip (g_strdup (css->str));
    if (rest[0] == '\0') {
        if (old && g_unlink (path) != 0)
            g_warning ("Could not remove %s: %s", path, g_strerror (errno));
        return;
    }

    if (g_mkdir_with_parents (dir, 0700) != 0 ||
        !g_file_set_contents (path, css->str, css->len, &error))
        g_warning ("Could not write %s: %s", path, error ? error->message : g_strerror (errno));
}

/* Settings itself: an app reads gtk.css only when it starts. Above the user's gtk.css, which may
 * still hold an older color from that start. */
static void
apply_to_settings (const char *background, const char *foreground)
{
    static GtkCssProvider *provider;
    g_autofree char *css = NULL;

    if (!provider) {
        provider = gtk_css_provider_new ();
        gtk_style_context_add_provider_for_display (gdk_display_get_default (),
                                                    GTK_STYLE_PROVIDER (provider),
                                                    GTK_STYLE_PROVIDER_PRIORITY_USER + 1);
    }

    css = g_strdup_printf ("@define-color accent_bg_color %s;\n"
                           "@define-color accent_fg_color %s;\n"
                           ":root { --accent-bg-color: %s; --accent-fg-color: %s; }\n",
                           background, foreground, background, foreground);
    gtk_css_provider_load_from_string (provider, css);
}

/**
 * cc_plinius_color_wheel_style:
 * @button: a toggle button
 *
 * The look of a custom color button: GNOME's round accent button with a color wheel in it (the
 * ring of the active button is the button's --accent-bg-color, the accent color unless the
 * button sets one, as on GNOME's colors).
 */
void
cc_plinius_color_wheel_style (GtkWidget *button)
{
    static GtkCssProvider *provider;

    gtk_widget_add_css_class (button, "accent-button");
    gtk_widget_add_css_class (button, CUSTOM_CLASS);
    if (provider)
        return;

    provider = gtk_css_provider_new ();
    gtk_css_provider_load_from_string (provider,
                                       ".accent-button." CUSTOM_CLASS " {\n"
                                       "  background-image: conic-gradient(#e62d42, #ed5b00, #c88800, #3a944a, #2190a4,\n"
                                       "                                   #3584e4, #9141ac, #d56199, #e62d42);\n"
                                       "}\n");
    gtk_style_context_add_provider_for_display (gdk_display_get_default (), GTK_STYLE_PROVIDER (provider),
                                                GTK_STYLE_PROVIDER_PRIORITY_APPLICATION);
}

/* After a change of either key, by us or by anyone else */
static void
update (Accent *a)
{
    GdkRGBA rgba;
    g_autofree char *hex = NULL;
    gboolean custom = get_custom (a->appearance, &rgba);

    if (!custom)
        gdk_rgba_parse (&rgba, preset_colors[get_preset (a->interface)]);
    hex = cc_plinius_color_to_hex (&rgba);

    write_gtk_css (custom ? hex : NULL, foreground_for (&rgba));
    apply_to_settings (hex, custom ? foreground_for (&rgba) : "#ffffff");

    cc_plinius_accent_sync (a->accent_box);
}

static void
update_idle_cb (gpointer data)
{
    Accent *a = data;

    a->update_id = 0;
    update (a);
}

/* A tap on a preset changes GNOME's key and then drops the custom color: one update for both */
static void
queue_update (Accent *a)
{
    if (!a->update_id)
        a->update_id = g_idle_add_once (update_idle_cb, a);
}

/* ----- The color page ----- */

typedef struct {
    GtkColorChooser *chooser;
    CcPliniusColorSelected selected;
    gpointer data;
    GDestroyNotify destroy;
} ColorPage;

static void
color_page_free (gpointer data)
{
    ColorPage *page = data;

    if (page->destroy)
        page->destroy (page->data);
    g_free (page);
}

static void
select_clicked_cb (AdwDialog *dialog)
{
    ColorPage *page = g_object_get_data (G_OBJECT (dialog), "plinius-color-page");
    GdkRGBA rgba;

    gtk_color_chooser_get_rgba (page->chooser, &rgba);
    page->selected (&rgba, page->data);
    adw_dialog_close (dialog);
}

/**
 * cc_plinius_color_page_present:
 * @parent: a widget of the window to show the page in
 * @title: the page's title
 * @rgba: the color the editor starts with
 * @selected: called with the color when "Select" is tapped
 * @data: (closure): for @selected; the page may outlive @parent, so it should hold what
 *   @selected needs itself
 * @destroy: frees @data when the page is gone
 *
 * GTK's color editor (plane, hue and hex entry) on a page of its own; the back button and the
 * back swipe leave it without a change. Used for the custom accent color and the background
 * color (cc-plinius-bgcolor.c).
 */
void
cc_plinius_color_page_present (GtkWidget *parent, const char *title, const GdkRGBA *rgba,
                               CcPliniusColorSelected selected, gpointer data, GDestroyNotify destroy)
{
    AdwDialog *dialog = adw_dialog_new ();
    GtkWidget *toolbar, *header, *select, *scrolled, *clamp, *chooser;
    ColorPage *page;

    adw_dialog_set_title (dialog, title);
    adw_dialog_set_content_width (dialog, 420);

    header = adw_header_bar_new ();
    adw_header_bar_set_show_end_title_buttons (ADW_HEADER_BAR (header), FALSE);
    select = gtk_button_new_with_mnemonic (P_("_Select"));
    gtk_widget_add_css_class (select, "suggested-action");
    g_signal_connect_swapped (select, "clicked", G_CALLBACK (select_clicked_cb), dialog);
    adw_header_bar_pack_end (ADW_HEADER_BAR (header), select);

    /* GtkColorChooserWidget is deprecated without a successor that can be embedded */
    G_GNUC_BEGIN_IGNORE_DEPRECATIONS
    chooser = gtk_color_chooser_widget_new ();
    g_object_set (chooser, "show-editor", TRUE, NULL);
    gtk_color_chooser_set_use_alpha (GTK_COLOR_CHOOSER (chooser), FALSE);
    gtk_color_chooser_set_rgba (GTK_COLOR_CHOOSER (chooser), rgba);
    G_GNUC_END_IGNORE_DEPRECATIONS
    gtk_widget_set_margin_top (chooser, 12);
    gtk_widget_set_margin_bottom (chooser, 12);
    gtk_widget_set_margin_start (chooser, 12);
    gtk_widget_set_margin_end (chooser, 12);

    clamp = adw_clamp_new ();
    adw_clamp_set_child (ADW_CLAMP (clamp), chooser);
    scrolled = gtk_scrolled_window_new ();
    gtk_scrolled_window_set_policy (GTK_SCROLLED_WINDOW (scrolled), GTK_POLICY_NEVER, GTK_POLICY_AUTOMATIC);
    gtk_scrolled_window_set_propagate_natural_height (GTK_SCROLLED_WINDOW (scrolled), TRUE);
    gtk_scrolled_window_set_child (GTK_SCROLLED_WINDOW (scrolled), clamp);

    toolbar = adw_toolbar_view_new ();
    adw_toolbar_view_add_top_bar (ADW_TOOLBAR_VIEW (toolbar), header);
    adw_toolbar_view_set_content (ADW_TOOLBAR_VIEW (toolbar), scrolled);
    adw_dialog_set_child (dialog, toolbar);
    adw_dialog_set_default_widget (dialog, select);

    page = g_new0 (ColorPage, 1);
    page->chooser = GTK_COLOR_CHOOSER (chooser);
    page->selected = selected;
    page->data = data;
    page->destroy = destroy;
    g_object_set_data_full (G_OBJECT (dialog), "plinius-color-page", page, color_page_free);

    adw_dialog_present (dialog, parent);
}

/* The accent page keeps its own settings (it may outlive the Appearance page) */
typedef struct {
    GSettings *appearance;
    GSettings *interface;
} AccentTarget;

static void
accent_target_free (gpointer data)
{
    AccentTarget *target = data;

    g_object_unref (target->appearance);
    g_object_unref (target->interface);
    g_free (target);
}

static void
accent_selected_cb (const GdkRGBA *rgba, gpointer data)
{
    AccentTarget *target = data;

    set_custom (target->appearance, target->interface, rgba);
}

static void
open_color_page (Accent *a)
{
    AccentTarget *target = g_new0 (AccentTarget, 1);
    GdkRGBA rgba;

    if (!get_custom (a->appearance, &rgba))
        gdk_rgba_parse (&rgba, preset_colors[get_preset (a->interface)]);

    target->appearance = g_object_ref (a->appearance);
    target->interface = g_object_ref (a->interface);
    cc_plinius_color_page_present (a->accent_box, P_("Custom Color"), &rgba, accent_selected_cb, target,
                                   accent_target_free);
}

/* ----- Buttons ----- */

/* The tap made the custom button active; it stays so only once a color is selected */
static void
custom_clicked_cb (Accent *a)
{
    cc_plinius_accent_sync (a->accent_box);
    open_color_page (a);
}

/* One of GNOME's colors (the panel sets GNOME's key when it gets active) */
static void
preset_clicked_cb (Accent *a)
{
    g_autofree char *value = g_settings_get_string (a->appearance, CUSTOM_KEY);

    if (value[0] != '\0')
        g_settings_reset (a->appearance, CUSTOM_KEY);
}

static void
accent_free (Accent *a)
{
    /* The color page holds the same settings and may still change them */
    g_signal_handlers_disconnect_by_data (a->appearance, a);
    g_signal_handlers_disconnect_by_data (a->interface, a);
    g_clear_handle_id (&a->update_id, g_source_remove);
    g_clear_object (&a->appearance);
    g_clear_object (&a->interface);
    g_free (a);
}

/* Which button is active: the custom one with a custom color, else GNOME's color.
 * FALSE if there is no custom button, then the panel does that itself. */
gboolean
cc_plinius_accent_sync (GtkWidget *accent_box)
{
    Accent *a = g_object_get_data (G_OBJECT (accent_box), "plinius-accent");
    GtkWidget *child;
    GdkRGBA rgba;
    gboolean custom;
    GDesktopAccentColor preset;

    if (!a)
        return FALSE;

    custom = get_custom (a->appearance, &rgba);
    preset = get_preset (a->interface);

    for (child = gtk_widget_get_first_child (accent_box); child; child = gtk_widget_get_next_sibling (child)) {
        gboolean active;

        if (child == a->button)
            active = custom;
        else
            active = !custom && GPOINTER_TO_INT (g_object_get_data (G_OBJECT (child), "accent-color")) == (int) preset;

        gtk_toggle_button_set_active (GTK_TOGGLE_BUTTON (child), active);
    }

    return TRUE;
}

/* Adds the custom button after GNOME's colors, which must already be in @accent_box */
gboolean
cc_plinius_accent_setup (GtkWidget *accent_box)
{
    g_autoptr(GSettingsSchema) schema = NULL;
    GSettingsSchemaSource *source = g_settings_schema_source_get_default ();
    GtkWidget *first = gtk_widget_get_first_child (accent_box);
    GtkWidget *child;
    Accent *a;

    if (source)
        schema = g_settings_schema_source_lookup (source, APPEARANCE_SCHEMA, TRUE);
    if (!schema || !first)
        return FALSE;

    a = g_new0 (Accent, 1);
    a->accent_box = accent_box;
    a->appearance = g_settings_new (APPEARANCE_SCHEMA);
    a->interface = g_settings_new (INTERFACE_SCHEMA);

    /* A tap on a preset is a choice of the user; "toggled" also comes from syncing */
    for (child = first; child; child = gtk_widget_get_next_sibling (child))
        g_signal_connect_swapped (child, "clicked", G_CALLBACK (preset_clicked_cb), a);

    a->button = gtk_toggle_button_new ();
    gtk_widget_set_tooltip_text (a->button, P_("Custom Color"));
    cc_plinius_color_wheel_style (a->button);
    gtk_toggle_button_set_group (GTK_TOGGLE_BUTTON (a->button), GTK_TOGGLE_BUTTON (first));
    g_signal_connect_swapped (a->button, "clicked", G_CALLBACK (custom_clicked_cb), a);
    gtk_box_append (GTK_BOX (accent_box), a->button);

    g_object_set_data_full (G_OBJECT (accent_box), "plinius-accent", a, (GDestroyNotify) accent_free);

    g_signal_connect_swapped (a->appearance, "changed::" CUSTOM_KEY, G_CALLBACK (queue_update), a);
    g_signal_connect_swapped (a->interface, "changed::" INTERFACE_ACCENT_KEY, G_CALLBACK (queue_update), a);
    update (a);

    return TRUE;
}
