/*
 * cc-feedback-panel.c
 *
 * Plinius: "Sounds and Vibration", a panel of its own below Sound, the sound and the vibration
 * of each kind of event: calls, messages, email, other notifications, alarms, the keys of the
 * on-screen keyboard and the alert sound. For each one a sound (the default, none, a system sound
 * or one of the user's own), a vibration rhythm and its strength; the keyboard has taps instead
 * of rhythms.
 *
 * feedbackd plays both. The choices go into a feedback theme of the user's own,
 * $XDG_CONFIG_HOME/feedbackd/themes/plinius.json, which names the theme that was active before
 * as its parent: an event the file does not list keeps what the parent gives it. The file has
 * a "Sound" (file-name, the media role of feedbackd's default theme) in the "full" profile and
 * a "VibraPattern" in the "quiet" profile for each event set here; "Dummy" means none. feedbackd
 * plays the "quiet" feedbacks in the "full" profile too, so in Quiet only the vibration plays.
 * org.sigxcpu.feedbackd's "theme" points at the file while it sets anything; feedbackd reloads
 * on that change, or on SIGHUP when the file changed under the same name. Entries of other
 * events in the file are kept. The page reads the rhythm back from the durations it wrote.
 *
 * feedbackd plays sounds through libcanberra, which reads Ogg Vorbis and WAV only; an own sound
 * is copied to $XDG_DATA_HOME/plinius/sounds, so it stays when the original goes ("Add" at the top
 * right of the sound page; an added sound has an edit button to rename or delete it). Files in
 * $XDG_MUSIC_DIR/Ringtones are listed as well.
 *
 * The on-screen keyboard (Stevia) sends "key-pressed" for each key; the vibrator bridge plays a
 * pulse shorter than 20 ms as a full-strength tap, so the keyboard's taps last 20 ms or more and
 * their strength counts.
 *
 * Chatty sends SMS as "im.received" like chat messages, which phosh turns into
 * message-new-instant: SMS and chat are one kind here, written for both message events.
 *
 * The alert sound is the bell of the apps (GTK's error bell, a terminal's bell): phoc hands it to
 * feedbackd as "bell-window-system" (phoc-plinius 0032). The default theme has nothing for that
 * event, so its default stays silent; GNOME's own alert sound chooser is no longer on Sound.
 *
 * Our own strings use the gettext domain plinius-control-center (not GNOME's po/).
 *
 * SPDX-License-Identifier: MIT
 */

#undef G_LOG_DOMAIN
#define G_LOG_DOMAIN "cc-feedback-panel"

#include "config.h"

#include "cc-feedback-panel.h"
#include "shell/cc-application.h"

#include <errno.h>
#include <math.h>
#include <signal.h>
#include <string.h>
#include <glib/gi18n.h>
#include <glib/gstdio.h>
#include <gsound.h>
#include <json-glib/json-glib.h>

#define PLINIUS_DOMAIN "plinius-control-center"
#define P_(String) g_dgettext (PLINIUS_DOMAIN, String)
#define PN_(String) (String)

#define FBD_SCHEMA "org.sigxcpu.feedbackd"
#define FBD_BUS "org.sigxcpu.Feedback"
#define FBD_PATH "/org/sigxcpu/Feedback"
#define THEME_NAME "plinius"
#define APP_ID "org.gnome.Settings"
/* How long "Test" rings a call or an alarm, in seconds */
#define TEST_RING_SECONDS 8
#define MIN_STRENGTH 0.1

typedef struct {
    /* The events of this kind; the first one is read back */
    const char *events[3];
    const char *title;
    const char *icon;
    /* The media role of feedbackd's default theme (NULL: "event") */
    const char *role;
    /* The sound of feedbackd's default theme and its name here (NULL: none) */
    const char *stock_effect;
    const char *stock_title;
    /* The rhythm and strength a strength change starts from when the vibration is the
     * default one: the default theme's vibration comes closest to them */
    const char *stock_rhythm;
    double stock_strength;
    /* Rings until answered or stopped: the pattern repeats with its pause at the end */
    gboolean loops;
    /* The keys of the on-screen keyboard: taps instead of rhythms, short sounds only */
    gboolean keys;
    /* The default theme gives the event no feedback: by default no sound and no vibration */
    gboolean silent;
} Channel;

static const Channel channels[] = {
    { { "phone-incoming-call", NULL }, PN_("Calls"), "call-start-symbolic", "Ringtone",
      "phone-incoming-call", PN_("Ringing"), "continuous", 0.5, TRUE },
    { { "message-new-instant", "message-new-sms", NULL }, PN_("Messages"), "chat-message-new-symbolic", NULL,
      "message-new-instant", PN_("New Message"), "short", 1.0, FALSE },
    { { "message-new-email", NULL }, PN_("Email"), "mail-unread-symbolic", NULL,
      "message-new-email", PN_("Message"), "triple", 1.0, FALSE },
    { { "notification-new-generic", NULL }, PN_("Other Notifications"), "preferences-system-notifications-symbolic", NULL,
      NULL, NULL, "double", 0.25, FALSE },
    { { "alarm-clock-elapsed", NULL }, PN_("Alarms"), "alarm-symbolic", "Alarm",
      "alarm-clock-elapsed", PN_("Alarm Clock"), "continuous", 0.5, TRUE },
    /* The default theme's "key-pressed" sound is in no installed sound theme: no sound; its 7 ms
     * vibration becomes the bridge's full-strength 20 ms tap */
    { { "key-pressed", NULL }, PN_("Keyboard"), "input-keyboard-symbolic", NULL,
      NULL, NULL, "short-tap", 1.0, FALSE, TRUE },
    /* The bell of the apps, through phoc; a strength set from the default starts at "Short" */
    { { "bell-window-system", NULL }, PN_("Alert Sound"), "dialog-warning-symbolic", NULL,
      NULL, NULL, "short", 1.0, FALSE, FALSE, TRUE },
};
#define N_CHANNELS G_N_ELEMENTS (channels)

#define MAX_STEPS 8

typedef struct {
    const char *id;
    const char *title;
    guint n;
    /* Share of the strength (0 is a pause) and duration in ms of each step; the last step of
     * all but "continuous" is the pause before the next ring, left out for a single event */
    double levels[MAX_STEPS];
    guint ms[MAX_STEPS];
    /* A tap of the keyboard, offered there only (and the rhythms everywhere else) */
    gboolean keys;
} Rhythm;

static const Rhythm rhythms[] = {
    { "short", PN_("Short"), 2, { 1, 0 }, { 150, 850 } },
    { "long", PN_("Long"), 2, { 1, 0 }, { 600, 900 } },
    { "double", PN_("Double"), 4, { 1, 0, 1, 0 }, { 150, 120, 150, 900 } },
    { "triple", PN_("Triple"), 6, { 1, 0, 1, 0, 1, 0 }, { 120, 100, 120, 100, 120, 900 } },
    { "heartbeat", PN_("Heartbeat"), 4, { 0.7, 0, 1, 0 }, { 90, 110, 180, 1000 } },
    { "rising", PN_("Rising"), 5, { 0.25, 0.5, 0.75, 1, 0 }, { 250, 250, 250, 500, 750 } },
    { "continuous", PN_("Continuous"), 1, { 1 }, { 1000 } },
    { "short-tap", PN_("Short Tap"), 1, { 1 }, { 20 }, TRUE },
    { "tap", PN_("Tap"), 1, { 1 }, { 35 }, TRUE },
    { "long-tap", PN_("Long Tap"), 1, { 1 }, { 60 }, TRUE },
};

/* Rows of the rhythm list before the rhythms */
#define RHYTHM_DEFAULT -2
#define RHYTHM_OFF -1
#define RHYTHM_ROW_OFFSET 2

typedef struct {
    const char *path;
    const char *title;
    /* The title is GNOME's own (translated in its domain) */
    gboolean gnome;
    /* Short enough for a key press (a fraction of a second) */
    gboolean brief;
} SystemSound;

#define FREEDESKTOP SOUND_DATA_DIR "/freedesktop/stereo/"
#define GNOME_ALERTS SOUND_DATA_DIR "/gnome/default/alerts/"

static const SystemSound system_sounds[] = {
    { FREEDESKTOP "phone-incoming-call.oga", PN_("Ringing"), FALSE },
    { FREEDESKTOP "phone-outgoing-calling.oga", PN_("Calling"), FALSE },
    { FREEDESKTOP "alarm-clock-elapsed.oga", PN_("Alarm Clock"), FALSE },
    { FREEDESKTOP "message-new-instant.oga", PN_("New Message"), FALSE },
    { FREEDESKTOP "message.oga", PN_("Message"), FALSE },
    { FREEDESKTOP "bell.oga", PN_("Bell"), FALSE, TRUE },
    { FREEDESKTOP "complete.oga", PN_("Complete"), FALSE },
    { FREEDESKTOP "dialog-information.oga", PN_("Information"), FALSE, TRUE },
    { FREEDESKTOP "audio-volume-change.oga", PN_("Tick"), FALSE, TRUE },
    { FREEDESKTOP "dialog-warning.oga", PN_("Warning"), FALSE },
    { FREEDESKTOP "window-attention.oga", PN_("Attention"), FALSE },
    { FREEDESKTOP "service-login.oga", PN_("Login"), FALSE },
    { GNOME_ALERTS "click.ogg", "Click", TRUE, TRUE },
    { GNOME_ALERTS "hum.ogg", "Hum", TRUE },
    { GNOME_ALERTS "string.ogg", "String", TRUE },
    { GNOME_ALERTS "swing.ogg", "Swing", TRUE },
};

