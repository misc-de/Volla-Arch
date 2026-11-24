/*
 * gnss-nmea-bridge: Android GNSS HAL (AIDL android.hardware.gnss.IGnss) -> NMEA on a Unix socket
 *
 * geoclue reads the socket unchanged via its NMEA source:
 *   [network-nmea]
 *   enable=true
 *   nmea-socket=/run/gnss-nmea-bridge/nmea.sock
 *
 * The receiver runs only while at least one client is connected. If the HAL sends no NMEA
 * sentences of its own, $GPRMC and $GPGGA are generated from gnssLocationCb. GGA and RMC
 * sentences without a real fix are not passed on, so apps only ever see GPS fixes.
 *
 * Transaction codes follow the frozen AIDL definitions (android.hardware.gnss version 3,
 * aidl_api in AOSP hardware/interfaces): the n-th method has code n.
 *
 * SPDX-License-Identifier: MIT
 */

#include <errno.h>
#include <math.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include <grp.h>

#include <gio/gio.h>
#include <gio/gunixsocketaddress.h>
#include <glib-unix.h>
#include <gbinder.h>

#define BINDER_DEVICE   "/dev/binder"
#define GNSS_IFACE      "android.hardware.gnss.IGnss"
#define GNSS_CB_IFACE   "android.hardware.gnss.IGnssCallback"
#define GNSS_INSTANCE   GNSS_IFACE "/default"
#define SOCKET_PATH     "/run/gnss-nmea-bridge/nmea.sock"

/* IGnss */
enum {
  GNSS_SET_CALLBACK = 1,
  GNSS_START = 14,
  GNSS_STOP = 15,
  GNSS_SET_POSITION_MODE = 20,
  GNSS_START_NMEA = 25,
  GNSS_STOP_NMEA = 26,
};

/* IGnssCallback */
enum {
  CB_SET_CAPABILITIES = 1,
  CB_LOCATION = 4,
  CB_NMEA = 5,
};

/* GnssLocationFlags */
#define HAS_LAT_LONG    0x0001
#define HAS_ALTITUDE    0x0002
#define HAS_SPEED       0x0004
#define HAS_BEARING     0x0008
#define HAS_H_ACCURACY  0x0010

/* Seconds without HAL NMEA after which sentences are generated from locations */
#define NMEA_SILENCE_US (3 * G_USEC_PER_SEC)

typedef struct {
  GBinderServiceManager *sm;
  GBinderRemoteObject   *remote;
  GBinderClient         *client;
  GBinderLocalObject    *callback;
  gulong                 death_id;
  gboolean               running;
  gint64                 last_nmea_us;
  GSocketService        *service;
  GList                 *clients;   /* GSocketConnection */
  guint                  retry_id;
} Bridge;

static Bridge bridge;

static void bridge_update (void);
static gboolean on_retry (gpointer user_data);

/* Drop a client; when the last one is gone, schedule stopping the receiver */
static void
client_remove (GSocketConnection *conn)
{
  GList *l = g_list_find (bridge.clients, conn);

  if (!l)
    return;
  bridge.clients = g_list_delete_link (bridge.clients, l);
  g_source_destroy (g_object_get_data (G_OBJECT (conn), "watch"));
  g_io_stream_close (G_IO_STREAM (conn), NULL, NULL);
  g_object_unref (conn);
  /* Not from within a HAL callback: stopping synchronously there may deadlock the HAL */
  if (!bridge.clients && !bridge.retry_id)
    bridge.retry_id = g_idle_add (on_retry, NULL);
}

/* Write one complete NMEA line to every connected client */
static void
broadcast (const char *line)
{
  gsize len = strlen (line);
  GList *l = bridge.clients;

  while (l) {
    GList *next = l->next;
    GSocket *sock = g_socket_connection_get_socket (l->data);
    g_autoptr (GError) err = NULL;

    gssize sent = g_socket_send (sock, line, len, NULL, &err);

    /* Nothing written: skip this sentence for a slow reader. Part of it written: finish the
     * line (briefly waiting) or drop the client, a torn sentence would corrupt the stream. */
    if (sent < 0 && g_error_matches (err, G_IO_ERROR, G_IO_ERROR_WOULD_BLOCK)) {
      l = next;
      continue;
    }
    gsize done = MAX (sent, 0);
    while (sent >= 0 && done < len) {
      g_clear_error (&err);
      if (!g_socket_condition_timed_wait (sock, G_IO_OUT, 100000, NULL, &err) ||
          (sent = g_socket_send (sock, line + done, len - done, NULL, &err)) < 0)
        sent = -1;
      else
        done += sent;
    }
    if (sent < 0) {
      g_debug ("Dropping NMEA client: %s", err ? err->message : "write failed");
      client_remove (l->data);
    }
    l = next;
  }
}

