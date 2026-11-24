/*
 * Harness for plinius-dnsfilter.c (the daemon itself, its main() renamed): the decision for a
 * name and an app, the user's own rules as they come in over D-Bus, the upstream server specs,
 * the network domains a name may go to, and who may change settings (check_caller against the
 * scripted bus of dsec_fakebus.h). Nothing is started: no sockets, no bus, no firewall.
 *
 * Commands on stdin, one output line each:
 *   enable 0|1            the filter on or off      pause SECONDS   paused for that long
 *   rule TEXT             an own rule as stored     rebuild         rebuild_own()
 *   list NAME FILE        a subscribed list from FILE (enabled)
 *   decide NAME [APP]     "STATUS reason"
 *   canon TEXT            canonical_rule(): the rule or "ERROR"
 *   canonhex HEX          the same for input with control characters
 *   split RULE            app_rule_split(): "APP|- BASE"
 *   appkey K / usable D / local N / system N / self ADDR   yes or no
 *   upstream SPEC         "udp ADDRESS" / "tls HOST PORT" / "https URL" / "ERROR"
 *   caller SENDER         check_caller(): "allowed" or "denied MESSAGE"
 *   bus KEY VALUE         see dsec_fakebus.h
 */
#define _GNU_SOURCE
#include <gio/gio.h>

#include "dsec_fakebus.h"

#define main dnsfilter_main
#include "plinius-dnsfilter/plinius-dnsfilter.c"
#undef main

static const char *const status_names[] = {
    "PASSED", "ALLOWED_RULE", "ALLOWED_LIST", "ALLOWED_SYSTEM", "BLOCKED_LIST",
    "BLOCKED_RULE", "UNFILTERED", "FAILED", "BLOCKED_SPECIAL",
};

static const char *
yes (gboolean v)
{
    return v ? "yes" : "no";
}

int
main (void)
{
    static char line[65536];

    setvbuf (stdout, NULL, _IOLBF, 0);
    self.rules = g_ptr_array_new_with_free_func (g_free);
    self.lists = g_ptr_array_new ();
    self.s.enabled = TRUE;
    self.system = TRUE;
    rebuild_own ();

    while (fgets (line, sizeof line, stdin)) {
        char a[4096] = "", b[4096] = "";
        long n;

        line[strcspn (line, "\n")] = '\0';
        if (fb_command (line))
            continue;
        if (sscanf (line, "enable %ld", &n) == 1) {
            self.s.enabled = n != 0;
            puts ("ok");
        } else if (sscanf (line, "pause %ld", &n) == 1) {
            self.paused_until = now_s () + n;
            puts ("ok");
        } else if (!strncmp (line, "rule ", 5)) {
            g_ptr_array_add (self.rules, g_strdup (line + 5));
            puts ("ok");
        } else if (!strcmp (line, "rebuild")) {
            rebuild_own ();
            puts ("ok");
        } else if (sscanf (line, "list %4095s %4095s", a, b) == 2) {
            List *l = list_new (a, a, "https://lists.invalid/", TRUE);
            g_autofree char *text = NULL;
            gsize len = 0;
            FilterBuilder *fbld = filter_builder_new ();

            g_file_get_contents (b, &text, &len, NULL);
            filter_builder_add_text (fbld, text ? text : "", len);
            l->set = filter_builder_finish (fbld, NULL);
            g_ptr_array_add (self.lists, l);
            puts ("ok");
        } else if (sscanf (line, "decide %4095s %4095s", a, b) >= 1) {
            Decision d = decide (a, b[0] ? b : NULL);

            printf ("%s %s\n", status_names[d.status], d.reason ? d.reason : "-");
            decision_clear (&d);
        } else if (!strncmp (line, "canon ", 6)) {
            g_autoptr (GError) error = NULL;
            g_autofree char *rule = canonical_rule (line + 6, &error);

            puts (rule ? rule : "ERROR");
        } else if (sscanf (line, "canonhex %4095s", a) == 1) {
            g_autoptr (GError) error = NULL;
            char input[2048] = "";
            g_autofree char *rule = NULL;

            for (size_t i = 0; a[2 * i] && a[2 * i + 1] && i < sizeof input - 1; i++) {
                unsigned v;

                sscanf (a + 2 * i, "%2x", &v);
                input[i] = (char) v;
            }
            rule = canonical_rule (input, &error);
            if (rule && strpbrk (rule, "\r\n"))
                puts ("LINEBREAK");
            else
                puts (rule ? rule : "ERROR");
        } else if (!strncmp (line, "split ", 6)) {
            g_autofree char *base = NULL;
            g_autofree char *app = app_rule_split (line + 6, &base);

            printf ("%s %s\n", app ? app : "-", base);
        } else if (!strncmp (line, "appkey ", 7)) {
            puts (yes (valid_app_key (line + 7)));
        } else if (!strncmp (line, "usable ", 7)) {
            puts (yes (usable_domain (line + 7)));
        } else if (!strncmp (line, "local ", 6)) {
            puts (yes (local_name (line + 6)));
        } else if (!strncmp (line, "system ", 7)) {
            puts (yes (system_allowed (line + 7)));
        } else if (!strncmp (line, "self ", 5)) {
            puts (yes (server_is_self (line + 5)));
        } else if (!strncmp (line, "upstream ", 9)) {
            g_autoptr (GError) error = NULL;
            Upstream *u = upstream_parse (line + 9, &error);

            if (!u) {
                puts ("ERROR");
            } else if (u->proto == PROTO_HTTPS) {
                printf ("https %s\n", u->url);
            } else if (u->proto == PROTO_TLS) {
                printf ("tls %s %u\n", u->host, u->port);
            } else {
                g_autofree char *text = g_socket_connectable_to_string (G_SOCKET_CONNECTABLE (u->addr));

                printf ("udp %s\n", text);
            }
            if (u)
                upstream_unref (u);
        } else if (sscanf (line, "caller %4095s", a) == 1) {
            g_autoptr (GError) error = NULL;

            if (check_caller (a, &error))
                puts ("allowed");
            else
                printf ("denied %s\n", error ? error->message : "");
        } else {
            printf ("<unknown command: %.60s>\n", line);
        }
    }
    return 0;
}
