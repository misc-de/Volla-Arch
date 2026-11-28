/*
 * Harness for the hold of swipe-away-bin.c of phosh-plinius 0117 (taken from the patch, see
 * test_desktop_phosh_force_quit.py): a card swiped at least halfway away and held there for a
 * second asks whether to force quit its app. A PhoshSwipeAwayBin here is a plain struct with the
 * fields that part uses; the callers of the swipe tracker do what the bin's callbacks do.
 *
 *   stdin "ask 0|1"      phosh_swipe_away_bin_set_hold_to_ask ()
 *   stdin "begin"        begin_swipe_cb ()
 *   stdin "progress P"   update_swipe_cb (): the finger is at P of the distance
 *   stdin "end TO"       end_swipe_cb (): "animate TO" with where the child goes
 *   stdin "wait MS"      the main loop runs for MS milliseconds
 *   stdin "quit"         ends it
 *
 * stdout: "ready", "held" for each ::plinius-held, "animate TO", "done" after each command, "exit".
 */
#include <glib.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct {
  double   progress;
  gboolean hold_to_ask;
  guint    hold_id;
  double   hold_progress;
  gboolean held;
} PhoshSwipeAwayBin;

#define PHOSH_SWIPE_AWAY_BIN(p) ((PhoshSwipeAwayBin *) (p))

enum { REMOVED, PLINIUS_HELD, N_SIGNALS };
static guint signals[N_SIGNALS] = { 0, 1 };

static void
emit (guint id)
{
  if (id == PLINIUS_HELD)
    printf ("held\n");
  fflush (stdout);
}
#define g_signal_emit(self, id, detail) emit (id)

#include SWIPE_HOLD_DEFINES
#include SWIPE_HOLD_SOURCE

static gboolean
on_wait_done (gpointer loop)
{
  g_main_loop_quit (loop);
  return G_SOURCE_REMOVE;
}

int
main (void)
{
  PhoshSwipeAwayBin bin = { 0 };
  char line[256];

  printf ("ready\n");
  fflush (stdout);

  while (fgets (line, sizeof (line), stdin)) {
    g_strchomp (line);
    if (g_str_equal (line, "quit")) {
      break;
    } else if (g_str_has_prefix (line, "ask ")) {
      bin.hold_to_ask = atoi (line + 4);
      if (!bin.hold_to_ask)
        reset_hold (&bin);
    } else if (g_str_equal (line, "begin")) {
      reset_hold (&bin);
    } else if (g_str_has_prefix (line, "progress ")) {
      bin.progress = g_ascii_strtod (line + 9, NULL);
      update_hold (&bin);
    } else if (g_str_has_prefix (line, "end ")) {
      double to = g_ascii_strtod (line + 4, NULL);
      printf ("animate %g\n", end_hold (&bin) ? 0.0 : to);
    } else if (g_str_has_prefix (line, "wait ")) {
      GMainLoop *loop = g_main_loop_new (NULL, FALSE);
      g_timeout_add (atoi (line + 5), on_wait_done, loop);
      g_main_loop_run (loop);
      g_main_loop_unref (loop);
    }
    printf ("done\n");
    fflush (stdout);
  }

  reset_hold (&bin);
  printf ("exit\n");
  return 0;
}
