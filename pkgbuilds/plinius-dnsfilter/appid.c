// SPDX-License-Identifier: MIT
// appid: see appid.h
#include "appid.h"

#include <errno.h>
#include <linux/inet_diag.h>
#include <linux/netlink.h>
#include <linux/rtnetlink.h>
#include <linux/sock_diag.h>
#include <netinet/in.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#ifndef INET_DIAG_CGROUP_ID
#define INET_DIAG_CGROUP_ID 21
#endif

#define CGROUP_ROOT "/sys/fs/cgroup"
/* Rescan the cgroup tree for an unknown id at most this often */
#define RESCAN_US (1 * G_USEC_PER_SEC)
/* Depth of the cgroup tree that is read (user.slice/user-N.slice/user@N.service/app.slice/x) */
#define MAX_DEPTH 8

static int diag_fd = -1;
static GHashTable *cgroups; /* inode (guint64*) -> path relative to the root */
static gint64 last_scan;

/* ---- Names (as plinius-energy) ---- */

/* systemd's \xNN escapes in unit names */
static char *unescape_unit(const char *text)
{
	GString *out = g_string_sized_new(strlen(text));

	for (const char *p = text; *p; p++) {
		if (p[0] == '\\' && p[1] == 'x' && g_ascii_isxdigit(p[2]) && g_ascii_isxdigit(p[3])) {
			g_string_append_c(out, g_ascii_xdigit_value(p[2]) * 16 + g_ascii_xdigit_value(p[3]));
			p += 3;
		} else {
			g_string_append_c(out, *p);
		}
	}
	return g_strdelimit(g_string_free(out, FALSE), "\t\n\r", ' ');
}

/* The part of @text between @prefix and @suffix; for a scope without the "-<pid>" before it */
static char *between(const char *text, const char *prefix, const char *suffix)
{
	const char *start, *end;

	if (!g_str_has_prefix(text, prefix) || !g_str_has_suffix(text, suffix))
		return NULL;
	start = text + strlen(prefix);
	end = text + strlen(text) - strlen(suffix);
	if (!strcmp(suffix, ".scope")) {
		const char *digits = end;

		while (digits > start && g_ascii_isdigit(digits[-1]))
			digits--;
		if (digits == end || digits <= start + 1 || digits[-1] != '-')
			return NULL;
		end = digits - 1;
	}
	return end > start ? g_strndup(start, end - start) : NULL;
}

/* D-Bus activated apps: "dbus-:1.2-org.gnome.Settings@3.service" in "app-dbus-:1.2-org.gnome.Settings.slice" */
static char *dbus_app(const char *unit)
{
	g_autofree char *rest = between(unit, "dbus-:", ".service");
	const char *dash, *at;

	if (!rest)
		rest = between(unit, "app-dbus-:", ".slice");
	if (!rest || !(dash = strchr(rest, '-')) || !dash[1])
		return NULL;
	at = strrchr(dash, '@');
	return at ? (at > dash + 1 ? g_strndup(dash + 1, at - dash - 1) : NULL) : g_strdup(dash + 1);
}

static gboolean is_unit(const char *name)
{
	static const char *const suffixes[] = {".service", ".scope", ".slice", ".mount", ".socket", ".swap"};

	for (guint i = 0; i < G_N_ELEMENTS(suffixes); i++)
		if (g_str_has_suffix(name, suffixes[i]))
			return TRUE;
	return FALSE;
}

