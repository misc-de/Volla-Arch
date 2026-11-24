// SPDX-License-Identifier: MIT
// plinius-wake: wake the screen by a double tap on it or by tilting the phone out of lying flat,
// and brighten a dimmed screen again when the phone is picked up.
// Switched on and off in GNOME Settings > Display through org.plinius.Wake.
//
// Double tap: the FocalTech touch driver keeps the chip in gesture mode while the display is off
// once fts_gesture_mode is 1, and reports a double tap only by setting double_tap_pressed to 1
// (sysfs_notify), not as an input event. Tilt: the sensor hub's pick-up gesture, which
// sensor-proxy-bridge offers as org.plinius.SensorProxyBridge.PickedUp; a wakeup sensor, so it also
// works while the phone is suspended and costs no accelerometer running all the time. Without it,
// iio-sensor-proxy's AccelerometerTilt: lying face up for a while and then tilted counts.
// The gesture fires whether the screen is on, dimmed or off (measured 2026-10-31); the DRM
// connector's dpms tells which: with the screen off only Tilt wakes it, with the screen on
// PickUpBrighten counts the pick-up as activity, which ends gsd-power's idle dimming.
//
// The pick-up sensor is a wakeup sensor: claimed, it can wake the phone from suspend. So it runs
// with the screen off only when Tilt is on; PickUpBrighten needs it only while the screen is on.
// The screen state comes from the hook of plinius-backlight-wake (SetDisplay, root only).
//
// Either way the screen comes on through a KEY_WAKEUP from a uinput device: phoc takes every key
// as activity, logind, gsd-media-keys and Phosh ignore this one (docs: HARDWARE.md). Nothing
// wakes while the proximity sensor is covered (in a pocket).
#include <errno.h>
#include <fcntl.h>
#include <gio/gio.h>
#include <glib-unix.h>
#include <glob.h>
#include <linux/uinput.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

#define BUS_NAME "org.plinius.Wake"
#define OBJECT_PATH "/org/plinius/Wake"
#define INTERFACE "org.plinius.Wake"
#define SETTINGS_FILE "/var/lib/plinius-wake/settings"

#define SPI_DEVICES "/sys/bus/spi/devices"
#define TOUCH_GESTURE_MODE "fts_gesture_mode"
#define TOUCH_DOUBLE_TAP "double_tap_pressed"
/* The touch module can come after us: look again this often while double tap is on */
#define TOUCH_RESCAN_S 5

/* "On" in a connector's dpms while the screen is on; follows Phosh's PowerSaveMode */
#define DRM_CLASS "/sys/class/drm"
/* Created by phosh-plinius while the always-on display is shown */
#define AOD_FLAG "/run/user/*/plinius-aod"

#define SENSORS_NAME "net.hadess.SensorProxy"
#define SENSORS_PATH "/net/hadess/SensorProxy"
#define BRIDGE_INTERFACE "org.plinius.SensorProxyBridge"
/* How long the phone must lie face up before tilting it wakes the screen */
#define FLAT_HOLD_TIME (1500 * G_TIME_SPAN_MILLISECOND)
/* How long the proximity sensor gets to answer after it is claimed */
#define PROXIMITY_WAIT_MS 300

static const char introspection[] =
	"<node>"
	" <interface name='" INTERFACE "'>"
	"  <property name='DoubleTap' type='b' access='readwrite'/>"
	"  <property name='Tilt' type='b' access='readwrite'/>"
	"  <property name='PickUpBrighten' type='b' access='readwrite'/>"
	"  <property name='HasDoubleTap' type='b' access='read'/>"
	"  <property name='HasTilt' type='b' access='read'/>"
	"  <method name='SetDisplay'><arg direction='in' type='b'/></method>"
	" </interface>"
	"</node>";

static struct {
	GMainLoop *loop;
	GDBusConnection *bus;
	int uinput_fd;
	gboolean double_tap;
	gboolean tilt;
	gboolean brighten;
	/* Double tap: the touch device's sysfs directory, the watched double_tap_pressed */
	char *touch_dir;
	int tap_fd;
	guint tap_source;
	guint rescan_source;
	/* Tilt and proximity: sensor-proxy-bridge */
	GDBusProxy *sensors;
	GDBusProxy *bridge;
	gboolean accelerometer_claimed;
	gboolean pick_up_claimed;
	/* Screen on, from the display hook; PickUpBrighten needs the sensor only then */
	gboolean display_on;
	gboolean flat;
	gint64 flat_since;
	guint proximity_source;
} self = {
	.uinput_fd = -1,
	.tap_fd = -1,
};

