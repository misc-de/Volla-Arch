/*
 * Harness for pam_plinius_pinguard.c: pam_sm_authenticate() with the fake libpam of
 * dsec_fakepam.h (see there for the arguments). The daemon is whatever listens on the module's
 * socket= option, in the tests a fake server in tmp_path.
 *
 * DSEC_EUID / DSEC_RUID: the effective and real uid the module sees (a setuid program: 0 and
 * the user who started it); unset: the harness's own. DSEC_SETCRED: pam_sm_setcred() instead.
 */
#include "dsec_fakepam.h"

#include <unistd.h>

static uid_t
dsec_uid (const char *var, uid_t real)
{
    const char *v = getenv (var);

    return v ? (uid_t) strtoul (v, NULL, 10) : real;
}

#define geteuid() dsec_uid ("DSEC_EUID", geteuid ())
#define getuid() dsec_uid ("DSEC_RUID", getuid ())

#include "plinius-pinguard/pam_plinius_pinguard.c"

int
main (int argc, char **argv)
{
    return dsec_pam_main (argc, argv, getenv ("DSEC_SETCRED") ? pam_sm_setcred : pam_sm_authenticate);
}
