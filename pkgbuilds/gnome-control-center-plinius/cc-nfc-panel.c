/*
 * cc-nfc-panel.c
 *
 * Plinius: "NFC", a panel of its own in the sidebar, to switch NFC on and off. nfcd does the
 * work (org.sailfishos.nfc.Settings); it lets only the group "privileged" switch NFC. The panel
 * is only listed when nfcd's settings service is installed.
 * Below the switch: the own contact card for the quick setting "Contact Card" (plinius-cardshare),
 * a subpage to choose the contact, the fields that go out and how long sharing runs
 * (org.plinius.cardshare). The contacts come from plinius-cardshare (org.plinius.CardShare on the
 * session bus), which reads the address books.
 * Our own strings use the gettext domain plinius-control-center (not GNOME's po/).
 *
 * SPDX-License-Identifier: MIT
 */

#include "config.h"

#include <adwaita.h>
#include <glib/gi18n.h>
#include <string.h>

#include "cc-nfc-panel.h"
#include "shell/cc-application.h"

#define PLINIUS_DOMAIN "plinius-control-center"
#define P_(String) g_dgettext (PLINIUS_DOMAIN, String)

#define NFC_SETTINGS_NAME "org.sailfishos.nfc.settings"
#define NFC_SETTINGS_PATH "/"
#define NFC_SETTINGS_INTERFACE "org.sailfishos.nfc.Settings"
#define NFC_SERVICE_POLICY "/usr/share/dbus-1/system.d/org.sailfishos.nfc.settings.conf"

#define CARDSHARE_SCHEMA "org.plinius.cardshare"
#define CARDSHARE_NAME "org.plinius.CardShare"
#define CARDSHARE_PATH "/org/plinius/CardShare"
#define CARDSHARE_INTERFACE "org.plinius.CardShare"

/* N_(): marked for translation (xgettext -k N_), looked up with P_() where shown */

/* Fields of org.plinius.cardshare.fields besides the name, which always goes out */
static const struct {
    const char *key;
    const char *title;
    const char *subtitle;
} card_fields[] = {
    { "phone", N_("Phone Numbers"), NULL },
    { "email", N_("Email Addresses"), NULL },
    { "organization", N_("Organization and Job Title"), NULL },
    { "address", N_("Addresses"), NULL },
    { "website", N_("Websites"), NULL },
    { "birthday", N_("Birthday"), NULL },
    { "note", N_("Notes"), NULL },
    { "photo", N_("Photo"), N_("Only over NFC, never in the QR code") },
};

struct _CcNfcPanel {
    CcPanel parent_instance;

    AdwSwitchRow *switch_row;
    AdwToastOverlay *toast_overlay;
    GDBusProxy *proxy;
    GCancellable *cancellable;
    gboolean updating;

    /* Contact card (plinius-cardshare); NULL settings: not installed */
    GSettings *card_settings;
    AdwActionRow *card_row;
    AdwActionRow *contact_row;
    AdwSwitchRow *field_rows[G_N_ELEMENTS (card_fields)];
    AdwSpinRow *seconds_row;
    AdwPreferencesGroup *contacts_group;
    GtkSearchEntry *contacts_search;
    GtkWidget *contacts_status;
    GPtrArray *contact_rows;
    gboolean card_updating;
};

G_DEFINE_TYPE (CcNfcPanel, cc_nfc_panel, CC_TYPE_PANEL)

static void
set_enabled_row (CcNfcPanel *self, gboolean enabled)
{
    self->updating = TRUE;
    adw_switch_row_set_active (self->switch_row, enabled);
    self->updating = FALSE;
}

static void
get_enabled_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(GError) error = NULL;
    g_autoptr(GVariant) reply = g_dbus_proxy_call_finish (G_DBUS_PROXY (source), result, &error);
    gboolean enabled;

    if (!reply) {
        if (!g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
            g_warning ("Reading the NFC state failed: %s", error->message);
        return;
    }
    g_variant_get (reply, "(b)", &enabled);
    set_enabled_row (CC_NFC_PANEL (user_data), enabled);
}

