/*
 * cc-plinius-connections.c
 *
 * Plinius: "Automatic Connections" at the end of the Privacy page, the connections the phone
 * makes on its own and that the owner may turn off.
 *
 * - "Search for Updates Automatically": plinius-software-check.timer, a user timer that the
 *   plinius-software package enables for every user (a link in /usr/lib/systemd/user), so it
 *   cannot be disabled, only masked. Off is "systemctl --user mask --now" (stops it as well), on
 *   is "systemctl --user unmask" followed by "systemctl --user start". The switch shows whether
 *   the user's unit directory masks it (a link to /dev/null), as systemd decides. Without the
 *   timer (no plinius-software) the row is hidden. Updates can always be looked for by hand under
 *   System > Updates.
 * - "Connection Check": NetworkManager's check whether the internet is reachable, which finds
 *   Wi-Fi networks with a login page; it asks a server every few minutes. The switch and the
 *   server come from /etc/NetworkManager/conf.d/90-plinius-connectivity.conf, written by
 *   "pkexec /usr/lib/plinius/plinius-connectivity-set off | on URI RESPONSE" (device package,
 *   no password); without that file Arch's 20-connectivity.conf applies: on, ping.archlinux.org.
 *   The server is one of a few known ones or the owner's own (address and expected answer). An
 *   own server is only written once its address is applied. Without the tool the rows are hidden.
 *
 * Nothing shows when neither is there. Our own strings use the gettext domain
 * plinius-control-center (not GNOME's po/).
 *
 * SPDX-License-Identifier: MIT
 */

#undef G_LOG_DOMAIN
#define G_LOG_DOMAIN "cc-plinius-connections"

#include "cc-plinius-connections.h"

#define PLINIUS_DOMAIN "plinius-control-center"
#define P_(String) g_dgettext (PLINIUS_DOMAIN, String)

/* plinius-software */
#define TIMER      "plinius-software-check.timer"
#define TIMER_UNIT "/usr/lib/systemd/user/" TIMER
/* device-volla-ansuz */
#define SET_TOOL   "/usr/lib/plinius/plinius-connectivity-set"
#define NM_CONF    "/etc/NetworkManager/conf.d/90-plinius-connectivity.conf"

/* NetworkManager's answer when the config names none */
#define NM_DEFAULT_RESPONSE "NetworkManager is online"

typedef struct {
    const char *label;
    const char *uri;
    const char *response; /* "" = an empty answer (HTTP 204) */
} Server;

/* The first is Arch's 20-connectivity.conf, what applies without our file */
static const Server servers[] = {
    { "Arch Linux (ping.archlinux.org)", "http://ping.archlinux.org/nm-check.txt", NM_DEFAULT_RESPONSE },
    { "GNOME (nmcheck.gnome.org)", "http://nmcheck.gnome.org/check_network_status.txt", NM_DEFAULT_RESPONSE },
    { "Ubuntu (connectivity-check.ubuntu.com)", "http://connectivity-check.ubuntu.com./", "" },
};
#define OWN_SERVER G_N_ELEMENTS (servers)

typedef struct {
    AdwPreferencesGroup *group;
    /* updates */
    AdwSwitchRow *updates_row;
    gboolean updates_syncing;
    /* connection check */
    AdwSwitchRow *check_row;
    AdwComboRow *server_row;
    AdwEntryRow *uri_row;
    AdwEntryRow *response_row;
    gboolean check_syncing;
} PliniusConnections;

/* ------------------------------------------------------------------ updates */

static char *
mask_path (void)
{
    return g_build_filename (g_get_user_config_dir (), "systemd", "user", TIMER, NULL);
}

/* Off when the user's unit directory masks the timer, as systemctl --user mask does */
static gboolean
updates_on (void)
{
    g_autofree char *path = mask_path ();
    g_autofree char *target = g_file_read_link (path, NULL);

    return g_strcmp0 (target, "/dev/null") != 0;
}