char *appid_cgroup_key(const char *rel)
{
	g_auto(GStrv) parts = NULL;
	g_autofree char *unit = NULL;
	char *id;
	int n;

	if (!*rel)
		return g_strdup("kernel");
	if (g_str_has_prefix(rel, "lxc.payload.") || g_str_has_prefix(rel, "lxc.monitor.")) {
		/* An Android app by its uid (uid_10123/pid_4567), else the container's own cgroup */
		const char *uid = strstr(rel, "/uid_");
		g_autofree char *last = g_path_get_basename(rel);

		if (uid) {
			const char *end = strchr(uid + 1, '/');

			return g_strdup_printf("android/%.*s", (int)(end ? end - uid - 1 : (long)strlen(uid + 1)), uid + 1);
		}
		return g_strconcat("android/", last, NULL);
	}
	parts = g_strsplit(rel, "/", -1);
	for (n = g_strv_length(parts) - 1; n > 0 && !is_unit(parts[n]); n--)
		;
	unit = unescape_unit(parts[n]);
	/* Apps the shell starts, Flatpak apps, autostart, D-Bus activated */
	if ((id = between(unit, "app-gnome-", ".scope")) || (id = between(unit, "app-flatpak-", ".scope")) ||
	    (id = dbus_app(unit)) || (id = between(unit, "app-", ".scope"))) {
		char *key = g_strconcat("app/", id, NULL);

		g_free(id);
		return key;
	}
	if (g_str_has_prefix(unit, "app-") && strchr(unit, '@') && g_str_has_suffix(unit, ".service")) {
		g_autofree char *app = g_strndup(unit + 4, strchr(unit, '@') - unit - 4);

		return g_strconcat("app/", app, NULL);
	}
	if (g_str_has_prefix(rel, "user.slice/")) {
		if (g_str_has_prefix(unit, "session-") && g_str_has_suffix(unit, ".scope"))
			return g_strdup("user/session.scope");
		if (!strcmp(unit, "init.scope"))
			return g_strdup("user/systemd");
		return g_strconcat("user/", unit, NULL);
	}
	return g_strconcat("system/", unit, NULL);
}

/* ---- cgroup ids ---- */

static void scan(const char *dir, const char *rel, int depth)
{
	g_autoptr(GDir) d = NULL;
	const char *name;
	struct stat st;

	if (stat(dir, &st) == 0) {
		guint64 *ino = g_new(guint64, 1);

		*ino = st.st_ino;
		g_hash_table_replace(cgroups, ino, g_strdup(rel));
	}
	if (depth >= MAX_DEPTH || !(d = g_dir_open(dir, 0, NULL)))
		return;
	while ((name = g_dir_read_name(d))) {
		g_autofree char *path = g_build_filename(dir, name, NULL);
		g_autofree char *sub = *rel ? g_strconcat(rel, "/", name, NULL) : g_strdup(name);

		/* Only directories are cgroups; lstat, the tree has no links */
		if (lstat(path, &st) == 0 && S_ISDIR(st.st_mode))
			scan(path, sub, depth + 1);
	}
}

static const char *cgroup_path(guint64 id)
{
	const char *rel;

	if (!cgroups)
		cgroups = g_hash_table_new_full(g_int64_hash, g_int64_equal, g_free, g_free);
	if ((rel = g_hash_table_lookup(cgroups, &id)))
		return rel;
	/* A new app or service since the last scan; not more than once a second */
	if (g_get_monotonic_time() - last_scan < RESCAN_US)
		return NULL;
	last_scan = g_get_monotonic_time();
	g_hash_table_remove_all(cgroups);
	scan(CGROUP_ROOT, "", 0);
	return g_hash_table_lookup(cgroups, &id);
}

/* ---- Sockets ---- */

