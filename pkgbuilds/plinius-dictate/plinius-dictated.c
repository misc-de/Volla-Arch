/*
 * plinius-dictated: speech to text for the on-screen keyboard.
 *
 * A session D-Bus service (org.plinius.Dictate1). The keyboard calls Start() when the user
 * taps the microphone key and Stop() when they tap it again. In between the service records
 * the microphone through PipeWire (pw-record), cuts the audio into utterances with a voice
 * activity detector (Silero VAD) and recognizes each utterance with an offline model
 * (NeMo Parakeet TDT, through sherpa-onnx). Every recognized utterance goes to the caller
 * as a Text signal; the keyboard commits it to the focused text field.
 *
 * Feedback for the microphone key: Activity tells what the service does (loading the model,
 * listening, hearing speech, recognizing), Level the loudness of the microphone about
 * 15 times a second while it is open.
 *
 * Privacy: the microphone is only open between Start() and Stop() (or until the caller
 * leaves the bus, or after a long silence). Audio is never written anywhere. Signals are
 * sent to the caller only, not broadcast on the bus.
 *
 * Memory: the recognizer needs about 1 GB. It is loaded on the first Start(), while the user
 * already speaks (audio is buffered meanwhile), and the whole process exits after
 * `keep-model` seconds without dictation, which frees it again. D-Bus activation starts the
 * service on the next call.
 *
 * SPDX-License-Identifier: MIT
 */

#include <gio/gio.h>
#include <glib-unix.h>
#include <math.h>
#include <signal.h>
#include <string.h>
#include <sherpa-onnx/c-api/c-api.h>

#define BUS_NAME "org.plinius.Dictate1"
#define OBJECT_PATH "/org/plinius/Dictate1"
#define IFACE "org.plinius.Dictate1"
#define SETTINGS_SCHEMA "org.plinius.dictate"

#define DATA_DIR "/usr/share/plinius-dictate"
#define MODEL_DIR DATA_DIR "/parakeet-tdt-0.6b-v3-int8"
#define VAD_MODEL DATA_DIR "/silero_vad_v5.onnx"

#define SAMPLE_RATE 16000
/* The phone's microphone: the source audio-hal-bridge makes of the HAL's input */
#define MICROPHONE "hal_input"
/* Silero VAD works on windows of 512 samples (32 ms) at 16 kHz */
#define VAD_WINDOW 512
/* Stop listening by itself after this much silence (seconds) */
#define SILENCE_STOP_S 10
/* Exit after this many seconds without dictation if the setting is missing */
#define DEFAULT_KEEP_MODEL_S 300
/* Level signals while the microphone is open: about 15 per second */
#define LEVEL_INTERVAL_US (G_USEC_PER_SEC / 15)
/* Loudness from this (silence, dBFS) up to LEVEL_TOP_DB maps to a level of 0 to 1 */
#define LEVEL_FLOOR_DB -60.0
#define LEVEL_TOP_DB -12.0

static const char introspection_xml[] =
  "<node>"
  "  <interface name='" IFACE "'>"
  "    <method name='Start'/>"
  "    <method name='Stop'/>"
  "    <property name='State' type='s' access='read'/>"
  "    <signal name='Text'><arg name='text' type='s'/></signal>"
  "    <signal name='StateChanged'><arg name='state' type='s'/></signal>"
  /* idle, loading, listening, speech or recognizing */
  "    <signal name='Activity'><arg name='activity' type='s'/></signal>"
  /* Loudness of the microphone from 0 to 1 */
  "    <signal name='Level'><arg name='level' type='d'/></signal>"
  "  </interface>"
  "</node>";

