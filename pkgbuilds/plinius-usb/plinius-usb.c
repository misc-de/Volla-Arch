// SPDX-License-Identifier: MIT
// plinius-usb: what the Volla Phone Plinius offers a computer over USB, chosen in the USB quick
// setting of Phosh through org.plinius.Usb. Functions, any combination:
//   mtp      File Sharing: the user's home folder over MTP (umtprd on FunctionFS)
//   ncm      Network: USB network to the computer (NCM), the phone at 10.15.19.82
//   gateway  Internet for the computer: NCM plus NAT through the phone's connection
//   webcam   Webcam: a camera of the phone as a UVC camera (MJPEG). This sets up the gadget's UVC
//            function and tells its video node (WebcamDevice); plinius-usb-webcam in the user's
//            session streams the camera chosen in WebcamCamera into it, the phone's cameras are
//            in the user's PipeWire. Needs a kernel whose f_uvc works on mtu3 (plinius14 on).
// None of them is charging only: the gadget is unbound and the computer sees no device.
//
// Power, apart from the functions and with any USB-C partner that speaks USB Power Delivery
// (a computer, another phone): the phone either takes power from the cable (sink, the default:
// it charges unless plinius-charge holds charging at its limit) or powers the device (source),
// through a power role swap of the Type-C port (/sys/class/typec/portN/power_role). The partner
// has to agree (PR_SWAP); a computer often refuses, a wall charger cannot take power. A device that
// only takes power (a USB stick, headphones) gets it without a swap, the port decides that by
// itself. While the phone powers a computer, charger detection sees no input; the computer
// still counts as attached as long as it stays the USB host on the cable.
//
// Rules (docs/HARDWARE.md, USB-Connect):
// - A computer counts as attached when a USB power supply is online as SDP or CDP (charger
//   detection, works without a gadget; a wall charger is DCP), or the UDC is configured.
// - Functions can only be switched on by the user of the active session on seat0 while it is
//   unlocked (logind LockedHint), or by root. Switching off is always allowed; what runs keeps
//   running when the phone locks.
// - Unplugging resets everything; after the next plug-in only the presets apply. The port goes
//   back to sink by itself when the partner goes.
// - Preset "NetworkOnConnect": NCM on every plug-in, without unlocking (SSH during development,
//   on by default for now).
// - Powering the device counts as switching something on: only the unlocked user or root, and
//   taking power again always works. Preset "SupplyPowerOnConnect" (off by default) tries the
//   swap at every plug-in of a USB PD partner, also while locked: it is the user's own explicit
//   choice in GNOME Settings, and setting it needs the unlocked user.
// - At POWER_MIN_BATTERY (10 %) or below the phone does not start powering a device, and one it
//   powers is asked to take over again (swap back to sink), so the phone keeps enough for calls.
//
// D-Bus, org.plinius.Usb on the system bus:
//   SetFunctions(as)              any combination of the functions above, but file sharing or the
//                                 webcam, not both (the one switched on last stays; apply()),
//                                 none = charging only
//   ChoosePort(s)                 the answer to PortQuestion ("usb-c" or "pogo")
//   SetPowerRole(s)               "source" = power the device, "sink" = take power; returns once
//                                 the partner agreed, or with an error saying why not (0.4 on)
//   Connected b, Functions as, AvailableFunctions as, Error s, ActivePort s, PortQuestion s,
//   WebcamDevice s                read only
//   NetworkOnConnect b, WebcamCamera s
//                                 read and write
//   PowerRole s                   "" nothing attached (or no Type-C port), "sink", "source" (0.4)
//   PowerRoleSwappable b          a USB PD partner is attached, SetPowerRole can ask it (0.4)
//   SupplyPowerOnConnect b        read and write: power every PD partner at plug-in (0.4)
//
// Every change rebuilds the gadget: the computer sees a new device and asks again for an
// address, so a gateway switched on later also reaches it (route and DNS come with DHCP).
//
// The pogo pins of the Plinius Plus share the one USB controller with USB-C. When a pogo accessory
// and a real device on USB-C meet, whichever came second, the user is asked which port stays
// active (plinius-usb-prompt shows PortQuestion, ChoosePort answers). Without an answer nothing
// changes; a wall charger on USB-C is no reason to ask. pogo-port.h has the decisions.
#include <errno.h>
#include <fcntl.h>
#include <gio/gio.h>
#include <glib-unix.h>
#include <glib/gstdio.h>
#include <gudev/gudev.h>
#include <pwd.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <signal.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/utsname.h>
#include <unistd.h>

#include "pogo-port.h"

#define BUS_NAME "org.plinius.Usb"
#define OBJECT_PATH "/org/plinius/Usb"
#define INTERFACE "org.plinius.Usb"
#define SETTINGS_FILE "/var/lib/plinius-usb/settings"
#define RUN_DIR "/run/plinius-usb"

#define GADGET "/sys/kernel/config/usb_gadget/plinius"
#define CONFIG GADGET "/configs/c.1"
#define UDC_CLASS "/sys/class/udc"
#define POWER_SUPPLY_CLASS "/sys/class/power_supply"
#define TYPEC_CLASS "/sys/class/typec"

/* File Sharing */
#define UMTPRD "/usr/bin/umtprd"
#define FFS_DIR RUN_DIR "/ffs-mtp"
#define UMTPRD_CONF RUN_DIR "/umtprd.conf"

/* Gateway: the address and the DHCP server of usb0 come from 40-plinius-usb.network */
#define USB_IFACE "usb0"
#define USB_SUBNET "10.15.19.0/24"
#define NETWORK_DROPIN_DIR "/run/systemd/network/40-plinius-usb.network.d"
#define NETWORK_DROPIN NETWORK_DROPIN_DIR "/50-plinius-gateway.conf"
#define IP_FORWARD "/proc/sys/net/ipv4/ip_forward"

/* Webcam: the UVC function, the streaming helper and the GStreamer plugin with uvcsink */
#define UVC_FUNCTION GADGET "/functions/uvc.usb0"
#define WEBCAM_HELPER "/usr/bin/plinius-usb-webcam"
#define UVCSINK_PLUGIN "/usr/lib/gstreamer-1.0/libgstuvcgadget.so"
#define VIDEO4LINUX_CLASS "/sys/class/video4linux"
/* The front camera of camera-hal-bridge */
#define WEBCAM_CAMERA_DEFAULT "hal_camera_1"
/* f_uvc keeps the whole gadget off the bus until its video node is open: if the helper has not
 * opened it by then, the webcam goes again, so the network over USB doesn't stay away */
#define WEBCAM_OPEN_TIMEOUT_S 5
/* How long plinius-usb-webcam gets to close the node before the gadget goes, then again after
 * whoever still holds it was killed */
#define WEBCAM_CLOSE_TIMEOUT_MS 2000
#define WEBCAM_KILL_TIMEOUT_MS 1000

/* Plug-in and unplugging: charger detection takes a moment, the type may change once */
#define SETTLE_MS 1000
/* How long umtprd may take to write its descriptors (then the endpoints exist) */
#define FFS_READY_TRIES 50
#define FFS_READY_STEP_US (100 * 1000)

/* Power role: the Type-C port reports changes as uevents (typec), read them a moment later;
 * while a partner is attached the state is also read every few seconds, in case a vendor
 * driver misses one */
#define POWER_REFRESH_MS 200
#define POWER_POLL_S 3
/* The preset waits for the PD contract after plug-in: tries one second apart */
#define POWER_PRESET_TRIES 8
/* The swap is done when the write returns; the role may show up a moment later */
#define POWER_ROLE_TRIES 10
#define POWER_ROLE_STEP_US (100 * 1000)
/* After a swap charger detection follows the new direction of the power for a while (VBUS goes
 * off and comes back, BC1.2 runs again): the partner stays what it was meanwhile */
#define POWER_SWAP_HOLD_MS 3000
/* Powering a device stops at this battery level, and does not start at it or below */
#define POWER_MIN_BATTERY 10

/* Pogo pins: the driver reports nothing when an accessory joins a cable, so its state is read
 * while something is attached. It waits 10 s for a choice; the read may be up to a second late and
 * the answer needs to arrive in time, so 1.5 s remain in reserve. */
#define POGO_STATE "/sys/usb/pogo_state"
#define POGO_TICK_MS 1000
#define POGO_WINDOW_MS 7500
/* A question with less time left than this can't be answered any more */
#define POGO_MIN_ASK_MS 2000
/* Reads after a cable event, the driver's state follows with a delay */
#define POGO_KICK_TICKS 5

enum {
	FN_MTP = 1 << 0,
	FN_NCM = 1 << 1,
	FN_GATEWAY = 1 << 2,
	FN_WEBCAM = 1 << 3,
};

static const struct {
	guint flag;
	const char *name;
} function_names[] = {
	{FN_MTP, "mtp"},
	{FN_NCM, "ncm"},
	{FN_GATEWAY, "gateway"},
	{FN_WEBCAM, "webcam"},
};

static const char introspection[] =
	"<node>"
	" <interface name='" INTERFACE "'>"
	"  <method name='SetFunctions'>"
	"   <arg name='functions' type='as' direction='in'/>"
	"  </method>"
	"  <method name='ChoosePort'>"
	"   <arg name='port' type='s' direction='in'/>"
	"  </method>"
	"  <method name='SetPowerRole'>"
	"   <arg name='role' type='s' direction='in'/>"
	"  </method>"
	"  <property name='ActivePort' type='s' access='read'/>"
	"  <property name='PortQuestion' type='s' access='read'/>"
	"  <property name='Connected' type='b' access='read'/>"
	"  <property name='Functions' type='as' access='read'/>"
	"  <property name='AvailableFunctions' type='as' access='read'/>"
	"  <property name='NetworkOnConnect' type='b' access='readwrite'/>"
	"  <property name='WebcamDevice' type='s' access='read'/>"
	"  <property name='WebcamCamera' type='s' access='readwrite'/>"
	"  <property name='Error' type='s' access='read'/>"
	"  <property name='PowerRole' type='s' access='read'/>"
	"  <property name='PowerRoleSwappable' type='b' access='read'/>"
	"  <property name='SupplyPowerOnConnect' type='b' access='readwrite'/>"
	" </interface>"
	"</node>";

