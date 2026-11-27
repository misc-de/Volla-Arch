/*
 * Word completion from a frequency list (plinius-words) and the words the user types.
 * Copied to src/completers/ by prepare(); pos-completer-words.c (0015) wraps it for Stevia.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#pragma once

#include <glib.h>

G_BEGIN_DECLS

/* Suggestions start with this many characters typed */
#define POS_WORDS_MIN_PREFIX 3
/* A word that is not in the list is offered once it was typed this often */
#define POS_WORDS_LEARN_MIN 2
/* At most this many words that are not in the list are kept */
#define POS_WORDS_MAX_LEARNED 5000

typedef struct _PosWords PosWords;

PosWords   *pos_words_new            (void);
void        pos_words_free           (PosWords    *self);
gboolean    pos_words_load_list      (PosWords    *self,
                                      const char  *path,
                                      GError     **error);
gboolean    pos_words_load_learned   (PosWords    *self,
                                      const char  *path,
                                      GError     **error);
gboolean    pos_words_save_learned   (PosWords    *self,
                                      const char  *path,
                                      GError     **error);
gboolean    pos_words_learn          (PosWords    *self,
                                      const char  *word);
void        pos_words_forget_learned (PosWords    *self);
guint       pos_words_get_n_words    (PosWords    *self);
GStrv       pos_words_complete       (PosWords    *self,
                                      const char  *prefix,
                                      guint        max);
char       *pos_words_pick_language  (const char         *dir,
                                      const char         *lang,
                                      const char         *region,
                                      const char * const *session_languages);

G_DEFINE_AUTOPTR_CLEANUP_FUNC (PosWords, pos_words_free)

G_END_DECLS
