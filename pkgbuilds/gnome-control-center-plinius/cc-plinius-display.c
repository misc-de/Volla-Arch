/*
 * cc-plinius-display.c
 *
 * Plinius: phone options.
 *
 * Display page, for the built-in panel only:
 * - Rotation speed: how long a new orientation must hold before the screen turns
 *   (sensor-proxy-bridge, org.plinius.SensorProxyBridge on net.hadess.SensorProxy); a row the
 *   display settings put right below "Auto Rotate".
 * - Wake up: double tap and tilt, and brightening a dimmed screen when the phone is picked up
 *   (plinius-wake, org.plinius.Wake).
 * - Flip cover: the Hall sensor switches the screen (plinius-assist, key "cover" of
 *   org.plinius.assistant-button).
 * Display page, moved from Power and Screen Lock so that all of it is in one place:
 * - Brightness and screen off: automatic brightness, dimming and the screen-off delay. GNOME's
 *   own settings with GNOME's strings; the first two only where Power showed them too.
 *
 * Appearance page, at the top:
 * - App overview: search, app names and the folder dock (phosh-plinius, schema
 *   org.plinius.display), and Phosh's own "mobile friendly apps" button (mobi.phosh.shell,
 *   sm.puri.phosh before Phosh 0.59).
 *
 * Each group only shows when the service or schema behind it is there.
 * Our own strings use the gettext domain plinius-control-center (not GNOME's po/).
 *
 * SPDX-License-Identifier: MIT
 */

#undef G_LOG_DOMAIN
#define G_LOG_DOMAIN "cc-plinius-display"

#include "cc-plinius-display.h"

#include <glib/gi18n.h>

#include "cc-number-row.h"

#define PLINIUS_DOMAIN "plinius-control-center"
#define P_(String) g_dgettext (PLINIUS_DOMAIN, String)

#define SENSORS_NAME "net.hadess.SensorProxy"
#define SENSORS_PATH "/net/hadess/SensorProxy"
#define SENSORS_PLINIUS_INTERFACE "org.plinius.SensorProxyBridge"
#define SETTLE_TIME_PROPERTY "OrientationSettleTime"

#define WAKE_NAME "org.plinius.Wake"
#define WAKE_PATH "/org/plinius/Wake"
#define WAKE_INTERFACE "org.plinius.Wake"

#define DISPLAY_SCHEMA "org.plinius.display"
#define ASSIST_SCHEMA "org.plinius.assistant-button"
#define PHOSH_SCHEMA "mobi.phosh.shell"
#define PHOSH_SCHEMA_OLD "sm.puri.phosh" /* Phosh before 0.59 */
#define PHOSH_FILTER_KEY "app-filter-mode"
#define PHOSH_FILTER_ADAPTIVE "adaptive"

/* Rotation speeds in ms, in the order of the combo row */
static const guint32 settle_times[] = { 250, 500, 1000 };

/* ----- D-Bus properties of a system service ----- */

static void
set_property_done_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(GError) error = NULL;
    g_autoptr(GVariant) reply = NULL;

    reply = g_dbus_proxy_call_finish (G_DBUS_PROXY (source), result, &error);
    if (reply == NULL)
        g_warning ("Changing %s failed: %s", (const char *) user_data, error->message);
}

/* Sets a property through org.freedesktop.DBus.Properties of the proxy's object */
static void
set_service_property (GDBusProxy *proxy, const char *interface, const char *property, GVariant *value)
{
    g_dbus_proxy_call (proxy, "org.freedesktop.DBus.Properties.Set",
                       g_variant_new ("(ssv)", interface, property, value),
                       G_DBUS_CALL_FLAGS_NONE, -1, NULL, set_property_done_cb, (gpointer) property);
}

static GDBusProxy *
service_proxy (const char *name, const char *path, const char *interface)
{
    g_autoptr(GError) error = NULL;
    g_autoptr(GDBusProxy) proxy = NULL;

    proxy = g_dbus_proxy_new_for_bus_sync (G_BUS_TYPE_SYSTEM, G_DBUS_PROXY_FLAGS_DO_NOT_AUTO_START,
                                           NULL, name, path, interface, NULL, &error);
    if (proxy == NULL) {
        g_debug ("No %s: %s", name, error->message);
        return NULL;
    }
    if (g_dbus_proxy_get_name_owner (proxy) == NULL)
        return NULL;
    return g_steal_pointer (&proxy);
}

