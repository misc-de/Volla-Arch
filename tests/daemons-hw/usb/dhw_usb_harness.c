/*
 * Harness for plinius-usb.c (a copy by dhw_harness.sandboxed_source, its paths in DHW_ROOT, main()
 * renamed): the gadget it builds in configfs, the gateway, File Sharing, the webcam, the pogo
 * pins, the power role and its D-Bus interface.
 *
 * What the copy reaches is faked here, nothing else:
 * - configfs: DHW_ROOT/cfg is a plain directory; mkdir of the gadget or of the UVC function also
 *   makes the groups configfs makes by itself (marked with a ".default" file), rmdir removes the
 *   attributes and those groups as configfs does, and refuses while links or items are left.
 * - mount/umount2 (FunctionFS), uname (the kernel release), kill: recorded, never real.
 * - The Type-C driver: write() of a power role is answered as told ("swap MODE").
 * - Time: a virtual clock. g_timeout_add* are timers on it, "advance MS" moves it and fires them;
 *   g_usleep moves it too (and sleeps a tenth of it, for the programs started meanwhile).
 * - The bus: GetConnectionUnixUser and logind as scripted ("bus KEY VALUE"); every
 *   PropertiesChanged is printed as "changed PROP VALUE". Method calls of the command mode
 *   answer as "reply METHOD ok" / "reply METHOD error DOMAIN:CODE: MESSAGE".
 *   In daemon mode (argv[1] "daemon") the copy's main() runs on DBUS_SYSTEM_BUS_ADDRESS, a private
 *   bus of the test: replies and signals also go out there.
 * - udev: uevents come from "uevent SUBSYSTEM [TYPE]"; the real client the copy creates listens to
 *   a subsystem no device has, so nothing of the build host reaches it.
 * Programs run for real from PATH and DHW_ROOT/bin: the test's stubs.
 *
 * Commands: the arguments, in their order; in daemon mode lines on stdin while the main loop runs.
 * Each is echoed as "> COMMAND" first; "done" ends the output of each in daemon mode.
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <gio/gio.h>
#include <glib-unix.h>
#include <glib/gstdio.h>
#include <gudev/gudev.h>
#include <pthread.h>
#include <pwd.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/utsname.h>
#include <unistd.h>

#ifndef DHW_ROOT
#error DHW_ROOT must name the test's directory
#endif
#define CFG_ROOT DHW_ROOT "/cfg"
#define GADGET_DIR CFG_ROOT "/usb_gadget/plinius"

static gboolean daemon_mode;
static pthread_t main_thread;

/* ---- Scripted failures: "fail KIND SUFFIX" makes the operation on a path ending so fail ---- */

typedef struct {
	const char *kind;
	char suffix[16][256];
	int n;
} FailList;

static FailList fails[] = {{"write"}, {"mkdir"}, {"mkdirp"}, {"symlink"}};

static gboolean fails_for(const char *kind, const char *path)
{
	for (guint i = 0; i < G_N_ELEMENTS(fails); i++)
		if (!strcmp(fails[i].kind, kind))
			for (int j = 0; j < fails[i].n; j++)
				if (g_str_has_suffix(path, fails[i].suffix[j]))
					return TRUE;
	return FALSE;
}

/* ---- Virtual clock ---- */

static gint64 vclock = G_GINT64_CONSTANT(1000000000000);

static gint64 fake_monotonic(void)
{
	return __atomic_load_n(&vclock, __ATOMIC_SEQ_CST);
}

typedef struct {
	guint id;
	gboolean active;
	gint64 due;
	guint interval_ms;
	GSourceFunc fn;
	gpointer data;
} VTimer;

static VTimer vtimers[256];
static guint vtimer_last = 1u << 24;

static guint fake_timeout_add(guint ms, GSourceFunc fn, gpointer data)
{
	for (guint i = 0; i < G_N_ELEMENTS(vtimers); i++)
		if (!vtimers[i].active) {
			vtimers[i] = (VTimer){++vtimer_last, TRUE, fake_monotonic() + (gint64)ms * 1000, ms, fn, data};
			return vtimers[i].id;
		}
	abort();
}

static guint fake_timeout_add_seconds(guint s, GSourceFunc fn, gpointer data)
{
	return fake_timeout_add(s * 1000, fn, data);
}

static gboolean fake_source_remove(guint id)
{
	for (guint i = 0; i < G_N_ELEMENTS(vtimers); i++)
		if (vtimers[i].active && vtimers[i].id == id) {
			vtimers[i].active = FALSE;
			return TRUE;
		}
	return g_source_remove(id);
}

static void pump_pending(void)
{
	while (g_main_context_iteration(NULL, FALSE))
		;
}

