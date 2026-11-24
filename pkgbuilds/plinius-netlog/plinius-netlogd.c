/*
 * plinius-netlogd: records which app connects where, for the "Connections" page of GNOME
 * Settings (Device Security).
 *
 * - BPF programs at the root of the cgroup hierarchy (netlog.bpf.c) report every outgoing
 *   connection with the cgroup of the process. The cgroup names the app: on systemd an app
 *   runs in a scope or service named after its application id (app-gnome-org.gnome.Maps-123.scope),
 *   a system service in its own unit.
 * - A packet socket reads incoming DNS answers (UDP source port 53), so an address can be shown
 *   with the name the app asked for instead of a CDN's reverse name.
 * - Connections are counted per minute and per hour in SQLite (/var/lib/plinius-netlog): minutes
 *   for the last two hours (the 15/30/60 minute views), hours for eight days (day and week).
 *   Loopback is never recorded. Writes are batched once a minute.
 * - D-Bus (system bus) org.plinius.NetLog1 at /org/plinius/NetLog1:
 *     Summary (u seconds) -> (u internet_connections, u internet_destinations, u internet_apps,
 *                             u lan_connections)
 *     Query (u seconds) -> a(ssuqssuuxx): app, executable, protocol, port, address, host name,
 *                             scope (1 LAN, 2 internet), count, first seen, last seen (unix time)
 *   Only root and regular users (uid >= 1000) with a session at logind (active or online, also
 *   over SSH) may call them: not nobody, not DynamicUser services, not a lingering user.
 *   Root and administrators (group wheel) see every connection; any other user only those of
 *   processes running as itself, not those of the system's services or of other accounts
 *   (attack review 2026-11-18, L15).
 *
 * Nothing is ever blocked; the daemon only watches.
 *
 * SPDX-License-Identifier: MIT
 */

#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/filter.h>
#include <linux/if_packet.h>
#include <net/ethernet.h>
#include <netinet/in.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#include <bpf/libbpf.h>
#include <gio/gio.h>
#include <glib-unix.h>
#include <sqlite3.h>

#include "netlog.h"

#define DEFAULT_BPF "/usr/lib/plinius-netlog/netlog.bpf.o"
#define DEFAULT_DB  "/var/lib/plinius-netlog/netlog.db"
#define CGROUP_ROOT "/sys/fs/cgroup"
#define BUS_NAME    "org.plinius.NetLog1"
#define OBJECT_PATH "/org/plinius/NetLog1"

/* Kept for the two views: minutes for 2 hours, hours for 8 days */
#define MINUTE_KEEP (2 * 3600)
#define HOUR_KEEP   (8 * 86400)
#define FLUSH_EVERY 60
/* Bounds of the in-memory caches; when full they start over */
#define MAX_CACHE   8192

/* Where a destination is */
enum { SCOPE_LOOPBACK = 0, SCOPE_LAN = 1, SCOPE_INTERNET = 2 };

/* One batched row: connections of one app/executable to one destination in one minute */
typedef struct {
    gint64 minute;
    char  *app;
    char  *exe;
    guint  uid;
    guint  proto;
    char  *addr;
    guint  port;
    char  *host;
    guint  scope;
    guint  count;
} Pending;

static struct {
    sqlite3        *db;
    GHashTable     *pending;   /* key string -> Pending */
    GHashTable     *apps;      /* cgroup id (gint64*) -> app string */
    GHashTable     *names;     /* address string -> host name */
    int             cgroup_fd;
    struct ring_buffer *rb;
    GDBusNodeInfo  *introspection;
    GMainLoop      *loop;
} nl;

static void
pending_free (Pending *p)
{
    g_free (p->app);
    g_free (p->exe);
    g_free (p->addr);
    g_free (p->host);
    g_free (p);
}

/* ----- Addresses ----- */

/* Turns an event's address into text and its scope; IPv4-mapped IPv6 counts as IPv4 */
static guint
classify (const struct netlog_event *ev, char *text, size_t len)
{
    const guint8 *b = (const guint8 *) ev->addr;
    guint8 v4[4];

    if (ev->family == 6) {
        static const guint8 mapped[12] = { 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0xff, 0xff };
        if (memcmp (b, mapped, 12) == 0) {
            memcpy (v4, b + 12, 4);
        } else {
            inet_ntop (AF_INET6, b, text, len);
            if (IN6_IS_ADDR_LOOPBACK ((const struct in6_addr *) b) ||
                IN6_IS_ADDR_UNSPECIFIED ((const struct in6_addr *) b))
                return SCOPE_LOOPBACK;
            /* fc00::/7 unique local, fe80::/10 link local, ff00::/8 multicast */
            if ((b[0] & 0xfe) == 0xfc || (b[0] == 0xfe && (b[1] & 0xc0) == 0x80) || b[0] == 0xff)
                return SCOPE_LAN;
            return SCOPE_INTERNET;
        }
    } else {
        memcpy (v4, b, 4);
    }
    inet_ntop (AF_INET, v4, text, len);
    if (v4[0] == 127 || v4[0] == 0)
        return SCOPE_LOOPBACK;
    if (v4[0] == 10 || (v4[0] == 172 && (v4[1] & 0xf0) == 16) || (v4[0] == 192 && v4[1] == 168) ||
        (v4[0] == 169 && v4[1] == 254) ||
        /* carrier-grade NAT: inside the mobile operator's network */
        (v4[0] == 100 && (v4[1] & 0xc0) == 64) ||
        /* multicast and broadcast */
        v4[0] >= 224)
        return SCOPE_LAN;
    return SCOPE_INTERNET;
}

