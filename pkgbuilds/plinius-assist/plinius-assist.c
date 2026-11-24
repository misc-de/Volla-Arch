// SPDX-License-Identifier: MIT
// plinius-assist: actions for the assistant button of the Volla Phone Plinius (KEY_ASSISTANT on
// gpio-keys). A short, a double and a long press each run the action chosen in GNOME Settings >
// Power (schema org.plinius.assistant-button): torch, screenshot, camera, silent mode, lock
// screen, or a custom command.
//
// Also the Hall sensor on the same device, for a flip cover: closing it blanks the screen (and
// locks it as set for blanking), opening it turns the screen on again, both through Phosh's
// org.gnome.ScreenSaver.SetActive. The device tree gives the sensor KEY_F2; the device package
// maps it to KEY_MACRO1 (hwdb), so apps get no F2 and phoc does not wake the screen on closing.
//
// Runs in the Phosh session (XDG autostart), so apps and custom scripts get the session's
// environment. Reads the key device directly (group "input"); nothing grabs it, Phosh and apps
// still see the key.
#include <errno.h>
#include <fcntl.h>
#include <gio/gdesktopappinfo.h>
#include <gio/gio.h>
#include <glib-unix.h>
#include <linux/input.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

#define SCHEMA "org.plinius.assistant-button"
#define INPUT_DIR "/dev/input"
/* The key device can come after us: look again this often */
#define RESCAN_S 5
/* A second press within this time is a double press */
#define DOUBLE_PRESS_MS 350
/* Held this long is a long press */
#define LONG_PRESS_MS 600

#define TORCH_LED "white:torch"
/* The torch's own brightness, kept by Phosh (phosh-plinius patch 0029) */
#define TORCH_SCHEMA "org.plinius.torch"
#define LEDS_DIR "/sys/class/leds"
#define CAMERA_APP "org.gnome.Snapshot.desktop"

/* Pressed while a magnet is near the Hall sensor: KEY_MACRO1 through the hwdb, else KEY_F2 */
#define IS_COVER_KEY(code) ((code) == KEY_MACRO1 || (code) == KEY_F2)

enum action { ACTION_NONE, ACTION_TORCH, ACTION_SCREENSHOT, ACTION_CAMERA, ACTION_SILENT, ACTION_LOCK, ACTION_CUSTOM };

static struct {
	GMainLoop *loop;
	GSettings *settings;
	GDBusConnection *session;
	GDBusConnection *system;
	int fd;
	guint fd_source;
	guint rescan_source;
	/* Press detection */
	gboolean down;
	gboolean long_fired;
	guint long_timer;
	guint double_timer;
	/* Flip cover */
	gboolean cover_closed;
} self = {
	.fd = -1,
};

/* ---- Actions ---- */

static void call_done(GObject *source, GAsyncResult *result, gpointer what)
{
	g_autoptr(GError) error = NULL;
	g_autoptr(GVariant) reply = g_dbus_connection_call_finish(G_DBUS_CONNECTION(source), result, &error);

	if (!reply)
		g_warning("%s failed: %s", (const char *)what, error->message);
}

static int read_led(const char *attribute)
{
	g_autofree char *path = g_build_filename(LEDS_DIR, TORCH_LED, attribute, NULL);
	g_autofree char *text = NULL;

	if (!g_file_get_contents(path, &text, NULL, NULL))
		return -1;
	return atoi(text);
}

/*
 * The brightness the user picked for the torch with Phosh's slider (org.plinius.torch from
 * plinius-torch >= 0.5); the brightest level without the schema or a choice
 */
static int torch_brightness(int max)
{
	GSettingsSchemaSource *source = g_settings_schema_source_get_default();
	g_autoptr(GSettingsSchema) schema = source ? g_settings_schema_source_lookup(source, TORCH_SCHEMA, TRUE) : NULL;
	g_autoptr(GSettings) settings = NULL;
	int level;

	if (!schema)
		return max;
	settings = g_settings_new(TORCH_SCHEMA);
	level = g_settings_get_int(settings, "brightness");
	return level > 0 && level <= max ? level : max;
}

