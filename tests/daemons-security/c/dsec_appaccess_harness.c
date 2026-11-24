/*
 * Harness for plinius-appaccessd.c (its main() renamed), included as a copy whose system paths
 * are relative ("sys/fs/cgroup", "run/systemd/users", "proc/%u/exe" …): the harness runs in a
 * test directory that stands for /, so the daemon walks and watches (inotify) a cgroup tree of
 * plain directories there, reads logind's user files, the callers' /proc entries and the sysfs
 * device classes from there and saves its state there.
 *
 * Faked: the BPF object, its programs, links, maps and ring buffer (in-memory maps keyed by fake
 * fds; "map ..." prints what the programs would see), open_by_handle_at (finds the directory
 * with that inode below sys/fs/cgroup; the real one needs CAP_DAC_READ_SEARCH), the clocks, the
 * bus (dsec_fakebus.h plus GetConnectionUnixProcessID; signals are recorded), g_bus_own_name and
 * the object registration. readlink() of /proc/self/fd/N is real, with the test directory cut
 * off, as if it were the root of the mount namespace.
 *
 * "main ARGS..." as the first arguments runs the daemon's main() instead of the command loop;
 * AA_TERM_MS sends SIGTERM that many ms into the main loop, AA_OWN=lost loses the bus name,
 * AA_REGISTER=fail fails the object registration, AA_BPF_FAIL as "bpf fail" below.
 *
 * Commands on stdin, one output line each:
 *   bus KEY VALUE                  see dsec_fakebus.h
 *   pid N                          GetConnectionUnixProcessID answers N (-1: an error)
 *   creds ok|fail                  whether GetConnectionCredentials answers
 *   clock MONO_S UNIX_S            the monotonic and the wall clock
 *   bpf fail WHAT[,WHAT]           make faked libbpf calls fail: update, lookup, open, load,
 *                                  attach:PROGRAM, map:NAME, ringbuf ("-" none)
 *   leaf NAME / valid ID / level PATH / res NAME    app_from_leaf, app_id_valid, cgroup_level,
 *                                  res_from_name ("-" for NULL / -1)
 *   scan PATH / add PATH / remove PATH   cgroup_scan / cgroup_add / cgroup_remove
 *   attach                         bpf_attach(): "ok" / "fail"
 *   cgroups                        every cgroup the daemon knows, sorted by path:
 *                                  "PATH app=APP uid=UID map=V0,V1,V2 once=O0,O1,O2" (map: the
 *                                  verdicts in the BPF map, "-" none)
 *   links                          "PATH N" per attached app.slice, then "to_attach N"
 *   destroyed                      links destroyed so far
 *   count PATH RES A D LAST_A LAST_D    put attempts into the counts map (LAST_* monotonic s)
 *   countid ID RES A D LAST_A LAST_D    the same for a raw cgroup id
 *   harvest [PATH]                 harvest(0) / harvest(id of PATH)
 *   app APP                        per resource "RES policy=P set=S allowed=A denied=D
 *                                  last=LA,LD" in one line ("-" if the daemon has no such app)
 *   apps                           the app ids the daemon keeps, sorted ("-" none)
 *   load / save                    state_load / state_save, then "dirty 0|1"
 *   timer harvest|save             on_harvest_timer / on_save_timer
 *   testdev SPEC[,SPEC]            --test-device values ("-" none)
 *   devices                        devices_scan(), then the devices map "MAJ:MIN=RES ..."
 *   reannounce                     keys_reannounce()
 *   devwatch DIR                   watch DIR as the daemon watches /dev
 *   noinotify                      the daemon's inotify fd closed: no watch can be added
 *   inotify                        on_inotify() on the daemon's inotify fd
 *   event ID RES KIND VERDICT [SIZE]   on_event() with that record (SIZE: bytes handed over)
 *   ringbuf ID RES KIND VERDICT    queue a record, then on_ringbuf() delivers it
 *   pidapp PID                     app_of_pid()
 *   call METHOD GVARIANT           on_method_call() as ":1.9": "ok ..." / "error ..."
 *   signals                        the signals emitted since the last time ("-" none)
 *   busoff                         no bus connection (emit drops the signal)
 *   maps                           entries in the maps: "devices=N cgroups=N counts=N"
 *   devmap                         the devices map, without a scan
 *   mkdir PATH / rmdir PATH / touch PATH / unlink PATH    below the test directory, between two
 *                                  commands
 *   mic                            mic_publish() now: the published accounts "UID ... gid=G" ("-"
 *                                  none; G: the group of the last file written, 0 none); the
 *                                  files are in run/plinius-appaccess
 *   micidle                        whether a publication waits for the idle main loop, then runs
 *                                  the main loop's pending sources: "idle 0|1"
 *   nopasswd UID[,UID] | nopasswd -  getpwuid() knows no such users (else every uid is one, its
 *                                  group 100000 + uid); "bpf fail chown" fails fchown()
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <ftw.h>
#include <grp.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
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
#include <pwd.h>

#include "dsec_fakebus.h"
#include "appaccess.h"

/* ---- Clocks ---- */