typedef struct {
  GMainLoop *loop;
  GDBusConnection *bus;
  GDBusNodeInfo *introspection;
  GSettings *settings;              /* NULL if the schema is not installed */

  /* Session of the current caller; NULL when idle */
  char *client;                     /* unique bus name of the caller */
  guint client_watch;
  GSubprocess *recorder;            /* pw-record writing raw float samples to stdout */
  GCancellable *read_cancel;
  guint8 readbuf[16384];
  float window[VAD_WINDOW];         /* samples not yet given to the VAD */
  guint8 partial[sizeof (float)];   /* bytes of a sample split between two reads */
  gsize n_window, n_partial;
  gint64 last_voice;                /* monotonic time of the last detected speech */
  guint silence_check;

  const SherpaOnnxVoiceActivityDetector *vad;

  /* Recognition runs in one worker thread; only that thread touches the recognizer */
  GThreadPool *worker;
  const SherpaOnnxOfflineRecognizer *recognizer;
  guint pending;                    /* jobs queued or running (main thread) */
  guint utterances;                 /* of these, utterances to recognize */
  gboolean loaded;                  /* the worker tried to load the model (main thread) */
  gboolean speech;                  /* the VAD hears speech right now */
  double level_sum;                 /* squared samples since the last Level signal */
  gsize level_n;
  gint64 level_time;                /* monotonic time of the last Level signal */
  const char *activity;
  guint session;                    /* increases with every Start(); stale results are dropped */

  guint exit_timeout;
  const char *state;
} Dictate;

typedef struct {
  Dictate *d;
  guint session;
  float *samples;                   /* NULL: only load the model */
  gint32 n;
} Job;

typedef struct {
  Dictate *d;
  guint session;
  gboolean utterance;               /* FALSE: the job only loaded the model */
  char *text;                       /* NULL on failure */
} JobResult;

static void stop_session (Dictate *d);

/* Send a signal to the current caller only */
static void
emit_to_client (Dictate *d, const char *signal, GVariant *params)
{
  g_autoptr (GError) err = NULL;

  if (d->client == NULL) {
    g_variant_unref (g_variant_ref_sink (params));
    return;
  }
  if (!g_dbus_connection_emit_signal (d->bus, d->client, OBJECT_PATH, IFACE, signal, params, &err))
    g_warning ("Emitting %s failed: %s", signal, err->message);
}

static void
set_state (Dictate *d, const char *state)
{
  if (g_strcmp0 (d->state, state) == 0)
    return;
  d->state = state;
  g_debug ("State: %s", state);
  emit_to_client (d, "StateChanged", g_variant_new ("(s)", state));
}

/*
 * What the microphone key shows. Recognition runs while the microphone stays open, so
 * hearing speech comes first, then a model still loading or utterances being recognized.
 * Sent before the client is released, so the keyboard sees idle at the end.
 */
static void
update_activity (Dictate *d)
{
  const char *activity;

  if (d->recorder == NULL && d->utterances == 0)
    activity = "idle";
  else if (d->recorder != NULL && d->speech)
    activity = "speech";
  else if (!d->loaded)
    activity = "loading";
  else if (d->utterances > 0)
    activity = "recognizing";
  else
    activity = "listening";

  if (g_strcmp0 (d->activity, activity) == 0)
    return;
  d->activity = activity;
  g_debug ("Activity: %s", activity);
  emit_to_client (d, "Activity", g_variant_new ("(s)", activity));
}

static guint
keep_model_seconds (Dictate *d)
{
  return d->settings ? g_settings_get_uint (d->settings, "keep-model") : DEFAULT_KEEP_MODEL_S;
}

static gboolean
on_exit_timeout (gpointer data)
{
  Dictate *d = data;

  d->exit_timeout = 0;
  g_message ("No dictation for %u s, exiting to free the model", keep_model_seconds (d));
  g_main_loop_quit (d->loop);
  return G_SOURCE_REMOVE;
}

/* Exit (and free the model) once idle for a while; nothing to do while a session runs */
static void
schedule_exit (Dictate *d)
{
  g_clear_handle_id (&d->exit_timeout, g_source_remove);
  if (d->client != NULL || d->pending > 0)
    return;
  d->exit_timeout = g_timeout_add_seconds (keep_model_seconds (d), on_exit_timeout, d);
}