/* Through logind, as Phosh's torch quick setting does: no root needed */
static void toggle_torch(void)
{
	int brightness = read_led("brightness");
	int max = read_led("max_brightness");

	if (brightness < 0 || max <= 0) {
		g_warning("No torch LED %s", TORCH_LED);
		return;
	}
	g_dbus_connection_call(self.system, "org.freedesktop.login1", "/org/freedesktop/login1/session/auto",
			       "org.freedesktop.login1.Session", "SetBrightness",
			       g_variant_new("(ssu)", "leds", TORCH_LED, brightness > 0 ? 0 : (guint32)torch_brightness(max)), NULL,
			       G_DBUS_CALL_FLAGS_NONE, -1, NULL, call_done, "Switching the torch");
}

static void take_screenshot(void)
{
	g_autoptr(GDateTime) now = g_date_time_new_now_local();
	g_autofree char *name = g_date_time_format(now, "Screenshot from %Y-%m-%d %H-%M-%S.png");
	const char *pictures = g_get_user_special_dir(G_USER_DIRECTORY_PICTURES);
	g_autofree char *dir = g_build_filename(pictures ? pictures : g_get_home_dir(), "Screenshots", NULL);
	g_autofree char *path = g_build_filename(dir, name, NULL);

	g_mkdir_with_parents(dir, 0755);
	/* Phosh serves GNOME Shell's screenshot interface; with flash */
	g_dbus_connection_call(self.session, "org.gnome.Shell.Screenshot", "/org/gnome/Shell/Screenshot",
			       "org.gnome.Shell.Screenshot", "Screenshot", g_variant_new("(bbs)", FALSE, TRUE, path),
			       NULL, G_DBUS_CALL_FLAGS_NONE, -1, NULL, call_done, "Taking a screenshot");
}

static void open_camera(void)
{
	g_autoptr(GDesktopAppInfo) app = g_desktop_app_info_new(CAMERA_APP);
	g_autoptr(GError) error = NULL;

	if (!app) {
		g_warning("No camera app %s", CAMERA_APP);
		return;
	}
	if (!g_app_info_launch(G_APP_INFO(app), NULL, NULL, &error))
		g_warning("Starting the camera failed: %s", error->message);
}

/* feedbackd's profile, as Phosh's quick setting switches it: "full" or "silent" */
static void toggle_silent(void)
{
	g_autoptr(GError) error = NULL;
	g_autoptr(GVariant) reply = NULL;
	g_autoptr(GVariant) value = NULL;
	const char *profile;

	reply = g_dbus_connection_call_sync(self.session, "org.sigxcpu.Feedback", "/org/sigxcpu/Feedback",
					    "org.freedesktop.DBus.Properties", "Get",
					    g_variant_new("(ss)", "org.sigxcpu.Feedback", "Profile"), G_VARIANT_TYPE("(v)"),
					    G_DBUS_CALL_FLAGS_NONE, 2000, NULL, &error);
	if (!reply) {
		g_warning("Reading the feedback profile failed: %s", error->message);
		return;
	}
	g_variant_get(reply, "(v)", &value);
	profile = g_variant_get_string(value, NULL);
	g_dbus_connection_call(self.session, "org.sigxcpu.Feedback", "/org/sigxcpu/Feedback",
			       "org.freedesktop.DBus.Properties", "Set",
			       g_variant_new("(ssv)", "org.sigxcpu.Feedback", "Profile",
					     g_variant_new_string(g_strcmp0(profile, "full") == 0 ? "silent" : "full")),
			       NULL, G_DBUS_CALL_FLAGS_NONE, -1, NULL, call_done, "Switching silent mode");
}