/* ----- Apps from cgroups ----- */

/* Undoes systemd's unit name escaping (\x2d for '-'); NULL for an escaped NUL (it would cut the
 * name short) */
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

/* @prefix and the unescaped @escaped; "" for a name with an escaped NUL */
static char *
unescaped_key (const char *prefix, const char *escaped)
{
    g_autofree char *name = unit_unescape (escaped);

    return name ? g_strconcat (prefix, name, NULL) : g_strdup ("");
}

/*
 * The app behind a cgroup path, as a short key for GNOME Settings:
 *   "org.gnome.Maps"          an app (XDG application id, also for Flatpak and D-Bus activation)
 *   "service:NetworkManager"  a system or user service
 *   "android"                 the Android container (vendor services)
 *   "session"                 a login session (shell, ssh), the executable tells more
 * Unit names follow systemd's desktop environment conventions:
 *   app-[<launcher>-]<ApplicationID>-<RANDOM>.scope, app-[<launcher>-]<ApplicationID>[@<RANDOM>].service,
 *   dbus-:<bus id>-<ApplicationID>@<n>.service (D-Bus activated)
 * The name is split while it is still escaped: systemd writes a dash inside the application id
 * as \x2d, so only a literal dash separates the parts (as plinius-appaccessd).
 */
static char *
app_from_cgroup (const char *path)
{
    g_autofree char *leaf = NULL;
    const char *slash = strrchr (path, '/');
    char *id, *p;

    if (strstr (path, "lxc.payload.android") || strstr (path, "lxc.monitor.android"))
        return g_strdup ("android");
    leaf = g_strdup (slash ? slash + 1 : path);

    if (g_str_has_prefix (leaf, "session-") && g_str_has_suffix (leaf, ".scope"))
        return g_strdup ("session");

    if (g_str_has_prefix (leaf, "dbus-:") && g_str_has_suffix (leaf, ".service")) {
        /* dbus-:1.2-org.gnome.Weather@0.service */
        id = strchr (leaf + 6, '-');
        if (id) {
            id++;
            if ((p = strchr (id, '@')) || (p = strstr (id, ".service")))
                *p = '\0';
            return unescaped_key ("", id);
        }
    }

    if (g_str_has_prefix (leaf, "app-")) {
        id = leaf + 4;
        if (g_str_has_suffix (id, ".scope")) {
            id[strlen (id) - 6] = '\0';
            /* the random part after the last '-' */
            if ((p = strrchr (id, '-')))
                *p = '\0';
        } else if (g_str_has_suffix (id, ".service")) {
            id[strlen (id) - 8] = '\0';
            if ((p = strchr (id, '@')))
                *p = '\0';
        }
        /* A launcher prefix ("gnome-", "flatpak-") has no dots, an application id usually has */
        p = strchr (id, '-');
        if (p && memchr (id, '.', p - id) == NULL && p[1])
            id = p + 1;
        return unescaped_key ("", id);
    }

    if (g_str_has_suffix (leaf, ".service")) {
        leaf[strlen (leaf) - 8] = '\0';
        if ((p = strchr (leaf, '@')) && p[1] == '\0')
            *p = '\0';
        return unescaped_key ("service:", leaf);
    }
    return unescaped_key ("", leaf);
}

