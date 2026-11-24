/* Host stand-in for Android's hardware/audio.h (legacy audio HAL), as far as audio-hal-bridge
 * uses it (tests/daemons-hw). Values as in AOSP system/audio.h. */
#ifndef FAKE_HARDWARE_AUDIO_H
#define FAKE_HARDWARE_AUDIO_H
#include <stddef.h>
#include <stdint.h>
#include <sys/types.h>
#include "hardware.h"

typedef uint32_t audio_devices_t;
typedef uint32_t audio_channel_mask_t;
typedef uint32_t audio_format_t;
typedef int audio_io_handle_t;
typedef uint32_t audio_output_flags_t;
typedef uint32_t audio_input_flags_t;
typedef int audio_source_t;
typedef int audio_mode_t;

#define AUDIO_HARDWARE_MODULE_ID "audio"
#define AUDIO_HARDWARE_MODULE_ID_PRIMARY "primary"
#define AUDIO_HARDWARE_INTERFACE "audio_hw_if"
#define AUDIO_PARAMETER_STREAM_ROUTING "routing"

enum {
	AUDIO_DEVICE_OUT_EARPIECE = 0x1,
	AUDIO_DEVICE_OUT_SPEAKER = 0x2,
	AUDIO_DEVICE_OUT_WIRED_HEADSET = 0x4,
	AUDIO_DEVICE_OUT_WIRED_HEADPHONE = 0x8,
	AUDIO_DEVICE_OUT_BLUETOOTH_SCO = 0x10,
	AUDIO_DEVICE_IN_BUILTIN_MIC = 0x80000004u,
	AUDIO_DEVICE_IN_BLUETOOTH_SCO_HEADSET = 0x80000008u,
	AUDIO_DEVICE_IN_WIRED_HEADSET = 0x80000010u,
	AUDIO_DEVICE_IN_FM_TUNER = 0x80002000u,
};
enum { AUDIO_CHANNEL_OUT_STEREO = 0x3, AUDIO_CHANNEL_IN_MONO = 0x10, AUDIO_CHANNEL_IN_STEREO = 0xc };
enum { AUDIO_FORMAT_PCM_16_BIT = 0x1 };
enum { AUDIO_OUTPUT_FLAG_PRIMARY = 0x2 };
enum { AUDIO_INPUT_FLAG_NONE = 0 };
enum { AUDIO_SOURCE_MIC = 1, AUDIO_SOURCE_FM_TUNER = 1998 };
enum { AUDIO_MODE_NORMAL = 0, AUDIO_MODE_IN_CALL = 2 };

static inline uint32_t audio_channel_count_from_in_mask(audio_channel_mask_t mask)
{
	return __builtin_popcount(mask);
}

struct audio_config {
	uint32_t sample_rate;
	audio_channel_mask_t channel_mask;
	audio_format_t format;
};

struct audio_stream {
	size_t (*get_buffer_size)(const struct audio_stream *stream);
	int (*set_parameters)(struct audio_stream *stream, const char *kv_pairs);
	int (*standby)(struct audio_stream *stream);
};

struct audio_stream_out {
	struct audio_stream common;
	uint32_t (*get_latency)(const struct audio_stream_out *stream);
	ssize_t (*write)(struct audio_stream_out *stream, const void *buffer, size_t bytes);
};

struct audio_stream_in {
	struct audio_stream common;
	ssize_t (*read)(struct audio_stream_in *stream, void *buffer, size_t bytes);
};

struct audio_hw_device {
	struct hw_device_t common;
	int (*init_check)(const struct audio_hw_device *dev);
	int (*set_voice_volume)(struct audio_hw_device *dev, float volume);
	int (*set_mode)(struct audio_hw_device *dev, audio_mode_t mode);
	int (*set_mic_mute)(struct audio_hw_device *dev, int state);
	int (*set_parameters)(struct audio_hw_device *dev, const char *kv_pairs);
	int (*open_output_stream)(struct audio_hw_device *dev, audio_io_handle_t handle, audio_devices_t devices,
				  audio_output_flags_t flags, struct audio_config *config,
				  struct audio_stream_out **stream_out, const char *address);
	void (*close_output_stream)(struct audio_hw_device *dev, struct audio_stream_out *stream_out);
	int (*open_input_stream)(struct audio_hw_device *dev, audio_io_handle_t handle, audio_devices_t devices,
				 struct audio_config *config, struct audio_stream_in **stream_in,
				 audio_input_flags_t flags, const char *address, audio_source_t source);
	void (*close_input_stream)(struct audio_hw_device *dev, struct audio_stream_in *stream_in);
};
#endif