/* Frame body as "$body*CS\r\n" with the NMEA XOR checksum and broadcast it */
static void
send_sentence (const char *body)
{
  guint8 sum = 0;
  g_autofree char *line = NULL;

  for (const char *p = body; *p; p++)
    sum ^= (guint8) *p;
  line = g_strdup_printf ("$%s*%02X\r\n", body, sum);
  broadcast (line);
}

/* Degrees -> NMEA ddmm.mmmm (lat) / dddmm.mmmm (lon) plus hemisphere letter */
static void
format_coord (double deg, gboolean lat, char *buf, gsize size, char *hemi)
{
  /* Round once, in units of 1/10000 minute: rounding the minutes alone turns 59.99999 into
   * "60.0000", which is no valid NMEA */
  gint64 t = llround (fabs (deg) * 600000.0);
  int d = t / 600000;
  int m = (t % 600000) / 10000, frac = t % 10000;

  *hemi = lat ? (deg < 0 ? 'S' : 'N') : (deg < 0 ? 'W' : 'E');
  g_snprintf (buf, size, lat ? "%02d%02d.%04d" : "%03d%02d.%04d", d, m, frac);
}

/* $GPRMC and $GPGGA from a GnssLocation, for HALs that report no NMEA themselves */
static void
emit_location (gint32 flags, double lat, double lon, double alt, double speed,
               double bearing, double h_acc, gint64 ts_ms)
{
  char la[16], lo[16], ns, ew, hms[16], dmy[8];
  time_t t = ts_ms / 1000;
  struct tm tm;
  g_autofree char *rmc = NULL, *gga = NULL;

  if (!(flags & HAS_LAT_LONG) || !gmtime_r (&t, &tm))
    return;
  g_snprintf (hms, sizeof hms, "%02d%02d%02d.%02d", tm.tm_hour, tm.tm_min, tm.tm_sec,
              (int) (ts_ms % 1000) / 10);
  g_snprintf (dmy, sizeof dmy, "%02d%02d%02d", tm.tm_mday, tm.tm_mon + 1, tm.tm_year % 100);
  format_coord (lat, TRUE, la, sizeof la, &ns);
  format_coord (lon, FALSE, lo, sizeof lo, &ew);

  rmc = g_strdup_printf ("GPRMC,%s,A,%s,%c,%s,%c,%.1f,%.1f,%s,,,A", hms, la, ns, lo, ew,
                         (flags & HAS_SPEED) ? speed * 1.943844 : 0.0,
                         (flags & HAS_BEARING) ? bearing : 0.0, dmy);
  /* HDOP is not known; geoclue takes the accuracy from it, approximate with h_acc / 5 m */
  gga = g_strdup_printf ("GPGGA,%s,%s,%c,%s,%c,1,04,%.1f,%.1f,M,,M,,", hms, la, ns, lo, ew,
                         (flags & HAS_H_ACCURACY) ? MAX (h_acc / 5.0, 0.5) : 1.0,
                         (flags & HAS_ALTITUDE) ? alt : 0.0);
  send_sentence (rmc);
  send_sentence (gga);
}

/* IGnssCallback.gnssLocationCb: synthesize sentences only while the HAL sends no NMEA */
static void
handle_location (GBinderReader *reader)
{
  gint32 present = 0, size = 0, flags = 0;
  double lat = 0, lon = 0, alt = 0, speed = 0, bearing = 0, h_acc = 0, v_acc, s_acc, b_acc;
  gint64 ts = 0;

  /* GnssLocation: non-null marker, parcelable size, fields in declaration order */
  if (!gbinder_reader_read_int32 (reader, &present) || !present ||
      !gbinder_reader_read_int32 (reader, &size) ||
      !gbinder_reader_read_int32 (reader, &flags) ||
      !gbinder_reader_read_double (reader, &lat) ||
      !gbinder_reader_read_double (reader, &lon) ||
      !gbinder_reader_read_double (reader, &alt) ||
      !gbinder_reader_read_double (reader, &speed) ||
      !gbinder_reader_read_double (reader, &bearing) ||
      !gbinder_reader_read_double (reader, &h_acc) ||
      !gbinder_reader_read_double (reader, &v_acc) ||
      !gbinder_reader_read_double (reader, &s_acc) ||
      !gbinder_reader_read_double (reader, &b_acc) ||
      !gbinder_reader_read_int64 (reader, &ts))
    return;

  if (g_get_monotonic_time () - bridge.last_nmea_us > NMEA_SILENCE_US)
    emit_location (flags, lat, lon, alt, speed, bearing, h_acc, ts);
}

