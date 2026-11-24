/*
 * pam_plinius_secstate.so: notes how strong a user's new login password is, for the
 * Device Security page of GNOME Settings. On the phone the login password is the PIN of the
 * screen lock; the page warns when it is shorter than 6 digits (or as weak as that).
 *
 * For the end of the password stack of /etc/pam.d/passwd:
 *   password  optional  pam_plinius_secstate.so
 * In the update phase it rates PAM_AUTHTOK (pwrate.h) and writes
 * /var/lib/plinius-security/lock/<user>:
 *   level=good|weak
 *   kind=pin|password
 *   rated=UNIX_TIME
 * Neither the password, its length nor its hash is stored. The file is written only when the
 * password in /etc/shadow really is the new one (an earlier module may have refused it), so
 * a refused change leaves the old rating. Runs as root (passwd is setuid).
 *
 * The modes are set, not left to the caller's umask: GNOME Settings changes the password through
 * passwd with a umask of 077, the directories became 0700 and Settings, running as the user,
 * could not read the rating ("not rated yet" after every change). The directories are 0755, the
 * file 0640 root:<the user's group>, so only its user (and root) reads it.
 *
 * SPDX-License-Identifier: MIT
 */

#define _GNU_SOURCE
#include <crypt.h>
#include <errno.h>
#include <fcntl.h>
#include <pwd.h>
#include <shadow.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <syslog.h>
#include <time.h>
#include <unistd.h>

#include <security/pam_ext.h>
#include <security/pam_modules.h>

#include "pwrate.h"

#define STATE_DIR "/var/lib/plinius-security"
#define LOCK_DIR  STATE_DIR "/lock"

/* 1 if @password is what /etc/shadow now holds for @user */
static int
is_current_password (const char *user, const char *password)
{
    struct crypt_data data;
    struct spwd *sp;
    const char *hash;

    sp = getspnam (user);
    if (!sp || !sp->sp_pwdp || !*sp->sp_pwdp)
        return 0;
    memset (&data, 0, sizeof (data));
    hash = crypt_r (password, sp->sp_pwdp, &data);
    return hash && strcmp (hash, sp->sp_pwdp) == 0;
}

/* Replaces the user's rating file atomically */
static int
write_rating (pam_handle_t *pamh, const char *user, const char *level, const char *kind)
{
    char path[256], tmp[272];
    struct passwd *pw;
    FILE *f;
    int fd;

    /* A user name with a slash would leave the directory */
    if (strchr (user, '/') || user[0] == '.')
        return -1;
    pw = getpwnam (user);
    if (!pw)
        return -1;
    mkdir (STATE_DIR, 0755);
    mkdir (LOCK_DIR, 0755);
    chmod (STATE_DIR, 0755);
    chmod (LOCK_DIR, 0755);
    snprintf (path, sizeof (path), "%s/%s", LOCK_DIR, user);
    snprintf (tmp, sizeof (tmp), "%s.tmp", path);
    fd = open (tmp, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC | O_NOFOLLOW, 0640);
    if (fd < 0)
        return -1;
    if (fchown (fd, 0, pw->pw_gid) != 0 || fchmod (fd, 0640) != 0) {
        pam_syslog (pamh, LOG_WARNING, "cannot set the mode of %s: %s", tmp, strerror (errno));
        close (fd);
        unlink (tmp);
        return -1;
    }
    f = fdopen (fd, "w");
    if (!f) {
        pam_syslog (pamh, LOG_WARNING, "cannot write %s: %s", tmp, strerror (errno));
        close (fd);
        unlink (tmp);
        return -1;
    }
    fprintf (f, "level=%s\nkind=%s\nrated=%lld\n", level, kind, (long long) time (NULL));
    if (fclose (f) != 0 || rename (tmp, path) != 0) {
        pam_syslog (pamh, LOG_WARNING, "cannot write %s: %s", path, strerror (errno));
        unlink (tmp);
        return -1;
    }
    return 0;
}

PAM_EXTERN int
pam_sm_chauthtok (pam_handle_t *pamh, int flags, int argc, const char **argv)
{
    const char *user = NULL;
    const void *item = NULL;
    const char *password;
    PwRate r;
    int good;

    (void) argc;
    (void) argv;
    if (!(flags & PAM_UPDATE_AUTHTOK))
        return PAM_IGNORE;
    if (pam_get_user (pamh, &user, NULL) != PAM_SUCCESS || !user)
        return PAM_IGNORE;
    if (pam_get_item (pamh, PAM_AUTHTOK, &item) != PAM_SUCCESS || !item)
        return PAM_IGNORE;
    password = item;
    if (!is_current_password (user, password))
        return PAM_IGNORE;

    r = pwrate_estimate (password);
    /* Six digits, or a password that is at least as hard to guess */
    good = (r.digits_only && r.length >= 6) || r.bits >= PWRATE_LOCK_GOOD;
    write_rating (pamh, user, good ? "good" : "weak", r.digits_only ? "pin" : "password");
    /* Never stands in the way of the change itself */
    return PAM_IGNORE;
}