/* ---- recognition (worker thread) ---------------------------------------------------- */

static const SherpaOnnxOfflineRecognizer *
load_recognizer (void)
{
  SherpaOnnxOfflineRecognizerConfig config;
  const SherpaOnnxOfflineRecognizer *rec;
  gint64 start = g_get_monotonic_time ();

  memset (&config, 0, sizeof config);
  config.feat_config.sample_rate = SAMPLE_RATE;
  config.feat_config.feature_dim = 80;
  config.model_config.transducer.encoder = MODEL_DIR "/encoder.int8.onnx";
  config.model_config.transducer.decoder = MODEL_DIR "/decoder.int8.onnx";
  config.model_config.transducer.joiner = MODEL_DIR "/joiner.int8.onnx";
  config.model_config.tokens = MODEL_DIR "/tokens.txt";
  config.model_config.model_type = "nemo_transducer";
  /* Four threads: measured on the four big cores, 0.3 s per sentence */
  config.model_config.num_threads = 4;
  config.model_config.provider = "cpu";
  config.decoding_method = "greedy_search";

  rec = SherpaOnnxCreateOfflineRecognizer (&config);
  if (rec == NULL)
    g_warning ("Loading the recognizer from " MODEL_DIR " failed");
  else
    g_message ("Recognizer loaded in %.1f s", (g_get_monotonic_time () - start) / 1e6);
  return rec;
}

static gboolean
on_job_done (gpointer data)
{
  JobResult *r = data;
  Dictate *d = r->d;

  d->pending--;
  /* The worker loads the model with its first job, whatever that job was */
  d->loaded = TRUE;
  if (r->utterance)
    d->utterances--;
  /* Results of an older session (the user stopped and started again) are dropped */
  if (r->text != NULL && r->session == d->session && d->client != NULL)
    emit_to_client (d, "Text", g_variant_new ("(s)", r->text));
  update_activity (d);
  /* Session stopped and its last utterance delivered: release the client */
  if (d->recorder == NULL && d->pending == 0) {
    g_clear_handle_id (&d->client_watch, g_bus_unwatch_name);
    g_clear_pointer (&d->client, g_free);
    schedule_exit (d);
  }

  g_free (r->text);
  g_free (r);
  return G_SOURCE_REMOVE;
}

static void
run_job (gpointer data, gpointer user_data)
{
  Job *job = data;
  Dictate *d = job->d;
  JobResult *r = g_new0 (JobResult, 1);

  r->d = d;
  r->session = job->session;
  r->utterance = job->samples != NULL;

  if (d->recognizer == NULL)
    d->recognizer = load_recognizer ();

  if (d->recognizer != NULL && job->samples != NULL) {
    const SherpaOnnxOfflineStream *stream = SherpaOnnxCreateOfflineStream (d->recognizer);
    const SherpaOnnxOfflineRecognizerResult *res;
    gint64 start = g_get_monotonic_time ();

    SherpaOnnxAcceptWaveformOffline (stream, SAMPLE_RATE, job->samples, job->n);
    SherpaOnnxDecodeOfflineStream (d->recognizer, stream);
    res = SherpaOnnxGetOfflineStreamResult (stream);
    if (res != NULL && res->text != NULL) {
      r->text = g_strstrip (g_strdup (res->text));
      if (*r->text == '\0')
        g_clear_pointer (&r->text, g_free);
    }
    g_debug ("Utterance of %.1f s recognized in %.2f s", (double) job->n / SAMPLE_RATE,
             (g_get_monotonic_time () - start) / 1e6);
    SherpaOnnxDestroyOfflineRecognizerResult (res);
    SherpaOnnxDestroyOfflineStream (stream);
  }

  g_free (job->samples);
  g_free (job);
  g_idle_add (on_job_done, r);
}

