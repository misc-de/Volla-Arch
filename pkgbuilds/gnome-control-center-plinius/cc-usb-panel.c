/*
 * cc-usb-panel.c
 *
 * Plinius: "USB", a panel of its own in the sidebar below NFC: what the phone offers a
 * computer on the cable (file sharing, network, internet for the computer), the same as the
 * USB quick setting of Phosh. plinius-usb does the work (org.plinius.Usb on the system bus,
 * docs/HARDWARE.md "USB-Connect"):
 * - SetFunctions(as) with any combination of "mtp", "ncm", "gateway" (gateway includes ncm),
 *   none = charging only. Only while a computer is attached (Connected); unplugging switches
 *   everything off, so the choice is made again for every plug-in.
 * - Switching on needs the unlocked user of the active session, the service checks it.
 * - NetworkOnConnect: network right at plug-in, without unlocking (development preset).
 * - Power, with any partner that speaks USB Power Delivery (plinius-usb 0.4): PowerRole "sink"
 *   (the phone takes power) or "source" (it powers the device), SetPowerRole(s) asks the partner
 *   for a power role swap and fails with the reason if it refuses. PowerRoleSwappable says
 *   whether the partner can be asked; a device that only takes power gets it anyway.
 *   SupplyPowerOnConnect is the choice under "When Plugging In": power every such device at
 *   plug-in, also while the phone is locked.
 * A function the service does not list in AvailableFunctions has no row (e.g. "webcam" before
 * the mtu3 patch, "mtp" without umtprd).
 *
 * The panel is only listed when plinius-usb is installed.
 * Our own strings use the gettext domain plinius-control-center (not GNOME's po/).
 *
 * SPDX-License-Identifier: MIT
 */

#include "config.h"

#include <adwaita.h>
#include <glib/gi18n.h>

#include "cc-usb-panel.h"
#include "shell/cc-application.h"

#define PLINIUS_DOMAIN "plinius-control-center"
#define P_(String) g_dgettext (PLINIUS_DOMAIN, String)
/* N_(): marked for translation (xgettext -k N_), looked up with P_() where shown */

#define USB_NAME "org.plinius.Usb"
#define USB_PATH "/org/plinius/Usb"
#define USB_INTERFACE "org.plinius.Usb"
#define USB_SERVICE_POLICY "/usr/share/dbus-1/system.d/org.plinius.Usb.conf"

/* The functions in the order of the rows */
static const struct {
    const char *name;
    const char *title;
    const char *subtitle;
} functions[] = {
    { "mtp", N_("File Sharing"), N_("The computer sees the files of your home folder (MTP)") },
    { "ncm", N_("Network"), N_("A network between the phone and the computer (NCM)") },
    { "gateway", N_("Internet for the Computer"), N_("The computer goes online through the phone, includes Network") },
    { "webcam", N_("Webcam"), N_("The computer gets a picture of the phone's camera") },
};

struct _CcUsbPanel {
    CcPanel parent_instance;

    AdwToastOverlay *toast_overlay;
    AdwActionRow *status_row;
    AdwPreferencesGroup *functions_group;
    AdwSwitchRow *function_rows[G_N_ELEMENTS (functions)];
    AdwSwitchRow *preset_row;
    AdwSwitchRow *power_row;
    AdwComboRow *power_preset_row;

    GDBusProxy *proxy;
    GCancellable *cancellable;
    gboolean updating;
    /* SetPowerRole is waiting for the partner */
    gboolean power_busy;
};

G_DEFINE_TYPE (CcUsbPanel, cc_usb_panel, CC_TYPE_PANEL)

static gboolean
strv_has (GVariant *list, const char *name)
{
    g_autofree const char **names = list ? g_variant_get_strv (list, NULL) : NULL;

    return names && g_strv_contains (names, name);
}

static AdwSwitchRow *
row_of (CcUsbPanel *self, const char *name)
{
    for (guint i = 0; i < G_N_ELEMENTS (functions); i++)
        if (g_str_equal (functions[i].name, name))
            return self->function_rows[i];
    return NULL;
}

/* All rows from the service's properties */
/* The power row: what the cable does now, and whether the partner could be asked to change it */
static void
update_power (CcUsbPanel *self, GVariant *role_value, GVariant *swappable_value)
{
    const char *role = role_value ? g_variant_get_string (role_value, NULL) : "";
    gboolean swappable = swappable_value && g_variant_get_boolean (swappable_value);
    gboolean source = g_str_equal (role, "source");
    const char *subtitle;

    if (!*role)
        subtitle = P_("Nothing connected");
    else if (source && swappable)
        subtitle = P_("The phone supplies power to the connected device");
    else if (source)
        subtitle = P_("The connected device takes its power from the phone");
    else if (swappable)
        subtitle = P_("This phone takes power from the cable");
    else
        subtitle = P_("Only with devices that support USB Power Delivery");

    adw_switch_row_set_active (self->power_row, source);
    adw_action_row_set_subtitle (ADW_ACTION_ROW (self->power_row), subtitle);
    gtk_widget_set_sensitive (GTK_WIDGET (self->power_row), *role && swappable && !self->power_busy);
}

