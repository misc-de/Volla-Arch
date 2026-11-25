/*
 * cc-speech-panel.c
 *
 * Plinius: "Speech Output", a panel of its own below Sounds and Vibration: the voice, the speech
 * rate and the announcements of plinius-speech. The voice and the rate apply to everything the
 * phone says (plinius-speechd reads them for every Speak()), the caller announcement in a
 * Bluetooth headset is the first thing that speaks and has a switch of its own.
 *
 * Settings: org.plinius.speech - voices (language -> voice id; a language without an entry uses
 * its default voice), rate, announce-callers.
 *
 * Speech always comes in the system language (the first of g_get_language_names(), "pt_BR.UTF-8"
 * -> language "pt", locale "pt_BR"; "C" -> "en", as plinius-speechd decides it), so the page lists
 * only the voices of that language: the installed ones (/usr/share/plinius-speech/voices/<id>/
 * voice.json) and the ones the repository has (the index /usr/share/plinius-speech/voices.json of
 * plinius-speech, with the package and its installed size). The voices of the system's locale come
 * first; when the language's voices are of several locales (en_US and en_GB, pt_PT and pt_BR),
 * each name says its region. The voice that speaks, as the service picks it: the one chosen for
 * the language, else the default voice of the system's locale, else any voice of that locale, else
 * a default voice of the language, else the first one. A voice is downloaded and removed like the dictation model:
 * pacman -Si for the download size, a question, then "pkexec plinius-pkg install|remove <package>"
 * (plinius-software's root helper) with the progress from the package file growing in pacman's
 * cache. A newly installed voice becomes the voice of its language; the chosen voice cannot be
 * removed (choose another one first).
 *
 * Each installed voice has a button to listen to it: Speak() of org.plinius.Speech1 with the voice
 * as option and a sample sentence; the same button stops it again, and only one sample plays at a
 * time. Finished() or the service leaving the bus ends it.
 *
 * Our own strings use the gettext domain plinius-control-center (not GNOME's po/).
 *
 * SPDX-License-Identifier: MIT
 */

#undef G_LOG_DOMAIN
#define G_LOG_DOMAIN "cc-speech-panel"

#include "config.h"

#include "cc-speech-panel.h"
#include "shell/cc-application.h"

#include <glib/gstdio.h>
#include <json-glib/json-glib.h>
#include <string.h>

#define PLINIUS_DOMAIN "plinius-control-center"
#define P_(String) g_dgettext (PLINIUS_DOMAIN, String)
#define PN_(String) (String)

#define SPEECH_SCHEMA "org.plinius.speech"
#define SPEECH_BUS "org.plinius.Speech1"
#define SPEECH_PATH "/org/plinius/Speech1"
#define SPEECH_IFACE "org.plinius.Speech1"
#define DATA_DIR "/usr/share/plinius-speech"
#define VOICES_DIR DATA_DIR "/voices"
#define INDEX_FILE DATA_DIR "/voices.json"
#define PKG_TOOL "/usr/lib/plinius-software/plinius-pkg"
#define PACMAN_CACHE "/var/cache/pacman/pkg"

/* Exit codes of plinius-pkg (see the top of the script) and of pkexec */
enum {
    EXIT_DB_NEWER         = 3,
    EXIT_FILE_GONE        = 4,
    EXIT_BUSY             = 9,
    EXIT_PKEXEC_DISMISSED = 126,
    EXIT_PKEXEC_DENIED    = 127,
};

/* The regions of the locales plinius-speech has voices for, named after a voice's name */
static const struct {
    const char *code;
    const char *name;
} regions[] = {
    { "BE", PN_("Belgium") },
    { "BR", PN_("Brazil") },
    { "DE", PN_("Germany") },
    { "ES", PN_("Spain") },
    { "FR", PN_("France") },
    { "GB", PN_("United Kingdom") },
    { "IT", PN_("Italy") },
    { "NL", PN_("Netherlands") },
    { "PL", PN_("Poland") },
    { "PT", PN_("Portugal") },
    { "RU", PN_("Russia") },
    { "US", PN_("United States") },
};

/* The languages plinius-speech has voices for, named in the page's text */
static const struct {
    const char *code;
    const char *name;
} languages[] = {
    { "de", PN_("German") },
    { "en", PN_("English") },
    { "es", PN_("Spanish") },
    { "fr", PN_("French") },
    { "it", PN_("Italian") },
    { "nl", PN_("Dutch") },
    { "pl", PN_("Polish") },
    { "pt", PN_("Portuguese") },
    { "ru", PN_("Russian") },
};

typedef struct {
    char *id;
    char *name;
    char *locale;       /* "de_DE"; "" if not given */
    char *package;      /* NULL: installed, but not from a package the index names */
    guint64 size;       /* installed size from the index, 0 if unknown */
    gboolean is_default;
    gboolean installed;
} Voice;

struct _CcSpeechPanel {
    CcPanel parent_instance;

    GSettings *settings;
    GCancellable *cancellable;
    char *lang;
    char *locale;                /* "pt_BR"; "" if the system names no region */
    GPtrArray *voices;           /* Voice, of the system language */

    AdwPreferencesGroup *voice_group;
    GPtrArray *rows;             /* the voice rows now in voice_group */
    GPtrArray *preview_buttons;  /* their listen buttons */
    AdwActionRow *progress_row;
    GtkProgressBar *bar;
    gboolean syncing;            /* the check buttons are set from the setting */

    /* Installing or removing a voice */
    gboolean busy;
    char *busy_package;
    char *busy_voice;
    guint64 total;               /* download size of busy_package */
    guint poll_id;

    /* Listening to a voice */
    GDBusConnection *bus;
    guint finished_sub;
    guint name_watch;
    char *playing_voice;         /* NULL: nothing plays */
    guint32 playing_id;          /* 0 while Speak() has not answered */
    guint generation;            /* counts starts and stops; an older answer is stale */
    GArray *finished_early;      /* ids that finished before Speak() answered */
};

G_DEFINE_TYPE (CcSpeechPanel, cc_speech_panel, CC_TYPE_PANEL)

static void rebuild_rows (CcSpeechPanel *self);

static void
voice_free (gpointer data)
{
    Voice *v = data;

    g_free (v->id);
    g_free (v->name);
    g_free (v->locale);
    g_free (v->package);
    g_free (v);
}

