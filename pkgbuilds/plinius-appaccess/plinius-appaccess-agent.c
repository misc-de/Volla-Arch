// SPDX-License-Identifier: MIT
// plinius-appaccess-agent: the question in the session when plinius-appaccessd refused an app a
// camera, a security key, the network or the microphone because its policy is "ask". Three buttons: Allow Once
// (until the app is closed), Always Allow, Don't Allow (no more questions). The refused attempt
// itself stays refused: the app has to try again, the body says so. Without an answer nothing
// changes and the next attempt asks again (at most every 20 s).
// The question is Phosh's access dialog, the same as the portals' questions (phosh-plinius 0109:
// org.freedesktop.impl.portal.Access on mobi.phosh.Shell.Portal) with the app's name and icon
// from its desktop file; "plinius-once" keeps Phosh away from the permission store, the button
// comes back in "plinius-choice". One dialog at a time: a question that comes while one is open
// waits for it, the same question again (app and resource) is not asked twice. Without Phosh's
// dialog (not running, or busy with a question of a portal) it is a notification with the
// three buttons instead; it names the app that asked (desktop-entry): Phosh shows its name and
// icon and, on a button, brings that app up again, where the user tries once more.
// "Always Allow" and "Don't Allow" set the app's policy, which holds for every account on the
// phone; plinius-appaccessd takes that only from an administrator (group wheel). In the session
// of any other account the question offers Allow Once and Don't Allow, and Don't Allow only
// closes it: the next attempt asks again (attack review 2026-11-18, L15).
//
// Refusals of the phone's own cameras: those reach the apps through the camera portal
// (xdg-desktop-portal) and PipeWire, which decide themselves, so plinius-appaccessd never sees
// them refused. The agent watches its session bus as a monitor (org.freedesktop.DBus.Monitoring,
// open to every user on their own session bus): an app's AccessCamera call and the Response
// signal of that request, sent to the app on
// /org/freedesktop/portal/desktop/request/<sender, ':' dropped, '.' as '_'>/<handle_token>.
// A response other than 0 (1: the user said no, 2: the permission store said no) is counted
// for the app of the calling process as a refused attempt (CountRefused). Not seen: a native
// app that asks PipeWire directly and gets no camera node from the WirePlumber hook. Without
// the monitor the questions work as before and nothing of the portal is counted.
//
// The microphone: WirePlumber's hook (find-plinius-microphone-access.lua) reports every new
// recording of an app in its state file ~/.local/state/wireplumber/plinius-appaccess-microphone,
// "<serial of the stream> = used|refused <pid> <app>" (pid 0 and app "-" when not known), the
// last few. The agent passes each new one on (CountAttempt): plinius-appaccessd counts it and,
// for a refusal of an app that asks, sends the AccessDenied that brings the question here.
#include <gio/gdesktopappinfo.h>
#include <grp.h>
#include <gio/gio.h>
#include <glib-unix.h>
#include <glib/gi18n.h>
#include <locale.h>
#include <unistd.h>

#define GETTEXT_DOMAIN "plinius-appaccess"

#define AA_NAME "org.plinius.AppAccess1"
#define AA_PATH "/org/plinius/AppAccess1"
#define AA_INTERFACE "org.plinius.AppAccess1"

#define PERM_STORE_NAME "org.freedesktop.impl.portal.PermissionStore"
#define PERM_STORE_PATH "/org/freedesktop/impl/portal/PermissionStore"
#define PERM_STORE_INTERFACE "org.freedesktop.impl.portal.PermissionStore"

#define NOTIFY_NAME "org.freedesktop.Notifications"
#define NOTIFY_PATH "/org/freedesktop/Notifications"
#define NOTIFY_INTERFACE "org.freedesktop.Notifications"

#define DIALOG_NAME "mobi.phosh.Shell.Portal"
#define DIALOG_PATH "/org/freedesktop/portal/desktop"
#define DIALOG_INTERFACE "org.freedesktop.impl.portal.Access"
/* The request handle of each dialog: an object Phosh exports while it is open */
#define DIALOG_HANDLE "/org/plinius/AppAccessAgent/dialog/"