/* Move the clock by ms, firing the timers due on the way in their order */
static void vclock_advance(gint64 ms)
{
	gint64 target = fake_monotonic() + ms * 1000;

	for (;;) {
		int best = -1;
		VTimer t;
		gboolean again;

		pump_pending();
		for (guint i = 0; i < G_N_ELEMENTS(vtimers); i++)
			if (vtimers[i].active && vtimers[i].due <= target &&
			    (best < 0 || vtimers[i].due < vtimers[best].due))
				best = (int)i;
		if (best < 0)
			break;
		t = vtimers[best];
		if (t.due > fake_monotonic())
			__atomic_store_n(&vclock, t.due, __ATOMIC_SEQ_CST);
		again = t.fn(t.data);
		if (vtimers[best].active && vtimers[best].id == t.id) {
			if (again)
				vtimers[best].due = fake_monotonic() + (gint64)t.interval_ms * 1000;
			else
				vtimers[best].active = FALSE;
		}
	}
	if (target > fake_monotonic())
		__atomic_store_n(&vclock, target, __ATOMIC_SEQ_CST);
	pump_pending();
}

/* ---- Processes holding the webcam node: DHW_ROOT/proc/PID/fd/N ---- */

static void proc_close_fds(const char *pid_dir)
{
	g_autofree char *fds = g_build_filename(pid_dir, "fd", NULL);
	g_autoptr(GDir) dir = g_dir_open(fds, 0, NULL);
	const char *name;

	while (dir && (name = g_dir_read_name(dir))) {
		g_autofree char *path = g_build_filename(fds, name, NULL);

		unlink(path);
	}
}

/* A holder with a file "closes" closes the node by itself, at the first wait */
static void world_step(void)
{
	g_autoptr(GDir) dir = NULL;
	const char *name;

	if (!pthread_equal(pthread_self(), main_thread))
		return;
	dir = g_dir_open(DHW_ROOT "/proc", 0, NULL);
	while (dir && (name = g_dir_read_name(dir))) {
		g_autofree char *pid_dir = g_build_filename(DHW_ROOT "/proc", name, NULL);
		g_autofree char *marker = g_build_filename(pid_dir, "closes", NULL);

		if (g_file_test(marker, G_FILE_TEST_EXISTS)) {
			printf("closed by %s\n", name);
			proc_close_fds(pid_dir);
			unlink(marker);
		}
	}
}

static void fake_usleep(gulong us)
{
	__atomic_add_fetch(&vclock, (gint64)us, __ATOMIC_SEQ_CST);
	world_step();
	g_usleep(us / 10 ? us / 10 : 1);
}

/* Never a real signal: the process "dies" and its fds go, unless it has a file "stubborn" */
static int fake_kill(pid_t pid, int sig)
{
	g_autofree char *pid_dir = g_strdup_printf(DHW_ROOT "/proc/%d", (int)pid);
	g_autofree char *stubborn = g_build_filename(pid_dir, "stubborn", NULL);

	printf("kill %d %d\n", (int)pid, sig);
	if (!g_file_test(stubborn, G_FILE_TEST_EXISTS))
		proc_close_fds(pid_dir);
	return 0;
}

/* ---- configfs ---- */

static void cfg_default_group(const char *dir, const char *sub)
{
	g_autofree char *path = g_build_filename(dir, sub, NULL);
	g_autofree char *marker = g_build_filename(path, ".default", NULL);

	g_mkdir_with_parents(path, 0755);
	g_file_set_contents(marker, "", 0, NULL);
}

static int fake_mkdir(const char *path, mode_t mode)
{
	int r;

	if (fails_for("mkdir", path)) {
		errno = EACCES;
		return -1;
	}
	r = mkdir(path, mode);
	if (r == 0 && g_str_has_prefix(path, CFG_ROOT "/") && g_str_has_suffix(path, "/functions/uvc.usb0")) {
		static const char *const groups[] = {
			"control",		 "control/class",	  "control/class/fs", "control/class/ss",
			"control/header",	 "streaming",		  "streaming/class",  "streaming/class/fs",
			"streaming/class/hs",	 "streaming/class/ss",	  "streaming/header", "streaming/mjpeg",
		};

		for (guint i = 0; i < G_N_ELEMENTS(groups); i++)
			cfg_default_group(path, groups[i]);
	}
	return r;
}

static int fake_mkdir_with_parents(const char *path, int mode)
{
	int r;

	if (fails_for("mkdirp", path)) {
		errno = EACCES;
		return -1;
	}
	r = g_mkdir_with_parents(path, mode);
	if (r == 0 && g_file_test(GADGET_DIR, G_FILE_TEST_IS_DIR) &&
	    !g_file_test(GADGET_DIR "/functions", G_FILE_TEST_IS_DIR)) {
		cfg_default_group(GADGET_DIR, "functions");
		cfg_default_group(GADGET_DIR, "configs");
	}
	return r;
}

