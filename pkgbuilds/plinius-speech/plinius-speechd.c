/*
 * plinius-speechd: speech output for the whole system.
 *
 * A session D-Bus service (org.plinius.Speech1), started on demand by D-Bus activation. Whoever
 * wants something said calls Speak(text, options): the caller announcement in a Bluetooth
 * headset, the preview in GNOME Settings (Sound > Speech Output), speech-dispatcher through
 * plinius-speak, and every later speech output of the system. One voice and one speech rate for
 * all of them, chosen in Settings (GSettings org.plinius.speech).
 *
 * Language: always the system language (the session's locale): the voice chosen for that
 * language in Settings, else the default voice of the locale (pt_BR and pt_PT, en_US and en_GB
 * differ), else of the language. A voice is a Piper model (Piper 1.5: rhasspy/piper-voices,
 * /usr/share/plinius-speech/voices/<id>/ with voice.json, the model and Piper's config; one
 * package per voice), run on the CPU with ONNX Runtime, as Piper does: espeak-ng turns each
 * clause into IPA, the phonemes (NFD, vowel clusters such as "aɪ" as one) become the ids of the
 * voice's phoneme_id_map, framed by "^" and "$" with "_" between, and the model returns the
 * samples of the sentence (peak-normalized as by Piper).
 *
 * Fast: the voice stays loaded for `keep-voice` seconds after the last use (Prepare() loads it
 * ahead), the player (pw-cat) starts while the first sentence is computed, and every sentence
 * plays as soon as it is ready. Stop() is immediate: the player is killed (no fade-out) and the
 * computation is terminated in the middle of the model run.
 *
 * Requests queue up in order. Signals (Started, Finished) go to the caller only. Text is never
 * logged or written anywhere.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later (links libespeak-ng)
 */

#include <gio/gio.h>
#include <glib-unix.h>
#include <json-glib/json-glib.h>
#include <espeak-ng/speak_lib.h>
#include <math.h>
#include <signal.h>
#include <string.h>
#include <onnxruntime_c_api.h>

#define BUS_NAME "org.plinius.Speech1"
#define OBJECT_PATH "/org/plinius/Speech1"
#define IFACE "org.plinius.Speech1"
#define ERROR_NO_VOICE "org.plinius.Speech1.Error.NoVoice"
#define ERROR_INVALID "org.plinius.Speech1.Error.InvalidArgs"
#define SETTINGS_SCHEMA "org.plinius.speech"

#define VOICES_DIR "/usr/share/plinius-speech/voices"

/* Defaults if the schema is missing */
#define DEFAULT_RATE 1.2
#define DEFAULT_KEEP_VOICE_S 600
#define MIN_RATE 0.5
#define MAX_RATE 2.0
/* Role of the stream unless the caller names one: notifications duck music, as navigation does */
#define DEFAULT_ROLE "Notification"
/* Threads for the model: the phone has eight cores, more hardly helps a VITS model */
#define TTS_THREADS 4
/* Piper's markers in phoneme_id_map */
#define PHONEME_BOS "^"
#define PHONEME_EOS "$"
#define PHONEME_PAD "_"
/* Silence between two sentences, and the peak a sentence is normalized to */
#define SENTENCE_GAP_S 0.15
#define PEAK 0.95f
/* Longest text accepted */
#define MAX_TEXT 4096

static const char introspection_xml[] =
  "<node>"
  "  <interface name='" IFACE "'>"
  "    <method name='Speak'>"
  "      <arg direction='in' name='text' type='s'/>"
  "      <arg direction='in' name='options' type='a{sv}'/>"
  "      <arg direction='out' name='id' type='u'/>"
  "    </method>"
  "    <method name='Stop'><arg direction='in' name='id' type='u'/></method>"
  "    <method name='Prepare'/>"
  "    <signal name='Started'><arg name='id' type='u'/></signal>"
  "    <signal name='Finished'><arg name='id' type='u'/><arg name='completed' type='b'/></signal>"
  "    <property name='Language' type='s' access='read'/>"
  "    <property name='Voice' type='s' access='read'/>"
  "  </interface>"
  "</node>";

typedef struct {
  char *id;
  char *language;      /* "de" */
  char *locale;        /* "de_DE" */
  char *model;         /* absolute paths */
  char *config;
  gboolean is_default;
} Voice;

/* Piper's config of the loaded voice (<model>.onnx.json) */
typedef struct {
  char *espeak;            /* espeak-ng voice, "de" */
  int sample_rate;
  float noise_scale, length_scale, noise_w;
  GHashTable *ids;         /* phoneme (UTF-8, one or two codepoints) -> GArray of gint64 */
} VoiceConfig;

typedef struct _Speech Speech;

typedef struct {
  Speech *s;
  guint id;
  char *sender;        /* unique bus name of the caller */
  char *text;          /* NULL: only load the voice (Prepare) */
  Voice *voice;        /* owned copy */
  double rate;
  char *target;        /* PipeWire node name or NULL */
  char *role;
  gint cancelled;      /* atomic */
  gboolean started;    /* Started was sent */
  GSubprocess *player; /* pw-cat, under s->lock */
  GOutputStream *player_in;
  OrtRunOptions *run;  /* of the model run, under s->lock: Stop terminates it */
} Job;

struct _Speech {
  GMainLoop *loop;
  GDBusConnection *bus;
  GDBusNodeInfo *introspection;
  GSettings *settings;              /* NULL if the schema is not installed */
  char *voices_dir;
  char *language;                   /* "de" */
  char *locale;                     /* "de_DE" */

  GThreadPool *worker;              /* one thread: the model, espeak-ng and the player input */
  GMutex lock;                      /* job->player of the current job */
  GQueue queue;                     /* Job *, waiting */
  Job *current;                     /* NULL when idle */
  guint next_id;
  guint keep_source;

