/*
 * cc-plinius-bt-contacts.c
 *
 * Plinius: "Contacts" on the Bluetooth panel, a row at its top that opens a page with a
 * switch for each device that asked for the contacts over Bluetooth (PBAP), as a car does to
 * show who is calling. The Privacy panel has the same row below Cameras.
 *
 * obexd (bluez-obex-plinius, patch 0004) serves the phone book only to devices allowed in
 * $XDG_DATA_HOME/plinius/bluetooth-contacts:
 *
 *   [devices]
 *   AA:BB:CC:DD:EE:FF=true      allowed
 *   11:22:33:44:55:66=false     refused
 *
 * A device not listed there is refused, and plinius-pbap-ask asks the user in a notification;
 * the answer goes into the same file. This page shows the file and changes single entries: it
 * reads the file again before each change, so an answer given meanwhile is kept. obexd reads
 * the file at each connection, so a change applies when the device connects next.
 *
 * The names come from BlueZ (Device1.Alias); a device BlueZ no longer knows shows its address.
 * Nothing of this shows without bluez-obex-plinius. Our own strings use the gettext domain
 * plinius-control-center (not GNOME's po/).
 *
 * SPDX-License-Identifier: MIT
 */

#undef G_LOG_DOMAIN
#define G_LOG_DOMAIN "cc-plinius-bt-contacts"

#include "cc-plinius-bt-contacts.h"

#include <errno.h>
#include <string.h>
#include <glib/gstdio.h>

#define PLINIUS_DOMAIN "plinius-control-center"
#define P_(String) g_dgettext (PLINIUS_DOMAIN, String)

/* Installed by bluez-obex-plinius together with the obexd that reads the file */
#define PBAP_ASK "/usr/lib/bluetooth/plinius-pbap-ask"
#define GROUP "devices"

static char *
store_path (void)
{
    return g_build_filename (g_get_user_data_dir (), "plinius", "bluetooth-contacts", NULL);
}

static gboolean
valid_address (const char *address)
{
    if (strlen (address) != 17)
        return FALSE;
    for (guint i = 0; i < 17; i++) {
        if (i % 3 == 2 ? address[i] != ':' : !g_ascii_isxdigit (address[i]) || g_ascii_islower (address[i]))
            return FALSE;
    }
    return TRUE;
}

/* Set ADDRESS to ALLOWED in the file as it is now, readable by the user only (as
 * plinius-pbap-ask writes it) */
static gboolean
store_set (const char *address, gboolean allowed, GError **error)
{
    g_autofree char *path = store_path ();
    g_autofree char *dir = g_path_get_dirname (path);
    g_autoptr(GKeyFile) kf = g_key_file_new ();
    g_autoptr(GError) load_error = NULL;
    g_autofree char *data = NULL;
    gsize length;

    if (!g_key_file_load_from_file (kf, path, G_KEY_FILE_KEEP_COMMENTS, &load_error) &&
        !g_error_matches (load_error, G_FILE_ERROR, G_FILE_ERROR_NOENT)) {
        /* A file that does not parse is not overwritten: it may hold decisions */
        g_propagate_error (error, g_steal_pointer (&load_error));
        return FALSE;
    }
    g_key_file_set_boolean (kf, GROUP, address, allowed);
    data = g_key_file_to_data (kf, &length, NULL);
    if (g_mkdir_with_parents (dir, 0700) != 0) {
        int saved = errno;

        g_set_error (error, G_FILE_ERROR, g_file_error_from_errno (saved), "%s: %s", dir, g_strerror (saved));
        return FALSE;
    }
    return g_file_set_contents_full (path, data, length, G_FILE_SET_CONTENTS_CONSISTENT, 0600, error);
}

typedef struct {
    AdwPreferencesGroup *group;
    GPtrArray *rows;
    /* address -> alias, from BlueZ */
    GHashTable *names;
} Page;

static void
page_free (Page *page)
{
    g_clear_pointer (&page->rows, g_ptr_array_unref);
    g_clear_pointer (&page->names, g_hash_table_unref);
    g_free (page);
}

typedef struct {
    char *address;
    char *name;
    gboolean allowed;
} Entry;

static void
entry_free (Entry *entry)
{
    g_free (entry->address);
    g_free (entry->name);
    g_free (entry);
}

static int
entry_compare (gconstpointer a, gconstpointer b)
{
    const Entry *x = *(Entry **) a, *y = *(Entry **) b;

    return g_utf8_collate (x->name, y->name);
}

static void switch_changed (AdwSwitchRow *row, GParamSpec *pspec, gpointer data);