static AdwPreferencesGroup *
new_group (GtkBox *box, const char *title, const char *description)
{
    GtkWidget *group = adw_preferences_group_new ();

    adw_preferences_group_set_title (ADW_PREFERENCES_GROUP (group), title);
    if (description)
        adw_preferences_group_set_description (ADW_PREFERENCES_GROUP (group), description);
    gtk_box_append (box, group);
    return ADW_PREFERENCES_GROUP (group);
}

/* ----- Rotation speed ----- */

static void
settle_row_changed_cb (AdwComboRow *row, GParamSpec *pspec, GDBusProxy *proxy)
{
    guint selected = adw_combo_row_get_selected (row);

    if (selected >= G_N_ELEMENTS (settle_times))
        return;
    set_service_property (proxy, SENSORS_PLINIUS_INTERFACE, SETTLE_TIME_PROPERTY,
                          g_variant_new_uint32 (settle_times[selected]));
}

/**
 * cc_plinius_display_rotation_row_new:
 *
 * The "Rotation Speed" row, or %NULL without sensor-proxy-bridge.
 */
GtkWidget *
cc_plinius_display_rotation_row_new (void)
{
    g_autoptr(GDBusProxy) proxy = NULL;
    g_autoptr(GVariant) value = NULL;
    g_autoptr(GtkStringList) names = NULL;
    GtkWidget *row;
    guint32 current;
    guint selected = 1;

    proxy = service_proxy (SENSORS_NAME, SENSORS_PATH, SENSORS_PLINIUS_INTERFACE);
    if (proxy)
        value = g_dbus_proxy_get_cached_property (proxy, SETTLE_TIME_PROPERTY);
    if (value == NULL)
        return NULL;

    /* The nearest step to the stored time */
    current = g_variant_get_uint32 (value);
    for (guint i = 0; i < G_N_ELEMENTS (settle_times); i++)
        if (ABS ((int) current - (int) settle_times[i]) < ABS ((int) current - (int) settle_times[selected]))
            selected = i;

    /* Same order as settle_times */
    names = gtk_string_list_new (NULL);
    gtk_string_list_append (names, P_("Fast"));
    gtk_string_list_append (names, P_("Normal"));
    gtk_string_list_append (names, P_("Slow"));

    row = adw_combo_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_("Rotation Speed"));
    adw_action_row_set_subtitle (ADW_ACTION_ROW (row), P_("How long the phone must be held turned before the screen follows"));
    adw_combo_row_set_model (ADW_COMBO_ROW (row), G_LIST_MODEL (names));
    adw_combo_row_set_selected (ADW_COMBO_ROW (row), selected);

    /* The row keeps the proxy */
    g_object_set_data_full (G_OBJECT (row), "plinius-sensors", g_object_ref (proxy), g_object_unref);
    g_signal_connect (row, "notify::selected", G_CALLBACK (settle_row_changed_cb), proxy);
    return row;
}

/* ----- Wake up ----- */

typedef struct {
    GDBusProxy *proxy;
    AdwSwitchRow *double_tap_row;
    AdwSwitchRow *tilt_row;
    AdwSwitchRow *brighten_row;
    gboolean updating;
} WakeRows;

static void
wake_rows_free (WakeRows *rows)
{
    g_clear_object (&rows->proxy);
    g_free (rows);
}

static gboolean
wake_get (WakeRows *rows, const char *property)
{
    g_autoptr(GVariant) value = g_dbus_proxy_get_cached_property (rows->proxy, property);

    return value && g_variant_get_boolean (value);
}

static void
wake_update_rows (WakeRows *rows)
{
    rows->updating = TRUE;
    adw_switch_row_set_active (rows->double_tap_row, wake_get (rows, "DoubleTap"));
    adw_switch_row_set_active (rows->tilt_row, wake_get (rows, "Tilt"));
    adw_switch_row_set_active (rows->brighten_row, wake_get (rows, "PickUpBrighten"));
    rows->updating = FALSE;
}

