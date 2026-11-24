// SPDX-License-Identifier: MIT
// dnsmsg: the few pieces of the DNS wire format (RFC 1035, EDNS RFC 6891) plinius-dnsfilter
// needs. It never trusts a message: every read is bounds checked, and a message that does not
// parse is treated as garbage, not repaired.
#pragma once

#include <glib.h>

#define DNS_HEADER_SIZE 12
#define DNS_NAME_MAX 255
/* Text of a name: labels joined by dots, odd bytes as \DDD, so up to four times the wire size */
#define DNS_NAME_TEXT 1024

#define DNS_TYPE_A 1
#define DNS_TYPE_NS 2
#define DNS_TYPE_CNAME 5
#define DNS_TYPE_SOA 6
#define DNS_TYPE_PTR 12
#define DNS_TYPE_MX 15
#define DNS_TYPE_TXT 16
#define DNS_TYPE_AAAA 28
#define DNS_TYPE_SRV 33
#define DNS_TYPE_OPT 41
#define DNS_TYPE_SVCB 64
#define DNS_TYPE_HTTPS 65
#define DNS_TYPE_ANY 255
#define DNS_CLASS_IN 1

#define DNS_RCODE_NOERROR 0
#define DNS_RCODE_FORMERR 1
#define DNS_RCODE_SERVFAIL 2
#define DNS_RCODE_NXDOMAIN 3
#define DNS_RCODE_NOTIMP 4
#define DNS_RCODE_REFUSED 5

/* Header flags (second 16-bit word) */
#define DNS_FLAG_QR 0x8000
#define DNS_FLAG_AA 0x0400
#define DNS_FLAG_TC 0x0200
#define DNS_FLAG_RD 0x0100
#define DNS_FLAG_RA 0x0080
#define DNS_FLAG_AD 0x0020
#define DNS_FLAG_CD 0x0010
#define DNS_OPCODE(flags) (((flags) >> 11) & 0xf)
#define DNS_RCODE(flags) ((flags) & 0xf)

/* EDNS: the DO bit in the OPT record's TTL field */
#define DNS_EDNS_DO 0x8000

/* What a query asks, read from a client's message */
typedef struct {
	guint16 id;
	guint16 flags;
	/* Lower case, without the final dot; "" for the root. \DDD for bytes that can't be text. */
	char name[DNS_NAME_TEXT];
	/* The name has bytes outside letters, digits, '-' and '_' (it never matches a rule) */
	gboolean odd_name;
	guint16 type;
	guint16 klass;
	/* End of the question in the message */
	gsize question_end;
	gboolean edns;
	guint16 udp_size;
	gboolean do_bit;
} DnsQuery;

/* What an answer says, for the log, the cache and CNAME checks */
typedef struct {
	guint16 id;
	guint16 flags;
	int rcode;
	char name[DNS_NAME_TEXT];
	guint16 type;
	guint16 klass;
	/* Smallest TTL of all records but OPT; for a negative answer the SOA's (RFC 2308) */
	guint32 min_ttl;
	gboolean has_ttl;
	/* Targets of CNAME records in the answer section (char *) */
	GPtrArray *cnames;
	/* A short text of the answer: addresses and CNAMEs, ", " between them */
	GString *summary;
	guint answers;
} DnsAnswer;

guint16 dns_get16(const guint8 *p);
void dns_put16(guint8 *p, guint16 v);
guint32 dns_get32(const guint8 *p);
void dns_put32(guint8 *p, guint32 v);

/* Reads the name at @off into @out (text as in DnsQuery.name); returns the offset after it in
 * the message (after the first pointer for a compressed name), or -1. @odd is set if a label has
 * bytes outside [a-z0-9_-] (after lower-casing). */
gssize dns_read_name(const guint8 *msg, gsize len, gsize off, char *out, gsize outlen, gboolean *odd);

/* Offset after the name at @off, or -1 */
gssize dns_skip_name(const guint8 *msg, gsize len, gsize off);

/* Parses a client's query: one question, opcode QUERY, QR clear. EDNS from an OPT record in the
 * additional section. FALSE if it is not such a query; *rcode then says what to answer
 * (FORMERR, NOTIMP), or -1 to drop it. */
gboolean dns_parse_query(const guint8 *msg, gsize len, DnsQuery *q, int *rcode);

/* A fresh query for @q with @id: RD and CD as the client asked, EDNS with our buffer size and
 * the client's DO bit. Nothing else of the client's message goes out (no EDNS options). */
GByteArray *dns_build_query(const DnsQuery *q, guint16 id, guint16 udp_size);

/* A simple query for internal lookups (bootstrap of encrypted servers) */
GByteArray *dns_build_simple_query(const char *name, guint16 type, guint16 id);

/* Answer to @q made by us: @rcode, and for NOERROR with @null_ip an A 0.0.0.0 or AAAA ::
 * record with @ttl (other types get no records). EDNS if the client used it. */
GByteArray *dns_build_reply(const DnsQuery *q, int rcode, gboolean null_ip, guint32 ttl);

/* Parses an answer. FALSE if it is not one (QR clear, no question, cut short). The caller frees
 * cnames and summary with dns_answer_clear(). */
gboolean dns_parse_answer(const guint8 *msg, gsize len, DnsAnswer *a);
void dns_answer_clear(DnsAnswer *a);

/* Does the answer's question match the query? (case-insensitive name, type, class) */
gboolean dns_answer_matches(const DnsAnswer *a, const DnsQuery *q);

/* Turns a stored or forwarded answer into the reply for a client: its @id, TTLs lowered by
 * @age seconds (not below 0), OPT removed when the client had no EDNS (only when it is the last
 * record, which it always is in practice), and cut to the header and question with TC set when
 * it is longer than @max_size. FALSE if the message does not parse. */
gboolean dns_prepare_reply(GByteArray *msg, guint16 id, guint32 age, gboolean client_edns, gsize max_size);

/* IPv4/IPv6 addresses of the A/AAAA records of an answer, as text; for internal lookups */
GPtrArray *dns_answer_addresses(const guint8 *msg, gsize len);

/* Name of a record type for the log ("A", "AAAA", "TYPE99") */
const char *dns_type_name(guint16 type, char buf[16]);
