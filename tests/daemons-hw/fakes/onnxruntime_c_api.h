/*
 * Host stand-in for the parts of ONNX Runtime's C API that plinius-speechd uses
 * (tests/daemons-hw/test_dhw_speech.py). No model runs: a session opens unless its model path
 * contains "broken"; Run() returns FAKE_ORT_SAMPLES_PER_ID samples per input id, all of the value
 * fake_ort_value (or the run number), after fake_ort_delay_ms, and fails once RunOptionsSetTerminate
 * was called. What it was given (ids, scales, threads) is recorded.
 */
#ifndef FAKE_ONNXRUNTIME_C_API_H
#define FAKE_ONNXRUNTIME_C_API_H

#include <glib.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define ORT_API_VERSION 32
#define FAKE_ORT_SAMPLES_PER_ID 10

typedef enum { ORT_LOGGING_LEVEL_WARNING = 2 } OrtLoggingLevel;
typedef enum { ORT_ENABLE_ALL = 99 } GraphOptimizationLevel;
typedef enum { OrtArenaAllocator = 1 } OrtAllocatorType;
typedef enum { OrtMemTypeDefault = 0 } OrtMemType;
typedef enum { ONNX_TENSOR_ELEMENT_DATA_TYPE_FLOAT = 1, ONNX_TENSOR_ELEMENT_DATA_TYPE_INT64 = 7 } ONNXTensorElementDataType;

typedef struct { char *message; } OrtStatus;
typedef struct { int unused; } OrtEnv;
typedef struct { int threads, level; } OrtSessionOptions;
typedef struct { char *path; } OrtSession;
typedef struct { int unused; } OrtMemoryInfo;
typedef struct { volatile int terminate; } OrtRunOptions;
typedef struct { void *data; size_t bytes, count; int owned; } OrtValue;
typedef struct { size_t count; } OrtTensorTypeAndShapeInfo;

static int fake_ort_sessions, fake_ort_released, fake_ort_threads, fake_ort_runs, fake_ort_values;
static int fake_ort_fail_env, fake_ort_fail_memory, fake_ort_no_api, fake_ort_fail_tensor, fake_ort_fail_run;
static int fake_ort_empty;            /* Run returns no samples */
static int fake_ort_delay_ms;
static float fake_ort_value;          /* 0: the run number */
static float fake_ort_scales[3];
static GString *fake_ort_ids;         /* "id,id,...;" per run */
static char *fake_ort_model;

static OrtStatus *fake_status(const char *m)
{
	OrtStatus *s = calloc(1, sizeof *s);
	s->message = strdup(m);
	return s;
}

static OrtStatus *f_CreateEnv(OrtLoggingLevel l, const char *id, OrtEnv **out)
{
	if (fake_ort_fail_env)
		return fake_status("no env");
	*out = calloc(1, sizeof **out);
	return NULL;
}
static OrtStatus *f_CreateCpuMemoryInfo(OrtAllocatorType t, OrtMemType m, OrtMemoryInfo **out)
{
	if (fake_ort_fail_memory)
		return fake_status("no memory info");
	*out = calloc(1, sizeof **out);
	return NULL;
}
static OrtStatus *f_CreateSessionOptions(OrtSessionOptions **out)
{
	*out = calloc(1, sizeof **out);
	return NULL;
}
static OrtStatus *f_SetIntraOpNumThreads(OrtSessionOptions *o, int n)
{
	o->threads = n;
	fake_ort_threads = n;
	return NULL;
}
static OrtStatus *f_SetSessionGraphOptimizationLevel(OrtSessionOptions *o, GraphOptimizationLevel l)
{
	o->level = l;
	return NULL;
}
static OrtStatus *f_CreateSession(const OrtEnv *e, const char *path, const OrtSessionOptions *o, OrtSession **out)
{
	g_free(fake_ort_model);
	fake_ort_model = g_path_get_basename(path);
	if (strstr(path, "broken"))
		return fake_status("not a model");
	fake_ort_sessions++;
	*out = calloc(1, sizeof **out);
	(*out)->path = strdup(path);
	return NULL;
}
static OrtStatus *f_CreateTensorWithDataAsOrtValue(const OrtMemoryInfo *i, void *data, size_t bytes,
						   const int64_t *shape, size_t ndim, ONNXTensorElementDataType t,
						   OrtValue **out)
{
	if (fake_ort_fail_tensor)
		return fake_status("no tensor");
	*out = calloc(1, sizeof **out);
	(*out)->data = data;
	(*out)->bytes = bytes;
	fake_ort_values++;
	return NULL;
}
static OrtStatus *f_CreateRunOptions(OrtRunOptions **out)
{
	*out = calloc(1, sizeof **out);
	return NULL;
}
static OrtStatus *f_RunOptionsSetTerminate(OrtRunOptions *o)
{
	o->terminate = 1;
	return NULL;
}
static OrtStatus *f_Run(OrtSession *s, const OrtRunOptions *ro, const char *const *in_names, const OrtValue *const *in,
			size_t n_in, const char *const *out_names, size_t n_out, OrtValue **out)
{
	if (!fake_ort_ids)
		fake_ort_ids = g_string_new(NULL);
	size_t n = in[0]->bytes / sizeof(int64_t);
	for (size_t i = 0; i < n; i++)
		g_string_append_printf(fake_ort_ids, "%s%ld", i ? "," : "", (long)((int64_t *)in[0]->data)[i]);
	g_string_append_c(fake_ort_ids, ';');
	memcpy(fake_ort_scales, in[2]->data, sizeof fake_ort_scales);
	for (int k = 0; k < fake_ort_delay_ms / 10 && !(ro && ro->terminate); k++)
		g_usleep(10000);
	if (ro && ro->terminate)
		return fake_status("terminated");
	if (fake_ort_fail_run)
		return fake_status("run failed");
	fake_ort_runs++;
	size_t m = fake_ort_empty ? 0 : n * FAKE_ORT_SAMPLES_PER_ID;
	float *f = calloc(m ? m : 1, sizeof(float));
	for (size_t i = 0; i < m; i++)
		f[i] = fake_ort_value ? fake_ort_value : (float)fake_ort_runs;
	*out = calloc(1, sizeof **out);
	(*out)->data = f;
	(*out)->count = m;
	(*out)->owned = 1;
	fake_ort_values++;
	return NULL;
}
static OrtStatus *f_GetTensorTypeAndShape(const OrtValue *v, OrtTensorTypeAndShapeInfo **out)
{
	*out = calloc(1, sizeof **out);
	(*out)->count = v->count;
	return NULL;
}
static OrtStatus *f_GetTensorShapeElementCount(const OrtTensorTypeAndShapeInfo *i, size_t *out)
{
	*out = i->count;
	return NULL;
}
static OrtStatus *f_GetTensorMutableData(OrtValue *v, void **out)
{
	*out = v->data;
	return NULL;
}
static const char *f_GetErrorMessage(const OrtStatus *s)
{
	return s->message;
}
static void f_ReleaseStatus(OrtStatus *s)
{
	free(s->message);
	free(s);
}
static void f_ReleaseValue(OrtValue *v)
{
	if (v->owned)
		free(v->data);
	free(v);
	fake_ort_values--;
}
static void f_ReleaseSession(OrtSession *s)
{
	fake_ort_released++;
	free(s->path);
	free(s);
}
static void f_ReleaseSessionOptions(OrtSessionOptions *o) { free(o); }
static void f_ReleaseRunOptions(OrtRunOptions *o) { free(o); }
static void f_ReleaseTensorTypeAndShapeInfo(OrtTensorTypeAndShapeInfo *i) { free(i); }