static gint64 fake_mono_s = 5000;
static gint64 fake_unix_s = 1800000000;

static gint64
fake_monotonic_time (void)
{
    return fake_mono_s * G_USEC_PER_SEC;
}

static int
fake_clock_gettime (clockid_t clock, struct timespec *ts)
{
    (void) clock;
    ts->tv_sec = fake_mono_s;
    ts->tv_nsec = 0;
    return 0;
}

static time_t
fake_time (time_t *t)
{
    if (t)
        *t = fake_unix_s;
    return fake_unix_s;
}

/* ---- Failures on demand ---- */

static char bpf_fail[256] = "";

static gboolean
failing (const char *what)
{
    g_auto (GStrv) parts = g_strsplit (bpf_fail, ",", -1);

    return g_strv_contains ((const char *const *) parts, what);
}

/* ---- Accounts: getpwuid and fchown (the daemon gives a file to the account's group) ---- */

static char no_users[256] = "";

static struct passwd *
fake_getpwuid (uid_t uid)
{
    static struct passwd pw;
    g_auto (GStrv) parts = g_strsplit (no_users, ",", -1);
    g_autofree char *id = g_strdup_printf ("%u", (unsigned) uid);

    if (g_strv_contains ((const char *const *) parts, id))
        return NULL;
    memset (&pw, 0, sizeof pw);
    pw.pw_uid = uid;
    pw.pw_gid = 100000 + uid;
    return &pw;
}

static gid_t last_chown_gid;

static int
fake_fchown (int fd, uid_t owner, gid_t group)
{
    if (failing ("chown") || owner != (uid_t) -1) {
        errno = EPERM;
        return -1;
    }
    last_chown_gid = group;
    return 0;
}

/* ---- Bus ---- */

static gint64 fake_pid = 4242;
static gboolean creds_fail;
static GString *signals_log;

static GVariant *
aa_call_sync (GDBusConnection *bus, const char *dest, const char *path, const char *iface,
              const char *method, GVariant *params, const GVariantType *reply_type,
              GDBusCallFlags flags, int timeout, GCancellable *cancellable, GError **error)
{
    if (g_strcmp0 (method, "GetConnectionUnixProcessID") == 0) {
        if (params)
            g_variant_unref (g_variant_ref_sink (params));
        fb.sync_calls++;
        if (fake_pid < 0) {
            g_set_error (error, G_DBUS_ERROR, G_DBUS_ERROR_NAME_HAS_NO_OWNER, "fake bus: no such sender");
            return NULL;
        }
        return g_variant_ref_sink (g_variant_new ("(u)", (guint32) fake_pid));
    }
    if (creds_fail && g_strcmp0 (method, "GetConnectionCredentials") == 0) {
        if (params)
            g_variant_unref (g_variant_ref_sink (params));
        g_set_error (error, G_DBUS_ERROR, G_DBUS_ERROR_FAILED, "fake bus: no credentials");
        return NULL;
    }
    return fake_call_sync (bus, dest, path, iface, method, params, reply_type, flags, timeout,
                           cancellable, error);
}

static gboolean
aa_emit_signal (GDBusConnection *bus, const char *dest, const char *path, const char *iface,
                const char *signal, GVariant *params, GError **error)
{
    g_autoptr (GVariant) p = g_variant_ref_sink (params);
    g_autofree char *text = g_variant_print (p, FALSE);

    if (!signals_log)
        signals_log = g_string_new (NULL);
    g_string_append_printf (signals_log, "%s%s %s %s%s %s", signals_log->len ? " | " : "",
                            path, iface, dest ? "dest " : "", signal, text);
    return TRUE;
}

static int fake_conn;

typedef struct {
    GBusAcquiredCallback acquired;
    GBusNameLostCallback lost;
} OwnCallbacks;

static gboolean
fake_own_idle (gpointer data)
{
    OwnCallbacks *cbs = data;

    if (g_strcmp0 (g_getenv ("AA_OWN"), "lost") == 0)
        cbs->lost (NULL, "org.plinius.AppAccess1", NULL);
    else
        cbs->acquired ((GDBusConnection *) &fake_conn, "org.plinius.AppAccess1", NULL);
    g_free (cbs);
    return G_SOURCE_REMOVE;
}