#define DBUS_NAME "org.freedesktop.DBus"
#define DBUS_PATH "/org/freedesktop/DBus"

#define PORTAL_CAMERA_INTERFACE "org.freedesktop.portal.Camera"
#define PORTAL_REQUEST_INTERFACE "org.freedesktop.portal.Request"
#define PORTAL_REQUEST_PREFIX "/org/freedesktop/portal/desktop/request/"
/* A request the portal has not answered by then is forgotten */
#define REQUEST_MAX_AGE (10 * 60 * G_USEC_PER_SEC)

/* WirePlumber's state file of the microphone hook (below the user's state directory), its group */
#define MIC_REPORTS "plinius-appaccess-microphone"

enum { POLICY_ASK = 0, POLICY_ALLOW = 1, POLICY_DENY = 2 };

typedef struct {
	char *app;
	char *resource;
} Question;

/* An app's AccessCamera call the portal has not answered yet (reference counted: GRcBox) */
typedef struct {
	char *sender;     /* the app's unique name */
	char *path;       /* the request's object path, NULL if the app gave no handle_token */
	char *prefix;     /* the object paths of all the app's requests start with it */
	gint64 since;     /* monotonic */
	guint32 pid;      /* the app's process, 0 until the bus said (or if it could not) */
	gboolean looking; /* the bus has not answered yet */
	gboolean refused; /* refused before the bus answered: counted with the answer */
} Request;

/* A message the monitor saw, for the main thread */
typedef struct {
	gboolean call; /* AccessCamera, else a Response signal */
	char *peer;    /* the app: the call's sender, the signal's destination */
	char *path;
	GVariant *body;
} Seen;

static struct {
	GMainLoop *loop;
	GDBusConnection *system;
	GDBusConnection *session;
	GDBusConnection *monitor; /* the session bus once more, as a monitor; NULL without */
	GHashTable *questions;    /* notification id -> Question */
	Question *dialog;         /* the question in Phosh's dialog, NULL while none is open */
	GQueue waiting;           /* Question, asked while the dialog was open */
	guint dialogs;            /* dialogs opened, for their handles */
	GPtrArray *requests;      /* Request, oldest first */
	char *mic_file;           /* the hook's reports */
	GFileMonitor *mic_monitor;
	GHashTable *mic_seen;     /* the reports passed on already (their keys) */
} self;

static Question *question_new(const char *app, const char *resource)
{
	Question *q = g_new0(Question, 1);

	q->app = g_strdup(app);
	q->resource = g_strdup(resource);
	return q;
}

static void question_free(Question *q)
{
	g_free(q->app);
	g_free(q->resource);
	g_free(q);
}

static gboolean question_is(const Question *q, const char *app, const char *resource)
{
	return g_str_equal(q->app, app) && g_str_equal(q->resource, resource);
}

/* The app's name as its desktop file has it, else its id; @has_desktop: there is a desktop file */
static char *app_name(const char *app, gboolean *has_desktop)
{
	g_autofree char *desktop = g_strconcat(app, ".desktop", NULL);
	g_autoptr(GDesktopAppInfo) info = g_desktop_app_info_new(desktop);

	*has_desktop = info != NULL;
	return g_strdup(info ? g_app_info_get_display_name(G_APP_INFO(info)) : app);
}

/* The app's icon as its desktop file names it (a name or a file), NULL without one */
static char *app_icon(const char *app)
{
	g_autofree char *desktop = g_strconcat(app, ".desktop", NULL);
	g_autoptr(GDesktopAppInfo) info = g_desktop_app_info_new(desktop);
	GIcon *icon = info ? g_app_info_get_icon(G_APP_INFO(info)) : NULL;

	return icon ? g_icon_to_string(icon) : NULL;
}