/* configfs: a default group goes with its item, an attribute with its directory; a link or an
 * item made by mkdir keeps the directory there */
static gboolean cfg_clear(const char *path, gboolean is_default)
{
	g_autoptr(GDir) dir = g_dir_open(path, 0, NULL);
	const char *name;
	gboolean ok = TRUE;

	while (dir && (name = g_dir_read_name(dir))) {
		g_autofree char *child = g_build_filename(path, name, NULL);
		g_autofree char *marker = g_build_filename(child, ".default", NULL);
		struct stat st;

		if (lstat(child, &st) < 0)
			continue;
		if (S_ISLNK(st.st_mode))
			ok = FALSE;
		else if (S_ISDIR(st.st_mode))
			ok = g_file_test(marker, G_FILE_TEST_EXISTS) && cfg_clear(child, TRUE) && ok;
	}
	if (!ok)
		return FALSE;
	g_dir_rewind(dir);
	while (dir && (name = g_dir_read_name(dir))) {
		g_autofree char *child = g_build_filename(path, name, NULL);

		if (!g_file_test(child, G_FILE_TEST_IS_DIR))
			unlink(child);
	}
	return !is_default || rmdir(path) == 0;
}

static int fake_rmdir(const char *path)
{
	if (g_str_has_prefix(path, CFG_ROOT "/") && g_file_test(path, G_FILE_TEST_IS_DIR) && !cfg_clear(path, FALSE)) {
		errno = ENOTEMPTY;
		return -1;
	}
	return rmdir(path);
}

static int fake_symlink(const char *target, const char *link)
{
	if (fails_for("symlink", link)) {
		errno = EACCES;
		return -1;
	}
	return symlink(target, link);
}

static gboolean fake_set_contents_full(const char *path, const char *contents, gssize length,
				       GFileSetContentsFlags flags, int mode, GError **error)
{
	if (fails_for("write", path)) {
		g_set_error(error, G_FILE_ERROR, G_FILE_ERROR_FAILED, "Failed to write \"%s\": Device or resource busy",
			    path);
		return FALSE;
	}
	return g_file_set_contents_full(path, contents, length, flags, mode, error);
}

/* ---- FunctionFS, kernel ---- */

static int mount_errno, umount_errno;
static char mounted[4096];

static int fake_mount(const char *source, const char *target, const char *type, unsigned long flags,
		      const void *data)
{
	printf("mount %s %s %s\n", source, target, type);
	if (mount_errno) {
		errno = mount_errno;
		return -1;
	}
	if (!strcmp(type, "functionfs"))
		snprintf(mounted, sizeof mounted, "%s", target);
	return 0;
}

static int fake_umount2(const char *target, int flags)
{
	if (umount_errno) {
		errno = umount_errno;
		return -1;
	}
	if (strcmp(target, mounted)) {
		errno = EINVAL;
		return -1;
	}
	printf("umount %s\n", target);
	mounted[0] = '\0';
	return 0;
}

static char kernel_release[65] = "6.1.192-plinius14";

static int fake_uname(struct utsname *name)
{
	memset(name, 0, sizeof *name);
	if (!strcmp(kernel_release, "fail")) {
		errno = EFAULT;
		return -1;
	}
	snprintf(name->release, sizeof name->release, "%s", kernel_release);
	return 0;
}

/* ---- The Type-C driver: only power_swap_thread() calls open() and write() ---- */

static char swap_mode[32] = "accept";

/* In command mode the driver answers when "pump swap" lets it: output in a fixed order */
static GMutex gate_mutex;
static GCond gate_cond;
static gboolean gate_open;

static void gate_wait(void)
{
	if (daemon_mode)
		return;
	g_mutex_lock(&gate_mutex);
	while (!gate_open)
		g_cond_wait(&gate_cond, &gate_mutex);
	gate_open = FALSE;
	g_mutex_unlock(&gate_mutex);
}

static void gate_let_through(void)
{
	g_mutex_lock(&gate_mutex);
	gate_open = TRUE;
	g_cond_signal(&gate_cond);
	g_mutex_unlock(&gate_mutex);
}

static int fake_open(const char *path, int flags, ...)
{
	gate_wait();
	if (!strcmp(swap_mode, "enoent")) {
		errno = ENOENT;
		return -1;
	}
	return open(path, flags);
}