static void lock_screen(void)
{
	g_dbus_connection_call(self.session, "org.gnome.ScreenSaver", "/org/gnome/ScreenSaver",
			       "org.gnome.ScreenSaver", "Lock", NULL, NULL, G_DBUS_CALL_FLAGS_NONE, -1, NULL,
			       call_done, "Locking the screen");
}

static void run_custom(const char *command)
{
	g_auto(GStrv) argv = NULL;
	g_autoptr(GError) error = NULL;

	if (!command || !*command) {
		g_message("No custom command set");
		return;
	}
	if (!g_shell_parse_argv(command, NULL, &argv, &error) ||
	    !g_spawn_async(g_get_home_dir(), argv, NULL, G_SPAWN_SEARCH_PATH, NULL, NULL, NULL, &error))
		g_warning("Running \"%s\" failed: %s", command, error->message);
}

/* @press: "short-press", "double-press" or "long-press" */
static void run_action(const char *press)
{
	g_autofree char *command_key = g_strconcat(press, "-command", NULL);

	g_message("%s", press);
	switch (g_settings_get_enum(self.settings, press)) {
	case ACTION_TORCH:
		toggle_torch();
		break;
	case ACTION_SCREENSHOT:
		take_screenshot();
		break;
	case ACTION_CAMERA:
		open_camera();
		break;
	case ACTION_SILENT:
		toggle_silent();
		break;
	case ACTION_LOCK:
		lock_screen();
		break;
	case ACTION_CUSTOM: {
		g_autofree char *command = g_settings_get_string(self.settings, command_key);
		run_custom(command);
		break;
	}
	default:
		break;
	}
}

/* ---- Press detection ---- */

static gboolean on_long_press(gpointer user_data)
{
	self.long_timer = 0;
	self.long_fired = TRUE;
	g_clear_handle_id(&self.double_timer, g_source_remove);
	run_action("long-press");
	return G_SOURCE_REMOVE;
}

static gboolean on_double_timeout(gpointer user_data)
{
	self.double_timer = 0;
	run_action("short-press");
	return G_SOURCE_REMOVE;
}

static void on_key(gboolean down)
{
	if (down == self.down)
		return; /* autorepeat */
	self.down = down;

	if (down) {
		self.long_fired = FALSE;
		self.long_timer = g_timeout_add(LONG_PRESS_MS, on_long_press, NULL);
		return;
	}

	g_clear_handle_id(&self.long_timer, g_source_remove);
	if (self.long_fired)
		return;

	if (self.double_timer) {
		/* Second press within the window */
		g_clear_handle_id(&self.double_timer, g_source_remove);
		run_action("double-press");
	} else if (g_settings_get_enum(self.settings, "double-press") == ACTION_NONE) {
		run_action("short-press");
	} else {
		self.double_timer = g_timeout_add(DOUBLE_PRESS_MS, on_double_timeout, NULL);
	}
}

/* ---- Flip cover ---- */

static void on_cover(gboolean closed)
{
	if (closed == self.cover_closed)
		return;
	self.cover_closed = closed;
	if (!g_settings_get_boolean(self.settings, "cover"))
		return;
	g_message("Cover %s", closed ? "closed" : "opened");
	g_dbus_connection_call(self.session, "org.gnome.ScreenSaver", "/org/gnome/ScreenSaver",
			       "org.gnome.ScreenSaver", "SetActive", g_variant_new("(b)", closed), NULL,
			       G_DBUS_CALL_FLAGS_NONE, -1, NULL, call_done,
			       closed ? "Blanking the screen" : "Turning the screen on");
}

/* The cover's state when the device is opened: taken as it is, nothing happens */
static void read_cover(int fd)
{
	unsigned long keys[KEY_MAX / (8 * sizeof(unsigned long)) + 1] = {0};
	const size_t bits = 8 * sizeof(unsigned long);

	if (ioctl(fd, EVIOCGKEY(sizeof(keys)), keys) < 0)
		return;
	self.cover_closed = ((keys[KEY_MACRO1 / bits] >> (KEY_MACRO1 % bits)) & 1) ||
			    ((keys[KEY_F2 / bits] >> (KEY_F2 % bits)) & 1);
}

