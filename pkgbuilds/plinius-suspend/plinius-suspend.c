// plinius-suspend: puts the phone to sleep whenever the display is off, as Android does and UT's
// repowerd. The kernel only suspends with s2idle here, and logind would pause the seat around it
// and wait for delay inhibitors; like repowerd this writes to /sys/power directly instead, with
// the wakeup_count handshake so that no wakeup event in between is lost (docs/STANDBY.md).
//
// Loop: display off (signal display_status_ind of plinius-mce, com.nokia.mce) → wait OFF_DELAY →
// check that the display is really off, no USB host is connected (its wakeup source would block
// anyway) and no logind "sleep" block inhibitor is held (a call, music, systemd-inhibit) → read
// /sys/power/wakeup_count, write it back, write "mem" to /sys/power/state. That blocks until the
// next wake-up. Then stay awake AWAKE_DELAY so that whatever woke the phone can be handled (an
// incoming call turns the display on), log the wake-up reason and go to sleep again.
//
// The kernel holds the suspend back while a wakeup source is active: the drivers' own (modem,
// Wi-Fi, the display's CRTC), and the ones user space takes through /sys/power/wake_lock.
// Never suspend with the display on: that hangs the display driver (mtk_drm_sys_suspend).
//
// Whatever keeps the phone awake for longer than LOG_AFTER_MS while the display is off is named
// in the journal: a sleep inhibitor (who and why), a USB host, the connector still on, or the
// active wakeup sources when the kernel refuses or holds back the suspend. A night that sleeps
// less than it should can then be told from the log alone.
//
// Always-on display: while Phosh shows it ($XDG_RUNTIME_DIR/plinius-aod exists), the panel keeps
// the picture with the CRTC off, so suspending is fine; an alarm wakes the phone at each full
// minute so that Phosh can draw the new time (the CRTC comes on for a moment for that).
//
// Time sync: the kernel fires every TFD_TIMER_CANCEL_ON_SET timer at each resume, which
// systemd-timesyncd takes for a clock jump, so it asks its server at every wake-up, hundreds of
// times a night (docs/STANDBY.md). It is stopped before the first suspend of a dark phase and
// started again with the display, unless automatic time is off (the unit is not enabled then).
//
// Usage: plinius-suspend [--off-delay=SECONDS] [--awake-delay=SECONDS]
// SPDX-License-Identifier: MIT
#include <errno.h>
#include <fcntl.h>
#include <gio/gio.h>
#include <glob.h>
#include <sys/timerfd.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define DPMS_FILE "/sys/class/drm/card0-DSI-1/dpms"
#define UDC_DIR "/sys/class/udc"
#define WAKEUP_COUNT "/sys/power/wakeup_count"
#define POWER_STATE "/sys/power/state"
#define RESUME_REASON "/sys/kernel/wakeup_reasons/last_resume_reason"
// One directory per wakeup source; active_time_ms is not 0 while it is active
#define WAKEUP_CLASS "/sys/class/wakeup"
// Created by phosh-plinius while the always-on display is shown
#define AOD_FLAG "/run/user/*/plinius-aod"
// Wake this long after the full minute: the clock has changed by then
#define AOD_WAKE_AFTER_MINUTE_MS 300

#define MCE_SERVICE "com.nokia.mce"
#define MCE_REQUEST_PATH "/com/nokia/mce/request"
#define MCE_REQUEST_IF "com.nokia.mce.request"
#define MCE_SIGNAL_PATH "/com/nokia/mce/signal"
#define MCE_SIGNAL_IF "com.nokia.mce.signal"

#define LOGIND_SERVICE "org.freedesktop.login1"
#define LOGIND_PATH "/org/freedesktop/login1"
#define LOGIND_IF "org.freedesktop.login1.Manager"

#define SYSTEMD_SERVICE "org.freedesktop.systemd1"
#define SYSTEMD_PATH "/org/freedesktop/systemd1"
#define SYSTEMD_IF "org.freedesktop.systemd1.Manager"
#define TIMESYNC_UNIT "systemd-timesyncd.service"