/* ---- Waking ---- */

static void emit_key(int value)
{
	struct input_event events[2] = {
		{.type = EV_KEY, .code = KEY_WAKEUP, .value = value},
		{.type = EV_SYN, .code = SYN_REPORT, .value = 0},
	};
	if (write(self.uinput_fd, events, sizeof(events)) < 0)
		g_warning("Writing the wake key failed: %s", g_strerror(errno));
}

static void send_wake_key(void)
{
	emit_key(1);
	emit_key(0);
}

static gboolean proximity_answered(gpointer user_data)
{
	const char *reason = user_data;
	g_autoptr(GVariant) near = g_dbus_proxy_get_cached_property(self.sensors, "ProximityNear");

	self.proximity_source = 0;
	g_dbus_proxy_call(self.sensors, "ReleaseProximity", NULL, G_DBUS_CALL_FLAGS_NONE, -1, NULL, NULL, NULL);
	if (near && g_variant_get_boolean(near)) {
		g_message("%s ignored: proximity sensor covered", reason);
		return G_SOURCE_REMOVE;
	}
	g_message("Waking the screen (%s)", reason);
	send_wake_key();
	return G_SOURCE_REMOVE;
}

/* Wakes the screen unless the proximity sensor is covered */
static void request_wake(const char *reason)
{
	g_autoptr(GVariant) has_proximity = NULL;
	g_autoptr(GError) error = NULL;
	g_autoptr(GVariant) reply = NULL;

	if (self.proximity_source)
		return; /* a check is already running */

	if (self.sensors)
		has_proximity = g_dbus_proxy_get_cached_property(self.sensors, "HasProximity");
	if (!has_proximity || !g_variant_get_boolean(has_proximity)) {
		g_message("Waking the screen (%s)", reason);
		send_wake_key();
		return;
	}

	reply = g_dbus_proxy_call_sync(self.sensors, "ClaimProximity", NULL, G_DBUS_CALL_FLAGS_NONE, 1000, NULL,
				       &error);
	if (!reply) {
		g_warning("Cannot claim the proximity sensor: %s", error->message);
		send_wake_key();
		return;
	}
	self.proximity_source = g_timeout_add(PROXIMITY_WAIT_MS, proximity_answered, (gpointer)reason);
}

/* ---- Screen state ---- */

/* Whether any connector is on; unknown counts as off, so that only Tilt can wake */
static gboolean screen_on(void)
{
	g_autoptr(GDir) dir = NULL;
	const char *name;
	glob_t aod;
	gboolean dozing;

	/* The always-on display counts as off: only Tilt wakes it, and dpms goes Off and On with the
	 * CRTC for each new picture */
	dozing = glob(AOD_FLAG, GLOB_NOSORT, NULL, &aod) == 0 && aod.gl_pathc > 0;
	globfree(&aod);
	if (dozing)
		return FALSE;

	dir = g_dir_open(DRM_CLASS, 0, NULL);
	while (dir && (name = g_dir_read_name(dir))) {
		g_autofree char *path = g_build_filename(DRM_CLASS, name, "dpms", NULL);
		g_autofree char *dpms = NULL;

		if (g_file_get_contents(path, &dpms, NULL, NULL) && g_str_has_prefix(dpms, "On"))
			return TRUE;
	}
	return FALSE;
}

/* ---- Double tap ---- */

static gboolean write_sysfs(const char *dir, const char *name, const char *value)
{
	g_autofree char *path = g_build_filename(dir, name, NULL);
	g_autoptr(GError) error = NULL;

	if (!g_file_set_contents_full(path, value, -1, G_FILE_SET_CONTENTS_NONE, 0644, &error)) {
		g_warning("Cannot write %s: %s", path, error->message);
		return FALSE;
	}
	return TRUE;
}

static char *find_touch_dir(void)
{
	g_autoptr(GDir) dir = g_dir_open(SPI_DEVICES, 0, NULL);
	const char *name;

	while (dir && (name = g_dir_read_name(dir))) {
		g_autofree char *candidate = g_build_filename(SPI_DEVICES, name, NULL);
		g_autofree char *mode = g_build_filename(candidate, TOUCH_GESTURE_MODE, NULL);
		g_autofree char *tap = g_build_filename(candidate, TOUCH_DOUBLE_TAP, NULL);

		if (g_file_test(mode, G_FILE_TEST_EXISTS) && g_file_test(tap, G_FILE_TEST_EXISTS))
			return g_steal_pointer(&candidate);
	}
	return NULL;
}