/* ----- The system language and its voices ----- */

/* The system's language ("pt") and locale ("pt_BR", or "" without a region) */
static void
system_language (char **lang, char **locale)
{
    const char *const *names = g_get_language_names ();
    const char *first = names[0];
    g_autofree char *code = g_strndup (first, strcspn (first, "_.@"));
    g_autofree char *full = g_strndup (first, strcspn (first, ".@"));

    if (*code == '\0' || g_str_equal (code, "C") || g_str_equal (code, "POSIX")) {
        *lang = g_strdup ("en");
        *locale = g_strdup ("");
        return;
    }
    *lang = g_ascii_strdown (code, -1);
    *locale = strchr (full, '_') ? g_strdup (full) : g_strdup ("");
}

/* "United States" for "en_US", the region code if we have no name for it */
static const char *
region_name (const char *locale)
{
    const char *code = strchr (locale, '_');

    if (code == NULL)
        return locale;
    code++;
    for (guint i = 0; i < G_N_ELEMENTS (regions); i++)
        if (g_str_equal (regions[i].code, code))
            return P_(regions[i].name);
    return code;
}

static const char *
language_name (const char *code)
{
    for (guint i = 0; i < G_N_ELEMENTS (languages); i++)
        if (g_str_equal (languages[i].code, code))
            return P_(languages[i].name);
    return code;
}

/* A voice id names a directory: no slashes, not "." or ".." */
static gboolean
valid_id (const char *id)
{
    return id && *id && !strchr (id, '/') && !g_str_equal (id, ".") && !g_str_equal (id, "..");
}

static const char *
member_string (JsonObject *o, const char *name)
{
    JsonNode *n = json_object_get_member (o, name);

    return n && JSON_NODE_HOLDS_VALUE (n) && json_node_get_value_type (n) == G_TYPE_STRING ?
        json_node_get_string (n) : NULL;
}

static gboolean
member_true (JsonObject *o, const char *name)
{
    JsonNode *n = json_object_get_member (o, name);

    return n && JSON_NODE_HOLDS_VALUE (n) && json_node_get_value_type (n) == G_TYPE_BOOLEAN &&
        json_node_get_boolean (n);
}

static guint64
member_size (JsonObject *o, const char *name)
{
    JsonNode *n = json_object_get_member (o, name);

    return n && JSON_NODE_HOLDS_VALUE (n) && json_node_get_value_type (n) == G_TYPE_INT64 ?
        (guint64) MAX (json_node_get_int (n), 0) : 0;
}

static JsonNode *
read_json (const char *path)
{
    g_autoptr(JsonParser) parser = json_parser_new ();
    g_autoptr(GError) error = NULL;

    if (!json_parser_load_from_file (parser, path, &error)) {
        if (!g_error_matches (error, G_FILE_ERROR, G_FILE_ERROR_NOENT))
            g_warning ("Cannot read %s: %s", path, error->message);
        return NULL;
    }
    return json_parser_steal_root (parser);
}

static Voice *
find_voice (CcSpeechPanel *self, const char *id)
{
    for (guint i = 0; i < self->voices->len; i++) {
        Voice *v = g_ptr_array_index (self->voices, i);

        if (g_strcmp0 (v->id, id) == 0)
            return v;
    }
    return NULL;
}

static char *
voice_file (const char *id)
{
    return g_build_filename (VOICES_DIR, id, "voice.json", NULL);
}

static int
by_locale (gconstpointer a, gconstpointer b, gpointer data)
{
    CcSpeechPanel *self = data;
    const Voice *va = *(Voice **) a, *vb = *(Voice **) b;

    return (g_strcmp0 (vb->locale, self->locale) == 0) - (g_strcmp0 (va->locale, self->locale) == 0);
}

/* The voices of the system language: those of the index in its order, then installed ones it
 * does not name */
static void
load_voices (CcSpeechPanel *self)
{
    g_autoptr(JsonNode) index = read_json (INDEX_FILE);
    g_autoptr(GDir) dir = NULL;
    g_autoptr(GPtrArray) extra = g_ptr_array_new_with_free_func (g_free);
    const char *name;

    g_ptr_array_set_size (self->voices, 0);
    if (index && JSON_NODE_HOLDS_ARRAY (index)) {
        JsonArray *a = json_node_get_array (index);

        for (guint i = 0; i < json_array_get_length (a); i++) {
            JsonNode *n = json_array_get_element (a, i);
            JsonObject *o;
            Voice *v;

            if (!JSON_NODE_HOLDS_OBJECT (n))
                continue;
            o = json_node_get_object (n);
            if (g_strcmp0 (member_string (o, "language"), self->lang) != 0 ||
                !valid_id (member_string (o, "id")) || find_voice (self, member_string (o, "id")))
                continue;
            v = g_new0 (Voice, 1);
            v->id = g_strdup (member_string (o, "id"));
            v->name = g_strdup (member_string (o, "name") ? member_string (o, "name") : v->id);
            v->locale = g_strdup (member_string (o, "locale") ? member_string (o, "locale") : "");
            v->package = g_strdup (member_string (o, "package"));
            v->size = member_size (o, "size");
            v->is_default = member_true (o, "default");
            g_ptr_array_add (self->voices, v);
        }
    }

    dir = g_dir_open (VOICES_DIR, 0, NULL);
    while (dir && (name = g_dir_read_name (dir)))
        g_ptr_array_add (extra, g_strdup (name));
    g_ptr_array_sort_values (extra, (GCompareFunc) g_strcmp0);
    for (guint i = 0; i < extra->len; i++) {
        const char *id = g_ptr_array_index (extra, i);
        g_autofree char *path = voice_file (id);
        g_autoptr(JsonNode) node = read_json (path);
        JsonObject *o;
        Voice *v;

        if (!node || !JSON_NODE_HOLDS_OBJECT (node))
            continue;
        o = json_node_get_object (node);
        if (g_strcmp0 (member_string (o, "language"), self->lang) != 0 ||
            g_strcmp0 (member_string (o, "id"), id) != 0)
            continue;
        v = find_voice (self, id);
        if (v == NULL) {
            v = g_new0 (Voice, 1);
            v->id = g_strdup (id);
            v->name = g_strdup (member_string (o, "name") ? member_string (o, "name") : id);
            v->locale = g_strdup (member_string (o, "locale") ? member_string (o, "locale") : "");
            v->is_default = member_true (o, "default");
            g_ptr_array_add (self->voices, v);
        }
        v->installed = TRUE;
    }
    /* The system's locale first, otherwise as they came (a stable sort) */
    g_ptr_array_sort_with_data (self->voices, by_locale, self);
}

