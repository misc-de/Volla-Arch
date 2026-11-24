/*
 * cc-desktop-vnc-panel.c
 *
 * Plinius: "Remote Access", both ways another computer gets at the phone: a row for Secure Shell
 * and one for "Desktop over VNC", each telling whether it is on and opening a page of its own
 * (subpages "plinius-ssh" and "plinius-vnc", so "gnome-control-center desktop-vnc plinius-vnc"
 * opens the VNC page). The panel keeps the id "desktop-vnc", Phosh's quick setting opens it.
 *
 * Secure Shell is GNOME's switch and login command (from panels/system/secure-shell, where its
 * dialog no longer has a row) with the allowed networks of plinius-ssh below.
 *
 * Desktop over VNC: another computer's VNC viewer gets a normal desktop in the phone's own session,
 * Phosh stays on the phone (plinius-desktop package).
 *
 * The switch enables the user's socket unit plinius-desktop.socket; a connection to it starts the
 * server, and the virtual screen follows only after the login, so while nobody is connected
 * nothing but the listening port exists. The panel also tells who is connected, the address, and
 * the networks that may connect: those switches run "pkexec plinius-desktop-set allow|deny
 * usb|local", which asks for the PIN to allow a network, not to deny one
 * (org.plinius.desktop.policy), and show /etc/plinius-desktop.conf. The server makes its keys
 * itself when it starts (plinius-desktop-session).
 *
 * The taskbar row puts the bar of the desktop along the top edge (the default) or the bottom edge
 * of the viewer's screen (org.plinius.desktop bar-position); a running session moves its bar and
 * the bar's menus at once.
 *
 * The VNC row only shows when plinius-desktop is installed. Our own strings use the gettext
 * domain plinius-control-center (not GNOME's po/), the ones of Secure Shell are GNOME's.
 *
 * SPDX-License-Identifier: MIT
 */

#include "config.h"

#undef G_LOG_DOMAIN
#define G_LOG_DOMAIN "cc-desktop-vnc-panel"

#include <arpa/inet.h>
#include <ifaddrs.h>
#include <net/if.h>

#include <adwaita.h>
#include <glib/gi18n.h>

#include "cc-desktop-vnc-panel.h"
#include "cc-hostname.h"
#include "cc-list-row.h"
#include "panels/system/secure-shell/cc-plinius-ssh.h"
#include "panels/system/secure-shell/cc-secure-shell.h"
#include "shell/cc-application.h"

#define PLINIUS_DOMAIN "plinius-control-center"
#define P_(String) g_dgettext (PLINIUS_DOMAIN, String)

#define SET_TOOL  "/usr/lib/plinius-desktop/plinius-desktop-set"
#define CONF      "/etc/plinius-desktop.conf"
#define SOCKET    "plinius-desktop.socket"
#define SERVICE   "plinius-desktop.service"
#define PORT      5900
#define TAG_SSH   "plinius-ssh"
#define TAG_VNC   "plinius-vnc"
#define DESKTOP_SCHEMA "org.plinius.desktop"
#define BAR_KEY        "bar-position"
/* How often the panel looks who is connected while it is showing */
#define STATUS_SECONDS 2

/* Exit codes of pkexec: prompt dismissed, not authorized */
#define PKEXEC_DISMISSED 126
#define PKEXEC_NOT_AUTHORIZED 127

enum { NET_USB, NET_LOCAL, N_NETS };

/* Keys of the file and arguments of the tool; defaults as in plinius-desktop-set */
static const char *const net_keys[N_NETS] = { "usb", "local" };
static const gboolean net_defaults[N_NETS] = { TRUE, FALSE };

struct _CcDesktopVncPanel {
    CcPanel parent_instance;

    AdwToastOverlay *toast_overlay;
    AdwSwitchRow *ssh_row;
    AdwActionRow *ssh_command_row;

    /* Rows of the overview, with "On" or "Off" */
    GtkWidget *ssh_nav_row, *vnc_nav_row;

    AdwSwitchRow *enabled_row;
    AdwActionRow *status_row;
    GtkWidget *disconnect_button;
    AdwActionRow *address_row;
    AdwPreferencesGroup *networks_group;
    AdwSwitchRow *net_rows[N_NETS];
    guint status_id;
    gboolean syncing; /* the rows are set from the system, not by the user */
    gboolean busy;    /* a pkexec call (and its PIN prompt) is running */
};

