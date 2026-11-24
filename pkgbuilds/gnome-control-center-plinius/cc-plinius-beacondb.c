/*
 * cc-plinius-beacondb.c
 *
 * Plinius: "Contribute to beaconDB" on the Location page, a group right below the location switch.
 * The switch runs furios-gps-contribute on|off (github.com/misc-de/furios_gps). On, its user service
 * sends the addresses and signal strengths of nearby Wi-Fi networks together with a GPS fix to
 * beaconDB, over Wi-Fi only; off, nothing is collected or sent. The switch shows the tool's marker
 * file, so it tells what the tool will do, whoever switched it.
 *
 * Contributing needs GPS fixes, and geoclue hands out none while location is off: the switch is
 * insensitive then. geoclue itself never asks beaconDB for a position (gnss-nmea-bridge's
 * 91-gps-only.conf), this only goes the other way.
 *
 * Nothing of this shows without the tool. Our own strings use the gettext domain
 * plinius-control-center (not GNOME's po/).
 *
 * SPDX-License-Identifier: MIT
 */

#undef G_LOG_DOMAIN
#define G_LOG_DOMAIN "cc-plinius-beacondb"

#include "cc-plinius-beacondb.h"

#define PLINIUS_DOMAIN "plinius-control-center"
#define P_(String) g_dgettext (PLINIUS_DOMAIN, String)

#define TOOL "furios-gps-contribute"

/* Kept on the switch row; the row is referenced by every running tool call */
typedef struct {
    AdwSwitchRow *row;
    AdwActionRow *status_row;
    GtkLabel *status;
    char *marker;    /* exists = switched on, the tool's own definition */
    gboolean syncing; /* the row is set from the marker, not by the user */
} Beacondb;

static void
beacondb_free (gpointer data)
{
    Beacondb *self = data;

    g_free (self->marker);
    g_free (self);
}

/* The value of "key=value" in the output of "status", or NULL */
static char *
status_value (const char *out, const char *key)
{
    g_auto(GStrv) lines = g_strsplit (out, "\n", -1);
    gsize len = strlen (key);

    for (char **l = lines; *l; l++) {
        if (g_str_has_prefix (*l, key) && (*l)[len] == '=')
            return g_strdup (*l + len + 1);
    }
    return NULL;
}

static void
status_done_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(AdwSwitchRow) row = user_data;
    Beacondb *self = g_object_get_data (G_OBJECT (row), "plinius-beacondb");
    g_autofree char *out = NULL, *submitted = NULL, *queued = NULL, *text = NULL;

    if (!g_subprocess_communicate_utf8_finish (G_SUBPROCESS (source), result, &out, NULL, NULL) || !out)
        return;
    submitted = status_value (out, "submitted");
    queued = status_value (out, "queued");
    if (!submitted || !queued)
        return;

    /* Translators: observations handed to beaconDB, and those waiting for Wi-Fi */
    text = g_strdup_printf (P_("Sent: %s, waiting: %s"), submitted, queued);
    gtk_label_set_text (self->status, text);
    /* Only worth a row once something was collected, or while it is on */
    gtk_widget_set_visible (GTK_WIDGET (self->status_row),
                            adw_switch_row_get_active (self->row) || g_strcmp0 (submitted, "0") != 0 ||
                                g_strcmp0 (queued, "0") != 0);
}

/* Ask the tool what it has sent and what is queued */
static void
refresh_status (Beacondb *self)
{
    g_autoptr(GSubprocess) process = NULL;

    process = g_subprocess_new (G_SUBPROCESS_FLAGS_STDOUT_PIPE | G_SUBPROCESS_FLAGS_STDERR_SILENCE, NULL, TOOL,
                                "status", NULL);
    if (process)
        g_subprocess_communicate_utf8_async (process, NULL, NULL, status_done_cb, g_object_ref (self->row));
}

/* The switch as the marker says, without running the tool again */
static void
sync_from_marker (Beacondb *self)
{
    self->syncing = TRUE;
    adw_switch_row_set_active (self->row, g_file_test (self->marker, G_FILE_TEST_EXISTS));
    self->syncing = FALSE;
    refresh_status (self);
}

