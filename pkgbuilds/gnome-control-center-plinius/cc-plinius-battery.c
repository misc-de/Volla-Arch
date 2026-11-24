/*
 * cc-plinius-battery.c
 *
 * Plinius: battery condition and charging options on the Power page.
 * - "Battery Condition": health, full charge, charge cycles, temperature, voltage, power and type
 *   of the battery, from UPower; updated whenever UPower reports a change.
 * - Charging options from plinius-charge (org.plinius.Charge): the charge limit behind GNOME's
 *   "Preserve Battery Health" choice where UPower has no charge thresholds for the battery, and a
 *   "Fast Charging" switch.
 * The group is shown on a page of its own (cc-plinius-battery-usage.c), which also takes its
 * full energy for the Battery Usage page.
 * Our own strings use the gettext domain plinius-control-center (not GNOME's po/).
 *
 * SPDX-License-Identifier: MIT
 */

#undef G_LOG_DOMAIN
#define G_LOG_DOMAIN "cc-plinius-battery"

#include <glib/gi18n.h>

#include "cc-plinius-battery.h"

#define PLINIUS_DOMAIN "plinius-control-center"
#define P_(String) g_dgettext (PLINIUS_DOMAIN, String)

#define CHARGE_NAME      "org.plinius.Charge"
#define CHARGE_PATH      "/org/plinius/Charge"
#define CHARGE_INTERFACE "org.plinius.Charge"

/* ----- Battery condition ----- */

/* A row with the value on the right; the rows are kept on the group by name */
static GtkWidget *
add_value_row (GtkWidget *group, const char *name, const char *title, const char *subtitle)
{
    GtkWidget *row = adw_action_row_new ();
    GtkWidget *value = gtk_label_new (NULL);

    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), title);
    if (subtitle)
        adw_action_row_set_subtitle (ADW_ACTION_ROW (row), subtitle);
    gtk_widget_add_css_class (value, "dim-label");
    gtk_label_set_selectable (GTK_LABEL (value), TRUE);
    adw_action_row_add_suffix (ADW_ACTION_ROW (row), value);
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), row);
    g_object_set_data (G_OBJECT (group), name, value);
    g_object_set_data (G_OBJECT (value), "row", row);
    return row;
}

/* Show the row of value label @name with @text, or hide it for NULL */
static void
set_value (GtkWidget *group, const char *name, const char *text)
{
    GtkWidget *value = g_object_get_data (G_OBJECT (group), name);

    gtk_label_set_text (GTK_LABEL (value), text ? text : "");
    gtk_widget_set_visible (g_object_get_data (G_OBJECT (value), "row"), text != NULL);
}

GtkWidget *
cc_plinius_battery_group_new (void)
{
    GtkWidget *group = adw_preferences_group_new ();

    adw_preferences_group_set_title (ADW_PREFERENCES_GROUP (group), P_("Battery Condition"));
    add_value_row (group, "health", P_("Health"), P_("Capacity compared to when the battery was new"));
    add_value_row (group, "full", P_("Full Charge"), NULL);
    add_value_row (group, "cycles", P_("Charge Cycles"), NULL);
    add_value_row (group, "temperature", P_("Temperature"), NULL);
    add_value_row (group, "voltage", P_("Voltage"), NULL);
    add_value_row (group, "power", P_("Power"), NULL);
    add_value_row (group, "technology", P_("Battery Type"), NULL);
    gtk_widget_set_visible (group, FALSE);
    return group;
}

static const char *
state_text (UpDeviceState state)
{
    switch (state) {
    case UP_DEVICE_STATE_CHARGING:
        return P_("Charging");
    case UP_DEVICE_STATE_DISCHARGING:
        return P_("Discharging");
    case UP_DEVICE_STATE_FULLY_CHARGED:
        return P_("Fully charged");
    /* The charge limit (or the charger itself) holds charging back */
    case UP_DEVICE_STATE_PENDING_CHARGE:
        return P_("Charging paused");
    default:
        return NULL;
    }
}

static const char *
technology_text (UpDeviceTechnology technology)
{
    switch (technology) {
    case UP_DEVICE_TECHNOLOGY_LITHIUM_ION:
        return P_("Lithium-ion");
    case UP_DEVICE_TECHNOLOGY_LITHIUM_POLYMER:
        return P_("Lithium polymer");
    case UP_DEVICE_TECHNOLOGY_LITHIUM_IRON_PHOSPHATE:
        return P_("Lithium iron phosphate");
    default:
        return NULL;
    }
}