static void
queue_job (Dictate *d, const float *samples, gint32 n)
{
  Job *job = g_new0 (Job, 1);

  job->d = d;
  job->session = d->session;
  if (samples != NULL) {
    job->samples = g_memdup2 (samples, n * sizeof (float));
    job->n = n;
    d->utterances++;
  }
  d->pending++;
  g_thread_pool_push (d->worker, job, NULL);
}

/* ---- voice activity detection (main thread) ----------------------------------------- */

/* Hand every finished utterance to the worker */
static void
drain_vad (Dictate *d)
{
  while (!SherpaOnnxVoiceActivityDetectorEmpty (d->vad)) {
    const SherpaOnnxSpeechSegment *seg = SherpaOnnxVoiceActivityDetectorFront (d->vad);

    if (seg != NULL) {
      queue_job (d, seg->samples, seg->n);
      SherpaOnnxDestroySpeechSegment (seg);
    }
    SherpaOnnxVoiceActivityDetectorPop (d->vad);
  }
}

/* Loudness of the microphone for the key, as RMS in dBFS mapped onto 0 to 1 */
static void
update_level (Dictate *d, const float *samples, gsize n)
{
  gint64 now = g_get_monotonic_time ();
  double db, level;

  for (gsize i = 0; i < n; i++)
    d->level_sum += (double) samples[i] * samples[i];
  d->level_n += n;
  if (d->level_n == 0 || now - d->level_time < LEVEL_INTERVAL_US)
    return;

  db = 10.0 * log10 (d->level_sum / d->level_n + 1e-12);
  level = CLAMP ((db - LEVEL_FLOOR_DB) / (LEVEL_TOP_DB - LEVEL_FLOOR_DB), 0.0, 1.0);
  d->level_sum = 0.0;
  d->level_n = 0;
  d->level_time = now;
  emit_to_client (d, "Level", g_variant_new ("(d)", level));
}

static void
feed_samples (Dictate *d, const float *samples, gsize n)
{
  update_level (d, samples, n);

  while (n > 0) {
    gsize take = MIN (n, VAD_WINDOW - d->n_window);

    memcpy (d->window + d->n_window, samples, take * sizeof (float));
    d->n_window += take;
    samples += take;
    n -= take;
    if (d->n_window == VAD_WINDOW) {
      SherpaOnnxVoiceActivityDetectorAcceptWaveform (d->vad, d->window, VAD_WINDOW);
      d->n_window = 0;
      d->speech = SherpaOnnxVoiceActivityDetectorDetected (d->vad);
      if (d->speech)
        d->last_voice = g_get_monotonic_time ();
    }
  }
  drain_vad (d);
  update_activity (d);
}

/* ---- recording ------------------------------------------------------------------------ */

static void read_more (Dictate *d);

static void
on_read (GObject *source, GAsyncResult *res, gpointer data)
{
  Dictate *d = data;
  g_autoptr (GError) err = NULL;
  gssize len;
  guint8 *bytes;
  gsize total;
  g_autofree float *samples = NULL;
  gsize n;

  len = g_input_stream_read_finish (G_INPUT_STREAM (source), res, &err);
  if (len < 0) {
    if (!g_error_matches (err, G_IO_ERROR, G_IO_ERROR_CANCELLED))
      g_warning ("Reading the microphone failed: %s", err->message);
    return;
  }
  if (len == 0) {
    /* pw-record ended without Stop(): microphone gone or PipeWire restarted */
    if (d->client != NULL) {
      g_warning ("Recording ended unexpectedly");
      set_state (d, "error");
      stop_session (d);
    }
    return;
  }

  /* Raw float32 samples; a sample may be split between two reads */
  total = d->n_partial + (gsize) len;
  bytes = g_malloc (total);
  memcpy (bytes, d->partial, d->n_partial);
  memcpy (bytes + d->n_partial, d->readbuf, len);
  n = total / sizeof (float);
  d->n_partial = total % sizeof (float);
  memcpy (d->partial, bytes + n * sizeof (float), d->n_partial);
  samples = g_new (float, MAX (n, 1));
  memcpy (samples, bytes, n * sizeof (float));
  g_free (bytes);

  feed_samples (d, samples, n);
  read_more (d);
}