static void
updates_sync (PliniusConnections *self)
{
    self->updates_syncing = TRUE;
    adw_switch_row_set_active (self->updates_row, updates_on ());
    self->updates_syncing = FALSE;
    gtk_widget_set_sensitive (GTK_WIDGET (self->updates_row), TRUE);
}

static GSubprocess *
systemctl (const char *verb, GError **error)
{
    if (g_str_equal (verb, "mask"))
        return g_subprocess_new (G_SUBPROCESS_FLAGS_STDOUT_SILENCE | G_SUBPROCESS_FLAGS_STDERR_SILENCE, error,
                                 "systemctl", "--user", "mask", "--now", TIMER, NULL);
    return g_subprocess_new (G_SUBPROCESS_FLAGS_STDOUT_SILENCE | G_SUBPROCESS_FLAGS_STDERR_SILENCE, error,
                             "systemctl", "--user", verb, TIMER, NULL);
}

static void updates_step_cb (GObject *source, GAsyncResult *result, gpointer user_data);

/* Run systemctl VERB; "unmask" goes on with "start" when it worked */
static void
updates_run (AdwSwitchRow *row, const char *verb)
{
    PliniusConnections *self = g_object_get_data (G_OBJECT (row), "plinius-connections");
    g_autoptr(GSubprocess) process = NULL;
    g_autoptr(GError) error = NULL;

    process = systemctl (verb, &error);
    if (process == NULL) {
        g_warning ("Cannot run systemctl --user %s %s: %s", verb, TIMER, error->message);
        updates_sync (self);
        return;
    }
    g_object_set_data (G_OBJECT (process), "verb", (gpointer) verb);
    g_subprocess_wait_check_async (process, NULL, updates_step_cb, g_object_ref (row));
}

static void
updates_step_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(AdwSwitchRow) row = user_data;
    PliniusConnections *self = g_object_get_data (G_OBJECT (row), "plinius-connections");
    const char *verb = g_object_get_data (source, "verb");
    g_autoptr(GError) error = NULL;

    if (!g_subprocess_wait_check_finish (G_SUBPROCESS (source), result, &error))
        g_warning ("systemctl --user %s %s failed: %s", verb, TIMER, error->message);
    else if (g_str_equal (verb, "unmask")) {
        updates_run (row, "start");
        return;
    }
    /* Also after a failure: the switch shows what is true */
    updates_sync (self);
}

static gboolean
updates_sync_idle_cb (gpointer data)
{
    PliniusConnections *self = g_object_get_data (G_OBJECT (data), "plinius-connections");

    if (self)
        updates_sync (self);
    return G_SOURCE_REMOVE;
}

static void
updates_changed_cb (AdwSwitchRow *row, GParamSpec *pspec, PliniusConnections *self)
{
    g_autofree char *found = NULL;

    if (self->updates_syncing)
        return;
    if ((found = g_find_program_in_path ("systemctl")) == NULL) {
        g_warning ("Cannot run systemctl --user: not found");
        /* Not inside this handler: GTK still hands on the user's change */
        g_idle_add_full (G_PRIORITY_DEFAULT_IDLE, updates_sync_idle_cb, g_object_ref (row), g_object_unref);
        return;
    }
    gtk_widget_set_sensitive (GTK_WIDGET (row), FALSE);
    updates_run (row, adw_switch_row_get_active (row) ? "unmask" : "mask");
}

/* --------------------------------------------------------- connection check */

typedef struct {
    gboolean enabled;
    char *uri;
    char *response;
} CheckConf;

static void
check_conf_clear (CheckConf *conf)
{
    g_clear_pointer (&conf->uri, g_free);
    g_clear_pointer (&conf->response, g_free);
}