/* Sets the row back to what the file still says, once GTK is done with the user's change */
static gboolean
switch_revert_cb (gpointer data)
{
    AdwSwitchRow *row = data;

    g_signal_handlers_block_by_func (row, switch_changed, NULL);
    adw_switch_row_set_active (row, GPOINTER_TO_INT (g_object_get_data (G_OBJECT (row), "plinius-kept")));
    g_signal_handlers_unblock_by_func (row, switch_changed, NULL);
    return G_SOURCE_REMOVE;
}

static void
switch_changed (AdwSwitchRow *row, GParamSpec *pspec, gpointer data)
{
    const char *address = g_object_get_data (G_OBJECT (row), "plinius-address");
    gboolean active = adw_switch_row_get_active (row);
    g_autoptr(GError) error = NULL;

    if (store_set (address, active, &error))
        return;
    g_warning ("Could not keep the contacts permission of %s: %s", address, error->message);
    /* Not inside this handler: AdwSwitchRow would notify again at once, endlessly */
    g_object_set_data (G_OBJECT (row), "plinius-kept", GINT_TO_POINTER (!active));
    g_idle_add_full (G_PRIORITY_DEFAULT_IDLE, switch_revert_cb, g_object_ref (row), g_object_unref);
}

/* The rows from the file as it is now, named after BlueZ' devices */
static void
page_fill (Page *page)
{
    g_autofree char *path = store_path ();
    g_autoptr(GKeyFile) kf = g_key_file_new ();
    g_auto(GStrv) keys = NULL;
    g_autoptr(GPtrArray) entries = g_ptr_array_new_with_free_func ((GDestroyNotify) entry_free);

    for (guint i = 0; i < page->rows->len; i++)
        adw_preferences_group_remove (page->group, g_ptr_array_index (page->rows, i));
    g_ptr_array_set_size (page->rows, 0);

    if (g_key_file_load_from_file (kf, path, G_KEY_FILE_NONE, NULL))
        keys = g_key_file_get_keys (kf, GROUP, NULL, NULL);
    for (guint i = 0; keys && keys[i]; i++) {
        g_autoptr(GError) error = NULL;
        gboolean allowed = g_key_file_get_boolean (kf, GROUP, keys[i], &error);
        const char *name = page->names ? g_hash_table_lookup (page->names, keys[i]) : NULL;
        Entry *entry;

        /* obexd takes only these; anything else would be a switch that does nothing */
        if (error || !valid_address (keys[i]))
            continue;
        entry = g_new0 (Entry, 1);
        entry->address = g_strdup (keys[i]);
        entry->name = g_strdup (name && *name ? name : keys[i]);
        entry->allowed = allowed;
        g_ptr_array_add (entries, entry);
    }
    g_ptr_array_sort (entries, entry_compare);

    for (guint i = 0; i < entries->len; i++) {
        Entry *entry = g_ptr_array_index (entries, i);
        AdwSwitchRow *row = ADW_SWITCH_ROW (adw_switch_row_new ());

        adw_preferences_row_set_use_markup (ADW_PREFERENCES_ROW (row), FALSE);
        adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), entry->name);
        if (!g_str_equal (entry->name, entry->address))
            adw_action_row_set_subtitle (ADW_ACTION_ROW (row), entry->address);
        adw_switch_row_set_active (row, entry->allowed);
        g_object_set_data_full (G_OBJECT (row), "plinius-address", g_strdup (entry->address), g_free);
        g_signal_connect (row, "notify::active", G_CALLBACK (switch_changed), NULL);
        adw_preferences_group_add (page->group, GTK_WIDGET (row));
        g_ptr_array_add (page->rows, row);
    }
    if (entries->len == 0) {
        GtkWidget *row = adw_action_row_new ();

        adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_("No device has asked for the contacts yet"));
        gtk_widget_add_css_class (row, "dim-label");
        adw_preferences_group_add (page->group, row);
        g_ptr_array_add (page->rows, row);
    }
}

/* BlueZ' answer: the names of the devices it knows, then the rows (with addresses only if
 * BlueZ does not answer) */
static void
names_received (GObject *source, GAsyncResult *result, gpointer data)
{
    g_autoptr(AdwPreferencesGroup) group = data;
    g_autoptr(GVariant) reply = g_dbus_connection_call_finish (G_DBUS_CONNECTION (source), result, NULL);
    Page *page = g_object_get_data (G_OBJECT (group), "plinius-page");
    g_autoptr(GVariantIter) objects = NULL;
    GVariant *interfaces;

    if (page == NULL)
        return;
    if (reply) {
        g_variant_get (reply, "(a{oa{sa{sv}}})", &objects);
        while (g_variant_iter_loop (objects, "{&o@a{sa{sv}}}", NULL, &interfaces)) {
            g_autoptr(GVariant) device = g_variant_lookup_value (interfaces, "org.bluez.Device1", G_VARIANT_TYPE_VARDICT);
            const char *address = NULL, *alias = NULL;

            if (device && g_variant_lookup (device, "Address", "&s", &address) &&
                g_variant_lookup (device, "Alias", "&s", &alias) &&
                !g_hash_table_contains (page->names, address))
                g_hash_table_insert (page->names, g_strdup (address), g_strdup (alias));
        }
    }
    page_fill (page);
}

