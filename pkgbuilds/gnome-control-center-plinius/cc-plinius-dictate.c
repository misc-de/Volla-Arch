/*
 * cc-plinius-dictate.c
 *
 * Plinius: "Speech to Text" on the Keyboard page, a group right below the input sources.
 * The switch is the setting org.plinius.dictate enabled of plinius-dictate: on, the on-screen
 * keyboard (stevia-plinius) shows a microphone key left of the space bar in normal text
 * fields; tapping it types what one says until it is tapped again. Recognition runs on the
 * phone (plinius-dictated, sherpa-onnx); audio is neither stored nor sent anywhere.
 *
 * Dictation never replaces the voice messages of apps: it only fills the text field.
 *
 * The speech model (plinius-dictate-models, about 640 MB) is not part of the image. Switching
 * dictation on without it first says how large the download is, then installs the package
 * through plinius-software's root helper (pkexec plinius-pkg install, as the Software app
 * does) and shows the progress. pacman runs with --noprogressbar there, so the progress comes
 * from the size of the package file growing in pacman's cache against the download size the
 * sync database names. While dictation is off, a row removes the model again.
 *
 * Nothing of this shows without plinius-dictate. Our own strings use the gettext domain
 * plinius-control-center (not GNOME's po/).
 *
 * SPDX-License-Identifier: MIT
 */

#undef G_LOG_DOMAIN
#define G_LOG_DOMAIN "cc-plinius-dictate"

#include "cc-plinius-dictate.h"

#include <glib/gstdio.h>
#include <string.h>

#define PLINIUS_DOMAIN "plinius-control-center"
#define P_(String) g_dgettext (PLINIUS_DOMAIN, String)

#define DICTATE_SCHEMA "org.plinius.dictate"
#define MODEL_PKG "plinius-dictate-models"
#define MODEL_FILE "/usr/share/plinius-dictate/parakeet-tdt-0.6b-v3-int8/encoder.int8.onnx"
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

/* Kept on the group; callbacks hold a reference to the group */
typedef struct {
    AdwPreferencesGroup *group;
    AdwSwitchRow *row;
    AdwActionRow *progress_row;
    GtkProgressBar *bar;
    AdwButtonRow *remove_row;
    GSettings *settings;
    GCancellable *cancellable;
    gboolean syncing;   /* the switch is set from the state, not by the user */
    gboolean busy;      /* plinius-pkg is installing or removing the model */
    guint64 total;      /* download size of the model package */
    guint poll_id;
} Dictate;

static void
dictate_free (gpointer data)
{
    Dictate *self = data;

    g_cancellable_cancel (self->cancellable);
    g_clear_object (&self->cancellable);
    g_clear_handle_id (&self->poll_id, g_source_remove);
    g_clear_object (&self->settings);
    g_free (self);
}

static gboolean
model_installed (void)
{
    return g_file_test (MODEL_FILE, G_FILE_TEST_EXISTS);
}

/* The switch and the rows as the setting and the installed model say */
static void
sync_state (Dictate *self)
{
    gboolean installed = model_installed ();
    gboolean enabled = g_settings_get_boolean (self->settings, "enabled");

    self->syncing = TRUE;
    adw_switch_row_set_active (self->row, enabled && installed);
    self->syncing = FALSE;
    gtk_widget_set_sensitive (GTK_WIDGET (self->row), !self->busy);
    gtk_widget_set_visible (GTK_WIDGET (self->progress_row), self->busy);
    gtk_widget_set_visible (GTK_WIDGET (self->remove_row), installed && !enabled && !self->busy);
}

/* An error the user should see, unless they just closed the authentication dialog */
static void
show_error (Dictate *self, int status, const char *heading)
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
    adw_dialog_present (alert, GTK_WIDGET (self->group));
}

/* ----- Download progress ----- */

