/*
 * cc-plinius-ssh.c
 *
 * Plinius: "Allowed Networks" on the Secure Shell page, a group below GNOME's switch. Three
 * switches say where the SSH server takes logins from: the USB network to a computer, local
 * networks (Wi-Fi, hotspot, Ethernet; local addresses only) and all networks (also mobile data,
 * VPN, the internet). They only matter while SSH is on, so the group follows GNOME's switch.
 *
 * Each switch runs "pkexec plinius-ssh-set allow|deny usb|local|all" (plinius-ssh package);
 * polkit asks for the user's PIN to allow a network, not to deny one (org.plinius.ssh.policy).
 * The tool sets firewall rules and the sshd file. The switches show /etc/plinius-ssh.conf, so
 * they tell what applies, also after a dismissed prompt. "All Networks" includes the other two:
 * while it is on, they show on and cannot be changed.
 *
 * Nothing of this shows without plinius-ssh. Our own strings use the gettext domain
 * plinius-control-center (not GNOME's po/).
 *
 * SPDX-License-Identifier: MIT
 */

#undef G_LOG_DOMAIN
#define G_LOG_DOMAIN "cc-plinius-ssh"

#include "cc-plinius-ssh.h"

#define PLINIUS_DOMAIN "plinius-control-center"
#define P_(String) g_dgettext (PLINIUS_DOMAIN, String)

/* See plinius-ssh-set in the plinius-ssh package */
#define SET_TOOL "/usr/lib/plinius-ssh/plinius-ssh-set"
#define CONF     "/etc/plinius-ssh.conf"

/* Exit codes of pkexec: prompt dismissed, not authorized */
#define PKEXEC_DISMISSED 126
#define PKEXEC_NOT_AUTHORIZED 127

enum { NET_USB, NET_LOCAL, NET_ALL, N_NETS };

/* Keys of the file and arguments of the tool; defaults as in plinius-ssh-set */
static const char *const net_keys[N_NETS] = { "usb", "local", "all" };
static const gboolean net_defaults[N_NETS] = { TRUE, FALSE, FALSE };

/* Kept on the group; the group is referenced by a running pkexec call */
typedef struct {
    AdwSwitchRow *rows[N_NETS];
    gboolean syncing; /* the rows are set from the file, not by the user */
    gboolean busy;    /* a pkexec call (and its PIN prompt) is running */
} PliniusSsh;

/* The choices of /etc/plinius-ssh.conf: "key=yes|no" lines, the last one of a key counts, a
 * missing file or key gives the default. As plinius-ssh-set's get() reads it: the word of a line
 * is its lowercase letters after the "=", and a last line whose word is not exactly yes or no
 * ("maybe", "Yes", "yesterday") also gives the default */
static void
read_conf (gboolean values[N_NETS])
{
    g_autofree char *text = NULL;
    g_auto(GStrv) lines = NULL;

    for (int i = 0; i < N_NETS; i++)
        values[i] = net_defaults[i];
    if (!g_file_get_contents (CONF, &text, NULL, NULL))
        return;

    lines = g_strsplit (text, "\n", -1);
    for (char **l = lines; *l != NULL; l++) {
        g_auto(GStrv) kv = g_strsplit (*l, "=", 2);
        const char *key;
        char *word;
        size_t len = 0;

        if (kv[0] == NULL || kv[1] == NULL)
            continue;
        key = g_strstrip (kv[0]);
        word = g_strchug (kv[1]);
        while (word[len] >= 'a' && word[len] <= 'z')
            len++;
        word[len] = '\0';
        for (int i = 0; i < N_NETS; i++) {
            if (g_strcmp0 (key, net_keys[i]) != 0)
                continue;
            if (g_str_equal (word, "yes"))
                values[i] = TRUE;
            else if (g_str_equal (word, "no"))
                values[i] = FALSE;
            else
                values[i] = net_defaults[i];
        }
    }
}

/* The switches as the file says, without running the tool again */
static void
sync_from_conf (PliniusSsh *self)
{
    gboolean values[N_NETS];
    gboolean all;

    read_conf (values);
    all = values[NET_ALL];

    self->syncing = TRUE;
    adw_switch_row_set_active (self->rows[NET_USB], all || values[NET_USB]);
    adw_switch_row_set_active (self->rows[NET_LOCAL], all || values[NET_LOCAL]);
    adw_switch_row_set_active (self->rows[NET_ALL], all);
    self->syncing = FALSE;

    /* One change at a time; USB and local are part of "all" while it is on */
    gtk_widget_set_sensitive (GTK_WIDGET (self->rows[NET_USB]), !self->busy && !all);
    gtk_widget_set_sensitive (GTK_WIDGET (self->rows[NET_LOCAL]), !self->busy && !all);
    gtk_widget_set_sensitive (GTK_WIDGET (self->rows[NET_ALL]), !self->busy);
}

