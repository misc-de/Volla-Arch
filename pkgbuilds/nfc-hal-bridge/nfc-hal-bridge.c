/*
 * nfc-hal-bridge: the phone's NFC HAL (AIDL android.hardware.nfc.INfc) for Android apps in the
 * Waydroid container
 *
 * Android's NFC stack talks NCI to the controller through INfc/default. This daemon serves INfc
 * on the container's /dev/anbox-binder, registered whenever the container's service manager
 * shows up (the node opens to users other than root only once the container has started;
 * inotify tells), and passes every call unchanged to the vendor HAL on /dev/binder: the vendor's
 * INfc is the same version 1 Android 13 is built against.
 *
 * Android's calls wait asynchronously (gbinder_remote_request_block): the main thread never
 * waits for the vendor HAL, so the HAL's callbacks (sendEvent, sendData) always get through, and
 * go on one-way to Android's callback. The daemon holds a callback of its own at the HAL while
 * Android has it open.
 *
 * The controller has one user at a time: the phone's nfcd (Linux apps) or Android. Android may
 * open the HAL only while NFC is switched off for Linux (nfcd's setting); switched on there, it
 * takes the controller back and Android hears of an error.
 *
 * Transaction codes follow the frozen AIDL definition (android.hardware.nfc version 1): the n-th
 * method has code n.
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
#define NFC_IFACE        "android.hardware.nfc.INfc"
#define NFC_CB_IFACE     "android.hardware.nfc.INfcClientCallback"
#define NFC_INSTANCE     NFC_IFACE "/default"
/* What Android gets: INfc version 1 and the hash of that frozen version */
#define ANDROID_VERSION  1
#define ANDROID_HASH     "37acf94ceb095ad537b66c3bd77c30a6ad5f5b0e"
/* nfcd's setting: NFC on or off for Linux */
#define NFCD_SETTINGS    "org.sailfishos.nfc.settings"
#define NFCD_SETTINGS_IF "org.sailfishos.nfc.Settings"
#define NFCD_DAEMON      "org.sailfishos.nfc.daemon"
#define NFCD_ADAPTER_IF  "org.sailfishos.nfc.Adapter"
#define NFCD_ADAPTER     "/nfc0"
/* Transactions every AIDL interface answers */
#define TX_GET_INTERFACE_VERSION 0x00ffffff
#define TX_GET_INTERFACE_HASH    0x00fffffe
/* AIDL exception codes */
#define EX_NONE                  0
#define EX_ILLEGAL_ARGUMENT      (-3)
#define EX_ILLEGAL_STATE         (-5)
#define EX_UNSUPPORTED_OPERATION (-7)

/* INfc */
enum {
  NFC_OPEN = 1,
  NFC_CLOSE = 2,
  NFC_GET_CONFIG = 5,
  NFC_IS_VERBOSE_LOGGING_ENABLED = 10,
};
/* INfcClientCallback */
enum {
  CB_SEND_DATA = 1,
  CB_SEND_EVENT = 2,
};
/* NfcEvent, NfcStatus, NfcCloseType */
#define EVENT_ERROR    5
/* The status Android's NFC stack acts on: a transport error restarts its NFC service, which then
 * opens anew (a plain FAILED is ignored, and Android keeps a session that is gone) */
#define STATUS_ERR_TRANSPORT 2
#define CLOSE_DISABLE  0

static struct {
  /* The vendor HAL */
  GBinderServiceManager *sm;
  GBinderRemoteObject   *remote;
  GBinderClient         *client;
  gulong                 death_id;
  GBinderLocalObject    *callback;  /* ours, given to the HAL in open() */
  /* The container */
  GBinderServiceManager *android_sm;
  GBinderLocalObject    *service;
  gulong                 presence_id;
  char                  *node;      /* name of the device node in its directory */
  GBinderRemoteObject   *android_remote;  /* Android's callback while it has the HAL open */
  GBinderClient         *android_cb;
  gulong                 android_death_id;
  /* Android opened while Linux had the controller: it gets the HAL when Linux lets go */
  gboolean               android_waiting;
  /* NFC switched on for Linux (nfcd): then the controller is nfcd's */
  gboolean               linux_nfc;
  /* nfcd's adapter is powered: nfcd still has the HAL open */
  gboolean               linux_powered;
} self;

