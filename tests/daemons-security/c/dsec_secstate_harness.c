/*
 * Harness for pam_plinius_secstate.c (the rating of a new screen lock PIN) and pwrate.h.
 *
 * The module writes below /var/lib/plinius-security as root. Here every file call of the module
 * goes through redirect(): that prefix is moved into DSEC_ROOT, any other absolute path aborts
 * the harness, so nothing outside the test directory can be touched. fchown() is only recorded
 * (the harness is no root). /etc/shadow and the user database are fakes:
 *   DSEC_CURRENT   the password /etc/shadow "now holds" for any user (hashed here); unset: none
 *   users alice (gid 1001) and bob (gid 1002) are in the user database, others not
 *   DSEC_FDOPEN_FAILS  fdopen() finds no memory
 *
 *   HARNESS USER AUTHTOK_HEX FLAGS     pam_sm_chauthtok (see dsec_fakepam.h)
 *   HARNESS --rate                     pwrate_estimate for each line of stdin:
 *                                      "BITS LENGTH DIGITS_ONLY"
 */
#define _GNU_SOURCE
#include <crypt.h>
#include <errno.h>
#include <fcntl.h>
#include <pwd.h>
#include <shadow.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "dsec_fakepam.h"
#include "dsec_nolog.h"

#define REAL_PREFIX "/var/lib/plinius-security"

static const char *
redirect (const char *path, char *buf, size_t size)
{
    const char *root = getenv ("DSEC_ROOT");

    if (!root || !*root)
        abort ();
    if (strncmp (path, REAL_PREFIX, strlen (REAL_PREFIX)) == 0) {
        snprintf (buf, size, "%s%s", root, path + strlen (REAL_PREFIX));
        return buf;
    }
    fprintf (stderr, "harness: refused path outside the test: %s\n", path);
    abort ();
}

static int
fake_mkdir (const char *path, mode_t mode)
{
    char buf[4096];

    return mkdir (redirect (path, buf, sizeof buf), mode);
}

static int
fake_chmod (const char *path, mode_t mode)
{
    char buf[4096];

    return chmod (redirect (path, buf, sizeof buf), mode);
}

static int
fake_open (const char *path, int flags, ...)
{
    char buf[4096];
    va_list args;
    mode_t mode;

    va_start (args, flags);
    mode = (mode_t) va_arg (args, int);
    va_end (args);
    return open (redirect (path, buf, sizeof buf), flags, mode);
}

static int
fake_rename (const char *from, const char *to)
{
    char a[4096], b[4096];

    return rename (redirect (from, a, sizeof a), redirect (to, b, sizeof b));
}

static int
fake_unlink (const char *path)
{
    char buf[4096];

    return unlink (redirect (path, buf, sizeof buf));
}

static int
fake_fchown (int fd, uid_t uid, gid_t gid)
{
    (void) fd;
    printf ("chown %u:%u\n", (unsigned) uid, (unsigned) gid);
    return getenv ("DSEC_CHOWN_FAILS") ? -1 : 0;
}

static FILE *
fake_fdopen (int fd, const char *mode)
{
    if (getenv ("DSEC_FDOPEN_FAILS")) {
        errno = ENOMEM;
        return NULL;
    }
    return fdopen (fd, mode);
}

static struct passwd *
fake_getpwnam (const char *name)
{
    static struct passwd pw;

    if (strcmp (name, "alice") && strcmp (name, "bob"))
        return NULL;
    memset (&pw, 0, sizeof pw);
    pw.pw_name = (char *) name;
    pw.pw_uid = strcmp (name, "alice") ? 1002 : 1001;
    pw.pw_gid = pw.pw_uid;
    return &pw;
}

static struct spwd *
fake_getspnam (const char *name)
{
    static struct spwd sp;
    static struct crypt_data data;
    static char hash[256];
    const char *current = getenv ("DSEC_CURRENT");
    const char *h;

    if (!current)
        return NULL;
    h = crypt_r (current, "$6$dsecsecstate$", &data);
    snprintf (hash, sizeof hash, "%s", h ? h : "*");
    memset (&sp, 0, sizeof sp);
    sp.sp_namp = (char *) name;
    sp.sp_pwdp = hash;
    return &sp;
}

#define mkdir fake_mkdir
#define chmod fake_chmod
#define open(...) fake_open (__VA_ARGS__)
#define rename fake_rename
#define unlink fake_unlink
#define fchown fake_fchown
#define fdopen fake_fdopen
#define getpwnam fake_getpwnam
#define getspnam fake_getspnam
#include "plinius-security/pam_plinius_secstate.c"

int
main (int argc, char **argv)
{
    if (argc == 2 && strcmp (argv[1], "--rate") == 0) {
        char line[4096];

        while (fgets (line, sizeof line, stdin)) {
            PwRate r;

            line[strcspn (line, "\n")] = '\0';
            r = pwrate_estimate (line);
            printf ("%.2f %u %d\n", r.bits, r.length, r.digits_only);
        }
        return 0;
    }
    return dsec_pam_main (argc, argv, pam_sm_chauthtok);
}
