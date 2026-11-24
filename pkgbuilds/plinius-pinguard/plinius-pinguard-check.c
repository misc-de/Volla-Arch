/*
 * plinius-pinguard-check: checks the caller's own password (on the phone the PIN) through
 * plinius-pinguard, so that it is counted like one at the lock screen. For programs that ask
 * for the PIN themselves instead of through PAM, e.g. GNOME Settings before the unlock pattern
 * is changed; in place of unix_chkpwd, which counts nothing.
 *
 * Reads the password from stdin, up to a NUL byte or the end (as unix_chkpwd). Prints the
 * daemon's answer (see plinius-pinguard.h) and exits with
 *   0  right password
 *   1  wrong password
 *   2  not checked, a wait runs ("WAIT <seconds>" on stdout)
 *   3  the daemon cannot be reached or answers something else: the caller may fall back
 *
 * Only for the calling user: the daemon answers a user's password only to that user and root.
 *
 * SPDX-License-Identifier: MIT
 */

#define _GNU_SOURCE
#include <pwd.h>

#include "plinius-pinguard-client.h"

int
main (int argc, char **argv)
{
    char password[REQUEST_MAX], answer[64];
    struct passwd *pw = getpwuid (getuid ());
    size_t got = 0;
    int status = 3;

    if (argc != 1) {
        fprintf (stderr, "Usage: plinius-pinguard-check (password on stdin)\n");
        return 3;
    }
    if (!pw)
        return 3;

    while (got < sizeof password - 1) {
        ssize_t r = read (STDIN_FILENO, password + got, sizeof password - 1 - got);

        if (r <= 0)
            break;
        got += r;
        if (memchr (password, '\0', got))
            break;
    }
    /* Up to the first NUL, as pam_unix sends it to unix_chkpwd */
    password[got] = '\0';

    if (pinguard_ask (PINGUARD_SOCKET, pw->pw_name, password, NULL, answer, sizeof answer) == 0) {
        puts (answer);
        if (strncmp (answer, "OK", 2) == 0)
            status = 0;
        else if (strncmp (answer, "FAIL ", 5) == 0)
            status = 1;
        else if (strncmp (answer, "WAIT ", 5) == 0)
            status = 2;
    }
    explicit_bzero (password, sizeof password);
    return status;
}
