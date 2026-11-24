// SPDX-License-Identifier: MIT
// filter: block and allow rules for domain names, from blocklists and the user's own rules.
//
// Supported lines (what DNS blocklists use; anything else is counted as unsupported):
//   ||example.com^          example.com and its subdomains (also without the ^, and .example.com^)
//   |example.com^           example.com only
//   @@||example.com^        an exception: allows what a block rule would block
//   ...$important           beats exceptions of other lists
//   ...$badfilter           switches the same rule without $badfilter off
//   0.0.0.0 example.com     hosts format (also 127.0.0.1, ::, ::1): example.com only
//   example.com             a plain domain list: example.com only
//   *.example.com           subdomains of example.com and example.com itself
//   ! comment, # comment, [Adblock Plus]
// Wildcards inside names, regular expressions and other modifiers are not supported.
//
// A compiled set is a sorted array of 64-bit hashes of the names, with the names in a string
// table behind it, so a lookup is one binary search per label of the asked name and a set of
// 300 000 rules takes 4.8 MB, read through mmap.
#pragma once

#include <glib.h>

enum {
	/* The rule covers subdomains too */
	RULE_SUBDOMAINS = 1 << 0,
	/* An exception (@@) */
	RULE_ALLOW = 1 << 1,
	/* $important */
	RULE_IMPORTANT = 1 << 2,
};

typedef enum {
	PARSE_RULE,	   /* one or more rules (hosts lines may hold several names) */
	PARSE_SKIP,	   /* comment, empty line, header, harmless hosts entry (localhost) */
	PARSE_UNSUPPORTED, /* a rule we can't apply */
} ParseResult;

typedef void (*RuleFunc)(const char *domain, guint flags, gboolean badfilter, gpointer user_data);

/* Parses one line; calls @func for each rule in it */
ParseResult filter_parse_line(const char *line, gsize len, RuleFunc func, gpointer user_data);

/* Is @name a valid domain for a rule (lower case letters, digits, '-', '_', dots between labels
 * of 1 to 63 bytes, up to 253 bytes)? */
gboolean filter_valid_domain(const char *name);

/* The rule's canonical text, as the user sees it ("||example.com^", "@@|example.com^") */
char *filter_rule_text(const char *domain, guint flags);

typedef struct FilterBuilder FilterBuilder;
typedef struct FilterSet FilterSet;

FilterBuilder *filter_builder_new(void);
void filter_builder_free(FilterBuilder *b);
/* At most @max_rules rules (and as many $badfilter lines); beyond that they are dropped and
 * filter_builder_too_many() says so */
void filter_builder_set_max_rules(FilterBuilder *b, guint max_rules);
gboolean filter_builder_too_many(const FilterBuilder *b);
/* Feeds a whole list; counts its rules */
void filter_builder_add_text(FilterBuilder *b, const char *text, gsize len);
void filter_builder_add_rule(FilterBuilder *b, const char *domain, guint flags);
/* Sorts, removes duplicates and rules switched off by $badfilter */
FilterSet *filter_builder_finish(FilterBuilder *b, guint *unsupported);

/* The compiled form on disk (written atomically) and back (mmap) */
gboolean filter_set_save(const FilterSet *set, const char *path, GError **error);
FilterSet *filter_set_load(const char *path, GError **error);
void filter_set_free(FilterSet *set);
guint filter_set_size(const FilterSet *set);

typedef struct {
	gboolean found;
	guint flags;
	/* The rule's domain, valid while the set lives */
	const char *domain;
} FilterHit;

/* Looks up @name and its parent domains. The most specific matching block rule and the most
 * specific exception are returned separately; @important gets the most specific block rule
 * with $important. */
void filter_set_lookup(const FilterSet *set, const char *name, FilterHit *block, FilterHit *allow,
		       FilterHit *important);

/* All rules of a set, for the list of own rules: calls @func with domain and flags */
void filter_set_foreach(const FilterSet *set, void (*func)(const char *domain, guint flags, gpointer data),
			gpointer data);
