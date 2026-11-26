/*
 * cc-plinius-epo.c
 *
 * Plinius: "Faster GPS Fix" on the Location page, a group right below the location switch.
 * The switch runs "pkexec plinius-epo-set on|off" (plinius-epo package, no password): on,
 * plinius-epo.service downloads orbit predictions (EPO) for MediaTek's GNSS daemon every 6 hours,
 * so the GPS has its first position in seconds instead of about a minute; off, the files are
 * removed and mnld runs without them. The download carries nothing of the phone.
 *
 * The switch shows enabled= of /etc/plinius-epo.conf (default yes, as plinius-epo reads it), the
 * row below the time of the last download (/var/lib/plinius-epo/last, seconds since the epoch),
 * followed with a file monitor: the service writes it a moment after the switch went on.
 *
 * Nothing of this shows without the package. Our own strings use the gettext domain
 * plinius-control-center (not GNOME's po/).
 *
 * SPDX-License-Identifier: MIT
 */

#undef G_LOG_DOMAIN
#define G_LOG_DOMAIN "cc-plinius-epo"

#include "cc-plinius-epo.h"

#define PLINIUS_DOMAIN "plinius-control-center"
#define P_(String) g_dgettext (PLINIUS_DOMAIN, String)

/* See plinius-epo and plinius-epo-set in the plinius-epo package */
#define SET_TOOL "/usr/lib/plinius-epo/plinius-epo-set"
#define CONF     "/etc/plinius-epo.conf"
#define STAMP    "/var/lib/plinius-epo/last"

/* Kept on the switch row; the row is referenced by a running pkexec call */
typedef struct {
    AdwSwitchRow *row;
    AdwActionRow *last_row;
    GtkLabel *last;
    GFileMonitor *monitor; /* on the stamp's directory, the file is replaced, not written */
    gboolean syncing;      /* the row is set from the file, not by the user */
} PliniusEpo;

static void
epo_free (gpointer data)
{
    PliniusEpo *self = data;

    if (self->monitor)
        g_file_monitor_cancel (self->monitor);
    g_clear_object (&self->monitor);
    g_free (self);
}

/* enabled= of the config file: the last line counts, missing file or key = on */
static gboolean
read_enabled (void)
{
    g_autofree char *text = NULL;
    g_auto(GStrv) lines = NULL;
    gboolean enabled = TRUE;

    if (!g_file_get_contents (CONF, &text, NULL, NULL))
        return TRUE;
    lines = g_strsplit (text, "\n", -1);
    for (char **l = lines; *l; l++) {
        if (g_str_has_prefix (*l, "enabled="))
            enabled = g_strcmp0 (g_strstrip (*l + strlen ("enabled=")), "no") != 0;
    }
    return enabled;
}

/* The time of the last download, shown only while on and once there was one */
static void
update_last (PliniusEpo *self)
{
    g_autofree char *text = NULL, *when = NULL;
    g_autoptr(GDateTime) time = NULL;
    gint64 seconds;

    if (!adw_switch_row_get_active (self->row) || !g_file_get_contents (STAMP, &text, NULL, NULL) ||
        (seconds = g_ascii_strtoll (g_strstrip (text), NULL, 10)) <= 0 ||
        (time = g_date_time_new_from_unix_local (seconds)) == NULL) {
        gtk_widget_set_visible (GTK_WIDGET (self->last_row), FALSE);
        return;
    }
    /* Translators: date and time of the last download of the orbit files, strftime format */
    when = g_date_time_format (time, P_("%x, %H:%M"));
    gtk_label_set_text (self->last, when);
    gtk_widget_set_visible (GTK_WIDGET (self->last_row), TRUE);
}

/* The switch as the file says, without running the tool again */
static void
sync_from_conf (PliniusEpo *self)
{
    self->syncing = TRUE;
    adw_switch_row_set_active (self->row, read_enabled ());
    self->syncing = FALSE;
    update_last (self);
}

static void
stamp_changed_cb (GFileMonitor *monitor, GFile *file, GFile *other, GFileMonitorEvent event, PliniusEpo *self)
{
    update_last (self);
}