// Retry delays after a refused suspend (a wakeup source was active): doubles up to the maximum
#define RETRY_MIN_MS 1000
#define RETRY_MAX_MS 10000
// While a sleep inhibitor is held or a USB host is connected, look again this often
#define BLOCKED_POLL_MS 10000
// What holds the suspend back is logged once it has done so this long
#define LOG_AFTER_MS 30000
// Refused attempts in a row (at the back-off delays about this many seconds) before the active
// wakeup sources are logged
#define REFUSALS_LOGGED 6

// Outcome of one attempt in the worker thread
typedef enum {
	ATTEMPT_SLEPT,		// suspended and woke up again
	ATTEMPT_REFUSED,	// a wakeup event came in between (EBUSY or count changed)
	ATTEMPT_CANCELLED,	// the display came on meanwhile
	ATTEMPT_FAILED,		// unexpected error
} Attempt;

typedef struct {
	Attempt result;
	int error;		// errno for ATTEMPT_FAILED
	double slept;		// seconds in suspend (boot time minus monotonic time)
} AttemptResult;

// Why no attempt is made, while it lasts
typedef struct {
	char *reason;		// NULL: nothing blocks
	gint64 since;		// monotonic time in µs
	gboolean logged;
} Blocker;

static struct {
	GDBusConnection *bus;
	unsigned off_delay_ms;
	unsigned awake_delay_ms;
	gboolean display_off;
	gint cancel;		// atomic: the display came on while the worker runs
	gboolean busy;		// a worker thread is running
	unsigned timer;		// pending attempt
	unsigned retry_ms;
	// Statistics of the current display-off period, logged when the display comes on
	unsigned suspends;
	double slept;
	gint64 off_since;	// monotonic time in µs
	gboolean timesync_stopped;	// stopped by this service, to be started with the display
	unsigned log_after_ms;
	Blocker blocker;
	unsigned refusals;	// in a row
	gint reading;		// atomic: the worker waits in the wakeup_count read
	unsigned slow_watch;	// logs the wakeup sources when that read takes long
} st;

static gboolean read_line(const char *path, char *buf, size_t size)
{
	g_autofree char *text = NULL;
	if (!g_file_get_contents(path, &text, NULL, NULL))
		return FALSE;
	g_strlcpy(buf, g_strstrip(text), size);
	return TRUE;
}

// The DRM connector, not the remembered signal: the last word before writing "mem"
static gboolean display_is_off(void)
{
	char state[16];
	return read_line(DPMS_FILE, state, sizeof(state)) && strcmp(state, "Off") == 0;
}

// A USB host (the PC) is connected: the gadget's wakeup source keeps the phone awake anyway, and
// wakeup_count would block for as long as the cable is in
static gboolean usb_host_connected(void)
{
	g_autoptr(GDir) dir = g_dir_open(UDC_DIR, 0, NULL);
	const char *name;
	while (dir && (name = g_dir_read_name(dir))) {
		g_autofree char *path = g_build_filename(UDC_DIR, name, "state", NULL);
		char state[32];
		if (read_line(path, state, sizeof(state)) && strcmp(state, "configured") == 0)
			return TRUE;
	}
	return FALSE;
}

// A logind block inhibitor for "sleep" (BlockInhibited is e.g. "shutdown:sleep:idle"). Asked at
// each attempt: logind does not signal changes of it. Without an answer suspending is allowed.
static gboolean sleep_inhibited(void)
{
	g_autoptr(GError) error = NULL;
	g_autoptr(GVariant) reply = g_dbus_connection_call_sync(st.bus, LOGIND_SERVICE, LOGIND_PATH,
		"org.freedesktop.DBus.Properties", "Get", g_variant_new("(ss)", LOGIND_IF, "BlockInhibited"),
		G_VARIANT_TYPE("(v)"), G_DBUS_CALL_FLAGS_NONE, 2000, NULL, &error);
	if (!reply) {
		g_warning("logind BlockInhibited: %s", error->message);
		return FALSE;
	}
	g_autoptr(GVariant) value = NULL;
	g_variant_get(reply, "(v)", &value);
	g_auto(GStrv) what = g_strsplit(g_variant_get_string(value, NULL), ":", -1);
	return g_strv_contains((const char *const *)what, "sleep");
}

