/*
 * cc-plinius-appaccess.c: the group "Access" on an app's page (Apps › app): whether the app may
 * use a camera, the microphone, a security key and the network - Ask, Allow or Don't Allow - and
 * how often it used them or was blocked, from plinius-appaccessd (org.plinius.AppAccess1 on the
 * system bus). The group is the page's own group of Required Permissions: the four rows go on top
 * of it, the page's rows stay below them. Without the daemon the group is as the page has it.
 *
 * Camera: the phone's own cameras reach the apps through the camera portal, which keeps its own
 * answer per app (permission store, table devices, id camera: yes/no, none = it asks); cameras on
 * USB through plinius-appaccess. The row sets both; an answer the portal already had (given to its
 * own question) is taken over. The uses of the phone's cameras are counted by camera-hal-bridge.
 * For Flatpak apps GNOME's own camera switch is hidden, it is the same answer.
 *
 * Microphone: WirePlumber gives an app the audio sources by plinius-appaccessd's verdict (the hook
 * of plinius-appaccess), Flatpak apps too. GNOME's own microphone switch of Flatpak apps (the
 * permission store's answer for the device portal, which PipeWire does not ask) is hidden while
 * the row shows.
 *
 * The app is the one systemd names its scope after: the desktop file's id, for Flatpak the app id
 * (X-Flatpak). Changes take effect at once, also for the app's running instances; the page follows
 * changes made elsewhere (the agent's question) through the daemon's signals.
 *
 * Network: the attempts plinius-appaccess blocked, and the app's connections of the last 24 hours
 * from plinius-netlogd (org.plinius.NetLog1, the same app ids), when it answers.
 *
 * Our own strings use the gettext domain plinius-control-center (not GNOME's po/).
 *
 * SPDX-License-Identifier: MIT
 */

#include <config.h>

#include <adwaita.h>
#include <gio/gdesktopappinfo.h>
#include <glib/gi18n.h>

#include "cc-plinius-appaccess.h"

#define PLINIUS_DOMAIN "plinius-control-center"
#define P_(String) g_dgettext (PLINIUS_DOMAIN, String)
#define PN_(Singular, Plural, N) g_dngettext (PLINIUS_DOMAIN, Singular, Plural, N)

#define AA_NAME      "org.plinius.AppAccess1"
#define AA_PATH      "/org/plinius/AppAccess1"
#define AA_INTERFACE "org.plinius.AppAccess1"

#define NETLOG_NAME      "org.plinius.NetLog1"
#define NETLOG_PATH      "/org/plinius/NetLog1"
#define NETLOG_INTERFACE "org.plinius.NetLog1"
/* The connections shown: those of the last 24 hours */
#define NETLOG_SECONDS   86400

#define PERM_STORE_NAME      "org.freedesktop.impl.portal.PermissionStore"
#define PERM_STORE_PATH      "/org/freedesktop/impl/portal/PermissionStore"
#define PERM_STORE_INTERFACE "org.freedesktop.impl.portal.PermissionStore"

enum { RES_CAMERA, RES_MICROPHONE, RES_KEY, RES_NETWORK, RES_MAX };

/* The daemon's names; the rows' choices are its policies in this order: 0 ask, 1 allow, 2 deny */
static const char *const res_names[RES_MAX] = { "camera", "microphone", "security-key", "network" };

typedef struct {
    CcPanel         *panel;  /* owns this */
    GDBusConnection *bus;
    GDBusConnection *session;      /* the portal's permission store */
    GtkWidget       *group;        /* the page's group the rows are in */
    GPtrArray       *others;       /* the page's rows in that group */
    char            *title;        /* the group's own title and description */
    char            *description;
    GtkWidget       *gnome_camera; /* GNOME's camera switch of Flatpak apps */
    GtkWidget       *gnome_microphone; /* … and its microphone switch */
    AdwComboRow     *rows[RES_MAX];
    guint32          allowed[RES_MAX], denied[RES_MAX];  /* the daemon's counts */
    gint64           last_allowed[RES_MAX], last_denied[RES_MAX];
    gint64           connections;  /* plinius-netlogd's, -1 unknown */
    char            *app;
    gboolean         updating;
    GCancellable    *cancellable;
    guint            signal_ids[2];
} Access;

static void fetch (Access *a);
static void show_access (Access *a, gboolean shown);
static void portal_fetch (Access *a);
static void on_set (GObject *source, GAsyncResult *result, gpointer user_data);