/* Our file as NetworkManager reads it; without it (or without a key) Arch's default */
static void
check_conf_read (CheckConf *conf)
{
    g_autoptr(GKeyFile) file = g_key_file_new ();
    g_autoptr(GError) error = NULL;

    conf->enabled = TRUE;
    conf->uri = g_strdup (servers[0].uri);
    conf->response = g_strdup (NM_DEFAULT_RESPONSE);
    if (!g_key_file_load_from_file (file, NM_CONF, G_KEY_FILE_NONE, NULL))
        return;
    if (g_key_file_has_key (file, "connectivity", "enabled", NULL)) {
        gboolean enabled = g_key_file_get_boolean (file, "connectivity", "enabled", &error);
        if (error == NULL)
            conf->enabled = enabled;
    }
    if (g_key_file_has_key (file, "connectivity", "uri", NULL)) {
        g_autofree char *uri = g_key_file_get_string (file, "connectivity", "uri", NULL);
        if (uri != NULL && *g_strstrip (uri) != '\0') {
            g_free (conf->uri);
            conf->uri = g_steal_pointer (&uri);
        }
    }
    if (g_key_file_has_key (file, "connectivity", "response", NULL)) {
        g_free (conf->response);
        conf->response = g_key_file_get_string (file, "connectivity", "response", NULL);
        if (conf->response == NULL)
            conf->response = g_strdup ("");
        g_strstrip (conf->response);
    }
}

static guint
server_index (const CheckConf *conf)
{
    for (guint i = 0; i < G_N_ELEMENTS (servers); i++)
        if (g_str_equal (conf->uri, servers[i].uri) && g_str_equal (conf->response, servers[i].response))
            return i;
    return OWN_SERVER;
}

static void
check_show (PliniusConnections *self)
{
    gboolean on = adw_switch_row_get_active (self->check_row);
    gboolean own = adw_combo_row_get_selected (self->server_row) == OWN_SERVER;

    gtk_widget_set_visible (GTK_WIDGET (self->server_row), on);
    gtk_widget_set_visible (GTK_WIDGET (self->uri_row), on && own);
    gtk_widget_set_visible (GTK_WIDGET (self->response_row), on && own);
    adw_action_row_set_subtitle (ADW_ACTION_ROW (self->server_row),
                                 own ? P_("The server must answer the address with the expected text, or with "
                                          "nothing when it is left empty.")
                                     : NULL);
}

static void
check_set_busy (PliniusConnections *self, gboolean busy)
{
    gtk_widget_set_sensitive (GTK_WIDGET (self->check_row), !busy);
    gtk_widget_set_sensitive (GTK_WIDGET (self->server_row), !busy);
    gtk_widget_set_sensitive (GTK_WIDGET (self->uri_row), !busy);
    gtk_widget_set_sensitive (GTK_WIDGET (self->response_row), !busy);
}

/* All rows as the file says, without running the tool */
static void
check_sync (PliniusConnections *self)
{
    CheckConf conf = { 0 };

    check_conf_read (&conf);
    self->check_syncing = TRUE;
    adw_switch_row_set_active (self->check_row, conf.enabled);
    adw_combo_row_set_selected (self->server_row, server_index (&conf));
    gtk_editable_set_text (GTK_EDITABLE (self->uri_row), conf.uri);
    gtk_editable_set_text (GTK_EDITABLE (self->response_row), conf.response);
    self->check_syncing = FALSE;
    gtk_widget_remove_css_class (GTK_WIDGET (self->uri_row), "error");
    check_show (self);
    check_set_busy (self, FALSE);
    check_conf_clear (&conf);
}

