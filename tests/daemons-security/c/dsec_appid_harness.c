/*
 * Harness for plinius-dnsfilter's appid.c (which app asked: the client socket through sock_diag,
 * its cgroup id, the cgroup's path). Included as a test copy whose cgroup root is a directory of
 * the test (APPID_ROOT): the ids are the inodes of its directories, as on cgroup2.
 *
 * The netlink socket is the kernel's ("real") or a scripted one ("fake"): the request is recorded,
 * the answers are the dump messages queued with "msg" (then a timeout, as SO_RCVTIMEO gives).
 *
 * Commands (one output line each):
 *   real / fake / none           which netlink (none: the kernel has no sock_diag)
 *   key REL                      appid_cgroup_key ("-" for the root)
 *   bind udp4|udp6|tcp4|tcp6     a socket of this process on loopback: "port N"
 *   peer 4|6 ADDR PORT           the address appid_for_peer is asked for
 *   lookup udp|tcp               appid_for_peer: the key or "-"
 *   find 4|6 udp|tcp             find_socket for the peer: the cgroup id (real netlink)
 *   msg SPORT ADDR CGID [badseq|noid|error|done|short]   a dump message for the next answer
 *   batch                        the queued messages so far are one answer (recv)
 *   request                      the last request: family, protocol, port, filter bytecode
 *   ino PATH                     inode of a directory (the id the kernel would give)
 *   mkdir PATH                   a new cgroup (an app started since the last scan)
 *   msgdir SPORT ADDR PATH       "msg" with the inode of PATH as it is now
 *   peerbound                    the peer is the socket bound last
 *   clock US                     the monotonic clock forward (rescans: once a second)
 *   shutdown                     appid_shutdown
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <glib.h>
#include <linux/inet_diag.h>
#include <linux/netlink.h>
#include <linux/rtnetlink.h>
#include <linux/sock_diag.h>
#include <netinet/in.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <unistd.h>

static gboolean fake_nl, no_nl;
static gint64 clock_off;
static guint8 last_req[256];
static gsize last_req_len;
static GQueue answers = G_QUEUE_INIT; /* GByteArray*: one recv() each */
static GByteArray *building;
static guint32 req_seq;

static int fake_socket(int domain, int type, int protocol)
{
	if (domain == AF_NETLINK && no_nl) {
		errno = EAFNOSUPPORT;
		return -1;
	}
	if (domain == AF_NETLINK && fake_nl)
		return 1000; /* a number no real descriptor of this harness gets */
	return socket(domain, type, protocol);
}

static ssize_t fake_send(int fd, const void *buf, size_t len, int flags)
{
	if (fd != 1000)
		return send(fd, buf, len, flags);
	last_req_len = MIN(len, sizeof(last_req));
	memcpy(last_req, buf, last_req_len);
	memcpy(&req_seq, (const guint8 *)buf + offsetof(struct nlmsghdr, nlmsg_seq), 4);
	return (ssize_t)len;
}

static ssize_t fake_recv(int fd, void *buf, size_t len, int flags)
{
	GByteArray *a;

	if (fd != 1000)
		return recv(fd, buf, len, flags);
	if (!(a = g_queue_pop_head(&answers))) {
		errno = EAGAIN;
		return -1;
	}
	/* The dump answers the request just sent: its sequence number (+1 marks an old dump's) */
	for (gsize off = 0; off + sizeof(struct nlmsghdr) <= a->len;) {
		struct nlmsghdr *h = (struct nlmsghdr *)(a->data + off);

		h->nlmsg_seq += req_seq;
		off += NLMSG_ALIGN(h->nlmsg_len);
	}
	memcpy(buf, a->data, MIN(len, a->len));
	len = MIN(len, a->len);
	g_byte_array_unref(a);
	return (ssize_t)len;
}

static int fake_close(int fd)
{
	return fd == 1000 ? 0 : close(fd);
}

static gint64 fake_mono(void)
{
	return g_get_monotonic_time() + clock_off;
}