static char *title_for(const char *resource, const char *name)
{
	if (g_str_equal(resource, "camera"))
		/* TRANSLATORS: %s is the name of an app */
		return g_strdup_printf(_("%s wants to use a camera"), name);
	if (g_str_equal(resource, "security-key"))
		/* TRANSLATORS: %s is the name of an app */
		return g_strdup_printf(_("%s wants to use a security key"), name);
	if (g_str_equal(resource, "microphone"))
		/* TRANSLATORS: %s is the name of an app */
		return g_strdup_printf(_("%s wants to use the microphone"), name);
	/* TRANSLATORS: %s is the name of an app */
	return g_strdup_printf(_("%s wants to use the network"), name);
}

static const char *icon_for(const char *resource)
{
	if (g_str_equal(resource, "camera"))
		return "camera-web-symbolic";
	if (g_str_equal(resource, "security-key"))
		return "security-high-symbolic";
	if (g_str_equal(resource, "microphone"))
		return "audio-input-microphone-symbolic";
	return "network-workgroup-symbolic";
}

/* This session's user is an administrator: the process is in the group wheel */
static gboolean is_admin(void)
{
	struct group *wheel = getgrnam("wheel");
	g_autofree gid_t *groups = NULL;
	int n;

	if (!wheel)
		return FALSE;
	if (getgid() == wheel->gr_gid)
		return TRUE;
	n = getgroups(0, NULL);
	if (n <= 0)
		return FALSE;
	groups = g_new(gid_t, n);
	n = getgroups(n, groups);
	for (int i = 0; i < n; i++)
		if (groups[i] == wheel->gr_gid)
			return TRUE;
	return FALSE;
}

#define BODY N_("Access was blocked. If you allow it, try again in the app.")

/* The question as a notification: without Phosh's dialog */
static void notify_ask(const char *app, const char *resource)
{
	const char *const admin_actions[] = {"once", _("Allow Once"), "always", _("Always Allow"), "never",
					     _("Don't Allow"), NULL};
	/* "dismiss": closes the question, sets nothing */
	const char *const user_actions[] = {"once", _("Allow Once"), "dismiss", _("Don't Allow"), NULL};
	const char *const *actions = is_admin() ? admin_actions : user_actions;
	gboolean has_desktop = FALSE;
	g_autofree char *name = app_name(app, &has_desktop);
	g_autofree char *title = title_for(resource, name);
	g_autoptr(GVariant) reply = NULL;
	g_autoptr(GError) error = NULL;
	GHashTableIter iter;
	Question *q;
	guint32 id = 0, replaces = 0;
	GVariantBuilder hints;

	/* The same question still open: show it again instead of a second one */
	gpointer key;

	g_hash_table_iter_init(&iter, self.questions);
	while (g_hash_table_iter_next(&iter, &key, (gpointer *)&q))
		if (question_is(q, app, resource)) {
			replaces = GPOINTER_TO_UINT(key);
			g_hash_table_iter_remove(&iter);
			break;
		}

	g_variant_builder_init(&hints, G_VARIANT_TYPE("a{sv}"));
	g_variant_builder_add(&hints, "{sv}", "urgency", g_variant_new_byte(1));
	g_variant_builder_add(&hints, "{sv}", "category", g_variant_new_string("device"));
	/* Not Settings: a button would open it (at its top level); the app itself is where to go on */
	if (has_desktop)
		g_variant_builder_add(&hints, "{sv}", "desktop-entry", g_variant_new_string(app));

	reply = g_dbus_connection_call_sync(
		self.session, NOTIFY_NAME, NOTIFY_PATH, NOTIFY_INTERFACE, "Notify",
		g_variant_new("(susss^asa{sv}i)", "plinius-appaccess", replaces, icon_for(resource), title,
			      _(BODY), actions, &hints, -1),
		G_VARIANT_TYPE("(u)"), G_DBUS_CALL_FLAGS_NONE, 2000, NULL, &error);
	if (!reply) {
		g_warning("Cannot show the notification: %s", error->message);
		return;
	}
	g_variant_get(reply, "(u)", &id);
	g_hash_table_replace(self.questions, GUINT_TO_POINTER(id), question_new(app, resource));
}