static void
wake_row_changed_cb (AdwSwitchRow *row, GParamSpec *pspec, WakeRows *rows)
{
    const char *property = (row == rows->double_tap_row) ? "DoubleTap" :
                           (row == rows->tilt_row)       ? "Tilt" : "PickUpBrighten";

    if (rows->updating)
        return;
    set_service_property (rows->proxy, WAKE_INTERFACE, property,
                          g_variant_new_boolean (adw_switch_row_get_active (row)));
}

static AdwSwitchRow *
new_switch_row (AdwPreferencesGroup *group, const char *title, const char *subtitle)
{
    GtkWidget *row = adw_switch_row_new ();

    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), title);
    if (subtitle)
        adw_action_row_set_subtitle (ADW_ACTION_ROW (row), subtitle);
    adw_preferences_group_add (group, row);
    return ADW_SWITCH_ROW (row);
}

static gboolean
setup_wake (GtkBox *box)
{
    g_autoptr(GDBusProxy) proxy = NULL;
    g_autoptr(GVariant) brighten = NULL;
    AdwPreferencesGroup *group;
    WakeRows *rows;

    proxy = service_proxy (WAKE_NAME, WAKE_PATH, WAKE_INTERFACE);
    if (proxy == NULL)
        return FALSE;

    rows = g_new0 (WakeRows, 1);
    rows->proxy = g_steal_pointer (&proxy);

    group = new_group (box, P_("Wake Up"), P_("The screen also comes on without the power button. Not while the phone is in a pocket."));
    rows->double_tap_row = new_switch_row (group, P_("Double Tap"), P_("Tap the dark screen twice"));
    rows->tilt_row = new_switch_row (group, P_("Tilt"), P_("Pick the phone up from lying flat"));
    rows->brighten_row = new_switch_row (group, P_("Brighten When Picked Up"),
                                         P_("A dimmed screen gets bright again, a dark one stays off"));
    /* plinius-wake before 0.3 has no PickUpBrighten */
    brighten = g_dbus_proxy_get_cached_property (rows->proxy, "PickUpBrighten");
    gtk_widget_set_visible (GTK_WIDGET (rows->double_tap_row), wake_get (rows, "HasDoubleTap"));
    gtk_widget_set_visible (GTK_WIDGET (rows->tilt_row), wake_get (rows, "HasTilt"));
    gtk_widget_set_visible (GTK_WIDGET (rows->brighten_row), wake_get (rows, "HasTilt") && brighten != NULL);
    gtk_widget_set_visible (GTK_WIDGET (group), wake_get (rows, "HasDoubleTap") || wake_get (rows, "HasTilt"));
    wake_update_rows (rows);

    g_signal_connect (rows->double_tap_row, "notify::active", G_CALLBACK (wake_row_changed_cb), rows);
    g_signal_connect (rows->tilt_row, "notify::active", G_CALLBACK (wake_row_changed_cb), rows);
    g_signal_connect (rows->brighten_row, "notify::active", G_CALLBACK (wake_row_changed_cb), rows);
    g_signal_connect_swapped (rows->proxy, "g-properties-changed", G_CALLBACK (wake_update_rows), rows);
    /* The group owns the rows' state */
    g_object_set_data_full (G_OBJECT (group), "plinius-wake-rows", rows, (GDestroyNotify) wake_rows_free);
    return TRUE;
}

/* ----- App overview ----- */

/* The settings of a schema that has @key, or NULL (an older phosh-plinius has no such key) */
static GSettings *
settings_if_installed (const char *schema_id, const char *key)
{
    GSettingsSchemaSource *source = g_settings_schema_source_get_default ();
    g_autoptr(GSettingsSchema) schema = NULL;

    if (source)
        schema = g_settings_schema_source_lookup (source, schema_id, TRUE);
    if (schema == NULL || !g_settings_schema_has_key (schema, key))
        return NULL;
    return g_settings_new_full (schema, NULL, NULL);
}

