/*
 * cc-streaming-panel.c
 *
 * Plinius: "Streaming", a panel of its own in the sidebar above NFC, with the settings of
 * Schwupp, which mirrors the screen to TVs from Phosh's cast quick setting. Schwupp keeps
 * them (defaults for all TVs and own values per TV); this panel reads and changes them over
 * its D-Bus interface de.cais.Schwupp.Cast1 on the session bus (GetSettings, SetSetting,
 * ResetDevice, ForgetDevice). D-Bus starts Schwupp without a window when needed. Writing
 * Schwupp's config.json directly would not do: a running Schwupp writes back its own copy.
 *
 * Page: the defaults on top, below the TVs (found right now or remembered); a TV opens a page
 * of its own with the way of mirroring and its own values. While the panel is open, Schwupp
 * searches the network (StartDiscovery), so that new TVs show up.
 * "gnome-control-center streaming <id>" opens the page of that TV (quick setting footer).
 *
 * The panel is only listed when Schwupp's D-Bus service file is installed.
 * Our own strings use the gettext domain plinius-control-center (not GNOME's po/).
 *
 * SPDX-License-Identifier: MIT
 */

#include "config.h"

#include <adwaita.h>

#include "cc-streaming-panel.h"
#include "shell/cc-application.h"

#define PLINIUS_DOMAIN "plinius-control-center"
#define P_(String) g_dgettext (PLINIUS_DOMAIN, String)

#define SCHWUPP_NAME "de.cais.Schwupp"
#define SCHWUPP_PATH "/de/cais/Schwupp/Cast"
#define SCHWUPP_INTERFACE "de.cais.Schwupp.Cast1"
#define SCHWUPP_SERVICE_FILE "/usr/share/dbus-1/services/de.cais.Schwupp.service"
/* Schwupp marks speakers (sound instead of the screen) with this icon; they have no settings */
#define SPEAKER_ICON "audio-speakers-symbolic"

/* The id of the defaults in Schwupp's interface */
#define DEFAULTS_ID ""

struct _CcStreamingPanel {
    CcPanel parent_instance;

    AdwToastOverlay *toast_overlay;
    AdwPreferencesGroup *defaults_group;
    AdwPreferencesGroup *devices_group;
    GtkWidget *devices_placeholder;
    GPtrArray *device_rows;

    GDBusProxy *proxy;
    GCancellable *cancellable;
    /* Holding Schwupp's search (StartDiscovery called) */
    gboolean discovering;
    /* From "gnome-control-center streaming <id>": open this TV's page once Schwupp answers */
    char *pending_device;
};

G_DEFINE_TYPE (CcStreamingPanel, cc_streaming_panel, CC_TYPE_PANEL)

enum {
    PROP_0,
    PROP_PARAMETERS,
};

/* ------------------------------------------------------------------------------------------ */
/* Rows of one set of values (the defaults or one TV), shared by the panel and the TV's page   */
/* ------------------------------------------------------------------------------------------ */

typedef struct {
    CcStreamingPanel *panel; /* not owned, the rows live inside the panel's navigation */
    char *id;                /* "" = defaults */
    gboolean updating;       /* rows set from Schwupp's answer, not by the user */

    AdwComboRow *engine;     /* per TV only */
    GPtrArray *engine_names; /* engine keys in the order of the combo row */
    AdwComboRow *resolution;
    GArray *resolutions;     /* heights in the order of the combo row */
    AdwSpinRow *bitrate;
    AdwSpinRow *fps;
    AdwSwitchRow *audio;
    AdwComboRow *encoder;
    GPtrArray *encoder_names; /* "auto" first */
    AdwPreferencesGroup *video_group;
    GtkWidget *video_refused_row; /* AirPlay TV that refused videos */
    AdwButtonRow *reset_row;
} ValueRows;

static void
value_rows_free (gpointer data)
{
    ValueRows *rows = data;

    g_free (rows->id);
    g_clear_pointer (&rows->engine_names, g_ptr_array_unref);
    g_clear_pointer (&rows->resolutions, g_array_unref);
    g_clear_pointer (&rows->encoder_names, g_ptr_array_unref);
    g_free (rows);
}

static void show_error (CcStreamingPanel *self, GtkWidget *about, const char *message, const GError *error);
static void load_values (ValueRows *rows);

static void
set_value_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(GError) error = NULL;
    g_autoptr(GVariant) reply = g_dbus_proxy_call_finish (G_DBUS_PROXY (source), result, &error);
    g_autofree char *id = user_data;

    if (reply || g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
        return;
    g_warning ("Changing a streaming setting of '%s' failed: %s", id, error->message);
}