/* The first installed voice of the system's locale (or of any, locale NULL), a default one
 * only if want_default */
static Voice *
first_installed (CcSpeechPanel *self, const char *locale, gboolean want_default)
{
    for (guint i = 0; i < self->voices->len; i++) {
        Voice *v = g_ptr_array_index (self->voices, i);

        if (v->installed && (!want_default || v->is_default) && (!locale || g_str_equal (v->locale, locale)))
            return v;
    }
    return NULL;
}

/* The voice that speaks now, as plinius-speechd picks it: the chosen one if installed, else the
 * default voice of the system's locale, else any of that locale, else a default voice of the
 * language, else the first installed one */
static const char *
selected_voice (CcSpeechPanel *self)
{
    g_autoptr(GVariant) dict = g_settings_get_value (self->settings, "voices");
    const char *chosen = NULL;
    Voice *found = NULL;

    if (g_variant_lookup (dict, self->lang, "&s", &chosen)) {
        found = find_voice (self, chosen);
        if (found && !found->installed)
            found = NULL;
    }
    if (!found && *self->locale)
        found = first_installed (self, self->locale, TRUE);
    if (!found && *self->locale)
        found = first_installed (self, self->locale, FALSE);
    if (!found)
        found = first_installed (self, NULL, TRUE);
    if (!found)
        found = first_installed (self, NULL, FALSE);
    return found ? found->id : NULL;
}

/* Whether the language's voices are of more than one locale (then each one names its region) */
static gboolean
several_locales (CcSpeechPanel *self)
{
    for (guint i = 1; i < self->voices->len; i++) {
        Voice *a = g_ptr_array_index (self->voices, 0), *b = g_ptr_array_index (self->voices, i);

        if (!g_str_equal (a->locale, b->locale))
            return TRUE;
    }
    return FALSE;
}

static char *
voice_title (CcSpeechPanel *self, Voice *v)
{
    if (!*v->locale || !several_locales (self))
        return g_strdup (v->name);
    /* Translators: a voice's name and its region, e.g. "Joe (United States)" */
    return g_strdup_printf (P_("%1$s (%2$s)"), v->name, region_name (v->locale));
}

/* voices[lang] = id, the other languages' choices kept */
static void
choose_voice (CcSpeechPanel *self, const char *id)
{
    g_autoptr(GVariant) dict = g_settings_get_value (self->settings, "voices");
    GVariantBuilder b;
    GVariantIter iter;
    const char *lang, *voice;

    g_variant_builder_init (&b, G_VARIANT_TYPE ("a{ss}"));
    g_variant_iter_init (&iter, dict);
    while (g_variant_iter_next (&iter, "{&s&s}", &lang, &voice))
        if (!g_str_equal (lang, self->lang))
            g_variant_builder_add (&b, "{ss}", lang, voice);
    g_variant_builder_add (&b, "{ss}", self->lang, id);
    g_settings_set_value (self->settings, "voices", g_variant_builder_end (&b));
}

/* ----- Listening to a voice ----- */

static void
update_preview_buttons (CcSpeechPanel *self)
{
    for (guint i = 0; i < self->preview_buttons->len; i++) {
        GtkButton *button = g_ptr_array_index (self->preview_buttons, i);
        const char *id = g_object_get_data (G_OBJECT (button), "plinius-voice");
        gboolean playing = g_strcmp0 (id, self->playing_voice) == 0;

        gtk_button_set_icon_name (button, playing ? "media-playback-stop-symbolic" : "media-playback-start-symbolic");
        gtk_widget_set_tooltip_text (GTK_WIDGET (button), playing ? P_("Stop") : P_("Listen"));
    }
}

static void
playing_ended (CcSpeechPanel *self)
{
    g_clear_pointer (&self->playing_voice, g_free);
    self->playing_id = 0;
    self->generation++;
    g_array_set_size (self->finished_early, 0);
    update_preview_buttons (self);
}

static void
send_stop (CcSpeechPanel *self, guint32 id)
{
    g_dbus_connection_call (self->bus, SPEECH_BUS, SPEECH_PATH, SPEECH_IFACE, "Stop",
                            g_variant_new ("(u)", id), NULL, G_DBUS_CALL_FLAGS_NONE, -1, NULL, NULL, NULL);
}

/* Stops the sample that plays (or is about to): its Speak() answer, if still to come, is stale */
static void
stop_preview (CcSpeechPanel *self)
{
    if (self->playing_voice == NULL)
        return;
    if (self->playing_id != 0)
        send_stop (self, self->playing_id);
    playing_ended (self);
}

static void
on_finished (GDBusConnection *bus, const char *sender, const char *path, const char *iface,
             const char *signal, GVariant *params, gpointer user_data)
{
    CcSpeechPanel *self = user_data;
    guint32 id;
    gboolean completed;

    if (!g_variant_is_of_type (params, G_VARIANT_TYPE ("(ub)")) || self->playing_voice == NULL)
        return;
    g_variant_get (params, "(ub)", &id, &completed);
    if (self->playing_id == 0)
        g_array_append_val (self->finished_early, id);
    else if (id == self->playing_id)
        playing_ended (self);
}

static void
on_name_vanished (GDBusConnection *bus, const char *name, gpointer user_data)
{
    CcSpeechPanel *self = user_data;

    /* The service went away while a sample played (not while it is being started) */
    if (self->playing_voice && self->playing_id != 0)
        playing_ended (self);
}

typedef struct {
    CcSpeechPanel *self;
    guint generation;
    char *voice;
} SpeakCall;