static void
bind_switch (AdwPreferencesGroup *group, GSettings *settings, const char *key,
             const char *title, const char *subtitle)
{
    AdwSwitchRow *row = new_switch_row (group, title, subtitle);

    /* The binding keeps its own reference on the settings */
    g_settings_bind (settings, key, row, "active", G_SETTINGS_BIND_DEFAULT);
}

/* Phosh's filter mode is a flags key: the button exists while "adaptive" is set */
static gboolean
adaptive_get_mapping (GValue *value, GVariant *variant, gpointer user_data)
{
    g_autofree const char **flags = g_variant_get_strv (variant, NULL);

    g_value_set_boolean (value, g_strv_contains (flags, PHOSH_FILTER_ADAPTIVE));
    return TRUE;
}

static GVariant *
adaptive_set_mapping (const GValue *value, const GVariantType *expected_type, gpointer user_data)
{
    const char *adaptive[] = { PHOSH_FILTER_ADAPTIVE, NULL };

    /* Off is the empty list of flags */
    return g_variant_new_strv (adaptive, g_value_get_boolean (value) ? 1 : 0);
}

/* The running apps above the app grid (as shipped, "top") or below it ("bottom"): a switch
 * for phosh-plinius' choice, on for "bottom" */
static gboolean
running_apps_get_mapping (GValue *value, GVariant *variant, gpointer user_data)
{
    g_value_set_boolean (value, g_strcmp0 (g_variant_get_string (variant, NULL), "bottom") == 0);
    return TRUE;
}

static GVariant *
running_apps_set_mapping (const GValue *value, const GVariantType *type, gpointer user_data)
{
    return g_variant_new_string (g_value_get_boolean (value) ? "bottom" : "top");
}

static void
add_running_apps_row (AdwPreferencesGroup *group, GSettings *settings)
{
    AdwSwitchRow *row = new_switch_row (group, P_("Open Apps at the Bottom"),
                                        P_("The cards of the open apps below the app icons"));

    /* The binding keeps its own reference on the settings */
    g_settings_bind_with_mapping (settings, "overview-running-apps", row, "active",
                                  G_SETTINGS_BIND_DEFAULT, running_apps_get_mapping,
                                  running_apps_set_mapping, NULL, NULL);
}

/**
 * cc_plinius_display_setup_app_overview:
 * @page: the Appearance page
 *
 * Adds the app overview group at the top of @page. Returns %FALSE without phosh-plinius.
 */
gboolean
cc_plinius_display_setup_app_overview (AdwPreferencesPage *page)
{
    g_autoptr(GSettings) display = settings_if_installed (DISPLAY_SCHEMA, "app-grid-folder-dock");
    g_autoptr(GSettings) phosh = settings_if_installed (PHOSH_SCHEMA, PHOSH_FILTER_KEY);
    g_autoptr(GSettings) running = settings_if_installed (DISPLAY_SCHEMA, "overview-running-apps");
    AdwPreferencesGroup *group;
    AdwSwitchRow *row;

    if (phosh == NULL)
        phosh = settings_if_installed (PHOSH_SCHEMA_OLD, PHOSH_FILTER_KEY);
    if (display == NULL)
        return FALSE;

    group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_group_set_title (group, P_("App Overview"));
    adw_preferences_page_insert (page, group, 0);
    /* The search field is set on the Search page (cc_plinius_display_setup_search) */
    bind_switch (group, display, "app-grid-app-names", P_("App Names"), P_("Names under the app icons"));
    bind_switch (group, display, "app-grid-folder-dock", P_("Folders at the Bottom"),
                 P_("Folders in one row at the bottom edge, scrolling sideways"));
    /* The cards of the running apps above or below the app grid (phosh-plinius) */
    if (running)
        add_running_apps_row (group, running);

    if (phosh) {
        row = new_switch_row (group, P_("Mobile Apps Button"),
                              P_("Button that shows only the apps made for phones"));
        g_settings_bind_with_mapping (phosh, PHOSH_FILTER_KEY, row, "active", G_SETTINGS_BIND_DEFAULT,
                                      adaptive_get_mapping, adaptive_set_mapping, NULL, NULL);
    }
    return TRUE;
}

