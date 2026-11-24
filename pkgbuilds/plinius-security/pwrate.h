/*
 * pwrate.h: how hard a password is to guess, as an estimate in bits. Shared by
 * plinius-pwrate (the encryption password) and pam_plinius_secstate.so (the screen lock PIN).
 *
 * The estimate is deliberately simple and errs on the low side:
 *   bits = effective length * log2(size of the character pool)
 * The pool adds up the classes that occur: digits 10, lower case 26, upper case 26, ASCII
 * symbols and space 33, other characters (umlauts, Cyrillic, ...) 64. A character that repeats
 * the previous one, or continues a run like "abc" or "321", adds only a quarter.
 * Dictionary words are left to cracklib (plinius-pwrate), which is not part of this header.
 *
 * Levels (GNOME Settings, Device Security):
 *   encryption password: good from 70 bits, fair from 50 bits, below that weak. Its LUKS header
 *     can be attacked offline without any wait, so it has to withstand that on its own.
 *   screen lock: good from the strength of a 6-digit PIN (19.9 bits): the lock screen makes
 *     every wrong try wait longer (plinius-pinguard), a short secret is enough there.
 *
 * SPDX-License-Identifier: MIT
 */

#ifndef PWRATE_H
#define PWRATE_H

#include <math.h>
#include <stdint.h>
#include <string.h>

#define PWRATE_LUKS_GOOD 70.0
#define PWRATE_LUKS_FAIR 50.0
/* 6 * log2(10), slightly below so that six digits reach it */
#define PWRATE_LOCK_GOOD 19.9

typedef struct {
    double bits;
    unsigned length;     /* characters, not bytes */
    int    digits_only;
} PwRate;

/* Decodes the next UTF-8 character of *s (advancing it); invalid bytes count one each */
static inline uint32_t
pwrate_next_char (const unsigned char **s)
{
    const unsigned char *p = *s;
    uint32_t c = *p;
    int extra = c >= 0xf0 ? 3 : c >= 0xe0 ? 2 : c >= 0xc0 ? 1 : 0;

    if (extra == 0 || c < 0xc0) {
        *s = p + 1;
        return c;
    }
    c &= 0x3f >> extra;
    for (int i = 1; i <= extra; i++) {
        if ((p[i] & 0xc0) != 0x80) {
            *s = p + 1;
            return *p;
        }
        c = c << 6 | (p[i] & 0x3f);
    }
    *s = p + extra + 1;
    return c;
}

static inline PwRate
pwrate_estimate (const char *password)
{
    const unsigned char *s = (const unsigned char *) password;
    int digit = 0, lower = 0, upper = 0, symbol = 0, other = 0;
    uint32_t prev = 0;
    int step = 0, have_prev = 0;
    double effective = 0;
    unsigned pool;
    PwRate r = { 0, 0, 1 };

    while (*s) {
        uint32_t c = pwrate_next_char (&s);
        int this_step = have_prev ? (int) c - (int) prev : 0;

        if (c >= '0' && c <= '9')
            digit = 1;
        else if (c >= 'a' && c <= 'z')
            lower = 1;
        else if (c >= 'A' && c <= 'Z')
            upper = 1;
        else if (c >= 0x20 && c < 0x7f)
            symbol = 1;
        else
            other = 1;
        if (!(c >= '0' && c <= '9'))
            r.digits_only = 0;

        /* Repeats ("aaa") and runs ("abc", "987") add little */
        if (have_prev && (this_step == 0 || ((this_step == 1 || this_step == -1) && this_step == step)))
            effective += 0.25;
        else
            effective += 1;
        step = this_step;
        prev = c;
        have_prev = 1;
        r.length++;
    }
    pool = digit * 10 + lower * 26 + upper * 26 + symbol * 33 + other * 64;
    if (r.length == 0) {
        r.digits_only = 0;
        return r;
    }
    r.bits = pool > 1 ? effective * log2 ((double) pool) : 0;
    return r;
}

#endif
