/*
 * cc-desktop-vnc-panel.h
 *
 * SPDX-License-Identifier: MIT
 */

#pragma once

#include "shell/cc-panel.h"

G_BEGIN_DECLS

#define CC_TYPE_DESKTOP_VNC_PANEL (cc_desktop_vnc_panel_get_type ())
G_DECLARE_FINAL_TYPE (CcDesktopVncPanel, cc_desktop_vnc_panel, CC, DESKTOP_VNC_PANEL, CcPanel)

void cc_desktop_vnc_panel_static_init_func (void);

G_END_DECLS
