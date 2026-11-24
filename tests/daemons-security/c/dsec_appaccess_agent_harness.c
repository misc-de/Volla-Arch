/*
 * Harness for plinius-appaccess-agent.c (its main() renamed): which AccessDenied signals of
 * plinius-appaccessd become a question, Phosh's access dialog it asks with or the notification
 * instead (title, buttons for an administrator or another user, the app it names) and what each
 * answer calls on the daemon and the camera portal's permission store. Both buses are fakes:
 * Notify answers with a scripted id, AccessDialog as scripted with "dialog", every other call is
 * recorded ("log"), the answer to it comes at once ("answer ok|fail"). getuid/getgid/getgroups and the group wheel (dsec_fakebus.h) are
 * scripted; the desktop files come from XDG_DATA_DIRS in the test directory.
 *
 * "main" as the first argument runs the agent's main(): AA_BUSES=ok|nosession|nosystem|real,
 * AA_TERM_MS sends SIGTERM that many ms into the main loop. With "real" both buses are the
 * ones of DBUS_SESSION_BUS_ADDRESS and DBUS_SYSTEM_BUS_ADDRESS (private buses of the test):
 * every call and subscription on them is GDBus's own. The monitor connection is always real; in
 * the command loop the messages it would see are made with "msg" and handed to its filter.
 *
 * Commands on stdin, one output line each:
 *   bus KEY VALUE                  see dsec_fakebus.h (bus wheel GID)
 *   ids UID GID GROUPS             the process's ids; GROUPS comma-separated, "none" none,
 *                                  "fail" getgroups() fails
 *   notify ID | notify fail        what the next Notify answers
 *   dialog fail|wait               AccessDialog fails at once (no Phosh, the default) or waits
 *   choose ANSWER                  the oldest waiting dialog answers: once, always, never,
 *                                  cancel (plinius-choice), granted / denied (response 0 / 1
 *                                  without plinius-choice, a Phosh without the three buttons),
 *                                  error (no reply)
 *   dialogs                        the dialog's question and those waiting for it:
 *                                  "APP RES | wait APP RES ..." ("-" none)
 *   answer ok|fail                 how the daemon and the permission store answer
 *   denied APP RES POLICY UID      the daemon's AccessDenied signal
 *   signal NAME GVARIANT           any signal from the daemon's interface
 *   action ID KEY / closed ID      ActionInvoked / NotificationClosed of the notification server
 *   notifysig NAME GVARIANT        any signal of the notification server
 *   questions                      the open questions, "ID APP RES" sorted ("-" none)
 *   log                            the calls made since the last log, " | " between ("-" none)
 *   title RES NAME / icon RES / admin / name APP    title_for, icon_for, is_admin, app_name
 *   msg call|signal|return SENDER DEST PATH IFACE MEMBER BODY   a message as the monitor
 *                                  connection gets it ("-": no such field, BODY a GVariant),
 *                                  handed to on_monitored: "kept" / "dropped"
 *   outgoing                       the same filter for a message the agent sends
 *   pid N|fail|defer               how GetConnectionUnixProcessID answers (defer: not before
 *                                  "flush", with the N or fail current then)
 *   flush                          the deferred answers; from then on they come at once again
 *   requests                       the requests the agent waits for, oldest first:
 *                                  "SENDER PATH|PREFIX* pid=N looking=L refused=R" ("-" none)
 *   clock +S                       the monotonic clock S seconds later
 *   monitor                        monitor_start(): "monitor yes|no"
 *   mic start                      mic_start(): "mic yes|no" (whether the file is watched)
 *   mic read                       mic_reports_read() as after a change of the file
 *   mic event N                    the file monitor's "changed" with event N (GFileMonitorEvent)
 *   file PATH TEXT                 writes TEXT (C escapes: \n) to PATH below the test directory
 *                                  (its directories made)
 */
#define _GNU_SOURCE
#include <gio/gdesktopappinfo.h>
#include <gio/gio.h>
#include <glib-unix.h>
#include <grp.h>
#include <errno.h>
#include <libintl.h>
#include <locale.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/* The real calls, for the real buses of AA_BUSES=real (dsec_fakebus.h redefines their names) */
static __typeof__ (g_dbus_connection_call_sync) *const real_call_sync = g_dbus_connection_call_sync;
static __typeof__ (g_dbus_connection_call) *const real_call = g_dbus_connection_call;
static __typeof__ (g_dbus_connection_call_finish) *const real_call_finish = g_dbus_connection_call_finish;
static __typeof__ (g_bus_get_sync) *const real_bus_get_sync = g_bus_get_sync;
static __typeof__ (g_dbus_connection_signal_subscribe) *const real_signal_subscribe =
    g_dbus_connection_signal_subscribe;