/* double_tap_pressed reports through POLLPRI; reading from the start re-arms it */
static gboolean on_double_tap(int fd, GIOCondition condition, gpointer user_data)
{
	char value = '0';

	if (lseek(fd, 0, SEEK_SET) < 0 || read(fd, &value, 1) < 1) {
		g_warning("Reading %s failed: %s", TOUCH_DOUBLE_TAP, g_strerror(errno));
		return G_SOURCE_CONTINUE;
	}
	if (value == '1')
		request_wake("double tap");
	return G_SOURCE_CONTINUE;
}

static void stop_double_tap(void)
{
	g_clear_handle_id(&self.tap_source, g_source_remove);
	if (self.tap_fd >= 0) {
		close(self.tap_fd);
		self.tap_fd = -1;
	}
	if (self.touch_dir)
		write_sysfs(self.touch_dir, TOUCH_GESTURE_MODE, "0");
}

static gboolean start_double_tap(void)
{
	g_autofree char *path = NULL;
	char value;

	if (!self.touch_dir)
		self.touch_dir = find_touch_dir();
	if (!self.touch_dir)
		return FALSE;

	/* Must be set before the display goes off: the driver takes it over then */
	if (!write_sysfs(self.touch_dir, TOUCH_GESTURE_MODE, "1"))
		return FALSE;

	path = g_build_filename(self.touch_dir, TOUCH_DOUBLE_TAP, NULL);
	self.tap_fd = open(path, O_RDONLY | O_CLOEXEC);
	if (self.tap_fd < 0) {
		g_warning("Cannot open %s: %s", path, g_strerror(errno));
		return FALSE;
	}
	/* A first read, otherwise poll() reports at once */
	if (read(self.tap_fd, &value, 1) < 0)
		g_warning("Reading %s failed: %s", path, g_strerror(errno));
	self.tap_source = g_unix_fd_add(self.tap_fd, G_IO_PRI | G_IO_ERR, on_double_tap, NULL);
	g_message("Double tap to wake on (%s)", self.touch_dir);
	return TRUE;
}

static gboolean rescan_touch(gpointer user_data)
{
	if (!self.double_tap || start_double_tap()) {
		self.rescan_source = 0;
		return G_SOURCE_REMOVE;
	}
	return G_SOURCE_CONTINUE;
}

static void apply_double_tap(void)
{
	stop_double_tap();
	g_clear_handle_id(&self.rescan_source, g_source_remove);
	if (self.double_tap && !start_double_tap())
		self.rescan_source = g_timeout_add_seconds(TOUCH_RESCAN_S, rescan_touch, NULL);
}

/* ---- Tilt and pick-up ---- */

/* The phone was picked up (or tilted out of lying flat) */
static void on_picked_up(const char *reason)
{
	if (screen_on() ? self.brighten : self.tilt)
		request_wake(reason);
}

static void on_tilt_changed(const char *tilt)
{
	gint64 now = g_get_monotonic_time();

	if (!g_strcmp0(tilt, "face-up")) {
		if (!self.flat) {
			self.flat = TRUE;
			self.flat_since = now;
		}
		return;
	}
	if (self.flat && now - self.flat_since >= FLAT_HOLD_TIME)
		on_picked_up("tilt");
	self.flat = FALSE;
}

static void on_sensors_properties_changed(GDBusProxy *proxy, GVariant *changed, GStrv invalidated,
					  gpointer user_data)
{
	const char *tilt;

	/* Only as the fallback without the sensor hub's pick-up gesture: Phosh claims the
	 * accelerometer for rotation anyway, so its tilt changes arrive either way. Holding the phone
	 * flat in the hand and pressing the power key counted as a pick-up and woke the screen again
	 * at once. */
	if (self.accelerometer_claimed && (self.tilt || self.brighten) &&
	    g_variant_lookup(changed, "AccelerometerTilt", "&s", &tilt))
		on_tilt_changed(tilt);
}

static void on_bridge_signal(GDBusProxy *proxy, const char *sender, const char *signal, GVariant *params,
			     gpointer user_data)
{
	if (!strcmp(signal, "PickedUp"))
		on_picked_up("pick-up");
}