// Who holds the sleep block, e.g. "gnome-calls (Call in progress, pid 812)"
static char *sleep_inhibitors(void)
{
	g_autoptr(GVariant) reply = g_dbus_connection_call_sync(st.bus, LOGIND_SERVICE, LOGIND_PATH, LOGIND_IF,
		"ListInhibitors", NULL, G_VARIANT_TYPE("(a(ssssuu))"), G_DBUS_CALL_FLAGS_NONE, 2000, NULL, NULL);
	g_autoptr(GString) names = g_string_new(NULL);
	if (reply) {
		g_autoptr(GVariantIter) iter = NULL;
		const char *what, *who, *why, *mode;
		guint32 uid, pid;
		g_variant_get(reply, "(a(ssssuu))", &iter);
		while (g_variant_iter_next(iter, "(&s&s&s&suu)", &what, &who, &why, &mode, &uid, &pid)) {
			g_auto(GStrv) kinds = g_strsplit(what, ":", -1);
			if (strcmp(mode, "block") || !g_strv_contains((const char *const *)kinds, "sleep"))
				continue;
			g_string_append_printf(names, "%s%s (%s, pid %u)", names->len ? ", " : "", who, why, pid);
		}
	}
	return g_strdup_printf("sleep inhibitor %s", names->len ? names->str : "(unnamed)");
}

// The active wakeup sources with how long each has been active, e.g. "bt_drv_io (812 s)"
static char *active_wakeup_sources(void)
{
	g_autoptr(GDir) dir = g_dir_open(WAKEUP_CLASS, 0, NULL);
	g_autoptr(GString) names = g_string_new(NULL);
	const char *entry;
	while (dir && (entry = g_dir_read_name(dir))) {
		g_autofree char *time_path = g_build_filename(WAKEUP_CLASS, entry, "active_time_ms", NULL);
		g_autofree char *name_path = g_build_filename(WAKEUP_CLASS, entry, "name", NULL);
		char ms[32], name[64];
		if (!read_line(time_path, ms, sizeof(ms)) || g_ascii_strtoull(ms, NULL, 10) == 0)
			continue;
		if (!read_line(name_path, name, sizeof(name)))
			g_strlcpy(name, entry, sizeof(name));
		g_string_append_printf(names, "%s%s (%.0f s)", names->len ? ", " : "", name,
				       g_ascii_strtoull(ms, NULL, 10) / 1000.0);
	}
	return g_strdup(names->len ? names->str : "none");
}

// Remember what blocks now (takes the string; NULL: nothing). Logged once it has lasted
// log_after_ms, and its end only when its start was logged.
static void set_blocker(char *reason)
{
	gint64 now = g_get_monotonic_time();
	if (g_strcmp0(reason, st.blocker.reason) != 0) {
		if (st.blocker.logged)
			g_message("%s no longer blocks, after %.0f s", st.blocker.reason,
				  (now - st.blocker.since) / 1e6);
		g_free(st.blocker.reason);
		st.blocker.reason = reason;
		st.blocker.since = now;
		st.blocker.logged = FALSE;
	} else {
		g_free(reason);
	}
	if (st.blocker.reason && !st.blocker.logged && now - st.blocker.since >= st.log_after_ms * (gint64)1000) {
		st.blocker.logged = TRUE;
		g_message("not suspending for %.0f s: %s", (now - st.blocker.since) / 1e6, st.blocker.reason);
	}
}

// Automatic time is on: timedated enables the unit for it and disables it otherwise
static gboolean timesync_enabled(void)
{
	g_autoptr(GError) error = NULL;
	g_autoptr(GVariant) reply = g_dbus_connection_call_sync(st.bus, SYSTEMD_SERVICE, SYSTEMD_PATH, SYSTEMD_IF,
		"GetUnitFileState", g_variant_new("(s)", TIMESYNC_UNIT), G_VARIANT_TYPE("(s)"),
		G_DBUS_CALL_FLAGS_NONE, 2000, NULL, &error);
	if (!reply) {
		g_warning("%s state: %s", TIMESYNC_UNIT, error->message);
		return FALSE;
	}
	const char *state;
	g_variant_get(reply, "(&s)", &state);
	return strcmp(state, "enabled") == 0;
}