  /* Worker thread only */
  const OrtApi *ort;
  OrtEnv *env;
  OrtSession *session;
  OrtMemoryInfo *memory;
  VoiceConfig *config;
  char *loaded_id;
  gboolean espeak_ready;
  char *espeak_voice;
};

static void voice_free (Voice *v)
{
  if (!v)
    return;
  g_free (v->id);
  g_free (v->language);
  g_free (v->locale);
  g_free (v->model);
  g_free (v->config);
  g_free (v);
}

static Voice *voice_copy (const Voice *v)
{
  Voice *c = g_new0 (Voice, 1);
  c->id = g_strdup (v->id);
  c->language = g_strdup (v->language);
  c->locale = g_strdup (v->locale);
  c->model = g_strdup (v->model);
  c->config = g_strdup (v->config);
  c->is_default = v->is_default;
  return c;
}

/* ---- Language and voices ---- */

/* "de_DE.UTF-8", "pt_BR@euro" -> "de", "pt"; "C" and "POSIX" -> "en" */
static char *language_of_locale (const char *locale)
{
  if (!locale || !*locale || !strcmp (locale, "C") || !strcmp (locale, "POSIX") ||
      g_str_has_prefix (locale, "C."))
    return g_strdup ("en");
  gsize n = strcspn (locale, "_.@");
  return g_ascii_strdown (locale, n);
}

/* "pt_BR.UTF-8" -> "pt_BR", "de" -> "de"; "C" -> "en" */
static char *territory_of_locale (const char *locale)
{
  if (!locale || !*locale || !strcmp (locale, "C") || !strcmp (locale, "POSIX") ||
      g_str_has_prefix (locale, "C."))
    return g_strdup ("en");
  gsize n = strcspn (locale, ".@");
  g_autofree char *l = g_strndup (locale, n);
  char *u = strchr (l, '_');
  g_autofree char *lang = g_ascii_strdown (l, u ? (gssize) (u - l) : -1);
  if (!u)
    return g_steal_pointer (&lang);
  g_autofree char *region = g_ascii_strup (u + 1, -1);
  return g_strdup_printf ("%s_%s", lang, region);
}

/* The first language of the session (LANGUAGE, LC_ALL, LC_MESSAGES, LANG) */
static const char *session_locale (void)
{
  const char * const *names = g_get_language_names ();
  return names ? names[0] : NULL;
}

static char *system_language (void)
{
  return language_of_locale (session_locale ());
}

static const char *member_string (JsonObject *o, const char *name)
{
  JsonNode *n = json_object_get_member (o, name);
  if (!n || JSON_NODE_TYPE (n) != JSON_NODE_VALUE || json_node_get_value_type (n) != G_TYPE_STRING)
    return NULL;
  return json_node_get_string (n);
}

/* <dir>/voice.json; NULL if it is missing, broken or names files that are not there */
static Voice *voice_load (const char *dir)
{
  g_autofree char *path = g_build_filename (dir, "voice.json", NULL);
  g_autoptr (JsonParser) parser = json_parser_new ();
  if (!json_parser_load_from_file (parser, path, NULL))
    return NULL;
  JsonNode *root = json_parser_get_root (parser);
  if (!root || !JSON_NODE_HOLDS_OBJECT (root))
    return NULL;
  JsonObject *o = json_node_get_object (root);
  const char *id = member_string (o, "id"), *language = member_string (o, "language");
  const char *model = member_string (o, "model"), *config = member_string (o, "config");
  const char *locale = member_string (o, "locale");
  if (!id || !language || !model || !config || strchr (model, '/') || strchr (config, '/'))
    return NULL;
  Voice *v = g_new0 (Voice, 1);
  v->id = g_strdup (id);
  v->language = g_strdup (language);
  v->locale = g_strdup (locale ? locale : language);
  v->model = g_build_filename (dir, model, NULL);
  v->config = g_build_filename (dir, config, NULL);
  JsonNode *def = json_object_get_member (o, "default");
  v->is_default = def && JSON_NODE_TYPE (def) == JSON_NODE_VALUE &&
                  json_node_get_value_type (def) == G_TYPE_BOOLEAN && json_node_get_boolean (def);
  if (!g_file_test (v->model, G_FILE_TEST_IS_REGULAR) || !g_file_test (v->config, G_FILE_TEST_IS_REGULAR)) {
    voice_free (v);
    return NULL;
  }
  return v;
}

static gint compare_voice_ids (gconstpointer a, gconstpointer b)
{
  return strcmp (((const Voice *) a)->id, ((const Voice *) b)->id);
}

/* All installed voices, sorted by id */
static GList *voices_installed (const char *voices_dir)
{
  GList *list = NULL;
  g_autoptr (GDir) d = g_dir_open (voices_dir, 0, NULL);
  const char *name;
  while (d && (name = g_dir_read_name (d))) {
    g_autofree char *dir = g_build_filename (voices_dir, name, NULL);
    Voice *v = voice_load (dir);
    if (v)
      list = g_list_insert_sorted (list, v, compare_voice_ids);
  }
  return list;
}

/*
 * The voice to speak @language with: @wanted if it is installed (any language: the preview names
 * one), else the voice chosen for the language (@chosen), else the default voice of the locale
 * (@locale, "pt_BR"), else any voice of the locale, else a default voice of the language, else its
 * first voice. NULL if the language has no voice.
 */