static struct {
	GMainLoop *loop;
	GDBusConnection *bus;
	GUdevClient *udev;
	gboolean connected;
	/* What the gadget offers right now */
	guint functions;
	gboolean network_on_connect;
	/* Why the last change failed, empty if it did not */
	char *error;
	/* Whose home File Sharing offers and whose session streams the webcam while they are on */
	uid_t user;
	/* The webcam's video node while it is on, empty otherwise; the camera it streams */
	char *webcam_device;
	char *webcam_camera;
	guint webcam_timeout;
	GSubprocess *umtprd;
	GSubprocess *dnsmasq;
	guint settle_source;
	/* Power role of the Type-C port */
	struct {
		/* The port in /sys/class/typec, NULL without one (KVM preview) */
		char *port;
		gboolean partner;
		/* "", "sink" or "source" as last told */
		char *role;
		gboolean swappable;
		/* A swap is running in its thread */
		gboolean busy;
		guint refresh_source;
		guint poll_source;
		guint preset_source;
		guint preset_tries;
		/* Until then a partner still attached is still what it was (monotonic time) */
		gint64 hold_until;
		/* The device was asked once to take over at a low battery, while this "source" lasts */
		gboolean low_battery_asked;
	} power;
	gboolean supply_power_on_connect;
	/* Pogo pins, only on the Plinius Plus */
	struct {
		gboolean present;
		/* The driver's state as last read */
		int flag;
		PogoPort active;
		guint tick_source;
		guint kick_ticks;
		/* Both attached and the driver still waiting; 0 if not */
		guint window_source;
		gint64 window_end;
		PogoPort before;
		gboolean has_data;
		/* What stays without an answer while the question is open */
		PogoPort question;
	} pogo;
} self = {
	.network_on_connect = TRUE,
};

static void emit_changed(const char *property, GVariant *value);
static gboolean apply(guint functions, uid_t user, const char *why);
static void schedule_settle(void);
static void schedule_settle_in(guint ms);

/* ---- Small helpers ---- */

static gboolean write_file(const char *path, const char *text)
{
	g_autoptr(GError) error = NULL;

	if (!g_file_set_contents_full(path, text, -1, G_FILE_SET_CONTENTS_NONE, 0644, &error)) {
		g_warning("Cannot write %s: %s", path, error->message);
		return FALSE;
	}
	return TRUE;
}

static char *read_file(const char *path)
{
	char *text = NULL;

	if (!g_file_get_contents(path, &text, NULL, NULL))
		return NULL;
	return g_strchomp(text);
}

/* Run a command and wait for it; FALSE (with a message) if it fails */
static gboolean run(const char *const *argv, gboolean quiet)
{
	g_autoptr(GError) error = NULL;
	g_autofree char *err_out = NULL;
	int status;

	if (!g_spawn_sync(NULL, (char **)argv, NULL, G_SPAWN_SEARCH_PATH | G_SPAWN_STDOUT_TO_DEV_NULL, NULL, NULL,
			  NULL, &err_out, &status, &error)) {
		g_warning("Cannot run %s: %s", argv[0], error->message);
		return FALSE;
	}
	if (!g_spawn_check_wait_status(status, NULL)) {
		if (!quiet)
			g_warning("%s failed: %s", argv[0], g_strstrip(err_out));
		return FALSE;
	}
	return TRUE;
}

static void set_error(const char *message)
{
	if (!g_strcmp0(message, self.error))
		return;
	g_free(self.error);
	self.error = g_strdup(message);
	if (*message)
		g_warning("%s", message);
	emit_changed("Error", g_variant_new_string(message));
}

static GVariant *functions_variant(guint functions)
{
	GVariantBuilder builder;

	g_variant_builder_init(&builder, G_VARIANT_TYPE("as"));
	for (guint i = 0; i < G_N_ELEMENTS(function_names); i++)
		if (functions & function_names[i].flag)
			g_variant_builder_add(&builder, "s", function_names[i].name);
	return g_variant_builder_end(&builder);
}

static char *functions_text(guint functions)
{
	g_autoptr(GVariant) value = g_variant_ref_sink(functions_variant(functions));
	g_autofree const char **names = g_variant_get_strv(value, NULL);

	return names[0] ? g_strjoinv(", ", (char **)names) : g_strdup("charging only");
}

/* f_uvc of the GKI kernel queues the data stage of a control-OUT request only when userspace
 * answers, mtu3 stalls it before: the UVC probe always failed. Fixed in our kernel from
 * 6.1.192-plinius14 on (docs/HARDWARE.md, UVC). */
static gboolean kernel_has_uvc(void)
{
	struct utsname name;
	unsigned int major, minor, sub, rel;

	if (uname(&name) < 0 || sscanf(name.release, "%u.%u.%u-plinius%u", &major, &minor, &sub, &rel) != 4)
		return FALSE;
	if (major != 6 || minor != 1)
		return major > 6 || (major == 6 && minor > 1);
	return sub > 192 || (sub == 192 && rel >= 14);
}

static guint available_functions(void)
{
	guint functions = FN_NCM | FN_GATEWAY;

	if (g_file_test(UMTPRD, G_FILE_TEST_IS_EXECUTABLE))
		functions |= FN_MTP;
	if (kernel_has_uvc() && g_file_test(WEBCAM_HELPER, G_FILE_TEST_IS_EXECUTABLE) &&
	    g_file_test(UVCSINK_PLUGIN, G_FILE_TEST_EXISTS))
		functions |= FN_WEBCAM;
	return functions;
}

/* ---- Type-C port ---- */

static char *typec_read(const char *dir, const char *attr)
{
	g_autofree char *path = g_build_filename(dir, attr, NULL);

	return read_file(path);
}

/* "source [sink]": the one in brackets is the current one; NULL without the attribute */
static char *typec_selected(const char *dir, const char *attr)
{
	g_autofree char *text = typec_read(dir, attr);
	char *start, *end;

	if (!text || !(start = strchr(text, '[')) || !(end = strchr(start, ']')))
		return NULL;
	return g_strndup(start + 1, end - start - 1);
}

/* The first port with a power role; its partner shows up next to it as portN-partner */
static char *typec_find_port(void)
{
	g_autoptr(GDir) dir = g_dir_open(TYPEC_CLASS, 0, NULL);
	const char *name;

	while (dir && (name = g_dir_read_name(dir))) {
		g_autofree char *path = g_build_filename(TYPEC_CLASS, name, NULL);
		g_autofree char *role = g_build_filename(path, "power_role", NULL);
		unsigned int number;
		char rest;

		if (sscanf(name, "port%u%c", &number, &rest) == 1 && g_file_test(role, G_FILE_TEST_EXISTS))
			return g_steal_pointer(&path);
	}
	return NULL;
}

static char *typec_partner_dir(const char *port)
{
	return g_strdup_printf("%s-partner", port);
}

static gboolean typec_partner_present(const char *port)
{
	g_autofree char *partner = port ? typec_partner_dir(port) : NULL;

	return partner && g_file_test(partner, G_FILE_TEST_IS_DIR);
}

/* The battery's level in percent, -1 without a battery (the KVM preview) */
static int battery_level(void)
{
	g_autoptr(GDir) dir = g_dir_open(POWER_SUPPLY_CLASS, 0, NULL);
	const char *name;

	while (dir && (name = g_dir_read_name(dir))) {
		g_autofree char *path = g_build_filename(POWER_SUPPLY_CLASS, name, NULL);
		g_autofree char *type = typec_read(path, "type");
		g_autofree char *capacity = NULL;

		if (g_strcmp0(type ? g_strstrip(type) : NULL, "Battery"))
			continue;
		capacity = typec_read(path, "capacity");
		if (capacity)
			return atoi(capacity);
	}
	return -1;
}

static gboolean battery_too_low(void)
{
	int level = battery_level();

	return level >= 0 && level <= POWER_MIN_BATTERY;
}

/* The phone powers the partner and the partner is the USB host: a computer that takes power
 * from the phone after a swap. Charger detection sees no input then. */
static gboolean typec_powers_host(void)
{
	g_autofree char *power = NULL;
	g_autofree char *data = NULL;

	if (!typec_partner_present(self.power.port))
		return FALSE;
	power = typec_selected(self.power.port, "power_role");
	data = typec_selected(self.power.port, "data_role");
	return !g_strcmp0(power, "source") && !g_strcmp0(data, "device");
}

/* ---- Is a computer attached? ---- */

/* usb_type lists the possible types and marks the detected one: "Unknown [SDP] CDP DCP" */
static gboolean supply_is_computer(const char *dir)
{
	g_autofree char *type_path = g_build_filename(dir, "type", NULL);
	g_autofree char *online_path = g_build_filename(dir, "online", NULL);
	g_autofree char *usb_type_path = g_build_filename(dir, "usb_type", NULL);
	g_autofree char *type = read_file(type_path);
	g_autofree char *online = NULL;
	g_autofree char *usb_type = NULL;

	if (g_strcmp0(type, "USB"))
		return FALSE;
	online = read_file(online_path);
	if (!online || !g_strcmp0(online, "0"))
		return FALSE;
	usb_type = read_file(usb_type_path);
	return usb_type && (strstr(usb_type, "[SDP]") || strstr(usb_type, "[CDP]"));
}

static gboolean udc_configured(void)
{
	g_autoptr(GDir) dir = g_dir_open(UDC_CLASS, 0, NULL);
	const char *name;

	while (dir && (name = g_dir_read_name(dir))) {
		g_autofree char *path = g_build_filename(UDC_CLASS, name, "state", NULL);
		g_autofree char *state = read_file(path);

		if (!g_strcmp0(state, "configured"))
			return TRUE;
	}
	return FALSE;
}