/* Longest sentence passed on; NMEA 0183 allows 82 characters, vendor sentences may be longer */
#define NMEA_MAX_LEN 256

/* FALSE for GGA and RMC sentences that carry no real fix; geoclue takes positions only from
 * these two. It drops GGA quality 0 and RMC status V itself, but would pass estimated (dead
 * reckoning), manual and simulated positions on to the apps. Other sentences pass. */
static gboolean
nmea_has_fix (const char *line)
{
  g_auto (GStrv) f = NULL;

  if (strlen (line) < 6)
    return TRUE;
  if (g_str_has_prefix (line + 3, "GGA")) {
    /* Quality 1-5: GPS, DGPS, PPS, RTK, float RTK; not 0 (none), 6 (estimated), 7 (manual),
     * 8 (simulation) */
    f = g_strsplit (line, ",", -1);
    return g_strv_length (f) > 6 && f[6][0] >= '1' && f[6][0] <= '5' && !f[6][1];
  }
  if (g_str_has_prefix (line + 3, "RMC")) {
    f = g_strsplit (line, ",", -1);
    if (g_strv_length (f) < 3 || g_strcmp0 (f[2], "A"))
      return FALSE;
    /* Mode indicator (NMEA 2.3+): not N (no fix), E (estimated), M (manual), S (simulator) */
    return g_strv_length (f) < 13 || !f[12][0] || !strchr ("NEMS", f[12][0]);
  }
  return TRUE;
}

/* IGnssCallback.gnssNmeaCb (timestamp, sentence): pass the sentences through unchanged.
 * Each line on its own and only printable ASCII, so the HAL cannot slip control characters
 * or half lines to the clients. Positions without a fix are held back (nmea_has_fix). */
static void
handle_nmea (GBinderReader *reader)
{
  gint64 ts;
  GBinderReader copy;
  g_autofree char *nmea = NULL;
  g_auto (GStrv) lines = NULL;

  if (!gbinder_reader_read_int64 (reader, &ts))
    return;
  /* AIDL sends String as UTF-16 (@utf8InCpp only changes the C++ type); accept UTF-8 too */
  gbinder_reader_copy (&copy, reader);
  nmea = gbinder_reader_read_string16 (reader);
  if (!nmea)
    nmea = g_strdup (gbinder_reader_read_string8 (&copy));
  if (!nmea || nmea[0] != '$')
    return;

  bridge.last_nmea_us = g_get_monotonic_time ();
  lines = g_strsplit_set (nmea, "\r\n", -1);
  for (char **l = lines; *l; l++) {
    gsize len = strlen (*l);
    gboolean ok = (*l)[0] == '$' && len <= NMEA_MAX_LEN;

    for (gsize i = 0; ok && i < len; i++)
      ok = g_ascii_isprint ((*l)[i]);
    if (ok && nmea_has_fix (*l)) {
      g_autofree char *line = g_strconcat (*l, "\r\n", NULL);
      broadcast (line);
    }
  }
}

/* Transaction handler of our IGnssCallback object */
static GBinderLocalReply *
on_callback (GBinderLocalObject *obj, GBinderRemoteRequest *req, guint code, guint flags,
             int *status, void *user_data)
{
  GBinderReader reader;

  if (g_strcmp0 (gbinder_remote_request_interface (req), GNSS_CB_IFACE)) {
    *status = GBINDER_STATUS_FAILED;
    return NULL;
  }
  gbinder_remote_request_init_reader (req, &reader);
  switch (code) {
  case CB_NMEA:
    handle_nmea (&reader);
    break;
  case CB_LOCATION:
    handle_location (&reader);
    break;
  case CB_SET_CAPABILITIES: {
    guint32 caps;
    if (gbinder_reader_read_uint32 (&reader, &caps))
      g_debug ("GNSS capabilities 0x%x", caps);
    break;
  }
  default:
    /* Status, satellites, wakelocks, time and location requests: acknowledge only */
    break;
  }
  *status = GBINDER_STATUS_OK;
  /* AIDL reply: exception code 0 (EX_NONE) */
  return gbinder_local_reply_append_int32 (gbinder_local_object_new_reply (obj), 0);
}