static Voice *voice_pick (GList *voices, const char *language, const char *locale, const char *chosen,
                          const char *wanted)
{
  if (wanted) {
    for (GList *l = voices; l; l = l->next)
      if (!strcmp (((Voice *) l->data)->id, wanted))
        return l->data;
    return NULL;
  }
  Voice *local_def = NULL, *local = NULL, *def = NULL, *first = NULL;
  for (GList *l = voices; l; l = l->next) {
    Voice *v = l->data;
    if (strcmp (v->language, language))
      continue;
    if (chosen && !strcmp (v->id, chosen))
      return v;
    /* Only a locale with a region tells two voices of a language apart */
    gboolean here = locale && strchr (locale, '_') && !strcmp (v->locale, locale);
    if (here && v->is_default && !local_def)
      local_def = v;
    if (here && !local)
      local = v;
    if (v->is_default && !def)
      def = v;
    if (!first)
      first = v;
  }
  return local_def ? local_def : local ? local : def ? def : first;
}

static char *chosen_voice (Speech *s, const char *language)
{
  if (!s->settings)
    return NULL;
  g_autoptr (GVariant) map = g_settings_get_value (s->settings, "voices");
  char *id = NULL;
  g_variant_lookup (map, language, "s", &id);
  return id;
}

/* Resolves the voice for a request, in the main thread; a copy, or NULL */
static Voice *resolve_voice (Speech *s, const char *wanted)
{
  GList *voices = voices_installed (s->voices_dir);
  g_autofree char *chosen = chosen_voice (s, s->language);
  Voice *v = voice_pick (voices, s->language, s->locale, chosen, wanted);
  Voice *copy = v ? voice_copy (v) : NULL;
  g_list_free_full (voices, (GDestroyNotify) voice_free);
  return copy;
}

static double clamp_rate (double rate)
{
  if (!(rate == rate)) /* NaN */
    return DEFAULT_RATE;
  return CLAMP (rate, MIN_RATE, MAX_RATE);
}

static double setting_rate (Speech *s)
{
  return clamp_rate (s->settings ? g_settings_get_double (s->settings, "rate") : DEFAULT_RATE);
}

static guint keep_voice_s (Speech *s)
{
  return s->settings ? g_settings_get_uint (s->settings, "keep-voice") : DEFAULT_KEEP_VOICE_S;
}

/* ---- Text to phonemes ---- */

/*
 * Piper's phonemizer (piper-phonemize) runs espeak-ng clause by clause and puts the clause's
 * punctuation behind its phonemes: ".", "?", "!" end a sentence, ",", ":", ";" are followed by a
 * space. Upstream espeak-ng has no terminator in its API, so the clauses are cut here with the
 * same punctuation. A sentence without punctuation at its end gets a ".", as espeak-ng's end of
 * text counts as a full stop. Each sentence is one array of codepoints in @codepoints, its length
 * in @lens.
 */
typedef const char *(*PhonemizeFunc) (const char *clause, gpointer data);

static gboolean is_sentence_end (gunichar c)
{
  return c == '.' || c == '?' || c == '!' || c == 0x2026 /* … */;
}

static gboolean is_clause_end (gunichar c)
{
  return c == ',' || c == ':' || c == ';';
}

static void append_utf8 (GArray *codepoints, const char *s)
{
  for (const char *p = s; p && *p; p = g_utf8_next_char (p)) {
    gunichar c = g_utf8_get_char_validated (p, -1);
    if (c == (gunichar) -1 || c == (gunichar) -2)
      break;
    /* espeak-ng ends lines between the parts of a clause */
    gint32 v = (c == '\n' || c == '\r' || c == '\t') ? ' ' : (gint32) c;
    g_array_append_val (codepoints, v);
  }
}

static void end_sentence (GArray *codepoints, GArray *lens, gsize *start)
{
  /* Trailing spaces belong to no sentence */
  while (codepoints->len > *start && g_array_index (codepoints, gint32, codepoints->len - 1) == ' ')
    g_array_set_size (codepoints, codepoints->len - 1);
  gint32 n = codepoints->len - *start;
  if (n > 0)
    g_array_append_val (lens, n);
  *start = codepoints->len;
}

static void phonemize_text (const char *text, PhonemizeFunc phonemize, gpointer data,
                            GArray *codepoints, GArray *lens)
{
  g_autoptr (GString) clause = g_string_new (NULL);
  gsize start = codepoints->len;
  gboolean spoken = FALSE; /* the current sentence has phonemes */

  for (const char *p = text;; p = g_utf8_next_char (p)) {
    gunichar c = *p ? g_utf8_get_char (p) : 0;
    gboolean last = c == 0;
    if (!last && !is_sentence_end (c) && !is_clause_end (c)) {
      g_string_append_unichar (clause, c);
      continue;
    }
    g_strstrip (clause->str);
    clause->len = strlen (clause->str);
    if (clause->len) {
      const char *ph = phonemize (clause->str, data);
      if (ph && *ph) {
        append_utf8 (codepoints, ph);
        spoken = TRUE;
      }
    }
    g_string_truncate (clause, 0);
    if (spoken && !last) {
      gint32 v = (gint32) c;
      if (c == 0x2026)
        v = '.';
      g_array_append_val (codepoints, v);
      if (is_clause_end (c)) {
        gint32 space = ' ';
        g_array_append_val (codepoints, space);
      }
    }
    if (last || is_sentence_end (c)) {
      if (spoken && last) {
        /* The end of the text is a full stop, also after a comma */
        while (codepoints->len > start && g_array_index (codepoints, gint32, codepoints->len - 1) == ' ')
          g_array_set_size (codepoints, codepoints->len - 1);
        gint32 *end = &g_array_index (codepoints, gint32, codepoints->len - 1);
        if (is_clause_end (*end))
          *end = '.';
        else {
          gint32 dot = '.';
          g_array_append_val (codepoints, dot);
        }
      }
      end_sentence (codepoints, lens, &start);
      spoken = FALSE;
      if (last)
        break;
    }
  }
}