static void
speak_done_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
    SpeakCall *call = user_data;
    g_autofree char *voice = call->voice;
    g_autoptr(GError) error = NULL;
    g_autoptr(GVariant) reply = NULL;
    CcSpeechPanel *self = call->self;
    guint generation = call->generation;
    guint32 id;

    g_free (call);
    reply = g_dbus_connection_call_finish (G_DBUS_CONNECTION (source), result, &error);
    if (g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
        return;
    if (reply == NULL) {
        g_warning ("Cannot let the voice %s speak: %s", voice, error->message);
        if (generation == self->generation)
            playing_ended (self);
        return;
    }
    g_variant_get (reply, "(u)", &id);
    if (generation != self->generation) {
        /* Stopped or another voice started meanwhile */
        send_stop (self, id);
        return;
    }
    for (guint i = 0; i < self->finished_early->len; i++) {
        if (g_array_index (self->finished_early, guint32, i) == id) {
            playing_ended (self);
            return;
        }
    }
    self->playing_id = id;
    g_array_set_size (self->finished_early, 0);
}

static gboolean
ensure_bus (CcSpeechPanel *self)
{
    g_autoptr(GError) error = NULL;

    if (self->bus)
        return TRUE;
    self->bus = g_bus_get_sync (G_BUS_TYPE_SESSION, NULL, &error);
    if (self->bus == NULL) {
        g_warning ("No session bus: %s", error->message);
        return FALSE;
    }
    self->finished_sub = g_dbus_connection_signal_subscribe (self->bus, NULL, SPEECH_IFACE, "Finished",
                                                             SPEECH_PATH, NULL, G_DBUS_SIGNAL_FLAGS_NONE,
                                                             on_finished, self, NULL);
    self->name_watch = g_bus_watch_name_on_connection (self->bus, SPEECH_BUS, G_BUS_NAME_WATCHER_FLAGS_NONE,
                                                       NULL, on_name_vanished, self, NULL);
    return TRUE;
}

static void
start_preview (CcSpeechPanel *self, const char *voice)
{
    GVariantBuilder options;
    SpeakCall *call;

    /* Translators: the sample sentence a voice speaks; take a typical name of your language */
    const char *sample = P_("Daniel Müller is calling");

    if (!ensure_bus (self))
        return;
    self->playing_voice = g_strdup (voice);
    self->playing_id = 0;
    self->generation++;
    update_preview_buttons (self);

    g_variant_builder_init (&options, G_VARIANT_TYPE_VARDICT);
    g_variant_builder_add (&options, "{sv}", "voice", g_variant_new_string (voice));
    call = g_new0 (SpeakCall, 1);
    call->self = self;
    call->generation = self->generation;
    call->voice = g_strdup (voice);
    g_dbus_connection_call (self->bus, SPEECH_BUS, SPEECH_PATH, SPEECH_IFACE, "Speak",
                            g_variant_new ("(sa{sv})", sample, &options),
                            G_VARIANT_TYPE ("(u)"), G_DBUS_CALL_FLAGS_NONE, -1, self->cancellable,
                            speak_done_cb, call);
}

static void
preview_clicked_cb (GtkButton *button, CcSpeechPanel *self)
{
    const char *voice = g_object_get_data (G_OBJECT (button), "plinius-voice");
    gboolean same = g_strcmp0 (voice, self->playing_voice) == 0;

    stop_preview (self);
    if (!same)
        start_preview (self, voice);
}

/* ----- Errors ----- */

/* An error the user should see, unless they just closed the authentication dialog */
static void
show_error (CcSpeechPanel *self, int status, const char *heading)
{
    const char *message;
    AdwDialog *alert;

    if (status == EXIT_PKEXEC_DISMISSED || status == EXIT_PKEXEC_DENIED)
        return;

    switch (status) {
    case EXIT_DB_NEWER:
    case EXIT_FILE_GONE:
        message = P_("Update the system in Software first, then try again.");
        break;
    case EXIT_BUSY:
        message = P_("Another software installation is running. Try again when it is done.");
        break;
    default:
        message = P_("Check the internet connection and try again.");
        break;
    }
    alert = adw_alert_dialog_new (heading, message);
    adw_alert_dialog_add_response (ADW_ALERT_DIALOG (alert), "close", P_("_Close"));
    adw_dialog_present (alert, GTK_WIDGET (self));
}

/* ----- Download progress ----- */

/* Size of the package's file in pacman's cache, complete or still downloading; a package whose
 * name only starts the same (…-thorsten-high for …-thorsten) does not count */
static guint64
cached_size (const char *package)
{
    g_autoptr(GDir) dir = g_dir_open (PACMAN_CACHE, 0, NULL);
    g_autofree char *prefix = g_strconcat (package, "-", NULL);
    const char *name;
    guint64 size = 0;

    if (dir == NULL)
        return 0;
    while ((name = g_dir_read_name (dir))) {
        g_autofree char *path = NULL;
        GStatBuf st;

        if (!g_str_has_prefix (name, prefix) || !g_ascii_isdigit (name[strlen (prefix)]) ||
            g_str_has_suffix (name, ".sig") || g_str_has_suffix (name, ".sig.part"))
            continue;
        path = g_build_filename (PACMAN_CACHE, name, NULL);
        if (g_stat (path, &st) == 0)
            size = MAX (size, (guint64) st.st_size);
    }
    return size;
}

static gboolean
poll_progress (gpointer data)
{
    CcSpeechPanel *self = data;
    guint64 size = cached_size (self->busy_package);

    /* Nothing in the cache yet: still waiting (below), not 0 bytes downloaded */
    if (self->total > 0 && size > 0 && size < self->total) {
        g_autofree char *done = g_format_size (size);
        g_autofree char *total = g_format_size (self->total);
        /* Translators: download progress, e.g. "120.5 MB of 487.2 MB" */
        g_autofree char *text = g_strdup_printf (P_("%s of %s"), done, total);

        adw_preferences_row_set_title (ADW_PREFERENCES_ROW (self->progress_row), P_("Downloading Voice…"));
        adw_action_row_set_subtitle (self->progress_row, text);
        gtk_progress_bar_set_fraction (self->bar, (double) size / self->total);
    } else if (size > 0) {
        /* Downloaded; pacman checks and unpacks it */
        adw_preferences_row_set_title (ADW_PREFERENCES_ROW (self->progress_row), P_("Installing Voice…"));
        adw_action_row_set_subtitle (self->progress_row, "");
        gtk_progress_bar_pulse (self->bar);
    } else {
        /* Waiting for the password or for pacman to start */
        gtk_progress_bar_pulse (self->bar);
    }
    return G_SOURCE_CONTINUE;
}