static ssize_t fake_write(int fd, const void *buf, size_t count)
{
	g_autofree char *text = g_strndup(buf, count);
	gboolean source = g_str_has_prefix(text, "source");

	printf("driver power_role %s", text);
	if (!strcmp(swap_mode, "refuse")) {
		errno = EPERM;
		return -1;
	}
	if (!strcmp(swap_mode, "eio")) {
		errno = EIO;
		return -1;
	}
	if (!strcmp(swap_mode, "eopnotsupp")) {
		errno = EOPNOTSUPP;
		return -1;
	}
	if (!strcmp(swap_mode, "enxio")) {
		errno = ENXIO;
		return -1;
	}
	if (!strcmp(swap_mode, "accept") || !strcmp(swap_mode, "vanish")) {
		const char *now = source ? "[source] sink\n" : "source [sink]\n";

		if (ftruncate(fd, 0) < 0 || pwrite(fd, now, strlen(now), 0) < 0)
			abort();
	}
	if (!strcmp(swap_mode, "vanish")) {
		/* The cable is pulled while the partner agrees */
		unlink(DHW_ROOT "/typec/port0-partner/supports_usb_power_delivery");
		rmdir(DHW_ROOT "/typec/port0-partner");
	}
	/* "silent": the driver takes the request and nothing changes */
	return (ssize_t)count;
}

/* ---- udev ---- */

typedef struct {
	const char *subsystem;
	const char *type;
} FakeDev;

static GUdevClient *fake_udev_client_new(const char *const *subsystems)
{
	static const char *const none[] = {"dhw-usb-test-none", NULL};
	g_autofree char *names = g_strjoinv(",", (char **)subsystems);

	printf("udev %s\n", names);
	return g_udev_client_new(none);
}

static const char *fake_sysfs_attr(FakeDev *dev, const char *attr)
{
	return !strcmp(attr, "type") ? dev->type : NULL;
}

/* ---- The bus ---- */

static int fake_bus_object;
#define FAKE_BUS ((GDBusConnection *)&fake_bus_object)

static struct {
	gint64 caller;
	char session[128];
	gint64 suid;
	int locked;
} fb = {0, "/org/freedesktop/login1/session/_31", 0, 0};

static GVariant *fake_call_sync(GDBusConnection *bus, const char *dest, const char *path, const char *iface,
				const char *method, GVariant *params, const GVariantType *reply_type,
				GDBusCallFlags flags, int timeout, GCancellable *cancellable, GError **error)
{
	g_autoptr(GVariant) p = g_variant_ref_sink(params);
	const char *ifc, *prop;

	if (!strcmp(method, "GetConnectionUnixUser")) {
		if (fb.caller < 0) {
			g_set_error(error, G_DBUS_ERROR, G_DBUS_ERROR_NAME_HAS_NO_OWNER, "fake bus: no such sender");
			return NULL;
		}
		return g_variant_ref_sink(g_variant_new("(u)", (guint32)fb.caller));
	}
	g_variant_get(p, "(&s&s)", &ifc, &prop);
	if (!strcmp(prop, "ActiveSession") && fb.session[0])
		return g_variant_ref_sink(g_variant_new("(v)", g_variant_new("(so)", "31", fb.session)));
	if (!strcmp(prop, "User") && fb.suid >= 0)
		return g_variant_ref_sink(g_variant_new(
			"(v)", g_variant_new("(uo)", (guint32)fb.suid, "/org/freedesktop/login1/user/_1")));
	if (!strcmp(prop, "LockedHint"))
		return g_variant_ref_sink(g_variant_new("(v)", g_variant_new_boolean(fb.locked)));
	g_set_error(error, G_DBUS_ERROR, G_DBUS_ERROR_UNKNOWN_METHOD, "fake bus: %s", prop);
	return NULL;
}

static gboolean fake_emit_signal(GDBusConnection *bus, const char *dest, const char *path, const char *iface,
				 const char *signal, GVariant *params, GError **error)
{
	g_autoptr(GVariant) p = g_variant_ref_sink(params);
	g_autoptr(GVariantIter) iter = NULL;
	const char *name;
	GVariant *value;

	g_variant_get(p, "(&sa{sv}as)", NULL, &iter, NULL);
	while (g_variant_iter_next(iter, "{&sv}", &name, &value)) {
		g_autofree char *text = g_variant_print(value, FALSE);

		printf("changed %s %s\n", name, text);
		g_variant_unref(value);
	}
	if (bus == FAKE_BUS)
		return TRUE;
	return g_dbus_connection_emit_signal(bus, dest, path, iface, signal, p, error);
}

static gboolean fake_flush_sync(GDBusConnection *bus, GCancellable *cancellable, GError **error)
{
	printf("flush\n");
	return bus == FAKE_BUS || g_dbus_connection_flush_sync(bus, cancellable, error);
}

