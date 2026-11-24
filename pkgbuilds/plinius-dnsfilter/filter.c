// SPDX-License-Identifier: MIT
// filter: see filter.h
#include "filter.h"

#include <errno.h>
#include <fcntl.h>
#include <gio/gio.h>
#include <glib/gstdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/* On disk: header, entries sorted by hash, then the names (NUL-terminated). Native byte order,
 * the files never leave the phone. */
#define MAGIC "PLDNSF\x01"

typedef struct {
	char magic[8];
	guint32 count;
	guint32 strings_size;
	guint32 reserved[2];
} FileHeader;

typedef struct {
	guint64 hash;
	guint32 name;
	guint32 flags;
} Entry;

G_STATIC_ASSERT(sizeof(FileHeader) == 24);
G_STATIC_ASSERT(sizeof(Entry) == 16);

struct FilterSet {
	GMappedFile *mapped;
	Entry *own_entries;
	char *own_strings;
	const Entry *entries;
	guint count;
	const char *strings;
	gsize strings_size;
};

typedef struct {
	char *domain;
	guint flags;
} Bad;

struct FilterBuilder {
	GArray *entries;
	GString *strings;
	GArray *bad;
	guint unsupported;
	guint max_rules; /* 0: no limit */
	gboolean too_many;
};

/* FNV-1a, 64 bits: fast and stable across versions of GLib (the hashes are stored) */
static guint64 hash_name(const char *s)
{
	guint64 h = 0xcbf29ce484222325ULL;

	for (; *s; s++) {
		h ^= (guint8)*s;
		h *= 0x100000001b3ULL;
	}
	return h;
}

gboolean filter_valid_domain(const char *name)
{
	gsize label = 0, total = 0;

	if (!name || !*name)
		return FALSE;
	for (const char *p = name; *p; p++, total++) {
		char c = *p;

		if (c == '.') {
			if (label == 0)
				return FALSE;
			label = 0;
			continue;
		}
		if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-' || c == '_'))
			return FALSE;
		if (++label > 63)
			return FALSE;
	}
	return label > 0 && total <= 253;
}

char *filter_rule_text(const char *domain, guint flags)
{
	return g_strdup_printf("%s%s%s^%s", (flags & RULE_ALLOW) ? "@@" : "", (flags & RULE_SUBDOMAINS) ? "||" : "|",
			       domain, (flags & RULE_IMPORTANT) ? "$important" : "");
}

/* Names hosts files map to themselves; not rules */
static gboolean harmless_host(const char *name)
{
	static const char *const names[] = {
		"localhost",	 "localhost.localdomain", "local",	     "broadcasthost",  "ip6-localhost",
		"ip6-loopback",	 "ip6-localnet",	  "ip6-mcastprefix", "ip6-allnodes",   "ip6-allrouters",
		"ip6-allhosts",	 "0.0.0.0",
	};

	for (guint i = 0; i < G_N_ELEMENTS(names); i++)
		if (!strcmp(name, names[i]))
			return TRUE;
	return FALSE;
}

/* Address of a hosts line that means "nowhere" */
static gboolean null_address(const char *ip)
{
	static const char *const ips[] = {"0.0.0.0", "127.0.0.1", "0", "::", "::0", "::1", "0:0:0:0:0:0:0:0"};

	for (guint i = 0; i < G_N_ELEMENTS(ips); i++)
		if (!strcmp(ip, ips[i]))
			return TRUE;
	return FALSE;
}

static gboolean looks_like_address(const char *s)
{
	return g_hostname_is_ip_address(s) || strchr(s, ':') != NULL;
}

static ParseResult parse_hosts(char **tokens, RuleFunc func, gpointer user_data)
{
	gboolean any = FALSE, unsupported = FALSE;

	for (guint i = 1; tokens[i]; i++) {
		char *name = tokens[i];
		gsize l;

		if (name[0] == '#')
			break;
		l = strlen(name);
		if (l && name[l - 1] == '.')
			name[l - 1] = '\0';
		if (harmless_host(name))
			continue;
		if (!null_address(tokens[0])) {
			/* Some other address for a name: a local mapping, nothing to block */
			unsupported = TRUE;
			continue;
		}
		if (!filter_valid_domain(name)) {
			unsupported = TRUE;
			continue;
		}
		func(name, 0, FALSE, user_data);
		any = TRUE;
	}
	if (any)
		return PARSE_RULE;
	return unsupported ? PARSE_UNSUPPORTED : PARSE_SKIP;
}

