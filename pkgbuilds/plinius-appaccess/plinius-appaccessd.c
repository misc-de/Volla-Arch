/*
 * plinius-appaccessd: per app, whether it may use a camera, a security key, the network and the
 * microphone, and how often it tried; for the app pages of GNOME Settings.
 *
 * - cgroup BPF programs (appaccess.bpf.c) on each session's app.slice refuse or allow each open
 *   of a camera or key and each IPv4/IPv6 socket of an app's process, by the verdict this daemon
 *   writes for the process's cgroup, and count the attempts.
 * - The cgroup names the app: systemd runs each app of a session in a scope or service named
 *   after its application id (app-gnome-org.gnome.Snapshot-1234.scope), below
 *   user@<uid>.service/app.slice. The daemon watches that tree (inotify) and writes a verdict
 *   for every cgroup in it; user services (camera-hal-bridge, portals …) always get ALLOW.
 * - The microphone is PipeWire's: WirePlumber's hook (find-plinius-microphone-access.lua) gives
 *   an app's clients no audio source unless its verdict allows it. The daemon publishes the
 *   verdicts per account in /run/plinius-appaccess/microphone-<uid>.conf (JSON, readable by that
 *   account's group only): every cgroup of the account below app.slice ("allow", "ask", "deny",
 *   "service") and every app it knows (for a Flatpak app that records through pipewire-pulse,
 *   which names the app but not its process). The hook reports the attempts it sees to the agent,
 *   which counts them here (CountAttempt).
 * - Policy per app and resource: ask (refused; plinius-appaccess-agent asks the user), allow or
 *   deny. Defaults: cameras, security keys and the microphone ask, the network is allowed; the
 *   phone's own parts that run as apps and record (mic_own) have the microphone. "Allow once" answers
 *   the question of an app that asks and lasts until the app's running instances have ended
 *   (their cgroups are gone) or a policy is set for it (Don't Allow closes them again).
 * - A cgroup an app makes below its scope belongs to the app (its ancestor below app.slice).
 * - Policies and counts in /var/lib/plinius-appaccess/apps.ini.
 * - D-Bus (system bus) org.plinius.AppAccess1 at /org/plinius/AppAccess1:
 *     Get (s app) -> a(suuuxx): resource, policy, allowed, denied, last allowed, last denied
 *                                (the defaults for an app it does not know, which it does not add)
 *     List () -> a(ssuuuxx): the same for every app with a policy or a count
 *     SetPolicy (s app, s resource, u policy)   policy: 0 ask, 1 allow, 2 deny
 *     AllowOnce (s app, s resource)
 *     Forget (s app)                            policy back to the defaults, counts to 0
 *     CountUse (s app, u pid, s resource) -> s  a use someone else saw: camera-hal-bridge, when a
 *                                               camera stream starts (the phone's own cameras reach
 *                                               the apps through the camera portal and PipeWire,
 *                                               which decide themselves); counted for @app, or for
 *                                               the app of process @pid if @app is empty; returns
 *                                               the app counted ("" for none)
 *     CountAttempt (s app, u pid, s resource, b allowed) -> s   an attempt on the microphone
 *                                               WirePlumber saw (the agent passes it on): for @app
 *                                               if it is an application id (one of its instances
 *                                               must run in the caller's account), else for the app
 *                                               of process @pid; a refusal of an app that asks
 *                                               brings the question (AccessDenied); returns the
 *                                               app counted ("" for none)
 *     CountRefused (u pid, s resource) -> s     a refusal someone else saw: the agent, when the
 *                                               camera portal refused one of the phone's own
 *                                               cameras to the app of process @pid (network and
 *                                               key refusals the programs count themselves, so
 *                                               only "camera"); counted for the app of @pid as
 *                                               a denied attempt, without a question; returns
 *                                               the app counted ("" for none)
 *     signal AccessDenied (s app, s resource, u policy, u uid)   uid: whose session the app runs in
 *     signal Changed (s app)
 *   Get and List: root and regular users (uid >= 1000) with a session at logind. SetPolicy,
 *   AllowOnce and Forget: root, or such a user calling from GNOME Settings or the agent; the
 *   policies are per app for every account on the phone, so SetPolicy and Forget only if that
 *   user is an administrator (group wheel), and AllowOnce only reaches the running instances of
 *   the caller's own account (attack review 2026-11-18, L15).
 *   CountUse: root or camera-hal-bridge. CountRefused and CountAttempt: root, or the agent for an
 *   app that runs in the caller's own account (no account adds refusals to another account's apps).
 *
 * Limits: a native app runs as the user like every other one and can start a scope under any
 * name, so for it the app id is what it says it is: these switches keep honest apps from
 * cameras and keys and show what they tried, they do not stop an app built to get around
 * them. Flatpak apps cannot do that (no access to systemd); for them the portals decide too.
 *
 * /etc/plinius-appaccess/off: start nothing (nothing is refused while the daemon is not running:
 * the programs end with it).
 *
 * SPDX-License-Identifier: MIT
 */

#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <pwd.h>
#include <stdio.h>
#include <string.h>
#include <sys/inotify.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <time.h>
#include <unistd.h>

#include <bpf/bpf.h>
#include <bpf/libbpf.h>
#include <gio/gio.h>
#include <glib-unix.h>

#include "appaccess.h"

#define DEFAULT_BPF   "/usr/lib/plinius-appaccess/appaccess.bpf.o"
#define DEFAULT_STATE "/var/lib/plinius-appaccess/apps.ini"
#define OFF_FILE      "/etc/plinius-appaccess/off"
#define CGROUP_ROOT   "/sys/fs/cgroup"
#define USER_SLICE    CGROUP_ROOT "/user.slice"
#define LOGIN_USERS   "/run/systemd/users"
/* The microphone's verdicts for WirePlumber (RuntimeDirectory of the service) */
#define MIC_DIR       "/run/plinius-appaccess"
#define BUS_NAME      "org.plinius.AppAccess1"
#define OBJECT_PATH   "/org/plinius/AppAccess1"

/* Who may change policies besides root: GNOME Settings and the agent's question */
static const char *const changers[] = {
    "/usr/bin/gnome-control-center",
    "/usr/lib/plinius-appaccess/plinius-appaccess-agent",
    NULL
};
/* Who may count uses it saw: camera-hal-bridge, for the phone's own cameras */
static const char *const counters[] = { "/usr/bin/camera-hal-bridge", NULL };
/* Who may count refusals it saw: the agent, which watches the camera portal in the session */
static const char *const refusers[] = { "/usr/lib/plinius-appaccess/plinius-appaccess-agent", NULL };

#define HARVEST_EVERY 30
#define SAVE_EVERY    60
/* A question for the same app and resource at most this often (seconds) */
#define ASK_REPEAT    20

enum { POLICY_ASK = 0, POLICY_ALLOW = 1, POLICY_DENY = 2, POLICY_MAX = 3 };

static const char *const res_names[AA_RES_MAX] = { "camera", "security-key", "network", "microphone" };
static const char *const policy_names[POLICY_MAX] = { "ask", "allow", "deny" };
static const guint default_policy[AA_RES_MAX] = { POLICY_ASK, POLICY_ASK, POLICY_ALLOW, POLICY_ASK };
/*
 * The phone's own parts that run as apps and record: they have the microphone unless set otherwise.
 * GNOME Settings (Sound: the inputs and their level), the dictation of the keyboard (D-Bus
 * activated, its pw-record in its cgroup), plinius-callfocus (sees which input a recording uses;
 * XDG autostart). Calls, callaudiod (audio-hal-bridge), Phosh, the keyboard and the portals run as
 * services and are no apps.
 */
static const char *const mic_own[] = { "org.gnome.Settings", "org.plinius.Dictate1", "plinius-callfocus", NULL };

typedef struct {
    gboolean set[AA_RES_MAX];  /* policy chosen by the user, else the default */
    guint    policy[AA_RES_MAX];
    guint64  allowed[AA_RES_MAX];
    guint64  denied[AA_RES_MAX];
    gint64   last_allowed[AA_RES_MAX];  /* unix time, 0 never */
    gint64   last_denied[AA_RES_MAX];
    gint64   last_asked[AA_RES_MAX];    /* monotonic seconds */
} App;

typedef struct {
    guint64  id;
    char    *path;     /* below /sys/fs/cgroup */
    char    *app;      /* application id, NULL for a user service or a slice */
    gboolean slice;    /* a slice below app.slice, no unit of its own (app_from_path) */
    guint32  uid;      /* of user@<uid>.service */
    gboolean once[AA_RES_MAX];
} Cgroup;

static struct {
    GMainLoop        *loop;
    struct bpf_object *obj;
    struct ring_buffer *rb;
    int               devices_fd, cgroups_fd, counts_fd;
    GHashTable       *links;       /* app.slice path -> GPtrArray of struct bpf_link */
    GPtrArray        *to_attach;   /* app.slice paths found before the first scan ended */
    gboolean          scanned;
    int               cgroup_fd;   /* /sys/fs/cgroup, for open_by_handle_at */
    int               inotify_fd;
    int               dev_wd;
    GHashTable       *watches;     /* wd -> path */
    GHashTable       *cgroups;     /* id -> Cgroup */
    GHashTable       *by_path;     /* path -> id */
    GHashTable       *apps;        /* app id -> App */
    GHashTable       *devices;     /* dev -> res, as in the map */
    GHashTable       *device_dirs; /* dev -> its directory in /sys/class (for reannouncing) */
    char            **test_devices; /* --test-device MAJ:MIN=resource */
    char             *state_file;
    gboolean          dirty;
    GDBusConnection  *bus;
    GDBusNodeInfo    *introspection;
    guint             mic_idle;      /* the microphone's verdicts are to be published */
    GHashTable       *mic_uids;      /* uid -> TRUE: its file is published */
} aa;

