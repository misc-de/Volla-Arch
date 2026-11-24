/*
 * plinius-pattern.h: what the lock screen, the PAM module and the setup tool agree on.
 *
 * A pattern connects points of a square grid of 3x3 to 6x6 points, each point at most once. It
 * is written as the grid size, a colon and the points in order, numbered row by row from 0 at
 * the top left, separated by dots: "4:0.1.2.6.10.14" is an L on a 4x4 grid. The grid size is
 * part of what is hashed, so the same drawing on another grid is another pattern.
 *
 * The user's file ~/PATTERN_FILE holds the grid size on its first line (not secret: the lock
 * screen needs it to draw the grid) and the yescrypt hash of the pattern on the second.
 *
 * SPDX-License-Identifier: MIT
 */

#pragma once

#include <stdbool.h>
#include <stdlib.h>
#include <string.h>

/*
 * The lock screen answers the password prompt with this marker and the pattern. No keyboard
 * types \x01, so a PIN can never look like a pattern, and pam_plinius_pattern leaves anything
 * without the marker to pam_unix.
 */
#define PATTERN_PREFIX "\x01plinius-pattern:"

/* The user's grid size and pattern hash, relative to the home directory */
#define PATTERN_FILE ".config/plinius/unlock-pattern"

#define PATTERN_MIN_GRID   3
#define PATTERN_MAX_GRID   6
/* Setting a pattern needs at least this many points; checking accepts any valid pattern */
#define PATTERN_MIN_POINTS 6
/* "6:" and 36 points of at most two digits with dots */
#define PATTERN_MAX_LENGTH 112

/*
 * Checks "grid:p.p.p...": a grid size from PATTERN_MIN_GRID to PATTERN_MAX_GRID and 1 to
 * grid*grid different points below grid*grid, nothing else. Returns the number of points, or 0
 * if the pattern is not valid; *grid gets the grid size.
 */
static inline int
pattern_parse (const char *pattern, int *grid)
{
  bool used[PATTERN_MAX_GRID * PATTERN_MAX_GRID] = { false };
  const char *p = pattern;
  int size, n = 0;

  if (strlen (pattern) > PATTERN_MAX_LENGTH)
    return 0;
  if (p[0] < '0' + PATTERN_MIN_GRID || p[0] > '0' + PATTERN_MAX_GRID || p[1] != ':')
    return 0;
  size = p[0] - '0';
  p += 2;

  for (;;) {
    int point = 0, digits = 0;

    /* One way to write each point: no leading zero */
    if (p[0] == '0' && p[1] >= '0' && p[1] <= '9')
      return 0;
    while (*p >= '0' && *p <= '9' && digits < 2) {
      point = point * 10 + (*p - '0');
      p++;
      digits++;
    }
    if (digits == 0 || point >= size * size || used[point])
      return 0;
    used[point] = true;
    n++;
    if (*p == '\0')
      break;
    if (*p != '.')
      return 0;
    p++;
  }

  *grid = size;
  return n;
}