#include "dsec_fakebus.h"

static int system_bus_marker, session_bus_marker;
static uid_t fake_uid = 1000;
static gid_t fake_gid = 1000;
static char fake_groups[128] = "1000,998";
static gint64 notify_id = 7;
static gboolean answer_ok = TRUE;
static GString *calls;
static gint64 pid_answer = 4242;   /* -1: an error */
static gboolean pid_defer;
static gint64 clock_offset;        /* microseconds */
static gboolean dialog_wait;
static const char *dialog_answer = "error";

typedef struct {
    GAsyncReadyCallback callback;
    gpointer user_data;
} Deferred;
static GQueue deferred = G_QUEUE_INIT;
static GQueue dialogs = G_QUEUE_INIT;
/* The method whose answer agent_call_finish gives */
static const char *answering = "";

static gint64
fake_monotonic_time (void)
{
    return g_get_monotonic_time () + clock_offset;
}

static void
log_call (const char *fmt, ...)
{
    va_list args;
    g_autofree char *text = NULL;

    va_start (args, fmt);
    text = g_strdup_vprintf (fmt, args);
    va_end (args);
    if (!calls)
        calls = g_string_new (NULL);
    g_string_append_printf (calls, "%s%s", calls->len ? " | " : "", text);
}

static uid_t
fake_getuid (void)
{
    return fake_uid;
}

static gid_t
fake_getgid (void)
{
    return fake_gid;
}

static int
fake_getgroups (int size, gid_t list[])
{
    g_auto (GStrv) ids = NULL;
    int n = 0;

    if (strcmp (fake_groups, "fail") == 0) {
        errno = EPERM;
        return -1;
    }
    ids = g_strsplit (fake_groups, ",", -1);
    for (char **id = ids; *id; id++) {
        if (!**id || strcmp (*id, "none") == 0)
            continue;
        if (size > 0) {
            if (n >= size) {
                errno = EINVAL;
                return -1;
            }
            list[n] = (gid_t) g_ascii_strtoull (*id, NULL, 10);
        }
        n++;
    }
    return n;
}

static gboolean
is_fake (GDBusConnection *bus)
{
    return bus == (GDBusConnection *) &system_bus_marker || bus == (GDBusConnection *) &session_bus_marker;
}

static const char *
bus_name (GDBusConnection *bus)
{
    return bus == (GDBusConnection *) &system_bus_marker    ? "system"
           : bus == (GDBusConnection *) &session_bus_marker ? "session"
                                                            : "?";
}

static GVariant *
agent_call_sync (GDBusConnection *bus, const char *dest, const char *path, const char *iface,
                 const char *method, GVariant *params, const GVariantType *reply_type,
                 GDBusCallFlags flags, int timeout, GCancellable *cancellable, GError **error)
{
    g_autoptr (GVariant) p = NULL;
    g_autofree char *text = NULL;

    if (!is_fake (bus))
        return real_call_sync (bus, dest, path, iface, method, params, reply_type, flags, timeout, cancellable,
                               error);
    p = params ? g_variant_ref_sink (params) : NULL;
    text = p ? g_variant_print (p, FALSE) : g_strdup ("");
    if (strcmp (method, "Notify") == 0) {
        const char *app_name, *icon, *summary, *body;
        g_autofree const char **actions = NULL;
        g_autoptr (GVariant) hints = NULL;
        g_autofree char *hints_text = NULL;
        g_autofree char *actions_text = NULL;
        guint32 replaces;
        gint32 expire;

        g_variant_get (p, "(&su&s&s&s^a&s@a{sv}i)", &app_name, &replaces, &icon, &summary, &body,
                       &actions, &hints, &expire);
        hints_text = g_variant_print (hints, FALSE);
        actions_text = g_strjoinv (",", (char **) actions);
        log_call ("%s %s %s %s Notify app=%s replaces=%u icon=%s title=%s body=%s actions=%s hints=%s "
                  "expire=%d timeout=%d", bus_name (bus), dest, path, iface, app_name, replaces, icon, summary,
                  body, actions_text, hints_text, expire, timeout);
        if (notify_id < 0) {
            g_set_error (error, G_DBUS_ERROR, G_DBUS_ERROR_SERVICE_UNKNOWN, "fake: no notification server");
            return NULL;
        }
        return g_variant_ref_sink (g_variant_new ("(u)", (guint32) notify_id));
    }
    log_call ("%s %s %s %s %s %s", bus_name (bus), dest, path, iface, method, text);
    g_set_error (error, G_DBUS_ERROR, G_DBUS_ERROR_UNKNOWN_METHOD, "fake: %s", text);
    return NULL;
}

