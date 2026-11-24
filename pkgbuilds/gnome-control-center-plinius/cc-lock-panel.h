/*
 * cc-lock-panel.h
 *
 * SPDX-License-Identifier: MIT
 */

#pragma once

#include <shell/cc-panel.h>

G_BEGIN_DECLS

#define CC_TYPE_LOCK_PANEL (cc_lock_panel_get_type ())
G_DECLARE_FINAL_TYPE (CcLockPanel, cc_lock_panel, CC, LOCK_PANEL, CcPanel)

G_END_DECLS