static void
check_done_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(AdwSwitchRow) row = user_data;
    PliniusConnections *self = g_object_get_data (G_OBJECT (row), "plinius-connections");
    gboolean own = GPOINTER_TO_INT (g_object_get_data (source, "own"));
    g_autoptr(GError) error = NULL;

    if (g_subprocess_wait_check_finish (G_SUBPROCESS (source), result, &error)) {
        check_sync (self);
        return;
    }
    g_warning ("%s failed: %s", SET_TOOL, error->message);
    if (own) {
        /* The own server's address or answer was refused: keep what was typed and say so */
        check_set_busy (self, FALSE);
        gtk_widget_add_css_class (GTK_WIDGET (self->uri_row), "error");
        adw_action_row_set_subtitle (ADW_ACTION_ROW (self->server_row),
                                     P_("The address or the answer was not accepted."));
        return;
    }
    check_sync (self);
}

static gboolean
check_sync_idle_cb (gpointer data)
{
    PliniusConnections *self = g_object_get_data (G_OBJECT (data), "plinius-connections");

    if (self)
        check_sync (self);
    return G_SOURCE_REMOVE;
}

/* pkexec SET_TOOL off, or on URI RESPONSE */
static void
check_write (PliniusConnections *self, gboolean on, const char *uri, const char *response, gboolean own)
{
    g_autoptr(GSubprocess) process = NULL;
    g_autoptr(GError) error = NULL;

    if (on)
        process = g_subprocess_new (G_SUBPROCESS_FLAGS_STDOUT_SILENCE | G_SUBPROCESS_FLAGS_STDERR_SILENCE, &error,
                                    "pkexec", SET_TOOL, "on", uri, response, NULL);
    else
        process = g_subprocess_new (G_SUBPROCESS_FLAGS_STDOUT_SILENCE | G_SUBPROCESS_FLAGS_STDERR_SILENCE, &error,
                                    "pkexec", SET_TOOL, "off", NULL);
    if (process == NULL) {
        g_warning ("Cannot run pkexec %s: %s", SET_TOOL, error->message);
        g_idle_add_full (G_PRIORITY_DEFAULT_IDLE, check_sync_idle_cb, g_object_ref (self->check_row),
                         g_object_unref);
        return;
    }
    check_set_busy (self, TRUE);
    g_object_set_data (G_OBJECT (process), "own", GINT_TO_POINTER (own));
    g_subprocess_wait_check_async (process, NULL, check_done_cb, g_object_ref (self->check_row));
}

/* Write what the rows show: a known server, or the own one with the typed address and answer */
static void
check_write_shown (PliniusConnections *self)
{
    guint i = adw_combo_row_get_selected (self->server_row);

    if (!adw_switch_row_get_active (self->check_row))
        check_write (self, FALSE, NULL, NULL, FALSE);
    else if (i < G_N_ELEMENTS (servers))
        check_write (self, TRUE, servers[i].uri, servers[i].response, FALSE);
    else
        check_write (self, TRUE, gtk_editable_get_text (GTK_EDITABLE (self->uri_row)),
                     gtk_editable_get_text (GTK_EDITABLE (self->response_row)), TRUE);
}

static void
check_changed_cb (AdwSwitchRow *row, GParamSpec *pspec, PliniusConnections *self)
{
    if (self->check_syncing)
        return;
    check_show (self);
    check_write_shown (self);
}

static void
server_changed_cb (AdwComboRow *row, GParamSpec *pspec, PliniusConnections *self)
{
    if (self->check_syncing)
        return;
    check_show (self);
    /* The own server is written once its address is applied */
    if (adw_combo_row_get_selected (row) != OWN_SERVER)
        check_write_shown (self);
}

static void
own_applied_cb (AdwEntryRow *row, PliniusConnections *self)
{
    gtk_widget_remove_css_class (GTK_WIDGET (self->uri_row), "error");
    check_write_shown (self);
}

/* ---------------------------------------------------------------- the group */

/**
 * cc_plinius_connections_add:
 * @page: the Privacy page
 *
 * Adds the group "Automatic Connections" at the end of @page. Returns %FALSE, adding nothing,
 * when neither the update timer nor the connection check tool is installed.
 */