static gboolean
pulse (gpointer data)
{
    CcSpeechPanel *self = data;

    gtk_progress_bar_pulse (self->bar);
    return G_SOURCE_CONTINUE;
}

/* ----- Install and remove through plinius-pkg ----- */

typedef void (*PkgDone) (CcSpeechPanel *self, int status);

typedef struct {
    CcSpeechPanel *self;
    PkgDone done;
} PkgCall;

static void
pkg_done_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
    PkgCall *call = user_data;
    CcSpeechPanel *self = call->self;
    PkgDone done = call->done;
    g_autoptr(GError) error = NULL;
    g_autofree char *out = NULL;
    int status;

    g_free (call);
    if (!g_subprocess_communicate_utf8_finish (G_SUBPROCESS (source), result, &out, NULL, &error)) {
        if (g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
            return;
        status = 1;
    } else {
        status = g_subprocess_get_if_exited (G_SUBPROCESS (source)) ?
            g_subprocess_get_exit_status (G_SUBPROCESS (source)) : 1;
    }
    if (status != 0 && out && *g_strstrip (out))
        g_warning ("plinius-pkg ended with %d: %s", status, out);
    else if (status != 0)
        g_warning ("plinius-pkg ended with %d", status);

    self->busy = FALSE;
    g_clear_handle_id (&self->poll_id, g_source_remove);
    gtk_widget_set_visible (GTK_WIDGET (self->progress_row), FALSE);
    done (self, status);
    g_clear_pointer (&self->busy_package, g_free);
    g_clear_pointer (&self->busy_voice, g_free);
}

/* Runs "pkexec plinius-pkg COMMAND PACKAGE", then @done with its exit status */
static gboolean
run_pkg (CcSpeechPanel *self, const char *command, Voice *voice, PkgDone done)
{
    g_autoptr(GSubprocess) process = NULL;
    g_autoptr(GError) error = NULL;
    PkgCall *call;

    process = g_subprocess_new (G_SUBPROCESS_FLAGS_STDOUT_PIPE | G_SUBPROCESS_FLAGS_STDERR_MERGE, &error,
                                "pkexec", PKG_TOOL, command, voice->package, NULL);
    if (process == NULL) {
        g_warning ("Cannot run %s: %s", PKG_TOOL, error->message);
        done (self, 1);
        return FALSE;
    }
    self->busy = TRUE;
    g_free (self->busy_package);
    self->busy_package = g_strdup (voice->package);
    g_free (self->busy_voice);
    self->busy_voice = g_strdup (voice->id);
    stop_preview (self);
    rebuild_rows (self);
    gtk_widget_set_visible (GTK_WIDGET (self->progress_row), TRUE);
    call = g_new0 (PkgCall, 1);
    call->self = self;
    call->done = done;
    /* The output is only read so that a full pipe never stops plinius-pkg */
    g_subprocess_communicate_utf8_async (process, NULL, self->cancellable, pkg_done_cb, call);
    return TRUE;
}

static void
install_done (CcSpeechPanel *self, int status)
{
    g_autofree char *path = self->busy_voice ? voice_file (self->busy_voice) : NULL;

    load_voices (self);
    /* The new voice speaks from now on */
    if (status == 0 && path && g_file_test (path, G_FILE_TEST_EXISTS))
        choose_voice (self, self->busy_voice);
    rebuild_rows (self);
    if (status != 0)
        show_error (self, status, P_("Voice Not Installed"));
}

static void
start_download (CcSpeechPanel *self, Voice *voice)
{
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (self->progress_row), P_("Preparing Download…"));
    adw_action_row_set_subtitle (self->progress_row, "");
    gtk_progress_bar_set_fraction (self->bar, 0.0);
    if (run_pkg (self, "install", voice, install_done))
        self->poll_id = g_timeout_add (500, poll_progress, self);
}

static void
download_response_cb (AdwAlertDialog *alert, const char *response, CcSpeechPanel *self)
{
    const char *id = g_object_get_data (G_OBJECT (alert), "plinius-voice");
    Voice *voice = find_voice (self, id);

    if (g_strcmp0 (response, "download") == 0 && voice && !self->busy)
        start_download (self, voice);
}

/* "Download Size : 63.21 MiB" from pacman -Si, in bytes; 0 if not found */
static guint64
parse_download_size (const char *out)
{
    g_auto(GStrv) lines = g_strsplit (out, "\n", -1);

    for (char **l = lines; *l; l++) {
        const char *colon;
        char *end;
        double value;

        if (!g_str_has_prefix (*l, "Download Size") || !(colon = strchr (*l, ':')))
            continue;
        value = g_ascii_strtod (colon + 1, &end);
        while (*end == ' ')
            end++;
        if (g_str_has_prefix (end, "GiB"))
            value *= 1024.0 * 1024.0 * 1024.0;
        else if (g_str_has_prefix (end, "MiB"))
            value *= 1024.0 * 1024.0;
        else if (g_str_has_prefix (end, "KiB"))
            value *= 1024.0;
        return (guint64) value;
    }
    return 0;
}

typedef struct {
    CcSpeechPanel *self;
    char *voice;
} SizeCall;

