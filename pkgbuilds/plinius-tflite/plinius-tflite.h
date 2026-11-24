// SPDX-License-Identifier: MIT
// plinius-tflite: a small C interface to TensorFlow Lite that can run models on the Plinius' NPU.
//
// The NPU (MediaTek APU: MDLA and the Neuron runtime) is reached through the vendor's NNAPI
// Support Library, loaded with libhybris from /vendor. TensorFlow Lite's NNAPI delegate takes
// that library's function table directly, so no libneuralnetworks.so is involved. Operations the
// NPU cannot run stay on the CPU (XNNPACK, else TFLite's own kernels).
//
// Only the C functions below are exported; TensorFlow Lite and its dependencies are linked in
// statically and hidden, so users need neither its C++ headers nor flatbuffers of a matching
// version.
//
// Not thread-safe per model: one PlTfl must not be used from two threads at once. Different
// models may run in different threads.
#ifndef PLINIUS_TFLITE_H
#define PLINIUS_TFLITE_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct PlTfl PlTfl;

typedef enum {
	PLTFL_FLOAT32 = 1,
	PLTFL_INT32 = 2,
	PLTFL_UINT8 = 3,
	PLTFL_INT64 = 4,
	PLTFL_INT16 = 7,
	PLTFL_INT8 = 9,
	PLTFL_FLOAT16 = 10,
	/* Same numbers as TfLiteType; anything else is reported as is */
} PlTflType;

#define PLTFL_MAX_DIMS 8

typedef struct {
	PlTflType type;
	int ndims;
	int dims[PLTFL_MAX_DIMS];
	size_t bytes;
	/* Affine quantization of uint8/int8 tensors (per tensor); scale 0 if not quantized */
	float scale;
	int32_t zero_point;
	const char *name;
} PlTflTensorInfo;

typedef struct {
	/* NNAPI device for the NPU: "mtk-neuron" (float and quantized) or "mtk-mdla" (quantized
	 * only). NULL runs everything on the CPU. */
	const char *accelerator;
	/* CPU threads for the operations that stay on the CPU; 0 = TFLite's default */
	int threads;
	/* Directory for compiled NPU models (NNAPI compilation cache), NULL for none. With a cache,
	 * a model compiles once; later opens load it in a few milliseconds. */
	const char *cache_dir;
	/* Allow float32 models to run in float16 on the NPU (faster, slightly less exact) */
	int allow_fp16;
} PlTflOptions;

/* Default options: CPU only, default threads, no cache, no fp16 */
void pltfl_options_init (PlTflOptions *options);

/* Whether the NPU can be used in this process: the support library loads and lists the given
 * accelerator. Fills *error (free with free()) when not. */
int pltfl_npu_available (const char *accelerator, char **error);

/* Loads a .tflite model and prepares it. With an accelerator set, the operations the NPU supports
 * go to it; if the NPU cannot be used at all, the model runs on the CPU and pltfl_npu_nodes()
 * returns 0. Returns NULL and fills *error (free with free()) on failure. */
PlTfl *pltfl_open (const char *model_path, const PlTflOptions *options, char **error);

void pltfl_close (PlTfl *model);

int pltfl_input_count (PlTfl *model);
int pltfl_output_count (PlTfl *model);

/* Tensor data and description; inputs are written before pltfl_invoke(), outputs read after.
 * The pointers stay valid until pltfl_close(). */
void *pltfl_input (PlTfl *model, int index, PlTflTensorInfo *info);
const void *pltfl_output (PlTfl *model, int index, PlTflTensorInfo *info);

/* Runs the model; 0 on success */
int pltfl_invoke (PlTfl *model);

/* How many of the model's operations run on the NPU, and how many there are in all */
int pltfl_npu_nodes (PlTfl *model);
int pltfl_total_nodes (PlTfl *model);

/* Ends the process without running the exit handlers of the vendor libraries, which crash
 * (SIGSEGV) after the NPU was used. Use this instead of exit() once the NPU was touched. */
void pltfl_exit (int status) __attribute__ ((noreturn));

#ifdef __cplusplus
}
#endif

#endif