static gboolean read_connected(void)
{
	g_autoptr(GDir) dir = g_dir_open(POWER_SUPPLY_CLASS, 0, NULL);
	const char *name;

	while (dir && (name = g_dir_read_name(dir))) {
		g_autofree char *path = g_build_filename(POWER_SUPPLY_CLASS, name, NULL);

		if (supply_is_computer(path))
			return TRUE;
	}
	if (udc_configured() || typec_powers_host())
		return TRUE;
	/* The power is changing direction: the cable is still in, the partner the same */
	if (g_get_monotonic_time() < self.power.hold_until && typec_partner_present(self.power.port))
		return self.connected;
	return FALSE;
}

/* ---- Active session (who may switch functions on, whose home is shared) ---- */

static GVariant *get_property(const char *path, const char *iface, const char *property)
{
	g_autoptr(GVariant) reply = NULL;
	GVariant *value;

	reply = g_dbus_connection_call_sync(self.bus, "org.freedesktop.login1", path, "org.freedesktop.DBus.Properties",
					    "Get", g_variant_new("(ss)", iface, property), G_VARIANT_TYPE("(v)"),
					    G_DBUS_CALL_FLAGS_NONE, -1, NULL, NULL);
	if (!reply)
		return NULL;
	g_variant_get(reply, "(v)", &value);
	return value;
}

/* User of the session in front on seat0 and whether it is locked; FALSE if there is none */
static gboolean get_active_session(uid_t *uid, gboolean *locked)
{
	g_autoptr(GVariant) session = get_property("/org/freedesktop/login1/seat/seat0",
						   "org.freedesktop.login1.Seat", "ActiveSession");
	g_autoptr(GVariant) user = NULL;
	g_autoptr(GVariant) locked_hint = NULL;
	const char *path;
	guint32 id;

	if (!session || !g_variant_is_of_type(session, G_VARIANT_TYPE("(so)")))
		return FALSE;
	g_variant_get(session, "(&s&o)", NULL, &path);
	if (!g_strcmp0(path, "/"))
		return FALSE;
	user = get_property(path, "org.freedesktop.login1.Session", "User");
	locked_hint = get_property(path, "org.freedesktop.login1.Session", "LockedHint");
	if (!user || !g_variant_is_of_type(user, G_VARIANT_TYPE("(uo)")) || !locked_hint ||
	    !g_variant_is_of_type(locked_hint, G_VARIANT_TYPE_BOOLEAN))
		return FALSE;
	g_variant_get(user, "(uo)", &id, NULL);
	*uid = id;
	*locked = g_variant_get_boolean(locked_hint);
	return TRUE;
}

/* May the sender switch something on? Gives the user whose home File Sharing offers. */
static gboolean check_caller(const char *sender, uid_t *user, GError **error)
{
	g_autoptr(GVariant) reply = NULL;
	guint32 caller;
	uid_t session_uid;
	gboolean locked;

	reply = g_dbus_connection_call_sync(self.bus, "org.freedesktop.DBus", "/org/freedesktop/DBus",
					    "org.freedesktop.DBus", "GetConnectionUnixUser", g_variant_new("(s)", sender),
					    G_VARIANT_TYPE("(u)"), G_DBUS_CALL_FLAGS_NONE, -1, NULL, error);
	if (!reply)
		return FALSE;
	g_variant_get(reply, "(u)", &caller);

	if (!get_active_session(&session_uid, &locked)) {
		/* Root (SSH) may still switch the network on, there is just no home to share */
		if (caller == 0) {
			*user = 0;
			return TRUE;
		}
		g_set_error(error, G_DBUS_ERROR, G_DBUS_ERROR_ACCESS_DENIED, "Nobody is signed in on the phone");
		return FALSE;
	}
	if (caller != 0 && (caller != session_uid || locked)) {
		g_set_error(error, G_DBUS_ERROR, G_DBUS_ERROR_ACCESS_DENIED,
			    caller != session_uid ? "Only the user in front of the phone can do this"
						  : "Unlock the phone first");
		return FALSE;
	}
	*user = session_uid;
	return TRUE;
}

/* ---- Gateway ---- */

static const char *const nat_rules[][16] = {
	{"-t", "nat", "POSTROUTING", "-s", USB_SUBNET, "!", "-o", USB_IFACE, "-j", "MASQUERADE", NULL},
	{"FORWARD", "-i", USB_IFACE, "-j", "ACCEPT", NULL},
	{"FORWARD", "-o", USB_IFACE, "-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT", NULL},
};

/* iptables-legacy: the GKI kernel has no nf_tables (as for the hotspot, 50-plinius-nm.conf) */
static gboolean nat_rule(const char *op, const char *const *rule, gboolean quiet)
{
	const char *argv[20] = {"iptables-legacy", "-w"};
	guint n = 2, i = 0;

	/* "-t nat" goes before the operation */
	if (!g_strcmp0(rule[0], "-t")) {
		argv[n++] = rule[i++];
		argv[n++] = rule[i++];
	}
	argv[n++] = op;
	argv[n++] = rule[i++];
	/* Insert FORWARD rules at the top, before anything that drops */
	if (!g_strcmp0(op, "-I"))
		argv[n++] = "1";
	while (rule[i] && n < G_N_ELEMENTS(argv) - 1)
		argv[n++] = rule[i++];
	argv[n] = NULL;
	return run(argv, quiet);
}

static void nat_off(void)
{
	/* Also removes rules a crashed instance left behind */
	for (guint i = 0; i < G_N_ELEMENTS(nat_rules); i++)
		while (nat_rule("-D", nat_rules[i], TRUE))
			;
}

static gboolean nat_on(void)
{
	for (guint i = 0; i < G_N_ELEMENTS(nat_rules); i++)
		if (!nat_rule(i == 0 ? "-A" : "-I", nat_rules[i], FALSE))
			return FALSE;
	/* Left on afterwards, as NetworkManager does for its shared connections (hotspot) */
	return write_file(IP_FORWARD, "1\n");
}

/* DHCP of usb0 hands out the phone as router and DNS server (only while the gateway is on) */
static gboolean network_dropin(gboolean on)
{
	static const char *const reload[] = {"networkctl", "reload", NULL};
	gboolean present = g_file_test(NETWORK_DROPIN, G_FILE_TEST_EXISTS);

	if (on == present)
		return TRUE;
	if (on) {
		g_mkdir_with_parents(NETWORK_DROPIN_DIR, 0755);
		if (!write_file(NETWORK_DROPIN, "# plinius-usb: Internet for the computer\n"
						"[DHCPServer]\n"
						"EmitRouter=yes\n"
						"EmitDNS=yes\n"
						"DNS=_server_address\n"))
			return FALSE;
	} else {
		g_unlink(NETWORK_DROPIN);
	}
	return run(reload, FALSE);
}

static void stop_child(GSubprocess **child)
{
	if (!*child)
		return;
	g_subprocess_send_signal(*child, SIGTERM);
	g_subprocess_wait(*child, NULL, NULL);
	g_clear_object(child);
}

static void on_child_exited(GObject *source, GAsyncResult *result, gpointer user_data);

static GSubprocess *start_child(const char *const *argv)
{
	g_autoptr(GError) error = NULL;
	/* Output goes to the journal with the service's */
	GSubprocess *child = g_subprocess_newv(argv, G_SUBPROCESS_FLAGS_NONE, &error);

	if (!child) {
		g_warning("Cannot start %s: %s", argv[0], error->message);
		return NULL;
	}
	g_subprocess_wait_async(child, NULL, on_child_exited, NULL);
	return child;
}

/* DNS for the computer: forwards to the servers of NetworkManager's /etc/resolv.conf */
static gboolean dns_on(void)
{
	static const char *const argv[] = {
		"/usr/bin/dnsmasq", "--keep-in-foreground", "--conf-file=/dev/null", "--pid-file",
		"--no-hosts", "--port=53", "--interface=" USB_IFACE,
		"--bind-dynamic", "--no-dhcp-interface=" USB_IFACE, "--log-facility=-", NULL,
	};

	self.dnsmasq = start_child(argv);
	return self.dnsmasq != NULL;
}

/* ---- File Sharing ---- */

static gboolean mtp_config(uid_t user)
{
	g_autofree char *serial = read_file(GADGET "/strings/0x409/serialnumber");
	g_autofree char *text = NULL;
	struct passwd *pw = getpwuid(user);

	if (!pw || user == 0) {
		set_error("File sharing needs a user signed in on the phone");
		return FALSE;
	}
	/* The home folder without hidden files and folders (settings, keys) */
	text = g_strdup_printf("loop_on_disconnect 1\n"
			       "show_hidden_files 0\n"
			       "storage \"%s\" \"Phone\" \"rw,uid=%u,gid=%u\"\n"
			       "manufacturer \"Volla\"\n"
			       "product \"Plinius\"\n"
			       "serial \"%s\"\n"
			       "firmware_version \"1\"\n"
			       "interface \"MTP\"\n"
			       "usb_functionfs_mode 0x1\n"
			       "usb_dev_path \"" FFS_DIR "/ep0\"\n"
			       "usb_epin_path \"" FFS_DIR "/ep1\"\n"
			       "usb_epout_path \"" FFS_DIR "/ep2\"\n"
			       "usb_epint_path \"" FFS_DIR "/ep3\"\n"
			       "usb_max_packet_size 0x200\n",
			       pw->pw_dir, (guint)pw->pw_uid, (guint)pw->pw_gid, serial ?: "0");
	return write_file(UMTPRD_CONF, text);
}