static gboolean
sync_idle_cb (gpointer data)
{
    Beacondb *self = g_object_get_data (G_OBJECT (data), "plinius-beacondb");

    if (self)
        sync_from_marker (self);
    return G_SOURCE_REMOVE;
}

static void
switch_done_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(AdwSwitchRow) row = user_data;
    Beacondb *self = g_object_get_data (G_OBJECT (row), "plinius-beacondb");
    g_autoptr(GError) error = NULL;

    if (!g_subprocess_wait_check_finish (G_SUBPROCESS (source), result, &error))
        g_warning ("%s failed: %s", TOOL, error->message);
    /* Also on failure: the switch goes back to what is true */
    sync_from_marker (self);
}

static void
on_active_changed (AdwSwitchRow *row, GParamSpec *pspec, Beacondb *self)
{
    g_autoptr(GSubprocess) process = NULL;
    g_autoptr(GError) error = NULL;

    if (self->syncing)
        return;
    process = g_subprocess_new (G_SUBPROCESS_FLAGS_STDOUT_SILENCE | G_SUBPROCESS_FLAGS_STDERR_SILENCE, &error,
                                TOOL, adw_switch_row_get_active (row) ? "on" : "off", NULL);
    if (process == NULL) {
        g_warning ("Cannot run %s: %s", TOOL, error->message);
        /* Not inside this handler: set back while GTK still hands on the user's change, the row
         * notifies again and the handler would run the tool once more */
        g_idle_add_full (G_PRIORITY_DEFAULT_IDLE, sync_idle_cb, g_object_ref (row), g_object_unref);
        return;
    }
    g_subprocess_wait_check_async (process, NULL, switch_done_cb, g_object_ref (row));
}

/**
 * cc_plinius_beacondb_setup:
 * @page: the Location page
 * @location_row: its "Automatic Device Location" switch
 *
 * Adds the beaconDB group right below the group of @location_row. Returns %FALSE without the tool.
 */
gboolean
cc_plinius_beacondb_setup (AdwPreferencesPage *page, AdwSwitchRow *location_row)
{
    g_autofree char *tool = g_find_program_in_path (TOOL);
    AdwPreferencesGroup *group;
    GtkWidget *row, *status_row, *status;
    Beacondb *self;

    if (tool == NULL)
        return FALSE;

    group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_page_insert (page, group, 1);

    row = adw_switch_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_("Contribute to beaconDB"));
    adw_action_row_set_subtitle (ADW_ACTION_ROW (row),
                                 P_("Sends the addresses and signal strengths of nearby Wi-Fi networks with the "
                                    "GPS position to the open beaconDB project, only over Wi-Fi. Never network "
                                    "names, hidden networks or networks whose name ends in _nomap."));
    adw_preferences_group_add (group, row);

    status_row = adw_action_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (status_row), P_("Contributions"));
    status = gtk_label_new (NULL);
    gtk_widget_add_css_class (status, "dim-label");
    adw_action_row_add_suffix (ADW_ACTION_ROW (status_row), status);
    gtk_widget_set_visible (status_row, FALSE);
    adw_preferences_group_add (group, status_row);

    /* No GPS fixes without location, so nothing could be contributed */
    g_object_bind_property (location_row, "active", group, "sensitive", G_BINDING_SYNC_CREATE);

    self = g_new0 (Beacondb, 1);
    self->row = ADW_SWITCH_ROW (row);
    self->status_row = ADW_ACTION_ROW (status_row);
    self->status = GTK_LABEL (status);
    /* As the tool finds it: $XDG_CONFIG_HOME, else ~/.config */
    self->marker = g_build_filename (g_get_user_config_dir (), "furios-gps", "contribute", NULL);
    g_object_set_data_full (G_OBJECT (row), "plinius-beacondb", self, beacondb_free);

    sync_from_marker (self);
    g_signal_connect (row, "notify::active", G_CALLBACK (on_active_changed), self);
    return TRUE;
}
