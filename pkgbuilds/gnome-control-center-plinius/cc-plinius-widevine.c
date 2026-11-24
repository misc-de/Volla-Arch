/*
 * cc-plinius-widevine.c
 *
 * Plinius: "Protected Content" on the Privacy panel, a group below Devices.
 * Streaming services (Netflix, Prime Video) need Widevine, Google's closed-source DRM module.
 * It may not be redistributed, so plinius-widevine fetches it from Google onto the phone and
 * asks Google once a day for a newer one, but only while this switch is on. It is off unless
 * switched on here.
 *
 * Switching on asks first (what Widevine is, that Google is asked daily) and then runs
 * "pkexec plinius-widevine-set on", which waits for the download; polkit asks for the
 * administrator password. "off" removes the module, without a password
 * (org.plinius.widevine.policy). The switch shows the marker file and the subtitle the
 * installed version, so both tell what is true, also after a dismissed prompt or a failed
 * download (the service tries again later then).
 *
 * Nothing of this shows without plinius-widevine. Our own strings use the gettext domain
 * plinius-control-center (not GNOME's po/).
 *
 * SPDX-License-Identifier: MIT
 */

#undef G_LOG_DOMAIN
#define G_LOG_DOMAIN "cc-plinius-widevine"

#include "cc-plinius-widevine.h"

#define PLINIUS_DOMAIN "plinius-control-center"
#define P_(String) g_dgettext (PLINIUS_DOMAIN, String)

/* See plinius-widevine-set and plinius-widevine-update in the plinius-widevine package */
#define SET_TOOL "/usr/lib/plinius-widevine/plinius-widevine-set"
#define MARKER   "/etc/plinius-widevine/enabled"
#define CURRENT  "/var/lib/plinius-widevine/current"

/* Exit codes: pkexec's prompt dismissed / not authorized, plinius-widevine-set's failed download */
#define PKEXEC_DISMISSED 126
#define PKEXEC_NOT_AUTHORIZED 127
#define SET_DOWNLOAD_FAILED 3

/* Kept on the switch row; the row is referenced by a running pkexec call or dialog */
typedef struct {
    AdwSwitchRow *row;
    GtkWidget *spinner;
    gboolean syncing; /* the row is set from the marker, not by the user */
} Widevine;

/* The installed version: the link "current" points to versions/<version> */
static char *
installed_version (void)
{
    g_autofree char *target = g_file_read_link (CURRENT, NULL);

    if (target == NULL)
        return NULL;
    return g_path_get_basename (target);
}

/* The switch as the marker says, the subtitle what is installed */
static void
sync_state (Widevine *self)
{
    gboolean on = g_file_test (MARKER, G_FILE_TEST_EXISTS);
    g_autofree char *version = on ? installed_version () : NULL;
    g_autofree char *subtitle = NULL;

    self->syncing = TRUE;
    adw_switch_row_set_active (self->row, on);
    self->syncing = FALSE;

    if (!on)
        subtitle = g_strdup (P_("For streaming services such as Netflix and Prime Video in Firefox"));
    else if (version)
        /* Translators: {version} is the Widevine version, e.g. 4.10.3112.0 */
        subtitle = g_strdup (P_("Widevine {version} from Google"));
    else
        subtitle = g_strdup (P_("Not downloaded yet, tries again later"));
    if (version) {
        g_autoptr(GString) text = g_string_new (subtitle);

        g_string_replace (text, "{version}", version, 0);
        g_free (subtitle);
        subtitle = g_string_free (g_steal_pointer (&text), FALSE);
    }
    adw_action_row_set_subtitle (ADW_ACTION_ROW (self->row), subtitle);
}

static void
busy (Widevine *self, gboolean on)
{
    gtk_widget_set_sensitive (GTK_WIDGET (self->row), !on);
    gtk_widget_set_visible (self->spinner, on);
    if (on)
        adw_action_row_set_subtitle (ADW_ACTION_ROW (self->row),
                                     adw_switch_row_get_active (self->row) ? P_("Downloading from Google…")
                                                                           : P_("Removing…"));
}