#define socket fake_socket
#define send fake_send
#define recv fake_recv
#define close fake_close
#define g_get_monotonic_time fake_mono
static const char *appid_root;
#include "appid.c"
#undef socket
#undef send
#undef recv
#undef close
#undef g_get_monotonic_time

static struct sockaddr_storage peer, bound;

static void add_msg(guint16 sport_net, const char *addr, guint64 cgid, const char *how)
{
	guint8 m[NLMSG_SPACE(sizeof(struct inet_diag_msg) + RTA_SPACE(8))] = {0};
	struct nlmsghdr *h = (struct nlmsghdr *)m;
	struct inet_diag_msg *d = NLMSG_DATA(h);
	struct rtattr *a = (struct rtattr *)(d + 1);
	gboolean v6 = strchr(addr, ':') != NULL;

	h->nlmsg_len = NLMSG_LENGTH(sizeof(*d) + RTA_LENGTH(8));
	h->nlmsg_type = SOCK_DIAG_BY_FAMILY;
	h->nlmsg_seq = !strcmp(how, "badseq") ? 1 : 0;
	if (!strcmp(how, "done") || !strcmp(how, "error")) {
		h->nlmsg_type = !strcmp(how, "done") ? NLMSG_DONE : NLMSG_ERROR;
		h->nlmsg_len = NLMSG_LENGTH(4);
	}
	d->idiag_family = v6 ? AF_INET6 : AF_INET;
	d->id.idiag_sport = sport_net;
	inet_pton(v6 ? AF_INET6 : AF_INET, addr, d->id.idiag_src);
	a->rta_type = !strcmp(how, "noid") ? INET_DIAG_CGROUP_ID + 1 : INET_DIAG_CGROUP_ID;
	a->rta_len = !strcmp(how, "short") ? RTA_LENGTH(4) : RTA_LENGTH(8);
	memcpy(RTA_DATA(a), &cgid, 8);
	if (!building)
		building = g_byte_array_new();
	g_byte_array_append(building, m, NLMSG_ALIGN(h->nlmsg_len));
}