/*
 * espeak-ng: one clause to IPA, as Piper takes it. espeak-ng may stop after a part and hand back
 * the rest. Its language switches for foreign words ("(en)", "(de)") are no phonemes, and Piper's
 * phoneme maps hold decomposed characters (NFD).
 */
static void strip_language_marks (GString *s)
{
  gsize w = 0;
  for (gsize r = 0; r < s->len;) {
    if (s->str[r] == '(') {
      gsize e = r + 1;
      while (e < s->len && (g_ascii_isalpha (s->str[e]) || s->str[e] == '-'))
        e++;
      if (e < s->len && s->str[e] == ')' && e > r + 1) {
        r = e + 1;
        continue;
      }
    }
    s->str[w++] = s->str[r++];
  }
  g_string_truncate (s, w);
}

static const char *espeak_phonemize (const char *clause, gpointer data)
{
  GString *out = data;
  g_string_truncate (out, 0);
  const void *p = clause;
  while (p) {
    const char *ph = espeak_TextToPhonemes (&p, espeakCHARS_UTF8, espeakPHONEMES_IPA);
    if (!ph)
      break;
    if (*ph) {
      if (out->len)
        g_string_append_c (out, ' ');
      g_string_append (out, ph);
    }
  }
  strip_language_marks (out);
  g_autofree char *nfd = g_utf8_normalize (out->str, -1, G_NORMALIZE_NFD);
  g_string_assign (out, nfd ? nfd : "");
  return out->str;
}

/* ---- Piper's config and its phoneme ids ---- */

static void voice_config_free (VoiceConfig *c)
{
  if (!c)
    return;
  g_free (c->espeak);
  g_clear_pointer (&c->ids, g_hash_table_unref);
  g_free (c);
}

static float member_float (JsonObject *o, const char *name, float fallback)
{
  JsonNode *n = o ? json_object_get_member (o, name) : NULL;
  if (!n || JSON_NODE_TYPE (n) != JSON_NODE_VALUE)
    return fallback;
  GType t = json_node_get_value_type (n);
  if (t != G_TYPE_DOUBLE && t != G_TYPE_INT64)
    return fallback;
  return (float) json_node_get_double (n);
}

static JsonObject *member_object (JsonObject *o, const char *name)
{
  JsonNode *n = o ? json_object_get_member (o, name) : NULL;
  return n && JSON_NODE_HOLDS_OBJECT (n) ? json_node_get_object (n) : NULL;
}

/* <model>.onnx.json: espeak-ng voice, sample rate, inference defaults, phoneme_id_map */
static VoiceConfig *voice_config_load (const char *path)
{
  g_autoptr (JsonParser) parser = json_parser_new ();
  if (!json_parser_load_from_file (parser, path, NULL))
    return NULL;
  JsonNode *root = json_parser_get_root (parser);
  if (!root || !JSON_NODE_HOLDS_OBJECT (root))
    return NULL;
  JsonObject *o = json_node_get_object (root);
  JsonObject *espeak = member_object (o, "espeak"), *audio = member_object (o, "audio");
  JsonObject *inference = member_object (o, "inference"), *map = member_object (o, "phoneme_id_map");
  const char *voice = espeak ? member_string (espeak, "voice") : NULL;
  int rate = (int) member_float (audio, "sample_rate", 0);
  if (!voice || !map || rate < 8000 || rate > 96000)
    return NULL;
  VoiceConfig *c = g_new0 (VoiceConfig, 1);
  c->espeak = g_strdup (voice);
  c->sample_rate = rate;
  c->noise_scale = member_float (inference, "noise_scale", 0.667f);
  c->length_scale = member_float (inference, "length_scale", 1.0f);
  c->noise_w = member_float (inference, "noise_w", 0.8f);
  c->ids = g_hash_table_new_full (g_str_hash, g_str_equal, g_free, (GDestroyNotify) g_array_unref);
  g_autoptr (GList) keys = json_object_get_members (map);
  for (GList *l = keys; l; l = l->next) {
    JsonNode *n = json_object_get_member (map, l->data);
    if (!JSON_NODE_HOLDS_ARRAY (n))
      continue;
    JsonArray *a = json_node_get_array (n);
    GArray *ids = g_array_new (FALSE, FALSE, sizeof (gint64));
    for (guint i = 0; i < json_array_get_length (a); i++) {
      gint64 id = json_array_get_int_element (a, i);
      g_array_append_val (ids, id);
    }
    g_hash_table_insert (c->ids, g_strdup (l->data), ids);
  }
  if (!g_hash_table_contains (c->ids, PHONEME_BOS) || !g_hash_table_contains (c->ids, PHONEME_EOS) ||
      !g_hash_table_contains (c->ids, PHONEME_PAD)) {
    voice_config_free (c);
    return NULL;
  }
  return c;
}

static void append_ids (GArray *out, VoiceConfig *c, const char *phoneme)
{
  GArray *ids = g_hash_table_lookup (c->ids, phoneme);
  if (ids)
    g_array_append_vals (out, ids->data, ids->len);
}

/*
 * One sentence's phonemes to the model's ids, as Piper: "^" "_", then every phoneme followed by
 * "_", then "$". Two codepoints the map knows as one (Piper 1.5's vowel clusters, "aɪ") are one
 * phoneme; phonemes the voice does not know are left out.
 */