static void on_timesync_done(GObject *bus, GAsyncResult *result, gpointer data)
{
	g_autoptr(GError) error = NULL;
	g_autoptr(GVariant) reply = g_dbus_connection_call_finish(G_DBUS_CONNECTION(bus), result, &error);
	if (!reply)
		g_warning("%s %s: %s", (const char *)data, TIMESYNC_UNIT, error->message);
}

// Asynchronous: a suspend that overtakes the job only means one more request to the time server
static void timesync_call(const char *method)
{
	g_dbus_connection_call(st.bus, SYSTEMD_SERVICE, SYSTEMD_PATH, SYSTEMD_IF, method,
			       g_variant_new("(ss)", TIMESYNC_UNIT, "replace"), G_VARIANT_TYPE("(o)"),
			       G_DBUS_CALL_FLAGS_NONE, 5000, NULL, on_timesync_done, (gpointer)method);
}

static void timesync_stop(void)
{
	if (st.timesync_stopped || !timesync_enabled())
		return;
	st.timesync_stopped = TRUE;
	timesync_call("StopUnit");
}

static void timesync_start(void)
{
	if (!st.timesync_stopped)
		return;
	st.timesync_stopped = FALSE;
	if (timesync_enabled())
		timesync_call("StartUnit");
}

static gboolean aod_shown(void)
{
	glob_t g;
	gboolean shown = glob(AOD_FLAG, GLOB_NOSORT, NULL, &g) == 0 && g.gl_pathc > 0;
	globfree(&g);
	return shown;
}

// An alarm timer that wakes the phone from suspend shortly after the next full minute, or -1.
// It stays armed while the fd is open; closing it after the wake-up removes it.
static int arm_minute_alarm(void)
{
	int fd = timerfd_create(CLOCK_REALTIME_ALARM, TFD_CLOEXEC);
	if (fd < 0) {
		g_warning("alarm timer: %s", g_strerror(errno));
		return -1;
	}
	struct timespec now;
	clock_gettime(CLOCK_REALTIME, &now);
	struct itimerspec when = {
		.it_value = {
			.tv_sec = (now.tv_sec / 60 + 1) * 60,
			.tv_nsec = AOD_WAKE_AFTER_MINUTE_MS * 1000000L,
		},
	};
	if (timerfd_settime(fd, TFD_TIMER_ABSTIME, &when, NULL) < 0) {
		g_warning("alarm timer: %s", g_strerror(errno));
		close(fd);
		return -1;
	}
	return fd;
}

static gboolean write_file(const char *path, const char *text, int *error)
{
	int fd = open(path, O_WRONLY | O_CLOEXEC);
	if (fd < 0) {
		*error = errno;
		return FALSE;
	}
	gboolean ok = write(fd, text, strlen(text)) == (ssize_t)strlen(text);
	*error = ok ? 0 : errno;
	close(fd);
	return ok;
}

static double clock_seconds(clockid_t id)
{
	struct timespec ts;
	clock_gettime(id, &ts);
	return ts.tv_sec + ts.tv_nsec / 1e9;
}