static void
read_more (Dictate *d)
{
  GInputStream *out;

  if (d->recorder == NULL)
    return;
  out = g_subprocess_get_stdout_pipe (d->recorder);
  g_input_stream_read_async (out, d->readbuf, sizeof d->readbuf, G_PRIORITY_HIGH,
                             d->read_cancel, on_read, d);
}

static gboolean
on_silence_check (gpointer data)
{
  Dictate *d = data;

  /* Long silence: the user probably forgot to switch the microphone off */
  if (g_get_monotonic_time () - d->last_voice > SILENCE_STOP_S * G_USEC_PER_SEC &&
      !SherpaOnnxVoiceActivityDetectorDetected (d->vad)) {
    g_message ("No speech for %d s, stopping", SILENCE_STOP_S);
    d->silence_check = 0;
    stop_session (d);
    return G_SOURCE_REMOVE;
  }
  return G_SOURCE_CONTINUE;
}

static gboolean
start_recording (Dictate *d, GError **error)
{
  /*
   * Raw 16 kHz mono float32 to stdout; the role marks it as voice input for the session.
   * Always the phone's own microphone (the HAL's input, also a wired headset's): the default
   * source of a Bluetooth headset or car kit would switch it from music to hands-free for
   * every dictation, and its microphone only opens about a second later, while the user
   * already speaks into the phone that shows the keyboard.
   */
  d->recorder = g_subprocess_new (G_SUBPROCESS_FLAGS_STDOUT_PIPE, error,
                                  "pw-record", "--raw", "--rate", "16000", "--channels", "1",
                                  "--format", "f32", "--media-role", "Communication",
                                  "--latency", "64ms", "--target", MICROPHONE,
                                  "-P", "{ application.name = \"Dictation\" node.description = \"Dictation\" }",
                                  "-", NULL);
  if (d->recorder == NULL)
    return FALSE;
  d->read_cancel = g_cancellable_new ();
  d->n_window = d->n_partial = 0;
  d->speech = FALSE;
  d->level_sum = 0.0;
  d->level_n = 0;
  d->level_time = 0;
  d->last_voice = g_get_monotonic_time ();
  read_more (d);
  d->silence_check = g_timeout_add_seconds (1, on_silence_check, d);
  return TRUE;
}

/* ---- sessions ------------------------------------------------------------------------- */

static void
stop_session (Dictate *d)
{
  if (d->client == NULL)
    return;

  g_clear_handle_id (&d->silence_check, g_source_remove);
  if (d->read_cancel)
    g_cancellable_cancel (d->read_cancel);
  g_clear_object (&d->read_cancel);
  if (d->recorder) {
    g_subprocess_send_signal (d->recorder, SIGTERM);
    g_clear_object (&d->recorder);
  }

  /* The last utterance may still be open: finish it, it is recognized like the others */
  if (d->n_window > 0)
    SherpaOnnxVoiceActivityDetectorAcceptWaveform (d->vad, d->window, d->n_window);
  d->n_window = 0;
  SherpaOnnxVoiceActivityDetectorFlush (d->vad);
  drain_vad (d);
  SherpaOnnxVoiceActivityDetectorReset (d->vad);

  d->speech = FALSE;
  if (g_strcmp0 (d->state, "error") != 0)
    set_state (d, "idle");
  /* Recognizing what is still queued, or idle */
  update_activity (d);
  /* Keep the client name until the queued utterances are delivered: they belong to it.
   * The watch on the name stays, so a vanished client still ends everything. */
  if (d->pending == 0) {
    g_clear_handle_id (&d->client_watch, g_bus_unwatch_name);
    g_clear_pointer (&d->client, g_free);
    schedule_exit (d);
  }
}

