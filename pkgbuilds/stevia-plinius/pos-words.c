/*
 * Word completion from a frequency list (plinius-words) and the words the user types.
 *
 * The list has one word per line, the most frequent first, in the case it is written in
 * ("Bahnhof", "hallo"). A prefix of at least POS_WORDS_MIN_PREFIX characters finds the words
 * that start with it, case and ß/ss alike (case folded), the most frequent first. Words the
 * user typed POS_WORDS_LEARN_MIN times or more come before them, the most typed first; a word
 * that is not in the list is offered from then on as well, so a typo typed once never is.
 *
 * Learned words are kept in a file of their own ("<times>\t<word>" per line, mode 0600) next
 * to nothing else: the keyboard only completes, and so learns, in fields of purpose "normal"
 * that are neither hidden nor marked sensitive.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "pos-words.h"

#include <errno.h>
#include <stdlib.h>
#include <string.h>

#define NOT_LISTED G_MAXUINT

typedef struct {
  char  *word;    /* as offered */
  char  *key;     /* NFC, case folded: what a prefix is matched against */
  guint  rank;    /* line in the list, NOT_LISTED for a learned word only */
  guint  learned; /* times typed */
} Entry;

struct _PosWords {
  GPtrArray  *entries; /* Entry, sorted by key */
  GHashTable *by_key;  /* key -> Entry */
  guint       n_learned_only;
};


static void
entry_free (gpointer data)
{
  Entry *e = data;

  g_free (e->word);
  g_free (e->key);
  g_free (e);
}


static char *
fold (const char *word)
{
  g_autofree char *nfc = g_utf8_normalize (word, -1, G_NORMALIZE_NFC);

  return g_utf8_casefold (nfc, -1);
}


PosWords *
pos_words_new (void)
{
  PosWords *self = g_new0 (PosWords, 1);

  self->entries = g_ptr_array_new_with_free_func (entry_free);
  self->by_key = g_hash_table_new (g_str_hash, g_str_equal);
  return self;
}


void
pos_words_free (PosWords *self)
{
  if (self == NULL)
    return;
  g_hash_table_destroy (self->by_key);
  g_ptr_array_unref (self->entries);
  g_free (self);
}


static int
compare_entries (gconstpointer a, gconstpointer b)
{
  const Entry *ea = *(Entry * const *)a;
  const Entry *eb = *(Entry * const *)b;

  return strcmp (ea->key, eb->key);
}


/* Index of the first entry whose key is not less than @key */
static guint
lower_bound (PosWords *self, const char *key)
{
  guint lo = 0, hi = self->entries->len;

  while (lo < hi) {
    guint mid = lo + (hi - lo) / 2;
    Entry *e = g_ptr_array_index (self->entries, mid);

    if (strcmp (e->key, key) < 0)
      lo = mid + 1;
    else
      hi = mid;
  }
  return lo;
}


/* A word worth completing or learning: valid UTF-8 of letters, apostrophes and inner hyphens */
static gboolean
is_word (const char *word)
{
  gunichar prev = 0;

  if (word == NULL || !g_utf8_validate (word, -1, NULL) || *word == '\0')
    return FALSE;
  for (const char *p = word; *p; p = g_utf8_next_char (p)) {
    gunichar c = g_utf8_get_char (p);

    if (c == '-' || c == '\'' || c == 0x2019) {
      if (p == word || prev == '-' || prev == '\'' || prev == 0x2019)
        return FALSE;
    } else if (!g_unichar_isalpha (c) && !g_unichar_ismark (c)) {
      return FALSE;
    }
    prev = c;
  }
  return prev != '-';
}


/*
 * Reads the list at @path, in NFC as plinius-words writes it (normalizing 50000 words would
 * double the time to load). Lines that are no word, and later lines with a key seen before,
 * are passed over. Replaces a list read before; learned words stay.
 */
gboolean
pos_words_load_list (PosWords *self, const char *path, GError **error)
{
  g_autofree char *contents = NULL;
  g_auto (GStrv) lines = NULL;
  g_autoptr (GPtrArray) learned = g_ptr_array_new_with_free_func (entry_free);
  guint rank = 0;

  if (!g_file_get_contents (path, &contents, NULL, error))
    return FALSE;

  /* Keep what was learned, drop the old list */
  for (guint i = 0; i < self->entries->len; i++) {
    Entry *e = g_ptr_array_index (self->entries, i);

    if (e->learned > 0) {
      Entry *copy = g_new0 (Entry, 1);

      copy->word = g_strdup (e->word);
      copy->key = g_strdup (e->key);
      copy->rank = NOT_LISTED;
      copy->learned = e->learned;
      g_ptr_array_add (learned, copy);
    }
  }
  g_hash_table_remove_all (self->by_key);
  g_ptr_array_set_size (self->entries, 0);
  self->n_learned_only = 0;

  lines = g_strsplit (contents, "\n", -1);
  for (guint i = 0; lines[i]; i++) {
    char *word = g_strstrip (lines[i]);
    g_autofree char *key = NULL;
    Entry *e;

    if (!is_word (word))
      continue;
    key = g_utf8_casefold (word, -1);
    if (g_hash_table_contains (self->by_key, key))
      continue;
    e = g_new0 (Entry, 1);
    e->word = g_strdup (word);
    e->key = g_steal_pointer (&key);
    e->rank = rank++;
    g_ptr_array_add (self->entries, e);
    g_hash_table_insert (self->by_key, e->key, e);
  }
  g_ptr_array_sort (self->entries, compare_entries);

  for (guint i = 0; i < learned->len; i++) {
    Entry *e = g_ptr_array_index (learned, i);
    Entry *listed = g_hash_table_lookup (self->by_key, e->key);

    if (listed) {
      listed->learned = e->learned;
    } else {
      guint at = lower_bound (self, e->key);

      g_ptr_array_insert (self->entries, at, g_ptr_array_steal_index (learned, i));
      g_hash_table_insert (self->by_key, e->key, e);
      self->n_learned_only++;
      i--;
    }
  }
  return TRUE;
}