/* FunctionFS: umtprd writes the descriptors; the gadget can only be bound after that */
static gboolean mtp_on(uid_t user)
{
	static const char *const argv[] = {UMTPRD, "-conf", UMTPRD_CONF, NULL};

	if (!mtp_config(user))
		return FALSE;
	if (!g_file_test(GADGET "/functions/ffs.mtp", G_FILE_TEST_IS_DIR) &&
	    g_mkdir(GADGET "/functions/ffs.mtp", 0755) < 0) {
		g_warning("Cannot create the MTP function: %s", g_strerror(errno));
		return FALSE;
	}
	g_mkdir_with_parents(FFS_DIR, 0700);
	if (mount("mtp", FFS_DIR, "functionfs", MS_NOSUID | MS_NODEV | MS_NOEXEC, NULL) < 0 && errno != EBUSY) {
		g_warning("Cannot mount FunctionFS: %s", g_strerror(errno));
		return FALSE;
	}
	self.umtprd = start_child(argv);
	if (!self.umtprd)
		return FALSE;
	for (int i = 0; i < FFS_READY_TRIES; i++) {
		if (g_file_test(FFS_DIR "/ep1", G_FILE_TEST_EXISTS))
			return TRUE;
		g_usleep(FFS_READY_STEP_US);
	}
	g_warning("umtprd did not set up FunctionFS");
	return FALSE;
}

static void ffs_unmount(void)
{
	if (umount2(FFS_DIR, MNT_DETACH) < 0 && errno != EINVAL && errno != ENOENT)
		g_warning("Cannot unmount FunctionFS: %s", g_strerror(errno));
}

/* ---- Webcam ---- */

/* MJPEG in two sizes at 30 fps. The isochronous endpoint gets one packet of 1024 bytes per
 * microframe: mtu3 does not set up more (measured, 3072 cut every frame short), and 8 MB/s are
 * enough for MJPEG at 720p. */
static const struct {
	const char *name;
	guint width, height;
} webcam_frames[] = {
	{"720p", 1280, 720},
	{"480p", 640, 480},
};

static void uvc_write(const char *dir, const char *attr, const char *format, ...) G_GNUC_PRINTF(3, 4);

static void uvc_write(const char *dir, const char *attr, const char *format, ...)
{
	g_autofree char *path = g_build_filename(dir, attr, NULL);
	g_autofree char *text = NULL;
	va_list args;

	va_start(args, format);
	text = g_strdup_vprintf(format, args);
	va_end(args);
	write_file(path, text);
}

static gboolean uvc_link(const char *target, const char *link)
{
	if (symlink(target, link) < 0 && errno != EEXIST) {
		g_warning("Cannot link %s: %s", link, g_strerror(errno));
		return FALSE;
	}
	return TRUE;
}

static gboolean uvc_create(void)
{
	static const char *const streaming_classes[] = {"fs", "hs", "ss"};
	static const char *const control_classes[] = {"fs", "ss"};
	const char *mjpeg = UVC_FUNCTION "/streaming/mjpeg/m";
	const char *streaming_header = UVC_FUNCTION "/streaming/header/h";
	const char *control_header = UVC_FUNCTION "/control/header/h";

	if (g_mkdir(UVC_FUNCTION, 0755) < 0 && errno != EEXIST) {
		g_warning("Cannot create the UVC function: %s", g_strerror(errno));
		return FALSE;
	}
	for (guint i = 0; i < G_N_ELEMENTS(webcam_frames); i++) {
		g_autofree char *frame = g_build_filename(mjpeg, webcam_frames[i].name, NULL);
		guint w = webcam_frames[i].width, h = webcam_frames[i].height;

		if (g_mkdir_with_parents(frame, 0755) < 0) {
			g_warning("Cannot create the UVC frame %s: %s", frame, g_strerror(errno));
			return FALSE;
		}
		uvc_write(frame, "wWidth", "%u\n", w);
		uvc_write(frame, "wHeight", "%u\n", h);
		/* 100 ns units: 30 fps */
		uvc_write(frame, "dwDefaultFrameInterval", "333333\n");
		uvc_write(frame, "dwFrameInterval", "333333\n");
		uvc_write(frame, "dwMaxVideoFrameBufferSize", "%u\n", w * h * 2);
		uvc_write(frame, "dwMinBitRate", "%u\n", w * h * 16 * 30);
		uvc_write(frame, "dwMaxBitRate", "%u\n", w * h * 16 * 30);
	}
	if (g_mkdir_with_parents(streaming_header, 0755) < 0 || g_mkdir_with_parents(control_header, 0755) < 0) {
		g_warning("Cannot create the UVC headers: %s", g_strerror(errno));
		return FALSE;
	}
	if (!uvc_link(mjpeg, UVC_FUNCTION "/streaming/header/h/m"))
		return FALSE;
	for (guint i = 0; i < G_N_ELEMENTS(streaming_classes); i++) {
		g_autofree char *link =
			g_strdup_printf(UVC_FUNCTION "/streaming/class/%s/h", streaming_classes[i]);

		if (!uvc_link(streaming_header, link))
			return FALSE;
	}
	for (guint i = 0; i < G_N_ELEMENTS(control_classes); i++) {
		g_autofree char *link = g_strdup_printf(UVC_FUNCTION "/control/class/%s/h", control_classes[i]);

		if (!uvc_link(control_header, link))
			return FALSE;
	}
	uvc_write(UVC_FUNCTION, "streaming_maxpacket", "1024\n");
	return TRUE;
}

/* The function goes only once no configuration links it; the gadget itself always stays */
static void uvc_remove(void)
{
	static const char *const links[] = {
		UVC_FUNCTION "/streaming/class/fs/h", UVC_FUNCTION "/streaming/class/hs/h",
		UVC_FUNCTION "/streaming/class/ss/h", UVC_FUNCTION "/control/class/fs/h",
		UVC_FUNCTION "/control/class/ss/h",   UVC_FUNCTION "/streaming/header/h/m",
	};

	if (!g_file_test(UVC_FUNCTION, G_FILE_TEST_IS_DIR))
		return;
	for (guint i = 0; i < G_N_ELEMENTS(links); i++)
		g_unlink(links[i]);
	for (guint i = 0; i < G_N_ELEMENTS(webcam_frames); i++) {
		g_autofree char *frame = g_build_filename(UVC_FUNCTION "/streaming/mjpeg/m", webcam_frames[i].name, NULL);

		g_rmdir(frame);
	}
	g_rmdir(UVC_FUNCTION "/streaming/mjpeg/m");
	g_rmdir(UVC_FUNCTION "/streaming/header/h");
	g_rmdir(UVC_FUNCTION "/control/header/h");
	if (g_rmdir(UVC_FUNCTION) < 0)
		g_warning("Cannot remove the UVC function: %s", g_strerror(errno));
}

/* The gadget's video node: its device is a child of the gadget's, not of a camera */
static char *webcam_find_device(void)
{
	g_autoptr(GDir) dir = g_dir_open(VIDEO4LINUX_CLASS, 0, NULL);
	const char *name;

	while (dir && (name = g_dir_read_name(dir))) {
		g_autofree char *link = g_build_filename(VIDEO4LINUX_CLASS, name, NULL);
		g_autofree char *target = g_file_read_link(link, NULL);

		if (target && strstr(target, "/gadget"))
			return g_strdup_printf("/dev/%s", name);
	}
	return NULL;
}

static void webcam_set_device(const char *device)
{
	if (!g_strcmp0(device ?: "", self.webcam_device ?: ""))
		return;
	g_free(self.webcam_device);
	self.webcam_device = g_strdup(device ?: "");
	emit_changed("WebcamDevice", g_variant_new_string(self.webcam_device));
}

/* The helper opened the node by now when the computer sees a device */
static gboolean on_webcam_timeout(gpointer user_data)
{
	g_autofree char *state = NULL;
	g_autofree char *udc = read_file(GADGET "/UDC");

	self.webcam_timeout = 0;
	if (!(self.functions & FN_WEBCAM) || !udc || !*udc)
		return G_SOURCE_REMOVE;
	state = g_strdup_printf(UDC_CLASS "/%s/state", udc);
	g_free(udc);
	udc = read_file(state);
	if (udc && strcmp(udc, "not attached"))
		return G_SOURCE_REMOVE;
	apply(self.functions & ~FN_WEBCAM, 0, "The webcam could not start");
	return G_SOURCE_REMOVE;
}

/* The processes with the webcam node open (by its device number, so a node already removed
 * from /dev counts too); with @kill_them they are killed. */
static guint webcam_holders(dev_t rdev, gboolean kill_them)
{
	g_autoptr(GDir) proc = g_dir_open("/proc", 0, NULL);
	const char *pid;
	guint count = 0;

	while (proc && (pid = g_dir_read_name(proc))) {
		g_autofree char *fds = NULL;
		g_autoptr(GDir) dir = NULL;
		const char *fd;
		gboolean holds = FALSE;

		if (!g_ascii_isdigit(*pid) || atoi(pid) == getpid())
			continue;
		fds = g_build_filename("/proc", pid, "fd", NULL);
		dir = g_dir_open(fds, 0, NULL);
		while (!holds && dir && (fd = g_dir_read_name(dir))) {
			g_autofree char *path = g_build_filename(fds, fd, NULL);
			struct stat st;

			holds = stat(path, &st) == 0 && S_ISCHR(st.st_mode) && st.st_rdev == rdev;
		}
		if (!holds)
			continue;
		count++;
		if (kill_them) {
			g_autofree char *comm_path = g_build_filename("/proc", pid, "comm", NULL);
			g_autofree char *comm = read_file(comm_path);

			g_warning("%s (%s) still holds the webcam node: killed, or the gadget could not go",
				  comm ?: "?", pid);
			kill(atoi(pid), SIGKILL);
		}
	}
	return count;
}

static guint webcam_wait_closed(dev_t rdev, guint timeout_ms)
{
	gint64 end = g_get_monotonic_time() + (gint64)timeout_ms * 1000;
	guint count;

	while ((count = webcam_holders(rdev, FALSE)) && g_get_monotonic_time() < end)
		g_usleep(50 * 1000);
	return count;
}