gboolean
cc_plinius_connections_add (AdwPreferencesPage *page)
{
    gboolean have_timer = g_file_test (TIMER_UNIT, G_FILE_TEST_EXISTS);
    gboolean have_tool = g_file_test (SET_TOOL, G_FILE_TEST_IS_EXECUTABLE);
    g_autoptr(GtkStringList) names = NULL;
    PliniusConnections *self;
    GtkWidget *row;

    if (!have_timer && !have_tool)
        return FALSE;

    self = g_new0 (PliniusConnections, 1);
    self->group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_group_set_title (self->group, P_("Automatic Connections"));
    adw_preferences_page_add (page, self->group);

    row = adw_switch_row_new ();
    self->updates_row = ADW_SWITCH_ROW (row);
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_("Search for Updates Automatically"));
    adw_action_row_set_subtitle (ADW_ACTION_ROW (row),
                                 P_("Every 4 hours the phone asks plinius.cais.de and the servers of Arch Linux "
                                    "ARM for new versions. When off, look for them yourself under System › "
                                    "Updates."));
    gtk_widget_set_visible (row, have_timer);
    adw_preferences_group_add (self->group, row);

    row = adw_switch_row_new ();
    self->check_row = ADW_SWITCH_ROW (row);
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_("Connection Check"));
    adw_action_row_set_subtitle (ADW_ACTION_ROW (row),
                                 P_("NetworkManager regularly asks a server whether the internet is reachable; "
                                    "that is how it recognises Wi-Fi networks with a login page. When off, no "
                                    "server is asked and such networks are not recognised."));
    gtk_widget_set_visible (row, have_tool);
    adw_preferences_group_add (self->group, row);

    names = gtk_string_list_new (NULL);
    for (guint i = 0; i < G_N_ELEMENTS (servers); i++)
        gtk_string_list_append (names, servers[i].label);
    gtk_string_list_append (names, P_("Own Server"));
    row = adw_combo_row_new ();
    self->server_row = ADW_COMBO_ROW (row);
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_("Check Server"));
    adw_combo_row_set_model (self->server_row, G_LIST_MODEL (names));
    adw_preferences_group_add (self->group, row);

    row = adw_entry_row_new ();
    self->uri_row = ADW_ENTRY_ROW (row);
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_("Server Address"));
    adw_entry_row_set_show_apply_button (self->uri_row, TRUE);
    gtk_editable_set_enable_undo (GTK_EDITABLE (row), FALSE);
    adw_entry_row_set_input_purpose (self->uri_row, GTK_INPUT_PURPOSE_URL);
    adw_preferences_group_add (self->group, row);

    row = adw_entry_row_new ();
    self->response_row = ADW_ENTRY_ROW (row);
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_("Expected Answer"));
    adw_entry_row_set_show_apply_button (self->response_row, TRUE);
    adw_preferences_group_add (self->group, row);

    g_object_set_data_full (G_OBJECT (self->updates_row), "plinius-connections", self, g_free);
    g_object_set_data (G_OBJECT (self->check_row), "plinius-connections", self);

    updates_sync (self);
    check_sync (self);
    if (!have_tool) {
        gtk_widget_set_visible (GTK_WIDGET (self->server_row), FALSE);
        gtk_widget_set_visible (GTK_WIDGET (self->uri_row), FALSE);
        gtk_widget_set_visible (GTK_WIDGET (self->response_row), FALSE);
    }
    g_signal_connect (self->updates_row, "notify::active", G_CALLBACK (updates_changed_cb), self);
    g_signal_connect (self->check_row, "notify::active", G_CALLBACK (check_changed_cb), self);
    g_signal_connect (self->server_row, "notify::selected", G_CALLBACK (server_changed_cb), self);
    g_signal_connect (self->uri_row, "apply", G_CALLBACK (own_applied_cb), self);
    g_signal_connect (self->response_row, "apply", G_CALLBACK (own_applied_cb), self);
    return TRUE;
}
