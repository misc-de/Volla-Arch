/*
 * A stand-in for libcrack.so.2 (for dsec_pwrate_harness.c): FascistCheck() calls a word a
 * dictionary word if it is one of DSEC_CRACK_WORDS (comma-separated, compared case-insensitively),
 * and records every word it was asked about in DSEC_CRACK_LOG. Built with -DNO_DICT_FUNC it lacks
 * GetDefaultCracklibDict, like a broken library.
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>

const char *
FascistCheck (const char *password, const char *dict)
{
    const char *words = getenv ("DSEC_CRACK_WORDS"), *log = getenv ("DSEC_CRACK_LOG");
    char *copy, *save = NULL;
    const char *verdict = NULL;

    if (log) {
        FILE *f = fopen (log, "a");

        if (f) {
            fprintf (f, "%s %s\n", password, dict);
            fclose (f);
        }
    }
    if (!words)
        return NULL;
    copy = strdup (words);
    for (char *w = strtok_r (copy, ",", &save); w; w = strtok_r (NULL, ",", &save))
        if (strcasecmp (w, password) == 0)
            verdict = "it is based on a dictionary word";
    free (copy);
    return verdict;
}

#ifndef NO_DICT_FUNC
const char *
GetDefaultCracklibDict (void)
{
    return "/fake/pw_dict";
}
#endif