static gboolean proxy_has(GDBusProxy *proxy, const char *property)
{
	g_autoptr(GVariant) has = NULL;
	g_autofree char *owner = proxy ? g_dbus_proxy_get_name_owner(proxy) : NULL;

	if (owner)
		has = g_dbus_proxy_get_cached_property(proxy, property);
	return has && g_variant_get_boolean(has);
}

/* Claims or releases a sensor of sensor-proxy-bridge; *claimed follows the result */
static void claim_sensor(GDBusProxy *proxy, const char *name, gboolean want, gboolean *claimed)
{
	g_autofree char *method = g_strconcat(want ? "Claim" : "Release", name, NULL);
	g_autoptr(GError) error = NULL;
	g_autoptr(GVariant) reply = NULL;

	if (want == *claimed)
		return;
	reply = g_dbus_proxy_call_sync(proxy, method, NULL, G_DBUS_CALL_FLAGS_NONE, 1000, NULL, &error);
	if (!reply && want) {
		g_warning("Cannot claim the %s sensor: %s", name, error->message);
		return;
	}
	*claimed = want;
	if (want)
		g_message("Pick-up detection on (%s)", name);
}

static void apply_tilt(void)
{
	gboolean pick_up = proxy_has(self.bridge, "HasPickUp");
	gboolean want = self.tilt || (self.brighten && self.display_on);

	claim_sensor(self.bridge, "PickUp", want && pick_up, &self.pick_up_claimed);
	claim_sensor(self.sensors, "Accelerometer", want && !pick_up && proxy_has(self.sensors, "HasAccelerometer"),
		     &self.accelerometer_claimed);
	self.flat = FALSE;
}

/* sensor-proxy-bridge (re)started or went away: its claims are gone */
static void on_sensors_owner_changed(GObject *proxy, GParamSpec *pspec, gpointer user_data)
{
	self.accelerometer_claimed = FALSE;
	self.pick_up_claimed = FALSE;
	apply_tilt();
}

/* ---- Settings and D-Bus ---- */

static void load_settings(void)
{
	g_autoptr(GKeyFile) file = g_key_file_new();

	if (!g_key_file_load_from_file(file, SETTINGS_FILE, G_KEY_FILE_NONE, NULL))
		return;
	self.double_tap = g_key_file_get_boolean(file, "wake", "double-tap", NULL);
	self.tilt = g_key_file_get_boolean(file, "wake", "tilt", NULL);
	self.brighten = g_key_file_get_boolean(file, "wake", "pick-up-brighten", NULL);
}

static void save_settings(void)
{
	g_autoptr(GKeyFile) file = g_key_file_new();
	g_autoptr(GError) error = NULL;

	g_key_file_set_boolean(file, "wake", "double-tap", self.double_tap);
	g_key_file_set_boolean(file, "wake", "tilt", self.tilt);
	g_key_file_set_boolean(file, "wake", "pick-up-brighten", self.brighten);
	if (!g_key_file_save_to_file(file, SETTINGS_FILE, &error))
		g_warning("Cannot save the settings: %s", error->message);
}

static gboolean has_tilt(void)
{
	return proxy_has(self.bridge, "HasPickUp") || proxy_has(self.sensors, "HasAccelerometer");
}

static GVariant *on_get_property(GDBusConnection *bus, const char *sender, const char *path, const char *iface,
				 const char *property, GError **error, gpointer user_data)
{
	if (!strcmp(property, "DoubleTap"))
		return g_variant_new_boolean(self.double_tap);
	if (!strcmp(property, "Tilt"))
		return g_variant_new_boolean(self.tilt);
	if (!strcmp(property, "PickUpBrighten"))
		return g_variant_new_boolean(self.brighten);
	if (!strcmp(property, "HasDoubleTap")) {
		if (!self.touch_dir)
			self.touch_dir = find_touch_dir();
		return g_variant_new_boolean(self.touch_dir != NULL);
	}
	if (!strcmp(property, "HasTilt"))
		return g_variant_new_boolean(has_tilt());
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

	if (!strcmp(property, "DoubleTap")) {
		if (on == self.double_tap)
			return TRUE;
		self.double_tap = on;
		apply_double_tap();
	} else if (!strcmp(property, "Tilt")) {
		if (on == self.tilt)
			return TRUE;
		self.tilt = on;
		apply_tilt();
	} else if (!strcmp(property, "PickUpBrighten")) {
		if (on == self.brighten)
			return TRUE;
		self.brighten = on;
		apply_tilt();
	} else {
		g_set_error(error, G_DBUS_ERROR, G_DBUS_ERROR_PROPERTY_READ_ONLY, "Property %s is read-only",
			    property);
		return FALSE;
	}
	save_settings();
	emit_changed(property, g_variant_new_boolean(on));
	return TRUE;
}

