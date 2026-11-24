/*
 * appaccess.bpf.c: cgroup BPF programs of plinius-appaccessd. They decide, per app, whether a
 * process may open a camera or a security key and create IPv4/IPv6 sockets, and count each
 * attempt for the app's page in GNOME Settings.
 *
 * The daemon attaches them to user@<uid>.service/app.slice of each session, so only the apps of
 * a session are concerned; the kernel, system services, the Android container and login shells
 * never run them. Inside app.slice, each cgroup (an app's scope or a user service) has a verdict
 * per resource in the cgroups map, written by the daemon from the app's policy; a user service
 * always gets ALLOW.
 *
 * A cgroup without a verdict (an app started a moment ago, before the daemon saw its scope)
 * reports itself through the ring buffer; until the daemon has written its verdict, cameras and
 * keys stay closed for it and the network stays open, as it is for every app by default.
 *
 * Which character devices are cameras and keys is decided by the daemon too (devices map):
 * only V4L2 and media nodes on USB (the phone's own cameras belong to the Android container and
 * reach the apps through PipeWire) and hidraw nodes whose report descriptor has the FIDO usage
 * page.
 *
 * cgroup programs, not BPF LSM: on arm64, 6.1 cannot attach BPF to kernel functions (no ftrace
 * direct calls before 6.4). A refused device open fails with EPERM, a refused socket too.
 *
 * SPDX-License-Identifier: GPL-2.0-only
 */

#include <linux/bpf.h>
#include <bpf/bpf_helpers.h>

#include "appaccess.h"

#define AF_INET  2
#define AF_INET6 10

/* At most one event per cgroup and resource in this time; the counts are exact anyway */
#define EVENT_REPEAT_NS (2ULL * 1000 * 1000 * 1000)

/* Cameras and keys: dev_t (AA_DEV) -> AA_CAMERA or AA_KEY */
struct {
    __uint (type, BPF_MAP_TYPE_HASH);
    __uint (max_entries, 1024);
    __type (key, __u32);
    __type (value, __u32);
} devices SEC (".maps");

/* Verdicts: cgroup id -> struct aa_cgroup */
struct {
    __uint (type, BPF_MAP_TYPE_HASH);
    __uint (max_entries, 4096);
    __type (key, __u64);
    __type (value, struct aa_cgroup);
} cgroups SEC (".maps");

/* Attempts per cgroup and resource; the daemon adds them to the app and deletes them */
struct {
    __uint (type, BPF_MAP_TYPE_HASH);
    __uint (max_entries, 8192);
    __type (key, struct aa_count_key);
    __type (value, struct aa_count);
} counts SEC (".maps");

/* Time of the last event per cgroup and resource */
struct {
    __uint (type, BPF_MAP_TYPE_LRU_HASH);
    __uint (max_entries, 4096);
    __type (key, struct aa_count_key);
    __type (value, __u64);
} recent SEC (".maps");

struct {
    __uint (type, BPF_MAP_TYPE_RINGBUF);
    __uint (max_entries, 64 * 1024);
} events SEC (".maps");

static __always_inline void
count (__u64 cgroup_id, __u32 res, int allowed)
{
    struct aa_count_key key = { .cgroup_id = cgroup_id, .res = res };
    struct aa_count zero = {}, *c;
    __u64 now = bpf_ktime_get_ns ();

    c = bpf_map_lookup_elem (&counts, &key);
    if (!c) {
        bpf_map_update_elem (&counts, &key, &zero, BPF_NOEXIST);
        c = bpf_map_lookup_elem (&counts, &key);
        if (!c)
            return;
    }
    if (allowed) {
        __sync_fetch_and_add (&c->allowed, 1);
        c->last_allowed = now;
    } else {
        __sync_fetch_and_add (&c->denied, 1);
        c->last_denied = now;
    }
}

static __always_inline void
send_event (__u64 cgroup_id, __u32 res, __u32 kind, __u32 verdict)
{
    struct aa_count_key key = { .cgroup_id = cgroup_id, .res = res };
    __u64 now = bpf_ktime_get_ns ();
    __u64 *last;
    struct aa_event ev = {};

    last = bpf_map_lookup_elem (&recent, &key);
    if (last && now - *last < EVENT_REPEAT_NS)
        return;
    bpf_map_update_elem (&recent, &key, &now, BPF_ANY);

    ev.cgroup_id = cgroup_id;
    ev.uid = (__u32) bpf_get_current_uid_gid ();
    ev.res = res;
    ev.kind = kind;
    ev.verdict = verdict;
    bpf_get_current_comm (ev.comm, sizeof (ev.comm));
    bpf_ringbuf_output (&events, &ev, sizeof (ev), 0);
}

/* 1 to allow, 0 to refuse @res for the current process */
static __always_inline int
decide (__u32 res)
{
    __u64 cgroup_id = bpf_get_current_cgroup_id ();
    struct aa_cgroup *c;
    __u8 verdict = AA_VERDICT_NONE;

    if (res >= AA_RES_MAX)
        return 1;
    c = bpf_map_lookup_elem (&cgroups, &cgroup_id);
    if (c)
        verdict = c->verdict[res];

    switch (verdict) {
    case AA_VERDICT_ALLOW:
        /* Allowed network use is what plinius-netlog records; only cameras and keys here */
        if (res != AA_NETWORK)
            count (cgroup_id, res, 1);
        return 1;
    case AA_VERDICT_DENY:
    case AA_VERDICT_ASK:
        count (cgroup_id, res, 0);
        send_event (cgroup_id, res, AA_EVENT_DENIED, verdict);
        return 0;
    default:
        send_event (cgroup_id, res, AA_EVENT_UNKNOWN, verdict);
        return res == AA_NETWORK;
    }
}

SEC ("cgroup/dev")
int
appaccess_dev (struct bpf_cgroup_dev_ctx *ctx)
{
    __u32 type = ctx->access_type & 0xffff, access = ctx->access_type >> 16;
    __u32 dev, *res;

    /* Opening for reading or writing; mknod needs CAP_MKNOD anyway */
    if (type != BPF_DEVCG_DEV_CHAR || !(access & (BPF_DEVCG_ACC_READ | BPF_DEVCG_ACC_WRITE)))
        return 1;
    dev = AA_DEV (ctx->major, ctx->minor);
    res = bpf_map_lookup_elem (&devices, &dev);
    if (!res)
        return 1;
    return decide (*res);
}

SEC ("cgroup/sock_create")
int
appaccess_sock_create (struct bpf_sock *ctx)
{
    /* Run by inet_create and inet6_create only, for sockets of processes (not the kernel's) */
    if (ctx->family != AF_INET && ctx->family != AF_INET6)
        return 1;
    return decide (AA_NETWORK);
}

char LICENSE[] SEC ("license") = "GPL";
