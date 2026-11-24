// plinius-mce: the part of Sailfish's MCE (com.nokia.mce) that ofono needs. ofono-binder-plugin
// reads display, charger and battery through libmce-glib and tells the modem when the display is
// off and no charger is plugged in (sendDeviceState LOW_DATA_EXPECTED, setIndicationFilter).
// Without it the modem keeps reporting signal strength and cells, and its interrupt (CCIF) wakes
// the phone right after every suspend (docs/STANDBY.md).
// Sources: the display state from the hook of plinius-backlight-wake (/usr/lib/plinius/display.d),
// which calls org.plinius.Mce.SetDisplay (root only); charger and battery from the kernel's battery
// (power_supply uevents). UPower is no help here: it also counts the UPS "ext-bat", which always
// discharges, and reported the battery as discharging while the kernel said charging.
// Only the requests and signals libmce-glib uses: display, charger, battery, tklock, inactivity.
// With the display it also switches the Wi-Fi driver's suspend mode, as Android's framework does:
// in it, MediaTek's driver drops broadcasts of the network (ARP, mDNS) instead of waking the phone
// for each (about every 8 s at home, measured 2026-11-01).
// SPDX-License-Identifier: MIT
#include <errno.h>
#include <fcntl.h>
#include <gio/gio.h>
#include <glib-unix.h>
#include <libudev.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define DPMS_FILE "/sys/class/drm/card0-DSI-1/dpms"
/* Private commands of MediaTek's gen4m Wi-Fi driver */
#define WLAN_DRIVER_CMD "/proc/net/wlan/driver"
#define BATTERY "/sys/class/power_supply/battery"

#define MCE_SERVICE "com.nokia.mce"
#define MCE_REQUEST_PATH "/com/nokia/mce/request"
#define MCE_REQUEST_IF "com.nokia.mce.request"
#define MCE_SIGNAL_PATH "/com/nokia/mce/signal"
#define MCE_SIGNAL_IF "com.nokia.mce.signal"

#define PLINIUS_PATH "/org/plinius/Mce"
#define PLINIUS_IF "org.plinius.Mce"

// Battery levels for "low" and "empty", as MCE's defaults
#define LEVEL_LOW 15
#define LEVEL_EMPTY 5

static const char introspection[] =
	"<node>"
	"  <interface name='" MCE_REQUEST_IF "'>"
	"    <method name='get_display_status'><arg direction='out' type='s'/></method>"
	"    <method name='get_charger_state'><arg direction='out' type='s'/></method>"
	"    <method name='get_battery_status'><arg direction='out' type='s'/></method>"
	"    <method name='get_battery_level'><arg direction='out' type='i'/></method>"
	"    <method name='get_tklock_mode'><arg direction='out' type='s'/></method>"
	"    <method name='get_inactivity_status'><arg direction='out' type='b'/></method>"
	"  </interface>"
	"  <interface name='" PLINIUS_IF "'>"
	"    <method name='SetDisplay'><arg direction='in' type='s'/></method>"
	"  </interface>"
	"</node>";

static struct {
	GDBusConnection *bus;
	char display[8];	// "on" or "off"
	const char *charger;	// "on", "off" or "unknown"
	const char *battery_status;	// "full", "ok", "low", "empty" or "unknown"
	int battery_level;
} mce = { .display = "on", .charger = "unknown", .battery_status = "unknown", .battery_level = -1 };

static void emit(const char *name, GVariant *args)
{
	if (mce.bus)
		g_dbus_connection_emit_signal(mce.bus, NULL, MCE_SIGNAL_PATH, MCE_SIGNAL_IF, name, args, NULL);
}

// Wi-Fi suspend mode on with the display off. Without the driver (Wi-Fi not loaded yet) nothing
// happens; the next change sets it.
static void set_wlan_suspend(gboolean on)
{
	const char *cmd = on ? "SETSUSPENDMODE 1" : "SETSUSPENDMODE 0";
	int fd = open(WLAN_DRIVER_CMD, O_WRONLY | O_CLOEXEC);
	if (fd < 0)
		return;
	if (write(fd, cmd, strlen(cmd)) < 0)
		g_warning("Wi-Fi suspend mode: %s", g_strerror(errno));
	close(fd);
}

// Takes the display state "on"/"off"; at start (notify FALSE) from the DRM connector
static void set_display(const char *state, gboolean notify)
{
	if (strcmp(state, mce.display) == 0)
		return;
	g_strlcpy(mce.display, strcmp(state, "off") == 0 ? "off" : "on", sizeof(mce.display));
	g_message("display %s", mce.display);
	set_wlan_suspend(strcmp(mce.display, "off") == 0);
	if (notify) {
		emit("display_status_ind", g_variant_new("(s)", mce.display));
		emit("system_inactivity_ind", g_variant_new("(b)", strcmp(mce.display, "off") == 0));
	}
}

static void read_display(void)
{
	g_autofree char *text = NULL;
	if (g_file_get_contents(DPMS_FILE, &text, NULL, NULL))
		set_display(strcmp(g_strstrip(text), "On") == 0 ? "on" : "off", FALSE);
}

static char *read_attr(const char *name)
{
	g_autofree char *path = g_build_filename(BATTERY, name, NULL);
	char *text = NULL;
	if (!g_file_get_contents(path, &text, NULL, NULL))
		return NULL;
	return g_strstrip(text);
}