static GArray *phonemes_to_ids (VoiceConfig *c, const gint32 *cp, gsize n)
{
  GArray *out = g_array_new (FALSE, FALSE, sizeof (gint64));
  append_ids (out, c, PHONEME_BOS);
  append_ids (out, c, PHONEME_PAD);
  for (gsize i = 0; i < n; i++) {
    char one[16] = {0}, two[16] = {0};
    g_unichar_to_utf8 (cp[i], one);
    if (i + 1 < n) {
      gint len = g_unichar_to_utf8 (cp[i], two);
      g_unichar_to_utf8 (cp[i + 1], two + len);
      if (g_hash_table_contains (c->ids, two)) {
        append_ids (out, c, two);
        append_ids (out, c, PHONEME_PAD);
        i++;
        continue;
      }
    }
    if (g_hash_table_contains (c->ids, one)) {
      append_ids (out, c, one);
      append_ids (out, c, PHONEME_PAD);
    }
  }
  append_ids (out, c, PHONEME_EOS);
  return out;
}

/* Piper scales every sentence to full volume; a quiet one is not blown up beyond 100 times */
static void normalize_peak (float *samples, gsize n)
{
  float peak = 0;
  for (gsize i = 0; i < n; i++)
    peak = MAX (peak, fabsf (samples[i]));
  float scale = PEAK / MAX (peak, 0.01f);
  for (gsize i = 0; i < n; i++)
    samples[i] = CLAMP (samples[i] * scale, -1.0f, 1.0f);
}

/* ---- The worker: model, phonemes, player ---- */

static gboolean worker_espeak (Speech *s, const char *voice)
{
  if (!s->espeak_ready) {
    if (espeak_Initialize (AUDIO_OUTPUT_SYNCHRONOUS, 0, NULL, 0) < 0) {
      g_warning ("espeak-ng does not start");
      return FALSE;
    }
    s->espeak_ready = TRUE;
  }
  if (g_strcmp0 (s->espeak_voice, voice)) {
    if (espeak_SetVoiceByName (voice) != EE_OK) {
      g_warning ("espeak-ng has no voice %s", voice);
      return FALSE;
    }
    g_free (s->espeak_voice);
    s->espeak_voice = g_strdup (voice);
  }
  return TRUE;
}

static gboolean ort_ok (Speech *s, OrtStatus *status, const char *what)
{
  if (!status)
    return TRUE;
  if (strcmp (what, "run")) /* a terminated run is Stop(), not worth a warning */
    g_warning ("%s: %s", what, s->ort->GetErrorMessage (status));
  s->ort->ReleaseStatus (status);
  return FALSE;
}

static void worker_unload (Speech *s)
{
  if (s->session)
    s->ort->ReleaseSession (s->session);
  s->session = NULL;
  g_clear_pointer (&s->config, voice_config_free);
  g_clear_pointer (&s->loaded_id, g_free);
}

static gboolean worker_load (Speech *s, const Voice *v)
{
  if (s->session && !g_strcmp0 (s->loaded_id, v->id))
    return TRUE;
  if (!s->ort) {
    const OrtApiBase *base = OrtGetApiBase ();
    s->ort = base ? base->GetApi (ORT_API_VERSION) : NULL;
    if (!s->ort) {
      g_warning ("ONNX Runtime does not offer API %d", ORT_API_VERSION);
      return FALSE;
    }
  }
  if (!s->env && !ort_ok (s, s->ort->CreateEnv (ORT_LOGGING_LEVEL_WARNING, "plinius-speech", &s->env), "env"))
    return FALSE;
  if (!s->memory &&
      !ort_ok (s, s->ort->CreateCpuMemoryInfo (OrtArenaAllocator, OrtMemTypeDefault, &s->memory), "memory"))
    return FALSE;
  worker_unload (s);
  VoiceConfig *config = voice_config_load (v->config);
  if (!config) {
    g_warning ("voice %s: no usable %s", v->id, v->config);
    return FALSE;
  }
  OrtSessionOptions *opts = NULL;
  gboolean ok = ort_ok (s, s->ort->CreateSessionOptions (&opts), "options") &&
                ort_ok (s, s->ort->SetIntraOpNumThreads (opts, TTS_THREADS), "threads") &&
                ort_ok (s, s->ort->SetSessionGraphOptimizationLevel (opts, ORT_ENABLE_ALL), "optimization") &&
                ort_ok (s, s->ort->CreateSession (s->env, v->model, opts, &s->session), v->id);
  if (opts)
    s->ort->ReleaseSessionOptions (opts);
  if (!ok) {
    s->session = NULL;
    voice_config_free (config);
    return FALSE;
  }
  s->config = config;
  s->loaded_id = g_strdup (v->id);
  return TRUE;
}

/*
 * The model on one sentence: inputs "input" (ids), "input_lengths", "scales" (noise, length,
 * noise_w), output "output" (samples). NULL if it fails or Stop() terminated it.
 */
