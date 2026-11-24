/*
 * pam_plinius_pattern: unlock the screen with a pattern instead of the PIN.
 *
 * Phosh's lock screen answers the password prompt with PATTERN_PREFIX and the drawn pattern
 * ("grid:points", see plinius-pattern.h). A marked answer is checked here against the user's
 * hash (written by plinius-pattern into ~/PATTERN_FILE) and either succeeds or fails here, so the
 * marker never reaches pam_unix as a password.
 * Either PIN or pattern: while the user has a pattern, an answer without the prefix (the PIN)
 * fails here too. Without a pattern, or with a file this module does not accept, it is not ours:
 * PAM_IGNORE, and the stack goes on to pam_unix with the same answer (try_first_pass), so a
 * broken file never locks the user out. In /etc/pam.d/phosh:
 *
 *   -auth [success=done ignore=ignore default=die] pam_plinius_pattern.so
 *
 * Only the lock screen uses it; sudo, polkit and the login keep the PIN (the account password). Phosh authenticates as
 * the user, so the hash lives in the user's home: whoever can replace it already runs code in the
 * session (see docs/SICHERHEIT.md for the same trade-off with the PIN).
 *
 * Wrong patterns cost waiting time like wrong PINs, on the same count: before each check the try
 * is counted by plinius-pinguard ("attempt", see plinius-pinguard.h), a right pattern clears the
 * count again ("success"). While a wait runs, nothing is checked and the lock screen is told how
 * long it is. Without the daemon the pattern is checked as before, with the fixed delay only.
 * Option socket=PATH asks another daemon (for tests).
 *
 * SPDX-License-Identifier: MIT
 */

#define PAM_SM_AUTH

#include <crypt.h>
#include <fcntl.h>
#include <limits.h>
#include <pwd.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <syslog.h>
#include <unistd.h>

#include <security/pam_ext.h>
#include <security/pam_modules.h>
#include <security/pam_modutil.h>

#include "plinius-pattern.h"
#include "plinius-pinguard/plinius-pinguard-client.h"

/* A failed pattern is answered after this long (in µs), as pam_unix does for a wrong PIN */
#define FAIL_DELAY_US PINGUARD_FAIL_DELAY_US

/*
 * Read the user's grid size and hash. The file must be a regular file of the user and not
 * writable by anyone else, so that no other account can plant a pattern for them.
 */