static void
size_done_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
    SizeCall *call = user_data;
    CcSpeechPanel *self = call->self;
    g_autofree char *id = call->voice;
    g_autoptr(GError) error = NULL;
    g_autofree char *out = NULL;
    g_autofree char *size = NULL;
    g_autofree char *installed = NULL;
    g_autofree char *body = NULL;
    g_autofree char *title = NULL;
    AdwDialog *alert;
    Voice *voice;

    g_free (call);
    if (!g_subprocess_communicate_utf8_finish (G_SUBPROCESS (source), result, &out, NULL, &error))
        return;
    voice = find_voice (self, id);
    if (voice == NULL)
        return;

    self->total = out ? parse_download_size (out) : 0;
    if (self->total == 0) {
        /* Not in the package database the system was last updated with */
        alert = adw_alert_dialog_new (P_("Voice Not Available"),
                                      P_("Update the system in Software first, then try again."));
        adw_alert_dialog_add_response (ADW_ALERT_DIALOG (alert), "close", P_("_Close"));
        adw_dialog_present (alert, GTK_WIDGET (self));
        return;
    }

    size = g_format_size (self->total);
    installed = g_format_size (voice->size ? voice->size : self->total);
    /* Translators: %1$s is the voice's name, %2$s the download size, %3$s its size once
     * installed, e.g. "63.2 MB" */
    title = voice_title (self, voice);
    body = g_strdup_printf (P_("The voice %1$s is downloaded now (%2$s, %3$s once installed). "
                               "It speaks without an internet connection."),
                            title, size, installed);
    alert = adw_alert_dialog_new (P_("Download Voice?"), body);
    adw_alert_dialog_add_responses (ADW_ALERT_DIALOG (alert),
                                    "cancel", P_("_Cancel"),
                                    "download", P_("_Download"),
                                    NULL);
    adw_alert_dialog_set_response_appearance (ADW_ALERT_DIALOG (alert), "download", ADW_RESPONSE_SUGGESTED);
    adw_alert_dialog_set_close_response (ADW_ALERT_DIALOG (alert), "cancel");
    g_object_set_data_full (G_OBJECT (alert), "plinius-voice", g_strdup (id), g_free);
    g_signal_connect_object (alert, "response", G_CALLBACK (download_response_cb), self, 0);
    adw_dialog_present (alert, GTK_WIDGET (self));
}

/* Ask pacman how large the download is, then ask the user */
static void
ask_download (CcSpeechPanel *self, const char *id)
{
    g_autoptr(GSubprocessLauncher) launcher = NULL;
    g_autoptr(GSubprocess) process = NULL;
    Voice *voice = find_voice (self, id);
    SizeCall *call;

    if (voice == NULL || voice->package == NULL || self->busy)
        return;
    launcher = g_subprocess_launcher_new (G_SUBPROCESS_FLAGS_STDOUT_PIPE | G_SUBPROCESS_FLAGS_STDERR_SILENCE);
    /* English field names and units, whatever the system language */
    g_subprocess_launcher_setenv (launcher, "LC_ALL", "C", TRUE);
    process = g_subprocess_launcher_spawn (launcher, NULL, "pacman", "-Si", voice->package, NULL);
    if (process == NULL)
        return;
    call = g_new0 (SizeCall, 1);
    call->self = self;
    call->voice = g_strdup (id);
    g_subprocess_communicate_utf8_async (process, NULL, self->cancellable, size_done_cb, call);
}

static void
download_clicked_cb (GtkWidget *widget, CcSpeechPanel *self)
{
    ask_download (self, g_object_get_data (G_OBJECT (widget), "plinius-voice"));
}

static void
remove_done (CcSpeechPanel *self, int status)
{
    load_voices (self);
    rebuild_rows (self);
    if (status != 0)
        show_error (self, status, P_("Voice Not Removed"));
}

static void
remove_response_cb (AdwAlertDialog *alert, const char *response, CcSpeechPanel *self)
{
    Voice *voice = find_voice (self, g_object_get_data (G_OBJECT (alert), "plinius-voice"));

    if (g_strcmp0 (response, "remove") != 0 || voice == NULL || self->busy)
        return;
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (self->progress_row), P_("Removing Voice…"));
    adw_action_row_set_subtitle (self->progress_row, "");
    if (run_pkg (self, "remove", voice, remove_done))
        self->poll_id = g_timeout_add (500, pulse, self);
}

static void
remove_clicked_cb (GtkButton *button, CcSpeechPanel *self)
{
    const char *id = g_object_get_data (G_OBJECT (button), "plinius-voice");
    Voice *voice = find_voice (self, id);
    g_autofree char *size = NULL;
    g_autofree char *heading = NULL;
    g_autofree char *body = NULL;
    g_autofree char *title = NULL;
    AdwDialog *alert;

    if (voice == NULL)
        return;
    /* Translators: %s is the voice's name */
    title = voice_title (self, voice);
    heading = g_strdup_printf (P_("Remove the Voice %s?"), title);
    if (voice->size) {
        size = g_format_size (voice->size);
        /* Translators: %s is a size, e.g. "63.2 MB" */
        body = g_strdup_printf (P_("This frees %s. The voice can be downloaded again later."), size);
    } else {
        body = g_strdup (P_("The voice can be downloaded again later."));
    }
    alert = adw_alert_dialog_new (heading, body);
    adw_alert_dialog_add_responses (ADW_ALERT_DIALOG (alert),
                                    "cancel", P_("_Cancel"),
                                    "remove", P_("_Remove"),
                                    NULL);
    adw_alert_dialog_set_response_appearance (ADW_ALERT_DIALOG (alert), "remove", ADW_RESPONSE_DESTRUCTIVE);
    adw_alert_dialog_set_close_response (ADW_ALERT_DIALOG (alert), "cancel");
    g_object_set_data_full (G_OBJECT (alert), "plinius-voice", g_strdup (id), g_free);
    g_signal_connect_object (alert, "response", G_CALLBACK (remove_response_cb), self, 0);
    adw_dialog_present (alert, GTK_WIDGET (self));
}

/* ----- The voice rows ----- */

static void
voice_toggled_cb (GtkCheckButton *check, CcSpeechPanel *self)
{
    if (self->syncing || !gtk_check_button_get_active (check))
        return;
    choose_voice (self, g_object_get_data (G_OBJECT (check), "plinius-voice"));
}

static GtkWidget *
icon_button (const char *icon, const char *tooltip, const char *id)
{
    GtkWidget *button = gtk_button_new_from_icon_name (icon);

    gtk_widget_set_valign (button, GTK_ALIGN_CENTER);
    gtk_widget_add_css_class (button, "flat");
    gtk_widget_set_tooltip_text (button, tooltip);
    g_object_set_data_full (G_OBJECT (button), "plinius-voice", g_strdup (id), g_free);
    return button;
}