typedef enum {
    SOUND_DEFAULT,
    SOUND_NONE,
    SOUND_FILE,
} SoundKind;

typedef struct {
    SoundKind sound;
    char *file;
    int rhythm;
    double strength;
} Setting;

typedef struct {
    Setting settings[N_CHANNELS];
    /* The theme plinius.json is based on */
    char *parent;
    GSettings *fbd_settings;
    GDBusConnection *bus;
    GSoundContext *sound;
    GCancellable *preview;
    /* The stop button of the sound playing, shown while it plays */
    GtkWidget *preview_button;
    /* "Test": running from the request until feedbackd says the event ended */
    gboolean testing;
    gboolean test_stop_wanted;
    guint test_id;
    guint test_timeout_id;
    guint ended_subscription;
    /* Told when "testing" changes (the channel page shown) */
    void (*test_changed) (gpointer data);
    gpointer test_changed_data;
    /* The rows of the first page, for their summaries */
    GtkWidget *rows[N_CHANNELS];
} Model;

/* Helpers for reading JSON that may have been written by hand */

static JsonArray *
get_array (JsonObject *object, const char *name)
{
    JsonNode *node = object ? json_object_get_member (object, name) : NULL;

    return node && JSON_NODE_HOLDS_ARRAY (node) ? json_node_get_array (node) : NULL;
}

static const char *
get_string (JsonObject *object, const char *name)
{
    JsonNode *node = object ? json_object_get_member (object, name) : NULL;

    if (node && JSON_NODE_HOLDS_VALUE (node) && json_node_get_value_type (node) == G_TYPE_STRING)
        return json_node_get_string (node);
    return NULL;
}

static double
get_number (JsonNode *node)
{
    if (!JSON_NODE_HOLDS_VALUE (node))
        return -1;
    switch (json_node_get_value_type (node)) {
    case G_TYPE_DOUBLE:
    case G_TYPE_INT64:
        return json_node_get_double (node);
    default:
        return -1;
    }
}

static JsonArray *
profile_feedbacks (JsonObject *root, const char *profile)
{
    JsonArray *profiles = get_array (root, "profiles");

    for (guint i = 0; profiles && i < json_array_get_length (profiles); i++) {
        JsonNode *node = json_array_get_element (profiles, i);
        JsonObject *object = JSON_NODE_HOLDS_OBJECT (node) ? json_node_get_object (node) : NULL;

        if (g_strcmp0 (get_string (object, "name"), profile) == 0)
            return get_array (object, "feedbacks");
    }
    return NULL;
}

static JsonObject *
find_feedback (JsonObject *root, const char *profile, const char *event)
{
    JsonArray *feedbacks = profile_feedbacks (root, profile);

    for (guint i = 0; feedbacks && i < json_array_get_length (feedbacks); i++) {
        JsonNode *node = json_array_get_element (feedbacks, i);
        JsonObject *object = JSON_NODE_HOLDS_OBJECT (node) ? json_node_get_object (node) : NULL;

        if (g_strcmp0 (get_string (object, "event-name"), event) == 0)
            return object;
    }
    return NULL;
}

static gboolean
is_our_event (const char *event)
{
    for (guint c = 0; c < N_CHANNELS; c++) {
        for (guint e = 0; channels[c].events[e]; e++) {
            if (g_strcmp0 (channels[c].events[e], event) == 0)
                return TRUE;
        }
    }
    return FALSE;
}

/* Rhythms */

static int
rhythm_by_id (const char *id)
{
    for (guint r = 0; r < G_N_ELEMENTS (rhythms); r++) {
        if (g_str_equal (rhythms[r].id, id))
            return r;
    }
    return 0;
}

/* The rhythms a channel offers, in the order of its list */
static gboolean
rhythm_fits (const Channel *channel, guint rhythm)
{
    return rhythms[rhythm].keys == channel->keys;
}

/* The row of the rhythm list for RHYTHM (or RHYTHM_DEFAULT, RHYTHM_OFF) */
static guint
rhythm_to_row (const Channel *channel, int rhythm)
{
    guint row = RHYTHM_ROW_OFFSET;

    if (rhythm < 0)
        return rhythm + RHYTHM_ROW_OFFSET;
    for (guint r = 0; r < (guint) rhythm; r++)
        row += rhythm_fits (channel, r);
    return row;
}

static int
rhythm_from_row (const Channel *channel, guint row)
{
    if (row < RHYTHM_ROW_OFFSET)
        return (int) row - RHYTHM_ROW_OFFSET;
    row -= RHYTHM_ROW_OFFSET;
    for (guint r = 0; r < G_N_ELEMENTS (rhythms); r++) {
        if (!rhythm_fits (channel, r))
            continue;
        if (row-- == 0)
            return r;
    }
    return RHYTHM_DEFAULT;
}

/* Steps written for a channel: a single event leaves out the pause at the end */
static guint
rhythm_steps (const Rhythm *rhythm, gboolean loops)
{
    if (!loops && rhythm->n > 1 && rhythm->levels[rhythm->n - 1] == 0)
        return rhythm->n - 1;
    return rhythm->n;
}

static double
step_level (const Rhythm *rhythm, guint step, double strength)
{
    /* Whole percents, so that reading the file back finds the same strength */
    return round (rhythm->levels[step] * strength * 100) / 100;
}

/* The rhythm and strength of a VibraPattern this page wrote, FALSE for any other */
static gboolean
rhythm_match (JsonObject *feedback, const Channel *channel, int *rhythm, double *strength)
{
    JsonArray *levels = get_array (feedback, "magnitudes");
    JsonArray *durations = get_array (feedback, "durations");
    guint n;

    if (levels == NULL || durations == NULL)
        return FALSE;
    n = json_array_get_length (durations);
    if (n == 0 || n != json_array_get_length (levels))
        return FALSE;
    for (guint r = 0; r < G_N_ELEMENTS (rhythms); r++) {
        const Rhythm *candidate = &rhythms[r];
        double max = 0;
        gboolean same = rhythm_fits (channel, r) && (n == candidate->n || n == rhythm_steps (candidate, FALSE));

        for (guint i = 0; same && i < n; i++) {
            double level = get_number (json_array_get_element (levels, i));

            same = get_number (json_array_get_element (durations, i)) == candidate->ms[i] &&
                   (level == 0) == (candidate->levels[i] == 0);
            if (candidate->levels[i] == 1)
                max = MAX (max, level);
        }
        if (same && max > 0) {
            *rhythm = r;
            *strength = CLAMP (max, MIN_STRENGTH, 1.0);
            return TRUE;
        }
    }
    return FALSE;
}

/* The model */

static char *
theme_path (void)
{
    return g_build_filename (g_get_user_config_dir (), "feedbackd", "themes", THEME_NAME ".json", NULL);
}

static char *
own_sounds_dir (void)
{
    return g_build_filename (g_get_user_data_dir (), "plinius", "sounds", NULL);
}

static void
setting_reset (Setting *setting, const Channel *channel)
{
    g_clear_pointer (&setting->file, g_free);
    setting->sound = SOUND_DEFAULT;
    setting->rhythm = RHYTHM_DEFAULT;
    setting->strength = channel->stock_strength;
}

static gboolean
setting_is_default (const Setting *setting)
{
    return setting->sound == SOUND_DEFAULT && setting->rhythm == RHYTHM_DEFAULT;
}

static void
setting_read (Setting *setting, const Channel *channel, JsonObject *root)
{
    JsonObject *feedback;
    const char *type, *file;

    setting_reset (setting, channel);
    if (root == NULL)
        return;

    feedback = find_feedback (root, "full", channel->events[0]);
    type = get_string (feedback, "type");
    if (g_strcmp0 (type, "Dummy") == 0) {
        setting->sound = SOUND_NONE;
    } else if (g_strcmp0 (type, "Sound") == 0 && (file = get_string (feedback, "file-name"))) {
        setting->sound = SOUND_FILE;
        setting->file = g_strdup (file);
    }

    feedback = find_feedback (root, "quiet", channel->events[0]);
    type = get_string (feedback, "type");
    if (g_strcmp0 (type, "Dummy") == 0)
        setting->rhythm = RHYTHM_OFF;
    else if (g_strcmp0 (type, "VibraPattern") == 0)
        rhythm_match (feedback, channel, &setting->rhythm, &setting->strength);
}

static void
model_free (Model *model)
{
    for (guint c = 0; c < N_CHANNELS; c++)
        g_clear_pointer (&model->settings[c].file, g_free);
    g_clear_pointer (&model->parent, g_free);
    if (model->preview)
        g_cancellable_cancel (model->preview);
    g_clear_object (&model->preview);
    g_clear_handle_id (&model->test_timeout_id, g_source_remove);
    if (model->bus && model->ended_subscription)
        g_dbus_connection_signal_unsubscribe (model->bus, model->ended_subscription);
    g_clear_object (&model->sound);
    g_clear_object (&model->fbd_settings);
    g_clear_object (&model->bus);
}

