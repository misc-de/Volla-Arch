/*
 * plinius-pattern: set, check or remove the user's unlock pattern.
 *
 *   plinius-pattern set      reads a pattern from stdin and saves its grid size and hash
 *   plinius-pattern check    reads a pattern from stdin; exit 0 if it is the saved one, 1 if
 *                            not, 3 if not checked because a wait runs ("WAIT <seconds>" on
 *                            stdout). Each try counts with plinius-pinguard, as on the lock screen.
 *                            PLINIUS_PINGUARD_SOCKET names another daemon (for tests).
 *   plinius-pattern remove   unlock with the PIN only again
 *   plinius-pattern status   prints the grid size and exits 0 if a pattern is set
 *
 * A pattern is written as "grid:points", e.g. "4:0.1.2.6.10.14" (see plinius-pattern.h).
 * GNOME Settings runs this as the user; grid size and hash go to ~/PATTERN_FILE (mode 0600),
 * where pam_plinius_pattern reads them for the lock screen and Phosh the grid size. The hash is
 * yescrypt, libxcrypt's default.
 *
 * SPDX-License-Identifier: MIT
 */

#include <crypt.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <pwd.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <unistd.h>

#include "plinius-pattern.h"
#include "plinius-pinguard/plinius-pinguard-client.h"

static int
usage (void)
{
  fprintf (stderr, "usage: plinius-pattern set|check|remove|status (pattern on stdin, e.g. 4:0.1.2.6.10.14)\n");
  return 2;
}

/* ~/PATTERN_FILE, or ~/.config/plinius for the directory */
static bool
pattern_path (char *path, size_t size, bool dir_only)
{
  const char *home = getenv ("HOME");
  struct passwd *pw;
  int len;

  if (home == NULL || home[0] == '\0') {
    pw = getpwuid (getuid ());
    if (pw == NULL)
      return false;
    home = pw->pw_dir;
  }

  if (dir_only)
    len = snprintf (path, size, "%s/.config/plinius", home);
  else
    len = snprintf (path, size, "%s/%s", home, PATTERN_FILE);
  return len > 0 && len < (int) size;
}

/* One line from stdin, without the newline */
static bool
read_pattern (char *pattern, size_t size)
{
  if (fgets (pattern, (int) size, stdin) == NULL)
    return false;
  pattern[strcspn (pattern, "\r\n")] = '\0';
  return true;
}

/* The saved grid size and hash */
static bool
read_hash (int *grid, char *hash, size_t size)
{
  char path[PATH_MAX], line[16];
  FILE *f;
  bool ok;

  if (!pattern_path (path, sizeof (path), false) || (f = fopen (path, "re")) == NULL)
    return false;
  ok = fgets (line, sizeof (line), f) != NULL && fgets (hash, (int) size, f) != NULL;
  fclose (f);
  if (!ok)
    return false;
  *grid = atoi (line);
  hash[strcspn (hash, "\n")] = '\0';
  return *grid >= PATTERN_MIN_GRID && *grid <= PATTERN_MAX_GRID && hash[0] != '\0';
}

static int
cmd_set (void)
{
  char pattern[PATTERN_MAX_LENGTH + 2], dir[PATH_MAX], path[PATH_MAX], tmp[PATH_MAX];
  char setting[CRYPT_GENSALT_OUTPUT_SIZE];
  struct crypt_data *data;
  const char *hash;
  FILE *f;
  int fd, grid;

  if (!read_pattern (pattern, sizeof (pattern)) || pattern_parse (pattern, &grid) < PATTERN_MIN_POINTS) {
    fprintf (stderr, "plinius-pattern: a pattern needs at least %d different points of a grid "
             "of %d to %d points a side\n", PATTERN_MIN_POINTS, PATTERN_MIN_GRID, PATTERN_MAX_GRID);
    return 1;
  }

  /* NULL prefix: libxcrypt's preferred method (yescrypt) with fresh random salt */
  data = calloc (1, sizeof (*data));
  if (data == NULL || crypt_gensalt_rn (NULL, 0, NULL, 0, setting, sizeof (setting)) == NULL ||
      (hash = crypt_r (pattern, setting, data)) == NULL || hash[0] == '*') {
    fprintf (stderr, "plinius-pattern: hashing failed: %s\n", strerror (errno));
    free (data);
    return 1;
  }

  if (!pattern_path (dir, sizeof (dir), true) || !pattern_path (path, sizeof (path), false) ||
      strlen (path) + sizeof (".new") > sizeof (tmp)) {
    free (data);
    return 1;
  }

  /* ~/.config may not exist yet; ~/.config/plinius only for us */
  snprintf (tmp, sizeof (tmp), "%s", dir);
  *strrchr (tmp, '/') = '\0';
  if ((mkdir (tmp, 0700) < 0 && errno != EEXIST) || (mkdir (dir, 0700) < 0 && errno != EEXIST)) {
    fprintf (stderr, "plinius-pattern: %s: %s\n", dir, strerror (errno));
    free (data);
    return 1;
  }

  /* Write a new file and rename it over the old one, so a crash never leaves half a hash */
  snprintf (tmp, sizeof (tmp), "%s.new", path);
  fd = open (tmp, O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW | O_CLOEXEC, 0600);
  if (fd < 0 || (f = fdopen (fd, "w")) == NULL) {
    fprintf (stderr, "plinius-pattern: %s: %s\n", tmp, strerror (errno));
    free (data);
    return 1;
  }
  fprintf (f, "%d\n%s\n", grid, hash);
  explicit_bzero (data, sizeof (*data));
  free (data);
  if (fflush (f) != 0 || fsync (fd) != 0 || fclose (f) != 0 || rename (tmp, path) < 0) {
    fprintf (stderr, "plinius-pattern: %s: %s\n", path, strerror (errno));
    unlink (tmp);
    return 1;
  }
  return 0;
}

