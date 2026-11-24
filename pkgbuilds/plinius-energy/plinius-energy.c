// SPDX-License-Identifier: MIT
// plinius-energy: estimated battery use per app, for GNOME Settings > Battery > Battery Usage.
//
// The kernel counts for every thread how long it ran at each processor speed
// (/proc/<tid>/time_in_state), and its energy model knows the power of a core at each speed
// (/sys/kernel/debug/energy_model). Time times power is the energy a thread used on the
// processor; the device package calibrates it against the battery (see calibration ()).
// Threads are grouped by their cgroup: systemd starts each app in a scope of its own
// (app-gnome-<id>-<pid>.scope), each service in its unit, the Android container in
// lxc.payload.android/uid_N/pid_N, kernel threads stay in the root cgroup.
//
// A leaf cgroup's cpu.stat also counts threads that ended between two samples; its processor
// time is weighted with the speed mix of its living threads, so short-lived processes count too.
//
// The display state from plinius-mce splits the use into screen on and screen off. Measured
// battery drain (charge counter while discharging) and screen-on time are recorded alongside,
// so the page can compare the estimate with what the battery lost.
//
// Results per hour in RUN_FILE (tmpfs, written at every sample, read by Settings) and
// STATE_FILE (kept over reboots, written every hour and on stop). Line format, tab-separated:
//   <hour since epoch> <key> <mJ screen on> <mJ screen off> <ms screen on> <ms screen off> <name>
// Keys: app/<desktop id>, user/<unit>, system/<unit>, android/<process>, kernel, and the
// records @battery (mJ drained from the battery, ms: time discharging) and @screen (ms on).
// Only the processor is counted: no screen, radio or graphics in the app values.
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <gio/gio.h>
#include <glib-unix.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define EM_DIR "/sys/kernel/debug/energy_model"
/* Facts of the phone from the device package, here the calibration of the energy model */
#define DEVICE_CONF "/usr/share/plinius/device.conf"
#define CGROUP_ROOT "/sys/fs/cgroup"
#define POWER_SUPPLY_DIR "/sys/class/power_supply"
#define RUN_FILE "/run/plinius-energy/usage"
#define STATE_FILE "/var/lib/plinius-energy/usage"
/* Longest process name kept (bytes) */
#define NAME_MAX_LEN 64
/* Who may read RUN_FILE besides root: the phone's users that administer it, among them the
 * one GNOME Settings runs as. The file is a week of use per app, hour by hour; other local
 * accounts (services, nobody) have no business reading it. */
#define READER_GROUP "wheel"

#define KEEP_HOURS (7 * 24)
/* Samples while the screen is on and while it is off; changes of the display state sample too.
 * Timers do not wake the phone from suspend. */
#define INTERVAL_ON_S 60
#define INTERVAL_OFF_S 900

#define MCE_NAME "com.nokia.mce"
#define MCE_REQUEST_PATH "/com/nokia/mce/request"
#define MCE_REQUEST_INTERFACE "com.nokia.mce.request"
#define MCE_SIGNAL_PATH "/com/nokia/mce/signal"
#define MCE_SIGNAL_INTERFACE "com.nokia.mce.signal"

/* ---- Energy model ---- */

typedef struct {
	guint khz;
	double mw;
} PowerState;

typedef struct {
	guint first_cpu;
	GArray *states; /* PowerState, sorted by speed */
} Cluster;

static GPtrArray *clusters;
static double ms_per_tick;

static gint
compare_states (gconstpointer a, gconstpointer b)
{
	const PowerState *x = a, *y = b;

	return x->khz < y->khz ? -1 : x->khz > y->khz;
}

static gboolean
read_number (const char *path, double *value)
{
	g_autofree char *text = NULL;
	char *end;

	if (!g_file_get_contents (path, &text, NULL, NULL))
		return FALSE;
	*value = g_ascii_strtod (text, &end);
	return end != text;
}

/*
 * The energy model knows a core's own power only; the battery also pays for caches, memory
 * and the voltage regulators. The device package can give, per performance domain, what the
 * battery measurably lost per model milliwatt with that domain fully loaded, as "<kHz>:<factor>"
 * points (DEVICE_CONF, [Energy], cpu<N>=1000000:1.7,2500000:2.4) or one factor for all speeds.
 * Between two points the factor is interpolated; without an entry it is 1.
 */