/* Like GDBus: the callbacks come from the main loop */
static guint
fake_bus_own_name (GBusType type, const char *name, GBusNameOwnerFlags flags,
                   GBusAcquiredCallback bus_acquired, GBusNameAcquiredCallback name_acquired,
                   GBusNameLostCallback name_lost, gpointer data, GDestroyNotify free_data)
{
    OwnCallbacks *cbs = g_new0 (OwnCallbacks, 1);

    printf ("own %s %s\n", type == G_BUS_TYPE_SYSTEM ? "system" : "other", name);
    cbs->acquired = bus_acquired;
    cbs->lost = name_lost;
    g_idle_add (fake_own_idle, cbs);
    return 1;
}

static guint
fake_register_object (GDBusConnection *bus, const char *path, GDBusInterfaceInfo *info,
                      const GDBusInterfaceVTable *vtable, gpointer data, GDestroyNotify free_data,
                      GError **error)
{
    GString *methods = g_string_new (NULL);

    for (GDBusMethodInfo **m = info->methods; m && *m; m++)
        g_string_append_printf (methods, "%s%s", methods->len ? "," : "", (*m)->name);
    printf ("register %s %s %s %s\n", path, info->name, methods->str,
            vtable && vtable->method_call ? "handler" : "nohandler");
    g_string_free (methods, TRUE);
    if (g_strcmp0 (g_getenv ("AA_REGISTER"), "fail") == 0) {
        g_set_error (error, G_IO_ERROR, G_IO_ERROR_EXISTS, "fake: already exported");
        return 0;
    }
    return 1;
}

/* ---- The test directory as / ---- */

static char test_root[PATH_MAX];

static ssize_t
fake_readlink (const char *link, char *buf, size_t size)
{
    ssize_t n = readlink (link, buf, size);
    size_t c = strlen (test_root);

    if (n > 0 && g_str_has_prefix (link, "/proc/self/fd/") && (size_t) n > c &&
        strncmp (buf, test_root, c) == 0 && buf[c] == '/') {
        /* "/sys/fs/cgroup/…" as the daemon would see it, relative here like its CGROUP_ROOT */
        memmove (buf, buf + c + 1, n - c - 1);
        n -= c + 1;
    }
    return n;
}

static guint64 handle_wanted;
static char handle_found[PATH_MAX];

static int
handle_visit (const char *path, const struct stat *st, int type, struct FTW *ftw)
{
    if (type == FTW_D && st->st_ino == handle_wanted) {
        snprintf (handle_found, sizeof handle_found, "%s", path);
        return 1;
    }
    return 0;
}

static int
fake_open_by_handle_at (int mount_fd, struct file_handle *handle, int flags)
{
    guint64 id;

    (void) mount_fd;
    if (handle->handle_type != 0xfe || handle->handle_bytes != sizeof (id)) {
        errno = EINVAL;
        return -1;
    }
    memcpy (&id, handle->f_handle, sizeof (id));
    handle_wanted = id;
    handle_found[0] = '\0';
    if (nftw ("sys/fs/cgroup", handle_visit, 16, FTW_PHYS) != 1) {
        errno = ESTALE;
        return -1;
    }
    return open (handle_found, flags);
}

/* ---- libbpf ---- */

#define FD_DEVICES 9001
#define FD_CGROUPS 9002
#define FD_COUNTS  9003
#define FD_EVENTS  9004

typedef struct {
    int fd;
    gsize key_size, value_size;
    GHashTable *entries; /* GBytes key -> GBytes value */
} FakeMap;

static FakeMap fake_maps[] = {
    { FD_DEVICES, sizeof (__u32), sizeof (__u32), NULL },
    { FD_CGROUPS, sizeof (__u64), sizeof (struct aa_cgroup), NULL },
    { FD_COUNTS, sizeof (struct aa_count_key), sizeof (struct aa_count), NULL },
};

static FakeMap *
fake_map (int fd)
{
    for (gsize i = 0; i < G_N_ELEMENTS (fake_maps); i++)
        if (fake_maps[i].fd == fd) {
            if (!fake_maps[i].entries)
                fake_maps[i].entries = g_hash_table_new_full (g_bytes_hash, g_bytes_equal,
                                                              (GDestroyNotify) g_bytes_unref,
                                                              (GDestroyNotify) g_bytes_unref);
            return &fake_maps[i];
        }
    return NULL;
}

static int
fake_err (int e)
{
    errno = e;
    return -e;
}

static int
fake_map_update (int fd, const void *key, const void *value, __u64 flags)
{
    FakeMap *m = fake_map (fd);

    if (!m)
        return fake_err (EBADF);
    if (failing ("update"))
        return fake_err (EPERM);
    g_hash_table_replace (m->entries, g_bytes_new (key, m->key_size), g_bytes_new (value, m->value_size));
    return 0;
}