static void
write_exception (GBinderWriter *w, gint32 code)
{
  gbinder_writer_append_int32 (w, code);
  if (code != EX_NONE) {
    gbinder_writer_append_string16 (w, code == EX_UNSUPPORTED_OPERATION ? "Not supported"
                                       : code == EX_ILLEGAL_ARGUMENT ? "Bad argument"
                                       : self.linux_nfc ? "NFC is switched on for Linux"
                                       : "NFC HAL unavailable");
    gbinder_writer_append_int32 (w, 0); /* no remote stack trace */
  }
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

/* ---- The vendor HAL ---- */

static void android_forget (void);

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
  g_warning ("NFC HAL died");
  hal_disconnect ();
  android_forget ();
}

static gboolean
hal_connect (void)
{
  int status = 0;

  if (self.client)
    return TRUE;
  if (!self.sm && !(self.sm = gbinder_servicemanager_new (BINDER_DEVICE)))
    return FALSE;
  self.remote = gbinder_servicemanager_get_service_sync (self.sm, NFC_INSTANCE, &status);
  if (!self.remote)
    return FALSE;
  /* get_service returns a reference that is released automatically */
  gbinder_remote_object_ref (self.remote);
  self.client = gbinder_client_new (self.remote, NFC_IFACE);
  self.death_id = gbinder_remote_object_add_death_handler (self.remote, on_hal_died, NULL);
  g_message ("NFC HAL connected");
  return TRUE;
}

/* The HAL calls our callback: on to Android's, one-way */
static GBinderLocalReply *
on_hal_callback (GBinderLocalObject *obj, GBinderRemoteRequest *req, guint code, guint flags,
                 int *status, void *user_data)
{
  GBinderReader reader;

  if (g_strcmp0 (gbinder_remote_request_interface (req), NFC_CB_IFACE)) {
    *status = GBINDER_STATUS_FAILED;
    return NULL;
  }
  /* A waiting Android hears nothing from an earlier session of the HAL */
  if ((code == CB_SEND_DATA || code == CB_SEND_EVENT) && self.android_cb && !self.android_waiting) {
    GBinderLocalRequest *out = gbinder_client_new_request (self.android_cb);
    GBinderWriter w;

    gbinder_remote_request_init_reader (req, &reader);
    gbinder_local_request_init_writer (out, &w);
    append_rest (&w, &reader);
    gbinder_client_transact (self.android_cb, code, GBINDER_TX_FLAG_ONEWAY, out, NULL, NULL, NULL);
    gbinder_local_request_unref (out);
  }
  *status = GBINDER_STATUS_OK;
  /* AIDL reply: exception code 0 (EX_NONE) */
  return gbinder_local_reply_append_int32 (gbinder_local_object_new_reply (obj), 0);
}

/* ---- Android's side ---- */

/* Android no longer has the HAL open */
static void
android_forget (void)
{
  if (self.android_remote && self.android_death_id)
    gbinder_remote_object_remove_handler (self.android_remote, self.android_death_id);
  self.android_death_id = 0;
  g_clear_pointer (&self.android_cb, gbinder_client_unref);
  g_clear_pointer (&self.android_remote, gbinder_remote_object_unref);
  self.android_waiting = FALSE;
}

/* Closes the HAL for Android (it is gone, or the controller goes back to Linux) */
static void
android_release (void)
{
  if (!self.android_cb)
    return;
  /* A waiting Android never had the HAL opened for it */
  if (self.client && !self.android_waiting) {
    GBinderLocalRequest *req = gbinder_client_new_request (self.client);

    gbinder_local_request_append_int32 (req, CLOSE_DISABLE);
    gbinder_client_transact (self.client, NFC_CLOSE, GBINDER_TX_FLAG_ONEWAY, req, NULL, NULL, NULL);
    gbinder_local_request_unref (req);
  }
  android_forget ();
}

static void
on_android_died (GBinderRemoteObject *obj, void *user_data)
{
  g_message ("Android's NFC service is gone");
  self.android_death_id = 0;
  android_release ();
}