static float *synthesize (Speech *s, Job *job, GArray *ids, gsize *n_samples)
{
  const OrtApi *ort = s->ort;
  int64_t len = ids->len, shape_ids[2] = {1, len}, shape_len[1] = {1}, shape_scales[1] = {3};
  float scales[3] = {s->config->noise_scale, s->config->length_scale / (float) job->rate, s->config->noise_w};
  OrtValue *in[3] = {NULL, NULL, NULL}, *out = NULL;
  float *result = NULL;
  if (ort_ok (s, ort->CreateTensorWithDataAsOrtValue (s->memory, ids->data, ids->len * sizeof (gint64), shape_ids,
                                                       2, ONNX_TENSOR_ELEMENT_DATA_TYPE_INT64, &in[0]), "input") &&
      ort_ok (s, ort->CreateTensorWithDataAsOrtValue (s->memory, &len, sizeof len, shape_len, 1,
                                                       ONNX_TENSOR_ELEMENT_DATA_TYPE_INT64, &in[1]), "lengths") &&
      ort_ok (s, ort->CreateTensorWithDataAsOrtValue (s->memory, scales, sizeof scales, shape_scales, 1,
                                                       ONNX_TENSOR_ELEMENT_DATA_TYPE_FLOAT, &in[2]), "scales")) {
    const char *in_names[] = {"input", "input_lengths", "scales"}, *out_names[] = {"output"};
    if (ort_ok (s, ort->Run (s->session, job->run, in_names, (const OrtValue * const *) in, 3, out_names, 1, &out),
                "run")) {
      OrtTensorTypeAndShapeInfo *info = NULL;
      float *data = NULL;
      size_t n = 0;
      if (ort_ok (s, ort->GetTensorTypeAndShape (out, &info), "shape") &&
          ort_ok (s, ort->GetTensorShapeElementCount (info, &n), "count") &&
          ort_ok (s, ort->GetTensorMutableData (out, (void **) &data), "data") && n > 0) {
        result = g_memdup2 (data, n * sizeof (float));
        *n_samples = n;
      }
      if (info)
        ort->ReleaseTensorTypeAndShapeInfo (info);
    }
  }
  for (int i = 0; i < 3; i++)
    if (in[i])
      ort->ReleaseValue (in[i]);
  if (out)
    ort->ReleaseValue (out);
  return result;
}

/* pw-cat reading mono float samples from its stdin */
static GSubprocess *start_player (Job *job, int rate, GError **error)
{
  g_autofree char *rate_s = g_strdup_printf ("%d", rate);
  g_autofree char *props = g_strdup_printf ("{ application.name = \"Speech\" node.description = \"Speech\" "
                                            "media.role = \"%s\" }", job->role);
  g_autoptr (GPtrArray) argv = g_ptr_array_new ();
  g_ptr_array_add (argv, "pw-cat");
  g_ptr_array_add (argv, "--playback");
  g_ptr_array_add (argv, "--raw");
  g_ptr_array_add (argv, "--rate");
  g_ptr_array_add (argv, rate_s);
  g_ptr_array_add (argv, "--channels");
  g_ptr_array_add (argv, "1");
  g_ptr_array_add (argv, "--format");
  g_ptr_array_add (argv, "f32");
  g_ptr_array_add (argv, "--media-role");
  g_ptr_array_add (argv, job->role);
  if (job->target) {
    g_ptr_array_add (argv, "--target");
    g_ptr_array_add (argv, job->target);
  }
  g_ptr_array_add (argv, "-P");
  g_ptr_array_add (argv, props);
  g_ptr_array_add (argv, "-");
  g_ptr_array_add (argv, NULL);
  return g_subprocess_newv ((const char * const *) argv->pdata, G_SUBPROCESS_FLAGS_STDIN_PIPE, error);
}

/* FALSE when the player is gone (killed by Stop, or PipeWire) */
static gboolean play (Job *job, const float *samples, gsize n)
{
  return n == 0 || g_output_stream_write_all (job->player_in, samples, n * sizeof (float), NULL, NULL, NULL);
}

static void job_done_in_main (Job *job, gboolean completed);

typedef struct {
  Job *job;
  gboolean completed;
} DoneData;

static gboolean done_idle (gpointer p)
{
  DoneData *dd = p;
  job_done_in_main (dd->job, dd->completed);
  g_free (dd);
  return G_SOURCE_REMOVE;
}

static void finish_from_worker (Job *job, gboolean completed)
{
  DoneData *dd = g_new (DoneData, 1);
  dd->job = job;
  dd->completed = completed;
  g_idle_add (done_idle, dd);
}

static void on_player_exit (GObject *source, GAsyncResult *res, gpointer data)
{
  Job *job = data;
  GSubprocess *p = G_SUBPROCESS (source);
  gboolean ok = g_subprocess_wait_finish (p, res, NULL) && g_subprocess_get_if_exited (p) &&
                g_subprocess_get_exit_status (p) == 0;
  job_done_in_main (job, ok && !g_atomic_int_get (&job->cancelled));
}

static gboolean wait_player_idle (gpointer p)
{
  Job *job = p;
  g_subprocess_wait_async (job->player, NULL, on_player_exit, job);
  return G_SOURCE_REMOVE;
}

static void run_job (gpointer data, gpointer user_data)
{
  Job *job = data;
  Speech *s = job->s;

  if (!worker_load (s, job->voice) || !job->text) {
    finish_from_worker (job, s->session != NULL && !job->text);
    return;
  }
  g_autoptr (GArray) codepoints = g_array_new (FALSE, FALSE, sizeof (gint32));
  g_autoptr (GArray) lens = g_array_new (FALSE, FALSE, sizeof (gint32));
  if (worker_espeak (s, s->config->espeak)) {
    g_autoptr (GString) buf = g_string_new (NULL);
    phonemize_text (job->text, espeak_phonemize, buf, codepoints, lens);
  }
  if (lens->len == 0 || g_atomic_int_get (&job->cancelled)) {
    finish_from_worker (job, lens->len == 0 && !g_atomic_int_get (&job->cancelled));
    return;
  }

  g_autoptr (GError) error = NULL;
  OrtRunOptions *run = NULL;
  g_mutex_lock (&s->lock);
  if (!g_atomic_int_get (&job->cancelled) && ort_ok (s, s->ort->CreateRunOptions (&run), "run options")) {
    job->run = run;
    job->player = start_player (job, s->config->sample_rate, &error);
    if (job->player)
      job->player_in = g_subprocess_get_stdin_pipe (job->player);
  }
  g_mutex_unlock (&s->lock);
  if (!job->player) {
    if (error)
      g_warning ("no player: %s", error->message);
    g_mutex_lock (&s->lock);
    job->run = NULL;
    g_mutex_unlock (&s->lock);
    if (run)
      s->ort->ReleaseRunOptions (run);
    finish_from_worker (job, FALSE);
    return;
  }

  gsize gap = (gsize) (SENTENCE_GAP_S * s->config->sample_rate), at = 0;
  g_autofree float *silence = g_new0 (float, gap);
  for (guint i = 0; i < lens->len && !g_atomic_int_get (&job->cancelled); i++) {
    gint32 n = g_array_index (lens, gint32, i);
    g_autoptr (GArray) ids = phonemes_to_ids (s->config, &g_array_index (codepoints, gint32, at), n);
    at += n;
    gsize n_samples = 0;
    g_autofree float *samples = synthesize (s, job, ids, &n_samples);
    if (!samples)
      break;
    normalize_peak (samples, n_samples);
    if ((i > 0 && !play (job, silence, gap)) || !play (job, samples, n_samples))
      break;
  }
  g_mutex_lock (&s->lock);
  job->run = NULL;
  g_mutex_unlock (&s->lock);
  s->ort->ReleaseRunOptions (run);
  /* End of input: pw-cat plays what it has and exits */
  g_output_stream_close (job->player_in, NULL, NULL);
  g_idle_add (wait_player_idle, job);
}