static double
calibration (GKeyFile *conf, guint first_cpu, guint khz)
{
	g_autofree char *key = g_strdup_printf ("cpu%u", first_cpu);
	g_autofree char *value = conf ? g_key_file_get_string (conf, "Energy", key, NULL) : NULL;
	g_auto(GStrv) points = NULL;
	double last_khz = -1, last_factor = 1;

	if (value == NULL)
		return 1;
	points = g_strsplit (value, ",", -1);
	for (char **point = points; *point; point++) {
		char *colon = strchr (*point, ':');
		double at = colon ? g_ascii_strtod (*point, NULL) : 0;
		double factor = g_ascii_strtod (colon ? colon + 1 : *point, NULL);

		if (factor <= 0)
			continue;
		if (khz <= at) {
			if (last_khz < 0)
				return factor;
			return last_factor + (factor - last_factor) * (khz - last_khz) / (at - last_khz);
		}
		last_khz = at;
		last_factor = factor;
	}
	return last_factor;
}

/* One directory per performance domain (cpu0, cpu4, ...) with one ps:<kHz> per speed */
static gboolean
load_energy_model (void)
{
	g_autoptr(GKeyFile) conf = g_key_file_new ();

	g_autoptr(GDir) dir = g_dir_open (EM_DIR, 0, NULL);
	const char *name;

	clusters = g_ptr_array_new ();
	if (dir == NULL)
		return FALSE;
	if (!g_key_file_load_from_file (conf, DEVICE_CONF, G_KEY_FILE_NONE, NULL))
		g_clear_pointer (&conf, g_key_file_unref);
	while ((name = g_dir_read_name (dir)) != NULL) {
		g_autofree char *domain = g_build_filename (EM_DIR, name, NULL);
		g_autoptr(GDir) states = NULL;
		const char *state;
		Cluster *cluster;

		if (!g_str_has_prefix (name, "cpu") || !g_ascii_isdigit (name[3]) ||
		    (states = g_dir_open (domain, 0, NULL)) == NULL)
			continue;
		cluster = g_new0 (Cluster, 1);
		cluster->first_cpu = atoi (name + 3);
		cluster->states = g_array_new (FALSE, FALSE, sizeof (PowerState));
		while ((state = g_dir_read_name (states)) != NULL) {
			g_autofree char *freq = g_build_filename (domain, state, "frequency", NULL);
			g_autofree char *power = g_build_filename (domain, state, "power", NULL);
			double khz, mw;

			if (g_str_has_prefix (state, "ps:") && read_number (freq, &khz) && read_number (power, &mw))
				g_array_append_val (cluster->states, ((PowerState){ (guint)khz, mw }));
		}
		g_array_sort (cluster->states, compare_states);
		for (guint i = 0; i < cluster->states->len; i++) {
			PowerState *ps = &g_array_index (cluster->states, PowerState, i);

			ps->mw *= calibration (conf, cluster->first_cpu, ps->khz);
		}
		if (cluster->states->len > 0)
			g_ptr_array_add (clusters, cluster);
	}
	return clusters->len > 0;
}

static Cluster *
find_cluster (guint cpu)
{
	for (guint i = 0; i < clusters->len; i++) {
		Cluster *cluster = g_ptr_array_index (clusters, i);

		if (cluster->first_cpu == cpu)
			return cluster;
	}
	return NULL;
}

/* Power at @khz: the model's value, between two speeds interpolated */
static double
cluster_power (Cluster *cluster, guint khz)
{
	PowerState *s = (PowerState *)cluster->states->data;
	guint n = cluster->states->len;

	if (khz <= s[0].khz)
		return s[0].mw;
	for (guint i = 1; i < n; i++)
		if (khz <= s[i].khz)
			return s[i - 1].mw + (s[i].mw - s[i - 1].mw) * (khz - s[i - 1].khz) / (s[i].khz - s[i - 1].khz);
	return s[n - 1].mw;
}

/* ---- Threads ---- */

typedef struct {
	double uj; /* processor energy since the thread started, µJ (mW × ms) */
	double ms; /* processor time at a known speed */
	guint generation;
} ThreadState;

static GHashTable *threads; /* tid → ThreadState */
static guint generation;

/* Energy and time of a thread so far from its time_in_state ("cpu<N>" lines, then "<kHz> <ticks>") */
static gboolean
read_thread (const char *tid, double *uj, double *ms)
{
	char path[64], buf[2048];
	Cluster *cluster = NULL;
	ssize_t len;
	int fd;

	g_snprintf (path, sizeof path, "/proc/%s/time_in_state", tid);
	if ((fd = open (path, O_RDONLY | O_CLOEXEC)) < 0)
		return FALSE;
	len = read (fd, buf, sizeof buf - 1);
	close (fd);
	if (len <= 0)
		return FALSE;
	buf[len] = '\0';

	*uj = *ms = 0;
	for (char *line = buf, *next; line && *line; line = next) {
		next = strchr (line, '\n');
		if (next)
			*next++ = '\0';
		if (g_str_has_prefix (line, "cpu")) {
			cluster = find_cluster (atoi (line + 3));
		} else if (cluster) {
			char *end;
			guint64 khz = g_ascii_strtoull (line, &end, 10);
			guint64 ticks = g_ascii_strtoull (end, NULL, 10);
			double t = ticks * ms_per_tick;

			*ms += t;
			*uj += t * cluster_power (cluster, khz);
		}
	}
	return TRUE;
}

