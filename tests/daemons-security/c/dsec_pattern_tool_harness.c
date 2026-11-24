/*
 * Harness for plinius-pattern.c, the setup tool as it is, except for what would leave the test:
 *   - the default daemon socket (/run/plinius-pinguard.socket) is DSEC_DEFAULT_SOCKET
 *   - the user database's home (used when HOME is unset) is DSEC_PW_HOME, never the real one;
 *     DSEC_NO_USER: the caller has no entry
 *   - DSEC_NO_MEMORY: calloc() finds no memory
 */
#include <pwd.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "plinius-pinguard/plinius-pinguard-client.h"

static struct passwd *
dsec_getpwuid (uid_t uid)
{
    static struct passwd pw;
    struct passwd *real = getpwuid (uid);

    if (!real || getenv ("DSEC_NO_USER"))
        return NULL;
    pw = *real;
    pw.pw_dir = getenv ("DSEC_PW_HOME") ? getenv ("DSEC_PW_HOME") : (char *) "/nonexistent";
    return &pw;
}

#undef PINGUARD_SOCKET
#define PINGUARD_SOCKET (getenv ("DSEC_DEFAULT_SOCKET"))
#define getpwuid dsec_getpwuid
#define calloc(n, size) (getenv ("DSEC_NO_MEMORY") ? NULL : calloc (n, size))

#include "plinius-pattern/plinius-pattern.c"
