/*
 * netlog.bpf.c: BPF programs of plinius-netlogd. Attached to the root of the cgroup v2
 * hierarchy, they see every outgoing connection of every process: connect() for TCP and
 * connected UDP, sendmsg()/sendto() with an address for unconnected UDP. Each one is passed
 * to the daemon through a ring buffer, with the cgroup of the process (on systemd: the app's
 * scope or the service), its uid and, where the kernel allows it, pid and command name.
 *
 * The programs never block anything: they always return 1 (allow).
 *
 * Unconnected UDP sends one sendmsg per packet (QUIC, VoIP, mDNS); a small LRU map keeps
 * the time of the last report per (cgroup, address, port), so such a flow is reported at
 * most every REPEAT_NS.
 *
 * connect() on a UDP socket sends nothing: glibc's getaddrinfo connects one UDP socket per
 * candidate address only to sort them (RFC 6724). So a UDP connect is only noted on the socket
 * (socket storage) and reported by the egress program when the socket really sends its first
 * packet.
 *
 * SPDX-License-Identifier: GPL-2.0-only
 */

#include <linux/bpf.h>
#include <linux/in.h>
#include <bpf/bpf_endian.h>
#include <bpf/bpf_helpers.h>

#include "netlog.h"

/* Report the same unconnected UDP flow at most every 30 s */
#define REPEAT_NS (30ULL * 1000 * 1000 * 1000)

struct {
    __uint (type, BPF_MAP_TYPE_RINGBUF);
    __uint (max_entries, 256 * 1024);
} events SEC (".maps");

struct flow_key {
    __u64 cgroup_id;
    __u32 addr[4];
    __u16 port;
    __u8  family;
    __u8  proto;
};

/* A connected UDP socket that has not sent yet: the event to report on its first packet */
struct pending {
    struct netlog_event ev;
    __u32 armed;
};

struct {
    __uint (type, BPF_MAP_TYPE_SK_STORAGE);
    __uint (map_flags, BPF_F_NO_PREALLOC);
    __type (key, int);
    __type (value, struct pending);
} pending SEC (".maps");

struct {
    __uint (type, BPF_MAP_TYPE_LRU_HASH);
    __uint (max_entries, 4096);
    __type (key, struct flow_key);
    __type (value, __u64);
} recent SEC (".maps");

/* Fills @ev for the current process; @addr is in network order */
static __always_inline void
fill (struct netlog_event *ev, struct bpf_sock_addr *ctx, __u8 family, const __u32 *addr, __u8 kind)
{
    __u64 uid_gid = bpf_get_current_uid_gid ();

    ev->cgroup_id = bpf_get_current_cgroup_id ();
    ev->uid = (__u32) uid_gid;
    ev->pid = bpf_get_current_pid_tgid () >> 32;
    bpf_get_current_comm (ev->comm, sizeof (ev->comm));
    ev->family = family;
    ev->proto = ctx->protocol;
    ev->kind = kind;
    ev->port = bpf_ntohs ((__u16) ctx->user_port);
    ev->addr[0] = addr[0];
    ev->addr[1] = addr[1];
    ev->addr[2] = addr[2];
    ev->addr[3] = addr[3];
}

/* Submits one event right away */
static __always_inline void
report (struct bpf_sock_addr *ctx, __u8 family, const __u32 *addr, __u8 kind)
{
    struct netlog_event *ev;

    ev = bpf_ringbuf_reserve (&events, sizeof (*ev), 0);
    if (!ev)
        return;
    fill (ev, ctx, family, addr, kind);
    bpf_ringbuf_submit (ev, 0);
}

/* TCP is reported at connect(); a UDP socket only when it sends (see the top) */
static __always_inline void
on_connect (struct bpf_sock_addr *ctx, __u8 family, const __u32 *addr)
{
    struct pending *p;

    if (ctx->protocol != IPPROTO_UDP) {
        report (ctx, family, addr, NETLOG_CONNECT);
        return;
    }
    p = bpf_sk_storage_get (&pending, ctx->sk, 0, BPF_SK_STORAGE_GET_F_CREATE);
    if (!p)
        return;
    fill (&p->ev, ctx, family, addr, NETLOG_CONNECT);
    p->armed = 1;
}

/* 1 if this unconnected UDP flow was reported less than REPEAT_NS ago, else notes it */
static __always_inline int
seen_recently (struct bpf_sock_addr *ctx, __u8 family, const __u32 *addr)
{
    struct flow_key key = {};
    __u64 now = bpf_ktime_get_ns ();
    __u64 *last;

    key.cgroup_id = bpf_get_current_cgroup_id ();
    key.addr[0] = addr[0];
    key.addr[1] = addr[1];
    key.addr[2] = addr[2];
    key.addr[3] = addr[3];
    key.port = (__u16) ctx->user_port;
    key.family = family;
    key.proto = ctx->protocol;
    last = bpf_map_lookup_elem (&recent, &key);
    if (last && now - *last < REPEAT_NS)
        return 1;
    bpf_map_update_elem (&recent, &key, &now, BPF_ANY);
    return 0;
}

SEC ("cgroup/connect4")
int
netlog_connect4 (struct bpf_sock_addr *ctx)
{
    __u32 addr[4] = { ctx->user_ip4, 0, 0, 0 };

    on_connect (ctx, 4, addr);
    return 1;
}

SEC ("cgroup/connect6")
int
netlog_connect6 (struct bpf_sock_addr *ctx)
{
    __u32 addr[4] = { ctx->user_ip6[0], ctx->user_ip6[1], ctx->user_ip6[2], ctx->user_ip6[3] };

    on_connect (ctx, 6, addr);
    return 1;
}

SEC ("cgroup/sendmsg4")
int
netlog_sendmsg4 (struct bpf_sock_addr *ctx)
{
    __u32 addr[4] = { ctx->user_ip4, 0, 0, 0 };

    if (!seen_recently (ctx, 4, addr))
        report (ctx, 4, addr, NETLOG_SEND);
    return 1;
}

SEC ("cgroup/sendmsg6")
int
netlog_sendmsg6 (struct bpf_sock_addr *ctx)
{
    __u32 addr[4] = { ctx->user_ip6[0], ctx->user_ip6[1], ctx->user_ip6[2], ctx->user_ip6[3] };

    if (!seen_recently (ctx, 6, addr))
        report (ctx, 6, addr, NETLOG_SEND);
    return 1;
}

/* First packet of a connected UDP socket: report its connect() now */
SEC ("cgroup_skb/egress")
int
netlog_egress (struct __sk_buff *skb)
{
    struct bpf_sock *sk = skb->sk;
    struct pending *p;

    if (!sk)
        return 1;
    sk = bpf_sk_fullsock (sk);
    if (!sk || sk->protocol != IPPROTO_UDP)
        return 1;
    p = bpf_sk_storage_get (&pending, sk, 0, 0);
    if (p && p->armed) {
        p->armed = 0;
        bpf_ringbuf_output (&events, &p->ev, sizeof (p->ev), 0);
    }
    return 1;
}

char LICENSE[] SEC ("license") = "GPL";