static void
access_free (Access *a)
{
    g_cancellable_cancel (a->cancellable);
    g_clear_object (&a->cancellable);
    for (guint i = 0; i < G_N_ELEMENTS (a->signal_ids); i++)
        if (a->signal_ids[i])
            g_dbus_connection_signal_unsubscribe (a->bus, a->signal_ids[i]);
    g_clear_object (&a->bus);
    g_clear_object (&a->session);
    g_ptr_array_unref (a->others);
    g_free (a->title);
    g_free (a->description);
    g_free (a->app);
    g_free (a);
}

static int
res_from_name (const char *name)
{
    for (int i = 0; i < RES_MAX; i++)
        if (g_str_equal (name, res_names[i]))
            return i;
    return -1;
}

/* The rows and the title "Access" while plinius-appaccess answers, else the group as the page has
 * it: its own title, shown while one of its rows shows */
static void
show_access (Access *a, gboolean shown)
{
    gboolean visible = shown;

    for (int i = 0; i < RES_MAX; i++)
        gtk_widget_set_visible (GTK_WIDGET (a->rows[i]), shown);
    adw_preferences_group_set_title (ADW_PREFERENCES_GROUP (a->group), shown ? P_("Access") : a->title);
    adw_preferences_group_set_description (ADW_PREFERENCES_GROUP (a->group),
                                           shown ? P_("With “Ask”, the app is blocked when it tries, and a "
                                                      "notification asks whether to allow it.")
                                                 : a->description);
    for (guint i = 0; i < a->others->len && !visible; i++)
        visible = gtk_widget_get_visible (g_ptr_array_index (a->others, i));
    gtk_widget_set_visible (a->group, visible);
}

/* "14:02" today, else the date and time in the locale's way */
static char *
format_when (gint64 unix_time)
{
    g_autoptr(GDateTime) when = g_date_time_new_from_unix_local (unix_time);
    g_autoptr(GDateTime) now = g_date_time_new_now_local ();
    int y1, m1, d1, y2, m2, d2;

    if (!when)
        return g_strdup ("");
    g_date_time_get_ymd (when, &y1, &m1, &d1);
    g_date_time_get_ymd (now, &y2, &m2, &d2);
    if (y1 == y2 && m1 == m2 && d1 == d2)
        return g_date_time_format (when, "%R");
    return g_date_time_format (when, "%x %R");
}

static char *
row_subtitle (Access *a, int res)
{
    g_autoptr(GPtrArray) parts = g_ptr_array_new_with_free_func (g_free);
    guint32 allowed = a->allowed[res], denied = a->denied[res];

    if (denied) {
        g_autofree char *when = format_when (a->last_denied[res]);

        /* TRANSLATORS: %u is a number, %s a time ("14:02") or a date and time */
        g_ptr_array_add (parts, g_strdup_printf (PN_ ("%u attempt blocked, last %s",
                                                      "%u attempts blocked, last %s", denied),
                                                 denied, when));
    }
    /* Allowed network use: the connections plinius-netlogd saw (the daemon does not count it) */
    if (res == RES_NETWORK && a->connections > 0) {
        guint n = (guint) MIN (a->connections, G_MAXUINT);

        /* TRANSLATORS: %u is a number */
        g_ptr_array_add (parts, g_strdup_printf (PN_ ("%u connection in the last 24 hours",
                                                      "%u connections in the last 24 hours", n), n));
    }
    if (allowed && res != RES_NETWORK) {
        g_autofree char *when = format_when (a->last_allowed[res]);

        /* TRANSLATORS: %u is a number, %s a time ("14:02") or a date and time */
        g_ptr_array_add (parts, g_strdup_printf (PN_ ("Used %u time, last %s", "Used %u times, last %s", allowed),
                                                 allowed, when));
    }
    if (parts->len == 0) {
        if (res != RES_NETWORK)
            return g_strdup (P_("Not used yet"));
        return g_strdup (a->connections == 0 ? P_("No connections in the last 24 hours") : P_("No blocked attempts"));
    }
    g_ptr_array_add (parts, NULL);
    return g_strjoinv (" · ", (char **) parts->pdata);
}

static void
update_subtitle (Access *a, int res)
{
    g_autofree char *subtitle = row_subtitle (a, res);

    adw_action_row_set_subtitle (ADW_ACTION_ROW (a->rows[res]), subtitle);
}

