// SPDX-License-Identifier: MIT
// dnsmsg: see dnsmsg.h
#include "dnsmsg.h"

#include <arpa/inet.h>
#include <stdio.h>
#include <string.h>

/* Compression pointers followed at most; a loop of pointers stops here */
#define MAX_POINTERS 64

guint16 dns_get16(const guint8 *p)
{
	return (guint16)(p[0] << 8 | p[1]);
}

void dns_put16(guint8 *p, guint16 v)
{
	p[0] = v >> 8;
	p[1] = v & 0xff;
}

guint32 dns_get32(const guint8 *p)
{
	return (guint32)p[0] << 24 | (guint32)p[1] << 16 | (guint32)p[2] << 8 | p[3];
}

void dns_put32(guint8 *p, guint32 v)
{
	p[0] = v >> 24;
	p[1] = (v >> 16) & 0xff;
	p[2] = (v >> 8) & 0xff;
	p[3] = v & 0xff;
}

static gboolean plain_byte(guint8 c)
{
	return (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-' || c == '_';
}

gssize dns_read_name(const guint8 *msg, gsize len, gsize off, char *out, gsize outlen, gboolean *odd)
{
	gsize o = 0, wire = 0;
	gssize next = -1;
	guint pointers = 0;

	if (odd)
		*odd = FALSE;
	if (outlen == 0)
		return -1;
	for (;;) {
		guint8 l;

		if (off >= len)
			return -1;
		l = msg[off];
		if ((l & 0xc0) == 0xc0) {
			if (off + 1 >= len || ++pointers > MAX_POINTERS)
				return -1;
			if (next < 0)
				next = off + 2;
			off = (gsize)(l & 0x3f) << 8 | msg[off + 1];
			continue;
		}
		if (l & 0xc0)
			return -1; /* extended label types are obsolete */
		off++;
		if (l == 0)
			break;
		wire += l + 1;
		if (wire > DNS_NAME_MAX || off + l > len)
			return -1;
		if (o > 0) {
			if (o + 1 >= outlen)
				return -1;
			out[o++] = '.';
		}
		for (guint i = 0; i < l; i++) {
			guint8 c = msg[off + i];

			if (c >= 'A' && c <= 'Z')
				c += 'a' - 'A';
			if (plain_byte(c)) {
				if (o + 1 >= outlen)
					return -1;
				out[o++] = c;
			} else {
				if (odd)
					*odd = TRUE;
				if (o + 4 >= outlen)
					return -1;
				snprintf(out + o, 5, "\\%03u", c);
				o += 4;
			}
		}
		off += l;
	}
	out[o] = '\0';
	return next >= 0 ? next : (gssize)off;
}

gssize dns_skip_name(const guint8 *msg, gsize len, gsize off)
{
	gsize wire = 0;

	for (;;) {
		guint8 l;

		if (off >= len)
			return -1;
		l = msg[off];
		if ((l & 0xc0) == 0xc0)
			return off + 2 <= len ? (gssize)(off + 2) : -1;
		if (l & 0xc0)
			return -1;
		off += l + 1;
		if (l == 0)
			return off <= len ? (gssize)off : -1;
		wire += l + 1;
		if (wire > DNS_NAME_MAX)
			return -1;
	}
}

/* Offset after the resource record at @off, or -1; its type, class, TTL and RDATA position */
static gssize parse_rr(const guint8 *msg, gsize len, gsize off, guint16 *type, guint16 *klass, guint32 *ttl,
		       gsize *ttl_off, gsize *rdata, guint16 *rdlen)
{
	gssize p = dns_skip_name(msg, len, off);
	guint16 rl;

	if (p < 0 || (gsize)p + 10 > len)
		return -1;
	rl = dns_get16(msg + p + 8);
	if ((gsize)p + 10 + rl > len)
		return -1;
	if (type)
		*type = dns_get16(msg + p);
	if (klass)
		*klass = dns_get16(msg + p + 2);
	if (ttl)
		*ttl = dns_get32(msg + p + 4);
	if (ttl_off)
		*ttl_off = p + 4;
	if (rdata)
		*rdata = p + 10;
	if (rdlen)
		*rdlen = rl;
	return p + 10 + rl;
}

gboolean dns_parse_query(const guint8 *msg, gsize len, DnsQuery *q, int *rcode)
{
	guint16 qd, an, ns, ar;
	gssize off;

	memset(q, 0, sizeof(*q));
	*rcode = -1;
	if (len < DNS_HEADER_SIZE)
		return FALSE;
	q->id = dns_get16(msg);
	q->flags = dns_get16(msg + 2);
	/* Answers sent to us are dropped, not answered */
	if (q->flags & DNS_FLAG_QR)
		return FALSE;
	qd = dns_get16(msg + 4);
	an = dns_get16(msg + 6);
	ns = dns_get16(msg + 8);
	ar = dns_get16(msg + 10);
	*rcode = DNS_RCODE_FORMERR;
	if (DNS_OPCODE(q->flags) != 0) {
		*rcode = DNS_RCODE_NOTIMP;
		return FALSE;
	}
	if (qd != 1)
		return FALSE;
	off = dns_read_name(msg, len, DNS_HEADER_SIZE, q->name, sizeof(q->name), &q->odd_name);
	if (off < 0 || (gsize)off + 4 > len)
		return FALSE;
	q->type = dns_get16(msg + off);
	q->klass = dns_get16(msg + off + 2);
	q->question_end = off + 4;
	off += 4;

	/* Answer and authority of a query are skipped; EDNS is in the additional section */
	for (guint i = 0; i < (guint)an + ns; i++) {
		off = parse_rr(msg, len, off, NULL, NULL, NULL, NULL, NULL, NULL);
		if (off < 0)
			return FALSE;
	}
	for (guint i = 0; i < ar; i++) {
		guint16 type, klass;
		guint32 ttl;

		off = parse_rr(msg, len, off, &type, &klass, &ttl, NULL, NULL, NULL);
		if (off < 0)
			return FALSE;
		if (type == DNS_TYPE_OPT) {
			q->edns = TRUE;
			/* RFC 6891: below 512 counts as 512 */
			q->udp_size = MAX(klass, 512);
			q->do_bit = (ttl & DNS_EDNS_DO) != 0;
		}
	}
	*rcode = DNS_RCODE_NOERROR;
	return TRUE;
}

/* Appends a name given as text (as dns_read_name writes it) in wire format */
static gboolean append_name(GByteArray *m, const char *name)
{
	const char *p = name;
	guint8 label[64];
	gsize total = 0;

	while (*p) {
		guint l = 0;

		while (*p && *p != '.') {
			guint8 c = (guint8)*p;

			if (c == '\\' && g_ascii_isdigit(p[1]) && g_ascii_isdigit(p[2]) && g_ascii_isdigit(p[3])) {
				c = (guint8)((p[1] - '0') * 100 + (p[2] - '0') * 10 + (p[3] - '0'));
				p += 3;
			}
			if (l >= 63)
				return FALSE;
			label[l++] = c;
			p++;
		}
		if (l == 0)
			return FALSE;
		total += l + 1;
		if (total > DNS_NAME_MAX - 1)
			return FALSE;
		g_byte_array_append(m, (guint8[]){l}, 1);
		g_byte_array_append(m, label, l);
		if (*p == '.')
			p++;
	}
	g_byte_array_append(m, (guint8[]){0}, 1);
	return TRUE;
}

static void append16(GByteArray *m, guint16 v)
{
	guint8 b[2];

	dns_put16(b, v);
	g_byte_array_append(m, b, 2);
}

static void append32(GByteArray *m, guint32 v)
{
	guint8 b[4];

	dns_put32(b, v);
	g_byte_array_append(m, b, 4);
}

static void append_opt(GByteArray *m, guint16 udp_size, gboolean do_bit)
{
	g_byte_array_append(m, (guint8[]){0}, 1); /* root */
	append16(m, DNS_TYPE_OPT);
	append16(m, udp_size);
	append32(m, do_bit ? DNS_EDNS_DO : 0);
	append16(m, 0);
}

static GByteArray *header(guint16 id, guint16 flags, guint16 qd, guint16 an, guint16 ns, guint16 ar)
{
	GByteArray *m = g_byte_array_sized_new(512);

	append16(m, id);
	append16(m, flags);
	append16(m, qd);
	append16(m, an);
	append16(m, ns);
	append16(m, ar);
	return m;
}

GByteArray *dns_build_query(const DnsQuery *q, guint16 id, guint16 udp_size)
{
	GByteArray *m = header(id, q->flags & (DNS_FLAG_RD | DNS_FLAG_CD | DNS_FLAG_AD), 1, 0, 0, 1);

	if (!append_name(m, q->name) && q->name[0]) {
		g_byte_array_unref(m);
		return NULL;
	}
	append16(m, q->type);
	append16(m, q->klass);
	append_opt(m, udp_size, q->do_bit);
	return m;
}

GByteArray *dns_build_simple_query(const char *name, guint16 type, guint16 id)
{
	GByteArray *m = header(id, DNS_FLAG_RD, 1, 0, 0, 1);

	if (!append_name(m, name)) {
		g_byte_array_unref(m);
		return NULL;
	}
	append16(m, type);
	append16(m, DNS_CLASS_IN);
	append_opt(m, 1232, FALSE);
	return m;
}

GByteArray *dns_build_reply(const DnsQuery *q, int rcode, gboolean null_ip, guint32 ttl)
{
	gboolean record = rcode == DNS_RCODE_NOERROR && null_ip && q->klass == DNS_CLASS_IN &&
			  (q->type == DNS_TYPE_A || q->type == DNS_TYPE_AAAA);
	guint16 flags = DNS_FLAG_QR | (q->flags & (DNS_FLAG_RD | DNS_FLAG_CD)) | DNS_FLAG_RA | (rcode & 0xf);
	GByteArray *m = header(q->id, flags, 1, record ? 1 : 0, 0, q->edns ? 1 : 0);

	if (!append_name(m, q->name)) {
		/* A name we could not write back (only from a broken query): no question then */
		g_byte_array_set_size(m, 0);
		g_byte_array_unref(m);
		m = header(q->id, (flags & ~0xf) | DNS_RCODE_FORMERR, 0, 0, 0, 0);
		return m;
	}
	append16(m, q->type);
	append16(m, q->klass);
	if (record) {
		static const guint8 zero[16];
		guint16 size = q->type == DNS_TYPE_A ? 4 : 16;

		append16(m, 0xc000 | DNS_HEADER_SIZE); /* pointer to the question's name */
		append16(m, q->type);
		append16(m, DNS_CLASS_IN);
		append32(m, ttl);
		append16(m, size);
		g_byte_array_append(m, zero, size);
	}
	if (q->edns)
		append_opt(m, 1232, q->do_bit);
	return m;
}

void dns_answer_clear(DnsAnswer *a)
{
	g_clear_pointer(&a->cnames, g_ptr_array_unref);
	if (a->summary)
		g_string_free(a->summary, TRUE);
	a->summary = NULL;
}

static void summary_add(DnsAnswer *a, const char *text)
{
	/* Long answers (many addresses) are cut, the log only needs an idea */
	if (a->summary->len > 200)
		return;
	if (a->summary->len)
		g_string_append(a->summary, ", ");
	g_string_append(a->summary, text);
	if (a->summary->len > 200)
		g_string_append(a->summary, " …");
}

gboolean dns_parse_answer(const guint8 *msg, gsize len, DnsAnswer *a)
{
	guint16 qd, an, ns, ar;
	gssize off;
	gboolean odd;

	memset(a, 0, sizeof(*a));
	a->cnames = g_ptr_array_new_with_free_func(g_free);
	a->summary = g_string_new(NULL);
	if (len < DNS_HEADER_SIZE)
		return FALSE;
	a->id = dns_get16(msg);
	a->flags = dns_get16(msg + 2);
	a->rcode = DNS_RCODE(a->flags);
	qd = dns_get16(msg + 4);
	an = dns_get16(msg + 6);
	ns = dns_get16(msg + 8);
	ar = dns_get16(msg + 10);
	if (!(a->flags & DNS_FLAG_QR) || qd != 1)
		return FALSE;
	off = dns_read_name(msg, len, DNS_HEADER_SIZE, a->name, sizeof(a->name), &odd);
	if (off < 0 || (gsize)off + 4 > len)
		return FALSE;
	a->type = dns_get16(msg + off);
	a->klass = dns_get16(msg + off + 2);
	off += 4;
	/* A truncated answer may end anywhere; what is there is still read */
	for (guint i = 0; i < (guint)an + ns + ar; i++) {
		guint16 type, klass, rdlen;
		guint32 ttl;
		gsize rdata;
		gssize next = parse_rr(msg, len, off, &type, &klass, &ttl, NULL, &rdata, &rdlen);

		if (next < 0)
			return (a->flags & DNS_FLAG_TC) != 0;
		if (type != DNS_TYPE_OPT) {
			gboolean negative_soa = i >= an && i < (guint)an + ns && type == DNS_TYPE_SOA;

			/* Answers count, and the SOA of a negative answer; additional records may come
			 * with their own (longer) TTLs and don't decide how long the answer lives */
			if (i < an || negative_soa) {
				guint32 t = ttl;

				/* RFC 2308: a negative answer lives min(SOA TTL, SOA minimum) */
				if (negative_soa && rdlen >= 22) {
					gssize s = dns_skip_name(msg, len, rdata);

					if (s >= 0)
						s = dns_skip_name(msg, len, s);
					if (s >= 0 && (gsize)s + 20 <= rdata + rdlen)
						t = MIN(t, dns_get32(msg + s + 16));
				}
				if (!a->has_ttl || t < a->min_ttl)
					a->min_ttl = t;
				a->has_ttl = TRUE;
			}
		}
		if (i < an) {
			char text[DNS_NAME_TEXT];

			a->answers++;
			if (type == DNS_TYPE_A && rdlen == 4) {
				inet_ntop(AF_INET, msg + rdata, text, sizeof(text));
				summary_add(a, text);
			} else if (type == DNS_TYPE_AAAA && rdlen == 16) {
				inet_ntop(AF_INET6, msg + rdata, text, sizeof(text));
				summary_add(a, text);
			} else if (type == DNS_TYPE_CNAME &&
				   dns_read_name(msg, len, rdata, text, sizeof(text), &odd) >= 0) {
				g_ptr_array_add(a->cnames, g_strdup(text));
				summary_add(a, text);
			}
		}
		off = next;
	}
	return TRUE;
}

gboolean dns_answer_matches(const DnsAnswer *a, const DnsQuery *q)
{
	/* Both names are already lower case */
	return a->type == q->type && a->klass == q->klass && !strcmp(a->name, q->name);
}

gboolean dns_prepare_reply(GByteArray *msg, guint16 id, guint32 age, gboolean client_edns, gsize max_size)
{
	guint8 *m = msg->data;
	gsize len = msg->len;
	guint qd, count;  /* not 16 bits: the three record counts added up may exceed 65535 */
	gssize off;

	if (len < DNS_HEADER_SIZE)
		return FALSE;
	qd = dns_get16(m + 4);
	count = (guint)dns_get16(m + 6) + dns_get16(m + 8) + dns_get16(m + 10);
	off = DNS_HEADER_SIZE;
	for (guint i = 0; i < qd; i++) {
		off = dns_skip_name(m, len, off);
		if (off < 0 || (gsize)off + 4 > len)
			return FALSE;
		off += 4;
	}
	gsize question_end = off;

	for (guint i = 0; i < count; i++) {
		guint16 type;
		guint32 ttl;
		gsize ttl_off, rr_start = off;
		gssize next = parse_rr(m, len, off, &type, NULL, &ttl, &ttl_off, NULL, NULL);

		if (next < 0)
			return FALSE;
		if (type == DNS_TYPE_OPT) {
			/* The OPT "TTL" is flags, not a lifetime. Without EDNS from the client, it goes. */
			/* Only the OPT of the additional section (ARCOUNT > 0): one placed elsewhere by
			 * a broken server must not wrap ARCOUNT to 65535 */
			if (!client_edns && i == count - 1u && (gsize)next == len && dns_get16(m + 10) > 0) {
				g_byte_array_set_size(msg, rr_start);
				m = msg->data;
				len = msg->len;
				dns_put16(m + 10, dns_get16(m + 10) - 1);
				break;
			}
		} else if (age) {
			dns_put32(m + ttl_off, ttl > age ? ttl - age : 0);
		}
		off = next;
	}
	dns_put16(m, id);
	if (len > max_size) {
		/* Header and question only, TC tells the client to come back over TCP */
		g_byte_array_set_size(msg, question_end);
		m = msg->data;
		dns_put16(m + 2, dns_get16(m + 2) | DNS_FLAG_TC);
		dns_put16(m + 6, 0);
		dns_put16(m + 8, 0);
		dns_put16(m + 10, 0);
	}
	return TRUE;
}

GPtrArray *dns_answer_addresses(const guint8 *msg, gsize len)
{
	GPtrArray *out = g_ptr_array_new_with_free_func(g_free);
	guint16 an;
	gssize off;

	if (len < DNS_HEADER_SIZE || !(dns_get16(msg + 2) & DNS_FLAG_QR) || dns_get16(msg + 4) != 1 ||
	    DNS_RCODE(dns_get16(msg + 2)) != DNS_RCODE_NOERROR)
		return out;
	an = dns_get16(msg + 6);
	off = dns_skip_name(msg, len, DNS_HEADER_SIZE);
	if (off < 0 || (gsize)off + 4 > len)
		return out;
	off += 4;
	for (guint i = 0; i < an; i++) {
		guint16 type, klass, rdlen;
		gsize rdata;
		char text[INET6_ADDRSTRLEN];
		gssize next = parse_rr(msg, len, off, &type, &klass, NULL, NULL, &rdata, &rdlen);

		if (next < 0)
			break;
		if (klass == DNS_CLASS_IN && type == DNS_TYPE_A && rdlen == 4 &&
		    inet_ntop(AF_INET, msg + rdata, text, sizeof(text)))
			g_ptr_array_add(out, g_strdup(text));
		else if (klass == DNS_CLASS_IN && type == DNS_TYPE_AAAA && rdlen == 16 &&
			 inet_ntop(AF_INET6, msg + rdata, text, sizeof(text)))
			g_ptr_array_add(out, g_strdup(text));
		off = next;
	}
	return out;
}

const char *dns_type_name(guint16 type, char buf[16])
{
	static const struct {
		guint16 type;
		const char *name;
	} names[] = {
		{DNS_TYPE_A, "A"},	   {DNS_TYPE_NS, "NS"},	    {DNS_TYPE_CNAME, "CNAME"}, {DNS_TYPE_SOA, "SOA"},
		{DNS_TYPE_PTR, "PTR"},	   {DNS_TYPE_MX, "MX"},	    {DNS_TYPE_TXT, "TXT"},     {DNS_TYPE_AAAA, "AAAA"},
		{DNS_TYPE_SRV, "SRV"},	   {DNS_TYPE_SVCB, "SVCB"}, {DNS_TYPE_HTTPS, "HTTPS"}, {DNS_TYPE_ANY, "ANY"},
		{43, "DS"},		   {48, "DNSKEY"},	    {46, "RRSIG"},	       {257, "CAA"},
	};

	for (guint i = 0; i < G_N_ELEMENTS(names); i++)
		if (names[i].type == type)
			return names[i].name;
	snprintf(buf, 16, "TYPE%u", type);
	return buf;
}