static void
set_value (ValueRows *rows, const char *key, GVariant *value)
{
    GDBusProxy *proxy = rows->panel->proxy;

    if (rows->updating || !proxy) {
        g_variant_unref (g_variant_ref_sink (value));
        return;
    }
    g_dbus_proxy_call (proxy, "SetSetting", g_variant_new ("(ssv)", rows->id, key, value), G_DBUS_CALL_FLAGS_NONE,
                       -1, rows->panel->cancellable, set_value_cb, g_strdup (rows->id));
    /* A TV with an own value no longer follows the defaults */
    if (rows->reset_row)
        gtk_widget_set_sensitive (GTK_WIDGET (rows->reset_row), TRUE);
}

static void
on_engine_selected (ValueRows *rows)
{
    guint i = adw_combo_row_get_selected (rows->engine);

    if (rows->engine_names && i < rows->engine_names->len)
        set_value (rows, "mirror_engine", g_variant_new_string (g_ptr_array_index (rows->engine_names, i)));
}

static void
on_resolution_selected (ValueRows *rows)
{
    guint i = adw_combo_row_get_selected (rows->resolution);

    if (rows->resolutions && i < rows->resolutions->len)
        set_value (rows, "mirror_height", g_variant_new_int32 (g_array_index (rows->resolutions, gint32, i)));
}

static void
on_bitrate_changed (ValueRows *rows)
{
    set_value (rows, "mirror_bitrate_kbps", g_variant_new_int32 ((gint32) adw_spin_row_get_value (rows->bitrate)));
}

static void
on_fps_changed (ValueRows *rows)
{
    set_value (rows, "mirror_fps", g_variant_new_int32 ((gint32) adw_spin_row_get_value (rows->fps)));
}

static void
on_audio_changed (ValueRows *rows)
{
    set_value (rows, "mirror_audio", g_variant_new_boolean (adw_switch_row_get_active (rows->audio)));
}

static void
on_encoder_selected (ValueRows *rows)
{
    guint i = adw_combo_row_get_selected (rows->encoder);

    if (rows->encoder_names && i < rows->encoder_names->len)
        set_value (rows, "mirror_encoder", g_variant_new_string (g_ptr_array_index (rows->encoder_names, i)));
}

static void
on_video_retry (ValueRows *rows)
{
    set_value (rows, "airplay_video", g_variant_new_boolean (TRUE));
    gtk_widget_set_visible (rows->video_refused_row, FALSE);
}

/* The quality rows (both pages), added to @group */
static void
add_quality_rows (ValueRows *rows, AdwPreferencesGroup *group)
{
    rows->resolution = ADW_COMBO_ROW (adw_combo_row_new ());
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (rows->resolution), P_("Resolution"));
    adw_preferences_group_add (group, GTK_WIDGET (rows->resolution));
    g_signal_connect_swapped (rows->resolution, "notify::selected", G_CALLBACK (on_resolution_selected), rows);

    rows->bitrate = ADW_SPIN_ROW (adw_spin_row_new_with_range (1000, 20000, 500));
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (rows->bitrate), P_("Bitrate (kbit/s)"));
    adw_preferences_group_add (group, GTK_WIDGET (rows->bitrate));
    g_signal_connect_swapped (rows->bitrate, "notify::value", G_CALLBACK (on_bitrate_changed), rows);

    rows->fps = ADW_SPIN_ROW (adw_spin_row_new_with_range (10, 60, 5));
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (rows->fps), P_("Frame Rate (fps)"));
    adw_preferences_group_add (group, GTK_WIDGET (rows->fps));
    g_signal_connect_swapped (rows->fps, "notify::value", G_CALLBACK (on_fps_changed), rows);

    rows->audio = ADW_SWITCH_ROW (adw_switch_row_new ());
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (rows->audio), P_("Sound"));
    adw_action_row_set_subtitle (ADW_ACTION_ROW (rows->audio), P_("Send the phone's sound to the TV, too"));
    adw_preferences_group_add (group, GTK_WIDGET (rows->audio));
    g_signal_connect_swapped (rows->audio, "notify::active", G_CALLBACK (on_audio_changed), rows);

    /* Only when there is something to choose (more than one encoder) */
    rows->encoder = ADW_COMBO_ROW (adw_combo_row_new ());
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (rows->encoder), P_("Video Encoder"));
    gtk_widget_set_visible (GTK_WIDGET (rows->encoder), FALSE);
    adw_preferences_group_add (group, GTK_WIDGET (rows->encoder));
    g_signal_connect_swapped (rows->encoder, "notify::selected", G_CALLBACK (on_encoder_selected), rows);
}

