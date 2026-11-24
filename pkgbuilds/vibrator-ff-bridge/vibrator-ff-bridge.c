/*
 * vibrator-ff-bridge: force-feedback input device (uinput) -> Android vibrator
 *
 * feedbackd drives vibration motors through the kernel force-feedback API (FF_RUMBLE,
 * FF_PERIODIC). This daemon creates such a device and forwards the effects, in this order of
 * preference, to the force-feedback device of the vendor haptics driver (Awinic "aw-haptic-hv",
 * which feedbackd cannot use directly: it only plays FF_CONSTANT and waveform ids), to the
 * LED-class vibrator of the vendor kernel (/sys/class/leds/vibrator) or to the AIDL HAL
 * android.hardware.vibrator.IVibrator/default over binder.
 * A udev rule tags the device with FEEDBACKD_TYPE=vibra, so feedbackd stays unchanged.
 *
 * Transaction codes follow the frozen AIDL definition (android.hardware.vibrator version 2).
 *
 * SPDX-License-Identifier: MIT
 */

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <linux/uinput.h>

#include <glib.h>
#include <glib-unix.h>
#include <gbinder.h>

#define DEVICE_NAME     "vibrator-ff-bridge"
#define KERNEL_FF_NAME  "aw-haptic-hv"
#define SYSFS_DIR       "/sys/class/leds/vibrator"
#define BINDER_DEVICE   "/dev/binder"
#define VIB_IFACE       "android.hardware.vibrator.IVibrator"
#define VIB_INSTANCE    VIB_IFACE "/default"
#define MAX_EFFECTS     16
/* Vendor FF device: shortest pulse that is felt, and the strength a weak effect starts at */
#define TAP_MIN_MS      20
#define AMPLITUDE_FLOOR 0.4

/* IVibrator */
enum {
  VIB_GET_CAPABILITIES = 1,
  VIB_OFF = 2,
  VIB_ON = 3,
  VIB_SET_AMPLITUDE = 6,
};
#define CAP_AMPLITUDE_CONTROL 4

typedef struct {
  int                    fd;
  struct ff_effect       effects[MAX_EFFECTS];
  gboolean               used[MAX_EFFECTS];
  guint                  gain;          /* 0..0xffff */
  int                    kfd;           /* kernel FF device of the vendor driver, or -1 */
  gint16                 kid;           /* its uploaded effect, or -1 */
  gboolean               sysfs;
  gint64                 tap_end;       /* monotonic µs until a lengthened tap may run */
  GBinderServiceManager *sm;
  GBinderRemoteObject   *remote;
  GBinderClient         *client;
  gulong                 death_id;
  gint32                 caps;
  guint                  delay_id;
} Bridge;

static Bridge bridge = { .fd = -1, .gain = 0xffff, .kfd = -1, .kid = -1 };

/* Find and open the vendor haptics FF device (KERNEL_FF_NAME) among /dev/input/event*; -1 if absent */
static int
kernel_ff_open (void)
{
  g_autoptr (GDir) dir = g_dir_open ("/dev/input", 0, NULL);
  const char *entry;

  while (dir && (entry = g_dir_read_name (dir))) {
    g_autofree char *path = NULL;
    char name[64] = "";
    int fd;

    if (!g_str_has_prefix (entry, "event"))
      continue;
    path = g_build_filename ("/dev/input", entry, NULL);
    fd = open (path, O_RDWR | O_CLOEXEC);
    if (fd < 0)
      continue;
    if (ioctl (fd, EVIOCGNAME (sizeof name - 1), name) >= 0 && g_str_equal (name, KERNEL_FF_NAME)) {
      g_message ("Backend: %s (%s)", KERNEL_FF_NAME, path);
      return fd;
    }
    close (fd);
  }
  return -1;
}

static void
kernel_ff_write (guint16 code, gint32 value)
{
  struct input_event ev = { .type = EV_FF, .code = code, .value = value };

  if (write (bridge.kfd, &ev, sizeof ev) != sizeof ev)
    g_warning ("%s: %s", KERNEL_FF_NAME, g_strerror (errno));
}