static void
set_done_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(AdwSwitchRow) row = user_data;
    Widevine *self = g_object_get_data (G_OBJECT (row), "plinius-widevine");
    g_autoptr(GError) error = NULL;

    if (g_subprocess_wait_finish (G_SUBPROCESS (source), result, &error)) {
        int status = g_subprocess_get_if_exited (G_SUBPROCESS (source)) ?
                         g_subprocess_get_exit_status (G_SUBPROCESS (source)) : -1;

        /* A dismissed prompt is the user's answer; a failed download shows in the subtitle */
        if (status != 0 && status != PKEXEC_DISMISSED && status != PKEXEC_NOT_AUTHORIZED &&
            status != SET_DOWNLOAD_FAILED)
            g_warning ("%s exited with %d", SET_TOOL, status);
    } else {
        g_warning ("%s failed: %s", SET_TOOL, error->message);
    }

    busy (self, FALSE);
    /* Also on failure: the switch goes back to what is true */
    sync_state (self);
}

static void
run_set (Widevine *self, gboolean on)
{
    g_autoptr(GSubprocess) process = NULL;
    g_autoptr(GError) error = NULL;

    process = g_subprocess_new (G_SUBPROCESS_FLAGS_STDOUT_SILENCE | G_SUBPROCESS_FLAGS_STDERR_SILENCE, &error,
                                "pkexec", SET_TOOL, on ? "on" : "off", NULL);
    if (process == NULL) {
        g_warning ("Cannot run pkexec %s: %s", SET_TOOL, error->message);
        sync_state (self);
        return;
    }
    /* One change at a time, until pkexec (its prompt, the download) is done */
    busy (self, TRUE);
    g_subprocess_wait_async (process, NULL, set_done_cb, g_object_ref (self->row));
}

static void
response_cb (AdwAlertDialog *dialog, const char *response, gpointer user_data)
{
    g_autoptr(AdwSwitchRow) row = user_data;
    Widevine *self = g_object_get_data (G_OBJECT (row), "plinius-widevine");

    if (g_strcmp0 (response, "enable") == 0)
        run_set (self, TRUE);
    else
        sync_state (self);
}

static void
on_active_changed (AdwSwitchRow *row, GParamSpec *pspec, Widevine *self)
{
    AdwDialog *dialog;

    if (self->syncing)
        return;
    if (!adw_switch_row_get_active (row)) {
        run_set (self, FALSE);
        return;
    }

    dialog = adw_alert_dialog_new (P_("Turn On Protected Content?"),
                                   P_("Streaming services such as Netflix and Prime Video only play with Widevine, "
                                      "a closed-source module from Google. It may not be passed on, so the phone "
                                      "downloads it from Google itself (about 150 MB).\n\nOn Linux, services play at most in HD (720p). Firefox uses the "
                                      "module after a restart."));
    adw_alert_dialog_add_response (ADW_ALERT_DIALOG (dialog), "cancel", P_("Cancel"));
    adw_alert_dialog_add_response (ADW_ALERT_DIALOG (dialog), "enable", P_("Turn On"));
    adw_alert_dialog_set_response_appearance (ADW_ALERT_DIALOG (dialog), "enable", ADW_RESPONSE_SUGGESTED);
    adw_alert_dialog_set_close_response (ADW_ALERT_DIALOG (dialog), "cancel");
    g_signal_connect (dialog, "response", G_CALLBACK (response_cb), g_object_ref (row));
    adw_dialog_present (dialog, GTK_WIDGET (row));
}

/**
 * cc_plinius_widevine_add:
 * @page: the page of the Privacy panel
 *
 * Adds the group "Protected Content" at the end of @page; nothing without plinius-widevine.
 */
void
cc_plinius_widevine_add (AdwPreferencesPage *page)
{
    AdwPreferencesGroup *group;
    GtkWidget *row;
    Widevine *self;

    if (!g_file_test (SET_TOOL, G_FILE_TEST_IS_EXECUTABLE))
        return;

    group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_group_set_title (group, P_("Protected Content"));
    adw_preferences_page_add (page, group);

    row = adw_switch_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_("Play DRM Videos"));
    adw_preferences_group_add (group, row);

    self = g_new0 (Widevine, 1);
    self->row = ADW_SWITCH_ROW (row);
    self->spinner = adw_spinner_new ();
    gtk_widget_set_visible (self->spinner, FALSE);
    adw_action_row_add_suffix (ADW_ACTION_ROW (row), self->spinner);
    g_object_set_data_full (G_OBJECT (row), "plinius-widevine", self, g_free);

    sync_state (self);
    g_signal_connect (row, "notify::active", G_CALLBACK (on_active_changed), self);
}
