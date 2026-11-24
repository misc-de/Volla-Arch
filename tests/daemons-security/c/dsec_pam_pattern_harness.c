/*
 * Harness for pam_plinius_pattern.c: pam_sm_authenticate() with the fake libpam of
 * dsec_fakepam.h (see there for the arguments). pam_modutil_getpwnam() gives the user a home
 * in DSEC_HOME and the uid DSEC_UID (default: the caller's), "ghost" is unknown.
 *
 *   HARNESS --parse     reads patterns from stdin, prints "<points> <grid>" for each
 *
 * DSEC_NO_MEMORY: the module's calloc() finds no memory. DSEC_SETCRED: pam_sm_setcred() instead.
 */
#include "dsec_fakepam.h"

#include <pwd.h>
#include <unistd.h>

struct passwd *
pam_modutil_getpwnam (pam_handle_t *pamh, const char *user)
{
    static struct passwd pw;
    const char *uid = getenv ("DSEC_UID");

    (void) pamh;
    if (strcmp (user, "ghost") == 0)
        return NULL;
    memset (&pw, 0, sizeof pw);
    pw.pw_name = (char *) user;
    pw.pw_dir = getenv ("DSEC_HOME") ? getenv ("DSEC_HOME") : (char *) "/nonexistent";
    pw.pw_uid = uid ? (uid_t) atoi (uid) : getuid ();
    pw.pw_gid = getgid ();
    return &pw;
}

#define calloc(n, size) (getenv ("DSEC_NO_MEMORY") ? NULL : calloc (n, size))
#include "plinius-pattern/pam_plinius_pattern.c"
#undef calloc

int
main (int argc, char **argv)
{
    if (argc == 2 && strcmp (argv[1], "--parse") == 0) {
        char line[512];

        while (fgets (line, sizeof line, stdin)) {
            int grid = -1, n;

            line[strcspn (line, "\n")] = '\0';
            n = pattern_parse (line, &grid);
            printf ("%d %d\n", n, n ? grid : -1);
        }
        return 0;
    }
    return dsec_pam_main (argc, argv, getenv ("DSEC_SETCRED") ? pam_sm_setcred : pam_sm_authenticate);
}