static void on_answered(GObject *source, GAsyncResult *result, gpointer user_data)
{
	g_autoptr(GVariant) reply = NULL;
	g_autoptr(GError) error = NULL;

	reply = g_dbus_connection_call_finish(G_DBUS_CONNECTION(source), result, &error);
	if (!reply)
		g_warning("The answer did not count: %s", error->message);
}

/* The camera portal decides for the phone's own cameras with its own answer per app (permission
 * store, table devices, id camera); "always" and "never" go there too, so that both agree. "Once"
 * stays with plinius-appaccess: the portal asks itself next time. */
static void portal_camera_set(const char *app, gboolean allowed)
{
	const char *const value[] = {allowed ? "yes" : "no", NULL};

	g_dbus_connection_call(self.session, PERM_STORE_NAME, PERM_STORE_PATH, PERM_STORE_INTERFACE, "SetPermission",
			       g_variant_new("(sbss^as)", "devices", TRUE, "camera", app, value), NULL,
			       G_DBUS_CALL_FLAGS_NONE, -1, NULL, on_answered, NULL);
}

/* What an answer does: "once" allows until the app is closed; "always" and "never" set the
 * policy, only for an administrator; anything else ("dismiss", "cancel") nothing */
static void answer(const Question *q, const char *key)
{
	if (g_str_equal(key, "once")) {
		g_dbus_connection_call(self.system, AA_NAME, AA_PATH, AA_INTERFACE, "AllowOnce",
				       g_variant_new("(ss)", q->app, q->resource), NULL, G_DBUS_CALL_FLAGS_NONE, -1,
				       NULL, on_answered, NULL);
	} else if ((g_str_equal(key, "always") || g_str_equal(key, "never")) && is_admin()) {
		g_dbus_connection_call(self.system, AA_NAME, AA_PATH, AA_INTERFACE, "SetPolicy",
				       g_variant_new("(ssu)", q->app, q->resource,
						     g_str_equal(key, "always") ? POLICY_ALLOW : POLICY_DENY),
				       NULL, G_DBUS_CALL_FLAGS_NONE, -1, NULL, on_answered, NULL);
		if (g_str_equal(q->resource, "camera"))
			portal_camera_set(q->app, g_str_equal(key, "always"));
	}
}

static void dialog_open(Question *q);

/* The next question that waited for the dialog */
static void dialog_next(void)
{
	Question *q = g_queue_pop_head(&self.waiting);

	if (q)
		dialog_open(q);
}

static void on_dialog_answered(GObject *source, GAsyncResult *result, gpointer user_data)
{
	Question *q = user_data;
	g_autoptr(GVariant) reply = NULL;
	g_autoptr(GError) error = NULL;

	reply = g_dbus_connection_call_finish(G_DBUS_CONNECTION(source), result, &error);
	self.dialog = NULL;
	if (!reply) {
		g_warning("No dialog from Phosh, a notification instead: %s", error->message);
		notify_ask(q->app, q->resource);
	} else {
		g_autoptr(GVariant) results = NULL;
		const char *key = NULL;
		guint32 response = 2;

		g_variant_get(reply, "(u@a{sv})", &response, &results);
		/* A Phosh without the three buttons: granted is the least, once */
		if (!g_variant_lookup(results, "plinius-choice", "&s", &key))
			key = response == 0 ? "once" : "cancel";
		answer(q, key);
	}
	question_free(q);
	dialog_next();
}

