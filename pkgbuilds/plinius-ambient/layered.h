// SPDX-License-Identifier: MIT
// plinius-ambient: an effect drawn over a background of its own (the 360° photo, a video), see
// layered.c.
#pragma once

#include "effects.h"

/* @background (panorama_effect, video_effect) with @over drawn over it, just @background for
 * NULL; one table entry per combination, so the same pointer comes back for the same pair */
const struct effect *layered_with(const struct effect *background, const struct effect *over);