/*
 * Before the gadget goes: the webcam node closed by everyone. The kernel's UVC function waits
 * for the last close of its video device without a timeout when it is unbound
 * (uvc_function_unbind), holding the gadget's locks: plinius-usb, udev and journald (uevents)
 * stood still until then. On 13.11.2026 plinius-usb-webcam kept the node open after the cable
 * was pulled, 18 minutes until it was killed; any app of the session may open the node too.
 * plinius-usb-webcam gets WebcamDevice "" first and closes its pipeline; whoever holds the node
 * after WEBCAM_CLOSE_TIMEOUT_MS is killed.
 */
static void webcam_release(void)
{
	struct stat st;
	gboolean known;

	if (!self.webcam_device || !*self.webcam_device)
		return;
	known = stat(self.webcam_device, &st) == 0 && S_ISCHR(st.st_mode);
	webcam_set_device("");
	if (self.bus)
		g_dbus_connection_flush_sync(self.bus, NULL, NULL);
	if (!known)
		return;
	if (!webcam_wait_closed(st.st_rdev, WEBCAM_CLOSE_TIMEOUT_MS))
		return;
	webcam_holders(st.st_rdev, TRUE);
	if (webcam_wait_closed(st.st_rdev, WEBCAM_KILL_TIMEOUT_MS))
		g_warning("The webcam node is still open: removing the gadget may wait for it");
}

/* ---- Gadget ---- */

static void gadget_unbind(void)
{
	g_autofree char *udc = read_file(GADGET "/UDC");

	if (udc && *udc)
		write_file(GADGET "/UDC", "\n");
}

/* Strings and IDs stay, functions come and go */
static gboolean gadget_create(void)
{
	g_autofree char *machine_id = read_file("/etc/machine-id");
	g_autofree char *seed = NULL;
	g_autofree char *hash = NULL;

	if (g_file_test(CONFIG "/strings/0x409", G_FILE_TEST_IS_DIR))
		return TRUE;
	if (g_mkdir_with_parents(GADGET "/strings/0x409", 0755) < 0 ||
	    g_mkdir_with_parents(CONFIG "/strings/0x409", 0755) < 0) {
		g_warning("Cannot create the USB gadget: %s", g_strerror(errno));
		return FALSE;
	}
	write_file(GADGET "/idVendor", "0x0e8d\n");
	/* Derived from the machine ID, not the ID itself: every USB host sees the serial number,
	 * and the machine ID is meant to stay private (machine-id(5)) */
	seed = g_strdup_printf("plinius-usb-serial:%s", machine_id ?: "");
	hash = g_compute_checksum_for_string(G_CHECKSUM_SHA256, seed, -1);
	hash[12] = '\0';
	write_file(GADGET "/strings/0x409/serialnumber", hash);
	write_file(GADGET "/strings/0x409/manufacturer", "Volla");
	write_file(GADGET "/strings/0x409/product", "Plinius (Arch)");
	write_file(CONFIG "/strings/0x409/configuration", "plinius");
	return TRUE;
}

static void gadget_link(const char *function)
{
	g_autofree char *target = g_build_filename(GADGET, "functions", function, NULL);
	g_autofree char *link = g_build_filename(CONFIG, function, NULL);

	if (symlink(target, link) < 0 && errno != EEXIST)
		g_warning("Cannot add %s to the gadget: %s", function, g_strerror(errno));
}

static void gadget_unlink_all(void)
{
	g_autofree char *ncm = g_build_filename(CONFIG, "ncm.usb0", NULL);
	g_autofree char *mtp = g_build_filename(CONFIG, "ffs.mtp", NULL);
	g_autofree char *uvc = g_build_filename(CONFIG, "uvc.usb0", NULL);

	g_unlink(ncm);
	g_unlink(mtp);
	g_unlink(uvc);
}

/* The first real controller; mtu3 is a module and may show up after the service */
static char *find_udc(void)
{
	g_autoptr(GDir) dir = g_dir_open(UDC_CLASS, 0, NULL);
	const char *name;

	while (dir && (name = g_dir_read_name(dir)))
		if (!strstr(name, "dummy"))
			return g_strdup(name);
	return NULL;
}

static gboolean gadget_bind(guint functions)
{
	g_autofree char *udc = find_udc();
	/* A product ID per set of functions: hosts (Windows) remember the interfaces per ID.
	 * 0x2004 is the NCM gadget the phone always had (rescue system, UT); with the webcam
	 * 0x2010 plus 1 for MTP and 2 for NCM. */
	g_autofree char *product =
		(functions & FN_WEBCAM)
			? g_strdup_printf("0x%04x\n", 0x2010 | ((functions & FN_MTP) ? 1 : 0) | ((functions & FN_NCM) ? 2 : 0))
			: g_strdup((functions & FN_MTP) ? ((functions & FN_NCM) ? "0x200a\n" : "0x2008\n") : "0x2004\n");
	/* More than one function, or UVC (its two interfaces always come with an association):
	 * interface association descriptors (Misc/Common/IAD) */
	gboolean composite = ((functions & FN_MTP) && (functions & FN_NCM)) || (functions & FN_WEBCAM);

	if (!udc) {
		set_error("No USB controller found");
		return FALSE;
	}
	write_file(GADGET "/idProduct", product);
	write_file(GADGET "/bDeviceClass", composite ? "0xef\n" : "0x00\n");
	write_file(GADGET "/bDeviceSubClass", composite ? "0x02\n" : "0x00\n");
	write_file(GADGET "/bDeviceProtocol", composite ? "0x01\n" : "0x00\n");
	if (!write_file(GADGET "/UDC", udc)) {
		/* EBUSY: another gadget (e.g. Android's in the container) holds the controller */
		set_error("The USB controller is busy");
		return FALSE;
	}
	return TRUE;
}

/* Back to charging only: nothing bound, no function, no gateway. A function can only go once
 * no configuration links it, FunctionFS only once umtprd has closed it. */
static void teardown(void)
{
	g_clear_handle_id(&self.webcam_timeout, g_source_remove);
	/* Before the unbind, which waits for the node to be closed (webcam_release) */
	webcam_release();
	gadget_unbind();
	stop_child(&self.umtprd);
	gadget_unlink_all();
	ffs_unmount();
	g_rmdir(GADGET "/functions/ffs.mtp");
	g_rmdir(GADGET "/functions/ncm.usb0");
	uvc_remove();
	stop_child(&self.dnsmasq);
	nat_off();
	network_dropin(FALSE);
}

static gboolean setup(guint functions, uid_t user)
{
	if (!gadget_create()) {
		set_error("The USB functions could not start");
		return FALSE;
	}
	if (functions & FN_GATEWAY) {
		if (!network_dropin(TRUE) || !nat_on() || !dns_on()) {
			set_error("Internet for the computer could not start");
			return FALSE;
		}
	}
	if (functions & FN_NCM) {
		if (!g_file_test(GADGET "/functions/ncm.usb0", G_FILE_TEST_IS_DIR) &&
		    g_mkdir(GADGET "/functions/ncm.usb0", 0755) < 0) {
			set_error("The USB network could not start");
			return FALSE;
		}
		gadget_link("ncm.usb0");
	}
	if (functions & FN_MTP) {
		if (!mtp_on(user)) {
			if (!self.error || !*self.error)
				set_error("File sharing could not start");
			return FALSE;
		}
		gadget_link("ffs.mtp");
	}
	if (functions & FN_WEBCAM) {
		/* The camera is in the PipeWire of the user signed in */
		if (user == 0) {
			set_error("The webcam needs a user signed in on the phone");
			return FALSE;
		}
		if (!uvc_create()) {
			set_error("The webcam could not start");
			return FALSE;
		}
		gadget_link("uvc.usb0");
	}
	if (!gadget_bind(functions))
		return FALSE;
	if (functions & FN_WEBCAM) {
		/* Binding creates the node; uaccess gives the user of the active session access */
		g_autofree char *device = webcam_find_device();

		if (!device) {
			set_error("The webcam could not start");
			return FALSE;
		}
		webcam_set_device(device);
		self.webcam_timeout = g_timeout_add_seconds(WEBCAM_OPEN_TIMEOUT_S, on_webcam_timeout, NULL);
	}
	return TRUE;
}

/* Rebuild the gadget for a set of functions; on failure back to charging only.
 * user: whose home File Sharing offers, 0 for the one it already offers.
 * why: why a function went (Error says so once the others are back), NULL for a change asked
 * for, which starts without an error. */
static gboolean apply(guint functions, uid_t user, const char *why)
{
	gboolean ok = TRUE;

	if (user == 0)
		user = self.user;

	/* The gateway runs over the USB network */
	if (functions & FN_GATEWAY)
		functions |= FN_NCM;
	functions &= available_functions();
	/*
	 * File sharing or the webcam, not both: the controller's TX FIFO (mtu3, the vendor's module)
	 * has no room for the webcam's stream endpoint (1024 bytes, double-buffered) next to the
	 * bulk endpoints of MTP and NCM. The computer then saw the camera, but its stream endpoint
	 * was never set up ("alloc ep fifo failed(1024)", 13.11.2026) and no picture came. The one
	 * just switched on wins; the switches follow Functions.
	 */
	if ((functions & FN_MTP) && (functions & FN_WEBCAM)) {
		gboolean webcam_added = (self.functions & FN_MTP) && !(self.functions & FN_WEBCAM);

		functions &= webcam_added ? ~FN_MTP : ~FN_WEBCAM;
		g_message("File sharing and the webcam do not fit together: %s stays",
			  webcam_added ? "the webcam" : "file sharing");
	}

	teardown();
	if (functions) {
		/* setup() names its own failure; with a reason to keep, Error does not go through ""
		 * on the way */
		if (why)
			g_clear_pointer(&self.error, g_free);
		else
			set_error("");
		ok = setup(functions, user);
		if (!ok) {
			teardown();
			functions = 0;
		}
	}
	if (why && ok)
		set_error(why);
	self.user = (functions & (FN_MTP | FN_WEBCAM)) ? user : 0;
	if (functions != self.functions) {
		g_autofree char *text = functions_text(functions);

		self.functions = functions;
		g_message("USB functions: %s", text);
		emit_changed("Functions", functions_variant(functions));
	}
	return ok;
}