static GtkWidget *
installed_row (CcSpeechPanel *self, Voice *v, const char *selected, GtkCheckButton **group)
{
    GtkWidget *row = adw_action_row_new ();
    GtkWidget *check = gtk_check_button_new ();
    GtkWidget *listen = icon_button ("media-playback-start-symbolic", P_("Listen"), v->id);
    g_autofree char *title = voice_title (self, v);

    adw_preferences_row_set_use_markup (ADW_PREFERENCES_ROW (row), FALSE);
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), title);
    if (v->is_default)
        adw_action_row_set_subtitle (ADW_ACTION_ROW (row), P_("Standard voice"));
    if (*group)
        gtk_check_button_set_group (GTK_CHECK_BUTTON (check), *group);
    else
        *group = GTK_CHECK_BUTTON (check);
    gtk_check_button_set_active (GTK_CHECK_BUTTON (check), g_strcmp0 (v->id, selected) == 0);
    g_object_set_data_full (G_OBJECT (check), "plinius-voice", g_strdup (v->id), g_free);
    g_signal_connect (check, "toggled", G_CALLBACK (voice_toggled_cb), self);
    adw_action_row_add_prefix (ADW_ACTION_ROW (row), check);
    adw_action_row_set_activatable_widget (ADW_ACTION_ROW (row), check);

    g_signal_connect (listen, "clicked", G_CALLBACK (preview_clicked_cb), self);
    adw_action_row_add_suffix (ADW_ACTION_ROW (row), listen);
    g_ptr_array_add (self->preview_buttons, listen);

    /* The voice that speaks stays; one from no known package cannot be removed here */
    if (v->package && g_strcmp0 (v->id, selected) != 0) {
        GtkWidget *remove = icon_button ("user-trash-symbolic", P_("Remove"), v->id);

        g_signal_connect (remove, "clicked", G_CALLBACK (remove_clicked_cb), self);
        adw_action_row_add_suffix (ADW_ACTION_ROW (row), remove);
    }
    return row;
}

static GtkWidget *
available_row (CcSpeechPanel *self, Voice *v)
{
    GtkWidget *row = adw_action_row_new ();
    GtkWidget *download = icon_button ("folder-download-symbolic", P_("Download"), v->id);
    g_autofree char *title = voice_title (self, v);

    adw_preferences_row_set_use_markup (ADW_PREFERENCES_ROW (row), FALSE);
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), title);
    if (v->size) {
        g_autofree char *size = g_format_size (v->size);
        /* Translators: a voice that is not installed and its size, e.g. "Not installed, 63.2 MB" */
        g_autofree char *text = g_strdup_printf (P_("Not installed, %s"), size);

        adw_action_row_set_subtitle (ADW_ACTION_ROW (row), text);
    } else {
        adw_action_row_set_subtitle (ADW_ACTION_ROW (row), P_("Not installed"));
    }
    g_signal_connect (download, "clicked", G_CALLBACK (download_clicked_cb), self);
    adw_action_row_add_suffix (ADW_ACTION_ROW (row), download);
    g_object_set_data_full (G_OBJECT (row), "plinius-voice", g_strdup (v->id), g_free);
    gtk_list_box_row_set_activatable (GTK_LIST_BOX_ROW (row), TRUE);
    g_signal_connect (row, "activated", G_CALLBACK (download_clicked_cb), self);
    return row;
}

static void
rebuild_rows (CcSpeechPanel *self)
{
    const char *selected = selected_voice (self);
    GtkCheckButton *group = NULL;
    gboolean any = FALSE;
    g_autofree char *description = NULL;
    g_autofree char *escaped = NULL;

    for (guint i = 0; i < self->rows->len; i++)
        adw_preferences_group_remove (self->voice_group, g_ptr_array_index (self->rows, i));
    g_ptr_array_set_size (self->rows, 0);
    g_ptr_array_set_size (self->preview_buttons, 0);
    /* The progress row stays last */
    g_object_ref (self->progress_row);
    adw_preferences_group_remove (self->voice_group, GTK_WIDGET (self->progress_row));

    self->syncing = TRUE;
    for (guint i = 0; i < self->voices->len; i++) {
        Voice *v = g_ptr_array_index (self->voices, i);
        GtkWidget *row;

        if (!v->installed && v->package == NULL)
            continue;
        row = v->installed ? installed_row (self, v, selected, &group) : available_row (self, v);
        gtk_widget_set_sensitive (row, !self->busy);
        adw_preferences_group_add (self->voice_group, row);
        g_ptr_array_add (self->rows, row);
        any = TRUE;
    }
    self->syncing = FALSE;

    if (!any) {
        GtkWidget *row = adw_action_row_new ();

        adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_("No Voice Available"));
        adw_action_row_set_subtitle (ADW_ACTION_ROW (row),
                                     P_("There is no voice for this language yet, so the phone says nothing."));
        adw_preferences_group_add (self->voice_group, row);
        g_ptr_array_add (self->rows, row);
    }
    adw_preferences_group_add (self->voice_group, GTK_WIDGET (self->progress_row));
    g_object_unref (self->progress_row);

    /* Translators: %s is the language of the phone, e.g. "German" (Region & Language is the
     * name of GNOME's page where it is set) */
    description = g_strdup_printf (P_("Language: %s. The phone always speaks the language set in "
                                      "Region & Language."),
                                   language_name (self->lang));
    /* The description is markup */
    escaped = g_markup_escape_text (description, -1);
    adw_preferences_group_set_description (self->voice_group, escaped);
    update_preview_buttons (self);
}

static void
on_voices_changed (GSettings *settings, const char *key, CcSpeechPanel *self)
{
    rebuild_rows (self);
}

/* ----- The panel ----- */

static void
cc_speech_panel_dispose (GObject *object)
{
    CcSpeechPanel *self = CC_SPEECH_PANEL (object);

    if (self->bus)
        stop_preview (self);
    g_cancellable_cancel (self->cancellable);
    g_clear_object (&self->cancellable);
    g_clear_handle_id (&self->poll_id, g_source_remove);
    if (self->bus && self->finished_sub)
        g_dbus_connection_signal_unsubscribe (self->bus, self->finished_sub);
    self->finished_sub = 0;
    g_clear_handle_id (&self->name_watch, g_bus_unwatch_name);
    g_clear_object (&self->bus);
    if (self->settings)
        g_signal_handlers_disconnect_by_data (self->settings, self);
    g_clear_object (&self->settings);
    G_OBJECT_CLASS (cc_speech_panel_parent_class)->dispose (object);
}

static void
cc_speech_panel_finalize (GObject *object)
{
    CcSpeechPanel *self = CC_SPEECH_PANEL (object);

    g_free (self->lang);
    g_free (self->locale);
    g_free (self->busy_package);
    g_free (self->busy_voice);
    g_free (self->playing_voice);
    g_ptr_array_unref (self->voices);
    g_ptr_array_unref (self->rows);
    g_ptr_array_unref (self->preview_buttons);
    g_array_unref (self->finished_early);
    G_OBJECT_CLASS (cc_speech_panel_parent_class)->finalize (object);
}

