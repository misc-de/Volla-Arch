// SPDX-License-Identifier: MIT
// plinius-charge: battery charging options of the Volla Phone Plinius, set in GNOME Settings > Power
// through org.plinius.Charge.
//
// Limit ("Preserve Battery Health"): charging stops at LIMIT_STOP and starts again at LIMIT_START,
// the levels UPower uses for batteries with charge_control_*_threshold. This battery has no such
// attributes; MediaTek's charger driver only offers cmd_charge_disable, so this service watches
// the level and switches it. The phone keeps running from USB while charging is stopped.
// Fast charging: user_fast_charge_enabled of the same driver (higher voltage and current).
//
// When the service stops, charging is allowed again, so a stopped or crashed service never
// leaves the battery without charge (Restart=on-failure brings the limit back).
#include <errno.h>
#include <gio/gio.h>
#include <glib-unix.h>
#include <string.h>

#define BUS_NAME "org.plinius.Charge"
#define OBJECT_PATH "/org/plinius/Charge"
#define INTERFACE "org.plinius.Charge"
#define SETTINGS_FILE "/var/lib/plinius-charge/settings"

#define CHARGER_DIR "/sys/devices/platform/charger"
#define CHARGE_DISABLE CHARGER_DIR "/cmd_charge_disable"
#define FAST_CHARGE CHARGER_DIR "/user_fast_charge_enabled"
#define BATTERY_CAPACITY "/sys/class/power_supply/battery/capacity"

/* Same levels as UPower's default charge thresholds */
#define LIMIT_STOP 80
#define LIMIT_START 75
/* How often the level is checked while the limit is on; at most about 1 % per minute when
 * charging fast, so it never goes far beyond LIMIT_STOP */
#define CHECK_INTERVAL_S 60

static const char introspection[] =
	"<node>"
	" <interface name='" INTERFACE "'>"
	"  <property name='Limit' type='b' access='readwrite'/>"
	"  <property name='FastCharge' type='b' access='readwrite'/>"
	"  <property name='HasLimit' type='b' access='read'/>"
	"  <property name='HasFastCharge' type='b' access='read'/>"
	"  <property name='LimitStop' type='u' access='read'/>"
	"  <property name='LimitStart' type='u' access='read'/>"
	" </interface>"
	"</node>";

static struct {
	GMainLoop *loop;
	GDBusConnection *bus;
	gboolean limit;
	gboolean fast_charge;
	/* Charging stopped by the limit (cmd_charge_disable is 1) */
	gboolean stopped;
	guint check_source;
} self = {
	.fast_charge = TRUE,
};

/* ---- Charger driver ---- */

static gboolean write_sysfs(const char *path, int value)
{
	g_autoptr(GError) error = NULL;
	g_autofree char *text = g_strdup_printf("%d\n", value);

	if (!g_file_set_contents_full(path, text, -1, G_FILE_SET_CONTENTS_NONE, 0644, &error)) {
		g_warning("Cannot write %s: %s", path, error->message);
		return FALSE;
	}
	return TRUE;
}

static int read_sysfs(const char *path)
{
	g_autofree char *text = NULL;

	if (!g_file_get_contents(path, &text, NULL, NULL))
		return -1;
	return (int)g_ascii_strtoll(text, NULL, 10);
}

static void set_stopped(gboolean stopped)
{
	if (stopped == self.stopped)
		return;
	if (write_sysfs(CHARGE_DISABLE, stopped)) {
		self.stopped = stopped;
		g_message("Charging %s", stopped ? "stopped at the limit" : "allowed");
	}
}

/* Hysteresis between LIMIT_START and LIMIT_STOP, so charging does not toggle at every percent */
static gboolean check_level(gpointer user_data)
{
	int capacity = read_sysfs(BATTERY_CAPACITY);

	if (capacity < 0)
		return G_SOURCE_CONTINUE;
	if (capacity >= LIMIT_STOP)
		set_stopped(TRUE);
	else if (capacity <= LIMIT_START)
		set_stopped(FALSE);
	return G_SOURCE_CONTINUE;
}

static void apply_limit(void)
{
	if (self.limit) {
		check_level(NULL);
		if (!self.check_source)
			self.check_source = g_timeout_add_seconds(CHECK_INTERVAL_S, check_level, NULL);
	} else {
		g_clear_handle_id(&self.check_source, g_source_remove);
		set_stopped(FALSE);
	}
}

static void apply_fast_charge(void)
{
	if (g_file_test(FAST_CHARGE, G_FILE_TEST_EXISTS))
		write_sysfs(FAST_CHARGE, self.fast_charge);
}

/* ---- Settings and D-Bus ---- */