static void on_child_exited(GObject *source, GAsyncResult *result, gpointer user_data)
{
	GSubprocess *child = G_SUBPROCESS(source);

	g_subprocess_wait_finish(child, result, NULL);
	/* Stopped on purpose: stop_child already dropped it */
	if (child == self.umtprd) {
		g_clear_object(&self.umtprd);
		apply(self.functions & ~FN_MTP, 0, "File sharing stopped unexpectedly");
	} else if (child == self.dnsmasq) {
		g_clear_object(&self.dnsmasq);
		apply(self.functions & ~FN_GATEWAY, 0, "Internet for the computer stopped unexpectedly");
	}
}

/* ---- Pogo pins ---- */

static void pogo_set_active(PogoPort port)
{
	if (port == self.pogo.active)
		return;
	self.pogo.active = port;
	g_message("Active USB port: %s", pogo_port_name(port));
	emit_changed("ActivePort", g_variant_new_string(pogo_port_name(port)));
}

static void pogo_set_question(PogoPort stays)
{
	if (stays == self.pogo.question)
		return;
	self.pogo.question = stays;
	emit_changed("PortQuestion", g_variant_new_string(stays == POGO_PORT_NONE ? "" : pogo_port_name(stays)));
}

/* Tell the driver, unless it does the right thing on its own */
static void pogo_take_port(PogoPort port)
{
	const char *value = pogo_port_value(self.pogo.flag, port);

	if (value)
		write_file(POGO_STATE, value);
	pogo_set_active(port);
}

static void pogo_end_window(void)
{
	g_clear_handle_id(&self.pogo.window_source, g_source_remove);
	pogo_set_question(POGO_PORT_NONE);
}

/* Time is almost up without an answer */
static gboolean on_pogo_window_over(gpointer user_data)
{
	self.pogo.window_source = 0;
	pogo_take_port(pogo_port_without_answer(self.pogo.flag, self.pogo.before, self.pogo.has_data));
	pogo_set_question(POGO_PORT_NONE);
	return G_SOURCE_REMOVE;
}

/* Ask once it's clear that USB-C has a real device: charger detection takes a moment. Not on a
 * locked phone: its port stays, and an accessory can't change that without the owner. */
static void pogo_consider_question(void)
{
	uid_t uid;
	gboolean locked;

	if (!self.pogo.window_source || self.pogo.question != POGO_PORT_NONE)
		return;
	if (!self.pogo.has_data)
		self.pogo.has_data = pogo_usbc_has_data(self.pogo.flag, self.connected || read_connected());
	if (!self.pogo.has_data || g_get_monotonic_time() + POGO_MIN_ASK_MS * 1000 > self.pogo.window_end)
		return;
	if (!get_active_session(&uid, &locked) || locked)
		return;
	pogo_set_question(pogo_port_before(self.pogo.flag, self.pogo.before));
}

static void pogo_flag_changed(int flag)
{
	gboolean was_conflict = pogo_flag_is_conflict(self.pogo.flag);

	g_message("Pogo state %d -> %d", self.pogo.flag, flag);
	self.pogo.flag = flag;
	if (was_conflict)
		pogo_end_window();
	if (!pogo_flag_is_conflict(flag)) {
		pogo_set_active(pogo_flag_port(flag));
		return;
	}
	self.pogo.before = self.pogo.active;
	self.pogo.has_data = FALSE;
	self.pogo.window_end = g_get_monotonic_time() + POGO_WINDOW_MS * 1000;
	self.pogo.window_source = g_timeout_add(POGO_WINDOW_MS, on_pogo_window_over, NULL);
	pogo_consider_question();
}

static int pogo_read_flag(void)
{
	g_autofree char *text = read_file(POGO_STATE);

	return text ? atoi(text) : self.pogo.flag;
}

static gboolean on_pogo_tick(gpointer user_data)
{
	int flag = pogo_read_flag();

	if (flag != self.pogo.flag)
		pogo_flag_changed(flag);
	else
		pogo_consider_question();

	if (self.pogo.kick_ticks > 0)
		self.pogo.kick_ticks--;
	/* Nothing attached: the next plug-in is an event */
	if (flag == POGO_FLAG_NONE && !self.pogo.kick_ticks) {
		self.pogo.tick_source = 0;
		return G_SOURCE_REMOVE;
	}
	return G_SOURCE_CONTINUE;
}

static void pogo_kick(void)
{
	if (!self.pogo.present)
		return;
	self.pogo.kick_ticks = POGO_KICK_TICKS;
	if (!self.pogo.tick_source)
		self.pogo.tick_source = g_timeout_add(POGO_TICK_MS, on_pogo_tick, NULL);
}

/* Both may be attached already: then the driver has long decided and there's nothing to ask */
static void pogo_init(void)
{
	self.pogo.present = g_file_test(POGO_STATE, G_FILE_TEST_EXISTS);
	if (!self.pogo.present)
		return;
	self.pogo.flag = pogo_read_flag();
	self.pogo.active = pogo_flag_is_conflict(self.pogo.flag) ? pogo_port_before(self.pogo.flag, POGO_PORT_NONE)
								 : pogo_flag_port(self.pogo.flag);
	g_message("Pogo state %d, active USB port: %s", self.pogo.flag, pogo_port_name(self.pogo.active));
	if (self.pogo.flag != POGO_FLAG_NONE)
		pogo_kick();
}

static void pogo_choose_port(const char *sender, const char *name, GDBusMethodInvocation *invocation)
{
	g_autoptr(GError) error = NULL;
	PogoPort port;
	uid_t user;

	if (!g_strcmp0(name, pogo_port_name(POGO_PORT_USBC))) {
		port = POGO_PORT_USBC;
	} else if (!g_strcmp0(name, pogo_port_name(POGO_PORT_POGO))) {
		port = POGO_PORT_POGO;
	} else {
		g_dbus_method_invocation_return_error(invocation, G_DBUS_ERROR, G_DBUS_ERROR_INVALID_ARGS,
						      "Unknown port %s", name);
		return;
	}
	if (self.pogo.question == POGO_PORT_NONE) {
		g_dbus_method_invocation_return_error_literal(invocation, G_DBUS_ERROR, G_DBUS_ERROR_FAILED,
							      "There is no question open");
		return;
	}
	if (!check_caller(sender, &user, &error)) {
		g_dbus_error_strip_remote_error(error);
		g_dbus_method_invocation_return_error_literal(invocation, G_DBUS_ERROR, G_DBUS_ERROR_ACCESS_DENIED,
							      error->message);
		return;
	}
	pogo_take_port(port);
	pogo_end_window();
	g_dbus_method_invocation_return_value(invocation, NULL);
}

/* ---- Power role ---- */

/* A swap asked for, done in a thread of its own: the write to power_role blocks until the
 * partner answered the PR_SWAP or the driver gave up */
typedef struct {
	char *port;
	char *role;
	/* NULL for the preset */
	GDBusMethodInvocation *invocation;
} PowerSwap;

static void power_swap_free(gpointer data)
{
	PowerSwap *swap = data;

	if (swap->invocation)
		g_dbus_method_invocation_return_error_literal(swap->invocation, G_DBUS_ERROR, G_DBUS_ERROR_FAILED,
							      "Switching the power was interrupted");
	g_free(swap->port);
	g_free(swap->role);
	g_free(swap);
}

static void power_swap_thread(GTask *task, gpointer source, gpointer data, GCancellable *cancellable)
{
	PowerSwap *swap = data;
	g_autofree char *path = g_build_filename(swap->port, "power_role", NULL);
	g_autofree char *text = g_strdup_printf("%s\n", swap->role);
	int fd = open(path, O_WRONLY | O_CLOEXEC);
	int err = 0;

	if (fd < 0) {
		err = errno;
	} else {
		if (write(fd, text, strlen(text)) < 0)
			err = errno;
		close(fd);
	}
	/* The driver reports the new role while the swap runs, give it a moment */
	for (int i = 0; !err && i < POWER_ROLE_TRIES; i++) {
		g_autofree char *role = typec_selected(swap->port, "power_role");

		if (!g_strcmp0(role, swap->role))
			break;
		g_usleep(POWER_ROLE_STEP_US);
	}
	g_task_return_int(task, err);
}

/* Why a swap did not happen. The typec class answers EIO without a PD contract and EOPNOTSUPP
 * for a port fixed to one role; the MediaTek driver EPERM when the partner refused or the swap
 * failed. */
static char *power_swap_error(int err, gboolean source)
{
	switch (err) {
	case 0:
	case EPERM:
		return g_strdup(source ? "The connected device does not take power from the phone"
				       : "The connected device does not supply power");
	case EIO:
		return g_strdup("The connected device does not support this");
	case EOPNOTSUPP:
		return g_strdup("This USB port cannot change the direction of power");
	default:
		return g_strdup_printf("Switching the power failed: %s", g_strerror(err));
	}
}

static void power_refresh(void);

static void power_hold(void)
{
	self.power.hold_until = g_get_monotonic_time() + POWER_SWAP_HOLD_MS * 1000;
	schedule_settle_in(POWER_SWAP_HOLD_MS);
}

static void on_power_swapped(GObject *source, GAsyncResult *result, gpointer user_data)
{
	PowerSwap *swap = g_task_get_task_data(G_TASK(result));
	int err = g_task_propagate_int(G_TASK(result), NULL);
	g_autofree char *message = NULL;
	GDBusMethodInvocation *invocation = g_steal_pointer(&swap->invocation);

	self.power.busy = FALSE;
	power_refresh();
	power_hold();
	if (!self.power.partner)
		message = g_strdup("Nothing is connected");
	else if (err || g_strcmp0(self.power.role, swap->role))
		message = power_swap_error(err, !strcmp(swap->role, "source"));

	if (message)
		g_message("Power role %s %s: %s", swap->role, invocation ? "asked for" : "of the preset", message);
	if (!invocation)
		return;
	if (message)
		g_dbus_method_invocation_return_error_literal(invocation, G_DBUS_ERROR, G_DBUS_ERROR_FAILED, message);
	else
		g_dbus_method_invocation_return_value(invocation, NULL);
}

