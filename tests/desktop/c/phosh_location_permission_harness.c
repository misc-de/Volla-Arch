/*
 * Harness for location-permission.c of phosh-plinius 0112 (included from the patch, see
 * test_desktop_phosh_location_permission.py): what the permission store remembers of the
 * location answers of native apps. One call per run, on the session bus of the environment
 * (a private dbus-daemon with a fake permission store, or none):
 *
 *   harness name LEVEL                 phosh_location_level_to_string()
 *   harness level NAME                 phosh_location_level_from_string()
 *   harness decide APP LEVEL           phosh_location_permission_decide() without entries
 *   harness lookup APP LEVEL [cancel]  "ask|allow|deny store|nostore", or "cancelled"
 *   harness store APP LEVEL CHOICE     "written" or "not written"; CHOICE once, always,
 *                                      never or cancel. Waits for the store's reply.
 */
#include <gio/gio.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include LOCATION_PERMISSION_SOURCE

static const char *const decisions[] = { "ask", "allow", "deny" };

static PhoshAccessChoice
choice_of (const char *name)
{
  if (g_str_equal (name, "once"))
    return PHOSH_ACCESS_CHOICE_ONCE;
  if (g_str_equal (name, "always"))
    return PHOSH_ACCESS_CHOICE_ALWAYS;
  if (g_str_equal (name, "never"))
    return PHOSH_ACCESS_CHOICE_NEVER;
  return PHOSH_ACCESS_CHOICE_CANCEL;
}

static void
on_lookup_done (GObject *source, GAsyncResult *res, gpointer loop)
{
  g_autoptr (GError) err = NULL;
  gboolean can_store = TRUE;
  PhoshLocationPermission decision;

  decision = phosh_location_permission_lookup_finish (res, &can_store, &err);
  if (err)
    printf ("cancelled %s\n", can_store ? "store" : "nostore");
  else
    printf ("%s %s\n", decisions[decision], can_store ? "store" : "nostore");
  g_main_loop_quit (loop);
}

/* A call to the store after SetPermission: its reply comes after the one to SetPermission */
static void
round_trip (void)
{
  g_autoptr (GDBusConnection) bus = g_bus_get_sync (G_BUS_TYPE_SESSION, NULL, NULL);
  g_autoptr (GVariant) reply = NULL;

  if (bus)
    reply = g_dbus_connection_call_sync (bus, PERMISSION_STORE_NAME, PERMISSION_STORE_PATH,
                                         PERMISSION_STORE_INTERFACE, "Lookup",
                                         g_variant_new ("(ss)", "harness", "harness"), NULL,
                                         G_DBUS_CALL_FLAGS_NONE, 5000, NULL, NULL);
  while (g_main_context_iteration (NULL, FALSE))
    ;
}

int
main (int argc, char **argv)
{
  setvbuf (stdout, NULL, _IOLBF, 0);
  g_assert (argc >= 3);

  if (g_str_equal (argv[1], "name")) {
    puts (phosh_location_level_to_string (atoi (argv[2])));
  } else if (g_str_equal (argv[1], "level")) {
    printf ("%u\n", phosh_location_level_from_string (argv[2]));
  } else if (g_str_equal (argv[1], "decide")) {
    puts (decisions[phosh_location_permission_decide (NULL, argv[2], atoi (argv[3]))]);
  } else if (g_str_equal (argv[1], "lookup")) {
    g_autoptr (GMainLoop) loop = g_main_loop_new (NULL, FALSE);
    g_autoptr (GCancellable) cancel = g_cancellable_new ();

    phosh_location_permission_lookup (argv[2], atoi (argv[3]), cancel, on_lookup_done, loop);
    if (argc > 4)
      g_cancellable_cancel (cancel);
    g_main_loop_run (loop);
  } else {
    gboolean written = phosh_location_permission_store (argv[2], atoi (argv[3]), choice_of (argv[4]));

    puts (written ? "written" : "not written");
    round_trip ();
  }
  return 0;
}