/* The cgroup path of cgroup @id (the inode number of its directory), via a kernfs file handle;
 * NULL if the cgroup is gone */
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
    handle.fh.handle_type = 0xfe;  /* FILEID_KERNFS: the handle is the 64-bit kernfs id */
    memcpy (handle.fh.f_handle, &id, sizeof (id));
    fd = open_by_handle_at (nl.cgroup_fd, &handle.fh, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
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

/* The cgroup path of process @pid from /proc (fallback when the handle does not resolve) */
static char *
cgroup_path_by_pid (guint pid)
{
    g_autofree char *file = g_strdup_printf ("/proc/%u/cgroup", pid);
    g_autofree char *contents = NULL;
    char *line;

    if (!g_file_get_contents (file, &contents, NULL, NULL))
        return NULL;
    /* cgroup v2: one line "0::/path" */
    for (line = strtok (contents, "\n"); line; line = strtok (NULL, "\n"))
        if (g_str_has_prefix (line, "0::"))
            return g_strdup (line + 3);
    return NULL;
}

static const char *
app_of (const struct netlog_event *ev)
{
    gint64 key = (gint64) ev->cgroup_id;
    char *path, *app;

    app = g_hash_table_lookup (nl.apps, &key);
    if (app)
        return app;
    path = cgroup_path_by_id (ev->cgroup_id);
    if (!path)
        path = cgroup_path_by_pid (ev->pid);
    app = path ? app_from_cgroup (path) : g_strdup ("unknown");
    g_free (path);
    if (g_hash_table_size (nl.apps) >= MAX_CACHE)
        g_hash_table_remove_all (nl.apps);
    g_hash_table_insert (nl.apps, g_memdup2 (&key, sizeof (key)), app);
    return app;
}

/* The executable's file name, or the thread's command name once the process is gone */
static char *
exe_of (const struct netlog_event *ev)
{
    char link[64], target[PATH_MAX];
    ssize_t n;

    g_snprintf (link, sizeof (link), "/proc/%u/exe", ev->pid);
    n = readlink (link, target, sizeof (target) - 1);
    if (n > 0) {
        target[n] = '\0';
        /* " (deleted)" after an update replaced the file */
        if (g_str_has_suffix (target, " (deleted)"))
            target[n - 10] = '\0';
        return g_path_get_basename (target);
    }
    return g_strndup (ev->comm, sizeof (ev->comm));
}

/* ----- Events ----- */

static int
on_event (void *ctx, void *data, size_t size)
{
    const struct netlog_event *ev = data;
    char addr[INET6_ADDRSTRLEN];
    g_autofree char *exe = NULL;
    g_autofree char *key = NULL;
    const char *app, *host;
    gint64 minute;
    guint scope;
    Pending *p;

    if (size < sizeof (*ev))
        return 0;
    scope = classify (ev, addr, sizeof (addr));
    if (scope == SCOPE_LOOPBACK)
        return 0;

    app = app_of (ev);
    exe = exe_of (ev);
    host = g_hash_table_lookup (nl.names, addr);
    minute = g_get_real_time () / G_USEC_PER_SEC / 60 * 60;

    /* With the uid: the same app of two accounts are two rows, each shown to its own account */
    key = g_strdup_printf ("%" G_GINT64_FORMAT "\t%u\t%s\t%s\t%u\t%s\t%u", minute, ev->uid, app, exe, ev->proto,
                           addr, ev->port);
    p = g_hash_table_lookup (nl.pending, key);
    if (!p) {
        p = g_new0 (Pending, 1);
        p->minute = minute;
        p->app = g_strdup (app);
        p->exe = g_strdup (exe);
        p->uid = ev->uid;
        p->proto = ev->proto;
        p->addr = g_strdup (addr);
        p->port = ev->port;
        p->scope = scope;
        g_hash_table_insert (nl.pending, g_steal_pointer (&key), p);
    }
    if (host && !p->host)
        p->host = g_strdup (host);
    p->count++;
    return 0;
}

static gboolean
on_ringbuf (int fd, GIOCondition condition, gpointer user_data)
{
    ring_buffer__consume (nl.rb);
    return G_SOURCE_CONTINUE;
}

/* ----- DNS answers ----- */

/* Reads a (possibly compressed) name at @off of DNS message @msg into @out; returns the offset
 * after the name in the message itself, or -1 if it is malformed */
static int
dns_name (const guint8 *msg, int len, int off, char *out, size_t outlen)
{
    int end = -1, jumps = 0;
    size_t used = 0;

    while (off < len) {
        guint8 l = msg[off];
        if (l == 0) {
            out[used] = '\0';
            return end >= 0 ? end : off + 1;
        }
        if ((l & 0xc0) == 0xc0) {
            if (off + 1 >= len || ++jumps > 16)
                return -1;
            if (end < 0)
                end = off + 2;
            off = ((l & 0x3f) << 8) | msg[off + 1];
            continue;
        }
        if (l > 63 || off + 1 + l > len || used + l + 2 > outlen)
            return -1;
        if (used)
            out[used++] = '.';
        memcpy (out + used, msg + off + 1, l);
        used += l;
        off += 1 + l;
    }
    return -1;
}

/* Notes address -> asked name for each A/AAAA record of a DNS answer */
static void
dns_parse (const guint8 *msg, int len)
{
    char qname[256], name[256], addr[INET6_ADDRSTRLEN];
    int off, qd, an;

    if (len < 12 || !(msg[2] & 0x80))  /* not a response */
        return;
    qd = msg[4] << 8 | msg[5];
    an = msg[6] << 8 | msg[7];
    if (qd != 1)
        return;
    off = dns_name (msg, len, 12, qname, sizeof (qname));
    if (off < 0 || off + 4 > len)
        return;
    off += 4;
    for (int i = 0; i < an && off < len; i++) {
        guint type, rdlen;

        off = dns_name (msg, len, off, name, sizeof (name));
        if (off < 0 || off + 10 > len)
            return;
        type = msg[off] << 8 | msg[off + 1];
        rdlen = msg[off + 8] << 8 | msg[off + 9];
        off += 10;
        if (off + (int) rdlen > len)
            return;
        /* The name the app asked for, not the CNAME chain's end */
        if ((type == 1 && rdlen == 4) || (type == 28 && rdlen == 16)) {
            inet_ntop (type == 1 ? AF_INET : AF_INET6, msg + off, addr, sizeof (addr));
            if (g_hash_table_size (nl.names) >= MAX_CACHE)
                g_hash_table_remove_all (nl.names);
            g_hash_table_replace (nl.names, g_strdup (addr), g_ascii_strdown (qname, -1));
        }
        off += rdlen;
    }
}

static gboolean
on_dns (int fd, GIOCondition condition, gpointer user_data)
{
    guint8 buf[4096];
    struct sockaddr_ll from;
    socklen_t fromlen = sizeof (from);
    ssize_t n;
    int hl;

    n = recvfrom (fd, buf, sizeof (buf), MSG_DONTWAIT, (struct sockaddr *) &from, &fromlen);
    if (n <= 0 || from.sll_pkttype == PACKET_OUTGOING)
        return G_SOURCE_CONTINUE;
    /* The filter let through only UDP from port 53; skip the IP and UDP headers */
    if ((buf[0] >> 4) == 4)
        hl = (buf[0] & 0x0f) * 4;
    else
        hl = 40;
    if (n > hl + 8)
        dns_parse (buf + hl + 8, n - hl - 8);
    return G_SOURCE_CONTINUE;
}

/* A packet socket on all interfaces that sees only UDP packets from port 53 (IPv4 and IPv6
 * without extension headers). SOCK_DGRAM: the data starts at the IP header on every link type
 * (Wi-Fi, the modem's raw IP interfaces). */
static int
dns_socket (void)
{
    static struct sock_filter code[] = {
        BPF_STMT (BPF_LD | BPF_B | BPF_ABS, 0),
        BPF_STMT (BPF_ALU | BPF_AND | BPF_K, 0xf0),
        BPF_JUMP (BPF_JMP | BPF_JEQ | BPF_K, 0x40, 0, 6),
        /* IPv4: protocol UDP, then the source port behind the variable header */
        BPF_STMT (BPF_LD | BPF_B | BPF_ABS, 9),
        BPF_JUMP (BPF_JMP | BPF_JEQ | BPF_K, IPPROTO_UDP, 0, 10),
        BPF_STMT (BPF_LDX | BPF_B | BPF_MSH, 0),
        BPF_STMT (BPF_LD | BPF_H | BPF_IND, 0),
        BPF_JUMP (BPF_JMP | BPF_JEQ | BPF_K, 53, 6, 7),
        BPF_STMT (BPF_RET | BPF_K, 0),
        /* IPv6: next header UDP, source port at 40 */
        BPF_JUMP (BPF_JMP | BPF_JEQ | BPF_K, 0x60, 0, 5),
        BPF_STMT (BPF_LD | BPF_B | BPF_ABS, 6),
        BPF_JUMP (BPF_JMP | BPF_JEQ | BPF_K, IPPROTO_UDP, 0, 3),
        BPF_STMT (BPF_LD | BPF_H | BPF_ABS, 40),
        BPF_JUMP (BPF_JMP | BPF_JEQ | BPF_K, 53, 0, 1),
        BPF_STMT (BPF_RET | BPF_K, 0xffff),
        BPF_STMT (BPF_RET | BPF_K, 0),
    };
    struct sock_fprog prog = { .len = G_N_ELEMENTS (code), .filter = code };
    int fd;

    fd = socket (AF_PACKET, SOCK_DGRAM | SOCK_CLOEXEC | SOCK_NONBLOCK, htons (ETH_P_ALL));
    if (fd < 0)
        return -1;
    if (setsockopt (fd, SOL_SOCKET, SO_ATTACH_FILTER, &prog, sizeof (prog)) < 0) {
        close (fd);
        return -1;
    }
    return fd;
}

/* ----- Database ----- */

static gboolean
db_exec (const char *sql)
{
    char *err = NULL;

    if (sqlite3_exec (nl.db, sql, NULL, NULL, &err) != SQLITE_OK) {
        g_warning ("sqlite: %s (%s)", err, sql);
        sqlite3_free (err);
        return FALSE;
    }
    return TRUE;
}

/* The table of the buckets; the uid is part of the key, so two accounts' counts stay apart */
#define CONN_SCHEMA \
    "(span INTEGER NOT NULL, start INTEGER NOT NULL, app TEXT NOT NULL, exe TEXT NOT NULL," \
    "  uid INTEGER NOT NULL, proto INTEGER NOT NULL, addr TEXT NOT NULL, port INTEGER NOT NULL," \
    "  host TEXT, scope INTEGER NOT NULL, count INTEGER NOT NULL," \
    "  PRIMARY KEY (span, start, app, exe, uid, proto, addr, port)) WITHOUT ROWID"

/* Whether the table conn has the uid in its primary key (up to 0.1-6 it had not) */
static gboolean
db_uid_in_key (void)
{
    sqlite3_stmt *st = NULL;
    gboolean in_key = FALSE;

    if (sqlite3_prepare_v2 (nl.db, "SELECT pk FROM pragma_table_info('conn') WHERE name = 'uid'", -1, &st,
                            NULL) == SQLITE_OK &&
        sqlite3_step (st) == SQLITE_ROW)
        in_key = sqlite3_column_int (st, 0) > 0;
    sqlite3_finalize (st);
    return in_key;
}

/*
 * A table of 0.1-6 or older, whose key lacks the uid, into the new one with its rows. SQLite
 * cannot change a primary key: a new table, the rows copied, the old one dropped, all in one
 * transaction (a failure leaves the old table as it was). The old key is the new one without the
 * uid, so no two rows meet; an old row without a uid is nobody's but root's and the
 * administrators' (-1, as before: no uid matched it).
 */
static gboolean
db_migrate (void)
{
    if (db_uid_in_key ())
        return TRUE;
    g_message ("Converting the table conn: the uid becomes part of its key");
    if (db_exec ("BEGIN;"
                 "DROP TABLE IF EXISTS conn_new;"
                 "CREATE TABLE conn_new " CONN_SCHEMA ";"
                 "INSERT INTO conn_new (span, start, app, exe, uid, proto, addr, port, host, scope, count)"
                 "  SELECT span, start, app, exe, COALESCE(uid, -1), proto, addr, port, host, scope, count"
                 "  FROM conn;"
                 "DROP TABLE conn;"
                 "ALTER TABLE conn_new RENAME TO conn;"
                 "COMMIT;"))
        return TRUE;
    db_exec ("ROLLBACK");
    g_printerr ("Cannot convert the table conn\n");
    return FALSE;
}

static gboolean
db_open (const char *file)
{
    if (sqlite3_open (file, &nl.db) != SQLITE_OK) {
        g_printerr ("Cannot open %s: %s\n", file, sqlite3_errmsg (nl.db));
        return FALSE;
    }
    /*
     * WAL with normal sync: one batched write a minute, few flushes of the flash. Temporary
     * files (statement journals, sorting) in memory: SQLite would put them in /var/tmp, which
     * the AppArmor profile does not grant; the conversion of a 0.1-6 table then failed with
     * "unable to open database file" and the daemon did not start (measured 2026-11-20).
     */
    return db_exec ("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA temp_store=MEMORY;"
                    "CREATE TABLE IF NOT EXISTS conn " CONN_SCHEMA ";") &&
           db_migrate ();
}

/* Writes the batched rows into the minute and hour buckets and drops expired buckets */
static void
db_flush (void)
{
    static const char sql[] =
        "INSERT INTO conn (span, start, app, exe, uid, proto, addr, port, host, scope, count)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT DO UPDATE SET count = count + excluded.count,"
        "   host = COALESCE(excluded.host, host)";
    sqlite3_stmt *st = NULL;
    GHashTableIter iter;
    Pending *p;
    gint64 now = g_get_real_time () / G_USEC_PER_SEC;
    char cleanup[160];

    if (g_hash_table_size (nl.pending) == 0)
        return;
    if (!db_exec ("BEGIN"))
        return;
    if (sqlite3_prepare_v2 (nl.db, sql, -1, &st, NULL) != SQLITE_OK) {
        g_warning ("sqlite: %s", sqlite3_errmsg (nl.db));
        db_exec ("ROLLBACK");
        return;
    }
    g_hash_table_iter_init (&iter, nl.pending);
    while (g_hash_table_iter_next (&iter, NULL, (gpointer *) &p)) {
        for (int span = 60; span <= 3600; span *= 60) {
            sqlite3_bind_int (st, 1, span);
            sqlite3_bind_int64 (st, 2, p->minute / span * span);
            sqlite3_bind_text (st, 3, p->app, -1, SQLITE_STATIC);
            sqlite3_bind_text (st, 4, p->exe, -1, SQLITE_STATIC);
            sqlite3_bind_int (st, 5, p->uid);
            sqlite3_bind_int (st, 6, p->proto);
            sqlite3_bind_text (st, 7, p->addr, -1, SQLITE_STATIC);
            sqlite3_bind_int (st, 8, p->port);
            if (p->host)
                sqlite3_bind_text (st, 9, p->host, -1, SQLITE_STATIC);
            else
                sqlite3_bind_null (st, 9);
            sqlite3_bind_int (st, 10, p->scope);
            sqlite3_bind_int (st, 11, p->count);
            if (sqlite3_step (st) != SQLITE_DONE)
                g_warning ("sqlite: %s", sqlite3_errmsg (nl.db));
            sqlite3_reset (st);
        }
    }
    sqlite3_finalize (st);
    g_snprintf (cleanup, sizeof (cleanup),
                "DELETE FROM conn WHERE (span = 60 AND start < %" G_GINT64_FORMAT ")"
                " OR (span = 3600 AND start < %" G_GINT64_FORMAT ")",
                now - MINUTE_KEEP, now - HOUR_KEEP);
    db_exec (cleanup);
    db_exec ("COMMIT");
    g_hash_table_remove_all (nl.pending);
}

static gboolean
on_flush_timer (gpointer user_data)
{
    db_flush ();
    return G_SOURCE_CONTINUE;
}

/* The bucket size that covers @seconds back and the earliest bucket start that overlaps it */
static void
query_range (guint seconds, int *span, gint64 *from)
{
    gint64 now = g_get_real_time () / G_USEC_PER_SEC;

    *span = seconds <= MINUTE_KEEP ? 60 : 3600;
    *from = now - seconds - *span + 1;
}

/* @only_uid: the rows of that uid alone, or -1 for all (see caller_sight) */
static GVariant *
query_rows (guint seconds, gint64 only_uid)
{
    static const char sql[] =
        "SELECT app, exe, proto, port, addr, COALESCE(MAX(host), ''), scope, SUM(count),"
        "       MIN(start), MAX(start) + span"
        " FROM conn WHERE span = ?1 AND start >= ?2 AND (?3 < 0 OR uid = ?3)"
        " GROUP BY app, exe, proto, port, addr, scope ORDER BY SUM(count) DESC";
    GVariantBuilder rows;
    sqlite3_stmt *st = NULL;
    gint64 from;
    int span;

    g_variant_builder_init (&rows, G_VARIANT_TYPE ("a(ssuqssuuxx)"));
    query_range (seconds, &span, &from);
    if (sqlite3_prepare_v2 (nl.db, sql, -1, &st, NULL) == SQLITE_OK) {
        sqlite3_bind_int (st, 1, span);
        sqlite3_bind_int64 (st, 2, from);
        sqlite3_bind_int64 (st, 3, only_uid);
        while (sqlite3_step (st) == SQLITE_ROW) {
            gint64 last = sqlite3_column_int64 (st, 9);
            /* The current bucket has not ended yet */
            last = MIN (last, g_get_real_time () / G_USEC_PER_SEC);
            g_variant_builder_add (&rows, "(ssuqssuuxx)",
                                   (const char *) sqlite3_column_text (st, 0),
                                   (const char *) sqlite3_column_text (st, 1),
                                   (guint32) sqlite3_column_int (st, 2),
                                   (guint16) sqlite3_column_int (st, 3),
                                   (const char *) sqlite3_column_text (st, 4),
                                   (const char *) sqlite3_column_text (st, 5),
                                   (guint32) sqlite3_column_int (st, 6),
                                   (guint32) sqlite3_column_int64 (st, 7),
                                   sqlite3_column_int64 (st, 8), last);
        }
    } else {
        g_warning ("sqlite: %s", sqlite3_errmsg (nl.db));
    }
    sqlite3_finalize (st);
    return g_variant_new ("(@a(ssuqssuuxx))", g_variant_builder_end (&rows));
}

static GVariant *
query_summary (guint seconds, gint64 only_uid)
{
    static const char sql[] =
        "SELECT COALESCE(SUM(CASE WHEN scope = 2 THEN count END), 0),"
        "       COUNT(DISTINCT CASE WHEN scope = 2 THEN addr END),"
        "       COUNT(DISTINCT CASE WHEN scope = 2 THEN app END),"
        "       COALESCE(SUM(CASE WHEN scope = 1 THEN count END), 0)"
        " FROM conn WHERE span = ?1 AND start >= ?2 AND (?3 < 0 OR uid = ?3)";
    sqlite3_stmt *st = NULL;
    guint32 v[4] = { 0 };
    gint64 from;
    int span;

    query_range (seconds, &span, &from);
    if (sqlite3_prepare_v2 (nl.db, sql, -1, &st, NULL) == SQLITE_OK) {
        sqlite3_bind_int (st, 1, span);
        sqlite3_bind_int64 (st, 2, from);
        sqlite3_bind_int64 (st, 3, only_uid);
        if (sqlite3_step (st) == SQLITE_ROW)
            for (int i = 0; i < 4; i++)
                v[i] = (guint32) sqlite3_column_int64 (st, i);
    }
    sqlite3_finalize (st);
    return g_variant_new ("(uuuu)", v[0], v[1], v[2], v[3]);
}

/* ----- D-Bus ----- */

static const char introspection_xml[] =
    "<node>"
    "  <interface name='org.plinius.NetLog1'>"
    "    <method name='Summary'>"
    "      <arg direction='in' type='u' name='seconds'/>"
    "      <arg direction='out' type='u' name='internet_connections'/>"
    "      <arg direction='out' type='u' name='internet_destinations'/>"
    "      <arg direction='out' type='u' name='internet_apps'/>"
    "      <arg direction='out' type='u' name='lan_connections'/>"
    "    </method>"
    "    <method name='Query'>"
    "      <arg direction='in' type='u' name='seconds'/>"
    "      <arg direction='out' type='a(ssuqssuuxx)' name='connections'/>"
    "    </method>"
    "  </interface>"
    "</node>";

#ifndef NETLOG_LOGIN_USERS
#define NETLOG_LOGIN_USERS "/run/systemd/users"
#endif

/* Whether uid has a session at logind: its file in /run/systemd/users says STATE=active or
 * online (lingering is a user manager without a session). Read without a bus call. */
static gboolean
uid_has_session (guint32 uid)
{
    g_autofree char *path = g_strdup_printf ("%s/%u", NETLOG_LOGIN_USERS, uid);
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

#ifndef NETLOG_GROUP_FILE
#define NETLOG_GROUP_FILE "/etc/group"
#endif

/* The id of the group wheel, read from /etc/group itself rather than through NSS: the AppArmor
 * profile (plinius.netlogd) allows that one file, not the name service. -1 if unknown. */
static gint64
wheel_gid (void)
{
    g_autofree char *text = NULL;
    g_auto(GStrv) lines = NULL;

    if (!g_file_get_contents (NETLOG_GROUP_FILE, &text, NULL, NULL))
        return -1;
    lines = g_strsplit (text, "\n", -1);
    for (char **l = lines; *l; l++) {
        g_auto(GStrv) f = NULL;
        char *end;
        guint64 gid;

        if (!g_str_has_prefix (*l, "wheel:"))
            continue;
        f = g_strsplit (*l, ":", 4);
        if (g_strv_length (f) < 3 || !*f[2])
            return -1;
        gid = g_ascii_strtoull (f[2], &end, 10);
        return *end || gid > G_MAXUINT32 ? -1 : (gint64) gid;
    }
    return -1;
}

/*
 * What @sender may see: -2 nothing, -1 every connection, else only those of that uid.
 * Root and regular users with a session only: system users, nobody and DynamicUser services
 * (uids 61184-65519) have no business reading which app talked to whom. Of those, root and
 * administrators (group wheel) see all; another account only its own processes' connections.
 * The uid and groups come from the bus daemon (GetConnectionCredentials: the process as it
 * connected). Without wheel's id or the caller's groups, nobody is an administrator.
 */
static gint64
caller_sight (GDBusConnection *bus, const char *sender)
{
    g_autoptr(GVariant) reply = NULL;
    g_autoptr(GVariant) creds = NULL;
    g_autoptr(GVariant) gids = NULL;
    guint32 uid;
    gint64 wheel;

    reply = g_dbus_connection_call_sync (bus, "org.freedesktop.DBus", "/org/freedesktop/DBus",
                                         "org.freedesktop.DBus", "GetConnectionCredentials",
                                         g_variant_new ("(s)", sender), G_VARIANT_TYPE ("(a{sv})"),
                                         G_DBUS_CALL_FLAGS_NONE, -1, NULL, NULL);
    if (!reply)
        return -2;
    creds = g_variant_get_child_value (reply, 0);
    if (!g_variant_lookup (creds, "UnixUserID", "u", &uid))
        return -2;
    if (uid == 0)
        return -1;
    if (uid < 1000 || !uid_has_session (uid))
        return -2;
    wheel = wheel_gid ();
    gids = g_variant_lookup_value (creds, "UnixGroupIDs", G_VARIANT_TYPE ("au"));
    if (wheel >= 0 && gids) {
        gsize n;
        const guint32 *ids = g_variant_get_fixed_array (gids, &n, sizeof (guint32));

        for (gsize i = 0; i < n; i++)
            if (ids[i] == (guint32) wheel)
                return -1;
    }
    return uid;
}

static void
on_method_call (GDBusConnection *bus, const char *sender, const char *path, const char *iface,
                const char *method, GVariant *params, GDBusMethodInvocation *invocation, gpointer user_data)
{
    guint32 seconds;
    gint64 sight = caller_sight (bus, sender);

    if (sight == -2) {
        g_dbus_method_invocation_return_error_literal (invocation, G_DBUS_ERROR, G_DBUS_ERROR_ACCESS_DENIED,
                                                       "Not allowed");
        return;
    }
    g_variant_get (params, "(u)", &seconds);
    seconds = CLAMP (seconds, 60, HOUR_KEEP);
    /* Include what was counted since the last write */
    db_flush ();
    if (g_str_equal (method, "Summary"))
        g_dbus_method_invocation_return_value (invocation, query_summary (seconds, sight));
    else
        g_dbus_method_invocation_return_value (invocation, query_rows (seconds, sight));
}

static const GDBusInterfaceVTable vtable = { .method_call = on_method_call };

static void
on_bus_acquired (GDBusConnection *bus, const char *name, gpointer user_data)
{
    g_autoptr(GError) error = NULL;

    if (!g_dbus_connection_register_object (bus, OBJECT_PATH, nl.introspection->interfaces[0], &vtable,
                                            NULL, NULL, &error))
        g_warning ("Cannot export %s: %s", OBJECT_PATH, error->message);
}

static void
on_name_lost (GDBusConnection *bus, const char *name, gpointer user_data)
{
    g_printerr ("Lost the bus name %s\n", name);
    g_main_loop_quit (nl.loop);
}

static gboolean
on_signal (gpointer user_data)
{
    g_main_loop_quit (nl.loop);
    return G_SOURCE_REMOVE;
}

/* ----- Setup ----- */

/* Loads the BPF programs and attaches each to the root cgroup. The links end with the process,
 * so a crashed daemon leaves nothing attached. */
static gboolean
bpf_setup (const char *file)
{
    struct bpf_object *obj;
    struct bpf_program *prog;
    int map_fd;

    obj = bpf_object__open_file (file, NULL);
    if (!obj) {
        g_printerr ("Cannot open %s: %s\n", file, g_strerror (errno));
        return FALSE;
    }
    if (bpf_object__load (obj) != 0) {
        g_printerr ("Cannot load the BPF programs of %s\n", file);
        return FALSE;
    }
    bpf_object__for_each_program (prog, obj) {
        if (!bpf_program__attach_cgroup (prog, nl.cgroup_fd)) {
            g_printerr ("Cannot attach %s: %s\n", bpf_program__name (prog), g_strerror (errno));
            return FALSE;
        }
    }
    map_fd = bpf_object__find_map_fd_by_name (obj, "events");
    nl.rb = ring_buffer__new (map_fd, on_event, NULL, NULL);
    if (!nl.rb) {
        g_printerr ("Cannot open the ring buffer\n");
        return FALSE;
    }
    g_unix_fd_add (ring_buffer__epoll_fd (nl.rb), G_IO_IN, on_ringbuf, NULL);
    return TRUE;
}

int
main (int argc, char **argv)
{
    g_autofree char *bpf_file = NULL;
    g_autofree char *db_file = NULL;
    g_autoptr(GOptionContext) context = NULL;
    g_autoptr(GError) error = NULL;
    gboolean no_bus = FALSE;
    GOptionEntry entries[] = {
        { "bpf", 0, 0, G_OPTION_ARG_FILENAME, &bpf_file, "BPF object (default " DEFAULT_BPF ")", "FILE" },
        { "db", 0, 0, G_OPTION_ARG_FILENAME, &db_file, "Database (default " DEFAULT_DB ")", "FILE" },
        { "no-bus", 0, 0, G_OPTION_ARG_NONE, &no_bus, "Do not take the bus name (tests)", NULL },
        { NULL }
    };
    int dns_fd;

    context = g_option_context_new ("- record which app connects where");
    g_option_context_add_main_entries (context, entries, NULL);
    if (!g_option_context_parse (context, &argc, &argv, &error)) {
        g_printerr ("%s\n", error->message);
        return 2;
    }

    nl.pending = g_hash_table_new_full (g_str_hash, g_str_equal, g_free, (GDestroyNotify) pending_free);
    nl.apps = g_hash_table_new_full (g_int64_hash, g_int64_equal, g_free, g_free);
    nl.names = g_hash_table_new_full (g_str_hash, g_str_equal, g_free, g_free);
    nl.loop = g_main_loop_new (NULL, FALSE);

    if (!db_open (db_file ? db_file : DEFAULT_DB))
        return 1;
    nl.cgroup_fd = open (CGROUP_ROOT, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (nl.cgroup_fd < 0) {
        g_printerr ("Cannot open %s: %s\n", CGROUP_ROOT, g_strerror (errno));
        return 1;
    }
    if (!bpf_setup (bpf_file ? bpf_file : DEFAULT_BPF))
        return 1;
    /* Without DNS answers the page shows addresses only; not a reason to stop */
    dns_fd = dns_socket ();
    if (dns_fd >= 0)
        g_unix_fd_add (dns_fd, G_IO_IN, on_dns, NULL);
    else
        g_warning ("No DNS names: %s", g_strerror (errno));

    g_timeout_add_seconds (FLUSH_EVERY, on_flush_timer, NULL);
    g_unix_signal_add (SIGTERM, on_signal, NULL);
    g_unix_signal_add (SIGINT, on_signal, NULL);

    if (!no_bus) {
        nl.introspection = g_dbus_node_info_new_for_xml (introspection_xml, NULL);
        g_bus_own_name (G_BUS_TYPE_SYSTEM, BUS_NAME, G_BUS_NAME_OWNER_FLAGS_NONE, on_bus_acquired, NULL,
                        on_name_lost, NULL, NULL);
    }

    g_main_loop_run (nl.loop);
    db_flush ();
    sqlite3_close (nl.db);
    return 0;
}
