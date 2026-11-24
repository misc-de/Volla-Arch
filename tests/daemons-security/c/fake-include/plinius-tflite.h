/*
 * Stand-in for plinius-tflite.h (not on the build host): only what plinius-faced.c and
 * face-pipeline.h use, so the face service's decision logic compiles without the NPU stack.
 */
#pragma once

#include <glib.h>
#include <unistd.h>

typedef struct {
	const char *accelerator;
	int allow_fp16;
	const char *cache_dir;
} PlTflOptions;

static inline void
pltfl_options_init (PlTflOptions *options)
{
	options->accelerator = NULL;
	options->allow_fp16 = 0;
	options->cache_dir = NULL;
}

G_NORETURN static inline void
pltfl_exit (int status)
{
	_exit (status);
}