/* An Android call the HAL has answered: its reply goes back to Android as it is */
static void
on_hal_reply (GBinderClient *client, GBinderRemoteReply *reply, int status, void *user_data)
{
  GBinderRemoteRequest *req = user_data;
  GBinderLocalReply *local = gbinder_local_object_new_reply (self.service);
  GBinderWriter w;

  gbinder_local_reply_init_writer (local, &w);
  if (status == GBINDER_STATUS_OK && reply) {
    GBinderReader r;

    gbinder_remote_reply_init_reader (reply, &r);
    append_rest (&w, &r);
  } else {
    g_warning ("NFC HAL call failed (status %d)", status);
    write_exception (&w, EX_ILLEGAL_STATE);
  }
  gbinder_remote_request_complete (req, local, GBINDER_STATUS_OK);
  gbinder_local_reply_unref (local);
  gbinder_remote_request_unref (req);
}

/* open(callback): Android's callback kept, ours to the HAL */
/* Linux has the controller: nfcd's setting is on, or nfcd has not let go of the HAL yet */
static gboolean
linux_has_nfc (void)
{
  return self.linux_nfc || self.linux_powered;
}

static void
hal_append_open (GBinderLocalRequest *out)
{
  if (!self.callback) {
    self.callback = gbinder_servicemanager_new_local_object (self.sm, NFC_CB_IFACE,
                                                             on_hal_callback, NULL);
    /* Vendor HALs only accept callbacks with VINTF stability */
    gbinder_local_object_set_stability (self.callback, GBINDER_STABILITY_VINTF);
  }
  gbinder_local_request_append_local_object (out, self.callback);
}

/* open() for Android: its callback is kept; ANDROID_WAITS if Linux has the controller, then
 * Android hears nothing until Linux lets go (android_give), and its NFC stack waits for the
 * HAL's open event as it does for a slow controller. Refusing instead made Android's NFC service
 * crash on a command timeout and come back only on its next start */
#define ANDROID_WAITS 1
static gint32
android_open (GBinderReader *reader, GBinderLocalRequest *out)
{
  GBinderRemoteObject *remote = gbinder_reader_read_object (reader);

  if (!remote)
    return EX_ILLEGAL_ARGUMENT;
  android_forget ();
  self.android_remote = gbinder_remote_object_ref (remote);
  self.android_cb = gbinder_client_new (remote, NFC_CB_IFACE);
  self.android_death_id = gbinder_remote_object_add_death_handler (remote, on_android_died, NULL);
  if (linux_has_nfc ()) {
    self.android_waiting = TRUE;
    g_message ("Android waits for NFC: it is switched on for Linux");
    return ANDROID_WAITS;
  }
  hal_append_open (out);
  g_message ("Android opens NFC");
  return EX_NONE;
}

static void
on_give_reply (GBinderClient *client, GBinderRemoteReply *reply, int status, void *user_data)
{
  GBinderReader reader;
  gint32 ex = -1;

  if (reply) {
    gbinder_remote_reply_init_reader (reply, &reader);
    gbinder_reader_read_int32 (&reader, &ex);
  }
  if (status || ex)
    g_warning ("NFC HAL did not open for Android (status %d, exception %d)", status, ex);
}

/* Linux has let go: a waiting Android gets the HAL now; the HAL's open event reaches it through
 * our callback and its NFC stack goes on */
static void
android_give (void)
{
  GBinderLocalRequest *req;

  if (!self.android_waiting || linux_has_nfc () || !hal_connect ())
    return;
  self.android_waiting = FALSE;
  req = gbinder_client_new_request (self.client);
  hal_append_open (req);
  if (gbinder_client_transact (self.client, NFC_OPEN, 0, req, on_give_reply, NULL, NULL))
    g_message ("NFC released by Linux: Android opens it");
  else
    g_warning ("NFC HAL did not open for Android");
  gbinder_local_request_unref (req);
}

