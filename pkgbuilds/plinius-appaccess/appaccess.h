/*
 * appaccess.h: what the BPF programs (appaccess.bpf.c) and plinius-appaccessd share.
 *
 * SPDX-License-Identifier: MIT
 */

#ifndef APPACCESS_H
#define APPACCESS_H

#include <linux/types.h>

/* What an app may be allowed or refused */
enum {
    AA_CAMERA  = 0,  /* a camera on USB: V4L2 and media controller nodes */
    AA_KEY     = 1,  /* a security key: hidraw with the FIDO usage page */
    AA_NETWORK = 2,  /* IPv4 and IPv6 sockets */
    /* Audio sources in PipeWire: decided by WirePlumber (plinius-appaccess's hook) from the
     * verdicts the daemon publishes, never by the programs */
    AA_MICROPHONE = 3,
    AA_RES_MAX = 4,
};

/* Per cgroup, what the programs do; the daemon turns the app's policy into this */
enum {
    AA_VERDICT_NONE  = 0,  /* never written: the daemon has not seen the cgroup yet */
    AA_VERDICT_ALLOW = 1,
    AA_VERDICT_DENY  = 2,
    AA_VERDICT_ASK   = 3,  /* refused, and the user is asked (plinius-appaccess-agent) */
};

/* Value of the cgroups map: one verdict per resource */
struct aa_cgroup {
    __u8 verdict[AA_RES_MAX];
    __u8 pad[8 - AA_RES_MAX];
};

/* Key of the counts and ratelimit maps */
struct aa_count_key {
    __u64 cgroup_id;
    __u32 res;
    __u32 pad;
};

/* Value of the counts map, in CLOCK_MONOTONIC (bpf_ktime_get_ns) */
struct aa_count {
    __u64 allowed;
    __u64 denied;
    __u64 last_allowed;
    __u64 last_denied;
};

/* Why an event is sent */
enum {
    AA_EVENT_DENIED  = 1,  /* refused by a verdict of DENY or ASK */
    AA_EVENT_UNKNOWN = 2,  /* a cgroup without a verdict touched a resource */
};

struct aa_event {
    __u64 cgroup_id;
    __u32 uid;
    __u32 res;
    __u32 kind;
    __u32 verdict;
    char  comm[16];
};

/*
 * cgroup v2 level of user@<uid>.service/app.slice, where systemd puts the apps of a session and
 * the daemon attaches the programs: /user.slice (1) /user-<uid>.slice (2) /user@<uid>.service (3)
 * /app.slice (4)
 */
#define AA_APP_SLICE_LEVEL 4

/* Key of the devices map: the kernel's dev_t layout, 12 bits major, 20 bits minor */
#define AA_DEV(major, minor) (((__u32) (major) << 20) | (__u32) (minor))

#endif