/*
 * Like pam_plinius_pattern: the try is counted before the check and cleared by a right pattern.
 * Without the daemon the pattern is checked uncounted; a daemon that does not answer fails it.
 * The user can always send "success" themselves; the count is for tries at an unlocked phone in
 * passing, as on the lock screen.
 */
static int
cmd_check (void)
{
  char pattern[PATTERN_MAX_LENGTH + 2], hash[CRYPT_OUTPUT_SIZE], answer[64];
  struct crypt_data *data;
  struct passwd *pw = getpwuid (getuid ());
  const char *result;
  int grid, saved_grid;
  bool ok, counted = false;
  long wait;

  const char *sock = getenv ("PLINIUS_PINGUARD_SOCKET");

  if (!sock || !sock[0])
    sock = PINGUARD_SOCKET;
  if (!read_pattern (pattern, sizeof (pattern)) || !read_hash (&saved_grid, hash, sizeof (hash)))
    return 1;

  if (pw && pinguard_ask (sock, pw->pw_name, "", PINGUARD_ATTEMPT, answer, sizeof (answer)) == 0) {
    unsigned failures;

    if (sscanf (answer, "WAIT %ld", &wait) == 1) {
      printf ("WAIT %ld\n", wait);
      return 3;
    }
    counted = sscanf (answer, "COUNTED %u %ld", &failures, &wait) == 2;
  } else if (pw && !pinguard_unreachable (errno)) {
    /* The daemon is there but does not answer: not checked, as on the lock screen */
    fprintf (stderr, "plinius-pattern: no answer from plinius-pinguard: %s\n", strerror (errno));
    return 1;
  }

  if (pattern_parse (pattern, &grid) == 0 || grid != saved_grid)
    return 1;
  data = calloc (1, sizeof (*data));
  if (data == NULL)
    return 1;
  result = crypt_r (pattern, hash, data);
  ok = result != NULL && strcmp (result, hash) == 0;
  explicit_bzero (data, sizeof (*data));
  free (data);

  if (ok && counted &&
      (pinguard_ask (sock, pw->pw_name, "", PINGUARD_SUCCESS, answer, sizeof (answer)) < 0 ||
       strcmp (answer, "OK") != 0))
    fprintf (stderr, "plinius-pattern: plinius-pinguard did not take the right pattern\n");
  return ok ? 0 : 1;
}

static int
cmd_remove (void)
{
  char path[PATH_MAX];

  if (!pattern_path (path, sizeof (path), false))
    return 1;
  if (unlink (path) < 0 && errno != ENOENT) {
    fprintf (stderr, "plinius-pattern: %s: %s\n", path, strerror (errno));
    return 1;
  }
  return 0;
}

static int
cmd_status (void)
{
  char hash[CRYPT_OUTPUT_SIZE];
  int grid;

  if (!read_hash (&grid, hash, sizeof (hash)))
    return 1;
  printf ("%d\n", grid);
  return 0;
}

int
main (int argc, char **argv)
{
  if (argc != 2)
    return usage ();
  if (strcmp (argv[1], "set") == 0)
    return cmd_set ();
  if (strcmp (argv[1], "check") == 0)
    return cmd_check ();
  if (strcmp (argv[1], "remove") == 0)
    return cmd_remove ();
  if (strcmp (argv[1], "status") == 0)
    return cmd_status ();
  return usage ();
}
