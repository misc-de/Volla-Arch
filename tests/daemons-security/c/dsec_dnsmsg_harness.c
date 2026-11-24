/*
 * Harness for plinius-dnsfilter's dnsmsg.c (the DNS wire format, from clients and from servers)
 * and filter.c (block lists from the network). Commands on stdin, one line of output each:
 *
 *   query HEX            dns_parse_query; on success also the forwarded query and our reply are
 *                        built and parsed back
 *   answer HEX           dns_parse_answer
 *   prepare HEX ID AGE EDNS MAX   dns_prepare_reply: "ok HEX" or "fail"
 *   addr HEX             dns_answer_addresses
 *   name HEX OFF         dns_read_name: "END NAME ODD" or "-1"
 *   skip HEX OFF         dns_skip_name
 *   simple NAME TYPE     dns_build_simple_query: HEX or "NULL"
 *   fuzz FILE            every message of FILE (2-byte length + message, repeated) through all
 *                        parsers; "done N". Only the sanitizers judge.
 *   line HEX             filter_parse_line on the bytes: "RULE|SKIP|UNSUPPORTED [domain/flags/bad]..."
 *   valid NAME           filter_valid_domain
 *   build FILE [MAX]     a set from the list FILE: "rules N unsupported N toomany 0|1"
 *   lookup NAME          in the set: "block D/F allow D/F important D/F" ("-" for none)
 *   save FILE / load FILE  the compiled form: "ok" or "error MESSAGE"
 *   rules                every rule of the set (filter_set_foreach): "domain/flags ..."
 *   typename N           dns_type_name
 *   fsize BYTES          the largest file this process may write (RLIMIT_FSIZE; a full disk), 0 none
 */
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>

#include "plinius-dnsfilter/dnsmsg.h"
#include "plinius-dnsfilter/filter.h"

static GByteArray *
unhex (const char *hex)
{
    GByteArray *m = g_byte_array_new ();

    for (; hex[0] && hex[1]; hex += 2) {
        unsigned v;
        guint8 b;

        if (sscanf (hex, "%2x", &v) != 1)
            break;
        b = (guint8) v;
        g_byte_array_append (m, &b, 1);
    }
    return m;
}

static void
print_hex (const guint8 *p, gsize len)
{
    for (gsize i = 0; i < len; i++)
        printf ("%02x", p[i]);
}

static void
cmd_query (const char *hex)
{
    g_autoptr (GByteArray) m = unhex (hex);
    DnsQuery q, q2;
    int rcode, rcode2;

    /* An exact-size copy: a read past the end is an ASan report, not a lucky byte */
    guint8 *exact = g_memdup2 (m->data, m->len);

    if (!dns_parse_query (exact, m->len, &q, &rcode)) {
        printf ("fail rcode=%d\n", rcode);
        g_free (exact);
        return;
    }
    g_free (exact);
    printf ("ok id=%u name=%s odd=%d type=%u class=%u end=%zu edns=%d udp=%u do=%d", q.id, q.name,
            q.odd_name, q.type, q.klass, q.question_end, q.edns, q.udp_size, q.do_bit);
    {
        g_autoptr (GByteArray) fwd = dns_build_query (&q, 0xbeef, 1232);

        if (!fwd)
            printf (" fwd=NULL");
        else if (dns_parse_query (fwd->data, fwd->len, &q2, &rcode2))
            printf (" fwd=%s/%u/%u/do=%d", strcmp (q2.name, q.name) ? "CHANGED" : "same", q2.type,
                    q2.id, q2.do_bit);
        else
            printf (" fwd=UNPARSABLE");
    }
    for (int rc = 0; rc <= 3; rc += 3) {
        g_autoptr (GByteArray) reply = dns_build_reply (&q, rc, TRUE, 10);
        DnsAnswer a;

        if (dns_parse_answer (reply->data, reply->len, &a))
            printf (" reply%d=%s/rcode=%d/answers=%u/qd=%u", rc,
                    dns_answer_matches (&a, &q) ? "match" : "nomatch", a.rcode, a.answers,
                    dns_get16 (reply->data + 4));
        else
            printf (" reply%d=header-only/rcode=%u/qd=%u", rc, DNS_RCODE (dns_get16 (reply->data + 2)),
                    dns_get16 (reply->data + 4));
        dns_answer_clear (&a);
    }
    putchar ('\n');
}

static void
cmd_answer (const char *hex)
{
    g_autoptr (GByteArray) m = unhex (hex);
    guint8 *exact = g_memdup2 (m->data, m->len);
    DnsAnswer a;
    gboolean ok = dns_parse_answer (exact, m->len, &a);

    g_free (exact);
    if (!ok) {
        puts ("fail");
        dns_answer_clear (&a);
        return;
    }
    printf ("ok rcode=%d name=%s type=%u answers=%u ttl=%s", a.rcode, a.name, a.type, a.answers,
            a.has_ttl ? "" : "none");
    if (a.has_ttl)
        printf ("%u", a.min_ttl);
    printf (" cnames=");
    for (guint i = 0; i < a.cnames->len; i++)
        printf ("%s%s", i ? "," : "", (char *) g_ptr_array_index (a.cnames, i));
    printf (" summary=%s\n", a.summary->str);
    dns_answer_clear (&a);
}