static void
on_client_vanished (GDBusConnection *bus, const char *name, gpointer data)
{
  Dictate *d = data;

  g_message ("Client %s left, stopping", name);
  stop_session (d);
  /* Nobody to deliver results to */
  d->session++;
  g_clear_handle_id (&d->client_watch, g_bus_unwatch_name);
  g_clear_pointer (&d->client, g_free);
  schedule_exit (d);
}

static gboolean
models_present (void)
{
  return g_file_test (MODEL_DIR "/encoder.int8.onnx", G_FILE_TEST_EXISTS) &&
         g_file_test (VAD_MODEL, G_FILE_TEST_EXISTS);
}

static void
handle_start (Dictate *d, GDBusMethodInvocation *inv)
{
  const char *sender = g_dbus_method_invocation_get_sender (inv);
  g_autoptr (GError) err = NULL;

  if (d->settings && !g_settings_get_boolean (d->settings, "enabled")) {
    g_dbus_method_invocation_return_dbus_error (inv, IFACE ".Error.Disabled",
                                                "Dictation is switched off in the settings");
    return;
  }
  if (!models_present ()) {
    g_dbus_method_invocation_return_dbus_error (inv, IFACE ".Error.NoModel",
                                                "The speech model is not installed");
    return;
  }

  /* A second caller takes over: the keyboard may have been restarted */
  if (d->client != NULL) {
    stop_session (d);
    g_clear_handle_id (&d->client_watch, g_bus_unwatch_name);
    g_clear_pointer (&d->client, g_free);
  }
  g_clear_handle_id (&d->exit_timeout, g_source_remove);

  d->session++;
  d->client = g_strdup (sender);
  d->client_watch = g_bus_watch_name_on_connection (d->bus, sender, G_BUS_NAME_WATCHER_FLAGS_NONE,
                                                    NULL, on_client_vanished, d, NULL);
  if (!start_recording (d, &err)) {
    g_dbus_method_invocation_return_dbus_error (inv, IFACE ".Error.Microphone", err->message);
    g_clear_handle_id (&d->client_watch, g_bus_unwatch_name);
    g_clear_pointer (&d->client, g_free);
    schedule_exit (d);
    return;
  }
  /* Load the model now, while the user already speaks */
  if (d->recognizer == NULL)
    queue_job (d, NULL, 0);

  d->state = NULL;
  set_state (d, "listening");
  /* A new caller learns the activity from scratch */
  d->activity = NULL;
  update_activity (d);
  g_dbus_method_invocation_return_value (inv, NULL);
}

static void
handle_method (GDBusConnection *bus, const char *sender, const char *path, const char *iface,
               const char *method, GVariant *params, GDBusMethodInvocation *inv, gpointer data)
{
  Dictate *d = data;

  if (g_strcmp0 (method, "Start") == 0) {
    handle_start (d, inv);
  } else if (g_strcmp0 (method, "Stop") == 0) {
    /* Only the caller of Start() may stop its session */
    if (d->client != NULL && g_strcmp0 (sender, d->client) == 0)
      stop_session (d);
    g_dbus_method_invocation_return_value (inv, NULL);
  } else {
    g_dbus_method_invocation_return_error (inv, G_DBUS_ERROR, G_DBUS_ERROR_UNKNOWN_METHOD,
                                           "Unknown method %s", method);
  }
}

static GVariant *
handle_get_property (GDBusConnection *bus, const char *sender, const char *path,
                     const char *iface, const char *prop, GError **error, gpointer data)
{
  Dictate *d = data;

  if (g_strcmp0 (prop, "State") == 0)
    return g_variant_new_string (d->state ? d->state : "idle");
  return NULL;
}

