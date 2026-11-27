/*
 * health-hal-bridge: the phone's health HAL (AIDL android.hardware.health.IHealth) for Android
 * apps in the Waydroid container
 *
 * Android's BatteryService reads the battery through IHealth/default. The Waydroid image only
 * has a stand-in that reports a battery of its own making; this daemon serves IHealth on the
 * container's /dev/anbox-binder instead, registered whenever the container's service manager
 * shows up (the node opens to users other than root only once the container has started;
 * inotify tells).
 *
 * Every query (update, the counters, getChargeStatus, getHealthInfo, storage and disk stats) goes
 * unchanged to the vendor HAL on /dev/binder and its reply unchanged back: the vendor knows the
 * battery, and parcelables carry their size, so Android 13 (version 1) reads the vendor's newer
 * HealthInfo (version 2) as far as it knows it. Android's callbacks stay here and get
 * healthInfoChanged() one-way from the daemon, with the vendor's getHealthInfo(): at once when
 * registered (as the HAL itself does), after update(), and whenever UPower reports a change of
 * the battery or the charger. The daemon holds no callback at the vendor HAL: the HAL calls a
 * new callback back before registerCallback() returns, and libgbinder hands that call to the main
 * thread, which is still waiting for the reply (measured: Android's BatteryService hung).
 *
 * Transaction codes follow the frozen AIDL definitions (android.hardware.health version 1 and
 * its successors keep the order): the n-th method has code n.
 *
 * SPDX-License-Identifier: MIT
 */

#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <gio/gio.h>
#include <glib-unix.h>
#include <gbinder.h>

#define BINDER_DEVICE    "/dev/binder"
#define ANDROID_DEVICE   "/dev/anbox-binder"
#define HEALTH_IFACE     "android.hardware.health.IHealth"
#define HEALTH_CB_IFACE  "android.hardware.health.IHealthInfoCallback"
#define HEALTH_INSTANCE  HEALTH_IFACE "/default"
/* UPower's devices: a change of one means a new HealthInfo */
#define UPOWER_NAME      "org.freedesktop.UPower"
#define UPOWER_DEVICE    "org.freedesktop.UPower.Device"
/* UPower reports a change in several signals at once: one HealthInfo for them */
#define PUSH_DELAY_MS    500
/* What Android gets: IHealth version 1 and the hash of that frozen version */
#define ANDROID_VERSION  1
#define ANDROID_HASH     "94e77215594f8ad98ab33a769263d48fdabed92e"
/* Transactions every AIDL interface answers */
#define TX_GET_INTERFACE_VERSION 0x00ffffff
#define TX_GET_INTERFACE_HASH    0x00fffffe
/* AIDL exception codes */
#define EX_NONE                  0
#define EX_ILLEGAL_ARGUMENT      (-3)
#define EX_UNSUPPORTED_OPERATION (-7)
#define EX_ILLEGAL_STATE         (-5)

/* IHealth */
enum {
  HEALTH_REGISTER_CALLBACK = 1,
  HEALTH_UNREGISTER_CALLBACK = 2,
  HEALTH_UPDATE = 3,
  HEALTH_GET_HEALTH_INFO = 12,
};
/* IHealthInfoCallback (Android's) */
#define CB_HEALTH_INFO_CHANGED 1

/* One callback of Android's BatteryService (or anyone else in the container) */
typedef struct {
  GBinderRemoteObject *remote;
  GBinderClient       *client;
  gulong               death_id;
} Callback;

static struct {
  /* The vendor HAL */
  GBinderServiceManager *sm;
  GBinderRemoteObject   *remote;
  GBinderClient         *client;
  gulong                 death_id;
  guint                  push_id;   /* pending HealthInfo for every callback after a change */
  /* The container */
  GBinderServiceManager *android_sm;
  GBinderLocalObject    *service;
  gulong                 presence_id;
  char                  *node;      /* name of the device node in its directory */
  GList                 *callbacks; /* Callback, Android's */
} self;

static void
callback_free (Callback *c)
{
  if (c->death_id)
    gbinder_remote_object_remove_handler (c->remote, c->death_id);
  gbinder_client_unref (c->client);
  gbinder_remote_object_unref (c->remote);
  g_free (c);
}

/* ---- The vendor HAL ---- */