// Charger and battery from the kernel. "Not charging" is the 80 % limit of plinius-charge: the
// charger is plugged in all the same.
static void read_power(gboolean notify)
{
	g_autofree char *state = read_attr("status");
	g_autofree char *capacity = read_attr("capacity");

	const char *charger = "unknown";
	if (state && strcmp(state, "Discharging") == 0)
		charger = "off";
	else if (state && (strcmp(state, "Charging") == 0 || strcmp(state, "Full") == 0 ||
			   strcmp(state, "Not charging") == 0))
		charger = "on";
	int level = capacity ? atoi(capacity) : -1;
	const char *status = "unknown";
	if (level >= 0) {
		if (state && strcmp(state, "Full") == 0)
			status = "full";
		else if (level <= LEVEL_EMPTY)
			status = "empty";
		else if (level <= LEVEL_LOW)
			status = "low";
		else
			status = "ok";
	}

	if (strcmp(charger, mce.charger) != 0) {
		mce.charger = charger;
		g_message("charger %s", charger);
		if (notify)
			emit("charger_state_ind", g_variant_new("(s)", charger));
	}
	if (strcmp(status, mce.battery_status) != 0) {
		mce.battery_status = status;
		if (notify)
			emit("battery_status_ind", g_variant_new("(s)", status));
	}
	if (level != mce.battery_level) {
		mce.battery_level = level;
		if (notify && level >= 0)
			emit("battery_level_ind", g_variant_new("(i)", level));
	}
}

// Any power_supply uevent (cable plugged in or out, level, charging stopped)
static gboolean on_uevent(int fd, GIOCondition condition, gpointer data)
{
	struct udev_device *dev = udev_monitor_receive_device(data);
	if (dev)
		udev_device_unref(dev);
	read_power(TRUE);
	return G_SOURCE_CONTINUE;
}

static void handle_method(GDBusConnection *bus, const char *sender, const char *path, const char *iface,
			  const char *method, GVariant *params, GDBusMethodInvocation *invocation, gpointer data)
{
	if (strcmp(iface, PLINIUS_IF) == 0) {
		// SetDisplay: only root may call it (com.nokia.mce.conf)
		const char *state;
		g_variant_get(params, "(&s)", &state);
		set_display(state, TRUE);
		g_dbus_method_invocation_return_value(invocation, NULL);
	} else if (strcmp(method, "get_display_status") == 0)
		g_dbus_method_invocation_return_value(invocation, g_variant_new("(s)", mce.display));
	else if (strcmp(method, "get_charger_state") == 0)
		g_dbus_method_invocation_return_value(invocation, g_variant_new("(s)", mce.charger));
	else if (strcmp(method, "get_battery_status") == 0)
		g_dbus_method_invocation_return_value(invocation, g_variant_new("(s)", mce.battery_status));
	else if (strcmp(method, "get_battery_level") == 0)
		g_dbus_method_invocation_return_value(invocation, g_variant_new("(i)", mce.battery_level));
	else if (strcmp(method, "get_tklock_mode") == 0)
		// Phosh has its own lock screen; MCE's touch lock does not exist here
		g_dbus_method_invocation_return_value(invocation, g_variant_new("(s)", "unlocked"));
	else if (strcmp(method, "get_inactivity_status") == 0)
		g_dbus_method_invocation_return_value(invocation,
						      g_variant_new("(b)", strcmp(mce.display, "off") == 0));
	else
		g_dbus_method_invocation_return_error(invocation, G_DBUS_ERROR, G_DBUS_ERROR_UNKNOWN_METHOD,
						      "unknown method %s", method);
}

static const GDBusInterfaceVTable vtable = { .method_call = handle_method };

static void on_bus_acquired(GDBusConnection *bus, const char *name, gpointer data)
{
	g_autoptr(GDBusNodeInfo) info = g_dbus_node_info_new_for_xml(introspection, NULL);
	g_autoptr(GError) error = NULL;
	mce.bus = bus;
	if (!g_dbus_connection_register_object(bus, MCE_REQUEST_PATH, info->interfaces[0], &vtable,
					       NULL, NULL, &error) ||
	    !g_dbus_connection_register_object(bus, PLINIUS_PATH, info->interfaces[1], &vtable,
					       NULL, NULL, &error))
		g_error("register: %s", error->message);
}

static void on_name_lost(GDBusConnection *bus, const char *name, gpointer data)
{
	g_error("could not own %s", name);
}

int main(void)
{
	read_display();
	// Also when the display is on: a previous run may have left the suspend mode on
	set_wlan_suspend(strcmp(mce.display, "off") == 0);
	read_power(FALSE);

	struct udev *udev = udev_new();
	struct udev_monitor *monitor = udev ? udev_monitor_new_from_netlink(udev, "udev") : NULL;
	if (!monitor || udev_monitor_filter_add_match_subsystem_devtype(monitor, "power_supply", NULL) < 0 ||
	    udev_monitor_enable_receiving(monitor) < 0)
		g_error("udev monitor for power_supply failed");
	g_unix_fd_add(udev_monitor_get_fd(monitor), G_IO_IN, on_uevent, monitor);

	g_bus_own_name(G_BUS_TYPE_SYSTEM, MCE_SERVICE, G_BUS_NAME_OWNER_FLAGS_NONE, on_bus_acquired, NULL,
		       on_name_lost, NULL, NULL);
	g_main_loop_run(g_main_loop_new(NULL, FALSE));
	return 0;
}