G_DEFINE_TYPE (CcDesktopVncPanel, cc_desktop_vnc_panel, CC_TYPE_PANEL)

/* The socket unit is enabled: the link "systemctl enable" makes. Checked without a process, so
 * opening the panel doesn't wait for one. */
static gboolean
socket_enabled (void)
{
    g_autofree char *link = g_build_filename (g_get_user_config_dir (), "systemd", "user",
                                              "graphical-session.target.wants", SOCKET, NULL);

    return g_file_test (link, G_FILE_TEST_EXISTS);
}

/* The choices of /etc/plinius-desktop.conf: "key=yes|no" lines, the last one of a key counts */
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
        const char *key, *val;

        if (kv[0] == NULL || kv[1] == NULL)
            continue;
        key = g_strstrip (kv[0]);
        val = g_strstrip (kv[1]);
        for (int i = 0; i < N_NETS; i++) {
            if (g_strcmp0 (key, net_keys[i]) != 0)
                continue;
            if (g_str_has_prefix (val, "yes"))
                values[i] = TRUE;
            else if (g_str_has_prefix (val, "no"))
                values[i] = FALSE;
        }
    }
}

/* Where a viewer can reach the phone right now, with the networks allowed: "10.15.19.82 (USB)".
 * NULL when there is no such address. */
static char *
reachable_addresses (const gboolean values[N_NETS])
{
    g_autoptr(GString) text = g_string_new (NULL);
    struct ifaddrs *list = NULL;

    if (getifaddrs (&list) < 0)
        return NULL;

    for (struct ifaddrs *i = list; i != NULL; i = i->ifa_next) {
        const char *kind = NULL;
        char buf[INET_ADDRSTRLEN];

        if (i->ifa_addr == NULL || i->ifa_addr->sa_family != AF_INET || !(i->ifa_flags & IFF_UP))
            continue;
        if (values[NET_USB] && g_str_equal (i->ifa_name, "usb0"))
            kind = P_("USB");
        else if (values[NET_LOCAL] && g_str_has_prefix (i->ifa_name, "wlan"))
            kind = P_("Wi-Fi");
        else if (values[NET_LOCAL] && g_str_has_prefix (i->ifa_name, "ap"))
            kind = P_("Hotspot");
        else if (values[NET_LOCAL] && (g_str_has_prefix (i->ifa_name, "eth") || g_str_has_prefix (i->ifa_name, "enx")))
            kind = P_("Ethernet");
        if (kind == NULL
            || !inet_ntop (AF_INET, &((struct sockaddr_in *) i->ifa_addr)->sin_addr, buf, sizeof buf))
            continue;
        g_string_append_printf (text, "%s%s (%s)", text->len ? ", " : "", buf, kind);
    }
    freeifaddrs (list);

    return text->len ? g_strdup (text->str) : NULL;
}

/* All rows as the system says, without running a tool again */
static void
sync_panel (CcDesktopVncPanel *self)
{
    gboolean enabled = socket_enabled ();
    gboolean values[N_NETS];
    g_autofree char *client_file = g_build_filename (g_get_user_runtime_dir (), "plinius-desktop", "client", NULL);
    g_autofree char *client = NULL;
    g_autofree char *addresses = NULL;
    g_autofree char *status = NULL;
    gboolean connected;

    read_conf (values);

    self->syncing = TRUE;
    adw_switch_row_set_active (self->enabled_row, enabled);
    for (int i = 0; i < N_NETS; i++) {
        adw_switch_row_set_active (self->net_rows[i], values[i]);
        gtk_widget_set_sensitive (GTK_WIDGET (self->net_rows[i]), !self->busy);
    }
    self->syncing = FALSE;

    /* The session writes this file once somebody has logged in, and it goes with the session */
    connected = g_file_get_contents (client_file, &client, NULL, NULL);
    if (connected)
        status = g_strdup_printf (P_("Connected: %s"), client);
    gtk_widget_set_visible (GTK_WIDGET (self->status_row), enabled);
    adw_action_row_set_subtitle (self->status_row, connected ? status : P_("Waiting for a connection"));
    gtk_widget_set_visible (self->disconnect_button, connected);

    addresses = reachable_addresses (values);
    gtk_widget_set_visible (GTK_WIDGET (self->address_row), enabled);
    if (addresses) {
        g_autofree char *line = g_strdup_printf ("%s, %s %d", addresses, P_("port"), PORT);

        adw_action_row_set_subtitle (self->address_row, line);
    } else {
        adw_action_row_set_subtitle (self->address_row, P_("Not connected to an allowed network"));
    }

    /* Only where the port is open */
    gtk_widget_set_sensitive (GTK_WIDGET (self->networks_group), enabled);
}

