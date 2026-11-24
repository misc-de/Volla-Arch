/* SPDX-License-Identifier: MIT */
/*
 * The MDP conversion of mdp.h for the plugin: each frame goes to plinius-vdec-mdpd, which alone
 * may open the MDP (see mdp-proto.h). One connection per decoder instance; the daemon starts on
 * the first connection (socket activation).
 *
 * PLINIUS_VDEC_MDP_SOCKET names another socket (tests).
 */
#define _GNU_SOURCE
#include "mdp.h"
#include "mdp-proto.h"

#include <errno.h>
#include <gio/gio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/un.h>
#include <unistd.h>

/* A conversion takes about 3 ms; the first one also loads libdpframework in the daemon */
#define REPLY_TIMEOUT 10 /* s */

struct PvMdp {
	int fd;
};

PvMdp *pv_mdp_new(GError **error)
{
	struct sockaddr_un addr = { .sun_family = AF_UNIX };
	struct timeval timeout = { REPLY_TIMEOUT, 0 };
	const char *path = g_getenv("PLINIUS_VDEC_MDP_SOCKET");
	int fd;

	if (!path || !*path)
		path = PV_MDP_SOCKET;
	if (strlen(path) >= sizeof addr.sun_path) {
		g_set_error(error, G_IO_ERROR, G_IO_ERROR_INVALID_ARGUMENT, "socket path too long");
		return NULL;
	}
	strcpy(addr.sun_path, path);
	fd = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0);
	if (fd < 0 || connect(fd, (struct sockaddr *)&addr, sizeof addr) < 0) {
		int saved = errno;
		if (fd >= 0)
			close(fd);
		g_set_error(error, G_IO_ERROR, g_io_error_from_errno(saved),
			"MDP service %s: %s", path, g_strerror(saved));
		return NULL;
	}
	setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof timeout);
	setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof timeout);

	PvMdp *mdp = g_new0(PvMdp, 1);
	mdp->fd = fd;
	return mdp;
}

void pv_mdp_free(PvMdp *mdp)
{
	if (!mdp)
		return;
	close(mdp->fd);
	g_free(mdp);
}

guint pv_mdp_hyfbc_src_size(guint coded_width, guint height)
{
	return (guint)pv_mdp_src_bytes(coded_width, height);
}

gboolean pv_mdp_hyfbc_to_nv12(PvMdp *mdp, int src_fd, guint src_size, guint coded_width,
	guint width, guint height, int dst_fd, guint dst_pitch, GError **error)
{
	PvMdpRequest req = { .magic = PV_MDP_MAGIC, .coded_width = coded_width, .width = width,
		.height = height, .dst_pitch = dst_pitch };
	PvMdpReply reply = { 0 };
	char control[CMSG_SPACE(2 * sizeof(int))] = { 0 };
	struct iovec iov = { .iov_base = &req, .iov_len = sizeof req };
	struct msghdr msg = { .msg_iov = &iov, .msg_iovlen = 1,
		.msg_control = control, .msg_controllen = sizeof control };
	struct cmsghdr *cm = CMSG_FIRSTHDR(&msg);
	int fds[2] = { src_fd, dst_fd };
	ssize_t n;

	/* The daemon takes the size from the buffer itself; checked here too for a clear message */
	if (src_size < pv_mdp_src_bytes(coded_width, height)) {
		g_set_error(error, G_IO_ERROR, G_IO_ERROR_INVALID_ARGUMENT,
			"HyFBC buffer of %u bytes is too small for %ux%u", src_size, width, height);
		return FALSE;
	}
	cm->cmsg_level = SOL_SOCKET;
	cm->cmsg_type = SCM_RIGHTS;
	cm->cmsg_len = CMSG_LEN(sizeof fds);
	memcpy(CMSG_DATA(cm), fds, sizeof fds);

	if (sendmsg(mdp->fd, &msg, MSG_NOSIGNAL) != sizeof req) {
		g_set_error(error, G_IO_ERROR, g_io_error_from_errno(errno),
			"MDP service: %s", g_strerror(errno));
		return FALSE;
	}
	n = recv(mdp->fd, &reply, sizeof reply, 0);
	if (n != sizeof reply) {
		g_set_error(error, G_IO_ERROR, n < 0 ? g_io_error_from_errno(errno) : G_IO_ERROR_CLOSED,
			"MDP service: %s", n < 0 ? g_strerror(errno) : "no answer");
		return FALSE;
	}
	if (!reply.ok) {
		reply.message[sizeof reply.message - 1] = '\0';
		g_set_error(error, G_IO_ERROR, G_IO_ERROR_FAILED, "MDP service: %s", reply.message);
		return FALSE;
	}
	return TRUE;
}
