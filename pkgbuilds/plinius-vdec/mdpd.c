/* SPDX-License-Identifier: MIT */
/*
 * plinius-vdec-mdpd: the MDP colour conversion of the hardware video decoder for every app, as a
 * user of its own (see mdp-proto.h for why and for the protocol).
 *
 * Socket-activated (plinius-vdec-mdpd.socket hands over fd 3), exits IDLE_EXIT seconds after the
 * last client left. Each connection gets a DpBlitStream of its own, created on its first
 * request; requests are served one at a time (one conversion takes about 3 ms).
 *
 * What a client can make the MDP do is limited to converting between two dma-bufs it sends:
 * both must be dma-bufs, both large enough for the picture asked for (sizes from the buffers
 * themselves, not from the client), the picture within PV_MDP_MIN_SIZE..PV_MDP_MAX_SIZE.
 *
 * For tests: plinius-vdec-mdpd --listen PATH [--fake] (--fake answers without the MDP, for the
 * checks on a computer without libhybris).
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#include <glib.h>
#include <gio/gio.h>

#include "mdp.h"
#include "mdp-proto.h"

#define IDLE_EXIT   60 /* s */
#define MAX_CLIENTS 16

typedef struct {
	int fd; /* -1: free */
	PvMdp *mdp;
} Client;

static Client clients[MAX_CLIENTS];
static gboolean fake;

/* Size of a dma-buf, or -1 if @fd is none (only dma-bufs reach the MDP) */
static long long dmabuf_size(int fd)
{
	char path[64], target[64];
	ssize_t n;
	off_t size;

	snprintf(path, sizeof path, "/proc/self/fd/%d", fd);
	n = readlink(path, target, sizeof target - 1);
	if (n < 0)
		return -1;
	target[n] = '\0';
	if (strncmp(target, "/dmabuf:", 8) != 0)
		return -1;
	size = lseek(fd, 0, SEEK_END);
	return size < 0 ? -1 : (long long)size;
}

static void reply(int fd, gboolean ok, const char *fmt, ...) G_GNUC_PRINTF(3, 4);

static void reply(int fd, gboolean ok, const char *fmt, ...)
{
	PvMdpReply r = { .ok = ok };
	va_list args;

	if (fmt) {
		va_start(args, fmt);
		vsnprintf(r.message, sizeof r.message, fmt, args);
		va_end(args);
	}
	if (send(fd, &r, sizeof r, MSG_NOSIGNAL) < 0)
		g_debug("reply: %s", g_strerror(errno));
}

/* Checks one request and converts. FALSE: the connection is closed (malformed message). */
static gboolean serve(Client *c)
{
	PvMdpRequest req;
	char control[CMSG_SPACE(2 * sizeof(int))];
	struct iovec iov = { .iov_base = &req, .iov_len = sizeof req };
	struct msghdr msg = { .msg_iov = &iov, .msg_iovlen = 1,
		.msg_control = control, .msg_controllen = sizeof control };
	int fds[2] = { -1, -1 }, nfds = 0;
	g_autoptr(GError) error = NULL;
	long long src, dst;
	gboolean keep = TRUE;
	ssize_t n;

	n = recvmsg(c->fd, &msg, MSG_CMSG_CLOEXEC);
	if (n <= 0)
		return FALSE;
	for (struct cmsghdr *cm = CMSG_FIRSTHDR(&msg); cm; cm = CMSG_NXTHDR(&msg, cm)) {
		if (cm->cmsg_level != SOL_SOCKET || cm->cmsg_type != SCM_RIGHTS)
			continue;
		int count = (cm->cmsg_len - CMSG_LEN(0)) / sizeof(int);
		for (int i = 0; i < count; i++) {
			int fd;
			memcpy(&fd, CMSG_DATA(cm) + i * sizeof(int), sizeof fd);
			if (nfds < 2)
				fds[nfds] = fd;
			else
				close(fd);
			nfds++;
		}
	}

	if (n != sizeof req || req.magic != PV_MDP_MAGIC || nfds != 2 || (msg.msg_flags & MSG_CTRUNC)) {
		reply(c->fd, FALSE, "malformed request");
		keep = FALSE;
		goto out;
	}

	if (req.width < PV_MDP_MIN_SIZE || req.width > PV_MDP_MAX_SIZE || req.width % 2 ||
	    req.height < PV_MDP_MIN_SIZE || req.height > PV_MDP_MAX_SIZE || req.height % 2 ||
	    req.coded_width < req.width || req.coded_width > PV_MDP_MAX_SIZE ||
	    req.dst_pitch < req.coded_width || req.dst_pitch > PV_MDP_MAX_PITCH) {
		reply(c->fd, FALSE, "picture %ux%u (coded width %u, pitch %u) out of range",
			req.width, req.height, req.coded_width, req.dst_pitch);
		goto out;
	}
	src = dmabuf_size(fds[0]);
	dst = dmabuf_size(fds[1]);
	if (src < 0 || dst < 0) {
		reply(c->fd, FALSE, "both buffers must be dma-bufs");
		goto out;
	}
	if ((unsigned long long)src < pv_mdp_src_bytes(req.coded_width, req.height) ||
	    (unsigned long long)dst < pv_mdp_dst_bytes(req.dst_pitch, req.height)) {
		reply(c->fd, FALSE, "buffers of %lld and %lld bytes are too small for %ux%u",
			src, dst, req.width, req.height);
		goto out;
	}

	if (fake) {
		reply(c->fd, TRUE, NULL);
		goto out;
	}

	if (!c->mdp && !(c->mdp = pv_mdp_new(&error))) {
		reply(c->fd, FALSE, "%s", error->message);
		goto out;
	}
	/* The checked size, never more than the buffer holds */
	if (pv_mdp_hyfbc_to_nv12(c->mdp, fds[0], (guint)MIN(src, (long long)G_MAXUINT),
		req.coded_width, req.width, req.height, fds[1], req.dst_pitch, &error))
		reply(c->fd, TRUE, NULL);
	else
		reply(c->fd, FALSE, "%s", error->message);

out:
	for (int i = 0; i < 2; i++)
		if (fds[i] >= 0)
			close(fds[i]);
	return keep;
}

