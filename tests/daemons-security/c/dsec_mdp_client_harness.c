/*
 * Harness for the plugin's side of plinius-vdec (mdp-client.c): one conversion request through
 * PLINIUS_VDEC_MDP_SOCKET.
 *   HARNESS SRC SRC_SIZE CODED_WIDTH WIDTH HEIGHT DST PITCH   -> "ok" or "error MESSAGE"
 */
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

#include "plinius-vdec/mdp.h"

int
main (int argc, char **argv)
{
    g_autoptr (GError) error = NULL;
    PvMdp *mdp;
    int src, dst;

    if (argc != 8)
        return 2;
    src = open (argv[1], O_RDWR | O_CLOEXEC);
    dst = open (argv[6], O_RDWR | O_CLOEXEC);
    mdp = pv_mdp_new (&error);
    if (!mdp) {
        printf ("error %s\n", error->message);
        return 0;
    }
    if (pv_mdp_hyfbc_to_nv12 (mdp, src, (guint) strtoul (argv[2], NULL, 10),
                              (guint) atoi (argv[3]), (guint) atoi (argv[4]), (guint) atoi (argv[5]),
                              dst, (guint) atoi (argv[7]), &error))
        puts ("ok");
    else
        printf ("error %s\n", error->message);
    pv_mdp_free (mdp);
    return 0;
}