static void
page_showing (AdwNavigationPage *navigation_page, gpointer data)
{
    AdwPreferencesGroup *group = data;
    Page *page = g_object_get_data (G_OBJECT (group), "plinius-page");
    g_autoptr(GDBusConnection) bus = g_bus_get_sync (G_BUS_TYPE_SYSTEM, NULL, NULL);

    /* A notification may have been answered since the page was last shown */
    if (bus == NULL) {
        page_fill (page);
        return;
    }
    g_hash_table_remove_all (page->names);
    g_dbus_connection_call (bus, "org.bluez", "/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects", NULL,
                            G_VARIANT_TYPE ("(a{oa{sa{sv}}})"), G_DBUS_CALL_FLAGS_NONE, 5000, NULL, names_received,
                            g_object_ref (group));
}

static AdwNavigationPage *
contacts_page_new (void)
{
    GtkWidget *toolbar = adw_toolbar_view_new ();
    GtkWidget *prefs = adw_preferences_page_new ();
    AdwPreferencesGroup *group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    AdwNavigationPage *navigation_page = adw_navigation_page_new (toolbar, P_("Contacts"));
    Page *page = g_new0 (Page, 1);

    page->group = group;
    page->rows = g_ptr_array_new ();
    page->names = g_hash_table_new_full (g_str_hash, g_str_equal, g_free, g_free);
    g_object_set_data_full (G_OBJECT (group), "plinius-page", page, (GDestroyNotify) page_free);

    adw_preferences_group_set_title (group, P_("Devices"));
    adw_preferences_group_set_description (
        group, P_("Devices switched on here may read your contacts over Bluetooth, a car for example to show who is "
                  "calling. A device that is not listed asks first. Changes apply when the device connects again."));
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (prefs), group);
    adw_toolbar_view_add_top_bar (ADW_TOOLBAR_VIEW (toolbar), adw_header_bar_new ());
    adw_toolbar_view_set_content (ADW_TOOLBAR_VIEW (toolbar), prefs);
    g_signal_connect (navigation_page, "showing", G_CALLBACK (page_showing), group);
    return navigation_page;
}

static void
row_activated (AdwActionRow *row, CcPanel *panel)
{
    cc_panel_push_subpage (panel, contacts_page_new ());
}

/* The row to the page; on the Privacy page it says that it is about Bluetooth */
GtkWidget *
cc_plinius_bt_contacts_row_new (CcPanel *panel, gboolean on_privacy)
{
    GtkWidget *row;

    if (!g_file_test (PBAP_ASK, G_FILE_TEST_IS_EXECUTABLE))
        return NULL;
    row = adw_action_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_("Contacts"));
    adw_action_row_set_subtitle (ADW_ACTION_ROW (row), on_privacy ? P_("Which Bluetooth devices may read them")
                                                                  : P_("Which devices may read them"));
    adw_action_row_add_prefix (ADW_ACTION_ROW (row), gtk_image_new_from_icon_name ("x-office-address-book-symbolic"));
    adw_action_row_add_suffix (ADW_ACTION_ROW (row), gtk_image_new_from_icon_name ("go-next-symbolic"));
    gtk_list_box_row_set_activatable (GTK_LIST_BOX_ROW (row), TRUE);
    g_signal_connect (row, "activated", G_CALLBACK (row_activated), panel);
    return row;
}

/* Below the header bar, so that it shows also with Bluetooth off */
void
cc_plinius_bt_contacts_add (CcPanel *panel, AdwToolbarView *toolbar)
{
    GtkWidget *list, *row;

    row = cc_plinius_bt_contacts_row_new (panel, FALSE);
    if (row == NULL)
        return;
    list = gtk_list_box_new ();
    gtk_list_box_set_selection_mode (GTK_LIST_BOX (list), GTK_SELECTION_NONE);
    gtk_widget_add_css_class (list, "boxed-list");
    gtk_widget_set_margin_start (list, 12);
    gtk_widget_set_margin_end (list, 12);
    gtk_widget_set_margin_top (list, 12);
    gtk_widget_set_margin_bottom (list, 6);
    gtk_list_box_append (GTK_LIST_BOX (list), row);
    adw_toolbar_view_add_top_bar (toolbar, list);
}
