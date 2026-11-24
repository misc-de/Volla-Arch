// SPDX-License-Identifier: MIT
// Echo cancellation for audio-hal-bridge: WebRTC's audio processing module (AEC3, the echo
// canceller of the browsers and PipeWire's echo-cancel module). The bridge feeds the render and
// the capture side from its capture thread, one block each.
#include <api/audio/echo_canceller3_config.h>
#include <api/audio/echo_control.h>
#include <api/scoped_refptr.h>
#include <modules/audio_processing/include/audio_processing.h>

#include <memory>
#include <new>

#include "aec.h"

// AEC3 with a suppressor of our own. The library builds AEC3 from a config only through a
// factory, and neither the factory nor the class is public; the class's constructor is
// exported, so the object is built in memory of ample size (its layout is not known here) and
// handed over as the EchoControl it derives from. With the default config the output is bit
// for bit the library's own AEC3 (checked).
extern void aec3_construct(void *self, const webrtc::EchoCanceller3Config &config, int rate, size_t render,
			   size_t capture) __asm__("_ZN6webrtc14EchoCanceller3C1ERKNS_20EchoCanceller3ConfigEimm");

namespace {
class Aec3Factory : public webrtc::EchoControlFactory {
public:
	explicit Aec3Factory(const webrtc::EchoCanceller3Config &config) : config_(config) {}
	std::unique_ptr<webrtc::EchoControl> Create(int rate, int render, int capture) override
	{
		void *mem = ::operator new(64 * 1024);
		aec3_construct(mem, config_, rate, render, capture);
		return std::unique_ptr<webrtc::EchoControl>(static_cast<webrtc::EchoControl *>(mem));
	}

private:
	webrtc::EchoCanceller3Config config_;
};

/* When both sides talk at once, the default suppressor took the near end's voice down in bursts:
 * a spread of 22 dB around its level (measured on a recorded conversation with a known voice).
 * More transparent masks and a quicker nearend detection bring that to 14 dB for a little more
 * residual echo; of three settings this was the one misc-de preferred by ear. */
webrtc::EchoCanceller3Config suppressor_config()
{
	webrtc::EchoCanceller3Config config;
	auto &s = config.suppressor;
	s.normal_tuning.mask_lf.enr_transparent = .6f;
	s.normal_tuning.mask_lf.enr_suppress = 1.0f;
	s.normal_tuning.mask_hf.enr_transparent = .2f;
	s.normal_tuning.mask_hf.enr_suppress = .5f;
	s.nearend_tuning.mask_hf.enr_transparent = .4f;
	s.nearend_tuning.mask_hf.enr_suppress = .7f;
	s.dominant_nearend_detection.enr_threshold = .08f;
	s.dominant_nearend_detection.snr_threshold = 8.f;
	s.dominant_nearend_detection.hold_duration = 120;
	s.dominant_nearend_detection.trigger_threshold = 3;
	return config;
}
} // namespace

struct aec {
	rtc::scoped_refptr<webrtc::AudioProcessing> apm;
	webrtc::StreamConfig render;
	webrtc::StreamConfig capture;
};

struct aec *aec_new(int rate, int render_channels, int capture_channels)
{
	webrtc::AudioProcessingBuilder builder;
	builder.SetEchoControlFactory(std::make_unique<Aec3Factory>(suppressor_config()));
	rtc::scoped_refptr<webrtc::AudioProcessing> apm(builder.Create());
	if (!apm)
		return nullptr;
	webrtc::AudioProcessing::Config config;
	config.echo_canceller.enabled = true;
	config.high_pass_filter.enabled = true;
	config.noise_suppression.enabled = true;
	config.noise_suppression.level = webrtc::AudioProcessing::Config::NoiseSuppression::kHigh;
	/* The microphone is quiet: speech at a hand's length comes at -35 to -45 dBFS (peaks -29),
	 * +12 dB brings it to a usual level. A fixed gain, not the adaptive one: that one pumped up
	 * the residual echo in the pauses by some 30 dB (measured); the limiter stays */
	config.gain_controller2.enabled = true;
	config.gain_controller2.fixed_digital.gain_db = 12.f;
	apm->ApplyConfig(config);
	return new aec{apm, webrtc::StreamConfig(rate, render_channels), webrtc::StreamConfig(rate, capture_channels)};
}

void aec_free(struct aec *aec)
{
	delete aec;
}

void aec_render(struct aec *aec, const int16_t *frame)
{
	/* The output is only analysed; dest must still be given and may be the same memory */
	static thread_local int16_t scratch[48000 / 100 * 8];
	aec->apm->ProcessReverseStream(frame, aec->render, aec->render, scratch);
}

void aec_capture(struct aec *aec, int16_t *frame, int delay_ms)
{
	aec->apm->set_stream_delay_ms(delay_ms);
	aec->apm->ProcessStream(frame, aec->capture, aec->capture, frame);
}