static void
cmd_prepare (const char *hex, unsigned id, unsigned age, int edns, unsigned max)
{
    g_autoptr (GByteArray) m = unhex (hex);

    if (!dns_prepare_reply (m, (guint16) id, age, edns, max)) {
        puts ("fail");
        return;
    }
    printf ("ok ");
    print_hex (m->data, m->len);
    putchar ('\n');
}

static void
cmd_addr (const char *hex)
{
    g_autoptr (GByteArray) m = unhex (hex);
    guint8 *exact = g_memdup2 (m->data, m->len);
    g_autoptr (GPtrArray) list = dns_answer_addresses (exact, m->len);

    g_free (exact);
    printf ("addr");
    for (guint i = 0; i < list->len; i++)
        printf (" %s", (char *) g_ptr_array_index (list, i));
    putchar ('\n');
}

static void
fuzz_one (const guint8 *data, gsize len)
{
    DnsQuery q;
    DnsAnswer a;
    int rcode;
    char name[DNS_NAME_TEXT];
    gboolean odd;

    if (dns_parse_query (data, len, &q, &rcode)) {
        g_autoptr (GByteArray) fwd = dns_build_query (&q, 1, 1232);
        g_autoptr (GByteArray) r1 = dns_build_reply (&q, 0, TRUE, 10);
        g_autoptr (GByteArray) r2 = dns_build_reply (&q, 3, FALSE, 10);
        DnsAnswer b;

        if (dns_parse_answer (r1->data, r1->len, &b))
            dns_answer_matches (&b, &q);
        dns_answer_clear (&b);
        (void) fwd;
        (void) r2;
    }
    dns_parse_answer (data, len, &a);
    dns_answer_clear (&a);
    g_ptr_array_unref (dns_answer_addresses (data, len));
    for (gsize off = 0; off < len && off < 64; off++) {
        dns_read_name (data, len, off, name, sizeof name, &odd);
        dns_read_name (data, len, off, name, 8, &odd);
        dns_skip_name (data, len, off);
    }
    {
        GByteArray *copy = g_byte_array_new ();

        g_byte_array_append (copy, data, len);
        dns_prepare_reply (copy, 7, 100, FALSE, 512);
        g_byte_array_set_size (copy, 0);
        g_byte_array_append (copy, data, len);
        dns_prepare_reply (copy, 7, 0, TRUE, 20);
        g_byte_array_unref (copy);
    }
}

static void
cmd_fuzz (const char *path)
{
    g_autofree char *data = NULL;
    gsize len = 0, off = 0;
    guint n = 0;

    if (!g_file_get_contents (path, &data, &len, NULL)) {
        puts ("error");
        return;
    }
    while (off + 2 <= len) {
        gsize l = (guint8) data[off] << 8 | (guint8) data[off + 1];
        guint8 *exact;

        off += 2;
        if (off + l > len)
            break;
        exact = g_memdup2 (data + off, l ? l : 1);
        fuzz_one (exact, l);
        g_free (exact);
        off += l;
        n++;
    }
    printf ("done %u\n", n);
}

static void
on_rule (const char *domain, guint flags, gboolean badfilter, gpointer data)
{
    printf (" %s/%u/%d", domain, flags, badfilter);
}

static FilterSet *set;

static void
print_rule (const char *domain, guint flags, gpointer data)
{
    printf (" %s/%u", domain, flags);
}

static void
print_hit (const char *what, const FilterHit *h)
{
    if (h->found)
        printf ("%s %s/%u", what, h->domain, h->flags);
    else
        printf ("%s -", what);
}