static ParseResult parse_adblock(char *p, RuleFunc func, gpointer user_data)
{
	guint flags = RULE_SUBDOMAINS;
	gboolean badfilter = FALSE;
	char *end, *mods = NULL;

	if (!strncmp(p, "@@", 2)) {
		flags |= RULE_ALLOW;
		p += 2;
	}
	if (!strncmp(p, "||", 2)) {
		p += 2;
		if (!strncmp(p, "*.", 2))
			p += 2;
	} else if (*p == '|') {
		flags &= ~RULE_SUBDOMAINS;
		p++;
	} else if (*p == '.') {
		p++;
	} else if (!strncmp(p, "*.", 2)) {
		p += 2;
	} else if (!strchr(p, '^') && !strchr(p, '$')) {
		/* No adblock marks at all: a plain domain */
		flags &= ~RULE_SUBDOMAINS;
	}

	end = p + strcspn(p, "^$|");
	if (*end == '^')
		*end++ = '\0';
	if (*end == '|')
		*end++ = '\0';
	if (*end == '$') {
		*end++ = '\0';
		mods = end;
		end += strlen(end);
	}
	if (*end)
		return PARSE_UNSUPPORTED;
	*end = '\0';

	if (mods) {
		g_auto(GStrv) list = g_strsplit(mods, ",", -1);

		for (guint i = 0; list[i]; i++) {
			if (!strcmp(list[i], "important"))
				flags |= RULE_IMPORTANT;
			else if (!strcmp(list[i], "badfilter"))
				badfilter = TRUE;
			else
				return PARSE_UNSUPPORTED;
		}
	}
	/* Lower case is what a rule matches; lists are lower case already, own rules may not be */
	for (char *c = p; *c; c++)
		*c = g_ascii_tolower(*c);
	if (!filter_valid_domain(p))
		return PARSE_UNSUPPORTED;
	func(p, flags, badfilter, user_data);
	return PARSE_RULE;
}

ParseResult filter_parse_line(const char *line, gsize len, RuleFunc func, gpointer user_data)
{
	g_autofree char *copy = NULL;
	char *p;

	while (len && g_ascii_isspace(*line)) {
		line++;
		len--;
	}
	while (len && g_ascii_isspace(line[len - 1]))
		len--;
	if (!len || line[0] == '!' || line[0] == '#' || line[0] == '[')
		return PARSE_SKIP;
	/* Regular expressions */
	if (line[0] == '/')
		return PARSE_UNSUPPORTED;
	if (len > 1024)
		return PARSE_UNSUPPORTED;

	copy = g_strndup(line, len);
	p = copy;
	if (strpbrk(p, " \t")) {
		g_auto(GStrv) tokens = g_strsplit_set(p, " \t", -1);
		guint n = 0;

		/* g_strsplit_set gives empty strings between separators; drop them */
		for (guint i = 0; tokens[i]; i++) {
			if (*tokens[i])
				tokens[n++] = tokens[i];
			else
				g_free(tokens[i]);
		}
		tokens[n] = NULL;
		if (n == 0)
			return PARSE_SKIP;
		for (char *c = tokens[0]; *c; c++)
			*c = g_ascii_tolower(*c);
		if (looks_like_address(tokens[0])) {
			for (guint i = 1; tokens[i]; i++)
				for (char *c = tokens[i]; *c; c++)
					*c = g_ascii_tolower(*c);
			return parse_hosts(tokens, func, user_data);
		}
		/* "example.com # why" */
		if (n >= 2 && tokens[1][0] == '#')
			return parse_adblock(tokens[0], func, user_data);
		return PARSE_UNSUPPORTED;
	}
	return parse_adblock(p, func, user_data);
}

FilterBuilder *filter_builder_new(void)
{
	FilterBuilder *b = g_new0(FilterBuilder, 1);

	b->entries = g_array_new(FALSE, FALSE, sizeof(Entry));
	b->strings = g_string_sized_new(4096);
	b->bad = g_array_new(FALSE, FALSE, sizeof(Bad));
	/* Offset 0 is the empty name, so no entry points there by accident */
	g_string_append_c(b->strings, '\0');
	return b;
}

void filter_builder_free(FilterBuilder *b)
{
	if (!b)
		return;
	if (b->entries)
		g_array_unref(b->entries);
	if (b->strings)
		g_string_free(b->strings, TRUE);
	for (guint i = 0; i < b->bad->len; i++)
		g_free(g_array_index(b->bad, Bad, i).domain);
	g_array_unref(b->bad);
	g_free(b);
}

void filter_builder_set_max_rules(FilterBuilder *b, guint max_rules)
{
	b->max_rules = max_rules;
}

gboolean filter_builder_too_many(const FilterBuilder *b)
{
	return b->too_many;
}