/* ---- Names ---- */

/* Process name of @tid: the program, for an interpreter the script it runs, for a kernel thread
 * its name. Tabs and line breaks never get into the file. */
static char *
process_name (const char *tid)
{
	static const char *interpreters[] = { "python", "perl", "sh", "bash", "node", "gjs", "ruby", "lua", NULL };
	g_autofree char *path = g_strdup_printf ("/proc/%s/cmdline", tid);
	g_autofree char *cmdline = NULL;
	g_autofree char *name = NULL;
	gsize len = 0;

	if (g_file_get_contents (path, &cmdline, &len, NULL) && len > 0) {
		const char *arg = cmdline, *end = cmdline + len;

		name = g_path_get_basename (arg);
		for (const char **i = interpreters; *i; i++) {
			if (!g_str_has_prefix (name, *i) || strspn (name + strlen (*i), "0123456789.") != strlen (name + strlen (*i)))
				continue;
			gboolean inline_code = FALSE;

			for (arg += strlen (arg) + 1; arg < end && *arg == '-'; arg += strlen (arg) + 1)
				/* "python3 -c CODE", "perl -e CODE", "node -e CODE": the next argument is
				 * code, not a script; it can hold anything, secrets too. Then the
				 * interpreter is the name. */
				inline_code = inline_code || strcmp (arg, "-c") == 0 || strcmp (arg, "-e") == 0 ||
				              strcmp (arg, "--eval") == 0;
			if (!inline_code && arg < end && *arg) {
				g_free (name);
				name = g_path_get_basename (arg);
			}
			break;
		}
	} else {
		g_free (path);
		path = g_strdup_printf ("/proc/%s/comm", tid);
		if (g_file_get_contents (path, &name, NULL, NULL))
			g_strstrip (name);
	}
	if (name == NULL || *name == '\0')
		return NULL;
	/* A name, not a command line: script names are short, anything longer is cut */
	if (strlen (name) > NAME_MAX_LEN)
		name[g_utf8_find_prev_char (name, name + NAME_MAX_LEN + 1) - name] = '\0';
	return g_strdelimit (g_steal_pointer (&name), "\t\n\r", ' ');
}

/* systemd's \xNN escapes in unit names */
static char *
unescape_unit (const char *text, gsize len)
{
	GString *out = g_string_sized_new (len);

	for (gsize i = 0; i < len; i++) {
		if (text[i] == '\\' && i + 3 < len && text[i + 1] == 'x' && g_ascii_isxdigit (text[i + 2]) &&
		    g_ascii_isxdigit (text[i + 3])) {
			g_string_append_c (out, g_ascii_xdigit_value (text[i + 2]) * 16 + g_ascii_xdigit_value (text[i + 3]));
			i += 3;
		} else {
			g_string_append_c (out, text[i]);
		}
	}
	return g_strdelimit (g_string_free (out, FALSE), "\t\n\r", ' ');
}

/* The part of @text (unescaped unit name) between @prefix and @suffix; for a scope without
 * the "-<pid>" before the suffix */
static char *
between (const char *text, const char *prefix, const char *suffix)
{
	const char *start, *end;

	if (!g_str_has_prefix (text, prefix) || !g_str_has_suffix (text, suffix))
		return NULL;
	start = text + strlen (prefix);
	end = text + strlen (text) - strlen (suffix);
	if (g_str_equal (suffix, ".scope")) {
		const char *digits = end;

		while (digits > start && g_ascii_isdigit (digits[-1]))
			digits--;
		if (digits == end || digits <= start + 1 || digits[-1] != '-')
			return NULL;
		end = digits - 1;
	}
	return end > start ? g_strndup (start, end - start) : NULL;
}

/* D-Bus activated apps: "dbus-:1.2-org.gnome.Settings@3.service" in the slice
 * "app-dbus-:1.2-org.gnome.Settings.slice"; the name after the bus address */
static char *
dbus_app (const char *unit)
{
	g_autofree char *rest = between (unit, "dbus-:", ".service");
	const char *dash, *at;

	if (rest == NULL)
		rest = between (unit, "app-dbus-:", ".slice");
	if (rest == NULL || (dash = strchr (rest, '-')) == NULL || dash[1] == '\0')
		return NULL;
	at = strrchr (dash, '@');
	return at ? (at > dash + 1 ? g_strndup (dash + 1, at - dash - 1) : NULL) : g_strdup (dash + 1);
}

static gboolean
is_unit (const char *name)
{
	static const char *suffixes[] = { ".service", ".scope", ".slice", ".mount", ".socket", ".swap", NULL };

	for (const char **s = suffixes; *s; s++)
		if (g_str_has_suffix (name, *s))
			return TRUE;
	return FALSE;
}