static void
update (CcUsbPanel *self)
{
    g_autofree char *owner = self->proxy ? g_dbus_proxy_get_name_owner (self->proxy) : NULL;
    g_autoptr(GVariant) connected = NULL, active = NULL, available = NULL, preset = NULL;
    g_autoptr(GVariant) power_role = NULL, swappable = NULL, power_preset = NULL;
    gboolean is_connected, gateway;

    if (owner) {
        connected = g_dbus_proxy_get_cached_property (self->proxy, "Connected");
        active = g_dbus_proxy_get_cached_property (self->proxy, "Functions");
        available = g_dbus_proxy_get_cached_property (self->proxy, "AvailableFunctions");
        preset = g_dbus_proxy_get_cached_property (self->proxy, "NetworkOnConnect");
        /* Not there before plinius-usb 0.4: the rows stay insensitive */
        power_role = g_dbus_proxy_get_cached_property (self->proxy, "PowerRole");
        swappable = g_dbus_proxy_get_cached_property (self->proxy, "PowerRoleSwappable");
        power_preset = g_dbus_proxy_get_cached_property (self->proxy, "SupplyPowerOnConnect");
    }
    is_connected = connected && g_variant_get_boolean (connected);
    gateway = strv_has (active, "gateway");

    self->updating = TRUE;

    if (!owner)
        adw_action_row_set_subtitle (self->status_row, P_("The USB service is not running"));
    else if (is_connected)
        adw_action_row_set_subtitle (self->status_row, P_("Connected to a computer"));
    else
        adw_action_row_set_subtitle (self->status_row, P_("No computer connected, only charging"));

    for (guint i = 0; i < G_N_ELEMENTS (functions); i++) {
        AdwSwitchRow *row = self->function_rows[i];

        gtk_widget_set_visible (GTK_WIDGET (row), strv_has (available, functions[i].name));
        adw_switch_row_set_active (row, strv_has (active, functions[i].name));
        gtk_widget_set_sensitive (GTK_WIDGET (row), is_connected);
    }
    /* Internet for the computer needs the network, it cannot go off on its own then */
    if (gateway)
        gtk_widget_set_sensitive (GTK_WIDGET (row_of (self, "ncm")), FALSE);

    gtk_widget_set_sensitive (GTK_WIDGET (self->preset_row), preset != NULL);
    adw_switch_row_set_active (self->preset_row, preset && g_variant_get_boolean (preset));

    update_power (self, power_role, swappable);
    gtk_widget_set_sensitive (GTK_WIDGET (self->power_preset_row), power_preset != NULL);
    adw_combo_row_set_selected (self->power_preset_row, power_preset && g_variant_get_boolean (power_preset) ? 1 : 0);

    self->updating = FALSE;
}

static void
call_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(GError) error = NULL;
    g_autoptr(GVariant) reply = g_dbus_proxy_call_finish (G_DBUS_PROXY (source), result, &error);
    CcUsbPanel *self;

    if (reply || g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
        return;

    self = CC_USB_PANEL (user_data);
    g_dbus_error_strip_remote_error (error);
    g_warning ("Changing the USB functions failed: %s", error->message);
    adw_toast_overlay_add_toast (self->toast_overlay, adw_toast_new (error->message));
    /* Show the real state again */
    update (self);
}

static void
on_function_changed (CcUsbPanel *self)
{
    g_autoptr(GStrvBuilder) builder = NULL;
    g_auto(GStrv) names = NULL;

    if (self->updating || !self->proxy)
        return;

    builder = g_strv_builder_new ();
    for (guint i = 0; i < G_N_ELEMENTS (functions); i++)
        if (gtk_widget_get_visible (GTK_WIDGET (self->function_rows[i])) &&
            adw_switch_row_get_active (self->function_rows[i]))
            g_strv_builder_add (builder, functions[i].name);
    names = g_strv_builder_end (builder);

    /* The service rebuilds the gadget and reports the result in Functions */
    g_dbus_proxy_call (self->proxy, "SetFunctions", g_variant_new ("(^as)", names), G_DBUS_CALL_FLAGS_NONE, -1,
                       self->cancellable, call_cb, self);
}