void
cc_plinius_battery_group_update (GtkWidget *group, UpDevice *battery)
{
    g_autofree char *health = NULL, *full = NULL, *cycles = NULL, *temperature = NULL;
    g_autofree char *voltage = NULL, *power = NULL;
    double capacity = 0, energy_full = 0, energy_full_design = 0, temp = 0, volts = 0, rate = 0;
    int charge_cycles = -1;
    UpDeviceState state = UP_DEVICE_STATE_UNKNOWN;
    UpDeviceTechnology technology = UP_DEVICE_TECHNOLOGY_UNKNOWN;
    const char *state_str;

    gtk_widget_set_visible (group, battery != NULL);
    if (battery == NULL)
        return;

    g_object_get (battery, "capacity", &capacity, "energy-full", &energy_full, "energy-full-design",
                  &energy_full_design, "charge-cycles", &charge_cycles, "temperature", &temp, "voltage", &volts,
                  "energy-rate", &rate, "state", &state, "technology", &technology, NULL);

    /* Zero or less means the battery does not report it */
    if (capacity > 0)
        health = g_strdup_printf ("%.0f %%", capacity);
    if (energy_full > 0 && energy_full_design > 0)
        /* Translators: energy of a full battery now, of a new one */
        full = g_strdup_printf (P_("%.1f of %.1f Wh"), energy_full, energy_full_design);
    if (charge_cycles >= 0)
        cycles = g_strdup_printf ("%d", charge_cycles);
    if (temp != 0)
        temperature = g_strdup_printf ("%.0f °C", temp);
    if (volts > 0)
        voltage = g_strdup_printf ("%.2f V", volts);
    state_str = state_text (state);
    if (state_str && rate > 0)
        /* Translators: power flowing into or out of the battery, and the state, e.g. "1.2 W, Charging" */
        power = g_strdup_printf (P_("%.1f W, %s"), rate, state_str);
    else if (state_str)
        power = g_strdup (state_str);

    /* For the Battery Usage page: energy as a share of a full charge */
    {
        double *full_wh = g_new (double, 1);

        *full_wh = energy_full;
        g_object_set_data_full (G_OBJECT (group), "energy-full-wh", full_wh, g_free);
    }

    set_value (group, "health", health);
    set_value (group, "full", full);
    set_value (group, "cycles", cycles);
    set_value (group, "temperature", temperature);
    set_value (group, "voltage", voltage);
    set_value (group, "power", power);
    set_value (group, "technology", technology_text (technology));
}

/* ----- Charging options (plinius-charge) ----- */

GDBusProxy *
cc_plinius_charge_proxy_new (void)
{
    g_autoptr(GDBusProxy) proxy = NULL;
    g_autoptr(GVariant) has_limit = NULL;

    proxy = g_dbus_proxy_new_for_bus_sync (G_BUS_TYPE_SYSTEM, G_DBUS_PROXY_FLAGS_DO_NOT_AUTO_START, NULL,
                                           CHARGE_NAME, CHARGE_PATH, CHARGE_INTERFACE, NULL, NULL);
    /* Without the service the proxy has no owner and no properties */
    if (proxy == NULL || (has_limit = g_dbus_proxy_get_cached_property (proxy, "HasLimit")) == NULL)
        return NULL;
    return g_steal_pointer (&proxy);
}

gboolean
cc_plinius_charge_get (GDBusProxy *proxy, const char *property)
{
    g_autoptr(GVariant) value = proxy ? g_dbus_proxy_get_cached_property (proxy, property) : NULL;

    return value && g_variant_is_of_type (value, G_VARIANT_TYPE_BOOLEAN) && g_variant_get_boolean (value);
}

guint
cc_plinius_charge_get_level (GDBusProxy *proxy, const char *property)
{
    g_autoptr(GVariant) value = proxy ? g_dbus_proxy_get_cached_property (proxy, property) : NULL;

    return value && g_variant_is_of_type (value, G_VARIANT_TYPE_UINT32) ? g_variant_get_uint32 (value) : 0;
}

void
cc_plinius_charge_set (GDBusProxy *proxy, const char *property, gboolean on)
{
    g_autoptr(GError) error = NULL;
    g_autoptr(GVariant) reply = NULL;

    reply = g_dbus_proxy_call_sync (proxy, "org.freedesktop.DBus.Properties.Set",
                                    g_variant_new ("(ssv)", CHARGE_INTERFACE, property, g_variant_new_boolean (on)),
                                    G_DBUS_CALL_FLAGS_NONE, 2000, NULL, &error);
    if (reply == NULL)
        g_warning ("Setting %s failed: %s", property, error->message);
    else
        /* The service also emits PropertiesChanged; set the cache now so the UI follows at once */
        g_dbus_proxy_set_cached_property (proxy, property, g_variant_new_boolean (on));
}

GtkWidget *
cc_plinius_charge_fast_row_new (void)
{
    GtkWidget *row = adw_switch_row_new ();

    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_("Fast Charging"));
    adw_action_row_set_subtitle (ADW_ACTION_ROW (row), P_("Charges faster with a suitable charger. The battery gets warmer."));
    return row;
}

char *
cc_plinius_charge_limit_subtitle (GDBusProxy *proxy)
{
    /* Translators: the charge limit, e.g. "Charging stops at 80 % and starts again at 75 %" */
    return g_strdup_printf (P_("Charging stops at %u %% and starts again at %u %%"),
                            cc_plinius_charge_get_level (proxy, "LimitStop"),
                            cc_plinius_charge_get_level (proxy, "LimitStart"));
}
