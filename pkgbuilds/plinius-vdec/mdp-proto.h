/* SPDX-License-Identifier: MIT */
/*
 * What the GStreamer plugin and plinius-vdec-mdpd agree on.
 *
 * The MDP (MediaTek's colour converter, driven through the vendor's libdpframework and its
 * command queue) used to run in the app that plays a video, so every program of the user could
 * open /dev/mtk_mdp: a driver of the kind that has given apps kernel rights before (MediaTek's
 * CMDQ, CVE-2020-0069). Now only plinius-vdec-mdpd, as a user of its own, opens it. The plugin
 * sends it the two dma-bufs of one frame over a Unix socket and gets the result back; the daemon
 * checks that both are dma-bufs large enough for what is asked, within fixed limits.
 *
 * Socket: SOCK_SEQPACKET, one request per message with exactly two fds (SCM_RIGHTS): the HyFBC
 * source and the NV12 destination. Answer: one PvMdpReply.
 */
#pragma once

#include <stdint.h>

#define PV_MDP_SOCKET "/run/plinius-vdec-mdp.socket"
#define PV_MDP_MAGIC  0x504d4431u /* "PMD1" */

/* Limits of a request: the decoder's largest picture (4096 wide, 4096 high) and some margin for
 * the destination's line pitch */
#define PV_MDP_MIN_SIZE  16
#define PV_MDP_MAX_SIZE  4096
#define PV_MDP_MAX_PITCH 8192

typedef struct {
	uint32_t magic;
	uint32_t coded_width; /* line width of the decoded picture */
	uint32_t width;       /* visible size */
	uint32_t height;
	uint32_t dst_pitch;   /* bytes per line of Y and of UV in the destination, >= coded_width */
} PvMdpRequest;

typedef struct {
	int32_t ok;           /* 1: converted */
	char message[124];    /* else why not, NUL-terminated */
} PvMdpReply;

/* Size a HyFBC source buffer needs so that the MDP's plane checks pass for this picture: the
 * source Y pitch is coded_width * 12 / 8, the MDP checks each plane against pitch * height. */
static inline uint64_t pv_mdp_src_bytes(uint32_t coded_width, uint32_t height)
{
	uint64_t ypitch = (uint64_t)coded_width * 12 / 8;
	return ypitch * height + ypitch / 2 * (height / 2);
}

/* Size of an NV12 destination with @pitch bytes per line */
static inline uint64_t pv_mdp_dst_bytes(uint32_t pitch, uint32_t height)
{
	return (uint64_t)pitch * height + (uint64_t)pitch * (height / 2);
}
