/*
 * plinius-pinguard.h: what the daemon and its clients agree on.
 *
 * Trying PINs at the phone gets slower with every wrong one: the first FREE_TRIES wrong PINs
 * cost nothing, then the next try has to wait 30 s, 1 min, 5 min, 15 min and from then on 1 h.
 * A 4-digit PIN would take over a year to try out, a 6-digit one over a hundred. The unlock
 * pattern (plinius-pattern) shares the count: a try is a try, whichever of the two it is.
 *
 * The wait is off unless PINGUARD_ENABLED exists (GNOME Settings › Screen Lock, through
 * plinius-pinguard-set). Off, the daemon still checks the PIN, but counts nothing and makes
 * nobody wait. Every wrong try still costs PINGUARD_FAIL_DELAY_US, as with pam_unix.
 *
 * Logins over a network (VNC) have a counter of their own, with the same waits, that always
 * counts: nobody watches there, and a stranger's tries must not lock the phone itself.
 *
 * Tries for a user's PIN made from another local account (su, or a polkit dialog in the session
 * of another user, attack review 2026-11-18 L13/T13) have a counter of their own too,
 * one per pair of accounts, that always counts, also with the wait off: an account must not be
 * able to try out the PIN of another one without limit, and its tries must not lock the other
 * one out of its own lock screen either. Only root can tell (see "from:" below), so only the
 * setuid programs that load pam_plinius_pinguard do.
 *
 * The lock screen checks the PIN as the user, so a counter the user can write would protect
 * nothing. plinius-pinguard runs as root, keeps the counter in /var/lib/plinius-pinguard and
 * checks the PIN itself; while a wait runs it does not check at all, so trying cannot go on
 * unseen. pam_plinius_pinguard asks it over a Unix socket.
 *
 * Waits run on the time since boot (CLOCK_BOOTTIME, which goes on in suspend), not on the
 * clock: setting the clock forward, by hand or through a forged time server, does not end
 * them. A restart starts a running wait over in full.
 *
 * Request: user name, NUL, password, NUL, optionally a kind and NUL (at most REQUEST_MAX bytes,
 * one request per connection, the caller ends it by shutting down its writing side).
 *
 *   no kind     check the password at the phone
 *   "network"   check the password of a login over a network (its own counter)
 *   "attempt"   count a try of the unlock pattern, which the caller checks itself; the password
 *               is empty. Counted as wrong in advance, so a caller that never reports back has
 *               paid for its try.
 *   "success"   the counted pattern was right: the counter is back at 0; the password is empty
 *   "from:UID"  check the password, typed in a program the user UID started (the real uid of
 *               a setuid su or polkit-agent-helper-1). If UID is another account, counted per
 *               pair of accounts, always (see above); if it is the user itself, as without a
 *               kind. Only from root (else DENIED); UID 0 or malformed is an ERROR.
 *
 * The pattern is checked by the caller because its hash is in the user's home, readable by the
 * user anyway: the count guards against tries at the phone, not against code in the session.
 * For the same reason a caller that is the user may report a success.
 *
 * Answer: one line,
 *   "OK"                     right password (or success noted), the counter is back at 0
 *   "FAIL <failures> <wait>" wrong password; the next try has to wait <wait> seconds
 *   "COUNTED <failures> <wait>"  attempt counted; if it was wrong, the next try waits <wait>
 *   "WAIT <seconds>"         not checked or counted, the wait runs for <seconds> more
 *   "DENIED"                 the caller may not ask for this user (only root or the user)
 *   "ERROR"                  unknown user or malformed request
 *
 * SPDX-License-Identifier: MIT
 */

#pragma once

#define PINGUARD_SOCKET "/run/plinius-pinguard.socket"
#define PINGUARD_STATE_DIR "/var/lib/plinius-pinguard"
/* Exists = the wait after wrong PINs is on (written by plinius-pinguard-set as root) */
#define PINGUARD_ENABLED "/etc/plinius-pinguard/enabled"

#define REQUEST_MAX 1024
#define FREE_TRIES  5
/* Kinds of request, the third field (see above) */
#define PINGUARD_NETWORK "network"
#define PINGUARD_ATTEMPT "attempt"
#define PINGUARD_SUCCESS "success"
/* Prefix of the kind for a try from another account, followed by its uid in decimal */
#define PINGUARD_FROM    "from:"
/* A wrong try is answered after at least this long (µs), even with the wait off */
#define PINGUARD_FAIL_DELAY_US 2000000

/* Seconds to wait before the next try after @failures wrong passwords in a row */
static inline long
pinguard_wait_after (unsigned failures)
{
    static const long steps[] = { 30, 60, 5 * 60, 15 * 60 };

    if (failures <= FREE_TRIES)
        return 0;
    if (failures - FREE_TRIES - 1 < sizeof steps / sizeof steps[0])
        return steps[failures - FREE_TRIES - 1];
    return 60 * 60;
}