/* ---- Jobs in the main thread ---- */

static void job_free (Job *job)
{
  g_free (job->sender);
  g_free (job->text);
  voice_free (job->voice);
  g_free (job->target);
  g_free (job->role);
  g_clear_object (&job->player);
  g_free (job);
}

static void emit_to (Speech *s, const char *dest, const char *name, GVariant *params)
{
  if (s->bus)
    g_dbus_connection_emit_signal (s->bus, dest, OBJECT_PATH, IFACE, name, params, NULL);
  else
    g_variant_unref (g_variant_ref_sink (params));
}

static gboolean on_keep_timeout (gpointer data)
{
  Speech *s = data;
  s->keep_source = 0;
  /* Exiting frees the model; D-Bus activation starts the service again */
  if (s->loop)
    g_main_loop_quit (s->loop);
  return G_SOURCE_REMOVE;
}

static void schedule_keep (Speech *s)
{
  g_clear_handle_id (&s->keep_source, g_source_remove);
  s->keep_source = g_timeout_add_seconds (MAX (1u, keep_voice_s (s)), on_keep_timeout, s);
}

static void start_next (Speech *s)
{
  if (s->current)
    return;
  s->current = g_queue_pop_head (&s->queue);
  if (!s->current) {
    schedule_keep (s);
    return;
  }
  g_clear_handle_id (&s->keep_source, g_source_remove);
  Job *job = s->current;
  if (job->text) {
    job->started = TRUE;
    emit_to (s, job->sender, "Started", g_variant_new ("(u)", job->id));
  }
  g_thread_pool_push (s->worker, job, NULL);
}

static void job_done_in_main (Job *job, gboolean completed)
{
  Speech *s = job->s;
  if (job->started)
    emit_to (s, job->sender, "Finished", g_variant_new ("(ub)", job->id, completed));
  g_mutex_lock (&s->lock);
  if (s->current == job)
    s->current = NULL;
  g_mutex_unlock (&s->lock);
  job_free (job);
  start_next (s);
}

static void cancel_job (Speech *s, Job *job)
{
  g_atomic_int_set (&job->cancelled, 1);
  g_mutex_lock (&s->lock);
  if (job->player)
    g_subprocess_force_exit (job->player); /* silence now, no fade-out */
  if (job->run) {
    OrtStatus *status = s->ort->RunOptionsSetTerminate (job->run); /* the model stops in the middle */
    if (status)
      s->ort->ReleaseStatus (status);
  }
  g_mutex_unlock (&s->lock);
}

/* Stop(id): 0 stops everything of every caller */
static void stop_jobs (Speech *s, guint id)
{
  for (GList *l = s->queue.head; l;) {
    GList *next = l->next;
    Job *job = l->data;
    if (id == 0 || job->id == id) {
      g_queue_delete_link (&s->queue, l);
      if (job->text)
        emit_to (s, job->sender, "Finished", g_variant_new ("(ub)", job->id, FALSE));
      job_free (job);
    }
    l = next;
  }
  if (s->current && (id == 0 || s->current->id == id))
    cancel_job (s, s->current);
}

static Job *new_job (Speech *s, const char *sender, const char *text, Voice *voice)
{
  Job *job = g_new0 (Job, 1);
  job->s = s;
  job->id = ++s->next_id ? s->next_id : ++s->next_id; /* never 0 */
  job->sender = g_strdup (sender);
  job->text = g_strdup (text);
  job->voice = voice;
  job->rate = setting_rate (s);
  job->role = g_strdup (DEFAULT_ROLE);
  return job;
}

/* Speak(text, options): NULL on success with *id set, else the error name and message */
static const char *speak (Speech *s, const char *sender, const char *text, GVariant *options,
                          guint *id, char **message)
{
  if (!g_utf8_validate (text, -1, NULL) || strlen (text) > MAX_TEXT) {
    *message = g_strdup ("The text is not UTF-8 or too long");
    return ERROR_INVALID;
  }
  const char *wanted = NULL, *target = NULL, *role = NULL;
  double rate = -1;
  g_variant_lookup (options, "voice", "&s", &wanted);
  g_variant_lookup (options, "target", "&s", &target);
  g_variant_lookup (options, "role", "&s", &role);
  g_variant_lookup (options, "rate", "d", &rate);
  if (role && (!*role || strpbrk (role, "\"{}\\\n")))
    role = NULL;
  Voice *voice = resolve_voice (s, wanted && *wanted ? wanted : NULL);
  if (!voice) {
    *message = wanted && *wanted ? g_strdup_printf ("Voice %s is not installed", wanted)
                                 : g_strdup_printf ("No voice for the language %s", s->language);
    return ERROR_NO_VOICE;
  }
  Job *job = new_job (s, sender, text, voice);
  if (rate > 0)
    job->rate = clamp_rate (rate);
  if (target && *target)
    job->target = g_strdup (target);
  if (role) {
    g_free (job->role);
    job->role = g_strdup (role);
  }
  *id = job->id;
  g_queue_push_tail (&s->queue, job);
  start_next (s);
  return NULL;
}

