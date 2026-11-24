/*
 * Host stand-in for the parts of sherpa-onnx's C API that plinius-dictated uses
 * (tests/daemons-hw). No model is loaded: the voice activity detector reports what the test
 * sets in fake_vad_speech and hands out the segments in fake_vad_segments; the recognizer
 * "recognizes" an utterance as fake_text_for(samples, n).
 */
#ifndef FAKE_SHERPA_ONNX_C_API_H
#define FAKE_SHERPA_ONNX_C_API_H

#include <stdint.h>
#include <stdlib.h>
#include <string.h>

typedef struct {
	const float *samples;
	int32_t n;
	int32_t start;
} SherpaOnnxSpeechSegment;

typedef struct {
	int unused;
} SherpaOnnxVoiceActivityDetector;
typedef struct {
	int unused;
} SherpaOnnxOfflineRecognizer;
typedef struct {
	const float *samples;
	int32_t n;
} SherpaOnnxOfflineStream;
typedef struct {
	const char *text;
} SherpaOnnxOfflineRecognizerResult;

typedef struct {
	const char *model;
	float threshold, min_silence_duration, min_speech_duration;
	int32_t window_size;
	float max_speech_duration;
} SherpaOnnxSileroVadModelConfig;

typedef struct {
	SherpaOnnxSileroVadModelConfig silero_vad;
	int32_t sample_rate, num_threads;
	const char *provider;
	int32_t debug;
} SherpaOnnxVadModelConfig;

typedef struct {
	int32_t sample_rate, feature_dim;
} SherpaOnnxFeatureConfig;

typedef struct {
	const char *encoder, *decoder, *joiner;
} SherpaOnnxOfflineTransducerModelConfig;

typedef struct {
	SherpaOnnxOfflineTransducerModelConfig transducer;
	const char *tokens;
	int32_t num_threads;
	int32_t debug;
	const char *provider, *model_type;
} SherpaOnnxOfflineModelConfig;

typedef struct {
	SherpaOnnxFeatureConfig feat_config;
	SherpaOnnxOfflineModelConfig model_config;
	const char *decoding_method;
} SherpaOnnxOfflineRecognizerConfig;

static SherpaOnnxVoiceActivityDetector fake_vad;
static SherpaOnnxOfflineRecognizer fake_recognizer;
static int fake_vad_speech;          /* what Detected() answers */
static long fake_vad_accepted;       /* samples handed to the VAD */
static float fake_vad_last[512];     /* the last window */
static SherpaOnnxSpeechSegment fake_vad_segments[8];
static int fake_vad_n_segments;
static const char *fake_text_for(const float *samples, int32_t n);

static const SherpaOnnxVoiceActivityDetector *
SherpaOnnxCreateVoiceActivityDetector(const SherpaOnnxVadModelConfig *c, float buffer_s)
{
	return &fake_vad;
}
static void SherpaOnnxVoiceActivityDetectorAcceptWaveform(const SherpaOnnxVoiceActivityDetector *v,
							   const float *samples, int32_t n)
{
	fake_vad_accepted += n;
	memcpy(fake_vad_last, samples, (n < 512 ? n : 512) * sizeof(float));
}
static int32_t SherpaOnnxVoiceActivityDetectorDetected(const SherpaOnnxVoiceActivityDetector *v)
{
	return fake_vad_speech;
}
static int32_t SherpaOnnxVoiceActivityDetectorEmpty(const SherpaOnnxVoiceActivityDetector *v)
{
	return fake_vad_n_segments == 0;
}
static const SherpaOnnxSpeechSegment *SherpaOnnxVoiceActivityDetectorFront(const SherpaOnnxVoiceActivityDetector *v)
{
	SherpaOnnxSpeechSegment *s = malloc(sizeof *s);
	*s = fake_vad_segments[0];
	return s;
}
static void SherpaOnnxVoiceActivityDetectorPop(const SherpaOnnxVoiceActivityDetector *v)
{
	memmove(fake_vad_segments, fake_vad_segments + 1, sizeof fake_vad_segments - sizeof fake_vad_segments[0]);
	fake_vad_n_segments--;
}
static int fake_vad_flushed, fake_vad_reset;
static void SherpaOnnxVoiceActivityDetectorFlush(const SherpaOnnxVoiceActivityDetector *v)
{
	fake_vad_flushed++;
}
static void SherpaOnnxVoiceActivityDetectorReset(const SherpaOnnxVoiceActivityDetector *v)
{
	fake_vad_reset++;
}
static void SherpaOnnxDestroySpeechSegment(const SherpaOnnxSpeechSegment *s)
{
	free((void *)s);
}

static const SherpaOnnxOfflineRecognizer *SherpaOnnxCreateOfflineRecognizer(const SherpaOnnxOfflineRecognizerConfig *c)
{
	return &fake_recognizer;
}
static const SherpaOnnxOfflineStream *SherpaOnnxCreateOfflineStream(const SherpaOnnxOfflineRecognizer *r)
{
	return calloc(1, sizeof(SherpaOnnxOfflineStream));
}
static void SherpaOnnxAcceptWaveformOffline(const SherpaOnnxOfflineStream *s, int32_t rate, const float *samples,
					    int32_t n)
{
	((SherpaOnnxOfflineStream *)s)->samples = samples;
	((SherpaOnnxOfflineStream *)s)->n = n;
}
static void SherpaOnnxDecodeOfflineStream(const SherpaOnnxOfflineRecognizer *r, const SherpaOnnxOfflineStream *s) {}
static const SherpaOnnxOfflineRecognizerResult *SherpaOnnxGetOfflineStreamResult(const SherpaOnnxOfflineStream *s)
{
	SherpaOnnxOfflineRecognizerResult *r = malloc(sizeof *r);
	r->text = fake_text_for(s->samples, s->n);
	return r;
}
static void SherpaOnnxDestroyOfflineRecognizerResult(const SherpaOnnxOfflineRecognizerResult *r)
{
	free((void *)r);
}
static void SherpaOnnxDestroyOfflineStream(const SherpaOnnxOfflineStream *s)
{
	free((void *)s);
}

#endif