static void power_swap(const char *role, GDBusMethodInvocation *invocation)
{
	g_autoptr(GTask) task = g_task_new(NULL, NULL, on_power_swapped, NULL);
	PowerSwap *swap = g_new0(PowerSwap, 1);

	swap->port = g_strdup(self.power.port);
	swap->role = g_strdup(role);
	swap->invocation = invocation;
	g_task_set_task_data(task, swap, power_swap_free);
	self.power.busy = TRUE;
	/* VBUS may go away for a moment: no "detached" while the swap runs */
	self.power.hold_until = G_MAXINT64;
	g_message("Power role: asking the partner for %s", role);
	g_task_run_in_thread(task, power_swap_thread);
}

/* Preset: power the device once its PD contract is there (it takes a moment after plug-in).
 * A partner without PD, or one that refuses (a wall charger), keeps powering the phone. */
static gboolean on_power_preset(gpointer user_data)
{
	power_refresh();
	if (!self.power.preset_source)
		return G_SOURCE_REMOVE;
	if (!self.power.partner || !self.supply_power_on_connect || !g_strcmp0(self.power.role, "source")) {
		self.power.preset_source = 0;
		return G_SOURCE_REMOVE;
	}
	if (battery_too_low()) {
		g_message("The battery is at %d %%, the phone keeps taking power", battery_level());
		self.power.preset_source = 0;
		return G_SOURCE_REMOVE;
	}
	if (self.power.swappable && !self.power.busy) {
		self.power.preset_source = 0;
		power_swap("source", NULL);
		return G_SOURCE_REMOVE;
	}
	if (++self.power.preset_tries >= POWER_PRESET_TRIES) {
		g_message("The USB-C partner has no PD contract, the phone keeps taking power");
		self.power.preset_source = 0;
		return G_SOURCE_REMOVE;
	}
	return G_SOURCE_CONTINUE;
}

static gboolean on_power_poll(gpointer user_data)
{
	power_refresh();
	/* Gone with the partner */
	return self.power.poll_source ? G_SOURCE_CONTINUE : G_SOURCE_REMOVE;
}

static void power_partner_changed(gboolean partner)
{
	self.power.partner = partner;
	g_message("USB-C partner %s", partner ? "attached" : "detached");
	g_clear_handle_id(&self.power.preset_source, g_source_remove);
	if (!partner) {
		/* The port goes back to its default role (sink) by itself */
		g_clear_handle_id(&self.power.poll_source, g_source_remove);
		return;
	}
	if (!self.power.poll_source)
		self.power.poll_source = g_timeout_add_seconds(POWER_POLL_S, on_power_poll, NULL);
	if (self.supply_power_on_connect) {
		self.power.preset_tries = 0;
		self.power.preset_source = g_timeout_add_seconds(1, on_power_preset, NULL);
	}
}

/* Read the port and tell what changed */
static void power_refresh(void)
{
	const char *port = self.power.port;
	gboolean partner = typec_partner_present(port);
	g_autofree char *role = partner ? typec_selected(port, "power_role") : NULL;
	gboolean swappable = FALSE;

	if (partner) {
		g_autofree char *type = typec_selected(port, "port_type");
		g_autofree char *mode = typec_read(port, "power_operation_mode");
		g_autofree char *partner_dir = typec_partner_dir(port);
		g_autofree char *pd = typec_read(partner_dir, "supports_usb_power_delivery");

		/* A dual-role port with a PD contract; the partner may still refuse */
		swappable = !g_strcmp0(type, "dual") && !g_strcmp0(mode, "usb_power_delivery") && g_strcmp0(pd, "no");
	}
	if (!role || (strcmp(role, "sink") && strcmp(role, "source"))) {
		g_free(role);
		role = g_strdup("");
	}

	if (partner != self.power.partner)
		power_partner_changed(partner);
	if (g_strcmp0(role, self.power.role)) {
		/* A swap, also one the partner asked for, not a plug-in or unplugging */
		gboolean swapped = *role && self.power.role && *self.power.role;

		g_message("Power role: %s", *role ? role : "none");
		g_free(self.power.role);
		self.power.role = g_steal_pointer(&role);
		emit_changed("PowerRole", g_variant_new_string(self.power.role));
		/* The direction of the power decides what charger detection sees */
		if (swapped)
			power_hold();
		else
			schedule_settle();
	}
	if (swappable != self.power.swappable) {
		self.power.swappable = swappable;
		emit_changed("PowerRoleSwappable", g_variant_new_boolean(swappable));
	}
	/* The battery got low while powering the device: the device takes over again. A partner
	 * that only takes power (no swap possible) keeps it, as it would without plinius-usb. */
	if (g_strcmp0(self.power.role, "source"))
		self.power.low_battery_asked = FALSE;
	else if (self.power.swappable && !self.power.busy && !self.power.low_battery_asked &&
		 battery_too_low()) {
		/* Once: a partner that refuses would be asked every few seconds otherwise */
		self.power.low_battery_asked = TRUE;
		g_message("The battery is at %d %%, the phone stops powering the device", battery_level());
		set_error("The battery is low, the phone stopped powering the device");
		power_swap("sink", NULL);
	}
}

static gboolean on_power_refresh(gpointer user_data)
{
	self.power.refresh_source = 0;
	power_refresh();
	return G_SOURCE_REMOVE;
}

static void power_refresh_soon(void)
{
	if (self.power.port && !self.power.refresh_source)
		self.power.refresh_source = g_timeout_add(POWER_REFRESH_MS, on_power_refresh, NULL);
}

static void power_set_role(const char *sender, const char *role, GDBusMethodInvocation *invocation)
{
	g_autoptr(GError) error = NULL;
	gboolean source = !g_strcmp0(role, "source");
	uid_t user;

	if (!source && g_strcmp0(role, "sink")) {
		g_dbus_method_invocation_return_error(invocation, G_DBUS_ERROR, G_DBUS_ERROR_INVALID_ARGS,
						      "Unknown power role %s", role);
		return;
	}
	if (!self.power.port) {
		g_dbus_method_invocation_return_error_literal(invocation, G_DBUS_ERROR, G_DBUS_ERROR_NOT_SUPPORTED,
							      "This USB port cannot change the direction of power");
		return;
	}
	power_refresh();
	if (!self.power.partner) {
		g_dbus_method_invocation_return_error_literal(invocation, G_DBUS_ERROR, G_DBUS_ERROR_FAILED,
							      "Nothing is connected");
		return;
	}
	if (!g_strcmp0(self.power.role, role)) {
		g_dbus_method_invocation_return_value(invocation, NULL);
		return;
	}
	/* Powering a device is switching something on, taking power again is not */
	if (source && !check_caller(sender, &user, &error)) {
		g_dbus_error_strip_remote_error(error);
		g_dbus_method_invocation_return_error_literal(invocation, G_DBUS_ERROR, G_DBUS_ERROR_ACCESS_DENIED,
							      error->message);
		return;
	}
	if (!self.power.swappable) {
		g_dbus_method_invocation_return_error_literal(invocation, G_DBUS_ERROR, G_DBUS_ERROR_NOT_SUPPORTED,
							      "The connected device does not support this");
		return;
	}
	if (source && battery_too_low()) {
		g_dbus_method_invocation_return_error_literal(invocation, G_DBUS_ERROR, G_DBUS_ERROR_FAILED,
							      "The battery is too low to power a device");
		return;
	}
	if (self.power.busy) {
		g_dbus_method_invocation_return_error_literal(invocation, G_DBUS_ERROR, G_DBUS_ERROR_FAILED,
							      "The power is switching already");
		return;
	}
	/* What the user chose wins over a preset still waiting */
	g_clear_handle_id(&self.power.preset_source, g_source_remove);
	power_swap(role, invocation);
}

/* ---- Plug-in and unplugging ---- */

static void set_connected(gboolean connected)
{
	if (connected == self.connected)
		return;
	self.connected = connected;
	g_message("Computer %s", connected ? "attached" : "detached");
	emit_changed("Connected", g_variant_new_boolean(connected));
	set_error("");
	/* Unplugging resets everything, plugging in only brings the preset */
	apply(connected && self.network_on_connect ? FN_NCM : 0, 0, NULL);
}

static gboolean on_settled(gpointer user_data)
{
	self.settle_source = 0;
	set_connected(read_connected());
	return G_SOURCE_REMOVE;
}

static void schedule_settle_in(guint ms)
{
	g_clear_handle_id(&self.settle_source, g_source_remove);
	self.settle_source = g_timeout_add(ms, on_settled, NULL);
}

static void schedule_settle(void)
{
	schedule_settle_in(SETTLE_MS);
}

static void on_uevent(GUdevClient *client, const char *action, GUdevDevice *device, gpointer user_data)
{
	const char *subsystem = g_udev_device_get_subsystem(device);

	/* USB-C and the data role: for the pogo pins; the partner and the power role */
	if (g_strcmp0(subsystem, "power_supply")) {
		pogo_kick();
		if (!g_strcmp0(subsystem, "typec"))
			power_refresh_soon();
		return;
	}
	/* The battery reports its level here too; only USB supplies matter */
	if (g_strcmp0(g_udev_device_get_sysfs_attr(device, "type"), "USB"))
		return;
	pogo_kick();
	schedule_settle();
}

/* ---- Settings and D-Bus ---- */