/* What a cgroup is in the list: an app (by desktop id), a user or system unit, an Android
 * process, the kernel. A cgroup a service makes inside its unit (udev's "workers") counts
 * for the unit. */
static char *
cgroup_key (const char *rel, const char *name)
{
	g_auto(GStrv) parts = NULL;
	g_autofree char *unit = NULL;
	char *id;
	int n;

	if (*rel == '\0')
		return g_strdup ("kernel");
	if (g_str_has_prefix (rel, "lxc.payload.") || g_str_has_prefix (rel, "lxc.monitor.")) {
		g_autofree char *last = g_path_get_basename (rel);
		return g_strconcat ("android/", name ? name : last, NULL);
	}
	parts = g_strsplit (rel, "/", -1);
	for (n = g_strv_length (parts) - 1; n > 0 && !is_unit (parts[n]); n--)
		;
	unit = unescape_unit (parts[n], strlen (parts[n]));

	/* Apps the shell starts, Flatpak apps, autostart (xdg-autostart-generator), D-Bus activated */
	if ((id = between (unit, "app-gnome-", ".scope")) || (id = between (unit, "app-flatpak-", ".scope")) ||
	    (id = dbus_app (unit)) || (id = between (unit, "app-", ".scope"))) {
		char *key = g_strconcat ("app/", id, NULL);

		g_free (id);
		return key;
	}
	if (g_str_has_prefix (unit, "app-") && strchr (unit, '@') && g_str_has_suffix (unit, ".service")) {
		g_autofree char *app = g_strndup (unit + 4, strchr (unit, '@') - unit - 4);
		return g_strconcat ("app/", app, NULL);
	}
	if (g_str_has_prefix (rel, "user.slice/")) {
		if (g_str_has_prefix (unit, "session-") && g_str_has_suffix (unit, ".scope"))
			return g_strdup ("user/session.scope");
		if (g_str_equal (unit, "init.scope"))
			return g_strdup ("user/systemd");
		return g_strconcat ("user/", unit, NULL);
	}
	return g_strconcat ("system/", unit, NULL);
}

/* ---- Usage per hour ---- */

typedef struct {
	double on_mj, off_mj, on_ms, off_ms;
	char *name;
	double name_mj; /* the name comes from the largest single sample */
} Usage;

static GHashTable *hours; /* hour (gint64) → (key → Usage) */

static void
usage_free (Usage *usage)
{
	g_free (usage->name);
	g_free (usage);
}

static Usage *
usage_get (gint64 hour, const char *key)
{
	GHashTable *keys = g_hash_table_lookup (hours, &hour);
	Usage *usage;

	if (keys == NULL) {
		gint64 *k = g_new (gint64, 1);

		*k = hour;
		keys = g_hash_table_new_full (g_str_hash, g_str_equal, g_free, (GDestroyNotify)usage_free);
		g_hash_table_insert (hours, k, keys);
	}
	if ((usage = g_hash_table_lookup (keys, key)) == NULL) {
		usage = g_new0 (Usage, 1);
		g_hash_table_insert (keys, g_strdup (key), usage);
	}
	return usage;
}

static void
usage_add (gint64 hour, const char *key, gboolean screen_on, double mj, double ms, const char *name)
{
	Usage *usage = usage_get (hour, key);

	if (screen_on) {
		usage->on_mj += mj;
		usage->on_ms += ms;
	} else {
		usage->off_mj += mj;
		usage->off_ms += ms;
	}
	if (name && (usage->name == NULL || mj > usage->name_mj)) {
		g_free (usage->name);
		usage->name = g_strdup (name);
		usage->name_mj = mj;
	}
}

static gint
compare_hours (gconstpointer a, gconstpointer b)
{
	gint64 x = *(const gint64 *)a, y = *(const gint64 *)b;

	return x < y ? -1 : x > y;
}