/**
 * cc_plinius_display_setup_search:
 * @page: the Search page
 *
 * Adds the phone's own searches at the top of @page: from the notch and in the app overview.
 * Returns %FALSE without phosh-plinius.
 */
gboolean
cc_plinius_display_setup_search (AdwPreferencesPage *page)
{
    g_autoptr(GSettings) display = settings_if_installed (DISPLAY_SCHEMA, "app-grid-search");
    g_autoptr(GSettings) notch = settings_if_installed (DISPLAY_SCHEMA, "notch-search");
    AdwPreferencesGroup *group;

    if (display == NULL)
        return FALSE;

    group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_group_set_title (group, P_("Home Screen"));
    adw_preferences_page_insert (page, group, 0);
    /* phosh-plinius before 0.57.0-24 has no search from the notch */
    if (notch)
        bind_switch (group, notch, "notch-search", P_("Search from the Notch"),
                     P_("Tap the camera notch to search apps, settings, contacts and more"));
    bind_switch (group, display, "app-grid-search", P_("Search in the App Overview"),
                 P_("Search field above the apps"));
    return TRUE;
}

/* ----- Brightness and screen off ----- */

#define POWER_SCHEMA "org.gnome.settings-daemon.plugins.power"
#define SESSION_SCHEMA "org.gnome.desktop.session"
#define BRIGHTNESS_NAME "org.gnome.Shell.Brightness"
#define BRIGHTNESS_PATH "/org/gnome/Shell/Brightness"

static gboolean
proxy_says_yes (AdwPreferencesGroup *group, const char *proxy_key, const char *property)
{
    GDBusProxy *proxy = g_object_get_data (G_OBJECT (group), proxy_key);
    g_autoptr(GVariant) value = proxy ? g_dbus_proxy_get_cached_property (proxy, property) : NULL;

    return value && g_variant_get_boolean (value);
}

/* As on GNOME's Power page: dimming needs a brightness control, automatic also the light sensor */
static void
sync_brightness_rows (AdwPreferencesGroup *group)
{
    gboolean control = proxy_says_yes (group, "plinius-brightness", "HasBrightnessControl");
    gboolean sensor = proxy_says_yes (group, "plinius-sensors", "HasAmbientLight");

    gtk_widget_set_visible (g_object_get_data (G_OBJECT (group), "plinius-dim-row"), control);
    gtk_widget_set_visible (g_object_get_data (G_OBJECT (group), "plinius-auto-row"), control && sensor);
}

static void
brightness_proxy_ready_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(AdwPreferencesGroup) group = user_data;
    g_autoptr(GError) error = NULL;
    GDBusProxy *proxy = g_dbus_proxy_new_for_bus_finish (result, &error);

    if (proxy == NULL) {
        g_debug ("No brightness information: %s", error->message);
        return;
    }

    g_object_set_data_full (G_OBJECT (group),
                            g_strcmp0 (g_dbus_proxy_get_name (proxy), BRIGHTNESS_NAME) == 0 ? "plinius-brightness"
                                                                                            : "plinius-sensors",
                            proxy, g_object_unref);
    g_signal_connect_object (proxy, "g-properties-changed", G_CALLBACK (sync_brightness_rows), group,
                             G_CONNECT_SWAPPED);
    sync_brightness_rows (group);
}