static void load_settings(void)
{
	g_autoptr(GKeyFile) file = g_key_file_new();
	g_autoptr(GError) error = NULL;
	g_autofree char *camera = NULL;
	gboolean on;

	if (!g_key_file_load_from_file(file, SETTINGS_FILE, G_KEY_FILE_NONE, NULL))
		return;
	on = g_key_file_get_boolean(file, "usb", "network-on-connect", &error);
	if (!error)
		self.network_on_connect = on;
	g_clear_error(&error);
	on = g_key_file_get_boolean(file, "usb", "supply-power-on-connect", &error);
	if (!error)
		self.supply_power_on_connect = on;
	camera = g_key_file_get_string(file, "usb", "webcam-camera", NULL);
	if (camera && *camera) {
		g_free(self.webcam_camera);
		self.webcam_camera = g_steal_pointer(&camera);
	}
}

static void save_settings(void)
{
	g_autoptr(GKeyFile) file = g_key_file_new();
	g_autoptr(GError) error = NULL;

	g_key_file_set_boolean(file, "usb", "network-on-connect", self.network_on_connect);
	g_key_file_set_boolean(file, "usb", "supply-power-on-connect", self.supply_power_on_connect);
	g_key_file_set_string(file, "usb", "webcam-camera", self.webcam_camera);
	if (!g_key_file_save_to_file(file, SETTINGS_FILE, &error))
		g_warning("Cannot save the settings: %s", error->message);
}

static void emit_changed(const char *property, GVariant *value)
{
	GVariantBuilder changed;

	if (!self.bus) {
		g_variant_unref(g_variant_ref_sink(value));
		return;
	}
	g_variant_builder_init(&changed, G_VARIANT_TYPE("a{sv}"));
	g_variant_builder_add(&changed, "{sv}", property, value);
	g_dbus_connection_emit_signal(self.bus, NULL, OBJECT_PATH, "org.freedesktop.DBus.Properties",
				      "PropertiesChanged", g_variant_new("(sa{sv}as)", INTERFACE, &changed, NULL),
				      NULL);
}

static void on_method_call(GDBusConnection *bus, const char *sender, const char *path, const char *iface,
			   const char *method, GVariant *params, GDBusMethodInvocation *invocation,
			   gpointer user_data)
{
	g_autoptr(GVariantIter) iter = NULL;
	g_autoptr(GError) error = NULL;
	const char *name;
	guint functions = 0;
	uid_t user = 0;

	if (!strcmp(method, "ChoosePort")) {
		g_variant_get(params, "(&s)", &name);
		pogo_choose_port(sender, name, invocation);
		return;
	}
	if (!strcmp(method, "SetPowerRole")) {
		g_variant_get(params, "(&s)", &name);
		power_set_role(sender, name, invocation);
		return;
	}

	g_variant_get(params, "(as)", &iter);
	while (g_variant_iter_next(iter, "&s", &name)) {
		guint i;

		for (i = 0; i < G_N_ELEMENTS(function_names); i++)
			if (!strcmp(name, function_names[i].name))
				break;
		if (i == G_N_ELEMENTS(function_names)) {
			g_dbus_method_invocation_return_error(invocation, G_DBUS_ERROR, G_DBUS_ERROR_INVALID_ARGS,
							      "Unknown function %s", name);
			return;
		}
		functions |= function_names[i].flag;
	}
	if (functions & FN_GATEWAY)
		functions |= FN_NCM;

	if (!self.connected) {
		g_dbus_method_invocation_return_error(invocation, G_DBUS_ERROR, G_DBUS_ERROR_FAILED,
						      "No computer attached");
		return;
	}
	if (functions & ~available_functions()) {
		g_dbus_method_invocation_return_error(invocation, G_DBUS_ERROR, G_DBUS_ERROR_NOT_SUPPORTED,
						      "Function not installed");
		return;
	}
	/* Switching off needs nobody's consent, switching on the unlocked user's */
	if ((functions & ~self.functions) && !check_caller(sender, &user, &error)) {
		g_dbus_error_strip_remote_error(error);
		g_dbus_method_invocation_return_error_literal(invocation, G_DBUS_ERROR, G_DBUS_ERROR_ACCESS_DENIED,
							      error->message);
		return;
	}
	if (functions == self.functions) {
		g_dbus_method_invocation_return_value(invocation, NULL);
		return;
	}
	if (!apply(functions, user, NULL)) {
		g_dbus_method_invocation_return_error_literal(invocation, G_DBUS_ERROR, G_DBUS_ERROR_FAILED,
							      self.error);
		return;
	}
	g_dbus_method_invocation_return_value(invocation, NULL);
}

static GVariant *on_get_property(GDBusConnection *bus, const char *sender, const char *path, const char *iface,
				 const char *property, GError **error, gpointer user_data)
{
	if (!strcmp(property, "Connected"))
		return g_variant_new_boolean(self.connected);
	if (!strcmp(property, "Functions"))
		return functions_variant(self.functions);
	if (!strcmp(property, "AvailableFunctions"))
		return functions_variant(available_functions());
	if (!strcmp(property, "NetworkOnConnect"))
		return g_variant_new_boolean(self.network_on_connect);
	if (!strcmp(property, "WebcamDevice"))
		return g_variant_new_string(self.webcam_device ?: "");
	if (!strcmp(property, "WebcamCamera"))
		return g_variant_new_string(self.webcam_camera);
	if (!strcmp(property, "Error"))
		return g_variant_new_string(self.error ?: "");
	if (!strcmp(property, "ActivePort"))
		return g_variant_new_string(pogo_port_name(self.pogo.active));
	if (!strcmp(property, "PortQuestion"))
		return g_variant_new_string(self.pogo.question == POGO_PORT_NONE ? ""
										 : pogo_port_name(self.pogo.question));
	if (!strcmp(property, "PowerRole"))
		return g_variant_new_string(self.power.role ?: "");
	if (!strcmp(property, "PowerRoleSwappable"))
		return g_variant_new_boolean(self.power.swappable);
	if (!strcmp(property, "SupplyPowerOnConnect"))
		return g_variant_new_boolean(self.supply_power_on_connect);
	g_set_error(error, G_DBUS_ERROR, G_DBUS_ERROR_UNKNOWN_PROPERTY, "Unknown property %s", property);
	return NULL;
}

static gboolean on_set_property(GDBusConnection *bus, const char *sender, const char *path, const char *iface,
				const char *property, GVariant *value, GError **error, gpointer user_data)
{
	gboolean on;
	uid_t user;

	if (!strcmp(property, "WebcamCamera")) {
		const char *camera = g_variant_get_string(value, NULL);

		/* A PipeWire node name of the bridge, nothing else reaches the helper's pipeline */
		if (!*camera || strlen(camera) > 64 || strspn(camera, "abcdefghijklmnopqrstuvwxyz0123456789_-.") != strlen(camera)) {
			g_set_error(error, G_DBUS_ERROR, G_DBUS_ERROR_INVALID_ARGS, "Invalid camera %s", camera);
			return FALSE;
		}
		if (!strcmp(camera, self.webcam_camera))
			return TRUE;
		/* Which camera a computer gets to see: only the user in front of the phone */
		if (!check_caller(sender, &user, error))
			return FALSE;
		g_free(self.webcam_camera);
		self.webcam_camera = g_strdup(camera);
		save_settings();
		emit_changed(property, g_variant_new_string(camera));
		return TRUE;
	}
	if (!strcmp(property, "SupplyPowerOnConnect")) {
		on = g_variant_get_boolean(value);
		if (on == self.supply_power_on_connect)
			return TRUE;
		/* It powers devices without unlocking, so setting it needs the unlocked user */
		if (on && !check_caller(sender, &user, error))
			return FALSE;
		self.supply_power_on_connect = on;
		save_settings();
		emit_changed(property, g_variant_new_boolean(on));
		return TRUE;
	}
	if (strcmp(property, "NetworkOnConnect")) {
		g_set_error(error, G_DBUS_ERROR, G_DBUS_ERROR_PROPERTY_READ_ONLY, "Property %s is read-only",
			    property);
		return FALSE;
	}
	on = g_variant_get_boolean(value);
	if (on == self.network_on_connect)
		return TRUE;
	/* The preset works without unlocking, so setting it needs the unlocked user */
	if (on && !check_caller(sender, &user, error))
		return FALSE;
	self.network_on_connect = on;
	save_settings();
	emit_changed(property, g_variant_new_boolean(on));
	return TRUE;
}

static const GDBusInterfaceVTable vtable = {on_method_call, on_get_property, on_set_property, {0}};

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
	/* typec and extcon tell about the pogo pins too: USB-C partners and the data role */
	static const char *const subsystems[] = {"power_supply", "typec", "extcon", NULL};
	guint owner;

	self.webcam_camera = g_strdup(WEBCAM_CAMERA_DEFAULT);
	load_settings();
	g_mkdir_with_parents(RUN_DIR, 0755);
	if (!g_file_test("/sys/kernel/config/usb_gadget", G_FILE_TEST_IS_DIR))
		mount("none", "/sys/kernel/config", "configfs", 0, NULL);

	self.udev = g_udev_client_new(subsystems);
	g_signal_connect(self.udev, "uevent", G_CALLBACK(on_uevent), NULL);

	/* Leftovers of an earlier instance, then the cable as it is now */
	teardown();
	self.power.role = g_strdup("");
	self.power.port = typec_find_port();
	g_message("Type-C port: %s", self.power.port ?: "none");
	power_refresh();
	self.connected = read_connected();
	g_message("Computer %s", self.connected ? "attached" : "not attached");
	if (self.connected && self.network_on_connect)
		apply(FN_NCM, 0, NULL);
	pogo_init();

	self.loop = g_main_loop_new(NULL, FALSE);
	owner = g_bus_own_name(G_BUS_TYPE_SYSTEM, BUS_NAME, G_BUS_NAME_OWNER_FLAGS_NONE, on_bus_acquired, NULL,
			       on_name_lost, NULL, NULL);
	g_unix_signal_add(SIGTERM, on_signal, NULL);
	g_unix_signal_add(SIGINT, on_signal, NULL);
	g_main_loop_run(self.loop);

	/* Nothing offered to a computer without the service deciding about it */
	teardown();
	g_bus_unown_name(owner);
	return 0;
}