static guint
index_of (GPtrArray *names, const char *name)
{
    guint i;

    /* No name in the answer: the first one */
    if (names && name && g_ptr_array_find_with_equal_func (names, name, g_str_equal, &i))
        return i;
    return 0;
}

/* Rows from the answer of GetSettings */
static void
apply_values (ValueRows *rows, GVariantDict *values)
{
    g_autoptr(GVariant) resolutions = g_variant_dict_lookup_value (values, "resolutions", G_VARIANT_TYPE ("ai"));
    g_autoptr(GVariant) engines = g_variant_dict_lookup_value (values, "engines", G_VARIANT_TYPE ("a(ssbs)"));
    g_autofree const char **encoders = NULL;
    g_autoptr(GtkStringList) labels = NULL;
    const char *engine = NULL, *encoder = "auto";
    gint32 height = 1080, bitrate = 6000, fps = 30;
    gboolean audio = TRUE, video = TRUE, custom = FALSE;
    guint selected = 0;

    rows->updating = TRUE;

    g_variant_dict_lookup (values, "mirror_height", "i", &height);
    g_variant_dict_lookup (values, "mirror_bitrate_kbps", "i", &bitrate);
    g_variant_dict_lookup (values, "mirror_fps", "i", &fps);
    g_variant_dict_lookup (values, "mirror_audio", "b", &audio);
    g_variant_dict_lookup (values, "mirror_encoder", "&s", &encoder);
    g_variant_dict_lookup (values, "encoders", "^a&s", &encoders);

    /* Resolution: 16:9 heights, 2160 is 4K */
    g_clear_pointer (&rows->resolutions, g_array_unref);
    rows->resolutions = g_array_new (FALSE, FALSE, sizeof (gint32));
    labels = gtk_string_list_new (NULL);
    if (resolutions) {
        gsize n;
        const gint32 *heights = g_variant_get_fixed_array (resolutions, &n, sizeof (gint32));

        for (gsize i = 0; i < n; i++) {
            g_autofree char *label = heights[i] >= 2160 ? g_strdup_printf ("%dp (4K)", heights[i])
                                                        : g_strdup_printf ("%dp", heights[i]);
            g_array_append_val (rows->resolutions, heights[i]);
            gtk_string_list_append (labels, label);
            if (heights[i] == height)
                selected = (guint) i;
        }
    }
    adw_combo_row_set_model (rows->resolution, G_LIST_MODEL (labels));
    adw_combo_row_set_selected (rows->resolution, selected);

    adw_spin_row_set_value (rows->bitrate, bitrate);
    adw_spin_row_set_value (rows->fps, fps);
    adw_switch_row_set_active (rows->audio, audio);

    /* Encoder: "Automatic" and the ones that exist */
    g_clear_pointer (&rows->encoder_names, g_ptr_array_unref);
    rows->encoder_names = g_ptr_array_new_with_free_func (g_free);
    g_ptr_array_add (rows->encoder_names, g_strdup ("auto"));
    g_clear_object (&labels);
    labels = gtk_string_list_new (NULL);
    gtk_string_list_append (labels, P_("Automatic"));
    for (guint i = 0; encoders && encoders[i]; i++) {
        g_ptr_array_add (rows->encoder_names, g_strdup (encoders[i]));
        gtk_string_list_append (labels, encoders[i]);
    }
    adw_combo_row_set_model (rows->encoder, G_LIST_MODEL (labels));
    adw_combo_row_set_selected (rows->encoder, index_of (rows->encoder_names, encoder));
    gtk_widget_set_visible (GTK_WIDGET (rows->encoder), rows->encoder_names->len > 2);

    /* Per TV: the ways of mirroring this kind of TV knows, unavailable ones marked */
    if (rows->engine && engines) {
        GVariantIter iter;
        const char *name, *display, *detail;
        gboolean available;

        g_variant_dict_lookup (values, "mirror_engine", "&s", &engine);
        g_clear_pointer (&rows->engine_names, g_ptr_array_unref);
        rows->engine_names = g_ptr_array_new_with_free_func (g_free);
        g_clear_object (&labels);
        labels = gtk_string_list_new (NULL);
        g_variant_iter_init (&iter, engines);
        while (g_variant_iter_next (&iter, "(&s&sb&s)", &name, &display, &available, &detail)) {
            g_autofree char *label = available ? g_strdup (display)
                                               : g_strdup_printf (P_("%s (not available)"), display);
            g_ptr_array_add (rows->engine_names, g_strdup (name));
            gtk_string_list_append (labels, label);
        }
        adw_combo_row_set_model (rows->engine, G_LIST_MODEL (labels));
        adw_combo_row_set_selected (rows->engine, index_of (rows->engine_names, engine));
        gtk_widget_set_visible (GTK_WIDGET (rows->engine), rows->engine_names->len > 1);
    }

    if (rows->video_refused_row) {
        g_variant_dict_lookup (values, "airplay_video", "b", &video);
        gtk_widget_set_visible (rows->video_refused_row, !video);
    }
    if (rows->reset_row) {
        g_variant_dict_lookup (values, "custom", "b", &custom);
        gtk_widget_set_sensitive (GTK_WIDGET (rows->reset_row), custom);
    }

    rows->updating = FALSE;
}