static void
hal_disconnect (void)
{
  if (self.remote && self.death_id)
    gbinder_remote_object_remove_handler (self.remote, self.death_id);
  self.death_id = 0;
  g_clear_pointer (&self.client, gbinder_client_unref);
  g_clear_pointer (&self.remote, gbinder_remote_object_unref);
}

static void
on_hal_died (GBinderRemoteObject *obj, void *user_data)
{
  g_warning ("Health HAL died");
  hal_disconnect ();
}

/* Copies the rest of a parcel (after what reader has read) into w */
static void
append_rest (GBinderWriter *w, const GBinderReader *reader)
{
  gsize size = 0;
  const guint8 *data = gbinder_reader_get_data (reader, &size);
  gsize rest = gbinder_reader_bytes_remaining (reader);

  if (data && rest <= size)
    gbinder_writer_append_bytes (w, data + size - rest, rest);
}

/* healthInfoChanged() one-way to one of Android's callbacks; reader holds the HealthInfo */
static void
send_info (Callback *c, const GBinderReader *reader)
{
  GBinderLocalRequest *req = gbinder_client_new_request (c->client);
  GBinderWriter w;

  gbinder_local_request_init_writer (req, &w);
  append_rest (&w, reader);
  gbinder_client_transact (c->client, CB_HEALTH_INFO_CHANGED, GBINDER_TX_FLAG_ONEWAY, req,
                           NULL, NULL, NULL);
  gbinder_local_request_unref (req);
}

/* Calls the vendor HAL with the rest of request (none if NULL); returns its reply or NULL */
static GBinderRemoteReply *
hal_call (guint code, const GBinderReader *request)
{
  GBinderLocalRequest *req = gbinder_client_new_request (self.client);
  GBinderRemoteReply *reply;
  GBinderWriter w;
  int status = 0;

  gbinder_local_request_init_writer (req, &w);
  if (request)
    append_rest (&w, request);
  reply = gbinder_client_transact_sync_reply (self.client, code, req, &status);
  gbinder_local_request_unref (req);
  if (status != GBINDER_STATUS_OK) {
    g_warning ("IHealth call %u failed (status %d)", code, status);
    if (reply)
      gbinder_remote_reply_unref (reply);
    return NULL;
  }
  return reply;
}

/* Looks up IHealth/default (the Android container of the phone may start later) */
static gboolean
hal_connect (void)
{
  int status = 0;

  if (self.client)
    return TRUE;
  if (!self.sm && !(self.sm = gbinder_servicemanager_new (BINDER_DEVICE)))
    return FALSE;
  self.remote = gbinder_servicemanager_get_service_sync (self.sm, HEALTH_INSTANCE, &status);
  if (!self.remote)
    return FALSE;
  /* get_service returns a reference that is released automatically */
  gbinder_remote_object_ref (self.remote);
  self.client = gbinder_client_new (self.remote, HEALTH_IFACE);
  self.death_id = gbinder_remote_object_add_death_handler (self.remote, on_hal_died, NULL);
  g_message ("Health HAL connected");
  return TRUE;
}

/* The vendor's current HealthInfo to one of Android's callbacks (only), or to all of them */
static void
push_info (Callback *only)
{
  GBinderRemoteReply *reply;
  GBinderReader r, copy;
  gint32 ex = -1;

  if (!self.callbacks || !hal_connect () || !(reply = hal_call (HEALTH_GET_HEALTH_INFO, NULL)))
    return;
  gbinder_remote_reply_init_reader (reply, &r);
  /* Only a reply with a HealthInfo in it: an empty one would read as null */
  if (gbinder_reader_read_int32 (&r, &ex) && ex == EX_NONE && gbinder_reader_bytes_remaining (&r)) {
    for (GList *l = self.callbacks; l; l = l->next) {
      if (only && l->data != only)
        continue;
      gbinder_reader_copy (&copy, &r);
      send_info (l->data, &copy);
    }
  }
  gbinder_remote_reply_unref (reply);
}

static gboolean
on_push (gpointer user_data)
{
  self.push_id = 0;
  push_info (NULL);
  return G_SOURCE_REMOVE;
}