/* ---- Key device ---- */

static void start_rescan(void);

static gboolean on_input(int fd, GIOCondition condition, gpointer user_data)
{
	struct input_event events[16];
	ssize_t len = read(fd, events, sizeof(events));

	if (len <= 0 || (condition & (G_IO_ERR | G_IO_HUP))) {
		g_warning("Key device gone");
		close(self.fd);
		self.fd = -1;
		self.fd_source = 0;
		start_rescan();
		return G_SOURCE_REMOVE;
	}
	for (size_t i = 0; i < (size_t)len / sizeof(events[0]); i++)
		if (events[i].type == EV_KEY && events[i].code == KEY_ASSISTANT)
			on_key(events[i].value != 0);
		else if (events[i].type == EV_KEY && IS_COVER_KEY(events[i].code))
			on_cover(events[i].value != 0);
	return G_SOURCE_CONTINUE;
}

static gboolean has_assistant_key(int fd)
{
	unsigned long keys[KEY_MAX / (8 * sizeof(unsigned long)) + 1] = {0};
	const size_t bits = 8 * sizeof(unsigned long);

	if (ioctl(fd, EVIOCGBIT(EV_KEY, sizeof(keys)), keys) < 0)
		return FALSE;
	return (keys[KEY_ASSISTANT / bits] >> (KEY_ASSISTANT % bits)) & 1;
}

static gboolean open_key_device(void)
{
	g_autoptr(GDir) dir = g_dir_open(INPUT_DIR, 0, NULL);
	const char *name;

	while (dir && (name = g_dir_read_name(dir))) {
		g_autofree char *path = NULL;
		int fd;

		if (!g_str_has_prefix(name, "event"))
			continue;
		path = g_build_filename(INPUT_DIR, name, NULL);
		fd = open(path, O_RDONLY | O_NONBLOCK | O_CLOEXEC);
		if (fd < 0)
			continue;
		if (has_assistant_key(fd)) {
			g_message("Assistant button on %s", path);
			read_cover(fd);
			self.fd = fd;
			self.fd_source = g_unix_fd_add(fd, G_IO_IN | G_IO_ERR | G_IO_HUP, on_input, NULL);
			return TRUE;
		}
		close(fd);
	}
	return FALSE;
}

static gboolean rescan(gpointer user_data)
{
	if (!open_key_device())
		return G_SOURCE_CONTINUE;
	self.rescan_source = 0;
	return G_SOURCE_REMOVE;
}

static void start_rescan(void)
{
	if (!self.rescan_source)
		self.rescan_source = g_timeout_add_seconds(RESCAN_S, rescan, NULL);
}

static gboolean on_signal(gpointer user_data)
{
	g_main_loop_quit(self.loop);
	return G_SOURCE_REMOVE;
}

int main(void)
{
	g_autoptr(GError) error = NULL;

	self.session = g_bus_get_sync(G_BUS_TYPE_SESSION, NULL, &error);
	if (self.session)
		self.system = g_bus_get_sync(G_BUS_TYPE_SYSTEM, NULL, &error);
	if (!self.session || !self.system) {
		g_warning("No D-Bus: %s", error->message);
		return 1;
	}
	self.settings = g_settings_new(SCHEMA);
	self.loop = g_main_loop_new(NULL, FALSE);

	if (!open_key_device()) {
		g_message("No assistant button yet, looking again every %d s", RESCAN_S);
		start_rescan();
	}
	g_unix_signal_add(SIGTERM, on_signal, NULL);
	g_unix_signal_add(SIGINT, on_signal, NULL);
	g_main_loop_run(self.loop);

	if (self.fd >= 0)
		close(self.fd);
	return 0;
}
