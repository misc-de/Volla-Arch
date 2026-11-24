// SPDX-License-Identifier: MIT
// Tests of the wire format and the rule engine of plinius-dnsfilter, with made-up lists only.
#include <glib/gstdio.h>
#include <string.h>
#include <sys/stat.h>

#include "appid.h"
#include "dnsmsg.h"
#include "filter.h"

static const char list[] = "[Adblock Plus]\n"
			   "! Title: test\n"
			   "||ads.example.com^\n"
			   "||tracker.test^$important\n"
			   "|exact.test^\n"
			   "@@||good.ads.example.com^\n"
			   "@@||tracker.test^\n"
			   "||Upper.Test^\n"
			   ".dot.test^\n"
			   "||nocaret.test\n"
			   "||*.star.test^\n"
			   "||bad.test^\n"
			   "||bad.test^$badfilter\n"
			   "||wild*.test^\n"
			   "/^regex/\n"
			   "||ads.example.com^$third-party\n"
			   "||prefix.\n"
			   "0.0.0.0 hosts1.test hosts2.test # two names\n"
			   "127.0.0.1 localhost\n"
			   "10.0.0.1 mapped.test\n"
			   "plain.test\n"
			   "plain2.test   # comment\n"
			   "*.wildcard.test\n"
			   "\r\n"
			   "# end\n";

static FilterSet *build(void)
{
	FilterBuilder *b = filter_builder_new();
	guint unsupported = 0;
	FilterSet *set;

	filter_builder_add_text(b, list, strlen(list));
	set = filter_builder_finish(b, &unsupported);
	/* wild*, regex, $third-party, prefix., the mapped hosts line */
	g_assert_cmpuint(unsupported, ==, 5);
	return set;
}

static gboolean blocked(const FilterSet *set, const char *name)
{
	FilterHit block, allow, important;

	filter_set_lookup(set, name, &block, &allow, &important);
	return important.found || (block.found && !allow.found);
}

static void check_set(const FilterSet *set)
{
	g_assert_true(blocked(set, "ads.example.com"));
	g_assert_true(blocked(set, "x.y.ads.example.com"));
	g_assert_false(blocked(set, "example.com"));
	g_assert_false(blocked(set, "good.ads.example.com"));
	g_assert_false(blocked(set, "a.good.ads.example.com"));
	/* $important beats the exception */
	g_assert_true(blocked(set, "tracker.test"));
	g_assert_true(blocked(set, "exact.test"));
	g_assert_false(blocked(set, "sub.exact.test"));
	g_assert_true(blocked(set, "upper.test"));
	g_assert_true(blocked(set, "a.dot.test"));
	g_assert_true(blocked(set, "nocaret.test"));
	g_assert_true(blocked(set, "a.star.test"));
	g_assert_false(blocked(set, "bad.test"));
	g_assert_true(blocked(set, "hosts1.test"));
	g_assert_true(blocked(set, "hosts2.test"));
	g_assert_false(blocked(set, "sub.hosts1.test"));
	g_assert_false(blocked(set, "localhost"));
	g_assert_false(blocked(set, "mapped.test"));
	g_assert_true(blocked(set, "plain.test"));
	g_assert_true(blocked(set, "plain2.test"));
	g_assert_true(blocked(set, "a.wildcard.test"));
	g_assert_false(blocked(set, "test"));
	g_assert_false(blocked(set, ""));
	g_assert_false(blocked(set, "ads.example.com\\000"));
}

static void test_filter(void)
{
	g_autofree char *dir = g_dir_make_tmp("dnsfilter-test-XXXXXX", NULL);
	g_autofree char *path = g_build_filename(dir, "list.bin", NULL);
	g_autoptr(GError) error = NULL;
	FilterSet *set = build(), *loaded;
	FilterHit block;

	check_set(set);
	g_assert_true(filter_set_save(set, path, &error));
	g_assert_no_error(error);
	loaded = filter_set_load(path, &error);
	g_assert_no_error(error);
	g_assert_cmpuint(filter_set_size(loaded), ==, filter_set_size(set));
	check_set(loaded);
	filter_set_lookup(loaded, "a.b.ads.example.com", &block, NULL, NULL);
	g_assert_cmpstr(block.domain, ==, "ads.example.com");

	/* A damaged file is refused */
	g_assert_true(g_file_set_contents(path, "PLDNSF\x01\0garbage", 15, NULL));
	g_assert_null(filter_set_load(path, &error));
	g_clear_error(&error);

	filter_set_free(set);
	filter_set_free(loaded);
	g_unlink(path);
	g_rmdir(dir);
}