static void
on_name_owner_changed (CcNfcPanel *self)
{
    g_autofree char *owner = g_dbus_proxy_get_name_owner (self->proxy);

    gtk_widget_set_sensitive (GTK_WIDGET (self->switch_row), owner != NULL);
    if (owner)
        g_dbus_proxy_call (self->proxy, "GetEnabled", NULL, G_DBUS_CALL_FLAGS_NONE, -1, self->cancellable,
                           get_enabled_cb, self);
}

static void
on_signal (CcNfcPanel *self, const char *sender, const char *signal, GVariant *params)
{
    gboolean enabled;

    if (g_strcmp0 (signal, "EnabledChanged") != 0)
        return;
    g_variant_get (params, "(b)", &enabled);
    set_enabled_row (self, enabled);
}

static void
set_enabled_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(GError) error = NULL;
    g_autoptr(GVariant) reply = g_dbus_proxy_call_finish (G_DBUS_PROXY (source), result, &error);
    CcNfcPanel *self;

    if (reply || g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
        return;

    self = CC_NFC_PANEL (user_data);
    g_warning ("Switching NFC failed: %s", error->message);
    adw_toast_overlay_add_toast (self->toast_overlay, adw_toast_new (P_("Switching NFC failed")));
    /* Show the real state again */
    on_name_owner_changed (self);
}

static void
on_switch_changed (CcNfcPanel *self)
{
    if (self->updating || !self->proxy)
        return;
    g_dbus_proxy_call (self->proxy, "SetEnabled", g_variant_new ("(b)", adw_switch_row_get_active (self->switch_row)),
                       G_DBUS_CALL_FLAGS_NONE, -1, self->cancellable, set_enabled_cb, self);
}

static void
proxy_ready_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(GError) error = NULL;
    GDBusProxy *proxy = g_dbus_proxy_new_for_bus_finish (result, &error);
    CcNfcPanel *self;

    if (!proxy) {
        if (!g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
            g_warning ("Cannot reach nfcd: %s", error->message);
        return;
    }

    self = CC_NFC_PANEL (user_data);
    self->proxy = proxy;
    g_signal_connect_object (proxy, "g-signal", G_CALLBACK (on_signal), self, G_CONNECT_SWAPPED);
    g_signal_connect_object (proxy, "notify::g-name-owner", G_CALLBACK (on_name_owner_changed), self,
                             G_CONNECT_SWAPPED);
    on_name_owner_changed (self);
}

/* ----- Contact card ----- */

static void
card_name_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(GError) error = NULL;
    g_autoptr(GVariant) reply = g_dbus_connection_call_finish (G_DBUS_CONNECTION (source), result, &error);
    g_autoptr(GVariant) value = NULL;
    CcNfcPanel *self;
    const char *name = "";

    if (g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
        return;
    self = CC_NFC_PANEL (user_data);
    if (reply) {
        g_variant_get (reply, "(v)", &value);
        if (g_variant_is_of_type (value, G_VARIANT_TYPE_STRING))
            name = g_variant_get_string (value, NULL);
    } else {
        g_warning ("Reading the contact card failed: %s", error->message);
    }
    adw_action_row_set_subtitle (self->card_row, *name ? name : P_("No card chosen yet"));
    adw_action_row_set_subtitle (self->contact_row, *name ? name : P_("None"));
}

/* Name on the card, from plinius-cardshare (started by the call, it exits when idle) */
static void
update_card_name (CcNfcPanel *self)
{
    g_autoptr(GDBusConnection) bus = g_bus_get_sync (G_BUS_TYPE_SESSION, NULL, NULL);

    if (!bus)
        return;
    g_dbus_connection_call (bus, CARDSHARE_NAME, CARDSHARE_PATH, "org.freedesktop.DBus.Properties", "Get",
                            g_variant_new ("(ss)", CARDSHARE_INTERFACE, "CardName"), G_VARIANT_TYPE ("(v)"),
                            G_DBUS_CALL_FLAGS_NONE, 20000, self->cancellable, card_name_cb, self);
}