/* Method calls of the command mode; a real invocation (daemon mode) goes to the real functions */
typedef struct {
	const char *method;
} FakeInv;

static FakeInv fake_invs[64];
static guint fake_inv_next;

static gboolean is_fake(GDBusMethodInvocation *invocation)
{
	return (void *)invocation >= (void *)fake_invs && (void *)invocation < (void *)(fake_invs + 64);
}

static void fake_return_value(GDBusMethodInvocation *invocation, GVariant *value)
{
	if (!is_fake(invocation)) {
		g_dbus_method_invocation_return_value(invocation, value);
		return;
	}
	printf("reply %s ok\n", ((FakeInv *)invocation)->method);
}

static void fake_return_error_literal(GDBusMethodInvocation *invocation, GQuark domain, int code,
				      const char *message)
{
	if (!is_fake(invocation)) {
		g_dbus_method_invocation_return_error_literal(invocation, domain, code, message);
		return;
	}
	printf("reply %s error %s:%d: %s\n", ((FakeInv *)invocation)->method, g_quark_to_string(domain), code,
	       message);
}

static void G_GNUC_PRINTF(4, 5) fake_return_error(GDBusMethodInvocation *invocation, GQuark domain, int code,
						   const char *format, ...)
{
	va_list args;
	g_autofree char *message = NULL;

	va_start(args, format);
	message = g_strdup_vprintf(format, args);
	va_end(args);
	fake_return_error_literal(invocation, domain, code, message);
}

#define g_file_set_contents_full fake_set_contents_full
#define mkdir fake_mkdir
#define g_mkdir_with_parents fake_mkdir_with_parents
#define g_rmdir fake_rmdir
#define symlink fake_symlink
#define mount fake_mount
#define umount2 fake_umount2
#define uname fake_uname
#define kill fake_kill
#define open fake_open
#define write fake_write
#define g_usleep fake_usleep
#define g_get_monotonic_time fake_monotonic
#define g_timeout_add fake_timeout_add
#define g_timeout_add_seconds fake_timeout_add_seconds
#define g_source_remove fake_source_remove
#define g_udev_client_new fake_udev_client_new
#define g_udev_device_get_subsystem(d) (((FakeDev *)(d))->subsystem)
#define g_udev_device_get_sysfs_attr(d, a) fake_sysfs_attr((FakeDev *)(d), (a))
#define g_dbus_connection_call_sync fake_call_sync
#define g_dbus_connection_emit_signal fake_emit_signal
#define g_dbus_connection_flush_sync fake_flush_sync
#define g_dbus_method_invocation_return_value fake_return_value
#define g_dbus_method_invocation_return_error fake_return_error
#define g_dbus_method_invocation_return_error_literal fake_return_error_literal
#define main real_main
#include "plinius-usb.c"
#undef main
#undef g_file_set_contents_full
#undef mkdir
#undef g_mkdir_with_parents
#undef g_rmdir
#undef symlink
#undef mount
#undef umount2
#undef uname
#undef kill
#undef open
#undef write
#undef g_usleep
#undef g_get_monotonic_time
#undef g_timeout_add
#undef g_timeout_add_seconds
#undef g_source_remove
#undef g_udev_client_new
#undef g_dbus_connection_call_sync
#undef g_dbus_connection_emit_signal
#undef g_dbus_connection_flush_sync

/* ---- Commands ---- */

static const char *timer_name(GSourceFunc fn)
{
	static const struct {
		GSourceFunc fn;
		const char *name;
	} names[] = {
		{on_settled, "settle"},	      {on_webcam_timeout, "webcam"},   {on_pogo_window_over, "pogo-window"},
		{on_pogo_tick, "pogo-tick"},  {on_power_preset, "power-preset"}, {on_power_poll, "power-poll"},
		{on_power_refresh, "power-refresh"},
	};

	for (guint i = 0; i < G_N_ELEMENTS(names); i++)
		if (names[i].fn == fn)
			return names[i].name;
	return "?";
}

static void print_timers(void)
{
	GString *out = g_string_new("timers");

	for (guint i = 0; i < G_N_ELEMENTS(vtimers); i++)
		if (vtimers[i].active)
			g_string_append_printf(out, " %s:%" G_GINT64_FORMAT, timer_name(vtimers[i].fn),
					       (vtimers[i].due - fake_monotonic()) / 1000);
	puts(out->str);
	g_string_free(out, TRUE);
}