static void
on_preset_changed (CcUsbPanel *self)
{
    if (self->updating || !self->proxy)
        return;
    g_dbus_proxy_call (self->proxy, "org.freedesktop.DBus.Properties.Set",
                       g_variant_new ("(ssv)", USB_INTERFACE, "NetworkOnConnect",
                                      g_variant_new_boolean (adw_switch_row_get_active (self->preset_row))),
                       G_DBUS_CALL_FLAGS_NONE, -1, self->cancellable, call_cb, self);
}

static void
power_role_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(GError) error = NULL;
    g_autoptr(GVariant) reply = g_dbus_proxy_call_finish (G_DBUS_PROXY (source), result, &error);
    CcUsbPanel *self;

    if (g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
        return;

    self = CC_USB_PANEL (user_data);
    self->power_busy = FALSE;
    if (!reply) {
        g_dbus_error_strip_remote_error (error);
        g_warning ("Changing the power direction failed: %s", error->message);
        adw_toast_overlay_add_toast (self->toast_overlay, adw_toast_new (error->message));
    }
    /* The real state, the service reports it in PowerRole */
    update (self);
}

static void
on_power_changed (CcUsbPanel *self)
{
    gboolean source;

    if (self->updating || !self->proxy)
        return;

    source = adw_switch_row_get_active (self->power_row);
    /* The partner answers the swap, that takes a moment */
    self->power_busy = TRUE;
    gtk_widget_set_sensitive (GTK_WIDGET (self->power_row), FALSE);
    g_dbus_proxy_call (self->proxy, "SetPowerRole", g_variant_new ("(s)", source ? "source" : "sink"),
                       G_DBUS_CALL_FLAGS_NONE, 15000, self->cancellable, power_role_cb, self);
}

static void
on_power_preset_changed (CcUsbPanel *self)
{
    if (self->updating || !self->proxy)
        return;
    g_dbus_proxy_call (self->proxy, "org.freedesktop.DBus.Properties.Set",
                       g_variant_new ("(ssv)", USB_INTERFACE, "SupplyPowerOnConnect",
                                      g_variant_new_boolean (adw_combo_row_get_selected (self->power_preset_row) == 1)),
                       G_DBUS_CALL_FLAGS_NONE, -1, self->cancellable, call_cb, self);
}

static void
proxy_ready_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(GError) error = NULL;
    GDBusProxy *proxy = g_dbus_proxy_new_for_bus_finish (result, &error);
    CcUsbPanel *self;

    if (!proxy) {
        if (!g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
            g_warning ("Cannot reach plinius-usb: %s", error->message);
        return;
    }

    self = CC_USB_PANEL (user_data);
    self->proxy = proxy;
    g_signal_connect_object (proxy, "g-properties-changed", G_CALLBACK (update), self, G_CONNECT_SWAPPED);
    g_signal_connect_object (proxy, "notify::g-name-owner", G_CALLBACK (update), self, G_CONNECT_SWAPPED);
    update (self);
}

static void
cc_usb_panel_dispose (GObject *object)
{
    CcUsbPanel *self = CC_USB_PANEL (object);

    g_cancellable_cancel (self->cancellable);
    g_clear_object (&self->cancellable);
    g_clear_object (&self->proxy);

    G_OBJECT_CLASS (cc_usb_panel_parent_class)->dispose (object);
}

static void
cc_usb_panel_class_init (CcUsbPanelClass *klass)
{
    G_OBJECT_CLASS (klass)->dispose = cc_usb_panel_dispose;
}

