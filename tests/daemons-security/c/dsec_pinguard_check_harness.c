/*
 * Harness for plinius-pinguard-check.c: the tool as it is, with the daemon's socket taken from
 * DSEC_SOCKET (a fake daemon in tmp_path) instead of /run, and the caller's user entry missing
 * when DSEC_NO_USER is set.
 */
#define _GNU_SOURCE
#include <pwd.h>
#include <stdlib.h>

#include "plinius-pinguard/plinius-pinguard-client.h"

#undef PINGUARD_SOCKET
#define PINGUARD_SOCKET (getenv ("DSEC_SOCKET"))
#define getpwuid(uid) (getenv ("DSEC_NO_USER") ? NULL : getpwuid (uid))

#include "plinius-pinguard/plinius-pinguard-check.c"