static const GDBusInterfaceVTable vtable = {
  .method_call = handle_method,
  .get_property = handle_get_property,
};

static void
on_bus_acquired (GDBusConnection *bus, const char *name, gpointer data)
{
  Dictate *d = data;
  g_autoptr (GError) err = NULL;

  d->bus = g_object_ref (bus);
  if (!g_dbus_connection_register_object (bus, OBJECT_PATH, d->introspection->interfaces[0],
                                          &vtable, d, NULL, &err)) {
    g_warning ("Registering the object failed: %s", err->message);
    g_main_loop_quit (d->loop);
  }
}

static void
on_name_lost (GDBusConnection *bus, const char *name, gpointer data)
{
  Dictate *d = data;

  g_warning ("Lost or could not get the bus name %s", name);
  g_main_loop_quit (d->loop);
}

static gboolean
on_term (gpointer data)
{
  Dictate *d = data;

  g_main_loop_quit (d->loop);
  return G_SOURCE_REMOVE;
}

static const SherpaOnnxVoiceActivityDetector *
create_vad (void)
{
  SherpaOnnxVadModelConfig config;

  memset (&config, 0, sizeof config);
  config.silero_vad.model = VAD_MODEL;
  config.silero_vad.threshold = 0.5f;
  /* A pause of 0.5 s ends an utterance: short enough that text appears while one
   * speaks on, long enough not to split sentences at commas */
  config.silero_vad.min_silence_duration = 0.5f;
  config.silero_vad.min_speech_duration = 0.25f;
  config.silero_vad.window_size = VAD_WINDOW;
  /* The model is trained on utterances of up to about 20 s */
  config.silero_vad.max_speech_duration = 20.0f;
  config.sample_rate = SAMPLE_RATE;
  config.num_threads = 1;
  config.provider = "cpu";
  return SherpaOnnxCreateVoiceActivityDetector (&config, 60.0f);
}

int
main (int argc, char **argv)
{
  Dictate d = { 0 };
  GSettingsSchemaSource *src = g_settings_schema_source_get_default ();
  g_autoptr (GSettingsSchema) schema = NULL;
  guint owner;

  if (src)
    schema = g_settings_schema_source_lookup (src, SETTINGS_SCHEMA, TRUE);
  if (schema)
    d.settings = g_settings_new (SETTINGS_SCHEMA);
  else
    g_warning ("Settings schema %s missing, using defaults", SETTINGS_SCHEMA);

  d.vad = create_vad ();
  if (d.vad == NULL) {
    g_critical ("Creating the voice activity detector from " VAD_MODEL " failed");
    return 1;
  }

  /* One worker: recognition is already multi-threaded inside, and the recognizer is not
   * meant to be used from two threads at once */
  d.worker = g_thread_pool_new (run_job, NULL, 1, TRUE, NULL);
  d.introspection = g_dbus_node_info_new_for_xml (introspection_xml, NULL);
  d.loop = g_main_loop_new (NULL, FALSE);

  owner = g_bus_own_name (G_BUS_TYPE_SESSION, BUS_NAME, G_BUS_NAME_OWNER_FLAGS_NONE,
                          on_bus_acquired, NULL, on_name_lost, &d, NULL);
  g_unix_signal_add (SIGTERM, on_term, &d);
  g_unix_signal_add (SIGINT, on_term, &d);
  /* Activated but never called: do not stay around */
  schedule_exit (&d);

  g_main_loop_run (d.loop);

  g_bus_unown_name (owner);
  if (d.recorder) {
    /* send_signal() only queues the kill() for GLib's worker thread: without the wait the
     * process can end before it is sent, and pw-record keeps the microphone open */
    g_subprocess_send_signal (d.recorder, SIGTERM);
    g_subprocess_wait (d.recorder, NULL, NULL);
  }
  /* Exit without tearing down ONNX Runtime and its threads: the process ends anyway */
  return 0;
}