/* ----- Apps ----- */

static int
res_from_name (const char *name)
{
    for (int i = 0; i < AA_RES_MAX; i++)
        if (g_str_equal (name, res_names[i]))
            return i;
    return -1;
}

/* An application id as systemd and the desktop files have it: letters, digits, . _ - */
static gboolean
app_id_valid (const char *app)
{
    size_t n = strlen (app);

    if (n == 0 || n > 255)
        return FALSE;
    for (const char *p = app; *p; p++)
        if (!g_ascii_isalnum (*p) && *p != '.' && *p != '_' && *p != '-')
            return FALSE;
    return TRUE;
}

/* The policy of @app for @res until one is set */
static guint
default_policy_of (const char *app, int res)
{
    if (res == AA_MICROPHONE && g_strv_contains (mic_own, app))
        return POLICY_ALLOW;
    return default_policy[res];
}

static void
app_defaults (App *a, const char *app)
{
    for (int i = 0; i < AA_RES_MAX; i++)
        a->policy[i] = default_policy_of (app, i);
}

static App *
app_get (const char *app)
{
    App *a = g_hash_table_lookup (aa.apps, app);

    if (!a) {
        a = g_new0 (App, 1);
        app_defaults (a, app);
        g_hash_table_insert (aa.apps, g_strdup (app), a);
    }
    return a;
}

/* The policy of @app for @res, without adding the app */
static guint
app_policy (const char *app, int res)
{
    App *a = g_hash_table_lookup (aa.apps, app);

    return a ? a->policy[res] : default_policy_of (app, res);
}

static gint64
monotonic_seconds (void)
{
    return g_get_monotonic_time () / G_USEC_PER_SEC;
}

/* CLOCK_MONOTONIC nanoseconds of the BPF programs to unix time */
static gint64
mono_ns_to_unix (guint64 ns)
{
    struct timespec now;

    if (ns == 0)
        return 0;
    clock_gettime (CLOCK_MONOTONIC, &now);
    return (gint64) time (NULL) - ((gint64) now.tv_sec - (gint64) (ns / 1000000000ULL));
}

/* ----- Cgroups and their verdicts ----- */

/* systemd's \xNN escapes undone; NULL for an escaped NUL (it would cut the name short) */
static char *
unit_unescape (const char *s)
{
    GString *out = g_string_new (NULL);

    while (*s) {
        if (s[0] == '\\' && s[1] == 'x' && g_ascii_isxdigit (s[2]) && g_ascii_isxdigit (s[3])) {
            char c = (char) (g_ascii_xdigit_value (s[2]) << 4 | g_ascii_xdigit_value (s[3]));

            if (c == '\0') {
                g_string_free (out, TRUE);
                return NULL;
            }
            g_string_append_c (out, c);
            s += 4;
        } else {
            g_string_append_c (out, *s++);
        }
    }
    return g_string_free (out, FALSE);
}

/* The application id from its escaped part of a unit name, NULL if it is none */
static char *
app_id_unescaped (const char *escaped)
{
    g_autofree char *id = unit_unescape (escaped);

    return id && app_id_valid (id) ? g_steal_pointer (&id) : NULL;
}

/*
 * The application id of an app's cgroup below app.slice, NULL for anything else (a user service,
 * a slice). As plinius-netlogd: app-[<launcher>-]<ApplicationID>-<RANDOM>.scope,
 * app-[<launcher>-]<ApplicationID>[@<RANDOM>].service, dbus-:<bus id>-<ApplicationID>@<n>.service.
 * The name is split while it is still escaped: systemd writes a dash inside the application id as
 * \x2d, so only a literal dash separates the parts.
 */
static char *
app_from_leaf (const char *escaped)
{
    g_autofree char *leaf = g_strdup (escaped);
    char *id, *p;

    if (g_str_has_prefix (leaf, "dbus-:") && g_str_has_suffix (leaf, ".service")) {
        id = strchr (leaf + 6, '-');
        if (!id)
            return NULL;
        id++;
        if ((p = strchr (id, '@')) || (p = strstr (id, ".service")))
            *p = '\0';
        return app_id_unescaped (id);
    }
    if (!g_str_has_prefix (leaf, "app-"))
        return NULL;
    id = leaf + 4;
    if (g_str_has_suffix (id, ".scope")) {
        id[strlen (id) - 6] = '\0';
        if ((p = strrchr (id, '-')))
            *p = '\0';
    } else if (g_str_has_suffix (id, ".service")) {
        id[strlen (id) - 8] = '\0';
        if ((p = strchr (id, '@')))
            *p = '\0';
    } else {
        return NULL;  /* a slice */
    }
    /* A launcher prefix ("gnome-", "flatpak-") has no dots, an application id usually has */
    p = strchr (id, '-');
    if (p && memchr (id, '.', p - id) == NULL && p[1])
        id = p + 1;
    return app_id_unescaped (id);
}

/* The policy decides; "allow once" only answers the question of an app that asks */
static guint8
verdict_of (const Cgroup *cg, int res)
{
    App *a;

    /* What every app gets as long as it has not been asked, without the question */
    if (cg->slice)
        return default_policy[res] == POLICY_ALLOW ? AA_VERDICT_ALLOW : AA_VERDICT_DENY;
    if (!cg->app)
        return AA_VERDICT_ALLOW;
    a = app_get (cg->app);
    switch (a->policy[res]) {
    case POLICY_ALLOW:
        return AA_VERDICT_ALLOW;
    case POLICY_DENY:
        return AA_VERDICT_DENY;
    default:
        return cg->once[res] ? AA_VERDICT_ALLOW : AA_VERDICT_ASK;
    }
}

static void mic_schedule (void);

static void
cgroup_write (const Cgroup *cg)
{
    struct aa_cgroup value = {};

    for (int i = 0; i < AA_RES_MAX; i++)
        value.verdict[i] = verdict_of (cg, i);
    if (bpf_map_update_elem (aa.cgroups_fd, &cg->id, &value, BPF_ANY) != 0)
        g_warning ("Cannot write the verdict of %s: %s", cg->path, g_strerror (errno));
    mic_schedule ();
}

/* Ends what was allowed once for @res of @app's running instances (their verdicts: app_rewrite) */
static void
once_clear (const char *app, int res)
{
    GHashTableIter iter;
    Cgroup *cg;

    g_hash_table_iter_init (&iter, aa.cgroups);
    while (g_hash_table_iter_next (&iter, NULL, (gpointer *) &cg))
        if (cg->app && g_str_equal (cg->app, app))
            cg->once[res] = FALSE;
}

static void
app_rewrite (const char *app)
{
    GHashTableIter iter;
    Cgroup *cg;

    g_hash_table_iter_init (&iter, aa.cgroups);
    while (g_hash_table_iter_next (&iter, NULL, (gpointer *) &cg))
        if (cg->app && g_str_equal (cg->app, app))
            cgroup_write (cg);
}

/* Adds the attempts the programs counted for cgroup @id (all of them for @id 0) to its app */
static void
harvest (guint64 only)
{
    struct aa_count_key key, next;
    struct aa_count value;
    gboolean first = TRUE;
    GArray *keys = g_array_new (FALSE, FALSE, sizeof (struct aa_count_key));

    while (bpf_map_get_next_key (aa.counts_fd, first ? NULL : &key, &next) == 0) {
        first = FALSE;
        key = next;
        if (only == 0 || key.cgroup_id == only)
            g_array_append_val (keys, key);
    }
    for (guint i = 0; i < keys->len; i++) {
        struct aa_count_key *k = &g_array_index (keys, struct aa_count_key, i);
        Cgroup *cg;
        App *a;

        if (bpf_map_lookup_and_delete_elem (aa.counts_fd, k, &value) != 0 || k->res >= AA_RES_MAX)
            continue;
        cg = g_hash_table_lookup (aa.cgroups, &k->cgroup_id);
        if (!cg || !cg->app)
            continue;
        a = app_get (cg->app);
        a->allowed[k->res] += value.allowed;
        a->denied[k->res] += value.denied;
        if (value.allowed)
            a->last_allowed[k->res] = MAX (a->last_allowed[k->res], mono_ns_to_unix (value.last_allowed));
        if (value.denied)
            a->last_denied[k->res] = MAX (a->last_denied[k->res], mono_ns_to_unix (value.last_denied));
        aa.dirty = TRUE;
    }
    g_array_free (keys, TRUE);
}

static void
cgroup_free (Cgroup *cg)
{
    g_free (cg->path);
    g_free (cg->app);
    g_free (cg);
}

static void watch_add (const char *path);

static void
links_free (GPtrArray *links)
{
    for (guint i = 0; i < links->len; i++)
        bpf_link__destroy (links->pdata[i]);
    g_ptr_array_free (links, TRUE);
}