static gboolean
write_usage (const char *path, GError **error)
{
	g_autoptr(GString) out = g_string_new ("# plinius-energy 1: hour key mJ-on mJ-off ms-on ms-off name\n");
	g_autoptr(GList) list = g_list_sort (g_hash_table_get_keys (hours), compare_hours);

	for (GList *l = list; l; l = l->next) {
		GHashTableIter iter;
		const char *key;
		Usage *u;

		g_hash_table_iter_init (&iter, g_hash_table_lookup (hours, l->data));
		while (g_hash_table_iter_next (&iter, (gpointer *)&key, (gpointer *)&u)) {
			char a[G_ASCII_DTOSTR_BUF_SIZE], b[G_ASCII_DTOSTR_BUF_SIZE];

			/* Locale-independent decimals; whole milliseconds are enough */
			g_ascii_formatd (a, sizeof a, "%.1f", u->on_mj);
			g_ascii_formatd (b, sizeof b, "%.1f", u->off_mj);
			g_string_append_printf (out, "%" G_GINT64_FORMAT "\t%s\t%s\t%s\t%.0f\t%.0f\t%s\n", *(gint64 *)l->data,
			                        key, a, b, u->on_ms, u->off_ms, u->name ? u->name : "");
		}
	}
	/* RUN_FILE for Settings (0640, group READER_GROUP), STATE_FILE for this service only */
	gboolean for_settings = g_strcmp0 (path, RUN_FILE) == 0;
	struct group *readers = for_settings ? getgrnam (READER_GROUP) : NULL;

	mode_t mode = for_settings ? 0640 : 0600;

	if (!g_file_set_contents_full (path, out->str, out->len, G_FILE_SET_CONTENTS_CONSISTENT, mode, error))
		return FALSE;
	/* GLib keeps the mode of a file that is there already (0644 before 0.1-4): set it here */
	if (chmod (path, mode) < 0)
		g_warning ("chmod %s: %s", path, g_strerror (errno));
	if (readers && chown (path, 0, readers->gr_gid) < 0)
		g_warning ("chown %s: %s", path, g_strerror (errno));
	return TRUE;
}

static void
prune_hours (gint64 now_hour)
{
	GHashTableIter iter;
	gint64 *hour;

	g_hash_table_iter_init (&iter, hours);
	while (g_hash_table_iter_next (&iter, (gpointer *)&hour, NULL))
		if (*hour <= now_hour - KEEP_HOURS || *hour > now_hour + 1)
			g_hash_table_iter_remove (&iter);
}

static void
load_usage (const char *path)
{
	g_autofree char *text = NULL;
	g_auto(GStrv) lines = NULL;

	if (!g_file_get_contents (path, &text, NULL, NULL))
		return;
	lines = g_strsplit (text, "\n", -1);
	for (char **line = lines; *line; line++) {
		g_auto(GStrv) f = NULL;
		Usage *u;

		if (**line == '#' || **line == '\0')
			continue;
		f = g_strsplit (*line, "\t", 7);
		if (g_strv_length (f) < 6)
			continue;
		u = usage_get (g_ascii_strtoll (f[0], NULL, 10), f[1]);
		u->on_mj += g_ascii_strtod (f[2], NULL);
		u->off_mj += g_ascii_strtod (f[3], NULL);
		u->on_ms += g_ascii_strtod (f[4], NULL);
		u->off_ms += g_ascii_strtod (f[5], NULL);
		if (f[6] && *f[6] && u->name == NULL)
			u->name = g_strdup (f[6]);
	}
}

/* ---- Sampling ---- */

/* Time since boot including suspend: the battery drains while the phone sleeps too */
static gint64
boottime_us (void)
{
	struct timespec ts;

	clock_gettime (CLOCK_BOOTTIME, &ts);
	return (gint64)ts.tv_sec * G_USEC_PER_SEC + ts.tv_nsec / 1000;
}

typedef struct {
	guint64 usage_us;
	/* The children grew more than the cgroup itself (they are read later): owed, µs */
	gint64 carry_us;
	double mix; /* µJ per ms of its living threads, last known; < 0 unknown */
	char *parent; /* relative path of the parent cgroup */
	char *key, *name; /* what it counted as the last time */
	guint generation;
} CgroupState;

typedef struct {
	char *rel, *key, *name;
	double uj, ms;
	gboolean has_threads, needs_mix;
} Entry;

static GHashTable *cgroups; /* relative path → CgroupState */
static gboolean first_sample = TRUE;
/* At the first sample after boot everything so far counts, after a restart of the service
 * only what comes after */
static gboolean count_since_boot;

static void
cgroup_state_free (CgroupState *cg)
{
	g_free (cg->parent);
	g_free (cg->key);
	g_free (cg->name);
	g_free (cg);
}

static void
entry_free (Entry *e)
{
	g_free (e->rel);
	g_free (e->key);
	g_free (e->name);
	g_free (e);
}

static guint64
read_usage_us (const char *dir)
{
	g_autofree char *path = g_build_filename (dir, "cpu.stat", NULL);
	g_autofree char *text = NULL;
	const char *usage;

	if (!g_file_get_contents (path, &text, NULL, NULL) || (usage = strstr (text, "usage_usec ")) == NULL)
		return 0;
	return g_ascii_strtoull (usage + strlen ("usage_usec "), NULL, 10);
}

/*
 * Counts the cgroup at @dir and below. A cgroup's cpu.stat includes its children, and keeps
 * what ended threads and removed children used; its own share is what it grew minus what the
 * children grew: its own threads, the kernel's interrupts for the root, and the last moments of
 * a child removed since the last sample. That time is weighted with the speed mix of the
 * cgroup's living threads.
 *
 * Returns the cgroup's processor time since the last sample, µs.
 */
