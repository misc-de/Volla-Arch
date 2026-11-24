// SPDX-License-Identifier: MIT
// plinius-tflite-bench: loads a .tflite model through plinius-tflite, on the CPU or the NPU, and
// times it with random input. Shows the tensors and how many operations the NPU took.
//
// Usage: plinius-tflite-bench [-a accelerator] [-t threads] [-n runs] [-c cache_dir] [-f] model
//   -a mtk-neuron | mtk-mdla   NNAPI device of the NPU (default: CPU only)
//   -f                         allow float16 on the NPU for float32 models
#include "plinius-tflite.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static double
now_ms (void)
{
	struct timespec ts;

	clock_gettime (CLOCK_MONOTONIC, &ts);
	return ts.tv_sec * 1e3 + ts.tv_nsec / 1e6;
}

static const char *
type_name (PlTflType type)
{
	switch (type) {
	case PLTFL_FLOAT32: return "float32";
	case PLTFL_INT32: return "int32";
	case PLTFL_UINT8: return "uint8";
	case PLTFL_INT64: return "int64";
	case PLTFL_INT16: return "int16";
	case PLTFL_INT8: return "int8";
	case PLTFL_FLOAT16: return "float16";
	default: return "other";
	}
}

static void
print_tensor (const char *kind, int index, const PlTflTensorInfo *info)
{
	printf ("  %s %d: %s %s [", kind, index, info->name ? info->name : "?", type_name (info->type));
	for (int i = 0; i < info->ndims; i++)
		printf ("%s%d", i ? "," : "", info->dims[i]);
	printf ("]");
	if (info->scale != 0)
		printf (" scale %g zero %d", info->scale, info->zero_point);
	printf ("\n");
}

int
main (int argc, char **argv)
{
	PlTflOptions options;
	PlTflTensorInfo info;
	double start, first, sum = 0, best = 1e9;
	char *error = NULL;
	int runs = 50, opt;
	PlTfl *model;

	setvbuf (stdout, NULL, _IONBF, 0);
	pltfl_options_init (&options);
	while ((opt = getopt (argc, argv, "a:t:n:c:f")) != -1) {
		switch (opt) {
		case 'a': options.accelerator = optarg; break;
		case 't': options.threads = atoi (optarg); break;
		case 'n': runs = atoi (optarg); break;
		case 'c': options.cache_dir = optarg; break;
		case 'f': options.allow_fp16 = 1; break;
		default:
			fprintf (stderr, "usage: %s [-a accelerator] [-t threads] [-n runs] "
				 "[-c cache_dir] [-f] model.tflite\n", argv[0]);
			return 2;
		}
	}
	if (optind >= argc) {
		fprintf (stderr, "no model given\n");
		return 2;
	}

	start = now_ms ();
	model = pltfl_open (argv[optind], &options, &error);
	if (!model) {
		fprintf (stderr, "%s\n", error);
		pltfl_exit (1);
	}
	printf ("opened in %.1f ms, %d of %d operations on the NPU%s%s\n", now_ms () - start,
		pltfl_npu_nodes (model), pltfl_total_nodes (model),
		options.accelerator ? " " : "", options.accelerator ? options.accelerator : "");

	srand (1);
	for (int i = 0; i < pltfl_input_count (model); i++) {
		unsigned char *data = pltfl_input (model, i, &info);

		print_tensor ("input", i, &info);
		if (info.type == PLTFL_FLOAT32) {
			for (size_t j = 0; j < info.bytes / sizeof (float); j++)
				((float *) data)[j] = (float) rand () / RAND_MAX;
		} else {
			for (size_t j = 0; j < info.bytes; j++)
				data[j] = (unsigned char) rand ();
		}
	}
	for (int i = 0; i < pltfl_output_count (model); i++) {
		pltfl_output (model, i, &info);
		print_tensor ("output", i, &info);
	}

	start = now_ms ();
	if (pltfl_invoke (model) != 0) {
		fprintf (stderr, "invoke failed\n");
		pltfl_exit (1);
	}
	first = now_ms () - start;
	for (int i = 0; i < runs; i++) {
		double t;

		start = now_ms ();
		if (pltfl_invoke (model) != 0) {
			fprintf (stderr, "invoke %d failed\n", i);
			pltfl_exit (1);
		}
		t = now_ms () - start;
		sum += t;
		if (t < best)
			best = t;
	}
	printf ("first run %.2f ms, then %d runs: mean %.2f ms, best %.2f ms\n", first, runs,
		runs ? sum / runs : 0, runs ? best : 0);

	pltfl_close (model);
	pltfl_exit (0);
}