static void
get_values_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(GError) error = NULL;
    g_autoptr(GVariant) reply = g_dbus_proxy_call_finish (G_DBUS_PROXY (source), result, &error);
    g_autoptr(GObject) widget = user_data; /* keeps the rows alive until the answer */
    g_autoptr(GVariant) settings = NULL;
    g_autoptr(GVariantDict) values = NULL;
    ValueRows *rows;

    if (!reply) {
        if (!g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED)) {
            rows = g_object_get_data (widget, "value-rows");
            if (rows)
                show_error (rows->panel, GTK_WIDGET (rows->resolution), P_("Schwupp does not answer"), error);
        }
        return;
    }
    rows = g_object_get_data (widget, "value-rows");
    if (!rows)
        return;
    g_variant_get (reply, "(@a{sv})", &settings);
    values = g_variant_dict_new (settings);
    apply_values (rows, values);
}

/* @rows belong to the widget that holds them as "value-rows" */
static void
load_values (ValueRows *rows)
{
    GtkWidget *holder = GTK_WIDGET (rows->resolution);

    if (!rows->panel->proxy)
        return;
    g_dbus_proxy_call (rows->panel->proxy, "GetSettings", g_variant_new ("(s)", rows->id), G_DBUS_CALL_FLAGS_NONE,
                       -1, rows->panel->cancellable, get_values_cb, g_object_ref (holder));
}

static ValueRows *
value_rows_new (CcStreamingPanel *self, const char *id)
{
    ValueRows *rows = g_new0 (ValueRows, 1);

    rows->panel = self;
    rows->id = g_strdup (id);
    return rows;
}

/* ------------------------------------------------------------------------------------------ */
/* The page of one TV                                                                          */
/* ------------------------------------------------------------------------------------------ */

static void
device_call_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(GError) error = NULL;
    g_autoptr(GVariant) reply = g_dbus_proxy_call_finish (G_DBUS_PROXY (source), result, &error);
    g_autoptr(GtkWidget) holder = user_data;
    ValueRows *rows;

    if (g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
        return;
    rows = g_object_get_data (G_OBJECT (holder), "value-rows");
    if (!rows)
        return;
    if (!reply) {
        show_error (rows->panel, GTK_WIDGET (rows->resolution), P_("Schwupp does not answer"), error);
        return;
    }
    /* Show what applies now (the defaults after a reset) */
    load_values (rows);
}

static void
on_reset_activated (ValueRows *rows)
{
    if (!rows->panel->proxy)
        return;
    g_dbus_proxy_call (rows->panel->proxy, "ResetDevice", g_variant_new ("(s)", rows->id), G_DBUS_CALL_FLAGS_NONE, -1,
                       rows->panel->cancellable, device_call_cb, g_object_ref (rows->resolution));
}

static void
on_forget_activated (AdwNavigationPage *page)
{
    ValueRows *rows = g_object_get_data (G_OBJECT (page), "value-rows-ref");
    GtkWidget *view;

    if (!rows || !rows->panel->proxy)
        return;
    g_dbus_proxy_call (rows->panel->proxy, "ForgetDevice", g_variant_new ("(s)", rows->id), G_DBUS_CALL_FLAGS_NONE,
                       -1, rows->panel->cancellable, NULL, NULL);
    view = gtk_widget_get_ancestor (GTK_WIDGET (page), ADW_TYPE_NAVIGATION_VIEW);
    if (view)
        adw_navigation_view_pop (ADW_NAVIGATION_VIEW (view));
}

