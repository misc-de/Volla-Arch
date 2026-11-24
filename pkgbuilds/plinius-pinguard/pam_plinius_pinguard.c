/*
 * pam_plinius_pinguard.so: checks the password (on the phone the PIN) through plinius-pinguard,
 * which makes every wrong one after the first few cost waiting time (see plinius-pinguard.h).
 *
 * For the auth stack, in place of pam_unix for the password check, e.g.
 *   auth  [success=done authinfo_unavail=ignore default=die]  pam_plinius_pinguard.so
 *   auth  required                                             pam_unix.so try_first_pass
 * Without the daemon (socket missing or refused) the module says PAM_AUTHINFO_UNAVAIL, so such
 * a stack falls back to pam_unix instead of locking everyone out. A daemon that is there but does
 * not answer in time is a failed check (PAM_AUTH_ERR), never a fallback: else anyone who keeps
 * it busy would get the PIN checked without the count.
 *
 * Messages: a wrong PIN that starts a wait, and a try during a wait, are told with the time
 * left (PAM_ERROR_MSG). An answer with the unlock pattern's marker is not a PIN; it is left to
 * pam_plinius_pattern (PAM_IGNORE).
 *
 * A wrong PIN costs PINGUARD_FAIL_DELAY_US (pam_fail_delay) also with the wait off: the stack
 * ends here with default=die, so pam_unix's own delay never comes.
 *
 * Option network marks the login as coming over a network, which has a counter of its own that
 * always counts. Such a stack must not fall back to pam_unix.
 *
 * In a setuid program (su, polkit's agent helper: effective uid root, real uid the user who
 * started it) the try is sent as "from:<real uid>". If that real uid is not the user whose PIN is
 * asked for, the daemon counts the try per pair of accounts and always, so a second account can
 * neither try out the PIN of the first one without limit nor lock it out of its own lock screen
 * (attack review 2026-11-18, L13/T13); from the user itself it is a try at the phone as before.
 * The daemon believes "from:" only from root. Nothing extra is sent by a module running as the
 * user (the lock screen) or with a real uid of 0 (sudo, which runs PAM as full root and asks for
 * the caller's own PIN; root's own su; greetd; a polkit helper started by a socket service
 * instead of setuid): those stay tries at the phone, as before.
 *
 * Option socket=PATH asks another daemon (for tests).
 *
 * SPDX-License-Identifier: MIT
 */

#define _GNU_SOURCE
#include <syslog.h>

#include <security/pam_ext.h>
#include <security/pam_modules.h>

#include "plinius-pinguard-client.h"

/* See plinius-pattern.h */
#define PATTERN_PREFIX "\x01plinius-pattern:"

PAM_EXTERN int
pam_sm_authenticate (pam_handle_t *pamh, int flags, int argc, const char **argv)
{
    const char *path = PINGUARD_SOCKET;
    const char *user = NULL, *password = NULL;
    char answer[64], when[32], msg[128], from[32];
    const char *kind = NULL;
    unsigned failures;
    long wait;
    bool network = false;
    int r;

    for (int i = 0; i < argc; i++) {
        if (strncmp (argv[i], "socket=", 7) == 0)
            path = argv[i] + 7;
        else if (strcmp (argv[i], "network") == 0)
            network = true;
        else
            pam_syslog (pamh, LOG_ERR, "unknown option: %s", argv[i]);
    }

    r = pam_get_user (pamh, &user, NULL);
    if (r != PAM_SUCCESS)
        return r;
    r = pam_get_authtok (pamh, PAM_AUTHTOK, &password, NULL);
    if (r != PAM_SUCCESS)
        return r;
    if (!password)
        return PAM_AUTH_ERR;
    if (strncmp (password, PATTERN_PREFIX, strlen (PATTERN_PREFIX)) == 0)
        return PAM_IGNORE;

    /* Applied by PAM only if the whole check fails */
    pam_fail_delay (pamh, PINGUARD_FAIL_DELAY_US);

    if (network) {
        kind = PINGUARD_NETWORK;
    } else if (geteuid () == 0 && getuid () != 0) {
        /* A setuid program: who started it (see above); the daemon compares with the user */
        snprintf (from, sizeof from, "%s%u", PINGUARD_FROM, (unsigned) getuid ());
        kind = from;
    }

    if (pinguard_ask (path, user, password, kind, answer, sizeof answer) < 0) {
        if (pinguard_unreachable (errno)) {
            pam_syslog (pamh, LOG_ERR, "%s not reachable: %m", path);
            return PAM_AUTHINFO_UNAVAIL;
        }
        pam_syslog (pamh, LOG_ERR, "no answer from %s: %m", path);
        if (!(flags & PAM_SILENT))
            pam_error (pamh, "%s", "The PIN could not be checked. Try again.");
        return PAM_AUTH_ERR;
    }

    if (strncmp (answer, "OK", 2) == 0)
        return PAM_SUCCESS;

    if (sscanf (answer, "FAIL %u %ld", &failures, &wait) == 2) {
        if (wait > 0 && !(flags & PAM_SILENT)) {
            pinguard_format_wait (wait, when, sizeof when);
            snprintf (msg, sizeof msg, "Wrong PIN. Next try in %s.", when);
            pam_error (pamh, "%s", msg);
        }
        return PAM_AUTH_ERR;
    }

    if (sscanf (answer, "WAIT %ld", &wait) == 1) {
        if (!(flags & PAM_SILENT)) {
            pinguard_format_wait (wait, when, sizeof when);
            snprintf (msg, sizeof msg, "Too many wrong PINs. Next try in %s.", when);
            pam_error (pamh, "%s", msg);
        }
        return PAM_MAXTRIES;
    }

    if (strncmp (answer, "DENIED", 6) == 0)
        return PAM_PERM_DENIED;

    return PAM_AUTH_ERR;
}

PAM_EXTERN int
pam_sm_setcred (pam_handle_t *pamh, int flags, int argc, const char **argv)
{
    return PAM_SUCCESS;
}
