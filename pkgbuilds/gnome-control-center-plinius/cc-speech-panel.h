/*
 * cc-speech-panel.h
 *
 * SPDX-License-Identifier: MIT
 */

#pragma once

#include "shell/cc-panel.h"

G_BEGIN_DECLS

#define CC_TYPE_SPEECH_PANEL (cc_speech_panel_get_type ())
G_DECLARE_FINAL_TYPE (CcSpeechPanel, cc_speech_panel, CC, SPEECH_PANEL, CcPanel)

void cc_speech_panel_static_init_func (void);

G_END_DECLS