/* Synchronous IGnss transaction; TRUE if both binder status and AIDL exception are 0 */
static gboolean
call (guint code, GBinderLocalRequest *req)
{
  int status = 0;
  gint32 ex = -1;
  GBinderRemoteReply *reply;
  GBinderReader reader;

  reply = gbinder_client_transact_sync_reply (bridge.client, code, req, &status);
  if (reply) {
    gbinder_remote_reply_init_reader (reply, &reader);
    gbinder_reader_read_int32 (&reader, &ex);
    gbinder_remote_reply_unref (reply);
  }
  if (status || ex) {
    g_warning ("IGnss transaction %u failed (status %d, exception %d)", code, status, ex);
    return FALSE;
  }
  return TRUE;
}

/* Release all HAL references; the next bridge_update() reconnects */
static void
hal_disconnect (void)
{
  if (bridge.remote && bridge.death_id)
    gbinder_remote_object_remove_handler (bridge.remote, bridge.death_id);
  bridge.death_id = 0;
  g_clear_pointer (&bridge.client, gbinder_client_unref);
  g_clear_pointer (&bridge.callback, gbinder_local_object_unref);
  g_clear_pointer (&bridge.remote, gbinder_remote_object_unref);
  bridge.running = FALSE;
}

static void
on_hal_died (GBinderRemoteObject *obj, void *user_data)
{
  g_warning ("GNSS HAL died");
  hal_disconnect ();
  bridge_update ();
}

/* Look up IGnss/default and register our callback (setCallback) */
static gboolean
hal_connect (void)
{
  int status = 0;
  GBinderLocalRequest *req;
  gboolean ok;

  if (bridge.client)
    return TRUE;
  bridge.remote = gbinder_servicemanager_get_service_sync (bridge.sm, GNSS_INSTANCE, &status);
  if (!bridge.remote)
    return FALSE;
  /* get_service returns a reference that is released automatically */
  gbinder_remote_object_ref (bridge.remote);
  bridge.client = gbinder_client_new (bridge.remote, GNSS_IFACE);
  bridge.death_id = gbinder_remote_object_add_death_handler (bridge.remote, on_hal_died, NULL);
  bridge.callback = gbinder_servicemanager_new_local_object (bridge.sm, GNSS_CB_IFACE,
                                                             on_callback, NULL);
  /* Vendor HALs only accept callbacks with VINTF stability */
  gbinder_local_object_set_stability (bridge.callback, GBINDER_STABILITY_VINTF);

  req = gbinder_client_new_request (bridge.client);
  gbinder_local_request_append_local_object (req, bridge.callback);
  ok = call (GNSS_SET_CALLBACK, req);
  gbinder_local_request_unref (req);
  if (!ok)
    hal_disconnect ();
  return ok;
}

/* setPositionMode (1 Hz standalone), start, startNmea */
static gboolean
hal_start (void)
{
  GBinderLocalRequest *req;
  GBinderWriter w;
  gboolean ok;

  req = gbinder_client_new_request (bridge.client);
  gbinder_local_request_init_writer (req, &w);
  /* PositionModeOptions: non-null, size incl. this field, mode STANDALONE, recurrence
   * PERIODIC, 1000 ms interval, accuracy and time without preference, no low-power mode */
  gbinder_writer_append_int32 (&w, 1);
  gbinder_writer_append_int32 (&w, 7 * sizeof (gint32));
  gbinder_writer_append_int32 (&w, 0);
  gbinder_writer_append_int32 (&w, 0);
  gbinder_writer_append_int32 (&w, 1000);
  gbinder_writer_append_int32 (&w, 0);
  gbinder_writer_append_int32 (&w, 0);
  gbinder_writer_append_int32 (&w, 0);
  ok = call (GNSS_SET_POSITION_MODE, req);
  gbinder_local_request_unref (req);

  ok = ok && call (GNSS_START, NULL);
  /* Since IGnss V2 the HAL reports NMEA only after startNmea(); without it emit_location() helps */
  if (ok)
    call (GNSS_START_NMEA, NULL);
  return ok;
}

