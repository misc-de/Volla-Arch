// SPDX-License-Identifier: MIT
// Echo cancellation for audio-hal-bridge: WebRTC's audio processing behind a C interface.
#pragma once

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

struct aec;

/* Echo canceller, noise suppression, high-pass filter and a fixed gain for voice. Frames are
 * 10 ms (rate / 100 frames) of interleaved S16. NULL if the library fails. */
struct aec *aec_new(int rate, int render_channels, int capture_channels);
void aec_free(struct aec *aec);
/* What went to the speaker or earpiece, paired with the capture block that follows */
void aec_render(struct aec *aec, const int16_t *frame);
/* What the microphone took, in place; delay_ms: from the HAL write to its sound in the mic */
void aec_capture(struct aec *aec, int16_t *frame, int delay_ms);

#ifdef __cplusplus
}
#endif
