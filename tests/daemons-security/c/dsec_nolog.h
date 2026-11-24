/*
 * dsec_nolog.h: force-included (-include) into daemons under test, so that their syslog lines go
 * to stderr instead of the host's journal. syslog.h is included first; its guard keeps the
 * daemon's own #include from undoing the macros.
 */
#pragma once

/* Comes before the daemon's own first line: the headers below must see it already */
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include <stdarg.h>
#include <stdio.h>
#include <syslog.h>

static inline void
dsec_syslog (int priority, const char *fmt, ...)
{
    va_list args;

    (void) priority;
    va_start (args, fmt);
    fputs ("SYSLOG: ", stderr);
    vfprintf (stderr, fmt, args);
    fputc ('\n', stderr);
    va_end (args);
}

#define syslog(priority, ...) dsec_syslog (priority, __VA_ARGS__)
#define openlog(ident, option, facility) ((void) 0)