static gboolean
status_tick (gpointer user_data)
{
    sync_panel (user_data);
    return G_SOURCE_CONTINUE;
}

/* "On" or "Off" in the overview's rows, like GNOME's panels say it */
static gboolean
on_off_mapping (GBinding *binding, const GValue *from, GValue *to, gpointer user_data)
{
    g_value_set_string (to, g_value_get_boolean (from) ? _("On") : _("Off"));
    return TRUE;
}

static void
on_showing (CcDesktopVncPanel *self)
{
    sync_panel (self);
    if (self->status_id == 0)
        self->status_id = g_timeout_add_seconds (STATUS_SECONDS, status_tick, self);
}

static void
on_hidden (CcDesktopVncPanel *self)
{
    g_clear_handle_id (&self->status_id, g_source_remove);
}

static void
systemctl_done_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(CcDesktopVncPanel) self = user_data;
    g_autoptr(GError) error = NULL;

    if (!g_subprocess_wait_check_finish (G_SUBPROCESS (source), result, &error))
        g_warning ("systemctl failed: %s", error->message);

    /* Also on failure: the switch goes back to what is true */
    gtk_widget_set_sensitive (GTK_WIDGET (self->enabled_row), TRUE);
    sync_panel (self);
}

static void
run_systemctl (CcDesktopVncPanel *self, const char *const *argv)
{
    g_autoptr(GSubprocess) process = NULL;
    g_autoptr(GError) error = NULL;

    process = g_subprocess_newv (argv, G_SUBPROCESS_FLAGS_STDOUT_SILENCE | G_SUBPROCESS_FLAGS_STDERR_SILENCE, &error);
    if (process == NULL) {
        g_warning ("Cannot run systemctl: %s", error->message);
        sync_panel (self);
        return;
    }
    g_subprocess_wait_check_async (process, NULL, systemctl_done_cb, g_object_ref (self));
}

static void
on_enabled_changed (CcDesktopVncPanel *self)
{
    /* Stopping the socket takes a running session with it (Requires= in the service) */
    const char *on[] = { "systemctl", "--user", "enable", "--now", SOCKET, NULL };
    const char *off[] = { "systemctl", "--user", "disable", "--now", SOCKET, NULL };

    if (self->syncing)
        return;
    gtk_widget_set_sensitive (GTK_WIDGET (self->enabled_row), FALSE);
    run_systemctl (self, adw_switch_row_get_active (self->enabled_row) ? on : off);
}

static void
on_disconnect_clicked (CcDesktopVncPanel *self)
{
    const char *stop[] = { "systemctl", "--user", "stop", SERVICE, NULL };

    run_systemctl (self, stop);
}

static void
set_done_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(CcDesktopVncPanel) self = user_data;
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
    sync_panel (self);
}

static void
on_net_changed (AdwSwitchRow *row, GParamSpec *pspec, CcDesktopVncPanel *self)
{
    g_autoptr(GSubprocess) process = NULL;
    g_autoptr(GError) error = NULL;
    int net = -1;

    if (self->syncing)
        return;
    for (int i = 0; i < N_NETS; i++)
        if (self->net_rows[i] == row)
            net = i;
    g_return_if_fail (net >= 0);

    process = g_subprocess_new (G_SUBPROCESS_FLAGS_STDOUT_SILENCE | G_SUBPROCESS_FLAGS_STDERR_SILENCE, &error,
                                "pkexec", SET_TOOL, adw_switch_row_get_active (row) ? "allow" : "deny",
                                net_keys[net], NULL);
    if (process == NULL) {
        g_warning ("Cannot run pkexec %s: %s", SET_TOOL, error->message);
        sync_panel (self);
        return;
    }
    /* One change at a time, until pkexec (and its PIN prompt) is done */
    self->busy = TRUE;
    for (int i = 0; i < N_NETS; i++)
        gtk_widget_set_sensitive (GTK_WIDGET (self->net_rows[i]), FALSE);
    g_subprocess_wait_async (process, NULL, set_done_cb, g_object_ref (self));
}