static guint64
walk_cgroup (const char *dir, const char *rel, const char *parent, GPtrArray *entries)
{
	g_autofree char *threads_path = g_build_filename (dir, "cgroup.threads", NULL);
	g_autofree char *tids = NULL;
	g_autofree char *top_tid = NULL;
	g_autoptr(GDir) d = NULL;
	CgroupState *cg = g_hash_table_lookup (cgroups, rel);
	gboolean known = cg != NULL;
	double uj = 0, ms = 0, top_uj = -1;
	guint64 usage, delta_us, children_us = 0;
	gint64 own_us;
	const char *child;
	Entry *e;

	if (cg == NULL) {
		cg = g_new0 (CgroupState, 1);
		cg->mix = -1;
		cg->parent = g_strdup (parent);
		g_hash_table_insert (cgroups, g_strdup (rel), cg);
	}
	/* The cgroup's own counter before its children's: they can only have grown more */
	usage = read_usage_us (dir);
	if (!known && first_sample && !count_since_boot)
		cg->usage_us = usage;
	delta_us = usage >= cg->usage_us ? usage - cg->usage_us : 0;
	cg->usage_us = usage;
	cg->generation = generation;

	if (g_file_get_contents (threads_path, &tids, NULL, NULL)) {
		for (char *tid = tids, *next; tid && *tid; tid = next) {
			ThreadState *t;
			double tuj, tms;

			next = strchr (tid, '\n');
			if (next)
				*next++ = '\0';
			if (!read_thread (tid, &tuj, &tms))
				continue;
			t = g_hash_table_lookup (threads, GINT_TO_POINTER (atoi (tid)));
			if (t == NULL) {
				t = g_new0 (ThreadState, 1);
				g_hash_table_insert (threads, GINT_TO_POINTER (atoi (tid)), t);
				/* A thread started since the last sample counts from its start */
				if (first_sample && !count_since_boot) {
					t->uj = tuj;
					t->ms = tms;
				}
			} else if (tms < t->ms) {
				/* The id belongs to a new thread now */
				t->uj = t->ms = 0;
			}
			if (tuj - t->uj > top_uj) {
				top_uj = tuj - t->uj;
				g_free (top_tid);
				top_tid = g_strdup (tid);
			}
			uj += tuj - t->uj;
			ms += tms - t->ms;
			t->uj = tuj;
			t->ms = tms;
			t->generation = generation;
		}
	}

	if ((d = g_dir_open (dir, 0, NULL)) != NULL) {
		while ((child = g_dir_read_name (d)) != NULL) {
			g_autofree char *path = g_build_filename (dir, child, NULL);
			g_autofree char *child_rel = *rel ? g_build_filename (rel, child, NULL) : g_strdup (child);

			if (g_file_test (path, G_FILE_TEST_IS_DIR) && !g_file_test (path, G_FILE_TEST_IS_SYMLINK))
				children_us += walk_cgroup (path, child_rel, rel, entries);
		}
	}

	own_us = (gint64)delta_us - (gint64)children_us + cg->carry_us;
	cg->carry_us = MIN (own_us, 0);
	if (ms > 0)
		cg->mix = uj / ms;
	if (own_us <= 0)
		return delta_us;

	e = g_new0 (Entry, 1);
	e->ms = own_us / 1000.0;
	if (cg->mix >= 0)
		e->uj = e->ms * cg->mix;
	else
		e->needs_mix = TRUE;
	e->has_threads = top_tid != NULL;
	e->name = top_tid ? process_name (top_tid) : g_strdup (cg->name);
	e->key = cgroup_key (rel, e->name);
	e->rel = g_strdup (rel);
	g_free (cg->key);
	cg->key = g_strdup (e->key);
	if (e->name) {
		g_free (cg->name);
		cg->name = g_strdup (e->name);
	}
	g_ptr_array_add (entries, e);
	return delta_us;
}

static struct {
	GMainLoop *loop;
	GDBusConnection *bus;
	gboolean screen_on;
	guint timer;
	gint64 last_boottime;
	gint64 last_saved_hour;
	/* Battery: charge counter (µAh) at the last sample while discharging */
	char *battery_dir;
	double last_charge_uah;
	gboolean last_discharging;
} self = {
	.screen_on = TRUE,
	.last_saved_hour = -1,
};

static void
find_battery (void)
{
	g_autoptr(GDir) dir = g_dir_open (POWER_SUPPLY_DIR, 0, NULL);
	const char *name;

	while (dir && (name = g_dir_read_name (dir)) != NULL) {
		g_autofree char *type_path = g_build_filename (POWER_SUPPLY_DIR, name, "type", NULL);
		g_autofree char *counter = g_build_filename (POWER_SUPPLY_DIR, name, "charge_counter", NULL);
		g_autofree char *type = NULL;

		if (g_file_get_contents (type_path, &type, NULL, NULL) && g_str_has_prefix (type, "Battery") &&
		    g_file_test (counter, G_FILE_TEST_EXISTS)) {
			self.battery_dir = g_build_filename (POWER_SUPPLY_DIR, name, NULL);
			return;
		}
	}
}