static void
update_card_fields (CcNfcPanel *self)
{
    g_auto(GStrv) fields = g_settings_get_strv (self->card_settings, "fields");

    self->card_updating = TRUE;
    for (guint i = 0; i < G_N_ELEMENTS (card_fields); i++)
        adw_switch_row_set_active (self->field_rows[i],
                                   g_strv_contains ((const char * const *) fields, card_fields[i].key));
    self->card_updating = FALSE;
}

static void
on_card_settings_changed (CcNfcPanel *self, const char *key)
{
    if (g_strcmp0 (key, "fields") == 0)
        update_card_fields (self);
    else if (g_strcmp0 (key, "contact-uid") == 0 || g_strcmp0 (key, "contact-book") == 0)
        update_card_name (self);
}

static void
on_field_toggled (CcNfcPanel *self)
{
    g_autoptr(GStrvBuilder) builder = g_strv_builder_new ();
    g_auto(GStrv) fields = NULL;

    if (self->card_updating)
        return;
    g_strv_builder_add (builder, "name");
    for (guint i = 0; i < G_N_ELEMENTS (card_fields); i++) {
        if (adw_switch_row_get_active (self->field_rows[i]))
            g_strv_builder_add (builder, card_fields[i].key);
    }
    fields = g_strv_builder_end (builder);
    g_settings_set_strv (self->card_settings, "fields", (const char * const *) fields);
}

static void
on_contact_activated (CcNfcPanel *self, AdwActionRow *row)
{
    g_settings_set_string (self->card_settings, "contact-book", g_object_get_data (G_OBJECT (row), "book"));
    g_settings_set_string (self->card_settings, "contact-uid", g_object_get_data (G_OBJECT (row), "uid"));
    gtk_widget_activate_action (GTK_WIDGET (row), "navigation.pop", NULL);
}

static void
on_contacts_search_changed (CcNfcPanel *self)
{
    g_autofree char *needle = g_utf8_casefold (gtk_editable_get_text (GTK_EDITABLE (self->contacts_search)), -1);

    if (!self->contact_rows)
        return;
    for (guint i = 0; i < self->contact_rows->len; i++) {
        GtkWidget *row = g_ptr_array_index (self->contact_rows, i);
        const char *key = g_object_get_data (G_OBJECT (row), "key");

        gtk_widget_set_visible (row, !*needle || strstr (key, needle) != NULL);
    }
}

static void
contacts_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(GError) error = NULL;
    g_autoptr(GVariant) reply = g_dbus_connection_call_finish (G_DBUS_CONNECTION (source), result, &error);
    g_autofree char *current = NULL;
    g_autoptr(GVariantIter) iter = NULL;
    const char *book, *uid, *name;
    CcNfcPanel *self;

    if (g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
        return;
    self = CC_NFC_PANEL (user_data);
    if (!reply) {
        g_dbus_error_strip_remote_error (error);
        gtk_label_set_text (GTK_LABEL (self->contacts_status), error->message);
        return;
    }

    current = g_settings_get_string (self->card_settings, "contact-uid");
    g_variant_get (reply, "(a(sss))", &iter);
    while (g_variant_iter_next (iter, "(&s&s&s)", &book, &uid, &name)) {
        GtkWidget *row = adw_action_row_new ();

        adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), name);
        adw_preferences_row_set_use_markup (ADW_PREFERENCES_ROW (row), FALSE);
        gtk_list_box_row_set_activatable (GTK_LIST_BOX_ROW (row), TRUE);
        if (g_strcmp0 (uid, current) == 0)
            adw_action_row_add_suffix (ADW_ACTION_ROW (row), gtk_image_new_from_icon_name ("object-select-symbolic"));
        g_object_set_data_full (G_OBJECT (row), "book", g_strdup (book), g_free);
        g_object_set_data_full (G_OBJECT (row), "uid", g_strdup (uid), g_free);
        g_object_set_data_full (G_OBJECT (row), "key", g_utf8_casefold (name, -1), g_free);
        g_signal_connect_swapped (row, "activated", G_CALLBACK (on_contact_activated), self);
        adw_preferences_group_add (self->contacts_group, row);
        g_ptr_array_add (self->contact_rows, row);
    }
    gtk_widget_set_visible (self->contacts_status, self->contact_rows->len == 0);
    gtk_label_set_text (GTK_LABEL (self->contacts_status), P_("No contacts with a name"));
    on_contacts_search_changed (self);
}