/* Limits against hostile lists: at most N rules, $badfilter in linear time, private file */
static void test_filter_limits(void)
{
	g_autofree char *dir = g_dir_make_tmp("dnsfilter-test-XXXXXX", NULL);
	g_autofree char *path = g_build_filename(dir, "list.bin", NULL);
	g_autoptr(GString) text = g_string_new(NULL);
	g_autoptr(GError) error = NULL;
	FilterBuilder *b;
	FilterSet *set;
	struct stat st;
	gint64 t0;
	const guint n = 100000;

	/* Too many rules: refused, not cut silently */
	b = filter_builder_new();
	filter_builder_set_max_rules(b, 3);
	filter_builder_add_text(b, "a.test\nb.test\nc.test\nd.test\n", 28);
	g_assert_true(filter_builder_too_many(b));
	filter_builder_free(b);
	b = filter_builder_new();
	filter_builder_set_max_rules(b, 4);
	filter_builder_add_text(b, "a.test\nb.test\nc.test\nd.test\n", 28);
	g_assert_false(filter_builder_too_many(b));
	filter_builder_free(b);

	/* n rules and n $badfilter lines for every other one: was quadratic (hours for big lists) */
	for (guint i = 0; i < n; i++)
		g_string_append_printf(text, "||r%u.test^\n", i);
	for (guint i = 0; i < n; i += 2)
		g_string_append_printf(text, "||r%u.test^$badfilter\n", i);
	b = filter_builder_new();
	t0 = g_get_monotonic_time();
	filter_builder_add_text(b, text->str, text->len);
	set = filter_builder_finish(b, NULL);
	g_assert_cmpint(g_get_monotonic_time() - t0, <, 5 * G_USEC_PER_SEC);
	g_assert_cmpuint(filter_set_size(set), ==, n / 2);
	g_assert_false(blocked(set, "r0.test"));
	g_assert_true(blocked(set, "r1.test"));

	/* Saved owner-only (which lists one uses is private) */
	g_assert_true(filter_set_save(set, path, &error));
	g_assert_no_error(error);
	g_assert_cmpint(g_stat(path, &st), ==, 0);
	g_assert_cmpuint(st.st_mode & 0777, ==, 0600);
	filter_set_free(set);
	set = filter_set_load(path, &error);
	g_assert_no_error(error);
	g_assert_cmpuint(filter_set_size(set), ==, n / 2);
	filter_set_free(set);
	g_unlink(path);
	g_rmdir(dir);
}

static void test_rule_text(void)
{
	g_autofree char *a = filter_rule_text("x.test", RULE_SUBDOMAINS);
	g_autofree char *b = filter_rule_text("x.test", RULE_ALLOW);

	g_assert_cmpstr(a, ==, "||x.test^");
	g_assert_cmpstr(b, ==, "@@|x.test^");
	g_assert_true(filter_valid_domain("a-b_c.d"));
	g_assert_false(filter_valid_domain("a..b"));
	g_assert_false(filter_valid_domain(".a"));
	g_assert_false(filter_valid_domain("a."));
	g_assert_false(filter_valid_domain("A.b"));
}

/* A query for www.Example.COM AAAA with EDNS (DO bit) and an ECS option */
static const guint8 query[] = {
	0x12, 0x34, 0x01, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01, 3,    'w',  'w',  'w', 7,
	'E',  'x',  'a',  'm',	'p',  'l',  'e',  3,	'C',  'O',  'M',  0,	0x00, 0x1c, 0x00, 0x01,
	/* OPT: root, type 41, size 4096, DO, rdlen 8: ECS family 1 /24 */
	0x00, 0x00, 0x29, 0x10, 0x00, 0x00, 0x00, 0x80, 0x00, 0x00, 0x08, 0x00, 0x08, 0x00, 0x04,
	0x00, 0x01, 0x18, 0x00,
};

