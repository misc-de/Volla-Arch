/*
 * cc-nfc-panel.h
 *
 * SPDX-License-Identifier: MIT
 */

#pragma once

#include "shell/cc-panel.h"

G_BEGIN_DECLS

#define CC_TYPE_NFC_PANEL (cc_nfc_panel_get_type ())
G_DECLARE_FINAL_TYPE (CcNfcPanel, cc_nfc_panel, CC, NFC_PANEL, CcPanel)

void cc_nfc_panel_static_init_func (void);

G_END_DECLS
