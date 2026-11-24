/*
 * cc-feedback-panel.h
 *
 * SPDX-License-Identifier: MIT
 */

#pragma once

#include "shell/cc-panel.h"

G_BEGIN_DECLS

#define CC_TYPE_FEEDBACK_PANEL (cc_feedback_panel_get_type ())
G_DECLARE_FINAL_TYPE (CcFeedbackPanel, cc_feedback_panel, CC, FEEDBACK_PANEL, CcPanel)

void cc_feedback_panel_static_init_func (void);

G_END_DECLS