static AdwActionRow *
add_info_row (AdwPreferencesGroup *group, const char *title, const char *subtitle)
{
    GtkWidget *row = adw_action_row_new ();

    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), title);
    adw_action_row_set_subtitle (ADW_ACTION_ROW (row), subtitle);
    /* The title is the small label, the value what one reads and may copy */
    gtk_widget_add_css_class (row, "property");
    adw_action_row_set_subtitle_selectable (ADW_ACTION_ROW (row), TRUE);
    adw_preferences_group_add (group, row);
    return ADW_ACTION_ROW (row);
}

static AdwSwitchRow *
add_switch_row (AdwPreferencesGroup *group, const char *title, const char *subtitle)
{
    GtkWidget *row = adw_switch_row_new ();

    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), title);
    adw_action_row_set_subtitle (ADW_ACTION_ROW (row), subtitle);
    adw_preferences_group_add (group, row);
    return ADW_SWITCH_ROW (row);
}

static void
on_copy_ssh_command (CcDesktopVncPanel *self)
{
    gdk_clipboard_set_text (gtk_widget_get_clipboard (GTK_WIDGET (self)),
                            adw_action_row_get_subtitle (self->ssh_command_row));
    adw_toast_overlay_add_toast (self->toast_overlay, adw_toast_new (_("Command copied to clipboard")));
}

static void
on_ssh_switched (CcDesktopVncPanel *self, GParamSpec *pspec, AdwSwitchRow *row)
{
    cc_secure_shell_set_enabled (cc_panel_get_cancellable (CC_PANEL (self)), row);
}

/* A page of the navigation with a header bar around @content */
static AdwNavigationPage *
subpage_new (GtkWidget *content, const char *title, const char *tag)
{
    GtkWidget *toolbar = adw_toolbar_view_new ();

    adw_toolbar_view_add_top_bar (ADW_TOOLBAR_VIEW (toolbar), adw_header_bar_new ());
    adw_toolbar_view_set_content (ADW_TOOLBAR_VIEW (toolbar), content);
    return adw_navigation_page_new_with_tag (toolbar, title, tag);
}

/* A row of the overview that opens the page @tag */
static GtkWidget *
nav_row_new (AdwPreferencesGroup *group, const char *title, const char *subtitle, const char *tag)
{
    GtkWidget *row = g_object_new (CC_TYPE_LIST_ROW, "show-arrow", TRUE, "use-underline", TRUE, "action-name",
                                   "navigation.push", NULL);

    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), title);
    adw_action_row_set_subtitle (ADW_ACTION_ROW (row), subtitle);
    gtk_actionable_set_action_target (GTK_ACTIONABLE (row), "s", tag);
    adw_preferences_group_add (group, row);
    return row;
}

/* What GNOME's Secure Shell dialog shows, and the networks below it */
static void
add_secure_shell (CcDesktopVncPanel *self, AdwPreferencesPage *prefs)
{
    AdwPreferencesGroup *group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    GtkWidget *row = adw_switch_row_new ();
    GtkWidget *copy = gtk_button_new_from_icon_name ("edit-copy-symbolic");
    g_autofree char *hostname = cc_hostname_get_static_hostname (cc_hostname_get_default ());
    g_autofree char *command = g_strdup_printf ("ssh %s", hostname);

    adw_preferences_page_add (prefs, group);
    self->ssh_row = ADW_SWITCH_ROW (row);

    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), _("_Secure Shell"));
    adw_preferences_row_set_use_underline (ADW_PREFERENCES_ROW (row), TRUE);
    adw_action_row_set_subtitle (ADW_ACTION_ROW (row), _("Access this device using Secure Shell (SSH)"));
    adw_preferences_group_add (group, row);

    self->ssh_command_row = add_info_row (group, _("SSH Login _Command"), command);
    adw_preferences_row_set_use_underline (ADW_PREFERENCES_ROW (self->ssh_command_row), TRUE);
    gtk_widget_set_valign (copy, GTK_ALIGN_CENTER);
    gtk_widget_set_tooltip_text (copy, _("Copy"));
    gtk_widget_add_css_class (copy, "flat");
    adw_action_row_add_suffix (self->ssh_command_row, copy);
    g_signal_connect_swapped (copy, "clicked", G_CALLBACK (on_copy_ssh_command), self);

    /* First what is true, then what the user does */
    cc_secure_shell_get_enabled (ADW_SWITCH_ROW (row));
    g_signal_connect_swapped (row, "notify::active", G_CALLBACK (on_ssh_switched), self);

    cc_plinius_ssh_setup (prefs, ADW_SWITCH_ROW (row));
}