static void load_settings(void)
{
	g_autoptr(GKeyFile) file = g_key_file_new();
	g_autoptr(GError) error = NULL;
	gboolean fast;

	if (!g_key_file_load_from_file(file, SETTINGS_FILE, G_KEY_FILE_NONE, NULL))
		return;
	self.limit = g_key_file_get_boolean(file, "charge", "limit", NULL);
	/* Fast charging is on unless switched off */
	fast = g_key_file_get_boolean(file, "charge", "fast", &error);
	self.fast_charge = error ? TRUE : fast;
}

static void save_settings(void)
{
	g_autoptr(GKeyFile) file = g_key_file_new();
	g_autoptr(GError) error = NULL;

	g_key_file_set_boolean(file, "charge", "limit", self.limit);
	g_key_file_set_boolean(file, "charge", "fast", self.fast_charge);
	if (!g_key_file_save_to_file(file, SETTINGS_FILE, &error))
		g_warning("Cannot save the settings: %s", error->message);
}

static GVariant *on_get_property(GDBusConnection *bus, const char *sender, const char *path, const char *iface,
				 const char *property, GError **error, gpointer user_data)
{
	if (!strcmp(property, "Limit"))
		return g_variant_new_boolean(self.limit);
	if (!strcmp(property, "FastCharge"))
		return g_variant_new_boolean(self.fast_charge);
	if (!strcmp(property, "HasLimit"))
		return g_variant_new_boolean(g_file_test(CHARGE_DISABLE, G_FILE_TEST_EXISTS));
	if (!strcmp(property, "HasFastCharge"))
		return g_variant_new_boolean(g_file_test(FAST_CHARGE, G_FILE_TEST_EXISTS));
	if (!strcmp(property, "LimitStop"))
		return g_variant_new_uint32(LIMIT_STOP);
	if (!strcmp(property, "LimitStart"))
		return g_variant_new_uint32(LIMIT_START);
	g_set_error(error, G_DBUS_ERROR, G_DBUS_ERROR_UNKNOWN_PROPERTY, "Unknown property %s", property);
	return NULL;
}

static void emit_changed(const char *property, GVariant *value)
{
	GVariantBuilder changed;

	g_variant_builder_init(&changed, G_VARIANT_TYPE("a{sv}"));
	g_variant_builder_add(&changed, "{sv}", property, value);
	g_dbus_connection_emit_signal(self.bus, NULL, OBJECT_PATH, "org.freedesktop.DBus.Properties",
				      "PropertiesChanged", g_variant_new("(sa{sv}as)", INTERFACE, &changed, NULL),
				      NULL);
}

static gboolean on_set_property(GDBusConnection *bus, const char *sender, const char *path, const char *iface,
				const char *property, GVariant *value, GError **error, gpointer user_data)
{
	gboolean on = g_variant_get_boolean(value);

	if (!strcmp(property, "Limit")) {
		if (on == self.limit)
			return TRUE;
		self.limit = on;
		apply_limit();
	} else if (!strcmp(property, "FastCharge")) {
		if (on == self.fast_charge)
			return TRUE;
		self.fast_charge = on;
		apply_fast_charge();
	} else {
		g_set_error(error, G_DBUS_ERROR, G_DBUS_ERROR_PROPERTY_READ_ONLY, "Property %s is read-only",
			    property);
		return FALSE;
	}
	save_settings();
	emit_changed(property, g_variant_new_boolean(on));
	return TRUE;
}

static const GDBusInterfaceVTable vtable = {NULL, on_get_property, on_set_property, {0}};

static void on_bus_acquired(GDBusConnection *bus, const char *name, gpointer user_data)
{
	GDBusNodeInfo *info = g_dbus_node_info_new_for_xml(introspection, NULL);

	self.bus = bus;
	g_dbus_connection_register_object(bus, OBJECT_PATH, info->interfaces[0], &vtable, NULL, NULL, NULL);
	g_dbus_node_info_unref(info);
}

static void on_name_lost(GDBusConnection *bus, const char *name, gpointer user_data)
{
	g_warning("Lost or could not own %s", name);
	g_main_loop_quit(self.loop);
}

static gboolean on_signal(gpointer user_data)
{
	g_main_loop_quit(self.loop);
	return G_SOURCE_REMOVE;
}

int main(void)
{
	guint owner;

	load_settings();
	/* Start from what the driver has: a previous instance may have stopped charging */
	self.stopped = read_sysfs(CHARGE_DISABLE) == 1;
	apply_fast_charge();
	apply_limit();

	self.loop = g_main_loop_new(NULL, FALSE);
	owner = g_bus_own_name(G_BUS_TYPE_SYSTEM, BUS_NAME, G_BUS_NAME_OWNER_FLAGS_NONE, on_bus_acquired, NULL,
			       on_name_lost, NULL, NULL);
	g_unix_signal_add(SIGTERM, on_signal, NULL);
	g_unix_signal_add(SIGINT, on_signal, NULL);
	g_main_loop_run(self.loop);

	/* Never leave the battery without charging */
	set_stopped(FALSE);
	g_bus_unown_name(owner);
	return 0;
}