static void print_state(void)
{
	g_autofree char *fns = functions_text(self.functions);

	printf("state connected=%d functions=%s user=%u error=%s webcam=%s camera=%s umtprd=%d dnsmasq=%d\n",
	       self.connected, fns, (unsigned)self.user, self.error ?: "", self.webcam_device ?: "",
	       self.webcam_camera ?: "", self.umtprd != NULL, self.dnsmasq != NULL);
	printf("power port=%s partner=%d role=%s swappable=%d busy=%d hold=%d asked=%d supply=%d\n",
	       self.power.port ?: "-", self.power.partner, self.power.role ?: "-", self.power.swappable,
	       self.power.busy, self.power.hold_until > fake_monotonic(), self.power.low_battery_asked,
	       self.supply_power_on_connect);
	printf("pogo present=%d flag=%d active=%s question=%s before=%s data=%d window=%d\n", self.pogo.present,
	       self.pogo.flag, pogo_port_name(self.pogo.active), pogo_port_name(self.pogo.question),
	       pogo_port_name(self.pogo.before), self.pogo.has_data, self.pogo.window_source != 0);
}

static void call(const char *method, GVariant *params)
{
	FakeInv *inv = &fake_invs[fake_inv_next++ % G_N_ELEMENTS(fake_invs)];
	g_autoptr(GVariant) p = g_variant_ref_sink(params);

	inv->method = method;
	on_method_call(self.bus, ":1.7", OBJECT_PATH, INTERFACE, method, p, (GDBusMethodInvocation *)inv, NULL);
}

static GVariant *parse_value(const char *text)
{
	if (!strcmp(text, "true") || !strcmp(text, "false"))
		return g_variant_new_boolean(!strcmp(text, "true"));
	return g_variant_new_string(strcmp(text, "-") ? text : "");
}

/* Waits (really) until a condition holds: what a thread or a program does takes its time */
static gboolean condition(const char *what)
{
	if (!strcmp(what, "swap"))
		return !self.power.busy;
	if (!strcmp(what, "dnsmasq"))
		return !self.dnsmasq;
	if (!strcmp(what, "umtprd"))
		return !self.umtprd;
	/* file:PATH: a program the daemon started has got as far as writing PATH (a stand-in's log) */
	if (!strncmp(what, "file:", 5))
		return g_file_test(what + 5, G_FILE_TEST_EXISTS);
	return TRUE;
}

static gboolean pump_tick(gpointer data)
{
	return G_SOURCE_CONTINUE;
}

static void pump_until(const char *what)
{
	/* Real time: g_get_monotonic_time is the virtual clock here, which stands still. A real
	 * tick wakes the blocking iteration, so a condition no event of the main context announces
	 * (a file a program writes, late under load) is seen too, and the 10 s hold */
	gint64 end = g_get_real_time() + 10 * G_USEC_PER_SEC;
	GSource *tick = g_timeout_source_new(10);

	g_source_set_callback(tick, pump_tick, NULL, NULL);
	g_source_attach(tick, NULL);
	if (!strcmp(what, "swap") && self.power.busy)
		gate_let_through();
	while (!condition(what) && g_get_real_time() < end)
		g_main_context_iteration(NULL, TRUE);
	g_source_destroy(tick);
	g_source_unref(tick);
	pump_pending();
	printf("pumped %s %s\n", what, condition(what) ? "yes" : "no");
}

/* The rest of a command after its keyword, NULL for another command */
static const char *arg(const char *line, const char *keyword)
{
	return g_str_has_prefix(line, keyword) ? line + strlen(keyword) : NULL;
}