static gboolean
is_known (CcStreamingPanel *self, const char *id)
{
    g_autoptr(GVariant) known = self->proxy ? g_dbus_proxy_get_cached_property (self->proxy, "KnownDevices") : NULL;
    GVariantIter iter;
    const char *known_id;

    if (!known)
        return FALSE;
    g_variant_iter_init (&iter, known);
    while (g_variant_iter_next (&iter, "(&s&s&s&s)", &known_id, NULL, NULL, NULL))
        if (g_strcmp0 (known_id, id) == 0)
            return TRUE;
    return FALSE;
}

static void
open_device (CcStreamingPanel *self, const char *id, const char *name, const char *description, const char *kind)
{
    AdwNavigationPage *page;
    GtkWidget *toolbar = adw_toolbar_view_new ();
    GtkWidget *prefs = adw_preferences_page_new ();
    GtkWidget *group = adw_preferences_group_new ();
    GtkWidget *actions = adw_preferences_group_new ();
    GtkWidget *overlay;
    GtkWidget *button;
    ValueRows *rows = value_rows_new (self, id);

    adw_toolbar_view_add_top_bar (ADW_TOOLBAR_VIEW (toolbar), adw_header_bar_new ());
    /* Messages about this TV show on its page, which covers the panel's */
    overlay = adw_toast_overlay_new ();
    adw_toast_overlay_set_child (ADW_TOAST_OVERLAY (overlay), prefs);
    adw_toolbar_view_set_content (ADW_TOOLBAR_VIEW (toolbar), overlay);
    page = adw_navigation_page_new (toolbar, name);

    /* Way of mirroring, per kind of TV */
    adw_preferences_group_set_description (ADW_PREFERENCES_GROUP (group), description);
    rows->engine = ADW_COMBO_ROW (adw_combo_row_new ());
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (rows->engine), P_("Mirroring"));
    adw_action_row_set_subtitle (ADW_ACTION_ROW (rows->engine), P_("How the screen gets to this TV"));
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), GTK_WIDGET (rows->engine));
    g_signal_connect_swapped (rows->engine, "notify::selected", G_CALLBACK (on_engine_selected), rows);
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (prefs), ADW_PREFERENCES_GROUP (group));

    rows->video_group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_group_set_title (rows->video_group, P_("Picture and Sound"));
    add_quality_rows (rows, rows->video_group);
    /* An AirPlay TV that refused videos once gets none offered; a firmware update may change that */
    if (g_strcmp0 (kind, "airplay") == 0) {
        rows->video_refused_row = adw_action_row_new ();
        adw_preferences_row_set_title (ADW_PREFERENCES_ROW (rows->video_refused_row), P_("Video Playback"));
        adw_action_row_set_subtitle (ADW_ACTION_ROW (rows->video_refused_row),
                                     P_("This TV refused videos, they are no longer offered to it"));
        button = gtk_button_new_with_label (P_("Try Again"));
        gtk_widget_set_valign (button, GTK_ALIGN_CENTER);
        g_signal_connect_swapped (button, "clicked", G_CALLBACK (on_video_retry), rows);
        adw_action_row_add_suffix (ADW_ACTION_ROW (rows->video_refused_row), button);
        gtk_widget_set_visible (rows->video_refused_row, FALSE);
        adw_preferences_group_add (rows->video_group, rows->video_refused_row);
    }
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (prefs), rows->video_group);

    rows->reset_row = ADW_BUTTON_ROW (adw_button_row_new ());
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (rows->reset_row), P_("Use the Defaults"));
    gtk_widget_set_sensitive (GTK_WIDGET (rows->reset_row), FALSE);
    g_signal_connect_swapped (rows->reset_row, "activated", G_CALLBACK (on_reset_activated), rows);
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (actions), GTK_WIDGET (rows->reset_row));
    if (is_known (self, id)) {
        GtkWidget *forget = adw_button_row_new ();

        adw_preferences_row_set_title (ADW_PREFERENCES_ROW (forget), P_("Forget This TV"));
        gtk_widget_add_css_class (forget, "destructive-action");
        g_signal_connect_swapped (forget, "activated", G_CALLBACK (on_forget_activated), page);
        adw_preferences_group_add (ADW_PREFERENCES_GROUP (actions), forget);
    }
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (prefs), ADW_PREFERENCES_GROUP (actions));

    /* The rows belong to the first row widget; the page only points at them */
    g_object_set_data_full (G_OBJECT (rows->resolution), "value-rows", rows, value_rows_free);
    g_object_set_data (G_OBJECT (page), "value-rows-ref", rows);

    cc_panel_push_subpage (CC_PANEL (self), page);
    load_values (rows);
}