static Model *
model_ref (Model *model)
{
    return g_rc_box_acquire (model);
}

static void
model_unref (Model *model)
{
    g_rc_box_release_full (model, (GDestroyNotify) model_free);
}

static Model *
model_new (void)
{
    Model *model = g_rc_box_new0 (Model);
    g_autofree char *path = theme_path ();
    g_autofree char *current = NULL;
    g_autoptr(JsonParser) parser = json_parser_new ();
    g_autoptr(GError) error = NULL;
    JsonObject *root = NULL;

    model->fbd_settings = g_settings_new (FBD_SCHEMA);
    model->bus = g_bus_get_sync (G_BUS_TYPE_SESSION, NULL, NULL);
    model->sound = gsound_context_new (NULL, &error);
    if (model->sound == NULL)
        g_warning ("No sound previews: %s", error->message);

    if (json_parser_load_from_file (parser, path, NULL) && JSON_NODE_HOLDS_OBJECT (json_parser_get_root (parser)))
        root = json_node_get_object (json_parser_get_root (parser));

    current = g_settings_get_string (model->fbd_settings, "theme");
    if (g_strcmp0 (current, THEME_NAME) == 0)
        model->parent = g_strdup (get_string (root, "parent-name"));
    else if (current && *current)
        model->parent = g_strdup (current);
    if (model->parent == NULL || g_str_equal (model->parent, THEME_NAME))
        g_set_str (&model->parent, "default");

    for (guint c = 0; c < N_CHANNELS; c++)
        setting_read (&model->settings[c], &channels[c], g_strcmp0 (current, THEME_NAME) == 0 ? root : NULL);
    return model;
}

static void
add_sound (JsonBuilder *builder, const char *event, const Channel *channel, const Setting *setting)
{
    json_builder_begin_object (builder);
    json_builder_set_member_name (builder, "event-name");
    json_builder_add_string_value (builder, event);
    json_builder_set_member_name (builder, "type");
    if (setting->sound == SOUND_NONE) {
        json_builder_add_string_value (builder, "Dummy");
        /* A call repeats its feedbacks; without a duration "none" would repeat at once */
        json_builder_set_member_name (builder, "duration");
        json_builder_add_int_value (builder, 1000);
    } else {
        json_builder_add_string_value (builder, "Sound");
        json_builder_set_member_name (builder, "file-name");
        json_builder_add_string_value (builder, setting->file);
        if (channel->role) {
            json_builder_set_member_name (builder, "media-role");
            json_builder_add_string_value (builder, channel->role);
        }
    }
    json_builder_end_object (builder);
}

static void
add_vibration (JsonBuilder *builder, const char *event, const Channel *channel, const Setting *setting)
{
    json_builder_begin_object (builder);
    json_builder_set_member_name (builder, "event-name");
    json_builder_add_string_value (builder, event);
    json_builder_set_member_name (builder, "type");
    if (setting->rhythm == RHYTHM_OFF) {
        json_builder_add_string_value (builder, "Dummy");
        json_builder_set_member_name (builder, "duration");
        json_builder_add_int_value (builder, 1000);
    } else {
        const Rhythm *rhythm = &rhythms[setting->rhythm];
        guint steps = rhythm_steps (rhythm, channel->loops);

        json_builder_add_string_value (builder, "VibraPattern");
        json_builder_set_member_name (builder, "magnitudes");
        json_builder_begin_array (builder);
        for (guint i = 0; i < steps; i++)
            json_builder_add_double_value (builder, step_level (rhythm, i, setting->strength));
        json_builder_end_array (builder);
        json_builder_set_member_name (builder, "durations");
        json_builder_begin_array (builder);
        for (guint i = 0; i < steps; i++)
            json_builder_add_int_value (builder, rhythm->ms[i]);
        json_builder_end_array (builder);
    }
    json_builder_end_object (builder);
}

/* feedbackd reads the theme file again on SIGHUP */
static void
pid_received (GObject *source, GAsyncResult *result, gpointer data)
{
    g_autoptr(GVariant) reply = g_dbus_connection_call_finish (G_DBUS_CONNECTION (source), result, NULL);
    guint32 pid;

    /* Not running: it reads the file when it starts */
    if (reply == NULL)
        return;
    g_variant_get (reply, "(u)", &pid);
    if (pid > 1 && kill ((pid_t) pid, SIGHUP) != 0)
        g_warning ("Could not ask feedbackd (%u) to reload its theme: %s", pid, g_strerror (errno));
}

static void
feedbackd_reload (Model *model)
{
    if (model->bus == NULL)
        return;
    g_dbus_connection_call (model->bus, "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                            "GetConnectionUnixProcessID", g_variant_new ("(s)", FBD_BUS), G_VARIANT_TYPE ("(u)"),
                            G_DBUS_CALL_FLAGS_NONE, 2000, NULL, pid_received, NULL);
}

/* Write plinius.json from the settings, keeping the entries of other events, and make it the
 * theme; without anything left in it the parent theme comes back */
static void
model_save (Model *model)
{
    static const char *profiles[] = { "full", "quiet", "silent" };
    g_autofree char *path = theme_path ();
    g_autofree char *dir = g_path_get_dirname (path);
    g_autofree char *current = g_settings_get_string (model->fbd_settings, "theme");
    g_autoptr(JsonParser) parser = json_parser_new ();
    g_autoptr(JsonBuilder) builder = json_builder_new ();
    g_autoptr(JsonGenerator) generator = json_generator_new ();
    g_autoptr(JsonNode) root_node = NULL;
    g_autoptr(GError) error = NULL;
    g_autofree char *data = NULL;
    JsonObject *old = NULL;
    gboolean anything = FALSE;
    gsize length;

    if (json_parser_load_from_file (parser, path, NULL) && JSON_NODE_HOLDS_OBJECT (json_parser_get_root (parser)))
        old = json_node_get_object (json_parser_get_root (parser));

    json_builder_begin_object (builder);
    json_builder_set_member_name (builder, "name");
    json_builder_add_string_value (builder, THEME_NAME);
    json_builder_set_member_name (builder, "parent-name");
    json_builder_add_string_value (builder, model->parent);
    json_builder_set_member_name (builder, "profiles");
    json_builder_begin_array (builder);
    for (guint p = 0; p < G_N_ELEMENTS (profiles); p++) {
        JsonArray *kept = profile_feedbacks (old, profiles[p]);
        guint count = 0;

        json_builder_begin_object (builder);
        json_builder_set_member_name (builder, "name");
        json_builder_add_string_value (builder, profiles[p]);
        json_builder_set_member_name (builder, "feedbacks");
        json_builder_begin_array (builder);
        for (guint i = 0; kept && i < json_array_get_length (kept); i++) {
            JsonNode *node = json_array_get_element (kept, i);
            JsonObject *object = JSON_NODE_HOLDS_OBJECT (node) ? json_node_get_object (node) : NULL;

            if (object && !is_our_event (get_string (object, "event-name"))) {
                json_builder_add_value (builder, json_node_copy (node));
                count++;
            }
        }
        for (guint c = 0; c < N_CHANNELS; c++) {
            const Setting *setting = &model->settings[c];

            for (guint e = 0; channels[c].events[e]; e++) {
                if (p == 0 && setting->sound != SOUND_DEFAULT) {
                    add_sound (builder, channels[c].events[e], &channels[c], setting);
                    count++;
                } else if (p == 1 && setting->rhythm != RHYTHM_DEFAULT) {
                    add_vibration (builder, channels[c].events[e], &channels[c], setting);
                    count++;
                }
            }
        }
        json_builder_end_array (builder);
        json_builder_end_object (builder);
        anything |= count > 0;
    }
    json_builder_end_array (builder);
    json_builder_end_object (builder);

    if (!anything) {
        if (g_strcmp0 (current, THEME_NAME) == 0)
            g_settings_set_string (model->fbd_settings, "theme", model->parent);
        if (g_unlink (path) != 0 && errno != ENOENT)
            g_warning ("Could not remove %s: %s", path, g_strerror (errno));
        return;
    }

    root_node = json_builder_get_root (builder);
    json_generator_set_root (generator, root_node);
    json_generator_set_pretty (generator, TRUE);
    data = json_generator_to_data (generator, &length);
    if (g_mkdir_with_parents (dir, 0755) != 0) {
        g_warning ("Could not create %s: %s", dir, g_strerror (errno));
        return;
    }
    if (!g_file_set_contents_full (path, data, length, G_FILE_SET_CONTENTS_CONSISTENT, 0644, &error)) {
        g_warning ("Could not write the feedback theme: %s", error->message);
        return;
    }
    if (g_strcmp0 (current, THEME_NAME) != 0)
        g_settings_set_string (model->fbd_settings, "theme", THEME_NAME);
    else
        feedbackd_reload (model);
}

/* Names */