/* The question in Phosh's access dialog */
static void dialog_open(Question *q)
{
	const char *const admin_buttons[] = {"once", "always", "never", NULL};
	/* "never" of another user only closes it (answer()) */
	const char *const user_buttons[] = {"once", "never", NULL};
	gboolean has_desktop = FALSE;
	g_autofree char *name = app_name(q->app, &has_desktop);
	g_autofree char *icon = app_icon(q->app);
	g_autofree char *title = title_for(q->resource, name);
	g_autofree char *handle = g_strdup_printf(DIALOG_HANDLE "%u", ++self.dialogs);
	GVariantBuilder options;

	g_variant_builder_init(&options, G_VARIANT_TYPE_VARDICT);
	g_variant_builder_add(&options, "{sv}", "icon", g_variant_new_string(icon ? icon : icon_for(q->resource)));
	g_variant_builder_add(&options, "{sv}", "plinius-once", g_variant_new_boolean(TRUE));
	g_variant_builder_add(&options, "{sv}", "plinius-buttons",
			      g_variant_new_strv(is_admin() ? admin_buttons : user_buttons, -1));
	self.dialog = q;
	/* No timeout: the dialog stays until it is answered */
	g_dbus_connection_call(self.session, DIALOG_NAME, DIALOG_PATH, DIALOG_INTERFACE, "AccessDialog",
			       g_variant_new("(osssssa{sv})", handle, q->app, "", title, "", _(BODY), &options),
			       G_VARIANT_TYPE("(ua{sv})"), G_DBUS_CALL_FLAGS_NO_AUTO_START, G_MAXINT, NULL,
			       on_dialog_answered, q);
}

static void ask(const char *app, const char *resource)
{
	GHashTableIter iter;
	Question *q;

	/* The same question still open or waiting: once is enough */
	if (self.dialog && question_is(self.dialog, app, resource))
		return;
	for (GList *l = self.waiting.head; l; l = l->next)
		if (question_is(l->data, app, resource))
			return;
	/* Already a notification (Phosh had no dialog then): shown again in its place */
	g_hash_table_iter_init(&iter, self.questions);
	while (g_hash_table_iter_next(&iter, NULL, (gpointer *)&q))
		if (question_is(q, app, resource)) {
			notify_ask(app, resource);
			return;
		}
	q = question_new(app, resource);
	if (self.dialog)
		g_queue_push_tail(&self.waiting, q);
	else
		dialog_open(q);
}

static void on_aa_signal(GDBusConnection *connection, const char *sender, const char *path, const char *iface,
			 const char *signal_name, GVariant *params, gpointer user_data)
{
	const char *app = NULL, *resource = NULL;
	guint32 policy, uid;

	if (!g_str_equal(signal_name, "AccessDenied") || !g_variant_is_of_type(params, G_VARIANT_TYPE("(ssuu)")))
		return;
	g_variant_get(params, "(&s&suu)", &app, &resource, &policy, &uid);
	/* Only the apps of this session, and only what is to be asked */
	if (uid != getuid() || policy != POLICY_ASK)
		return;
	ask(app, resource);
}

static void on_notify_signal(GDBusConnection *connection, const char *sender, const char *path, const char *iface,
			     const char *signal_name, GVariant *params, gpointer user_data)
{
	guint32 id = 0;

	if (g_str_equal(signal_name, "NotificationClosed")) {
		g_variant_get(params, "(uu)", &id, NULL);
		g_hash_table_remove(self.questions, GUINT_TO_POINTER(id));
	} else if (g_str_equal(signal_name, "ActionInvoked")) {
		const char *key = NULL;
		Question *q;

		g_variant_get(params, "(u&s)", &id, &key);
		q = g_hash_table_lookup(self.questions, GUINT_TO_POINTER(id));
		if (!q)
			return;
		answer(q, key);
		g_hash_table_remove(self.questions, GUINT_TO_POINTER(id));
	}
}

/* ----- The microphone: what WirePlumber's hook saw ----- */

static void on_counted(GObject *source, GAsyncResult *result, gpointer user_data);

static void count_attempt(const char *app, guint32 pid, gboolean allowed)
{
	g_dbus_connection_call(self.system, AA_NAME, AA_PATH, AA_INTERFACE, "CountAttempt",
			       g_variant_new("(susb)", app, pid, "microphone", allowed), G_VARIANT_TYPE("(s)"),
			       G_DBUS_CALL_FLAGS_NONE, -1, NULL, on_counted, NULL);
}