static void
on_upower_changed (GDBusConnection *bus, const char *sender, const char *path, const char *iface,
                   const char *signal, GVariant *params, gpointer user_data)
{
  if (!self.push_id)
    self.push_id = g_timeout_add (PUSH_DELAY_MS, on_push, NULL);
}

/* ---- IHealth for Android ---- */

static void
write_exception (GBinderWriter *w, gint32 code)
{
  gbinder_writer_append_int32 (w, code);
  if (code != EX_NONE) {
    gbinder_writer_append_string16 (w, code == EX_UNSUPPORTED_OPERATION ? "Not supported"
                                       : code == EX_ILLEGAL_ARGUMENT ? "Bad argument"
                                       : "Health HAL unavailable");
    gbinder_writer_append_int32 (w, 0); /* no remote stack trace */
  }
}

static void
on_android_callback_died (GBinderRemoteObject *obj, void *user_data)
{
  Callback *c = user_data;

  c->death_id = 0;
  self.callbacks = g_list_remove (self.callbacks, c);
  callback_free (c);
}

static GList *
find_callback (GBinderRemoteObject *remote)
{
  for (GList *l = self.callbacks; l; l = l->next)
    if (((Callback *) l->data)->remote == remote)
      return l;
  return NULL;
}

/* registerCallback(callback): kept here, given the current HealthInfo at once */
static gint32
android_register_callback (GBinderReader *reader)
{
  GBinderRemoteObject *remote = gbinder_reader_read_object (reader);
  GList *known;
  Callback *c;

  if (!remote)
    return EX_ILLEGAL_ARGUMENT;
  known = find_callback (remote);
  if (known) {
    c = known->data;
  } else {
    c = g_new0 (Callback, 1);
    c->remote = gbinder_remote_object_ref (remote);
    c->client = gbinder_client_new (remote, HEALTH_CB_IFACE);
    c->death_id = gbinder_remote_object_add_death_handler (remote, on_android_callback_died, c);
    self.callbacks = g_list_append (self.callbacks, c);
  }
  push_info (c);
  return EX_NONE;
}

static gint32
android_unregister_callback (GBinderReader *reader)
{
  GBinderRemoteObject *remote = gbinder_reader_read_object (reader);
  GList *l = remote ? find_callback (remote) : NULL;

  if (!l)
    return EX_ILLEGAL_ARGUMENT;
  callback_free (l->data);
  self.callbacks = g_list_delete_link (self.callbacks, l);
  return EX_NONE;
}

static GBinderLocalReply *
on_android_call (GBinderLocalObject *obj, GBinderRemoteRequest *req, guint code, guint flags,
                 int *status, void *user_data)
{
  GBinderReader reader;
  GBinderWriter w;
  GBinderLocalReply *reply = gbinder_local_object_new_reply (obj);
  GBinderRemoteReply *hal_reply;

  gbinder_remote_request_init_reader (req, &reader);
  gbinder_local_reply_init_writer (reply, &w);
  *status = GBINDER_STATUS_OK;
  switch (code) {
  case TX_GET_INTERFACE_VERSION:
    write_exception (&w, EX_NONE);
    gbinder_writer_append_int32 (&w, ANDROID_VERSION);
    break;
  case TX_GET_INTERFACE_HASH:
    write_exception (&w, EX_NONE);
    gbinder_writer_append_string16 (&w, ANDROID_HASH);
    break;
  case HEALTH_REGISTER_CALLBACK:
    write_exception (&w, android_register_callback (&reader));
    break;
  case HEALTH_UNREGISTER_CALLBACK:
    write_exception (&w, android_unregister_callback (&reader));
    break;
  default:
    /* update() up to getHealthInfo(): no binder objects, so the parcels pass unchanged; the
     * methods of later versions are not Android's to call */
    if (code < HEALTH_UPDATE || code > HEALTH_GET_HEALTH_INFO) {
      write_exception (&w, EX_UNSUPPORTED_OPERATION);
    } else if (!hal_connect () || !(hal_reply = hal_call (code, &reader))) {
      write_exception (&w, EX_ILLEGAL_STATE);
    } else {
      GBinderReader r;

      gbinder_remote_reply_init_reader (hal_reply, &r);
      append_rest (&w, &r);
      gbinder_remote_reply_unref (hal_reply);
      /* The HAL calls its own callbacks after update(); Android's hear it from us */
      if (code == HEALTH_UPDATE)
        push_info (NULL);
    }
    break;
  }
  return reply;
}