static void
open_pending_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(GError) error = NULL;
    g_autoptr(GVariant) reply = g_dbus_proxy_call_finish (G_DBUS_PROXY (source), result, &error);
    g_autofree char *id = user_data;
    g_autoptr(GVariant) settings = NULL;
    g_autoptr(GVariantDict) values = NULL;
    const char *name = id, *description = "", *kind = "";

    if (!reply) {
        /* Not found (any more): the panel itself is what shows then */
        if (!g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
            g_debug ("TV '%s' for the streaming page: %s", id, error->message);
        return;
    }
    g_variant_get (reply, "(@a{sv})", &settings);
    values = g_variant_dict_new (settings);
    g_variant_dict_lookup (values, "name", "&s", &name);
    g_variant_dict_lookup (values, "description", "&s", &description);
    g_variant_dict_lookup (values, "kind", "&s", &kind);
    open_device (CC_STREAMING_PANEL (g_object_get_data (G_OBJECT (source), "panel")), id, name, description, kind);
}

static void
open_pending (CcStreamingPanel *self)
{
    char *id;

    if (!self->pending_device || !self->proxy)
        return;
    /* Taken first: the order in which a call's arguments are evaluated is not fixed */
    id = g_steal_pointer (&self->pending_device);
    g_dbus_proxy_call (self->proxy, "GetSettings", g_variant_new ("(s)", id), G_DBUS_CALL_FLAGS_NONE,
                       -1, self->cancellable, open_pending_cb, id);
}

/* ------------------------------------------------------------------------------------------ */
/* The list of TVs                                                                             */
/* ------------------------------------------------------------------------------------------ */

/* The page needs the kind of TV (AirPlay rows) and its name: GetSettings first, then the page */
static void
on_device_activated (CcStreamingPanel *self, AdwActionRow *row)
{
    g_free (self->pending_device);
    self->pending_device = g_strdup (g_object_get_data (G_OBJECT (row), "device-id"));
    open_pending (self);
}

static void
add_devices (CcStreamingPanel *self, GHashTable *seen, const char *property, gboolean found)
{
    g_autoptr(GVariant) devices = g_dbus_proxy_get_cached_property (self->proxy, property);
    GVariantIter iter;
    const char *id, *name, *description, *icon;

    if (!devices)
        return;
    g_variant_iter_init (&iter, devices);
    while (g_variant_iter_next (&iter, "(&s&s&s&s)", &id, &name, &description, &icon)) {
        GtkWidget *row;
        g_autofree char *subtitle = NULL;

        if (g_str_equal (icon, SPEAKER_ICON) || g_hash_table_contains (seen, id))
            continue;
        g_hash_table_add (seen, g_strdup (id));

        row = adw_action_row_new ();
        adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), name);
        adw_preferences_row_set_use_markup (ADW_PREFERENCES_ROW (row), FALSE);
        subtitle = found ? g_strdup (description) : g_strdup_printf (P_("%s · not found right now"), description);
        adw_action_row_set_subtitle (ADW_ACTION_ROW (row), subtitle);
        adw_action_row_add_prefix (ADW_ACTION_ROW (row), gtk_image_new_from_icon_name (icon));
        adw_action_row_add_suffix (ADW_ACTION_ROW (row), gtk_image_new_from_icon_name ("go-next-symbolic"));
        gtk_list_box_row_set_activatable (GTK_LIST_BOX_ROW (row), TRUE);
        g_object_set_data_full (G_OBJECT (row), "device-id", g_strdup (id), g_free);
        g_signal_connect_swapped (row, "activated", G_CALLBACK (on_device_activated), self);
        adw_preferences_group_add (self->devices_group, row);
        g_ptr_array_add (self->device_rows, row);
    }
}

static void
update_devices (CcStreamingPanel *self)
{
    g_autoptr(GHashTable) seen = g_hash_table_new_full (g_str_hash, g_str_equal, g_free, NULL);

    for (guint i = 0; i < self->device_rows->len; i++)
        adw_preferences_group_remove (self->devices_group, g_ptr_array_index (self->device_rows, i));
    g_ptr_array_set_size (self->device_rows, 0);

    if (self->proxy) {
        /* Found right now first, then the remembered ones that are not around */
        add_devices (self, seen, "Devices", TRUE);
        add_devices (self, seen, "KnownDevices", FALSE);
    }
    gtk_widget_set_visible (self->devices_placeholder, self->device_rows->len == 0);
}