static GBinderLocalReply *
on_android_call (GBinderLocalObject *obj, GBinderRemoteRequest *req, guint code, guint flags,
                 int *status, void *user_data)
{
  GBinderReader reader;
  GBinderWriter w;
  GBinderLocalReply *reply = gbinder_local_object_new_reply (obj);
  GBinderLocalRequest *out;
  gint32 ex = EX_NONE;

  gbinder_remote_request_init_reader (req, &reader);
  gbinder_local_reply_init_writer (reply, &w);
  *status = GBINDER_STATUS_OK;
  switch (code) {
  case TX_GET_INTERFACE_VERSION:
    write_exception (&w, EX_NONE);
    gbinder_writer_append_int32 (&w, ANDROID_VERSION);
    return reply;
  case TX_GET_INTERFACE_HASH:
    write_exception (&w, EX_NONE);
    gbinder_writer_append_string16 (&w, ANDROID_HASH);
    return reply;
  }
  if (code < NFC_OPEN || code > NFC_IS_VERBOSE_LOGGING_ENABLED) {
    write_exception (&w, EX_UNSUPPORTED_OPERATION);
    return reply;
  }
  if (!hal_connect ()) {
    write_exception (&w, EX_ILLEGAL_STATE);
    return reply;
  }
  out = gbinder_client_new_request (self.client);
  if (code == NFC_OPEN) {
    ex = android_open (&reader, out);
  } else if (code == NFC_CLOSE && self.android_waiting) {
    /* It gives up waiting: there is nothing to close at the HAL */
    android_forget ();
    ex = ANDROID_WAITS;
  } else if ((!self.android_cb || self.android_waiting) && code != NFC_GET_CONFIG &&
             code != NFC_IS_VERBOSE_LOGGING_ENABLED) {
    /* Only open() starts a session; nothing else reaches the HAL while Linux has it. The two
     * questions read the HAL's settings only, and Android asks for its configuration (the
     * controller's presence check, host allow list, routes) before it opens */
    ex = EX_ILLEGAL_STATE;
  } else {
    GBinderWriter ow;

    gbinder_local_request_init_writer (out, &ow);
    append_rest (&ow, &reader);
  }
  if (ex == ANDROID_WAITS) {
    /* Answered here, the HAL is not asked */
    gbinder_local_request_unref (out);
    write_exception (&w, EX_NONE);
    return reply;
  }
  if (ex != EX_NONE) {
    gbinder_local_request_unref (out);
    write_exception (&w, ex);
    return reply;
  }
  if (code == NFC_CLOSE)
    android_forget ();
  /* The reply comes when the HAL answers (on_hal_reply, on this main thread, so never before
   * the request is blocked); the main thread goes on meanwhile */
  gbinder_remote_request_ref (req);
  if (!gbinder_client_transact (self.client, code, 0, out, on_hal_reply, NULL, req)) {
    gbinder_remote_request_unref (req);
    gbinder_local_request_unref (out);
    write_exception (&w, EX_ILLEGAL_STATE);
    return reply;
  }
  gbinder_remote_request_block (req);
  gbinder_local_request_unref (out);
  gbinder_local_reply_unref (reply);
  return NULL;
}

/* NFC switched on for Linux: the controller goes back to nfcd, Android hears of an error. The
 * HAL is not closed: nfcd opens it in the same moment (its own setting is what changed), and the
 * HAL has a single client, so a close arriving after nfcd's open() would end nfcd's session
 * (measured: nfcd's CORE_RESET failed, "State machine broke", and NFC stayed unpowered until
 * nfcd restarted). nfcd's open() takes the HAL over from Android's session instead */
static void
set_linux_nfc (gboolean on)
{
  self.linux_nfc = on;
  if (!on) {
    android_give ();
    return;
  }
  if (!self.android_cb || self.android_waiting)
    return;
  {
    GBinderLocalRequest *req = gbinder_client_new_request (self.android_cb);

    gbinder_local_request_append_int32 (req, EVENT_ERROR);
    gbinder_local_request_append_int32 (req, STATUS_ERR_TRANSPORT);
    gbinder_client_transact (self.android_cb, CB_SEND_EVENT, GBINDER_TX_FLAG_ONEWAY, req,
                             NULL, NULL, NULL);
    gbinder_local_request_unref (req);
  }
  g_message ("NFC switched on for Linux: Android loses it");
  android_forget ();
}

static void
on_nfcd_enabled (GDBusConnection *bus, const char *sender, const char *path, const char *iface,
                 const char *signal, GVariant *params, gpointer user_data)
{
  gboolean on = FALSE;

  if (g_variant_is_of_type (params, G_VARIANT_TYPE ("(b)"))) {
    g_variant_get (params, "(b)", &on);
    set_linux_nfc (on);
  }
}