/* Play ms milliseconds at amplitude 0..1 on the vendor FF device */
static void
kernel_ff_on (guint ms, double amplitude)
{
  /* FF_CONSTANT is a looped RAM waveform for replay.length ms. The driver ignores the
   * magnitude of the effect; the strength is the global gain, which it caps at 0x7fff. */
  struct ff_effect e = { .type = FF_CONSTANT, .id = bridge.kid };

  /* The linear motor (resonance about 168 Hz) needs a few cycles at full drive to be felt:
   * feedbackd's key press (7 ms at 20 %) and even a call (50 %) were not noticed. A pulse
   * shorter than TAP_MIN_MS becomes a TAP_MIN_MS tap at full strength (picked by feel on the
   * phone), longer ones start at AMPLITUDE_FLOOR. */
  if (ms < TAP_MIN_MS) {
    ms = TAP_MIN_MS;
    amplitude = 1.0;
    bridge.tap_end = g_get_monotonic_time () + TAP_MIN_MS * G_TIME_SPAN_MILLISECOND;
  } else {
    bridge.tap_end = 0;
    amplitude = AMPLITUDE_FLOOR + (1.0 - AMPLITUDE_FLOOR) * CLAMP (amplitude, 0.0, 1.0);
  }
  e.replay.length = ms;
  kernel_ff_write (FF_GAIN, CLAMP (amplitude, 0.01, 1.0) * 0x7fff);
  if (ioctl (bridge.kfd, EVIOCSFF, &e) < 0) {
    g_warning ("%s: cannot upload effect: %s", KERNEL_FF_NAME, g_strerror (errno));
    return;
  }
  bridge.kid = e.id;
  kernel_ff_write (bridge.kid, 1);
}

/* Write value to an attribute of the LED-class vibrator */
static gboolean
sysfs_write (const char *attr, const char *value)
{
  g_autofree char *path = g_build_filename (SYSFS_DIR, attr, NULL);
  g_autoptr (GError) err = NULL;

  if (!g_file_set_contents_full (path, value, -1, G_FILE_SET_CONTENTS_NONE, 0644, &err)) {
    g_warning ("%s", err->message);
    return FALSE;
  }
  return TRUE;
}

/* Release the HAL references; the next vibrate_on() reconnects */
static void
hal_disconnect (void)
{
  if (bridge.remote && bridge.death_id)
    gbinder_remote_object_remove_handler (bridge.remote, bridge.death_id);
  bridge.death_id = 0;
  g_clear_pointer (&bridge.client, gbinder_client_unref);
  g_clear_pointer (&bridge.remote, gbinder_remote_object_unref);
}

static void
on_hal_died (GBinderRemoteObject *obj, void *user_data)
{
  g_warning ("Vibrator HAL died");
  hal_disconnect ();
}

/* AIDL call; returns FALSE on transport error or exception. Optionally reads an int result. */
static gboolean
call (guint code, GBinderLocalRequest *req, gint32 *result)
{
  int status = 0;
  gint32 ex = -1;
  GBinderRemoteReply *reply;
  GBinderReader reader;

  reply = gbinder_client_transact_sync_reply (bridge.client, code, req, &status);
  if (reply) {
    gbinder_remote_reply_init_reader (reply, &reader);
    if (gbinder_reader_read_int32 (&reader, &ex) && !ex && result)
      gbinder_reader_read_int32 (&reader, result);
    gbinder_remote_reply_unref (reply);
  }
  if (status || ex) {
    g_warning ("IVibrator transaction %u failed (status %d, exception %d)", code, status, ex);
    return FALSE;
  }
  return TRUE;
}

/* Look up IVibrator/default (lazily, the container may start later) and query its capabilities */
static gboolean
hal_connect (void)
{
  int status = 0;

  if (bridge.client)
    return TRUE;
  if (!bridge.sm && !(bridge.sm = gbinder_servicemanager_new (BINDER_DEVICE)))
    return FALSE;
  bridge.remote = gbinder_servicemanager_get_service_sync (bridge.sm, VIB_INSTANCE, &status);
  if (!bridge.remote)
    return FALSE;
  gbinder_remote_object_ref (bridge.remote);
  bridge.client = gbinder_client_new (bridge.remote, VIB_IFACE);
  bridge.death_id = gbinder_remote_object_add_death_handler (bridge.remote, on_hal_died, NULL);
  bridge.caps = 0;
  call (VIB_GET_CAPABILITIES, NULL, &bridge.caps);
  g_message ("Vibrator HAL connected, capabilities 0x%x", bridge.caps);
  return TRUE;
}

/* Stop vibrating on whichever backend is active */
static void
vibrate_off (void)
{
  if (bridge.kfd >= 0) {
    /* feedbackd stops a key press after its own 7 ms, which would cut the lengthened tap
     * short again; the effect ends by itself after replay.length */
    if (bridge.kid >= 0 && g_get_monotonic_time () >= bridge.tap_end)
      kernel_ff_write (bridge.kid, 0);
  } else if (bridge.sysfs)
    sysfs_write ("activate", "0");
  else if (bridge.client)
    call (VIB_OFF, NULL, NULL);
}