/* Attaches the programs to a session's app.slice: from then on they run for all its apps */
static void
slice_attach (const char *path)
{
    g_autofree char *full = g_strconcat (CGROUP_ROOT, path, NULL);
    struct bpf_program *prog;
    GPtrArray *links;
    int fd;

    if (g_hash_table_contains (aa.links, path))
        return;
    fd = open (full, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (fd < 0) {
        g_warning ("Cannot open %s: %s", full, g_strerror (errno));
        return;
    }
    links = g_ptr_array_new ();
    bpf_object__for_each_program (prog, aa.obj) {
        struct bpf_link *link = bpf_program__attach_cgroup (prog, fd);

        if (!link)
            g_warning ("Cannot attach %s to %s: %s", bpf_program__name (prog), full, g_strerror (errno));
        else
            g_ptr_array_add (links, link);
    }
    close (fd);
    g_hash_table_insert (aa.links, g_strdup (path), links);
}

/* Level below /sys/fs/cgroup: /user.slice is 1 */
static int
cgroup_level (const char *path)
{
    int level = 0;

    for (const char *p = path; *p; p++)
        if (*p == '/')
            level++;
    return level;
}

/*
 * The application id of a cgroup below app.slice (level AA_APP_SLICE_LEVEL), NULL for a user
 * service. Slices right below app.slice are passed over: systemd puts the instances of a template
 * unit into a slice of their own, so an app started by D-Bus activation runs one level deeper,
 * app.slice/app-dbus\x2d:1.2\x2dorg.gnome.TextEditor.slice/dbus-:1.2-org.gnome.TextEditor@0.service.
 * The first unit that is no slice decides, as in camera-hal-bridge's app_of_pid: a cgroup an app
 * makes below its own unit (Delegate=, the user owns that tree) is still the app's, whatever it
 * is called, and a user service (no app-* or dbus-* unit) stays one.
 *
 * A cgroup that is nothing but slices below app.slice sets @slice. systemd never runs a process
 * in a slice, but the user owns the tree and could move one there; its name does not tell an app
 * from a user service's template (app-gnome\x2dsession\x2dmanager.slice), so it gets neither an
 * app's policy nor a user service's ALLOW: what an app gets before it is asked (verdict_of).
 */
static char *
app_from_path (const char *path, gboolean *slice)
{
    /* "", user.slice, user-<uid>.slice, user@<uid>.service, app.slice, slices, the unit, the rest */
    g_auto(GStrv) parts = g_strsplit (path, "/", -1);
    guint i = AA_APP_SLICE_LEVEL + 1;

    *slice = FALSE;
    if (g_strv_length (parts) < i + 1 || !g_str_equal (parts[AA_APP_SLICE_LEVEL], "app.slice"))
        return NULL;
    while (parts[i] && g_str_has_suffix (parts[i], ".slice"))
        i++;
    if (!parts[i]) {
        *slice = TRUE;
        return NULL;
    }
    return app_from_leaf (parts[i]);
}

/* A cgroup directory (path below /sys/fs/cgroup) appeared or was found by the scan */
static void
cgroup_add (const char *path)
{
    g_autofree char *full = g_strconcat (CGROUP_ROOT, path, NULL);
    g_autofree char *parent = g_strndup (path, strrchr (path, '/') - path);
    const char *leaf = strrchr (path, '/') + 1;
    int level = cgroup_level (path);
    guint64 *parent_id;
    struct stat st;
    Cgroup *cg, *up;

    if (stat (full, &st) != 0 || !S_ISDIR (st.st_mode))
        return;
    /* Watch the whole user.slice tree down to the apps, to see users, sessions and apps come,
     * and one level below them; one more for an app in a slice (D-Bus activation) */
    if (level <= AA_APP_SLICE_LEVEL + 3)
        watch_add (full);
    if (level == AA_APP_SLICE_LEVEL && g_str_equal (leaf, "app.slice")) {
        /* At start, only once every cgroup below has its verdict; a new session has none yet */
        if (aa.scanned) {
            slice_attach (path);
            mic_schedule ();
        } else
            g_ptr_array_add (aa.to_attach, g_strdup (path));
        return;
    }
    if (level <= AA_APP_SLICE_LEVEL || !strstr (path, "/app.slice/"))
        return;
    if (g_hash_table_contains (aa.cgroups, &(guint64) { st.st_ino }))
        return;
    cg = g_new0 (Cgroup, 1);
    cg->id = st.st_ino;
    cg->path = g_strdup (path);
    cg->app = app_from_path (path, &cg->slice);
    if (sscanf (path, "/user.slice/user-%*u.slice/user@%u.service/", &cg->uid) != 1)
        cg->uid = G_MAXUINT32;
    /* A cgroup below a running instance is part of it: what was allowed once holds for it too */
    parent_id = g_hash_table_lookup (aa.by_path, parent);
    up = parent_id ? g_hash_table_lookup (aa.cgroups, parent_id) : NULL;
    if (up && g_strcmp0 (up->app, cg->app) == 0)
        memcpy (cg->once, up->once, sizeof (cg->once));
    g_hash_table_insert (aa.cgroups, &cg->id, cg);
    g_hash_table_insert (aa.by_path, g_strdup (path), g_memdup2 (&cg->id, sizeof (cg->id)));
    cgroup_write (cg);
}

static void
cgroup_remove (const char *path)
{
    guint64 *id = g_hash_table_lookup (aa.by_path, path);
    guint64 key;
    Cgroup *cg;

    if (!id) {
        /* app.slice of a user who logged out */
        if (cgroup_level (path) == AA_APP_SLICE_LEVEL) {
            g_hash_table_remove (aa.links, path);
            mic_schedule ();
            GHashTableIter iter;
            gpointer k;

            g_hash_table_iter_init (&iter, aa.by_path);
            while (g_hash_table_iter_next (&iter, &k, NULL))
                if (g_str_has_prefix (k, path))
                    g_hash_table_iter_remove (&iter);
        }
        return;
    }
    key = *id;
    harvest (key);
    bpf_map_delete_elem (aa.cgroups_fd, &key);
    cg = g_hash_table_lookup (aa.cgroups, &key);
    g_hash_table_remove (aa.by_path, path);
    if (cg)
        g_hash_table_remove (aa.cgroups, &key);
    mic_schedule ();
}

/* The cgroup path of cgroup @id via a kernfs file handle, NULL if it is gone */
static char *
cgroup_path_by_id (guint64 id)
{
    union {
        struct file_handle fh;
        char buf[sizeof (struct file_handle) + sizeof (guint64)];
    } handle;
    char link[64], target[PATH_MAX];
    ssize_t n;
    int fd;

    handle.fh.handle_bytes = sizeof (guint64);
    handle.fh.handle_type = 0xfe;  /* FILEID_KERNFS */
    memcpy (handle.fh.f_handle, &id, sizeof (id));
    fd = open_by_handle_at (aa.cgroup_fd, &handle.fh, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (fd < 0)
        return NULL;
    g_snprintf (link, sizeof (link), "/proc/self/fd/%d", fd);
    n = readlink (link, target, sizeof (target) - 1);
    close (fd);
    if (n <= 0)
        return NULL;
    target[n] = '\0';
    return g_strdup (g_str_has_prefix (target, CGROUP_ROOT) ? target + strlen (CGROUP_ROOT) : target);
}

/* Walks @path (below /sys/fs/cgroup) and everything under it */
static void
cgroup_scan (const char *path)
{
    g_autofree char *full = g_strconcat (CGROUP_ROOT, path, NULL);
    DIR *dir;
    struct dirent *de;

    cgroup_add (path);
    dir = opendir (full);
    if (!dir)
        return;
    while ((de = readdir (dir))) {
        g_autofree char *child = NULL;

        if (de->d_type != DT_DIR || de->d_name[0] == '.')
            continue;
        child = g_strconcat (path, "/", de->d_name, NULL);
        cgroup_scan (child);
    }
    closedir (dir);
}

/* ----- Devices ----- */

static gboolean
on_usb (const char *sysfs_dir)
{
    g_autofree char *link = g_build_filename (sysfs_dir, "device", NULL);
    g_autofree char *real = realpath (link, NULL);

    return real && strstr (real, "/usb") != NULL;
}

static gboolean
read_dev (const char *sysfs_dir, guint32 *dev)
{
    g_autofree char *file = g_build_filename (sysfs_dir, "dev", NULL);
    g_autofree char *text = NULL;
    unsigned int major, minor;

    if (!g_file_get_contents (file, &text, NULL, NULL) || sscanf (text, "%u:%u", &major, &minor) != 2)
        return FALSE;
    *dev = AA_DEV (major, minor);
    return TRUE;
}

/* The report descriptor declares the FIDO Alliance usage page (Usage Page 0xF1D0) */
static gboolean
is_fido (const char *sysfs_dir)
{
    g_autofree char *file = g_build_filename (sysfs_dir, "device", "report_descriptor", NULL);
    g_autofree guint8 *data = NULL;
    gsize len;

    if (!g_file_get_contents (file, (char **) &data, &len, NULL))
        return FALSE;
    for (gsize i = 0; i + 2 < len; i++)
        if (data[i] == 0x06 && data[i + 1] == 0xd0 && data[i + 2] == 0xf1)
            return TRUE;
    return FALSE;
}

static void
scan_class (GHashTable *found, const char *class_dir, int res)
{
    DIR *dir = opendir (class_dir);
    struct dirent *de;

    if (!dir)
        return;
    while ((de = readdir (dir))) {
        g_autofree char *d = NULL;
        guint32 dev;

        if (de->d_name[0] == '.')
            continue;
        d = g_build_filename (class_dir, de->d_name, NULL);
        if (!read_dev (d, &dev))
            continue;
        if (res == AA_CAMERA ? on_usb (d) : is_fido (d)) {
            g_hash_table_insert (found, GUINT_TO_POINTER (dev), GINT_TO_POINTER (res));
            g_hash_table_replace (aa.device_dirs, GUINT_TO_POINTER (dev), g_strdup (d));
        }
    }
    closedir (dir);
}

/* Brings the devices map up to date: cameras on USB, FIDO keys */
static void
devices_scan (void)
{
    g_autoptr(GHashTable) found = g_hash_table_new (NULL, NULL);
    GHashTableIter iter;
    gpointer k, v;

    scan_class (found, "/sys/class/video4linux", AA_CAMERA);
    scan_class (found, "/sys/bus/media/devices", AA_CAMERA);
    scan_class (found, "/sys/class/hidraw", AA_KEY);
    /* Tests: a node without a driver (mknod … c 240 0) behaves like a camera or key: refused it
     * gives EACCES, allowed ENXIO (the LSM hook comes before the driver's open) */
    for (char **t = aa.test_devices; t && *t; t++) {
        unsigned int major, minor;
        char name[32];
        int res;

        if (sscanf (*t, "%u:%u=%31s", &major, &minor, name) == 3 && (res = res_from_name (name)) >= 0 &&
            res != AA_NETWORK)
            g_hash_table_insert (found, GUINT_TO_POINTER (AA_DEV (major, minor)), GINT_TO_POINTER (res));
        else
            g_warning ("Ignoring --test-device %s", *t);
    }

    g_hash_table_iter_init (&iter, found);
    while (g_hash_table_iter_next (&iter, &k, &v)) {
        guint32 dev = GPOINTER_TO_UINT (k), res = GPOINTER_TO_INT (v);

        if (g_hash_table_lookup_extended (aa.devices, k, NULL, NULL))
            continue;
        bpf_map_update_elem (aa.devices_fd, &dev, &res, BPF_ANY);
        g_hash_table_insert (aa.devices, k, v);
    }
    g_hash_table_iter_init (&iter, aa.devices);
    while (g_hash_table_iter_next (&iter, &k, NULL)) {
        guint32 dev = GPOINTER_TO_UINT (k);

        if (g_hash_table_lookup_extended (found, k, NULL, NULL))
            continue;
        bpf_map_delete_elem (aa.devices_fd, &dev);
        g_hash_table_iter_remove (&iter);
    }
}

/*
 * After a key was allowed: announce the keys again ("add" uevent). Firefox (authenticator-rs)
 * opens a key only when it appears; one it could not open stays ignored until it is plugged in
 * again, so without this "Allow" would not help until then. Not for cameras: PipeWire would set
 * them up anew.
 */
static void
keys_reannounce (void)
{
    GHashTableIter iter;
    gpointer k, v;

    g_hash_table_iter_init (&iter, aa.devices);
    while (g_hash_table_iter_next (&iter, &k, &v)) {
        const char *dir = g_hash_table_lookup (aa.device_dirs, k);
        g_autofree char *file = NULL;
        g_autoptr(GError) error = NULL;

        if (GPOINTER_TO_INT (v) != AA_KEY || !dir)
            continue;
        file = g_build_filename (dir, "uevent", NULL);
        if (!g_file_set_contents_full (file, "add", -1, G_FILE_SET_CONTENTS_NONE, 0644, &error))
            g_warning ("Cannot announce %s again: %s", dir, error->message);
    }
}

/* ----- inotify ----- */

static void
watch_add (const char *full)
{
    int wd = inotify_add_watch (aa.inotify_fd, full, IN_CREATE | IN_DELETE | IN_ONLYDIR);

    if (wd < 0) {
        g_warning ("Cannot watch %s: %s", full, g_strerror (errno));
        return;
    }
    g_hash_table_replace (aa.watches, GINT_TO_POINTER (wd), g_strdup (full));
}

static gboolean
on_inotify (int fd, GIOCondition condition, gpointer user_data)
{
    char buf[16384] __attribute__ ((aligned (__alignof__ (struct inotify_event))));
    gboolean devices_changed = FALSE;
    ssize_t n = read (fd, buf, sizeof (buf));

    for (char *p = buf; n > 0 && p < buf + n;) {
        struct inotify_event *ev = (struct inotify_event *) p;
        const char *dir = g_hash_table_lookup (aa.watches, GINT_TO_POINTER (ev->wd));

        p += sizeof (*ev) + ev->len;
        if (ev->mask & IN_IGNORED) {
            g_hash_table_remove (aa.watches, GINT_TO_POINTER (ev->wd));
            continue;
        }
        if (ev->wd == aa.dev_wd) {
            devices_changed = TRUE;
            continue;
        }
        if (!dir || !ev->len || !(ev->mask & IN_ISDIR))
            continue;
        {
            g_autofree char *path = g_strconcat (dir + strlen (CGROUP_ROOT), "/", ev->name, NULL);

            if (ev->mask & IN_CREATE)
                cgroup_scan (path);
            else if (ev->mask & IN_DELETE)
                cgroup_remove (path);
        }
    }
    if (devices_changed)
        devices_scan ();
    return G_SOURCE_CONTINUE;
}

/* ----- Events of the programs ----- */

static void emit_denied (const char *app, int res, guint policy, guint32 uid);

static int
on_event (void *ctx, void *data, size_t size)
{
    const struct aa_event *ev = data;
    Cgroup *cg;

    if (size < sizeof (*ev) || ev->res >= AA_RES_MAX)
        return 0;
    cg = g_hash_table_lookup (aa.cgroups, &ev->cgroup_id);
    if (!cg) {
        /* A cgroup inotify has not reported yet (or the scan missed): add it now */
        g_autofree char *path = cgroup_path_by_id (ev->cgroup_id);

        if (path)
            cgroup_scan (path);
        cg = g_hash_table_lookup (aa.cgroups, &ev->cgroup_id);
        if (!cg)
            return 0;
    }
    if (ev->kind == AA_EVENT_DENIED && ev->verdict == AA_VERDICT_ASK && cg->app)
        emit_denied (cg->app, ev->res, POLICY_ASK, cg->uid);
    return 0;
}

static gboolean
on_ringbuf (int fd, GIOCondition condition, gpointer user_data)
{
    ring_buffer__consume (aa.rb);
    return G_SOURCE_CONTINUE;
}

/* ----- The microphone's verdicts for WirePlumber ----- */

static const char *
verdict_name (guint8 verdict)
{
    return verdict == AA_VERDICT_ALLOW ? "allow" : verdict == AA_VERDICT_ASK ? "ask" : "deny";
}

/* What the hook gives a cgroup's clients: its verdict; "service" for a user service (allowed, and
 * no app whose uses are counted) */
static const char *
mic_cgroup_verdict (const Cgroup *cg)
{
    if (!cg->app && !cg->slice)
        return "service";
    return verdict_name (verdict_of (cg, AA_MICROPHONE));
}

/* An app's verdict in @uid's account, for a client that names its app but not its process (a
 * Flatpak app through pipewire-pulse): allowed once while one of its instances there is */
static const char *
mic_app_verdict (const char *app, guint32 uid)
{
    guint policy = app_policy (app, AA_MICROPHONE);
    GHashTableIter iter;
    Cgroup *cg;

    if (policy != POLICY_ASK)
        return policy == POLICY_ALLOW ? "allow" : "deny";
    g_hash_table_iter_init (&iter, aa.cgroups);
    while (g_hash_table_iter_next (&iter, NULL, (gpointer *) &cg))
        if (cg->uid == uid && cg->app && g_str_equal (cg->app, app) && cg->once[AA_MICROPHONE])
            return "allow";
    return "ask";
}

/* "key": "value" in JSON; NULL for a key that is no UTF-8 (a cgroup of an app's own: the hook
 * takes its parent's verdict then) */
static char *
json_pair (const char *key, const char *value)
{
    GString *out;

    if (!g_utf8_validate (key, -1, NULL))
        return NULL;
    out = g_string_new ("\"");
    for (const unsigned char *p = (const unsigned char *) key; *p; p++) {
        if (*p == '"' || *p == '\\')
            g_string_append_printf (out, "\\%c", *p);
        else if (*p < 0x20)
            g_string_append_printf (out, "\\u%04x", *p);
        else
            g_string_append_c (out, (char) *p);
    }
    g_string_append_printf (out, "\": \"%s\"", value);
    return g_string_free (out, FALSE);
}

static gint
str_compare (gconstpointer a, gconstpointer b)
{
    return strcmp (*(const char *const *) a, *(const char *const *) b);
}

/* "name": { the pairs, sorted } */
static void
json_section (GString *out, const char *name, GPtrArray *pairs, gboolean last)
{
    g_ptr_array_sort (pairs, str_compare);
    g_string_append_printf (out, "  \"%s\": {", name);
    for (guint i = 0; i < pairs->len; i++)
        g_string_append_printf (out, "%s\n    %s", i ? "," : "", (char *) pairs->pdata[i]);
    g_string_append_printf (out, "%s}%s\n", pairs->len ? "\n  " : "", last ? "" : ",");
}

/* The verdicts of @uid's account: every cgroup below its app.slice and every app the daemon knows */
static char *
mic_json (guint32 uid)
{
    g_autoptr(GPtrArray) apps = g_ptr_array_new_with_free_func (g_free);
    g_autoptr(GPtrArray) cgroups = g_ptr_array_new_with_free_func (g_free);
    GString *out = g_string_new (NULL);
    GHashTableIter iter;
    const char *app;
    Cgroup *cg;

    g_hash_table_iter_init (&iter, aa.apps);
    while (g_hash_table_iter_next (&iter, (gpointer *) &app, NULL))
        g_ptr_array_add (apps, json_pair (app, mic_app_verdict (app, uid)));
    for (const char *const *own = mic_own; *own; own++)
        if (!g_hash_table_contains (aa.apps, *own))
            g_ptr_array_add (apps, json_pair (*own, mic_app_verdict (*own, uid)));
    g_hash_table_iter_init (&iter, aa.cgroups);
    while (g_hash_table_iter_next (&iter, NULL, (gpointer *) &cg)) {
        char *pair = cg->uid == uid ? json_pair (cg->path, mic_cgroup_verdict (cg)) : NULL;

        if (pair)
            g_ptr_array_add (cgroups, pair);
    }
    g_string_append_printf (out, "{\n  \"default\": \"%s\",\n", policy_names[default_policy[AA_MICROPHONE]]);
    json_section (out, "apps", apps, FALSE);
    json_section (out, "cgroups", cgroups, TRUE);
    g_string_append (out, "}\n");
    return g_string_free (out, FALSE);
}

/* @data as @path for root and group @gid only (0640), replaced in one step: a reader never sees
 * half a file, nor one it may not read */
static gboolean
file_publish (const char *path, const char *data, gid_t gid)
{
    g_autofree char *tmp = g_strconcat (path, ".new", NULL);
    int fd = open (tmp, O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW | O_CLOEXEC, 0600);
    gsize len = strlen (data), done = 0;
    gboolean ok = fd >= 0 && fchown (fd, (uid_t) -1, gid) == 0 && fchmod (fd, 0640) == 0;

    while (ok && done < len) {
        ssize_t n = write (fd, data + done, len - done);

        ok = n > 0;
        done += ok ? (gsize) n : 0;
    }
    if (fd >= 0 && close (fd) != 0)
        ok = FALSE;
    if (ok && rename (tmp, path) != 0)
        ok = FALSE;
    if (!ok) {
        g_warning ("Cannot publish %s: %s", path, g_strerror (errno));
        unlink (tmp);
    }
    return ok;
}

/* Writes the file of every account with a session, removes those of accounts that logged out */
static void
mic_publish (void)
{
    g_autoptr(GHashTable) uids = g_hash_table_new (NULL, NULL);
    GHashTableIter iter;
    gpointer key;

    if (!aa.mic_uids)
        aa.mic_uids = g_hash_table_new (NULL, NULL);
    g_hash_table_iter_init (&iter, aa.links);
    while (g_hash_table_iter_next (&iter, &key, NULL)) {
        guint32 uid;

        if (sscanf (key, "/user.slice/user-%*u.slice/user@%u.service/", &uid) == 1)
            g_hash_table_add (uids, GUINT_TO_POINTER (uid));
    }
    g_hash_table_iter_init (&iter, uids);
    while (g_hash_table_iter_next (&iter, &key, NULL)) {
        guint32 uid = GPOINTER_TO_UINT (key);
        g_autofree char *path = g_strdup_printf (MIC_DIR "/microphone-%u.conf", uid);
        g_autofree char *json = mic_json (uid);
        struct passwd *pw = getpwuid (uid);

        if (!pw)
            g_warning ("User %u has no group: WirePlumber decides nothing for it", uid);
        else if (file_publish (path, json, pw->pw_gid))
            g_hash_table_add (aa.mic_uids, key);
    }
    g_hash_table_iter_init (&iter, aa.mic_uids);
    while (g_hash_table_iter_next (&iter, &key, NULL)) {
        g_autofree char *path = NULL;

        if (g_hash_table_contains (uids, key))
            continue;
        path = g_strdup_printf (MIC_DIR "/microphone-%u.conf", GPOINTER_TO_UINT (key));
        unlink (path);
        g_hash_table_iter_remove (&iter);
    }
}

static gboolean
on_mic_idle (gpointer user_data)
{
    aa.mic_idle = 0;
    mic_publish ();
    return G_SOURCE_REMOVE;
}

/* Publishes once the main loop is idle: a scan or a policy change writes many verdicts at once.
 * Not before the first scan has ended (bpf_attach): the verdicts are not complete until then */
static void
mic_schedule (void)
{
    if (aa.scanned && !aa.mic_idle)
        aa.mic_idle = g_idle_add (on_mic_idle, NULL);
}

/* ----- State file ----- */

static void
state_load (void)
{
    g_autoptr(GKeyFile) kf = g_key_file_new ();
    g_auto(GStrv) groups = NULL;

    if (!g_key_file_load_from_file (kf, aa.state_file, G_KEY_FILE_NONE, NULL))
        return;
    groups = g_key_file_get_groups (kf, NULL);
    for (char **g = groups; *g; g++) {
        App *a;

        if (!app_id_valid (*g))
            continue;
        a = app_get (*g);
        for (int i = 0; i < AA_RES_MAX; i++) {
            g_autofree char *policy = g_key_file_get_string (kf, *g, res_names[i], NULL);
            g_autofree char *k_allowed = g_strconcat (res_names[i], "-allowed", NULL);
            g_autofree char *k_denied = g_strconcat (res_names[i], "-denied", NULL);
            g_autofree char *k_last_allowed = g_strconcat (res_names[i], "-last-allowed", NULL);
            g_autofree char *k_last_denied = g_strconcat (res_names[i], "-last-denied", NULL);

            for (int p = 0; policy && p < POLICY_MAX; p++)
                if (g_str_equal (policy, policy_names[p])) {
                    a->policy[i] = p;
                    a->set[i] = TRUE;
                }
            a->allowed[i] = g_key_file_get_uint64 (kf, *g, k_allowed, NULL);
            a->denied[i] = g_key_file_get_uint64 (kf, *g, k_denied, NULL);
            a->last_allowed[i] = g_key_file_get_int64 (kf, *g, k_last_allowed, NULL);
            a->last_denied[i] = g_key_file_get_int64 (kf, *g, k_last_denied, NULL);
        }
    }
}

static void
state_save (void)
{
    g_autoptr(GKeyFile) kf = g_key_file_new ();
    g_autoptr(GError) error = NULL;
    g_autofree char *data = NULL;
    GHashTableIter iter;
    const char *app;
    App *a;

    g_hash_table_iter_init (&iter, aa.apps);
    while (g_hash_table_iter_next (&iter, (gpointer *) &app, (gpointer *) &a)) {
        for (int i = 0; i < AA_RES_MAX; i++) {
            g_autofree char *k_allowed = g_strconcat (res_names[i], "-allowed", NULL);
            g_autofree char *k_denied = g_strconcat (res_names[i], "-denied", NULL);
            g_autofree char *k_last_allowed = g_strconcat (res_names[i], "-last-allowed", NULL);
            g_autofree char *k_last_denied = g_strconcat (res_names[i], "-last-denied", NULL);

            if (a->set[i])
                g_key_file_set_string (kf, app, res_names[i], policy_names[a->policy[i]]);
            if (a->allowed[i]) {
                g_key_file_set_uint64 (kf, app, k_allowed, a->allowed[i]);
                g_key_file_set_int64 (kf, app, k_last_allowed, a->last_allowed[i]);
            }
            if (a->denied[i]) {
                g_key_file_set_uint64 (kf, app, k_denied, a->denied[i]);
                g_key_file_set_int64 (kf, app, k_last_denied, a->last_denied[i]);
            }
        }
    }
    data = g_key_file_to_data (kf, NULL, NULL);
    if (!g_file_set_contents_full (aa.state_file, data, -1, G_FILE_SET_CONTENTS_CONSISTENT, 0600, &error))
        g_warning ("Cannot save %s: %s", aa.state_file, error->message);
    else
        aa.dirty = FALSE;
}

static gboolean
on_harvest_timer (gpointer user_data)
{
    harvest (0);
    return G_SOURCE_CONTINUE;
}

static gboolean
on_save_timer (gpointer user_data)
{
    if (aa.dirty)
        state_save ();
    return G_SOURCE_CONTINUE;
}

/* ----- D-Bus ----- */

static const char introspection_xml[] =
    "<node>"
    "  <interface name='org.plinius.AppAccess1'>"
    "    <method name='Get'>"
    "      <arg direction='in' type='s' name='app'/>"
    "      <arg direction='out' type='a(suuuxx)' name='resources'/>"
    "    </method>"
    "    <method name='List'>"
    "      <arg direction='out' type='a(ssuuuxx)' name='entries'/>"
    "    </method>"
    "    <method name='SetPolicy'>"
    "      <arg direction='in' type='s' name='app'/>"
    "      <arg direction='in' type='s' name='resource'/>"
    "      <arg direction='in' type='u' name='policy'/>"
    "    </method>"
    "    <method name='AllowOnce'>"
    "      <arg direction='in' type='s' name='app'/>"
    "      <arg direction='in' type='s' name='resource'/>"
    "    </method>"
    "    <method name='Forget'>"
    "      <arg direction='in' type='s' name='app'/>"
    "    </method>"
    "    <method name='CountUse'>"
    "      <arg direction='in' type='s' name='app'/>"
    "      <arg direction='in' type='u' name='pid'/>"
    "      <arg direction='in' type='s' name='resource'/>"
    "      <arg direction='out' type='s' name='counted'/>"
    "    </method>"
    "    <method name='CountAttempt'>"
    "      <arg direction='in' type='s' name='app'/>"
    "      <arg direction='in' type='u' name='pid'/>"
    "      <arg direction='in' type='s' name='resource'/>"
    "      <arg direction='in' type='b' name='allowed'/>"
    "      <arg direction='out' type='s' name='counted'/>"
    "    </method>"
    "    <method name='CountRefused'>"
    "      <arg direction='in' type='u' name='pid'/>"
    "      <arg direction='in' type='s' name='resource'/>"
    "      <arg direction='out' type='s' name='counted'/>"
    "    </method>"
    "    <signal name='AccessDenied'>"
    "      <arg type='s' name='app'/>"
    "      <arg type='s' name='resource'/>"
    "      <arg type='u' name='policy'/>"
    "      <arg type='u' name='uid'/>"
    "    </signal>"
    "    <signal name='Changed'>"
    "      <arg type='s' name='app'/>"
    "    </signal>"
    "  </interface>"
    "</node>";

static void
emit (const char *signal, GVariant *params)
{
    if (aa.bus)
        g_dbus_connection_emit_signal (aa.bus, NULL, OBJECT_PATH, BUS_NAME, signal, params, NULL);
    else
        g_variant_unref (g_variant_ref_sink (params));
}

static void
emit_denied (const char *app, int res, guint policy, guint32 uid)
{
    App *a = app_get (app);
    gint64 now = monotonic_seconds ();

    if (a->last_asked[res] && now - a->last_asked[res] < ASK_REPEAT)
        return;
    a->last_asked[res] = now;
    emit ("AccessDenied", g_variant_new ("(ssuu)", app, res_names[res], policy, uid));
}

static gboolean
uid_has_session (guint32 uid)
{
    g_autofree char *path = g_strdup_printf ("%s/%u", LOGIN_USERS, uid);
    g_autofree char *text = NULL;
    g_auto(GStrv) lines = NULL;

    if (!g_file_get_contents (path, &text, NULL, NULL))
        return FALSE;
    lines = g_strsplit (text, "\n", -1);
    for (char **l = lines; *l; l++)
        if (g_str_equal (*l, "STATE=active") || g_str_equal (*l, "STATE=online"))
            return TRUE;
    return FALSE;
}

static guint32
caller_uint (GDBusConnection *bus, const char *sender, const char *method)
{
    g_autoptr(GVariant) reply = NULL;
    guint32 value = G_MAXUINT32;

    reply = g_dbus_connection_call_sync (bus, "org.freedesktop.DBus", "/org/freedesktop/DBus",
                                         "org.freedesktop.DBus", method, g_variant_new ("(s)", sender),
                                         G_VARIANT_TYPE ("(u)"), G_DBUS_CALL_FLAGS_NONE, -1, NULL, NULL);
    if (reply)
        g_variant_get (reply, "(u)", &value);
    return value;
}

/* The caller runs one of @exes (its process's executable, from /proc) */
static gboolean
caller_runs (GDBusConnection *bus, const char *sender, const char *const *exes)
{
    guint32 pid = caller_uint (bus, sender, "GetConnectionUnixProcessID");
    char link[64], target[PATH_MAX];
    ssize_t n;

    if (pid == G_MAXUINT32 || pid == 0)
        return FALSE;
    g_snprintf (link, sizeof (link), "/proc/%u/exe", pid);
    n = readlink (link, target, sizeof (target) - 1);
    if (n <= 0)
        return FALSE;
    target[n] = '\0';
    return g_strv_contains (exes, target);
}

/*
 * The caller is in the group wheel: its groups from the bus daemon (GetConnectionCredentials,
 * UnixGroupIDs: the process as it connected), wheel's id from the group database. Anything
 * missing counts as no.
 */
static gboolean
caller_in_wheel (GDBusConnection *bus, const char *sender)
{
    g_autoptr(GVariant) reply = NULL;
    g_autoptr(GVariant) creds = NULL;
    g_autoptr(GVariant) gids = NULL;
    struct group *wheel = getgrnam ("wheel");
    const guint32 *ids;
    gid_t wheel_gid;
    gsize n;

    if (!wheel)
        return FALSE;
    wheel_gid = wheel->gr_gid;
    reply = g_dbus_connection_call_sync (bus, "org.freedesktop.DBus", "/org/freedesktop/DBus",
                                         "org.freedesktop.DBus", "GetConnectionCredentials",
                                         g_variant_new ("(s)", sender), G_VARIANT_TYPE ("(a{sv})"),
                                         G_DBUS_CALL_FLAGS_NONE, -1, NULL, NULL);
    if (!reply)
        return FALSE;
    creds = g_variant_get_child_value (reply, 0);
    gids = g_variant_lookup_value (creds, "UnixGroupIDs", G_VARIANT_TYPE ("au"));
    if (!gids)
        return FALSE;
    ids = g_variant_get_fixed_array (gids, &n, sizeof (guint32));
    for (gsize i = 0; i < n; i++)
        if (ids[i] == wheel_gid)
            return TRUE;
    return FALSE;
}

enum {
    CALL_READ,    /* Get, List */
    CALL_ONCE,    /* AllowOnce: the caller's own running apps */
    CALL_POLICY,  /* SetPolicy, Forget: for every account */
    CALL_COUNT,   /* CountUse */
    CALL_REFUSED, /* CountRefused, CountAttempt: whose app it is, on_method_call checks */
};

/* Whether @sender may make a call of kind @what; @uid: the caller's uid (G_MAXUINT32 unknown) */
static gboolean
caller_allowed (GDBusConnection *bus, const char *sender, int what, guint32 *uid_out)
{
    guint32 uid = caller_uint (bus, sender, "GetConnectionUnixUser");

    *uid_out = uid;
    if (uid == 0)
        return TRUE;
    if (uid == G_MAXUINT32)
        return FALSE;
    if (what == CALL_COUNT)
        return caller_runs (bus, sender, counters);
    if (what == CALL_REFUSED)
        return caller_runs (bus, sender, refusers);
    if (uid < 1000 || !uid_has_session (uid))
        return FALSE;
    if (what == CALL_READ)
        return TRUE;
    if (!caller_runs (bus, sender, changers))
        return FALSE;
    return what == CALL_ONCE || caller_in_wheel (bus, sender);
}

/* The rows of @app; an app the daemon does not know gets the defaults, and is not added */
static void
add_rows (GVariantBuilder *b, const char *app, gboolean with_app)
{
    App unknown = { 0 };
    App *a = g_hash_table_lookup (aa.apps, app);

    if (!a) {
        app_defaults (&unknown, app);
        a = &unknown;
    }

    for (int i = 0; i < AA_RES_MAX; i++) {
        if (with_app)
            g_variant_builder_add (b, "(ssuuuxx)", app, res_names[i], a->policy[i],
                                   (guint32) MIN (a->allowed[i], G_MAXUINT32),
                                   (guint32) MIN (a->denied[i], G_MAXUINT32),
                                   a->last_allowed[i], a->last_denied[i]);
        else
            g_variant_builder_add (b, "(suuuxx)", res_names[i], a->policy[i],
                                   (guint32) MIN (a->allowed[i], G_MAXUINT32),
                                   (guint32) MIN (a->denied[i], G_MAXUINT32),
                                   a->last_allowed[i], a->last_denied[i]);
    }
}

/* The cgroup of process @pid as the daemon knows it, NULL for none outside app.slice */
static Cgroup *
cgroup_of_pid (guint32 pid)
{
    g_autofree char *file = g_strdup_printf ("/proc/%u/cgroup", pid);
    g_autofree char *text = NULL;
    const char *path = NULL;
    guint64 *id;

    if (pid == 0 || !g_file_get_contents (file, &text, NULL, NULL))
        return NULL;
    for (char *line = strtok (text, "\n"); line; line = strtok (NULL, "\n"))
        if (g_str_has_prefix (line, "0::"))
            path = line + 3;
    if (!path || !strstr (path, "/app.slice/"))
        return NULL;
    id = g_hash_table_lookup (aa.by_path, path);
    if (!id) {
        cgroup_scan (path);
        id = g_hash_table_lookup (aa.by_path, path);
    }
    return id ? g_hash_table_lookup (aa.cgroups, id) : NULL;
}

/* The application id of process @pid from its cgroup, NULL for none (a service, a shell) */
static const char *
app_of_pid (guint32 pid)
{
    Cgroup *cg = cgroup_of_pid (pid);

    return cg ? cg->app : NULL;
}

/* An allowed use of @res someone else saw, for @app or the app of @pid; the app counted or NULL */
static const char *
count_use (const char *app, guint32 pid, int res)
{
    App *a;

    if (!app_id_valid (app))
        app = app_of_pid (pid);
    if (!app)
        return NULL;
    a = app_get (app);
    a->allowed[res]++;
    a->last_allowed[res] = time (NULL);
    aa.dirty = TRUE;
    return app;
}

/* A refusal of @res for @app someone else saw, counted like the programs' refusals */
static void
count_refused (const char *app, int res)
{
    App *a = app_get (app);

    a->denied[res]++;
    a->last_denied[res] = time (NULL);
    aa.dirty = TRUE;
}

static void
on_count_refused (GVariant *params, guint32 caller, GDBusMethodInvocation *invocation)
{
    const char *resource = NULL;
    guint32 pid;
    Cgroup *cg;
    int res;

    g_variant_get (params, "(u&s)", &pid, &resource);
    res = res_from_name (resource);
    if (res != AA_CAMERA) {
        g_dbus_method_invocation_return_error (invocation, G_DBUS_ERROR, G_DBUS_ERROR_INVALID_ARGS,
                                               res < 0 ? "Unknown resource %s"
                                                       : "Refusals of %s are counted by the programs",
                                               resource);
        return;
    }
    cg = cgroup_of_pid (pid);
    if (!cg || !cg->app) {
        g_dbus_method_invocation_return_value (invocation, g_variant_new ("(s)", ""));
        return;
    }
    /* The agent counts for its own account's apps only; root for any */
    if (caller != 0 && cg->uid != caller) {
        g_dbus_method_invocation_return_error_literal (invocation, G_DBUS_ERROR, G_DBUS_ERROR_ACCESS_DENIED,
                                                       "Not allowed");
        return;
    }
    /* The portal has answered the app already: Changed for Settings, no AccessDenied (no question) */
    count_refused (cg->app, res);
    emit ("Changed", g_variant_new ("(s)", cg->app));
    g_dbus_method_invocation_return_value (invocation, g_variant_new ("(s)", cg->app));
}

/* The cgroup of an instance of @app: in @caller's account (root: any), else in another (@other) */
static Cgroup *
app_instance (const char *app, guint32 caller, gboolean *other)
{
    GHashTableIter iter;
    Cgroup *cg;

    *other = FALSE;
    g_hash_table_iter_init (&iter, aa.cgroups);
    while (g_hash_table_iter_next (&iter, NULL, (gpointer *) &cg)) {
        if (!cg->app || !g_str_equal (cg->app, app))
            continue;
        if (caller == 0 || cg->uid == caller)
            return cg;
        *other = TRUE;
    }
    return NULL;
}

static void
on_count_attempt (GVariant *params, guint32 caller, GDBusMethodInvocation *invocation)
{
    const char *app = NULL, *resource = NULL;
    gboolean allowed, other = FALSE;
    guint32 pid;
    Cgroup *cg;
    int res;

    g_variant_get (params, "(&su&sb)", &app, &pid, &resource, &allowed);
    res = res_from_name (resource);
    if (res != AA_MICROPHONE) {
        g_dbus_method_invocation_return_error (invocation, G_DBUS_ERROR, G_DBUS_ERROR_INVALID_ARGS,
                                               res < 0 ? "Unknown resource %s" : "Attempts of %s are counted by others",
                                               resource);
        return;
    }
    /* WirePlumber names the app of a Flatpak client of pipewire-pulse, else the process */
    if (app_id_valid (app)) {
        cg = app_instance (app, caller, &other);
    } else {
        cg = cgroup_of_pid (pid);
        other = cg && cg->app && caller != 0 && cg->uid != caller;
        if (other || (cg && !cg->app))
            cg = NULL;
    }
    if (!cg) {
        if (other)
            g_dbus_method_invocation_return_error_literal (invocation, G_DBUS_ERROR, G_DBUS_ERROR_ACCESS_DENIED,
                                                           "Not allowed");
        else
            g_dbus_method_invocation_return_value (invocation, g_variant_new ("(s)", ""));
        return;
    }
    if (allowed) {
        count_use (cg->app, 0, res);
    } else {
        count_refused (cg->app, res);
        /* The hook refused it: an app that asks gets the question (verdict ASK, not allowed once) */
        if (verdict_of (cg, res) == AA_VERDICT_ASK)
            emit_denied (cg->app, res, POLICY_ASK, cg->uid);
    }
    emit ("Changed", g_variant_new ("(s)", cg->app));
    g_dbus_method_invocation_return_value (invocation, g_variant_new ("(s)", cg->app));
}

static void
on_method_call (GDBusConnection *bus, const char *sender, const char *path, const char *iface,
                const char *method, GVariant *params, GDBusMethodInvocation *invocation, gpointer user_data)
{
    gboolean policy_change = g_str_equal (method, "SetPolicy") || g_str_equal (method, "Forget");
    gboolean once = g_str_equal (method, "AllowOnce");
    gboolean check = g_str_equal (method, "CountUse");
    gboolean refused = g_str_equal (method, "CountRefused") || g_str_equal (method, "CountAttempt");
    const char *app = NULL, *resource = NULL;
    guint32 caller = G_MAXUINT32;
    gboolean opened = FALSE;
    int res = 0;

    if (!caller_allowed (bus, sender,
                         refused  ? CALL_REFUSED
                         : check  ? CALL_COUNT
                         : policy_change ? CALL_POLICY
                         : once   ? CALL_ONCE
                                  : CALL_READ,
                         &caller)) {
        g_dbus_method_invocation_return_error_literal (invocation, G_DBUS_ERROR, G_DBUS_ERROR_ACCESS_DENIED,
                                                       "Not allowed");
        return;
    }
    if (g_str_equal (method, "List")) {
        GVariantBuilder b;
        GHashTableIter iter;
        const char *key;

        harvest (0);
        g_variant_builder_init (&b, G_VARIANT_TYPE ("a(ssuuuxx)"));
        g_hash_table_iter_init (&iter, aa.apps);
        while (g_hash_table_iter_next (&iter, (gpointer *) &key, NULL))
            add_rows (&b, key, TRUE);
        g_dbus_method_invocation_return_value (invocation, g_variant_new ("(a(ssuuuxx))", &b));
        return;
    }
    if (g_str_equal (method, "CountAttempt")) {
        on_count_attempt (params, caller, invocation);
        return;
    }
    if (refused) {
        on_count_refused (params, caller, invocation);
        return;
    }
    if (check) {
        const char *counted;
        guint32 pid;

        g_variant_get (params, "(&su&s)", &app, &pid, &resource);
        res = res_from_name (resource);
        if (res < 0) {
            g_dbus_method_invocation_return_error (invocation, G_DBUS_ERROR, G_DBUS_ERROR_INVALID_ARGS,
                                                   "Unknown resource %s", resource);
            return;
        }
        counted = count_use (app, pid, res);
        if (counted)
            emit ("Changed", g_variant_new ("(s)", counted));
        g_dbus_method_invocation_return_value (invocation, g_variant_new ("(s)", counted ? counted : ""));
        return;
    }

    g_variant_get_child (params, 0, "&s", &app);
    if (!app_id_valid (app)) {
        g_dbus_method_invocation_return_error (invocation, G_DBUS_ERROR, G_DBUS_ERROR_INVALID_ARGS,
                                               "Not an application id: %s", app);
        return;
    }
    if (g_str_equal (method, "SetPolicy") || g_str_equal (method, "AllowOnce")) {
        g_variant_get_child (params, 1, "&s", &resource);
        res = res_from_name (resource);
        if (res < 0) {
            g_dbus_method_invocation_return_error (invocation, G_DBUS_ERROR, G_DBUS_ERROR_INVALID_ARGS,
                                                   "Unknown resource %s", resource);
            return;
        }
    }

    if (g_str_equal (method, "Get")) {
        GVariantBuilder b;

        harvest (0);
        g_variant_builder_init (&b, G_VARIANT_TYPE ("a(suuuxx)"));
        add_rows (&b, app, FALSE);
        g_dbus_method_invocation_return_value (invocation, g_variant_new ("(a(suuuxx))", &b));
        return;
    }
    if (g_str_equal (method, "SetPolicy")) {
        guint32 policy;
        App *a;

        g_variant_get_child (params, 2, "u", &policy);
        if (policy >= POLICY_MAX) {
            g_dbus_method_invocation_return_error (invocation, G_DBUS_ERROR, G_DBUS_ERROR_INVALID_ARGS,
                                                   "Unknown policy %u", policy);
            return;
        }
        a = app_get (app);
        a->policy[res] = policy;
        a->set[res] = TRUE;
        /* A policy replaces what was allowed once: Don't Allow closes the running instances too */
        once_clear (app, res);
    } else if (g_str_equal (method, "AllowOnce")) {
        GHashTableIter iter;
        Cgroup *cg;

        /* Only the answer to a question: an app an administrator set to Allow or Don't Allow
         * keeps that (a user who may not change policies must not open it this way).
         * Only the instances in the caller's own session (root: all): the agent asks the user of
         * the session the app runs in, which must not open it for another account's app */
        opened = app_policy (app, res) == POLICY_ASK;
        g_hash_table_iter_init (&iter, aa.cgroups);
        while (opened && g_hash_table_iter_next (&iter, NULL, (gpointer *) &cg))
            if (cg->app && g_str_equal (cg->app, app) && (caller == 0 || cg->uid == caller))
                cg->once[res] = TRUE;
    } else {
        App *a = app_get (app);

        harvest (0);
        memset (a, 0, sizeof (*a));
        app_defaults (a, app);
        for (int i = 0; i < AA_RES_MAX; i++)
            once_clear (app, i);
    }
    app_rewrite (app);
    /* Also for an app without a running instance: the hook's list of apps */
    mic_schedule ();
    if (res == AA_KEY && ((g_str_equal (method, "AllowOnce") && opened) ||
                          (g_str_equal (method, "SetPolicy") && app_policy (app, res) == POLICY_ALLOW)))
        keys_reannounce ();
    aa.dirty = TRUE;
    state_save ();
    emit ("Changed", g_variant_new ("(s)", app));
    g_dbus_method_invocation_return_value (invocation, NULL);
}

static const GDBusInterfaceVTable vtable = { .method_call = on_method_call };

static void
on_bus_acquired (GDBusConnection *bus, const char *name, gpointer user_data)
{
    g_autoptr(GError) error = NULL;

    aa.bus = bus;
    if (!g_dbus_connection_register_object (bus, OBJECT_PATH, aa.introspection->interfaces[0], &vtable,
                                            NULL, NULL, &error))
        g_warning ("Cannot export %s: %s", OBJECT_PATH, error->message);
}

static void
on_name_lost (GDBusConnection *bus, const char *name, gpointer user_data)
{
    g_printerr ("Lost the bus name %s\n", name);
    g_main_loop_quit (aa.loop);
}

static gboolean
on_signal (gpointer user_data)
{
    g_main_loop_quit (aa.loop);
    return G_SOURCE_REMOVE;
}

/* ----- Setup ----- */

static int
map_fd (const char *name)
{
    int fd = bpf_object__find_map_fd_by_name (aa.obj, name);

    if (fd < 0)
        g_printerr ("No map %s in the BPF object\n", name);
    return fd;
}

/* Loads the programs; they are attached only after every verdict is written (bpf_attach) */
static gboolean
bpf_load (const char *file)
{
    aa.obj = bpf_object__open_file (file, NULL);
    if (!aa.obj) {
        g_printerr ("Cannot open %s: %s\n", file, g_strerror (errno));
        return FALSE;
    }
    if (bpf_object__load (aa.obj) != 0) {
        g_printerr ("Cannot load the BPF programs of %s\n", file);
        return FALSE;
    }
    aa.devices_fd = map_fd ("devices");
    aa.cgroups_fd = map_fd ("cgroups");
    aa.counts_fd = map_fd ("counts");
    return aa.devices_fd >= 0 && aa.cgroups_fd >= 0 && aa.counts_fd >= 0;
}

/* Attaches to the app.slices the scan found. The links end with the process: a daemon that is
 * gone refuses nothing */
static gboolean
bpf_attach (void)
{
    int events_fd;

    aa.scanned = TRUE;
    for (guint i = 0; i < aa.to_attach->len; i++)
        slice_attach (aa.to_attach->pdata[i]);
    g_ptr_array_set_size (aa.to_attach, 0);
    mic_schedule ();
    if (g_hash_table_size (aa.links) == 0)
        g_message ("No session yet; the programs are attached when one starts");
    events_fd = map_fd ("events");
    aa.rb = events_fd >= 0 ? ring_buffer__new (events_fd, on_event, NULL, NULL) : NULL;
    if (!aa.rb) {
        g_printerr ("Cannot open the ring buffer\n");
        return FALSE;
    }
    g_unix_fd_add (ring_buffer__epoll_fd (aa.rb), G_IO_IN, on_ringbuf, NULL);
    return TRUE;
}

int
main (int argc, char **argv)
{
    g_autofree char *bpf_file = NULL;
    g_autoptr(GOptionContext) context = NULL;
    g_autoptr(GError) error = NULL;
    gboolean no_bus = FALSE, check = FALSE;
    GOptionEntry entries[] = {
        { "bpf", 0, 0, G_OPTION_ARG_FILENAME, &bpf_file, "BPF object (default " DEFAULT_BPF ")", "FILE" },
        { "state", 0, 0, G_OPTION_ARG_FILENAME, &aa.state_file, "State file (default " DEFAULT_STATE ")", "FILE" },
        { "no-bus", 0, 0, G_OPTION_ARG_NONE, &no_bus, "Do not take the bus name (tests)", NULL },
        { "check", 0, 0, G_OPTION_ARG_NONE, &check, "Load and attach the programs, list the cgroups, exit", NULL },
        { "test-device", 0, 0, G_OPTION_ARG_STRING_ARRAY, &aa.test_devices,
          "Treat a device as camera or security-key (tests)", "MAJ:MIN=RESOURCE" },
        { NULL }
    };

    context = g_option_context_new ("- decide and count per app: cameras, security keys, network, microphone");
    g_option_context_add_main_entries (context, entries, NULL);
    if (!g_option_context_parse (context, &argc, &argv, &error)) {
        g_printerr ("%s\n", error->message);
        return 2;
    }
    if (g_file_test (OFF_FILE, G_FILE_TEST_EXISTS)) {
        g_print ("%s exists: nothing is decided\n", OFF_FILE);
        return 0;
    }
    if (!aa.state_file)
        aa.state_file = g_strdup (DEFAULT_STATE);

    aa.watches = g_hash_table_new_full (NULL, NULL, NULL, g_free);
    aa.cgroups = g_hash_table_new_full (g_int64_hash, g_int64_equal, NULL, (GDestroyNotify) cgroup_free);
    aa.by_path = g_hash_table_new_full (g_str_hash, g_str_equal, g_free, g_free);
    aa.apps = g_hash_table_new_full (g_str_hash, g_str_equal, g_free, g_free);
    aa.devices = g_hash_table_new (NULL, NULL);
    aa.device_dirs = g_hash_table_new_full (NULL, NULL, NULL, g_free);
    aa.links = g_hash_table_new_full (g_str_hash, g_str_equal, g_free, (GDestroyNotify) links_free);
    aa.to_attach = g_ptr_array_new_with_free_func (g_free);
    aa.loop = g_main_loop_new (NULL, FALSE);
    state_load ();

    aa.cgroup_fd = open (CGROUP_ROOT, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    aa.inotify_fd = inotify_init1 (IN_NONBLOCK | IN_CLOEXEC);
    if (aa.cgroup_fd < 0 || aa.inotify_fd < 0) {
        g_printerr ("Cannot open %s or inotify: %s\n", CGROUP_ROOT, g_strerror (errno));
        return 1;
    }
    if (!bpf_load (bpf_file ? bpf_file : DEFAULT_BPF))
        return 1;
    /* Watches first, then the scan: nothing created in between is missed */
    aa.dev_wd = inotify_add_watch (aa.inotify_fd, "/dev", IN_CREATE | IN_DELETE);
    devices_scan ();
    cgroup_scan ("/user.slice");
    if (!bpf_attach ())
        return 1;

    if (check) {
        GHashTableIter iter;
        Cgroup *cg;
        gpointer k, v;

        g_hash_table_iter_init (&iter, aa.devices);
        while (g_hash_table_iter_next (&iter, &k, &v))
            g_print ("device %u:%u %s\n", GPOINTER_TO_UINT (k) >> 20, GPOINTER_TO_UINT (k) & 0xfffff,
                     res_names[GPOINTER_TO_INT (v)]);
        g_hash_table_iter_init (&iter, aa.cgroups);
        while (g_hash_table_iter_next (&iter, NULL, (gpointer *) &cg)) {
            g_print ("%-12s", cg->app ? "app" : "service");
            for (int i = 0; i < AA_RES_MAX; i++)
                g_print (" %s=%u", res_names[i], verdict_of (cg, i));
            g_print (" %s %s\n", cg->app ? cg->app : "-", cg->path);
        }
        g_hash_table_iter_init (&iter, aa.links);
        while (g_hash_table_iter_next (&iter, &k, &v))
            g_print ("attached to %s: %u programs\n", (char *) k, ((GPtrArray *) v)->len);
        return 0;
    }

    g_unix_fd_add (aa.inotify_fd, G_IO_IN, on_inotify, NULL);
    g_timeout_add_seconds (HARVEST_EVERY, on_harvest_timer, NULL);
    g_timeout_add_seconds (SAVE_EVERY, on_save_timer, NULL);
    g_unix_signal_add (SIGTERM, on_signal, NULL);
    g_unix_signal_add (SIGINT, on_signal, NULL);

    if (!no_bus) {
        aa.introspection = g_dbus_node_info_new_for_xml (introspection_xml, NULL);
        g_bus_own_name (G_BUS_TYPE_SYSTEM, BUS_NAME, G_BUS_NAME_OWNER_FLAGS_NONE, on_bus_acquired, NULL,
                        on_name_lost, NULL, NULL);
    }

    g_main_loop_run (aa.loop);
    harvest (0);
    state_save ();
    return 0;
}