static void
cc_usb_panel_init (CcUsbPanel *self)
{
    GtkWidget *toolbar = adw_toolbar_view_new ();
    GtkWidget *page = adw_preferences_page_new ();
    GtkWidget *status_group = adw_preferences_group_new ();
    GtkWidget *preset_group = adw_preferences_group_new ();
    GtkWidget *power_group = adw_preferences_group_new ();
    /* The order of the choices under "When Plugging In": SupplyPowerOnConnect off, on */
    const char *power_presets[] = { P_("Charge This Phone"), P_("Power the Connected Device"), NULL };

    adw_navigation_page_set_title (ADW_NAVIGATION_PAGE (self), "USB");
    self->toast_overlay = ADW_TOAST_OVERLAY (adw_toast_overlay_new ());
    adw_toast_overlay_set_child (self->toast_overlay, page);
    adw_toolbar_view_add_top_bar (ADW_TOOLBAR_VIEW (toolbar), adw_header_bar_new ());
    adw_toolbar_view_set_content (ADW_TOOLBAR_VIEW (toolbar), GTK_WIDGET (self->toast_overlay));
    adw_navigation_page_set_child (ADW_NAVIGATION_PAGE (self), toolbar);

    /* Is a computer on the cable? (a wall charger is not one) */
    self->status_row = ADW_ACTION_ROW (adw_action_row_new ());
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (self->status_row), P_("Computer"));
    adw_action_row_add_prefix (self->status_row, gtk_image_new_from_icon_name ("plinius-usb-symbolic"));
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (status_group), GTK_WIDGET (self->status_row));
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (page), ADW_PREFERENCES_GROUP (status_group));

    /* Functions, any combination; all off = charging only */
    self->functions_group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_group_set_title (self->functions_group, P_("Use USB For"));
    adw_preferences_group_set_description (self->functions_group,
                                           P_("Only while a computer is connected and the phone is unlocked. Unplugging switches everything off, so nothing is shared with the next computer by itself."));
    for (guint i = 0; i < G_N_ELEMENTS (functions); i++) {
        AdwSwitchRow *row = ADW_SWITCH_ROW (adw_switch_row_new ());

        adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_(functions[i].title));
        adw_action_row_set_subtitle (ADW_ACTION_ROW (row), P_(functions[i].subtitle));
        gtk_widget_set_visible (GTK_WIDGET (row), FALSE);
        gtk_widget_set_sensitive (GTK_WIDGET (row), FALSE);
        g_signal_connect_swapped (row, "notify::active", G_CALLBACK (on_function_changed), self);
        adw_preferences_group_add (self->functions_group, GTK_WIDGET (row));
        self->function_rows[i] = row;
    }
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (page), self->functions_group);

    /* Charge or discharge: which way the power goes over the cable */
    adw_preferences_group_set_title (ADW_PREFERENCES_GROUP (power_group), P_("Power Supply"));
    self->power_row = ADW_SWITCH_ROW (adw_switch_row_new ());
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (self->power_row), P_("Power the Connected Device"));
    adw_action_row_set_subtitle (ADW_ACTION_ROW (self->power_row), P_("Nothing connected"));
    gtk_widget_set_sensitive (GTK_WIDGET (self->power_row), FALSE);
    g_signal_connect_swapped (self->power_row, "notify::active", G_CALLBACK (on_power_changed), self);
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (power_group), GTK_WIDGET (self->power_row));
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (page), ADW_PREFERENCES_GROUP (power_group));

    /* Development preset: network at every plug-in, also while locked (SSH) */
    adw_preferences_group_set_title (ADW_PREFERENCES_GROUP (preset_group), P_("When Plugging In"));
    self->preset_row = ADW_SWITCH_ROW (adw_switch_row_new ());
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (self->preset_row), P_("Network Right Away"));
    adw_action_row_set_subtitle (ADW_ACTION_ROW (self->preset_row),
                                 P_("Network with every computer, also while the phone is locked. Meant for development."));
    gtk_widget_set_sensitive (GTK_WIDGET (self->preset_row), FALSE);
    g_signal_connect_swapped (self->preset_row, "notify::active", G_CALLBACK (on_preset_changed), self);
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (preset_group), GTK_WIDGET (self->preset_row));

    /* The basic choice: charge the phone, or power whatever can take it (also while locked) */
    self->power_preset_row = ADW_COMBO_ROW (adw_combo_row_new ());
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (self->power_preset_row), P_("Power Supply"));
    adw_action_row_set_subtitle (ADW_ACTION_ROW (self->power_preset_row),
                                 P_("Powering applies to every device that supports USB Power Delivery and agrees, also while the phone is locked. Others keep charging the phone. At 10 % battery the phone stops powering."));
    adw_combo_row_set_model (self->power_preset_row, G_LIST_MODEL (gtk_string_list_new (power_presets)));
    gtk_widget_set_sensitive (GTK_WIDGET (self->power_preset_row), FALSE);
    g_signal_connect_swapped (self->power_preset_row, "notify::selected", G_CALLBACK (on_power_preset_changed), self);
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (preset_group), GTK_WIDGET (self->power_preset_row));
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (page), ADW_PREFERENCES_GROUP (preset_group));

    self->cancellable = g_cancellable_new ();
    g_dbus_proxy_new_for_bus (G_BUS_TYPE_SYSTEM, G_DBUS_PROXY_FLAGS_NONE, NULL, USB_NAME, USB_PATH, USB_INTERFACE,
                              self->cancellable, proxy_ready_cb, self);
}

/* Only with plinius-usb */
void
cc_usb_panel_static_init_func (void)
{
    CcApplication *application = CC_APPLICATION (g_application_get_default ());

    cc_shell_model_set_panel_visibility (cc_application_get_model (application), "usb",
                                         g_file_test (USB_SERVICE_POLICY, G_FILE_TEST_EXISTS) ? CC_PANEL_VISIBLE
                                                                                            : CC_PANEL_HIDDEN);
}