int
main (void)
{
    static char line[1 << 20];

    setvbuf (stdout, NULL, _IOLBF, 0);
    while (fgets (line, sizeof line, stdin)) {
        static char a[1 << 19];
        unsigned u1, u2, u3;
        int i1;

        line[strcspn (line, "\n")] = '\0';
        /* "query " with an empty message: the same as "query" */
        for (size_t n = strlen (line); n > 0 && line[n - 1] == ' '; n--)
            line[n - 1] = '\0';
        if (sscanf (line, "query %524287s", a) == 1)
            cmd_query (a);
        else if (!strcmp (line, "query"))
            cmd_query ("");
        else if (sscanf (line, "answer %524287s", a) == 1)
            cmd_answer (a);
        else if (sscanf (line, "prepare %524287s %u %u %d %u", a, &u1, &u2, &i1, &u3) == 5)
            cmd_prepare (a, u1, u2, i1, u3);
        else if (sscanf (line, "prepare %u %u %d %u", &u1, &u2, &i1, &u3) == 4)
            cmd_prepare ("", u1, u2, i1, u3);
        else if (sscanf (line, "addr %524287s", a) == 1)
            cmd_addr (a);
        else if (sscanf (line, "name %524287s %u", a, &u1) == 2 ||
                 sscanf (line, "skip %524287s %u", a, &u1) == 2) {
            g_autoptr (GByteArray) m = unhex (a);
            guint8 *exact = g_memdup2 (m->data, m->len ? m->len : 1);
            char name[DNS_NAME_TEXT];
            gboolean odd = FALSE;

            if (line[0] == 'n') {
                gssize end = dns_read_name (exact, m->len, u1, name, sizeof name, &odd);

                if (end < 0)
                    puts ("-1");
                else
                    printf ("%zd %s %d\n", end, name, odd);
            } else {
                printf ("%zd\n", dns_skip_name (exact, m->len, u1));
            }
            g_free (exact);
        } else if (sscanf (line, "simple %524287s %u", a, &u1) == 2) {
            g_autoptr (GByteArray) m = dns_build_simple_query (a, (guint16) u1, 0x1234);

            if (!m)
                puts ("NULL");
            else {
                print_hex (m->data, m->len);
                putchar ('\n');
            }
        } else if (sscanf (line, "fuzz %524287s", a) == 1) {
            cmd_fuzz (a);
        } else if (!strncmp (line, "line ", 5) || !strcmp (line, "line")) {
            g_autoptr (GByteArray) m = unhex (line[4] ? line + 5 : "");
            /* filter_parse_line gets a length, not a string: no NUL after it */
            char *exact = g_memdup2 (m->data, m->len ? m->len : 1);
            ParseResult r;

            r = filter_parse_line (exact, m->len, on_rule, NULL);
            printf (" %s\n", r == PARSE_RULE ? "RULE" : r == PARSE_SKIP ? "SKIP" : "UNSUPPORTED");
            g_free (exact);
        } else if (!strncmp (line, "valid ", 6) || !strcmp (line, "valid")) {
            puts (filter_valid_domain (line[5] ? line + 6 : "") ? "yes" : "no");
        } else if (sscanf (line, "build %524287s %u", a, &u1) >= 1 && !strncmp (line, "build", 5)) {
            g_autofree char *text = NULL;
            gsize len = 0;
            FilterBuilder *b = filter_builder_new ();
            guint unsupported = 0;
            gboolean too_many;

            if (sscanf (line, "build %*s %u", &u1) == 1)
                filter_builder_set_max_rules (b, u1);
            g_file_get_contents (a, &text, &len, NULL);
            filter_builder_add_text (b, text ? text : "", len);
            too_many = filter_builder_too_many (b);
            filter_set_free (set);
            set = filter_builder_finish (b, &unsupported);
            printf ("rules %u unsupported %u toomany %d\n", filter_set_size (set), unsupported,
                    too_many);
        } else if (!strncmp (line, "lookup ", 7) || !strcmp (line, "lookup")) {
            FilterHit block, allow, important;

            filter_set_lookup (set, line[6] ? line + 7 : "", &block, &allow, &important);
            print_hit ("block", &block);
            print_hit (" allow", &allow);
            print_hit (" important", &important);
            putchar ('\n');
        } else if (sscanf (line, "save %524287s", a) == 1) {
            g_autoptr (GError) error = NULL;

            if (filter_set_save (set, a, &error))
                puts ("ok");
            else
                printf ("error %s\n", error->message);
        } else if (sscanf (line, "load %524287s", a) == 1) {
            g_autoptr (GError) error = NULL;
            FilterSet *loaded = filter_set_load (a, &error);

            if (loaded) {
                filter_set_free (set);
                set = loaded;
                printf ("ok %u\n", filter_set_size (set));
            } else {
                printf ("error %s\n", error->message);
            }
        } else if (!strcmp (line, "rules")) {
            printf ("rules");
            filter_set_foreach (set, print_rule, NULL);
            putchar ('\n');
        } else if (sscanf (line, "typename %u", &u1) == 1) {
            char buf[16];

            puts (dns_type_name ((guint16) u1, buf));
        } else if (sscanf (line, "fsize %u", &u1) == 1) {
            struct rlimit rl;

            /* EFBIG from write() instead of the signal; 0: no limit again (only the soft one
             * changes, so it can go back) */
            signal (SIGXFSZ, SIG_IGN);
            getrlimit (RLIMIT_FSIZE, &rl);
            rl.rlim_cur = u1 ? u1 : rl.rlim_max;
            puts (setrlimit (RLIMIT_FSIZE, &rl) == 0 ? "ok" : "error");
        } else {
            printf ("<unknown command: %.40s>\n", line);
        }
    }
    filter_set_free (set);
    return 0;
}