/* ------------------------------------------------------------------------------------------ */
/* Schwupp on the bus                                                                          */
/* ------------------------------------------------------------------------------------------ */

/* On the page that shows @about (a TV's page has its own overlay), else on the panel's page */
static void
show_error (CcStreamingPanel *self, GtkWidget *about, const char *message, const GError *error)
{
    GtkWidget *overlay = about ? gtk_widget_get_ancestor (about, ADW_TYPE_TOAST_OVERLAY) : NULL;

    if (error)
        g_warning ("%s: %s", message, error->message);
    adw_toast_overlay_add_toast (overlay ? ADW_TOAST_OVERLAY (overlay) : self->toast_overlay, adw_toast_new (message));
}

static void
start_discovery_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(GError) error = NULL;
    g_autoptr(GVariant) reply = g_dbus_proxy_call_finish (G_DBUS_PROXY (source), result, &error);

    if (!reply && !g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
        show_error (CC_STREAMING_PANEL (user_data), NULL, P_("Schwupp does not answer"), error);
}

static void
on_properties_changed (CcStreamingPanel *self, GVariant *changed, GStrv invalidated)
{
    g_autoptr(GVariant) devices = g_variant_lookup_value (changed, "Devices", NULL);
    g_autoptr(GVariant) known = g_variant_lookup_value (changed, "KnownDevices", NULL);

    if (devices || known)
        update_devices (self);
}

static void
on_name_owner_changed (CcStreamingPanel *self)
{
    g_autofree char *owner = g_dbus_proxy_get_name_owner (self->proxy);
    ValueRows *defaults = g_object_get_data (G_OBJECT (self->defaults_group), "value-rows");

    /* Schwupp quits 30 s after the last caller; while the panel holds the search it stays */
    update_devices (self);
    if (owner && defaults)
        load_values (defaults);
}

static void
proxy_ready_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(GError) error = NULL;
    GDBusProxy *proxy = g_dbus_proxy_new_for_bus_finish (result, &error);
    CcStreamingPanel *self;

    if (!proxy) {
        if (!g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
            g_warning ("Cannot reach Schwupp: %s", error->message);
        return;
    }

    self = CC_STREAMING_PANEL (user_data);
    self->proxy = proxy;
    g_object_set_data (G_OBJECT (proxy), "panel", self);
    g_signal_connect_object (proxy, "g-properties-changed", G_CALLBACK (on_properties_changed), self,
                             G_CONNECT_SWAPPED);
    g_signal_connect_object (proxy, "notify::g-name-owner", G_CALLBACK (on_name_owner_changed), self,
                             G_CONNECT_SWAPPED);

    /* Starts Schwupp if needed; it searches until the panel says StopDiscovery */
    self->discovering = TRUE;
    g_dbus_proxy_call (proxy, "StartDiscovery", NULL, G_DBUS_CALL_FLAGS_NONE, -1, self->cancellable,
                       start_discovery_cb, self);
    /* Already running: values now; otherwise when D-Bus has started it (g-name-owner) */
    on_name_owner_changed (self);
    open_pending (self);
}

/* ------------------------------------------------------------------------------------------ */

static void
cc_streaming_panel_set_property (GObject *object, guint prop_id, const GValue *value, GParamSpec *pspec)
{
    CcStreamingPanel *self = CC_STREAMING_PANEL (object);
    GVariant *parameters;

    switch (prop_id) {
    case PROP_PARAMETERS:
        /* "gnome-control-center streaming <id>": the page of that TV */
        parameters = g_value_get_variant (value);
        if (parameters && g_variant_n_children (parameters) > 0) {
            g_autoptr(GVariant) child = g_variant_get_child_value (parameters, 0);
            g_autoptr(GVariant) id = g_variant_get_variant (child);

            if (g_variant_is_of_type (id, G_VARIANT_TYPE_STRING) && *g_variant_get_string (id, NULL)) {
                g_free (self->pending_device);
                self->pending_device = g_variant_dup_string (id, NULL);
                open_pending (self);
            }
        }
        break;
    default:
        G_OBJECT_WARN_INVALID_PROPERTY_ID (object, prop_id, pspec);
    }
}

static void
cc_streaming_panel_get_property (GObject *object, guint prop_id, GValue *value, GParamSpec *pspec)
{
    switch (prop_id) {
    case PROP_PARAMETERS:
        g_value_set_variant (value, NULL);
        break;
    default:
        G_OBJECT_WARN_INVALID_PROPERTY_ID (object, prop_id, pspec);
    }
}

