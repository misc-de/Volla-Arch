/*
 * Harness for plinius-netlogd.c (its main() renamed): address scopes, app names from cgroup
 * paths, the DNS answers it reads from a packet socket on every interface (anyone on the network
 * can send those), the counting of connection events into SQLite and the D-Bus methods with the
 * caller check (dsec_fakebus.h). No BPF, no packet socket, no bus; the database is in a test
 * directory and the wall clock is a fake.
 *
 * Commands on stdin, one output line each:
 *   classify 4|6 HEX            "SCOPE TEXT"
 *   app PATH                    app_from_cgroup()
 *   dnsname HEX OFF             dns_name(): "END NAME" or "-1"
 *   dns HEX                     dns_parse(): "names N"
 *   name ADDRESS                the host name noted for ADDRESS, "-" for none
 *   fuzz FILE                   every message of FILE (2-byte length + message) through
 *                               dns_parse() and dns_name(): "done N"
 *   cgapp ID APP                app of cgroup ID (as if looked up already)
 *   event ID UID FAMILY HEX PORT PROTO   on_event() for a process that is gone (comm "dsec")
 *   db FILE / flush / time SECONDS        open the database, write, set the clock
 *   pending                     rows waiting for the next write
 *   rows SECONDS [UID] / summary SECONDS [UID]   Query / Summary as on the bus, printed as
 *                               GVariant: every uid, or only UID (what caller_sight gives)
 *   call METHOD SECONDS         on_method_call() as sender ":1.9": "ok ..." or "error ..."
 *   bus KEY VALUE               see dsec_fakebus.h
 */
#define _GNU_SOURCE
#include <gio/gio.h>

#include "dsec_fakebus.h"

static gint64 fake_now_s = 1800000000;

static gint64
fake_get_real_time (void)
{
    return fake_now_s * G_USEC_PER_SEC;
}

#define g_get_real_time fake_get_real_time
#define main netlogd_main
#include "plinius-netlog/plinius-netlogd.c"
#undef main

static int
unhex (const char *hex, guint8 *out, int size)
{
    int n = 0;

    for (; hex[0] && hex[1] && n < size; hex += 2) {
        unsigned v;

        if (sscanf (hex, "%2x", &v) != 1)
            break;
        out[n++] = (guint8) v;
    }
    return n;
}

static void
print_variant (GVariant *v)
{
    g_autofree char *text = g_variant_print (v, FALSE);

    puts (text);
    g_variant_unref (g_variant_ref_sink (v));
}