static char *
file_title (const char *path)
{
    g_autofree char *base = NULL;
    char *dot;

    for (guint i = 0; i < G_N_ELEMENTS (system_sounds); i++) {
        if (g_strcmp0 (system_sounds[i].path, path) == 0)
            return g_strdup (system_sounds[i].gnome ? _(system_sounds[i].title) : P_(system_sounds[i].title));
    }
    base = g_path_get_basename (path);
    dot = strrchr (base, '.');
    if (dot && dot != base)
        *dot = '\0';
    return g_filename_display_name (base);
}

static char *
sound_title (const Channel *channel, const Setting *setting)
{
    switch (setting->sound) {
    case SOUND_NONE:
        return g_strdup (P_("No Sound"));
    case SOUND_FILE:
        return file_title (setting->file);
    case SOUND_DEFAULT:
    default:
        return g_strdup (channel->stock_title ? P_(channel->stock_title) : P_("No Sound"));
    }
}

static char *
vibration_title (const Channel *channel, const Setting *setting)
{
    if (setting->rhythm == RHYTHM_DEFAULT)
        return g_strdup (channel->silent ? P_("No Vibration") : P_("Default Vibration"));
    if (setting->rhythm == RHYTHM_OFF)
        return g_strdup (P_("No Vibration"));
    /* Translators: a vibration rhythm and its strength, e.g. "Double, 60 %" */
    return g_strdup_printf (P_("%s, %d %%"), P_(rhythms[setting->rhythm].title), (int) round (setting->strength * 100));
}

static char *
summary (const Channel *channel, const Setting *setting)
{
    g_autofree char *sound = sound_title (channel, setting);
    g_autofree char *vibration = vibration_title (channel, setting);

    return g_strdup_printf ("%s · %s", sound, vibration);
}

static void
model_update_rows (Model *model)
{
    for (guint c = 0; c < N_CHANNELS; c++) {
        g_autofree char *text = NULL;

        if (model->rows[c] == NULL)
            continue;
        text = summary (&channels[c], &model->settings[c]);
        adw_action_row_set_subtitle (ADW_ACTION_ROW (model->rows[c]), text);
    }
}

/* Previews and tests */

static void
preview_stop (Model *model)
{
    if (model->preview)
        g_cancellable_cancel (model->preview);
    g_clear_object (&model->preview);
    if (model->preview_button)
        gtk_widget_set_visible (model->preview_button, FALSE);
    model->preview_button = NULL;
}

typedef struct {
    Model *model;
    GCancellable *cancellable;
    GtkWidget *button;
} Preview;

/* A sound ended by itself or was stopped: its stop button goes, unless the same button already
 * belongs to the next sound (the row tapped again) */
static void
preview_done (GObject *source, GAsyncResult *result, gpointer data)
{
    Preview *preview = data;
    Model *model = preview->model;
    g_autoptr(GError) error = NULL;

    if (!gsound_context_play_full_finish (GSOUND_CONTEXT (source), result, &error) &&
        !g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
        g_warning ("Could not play the sound: %s", error->message);
    if (model->preview == preview->cancellable) {
        g_clear_object (&model->preview);
        model->preview_button = NULL;
        if (preview->button)
            gtk_widget_set_visible (preview->button, FALSE);
    } else if (preview->button && preview->button != model->preview_button) {
        gtk_widget_set_visible (preview->button, FALSE);
    }
    g_clear_object (&preview->button);
    g_object_unref (preview->cancellable);
    model_unref (model);
    g_free (preview);
}

static void test_stop (Model *model);

/* The sound as feedbackd would play it, with the theme of the system sounds; BUTTON (may be
 * NULL) shows while it plays */
static void
preview_sound (Model *model, const Channel *channel, const Setting *setting, GtkWidget *button)
{
    Preview *preview;
    g_autoptr(GSettings) sound_settings = NULL;
    g_autofree char *theme = NULL;
    const char *role = channel->role ? channel->role : "event";

    preview_stop (model);
    if (model->sound == NULL || setting->sound == SOUND_NONE)
        return;
    if (setting->sound == SOUND_DEFAULT && channel->stock_effect == NULL)
        return;
    test_stop (model);
    model->preview = g_cancellable_new ();
    preview = g_new0 (Preview, 1);
    preview->model = model_ref (model);
    preview->cancellable = g_object_ref (model->preview);
    if (button) {
        preview->button = g_object_ref (button);
        model->preview_button = button;
        gtk_widget_set_visible (button, TRUE);
    }
    sound_settings = g_settings_new ("org.gnome.desktop.sound");
    theme = g_settings_get_string (sound_settings, "theme-name");
    if (setting->sound == SOUND_FILE)
        gsound_context_play_full (model->sound, model->preview, preview_done, preview, GSOUND_ATTR_MEDIA_FILENAME,
                                  setting->file, GSOUND_ATTR_MEDIA_ROLE, role, GSOUND_ATTR_APPLICATION_ID,
                                  APP_ID, NULL);
    else
        gsound_context_play_full (model->sound, model->preview, preview_done, preview, GSOUND_ATTR_EVENT_ID,
                                  channel->stock_effect, GSOUND_ATTR_CANBERRA_XDG_THEME_NAME, theme,
                                  GSOUND_ATTR_MEDIA_ROLE, role, GSOUND_ATTR_APPLICATION_ID, APP_ID, NULL);
}

/* The rhythm once through feedbackd's haptic interface (which keeps the feedback profile) */
static void
preview_vibration (Model *model, const Channel *channel, const Setting *setting)
{
    GVariantBuilder pattern;
    const Rhythm *rhythm;
    guint steps;

    if (model->bus == NULL || setting->rhythm < 0)
        return;
    rhythm = &rhythms[setting->rhythm];
    steps = rhythm_steps (rhythm, FALSE);
    g_variant_builder_init (&pattern, G_VARIANT_TYPE ("a(du)"));
    for (guint i = 0; i < steps; i++)
        g_variant_builder_add (&pattern, "(du)", step_level (rhythm, i, setting->strength), rhythm->ms[i]);
    g_dbus_connection_call (model->bus, FBD_BUS, FBD_PATH, FBD_BUS ".Haptic", "Vibrate",
                            g_variant_new ("(sa(du))", APP_ID, &pattern), G_VARIANT_TYPE ("(b)"),
                            G_DBUS_CALL_FLAGS_NONE, 2000, NULL, NULL, NULL);
}

static void
test_set_idle (Model *model)
{
    gboolean was = model->testing;

    model->testing = FALSE;
    model->test_stop_wanted = FALSE;
    model->test_id = 0;
    g_clear_handle_id (&model->test_timeout_id, g_source_remove);
    if (was && model->test_changed)
        model->test_changed (model->test_changed_data);
}

static void
end_feedback (Model *model, guint id)
{
    g_dbus_connection_call (model->bus, FBD_BUS, FBD_PATH, FBD_BUS, "EndFeedback", g_variant_new ("(u)", id), NULL,
                            G_DBUS_CALL_FLAGS_NONE, 2000, NULL, NULL, NULL);
}

static void
test_stop (Model *model)
{
    if (!model->testing)
        return;
    if (model->test_id == 0) {
        /* Not started yet: ended as soon as feedbackd answers */
        model->test_stop_wanted = TRUE;
        return;
    }
    end_feedback (model, model->test_id);
    test_set_idle (model);
}

/* feedbackd says the event ended: by itself, stopped, or with nothing to play */
static void
feedback_ended (GDBusConnection *bus, const char *sender, const char *path, const char *interface,
                const char *signal, GVariant *parameters, gpointer data)
{
    Model *model = data;
    guint32 id, reason;

    g_variant_get (parameters, "(uu)", &id, &reason);
    if (model->testing && id == model->test_id)
        test_set_idle (model);
}

/* In case feedbackd never says so (it stopped meanwhile) */
static gboolean
test_timeout (gpointer data)
{
    Model *model = data;

    model->test_timeout_id = 0;
    test_set_idle (model);
    return G_SOURCE_REMOVE;
}

static void
test_started (GObject *source, GAsyncResult *result, gpointer data)
{
    g_autoptr(GVariant) reply = g_dbus_connection_call_finish (G_DBUS_CONNECTION (source), result, NULL);
    Model *model = data;

    if (reply == NULL) {
        test_set_idle (model);
    } else if (model->testing) {
        g_variant_get (reply, "(u)", &model->test_id);
        if (model->test_stop_wanted) {
            end_feedback (model, model->test_id);
            test_set_idle (model);
        }
    }
    model_unref (model);
}

/* The event itself, as an app would send it: sound and vibration as the feedback profile allows */
static void
test_start (Model *model, const Channel *channel)
{
    GVariantBuilder hints;

    preview_stop (model);
    test_stop (model);
    if (model->bus == NULL)
        return;
    if (model->ended_subscription == 0)
        model->ended_subscription = g_dbus_connection_signal_subscribe (
            model->bus, FBD_BUS, FBD_BUS, "FeedbackEnded", FBD_PATH, NULL, G_DBUS_SIGNAL_FLAGS_NONE, feedback_ended,
            model, NULL);
    model->testing = TRUE;
    model->test_stop_wanted = FALSE;
    model->test_id = 0;
    model->test_timeout_id = g_timeout_add_seconds (channel->loops ? TEST_RING_SECONDS + 3 : 30, test_timeout, model);
    if (model->test_changed)
        model->test_changed (model->test_changed_data);
    g_variant_builder_init (&hints, G_VARIANT_TYPE_VARDICT);
    g_dbus_connection_call (model->bus, FBD_BUS, FBD_PATH, FBD_BUS, "TriggerFeedback",
                            g_variant_new ("(ssa{sv}i)", APP_ID, channel->events[0], &hints,
                                           channel->loops ? TEST_RING_SECONDS : -1),
                            G_VARIANT_TYPE ("(u)"), G_DBUS_CALL_FLAGS_NONE, 2000, NULL, test_started,
                            model_ref (model));
}

/* The sound chooser of a channel */

typedef struct {
    Model *model;
    guint channel;
    AdwPreferencesGroup *own_group;
    GtkWidget *first_check;
} SoundPage;

static void
sound_page_free (SoundPage *page)
{
    preview_stop (page->model);
    model_unref (page->model);
    g_free (page);
}

static void
sound_toggled (GtkCheckButton *check, SoundPage *page)
{
    Setting *setting = &page->model->settings[page->channel];
    SoundKind kind = GPOINTER_TO_INT (g_object_get_data (G_OBJECT (check), "plinius-kind"));
    const char *file = g_object_get_data (G_OBJECT (check), "plinius-file");

    if (!gtk_check_button_get_active (check))
        return;
    setting->sound = kind;
    g_set_str (&setting->file, kind == SOUND_FILE ? file : NULL);
    model_save (page->model);
    preview_sound (page->model, &channels[page->channel], setting,
                   g_object_get_data (G_OBJECT (check), "plinius-stop"));
}

/* Tapping the row of the sound already chosen plays it again */
static void
sound_row_activated (AdwActionRow *row, SoundPage *page)
{
    GtkCheckButton *check = g_object_get_data (G_OBJECT (row), "plinius-check");

    if (gtk_check_button_get_active (check))
        preview_sound (page->model, &channels[page->channel], &page->model->settings[page->channel],
                       g_object_get_data (G_OBJECT (check), "plinius-stop"));
    else
        gtk_check_button_set_active (check, TRUE);
}

static GtkWidget *
sound_row (SoundPage *page, AdwPreferencesGroup *group, const char *title, const char *subtitle, SoundKind kind,
           const char *file)
{
    const Setting *setting = &page->model->settings[page->channel];
    GtkWidget *row = adw_action_row_new ();
    GtkWidget *check = gtk_check_button_new ();
    GtkWidget *stop = gtk_button_new_from_icon_name ("media-playback-stop-symbolic");

    adw_preferences_row_set_use_markup (ADW_PREFERENCES_ROW (row), FALSE);
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), title);
    if (subtitle)
        adw_action_row_set_subtitle (ADW_ACTION_ROW (row), subtitle);
    gtk_widget_set_valign (check, GTK_ALIGN_CENTER);
    if (page->first_check)
        gtk_check_button_set_group (GTK_CHECK_BUTTON (check), GTK_CHECK_BUTTON (page->first_check));
    else
        page->first_check = check;
    gtk_check_button_set_active (GTK_CHECK_BUTTON (check),
                                 setting->sound == kind && (kind != SOUND_FILE || g_strcmp0 (setting->file, file) == 0));
    g_object_set_data (G_OBJECT (check), "plinius-kind", GINT_TO_POINTER (kind));
    g_object_set_data_full (G_OBJECT (check), "plinius-file", g_strdup (file), g_free);
    g_signal_connect (check, "toggled", G_CALLBACK (sound_toggled), page);
    adw_action_row_add_prefix (ADW_ACTION_ROW (row), check);
    /* Shown while this sound plays: a tap ends it */
    gtk_widget_set_valign (stop, GTK_ALIGN_CENTER);
    gtk_widget_add_css_class (stop, "flat");
    gtk_widget_add_css_class (stop, "circular");
    gtk_widget_set_tooltip_text (stop, P_("Stop"));
    gtk_widget_set_visible (stop, FALSE);
    g_signal_connect_swapped (stop, "clicked", G_CALLBACK (preview_stop), page->model);
    adw_action_row_add_suffix (ADW_ACTION_ROW (row), stop);
    g_object_set_data (G_OBJECT (check), "plinius-stop", stop);
    g_object_set_data (G_OBJECT (check), "plinius-row", row);
    g_object_set_data (G_OBJECT (row), "plinius-check", check);
    gtk_list_box_row_set_activatable (GTK_LIST_BOX_ROW (row), TRUE);
    g_signal_connect (row, "activated", G_CALLBACK (sound_row_activated), page);
    adw_preferences_group_add (group, row);
    return check;
}

