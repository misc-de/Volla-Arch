/*
 * cc-usb-panel.h
 *
 * SPDX-License-Identifier: MIT
 */

#pragma once

#include "shell/cc-panel.h"

G_BEGIN_DECLS

#define CC_TYPE_USB_PANEL (cc_usb_panel_get_type ())
G_DECLARE_FINAL_TYPE (CcUsbPanel, cc_usb_panel, CC, USB_PANEL, CcPanel)

void cc_usb_panel_static_init_func (void);

G_END_DECLS
