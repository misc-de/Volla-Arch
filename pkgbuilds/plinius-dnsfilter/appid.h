// SPDX-License-Identifier: MIT
// appid: which app a DNS question on this phone came from. The client's socket is found through
// sock_diag (its local address and port are the question's source); the kernel tells the
// socket's cgroup (INET_DIAG_CGROUP_ID, the inode of the cgroup's directory on cgroup2), and the
// cgroup names the app, as in plinius-energy:
//   app/<desktop id>   an app the shell, Flatpak, autostart or D-Bus started
//   user/<unit>        another unit of the user
//   system/<unit>      a system service
//   android/uid_<N>    an Android app (or android/<cgroup> for the container itself)
#pragma once

#include <glib.h>
#include <sys/socket.h>

/* Key of the app whose @protocol (IPPROTO_UDP or IPPROTO_TCP) socket has the local address
 * @peer (as recvmsg/accept saw the client); NULL if no socket or cgroup matches */
char *appid_for_peer(int protocol, const struct sockaddr_storage *peer);

/* The key of a cgroup path relative to the cgroup root ("" for the root) */
char *appid_cgroup_key(const char *rel);

/* Closes the netlink socket and forgets the cgroups */
void appid_shutdown(void);