static void
agent_call (GDBusConnection *bus, const char *dest, const char *path, const char *iface,
            const char *method, GVariant *params, const GVariantType *reply_type, GDBusCallFlags flags,
            int timeout, GCancellable *cancellable, GAsyncReadyCallback callback, gpointer user_data)
{
    g_autoptr (GVariant) p = NULL;
    g_autofree char *text = NULL;

    if (!is_fake (bus)) {
        real_call (bus, dest, path, iface, method, params, reply_type, flags, timeout, cancellable, callback,
                   user_data);
        return;
    }
    p = params ? g_variant_ref_sink (params) : NULL;
    text = p ? g_variant_print (p, FALSE) : g_strdup ("()");
    log_call ("%s %s %s %s %s %s", bus_name (bus), dest, path, iface, method, text);
    if (strcmp (method, "AccessDialog") == 0 && dialog_wait) {
        Deferred *d = g_new (Deferred, 1);

        d->callback = callback;
        d->user_data = user_data;
        g_queue_push_tail (&dialogs, d);
        return;
    }
    if (strcmp (method, "AccessDialog") == 0)
        dialog_answer = "error";
    if (strcmp (method, "GetConnectionUnixProcessID") == 0 && pid_defer) {
        Deferred *d = g_new (Deferred, 1);

        d->callback = callback;
        d->user_data = user_data;
        g_queue_push_tail (&deferred, d);
        return;
    }
    answering = method;
    if (callback)
        callback (NULL, NULL, user_data);
    answering = "";
}

/* The fake answers come with a NULL source (the real ones with their connection) */
static GVariant *
agent_call_finish (GDBusConnection *bus, GAsyncResult *result, GError **error)
{
    if (bus)
        return real_call_finish (bus, result, error);
    if (strcmp (answering, "AccessDialog") == 0) {
        GVariantDict results;

        if (strcmp (dialog_answer, "error") == 0) {
            g_set_error (error, G_DBUS_ERROR, G_DBUS_ERROR_SERVICE_UNKNOWN, "fake: no dialog");
            return NULL;
        }
        g_variant_dict_init (&results, NULL);
        if (strcmp (dialog_answer, "granted") && strcmp (dialog_answer, "denied"))
            g_variant_dict_insert (&results, "plinius-choice", "s", dialog_answer);
        return g_variant_ref_sink (g_variant_new ("(u@a{sv})",
                                                  strcmp (dialog_answer, "granted") == 0 ||
                                                  strcmp (dialog_answer, "once") == 0 ||
                                                  strcmp (dialog_answer, "always") == 0 ? 0 : 1,
                                                  g_variant_dict_end (&results)));
    }
    if (strcmp (answering, "GetConnectionUnixProcessID") == 0) {
        if (pid_answer >= 0)
            return g_variant_ref_sink (g_variant_new ("(u)", (guint32) pid_answer));
        g_set_error (error, G_DBUS_ERROR, G_DBUS_ERROR_NAME_HAS_NO_OWNER, "fake: no such name");
        return NULL;
    }
    if (!answer_ok) {
        g_set_error (error, G_DBUS_ERROR, G_DBUS_ERROR_ACCESS_DENIED, "fake: Not allowed");
        return NULL;
    }
    if (strcmp (answering, "CountRefused") == 0)
        return g_variant_ref_sink (g_variant_new ("(s)", "org.gnome.Snapshot"));
    return g_variant_ref_sink (g_variant_new ("()"));
}

