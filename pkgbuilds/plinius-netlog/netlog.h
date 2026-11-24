/*
 * netlog.h: the event that the BPF programs (netlog.bpf.c) hand to plinius-netlogd.
 *
 * SPDX-License-Identifier: MIT
 */

#ifndef NETLOG_H
#define NETLOG_H

#include <linux/types.h>

/* How the connection was made */
enum {
    NETLOG_CONNECT = 1,  /* connect(): TCP, connected UDP */
    NETLOG_SEND    = 2,  /* sendmsg()/sendto() with an address: unconnected UDP */
};

struct netlog_event {
    __u64 cgroup_id;     /* cgroup v2 id of the process (inode number of its directory) */
    __u32 uid;
    __u32 pid;           /* process (thread group) id */
    __u32 addr[4];       /* destination, network order; IPv4 in addr[0] */
    __u16 port;          /* destination port, host order */
    __u8  family;        /* 4 or 6 */
    __u8  proto;         /* IPPROTO_TCP, IPPROTO_UDP, ... */
    __u8  kind;          /* NETLOG_CONNECT or NETLOG_SEND */
    __u8  pad[3];
    char  comm[16];      /* command name of the thread */
};

#endif
