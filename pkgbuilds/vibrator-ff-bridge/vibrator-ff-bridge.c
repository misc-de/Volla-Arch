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
 * Android apps (Waydroid container): the bridge serves the same interface (IVibrator version 2)
 * to the container's vibrator service on /dev/anbox-binder, registered whenever the container's
 * service manager shows up (the node opens to users other than root only once the container has
 * started; inotify tells). Android's vibrations go to the same backend as feedbackd's: on() and
 * off(), the strength of setAmplitude() and the predefined click effects. Android times the
 * vibrations itself (no completion callbacks) and makes up the effects it is not offered.
 *
 * SPDX-License-Identifier: MIT
 */

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <linux/uinput.h>

#include <glib.h>
#include <glib-unix.h>
#include <gio/gio.h>
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
  VIB_PERFORM = 4,
  VIB_GET_SUPPORTED_EFFECTS = 5,
  VIB_SET_AMPLITUDE = 6,
};
#define CAP_AMPLITUDE_CONTROL 4

/* What Android in the Waydroid container gets: IVibrator version 2 and the hash of that frozen
 * version */
#define ANDROID_DEVICE  "/dev/anbox-binder"
#define ANDROID_VERSION 2
#define ANDROID_HASH    "ea8742d6993e1a82917da38b9938e537aa7fcb54"
/* Transactions every AIDL interface answers */
#define TX_GET_INTERFACE_VERSION 0x00ffffff
#define TX_GET_INTERFACE_HASH    0x00fffffe
/* AIDL exception codes */
#define EX_NONE                  0
#define EX_ILLEGAL_ARGUMENT      (-3)
#define EX_UNSUPPORTED_OPERATION (-7)
/* Effect values (android.hardware.vibrator.Effect) Android gets as short pulses */
enum {
  EFFECT_CLICK = 0,
  EFFECT_DOUBLE_CLICK = 1,
  EFFECT_TICK = 2,
  EFFECT_HEAVY_CLICK = 5,
};
/* Between the starts of the two clicks of a double click */
#define DOUBLE_CLICK_GAP_MS 100

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

/* Android in the Waydroid container: its vibrator service drives the same motor */
typedef struct {
  GBinderServiceManager *sm;
  GBinderLocalObject    *service;
  gulong                 presence_id;
  char                  *node;      /* name of the device node in its directory */
  double                 amplitude; /* of setAmplitude(), 0..1 */
  gint64                 end;       /* monotonic µs when Android's on() ends, 0: none */
  double                 click;     /* strength of the pending second click */
  guint                  second_id; /* the second click of a double click */
} Android;

static Android android = { .amplitude = 1.0 };

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

/* ---- IVibrator for Android ---- */

/* The pulses Android gets for its predefined effects, in ms, and the strength per EffectStrength
 * (LIGHT, MEDIUM, STRONG) */
static const struct { gint32 effect; guint ms; } android_effects[] = {
  { EFFECT_TICK, 10 },
  { EFFECT_CLICK, 20 },
  { EFFECT_HEAVY_CLICK, 30 },
  { EFFECT_DOUBLE_CLICK, 20 },
};
static const double android_strengths[] = { 0.5, 0.75, 1.0 };

static void
android_off (void)
{
  g_clear_handle_id (&android.second_id, g_source_remove);
  android.end = 0;
  vibrate_off ();
}

/* on(timeoutMs, callback), with timeoutMs > 0 */
static void
android_on (gint32 ms)
{
  g_clear_handle_id (&android.second_id, g_source_remove);
  android.end = g_get_monotonic_time () + (gint64) ms * G_TIME_SPAN_MILLISECOND;
  vibrate_on (ms, android.amplitude);
}

/* Android sets the strength right after on(): what is left of it plays at the new strength */
static void
android_set_amplitude (float amplitude)
{
  gint64 left = android.end - g_get_monotonic_time ();

  android.amplitude = CLAMP (amplitude, 0.0, 1.0);
  if (left >= G_TIME_SPAN_MILLISECOND)
    vibrate_on (left / G_TIME_SPAN_MILLISECOND, android.amplitude);
}

static gboolean
on_second_click (gpointer data)
{
  android.second_id = 0;
  vibrate_on (GPOINTER_TO_UINT (data), android.click);
  return G_SOURCE_REMOVE;
}

/* perform(effect, strength, callback): the pulse's length in ms, or -1 if not offered */
static gint32
android_perform (gint32 effect, gint32 strength)
{
  for (gsize i = 0; i < G_N_ELEMENTS (android_effects); i++) {
    guint ms = android_effects[i].ms;

    if (android_effects[i].effect != effect)
      continue;
    g_clear_handle_id (&android.second_id, g_source_remove);
    android.end = 0;
    android.click = android_strengths[strength];
    vibrate_on (ms, android.click);
    if (effect != EFFECT_DOUBLE_CLICK)
      return ms;
    android.second_id = g_timeout_add (DOUBLE_CLICK_GAP_MS, on_second_click, GUINT_TO_POINTER (ms));
    return DOUBLE_CLICK_GAP_MS + ms;
  }
  return -1;
}

static void
write_exception (GBinderWriter *w, gint32 code)
{
  gbinder_writer_append_int32 (w, code);
  if (code != EX_NONE) {
    gbinder_writer_append_string16 (w, code == EX_ILLEGAL_ARGUMENT ? "Bad argument" : "Not supported");
    gbinder_writer_append_int32 (w, 0); /* no remote stack trace */
  }
}