static void
flush_deferred (void)
{
    Deferred *d;

    while ((d = g_queue_pop_head (&deferred))) {
        answering = "GetConnectionUnixProcessID";
        d->callback (NULL, NULL, d->user_data);
        answering = "";
        g_free (d);
    }
}

static GDBusConnection *
fake_bus_get_sync (GBusType type, GCancellable *cancellable, GError **error)
{
    const char *mode = g_getenv ("AA_BUSES");

    if (type == G_BUS_TYPE_SESSION && g_strcmp0 (mode, "nosession") == 0) {
        g_set_error (error, G_IO_ERROR, G_IO_ERROR_NOT_FOUND, "fake: no session bus");
        return NULL;
    }
    if (g_strcmp0 (mode, "real") == 0)
        return real_bus_get_sync (type, cancellable, error);
    if (type == G_BUS_TYPE_SYSTEM && g_strcmp0 (mode, "nosystem") == 0) {
        g_set_error (error, G_IO_ERROR, G_IO_ERROR_NOT_FOUND, "fake: no system bus");
        return NULL;
    }
    return (GDBusConnection *) (type == G_BUS_TYPE_SYSTEM ? &system_bus_marker : &session_bus_marker);
}

static guint
fake_signal_subscribe (GDBusConnection *bus, const char *sender, const char *iface, const char *member,
                       const char *path, const char *arg0, GDBusSignalFlags flags,
                       GDBusSignalCallback callback, gpointer user_data, GDestroyNotify free_data)
{
    printf ("subscribe %s sender=%s iface=%s member=%s path=%s arg0=%s\n", bus_name (bus), sender, iface,
            member ? member : "*", path, arg0 ? arg0 : "*");
    if (!is_fake (bus))
        return real_signal_subscribe (bus, sender, iface, member, path, arg0, flags, callback, user_data,
                                      free_data);
    return 1;
}

#define getuid fake_getuid
#define getgid fake_getgid
#define getgroups fake_getgroups
#undef g_dbus_connection_call_sync
#define g_dbus_connection_call_sync agent_call_sync
#undef g_dbus_connection_call
#define g_dbus_connection_call agent_call
#define g_dbus_connection_call_finish agent_call_finish
#define g_bus_get_sync fake_bus_get_sync
#define g_dbus_connection_signal_subscribe fake_signal_subscribe
#define g_get_monotonic_time fake_monotonic_time

#define main agent_main
#include "plinius-appaccess-agent.c"
#undef main
#undef g_get_monotonic_time

static gboolean
send_term (gpointer data)
{
    raise (SIGTERM);
    return G_SOURCE_REMOVE;
}

static void
print_questions (void)
{
    g_autoptr (GPtrArray) list = g_ptr_array_new_with_free_func (g_free);
    GHashTableIter iter;
    gpointer k;
    Question *q;

    g_hash_table_iter_init (&iter, self.questions);
    while (g_hash_table_iter_next (&iter, &k, (gpointer *) &q))
        g_ptr_array_add (list, g_strdup_printf ("%u %s %s", GPOINTER_TO_UINT (k), q->app, q->resource));
    g_ptr_array_sort_values (list, (GCompareFunc) strcmp);
    for (guint i = 0; i < list->len; i++)
        printf ("%s%s", i ? " | " : "", (char *) list->pdata[i]);
    puts (list->len ? "" : "-");
}

static void
print_dialogs (void)
{
    if (self.dialog)
        printf ("%s %s", self.dialog->app, self.dialog->resource);
    for (GList *l = self.waiting.head; l; l = l->next) {
        Question *q = l->data;

        printf (" | wait %s %s", q->app, q->resource);
    }
    puts (self.dialog || self.waiting.length ? "" : "-");
}

static void
choose (const char *answer)
{
    Deferred *d = g_queue_pop_head (&dialogs);

    if (!d)
        return;
    dialog_answer = answer;
    answering = "AccessDialog";
    d->callback (NULL, NULL, d->user_data);
    answering = "";
    g_free (d);
}

static void
print_requests (void)
{
    for (guint i = 0; i < self.requests->len; i++) {
        Request *r = self.requests->pdata[i];

        printf ("%s%s %s%s pid=%u looking=%d refused=%d", i ? " | " : "", r->sender, r->path ? r->path : r->prefix,
                r->path ? "" : "*", r->pid, r->looking, r->refused);
    }
    puts (self.requests->len ? "" : "-");
}

