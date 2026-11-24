/* SPDX-License-Identifier: MIT */
/* Colour conversion with the MediaTek MDP through the vendor's libdpframework (libhybris). */
#pragma once

#include <glib.h>

typedef struct PvMdp PvMdp;

/* Loads libdpframework on first use; NULL with a message in *error if that is not possible. */
PvMdp *pv_mdp_new(GError **error);
void pv_mdp_free(PvMdp *mdp);

/*
 * Converts one 8-bit HyFBC frame of the decoder (src_fd, src_size bytes) to NV12 in dst_fd
 * (Y with dst_pitch bytes per line, followed by interleaved UV with the same pitch).
 * coded_width is the line width of the decoded picture, width/height the visible size. Whole
 * decoded lines are converted, so dst_pitch is at least coded_width; the columns from width on
 * are the decoder's padding.
 */
gboolean pv_mdp_hyfbc_to_nv12(PvMdp *mdp, int src_fd, guint src_size, guint coded_width,
	guint width, guint height, int dst_fd, guint dst_pitch, GError **error);

/* Size a source buffer needs so that the MDP's plane checks pass for this picture. */
guint pv_mdp_hyfbc_src_size(guint coded_width, guint height);