/* Adds @times to @word's count, as a word of its own if it is not in the list */
static gboolean
add_learned (PosWords *self, const char *word, guint times)
{
  g_autofree char *key = NULL;
  Entry *e;

  if (!is_word (word) || g_utf8_strlen (word, -1) < POS_WORDS_MIN_PREFIX || times == 0)
    return FALSE;

  key = fold (word);
  e = g_hash_table_lookup (self->by_key, key);
  if (e) {
    e->learned = (e->learned > G_MAXUINT - times) ? G_MAXUINT : e->learned + times;
    /* A word only the user writes: the case they wrote it in last */
    if (e->rank == NOT_LISTED) {
      g_free (e->word);
      e->word = g_utf8_normalize (word, -1, G_NORMALIZE_NFC);
    }
    return TRUE;
  }

  if (self->n_learned_only >= POS_WORDS_MAX_LEARNED) {
    Entry *least = NULL;
    guint at = 0;

    /*
     * Make room: the least typed word of the user's own. A new word always gets in (it starts
     * at one, so it could never push out a word typed twice), the words typed most stay.
     */
    for (guint i = 0; i < self->entries->len; i++) {
      Entry *c = g_ptr_array_index (self->entries, i);

      if (c->rank == NOT_LISTED && (least == NULL || c->learned < least->learned)) {
        least = c;
        at = i;
      }
    }
    g_hash_table_remove (self->by_key, least->key);
    g_ptr_array_remove_index (self->entries, at);
    self->n_learned_only--;
  }

  e = g_new0 (Entry, 1);
  e->word = g_utf8_normalize (word, -1, G_NORMALIZE_NFC);
  e->key = g_steal_pointer (&key);
  e->rank = NOT_LISTED;
  e->learned = times;
  g_ptr_array_insert (self->entries, lower_bound (self, e->key), e);
  g_hash_table_insert (self->by_key, e->key, e);
  self->n_learned_only++;
  return TRUE;
}


/* The user typed @word: returns whether anything changed (to be saved) */
gboolean
pos_words_learn (PosWords *self, const char *word)
{
  return add_learned (self, word, 1);
}


/* Reads learned words from @path; a missing file is no error */
gboolean
pos_words_load_learned (PosWords *self, const char *path, GError **error)
{
  g_autofree char *contents = NULL;
  g_autoptr (GError) err = NULL;
  g_auto (GStrv) lines = NULL;

  if (!g_file_get_contents (path, &contents, NULL, &err)) {
    if (g_error_matches (err, G_FILE_ERROR, G_FILE_ERROR_NOENT))
      return TRUE;
    g_propagate_error (error, g_steal_pointer (&err));
    return FALSE;
  }

  lines = g_strsplit (contents, "\n", -1);
  for (guint i = 0; lines[i]; i++) {
    char *tab = strchr (lines[i], '\t');
    char *end = NULL;
    guint64 times;

    if (tab == NULL)
      continue;
    *tab = '\0';
    errno = 0;
    times = g_ascii_strtoull (lines[i], &end, 10);
    if (errno || end == lines[i] || *end != '\0' || times > G_MAXUINT)
      continue;
    add_learned (self, g_strstrip (tab + 1), (guint) times);
  }
  return TRUE;
}


/* Writes the learned words to @path (mode 0600, its directory made 0700 if missing) */
gboolean
pos_words_save_learned (PosWords *self, const char *path, GError **error)
{
  g_autoptr (GString) out = g_string_new (NULL);
  g_autofree char *dir = g_path_get_dirname (path);

  for (guint i = 0; i < self->entries->len; i++) {
    Entry *e = g_ptr_array_index (self->entries, i);

    if (e->learned > 0)
      g_string_append_printf (out, "%u\t%s\n", e->learned, e->word);
  }

  if (g_mkdir_with_parents (dir, 0700) != 0) {
    int saved = errno;

    g_set_error (error, G_FILE_ERROR, g_file_error_from_errno (saved),
                 "Creating %s failed: %s", dir, g_strerror (saved));
    return FALSE;
  }
  return g_file_set_contents_full (path, out->str, out->len,
                                   G_FILE_SET_CONTENTS_CONSISTENT, 0600, error);
}