void filter_builder_add_rule(FilterBuilder *b, const char *domain, guint flags)
{
	Entry e = {.hash = hash_name(domain), .name = (guint32)b->strings->len, .flags = flags};

	/* 4 GiB of names would be a list nobody can mean; refuse instead of wrapping */
	if (b->strings->len + strlen(domain) + 1 > G_MAXUINT32)
		return;
	if (b->max_rules && b->entries->len >= b->max_rules) {
		b->too_many = TRUE;
		return;
	}
	g_string_append(b->strings, domain);
	g_string_append_c(b->strings, '\0');
	g_array_append_val(b->entries, e);
}

static void on_rule(const char *domain, guint flags, gboolean badfilter, gpointer user_data)
{
	FilterBuilder *b = user_data;

	if (badfilter) {
		Bad bad = {g_strdup(domain), flags};

		if (b->max_rules && b->bad->len >= b->max_rules) {
			b->too_many = TRUE;
			return;
		}
		g_array_append_val(b->bad, bad);
		return;
	}
	filter_builder_add_rule(b, domain, flags);
}

void filter_builder_add_text(FilterBuilder *b, const char *text, gsize len)
{
	const char *p = text, *end = text + len;

	while (p < end) {
		const char *nl = memchr(p, '\n', end - p);
		gsize l = (nl ? nl : end) - p;

		if (filter_parse_line(p, l, on_rule, b) == PARSE_UNSUPPORTED)
			b->unsupported++;
		p += l + 1;
	}
}

static const char *sort_strings;

static int compare_entries(const void *a, const void *b)
{
	const Entry *x = a, *y = b;
	int c;

	if (x->hash != y->hash)
		return x->hash < y->hash ? -1 : 1;
	if (x->flags != y->flags)
		return x->flags < y->flags ? -1 : 1;
	c = strcmp(sort_strings + x->name, sort_strings + y->name);
	if (c)
		return c;
	return x->name < y->name ? -1 : x->name > y->name;
}

FilterSet *filter_builder_finish(FilterBuilder *b, guint *unsupported)
{
	FilterSet *set = g_new0(FilterSet, 1);
	Entry *e = (Entry *)b->entries->data;
	guint n = b->entries->len, out = 0;
	gsize slen = b->strings->len;
	/* $badfilter rules as "flags:domain": one lookup per rule instead of a pass over all of them
	 * (a list with many of both took hours) */
	g_autoptr(GHashTable) bad = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, NULL);

	for (guint k = 0; k < b->bad->len; k++) {
		Bad *x = &g_array_index(b->bad, Bad, k);

		g_hash_table_add(bad, g_strdup_printf("%u:%s", x->flags, x->domain));
	}

	/* Sort (qsort has no user data; one build at a time, in the main thread) */
	sort_strings = b->strings->str;
	if (n)
		qsort(e, n, sizeof(Entry), compare_entries);
	sort_strings = NULL;

	for (guint i = 0; i < n; i++) {
		gboolean drop = FALSE;

		/* Duplicates: same hash, flags and name */
		if (out > 0 && e[out - 1].hash == e[i].hash && e[out - 1].flags == e[i].flags &&
		    !strcmp(b->strings->str + e[out - 1].name, b->strings->str + e[i].name))
			drop = TRUE;
		if (!drop && g_hash_table_size(bad)) {
			g_autofree char *key = g_strdup_printf("%u:%s", e[i].flags, b->strings->str + e[i].name);

			drop = g_hash_table_contains(bad, key);
		}
		if (!drop)
			e[out++] = e[i];
	}

	if (unsupported)
		*unsupported = b->unsupported;
	set->count = out;
	set->own_entries = (Entry *)g_array_free(g_steal_pointer(&b->entries), FALSE);
	set->strings_size = slen;
	set->own_strings = g_string_free(g_steal_pointer(&b->strings), FALSE);
	set->entries = set->own_entries;
	set->strings = set->own_strings;
	filter_builder_free(b);
	return set;
}

static gboolean write_all(int fd, const void *data, gsize len)
{
	const guint8 *p = data;

	while (len) {
		gssize n = write(fd, p, len);

		if (n < 0 && errno == EINTR)
			continue;
		if (n <= 0)
			return FALSE;
		p += n;
		len -= n;
	}
	return TRUE;
}