static gboolean
is_audio_name (const char *name)
{
    return g_str_has_suffix (name, ".oga") || g_str_has_suffix (name, ".ogg") || g_str_has_suffix (name, ".wav");
}

static int
compare_titles (gconstpointer a, gconstpointer b)
{
    g_autofree char *x = file_title (*(const char **) a);
    g_autofree char *y = file_title (*(const char **) b);

    return g_utf8_collate (x, y);
}

/* The system sounds the chooser of CHANNEL lists */
static gboolean
system_sound_offered (const Channel *channel, guint sound)
{
    return !channel->keys || system_sounds[sound].brief;
}

/* Own sounds, those in Music/Ringtones and the one chosen if it is elsewhere */
static GPtrArray *
own_sounds (const Channel *channel, const Setting *setting)
{
    GPtrArray *files = g_ptr_array_new_with_free_func (g_free);
    g_autofree char *own = own_sounds_dir ();
    const char *music = g_get_user_special_dir (G_USER_DIRECTORY_MUSIC);
    g_autofree char *ringtones = music ? g_build_filename (music, "Ringtones", NULL) : NULL;
    const char *dirs[] = { own, ringtones };
    gboolean found = setting->sound != SOUND_FILE;

    for (guint d = 0; d < G_N_ELEMENTS (dirs); d++) {
        g_autoptr(GDir) dir = dirs[d] ? g_dir_open (dirs[d], 0, NULL) : NULL;
        const char *name;

        while (dir && (name = g_dir_read_name (dir))) {
            if (name[0] != '.' && is_audio_name (name))
                g_ptr_array_add (files, g_build_filename (dirs[d], name, NULL));
        }
    }
    for (guint i = 0; i < G_N_ELEMENTS (system_sounds); i++)
        found |= system_sound_offered (channel, i) && g_strcmp0 (system_sounds[i].path, setting->file) == 0;
    for (guint i = 0; !found && i < files->len; i++)
        found = g_strcmp0 (g_ptr_array_index (files, i), setting->file) == 0;
    if (!found)
        g_ptr_array_add (files, g_strdup (setting->file));
    g_ptr_array_sort (files, compare_titles);
    return files;
}

/* Ogg Vorbis or WAV, the formats libcanberra plays */
static gboolean
playable (GFile *file)
{
    g_autoptr(GFileInputStream) stream = g_file_read (file, NULL, NULL);
    guint8 head[64] = { 0 };
    gsize got = 0;

    if (stream == NULL ||
        !g_input_stream_read_all (G_INPUT_STREAM (stream), head, sizeof head, &got, NULL, NULL))
        return FALSE;
    if (got >= 12 && memcmp (head, "RIFF", 4) == 0 && memcmp (head + 8, "WAVE", 4) == 0)
        return TRUE;
    /* The first Ogg page holds the codec's identification header */
    return got >= 35 && memcmp (head, "OggS", 4) == 0 && memcmp (head + 28, "\001vorbis", 7) == 0;
}

static void
show_error (GtkWidget *widget, const char *heading, const char *body)
{
    AdwDialog *dialog = adw_alert_dialog_new (heading, body);

    adw_alert_dialog_add_response (ADW_ALERT_DIALOG (dialog), "close", P_("_Close"));
    adw_dialog_present (dialog, widget);
}

/* A free name in the own sounds for BASENAME: "name (2).oga" when "name.oga" is taken */
static GFile *
own_target (const char *dir, const char *basename)
{
    g_autofree char *stem = g_strdup (basename);
    char *dot = strrchr (stem, '.');
    g_autofree char *ext = g_strdup (dot ? dot : "");

    if (dot)
        *dot = '\0';
    for (guint i = 1; i < 1000; i++) {
        g_autofree char *name = i == 1 ? g_strdup (basename) : g_strdup_printf ("%s (%u)%s", stem, i, ext);
        g_autofree char *path = g_build_filename (dir, name, NULL);

        if (!g_file_test (path, G_FILE_TEST_EXISTS))
            return g_file_new_for_path (path);
    }
    return NULL;
}

/* Editing an added sound (one in the own sounds): rename or delete it */

typedef struct {
    SoundPage *page;
    GtkWidget *check;
    GtkWidget *group;   /* keeps the page alive (it is the group's data) */
    AdwEntryRow *name_row;
    AdwAlertDialog *dialog;   /* not a reference: the edit is the dialog's data */
} SoundEdit;

static void
sound_edit_free (SoundEdit *edit)
{
    g_object_unref (edit->check);
    g_object_unref (edit->group);
    g_free (edit);
}

static gboolean
is_own_sound (const char *path)
{
    g_autofree char *dir = own_sounds_dir ();
    g_autofree char *parent = path ? g_path_get_dirname (path) : NULL;

    return parent && g_strcmp0 (parent, dir) == 0;
}