static void command(char *line)
{
	char a[4096] = "", b[4096] = "";
	long n = 0, m = 0;
	const char *x;

	printf("> %s\n", line);
	if ((x = arg(line, "bus ")) && sscanf(x, "%4095s %4095s", a, b) == 2) {
		if (!strcmp(a, "caller"))
			fb.caller = atoll(b);
		else if (!strcmp(a, "session"))
			snprintf(fb.session, sizeof fb.session, "%s", strcmp(b, "-") ? b : "");
		else if (!strcmp(a, "suid"))
			fb.suid = atoll(b);
		else if (!strcmp(a, "locked"))
			fb.locked = atoi(b);
	} else if ((x = arg(line, "fail ")) && sscanf(x, "%4095s %4095s", a, b) == 2) {
		for (guint i = 0; i < G_N_ELEMENTS(fails); i++)
			if (!strcmp(fails[i].kind, a))
				snprintf(fails[i].suffix[fails[i].n++], sizeof fails[i].suffix[0], "%s", b);
	} else if ((x = arg(line, "unfail ")) && sscanf(x, "%4095s", a) == 1) {
		for (guint i = 0; i < G_N_ELEMENTS(fails); i++)
			if (!strcmp(fails[i].kind, a) || !strcmp(a, "all"))
				fails[i].n = 0;
	} else if ((x = arg(line, "mount-errno ")) && sscanf(x, "%ld", &n) == 1) {
		mount_errno = (int)n;
	} else if ((x = arg(line, "umount-errno ")) && sscanf(x, "%ld", &n) == 1) {
		umount_errno = (int)n;
	} else if ((x = arg(line, "kernel ")) && sscanf(x, "%64s", a) == 1) {
		snprintf(kernel_release, sizeof kernel_release, "%s", a);
	} else if ((x = arg(line, "swap ")) && sscanf(x, "%31s", a) == 1) {
		snprintf(swap_mode, sizeof swap_mode, "%s", a);
	} else if ((x = arg(line, "advance ")) && sscanf(x, "%ld", &n) == 1) {
		vclock_advance(n);
	} else if ((x = arg(line, "pump ")) && sscanf(x, "%4095s", a) == 1) {
		pump_until(a);
	} else if (!strcmp(line, "timers")) {
		print_timers();
	} else if (!strcmp(line, "state")) {
		print_state();
	} else if ((x = arg(line, "set ")) && sscanf(x, "%4095s %ld", a, &n) == 2) {
		/* The daemon's state as a test starts from it */
		if (!strcmp(a, "connected"))
			self.connected = n != 0;
		else if (!strcmp(a, "functions"))
			self.functions = (guint)n;
		else if (!strcmp(a, "user"))
			self.user = (uid_t)n;
		else if (!strcmp(a, "network_on_connect"))
			self.network_on_connect = n != 0;
		else if (!strcmp(a, "supply"))
			self.supply_power_on_connect = n != 0;
		else if (!strcmp(a, "question"))
			self.pogo.question = (PogoPort)n;
		else if (!strcmp(a, "busy"))
			self.power.busy = n != 0;
		else if (!strcmp(a, "pogo_flag"))
			self.pogo.flag = (int)n;
	} else if ((x = arg(line, "set port ")) && sscanf(x, "%4095s", a) == 1) {
		g_free(self.power.port);
		self.power.port = strcmp(a, "-") ? g_strdup(a) : NULL;
	} else if ((x = arg(line, "set webcam_device ")) && sscanf(x, "%4095s", a) == 1) {
		g_free(self.webcam_device);
		self.webcam_device = g_strdup(strcmp(a, "-") ? a : "");
	} else if ((x = arg(line, "call SetFunctions ")) && sscanf(x, "%4095s", a) == 1) {
		GVariantBuilder names;
		g_auto(GStrv) parts = g_strsplit(a, ",", -1);

		g_variant_builder_init(&names, G_VARIANT_TYPE("as"));
		for (char **p = parts; *p; p++)
			if (strcmp(*p, "-"))
				g_variant_builder_add(&names, "s", *p);
		call("SetFunctions", g_variant_new("(as)", &names));
	} else if ((x = arg(line, "call ")) && sscanf(x, "%4095s %4095s", a, b) == 2) {
		call(g_intern_string(a), g_variant_new("(s)", b));
	} else if ((x = arg(line, "get ")) && sscanf(x, "%4095s", a) == 1) {
		g_autoptr(GError) error = NULL;
		g_autoptr(GVariant) value = on_get_property(self.bus, ":1.7", OBJECT_PATH, INTERFACE, a, &error, NULL);
		g_autofree char *text = value ? g_variant_print(g_variant_ref_sink(value), FALSE) : NULL;

		if (value)
			printf("prop %s %s\n", a, text);
		else
			printf("prop %s error %s\n", a, error->message);
	} else if ((x = arg(line, "setprop ")) && sscanf(x, "%4095s %4095s", a, b) == 2) {
		g_autoptr(GError) error = NULL;
		g_autoptr(GVariant) value = g_variant_ref_sink(parse_value(b));

		if (on_set_property(self.bus, ":1.7", OBJECT_PATH, INTERFACE, a, value, &error, NULL))
			printf("setprop ok\n");
		else
			printf("setprop error %s\n", error->message);
	} else if ((x = arg(line, "uevent ")) && sscanf(x, "%4095s %4095s", a, b) >= 1) {
		FakeDev dev = {a, *b ? b : NULL};

		on_uevent(self.udev, "change", (GUdevDevice *)&dev, NULL);
	} else if ((x = arg(line, "apply ")) && sscanf(x, "%ld %ld", &n, &m) == 2) {
		printf("apply %d\n", apply((guint)n, (uid_t)m, NULL));
	} else if (!strcmp(line, "available")) {
		g_autofree char *text = functions_text(available_functions());

		printf("available %s\n", text);
	} else if (!strcmp(line, "kernel_has_uvc")) {
		printf("uvc %d\n", kernel_has_uvc());
	} else if (!strcmp(line, "read_connected")) {
		printf("read_connected %d\n", read_connected());
	} else if (!strcmp(line, "battery")) {
		printf("battery %d low=%d\n", battery_level(), battery_too_low());
	} else if (!strcmp(line, "find_port")) {
		g_autofree char *port = typec_find_port();

		printf("port %s\n", port ?: "-");
	} else if (!strcmp(line, "init_power")) {
		/* As main() does it */
		g_free(self.power.role);
		self.power.role = g_strdup("");
		g_free(self.power.port);
		self.power.port = typec_find_port();
		power_refresh();
	} else if (!strcmp(line, "power_refresh")) {
		power_refresh();
	} else if (!strcmp(line, "teardown")) {
		teardown();
	} else if (!strcmp(line, "load_settings")) {
		load_settings();
	} else if (!strcmp(line, "pogo_init")) {
		pogo_init();
	} else if (!strcmp(line, "webcam_release")) {
		webcam_release();
	} else if (!strcmp(line, "webcam_timeout")) {
		on_webcam_timeout(NULL);
	} else if ((x = arg(line, "set_connected ")) && sscanf(x, "%ld", &n) == 1) {
		set_connected(n != 0);
	} else if (!strcmp(line, "self_proc")) {
		/* The daemon itself holds the node too: it never counts (nor gets killed) */
		g_autofree char *fd = g_strdup_printf(DHW_ROOT "/proc/%d/fd", (int)getpid());
		g_autofree char *link = g_build_filename(fd, "5", NULL);

		g_mkdir_with_parents(fd, 0755);
		if (symlink("/dev/null", link) < 0)
			abort();
	} else if ((x = arg(line, "put ")) && sscanf(x, "%4095s", a) == 1) {
		/* The world changes meanwhile: "put REL TEXT" writes DHW_ROOT/REL, \n for a newline */
		g_autofree char *path = g_build_filename(DHW_ROOT, a, NULL);
		g_autofree char *dir = g_path_get_dirname(path);
		const char *text = x + strlen(a);
		g_autofree char *value = g_strcompress(*text ? text + 1 : text);

		g_mkdir_with_parents(dir, 0755);
		if (!g_file_set_contents(path, value, -1, NULL))
			abort();
	} else if ((x = arg(line, "rm ")) && sscanf(x, "%4095s", a) == 1) {
		/* "rm REL" removes a file or a directory with its files */
		g_autofree char *path = g_build_filename(DHW_ROOT, a, NULL);
		g_autoptr(GDir) dir = g_dir_open(path, 0, NULL);
		const char *name;

		while (dir && (name = g_dir_read_name(dir))) {
			g_autofree char *child = g_build_filename(path, name, NULL);

			unlink(child);
		}
		if (remove(path) < 0)
			abort();
	} else if (!strcmp(line, "quit")) {
		/* As systemd stops the service */
		raise(SIGTERM);
	} else if (*line) {
		printf("<unknown command: %.60s>\n", line);
	}
	if (daemon_mode)
		puts("done");
}