/* Forgets every learned word; the list stays */
void
pos_words_forget_learned (PosWords *self)
{
  for (guint i = 0; i < self->entries->len;) {
    Entry *e = g_ptr_array_index (self->entries, i);

    e->learned = 0;
    if (e->rank == NOT_LISTED) {
      g_hash_table_remove (self->by_key, e->key);
      g_ptr_array_remove_index (self->entries, i);
    } else {
      i++;
    }
  }
  self->n_learned_only = 0;
}


guint
pos_words_get_n_words (PosWords *self)
{
  return self->entries->len;
}


/* Whether @a comes before @b among the suggestions */
static gboolean
better (const Entry *a, const Entry *b)
{
  guint la = a->learned >= POS_WORDS_LEARN_MIN ? a->learned : 0;
  guint lb = b->learned >= POS_WORDS_LEARN_MIN ? b->learned : 0;

  if (la != lb)
    return la > lb;
  return a->rank < b->rank;
}


/* @word in the case of @prefix: a capital first letter stays one */
static char *
in_case_of (const char *prefix, const char *word)
{
  gunichar first = g_utf8_get_char (word);
  const char *rest = g_utf8_next_char (word);
  char buf[6];
  int len;

  if (!g_unichar_isupper (g_utf8_get_char (prefix)) || !g_unichar_islower (first))
    return g_strdup (word);

  len = g_unichar_to_utf8 (g_unichar_totitle (first), buf);
  return g_strdup_printf ("%.*s%s", len, buf, rest);
}


/*
 * Up to @max words that start with @prefix and are longer than it, best first. NULL for a
 * prefix shorter than POS_WORDS_MIN_PREFIX or none that is a word.
 */
GStrv
pos_words_complete (PosWords *self, const char *prefix, guint max)
{
  g_autofree char *key = NULL;
  g_autofree Entry **best = NULL;
  g_autoptr (GStrvBuilder) builder = NULL;
  guint n = 0;

  if (max == 0 || !is_word (prefix) || g_utf8_strlen (prefix, -1) < POS_WORDS_MIN_PREFIX)
    return NULL;

  key = fold (prefix);
  best = g_new0 (Entry *, max);
  for (guint i = lower_bound (self, key); i < self->entries->len; i++) {
    Entry *e = g_ptr_array_index (self->entries, i);
    guint at;

    if (!g_str_has_prefix (e->key, key))
      break;
    if (strcmp (e->key, key) == 0)
      continue;
    if (e->rank == NOT_LISTED && e->learned < POS_WORDS_LEARN_MIN)
      continue;

    /* Insert into the best ones so far */
    for (at = n; at > 0 && better (e, best[at - 1]); at--)
      if (at < max)
        best[at] = best[at - 1];
    if (at < max) {
      best[at] = e;
      if (n < max)
        n++;
    }
  }

  if (n == 0)
    return NULL;
  builder = g_strv_builder_new ();
  for (guint i = 0; i < n; i++)
    g_strv_builder_take (builder, in_case_of (prefix, best[i]->word));
  return g_strv_builder_end (builder);
}


/* The language part of a locale name ("de_DE.UTF-8" -> "de"), NULL for C and POSIX */
static char *
locale_language (const char *locale)
{
  size_t len = strcspn (locale, "_.@");

  if (len == 0 || strncmp (locale, "C", len) == 0 || strncmp (locale, "POSIX", len) == 0)
    return NULL;
  return g_ascii_strdown (locale, len);
}


static gboolean
have_list (const char *dir, const char *lang)
{
  g_autofree char *name = g_strdup_printf ("%s.words", lang);
  g_autofree char *path = g_build_filename (dir, name, NULL);

  return strchr (lang, '/') == NULL && g_file_test (path, G_FILE_TEST_IS_REGULAR);
}


/*
 * The language whose list in @dir completes for a layout of @lang and @region, NULL for none.
 * The plain US layout says nothing about the language someone writes in (it is the one for
 * most languages without letters of their own): it completes in the session's language.
 */
char *
pos_words_pick_language (const char         *dir,
                         const char         *lang,
                         const char         *region,
                         const char * const *session_languages)
{
  if (g_strcmp0 (lang, "en") == 0 && (region == NULL || g_ascii_strcasecmp (region, "us") == 0)) {
    for (guint i = 0; session_languages && session_languages[i]; i++) {
      g_autofree char *session = locale_language (session_languages[i]);

      if (session && have_list (dir, session))
        return g_steal_pointer (&session);
    }
  }
  if (lang && *lang && have_list (dir, lang))
    return g_strdup (lang);
  return NULL;
}