/* Passes on the reports not seen yet (@pass: else they only count as seen, at the start) */
static void mic_reports_read(gboolean pass)
{
	g_autoptr(GKeyFile) kf = g_key_file_new();
	g_auto(GStrv) keys = NULL;
	GHashTable *seen = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, NULL);

	if (g_key_file_load_from_file(kf, self.mic_file, G_KEY_FILE_NONE, NULL))
		keys = g_key_file_get_keys(kf, MIC_REPORTS, NULL, NULL);
	for (char **k = keys; k && *k; k++) {
		g_autofree char *value = g_key_file_get_string(kf, MIC_REPORTS, *k, NULL);
		char kind[16], app[256];
		guint pid;

		g_hash_table_add(seen, g_strdup(*k));
		if (!pass || g_hash_table_contains(self.mic_seen, *k) || !value ||
		    sscanf(value, "%15s %u %255s", kind, &pid, app) != 3 ||
		    (!g_str_equal(kind, "used") && !g_str_equal(kind, "refused")))
			continue;
		count_attempt(g_str_equal(app, "-") ? "" : app, pid, g_str_equal(kind, "used"));
	}
	g_hash_table_unref(self.mic_seen);
	self.mic_seen = seen;
}

static void on_mic_reports(GFileMonitor *monitor, GFile *file, GFile *other, GFileMonitorEvent event,
			   gpointer user_data)
{
	if (event != G_FILE_MONITOR_EVENT_DELETED)
		mic_reports_read(TRUE);
}

static void mic_start(void)
{
	g_autoptr(GFile) file = NULL;
	g_autoptr(GError) error = NULL;

	self.mic_file = g_build_filename(g_get_user_state_dir(), "wireplumber", MIC_REPORTS, NULL);
	self.mic_seen = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, NULL);
	/* What was reported before the agent ran is old */
	mic_reports_read(FALSE);
	file = g_file_new_for_path(self.mic_file);
	self.mic_monitor = g_file_monitor_file(file, G_FILE_MONITOR_NONE, NULL, &error);
	if (!self.mic_monitor) {
		g_warning("The microphone's attempts are not seen: %s", error->message);
		return;
	}
	g_signal_connect(self.mic_monitor, "changed", G_CALLBACK(on_mic_reports), NULL);
}

/* ----- Refusals of the phone's own cameras ----- */

static void request_clear(Request *r)
{
	g_free(r->sender);
	g_free(r->path);
	g_free(r->prefix);
}

static void request_release(Request *r)
{
	g_rc_box_release_full(r, (GDestroyNotify)request_clear);
}

static void on_counted(GObject *source, GAsyncResult *result, gpointer user_data)
{
	g_autoptr(GVariant) reply = NULL;
	g_autoptr(GError) error = NULL;

	reply = g_dbus_connection_call_finish(G_DBUS_CONNECTION(source), result, &error);
	if (!reply)
		g_warning("An attempt did not count: %s", error->message);
}

static void count_refused(guint32 pid)
{
	g_dbus_connection_call(self.system, AA_NAME, AA_PATH, AA_INTERFACE, "CountRefused",
			       g_variant_new("(us)", pid, "camera"), G_VARIANT_TYPE("(s)"), G_DBUS_CALL_FLAGS_NONE, -1,
			       NULL, on_counted, NULL);
}

static void on_request_pid(GObject *source, GAsyncResult *result, gpointer user_data)
{
	Request *r = user_data;
	g_autoptr(GVariant) reply = NULL;

	reply = g_dbus_connection_call_finish(G_DBUS_CONNECTION(source), result, NULL);
	r->looking = FALSE;
	/* Without an answer the app is gone already: nothing can be counted for it */
	if (reply)
		g_variant_get(reply, "(u)", &r->pid);
	if (r->refused && r->pid)
		count_refused(r->pid);
	request_release(r);
}

/* Forgets the requests the portal has not answered in time */
static void requests_prune(void)
{
	gint64 now = g_get_monotonic_time();

	for (guint i = self.requests->len; i-- > 0;) {
		Request *r = self.requests->pdata[i];

		if (now - r->since > REQUEST_MAX_AGE)
			g_ptr_array_remove_index(self.requests, i);
	}
}