typedef struct {
	OrtStatus *(*CreateEnv)(OrtLoggingLevel, const char *, OrtEnv **);
	OrtStatus *(*CreateCpuMemoryInfo)(OrtAllocatorType, OrtMemType, OrtMemoryInfo **);
	OrtStatus *(*CreateSessionOptions)(OrtSessionOptions **);
	OrtStatus *(*SetIntraOpNumThreads)(OrtSessionOptions *, int);
	OrtStatus *(*SetSessionGraphOptimizationLevel)(OrtSessionOptions *, GraphOptimizationLevel);
	OrtStatus *(*CreateSession)(const OrtEnv *, const char *, const OrtSessionOptions *, OrtSession **);
	OrtStatus *(*CreateTensorWithDataAsOrtValue)(const OrtMemoryInfo *, void *, size_t, const int64_t *, size_t,
						     ONNXTensorElementDataType, OrtValue **);
	OrtStatus *(*CreateRunOptions)(OrtRunOptions **);
	OrtStatus *(*RunOptionsSetTerminate)(OrtRunOptions *);
	OrtStatus *(*Run)(OrtSession *, const OrtRunOptions *, const char *const *, const OrtValue *const *, size_t,
			  const char *const *, size_t, OrtValue **);
	OrtStatus *(*GetTensorTypeAndShape)(const OrtValue *, OrtTensorTypeAndShapeInfo **);
	OrtStatus *(*GetTensorShapeElementCount)(const OrtTensorTypeAndShapeInfo *, size_t *);
	OrtStatus *(*GetTensorMutableData)(OrtValue *, void **);
	const char *(*GetErrorMessage)(const OrtStatus *);
	void (*ReleaseStatus)(OrtStatus *);
	void (*ReleaseValue)(OrtValue *);
	void (*ReleaseSession)(OrtSession *);
	void (*ReleaseSessionOptions)(OrtSessionOptions *);
	void (*ReleaseRunOptions)(OrtRunOptions *);
	void (*ReleaseTensorTypeAndShapeInfo)(OrtTensorTypeAndShapeInfo *);
} OrtApi;

static const OrtApi fake_ort_api = {
	f_CreateEnv, f_CreateCpuMemoryInfo, f_CreateSessionOptions, f_SetIntraOpNumThreads,
	f_SetSessionGraphOptimizationLevel, f_CreateSession, f_CreateTensorWithDataAsOrtValue, f_CreateRunOptions,
	f_RunOptionsSetTerminate, f_Run, f_GetTensorTypeAndShape, f_GetTensorShapeElementCount, f_GetTensorMutableData,
	f_GetErrorMessage, f_ReleaseStatus, f_ReleaseValue, f_ReleaseSession, f_ReleaseSessionOptions,
	f_ReleaseRunOptions, f_ReleaseTensorTypeAndShapeInfo,
};

typedef struct {
	const OrtApi *(*GetApi)(uint32_t version);
} OrtApiBase;

static const OrtApi *fake_get_api(uint32_t version)
{
	return fake_ort_no_api || version != ORT_API_VERSION ? NULL : &fake_ort_api;
}
static const OrtApiBase fake_ort_base = {fake_get_api};
static const OrtApiBase *OrtGetApiBase(void)
{
	return &fake_ort_base;
}

#endif