/* The contact list is read anew each time the page shows: contacts change in Contacts */
static void
on_choose_page_showing (CcNfcPanel *self)
{
    g_autoptr(GDBusConnection) bus = g_bus_get_sync (G_BUS_TYPE_SESSION, NULL, NULL);

    if (self->contact_rows) {
        for (guint i = 0; i < self->contact_rows->len; i++)
            adw_preferences_group_remove (self->contacts_group, g_ptr_array_index (self->contact_rows, i));
        g_ptr_array_set_size (self->contact_rows, 0);
    } else {
        self->contact_rows = g_ptr_array_new ();
    }
    gtk_editable_set_text (GTK_EDITABLE (self->contacts_search), "");
    gtk_label_set_text (GTK_LABEL (self->contacts_status), P_("Loading contacts…"));
    gtk_widget_set_visible (self->contacts_status, TRUE);
    if (!bus)
        return;
    g_dbus_connection_call (bus, CARDSHARE_NAME, CARDSHARE_PATH, CARDSHARE_INTERFACE, "ListContacts", NULL,
                            G_VARIANT_TYPE ("(a(sss))"), G_DBUS_CALL_FLAGS_NONE, 30000, self->cancellable,
                            contacts_cb, self);
}

static AdwNavigationPage *
card_navigation_page_new (GtkWidget *content, const char *title, const char *tag)
{
    GtkWidget *toolbar = adw_toolbar_view_new ();

    adw_toolbar_view_add_top_bar (ADW_TOOLBAR_VIEW (toolbar), adw_header_bar_new ());
    adw_toolbar_view_set_content (ADW_TOOLBAR_VIEW (toolbar), content);
    return ADW_NAVIGATION_PAGE (adw_navigation_page_new_with_tag (toolbar, title, tag));
}

static GtkWidget *
push_row_new (const char *title, const char *tag)
{
    GtkWidget *row = adw_action_row_new ();

    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), title);
    adw_action_row_add_suffix (ADW_ACTION_ROW (row), gtk_image_new_from_icon_name ("go-next-symbolic"));
    gtk_list_box_row_set_activatable (GTK_LIST_BOX_ROW (row), TRUE);
    gtk_actionable_set_action_name (GTK_ACTIONABLE (row), "navigation.push");
    gtk_actionable_set_action_target (GTK_ACTIONABLE (row), "s", tag);
    return row;
}