/* An app called AccessCamera (@options: its a{sv}) */
static void on_access_camera(const char *sender, GVariant *options)
{
	g_autofree char *escaped = g_strdelimit(g_strdup(sender + (sender[0] == ':')), ".", '_');
	const char *token = NULL;
	Request *r;

	requests_prune();
	r = g_rc_box_new0(Request);
	r->sender = g_strdup(sender);
	r->prefix = g_strconcat(PORTAL_REQUEST_PREFIX, escaped, "/", NULL);
	if (g_variant_lookup(options, "handle_token", "&s", &token))
		r->path = g_strconcat(r->prefix, token, NULL);
	r->since = g_get_monotonic_time();
	r->looking = TRUE;
	g_ptr_array_add(self.requests, r);
	/* At once: the app may be gone by the time the portal answers */
	g_dbus_connection_call(self.session, DBUS_NAME, DBUS_PATH, DBUS_NAME, "GetConnectionUnixProcessID",
			       g_variant_new("(s)", sender), G_VARIANT_TYPE("(u)"), G_DBUS_CALL_FLAGS_NONE, -1, NULL,
			       on_request_pid, g_rc_box_acquire(r));
}

/* A Response signal of the portal to @destination on @path (@body: its (ua{sv})) */
static void on_response(const char *destination, const char *path, GVariant *body)
{
	guint32 response = 0;

	requests_prune();
	g_variant_get(body, "(u@a{sv})", &response, NULL);
	for (guint i = 0; i < self.requests->len; i++) {
		Request *r = self.requests->pdata[i];

		if (!g_str_equal(r->sender, destination))
			continue;
		/* Without a handle_token: the first answer to the app after the call */
		if (r->path ? !g_str_equal(r->path, path) : !g_str_has_prefix(path, r->prefix))
			continue;
		if (response != 0) {
			if (r->pid)
				count_refused(r->pid);
			else
				r->refused = TRUE;
		}
		g_ptr_array_remove_index(self.requests, i);
		return;
	}
}

static void seen_free(Seen *s)
{
	g_free(s->peer);
	g_free(s->path);
	g_variant_unref(s->body);
	g_free(s);
}

static gboolean on_seen(gpointer user_data)
{
	Seen *s = user_data;

	if (s->call && g_variant_is_of_type(s->body, G_VARIANT_TYPE("(a{sv})"))) {
		g_autoptr(GVariant) options = g_variant_get_child_value(s->body, 0);

		on_access_camera(s->peer, options);
	} else if (!s->call && g_variant_is_of_type(s->body, G_VARIANT_TYPE("(ua{sv})"))) {
		on_response(s->peer, s->path, s->body);
	}
	return G_SOURCE_REMOVE;
}

/*
 * Every message the monitor connection receives, in GDBus's worker thread: those of the match
 * rules go to the main thread. Method calls and signals are all somebody else's, so GDBus never
 * gets them (it would answer a call, and a monitor must not send anything); the reply to
 * BecomeMonitor passes.
 */
static GDBusMessage *on_monitored(GDBusConnection *connection, GDBusMessage *message, gboolean incoming,
				  gpointer user_data)
{
	GDBusMessageType type = g_dbus_message_get_message_type(message);
	gboolean call = type == G_DBUS_MESSAGE_TYPE_METHOD_CALL;
	const char *iface = g_dbus_message_get_interface(message);
	const char *member = g_dbus_message_get_member(message);
	const char *peer = call ? g_dbus_message_get_sender(message) : g_dbus_message_get_destination(message);
	GVariant *body = g_dbus_message_get_body(message);

	if (!incoming || (!call && type != G_DBUS_MESSAGE_TYPE_SIGNAL))
		return message;
	if (peer && body &&
	    (call ? g_strcmp0(iface, PORTAL_CAMERA_INTERFACE) == 0 && g_strcmp0(member, "AccessCamera") == 0
		  : g_strcmp0(iface, PORTAL_REQUEST_INTERFACE) == 0 && g_strcmp0(member, "Response") == 0)) {
		Seen *s = g_new0(Seen, 1);

		s->call = call;
		s->peer = g_strdup(peer);
		s->path = g_strdup(g_dbus_message_get_path(message));
		s->body = g_variant_ref(body);
		g_main_context_invoke_full(NULL, G_PRIORITY_DEFAULT, on_seen, s, (GDestroyNotify)seen_free);
	}
	g_object_unref(message);
	return NULL;
}