/* A file name made of what was typed: no slash, not hidden, not empty */
static char *
sound_edit_name (SoundEdit *edit)
{
    g_autofree char *name = g_strstrip (g_strdup (gtk_editable_get_text (GTK_EDITABLE (edit->name_row))));

    if (*name == '\0' || *name == '.' || strchr (name, '/') != NULL)
        return NULL;
    return g_steal_pointer (&name);
}

static void
sound_edit_changed (SoundEdit *edit)
{
    g_autofree char *name = sound_edit_name (edit);

    adw_alert_dialog_set_response_enabled (edit->dialog, "rename", name != NULL);
}

/* The settings of all kinds that use OLD: to NEW, or back to the default without NEW */
static void
sound_edit_settings (SoundPage *page, const char *old, const char *new)
{
    gboolean changed = FALSE;

    for (guint c = 0; c < N_CHANNELS; c++) {
        Setting *setting = &page->model->settings[c];

        if (setting->sound != SOUND_FILE || g_strcmp0 (setting->file, old) != 0)
            continue;
        if (new) {
            g_set_str (&setting->file, new);
        } else {
            g_clear_pointer (&setting->file, g_free);
            setting->sound = SOUND_DEFAULT;
        }
        changed = TRUE;
    }
    if (changed)
        model_save (page->model);
}

static void
sound_rename (SoundEdit *edit)
{
    g_autofree char *old = g_strdup (g_object_get_data (G_OBJECT (edit->check), "plinius-file"));
    g_autofree char *name = sound_edit_name (edit);
    g_autofree char *dir = g_path_get_dirname (old);
    g_autofree char *base = g_path_get_basename (old);
    const char *ext = strrchr (base, '.');
    g_autofree char *basename = NULL, *path = NULL, *title = NULL;
    g_autoptr(GFile) target = NULL;

    if (name == NULL)
        return;
    basename = g_strconcat (name, ext ? ext : "", NULL);
    if (g_strcmp0 (basename, base) == 0)
        return;
    target = own_target (dir, basename);
    if (target == NULL || g_rename (old, (path = g_file_get_path (target))) != 0) {
        show_error (edit->group, P_("Sound Not Renamed"), g_strerror (target ? errno : EEXIST));
        return;
    }
    sound_edit_settings (edit->page, old, path);
    title = file_title (path);
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (g_object_get_data (G_OBJECT (edit->check), "plinius-row")),
                                   title);
    g_object_set_data_full (G_OBJECT (edit->check), "plinius-file", g_steal_pointer (&path), g_free);
}

static void
sound_delete (SoundEdit *edit)
{
    SoundPage *page = edit->page;
    g_autofree char *path = g_strdup (g_object_get_data (G_OBJECT (edit->check), "plinius-file"));
    GtkWidget *row = g_object_get_data (G_OBJECT (edit->check), "plinius-row");

    if (g_remove (path) != 0 && errno != ENOENT) {
        show_error (edit->group, P_("Sound Not Deleted"), g_strerror (errno));
        return;
    }
    preview_stop (page->model);
    sound_edit_settings (page, path, NULL);
    /* The kind of this page goes back to its default, without playing it */
    if (gtk_check_button_get_active (GTK_CHECK_BUTTON (edit->check)) && page->first_check) {
        g_signal_handlers_block_by_func (page->first_check, sound_toggled, page);
        gtk_check_button_set_active (GTK_CHECK_BUTTON (page->first_check), TRUE);
        g_signal_handlers_unblock_by_func (page->first_check, sound_toggled, page);
    }
    adw_preferences_group_remove (page->own_group, row);
}

static void
sound_edit_response (AdwAlertDialog *dialog, const char *response, SoundEdit *edit)
{
    if (g_strcmp0 (response, "rename") == 0)
        sound_rename (edit);
    else if (g_strcmp0 (response, "delete") == 0)
        sound_delete (edit);
}

static void
edit_clicked (GtkWidget *button, GtkWidget *check)
{
    SoundPage *page = g_object_get_data (G_OBJECT (button), "plinius-sound-page");
    const char *path = g_object_get_data (G_OBJECT (check), "plinius-file");
    g_autofree char *title = file_title (path);
    AdwDialog *dialog = adw_alert_dialog_new (P_("Edit Sound"), NULL);
    GtkWidget *list = gtk_list_box_new ();
    SoundEdit *edit = g_new0 (SoundEdit, 1);

    edit->page = page;
    edit->dialog = ADW_ALERT_DIALOG (dialog);
    edit->check = g_object_ref (check);
    edit->group = g_object_ref (GTK_WIDGET (page->own_group));
    edit->name_row = ADW_ENTRY_ROW (adw_entry_row_new ());
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (edit->name_row), P_("Name"));
    gtk_editable_set_text (GTK_EDITABLE (edit->name_row), title);
    adw_entry_row_set_activates_default (edit->name_row, TRUE);
    gtk_list_box_set_selection_mode (GTK_LIST_BOX (list), GTK_SELECTION_NONE);
    gtk_widget_add_css_class (list, "boxed-list");
    gtk_list_box_append (GTK_LIST_BOX (list), GTK_WIDGET (edit->name_row));
    adw_alert_dialog_set_extra_child (ADW_ALERT_DIALOG (dialog), list);

    adw_alert_dialog_add_responses (ADW_ALERT_DIALOG (dialog), "cancel", P_("_Cancel"), "delete", P_("_Delete"),
                                    "rename", P_("_Rename"), NULL);
    adw_alert_dialog_set_response_appearance (ADW_ALERT_DIALOG (dialog), "delete", ADW_RESPONSE_DESTRUCTIVE);
    adw_alert_dialog_set_response_appearance (ADW_ALERT_DIALOG (dialog), "rename", ADW_RESPONSE_SUGGESTED);
    adw_alert_dialog_set_default_response (ADW_ALERT_DIALOG (dialog), "rename");
    adw_alert_dialog_set_close_response (ADW_ALERT_DIALOG (dialog), "cancel");
    g_object_set_data_full (G_OBJECT (dialog), "plinius-sound-edit", edit, (GDestroyNotify) sound_edit_free);
    g_signal_connect_swapped (edit->name_row, "notify::text", G_CALLBACK (sound_edit_changed), edit);
    g_signal_connect (dialog, "response", G_CALLBACK (sound_edit_response), edit);
    adw_dialog_present (dialog, button);
}

/* The edit button of an added sound, at the end of its row */
static void
add_edit_button (SoundPage *page, GtkWidget *check)
{
    GtkWidget *row = g_object_get_data (G_OBJECT (check), "plinius-row");
    GtkWidget *button = gtk_button_new_from_icon_name ("document-edit-symbolic");

    gtk_widget_set_valign (button, GTK_ALIGN_CENTER);
    gtk_widget_add_css_class (button, "flat");
    gtk_widget_set_tooltip_text (button, P_("Rename or Delete"));
    g_object_set_data (G_OBJECT (button), "plinius-sound-page", page);
    g_signal_connect (button, "clicked", G_CALLBACK (edit_clicked), check);
    adw_action_row_add_suffix (ADW_ACTION_ROW (row), button);
}

static void
sound_chosen (GObject *source, GAsyncResult *result, gpointer data)
{
    g_autoptr(GtkWidget) group = data;
    SoundPage *page = g_object_get_data (G_OBJECT (group), "plinius-sound-page");
    g_autoptr(GFile) file = gtk_file_dialog_open_finish (GTK_FILE_DIALOG (source), result, NULL);
    g_autofree char *dir = own_sounds_dir ();
    g_autofree char *basename = NULL, *path = NULL, *title = NULL;
    g_autoptr(GFile) target = NULL;
    g_autoptr(GError) error = NULL;
    GtkWidget *check;

    if (file == NULL || page == NULL)
        return;
    if (!playable (file)) {
        show_error (group, P_("Sound Not Supported"),
                    P_("The phone plays ringtones and notification sounds in the Ogg Vorbis or WAV format. "
                       "Convert the file to one of them first."));
        return;
    }
    basename = g_file_get_basename (file);
    if (g_mkdir_with_parents (dir, 0700) != 0 || (target = own_target (dir, basename)) == NULL ||
        !g_file_copy (file, target, G_FILE_COPY_NONE, NULL, NULL, NULL, &error)) {
        show_error (group, P_("Sound Not Added"), error ? error->message : g_strerror (errno));
        return;
    }
    path = g_file_get_path (target);
    title = file_title (path);
    check = sound_row (page, page->own_group, title, NULL, SOUND_FILE, path);
    add_edit_button (page, check);
    gtk_check_button_set_active (GTK_CHECK_BUTTON (check), TRUE);
}