static bool
read_hash (const struct passwd *pw, int *grid, char *hash, size_t size)
{
  char path[PATH_MAX], buf[16 + CRYPT_OUTPUT_SIZE], *line;
  struct stat st;
  ssize_t len;
  int fd;

  if (snprintf (path, sizeof (path), "%s/%s", pw->pw_dir, PATTERN_FILE) >= (int) sizeof (path))
    return false;

  fd = open (path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
  if (fd < 0)
    return false;

  if (fstat (fd, &st) < 0 || !S_ISREG (st.st_mode) || st.st_uid != pw->pw_uid ||
      (st.st_mode & (S_IWGRP | S_IWOTH))) {
    close (fd);
    return false;
  }

  len = read (fd, buf, sizeof (buf) - 1);
  close (fd);
  if (len <= 0)
    return false;
  buf[len] = '\0';

  /* First line the grid size, second line the hash */
  *grid = atoi (buf);
  line = strchr (buf, '\n');
  if (line == NULL || *grid < PATTERN_MIN_GRID || *grid > PATTERN_MAX_GRID)
    return false;
  line++;
  line[strcspn (line, "\n")] = '\0';
  if (line[0] == '\0' || strlen (line) >= size)
    return false;
  strcpy (hash, line);
  return true;
}

/* Compare without stopping at the first difference */
static bool
equal_in_constant_time (const char *a, const char *b)
{
  size_t len = strlen (a);
  unsigned char diff = len != strlen (b);

  for (size_t i = 0; i < len && b[i]; i++)
    diff |= (unsigned char) a[i] ^ (unsigned char) b[i];
  return diff == 0;
}

/* Counts the try with plinius-pinguard. FALSE: a wait runs, the try must not be checked. *counted
 * tells whether the daemon counted it (then a right pattern has to be reported), *wait how long
 * the next try waits if this one is wrong. */
static bool
count_try (pam_handle_t *pamh, int flags, const char *sock, const char *user, bool *counted,
           long *wait)
{
  char answer[64], when[32], msg[128];
  unsigned failures;

  *counted = false;
  *wait = 0;
  if (pinguard_ask (sock, user, "", PINGUARD_ATTEMPT, answer, sizeof (answer)) < 0) {
    /* No daemon at all: checked with the fixed delay only, as before plinius-pinguard. One that
     * is there but does not answer is a failed try: anyone can keep it busy. */
    if (pinguard_unreachable (errno)) {
      pam_syslog (pamh, LOG_WARNING, "plinius-pinguard not reached (%m), pattern tries are not counted");
      return true;
    }
    pam_syslog (pamh, LOG_ERR, "no answer from plinius-pinguard (%m), pattern not checked");
    if (!(flags & PAM_SILENT))
      pam_error (pamh, "%s", "The pattern could not be checked. Try again.");
    return false;
  }
  if (sscanf (answer, "COUNTED %u %ld", &failures, wait) == 2) {
    *counted = true;
    return true;
  }
  if (sscanf (answer, "WAIT %ld", wait) == 1) {
    if (!(flags & PAM_SILENT)) {
      pinguard_format_wait (*wait, when, sizeof (when));
      snprintf (msg, sizeof (msg), "Too many wrong tries. Next try in %s.", when);
      pam_error (pamh, "%s", msg);
    }
    return false;
  }
  /* DENIED or ERROR: the daemon is there but would not count; check as without it */
  pam_syslog (pamh, LOG_WARNING, "plinius-pinguard answered \"%s\" to a pattern try", answer);
  return true;
}

PAM_EXTERN int
pam_sm_authenticate (pam_handle_t *pamh, int flags, int argc, const char **argv)
{
  struct crypt_data *data;
  const char *user = NULL, *answer = NULL, *pattern, *result;
  const struct passwd *pw;
  char hash[CRYPT_OUTPUT_SIZE], answer_line[64], when[32], msg[128];
  int grid, saved_grid;
  const char *sock = PINGUARD_SOCKET;
  bool has_pattern, ok, counted;
  long wait;

  for (int i = 0; i < argc; i++) {
    if (strncmp (argv[i], "socket=", 7) == 0)
      sock = argv[i] + 7;
    else
      pam_syslog (pamh, LOG_ERR, "unknown option: %s", argv[i]);
  }

  if (pam_get_user (pamh, &user, NULL) != PAM_SUCCESS || user == NULL)
    return PAM_IGNORE;

  /* Asks the lock screen once; pam_unix takes the same answer afterwards */
  if (pam_get_authtok (pamh, PAM_AUTHTOK, &answer, NULL) != PAM_SUCCESS || answer == NULL)
    return PAM_IGNORE;

  pw = pam_modutil_getpwnam (pamh, user);
  has_pattern = pw != NULL && read_hash (pw, &saved_grid, hash, sizeof (hash));

  if (strncmp (answer, PATTERN_PREFIX, strlen (PATTERN_PREFIX)) != 0) {
    if (!has_pattern)
      return PAM_IGNORE;
    /* A pattern is set: the PIN does not unlock the screen */
    pam_fail_delay (pamh, FAIL_DELAY_US);
    pam_syslog (pamh, LOG_NOTICE, "%s has an unlock pattern, PIN refused", user);
    return PAM_AUTH_ERR;
  }

  /* From here on the answer is a pattern and must not reach pam_unix */
  pam_fail_delay (pamh, FAIL_DELAY_US);

  /* Nothing to guess without a pattern */
  if (!has_pattern)
    return PAM_AUTH_ERR;
  if (!count_try (pamh, flags, sock, user, &counted, &wait))
    return PAM_MAXTRIES;

  pattern = answer + strlen (PATTERN_PREFIX);
  if (pattern_parse (pattern, &grid) == 0 || grid != saved_grid)
    goto wrong;

  /* struct crypt_data is large (32 KiB); keep it off the caller's thread stack */
  data = calloc (1, sizeof (*data));
  if (data == NULL)
    return PAM_BUF_ERR;

  result = crypt_r (pattern, hash, data);
  ok = result != NULL && result[0] != '*' && equal_in_constant_time (result, hash);
  explicit_bzero (data, sizeof (*data));
  free (data);

  if (!ok)
    goto wrong;

  if (counted && (pinguard_ask (sock, user, "", PINGUARD_SUCCESS, answer_line,
                                sizeof (answer_line)) < 0 || strcmp (answer_line, "OK") != 0))
    pam_syslog (pamh, LOG_ERR, "plinius-pinguard did not take the right pattern, the try stays counted");
  return PAM_SUCCESS;

wrong:
  pam_syslog (pamh, LOG_NOTICE, "wrong unlock pattern for %s", user);
  if (wait > 0 && !(flags & PAM_SILENT)) {
    pinguard_format_wait (wait, when, sizeof (when));
    snprintf (msg, sizeof (msg), "Wrong pattern. Next try in %s.", when);
    pam_error (pamh, "%s", msg);
  }
  return PAM_AUTH_ERR;
}

PAM_EXTERN int
pam_sm_setcred (pam_handle_t *pamh, int flags, int argc, const char **argv)
{
  (void) pamh;
  (void) flags;
  (void) argc;
  (void) argv;

  return PAM_IGNORE;
}