int main(void)
{
	static char line[4096];

	setvbuf(stdout, NULL, _IOLBF, 0);
	appid_root = g_getenv("APPID_ROOT");
	while (fgets(line, sizeof line, stdin)) {
		char a[512] = "", b[512] = "", c[512] = "";
		long n;
		unsigned long long id;

		line[strcspn(line, "\n")] = '\0';
		if (!strcmp(line, "real") || !strcmp(line, "fake") || !strcmp(line, "none")) {
			appid_shutdown();
			fake_nl = line[0] == 'f';
			no_nl = line[0] == 'n';
			puts("ok");
		} else if (sscanf(line, "bind %511s", a) == 1) {
			gboolean v6 = a[3] == '6';
			int fd = socket(v6 ? AF_INET6 : AF_INET, a[0] == 'u' ? SOCK_DGRAM : SOCK_STREAM, 0);
			struct sockaddr_storage ss = {0};
			socklen_t len = sizeof(ss);

			if (v6) {
				struct sockaddr_in6 *s6 = (struct sockaddr_in6 *)&ss;

				s6->sin6_family = AF_INET6;
				s6->sin6_addr = in6addr_loopback;
				len = sizeof(*s6);
			} else {
				struct sockaddr_in *s4 = (struct sockaddr_in *)&ss;

				s4->sin_family = AF_INET;
				s4->sin_addr.s_addr = htonl(INADDR_LOOPBACK);
				len = sizeof(*s4);
			}
			bind(fd, (struct sockaddr *)&ss, len);
			if (a[0] == 't')
				listen(fd, 1);
			len = sizeof(ss);
			getsockname(fd, (struct sockaddr *)&ss, &len);
			bound = ss;
			printf("port %u\n", ntohs(v6 ? ((struct sockaddr_in6 *)&ss)->sin6_port : ((struct sockaddr_in *)&ss)->sin_port));
		} else if (sscanf(line, "peer %511s %511s %ld", a, b, &n) == 3) {
			memset(&peer, 0, sizeof(peer));
			if (a[0] == '6') {
				struct sockaddr_in6 *s6 = (struct sockaddr_in6 *)&peer;

				s6->sin6_family = AF_INET6;
				s6->sin6_port = htons((guint16)n);
				inet_pton(AF_INET6, b, &s6->sin6_addr);
			} else if (a[0] == '4') {
				struct sockaddr_in *s4 = (struct sockaddr_in *)&peer;

				s4->sin_family = AF_INET;
				s4->sin_port = htons((guint16)n);
				inet_pton(AF_INET, b, &s4->sin_addr);
			} else {
				peer.ss_family = AF_UNIX;
			}
			puts("ok");
		} else if (sscanf(line, "lookup %511s", a) == 1) {
			g_autofree char *key = appid_for_peer(a[0] == 'u' ? IPPROTO_UDP : IPPROTO_TCP, &peer);

			puts(key ? key : "-");
		} else if (sscanf(line, "find %511s %511s", a, b) == 2) {
			int family;
			guint16 port;
			guint32 addr[4];

			peer_id(&peer, &family, &port, addr);
			if (!diag_open()) {
				printf("error %s\n", g_strerror(errno));
				continue;
			}
			printf("id %" G_GUINT64_FORMAT "\n",
			       find_socket(a[0] == '6' ? AF_INET6 : AF_INET, b[0] == 'u' ? IPPROTO_UDP : IPPROTO_TCP, port, addr));
		} else if (sscanf(line, "msg %ld %511s %llu %511s", &n, b, &id, c) >= 3) {
			add_msg(htons((guint16)n), b, id, c);
			puts("ok");
		} else if (!strcmp(line, "batch")) {
			g_queue_push_tail(&answers, building ? building : g_byte_array_new());
			building = NULL;
			puts("ok");
		} else if (!strcmp(line, "request")) {
			const struct nlmsghdr *h = (const void *)last_req;
			const struct inet_diag_req_v2 *r = NLMSG_DATA(h);
			const struct nlattr *at = (const void *)(r + 1);
			const struct inet_diag_bc_op *op = (const void *)(at + 1);

			if (!last_req_len) {
				puts("none");
				continue;
			}
			printf("type %u flags %#x family %u proto %u states %#x attr %u/%u op %u/%u/%u port %u/%u\n",
			       h->nlmsg_type, h->nlmsg_flags, r->sdiag_family, r->sdiag_protocol, r->idiag_states,
			       at->nla_type, at->nla_len, op[0].code, op[0].yes, op[0].no, op[1].code, op[1].no);
		} else if (sscanf(line, "ino %511s", a) == 1) {
			struct stat st;

			if (stat(a, &st) == 0)
				printf("%llu\n", (unsigned long long)st.st_ino);
			else
				puts("-");
		} else if (sscanf(line, "msgdir %ld %511s %511s", &n, b, c) == 3) {
			struct stat st;

			add_msg(htons((guint16)n), b, stat(c, &st) == 0 ? st.st_ino : 0, "");
			puts("ok");
		} else if (!strcmp(line, "peerbound")) {
			peer = bound;
			puts("ok");
		} else if (sscanf(line, "key %511s", a) == 1) {
			g_autofree char *key = appid_cgroup_key(strcmp(a, "-") ? a : "");

			puts(key);
		} else if (sscanf(line, "mkdir %511s", a) == 1) {
			puts(mkdir(a, 0755) == 0 ? "ok" : "error");
		} else if (sscanf(line, "clock %ld", &n) == 1) {
			clock_off += n;
			puts("ok");
		} else if (!strcmp(line, "shutdown")) {
			appid_shutdown();
			puts("ok");
		} else {
			printf("<unknown command: %.60s>\n", line);
		}
	}
	return 0;
}