static int
fake_map_delete (int fd, const void *key)
{
    FakeMap *m = fake_map (fd);
    g_autoptr (GBytes) k = NULL;

    if (!m)
        return fake_err (EBADF);
    k = g_bytes_new (key, m->key_size);
    return g_hash_table_remove (m->entries, k) ? 0 : fake_err (ENOENT);
}

static gboolean
fake_map_get (int fd, const void *key, void *value)
{
    FakeMap *m = fake_map (fd);
    g_autoptr (GBytes) k = g_bytes_new (key, m->key_size);
    GBytes *v = g_hash_table_lookup (m->entries, k);

    if (!v)
        return FALSE;
    memcpy (value, g_bytes_get_data (v, NULL), m->value_size);
    return TRUE;
}

static int
fake_map_lookup_and_delete (int fd, const void *key, void *value)
{
    if (!fake_map (fd))
        return fake_err (EBADF);
    if (failing ("lookup") || !fake_map_get (fd, key, value))
        return fake_err (ENOENT);
    return fake_map_delete (fd, key);
}

static gint
bytes_order (gconstpointer a, gconstpointer b)
{
    return g_bytes_compare (*(GBytes **) a, *(GBytes **) b);
}

/* As the kernel: NULL or a key not in the map gives the first key */
static int
fake_map_next_key (int fd, const void *key, void *next)
{
    FakeMap *m = fake_map (fd);
    g_autoptr (GPtrArray) keys = NULL;
    g_autoptr (GBytes) k = NULL;
    guint start = 0;

    if (!m)
        return fake_err (EBADF);
    keys = g_hash_table_get_keys_as_ptr_array (m->entries);
    g_ptr_array_sort (keys, bytes_order);
    if (key) {
        k = g_bytes_new (key, m->key_size);
        for (guint i = 0; i < keys->len; i++)
            if (g_bytes_equal (keys->pdata[i], k)) {
                start = i + 1;
                break;
            }
    }
    if (start >= keys->len)
        return fake_err (ENOENT);
    memcpy (next, g_bytes_get_data (keys->pdata[start], NULL), m->key_size);
    return 0;
}

struct fake_prog {
    const char *name;
};
static struct fake_prog fake_progs[] = { { "appaccess_dev" }, { "appaccess_sock_create" } };
static int fake_obj;
static int links_destroyed;

static struct bpf_object *
fake_object_open (const char *file, const struct bpf_object_open_opts *opts)
{
    if (failing ("open") || access (file, R_OK) != 0) {
        errno = ENOENT;
        return NULL;
    }
    return (struct bpf_object *) &fake_obj;
}

static int
fake_object_load (struct bpf_object *obj)
{
    return failing ("load") ? fake_err (EPERM) : 0;
}

static int
fake_find_map_fd (const struct bpf_object *obj, const char *name)
{
    static const char *const names[] = { "devices", "cgroups", "counts", "events" };
    g_autofree char *key = g_strconcat ("map:", name, NULL);

    if (failing (key))
        return fake_err (ENOENT);
    for (int i = 0; i < 4; i++)
        if (strcmp (names[i], name) == 0)
            return FD_DEVICES + i;
    return fake_err (ENOENT);
}

static struct bpf_program *
fake_next_program (const struct bpf_object *obj, struct bpf_program *prog)
{
    struct fake_prog *p = (struct fake_prog *) prog;

    if (!p)
        return (struct bpf_program *) &fake_progs[0];
    if (p + 1 < fake_progs + G_N_ELEMENTS (fake_progs))
        return (struct bpf_program *) (p + 1);
    return NULL;
}

static const char *
fake_program_name (const struct bpf_program *prog)
{
    return ((const struct fake_prog *) prog)->name;
}

static struct bpf_link *
fake_attach_cgroup (const struct bpf_program *prog, int cgroup_fd)
{
    g_autofree char *key = g_strconcat ("attach:", fake_program_name (prog), NULL);
    struct stat st;

    /* The daemon hands over a directory it opened */
    if (fstat (cgroup_fd, &st) != 0 || !S_ISDIR (st.st_mode)) {
        errno = EBADF;
        return NULL;
    }
    if (failing (key)) {
        errno = EPERM;
        return NULL;
    }
    return (struct bpf_link *) g_strdup (fake_program_name (prog));
}

static int
fake_link_destroy (struct bpf_link *link)
{
    g_free (link);
    links_destroyed++;
    return 0;
}

typedef struct {
    ring_buffer_sample_fn sample;
    void *ctx;
    int pipe[2];
    GQueue queued; /* struct aa_event, g_free'd */
} FakeRing;