// Worker thread: the wakeup_count read blocks while a wakeup event is in progress, and the write
// of "mem" blocks for the whole suspend
static void attempt_thread(GTask *task, gpointer source, gpointer data, GCancellable *cancellable)
{
	AttemptResult *res = g_new0(AttemptResult, 1);
	char count[32];
	int error;

	g_atomic_int_set(&st.reading, 1);
	gboolean have_count = read_line(WAKEUP_COUNT, count, sizeof(count));
	g_atomic_int_set(&st.reading, 0);
	if (!have_count) {
		res->result = ATTEMPT_REFUSED;	// interrupted read
	} else if (g_atomic_int_get(&st.cancel) || !display_is_off()) {
		res->result = ATTEMPT_CANCELLED;
	} else if (!write_file(WAKEUP_COUNT, count, &error)) {
		// EINVAL: an event came in since the read
		res->result = ATTEMPT_REFUSED;
	} else if (g_atomic_int_get(&st.cancel) || !display_is_off()) {
		res->result = ATTEMPT_CANCELLED;
	} else {
		int alarm = aod_shown() ? arm_minute_alarm() : -1;
		double boot0 = clock_seconds(CLOCK_BOOTTIME), mono0 = clock_seconds(CLOCK_MONOTONIC);
		gboolean slept = write_file(POWER_STATE, "mem", &error);
		if (alarm >= 0)
			close(alarm);
		if (slept) {
			res->result = ATTEMPT_SLEPT;
			res->slept = (clock_seconds(CLOCK_BOOTTIME) - boot0) -
				     (clock_seconds(CLOCK_MONOTONIC) - mono0);
		} else if (error == EBUSY || error == EAGAIN) {
			// A wakeup source became active during the suspend preparation
			res->result = ATTEMPT_REFUSED;
		} else {
			res->result = ATTEMPT_FAILED;
			res->error = error;
		}
	}
	g_task_return_pointer(task, res, g_free);
}

static void schedule(unsigned delay_ms);

// The wakeup_count read blocks while a wakeup event is in progress (a driver holds its source,
// e.g. Bluetooth audio): name the sources once it has blocked log_after_ms
static gboolean on_slow_attempt(gpointer data)
{
	st.slow_watch = 0;
	if (st.busy && g_atomic_int_get(&st.reading)) {
		g_autofree char *sources = active_wakeup_sources();
		g_message("wakeup events in progress for %.0f s, active wakeup sources: %s",
			  st.log_after_ms / 1000.0, sources);
	}
	return G_SOURCE_REMOVE;
}

static void on_attempt_done(GObject *source, GAsyncResult *result, gpointer data)
{
	g_autofree AttemptResult *res = g_task_propagate_pointer(G_TASK(result), NULL);
	st.busy = FALSE;
	if (st.slow_watch) {
		g_source_remove(st.slow_watch);
		st.slow_watch = 0;
	}

	switch (res->result) {
	case ATTEMPT_SLEPT: {
		char reason[128] = "unknown";
		read_line(RESUME_REASON, reason, sizeof(reason));
		g_strdelimit(reason, "\n", ' ');
		st.suspends++;
		st.slept += res->slept;
		st.retry_ms = RETRY_MIN_MS;
		st.refusals = 0;
		g_message("slept %.1f s, woken by %s", res->slept, reason);
		schedule(st.awake_delay_ms);
		break;
	}
	case ATTEMPT_REFUSED:
		if (++st.refusals == REFUSALS_LOGGED) {
			g_autofree char *sources = active_wakeup_sources();
			g_message("suspend refused %u times in a row, active wakeup sources: %s", st.refusals,
				  sources);
		}
		schedule(st.retry_ms);
		st.retry_ms = MIN(st.retry_ms * 2, RETRY_MAX_MS);
		break;
	case ATTEMPT_FAILED:
		g_warning("suspend failed: %s", g_strerror(res->error));
		schedule(RETRY_MAX_MS);
		break;
	case ATTEMPT_CANCELLED:
		// The display came on, or went off again while the worker still ran
		if (st.display_off)
			schedule(st.retry_ms);
		break;
	}
}

static gboolean on_timer(gpointer data)
{
	st.timer = 0;
	if (!st.display_off || st.busy)
		return G_SOURCE_REMOVE;
	if (!display_is_off()) {
		// The signal said off, the connector still says on: try again shortly
		set_blocker(g_strdup("the display connector is still on"));
		schedule(RETRY_MIN_MS);
	} else if (usb_host_connected()) {
		set_blocker(g_strdup("a USB host is connected"));
		schedule(BLOCKED_POLL_MS);
	} else if (sleep_inhibited()) {
		set_blocker(sleep_inhibitors());
		schedule(BLOCKED_POLL_MS);
	} else {
		set_blocker(NULL);
		timesync_stop();
		st.busy = TRUE;
		g_atomic_int_set(&st.cancel, 0);
		st.slow_watch = g_timeout_add(st.log_after_ms, on_slow_attempt, NULL);
		g_autoptr(GTask) task = g_task_new(NULL, NULL, on_attempt_done, NULL);
		g_task_run_in_thread(task, attempt_thread);
	}
	return G_SOURCE_REMOVE;
}

