/*
 * cc-streaming-panel.h
 *
 * SPDX-License-Identifier: MIT
 */

#pragma once

#include "shell/cc-panel.h"

G_BEGIN_DECLS

#define CC_TYPE_STREAMING_PANEL (cc_streaming_panel_get_type ())
G_DECLARE_FINAL_TYPE (CcStreamingPanel, cc_streaming_panel, CC, STREAMING_PANEL, CcPanel)

void cc_streaming_panel_static_init_func (void);

G_END_DECLS