/* Size of the model package file in pacman's cache, complete or still downloading */
static guint64
cached_size (void)
{
    g_autoptr(GDir) dir = g_dir_open (PACMAN_CACHE, 0, NULL);
    const char *name;
    guint64 size = 0;

    if (dir == NULL)
        return 0;
    while ((name = g_dir_read_name (dir))) {
        g_autofree char *path = NULL;
        GStatBuf st;

        if (!g_str_has_prefix (name, MODEL_PKG "-") || g_str_has_suffix (name, ".sig") ||
            g_str_has_suffix (name, ".sig.part"))
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
    Dictate *self = data;
    guint64 size = cached_size ();

    /* Nothing in the cache yet: still waiting (below), not 0 bytes downloaded */
    if (self->total > 0 && size > 0 && size < self->total) {
        g_autofree char *done = g_format_size (size);
        g_autofree char *total = g_format_size (self->total);
        /* Translators: download progress, e.g. "120.5 MB of 487.2 MB" */
        g_autofree char *text = g_strdup_printf (P_("%s of %s"), done, total);

        adw_preferences_row_set_title (ADW_PREFERENCES_ROW (self->progress_row),
                                       P_("Downloading Speech Model…"));
        adw_action_row_set_subtitle (self->progress_row, text);
        gtk_progress_bar_set_fraction (self->bar, (double) size / self->total);
    } else if (size > 0) {
        /* Downloaded; pacman checks and unpacks it */
        adw_preferences_row_set_title (ADW_PREFERENCES_ROW (self->progress_row),
                                       P_("Installing Speech Model…"));
        adw_action_row_set_subtitle (self->progress_row, "");
        gtk_progress_bar_pulse (self->bar);
    } else {
        /* Waiting for the password or for pacman to start */
        gtk_progress_bar_pulse (self->bar);
    }
    return G_SOURCE_CONTINUE;
}

/* ----- Install and remove through plinius-pkg ----- */

typedef void (*PkgDone) (Dictate *self, int status);

typedef struct {
    AdwPreferencesGroup *group;
    PkgDone done;
} PkgCall;

static void
pkg_done_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
    PkgCall *call = user_data;
    g_autoptr(AdwPreferencesGroup) group = call->group;
    PkgDone done = call->done;
    g_autoptr(GError) error = NULL;
    g_autofree char *out = NULL;
    Dictate *self;
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
    else if (status != 0 && out)
        g_warning ("plinius-pkg ended with %d", status);

    self = g_object_get_data (G_OBJECT (group), "plinius-dictate");
    if (self == NULL)
        return;
    self->busy = FALSE;
    g_clear_handle_id (&self->poll_id, g_source_remove);
    done (self, status);
}

/* Runs "pkexec plinius-pkg COMMAND plinius-dictate-models", then @done with its exit status */
static void
run_pkg (Dictate *self, const char *command, PkgDone done)
{
    g_autoptr(GSubprocess) process = NULL;
    g_autoptr(GError) error = NULL;
    PkgCall *call;

    process = g_subprocess_new (G_SUBPROCESS_FLAGS_STDOUT_PIPE | G_SUBPROCESS_FLAGS_STDERR_MERGE, &error,
                                "pkexec", PKG_TOOL, command, MODEL_PKG, NULL);
    if (process == NULL) {
        g_warning ("Cannot run %s: %s", PKG_TOOL, error->message);
        done (self, 1);
        return;
    }
    self->busy = TRUE;
    sync_state (self);
    call = g_new0 (PkgCall, 1);
    call->group = g_object_ref (self->group);
    call->done = done;
    /* The output is only read so that a full pipe never stops plinius-pkg */
    g_subprocess_communicate_utf8_async (process, NULL, self->cancellable, pkg_done_cb, call);
}

static void
install_done (Dictate *self, int status)
{
    if (status == 0 && model_installed ())
        g_settings_set_boolean (self->settings, "enabled", TRUE);
    sync_state (self);
    if (status != 0)
        show_error (self, status, P_("Speech Model Not Installed"));
}

static void
start_download (Dictate *self)
{
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (self->progress_row), P_("Preparing Download…"));
    adw_action_row_set_subtitle (self->progress_row, "");
    gtk_progress_bar_set_fraction (self->bar, 0.0);
    run_pkg (self, "install", install_done);
    if (self->busy)
        self->poll_id = g_timeout_add (500, poll_progress, self);
}