/* What the battery lost since the last sample, in mJ, while it was discharging the whole time */
static double
battery_drain_mj (double elapsed_ms, double *discharging_ms)
{
	g_autofree char *status_path = NULL, *counter_path = NULL, *voltage_path = NULL, *status = NULL;
	double charge, voltage, mj = 0;
	gboolean discharging;

	*discharging_ms = 0;
	if (self.battery_dir == NULL)
		return 0;
	status_path = g_build_filename (self.battery_dir, "status", NULL);
	counter_path = g_build_filename (self.battery_dir, "charge_counter", NULL);
	voltage_path = g_build_filename (self.battery_dir, "voltage_now", NULL);
	discharging = g_file_get_contents (status_path, &status, NULL, NULL) && g_str_has_prefix (status, "Discharging");
	if (!read_number (counter_path, &charge) || !read_number (voltage_path, &voltage))
		discharging = FALSE;
	if (discharging && self.last_discharging && charge <= self.last_charge_uah) {
		/* µAh × V × 3.6 = mJ */
		mj = (self.last_charge_uah - charge) * (voltage / 1e6) * 3.6;
		*discharging_ms = elapsed_ms;
	}
	self.last_discharging = discharging;
	self.last_charge_uah = charge;
	return mj;
}

static void
sample (void)
{
	g_autoptr(GPtrArray) entries = g_ptr_array_new_with_free_func ((GDestroyNotify)entry_free);
	g_autoptr(GHashTable) by_rel = g_hash_table_new (g_str_hash, g_str_equal);
	gint64 hour = g_get_real_time () / G_USEC_PER_SEC / 3600;
	gint64 now_us = boottime_us ();
	double elapsed_ms = self.last_boottime ? (now_us - self.last_boottime) / 1000.0 : 0;
	double total_uj = 0, total_ms = 0, mix, drain, discharging_ms;
	GHashTableIter iter;
	gpointer value;
	g_autoptr(GError) error = NULL;

	generation++;
	walk_cgroup (CGROUP_ROOT, "", NULL, entries);
	for (guint i = 0; i < entries->len; i++) {
		Entry *e = g_ptr_array_index (entries, i);

		g_hash_table_insert (by_rel, e->rel, e);
	}

	/* Threads and cgroups gone since the last sample. The last moments of a removed cgroup
	 * are in its parent's share: without threads of its own there, they go to the removed one
	 * (an app closed since the last sample, not the app.slice around it). */
	g_hash_table_iter_init (&iter, threads);
	while (g_hash_table_iter_next (&iter, NULL, &value))
		if (((ThreadState *)value)->generation != generation)
			g_hash_table_iter_remove (&iter);
	g_hash_table_iter_init (&iter, cgroups);
	while (g_hash_table_iter_next (&iter, NULL, &value)) {
		CgroupState *cg = value;
		Entry *parent;

		if (cg->generation == generation)
			continue;
		parent = cg->parent ? g_hash_table_lookup (by_rel, cg->parent) : NULL;
		if (parent && !parent->has_threads && cg->key) {
			g_free (parent->key);
			parent->key = g_strdup (cg->key);
			g_free (parent->name);
			parent->name = g_strdup (cg->name);
			if (cg->mix >= 0) {
				parent->uj = parent->ms * cg->mix;
				parent->needs_mix = FALSE;
			}
		}
		g_hash_table_iter_remove (&iter);
	}

	/* Processor time without a living thread to weigh it: the mix of everything else */
	for (guint i = 0; i < entries->len; i++) {
		Entry *e = g_ptr_array_index (entries, i);

		if (!e->needs_mix) {
			total_uj += e->uj;
			total_ms += e->ms;
		}
	}
	mix = total_ms > 0 ? total_uj / total_ms : 0;
	for (guint i = 0; i < entries->len; i++) {
		Entry *e = g_ptr_array_index (entries, i);

		if (e->needs_mix)
			e->uj = e->ms * mix;
		usage_add (hour, e->key, self.screen_on, e->uj / 1000.0, e->ms, e->name);
	}

	if (elapsed_ms > 0) {
		if (self.screen_on)
			usage_add (hour, "@screen", TRUE, 0, elapsed_ms, NULL);
		drain = battery_drain_mj (elapsed_ms, &discharging_ms);
		if (discharging_ms > 0)
			usage_add (hour, "@battery", self.screen_on, drain, discharging_ms, NULL);
	} else {
		battery_drain_mj (0, &discharging_ms);
	}
	self.last_boottime = now_us;
	first_sample = FALSE;

	prune_hours (hour);
	if (!write_usage (RUN_FILE, &error))
		g_warning ("Writing %s failed: %s", RUN_FILE, error->message);
	if (hour != self.last_saved_hour) {
		g_clear_error (&error);
		if (self.last_saved_hour >= 0 && !write_usage (STATE_FILE, &error))
			g_warning ("Writing %s failed: %s", STATE_FILE, error->message);
		self.last_saved_hour = hour;
	}
}