static const char *
field (const char *s)
{
    return strcmp (s, "-") ? s : NULL;
}

/* "msg TYPE SENDER DEST PATH IFACE MEMBER BODY" through the monitor's filter */
static void
monitored (const char *spec, gboolean incoming)
{
    g_auto (GStrv) f = g_strsplit (spec, " ", 7);
    g_autoptr (GDBusMessage) msg = NULL;
    GDBusMessage *out;

    if (g_strv_length (f) < 7) {
        puts ("<bad message>");
        return;
    }
    msg = g_dbus_message_new ();
    g_dbus_message_set_message_type (msg, strcmp (f[0], "call") == 0     ? G_DBUS_MESSAGE_TYPE_METHOD_CALL
                                          : strcmp (f[0], "signal") == 0 ? G_DBUS_MESSAGE_TYPE_SIGNAL
                                                                         : G_DBUS_MESSAGE_TYPE_METHOD_RETURN);
    g_dbus_message_set_destination (msg, field (f[2]));
    g_dbus_message_set_path (msg, field (f[3]));
    g_dbus_message_set_interface (msg, field (f[4]));
    g_dbus_message_set_member (msg, field (f[5]));
    g_dbus_message_set_sender (msg, field (f[1]));
    if (field (f[6]))
        g_dbus_message_set_body (msg, g_variant_parse (NULL, f[6], NULL, NULL, NULL));
    /* The filter takes the reference it is given */
    out = on_monitored (NULL, g_object_ref (msg), incoming, NULL);
    puts (out ? "kept" : "dropped");
    if (out)
        g_object_unref (out);
}

static void
aa_signal (const char *name, GVariant *params)
{
    g_autoptr (GVariant) p = g_variant_ref_sink (params);

    on_aa_signal ((GDBusConnection *) &system_bus_marker, AA_NAME, AA_PATH, AA_INTERFACE, name, p, NULL);
}

static void
notify_signal (const char *name, GVariant *params)
{
    g_autoptr (GVariant) p = g_variant_ref_sink (params);

    on_notify_signal ((GDBusConnection *) &session_bus_marker, NOTIFY_NAME, NOTIFY_PATH, NOTIFY_INTERFACE, name,
                      p, NULL);
}

