/*
 * cc-plinius-pinguard.c
 *
 * Plinius: "Wait After Wrong PINs" on the Screen Lock page, a group right below GNOME's lock
 * options. plinius-pinguard checks the PIN for the lock screen, sudo and polkit; with this switch
 * on, every wrong PIN after the first five makes the next try wait longer (30 s up to 1 h).
 * It is off unless switched on here.
 *
 * The switch runs "pkexec plinius-pinguard-set on|off". polkit lets "on" through without a
 * password (it only makes the phone safer) and asks the user's own PIN for "off"
 * (org.plinius.pinguard.policy). The switch shows the daemon's marker file, so it tells what the
 * daemon does, also after a dismissed prompt.
 *
 * Nothing of this shows without plinius-pinguard. Our own strings use the gettext domain
 * plinius-control-center (not GNOME's po/).
 *
 * SPDX-License-Identifier: MIT
 */

#undef G_LOG_DOMAIN
#define G_LOG_DOMAIN "cc-plinius-pinguard"

#include "cc-plinius-pinguard.h"

#define PLINIUS_DOMAIN "plinius-control-center"
#define P_(String) g_dgettext (PLINIUS_DOMAIN, String)

/* See plinius-pinguard.h and plinius-pinguard-set in the plinius-pinguard package */
#define SET_TOOL "/usr/lib/plinius-pinguard/plinius-pinguard-set"
#define MARKER   "/etc/plinius-pinguard/enabled"

/* Exit codes of pkexec: prompt dismissed, not authorized */
#define PKEXEC_DISMISSED 126
#define PKEXEC_NOT_AUTHORIZED 127

/* Kept on the switch row; the row is referenced by a running pkexec call */
typedef struct {
    AdwSwitchRow *row;
    gboolean syncing; /* the row is set from the marker, not by the user */
} Pinguard;

/* The switch as the marker says, without running the tool again */
static void
sync_from_marker (Pinguard *self)
{
    self->syncing = TRUE;
    adw_switch_row_set_active (self->row, g_file_test (MARKER, G_FILE_TEST_EXISTS));
    self->syncing = FALSE;
}

static gboolean
sync_idle_cb (gpointer data)
{
    Pinguard *self = g_object_get_data (G_OBJECT (data), "plinius-pinguard");

    if (self)
        sync_from_marker (self);
    return G_SOURCE_REMOVE;
}

static void
set_done_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(AdwSwitchRow) row = user_data;
    Pinguard *self = g_object_get_data (G_OBJECT (row), "plinius-pinguard");
    g_autoptr(GError) error = NULL;

    if (g_subprocess_wait_finish (G_SUBPROCESS (source), result, &error)) {
        int status = g_subprocess_get_if_exited (G_SUBPROCESS (source)) ?
                         g_subprocess_get_exit_status (G_SUBPROCESS (source)) : -1;

        /* A dismissed or refused PIN prompt is the user's answer, not a failure */
        if (status != 0 && status != PKEXEC_DISMISSED && status != PKEXEC_NOT_AUTHORIZED)
            g_warning ("%s exited with %d", SET_TOOL, status);
    } else {
        g_warning ("%s failed: %s", SET_TOOL, error->message);
    }

    /* Also on failure: the switch goes back to what is true */
    sync_from_marker (self);
    gtk_widget_set_sensitive (GTK_WIDGET (row), TRUE);
}

static void
on_active_changed (AdwSwitchRow *row, GParamSpec *pspec, Pinguard *self)
{
    g_autoptr(GSubprocess) process = NULL;
    g_autoptr(GError) error = NULL;

    if (self->syncing)
        return;
    process = g_subprocess_new (G_SUBPROCESS_FLAGS_STDOUT_SILENCE | G_SUBPROCESS_FLAGS_STDERR_SILENCE, &error,
                                "pkexec", SET_TOOL, adw_switch_row_get_active (row) ? "on" : "off", NULL);
    if (process == NULL) {
        g_warning ("Cannot run pkexec %s: %s", SET_TOOL, error->message);
        /* Not inside this handler: set back while GTK still hands on the user's change, the row
         * notifies again and the handler would try pkexec once more */
        g_idle_add_full (G_PRIORITY_DEFAULT_IDLE, sync_idle_cb, g_object_ref (row), g_object_unref);
        return;
    }
    /* One change at a time, until pkexec (and its PIN prompt) is done */
    gtk_widget_set_sensitive (GTK_WIDGET (row), FALSE);
    g_subprocess_wait_async (process, NULL, set_done_cb, g_object_ref (row));
}

/**
 * cc_plinius_pinguard_setup:
 * @page: the Screen Lock page
 *
 * Adds the group with the switch right below the page's first group (GNOME's lock options).
 * Returns %FALSE without plinius-pinguard.
 */
gboolean
cc_plinius_pinguard_setup (AdwPreferencesPage *page)
{
    AdwPreferencesGroup *group;
    GtkWidget *row;
    Pinguard *self;

    if (!g_file_test (SET_TOOL, G_FILE_TEST_IS_EXECUTABLE))
        return FALSE;

    group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_page_add (page, group);

    row = adw_switch_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_("Wait After Wrong PINs"));
    adw_action_row_set_subtitle (ADW_ACTION_ROW (row),
                                 P_("After 5 wrong PINs, every further try has to wait longer: 30 seconds, "
                                    "1 minute, 5 and 15 minutes, then 1 hour."));
    adw_preferences_group_add (group, row);

    self = g_new0 (Pinguard, 1);
    self->row = ADW_SWITCH_ROW (row);
    g_object_set_data_full (G_OBJECT (row), "plinius-pinguard", self, g_free);

    sync_from_marker (self);
    g_signal_connect (row, "notify::active", G_CALLBACK (on_active_changed), self);
    return TRUE;
}