static gboolean
sync_idle_cb (gpointer data)
{
    PliniusEpo *self = g_object_get_data (G_OBJECT (data), "plinius-epo");

    if (self)
        sync_from_conf (self);
    return G_SOURCE_REMOVE;
}

static void
set_done_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(AdwSwitchRow) row = user_data;
    PliniusEpo *self = g_object_get_data (G_OBJECT (row), "plinius-epo");
    g_autoptr(GError) error = NULL;

    if (!g_subprocess_wait_check_finish (G_SUBPROCESS (source), result, &error))
        g_warning ("%s failed: %s", SET_TOOL, error->message);
    /* Also on failure: the switch goes back to what is true */
    sync_from_conf (self);
}

static void
on_active_changed (AdwSwitchRow *row, GParamSpec *pspec, PliniusEpo *self)
{
    g_autoptr(GSubprocess) process = NULL;
    g_autoptr(GError) error = NULL;

    if (self->syncing)
        return;
    update_last (self);
    process = g_subprocess_new (G_SUBPROCESS_FLAGS_STDOUT_SILENCE | G_SUBPROCESS_FLAGS_STDERR_SILENCE, &error,
                                "pkexec", SET_TOOL, adw_switch_row_get_active (row) ? "on" : "off", NULL);
    if (process == NULL) {
        g_warning ("Cannot run pkexec %s: %s", SET_TOOL, error->message);
        /* Not inside this handler: set back while GTK still hands on the user's change, the row
         * notifies again and the handler would run the tool once more */
        g_idle_add_full (G_PRIORITY_DEFAULT_IDLE, sync_idle_cb, g_object_ref (row), g_object_unref);
        return;
    }
    g_subprocess_wait_check_async (process, NULL, set_done_cb, g_object_ref (row));
}

/**
 * cc_plinius_epo_setup:
 * @page: the Location page
 *
 * Adds the group "Faster GPS Fix" right below the location switch. Returns %FALSE without the
 * plinius-epo package.
 */
gboolean
cc_plinius_epo_setup (AdwPreferencesPage *page)
{
    g_autoptr(GFile) dir = NULL;
    AdwPreferencesGroup *group;
    GtkWidget *row, *last_row, *last;
    PliniusEpo *self;

    if (!g_file_test (SET_TOOL, G_FILE_TEST_IS_EXECUTABLE))
        return FALSE;

    group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_page_insert (page, group, 1);

    row = adw_switch_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_("Faster GPS Fix"));
    adw_action_row_set_subtitle (ADW_ACTION_ROW (row),
                                 P_("Downloads satellite orbit predictions from MediaTek every 6 hours, about "
                                    "80 KB. The GPS then finds the position in seconds instead of about a "
                                    "minute. No data is sent."));
    adw_preferences_group_add (group, row);

    last_row = adw_action_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (last_row), P_("Last Download"));
    last = gtk_label_new (NULL);
    gtk_widget_add_css_class (last, "dim-label");
    adw_action_row_add_suffix (ADW_ACTION_ROW (last_row), last);
    gtk_widget_set_visible (last_row, FALSE);
    adw_preferences_group_add (group, last_row);

    self = g_new0 (PliniusEpo, 1);
    self->row = ADW_SWITCH_ROW (row);
    self->last_row = ADW_ACTION_ROW (last_row);
    self->last = GTK_LABEL (last);
    g_object_set_data_full (G_OBJECT (row), "plinius-epo", self, epo_free);

    /* plinius-epo replaces the stamp (mv), so watch its directory */
    dir = g_file_new_for_path ("/var/lib/plinius-epo");
    self->monitor = g_file_monitor_directory (dir, G_FILE_MONITOR_NONE, NULL, NULL);
    if (self->monitor)
        g_signal_connect (self->monitor, "changed", G_CALLBACK (stamp_changed_cb), self);

    sync_from_conf (self);
    g_signal_connect (row, "notify::active", G_CALLBACK (on_active_changed), self);
    return TRUE;
}