static struct ring_buffer *
fake_ring_new (int map_fd, ring_buffer_sample_fn sample, void *ctx, const struct ring_buffer_opts *opts)
{
    FakeRing *r;

    if (map_fd != FD_EVENTS || failing ("ringbuf")) {
        errno = EINVAL;
        return NULL;
    }
    r = g_new0 (FakeRing, 1);
    r->sample = sample;
    r->ctx = ctx;
    /* Never readable: the main loop only waits on it */
    if (pipe (r->pipe) != 0)
        abort ();
    return (struct ring_buffer *) r;
}

static int
fake_ring_epoll_fd (const struct ring_buffer *rb)
{
    return ((const FakeRing *) rb)->pipe[0];
}

static int
fake_ring_consume (struct ring_buffer *rb)
{
    FakeRing *r = (FakeRing *) rb;
    int n = 0;
    struct aa_event *ev;

    while ((ev = g_queue_pop_head (&r->queued))) {
        r->sample (r->ctx, ev, sizeof (*ev));
        g_free (ev);
        n++;
    }
    return n;
}

#define g_get_monotonic_time fake_monotonic_time
#define clock_gettime(c, ts) fake_clock_gettime (c, ts)
#define time(t) fake_time (t)
#undef g_dbus_connection_call_sync
#define g_dbus_connection_call_sync aa_call_sync
#undef g_dbus_connection_emit_signal
#define g_dbus_connection_emit_signal aa_emit_signal
#define g_bus_own_name fake_bus_own_name
#define g_dbus_connection_register_object fake_register_object
#define readlink fake_readlink
#define open_by_handle_at fake_open_by_handle_at
#define getpwuid fake_getpwuid
#define fchown fake_fchown
#define bpf_map_update_elem fake_map_update
#define bpf_map_delete_elem fake_map_delete
#define bpf_map_lookup_and_delete_elem fake_map_lookup_and_delete
#define bpf_map_get_next_key fake_map_next_key
#define bpf_object__open_file fake_object_open
#define bpf_object__load fake_object_load
#define bpf_object__find_map_fd_by_name fake_find_map_fd
#define bpf_object__next_program fake_next_program
#define bpf_program__name fake_program_name
#define bpf_program__attach_cgroup fake_attach_cgroup
#define bpf_link__destroy fake_link_destroy
#define ring_buffer__new fake_ring_new
#define ring_buffer__epoll_fd fake_ring_epoll_fd
#define ring_buffer__consume fake_ring_consume

#define main appaccessd_main
#include AA_DAEMON_SOURCE
#undef main
#undef readlink
#undef time

/* ---- Command loop ---- */

static gboolean
send_term (gpointer data)
{
    raise (SIGTERM);
    return G_SOURCE_REMOVE;
}