static void
cc_speech_panel_class_init (CcSpeechPanelClass *klass)
{
    G_OBJECT_CLASS (klass)->dispose = cc_speech_panel_dispose;
    G_OBJECT_CLASS (klass)->finalize = cc_speech_panel_finalize;
}

static GtkWidget *
rate_row (CcSpeechPanel *self)
{
    GtkWidget *row = adw_preferences_row_new ();
    GtkWidget *box = gtk_box_new (GTK_ORIENTATION_HORIZONTAL, 12);
    GtkWidget *slower = gtk_label_new (P_("Slower"));
    GtkWidget *faster = gtk_label_new (P_("Faster"));
    GtkAdjustment *adjustment = gtk_adjustment_new (1.2, 0.5, 2.0, 0.05, 0.1, 0.0);
    GtkWidget *scale = gtk_scale_new (GTK_ORIENTATION_HORIZONTAL, adjustment);

    gtk_widget_set_margin_start (box, 12);
    gtk_widget_set_margin_end (box, 12);
    gtk_widget_set_margin_top (box, 6);
    gtk_widget_set_margin_bottom (box, 6);
    gtk_widget_add_css_class (slower, "dim-label");
    gtk_widget_add_css_class (faster, "dim-label");
    gtk_scale_set_draw_value (GTK_SCALE (scale), FALSE);
    gtk_scale_add_mark (GTK_SCALE (scale), 1.0, GTK_POS_BOTTOM, P_("Normal"));
    gtk_widget_set_hexpand (scale, TRUE);
    gtk_accessible_update_property (GTK_ACCESSIBLE (scale), GTK_ACCESSIBLE_PROPERTY_LABEL, P_("Speech Rate"), -1);
    gtk_box_append (GTK_BOX (box), slower);
    gtk_box_append (GTK_BOX (box), scale);
    gtk_box_append (GTK_BOX (box), faster);
    gtk_list_box_row_set_activatable (GTK_LIST_BOX_ROW (row), FALSE);
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_("Speech Rate"));
    gtk_list_box_row_set_child (GTK_LIST_BOX_ROW (row), box);
    g_settings_bind (self->settings, "rate", adjustment, "value", G_SETTINGS_BIND_DEFAULT);
    return row;
}

static void
cc_speech_panel_init (CcSpeechPanel *self)
{
    GtkWidget *toolbar = adw_toolbar_view_new ();
    GtkWidget *prefs = adw_preferences_page_new ();
    AdwPreferencesGroup *rate = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    AdwPreferencesGroup *calls = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    GtkWidget *announce = adw_switch_row_new ();
    GtkWidget *progress = adw_action_row_new ();
    GtkWidget *bar = gtk_progress_bar_new ();

    self->settings = g_settings_new (SPEECH_SCHEMA);
    self->cancellable = g_cancellable_new ();
    system_language (&self->lang, &self->locale);
    self->voices = g_ptr_array_new_with_free_func (voice_free);
    self->rows = g_ptr_array_new ();
    self->preview_buttons = g_ptr_array_new ();
    self->finished_early = g_array_new (FALSE, FALSE, sizeof (guint32));

    adw_navigation_page_set_title (ADW_NAVIGATION_PAGE (self), P_("Speech Output"));

    /* The voice of the system language */
    self->voice_group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_group_set_title (self->voice_group, P_("Voice"));
    gtk_widget_set_valign (bar, GTK_ALIGN_CENTER);
    gtk_widget_set_size_request (bar, 96, -1);
    gtk_progress_bar_set_pulse_step (GTK_PROGRESS_BAR (bar), 0.1);
    adw_action_row_add_suffix (ADW_ACTION_ROW (progress), bar);
    gtk_widget_set_visible (progress, FALSE);
    self->progress_row = ADW_ACTION_ROW (progress);
    self->bar = GTK_PROGRESS_BAR (bar);
    adw_preferences_group_add (self->voice_group, progress);
    load_voices (self);
    rebuild_rows (self);
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (prefs), self->voice_group);

    /* The rate, for everything the phone says */
    adw_preferences_group_set_title (rate, P_("Speech Rate"));
    adw_preferences_group_set_description (rate, P_("The voice and the speech rate apply to everything the "
                                                    "phone says."));
    adw_preferences_group_add (rate, rate_row (self));
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (prefs), rate);

    /* What the phone says by itself */
    adw_preferences_group_set_title (calls, P_("Announcements"));
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (announce), P_("Announce Callers"));
    adw_action_row_set_subtitle (ADW_ACTION_ROW (announce),
                                 P_("In a Bluetooth headset or headphones, the phone says who is calling after "
                                    "the first ring: the name from the contacts, or “Unknown caller”. Not in "
                                    "the car."));
    g_settings_bind (self->settings, "announce-callers", announce, "active", G_SETTINGS_BIND_DEFAULT);
    adw_preferences_group_add (calls, announce);
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (prefs), calls);

    g_signal_connect (self->settings, "changed::voices", G_CALLBACK (on_voices_changed), self);

    adw_toolbar_view_add_top_bar (ADW_TOOLBAR_VIEW (toolbar), adw_header_bar_new ());
    adw_toolbar_view_set_content (ADW_TOOLBAR_VIEW (toolbar), prefs);
    adw_navigation_page_set_child (ADW_NAVIGATION_PAGE (self), toolbar);
}

/* Listed with plinius-speech only (its settings) */
void
cc_speech_panel_static_init_func (void)
{
    CcApplication *application = CC_APPLICATION (g_application_get_default ());
    g_autoptr(GSettingsSchema) schema = NULL;
    GSettingsSchemaSource *source = g_settings_schema_source_get_default ();

    if (source)
        schema = g_settings_schema_source_lookup (source, SPEECH_SCHEMA, TRUE);
    cc_shell_model_set_panel_visibility (cc_application_get_model (application), "speech",
                                         schema && g_settings_schema_has_key (schema, "announce-callers") ?
                                         CC_PANEL_VISIBLE : CC_PANEL_HIDDEN);
}