static void test_query(void)
{
	DnsQuery q;
	int rcode;
	g_autoptr(GByteArray) fwd = NULL;
	DnsQuery q2;

	g_assert_true(dns_parse_query(query, sizeof(query), &q, &rcode));
	g_assert_cmpstr(q.name, ==, "www.example.com");
	g_assert_cmpuint(q.type, ==, DNS_TYPE_AAAA);
	g_assert_true(q.edns);
	g_assert_true(q.do_bit);
	g_assert_cmpuint(q.udp_size, ==, 4096);
	g_assert_false(q.odd_name);

	/* The forwarded query has no ECS: just the question and a bare OPT */
	fwd = dns_build_query(&q, 0xbeef, 1232);
	g_assert_cmpuint(fwd->len, ==, 12 + 17 + 4 + 11);
	g_assert_true(dns_parse_query(fwd->data, fwd->len, &q2, &rcode));
	g_assert_cmpuint(q2.id, ==, 0xbeef);
	g_assert_cmpstr(q2.name, ==, "www.example.com");
	g_assert_true(q2.do_bit);
	g_assert_cmpuint(q2.udp_size, ==, 1232);

	/* Cut short and an answer instead of a query */
	g_assert_false(dns_parse_query(query, 20, &q, &rcode));
	g_assert_cmpint(rcode, ==, DNS_RCODE_FORMERR);
	{
		guint8 answer[sizeof(query)];

		memcpy(answer, query, sizeof(query));
		answer[2] |= 0x80;
		g_assert_false(dns_parse_query(answer, sizeof(answer), &q, &rcode));
		g_assert_cmpint(rcode, ==, -1);
	}
	/* A pointer loop */
	{
		static const guint8 loop[] = {0, 1, 1, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0xc0, 12, 0, 1, 0, 1};

		g_assert_false(dns_parse_query(loop, sizeof(loop), &q, &rcode));
	}
}

static void test_reply(void)
{
	DnsQuery q;
	int rcode;
	DnsAnswer a;
	g_autoptr(GByteArray) r = NULL;
	g_autoptr(GPtrArray) addresses = NULL;

	g_assert_true(dns_parse_query(query, sizeof(query), &q, &rcode));
	r = dns_build_reply(&q, DNS_RCODE_NOERROR, TRUE, 10);
	g_assert_true(dns_parse_answer(r->data, r->len, &a));
	g_assert_cmpuint(a.id, ==, 0x1234);
	g_assert_true(dns_answer_matches(&a, &q));
	g_assert_cmpuint(a.answers, ==, 1);
	g_assert_cmpstr(a.summary->str, ==, "::");
	g_assert_cmpuint(a.min_ttl, ==, 10);
	dns_answer_clear(&a);
	addresses = dns_answer_addresses(r->data, r->len);
	g_assert_cmpuint(addresses->len, ==, 1);

	/* TTLs age, OPT goes for a client without EDNS, a long answer is cut with TC */
	g_assert_true(dns_prepare_reply(r, 0x4321, 4, FALSE, 512));
	g_assert_true(dns_parse_answer(r->data, r->len, &a));
	g_assert_cmpuint(a.id, ==, 0x4321);
	g_assert_cmpuint(a.min_ttl, ==, 6);
	g_assert_cmpuint(dns_get16(r->data + 10), ==, 0);
	dns_answer_clear(&a);
	g_assert_true(dns_prepare_reply(r, 0x4321, 100, TRUE, 40));
	g_assert_true(dns_get16(r->data + 2) & DNS_FLAG_TC);
	g_assert_cmpuint(dns_get16(r->data + 6), ==, 0);
	g_assert_cmpuint(r->len, ==, 12 + 17 + 4);

	/* An OPT record outside the additional section (ARCOUNT 0) stays and ARCOUNT does not wrap */
	{
		g_autoptr(GByteArray) odd = dns_build_reply(&q, DNS_RCODE_NOERROR, FALSE, 0);
		static const guint8 opt[] = {0, 0, 41, 0x10, 0, 0, 0, 0, 0, 0, 0};

		g_byte_array_append(odd, opt, sizeof(opt));
		dns_put16(odd->data + 6, dns_get16(odd->data + 6) + 1); /* counted as an answer */
		dns_put16(odd->data + 10, 0);
		g_assert_true(dns_prepare_reply(odd, 1, 0, FALSE, 4096));
		g_assert_cmpuint(dns_get16(odd->data + 10), ==, 0);
	}

	/* NXDOMAIN without records */
	{
		g_autoptr(GByteArray) nx = dns_build_reply(&q, DNS_RCODE_NXDOMAIN, TRUE, 10);

		g_assert_true(dns_parse_answer(nx->data, nx->len, &a));
		g_assert_cmpint(a.rcode, ==, DNS_RCODE_NXDOMAIN);
		g_assert_cmpuint(a.answers, ==, 0);
		dns_answer_clear(&a);
	}
}

