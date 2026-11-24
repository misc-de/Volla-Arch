/*
 * cc-plinius-appearance.c
 *
 * Plinius: the two rows on top of the Appearance page, which open its pages Style (light or dark,
 * the accent color with our custom one) and Background (the animation group and the sections of
 * cc-background-chooser.c: video, 360° photos, own pictures, GNOME's pictures). Each row says in
 * its subtitle what is set right now, with nothing in front (no dot, no picture):
 * - Style: the style and the accent color ("Dark · Blue").
 * - Background: what shows (GNOME's or an own picture, a 360° photo, a video, or the background
 *   color of the Style page while none of them is chosen) and the effect over it ("Own Picture ·
 *   Snow").
 *
 * Our own strings use the gettext domain plinius-control-center; the names of GNOME's accent
 * colors come from GNOME's own translation, as on its buttons.
 *
 * SPDX-License-Identifier: MIT
 */

#undef G_LOG_DOMAIN
#define G_LOG_DOMAIN "cc-plinius-appearance"

#include <config.h>

#include <gdesktop-enums.h>

#include "cc-plinius-appearance.h"
#include "cc-plinius-ambient.h"
#include "cc-plinius-panorama.h"

#define PLINIUS_DOMAIN "plinius-control-center"
#define P_(String) g_dgettext (PLINIUS_DOMAIN, String)
#define N_(String) (String)

#define INTERFACE_SCHEMA "org.gnome.desktop.interface"
#define APPEARANCE_SCHEMA "org.plinius.appearance"
#define AMBIENT_SCHEMA "org.plinius.ambient"
#define BACKGROUND_SCHEMA "org.gnome.desktop.background"

/* GNOME's accent colors in the order of GDesktopAccentColor, translated in GNOME's domain
 * (cc-background-panel.c names its buttons so) */
static const char * const accent_names[] = {
    N_("Blue"), N_("Teal"), N_("Green"), N_("Yellow"), N_("Orange"), N_("Red"), N_("Pink"), N_("Purple"), N_("Slate"),
};

static GSettings *
settings_if_installed (const char *id)
{
    GSettingsSchemaSource *source = g_settings_schema_source_get_default ();
    g_autoptr(GSettingsSchema) schema = source ? g_settings_schema_source_lookup (source, id, TRUE) : NULL;

    return schema ? g_settings_new_full (schema, NULL, NULL) : NULL;
}

/* ----- Style ----- */

typedef struct {
    AdwActionRow *row; /* owns this */
    GSettings *interface;
    GSettings *appearance; /* NULL without phosh-plinius */
} StyleRow;

static void
update_style (StyleRow *s)
{
    GDesktopColorScheme scheme = g_settings_get_enum (s->interface, "color-scheme");
    guint accent = g_settings_get_enum (s->interface, "accent-color");
    g_autofree char *custom = s->appearance ? g_settings_get_string (s->appearance, "accent-color") : NULL;
    g_autofree char *subtitle = NULL;
    const char *style, *color = NULL;
    GdkRGBA rgba;

    if (scheme == G_DESKTOP_COLOR_SCHEME_PREFER_DARK)
        style = P_("Dark");
    else if (scheme == G_DESKTOP_COLOR_SCHEME_PREFER_LIGHT)
        style = P_("Light");
    else
        style = P_("Default");

    if (custom && custom[0] != '\0' && gdk_rgba_parse (&rgba, custom))
        color = P_("Custom Color");
    else if (accent < G_N_ELEMENTS (accent_names))
        color = g_dgettext (GETTEXT_PACKAGE, accent_names[accent]);

    subtitle = color ? g_strdup_printf ("%s · %s", style, color) : g_strdup (style);
    adw_action_row_set_subtitle (s->row, subtitle);
}

static void
style_row_free (gpointer data)
{
    StyleRow *s = data;

    g_signal_handlers_disconnect_by_data (s->interface, s);
    g_object_unref (s->interface);
    if (s->appearance) {
        g_signal_handlers_disconnect_by_data (s->appearance, s);
        g_object_unref (s->appearance);
    }
    g_free (s);
}

/**
 * cc_plinius_appearance_style_row_setup:
 * @row: the row that opens the page Style
 *
 * Keeps the row's subtitle at the style and accent color.
 */