static void
download_response_cb (AdwAlertDialog *alert, const char *response, Dictate *self)
{
    if (g_strcmp0 (response, "download") == 0)
        start_download (self);
}

/* "Download Size : 487.21 MiB" from pacman -Si, in bytes; 0 if not found */
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

static void
size_done_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(AdwPreferencesGroup) group = user_data;
    Dictate *self = g_object_get_data (G_OBJECT (group), "plinius-dictate");
    g_autofree char *out = NULL;
    g_autofree char *size = NULL;
    g_autofree char *body = NULL;
    AdwDialog *alert;

    if (!g_subprocess_communicate_utf8_finish (G_SUBPROCESS (source), result, &out, NULL, NULL) || self == NULL)
        return;

    self->total = out ? parse_download_size (out) : 0;
    if (self->total == 0) {
        /* Not in the package database the system was last updated with */
        alert = adw_alert_dialog_new (P_("Speech Model Not Available"),
                                      P_("Update the system in Software first, then try again."));
        adw_alert_dialog_add_response (ADW_ALERT_DIALOG (alert), "close", P_("_Close"));
        adw_dialog_present (alert, GTK_WIDGET (self->group));
        return;
    }

    size = g_format_size (self->total);
    /* Translators: %s is the download size, e.g. "487.2 MB" */
    body = g_strdup_printf (P_("Speech to text needs a speech model that is downloaded now (%s, "
                               "about 640 MB once installed). Wi-Fi is best for this. The model "
                               "recognizes 25 languages and works without an internet connection."),
                            size);
    alert = adw_alert_dialog_new (P_("Download Speech Model?"), body);
    adw_alert_dialog_add_responses (ADW_ALERT_DIALOG (alert),
                                    "cancel", P_("_Cancel"),
                                    "download", P_("_Download"),
                                    NULL);
    adw_alert_dialog_set_response_appearance (ADW_ALERT_DIALOG (alert), "download", ADW_RESPONSE_SUGGESTED);
    adw_alert_dialog_set_close_response (ADW_ALERT_DIALOG (alert), "cancel");
    g_signal_connect (alert, "response", G_CALLBACK (download_response_cb), self);
    adw_dialog_present (alert, GTK_WIDGET (self->group));
}

/* Ask pacman how large the download is, then ask the user */
static void
ask_download (Dictate *self)
{
    g_autoptr(GSubprocessLauncher) launcher = NULL;
    g_autoptr(GSubprocess) process = NULL;

    launcher = g_subprocess_launcher_new (G_SUBPROCESS_FLAGS_STDOUT_PIPE | G_SUBPROCESS_FLAGS_STDERR_SILENCE);
    /* English field names and units, whatever the system language */
    g_subprocess_launcher_setenv (launcher, "LC_ALL", "C", TRUE);
    process = g_subprocess_launcher_spawn (launcher, NULL, "pacman", "-Si", MODEL_PKG, NULL);
    if (process)
        g_subprocess_communicate_utf8_async (process, NULL, self->cancellable, size_done_cb,
                                             g_object_ref (self->group));
}

static void
remove_done (Dictate *self, int status)
{
    sync_state (self);
    if (status != 0)
        show_error (self, status, P_("Speech Model Not Removed"));
}

static gboolean
pulse (gpointer data)
{
    Dictate *self = data;

    gtk_progress_bar_pulse (self->bar);
    return G_SOURCE_CONTINUE;
}

static void
remove_response_cb (AdwAlertDialog *alert, const char *response, Dictate *self)
{
    if (g_strcmp0 (response, "remove") != 0)
        return;
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (self->progress_row), P_("Removing Speech Model…"));
    adw_action_row_set_subtitle (self->progress_row, "");
    run_pkg (self, "remove", remove_done);
    if (self->busy)
        self->poll_id = g_timeout_add (500, pulse, self);
}