static gboolean
setup_screen_off (GtkBox *box)
{
    /* Each row of GNOME's settings; the proxies come later, opening the page does not wait */
    g_autoptr(GSettings) power = settings_if_installed (POWER_SCHEMA, "ambient-enabled");
    g_autoptr(GSettings) session = settings_if_installed (SESSION_SCHEMA, "idle-delay");
    AdwPreferencesGroup *group;
    AdwSwitchRow *row;
    CcNumberRow *delay;
    const int delays[] = { 60, 120, 180, 240, 300, 480, 600, 720, 900 };

    if (power == NULL || session == NULL)
        return FALSE;

    group = new_group (box, P_("Brightness and Screen Off"), NULL);

    row = new_switch_row (group, _("A_utomatic Screen Brightness"), _("Adjust screen brightness to the surrounding light"));
    adw_preferences_row_set_use_underline (ADW_PREFERENCES_ROW (row), TRUE);
    gtk_widget_set_visible (GTK_WIDGET (row), FALSE);
    g_settings_bind (power, "ambient-enabled", row, "active", G_SETTINGS_BIND_DEFAULT);
    g_object_set_data (G_OBJECT (group), "plinius-auto-row", row);

    row = new_switch_row (group, _("_Dim Screen"), _("Reduce screen brightness when the device is inactive"));
    adw_preferences_row_set_use_underline (ADW_PREFERENCES_ROW (row), TRUE);
    gtk_widget_set_visible (GTK_WIDGET (row), FALSE);
    g_settings_bind (power, "idle-dim", row, "active", G_SETTINGS_BIND_DEFAULT);
    g_object_set_data (G_OBJECT (group), "plinius-dim-row", row);

    delay = cc_number_row_new (CC_NUMBER_VALUE_SECONDS, CC_NUMBER_SORT_ASCENDING);
    for (gsize i = 0; i < G_N_ELEMENTS (delays); i++)
        cc_number_row_add_value (delay, delays[i]);
    cc_number_row_add_value_full (delay, 0, _("Never"), CC_NUMBER_ORDER_LAST);
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (delay), _("_Blank Screen Delay"));
    adw_preferences_row_set_use_underline (ADW_PREFERENCES_ROW (delay), TRUE);
    adw_action_row_set_subtitle (ADW_ACTION_ROW (delay), _("Period of inactivity until screen blanks"));
    cc_number_row_bind_settings (delay, session, "idle-delay");
    adw_preferences_group_add (group, GTK_WIDGET (delay));

    g_dbus_proxy_new_for_bus (G_BUS_TYPE_SESSION,
                              G_DBUS_PROXY_FLAGS_DO_NOT_CONNECT_SIGNALS | G_DBUS_PROXY_FLAGS_DO_NOT_AUTO_START, NULL,
                              BRIGHTNESS_NAME, BRIGHTNESS_PATH, BRIGHTNESS_NAME, NULL, brightness_proxy_ready_cb,
                              g_object_ref (group));
    g_dbus_proxy_new_for_bus (G_BUS_TYPE_SYSTEM, G_DBUS_PROXY_FLAGS_DO_NOT_AUTO_START, NULL, SENSORS_NAME, SENSORS_PATH,
                              SENSORS_NAME, NULL, brightness_proxy_ready_cb, g_object_ref (group));
    return TRUE;
}

/* ----- Always-on display ----- */

static gboolean
setup_always_on (GtkBox *box)
{
    /* phosh-plinius before 0.57.0-40 has no always-on display */
    g_autoptr(GSettings) settings = settings_if_installed (DISPLAY_SCHEMA, "always-on-display");
    AdwPreferencesGroup *group;

    if (settings == NULL)
        return FALSE;

    /* One row, no group title */
    group = new_group (box, "", NULL);
    bind_switch (group, settings, "always-on-display", P_("Always-On Display"),
                 P_("Time and notifications on the dark screen at low power, not during calls"));
    return TRUE;
}

/* ----- Flip cover ----- */

static gboolean
setup_cover (GtkBox *box)
{
    g_autoptr(GSettings) settings = settings_if_installed (ASSIST_SCHEMA, "cover");
    AdwPreferencesGroup *group;

    if (settings == NULL)
        return FALSE;

    /* One row, no group title */
    group = new_group (box, "", NULL);
    bind_switch (group, settings, "cover", P_("Flip Cover"),
                 P_("Closing the cover turns the screen off, opening it turns the screen on"));
    return TRUE;
}

/**
 * cc_plinius_display_setup:
 * @box: a vertical box on the Display page
 *
 * Adds the phone's option groups to @box. Returns %FALSE if there is none.
 */
gboolean
cc_plinius_display_setup (GtkBox *box)
{
    gboolean any = FALSE;

    any |= setup_screen_off (box);
    any |= setup_always_on (box);
    any |= setup_wake (box);
    any |= setup_cover (box);
    return any;
}
