/*
 * Harness for plinius-force-quit.c of phosh-plinius 0117 (taken from the patch, see
 * test_desktop_phosh_force_quit.py): finding the units of an app and killing them through the
 * user's systemd, the Android packages of Waydroid's windows. On the session bus of the test (a
 * private dbus-daemon with a fake systemd manager):
 *
 *   unit NAME...      the app id of each unit: "NAME -> ID" or "NAME -> (none)"
 *   android ID...     the Android package of each app id: "ID -> PACKAGE" or "ID -> (none)"
 *   find ID...        the running units of these app ids: "unit NAME" each, then "found N"
 *                     or "error MESSAGE"
 *   cancel ID...      as find, cancelled right away: "cancelled" or "found N"
 *   kill UNIT...      kill them; waits for every answer ("Force quit ..." or "Failed ...")
 *   stop PACKAGE      stop an Android app; waits for the helper: "started" or "error MESSAGE"
 *
 * Every message GLib logs is printed as "log LEVEL MESSAGE"; "exit" at the end.
 */
#include <gio/gio.h>
#include <stdio.h>
#include <string.h>

#include "plinius-force-quit.h"

static GMainLoop *loop;
static guint answers_left;


static void
log_handler (const char *domain, GLogLevelFlags level, const char *message, gpointer data)
{
  const char *name = (level & G_LOG_LEVEL_WARNING) ? "warning" :
                     (level & G_LOG_LEVEL_CRITICAL) ? "critical" :
                     (level & G_LOG_LEVEL_MESSAGE) ? "message" : "debug";

  printf ("log %s %s\n", name, message);
  fflush (stdout);

  if (answers_left == 0)
    return;
  /* Without a session bus nothing was sent: no answers to wait for */
  if (strstr (message, "no session bus")) {
    answers_left = 0;
    g_main_loop_quit (loop);
  } else if (g_str_has_prefix (message, "Force quit ") ||
             g_str_has_prefix (message, "Failed to force quit ")) {
    if (--answers_left == 0)
      g_main_loop_quit (loop);
  }
}


static void
on_found (GObject *source, GAsyncResult *res, gpointer data)
{
  g_autoptr (GError) err = NULL;
  g_auto (GStrv) units = phosh_plinius_force_quit_find_finish (res, &err);

  if (g_error_matches (err, G_IO_ERROR, G_IO_ERROR_CANCELLED)) {
    printf ("cancelled\n");
  } else if (units == NULL) {
    printf ("error %s\n", err->message);
  } else {
    for (guint i = 0; units[i]; i++)
      printf ("unit %s\n", units[i]);
    printf ("found %u\n", g_strv_length (units));
  }
  g_main_loop_quit (loop);
}


int
main (int argc, char **argv)
{
  const char *command;

  if (argc < 2)
    return 2;

  command = argv[1];
  loop = g_main_loop_new (NULL, FALSE);
  g_log_set_default_handler (log_handler, NULL);
  g_log_set_handler ("phosh-force-quit", G_LOG_LEVEL_MASK | G_LOG_FLAG_FATAL, log_handler, NULL);

  if (g_str_equal (command, "unit")) {
    for (int i = 2; i < argc; i++) {
      g_autofree char *id = phosh_plinius_force_quit_unit_app_id (argv[i]);
      printf ("%s -> %s\n", argv[i], id ? id : "(none)");
    }
  } else if (g_str_equal (command, "android")) {
    for (int i = 2; i < argc; i++) {
      g_autofree char *package = phosh_plinius_force_quit_android_package (argv[i]);
      printf ("%s -> %s\n", argv[i], package ? package : "(none)");
    }
    {
      g_autofree char *none = phosh_plinius_force_quit_android_package (NULL);
      printf ("NULL -> %s\n", none ? none : "(none)");
    }
  } else if (g_str_equal (command, "find") || g_str_equal (command, "cancel")) {
    g_autoptr (GCancellable) cancellable = g_cancellable_new ();

    phosh_plinius_force_quit_find ((const char * const *) argv + 2, cancellable, on_found, NULL);
    if (g_str_equal (command, "cancel"))
      g_cancellable_cancel (cancellable);
    g_main_loop_run (loop);
  } else if (g_str_equal (command, "kill")) {
    answers_left = argc - 2;
    phosh_plinius_force_quit_kill ((const char * const *) argv + 2);
    if (answers_left)
      g_main_loop_run (loop);
  } else if (g_str_equal (command, "stop")) {
    g_autoptr (GError) err = NULL;

    if (phosh_plinius_force_quit_android (argv[2], &err)) {
      printf ("started\n");
      fflush (stdout);
      answers_left = 1;
      g_main_loop_run (loop);
    } else {
      printf ("error %s\n", err->message);
    }
  } else {
    return 2;
  }

  printf ("exit\n");
  g_main_loop_unref (loop);
  return 0;
}