static void drop(Client *c)
{
	close(c->fd);
	if (c->mdp)
		pv_mdp_free(c->mdp);
	c->fd = -1;
	c->mdp = NULL;
}

static int listen_on(const char *path)
{
	struct sockaddr_un addr = { .sun_family = AF_UNIX };
	int fd;

	if (strlen(path) >= sizeof addr.sun_path) {
		errno = ENAMETOOLONG; /* main() names errno as the reason */
		return -1;
	}
	strcpy(addr.sun_path, path);
	fd = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0);
	if (fd < 0)
		return -1;
	unlink(path);
	if (bind(fd, (struct sockaddr *)&addr, sizeof addr) < 0 || listen(fd, 16) < 0) {
		int saved = errno;

		close(fd);
		errno = saved;
		return -1;
	}
	return fd;
}

int main(int argc, char **argv)
{
	const char *listen_path = NULL, *pid, *fds;
	int listen_fd;

	for (int i = 1; i < argc; i++) {
		if (strcmp(argv[i], "--listen") == 0 && i + 1 < argc)
			listen_path = argv[++i];
		else if (strcmp(argv[i], "--fake") == 0)
			fake = TRUE;
		else {
			fprintf(stderr, "Usage: plinius-vdec-mdpd [--listen PATH [--fake]]\n");
			return 2;
		}
	}
	signal(SIGPIPE, SIG_IGN);

	if (listen_path) {
		listen_fd = listen_on(listen_path);
		if (listen_fd < 0) {
			fprintf(stderr, "plinius-vdec-mdpd: cannot listen on %s: %s\n", listen_path, g_strerror(errno));
			return 1;
		}
	} else {
		pid = getenv("LISTEN_PID");
		fds = getenv("LISTEN_FDS");
		if (!pid || !fds || atol(pid) != getpid() || atoi(fds) != 1) {
			fprintf(stderr, "plinius-vdec-mdpd: start through plinius-vdec-mdpd.socket or with --listen\n");
			return 1;
		}
		listen_fd = 3;
		fcntl(listen_fd, F_SETFD, FD_CLOEXEC);
	}

	for (int i = 0; i < MAX_CLIENTS; i++)
		clients[i].fd = -1;

	for (;;) {
		struct pollfd pfds[MAX_CLIENTS + 1];
		Client *polled[MAX_CLIENTS + 1];
		int n = 0, active = 0, r;

		/* No new connection while all slots are taken: it waits in the backlog */
		for (int i = 0; i < MAX_CLIENTS; i++) {
			if (clients[i].fd < 0)
				continue;
			pfds[n] = (struct pollfd){ .fd = clients[i].fd, .events = POLLIN };
			polled[n++] = &clients[i];
			active++;
		}
		if (active < MAX_CLIENTS) {
			pfds[n] = (struct pollfd){ .fd = listen_fd, .events = POLLIN };
			polled[n++] = NULL;
		}

		r = poll(pfds, n, active || listen_path ? -1 : IDLE_EXIT * 1000);
		if (r == 0)
			return 0;
		if (r < 0) {
			if (errno == EINTR)
				continue;
			return 1;
		}

		for (int i = 0; i < n; i++) {
			if (!pfds[i].revents)
				continue;
			if (!polled[i]) {
				int fd = accept4(listen_fd, NULL, NULL, SOCK_CLOEXEC);
				if (fd < 0)
					continue;
				for (int j = 0; j < MAX_CLIENTS; j++)
					if (clients[j].fd < 0) {
						clients[j].fd = fd;
						fd = -1;
						break;
					}
				if (fd >= 0)
					close(fd);
			} else if ((pfds[i].revents & (POLLHUP | POLLERR)) && !(pfds[i].revents & POLLIN)) {
				drop(polled[i]);
			} else if (!serve(polled[i])) {
				drop(polled[i]);
			}
		}
	}
}