static void
init_state (void)
{
    /* As main() does before it loads the programs */
    aa.state_file = g_strdup (g_getenv ("AA_STATE") ? g_getenv ("AA_STATE") : DEFAULT_STATE);
    aa.watches = g_hash_table_new_full (NULL, NULL, NULL, g_free);
    aa.cgroups = g_hash_table_new_full (g_int64_hash, g_int64_equal, NULL, (GDestroyNotify) cgroup_free);
    aa.by_path = g_hash_table_new_full (g_str_hash, g_str_equal, g_free, g_free);
    aa.apps = g_hash_table_new_full (g_str_hash, g_str_equal, g_free, g_free);
    aa.devices = g_hash_table_new (NULL, NULL);
    aa.device_dirs = g_hash_table_new_full (NULL, NULL, NULL, g_free);
    aa.links = g_hash_table_new_full (g_str_hash, g_str_equal, g_free, (GDestroyNotify) links_free);
    aa.to_attach = g_ptr_array_new_with_free_func (g_free);
    aa.loop = g_main_loop_new (NULL, FALSE);
    aa.cgroup_fd = open (CGROUP_ROOT, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    aa.inotify_fd = inotify_init1 (IN_NONBLOCK | IN_CLOEXEC);
    aa.dev_wd = -100;
    aa.obj = (struct bpf_object *) &fake_obj;
    aa.devices_fd = FD_DEVICES;
    aa.cgroups_fd = FD_CGROUPS;
    aa.counts_fd = FD_COUNTS;
    aa.bus = (GDBusConnection *) &fake_conn;
}

static gint
str_order (gconstpointer a, gconstpointer b)
{
    return strcmp (*(const char **) a, *(const char **) b);
}

static guint64
id_of (const char *path_or_id)
{
    guint64 *id;

    if (path_or_id[0] == '#')
        return g_ascii_strtoull (path_or_id + 1, NULL, 10);
    id = g_hash_table_lookup (aa.by_path, path_or_id);
    return id ? *id : 0;
}

static void
print_cgroups (void)
{
    g_autoptr (GPtrArray) list = g_ptr_array_new_with_free_func (g_free);
    GHashTableIter iter;
    Cgroup *cg;

    g_hash_table_iter_init (&iter, aa.cgroups);
    while (g_hash_table_iter_next (&iter, NULL, (gpointer *) &cg)) {
        struct aa_cgroup v;
        g_autofree char *map = NULL;

        if (fake_map_get (FD_CGROUPS, &cg->id, &v))
            map = g_strdup_printf ("%u,%u,%u", v.verdict[0], v.verdict[1], v.verdict[2]);
        g_ptr_array_add (list, g_strdup_printf ("%s app=%s uid=%d map=%s once=%d,%d,%d", cg->path,
                                                cg->app ? cg->app : "-", (int) cg->uid, map ? map : "-",
                                                cg->once[0], cg->once[1], cg->once[2]));
    }
    g_ptr_array_sort (list, str_order);
    for (guint i = 0; i < list->len; i++)
        printf ("%s%s", i ? " | " : "", (char *) list->pdata[i]);
    puts (list->len ? "" : "-");
}

static void
print_app (const char *app)
{
    App *a = g_hash_table_lookup (aa.apps, app);

    if (!a) {
        puts ("-");
        return;
    }
    for (int i = 0; i < AA_RES_MAX; i++)
        printf ("%s%s policy=%u set=%d allowed=%" G_GUINT64_FORMAT " denied=%" G_GUINT64_FORMAT
                " last=%" G_GINT64_FORMAT ",%" G_GINT64_FORMAT,
                i ? " | " : "", res_names[i], a->policy[i], a->set[i], a->allowed[i], a->denied[i],
                a->last_allowed[i], a->last_denied[i]);
    puts ("");
}

static void
print_sorted_keys (GHashTable *table)
{
    g_autoptr (GPtrArray) keys = g_hash_table_get_keys_as_ptr_array (table);

    g_ptr_array_sort (keys, str_order);
    for (guint i = 0; i < keys->len; i++)
        printf ("%s%s", i ? " " : "", (char *) keys->pdata[i]);
    puts (keys->len ? "" : "-");
}

static void
print_devices_map (void)
{
    FakeMap *m = fake_map (FD_DEVICES);
    g_autoptr (GPtrArray) list = g_ptr_array_new_with_free_func (g_free);
    GHashTableIter iter;
    gpointer k, v;

    g_hash_table_iter_init (&iter, m->entries);
    while (g_hash_table_iter_next (&iter, &k, &v)) {
        guint32 dev = *(const guint32 *) g_bytes_get_data (k, NULL);
        guint32 res = *(const guint32 *) g_bytes_get_data (v, NULL);

        g_ptr_array_add (list, g_strdup_printf ("%u:%u=%s", dev >> 20, dev & 0xfffff,
                                                res < AA_RES_MAX ? res_names[res] : "?"));
    }
    g_ptr_array_sort (list, str_order);
    for (guint i = 0; i < list->len; i++)
        printf ("%s%s", i ? " " : "", (char *) list->pdata[i]);
    puts (list->len ? "" : "-");
}

static void
put_count (guint64 id, const char *res, long long allowed, long long denied, long long last_a,
           long long last_d)
{
    struct aa_count_key key = { .cgroup_id = id, .res = (guint32) g_ascii_strtoull (res, NULL, 10) };
    struct aa_count value = { (__u64) allowed, (__u64) denied, (__u64) last_a * 1000000000ULL,
                              (__u64) last_d * 1000000000ULL };
    int r = res_from_name (res);

    if (r >= 0)
        key.res = r;
    fake_map_update (FD_COUNTS, &key, &value, BPF_ANY);
    puts ("ok");
}

static void
fill_event (struct aa_event *ev, long long id, const char *res, long long kind, long long verdict)
{
    int r = res_from_name (res);

    memset (ev, 0, sizeof (*ev));
    ev->cgroup_id = (__u64) id;
    ev->uid = 1000;
    ev->res = r >= 0 ? (guint32) r : (guint32) g_ascii_strtoull (res, NULL, 10);
    ev->kind = (__u32) kind;
    ev->verdict = (__u32) verdict;
    memcpy (ev->comm, "dsec-comm-name16", 16);
}

int
main (int argc, char **argv)
{
    static char line[1 << 16];

    setvbuf (stdout, NULL, _IOLBF, 0);
    if (!realpath (".", test_root))
        return 3;
    if (g_getenv ("AA_BPF_FAIL"))
        g_strlcpy (bpf_fail, g_getenv ("AA_BPF_FAIL"), sizeof bpf_fail);
    if (argc > 1 && strcmp (argv[1], "main") == 0) {
        int r;

        if (g_getenv ("AA_TERM_MS"))
            g_timeout_add (atoi (g_getenv ("AA_TERM_MS")), send_term, NULL);
        r = appaccessd_main (argc - 1, argv + 1);
        printf ("exit %d\n", r);
        return 0;
    }
    init_state ();

    while (fgets (line, sizeof line, stdin)) {
        static char a[4096], b[4096];
        long long n1, n2, n3, n4, n5;
        int got;

        line[strcspn (line, "\n")] = '\0';
        if (fb_command (line))
            continue;
        if (sscanf (line, "pid %lld", &n1) == 1) {
            fake_pid = n1;
            puts ("ok");
        } else if (sscanf (line, "creds %15s", a) == 1) {
            creds_fail = strcmp (a, "fail") == 0;
            puts ("ok");
        } else if (!strcmp (line, "noinotify")) {
            close (aa.inotify_fd);
            aa.inotify_fd = -1;
            puts ("ok");
        } else if (sscanf (line, "clock %lld %lld", &n1, &n2) == 2) {
            fake_mono_s = n1;
            fake_unix_s = n2;
            puts ("ok");
        } else if (sscanf (line, "bpf fail %255s", a) == 1) {
            g_strlcpy (bpf_fail, strcmp (a, "-") ? a : "", sizeof bpf_fail);
            puts ("ok");
        } else if (!strncmp (line, "leaf ", 5)) {
            g_autofree char *app = app_from_leaf (line + 5);

            puts (app ? app : "-");
        } else if (!strncmp (line, "valid ", 6) || !strcmp (line, "valid")) {
            puts (app_id_valid (line[5] ? line + 6 : "") ? "yes" : "no");
        } else if (!strncmp (line, "level ", 6)) {
            printf ("%d\n", cgroup_level (line + 6));
        } else if (!strncmp (line, "res ", 4)) {
            printf ("%d\n", res_from_name (line + 4));
        } else if (!strncmp (line, "scan ", 5)) {
            cgroup_scan (line + 5);
            puts ("ok");
        } else if (!strncmp (line, "add ", 4)) {
            cgroup_add (line + 4);
            puts ("ok");
        } else if (!strncmp (line, "remove ", 7)) {
            cgroup_remove (line + 7);
            puts ("ok");
        } else if (!strcmp (line, "attach")) {
            puts (bpf_attach () ? "ok" : "fail");
        } else if (!strcmp (line, "cgroups")) {
            print_cgroups ();
        } else if (!strcmp (line, "links")) {
            g_autoptr (GPtrArray) keys = g_hash_table_get_keys_as_ptr_array (aa.links);

            g_ptr_array_sort (keys, str_order);
            for (guint i = 0; i < keys->len; i++)
                printf ("%s %u | ", (char *) keys->pdata[i],
                        ((GPtrArray *) g_hash_table_lookup (aa.links, keys->pdata[i]))->len);
            printf ("to_attach %u\n", aa.to_attach->len);
        } else if (!strcmp (line, "destroyed")) {
            printf ("%d\n", links_destroyed);
        } else if (sscanf (line, "countid %lld %4095s %lld %lld %lld %lld", &n5, b, &n1, &n2, &n3, &n4) == 6) {
            put_count ((guint64) n5, b, n1, n2, n3, n4);
        } else if (sscanf (line, "count %4095s %4095s %lld %lld %lld %lld", a, b, &n1, &n2, &n3, &n4) == 6) {
            put_count (id_of (a), b, n1, n2, n3, n4);
        } else if (!strcmp (line, "harvest")) {
            harvest (0);
            puts ("ok");
        } else if (sscanf (line, "harvest %4095s", a) == 1) {
            harvest (id_of (a));
            puts ("ok");
        } else if (!strcmp (line, "apps")) {
            print_sorted_keys (aa.apps);
        } else if (sscanf (line, "app %4095s", a) == 1) {
            print_app (a);
        } else if (!strcmp (line, "load")) {
            state_load ();
            printf ("dirty %d\n", aa.dirty);
        } else if (!strcmp (line, "save")) {
            state_save ();
            printf ("dirty %d\n", aa.dirty);
        } else if (!strcmp (line, "timer harvest")) {
            on_harvest_timer (NULL);
            printf ("dirty %d\n", aa.dirty);
        } else if (!strcmp (line, "timer save")) {
            on_save_timer (NULL);
            printf ("dirty %d\n", aa.dirty);
        } else if (sscanf (line, "testdev %4095s", a) == 1) {
            g_strfreev (aa.test_devices);
            aa.test_devices = strcmp (a, "-") ? g_strsplit (a, ",", -1) : NULL;
            puts ("ok");
        } else if (!strcmp (line, "devices")) {
            devices_scan ();
            print_devices_map ();
        } else if (!strcmp (line, "reannounce")) {
            keys_reannounce ();
            puts ("ok");
        } else if (sscanf (line, "devwatch %4095s", a) == 1) {
            aa.dev_wd = inotify_add_watch (aa.inotify_fd, a, IN_CREATE | IN_DELETE);
            puts (aa.dev_wd >= 0 ? "ok" : "fail");
        } else if (!strcmp (line, "inotify")) {
            on_inotify (aa.inotify_fd, G_IO_IN, NULL);
            puts ("ok");
        } else if ((got = sscanf (line, "event %lld %4095s %lld %lld %lld", &n1, a, &n2, &n3, &n4)) >= 4) {
            struct aa_event ev;

            fill_event (&ev, n1, a, n2, n3);
            on_event (NULL, &ev, got == 5 ? (size_t) n4 : sizeof (ev));
            puts ("ok");
        } else if (sscanf (line, "ringbuf %lld %4095s %lld %lld", &n1, a, &n2, &n3) == 4) {
            struct aa_event *ev = g_new (struct aa_event, 1);

            if (!aa.rb) {
                puts ("no ring buffer");
                g_free (ev);
                continue;
            }
            fill_event (ev, n1, a, n2, n3);
            g_queue_push_tail (&((FakeRing *) aa.rb)->queued, ev);
            on_ringbuf (fake_ring_epoll_fd (aa.rb), G_IO_IN, NULL);
            puts ("ok");
        } else if (sscanf (line, "pidapp %lld", &n1) == 1) {
            const char *app = app_of_pid ((guint32) n1);

            puts (app ? app : "-");
        } else if (sscanf (line, "call %4095s %4095[^\n]", a, b) >= 1) {
            FakeInv inv = { 0 };
            GVariant *params = b[0] ? g_variant_parse (NULL, b, NULL, NULL, NULL) : g_variant_new ("()");

            if (!params) {
                printf ("<bad parameters: %s>\n", b);
                b[0] = '\0';
                continue;
            }
            fake_inv_reset (&inv, a, params);
            on_method_call (aa.bus, ":1.9", OBJECT_PATH, BUS_NAME, a, inv.params,
                            (GDBusMethodInvocation *) &inv, NULL);
            fake_inv_print (&inv);
            fake_inv_reset (&inv, NULL, NULL);
            b[0] = '\0';
        } else if (!strcmp (line, "signals")) {
            puts (signals_log && signals_log->len ? signals_log->str : "-");
            if (signals_log)
                g_string_truncate (signals_log, 0);
        } else if (!strcmp (line, "maps")) {
            printf ("devices=%u cgroups=%u counts=%u\n", g_hash_table_size (fake_map (FD_DEVICES)->entries),
                    g_hash_table_size (fake_map (FD_CGROUPS)->entries),
                    g_hash_table_size (fake_map (FD_COUNTS)->entries));
        } else if (!strcmp (line, "devmap")) {
            print_devices_map ();
        } else if (sscanf (line, "mkdir %4095s", a) == 1) {
            puts (g_mkdir_with_parents (a, 0755) == 0 ? "ok" : "fail");
        } else if (sscanf (line, "rmdir %4095s", a) == 1) {
            puts (rmdir (a) == 0 ? "ok" : "fail");
        } else if (sscanf (line, "unlink %4095s", a) == 1) {
            puts (unlink (a) == 0 ? "ok" : "fail");
        } else if (sscanf (line, "touch %4095s", a) == 1) {
            puts (g_file_set_contents (a, "", 0, NULL) ? "ok" : "fail");
        } else if (!strcmp (line, "busoff")) {
            aa.bus = NULL;
            puts ("ok");
        } else if (!strcmp (line, "mic")) {
            GHashTableIter iter;
            gpointer key;
            g_autoptr (GPtrArray) list = g_ptr_array_new_with_free_func (g_free);

            last_chown_gid = 0;
            mic_publish ();
            g_hash_table_iter_init (&iter, aa.mic_uids);
            while (g_hash_table_iter_next (&iter, &key, NULL))
                g_ptr_array_add (list, g_strdup_printf ("%u", GPOINTER_TO_UINT (key)));
            g_ptr_array_sort (list, str_order);
            for (guint i = 0; i < list->len; i++)
                printf ("%s%s", i ? " " : "", (char *) list->pdata[i]);
            printf ("%s gid=%u\n", list->len ? "" : "-", (unsigned) last_chown_gid);
        } else if (!strcmp (line, "micidle")) {
            printf ("idle %d\n", aa.mic_idle != 0);
            while (g_main_context_iteration (NULL, FALSE))
                ;
        } else if (sscanf (line, "nopasswd %255s", a) == 1) {
            g_strlcpy (no_users, strcmp (a, "-") ? a : "", sizeof no_users);
            puts ("ok");
        } else {
            printf ("<unknown command: %.60s>\n", line);
        }
    }
    return 0;
}