static void
on_android_added (GBinderServiceManager *sm, int status, void *user_data)
{
  if (status)
    g_warning ("Cannot serve the battery to Android (status %d)", status);
  else
    g_message ("Serving the battery to Android");
}

static void
android_register (void)
{
  if (gbinder_servicemanager_is_present (self.android_sm))
    gbinder_servicemanager_add_service (self.android_sm, HEALTH_INSTANCE, self.service,
                                        on_android_added, NULL);
}

/* The container's service manager comes with every start of the container; when it goes, so do
 * the callbacks of the Android that ran in it */
static void
on_android_presence (GBinderServiceManager *sm, void *user_data)
{
  if (gbinder_servicemanager_is_present (sm)) {
    android_register ();
  } else {
    g_list_free_full (self.callbacks, (GDestroyNotify) callback_free);
    self.callbacks = NULL;
  }
}

/* Serve IHealth on the container's binder. Idempotent: called at start and whenever the device
 * node changes, until it can be opened */
static void
android_start (void)
{
  if (self.android_sm || access (ANDROID_DEVICE, R_OK | W_OK) != 0)
    return;
  self.android_sm = gbinder_servicemanager_new (ANDROID_DEVICE);
  if (!self.android_sm)
    return;
  self.service = gbinder_servicemanager_new_local_object (self.android_sm, HEALTH_IFACE,
                                                          on_android_call, NULL);
  gbinder_local_object_set_stability (self.service, GBINDER_STABILITY_VINTF);
  self.presence_id = gbinder_servicemanager_add_presence_handler (self.android_sm,
                                                                  on_android_presence, NULL);
  android_register ();
}

static void
on_android_node (GFileMonitor *monitor, GFile *file, GFile *other, GFileMonitorEvent event,
                 gpointer user_data)
{
  g_autofree char *base = g_file_get_basename (file);

  if (event != G_FILE_MONITOR_EVENT_DELETED && g_strcmp0 (base, self.node) == 0)
    android_start ();
}

/* The kernel creates the node at boot, but only the start of the container opens it to users
 * other than root: watch its directory (binderfs reports chmod) instead of polling */
static GFileMonitor *
android_watch (void)
{
  g_autofree char *path = realpath (ANDROID_DEVICE, NULL);
  g_autoptr (GFile) node = g_file_new_for_path (path ? path : ANDROID_DEVICE);
  g_autoptr (GFile) dir = g_file_get_parent (node);
  GFileMonitor *monitor = g_file_monitor_directory (dir, G_FILE_MONITOR_NONE, NULL, NULL);

  g_free (self.node);
  self.node = g_file_get_basename (node);
  if (monitor)
    g_signal_connect (monitor, "changed", G_CALLBACK (on_android_node), NULL);
  return monitor;
}

static gboolean
on_quit (gpointer loop)
{
  g_main_loop_quit (loop);
  return G_SOURCE_REMOVE;
}

int
main (int argc, char **argv)
{
  g_autoptr (GMainLoop) loop = g_main_loop_new (NULL, FALSE);
  g_autoptr (GFileMonitor) monitor = NULL;
  g_autoptr (GDBusConnection) bus = g_bus_get_sync (G_BUS_TYPE_SYSTEM, NULL, NULL);

  if (bus)
    g_dbus_connection_signal_subscribe (bus, UPOWER_NAME, "org.freedesktop.DBus.Properties",
                                        "PropertiesChanged", NULL, UPOWER_DEVICE,
                                        G_DBUS_SIGNAL_FLAGS_NONE, on_upower_changed, NULL, NULL);
  else
    g_message ("No system bus: Android hears of battery changes only when it asks");
  g_unix_signal_add (SIGTERM, on_quit, loop);
  g_unix_signal_add (SIGINT, on_quit, loop);
  monitor = android_watch ();
  android_start ();
  g_main_loop_run (loop);

  g_clear_handle_id (&self.push_id, g_source_remove);
  g_list_free_full (self.callbacks, (GDestroyNotify) callback_free);
  hal_disconnect ();
  g_clear_pointer (&self.sm, gbinder_servicemanager_unref);
  return 0;
}