/* The session bus once more, as a monitor of the camera portal's calls and answers */
static void monitor_start(void)
{
	const char *const rules[] = {
		"type='method_call',interface='" PORTAL_CAMERA_INTERFACE "',member='AccessCamera'",
		"type='signal',interface='" PORTAL_REQUEST_INTERFACE "',member='Response'",
		NULL,
	};
	g_autofree char *address = NULL;
	g_autoptr(GVariant) reply = NULL;
	g_autoptr(GError) error = NULL;

	address = g_dbus_address_get_for_bus_sync(G_BUS_TYPE_SESSION, NULL, &error);
	if (address)
		self.monitor = g_dbus_connection_new_for_address_sync(
			address, G_DBUS_CONNECTION_FLAGS_AUTHENTICATION_CLIENT | G_DBUS_CONNECTION_FLAGS_MESSAGE_BUS_CONNECTION,
			NULL, NULL, &error);
	if (self.monitor) {
		/* Before BecomeMonitor: from its reply on, every message is somebody else's */
		g_dbus_connection_add_filter(self.monitor, on_monitored, NULL, NULL);
		reply = g_dbus_connection_call_sync(self.monitor, DBUS_NAME, DBUS_PATH, DBUS_NAME ".Monitoring",
						    "BecomeMonitor", g_variant_new("(^asu)", rules, 0), NULL,
						    G_DBUS_CALL_FLAGS_NONE, 5000, NULL, &error);
	}
	if (!reply) {
		/* Only the counting is missing: the questions do not need the monitor */
		g_warning("Refused cameras are not counted: %s", error->message);
		if (self.monitor)
			g_dbus_connection_close(self.monitor, NULL, NULL, NULL);
		g_clear_object(&self.monitor);
		return;
	}
	g_debug("Counting the refusals of the camera portal");
}

static gboolean on_signal(gpointer user_data)
{
	g_main_loop_quit(self.loop);
	return G_SOURCE_REMOVE;
}

int main(void)
{
	g_autoptr(GError) error = NULL;

	setlocale(LC_ALL, "");
	textdomain(GETTEXT_DOMAIN);
	bind_textdomain_codeset(GETTEXT_DOMAIN, "UTF-8");

	self.session = g_bus_get_sync(G_BUS_TYPE_SESSION, NULL, &error);
	if (!self.session) {
		g_warning("No session bus: %s", error->message);
		return 1;
	}
	self.system = g_bus_get_sync(G_BUS_TYPE_SYSTEM, NULL, &error);
	if (!self.system) {
		g_warning("No system bus: %s", error->message);
		return 1;
	}
	self.questions = g_hash_table_new_full(NULL, NULL, NULL, (GDestroyNotify)question_free);
	self.requests = g_ptr_array_new_with_free_func((GDestroyNotify)request_release);

	g_dbus_connection_signal_subscribe(self.system, AA_NAME, AA_INTERFACE, "AccessDenied", AA_PATH, NULL,
					   G_DBUS_SIGNAL_FLAGS_NONE, on_aa_signal, NULL, NULL);
	g_dbus_connection_signal_subscribe(self.session, NOTIFY_NAME, NOTIFY_INTERFACE, NULL, NOTIFY_PATH, NULL,
					   G_DBUS_SIGNAL_FLAGS_NONE, on_notify_signal, NULL, NULL);
	monitor_start();
	mic_start();

	self.loop = g_main_loop_new(NULL, FALSE);
	g_unix_signal_add(SIGTERM, on_signal, NULL);
	g_unix_signal_add(SIGINT, on_signal, NULL);
	g_main_loop_run(self.loop);
	return 0;
}
