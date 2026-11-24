/*
 * Harness for access-request.c of phosh-plinius 0111 (included from the patch, see
 * test_desktop_phosh_access_request.py): the request of an open access dialog, which goes away
 * when xdg-desktop-portal closes it or its caller leaves the bus. On the session bus of the
 * test (a private dbus-daemon) it owns org.plinius.Test and serves at /test:
 *
 *   Ask (s handle) -> (u response, a{sv} results)
 *       a pending AccessDialog as Phosh keeps it while its dialog is open; answered when the
 *       request goes away (phosh_access_request_cancel(), "gone WHY HANDLE" on stdout) or by
 *   Answer (s handle) -> ()   the user's tap: (0, {}), the request freed
 *       A handle ending in "/keep" is not freed when gone, only by Answer (then without a
 *       reply), so that the second way to go can still come.
 *   Pending () -> (u)         the requests still open
 *
 * "ready" on stdout once the name is owned; SIGTERM ends it, freeing what is still open.
 */
#include <gio/gio.h>
#include <glib-unix.h>
#include <stdio.h>
#include <string.h>

#include ACCESS_REQUEST_SOURCE

static const char test_introspection[] =
  "<node><interface name='org.plinius.Test'>"
  "  <method name='Ask'><arg direction='in' type='s'/>"
  "    <arg direction='out' type='u'/><arg direction='out' type='a{sv}'/></method>"
  "  <method name='Answer'><arg direction='in' type='s'/></method>"
  "  <method name='Pending'><arg direction='out' type='u'/></method>"
  "</interface></node>";

typedef struct {
  char                  *handle;
  GDBusMethodInvocation *invocation;
  PhoshAccessRequest    *request;
} Pending;

static GPtrArray *pending;   /* of Pending, in the order asked */

static void
pending_free (gpointer data)
{
  Pending *p = data;

  phosh_access_request_free (p->request);
  g_free (p->handle);
  g_free (p);
}

static void
on_gone (gpointer user_data, const char *why)
{
  Pending *p = user_data;

  printf ("gone %s %s\n", why, p->handle);
  phosh_access_request_cancel (p->invocation);
  p->invocation = NULL;
  if (g_str_has_suffix (p->handle, "/keep"))
    return;
  /* Frees the request from within its own callback, as the portal access manager does */
  g_ptr_array_remove (pending, p);
}

static Pending *
find (const char *handle)
{
  for (guint i = 0; i < pending->len; i++) {
    Pending *p = g_ptr_array_index (pending, i);

    if (g_str_equal (p->handle, handle))
      return p;
  }
  return NULL;
}

static void
on_call (GDBusConnection *bus, const char *sender, const char *path, const char *iface,
         const char *method, GVariant *params, GDBusMethodInvocation *invocation, gpointer data)
{
  if (g_str_equal (method, "Ask")) {
    Pending *p = g_new0 (Pending, 1);

    g_variant_get (params, "(s)", &p->handle);
    p->invocation = invocation;
    g_ptr_array_add (pending, p);
    p->request = phosh_access_request_new (invocation, p->handle, on_gone, p);
  } else if (g_str_equal (method, "Answer")) {
    const char *handle;
    Pending *p;

    g_variant_get (params, "(&s)", &handle);
    p = find (handle);
    if (p && p->invocation)
      g_dbus_method_invocation_return_value (p->invocation, g_variant_new_parsed ("(@u 0, @a{sv} {})"));
    if (p)
      g_ptr_array_remove (pending, p);
    g_dbus_method_invocation_return_value (invocation, NULL);
  } else {
    g_dbus_method_invocation_return_value (invocation,
                                           g_variant_new ("(u)", pending->len));
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
  pending = g_ptr_array_new_with_free_func (pending_free);
  bus = g_bus_get_sync (G_BUS_TYPE_SESSION, NULL, &err);
  g_assert_no_error (err);
  info = g_dbus_node_info_new_for_xml (test_introspection, &err);
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
  /* An open request freed without an answer: the shell going away */
  g_ptr_array_unref (pending);
  phosh_access_request_free (NULL);
  puts ("exit");
  return 0;
}
