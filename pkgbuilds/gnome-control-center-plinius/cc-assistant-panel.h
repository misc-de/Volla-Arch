/*
 * cc-assistant-panel.h
 *
 * SPDX-License-Identifier: MIT
 */

#pragma once

#include "shell/cc-panel.h"

G_BEGIN_DECLS

#define CC_TYPE_ASSISTANT_PANEL (cc_assistant_panel_get_type ())
G_DECLARE_FINAL_TYPE (CcAssistantPanel, cc_assistant_panel, CC, ASSISTANT_PANEL, CcPanel)

void cc_assistant_panel_static_init_func (void);

G_END_DECLS