/* Prepare(): load the voice of the system language now; FALSE if there is none */
static gboolean prepare (Speech *s, const char *sender)
{
  Voice *voice = resolve_voice (s, NULL);
  if (!voice)
    return FALSE;
  g_queue_push_tail (&s->queue, new_job (s, sender, NULL, voice));
  start_next (s);
  return TRUE;
}

/* ---- D-Bus ---- */

static void on_method (GDBusConnection *bus, const char *sender, const char *path, const char *iface,
                       const char *method, GVariant *params, GDBusMethodInvocation *invocation,
                       gpointer user_data)
{
  Speech *s = user_data;
  if (!strcmp (method, "Speak")) {
    const char *text;
    g_autoptr (GVariant) options = NULL;
    g_variant_get (params, "(&s@a{sv})", &text, &options);
    guint id = 0;
    g_autofree char *message = NULL;
    const char *err = speak (s, sender, text, options, &id, &message);
    if (err)
      g_dbus_method_invocation_return_dbus_error (invocation, err, message);
    else
      g_dbus_method_invocation_return_value (invocation, g_variant_new ("(u)", id));
  } else if (!strcmp (method, "Stop")) {
    guint id;
    g_variant_get (params, "(u)", &id);
    stop_jobs (s, id);
    g_dbus_method_invocation_return_value (invocation, NULL);
  } else if (!strcmp (method, "Prepare")) {
    if (prepare (s, sender))
      g_dbus_method_invocation_return_value (invocation, NULL);
    else
      g_dbus_method_invocation_return_dbus_error (invocation, ERROR_NO_VOICE, "No voice for the language");
  } else {
    g_dbus_method_invocation_return_error (invocation, G_DBUS_ERROR, G_DBUS_ERROR_UNKNOWN_METHOD,
                                           "Unknown method %s", method);
  }
}

static GVariant *on_get (GDBusConnection *bus, const char *sender, const char *path, const char *iface,
                         const char *property, GError **error, gpointer user_data)
{
  Speech *s = user_data;
  if (!strcmp (property, "Language"))
    return g_variant_new_string (s->language);
  if (!strcmp (property, "Voice")) {
    Voice *v = resolve_voice (s, NULL);
    GVariant *r = g_variant_new_string (v ? v->id : "");
    voice_free (v);
    return r;
  }
  g_set_error (error, G_DBUS_ERROR, G_DBUS_ERROR_UNKNOWN_PROPERTY, "Unknown property %s", property);
  return NULL;
}

static const GDBusInterfaceVTable vtable = {on_method, on_get, NULL, {0}};

static void on_bus (GDBusConnection *bus, const char *name, gpointer user_data)
{
  Speech *s = user_data;
  s->bus = bus;
  g_autoptr (GError) error = NULL;
  if (!g_dbus_connection_register_object (bus, OBJECT_PATH, s->introspection->interfaces[0],
                                          &vtable, s, NULL, &error)) {
    g_warning ("cannot export %s: %s", OBJECT_PATH, error->message);
    g_main_loop_quit (s->loop);
  }
}

static void on_name_lost (GDBusConnection *bus, const char *name, gpointer user_data)
{
  Speech *s = user_data;
  g_warning ("lost or could not get the name %s", name);
  g_main_loop_quit (s->loop);
}

static gboolean on_term (gpointer data)
{
  Speech *s = data;
  stop_jobs (s, 0);
  g_main_loop_quit (s->loop);
  return G_SOURCE_REMOVE;
}

static void speech_init (Speech *s, const char *voices_dir)
{
  g_mutex_init (&s->lock);
  g_queue_init (&s->queue);
  s->voices_dir = g_strdup (voices_dir);
  s->language = system_language ();
  s->locale = territory_of_locale (session_locale ());
  GSettingsSchemaSource *src = g_settings_schema_source_get_default ();
  g_autoptr (GSettingsSchema) schema = src ? g_settings_schema_source_lookup (src, SETTINGS_SCHEMA, TRUE) : NULL;
  if (schema)
    s->settings = g_settings_new (SETTINGS_SCHEMA);
  s->worker = g_thread_pool_new (run_job, NULL, 1, TRUE, NULL);
}

int main (int argc, char **argv)
{
  Speech s = {0};
  /* A write to a player Stop() killed fails with EPIPE instead of ending the service */
  signal (SIGPIPE, SIG_IGN);
  speech_init (&s, VOICES_DIR);
  s.loop = g_main_loop_new (NULL, FALSE);
  s.introspection = g_dbus_node_info_new_for_xml (introspection_xml, NULL);
  g_unix_signal_add (SIGTERM, on_term, &s);
  g_unix_signal_add (SIGINT, on_term, &s);
  guint owner = g_bus_own_name (G_BUS_TYPE_SESSION, BUS_NAME, G_BUS_NAME_OWNER_FLAGS_NONE,
                                on_bus, NULL, on_name_lost, &s, NULL);
  /* Started by activation for a call that is on its way; exit if none comes */
  schedule_keep (&s);
  g_main_loop_run (s.loop);
  g_bus_unown_name (owner);
  return 0;
}
