/*
 * cc-pattern-dialog.h
 *
 * SPDX-License-Identifier: MIT
 */

#pragma once

#include <act/act.h>
#include <adwaita.h>

G_BEGIN_DECLS

#define CC_TYPE_PATTERN_DIALOG (cc_pattern_dialog_get_type ())
G_DECLARE_FINAL_TYPE (CcPatternDialog, cc_pattern_dialog, CC, PATTERN_DIALOG, AdwDialog)

CcPatternDialog *cc_pattern_dialog_new (ActUser *user);
gboolean         cc_pattern_dialog_is_available (void);
gboolean         cc_pattern_dialog_has_pattern (void);

G_END_DECLS