/* Vibrate for ms milliseconds at amplitude 0..1 on the active backend */
static void
vibrate_on (guint ms, double amplitude)
{
  if (!ms)
    return;
  if (bridge.kfd >= 0) {
    kernel_ff_on (ms, amplitude);
    return;
  }
  if (bridge.sysfs) {
    g_autofree char *d = g_strdup_printf ("%u", ms);
    /* The LED-class vibrator of the vendor kernel has no strength control */
    if (sysfs_write ("duration", d))
      sysfs_write ("activate", "1");
    return;
  }
  if (!hal_connect ())
    return;
  if (bridge.caps & CAP_AMPLITUDE_CONTROL) {
    GBinderLocalRequest *req = gbinder_client_new_request (bridge.client);
    GBinderWriter w;

    gbinder_local_request_init_writer (req, &w);
    gbinder_writer_append_float (&w, CLAMP (amplitude, 0.01, 1.0));
    call (VIB_SET_AMPLITUDE, req, NULL);
    gbinder_local_request_unref (req);
  }
  {
    GBinderLocalRequest *req = gbinder_client_new_request (bridge.client);
    GBinderWriter w;

    gbinder_local_request_init_writer (req, &w);
    gbinder_writer_append_int32 (&w, ms);
    /* IVibratorCallback: none, the HAL stops by itself after timeoutMs */
    gbinder_writer_append_local_object (&w, NULL);
    call (VIB_ON, req, NULL);
    gbinder_local_request_unref (req);
  }
}

/* Effect strength 0..1, scaled by the gain set via FF_GAIN */
static double
effect_amplitude (const struct ff_effect *e)
{
  double m = 1.0;

  if (e->type == FF_RUMBLE)
    m = MAX (e->u.rumble.strong_magnitude, e->u.rumble.weak_magnitude) / 65535.0;
  else if (e->type == FF_PERIODIC)
    m = ABS (e->u.periodic.magnitude) / 32767.0;
  return m * bridge.gain / 65535.0;
}

typedef struct { guint ms; double amplitude; } Pending;

static gboolean
on_delay_done (gpointer data)
{
  Pending *p = data;

  bridge.delay_id = 0;
  vibrate_on (p->ms, p->amplitude);
  return G_SOURCE_REMOVE;
}

/* Start (honouring replay.delay) or stop an uploaded effect */
static void
play (int id, gboolean start)
{
  const struct ff_effect *e;
  Pending *p;

  g_clear_handle_id (&bridge.delay_id, g_source_remove);
  if (!start || id < 0 || id >= MAX_EFFECTS || !bridge.used[id]) {
    vibrate_off ();
    return;
  }
  e = &bridge.effects[id];
  /* Patterns (FF_PERIODIC) become one continuous pulse of the effect length */
  if (!e->replay.delay) {
    vibrate_on (e->replay.length, effect_amplitude (e));
    return;
  }
  p = g_new (Pending, 1);
  p->ms = e->replay.length;
  p->amplitude = effect_amplitude (e);
  bridge.delay_id = g_timeout_add_full (G_PRIORITY_DEFAULT, e->replay.delay, on_delay_done,
                                        p, g_free);
}

/* uinput FF upload request: store rumble/periodic effects, reject the rest */
static void
handle_upload (int request_id)
{
  struct uinput_ff_upload up = { .request_id = request_id };

  if (ioctl (bridge.fd, UI_BEGIN_FF_UPLOAD, &up) < 0)
    return;
  if (up.effect.id >= 0 && up.effect.id < MAX_EFFECTS &&
      (up.effect.type == FF_RUMBLE || up.effect.type == FF_PERIODIC)) {
    bridge.effects[up.effect.id] = up.effect;
    bridge.used[up.effect.id] = TRUE;
    up.retval = 0;
  } else {
    up.retval = -EINVAL;
  }
  ioctl (bridge.fd, UI_END_FF_UPLOAD, &up);
}

/* uinput FF erase request: forget the effect */
static void
handle_erase (int request_id)
{
  struct uinput_ff_erase er = { .request_id = request_id };

  if (ioctl (bridge.fd, UI_BEGIN_FF_ERASE, &er) < 0)
    return;
  if (er.effect_id < MAX_EFFECTS)
    bridge.used[er.effect_id] = FALSE;
  er.retval = 0;
  ioctl (bridge.fd, UI_END_FF_ERASE, &er);
}

