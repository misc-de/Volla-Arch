/*
 * Harness for plinius-pwrate.c, the tool as it is, with the cracklib it loads at run time taken
 * from DSEC_LIBCRACK (a path; "none": cracklib is missing, as in the initramfs) instead of
 * libcrack.so.2. Unset: the host's cracklib.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdlib.h>
#include <string.h>

static void *
dsec_dlopen (const char *name, int flags)
{
    const char *lib = getenv ("DSEC_LIBCRACK");

    if (lib && strcmp (name, "libcrack.so.2") == 0)
        return strcmp (lib, "none") ? dlopen (lib, flags) : NULL;
    return dlopen (name, flags);
}

#define dlopen dsec_dlopen

#include "plinius-security/plinius-pwrate.c"