static gboolean on_stdin(GIOChannel *channel, GIOCondition condition, gpointer user_data)
{
	g_autofree char *line = NULL;

	if (g_io_channel_read_line(channel, &line, NULL, NULL, NULL) != G_IO_STATUS_NORMAL)
		return G_SOURCE_REMOVE;
	g_strchomp(line);
	command(line);
	return G_SOURCE_CONTINUE;
}

/* Daemon mode: the clock runs ten times as fast as the real one */
static gboolean on_tick(gpointer user_data)
{
	vclock_advance(100);
	return G_SOURCE_CONTINUE;
}

int main(int argc, char **argv)
{
	setvbuf(stdout, NULL, _IOLBF, 0);
	main_thread = pthread_self();
	fb.caller = fb.suid = getuid();
	if (argc > 1 && !strcmp(argv[1], "daemon")) {
		GIOChannel *in = g_io_channel_unix_new(0);
		int r;

		daemon_mode = TRUE;
		g_io_add_watch(in, G_IO_IN | G_IO_HUP, on_stdin, NULL);
		g_timeout_add(10, on_tick, NULL);
		r = real_main();
		printf("exit %d\n", r);
		return r;
	}
	self.bus = FAKE_BUS;
	self.webcam_camera = g_strdup(WEBCAM_CAMERA_DEFAULT);
	self.power.role = g_strdup("");
	for (int i = 1; i < argc; i++)
		command(argv[i]);
	/* What the commands left running ends with the harness; the files stay for the test */
	if (self.umtprd)
		g_subprocess_force_exit(self.umtprd);
	if (self.dnsmasq)
		g_subprocess_force_exit(self.dnsmasq);
	return 0;
}
