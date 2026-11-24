/*
 * dsec_gcov_exit.h: force-included (-include) into a test program of a package whose fork
 * children end with _exit(): in a coverage build the child writes its gcov counters first (they
 * are lost otherwise, _exit skips atexit), elsewhere this changes nothing.
 */
#pragma once

#include <unistd.h>

extern void __gcov_dump (void) __attribute__ ((weak));

#define _exit(status) ((__gcov_dump ? __gcov_dump () : (void) 0), _exit (status))