static GBinderLocalReply *
on_android_call (GBinderLocalObject *obj, GBinderRemoteRequest *req, guint code, guint flags,
                 int *status, void *user_data)
{
  GBinderReader reader;
  GBinderWriter w;
  GBinderLocalReply *reply = gbinder_local_object_new_reply (obj);
  gint32 value = 0, strength = -1, ms;
  float amplitude;

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
  case VIB_GET_CAPABILITIES:
    write_exception (&w, EX_NONE);
    gbinder_writer_append_int32 (&w, CAP_AMPLITUDE_CONTROL);
    break;
  case VIB_OFF:
    android_off ();
    write_exception (&w, EX_NONE);
    break;
  case VIB_ON:
    if (gbinder_reader_read_int32 (&reader, &value) && value > 0) {
      android_on (value);
      write_exception (&w, EX_NONE);
    } else {
      write_exception (&w, EX_ILLEGAL_ARGUMENT);
    }
    break;
  case VIB_PERFORM:
    /* EffectStrength is a byte enum, sent as an int32 */
    if (!gbinder_reader_read_int32 (&reader, &value) ||
        !gbinder_reader_read_int32 (&reader, &strength) ||
        strength < 0 || strength >= (gint32) G_N_ELEMENTS (android_strengths)) {
      write_exception (&w, EX_ILLEGAL_ARGUMENT);
    } else if ((ms = android_perform (value, strength)) < 0) {
      write_exception (&w, EX_UNSUPPORTED_OPERATION);
    } else {
      write_exception (&w, EX_NONE);
      gbinder_writer_append_int32 (&w, ms);
    }
    break;
  case VIB_GET_SUPPORTED_EFFECTS:
    write_exception (&w, EX_NONE);
    gbinder_writer_append_int32 (&w, G_N_ELEMENTS (android_effects));
    for (gsize i = 0; i < G_N_ELEMENTS (android_effects); i++)
      gbinder_writer_append_int32 (&w, android_effects[i].effect);
    break;
  case VIB_SET_AMPLITUDE:
    if (gbinder_reader_read_float (&reader, &amplitude) && amplitude > 0 && amplitude <= 1) {
      android_set_amplitude (amplitude);
      write_exception (&w, EX_NONE);
    } else {
      write_exception (&w, EX_ILLEGAL_ARGUMENT);
    }
    break;
  default:
    /* External control, compositions, always-on effects, frequencies, PWLE: not offered */
    write_exception (&w, EX_UNSUPPORTED_OPERATION);
    break;
  }
  return reply;
}

static void
on_android_added (GBinderServiceManager *sm, int status, void *user_data)
{
  if (status)
    g_warning ("Cannot serve the vibrator to Android (status %d)", status);
  else
    g_message ("Serving the vibrator to Android");
}

static void
android_register (void)
{
  if (gbinder_servicemanager_is_present (android.sm))
    gbinder_servicemanager_add_service (android.sm, VIB_INSTANCE, android.service,
                                        on_android_added, NULL);
}

/* The container's service manager comes with every start of the container; when it goes, so
 * does whatever Android was playing */
static void
on_android_presence (GBinderServiceManager *sm, void *user_data)
{
  if (gbinder_servicemanager_is_present (sm))
    android_register ();
  else
    android_off ();
}

/* Serve IVibrator on the container's binder. Idempotent: called at start and whenever the device
 * node changes, until it can be opened */
static void
android_start (void)
{
  if (android.sm || access (ANDROID_DEVICE, R_OK | W_OK) != 0)
    return;
  android.sm = gbinder_servicemanager_new (ANDROID_DEVICE);
  if (!android.sm)
    return;
  android.service = gbinder_servicemanager_new_local_object (android.sm, VIB_IFACE,
                                                             on_android_call, NULL);
  gbinder_local_object_set_stability (android.service, GBINDER_STABILITY_VINTF);
  android.presence_id = gbinder_servicemanager_add_presence_handler (android.sm,
                                                                     on_android_presence, NULL);
  android_register ();
}

static void
on_android_node (GFileMonitor *monitor, GFile *file, GFile *other, GFileMonitorEvent event,
                 gpointer user_data)
{
  g_autofree char *base = g_file_get_basename (file);

  if (event != G_FILE_MONITOR_EVENT_DELETED && g_strcmp0 (base, android.node) == 0)
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

  g_free (android.node);
  android.node = g_file_get_basename (node);
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
  g_autofree char *activate = g_build_filename (SYSFS_DIR, "activate", NULL);
  g_autoptr (GFileMonitor) monitor = NULL;

  /* First: a signal during the set-up must end the loop, not the process */
  g_unix_signal_add (SIGTERM, on_quit, loop);
  g_unix_signal_add (SIGINT, on_quit, loop);
  bridge.kfd = kernel_ff_open ();
  if (bridge.kfd < 0) {
    bridge.sysfs = g_file_test (activate, G_FILE_TEST_EXISTS);
    g_message ("Backend: %s", bridge.sysfs ? SYSFS_DIR : VIB_INSTANCE);
    g_timeout_add_seconds (2, kernel_ff_retry, NULL);
  }
  if (!uinput_create ())
    return 1;

  g_unix_fd_add (bridge.fd, G_IO_IN | G_IO_HUP | G_IO_ERR, on_uinput, loop);
  monitor = android_watch ();
  android_start ();
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
