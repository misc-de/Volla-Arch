/*
 * dsec_fakepam.h: the few libpam calls our PAM modules make, as fakes, so a module's
 * pam_sm_*() can be called from a harness without a PAM stack (and without libpam). What the
 * module tells the user (pam_error), logs (pam_syslog) and the fail delay are printed to stdout
 * as "msg ...", "log ..." and "delay ..." lines.
 *
 * Driven by dsec_pam_main() from argv:
 *   HARNESS USER AUTHTOK_HEX FLAGS [MODULE ARGS...]
 * USER "-": pam_get_user fails; AUTHTOK_HEX "-": there is no token, "=": pam_get_authtok
 * succeeds without one (a NULL token).
 */
#pragma once

#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <security/pam_ext.h>
#include <security/pam_modules.h>
#include <security/pam_modutil.h>

static const char *dsec_user;
static char *dsec_authtok;
static int dsec_null_authtok_ok;

int
pam_get_user (pam_handle_t *pamh, const char **user, const char *prompt)
{
    (void) pamh;
    (void) prompt;
    if (!dsec_user)
        return PAM_USER_UNKNOWN;
    *user = dsec_user;
    return PAM_SUCCESS;
}

int
pam_get_authtok (pam_handle_t *pamh, int item, const char **authtok, const char *prompt)
{
    (void) pamh;
    (void) prompt;
    if (item != PAM_AUTHTOK)
        return PAM_SYSTEM_ERR;
    *authtok = dsec_authtok;
    return dsec_authtok || dsec_null_authtok_ok ? PAM_SUCCESS : PAM_AUTH_ERR;
}

int
pam_get_item (const pam_handle_t *pamh, int item_type, const void **item)
{
    (void) pamh;
    if (item_type != PAM_AUTHTOK)
        return PAM_BAD_ITEM;
    *item = dsec_authtok;
    return PAM_SUCCESS;
}

int
pam_fail_delay (pam_handle_t *pamh, unsigned int usec)
{
    (void) pamh;
    printf ("delay %u\n", usec);
    return PAM_SUCCESS;
}

void
pam_syslog (const pam_handle_t *pamh, int priority, const char *fmt, ...)
{
    va_list args;

    (void) pamh;
    (void) priority;
    va_start (args, fmt);
    fputs ("log ", stdout);
    vprintf (fmt, args);
    putchar ('\n');
    va_end (args);
}

int
pam_prompt (pam_handle_t *pamh, int style, char **response, const char *fmt, ...)
{
    va_list args;

    (void) pamh;
    (void) response;
    va_start (args, fmt);
    fputs (style == PAM_ERROR_MSG ? "msg " : "info ", stdout);
    vprintf (fmt, args);
    putchar ('\n');
    va_end (args);
    return PAM_SUCCESS;
}

static const char *
dsec_pam_rc_name (int rc)
{
    switch (rc) {
    case PAM_SUCCESS: return "SUCCESS";
    case PAM_AUTH_ERR: return "AUTH_ERR";
    case PAM_AUTHINFO_UNAVAIL: return "AUTHINFO_UNAVAIL";
    case PAM_MAXTRIES: return "MAXTRIES";
    case PAM_PERM_DENIED: return "PERM_DENIED";
    case PAM_IGNORE: return "IGNORE";
    case PAM_BUF_ERR: return "BUF_ERR";
    case PAM_USER_UNKNOWN: return "USER_UNKNOWN";
    case PAM_AUTHTOK_ERR: return "AUTHTOK_ERR";
    default: return "OTHER";
    }
}

static char *
dsec_unhex (const char *hex)
{
    size_t n = strlen (hex) / 2;
    char *out = calloc (1, n + 1);

    for (size_t i = 0; i < n; i++) {
        unsigned v;

        sscanf (hex + 2 * i, "%2x", &v);
        out[i] = (char) v;
    }
    return out;
}

typedef int (*DsecPamFunc) (pam_handle_t *pamh, int flags, int argc, const char **argv);

static int
dsec_pam_main (int argc, char **argv, DsecPamFunc func)
{
    static int handle;
    int rc;

    setvbuf (stdout, NULL, _IOLBF, 0);
    if (argc < 4) {
        fprintf (stderr, "usage: %s USER AUTHTOK_HEX FLAGS [ARGS...]\n", argv[0]);
        return 2;
    }
    dsec_user = strcmp (argv[1], "-") ? argv[1] : NULL;
    dsec_null_authtok_ok = strcmp (argv[2], "=") == 0;
    dsec_authtok = strcmp (argv[2], "-") && !dsec_null_authtok_ok ? dsec_unhex (argv[2]) : NULL;
    rc = func ((pam_handle_t *) &handle, atoi (argv[3]), argc - 4, (const char **) argv + 4);
    printf ("rc %s\n", dsec_pam_rc_name (rc));
    return 0;
}