/* Set when the uinput device fails; main then exits with an error so systemd restarts us */
static gboolean uinput_failed;

/* Events written to our uinput device by feedbackd: uploads, erases, gain, play/stop.
 * data is the main loop, ended if the device fails. */
static gboolean
on_uinput (int fd, GIOCondition cond, gpointer data)
{
  struct input_event ev;

  if (cond & (G_IO_HUP | G_IO_ERR)) {
    /* Otherwise poll reports the fd as ready again right away: 100 % CPU in a busy loop */
    g_warning ("uinput device failed, exiting");
    uinput_failed = TRUE;
    g_main_loop_quit (data);
    return G_SOURCE_REMOVE;
  }
  while (read (fd, &ev, sizeof ev) == sizeof ev) {
    if (ev.type == EV_UINPUT && ev.code == UI_FF_UPLOAD)
      handle_upload (ev.value);
    else if (ev.type == EV_UINPUT && ev.code == UI_FF_ERASE)
      handle_erase (ev.value);
    else if (ev.type == EV_FF && ev.code == FF_GAIN)
      bridge.gain = CLAMP (ev.value, 0, 0xffff);
    else if (ev.type == EV_FF)
      play (ev.code, ev.value > 0);
  }
  return G_SOURCE_CONTINUE;
}

/* Create the virtual FF input device that feedbackd talks to */
static gboolean
uinput_create (void)
{
  static const int ff_bits[] = { FF_RUMBLE, FF_PERIODIC, FF_SINE, FF_SQUARE, FF_TRIANGLE,
                                 FF_SAW_UP, FF_SAW_DOWN, FF_GAIN };
  struct uinput_setup us = {
    .id = { .bustype = BUS_VIRTUAL },
    .name = DEVICE_NAME,
    .ff_effects_max = MAX_EFFECTS,
  };

  bridge.fd = open ("/dev/uinput", O_RDWR | O_NONBLOCK | O_CLOEXEC);
  if (bridge.fd < 0) {
    g_printerr ("Cannot open /dev/uinput: %s\n", g_strerror (errno));
    return FALSE;
  }
  if (ioctl (bridge.fd, UI_SET_EVBIT, EV_FF) < 0)
    goto fail;
  for (gsize i = 0; i < G_N_ELEMENTS (ff_bits); i++)
    if (ioctl (bridge.fd, UI_SET_FFBIT, ff_bits[i]) < 0)
      goto fail;
  if (ioctl (bridge.fd, UI_DEV_SETUP, &us) < 0 || ioctl (bridge.fd, UI_DEV_CREATE) < 0)
    goto fail;
  return TRUE;

 fail:
  g_printerr ("Cannot set up uinput device: %s\n", g_strerror (errno));
  return FALSE;
}

/* The vendor module can load after this service started (late firmware, module order). Keep
 * looking for its device for a minute and switch over once it appears. */
#define KERNEL_FF_RETRIES 30

static gboolean
kernel_ff_retry (gpointer data)
{
  static guint tries;
  int fd = kernel_ff_open ();

  if (fd < 0)
    return ++tries < KERNEL_FF_RETRIES ? G_SOURCE_CONTINUE : G_SOURCE_REMOVE;
  vibrate_off ();
  hal_disconnect ();
  bridge.sysfs = FALSE;
  bridge.kfd = fd;
  return G_SOURCE_REMOVE;
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
  g_autofree char *activate = g_build_filename (SYSFS_DIR, "activate", NULL);

  bridge.kfd = kernel_ff_open ();
  if (bridge.kfd < 0) {
    bridge.sysfs = g_file_test (activate, G_FILE_TEST_EXISTS);
    g_message ("Backend: %s", bridge.sysfs ? SYSFS_DIR : VIB_INSTANCE);
    g_timeout_add_seconds (2, kernel_ff_retry, NULL);
  }
  if (!uinput_create ())
    return 1;

  g_unix_fd_add (bridge.fd, G_IO_IN | G_IO_HUP | G_IO_ERR, on_uinput, loop);
  g_unix_signal_add (SIGTERM, on_quit, loop);
  g_unix_signal_add (SIGINT, on_quit, loop);
  g_main_loop_run (loop);

  vibrate_off ();
  if (bridge.kfd >= 0)
    close (bridge.kfd);
  ioctl (bridge.fd, UI_DEV_DESTROY);
  close (bridge.fd);
  hal_disconnect ();
  g_clear_pointer (&bridge.sm, gbinder_servicemanager_unref);
  return uinput_failed ? 1 : 0;
}