/* One change at a time, until the change and its pkexec call (and its PIN prompt) are done */
static void
set_busy (PliniusSsh *self)
{
    self->busy = TRUE;
    for (int i = 0; i < N_NETS; i++)
        gtk_widget_set_sensitive (GTK_WIDGET (self->rows[i]), FALSE);
}

static gboolean
sync_idle (gpointer user_data)
{
    PliniusSsh *self = g_object_get_data (G_OBJECT (user_data), "plinius-ssh");

    self->busy = FALSE;
    sync_from_conf (self);
    return G_SOURCE_REMOVE;
}

static void
set_done_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(AdwPreferencesGroup) group = user_data;
    PliniusSsh *self = g_object_get_data (G_OBJECT (group), "plinius-ssh");
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

    /* Also on failure: the switches go back to what is true */
    self->busy = FALSE;
    sync_from_conf (self);
}

static void
on_active_changed (AdwSwitchRow *row, GParamSpec *pspec, AdwPreferencesGroup *group)
{
    PliniusSsh *self = g_object_get_data (G_OBJECT (group), "plinius-ssh");
    g_autoptr(GSubprocess) process = NULL;
    g_autoptr(GError) error = NULL;
    int net = -1;

    /* busy: the rows cannot be used, so this is no choice of the user */
    if (self->syncing || self->busy)
        return;
    for (int i = 0; i < N_NETS; i++)
        if (self->rows[i] == row)
            net = i;
    g_return_if_fail (net >= 0);

    process = g_subprocess_new (G_SUBPROCESS_FLAGS_STDOUT_SILENCE | G_SUBPROCESS_FLAGS_STDERR_SILENCE, &error,
                                "pkexec", SET_TOOL, adw_switch_row_get_active (row) ? "allow" : "deny",
                                net_keys[net], NULL);
    if (process == NULL) {
        g_warning ("Cannot run pkexec %s: %s", SET_TOOL, error->message);
        /* Put back once this change is over: a switch set back inside its own change notifies
         * again, which would try once more */
        set_busy (self);
        g_idle_add_full (G_PRIORITY_DEFAULT, sync_idle, g_object_ref (group), g_object_unref);
        return;
    }
    /* One change at a time, until pkexec (and its PIN prompt) is done */
    set_busy (self);
    g_subprocess_wait_async (process, NULL, set_done_cb, g_object_ref (group));
}

static GtkWidget *
add_row (AdwPreferencesGroup *group, const char *title, const char *subtitle)
{
    GtkWidget *row = adw_switch_row_new ();

    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), title);
    adw_action_row_set_subtitle (ADW_ACTION_ROW (row), subtitle);
    adw_preferences_group_add (group, row);
    return row;
}

/**
 * cc_plinius_ssh_setup:
 * @page: the preferences page of the Secure Shell dialog
 * @secure_shell_row: GNOME's switch for the SSH server
 *
 * Adds the group "Allowed Networks" at the end of @page; it can only be used while
 * @secure_shell_row is on. Returns %FALSE without plinius-ssh.
 */
gboolean
cc_plinius_ssh_setup (AdwPreferencesPage *page, AdwSwitchRow *secure_shell_row)
{
    AdwPreferencesGroup *group;
    PliniusSsh *self;

    if (!g_file_test (SET_TOOL, G_FILE_TEST_IS_EXECUTABLE))
        return FALSE;

    group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_group_set_title (group, P_("Allowed Networks"));
    adw_preferences_group_set_description (
        group, P_("Where logins over SSH are taken from. They need a key; allowing a network asks for the PIN."));
    adw_preferences_page_add (page, group);

    self = g_new0 (PliniusSsh, 1);
    self->rows[NET_USB] = ADW_SWITCH_ROW (
        add_row (group, P_("USB Network"), P_("A computer on the USB cable, with Network on in the USB settings")));
    self->rows[NET_LOCAL] = ADW_SWITCH_ROW (
        add_row (group, P_("Local Networks"), P_("Wi-Fi, hotspot and Ethernet, from local addresses only")));
    self->rows[NET_ALL] = ADW_SWITCH_ROW (
        add_row (group, P_("All Networks"), P_("Also mobile data, VPN and the internet")));
    g_object_set_data_full (G_OBJECT (group), "plinius-ssh", self, g_free);

    sync_from_conf (self);
    for (int i = 0; i < N_NETS; i++)
        g_signal_connect (self->rows[i], "notify::active", G_CALLBACK (on_active_changed), group);

    /* Only where the server runs: the group follows GNOME's switch */
    g_object_bind_property (secure_shell_row, "active", group, "sensitive", G_BINDING_SYNC_CREATE);
    return TRUE;
}
