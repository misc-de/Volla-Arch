/*
 * plinius-pwrate: rates the encryption password read from stdin (one line) and prints the
 * result for /var/lib/plinius-crypt/strength, which GNOME Settings shows on the Device Security
 * page. The password itself is never written anywhere.
 *
 * Output, one key=value per line:
 *   level=good|fair|weak    see pwrate.h (70 / 50 bits); weak also for a dictionary word
 *   bits=N                  the estimate, rounded down to 5 bits
 *   dictionary=checked|unchecked|word
 *                           cracklib's verdict; "unchecked" where cracklib is missing
 *                           (the initramfs, where the first password is set)
 *   rated=UNIX_TIME
 *
 * cracklib is loaded at run time (dlopen), so the same binary also works in the initramfs.
 * Exit codes: 0 rated, 2 usage error or nothing on stdin.
 *
 * SPDX-License-Identifier: MIT
 */

#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "pwrate.h"

/* Undoes common letter swaps ("Tr0ub4dor" -> "troubador") in place, so that cracklib also
 * finds the word behind them. Digits and symbols at the end ("P@ssw0rd2026!") are cut off
 * first: they are usually an appended year or "!", not swapped letters. */
static void
unleet (char *s)
{
    static const char from[] = "0134578@$!|+", to[] = "oieastbasilt";
    size_t n = strlen (s);

    while (n > 0 && !((s[n - 1] >= 'a' && s[n - 1] <= 'z') || (s[n - 1] >= 'A' && s[n - 1] <= 'Z')))
        s[--n] = '\0';
    for (; *s; s++) {
        const char *p = strchr (from, *s);
        if (p)
            *s = to[p - from];
    }
}

/* 1 if cracklib calls @password a dictionary word or too simple, 0 if not, -1 without cracklib */
static int
dictionary_word (const char *password)
{
    const char *(*fascist_check) (const char *, const char *);
    const char *(*default_dict) (void);
    const char *verdict;
    void *lib;

    lib = dlopen ("libcrack.so.2", RTLD_NOW | RTLD_LOCAL);
    if (!lib)
        return -1;
    fascist_check = (const char *(*) (const char *, const char *)) dlsym (lib, "FascistCheck");
    default_dict = (const char *(*) (void)) dlsym (lib, "GetDefaultCracklibDict");
    if (!fascist_check || !default_dict) {
        dlclose (lib);
        return -1;
    }
    verdict = fascist_check (password, default_dict ());
    if (!verdict) {
        char *plain = strdup (password);
        if (plain) {
            unleet (plain);
            verdict = fascist_check (plain, default_dict ());
            explicit_bzero (plain, strlen (plain));
            free (plain);
        }
    }
    dlclose (lib);
    return verdict != NULL;
}

int
main (int argc, char **argv)
{
    char *line = NULL;
    size_t size = 0;
    ssize_t n;
    PwRate r;
    const char *level, *dict;
    int word;

    (void) argv;
    if (argc != 1) {
        fprintf (stderr, "Usage: plinius-pwrate < password\n");
        return 2;
    }
    n = getline (&line, &size, stdin);
    if (n <= 0) {
        fprintf (stderr, "No password on stdin\n");
        return 2;
    }
    if (line[n - 1] == '\n')
        line[--n] = '\0';

    r = pwrate_estimate (line);
    word = dictionary_word (line);
    explicit_bzero (line, size);
    free (line);

    if (word == 1)
        level = "weak";
    else if (r.bits >= PWRATE_LUKS_GOOD)
        level = "good";
    else if (r.bits >= PWRATE_LUKS_FAIR)
        level = "fair";
    else
        level = "weak";
    dict = word == 1 ? "word" : word == 0 ? "checked" : "unchecked";

    printf ("level=%s\nbits=%d\ndictionary=%s\nrated=%lld\n", level, (int) r.bits / 5 * 5, dict,
            (long long) time (NULL));
    return 0;
}