static void
add_sound_clicked (GtkWidget *button, SoundPage *page)
{
    g_autoptr(GtkFileDialog) dialog = gtk_file_dialog_new ();
    g_autoptr(GtkFileFilter) filter = gtk_file_filter_new ();
    g_autoptr(GListStore) filters = g_list_store_new (GTK_TYPE_FILE_FILTER);
    const char *music = g_get_user_special_dir (G_USER_DIRECTORY_MUSIC);
    GtkRoot *root = gtk_widget_get_root (button);

    gtk_file_filter_set_name (filter, P_("Ogg Vorbis or WAV"));
    gtk_file_filter_add_mime_type (filter, "audio/ogg");
    gtk_file_filter_add_mime_type (filter, "audio/x-vorbis+ogg");
    gtk_file_filter_add_mime_type (filter, "audio/vorbis");
    gtk_file_filter_add_mime_type (filter, "audio/x-wav");
    gtk_file_filter_add_mime_type (filter, "audio/wav");
    gtk_file_filter_add_mime_type (filter, "audio/vnd.wave");
    gtk_file_filter_add_suffix (filter, "oga");
    gtk_file_filter_add_suffix (filter, "ogg");
    gtk_file_filter_add_suffix (filter, "wav");
    g_list_store_append (filters, filter);
    gtk_file_dialog_set_filters (dialog, G_LIST_MODEL (filters));
    gtk_file_dialog_set_default_filter (dialog, filter);
    gtk_file_dialog_set_title (dialog, P_("Choose a Sound"));
    if (music) {
        g_autoptr(GFile) folder = g_file_new_for_path (music);

        gtk_file_dialog_set_initial_folder (dialog, folder);
    }
    gtk_file_dialog_open (dialog, GTK_IS_WINDOW (root) ? GTK_WINDOW (root) : NULL, NULL, sound_chosen,
                          g_object_ref (page->own_group));
}

static void
sound_page_hiding (AdwNavigationPage *navigation_page, SoundPage *page)
{
    preview_stop (page->model);
}

static AdwNavigationPage *
sound_page_new (Model *model, guint channel)
{
    const Channel *ch = &channels[channel];
    const Setting *setting = &model->settings[channel];
    GtkWidget *toolbar = adw_toolbar_view_new ();
    GtkWidget *prefs = adw_preferences_page_new ();
    AdwNavigationPage *navigation_page = adw_navigation_page_new (toolbar, P_("Sound"));
    AdwPreferencesGroup *group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    AdwPreferencesGroup *system = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    AdwPreferencesGroup *own = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    SoundPage *page = g_new0 (SoundPage, 1);
    g_autoptr(GPtrArray) files = NULL;
    GtkWidget *header, *add_button;

    page->model = model_ref (model);
    page->channel = channel;
    page->own_group = own;
    g_object_set_data_full (G_OBJECT (own), "plinius-sound-page", page, (GDestroyNotify) sound_page_free);

    sound_row (page, group, P_("Default"), ch->stock_title ? P_(ch->stock_title) : P_("No Sound"), SOUND_DEFAULT,
               NULL);
    sound_row (page, group, _("None"), NULL, SOUND_NONE, NULL);
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (prefs), group);

    adw_preferences_group_set_title (system, P_("System Sounds"));
    for (guint i = 0; i < G_N_ELEMENTS (system_sounds); i++) {
        g_autofree char *title = NULL;

        if (!system_sound_offered (ch, i) || !g_file_test (system_sounds[i].path, G_FILE_TEST_IS_REGULAR))
            continue;
        title = file_title (system_sounds[i].path);
        sound_row (page, system, title, NULL, SOUND_FILE, system_sounds[i].path);
    }
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (prefs), system);

    adw_preferences_group_set_title (own, P_("Own Sounds"));
    files = own_sounds (ch, setting);
    for (guint i = 0; i < files->len; i++) {
        const char *path = g_ptr_array_index (files, i);
        g_autofree char *title = file_title (path);

        GtkWidget *check = sound_row (page, own, title, NULL, SOUND_FILE, path);

        /* Only added sounds can be renamed and deleted here, not those in Music/Ringtones */
        if (is_own_sound (path))
            add_edit_button (page, check);
    }
    /* Shown also while empty: where an added sound goes */
    adw_preferences_group_set_description (own, P_("Ogg Vorbis or WAV. The file is copied, so it stays when the "
                                                   "original is deleted."));
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (prefs), own);

    /* "Add" at the top right, like Web Apps and Local Accounts */
    header = adw_header_bar_new ();
    add_button = gtk_button_new_with_label (P_("Add"));
    gtk_widget_add_css_class (add_button, "suggested-action");
    gtk_widget_set_tooltip_text (add_button, P_("Add a Sound of Your Own"));
    g_signal_connect (add_button, "clicked", G_CALLBACK (add_sound_clicked), page);
    adw_header_bar_pack_end (ADW_HEADER_BAR (header), add_button);
    adw_toolbar_view_add_top_bar (ADW_TOOLBAR_VIEW (toolbar), header);
    adw_toolbar_view_set_content (ADW_TOOLBAR_VIEW (toolbar), prefs);
    g_signal_connect (navigation_page, "hiding", G_CALLBACK (sound_page_hiding), page);
    return navigation_page;
}

/* The page of a channel */

typedef struct {
    Model *model;
    guint channel;
    GtkWidget *sound_row;
    AdwComboRow *rhythm_row;
    GtkWidget *strength_row;
    GtkRange *strength;
    GtkWidget *reset_group;
    GtkWidget *test_row;
    guint preview_id;
} ChannelPage;

static void
channel_page_free (ChannelPage *page)
{
    if (page->model->test_changed_data == page) {
        page->model->test_changed = NULL;
        page->model->test_changed_data = NULL;
    }
    g_clear_handle_id (&page->preview_id, g_source_remove);
    model_unref (page->model);
    g_free (page);
}

static void rhythm_selected (AdwComboRow *row, GParamSpec *pspec, ChannelPage *page);
static void strength_changed (GtkRange *range, ChannelPage *page);

/* The rows as the setting is */
static void
channel_page_update (ChannelPage *page)
{
    const Setting *setting = &page->model->settings[page->channel];
    g_autofree char *sound = sound_title (&channels[page->channel], setting);

    adw_action_row_set_subtitle (ADW_ACTION_ROW (page->sound_row), sound);
    g_signal_handlers_block_by_func (page->rhythm_row, rhythm_selected, page);
    adw_combo_row_set_selected (page->rhythm_row, rhythm_to_row (&channels[page->channel], setting->rhythm));
    g_signal_handlers_unblock_by_func (page->rhythm_row, rhythm_selected, page);
    g_signal_handlers_block_by_func (page->strength, strength_changed, page);
    gtk_range_set_value (page->strength, round (setting->strength * 100));
    g_signal_handlers_unblock_by_func (page->strength, strength_changed, page);
    gtk_widget_set_sensitive (page->strength_row, setting->rhythm != RHYTHM_OFF);
    gtk_widget_set_visible (page->reset_group, !setting_is_default (setting));
}

static void
rhythm_selected (AdwComboRow *row, GParamSpec *pspec, ChannelPage *page)
{
    Setting *setting = &page->model->settings[page->channel];

    setting->rhythm = rhythm_from_row (&channels[page->channel], adw_combo_row_get_selected (row));
    if (setting->rhythm == RHYTHM_DEFAULT)
        setting->strength = channels[page->channel].stock_strength;
    model_save (page->model);
    channel_page_update (page);
    preview_vibration (page->model, &channels[page->channel], setting);
}

static gboolean
strength_settled (gpointer data)
{
    ChannelPage *page = data;
    Setting *setting = &page->model->settings[page->channel];

    page->preview_id = 0;
    model_save (page->model);
    preview_vibration (page->model, &channels[page->channel], setting);
    return G_SOURCE_REMOVE;
}

static void
strength_changed (GtkRange *range, ChannelPage *page)
{
    Setting *setting = &page->model->settings[page->channel];

    setting->strength = CLAMP (gtk_range_get_value (range) / 100, MIN_STRENGTH, 1.0);
    /* The default vibration has no strength of its own here: it becomes the rhythm closest to it */
    if (setting->rhythm == RHYTHM_DEFAULT) {
        setting->rhythm = rhythm_by_id (channels[page->channel].stock_rhythm);
        channel_page_update (page);
    }
    /* Saved and felt when the slider rests */
    g_clear_handle_id (&page->preview_id, g_source_remove);
    page->preview_id = g_timeout_add (350, strength_settled, page);
}

static char *
format_percent (GtkScale *scale, double value, gpointer data)
{
    return g_strdup_printf ("%d %%", (int) round (value));
}

static void
sound_row_open (AdwActionRow *row, ChannelPage *page)
{
    GtkWidget *view = gtk_widget_get_ancestor (GTK_WIDGET (row), ADW_TYPE_NAVIGATION_VIEW);

    if (view)
        adw_navigation_view_push (ADW_NAVIGATION_VIEW (view), sound_page_new (page->model, page->channel));
}

/* "Test" or, while the test runs, "Stop" */
static void
test_row_update (gpointer data)
{
    ChannelPage *page = data;
    gboolean testing = page->model->testing;

    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (page->test_row), testing ? P_("_Stop") : P_("_Test"));
    adw_button_row_set_start_icon_name (ADW_BUTTON_ROW (page->test_row),
                                        testing ? "media-playback-stop-symbolic" : "media-playback-start-symbolic");
}

static void
test_activated (AdwButtonRow *row, ChannelPage *page)
{
    if (page->model->testing)
        test_stop (page->model);
    else
        test_start (page->model, &channels[page->channel]);
}

