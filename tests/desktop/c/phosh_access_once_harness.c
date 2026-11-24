/*
 * Harness for access-once.c of phosh-plinius 0109 (included from the patch, see
 * test_desktop_phosh_access_once.py): the answers of the access dialog and what "Allow Once"
 * does to xdg-desktop-portal's permission store. On the session bus of the test (a private
 * dbus-daemon with a fake permission store) it owns org.plinius.Test and serves at /test:
 *
 *   Ask (s app_id, a{sv} options, s choice) -> (u response, a{sv} results)
 *       answered by phosh_access_once_complete() as Phosh answers AccessDialog; CHOICE is
 *       once, always, never or cancel (the dialog swiped away)
 *   Buttons (a{sv} options) -> (u)   phosh_access_choice_buttons()
 *
 * "ready" on stdout once the name is owned; SIGTERM ends it.
 */
#include <gio/gio.h>
#include <glib-unix.h>
#include <stdio.h>
#include <string.h>

#include ACCESS_ONCE_SOURCE

static const char introspection[] =
  "<node><interface name='org.plinius.Test'>"
  "  <method name='Ask'><arg direction='in' type='s'/><arg direction='in' type='a{sv}'/>"
  "    <arg direction='in' type='s'/><arg direction='out' type='u'/><arg direction='out' type='a{sv}'/>"
  "  </method>"
  "  <method name='Buttons'><arg direction='in' type='a{sv}'/><arg direction='out' type='u'/></method>"
  "</interface></node>";

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
on_call (GDBusConnection *bus, const char *sender, const char *path, const char *iface,
         const char *method, GVariant *params, GDBusMethodInvocation *invocation, gpointer data)
{
  if (g_str_equal (method, "Ask")) {
    const char *app_id, *choice;
    g_autoptr (GVariant) options = NULL;

    g_variant_get (params, "(&s@a{sv}&s)", &app_id, &options, &choice);
    phosh_access_once_complete (invocation, app_id, options, choice_of (choice),
                                g_variant_new_array (G_VARIANT_TYPE ("(ss)"), NULL, 0));
  } else {
    g_autoptr (GVariant) options = g_variant_get_child_value (params, 0);

    g_dbus_method_invocation_return_value (invocation,
                                           g_variant_new ("(u)", phosh_access_choice_buttons (options)));
  }
}

static gboolean
on_term (gpointer loop)
{
  g_main_loop_quit (loop);
  return G_SOURCE_REMOVE;
}

int
main (void)
{
  static const GDBusInterfaceVTable vtable = { on_call, NULL, NULL, { 0 } };
  g_autoptr (GError) err = NULL;
  g_autoptr (GDBusConnection) bus = NULL;
  g_autoptr (GDBusNodeInfo) info = NULL;
  g_autoptr (GVariant) reply = NULL;
  g_autoptr (GMainLoop) loop = g_main_loop_new (NULL, FALSE);

  setvbuf (stdout, NULL, _IOLBF, 0);
  bus = g_bus_get_sync (G_BUS_TYPE_SESSION, NULL, &err);
  g_assert_no_error (err);
  info = g_dbus_node_info_new_for_xml (introspection, &err);
  g_assert_no_error (err);
  g_dbus_connection_register_object (bus, "/test", info->interfaces[0], &vtable, NULL, NULL, &err);
  g_assert_no_error (err);
  reply = g_dbus_connection_call_sync (bus, "org.freedesktop.DBus", "/org/freedesktop/DBus",
                                       "org.freedesktop.DBus", "RequestName",
                                       g_variant_new ("(su)", "org.plinius.Test", 0), NULL,
                                       G_DBUS_CALL_FLAGS_NONE, 5000, NULL, &err);
  g_assert_no_error (err);
  puts ("ready");
  g_unix_signal_add (SIGTERM, on_term, loop);
  g_main_loop_run (loop);
  puts ("exit");
  return 0;
}