/* An answer with a CNAME (compressed) and an A record */
static void test_cname(void)
{
	static const guint8 m[] = {
		0, 7, 0x81, 0x80, 0, 1, 0, 2, 0, 0, 0, 0,
		/* question a.test A IN, name at 12 */
		1, 'a', 4, 't', 'e', 's', 't', 0, 0, 1, 0, 1,
		/* a.test CNAME t.track.test */
		0xc0, 12, 0, 5, 0, 1, 0, 0, 0, 60, 0, 10, 1, 't', 5, 't', 'r', 'a', 'c', 'k', 0xc0, 14,
		/* t.track.test A 1.2.3.4, name at 36 */
		0xc0, 36, 0, 1, 0, 1, 0, 0, 0, 30, 0, 4, 1, 2, 3, 4,
	};
	DnsAnswer a;

	g_assert_true(dns_parse_answer(m, sizeof(m), &a));
	g_assert_cmpuint(a.cnames->len, ==, 1);
	g_assert_cmpstr(g_ptr_array_index(a.cnames, 0), ==, "t.track.test");
	g_assert_cmpstr(a.summary->str, ==, "t.track.test, 1.2.3.4");
	g_assert_cmpuint(a.min_ttl, ==, 30);
	dns_answer_clear(&a);
}

/* cgroup paths as on the phone */
static void test_appid(void)
{
	static const struct {
		const char *rel, *key;
	} cases[] = {
		{"", "kernel"},
		{"system.slice/NetworkManager.service", "system/NetworkManager.service"},
		{"user.slice/user-32011.slice/user@32011.service/app.slice/app-gnome-firefox-4321.scope", "app/firefox"},
		{"user.slice/user-32011.slice/user@32011.service/app.slice/app-flatpak-de.cais.Emilia-77.scope",
		 "app/de.cais.Emilia"},
		{"user.slice/user-32011.slice/user@32011.service/app.slice/app-dbus\\x2d:1.2\\x2dorg.gnome.Fractal.slice/"
		 "dbus-:1.2-org.gnome.Fractal@0.service",
		 "app/org.gnome.Fractal"},
		{"user.slice/user-32011.slice/user@32011.service/app.slice/app-gnome\\x2dsession\\x2dmanager@phosh.service",
		 "app/gnome-session-manager"},
		{"user.slice/user-32011.slice/session-2.scope", "user/session.scope"},
		{"user.slice/user-32011.slice/user@32011.service/session.slice/pipewire.service", "user/pipewire.service"},
		{"lxc.payload.android/uid_10123/pid_4567", "android/uid_10123"},
		{"lxc.payload.android", "android/lxc.payload.android"},
	};

	for (guint i = 0; i < G_N_ELEMENTS(cases); i++) {
		g_autofree char *key = appid_cgroup_key(cases[i].rel);

		g_assert_cmpstr(key, ==, cases[i].key);
	}
}

int main(int argc, char **argv)
{
	g_test_init(&argc, &argv, NULL);
	g_test_add_func("/filter/rules", test_filter);
	g_test_add_func("/filter/text", test_rule_text);
	g_test_add_func("/filter/limits", test_filter_limits);
	g_test_add_func("/dns/query", test_query);
	g_test_add_func("/dns/reply", test_reply);
	g_test_add_func("/dns/cname", test_cname);
	g_test_add_func("/appid/cgroup", test_appid);
	return g_test_run();
}