int
main (void)
{
    static char line[1 << 18];

    setvbuf (stdout, NULL, _IOLBF, 0);
    nl.pending = g_hash_table_new_full (g_str_hash, g_str_equal, g_free, (GDestroyNotify) pending_free);
    nl.apps = g_hash_table_new_full (g_int64_hash, g_int64_equal, g_free, g_free);
    nl.names = g_hash_table_new_full (g_str_hash, g_str_equal, g_free, g_free);
    /* No cgroup lookups through the kernel: the handle never resolves */
    nl.cgroup_fd = -1;

    while (fgets (line, sizeof line, stdin)) {
        static char a[1 << 17], b[4096];
        static guint8 msg[1 << 16];
        long long n1, n2, n3, n4;
        int fam;

        line[strcspn (line, "\n")] = '\0';
        if (fb_command (line))
            continue;
        if (sscanf (line, "classify %d %131071s", &fam, a) == 2) {
            struct netlog_event ev = { 0 };
            char text[INET6_ADDRSTRLEN] = "";
            guint scope;

            ev.family = (guint8) fam;
            unhex (a, (guint8 *) ev.addr, 16);
            scope = classify (&ev, text, sizeof text);
            printf ("%u %s\n", scope, text);
        } else if (!strncmp (line, "app ", 4)) {
            g_autofree char *app = app_from_cgroup (line + 4);

            puts (app);
        } else if (sscanf (line, "dnsname %131071s %lld", a, &n1) == 2) {
            int len = unhex (a, msg, sizeof msg);
            guint8 *exact = g_memdup2 (msg, len ? len : 1);
            char out[256];
            int end = dns_name (exact, len, (int) n1, out, sizeof out);

            if (end < 0)
                puts ("-1");
            else
                printf ("%d %s\n", end, out);
            g_free (exact);
        } else if (sscanf (line, "dns %131071s", a) == 1) {
            int len = unhex (a, msg, sizeof msg);
            guint8 *exact = g_memdup2 (msg, len ? len : 1);

            dns_parse (exact, len);
            g_free (exact);
            printf ("names %u\n", g_hash_table_size (nl.names));
        } else if (sscanf (line, "name %4095s", b) == 1) {
            const char *host = g_hash_table_lookup (nl.names, b);

            puts (host ? host : "-");
        } else if (sscanf (line, "fuzz %131071s", a) == 1) {
            g_autofree char *data = NULL;
            gsize len = 0, off = 0;
            guint count = 0;

            g_file_get_contents (a, &data, &len, NULL);
            while (data && off + 2 <= len) {
                gsize l = (guint8) data[off] << 8 | (guint8) data[off + 1];
                guint8 *exact;
                char out[256];

                off += 2;
                if (off + l > len)
                    break;
                exact = g_memdup2 (data + off, l ? l : 1);
                dns_parse (exact, (int) l);
                for (int o = 0; o < (int) l && o < 48; o++)
                    dns_name (exact, (int) l, o, out, sizeof out);
                g_free (exact);
                off += l;
                count++;
            }
            printf ("done %u\n", count);
        } else if (sscanf (line, "cgapp %lld %4095s", &n1, b) == 2) {
            gint64 key = n1;

            g_hash_table_insert (nl.apps, g_memdup2 (&key, sizeof key), g_strdup (b));
            puts ("ok");
        } else if (sscanf (line, "event %lld %lld %d %131071s %lld %lld", &n1, &n2, &fam, a, &n3, &n4) == 6) {
            struct netlog_event ev = { 0 };

            ev.cgroup_id = (guint64) n1;
            ev.uid = (guint32) n2;
            /* Above pid_max: /proc has no such process, the command name is taken */
            ev.pid = 0x7ffffff0;
            ev.family = (guint8) fam;
            unhex (a, (guint8 *) ev.addr, 16);
            ev.port = (guint16) n3;
            ev.proto = (guint8) n4;
            ev.kind = NETLOG_CONNECT;
            memcpy (ev.comm, "dsec-comm-name16", 16); /* all 16 bytes, no NUL */
            on_event (NULL, &ev, sizeof ev);
            /* A short record is ignored */
            on_event (NULL, &ev, sizeof ev - 1);
            puts ("ok");
        } else if (sscanf (line, "db %4095s", b) == 1) {
            puts (db_open (b) ? "ok" : "error");
        } else if (!strcmp (line, "flush")) {
            db_flush ();
            puts ("ok");
        } else if (!strcmp (line, "tempstore")) {
            /* Where the open database keeps its temporary files: 2 is memory */
            sqlite3_stmt *st = NULL;

            if (sqlite3_prepare_v2 (nl.db, "PRAGMA temp_store", -1, &st, NULL) == SQLITE_OK &&
                sqlite3_step (st) == SQLITE_ROW)
                printf ("%d\n", sqlite3_column_int (st, 0));
            else
                puts ("error");
            sqlite3_finalize (st);
        } else if (sscanf (line, "time %lld", &n1) == 1) {
            fake_now_s = n1;
            puts ("ok");
        } else if (!strcmp (line, "pending")) {
            printf ("%u\n", g_hash_table_size (nl.pending));
        } else if ((fam = sscanf (line, "rows %lld %lld", &n1, &n2)) >= 1) {
            print_variant (query_rows ((guint) n1, fam == 2 ? n2 : -1));
        } else if ((fam = sscanf (line, "summary %lld %lld", &n1, &n2)) >= 1) {
            print_variant (query_summary ((guint) n1, fam == 2 ? n2 : -1));
        } else if (sscanf (line, "call %4095s %lld", b, &n1) == 2) {
            FakeInv inv = { 0 };

            fake_inv_reset (&inv, b, g_variant_new ("(u)", (guint32) n1));
            on_method_call (NULL, ":1.9", OBJECT_PATH, BUS_NAME, b, inv.params,
                            (GDBusMethodInvocation *) &inv, NULL);
            fake_inv_print (&inv);
            fake_inv_reset (&inv, NULL, NULL);
        } else {
            printf ("<unknown command: %.60s>\n", line);
        }
    }
    return 0;
}