int
main (int argc, char **argv)
{
    static char line[8192];
    g_autofree char *cwd = g_get_current_dir ();
    g_autofree char *locale_dir = g_build_filename (cwd, "locale", NULL);

    setvbuf (stdout, NULL, _IOLBF, 0);
    /* The catalogs of the test directory, never the system's */
    bindtextdomain (GETTEXT_DOMAIN, locale_dir);
    if (argc > 1 && strcmp (argv[1], "main") == 0) {
        int r;

        if (g_getenv ("AA_TERM_MS"))
            g_timeout_add (atoi (g_getenv ("AA_TERM_MS")), send_term, NULL);
        r = agent_main ();
        printf ("exit %d\n", r);
        return 0;
    }
    setlocale (LC_ALL, "");
    textdomain (GETTEXT_DOMAIN);
    self.system = (GDBusConnection *) &system_bus_marker;
    self.session = (GDBusConnection *) &session_bus_marker;
    self.questions = g_hash_table_new_full (NULL, NULL, NULL, (GDestroyNotify) question_free);
    self.requests = g_ptr_array_new_with_free_func ((GDestroyNotify) request_release);

    while (fgets (line, sizeof line, stdin)) {
        static char a[4096], b[4096];
        long long n1, n2;
        gboolean printed = FALSE;

        line[strcspn (line, "\n")] = '\0';
        if (fb_command (line))
            continue;
        if (sscanf (line, "ids %lld %lld %127s", &n1, &n2, a) == 3) {
            fake_uid = (uid_t) n1;
            fake_gid = (gid_t) n2;
            g_strlcpy (fake_groups, a, sizeof fake_groups);
        } else if (!strcmp (line, "dialogs")) {
            print_dialogs ();
            printed = TRUE;
        } else if (sscanf (line, "dialog %15s", a) == 1) {
            dialog_wait = strcmp (a, "wait") == 0;
        } else if (sscanf (line, "choose %15s", a) == 1) {
            static char answer[16];

            g_strlcpy (answer, a, sizeof answer);
            choose (answer);
        } else if (!strcmp (line, "notify fail")) {
            notify_id = -1;
        } else if (sscanf (line, "notify %lld", &n1) == 1) {
            notify_id = n1;
        } else if (sscanf (line, "answer %15s", a) == 1) {
            answer_ok = strcmp (a, "ok") == 0;
        } else if (sscanf (line, "denied %4095s %4095s %lld %lld", a, b, &n1, &n2) == 4) {
            aa_signal ("AccessDenied", g_variant_new ("(ssuu)", a, b, (guint32) n1, (guint32) n2));
        } else if (sscanf (line, "signal %4095s %4095[^\n]", a, b) == 2) {
            aa_signal (a, g_variant_parse (NULL, b, NULL, NULL, NULL));
        } else if (sscanf (line, "action %lld %4095s", &n1, a) == 2) {
            notify_signal ("ActionInvoked", g_variant_new ("(us)", (guint32) n1, a));
        } else if (sscanf (line, "closed %lld", &n1) == 1) {
            notify_signal ("NotificationClosed", g_variant_new ("(uu)", (guint32) n1, 2));
        } else if (sscanf (line, "notifysig %4095s %4095[^\n]", a, b) == 2) {
            notify_signal (a, g_variant_parse (NULL, b, NULL, NULL, NULL));
        } else if (!strncmp (line, "msg ", 4)) {
            monitored (line + 4, TRUE);
            printed = TRUE;
        } else if (!strncmp (line, "outgoing ", 9)) {
            monitored (line + 9, FALSE);
            printed = TRUE;
        } else if (!strcmp (line, "pid fail")) {
            pid_answer = -1;
        } else if (!strcmp (line, "pid defer")) {
            pid_defer = TRUE;
        } else if (sscanf (line, "pid %lld", &n1) == 1) {
            pid_answer = n1;
        } else if (!strcmp (line, "flush")) {
            pid_defer = FALSE;
            flush_deferred ();
        } else if (!strcmp (line, "requests")) {
            print_requests ();
            printed = TRUE;
        } else if (sscanf (line, "clock +%lld", &n1) == 1) {
            clock_offset += n1 * G_USEC_PER_SEC;
        } else if (!strcmp (line, "mic start")) {
            mic_start ();
            printf ("mic %s\n", self.mic_monitor ? "yes" : "no");
            printed = TRUE;
        } else if (sscanf (line, "file %4095s %4095[^\n]", a, b) == 2) {
            g_autofree char *text = g_strcompress (b);
            g_autofree char *dir = g_path_get_dirname (a);

            g_mkdir_with_parents (dir, 0700);
            g_file_set_contents (a, text, -1, NULL);
        } else if (!strcmp (line, "mic read")) {
            mic_reports_read (TRUE);
        } else if (sscanf (line, "mic event %lld", &n1) == 1) {
            on_mic_reports (NULL, NULL, NULL, (GFileMonitorEvent) n1, NULL);
        } else if (!strcmp (line, "monitor")) {
            monitor_start ();
            printf ("monitor %s\n", self.monitor ? "yes" : "no");
            printed = TRUE;
        } else if (!strcmp (line, "questions")) {
            print_questions ();
            printed = TRUE;
        } else if (!strcmp (line, "log")) {
            puts (calls && calls->len ? calls->str : "-");
            if (calls)
                g_string_truncate (calls, 0);
            printed = TRUE;
        } else if (sscanf (line, "title %4095s %4095[^\n]", a, b) == 2) {
            g_autofree char *t = title_for (a, b);

            puts (t);
            printed = TRUE;
        } else if (sscanf (line, "icon %4095s", a) == 1) {
            puts (icon_for (a));
            printed = TRUE;
        } else if (!strcmp (line, "admin")) {
            puts (is_admin () ? "yes" : "no");
            printed = TRUE;
        } else if (sscanf (line, "name %4095s", a) == 1) {
            gboolean has_desktop = FALSE;
            g_autofree char *name = app_name (a, &has_desktop);

            printf ("%s desktop=%d\n", name, has_desktop);
            printed = TRUE;
        } else {
            printf ("<unknown command: %.60s>\n", line);
            printed = TRUE;
        }
        if (!printed)
            puts ("ok");
    }
    return 0;
}