/* SetDisplay(on): the display hook of plinius-backlight-wake; only root may call it */
static void on_method_call(GDBusConnection *bus, const char *sender, const char *path, const char *iface,
			   const char *method, GVariant *params, GDBusMethodInvocation *invocation,
			   gpointer user_data)
{
	gboolean on;

	g_variant_get(params, "(b)", &on);
	if (on != self.display_on) {
		self.display_on = on;
		apply_tilt();
	}
	g_dbus_method_invocation_return_value(invocation, NULL);
}

static const GDBusInterfaceVTable vtable = {on_method_call, on_get_property, on_set_property, {0}};

static void on_bus_acquired(GDBusConnection *bus, const char *name, gpointer user_data)
{
	GDBusNodeInfo *info = g_dbus_node_info_new_for_xml(introspection, NULL);
	g_autoptr(GError) error = NULL;

	self.bus = bus;
	g_dbus_connection_register_object(bus, OBJECT_PATH, info->interfaces[0], &vtable, NULL, NULL, NULL);
	g_dbus_node_info_unref(info);

	self.sensors = g_dbus_proxy_new_sync(bus, G_DBUS_PROXY_FLAGS_DO_NOT_AUTO_START, NULL, SENSORS_NAME,
					     SENSORS_PATH, SENSORS_NAME, NULL, &error);
	if (self.sensors) {
		g_signal_connect(self.sensors, "g-properties-changed", G_CALLBACK(on_sensors_properties_changed),
				 NULL);
		g_signal_connect(self.sensors, "notify::g-name-owner", G_CALLBACK(on_sensors_owner_changed), NULL);
	} else {
		g_warning("No sensors: %s", error->message);
	}
	g_clear_error(&error);
	/* Same object, the bridge's own interface: the pick-up gesture */
	self.bridge = g_dbus_proxy_new_sync(bus, G_DBUS_PROXY_FLAGS_DO_NOT_AUTO_START, NULL, SENSORS_NAME,
					    SENSORS_PATH, BRIDGE_INTERFACE, NULL, &error);
	if (self.bridge) {
		g_signal_connect(self.bridge, "g-signal", G_CALLBACK(on_bridge_signal), NULL);
		g_signal_connect(self.bridge, "notify::g-name-owner", G_CALLBACK(on_sensors_owner_changed), NULL);
	} else {
		g_warning("No sensor-proxy-bridge: %s", error->message);
	}

	apply_double_tap();
	self.display_on = screen_on();
	apply_tilt();
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

static int open_uinput(void)
{
	struct uinput_setup setup = {
		.id = {.bustype = BUS_VIRTUAL},
		.name = "plinius-wake",
	};
	int fd = open("/dev/uinput", O_WRONLY | O_CLOEXEC);

	if (fd < 0)
		return -1;
	if (ioctl(fd, UI_SET_EVBIT, EV_KEY) < 0 || ioctl(fd, UI_SET_KEYBIT, KEY_WAKEUP) < 0 ||
	    ioctl(fd, UI_DEV_SETUP, &setup) < 0 || ioctl(fd, UI_DEV_CREATE) < 0) {
		close(fd);
		return -1;
	}
	return fd;
}

int main(void)
{
	guint owner;

	self.uinput_fd = open_uinput();
	if (self.uinput_fd < 0) {
		g_warning("Cannot create the uinput device: %s", g_strerror(errno));
		return 1;
	}
	load_settings();

	self.loop = g_main_loop_new(NULL, FALSE);
	owner = g_bus_own_name(G_BUS_TYPE_SYSTEM, BUS_NAME, G_BUS_NAME_OWNER_FLAGS_NONE, on_bus_acquired, NULL,
			       on_name_lost, NULL, NULL);
	g_unix_signal_add(SIGTERM, on_signal, NULL);
	g_unix_signal_add(SIGINT, on_signal, NULL);
	g_main_loop_run(self.loop);

	/* Leave the touch chip as the driver had it */
	if (self.double_tap)
		stop_double_tap();
	self.tilt = FALSE;
	self.brighten = FALSE;
	apply_tilt();
	g_bus_unown_name(owner);
	ioctl(self.uinput_fd, UI_DEV_DESTROY);
	close(self.uinput_fd);
	return 0;
}