static void schedule(unsigned delay_ms)
{
	if (st.timer)
		g_source_remove(st.timer);
	st.timer = st.display_off ? g_timeout_add(delay_ms, on_timer, NULL) : 0;
}

static void set_display(gboolean off)
{
	if (off == st.display_off)
		return;
	st.display_off = off;
	if (off) {
		st.suspends = 0;
		st.slept = 0;
		st.off_since = g_get_monotonic_time();
		st.retry_ms = RETRY_MIN_MS;
		st.refusals = 0;
		schedule(st.off_delay_ms);
	} else {
		g_atomic_int_set(&st.cancel, 1);
		schedule(0);
		set_blocker(NULL);
		timesync_start();
		// Monotonic time stops in suspend: awake time plus slept time is the whole period
		double awake = (g_get_monotonic_time() - st.off_since) / 1e6;
		if (st.suspends)
			g_message("display on: %u suspends, asleep %.0f of %.0f s", st.suspends, st.slept,
				  st.slept + awake);
	}
}

static void on_display_signal(GDBusConnection *bus, const char *sender, const char *path, const char *iface,
			      const char *signal, GVariant *params, gpointer data)
{
	const char *state;
	g_variant_get(params, "(&s)", &state);
	set_display(strcmp(state, "off") == 0);
}

int main(int argc, char **argv)
{
	int off_delay = 5, awake_delay = 2;
	GOptionEntry entries[] = {
		{ "off-delay", 0, 0, G_OPTION_ARG_INT, &off_delay,
		  "Seconds after the display goes off before the first suspend (5)", "SECONDS" },
		{ "awake-delay", 0, 0, G_OPTION_ARG_INT, &awake_delay,
		  "Seconds to stay awake after each wake-up (2)", "SECONDS" },
		{ NULL }
	};
	g_autoptr(GOptionContext) context = g_option_context_new("- suspend while the display is off");
	g_autoptr(GError) error = NULL;
	g_option_context_add_main_entries(context, entries, NULL);
	if (!g_option_context_parse(context, &argc, &argv, &error) || off_delay < 0 || awake_delay < 0) {
		g_printerr("%s\n", error ? error->message : "delays must not be negative");
		return 2;
	}
	st.off_delay_ms = off_delay * 1000;
	st.awake_delay_ms = awake_delay * 1000;
	st.log_after_ms = LOG_AFTER_MS;

	st.bus = g_bus_get_sync(G_BUS_TYPE_SYSTEM, NULL, &error);
	if (!st.bus)
		g_error("system bus: %s", error->message);
	g_dbus_connection_signal_subscribe(st.bus, MCE_SERVICE, MCE_SIGNAL_IF, "display_status_ind",
					   MCE_SIGNAL_PATH, NULL, G_DBUS_SIGNAL_FLAGS_NONE, on_display_signal,
					   NULL, NULL);
	// Start state from plinius-mce, as the signal would give it; without it from the connector
	g_autoptr(GVariant) reply = g_dbus_connection_call_sync(st.bus, MCE_SERVICE, MCE_REQUEST_PATH,
		MCE_REQUEST_IF, "get_display_status", NULL, G_VARIANT_TYPE("(s)"), G_DBUS_CALL_FLAGS_NONE,
		2000, NULL, NULL);
	const char *state = NULL;
	if (reply)
		g_variant_get(reply, "(&s)", &state);
	set_display(state ? strcmp(state, "off") == 0 : display_is_off());
	// A previous run may have stopped it in a dark phase and ended before the display came on
	if (!st.display_off) {
		st.timesync_stopped = TRUE;
		timesync_start();
	}

	g_main_loop_run(g_main_loop_new(NULL, FALSE));
	return 0;
}