void
cc_plinius_appearance_style_row_setup (AdwActionRow *row)
{
    StyleRow *s = g_new0 (StyleRow, 1);

    s->row = row;
    s->interface = g_settings_new (INTERFACE_SCHEMA);
    s->appearance = settings_if_installed (APPEARANCE_SCHEMA);
    g_object_set_data_full (G_OBJECT (row), "plinius-style-row", s, style_row_free);

    g_signal_connect_swapped (s->interface, "changed::color-scheme", G_CALLBACK (update_style), s);
    g_signal_connect_swapped (s->interface, "changed::accent-color", G_CALLBACK (update_style), s);
    if (s->appearance)
        g_signal_connect_swapped (s->appearance, "changed::accent-color", G_CALLBACK (update_style), s);
    update_style (s);
}

/* ----- Background ----- */

typedef struct {
    AdwActionRow *row;     /* owns this */
    char *uri;             /* of the picture */
    GSettings *ambient;    /* NULL without plinius-ambient */
    GSettings *background; /* whether a picture shows */
} BackgroundRow;

/* No picture shows: the background color does (cc-plinius-bgcolor.c) */
static gboolean
shows_color (BackgroundRow *b)
{
    return !b->uri || b->uri[0] == '\0' ||
           g_settings_get_enum (b->background, "picture-options") == G_DESKTOP_BACKGROUND_STYLE_NONE;
}

/* Pictures under /usr came with the system (GNOME's wallpapers), the rest one added */
static const char *
picture_kind (const char *uri)
{
    g_autofree char *path = NULL;

    path = g_filename_from_uri (uri, NULL, NULL);
    if (path && !g_str_has_prefix (path, "/usr/"))
        return P_("Own Picture");
    return P_("Default Picture");
}

static void
update_background (BackgroundRow *b)
{
    g_autofree char *video = cc_plinius_files_current (CC_PLINIUS_FILES_VIDEO);
    g_autofree char *photo = video ? NULL : cc_plinius_files_current (CC_PLINIUS_FILES_PANORAMA);
    const char *effect = cc_plinius_ambient_effect_label ();
    g_autofree char *subtitle = NULL;
    const char *kind;

    if (video)
        kind = P_("Video");
    else if (photo)
        kind = P_("360° Photo");
    else if (shows_color (b))
        kind = P_("Color");
    else
        kind = picture_kind (b->uri);

    subtitle = effect ? g_strdup_printf ("%s · %s", kind, effect) : g_strdup (kind);
    adw_action_row_set_subtitle (b->row, subtitle);
}

static void
on_ambient_changed (BackgroundRow *b, const char *key)
{
    update_background (b);
}

static void
on_background_changed (BackgroundRow *b, const char *key)
{
    update_background (b);
}

static void
background_row_free (gpointer data)
{
    BackgroundRow *b = data;

    if (b->ambient) {
        g_signal_handlers_disconnect_by_data (b->ambient, b);
        g_object_unref (b->ambient);
    }
    g_signal_handlers_disconnect_by_data (b->background, b);
    g_object_unref (b->background);
    g_free (b->uri);
    g_free (b);
}

/**
 * cc_plinius_appearance_background_row_setup:
 * @row: the row that opens the page Background
 *
 * Keeps the subtitle of @row at what shows and the effect over it; the background picture comes
 * from cc_plinius_appearance_background_row_set_uri().
 */
void
cc_plinius_appearance_background_row_setup (AdwActionRow *row)
{
    BackgroundRow *b = g_new0 (BackgroundRow, 1);

    b->row = row;
    b->ambient = settings_if_installed (AMBIENT_SCHEMA);
    b->background = g_settings_new (BACKGROUND_SCHEMA);
    g_object_set_data_full (G_OBJECT (row), "plinius-background-row", b, background_row_free);

    /* the effect, the 360° photo, the video and switching between them */
    if (b->ambient)
        g_signal_connect_swapped (b->ambient, "changed", G_CALLBACK (on_ambient_changed), b);
    /* a picture taken away (the background color shows) or chosen again */
    g_signal_connect_swapped (b->background, "changed::picture-options", G_CALLBACK (on_background_changed), b);
    /* GSettings sends "changed" only for keys that have been read */
    g_settings_get_enum (b->background, "picture-options");
    update_background (b);
}

/**
 * cc_plinius_appearance_background_row_set_uri:
 * @uri: the picture's URI (picture-uri), %NULL for a color
 *
 * The background picture changed; it counts unless a 360° photo, a video or the background color
 * (picture-options 'none') is the background.
 */
void
cc_plinius_appearance_background_row_set_uri (AdwActionRow *row, const char *uri)
{
    BackgroundRow *b = g_object_get_data (G_OBJECT (row), "plinius-background-row");

    if (!b)
        return;
    g_free (b->uri);
    b->uri = g_strdup (uri);
    update_background (b);
}