gboolean filter_set_save(const FilterSet *set, const char *path, GError **error)
{
	FileHeader h = {.count = set->count, .strings_size = (guint32)set->strings_size};
	g_autofree char *tmp = g_strconcat(path, ".new", NULL);
	int fd, saved;
	gboolean ok;

	memcpy(h.magic, MAGIC, sizeof(h.magic));
	/* Written in pieces straight from the set (a copy in memory doubled the peak for big
	 * lists), then renamed over the old file. Private: which lists one uses. */
	fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC | O_NOFOLLOW, 0600);
	if (fd < 0) {
		saved = errno;
		g_set_error(error, G_IO_ERROR, g_io_error_from_errno(saved), "%s: %s", tmp, g_strerror(saved));
		return FALSE;
	}
	ok = write_all(fd, &h, sizeof(h)) && write_all(fd, set->entries, (gsize)set->count * sizeof(Entry)) &&
	     write_all(fd, set->strings, set->strings_size) && fsync(fd) == 0;
	saved = errno;
	if (close(fd) < 0 && ok) {
		ok = FALSE;
		saved = errno;
	}
	if (ok && rename(tmp, path) < 0) {
		ok = FALSE;
		saved = errno;
	}
	if (!ok) {
		g_unlink(tmp);
		g_set_error(error, G_IO_ERROR, g_io_error_from_errno(saved), "%s: %s", path, g_strerror(saved));
	}
	return ok;
}

FilterSet *filter_set_load(const char *path, GError **error)
{
	g_autoptr(GMappedFile) mapped = g_mapped_file_new(path, FALSE, error);
	const char *data;
	gsize len;
	FileHeader h;
	FilterSet *set;

	if (!mapped)
		return NULL;
	data = g_mapped_file_get_contents(mapped);
	len = g_mapped_file_get_length(mapped);
	if (len < sizeof(h)) {
		g_set_error(error, G_IO_ERROR, G_IO_ERROR_INVALID_DATA, "%s: too short", path);
		return NULL;
	}
	memcpy(&h, data, sizeof(h));
	if (memcmp(h.magic, MAGIC, sizeof(h.magic)) ||
	    (guint64)sizeof(h) + (guint64)h.count * sizeof(Entry) + h.strings_size != len || h.strings_size == 0 ||
	    data[len - 1] != '\0') {
		g_set_error(error, G_IO_ERROR, G_IO_ERROR_INVALID_DATA, "%s: not a compiled list", path);
		return NULL;
	}
	set = g_new0(FilterSet, 1);
	set->entries = (const Entry *)(data + sizeof(h));
	set->count = h.count;
	set->strings = data + sizeof(h) + (gsize)h.count * sizeof(Entry);
	set->strings_size = h.strings_size;
	for (guint i = 0; i < set->count; i++) {
		if (set->entries[i].name >= set->strings_size) {
			g_set_error(error, G_IO_ERROR, G_IO_ERROR_INVALID_DATA, "%s: broken entry", path);
			g_free(set);
			return NULL;
		}
	}
	set->mapped = g_steal_pointer(&mapped);
	return set;
}

void filter_set_free(FilterSet *set)
{
	if (!set)
		return;
	g_clear_pointer(&set->mapped, g_mapped_file_unref);
	g_free(set->own_entries);
	g_free(set->own_strings);
	g_free(set);
}

guint filter_set_size(const FilterSet *set)
{
	return set ? set->count : 0;
}

static void note(FilterHit *hit, const Entry *e, const char *domain)
{
	if (hit && !hit->found) {
		hit->found = TRUE;
		hit->flags = e->flags;
		hit->domain = domain;
	}
}

void filter_set_lookup(const FilterSet *set, const char *name, FilterHit *block, FilterHit *allow,
		       FilterHit *important)
{
	if (block)
		*block = (FilterHit){0};
	if (allow)
		*allow = (FilterHit){0};
	if (important)
		*important = (FilterHit){0};
	if (!set || !set->count || !*name)
		return;

	for (const char *p = name; p;) {
		guint64 h = hash_name(p);
		guint lo = 0, hi = set->count;

		/* First entry with this hash */
		while (lo < hi) {
			guint mid = lo + (hi - lo) / 2;

			if (set->entries[mid].hash < h)
				lo = mid + 1;
			else
				hi = mid;
		}
		for (guint i = lo; i < set->count && set->entries[i].hash == h; i++) {
			const Entry *e = &set->entries[i];
			const char *domain = set->strings + e->name;

			if (strcmp(domain, p) || (p != name && !(e->flags & RULE_SUBDOMAINS)))
				continue;
			if (e->flags & RULE_ALLOW) {
				note(allow, e, domain);
			} else {
				note(block, e, domain);
				if (e->flags & RULE_IMPORTANT)
					note(important, e, domain);
			}
		}
		p = strchr(p, '.');
		if (p)
			p++;
	}
}

void filter_set_foreach(const FilterSet *set, void (*func)(const char *domain, guint flags, gpointer data),
			gpointer data)
{
	for (guint i = 0; set && i < set->count; i++)
		func(set->strings + set->entries[i].name, set->entries[i].flags, data);
}