/* The app's connections: the counts of its rows summed up */
static void
on_netlog_got (GObject *source, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(GVariant) reply = NULL;
    g_autoptr(GVariantIter) iter = NULL;
    g_autoptr(GError) error = NULL;
    const char *app;
    guint32 count;
    gint64 sum = 0;
    Access *a;

    reply = g_dbus_connection_call_finish (G_DBUS_CONNECTION (source), result, &error);
    if (g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
        return;
    a = user_data;
    /* No plinius-netlogd: the row shows the blocked attempts only */
    if (!reply) {
        g_debug ("plinius-netlog: %s", error->message);
        return;
    }
    g_variant_get (reply, "(a(ssuqssuuxx))", &iter);
    while (g_variant_iter_next (iter, "(&ssuqssuuxx)", &app, NULL, NULL, NULL, NULL, NULL, NULL, &count, NULL, NULL))
        if (g_str_equal (app, a->app))
            sum += count;
    a->connections = sum;
    update_subtitle (a, RES_NETWORK);
}

static void
netlog_fetch (Access *a)
{
    g_dbus_connection_call (a->bus, NETLOG_NAME, NETLOG_PATH, NETLOG_INTERFACE, "Query",
                            g_variant_new ("(u)", NETLOG_SECONDS), G_VARIANT_TYPE ("(a(ssuqssuuxx))"),
                            G_DBUS_CALL_FLAGS_NONE, -1, a->cancellable, on_netlog_got, a);
}

static void
on_got (GObject *source, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(GVariant) reply = NULL;
    g_autoptr(GVariantIter) iter = NULL;
    g_autoptr(GError) error = NULL;
    const char *name;
    guint32 policy, allowed, denied;
    gint64 last_allowed, last_denied;
    Access *a;

    reply = g_dbus_connection_call_finish (G_DBUS_CONNECTION (source), result, &error);
    if (g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
        return;
    a = user_data;
    if (!reply) {
        /* No plinius-appaccess, or it may not be asked: nothing to show */
        g_debug ("plinius-appaccess: %s", error->message);
        show_access (a, FALSE);
        return;
    }
    a->updating = TRUE;
    g_variant_get (reply, "(a(suuuxx))", &iter);
    while (g_variant_iter_next (iter, "(&suuuxx)", &name, &policy, &allowed, &denied, &last_allowed, &last_denied)) {
        int res = res_from_name (name);

        if (res < 0 || policy > 2)
            continue;
        adw_combo_row_set_selected (a->rows[res], policy);
        a->allowed[res] = allowed;
        a->denied[res] = denied;
        a->last_allowed[res] = last_allowed;
        a->last_denied[res] = last_denied;
        update_subtitle (a, res);
    }
    a->updating = FALSE;
    show_access (a, TRUE);
    if (a->gnome_camera)
        gtk_widget_set_visible (a->gnome_camera, FALSE);
    if (a->gnome_microphone)
        gtk_widget_set_visible (a->gnome_microphone, FALSE);
    portal_fetch (a);
    netlog_fetch (a);
}

/* The portal's answer for the app's camera: "yes" Allow, "no" Don't Allow; the row follows it and
 * plinius-appaccess is told, so that cameras on USB get the same */
static void
on_portal_got (GObject *source, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(GVariant) reply = NULL;
    g_autoptr(GError) error = NULL;
    g_autofree const char **values = NULL;
    Access *a;
    guint policy;

    reply = g_dbus_connection_call_finish (G_DBUS_CONNECTION (source), result, &error);
    if (g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
        return;
    a = user_data;
    /* No entry for the app (the portal will ask) or no permission store: the row stays */
    if (!reply)
        return;
    g_variant_get (reply, "(^a&s)", &values);
    if (!values || !values[0])
        return;
    policy = g_str_equal (values[0], "yes") ? 1 : g_str_equal (values[0], "no") ? 2 : 0;
    if (policy == 0 || adw_combo_row_get_selected (a->rows[RES_CAMERA]) == policy)
        return;
    a->updating = TRUE;
    adw_combo_row_set_selected (a->rows[RES_CAMERA], policy);
    a->updating = FALSE;
    g_dbus_connection_call (a->bus, AA_NAME, AA_PATH, AA_INTERFACE, "SetPolicy",
                            g_variant_new ("(ssu)", a->app, res_names[RES_CAMERA], policy), NULL,
                            G_DBUS_CALL_FLAGS_NONE, -1, NULL, on_set, g_object_ref (a->panel));
}

static void
portal_fetch (Access *a)
{
    if (!a->session || !a->app)
        return;
    g_dbus_connection_call (a->session, PERM_STORE_NAME, PERM_STORE_PATH, PERM_STORE_INTERFACE, "GetPermission",
                            g_variant_new ("(sss)", "devices", "camera", a->app), G_VARIANT_TYPE ("(as)"),
                            G_DBUS_CALL_FLAGS_NONE, -1, a->cancellable, on_portal_got, a);
}

/* Ask: no entry (the portal asks); Allow: "yes"; Don't Allow: "no" */
static void
portal_set (Access *a, guint policy)
{
    const char *const yes[] = { "yes", NULL }, *const no[] = { "no", NULL };

    if (!a->session)
        return;
    if (policy == 0)
        g_dbus_connection_call (a->session, PERM_STORE_NAME, PERM_STORE_PATH, PERM_STORE_INTERFACE,
                                "DeletePermission", g_variant_new ("(sss)", "devices", "camera", a->app), NULL,
                                G_DBUS_CALL_FLAGS_NONE, -1, NULL, NULL, NULL);
    else
        g_dbus_connection_call (a->session, PERM_STORE_NAME, PERM_STORE_PATH, PERM_STORE_INTERFACE,
                                "SetPermission",
                                g_variant_new ("(sbss^as)", "devices", TRUE, "camera", a->app,
                                               policy == 1 ? yes : no),
                                NULL, G_DBUS_CALL_FLAGS_NONE, -1, NULL, NULL, NULL);
}

static void
fetch (Access *a)
{
    g_cancellable_cancel (a->cancellable);
    g_clear_object (&a->cancellable);
    if (!a->app || !a->bus) {
        show_access (a, FALSE);
        return;
    }
    a->cancellable = g_cancellable_new ();
    /* What the last answer said is not this one's: counts the daemon leaves out are none */
    memset (a->allowed, 0, sizeof (a->allowed));
    memset (a->denied, 0, sizeof (a->denied));
    memset (a->last_allowed, 0, sizeof (a->last_allowed));
    memset (a->last_denied, 0, sizeof (a->last_denied));
    a->connections = -1;
    g_dbus_connection_call (a->bus, AA_NAME, AA_PATH, AA_INTERFACE, "Get", g_variant_new ("(s)", a->app),
                            G_VARIANT_TYPE ("(a(suuuxx))"), G_DBUS_CALL_FLAGS_NONE, -1, a->cancellable,
                            on_got, a);
}

/* @user_data: a reference to the panel; the change is never cancelled, the page may be gone */
static void
on_set (GObject *source, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(CcPanel) panel = user_data;
    g_autoptr(GVariant) reply = NULL;
    g_autoptr(GError) error = NULL;
    Access *a;

    reply = g_dbus_connection_call_finish (G_DBUS_CONNECTION (source), result, &error);
    if (reply)
        return;
    /* Refused: show again what holds */
    g_warning ("plinius-appaccess: cannot change the policy: %s", error->message);
    a = g_object_get_data (G_OBJECT (panel), "plinius-appaccess");
    if (a)
        fetch (a);
}

static void
on_selected (AdwComboRow *row, GParamSpec *pspec, Access *a)
{
    int res = GPOINTER_TO_INT (g_object_get_data (G_OBJECT (row), "resource"));
    guint policy = adw_combo_row_get_selected (row);

    if (a->updating || !a->app || policy > 2)
        return;
    if (res == RES_CAMERA)
        portal_set (a, policy);
    g_dbus_connection_call (a->bus, AA_NAME, AA_PATH, AA_INTERFACE, "SetPolicy",
                            g_variant_new ("(ssu)", a->app, res_names[res], policy), NULL,
                            G_DBUS_CALL_FLAGS_NONE, -1, NULL, on_set, g_object_ref (a->panel));
}

/* Changed (s app), AccessDenied (s app, …): the page shows that app, read it again */
static void
on_signal (GDBusConnection *bus, const char *sender, const char *path, const char *iface,
           const char *signal_name, GVariant *params, gpointer user_data)
{
    Access *a = user_data;
    const char *app = NULL;

    if (g_variant_n_children (params) < 1)
        return;
    g_variant_get_child (params, 0, "&s", &app);
    if (a->app && g_str_equal (app, a->app) && gtk_widget_get_mapped (GTK_WIDGET (a->rows[0])))
        fetch (a);
}

/* The id systemd names the app's scope after */
static char *
app_id_of (GAppInfo *info)
{
    const char *id;

    if (G_IS_DESKTOP_APP_INFO (info)) {
        char *flatpak = g_desktop_app_info_get_string (G_DESKTOP_APP_INFO (info), "X-Flatpak");

        if (flatpak)
            return flatpak;
    }
    id = g_app_info_get_id (info);
    if (!id)
        return NULL;
    return g_str_has_suffix (id, ".desktop") ? g_strndup (id, strlen (id) - strlen (".desktop")) : g_strdup (id);
}

static Access *
access_new (CcPanel *panel, GtkWidget *group)
{
    static const char *const titles[RES_MAX] = { N_("Camera"), N_("Microphone"), N_("Security Key"), N_("Network") };
    const char *choices[] = { P_("Ask"), P_("Allow"), P_("Don't Allow"), NULL };
    Access *a = g_new0 (Access, 1);
    GtkWidget *row;

    a->panel = panel;

    a->bus = g_bus_get_sync (G_BUS_TYPE_SYSTEM, NULL, NULL);
    a->session = g_bus_get_sync (G_BUS_TYPE_SESSION, NULL, NULL);
    a->group = group;
    a->title = g_strdup (adw_preferences_group_get_title (ADW_PREFERENCES_GROUP (group)));
    a->description = g_strdup (adw_preferences_group_get_description (ADW_PREFERENCES_GROUP (group)));
    /* The page's rows go below ours: out of the group and in again after them */
    a->others = g_ptr_array_new_with_free_func (g_object_unref);
    for (guint i = 0; (row = adw_preferences_group_get_row (ADW_PREFERENCES_GROUP (group), i)); i++)
        g_ptr_array_add (a->others, g_object_ref (row));
    for (guint i = 0; i < a->others->len; i++)
        adw_preferences_group_remove (ADW_PREFERENCES_GROUP (group), g_ptr_array_index (a->others, i));
    for (int i = 0; i < RES_MAX; i++) {
        g_autoptr(GtkStringList) model = gtk_string_list_new (choices);

        a->rows[i] = ADW_COMBO_ROW (adw_combo_row_new ());
        adw_preferences_row_set_title (ADW_PREFERENCES_ROW (a->rows[i]), P_(titles[i]));
        adw_combo_row_set_model (a->rows[i], G_LIST_MODEL (model));
        gtk_widget_set_visible (GTK_WIDGET (a->rows[i]), FALSE);
        g_object_set_data (G_OBJECT (a->rows[i]), "resource", GINT_TO_POINTER (i));
        g_signal_connect (a->rows[i], "notify::selected", G_CALLBACK (on_selected), a);
        adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), GTK_WIDGET (a->rows[i]));
    }
    for (guint i = 0; i < a->others->len; i++)
        adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), g_ptr_array_index (a->others, i));
    if (a->bus) {
        a->signal_ids[0] = g_dbus_connection_signal_subscribe (a->bus, AA_NAME, AA_INTERFACE, "Changed", AA_PATH,
                                                               NULL, G_DBUS_SIGNAL_FLAGS_NONE, on_signal, a, NULL);
        a->signal_ids[1] = g_dbus_connection_signal_subscribe (a->bus, AA_NAME, AA_INTERFACE, "AccessDenied",
                                                               AA_PATH, NULL, G_DBUS_SIGNAL_FLAGS_NONE, on_signal,
                                                               a, NULL);
    }
    g_object_set_data_full (G_OBJECT (panel), "plinius-appaccess", a, (GDestroyNotify) access_free);
    return a;
}

/**
 * cc_plinius_appaccess_update_app_page:
 * @panel: the Apps panel
 * @info: the app the page is about
 * @group: the page's group of Required Permissions, its rows already set for the app; it becomes
 *   the group "Access", the four rows on top
 * @gnome_camera: GNOME's camera switch of Flatpak apps, hidden while "Access" shows
 * @gnome_microphone: GNOME's microphone switch of Flatpak apps, hidden as well
 *
 * Shows plinius-appaccessd's policies and counts for the app (the group as the page has it
 * without the daemon).
 */
void
cc_plinius_appaccess_update_app_page (CcPanel *panel, GAppInfo *info, GtkWidget *group, GtkWidget *gnome_camera,
                                      GtkWidget *gnome_microphone)
{
    Access *a = g_object_get_data (G_OBJECT (panel), "plinius-appaccess");

    if (!a)
        a = access_new (panel, group);
    a->gnome_camera = gnome_camera;
    a->gnome_microphone = gnome_microphone;
    g_free (a->app);
    a->app = app_id_of (info);
    show_access (a, FALSE);
    fetch (a);
}