static void
remove_activated_cb (AdwButtonRow *row, Dictate *self)
{
    AdwDialog *alert;

    alert = adw_alert_dialog_new (P_("Remove Speech Model?"),
                                  P_("This frees about 640 MB. Switching speech to text on again downloads "
                                     "the model again."));
    adw_alert_dialog_add_responses (ADW_ALERT_DIALOG (alert),
                                    "cancel", P_("_Cancel"),
                                    "remove", P_("_Remove"),
                                    NULL);
    adw_alert_dialog_set_response_appearance (ADW_ALERT_DIALOG (alert), "remove", ADW_RESPONSE_DESTRUCTIVE);
    adw_alert_dialog_set_close_response (ADW_ALERT_DIALOG (alert), "cancel");
    g_signal_connect (alert, "response", G_CALLBACK (remove_response_cb), self);
    adw_dialog_present (alert, GTK_WIDGET (self->group));
}

/* ----- The switch ----- */

static void
on_active_changed (AdwSwitchRow *row, GParamSpec *pspec, Dictate *self)
{
    if (self->syncing)
        return;

    if (!adw_switch_row_get_active (row)) {
        g_settings_set_boolean (self->settings, "enabled", FALSE);
        return;
    }
    if (model_installed ()) {
        g_settings_set_boolean (self->settings, "enabled", TRUE);
        return;
    }
    /* No model yet: the switch stays off until it is installed */
    sync_state (self);
    ask_download (self);
}

static void
on_settings_changed (GSettings *settings, const char *key, Dictate *self)
{
    sync_state (self);
}

/**
 * cc_plinius_dictate_setup:
 * @page: the Keyboard page
 *
 * Adds the "Speech to Text" group right below the input sources. Returns %FALSE without
 * plinius-dictate.
 */
gboolean
cc_plinius_dictate_setup (AdwPreferencesPage *page)
{
    GSettingsSchemaSource *source = g_settings_schema_source_get_default ();
    g_autoptr(GSettingsSchema) schema = NULL;
    GtkWidget *row, *progress_row, *bar, *remove_row;
    AdwPreferencesGroup *group;
    Dictate *self;

    if (source)
        schema = g_settings_schema_source_lookup (source, DICTATE_SCHEMA, TRUE);
    if (schema == NULL)
        return FALSE;

    group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_group_set_title (group, P_("Speech to Text"));
    adw_preferences_page_insert (page, group, 1);

    row = adw_switch_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_("Microphone Key"));
    adw_action_row_set_subtitle (ADW_ACTION_ROW (row),
                                 P_("A microphone key next to the space bar of the on-screen keyboard types "
                                    "what you say into the text field. Speech is recognized on the phone; "
                                    "nothing is stored or sent. Needs about 1 GB of memory while you dictate."));
    adw_preferences_group_add (group, row);

    /* Download or removal of the model: a bar below the title */
    progress_row = adw_action_row_new ();
    bar = gtk_progress_bar_new ();
    gtk_widget_set_valign (bar, GTK_ALIGN_CENTER);
    gtk_widget_set_size_request (bar, 96, -1);
    gtk_progress_bar_set_pulse_step (GTK_PROGRESS_BAR (bar), 0.1);
    adw_action_row_add_suffix (ADW_ACTION_ROW (progress_row), bar);
    gtk_widget_set_visible (progress_row, FALSE);
    adw_preferences_group_add (group, progress_row);

    remove_row = adw_button_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (remove_row), P_("Remove Speech Model"));
    gtk_widget_add_css_class (remove_row, "destructive-action");
    adw_preferences_group_add (group, remove_row);

    self = g_new0 (Dictate, 1);
    self->group = group;
    self->row = ADW_SWITCH_ROW (row);
    self->progress_row = ADW_ACTION_ROW (progress_row);
    self->bar = GTK_PROGRESS_BAR (bar);
    self->remove_row = ADW_BUTTON_ROW (remove_row);
    self->settings = g_settings_new (DICTATE_SCHEMA);
    self->cancellable = g_cancellable_new ();
    g_object_set_data_full (G_OBJECT (group), "plinius-dictate", self, dictate_free);

    sync_state (self);
    g_signal_connect (row, "notify::active", G_CALLBACK (on_active_changed), self);
    g_signal_connect (remove_row, "activated", G_CALLBACK (remove_activated_cb), self);
    g_signal_connect (self->settings, "changed::enabled", G_CALLBACK (on_settings_changed), self);
    return TRUE;
}