/* Subpage "contact-card": which contact, which fields, how long */
static AdwNavigationPage *
build_card_page (CcNfcPanel *self)
{
    GtkWidget *page = adw_preferences_page_new ();
    GtkWidget *group = adw_preferences_group_new ();
    GtkWidget *fields_group = adw_preferences_group_new ();
    GtkWidget *time_group = adw_preferences_group_new ();

    adw_preferences_group_set_description (ADW_PREFERENCES_GROUP (group),
                                           P_("The contact that goes out when you share your card from the quick settings. Usually your own entry in Contacts."));
    self->contact_row = ADW_ACTION_ROW (push_row_new (P_("Contact"), "contact-card-choose"));
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), GTK_WIDGET (self->contact_row));
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (page), ADW_PREFERENCES_GROUP (group));

    adw_preferences_group_set_title (ADW_PREFERENCES_GROUP (fields_group), P_("Shared Details"));
    adw_preferences_group_set_description (ADW_PREFERENCES_GROUP (fields_group),
                                           P_("The name always goes out. Anyone who reads the card gets these details."));
    for (guint i = 0; i < G_N_ELEMENTS (card_fields); i++) {
        self->field_rows[i] = ADW_SWITCH_ROW (adw_switch_row_new ());
        adw_preferences_row_set_title (ADW_PREFERENCES_ROW (self->field_rows[i]), P_(card_fields[i].title));
        if (card_fields[i].subtitle)
            adw_action_row_set_subtitle (ADW_ACTION_ROW (self->field_rows[i]), P_(card_fields[i].subtitle));
        g_signal_connect_swapped (self->field_rows[i], "notify::active", G_CALLBACK (on_field_toggled), self);
        adw_preferences_group_add (ADW_PREFERENCES_GROUP (fields_group), GTK_WIDGET (self->field_rows[i]));
    }
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (page), ADW_PREFERENCES_GROUP (fields_group));

    adw_preferences_group_set_description (ADW_PREFERENCES_GROUP (time_group),
                                           P_("NFC and the QR code stop by themselves after this time, or when the card was read over NFC."));
    self->seconds_row = ADW_SPIN_ROW (adw_spin_row_new_with_range (10, 300, 10));
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (self->seconds_row), P_("Seconds to Share or Receive"));
    g_settings_bind (self->card_settings, "seconds", self->seconds_row, "value", G_SETTINGS_BIND_DEFAULT);
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (time_group), GTK_WIDGET (self->seconds_row));
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (page), ADW_PREFERENCES_GROUP (time_group));

    update_card_fields (self);
    return card_navigation_page_new (page, P_("Contact Card"), "contact-card");
}

/* Subpage "contact-card-choose": the contacts of all address books, with a search */
static AdwNavigationPage *
build_choose_page (CcNfcPanel *self)
{
    GtkWidget *page = adw_preferences_page_new ();
    GtkWidget *search_group = adw_preferences_group_new ();
    AdwNavigationPage *nav;

    self->contacts_search = GTK_SEARCH_ENTRY (gtk_search_entry_new ());
    gtk_widget_set_hexpand (GTK_WIDGET (self->contacts_search), TRUE);
    g_signal_connect_swapped (self->contacts_search, "search-changed", G_CALLBACK (on_contacts_search_changed), self);
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (search_group), GTK_WIDGET (self->contacts_search));
    self->contacts_status = gtk_label_new (NULL);
    gtk_label_set_wrap (GTK_LABEL (self->contacts_status), TRUE);
    gtk_widget_add_css_class (self->contacts_status, "dim-label");
    gtk_widget_set_margin_top (self->contacts_status, 12);
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (search_group), self->contacts_status);
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (page), ADW_PREFERENCES_GROUP (search_group));

    self->contacts_group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (page), self->contacts_group);

    nav = card_navigation_page_new (page, P_("Choose Contact"), "contact-card-choose");
    g_signal_connect_swapped (nav, "showing", G_CALLBACK (on_choose_page_showing), self);
    return nav;
}