static void
cc_streaming_panel_dispose (GObject *object)
{
    CcStreamingPanel *self = CC_STREAMING_PANEL (object);

    /* Leaving the panel ends our part of the search (GNOME Settings itself stays on the bus) */
    if (self->proxy && self->discovering)
        g_dbus_proxy_call (self->proxy, "StopDiscovery", NULL, G_DBUS_CALL_FLAGS_NONE, -1, NULL, NULL, NULL);
    self->discovering = FALSE;

    g_cancellable_cancel (self->cancellable);
    g_clear_object (&self->cancellable);
    g_clear_object (&self->proxy);
    g_clear_pointer (&self->device_rows, g_ptr_array_unref);
    g_clear_pointer (&self->pending_device, g_free);

    G_OBJECT_CLASS (cc_streaming_panel_parent_class)->dispose (object);
}

static void
cc_streaming_panel_class_init (CcStreamingPanelClass *klass)
{
    GObjectClass *object_class = G_OBJECT_CLASS (klass);

    object_class->dispose = cc_streaming_panel_dispose;
    object_class->set_property = cc_streaming_panel_set_property;
    object_class->get_property = cc_streaming_panel_get_property;
    g_object_class_override_property (object_class, PROP_PARAMETERS, "parameters");
}

static void
cc_streaming_panel_init (CcStreamingPanel *self)
{
    GtkWidget *toolbar = adw_toolbar_view_new ();
    GtkWidget *page = adw_preferences_page_new ();
    ValueRows *defaults = value_rows_new (self, DEFAULTS_ID);

    adw_navigation_page_set_title (ADW_NAVIGATION_PAGE (self), P_("Streaming"));
    self->toast_overlay = ADW_TOAST_OVERLAY (adw_toast_overlay_new ());
    adw_toast_overlay_set_child (self->toast_overlay, page);
    adw_toolbar_view_add_top_bar (ADW_TOOLBAR_VIEW (toolbar), adw_header_bar_new ());
    adw_toolbar_view_set_content (ADW_TOOLBAR_VIEW (toolbar), GTK_WIDGET (self->toast_overlay));
    adw_navigation_page_set_child (ADW_NAVIGATION_PAGE (self), toolbar);

    /* Defaults: what a TV without own values gets */
    self->defaults_group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_group_set_title (self->defaults_group, P_("Defaults"));
    adw_preferences_group_set_description (self->defaults_group,
                                           P_("Mirror the screen with the Cast quick setting. These values apply to every TV without own ones."));
    add_quality_rows (defaults, self->defaults_group);
    /* Like a TV's page: the rows belong to their first row widget, which the D-Bus answers look
     * at; the group only points at them */
    g_object_set_data_full (G_OBJECT (defaults->resolution), "value-rows", defaults, value_rows_free);
    g_object_set_data (G_OBJECT (self->defaults_group), "value-rows", defaults);
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (page), self->defaults_group);

    /* TVs: found right now, and remembered ones */
    self->devices_group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_group_set_title (self->devices_group, P_("TVs"));
    adw_preferences_group_set_description (self->devices_group, P_("Own values per TV. Speakers have none."));
    self->devices_placeholder = adw_action_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (self->devices_placeholder), P_("Searching for TVs…"));
    adw_action_row_set_subtitle (ADW_ACTION_ROW (self->devices_placeholder),
                                 P_("TVs in the same network show up here"));
    adw_preferences_group_add (self->devices_group, self->devices_placeholder);
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (page), self->devices_group);
    self->device_rows = g_ptr_array_new ();

    self->cancellable = g_cancellable_new ();
    g_dbus_proxy_new_for_bus (G_BUS_TYPE_SESSION, G_DBUS_PROXY_FLAGS_NONE, NULL, SCHWUPP_NAME, SCHWUPP_PATH,
                              SCHWUPP_INTERFACE, self->cancellable, proxy_ready_cb, self);
}

/* Only with Schwupp */
void
cc_streaming_panel_static_init_func (void)
{
    CcApplication *application = CC_APPLICATION (g_application_get_default ());

    cc_shell_model_set_panel_visibility (cc_application_get_model (application), "streaming",
                                         g_file_test (SCHWUPP_SERVICE_FILE, G_FILE_TEST_EXISTS) ? CC_PANEL_VISIBLE
                                                                                              : CC_PANEL_HIDDEN);
}