/* nfcd's adapter powered on or off: off, nfcd has closed the HAL, and a waiting Android may have it */
static void
on_nfcd_powered (GDBusConnection *bus, const char *sender, const char *path, const char *iface,
                 const char *signal, GVariant *params, gpointer user_data)
{
  if (g_variant_is_of_type (params, G_VARIANT_TYPE ("(b)"))) {
    g_variant_get (params, "(b)", &self.linux_powered);
    if (!self.linux_powered)
      android_give ();
  }
}

static void
on_android_added (GBinderServiceManager *sm, int status, void *user_data)
{
  if (status)
    g_warning ("Cannot serve NFC to Android (status %d)", status);
  else
    g_message ("Serving NFC to Android");
}

static void
android_register (void)
{
  if (gbinder_servicemanager_is_present (self.android_sm))
    gbinder_servicemanager_add_service (self.android_sm, NFC_INSTANCE, self.service,
                                        on_android_added, NULL);
}

/* The container's service manager comes with every start of the container; when it goes, so
 * does the NFC session of the Android that ran in it */
static void
on_android_presence (GBinderServiceManager *sm, void *user_data)
{
  if (gbinder_servicemanager_is_present (sm))
    android_register ();
  else
    android_release ();
}

/* Serve INfc on the container's binder. Idempotent: called at start and whenever the device
 * node changes, until it can be opened */
static void
android_start (void)
{
  if (self.android_sm || access (ANDROID_DEVICE, R_OK | W_OK) != 0)
    return;
  self.android_sm = gbinder_servicemanager_new (ANDROID_DEVICE);
  if (!self.android_sm)
    return;
  self.service = gbinder_servicemanager_new_local_object (self.android_sm, NFC_IFACE,
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

/* nfcd's setting now and from then on; without nfcd (or bus) Linux is taken to want NFC, so
 * Android never takes the controller from it unasked */
static void
watch_nfcd (GDBusConnection *bus)
{
  g_autoptr (GVariant) value = NULL;

  self.linux_nfc = TRUE;
  if (!bus) {
    g_message ("No system bus: NFC stays with Linux");
    return;
  }
  g_dbus_connection_signal_subscribe (bus, NFCD_SETTINGS, NFCD_SETTINGS_IF, "EnabledChanged", "/",
                                      NULL, G_DBUS_SIGNAL_FLAGS_NONE, on_nfcd_enabled, NULL, NULL);
  value = g_dbus_connection_call_sync (bus, NFCD_SETTINGS, "/", NFCD_SETTINGS_IF, "GetEnabled",
                                       NULL, G_VARIANT_TYPE ("(b)"), G_DBUS_CALL_FLAGS_NONE, 5000,
                                       NULL, NULL);
  if (value)
    g_variant_get (value, "(b)", &self.linux_nfc);
  g_clear_pointer (&value, g_variant_unref);
  /* Whether nfcd still has the HAL open (its adapter powered); without nfcd it has not */
  g_dbus_connection_signal_subscribe (bus, NFCD_DAEMON, NFCD_ADAPTER_IF, "PoweredChanged",
                                      NFCD_ADAPTER, NULL, G_DBUS_SIGNAL_FLAGS_NONE, on_nfcd_powered,
                                      NULL, NULL);
  value = g_dbus_connection_call_sync (bus, NFCD_DAEMON, NFCD_ADAPTER, NFCD_ADAPTER_IF, "GetPowered",
                                       NULL, G_VARIANT_TYPE ("(b)"), G_DBUS_CALL_FLAGS_NONE, 5000,
                                       NULL, NULL);
  if (value)
    g_variant_get (value, "(b)", &self.linux_powered);
  g_message ("NFC for Linux: %s%s", self.linux_nfc ? "on" : "off",
             self.linux_powered ? " (powered)" : "");
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
  g_autoptr (GDBusConnection) bus = NULL;

  /* First: a signal during the set-up must end the loop, not the process */
  g_unix_signal_add (SIGTERM, on_quit, loop);
  g_unix_signal_add (SIGINT, on_quit, loop);
  bus = g_bus_get_sync (G_BUS_TYPE_SYSTEM, NULL, NULL);
  watch_nfcd (bus);
  monitor = android_watch ();
  android_start ();
  g_main_loop_run (loop);

  android_release ();
  hal_disconnect ();
  g_clear_pointer (&self.sm, gbinder_servicemanager_unref);
  return 0;
}