/* Group on the NFC page and its subpages; only with plinius-cardshare */
static void
add_card_group (CcNfcPanel *self, AdwPreferencesPage *page)
{
    g_autoptr(GSettingsSchema) schema = NULL;
    GSettingsSchemaSource *source = g_settings_schema_source_get_default ();
    GtkWidget *group;

    if (source)
        schema = g_settings_schema_source_lookup (source, CARDSHARE_SCHEMA, TRUE);
    if (!schema)
        return;

    self->card_settings = g_settings_new (CARDSHARE_SCHEMA);
    g_signal_connect_object (self->card_settings, "changed", G_CALLBACK (on_card_settings_changed), self,
                             G_CONNECT_SWAPPED);

    group = adw_preferences_group_new ();
    adw_preferences_group_set_description (ADW_PREFERENCES_GROUP (group),
                                           P_("Give your contact card to another phone over NFC or a QR code, and take in cards over NFC or Bluetooth, from the quick setting “My Card”."));
    self->card_row = ADW_ACTION_ROW (push_row_new (P_("Contact Card"), "contact-card"));
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), GTK_WIDGET (self->card_row));
    adw_preferences_page_add (page, ADW_PREFERENCES_GROUP (group));

    cc_panel_add_subpage (CC_PANEL (self), "contact-card", build_card_page (self));
    cc_panel_add_subpage (CC_PANEL (self), "contact-card-choose", build_choose_page (self));
    update_card_name (self);
}

static void
cc_nfc_panel_dispose (GObject *object)
{
    CcNfcPanel *self = CC_NFC_PANEL (object);

    g_cancellable_cancel (self->cancellable);
    g_clear_object (&self->cancellable);
    g_clear_object (&self->proxy);
    g_clear_object (&self->card_settings);
    g_clear_pointer (&self->contact_rows, g_ptr_array_unref);

    G_OBJECT_CLASS (cc_nfc_panel_parent_class)->dispose (object);
}

static void
cc_nfc_panel_class_init (CcNfcPanelClass *klass)
{
    G_OBJECT_CLASS (klass)->dispose = cc_nfc_panel_dispose;
}

static void
cc_nfc_panel_init (CcNfcPanel *self)
{
    GtkWidget *toolbar = adw_toolbar_view_new ();
    GtkWidget *page = adw_preferences_page_new ();
    GtkWidget *group = adw_preferences_group_new ();

    adw_navigation_page_set_title (ADW_NAVIGATION_PAGE (self), "NFC");
    self->toast_overlay = ADW_TOAST_OVERLAY (adw_toast_overlay_new ());
    adw_toast_overlay_set_child (self->toast_overlay, page);
    adw_toolbar_view_add_top_bar (ADW_TOOLBAR_VIEW (toolbar), adw_header_bar_new ());
    adw_toolbar_view_set_content (ADW_TOOLBAR_VIEW (toolbar), GTK_WIDGET (self->toast_overlay));
    adw_navigation_page_set_child (ADW_NAVIGATION_PAGE (self), toolbar);

    adw_preferences_group_set_description (ADW_PREFERENCES_GROUP (group),
                                           P_("Reads NFC tags and talks to cards and terminals held to the back of the phone. It only looks for them while the screen is on."));
    self->switch_row = ADW_SWITCH_ROW (adw_switch_row_new ());
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (self->switch_row), "NFC");
    gtk_widget_set_sensitive (GTK_WIDGET (self->switch_row), FALSE);
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), GTK_WIDGET (self->switch_row));
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (page), ADW_PREFERENCES_GROUP (group));
    g_signal_connect_swapped (self->switch_row, "notify::active", G_CALLBACK (on_switch_changed), self);

    self->cancellable = g_cancellable_new ();
    add_card_group (self, ADW_PREFERENCES_PAGE (page));
    g_dbus_proxy_new_for_bus (G_BUS_TYPE_SYSTEM, G_DBUS_PROXY_FLAGS_DO_NOT_LOAD_PROPERTIES, NULL,
                              NFC_SETTINGS_NAME, NFC_SETTINGS_PATH, NFC_SETTINGS_INTERFACE, self->cancellable,
                              proxy_ready_cb, self);
}

/* Only with nfcd */
void
cc_nfc_panel_static_init_func (void)
{
    CcApplication *application = CC_APPLICATION (g_application_get_default ());

    cc_shell_model_set_panel_visibility (cc_application_get_model (application), "nfc",
                                         g_file_test (NFC_SERVICE_POLICY, G_FILE_TEST_EXISTS) ? CC_PANEL_VISIBLE
                                                                                            : CC_PANEL_HIDDEN);
}