static gboolean diag_open(void)
{
	struct timeval tv = {0, 200 * 1000};

	if (diag_fd >= 0)
		return TRUE;
	diag_fd = socket(AF_NETLINK, SOCK_RAW | SOCK_CLOEXEC, NETLINK_SOCK_DIAG);
	if (diag_fd < 0)
		return FALSE;
	/* Never hold up a question for long */
	setsockopt(diag_fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
	return TRUE;
}

static void diag_close(void)
{
	if (diag_fd >= 0)
		close(diag_fd);
	diag_fd = -1;
}

/* The local address and port of @peer, in the form of inet_diag_sockid */
static gboolean peer_id(const struct sockaddr_storage *peer, int *family, guint16 *port, guint32 addr[4])
{
	memset(addr, 0, 16);
	if (peer->ss_family == AF_INET) {
		const struct sockaddr_in *sin = (const struct sockaddr_in *)peer;

		*family = AF_INET;
		*port = sin->sin_port;
		addr[0] = sin->sin_addr.s_addr;
		return TRUE;
	}
	if (peer->ss_family == AF_INET6) {
		const struct sockaddr_in6 *sin6 = (const struct sockaddr_in6 *)peer;

		*family = AF_INET6;
		*port = sin6->sin6_port;
		memcpy(addr, &sin6->sin6_addr, 16);
		return TRUE;
	}
	return FALSE;
}

/* Dumps the sockets of @family/@protocol whose local port is @port (a filter in the kernel) and
 * returns the cgroup id of the one with the local address @addr; 0 if none */
static guint64 find_socket(int family, int protocol, guint16 port, const guint32 addr[4])
{
	struct {
		struct nlmsghdr nlh;
		struct inet_diag_req_v2 req;
		struct nlattr attr;
		struct inet_diag_bc_op op;
		struct inet_diag_bc_op port_op;
	} msg;
	static const guint32 any[4];
	guint8 buf[16384];
	guint64 found = 0;
	gboolean done = FALSE;

	memset(&msg, 0, sizeof(msg));
	msg.nlh.nlmsg_len = sizeof(msg);
	msg.nlh.nlmsg_type = SOCK_DIAG_BY_FAMILY;
	msg.nlh.nlmsg_flags = NLM_F_REQUEST | NLM_F_DUMP;
	msg.nlh.nlmsg_seq = (guint32)g_random_int();
	msg.req.sdiag_family = family;
	msg.req.sdiag_protocol = protocol;
	msg.req.idiag_states = ~0u;
	/* Bytecode: source port == @port (the "no" jump skips past the end: reject) */
	msg.attr.nla_len = sizeof(msg.attr) + sizeof(msg.op) + sizeof(msg.port_op);
	msg.attr.nla_type = INET_DIAG_REQ_BYTECODE;
	msg.op.code = INET_DIAG_BC_S_EQ;
	msg.op.yes = sizeof(msg.op) + sizeof(msg.port_op);
	msg.op.no = sizeof(msg.op) + sizeof(msg.port_op) + 4;
	msg.port_op.no = g_ntohs(port);

	if (send(diag_fd, &msg, sizeof(msg), 0) < 0) {
		diag_close();
		return 0;
	}
	while (!done) {
		ssize_t n = recv(diag_fd, buf, sizeof(buf), 0);

		if (n <= 0) {
			/* Timeout or error: the rest of the dump would come with the next request */
			diag_close();
			return found;
		}
		for (struct nlmsghdr *h = (struct nlmsghdr *)buf; NLMSG_OK(h, (guint32)n); h = NLMSG_NEXT(h, n)) {
			struct inet_diag_msg *m;
			int len;

			if (h->nlmsg_seq != msg.nlh.nlmsg_seq)
				continue;
			if (h->nlmsg_type == NLMSG_DONE || h->nlmsg_type == NLMSG_ERROR) {
				done = TRUE;
				break;
			}
			m = NLMSG_DATA(h);
			/* The address, or none for a socket that sends without connect() (bound to "any") */
			if (found || m->id.idiag_sport != port ||
			    (memcmp(m->id.idiag_src, addr, family == AF_INET ? 4 : 16) &&
			     memcmp(m->id.idiag_src, any, family == AF_INET ? 4 : 16)))
				continue;
			len = h->nlmsg_len - NLMSG_LENGTH(sizeof(*m));
			for (struct rtattr *a = (struct rtattr *)(m + 1); RTA_OK(a, len); a = RTA_NEXT(a, len)) {
				if (a->rta_type == INET_DIAG_CGROUP_ID && RTA_PAYLOAD(a) >= 8) {
					memcpy(&found, RTA_DATA(a), 8);
					break;
				}
			}
		}
	}
	return found;
}

char *appid_for_peer(int protocol, const struct sockaddr_storage *peer)
{
	int family;
	guint16 port;
	guint32 addr[4];
	guint64 id;
	const char *rel;

	if (!peer_id(peer, &family, &port, addr) || !diag_open())
		return NULL;
	id = find_socket(family, protocol, port, addr);
	/* A v6 socket may send from a v4-mapped address (dual-stack client): ask the other family */
	if (!id && family == AF_INET) {
		guint32 mapped[4] = {0, 0, g_htonl(0xffff), addr[0]};

		id = find_socket(AF_INET6, protocol, port, mapped);
	}
	if (!id || !(rel = cgroup_path(id)))
		return NULL;
	return appid_cgroup_key(rel);
}

void appid_shutdown(void)
{
	diag_close();
	g_clear_pointer(&cgroups, g_hash_table_unref);
}