static gboolean
on_timer (gpointer data)
{
	sample ();
	return G_SOURCE_CONTINUE;
}

static void
schedule (void)
{
	g_clear_handle_id (&self.timer, g_source_remove);
	self.timer = g_timeout_add_seconds (self.screen_on ? INTERVAL_ON_S : INTERVAL_OFF_S, on_timer, NULL);
}

static void
set_display (const char *state)
{
	/* "dimmed" still shows something */
	gboolean on = g_strcmp0 (state, "off") != 0;

	if (on == self.screen_on)
		return;
	/* What came before the change belongs to the old state */
	sample ();
	self.screen_on = on;
	schedule ();
}

static void
on_display_signal (GDBusConnection *bus, const char *sender, const char *path, const char *interface,
                   const char *signal, GVariant *parameters, gpointer data)
{
	const char *state;

	if (g_variant_is_of_type (parameters, G_VARIANT_TYPE ("(s)"))) {
		g_variant_get (parameters, "(&s)", &state);
		set_display (state);
	}
}

static void
query_display (void)
{
	g_autoptr(GVariant) reply = NULL;
	const char *state;

	reply = g_dbus_connection_call_sync (self.bus, MCE_NAME, MCE_REQUEST_PATH, MCE_REQUEST_INTERFACE,
	                                     "get_display_status", NULL, G_VARIANT_TYPE ("(s)"),
	                                     G_DBUS_CALL_FLAGS_NO_AUTO_START, 2000, NULL, NULL);
	if (reply) {
		g_variant_get (reply, "(&s)", &state);
		self.screen_on = g_strcmp0 (state, "off") != 0;
	}
}

static gboolean
on_quit (gpointer data)
{
	g_main_loop_quit (self.loop);
	return G_SOURCE_REMOVE;
}

int
main (int argc, char **argv)
{
	g_autoptr(GError) error = NULL;
	long hz = sysconf (_SC_CLK_TCK);

	ms_per_tick = 1000.0 / (hz > 0 ? hz : 100);
	if (!load_energy_model ()) {
		/* Without the model there is nothing to estimate; a clean stop keeps systemd quiet */
		g_message ("No energy model in %s, nothing to do", EM_DIR);
		return 0;
	}
	count_since_boot = !g_file_test (RUN_FILE, G_FILE_TEST_EXISTS);

	threads = g_hash_table_new_full (g_direct_hash, g_direct_equal, NULL, g_free);
	cgroups = g_hash_table_new_full (g_str_hash, g_str_equal, g_free, (GDestroyNotify)cgroup_state_free);
	hours = g_hash_table_new_full (g_int64_hash, g_int64_equal, g_free, (GDestroyNotify)g_hash_table_unref);
	/* The run file is newer than the state file after a restart of the service */
	load_usage (count_since_boot ? STATE_FILE : RUN_FILE);
	/* Written only every hour: up to 0.1-3 it was world-readable, close it now */
	if (chmod (STATE_FILE, 0600) < 0 && errno != ENOENT)
		g_warning ("chmod %s: %s", STATE_FILE, g_strerror (errno));
	find_battery ();

	self.loop = g_main_loop_new (NULL, FALSE);
	self.bus = g_bus_get_sync (G_BUS_TYPE_SYSTEM, NULL, &error);
	if (self.bus) {
		g_dbus_connection_signal_subscribe (self.bus, MCE_NAME, MCE_SIGNAL_INTERFACE, "display_status_ind",
		                                    MCE_SIGNAL_PATH, NULL, G_DBUS_SIGNAL_FLAGS_NONE, on_display_signal,
		                                    NULL, NULL);
		query_display ();
	} else {
		g_warning ("No system bus, counting everything as screen on: %s", error->message);
	}

	sample ();
	self.last_saved_hour = g_get_real_time () / G_USEC_PER_SEC / 3600;
	schedule ();
	g_unix_signal_add (SIGTERM, on_quit, NULL);
	g_unix_signal_add (SIGINT, on_quit, NULL);
	g_main_loop_run (self.loop);

	sample ();
	g_clear_error (&error);
	if (!write_usage (STATE_FILE, &error))
		g_warning ("Writing %s failed: %s", STATE_FILE, error->message);
	return 0;
}
