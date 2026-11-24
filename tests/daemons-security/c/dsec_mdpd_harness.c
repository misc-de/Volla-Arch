/*
 * Harness for plinius-vdec-mdpd (mdpd.c, its main() as is): the MDP itself is a stub that logs
 * each conversion to DSEC_MDP_LOG, and readlink() reports a file whose name starts with
 * "dsec-dmabuf" as a dma-buf ("/dmabuf:"), so the size checks can be driven with plain files
 * in tmp_path. Everything else, including the socket and fd handling, is the daemon's own.
 *   DSEC_MDP_NEW_FAIL   pv_mdp_new() fails       DSEC_MDP_FAIL   the conversion fails
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <glib.h>
#include <gio/gio.h>

#include "plinius-vdec/mdp.h"

struct PvMdp {
    int unused;
};

PvMdp *
pv_mdp_new (GError **error)
{
    if (getenv ("DSEC_MDP_NEW_FAIL")) {
        g_set_error (error, G_IO_ERROR, G_IO_ERROR_FAILED, "no libdpframework here");
        return NULL;
    }
    return g_new0 (PvMdp, 1);
}

void
pv_mdp_free (PvMdp *mdp)
{
    g_free (mdp);
}

gboolean
pv_mdp_hyfbc_to_nv12 (PvMdp *mdp, int src_fd, guint src_size, guint coded_width, guint width,
                      guint height, int dst_fd, guint dst_pitch, GError **error)
{
    const char *log = getenv ("DSEC_MDP_LOG");

    if (log) {
        FILE *f = fopen (log, "a");

        if (f) {
            fprintf (f, "convert %u %u %u %u %u\n", src_size, coded_width, width, height, dst_pitch);
            fclose (f);
        }
    }
    if (getenv ("DSEC_MDP_FAIL")) {
        g_set_error (error, G_IO_ERROR, G_IO_ERROR_FAILED, "MDP said no");
        return FALSE;
    }
    return TRUE;
}

static ssize_t
fake_readlink (const char *path, char *buf, size_t size)
{
    char real[4096];
    ssize_t n = readlink (path, real, sizeof real - 1);
    const char *base;

    if (n < 0)
        return n;
    real[n] = '\0';
    base = strrchr (real, '/');
    if (base && strncmp (base + 1, "dsec-dmabuf", 11) == 0) {
        snprintf (real, sizeof real, "/dmabuf:%s", base + 1);
        n = (ssize_t) strlen (real);
    }
    if ((size_t) n > size)
        n = (ssize_t) size;
    memcpy (buf, real, (size_t) n);
    return n;
}

#define readlink fake_readlink
#include "plinius-vdec/mdpd.c"