/* The taskbar's edge: the row's index and the nick of the schema's enum, the default first */
static const struct {
    const char *value;
    const char *label; /* translated when shown */
} bar_positions[] = {
    { "top", N_("Top") },
    { "bottom", N_("Bottom") },
};

static gboolean
bar_get_mapping (GValue *value, GVariant *variant, gpointer user_data)
{
    const char *text = g_variant_get_string (variant, NULL);

    for (guint i = 0; i < G_N_ELEMENTS (bar_positions); i++) {
        if (g_strcmp0 (text, bar_positions[i].value) == 0) {
            g_value_set_uint (value, i);
            return TRUE;
        }
    }
    return FALSE;
}

static GVariant *
bar_set_mapping (const GValue *value, const GVariantType *type, gpointer user_data)
{
    guint i = g_value_get_uint (value);

    if (i >= G_N_ELEMENTS (bar_positions))
        return NULL;
    return g_variant_new_string (bar_positions[i].value);
}

/* The row of the taskbar's edge; none with a plinius-desktop that has no such key */
static void
add_bar_row (AdwPreferencesGroup *group)
{
    GSettingsSchemaSource *source = g_settings_schema_source_get_default ();
    g_autoptr(GSettingsSchema) schema = NULL;
    g_autoptr(GSettings) settings = NULL;
    g_autoptr(GtkStringList) labels = NULL;
    GtkWidget *row;

    if (source)
        schema = g_settings_schema_source_lookup (source, DESKTOP_SCHEMA, TRUE);
    if (schema == NULL || !g_settings_schema_has_key (schema, BAR_KEY))
        return;
    settings = g_settings_new_full (schema, NULL, NULL);

    labels = gtk_string_list_new (NULL);
    for (guint i = 0; i < G_N_ELEMENTS (bar_positions); i++)
        gtk_string_list_append (labels, P_(bar_positions[i].label));
    row = adw_combo_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_("Taskbar"));
    adw_action_row_set_subtitle (ADW_ACTION_ROW (row),
                                 P_("Apps button and open windows, at the top or bottom edge"));
    adw_combo_row_set_model (ADW_COMBO_ROW (row), G_LIST_MODEL (labels));
    /* The binding keeps its own reference on the settings */
    g_settings_bind_with_mapping (settings, BAR_KEY, row, "selected", G_SETTINGS_BIND_DEFAULT,
                                  bar_get_mapping, bar_set_mapping, NULL, NULL);
    adw_preferences_group_add (group, row);
}

static void
cc_desktop_vnc_panel_dispose (GObject *object)
{
    CcDesktopVncPanel *self = CC_DESKTOP_VNC_PANEL (object);

    g_clear_handle_id (&self->status_id, g_source_remove);

    G_OBJECT_CLASS (cc_desktop_vnc_panel_parent_class)->dispose (object);
}

static void
cc_desktop_vnc_panel_class_init (CcDesktopVncPanelClass *klass)
{
    G_OBJECT_CLASS (klass)->dispose = cc_desktop_vnc_panel_dispose;
}