static void
reset_activated (AdwButtonRow *row, ChannelPage *page)
{
    setting_reset (&page->model->settings[page->channel], &channels[page->channel]);
    model_save (page->model);
    channel_page_update (page);
}

static void
channel_page_showing (AdwNavigationPage *navigation_page, ChannelPage *page)
{
    page->model->test_changed = test_row_update;
    page->model->test_changed_data = page;
    test_row_update (page);
    channel_page_update (page);
}

static void
channel_page_hiding (AdwNavigationPage *navigation_page, ChannelPage *page)
{
    /* A strength still moving is kept */
    if (page->preview_id) {
        g_clear_handle_id (&page->preview_id, g_source_remove);
        model_save (page->model);
    }
    test_stop (page->model);
}

static AdwNavigationPage *
channel_page_new (Model *model, guint channel)
{
    const Channel *ch = &channels[channel];
    GtkWidget *toolbar = adw_toolbar_view_new ();
    GtkWidget *prefs = adw_preferences_page_new ();
    AdwNavigationPage *navigation_page = adw_navigation_page_new (toolbar, P_(ch->title));
    AdwPreferencesGroup *sound = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    AdwPreferencesGroup *vibration = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    AdwPreferencesGroup *test = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    AdwPreferencesGroup *reset = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    g_autoptr(GtkStringList) names = gtk_string_list_new (NULL);
    ChannelPage *page = g_new0 (ChannelPage, 1);
    GtkWidget *row, *scale;

    page->model = model_ref (model);
    page->channel = channel;
    g_object_set_data_full (G_OBJECT (navigation_page), "plinius-channel-page", page,
                            (GDestroyNotify) channel_page_free);

    row = adw_action_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_("Sound"));
    adw_action_row_add_suffix (ADW_ACTION_ROW (row), gtk_image_new_from_icon_name ("go-next-symbolic"));
    gtk_list_box_row_set_activatable (GTK_LIST_BOX_ROW (row), TRUE);
    g_signal_connect (row, "activated", G_CALLBACK (sound_row_open), page);
    adw_preferences_group_add (sound, row);
    page->sound_row = row;
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (prefs), sound);

    adw_preferences_group_set_title (vibration, P_("Vibration"));
    gtk_string_list_append (names, P_("Default"));
    gtk_string_list_append (names, P_("Off"));
    for (guint r = 0; r < G_N_ELEMENTS (rhythms); r++) {
        if (rhythm_fits (ch, r))
            gtk_string_list_append (names, P_(rhythms[r].title));
    }
    row = adw_combo_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_("Rhythm"));
    adw_combo_row_set_model (ADW_COMBO_ROW (row), G_LIST_MODEL (names));
    page->rhythm_row = ADW_COMBO_ROW (row);
    adw_preferences_group_add (vibration, row);

    row = adw_action_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_("Strength"));
    scale = gtk_scale_new_with_range (GTK_ORIENTATION_HORIZONTAL, MIN_STRENGTH * 100, 100, 5);
    gtk_scale_set_draw_value (GTK_SCALE (scale), TRUE);
    gtk_scale_set_value_pos (GTK_SCALE (scale), GTK_POS_RIGHT);
    gtk_scale_set_format_value_func (GTK_SCALE (scale), format_percent, NULL, NULL);
    gtk_widget_set_hexpand (scale, TRUE);
    gtk_widget_set_size_request (scale, 150, -1);
    gtk_widget_set_valign (scale, GTK_ALIGN_CENTER);
    adw_action_row_add_suffix (ADW_ACTION_ROW (row), scale);
    page->strength_row = row;
    page->strength = GTK_RANGE (scale);
    adw_preferences_group_add (vibration, row);
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (prefs), vibration);

    row = adw_button_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_("_Test"));
    adw_preferences_row_set_use_underline (ADW_PREFERENCES_ROW (row), TRUE);
    adw_button_row_set_start_icon_name (ADW_BUTTON_ROW (row), "media-playback-start-symbolic");
    g_signal_connect (row, "activated", G_CALLBACK (test_activated), page);
    page->test_row = row;
    adw_preferences_group_add (test, row);
    if (ch->loops)
        adw_preferences_group_set_description (test, P_("Rings for a few seconds, as the phone will."));
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (prefs), test);

    row = adw_button_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_("_Reset to Default"));
    adw_preferences_row_set_use_underline (ADW_PREFERENCES_ROW (row), TRUE);
    g_signal_connect (row, "activated", G_CALLBACK (reset_activated), page);
    adw_preferences_group_add (reset, row);
    page->reset_group = GTK_WIDGET (reset);
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (prefs), reset);

    channel_page_update (page);
    g_signal_connect (page->rhythm_row, "notify::selected", G_CALLBACK (rhythm_selected), page);
    g_signal_connect (page->strength, "value-changed", G_CALLBACK (strength_changed), page);

    adw_toolbar_view_add_top_bar (ADW_TOOLBAR_VIEW (toolbar), adw_header_bar_new ());
    adw_toolbar_view_set_content (ADW_TOOLBAR_VIEW (toolbar), prefs);
    g_signal_connect (navigation_page, "showing", G_CALLBACK (channel_page_showing), page);
    g_signal_connect (navigation_page, "hiding", G_CALLBACK (channel_page_hiding), page);
    return navigation_page;
}

/* The rows of the first page */

static void
channel_row_activated (AdwActionRow *row, Model *model)
{
    GtkWidget *view = gtk_widget_get_ancestor (GTK_WIDGET (row), ADW_TYPE_NAVIGATION_VIEW);
    guint channel = GPOINTER_TO_UINT (g_object_get_data (G_OBJECT (row), "plinius-channel"));

    if (view)
        adw_navigation_view_push (ADW_NAVIGATION_VIEW (view), channel_page_new (model, channel));
}

static void
main_page_showing (AdwNavigationPage *navigation_page, Model *model)
{
    model_update_rows (model);
}

static void
main_page_gone (gpointer data)
{
    Model *model = data;

    for (guint c = 0; c < N_CHANNELS; c++)
        model->rows[c] = NULL;
    test_stop (model);
    model_unref (model);
}

/* The panel */

struct _CcFeedbackPanel {
    CcPanel parent_instance;

    Model *model;
};

G_DEFINE_TYPE (CcFeedbackPanel, cc_feedback_panel, CC_TYPE_PANEL)

static void
cc_feedback_panel_dispose (GObject *object)
{
    CcFeedbackPanel *self = CC_FEEDBACK_PANEL (object);

    g_clear_pointer (&self->model, main_page_gone);
    G_OBJECT_CLASS (cc_feedback_panel_parent_class)->dispose (object);
}

static void
cc_feedback_panel_class_init (CcFeedbackPanelClass *klass)
{
    G_OBJECT_CLASS (klass)->dispose = cc_feedback_panel_dispose;
}

/* The first page: one row for each kind */
static void
cc_feedback_panel_init (CcFeedbackPanel *self)
{
    Model *model = model_new ();
    GtkWidget *toolbar = adw_toolbar_view_new ();
    GtkWidget *prefs = adw_preferences_page_new ();
    AdwPreferencesGroup *group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());

    self->model = model;
    adw_navigation_page_set_title (ADW_NAVIGATION_PAGE (self), P_("Sounds and Vibration"));
    for (guint c = 0; c < N_CHANNELS; c++) {
        GtkWidget *row = adw_action_row_new ();

        adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_(channels[c].title));
        adw_action_row_add_prefix (ADW_ACTION_ROW (row), gtk_image_new_from_icon_name (channels[c].icon));
        adw_action_row_add_suffix (ADW_ACTION_ROW (row), gtk_image_new_from_icon_name ("go-next-symbolic"));
        gtk_list_box_row_set_activatable (GTK_LIST_BOX_ROW (row), TRUE);
        g_object_set_data (G_OBJECT (row), "plinius-channel", GUINT_TO_POINTER (c));
        g_signal_connect (row, "activated", G_CALLBACK (channel_row_activated), model);
        adw_preferences_group_add (group, row);
        model->rows[c] = row;
    }
    model_update_rows (model);
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (prefs), group);

    adw_toolbar_view_add_top_bar (ADW_TOOLBAR_VIEW (toolbar), adw_header_bar_new ());
    adw_toolbar_view_set_content (ADW_TOOLBAR_VIEW (toolbar), prefs);
    adw_navigation_page_set_child (ADW_NAVIGATION_PAGE (self), toolbar);
    g_signal_connect (self, "showing", G_CALLBACK (main_page_showing), model);
}

/* Listed with feedbackd only (its settings know a theme) */
void
cc_feedback_panel_static_init_func (void)
{
    CcApplication *application = CC_APPLICATION (g_application_get_default ());
    g_autoptr(GSettingsSchema) schema = NULL;
    GSettingsSchemaSource *source = g_settings_schema_source_get_default ();

    if (source)
        schema = g_settings_schema_source_lookup (source, FBD_SCHEMA, TRUE);
    cc_shell_model_set_panel_visibility (cc_application_get_model (application), "feedback",
                                         schema && g_settings_schema_has_key (schema, "theme") ? CC_PANEL_VISIBLE
                                                                                                : CC_PANEL_HIDDEN);
}