static void
hal_stop (void)
{
  call (GNSS_STOP_NMEA, NULL);
  call (GNSS_STOP, NULL);
}

static gboolean
on_retry (gpointer user_data)
{
  bridge.retry_id = 0;
  bridge_update ();
  return G_SOURCE_REMOVE;
}

/* Receiver on while clients are connected, off otherwise */
static void
bridge_update (void)
{
  gboolean want = bridge.clients != NULL;

  if (want == bridge.running)
    return;
  if (want) {
    if (hal_connect () && hal_start ()) {
      bridge.running = TRUE;
      g_message ("GNSS started");
    } else if (!bridge.retry_id) {
      /* HAL not registered yet (container still starting) or start refused */
      bridge.retry_id = g_timeout_add_seconds (3, on_retry, NULL);
    }
  } else {
    hal_stop ();
    bridge.running = FALSE;
    g_message ("GNSS stopped");
  }
}

/* Clients only read; readable means closed (or unexpected input, which is discarded) */
static gboolean
on_client_io (GSocket *sock, GIOCondition cond, gpointer conn)
{
  char buf[256];

  if (!(cond & (G_IO_HUP | G_IO_ERR)) && g_socket_receive (sock, buf, sizeof buf, NULL, NULL) > 0)
    return G_SOURCE_CONTINUE;
  client_remove (conn);
  return G_SOURCE_REMOVE;
}

/* New client: watch it for hang-up and start the receiver if needed */
static gboolean
on_incoming (GSocketService *service, GSocketConnection *conn, GObject *source, gpointer data)
{
  GSocket *sock = g_socket_connection_get_socket (conn);
  GSource *watch;

  g_socket_set_blocking (sock, FALSE);
  watch = g_socket_create_source (sock, G_IO_IN | G_IO_HUP | G_IO_ERR, NULL);
  g_source_set_callback (watch, G_SOURCE_FUNC (on_client_io), conn, NULL);
  g_source_attach (watch, NULL);
  g_object_set_data_full (G_OBJECT (conn), "watch", watch, (GDestroyNotify) g_source_unref);
  bridge.clients = g_list_prepend (bridge.clients, g_object_ref (conn));
  bridge_update ();
  return TRUE;
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
  g_autoptr (GSocketAddress) addr = NULL;
  g_autoptr (GError) err = NULL;
  struct group *gr;

  bridge.sm = gbinder_servicemanager_new (BINDER_DEVICE);
  if (!bridge.sm) {
    g_printerr ("Cannot open %s\n", BINDER_DEVICE);
    return 1;
  }

  unlink (SOCKET_PATH);
  addr = g_unix_socket_address_new (SOCKET_PATH);
  bridge.service = g_socket_service_new ();
  if (!g_socket_listener_add_address (G_SOCKET_LISTENER (bridge.service), addr,
                                      G_SOCKET_TYPE_STREAM, G_SOCKET_PROTOCOL_DEFAULT,
                                      NULL, NULL, &err)) {
    g_printerr ("Cannot listen on %s: %s\n", SOCKET_PATH, err->message);
    return 1;
  }
  /* Positions only for geoclue, not for every local user. Only the group changes (owner -1):
   * the service runs as a dynamic user in the supplementary group geoclue, which may do that
   * without CAP_CHOWN. */
  gr = getgrnam ("geoclue");
  if (gr && chown (SOCKET_PATH, (uid_t) -1, gr->gr_gid) == 0)
    chmod (SOCKET_PATH, 0660);
  else
    chmod (SOCKET_PATH, 0600);

  g_signal_connect (bridge.service, "incoming", G_CALLBACK (on_incoming), NULL);
  g_unix_signal_add (SIGTERM, on_quit, loop);
  g_unix_signal_add (SIGINT, on_quit, loop);
  g_main_loop_run (loop);

  if (bridge.running)
    hal_stop ();
  hal_disconnect ();
  gbinder_servicemanager_unref (bridge.sm);
  unlink (SOCKET_PATH);
  return 0;
}