static void
cc_desktop_vnc_panel_init (CcDesktopVncPanel *self)
{
    AdwPreferencesPage *prefs;
    AdwPreferencesGroup *group;
    AdwNavigationPage *page;
    GtkWidget *toolbar;
    GtkWidget *overlay;
    gboolean has_vnc = g_file_test (SET_TOOL, G_FILE_TEST_IS_EXECUTABLE);

    adw_navigation_page_set_title (ADW_NAVIGATION_PAGE (self), P_("Remote Access"));

    /* Secure Shell, with a toast for the copied command */
    prefs = ADW_PREFERENCES_PAGE (adw_preferences_page_new ());
    add_secure_shell (self, prefs);
    overlay = adw_toast_overlay_new ();
    self->toast_overlay = ADW_TOAST_OVERLAY (overlay);
    adw_toast_overlay_set_child (self->toast_overlay, GTK_WIDGET (prefs));
    cc_panel_add_subpage (CC_PANEL (self), TAG_SSH, subpage_new (overlay, _("Secure Shell"), TAG_SSH));

    /* Desktop over VNC */
    prefs = ADW_PREFERENCES_PAGE (adw_preferences_page_new ());
    group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_page_add (prefs, group);
    self->enabled_row = add_switch_row (group, P_("Desktop over VNC"),
                                        P_("A VNC viewer on another computer gets a desktop with this phone's apps"));
    self->status_row = add_info_row (group, P_("Status"), "");
    self->disconnect_button = gtk_button_new_with_label (P_("Disconnect"));
    gtk_widget_set_valign (self->disconnect_button, GTK_ALIGN_CENTER);
    adw_action_row_add_suffix (self->status_row, self->disconnect_button);
    self->address_row = add_info_row (group, P_("Address"), "");
    add_bar_row (group);

    group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    self->networks_group = group;
    adw_preferences_group_set_title (group, P_("Allowed Networks"));
    adw_preferences_group_set_description (
        group, P_("Where connections are taken from. Never mobile data or the internet; allowing a network asks for the PIN."));
    adw_preferences_page_add (prefs, group);
    self->net_rows[NET_USB] = add_switch_row (group, P_("USB Network"),
                                              P_("A computer on the USB cable, with Network on in the USB settings"));
    self->net_rows[NET_LOCAL] = add_switch_row (group, P_("Local Networks"),
                                                P_("Wi-Fi, hotspot and Ethernet, from local addresses only"));
    /* Also without plinius-desktop: the rows stay, only the overview's row goes */
    page = subpage_new (GTK_WIDGET (prefs), P_("Desktop over VNC"), TAG_VNC);
    cc_panel_add_subpage (CC_PANEL (self), TAG_VNC, page);

    /* The overview: one row each, saying whether it is on */
    prefs = ADW_PREFERENCES_PAGE (adw_preferences_page_new ());
    group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_page_add (prefs, group);
    self->ssh_nav_row = nav_row_new (group, _("_Secure Shell"), _("Access this device using Secure Shell (SSH)"),
                                     TAG_SSH);
    g_object_bind_property_full (self->ssh_row, "active", self->ssh_nav_row, "secondary-label",
                                 G_BINDING_SYNC_CREATE, on_off_mapping, NULL, NULL, NULL);
    self->vnc_nav_row = nav_row_new (group, P_("Desktop over VNC"),
                                     P_("A VNC viewer on another computer gets a desktop with this phone's apps"),
                                     TAG_VNC);
    gtk_widget_set_visible (self->vnc_nav_row, has_vnc);
    g_object_bind_property_full (self->enabled_row, "active", self->vnc_nav_row, "secondary-label",
                                 G_BINDING_SYNC_CREATE, on_off_mapping, NULL, NULL, NULL);

    toolbar = adw_toolbar_view_new ();
    adw_toolbar_view_add_top_bar (ADW_TOOLBAR_VIEW (toolbar), adw_header_bar_new ());
    adw_toolbar_view_set_content (ADW_TOOLBAR_VIEW (toolbar), GTK_WIDGET (prefs));
    adw_navigation_page_set_child (ADW_NAVIGATION_PAGE (self), toolbar);

    /* The status only runs while the VNC page shows; the overview's "On" or "Off" is read
     * anew whenever it shows */
    g_signal_connect_swapped (page, "showing", G_CALLBACK (on_showing), self);
    g_signal_connect_swapped (page, "hidden", G_CALLBACK (on_hidden), self);
    g_signal_connect_swapped (self, "showing", G_CALLBACK (sync_panel), self);
    g_signal_connect_swapped (self->enabled_row, "notify::active", G_CALLBACK (on_enabled_changed), self);
    g_signal_connect_swapped (self->disconnect_button, "clicked", G_CALLBACK (on_disconnect_clicked), self);
    for (int i = 0; i < N_NETS; i++)
        g_signal_connect (self->net_rows[i], "notify::active", G_CALLBACK (on_net_changed), self);

    sync_panel (self);
}

/* Secure Shell is always there, only the VNC row needs plinius-desktop */
void
cc_desktop_vnc_panel_static_init_func (void)
{
}
