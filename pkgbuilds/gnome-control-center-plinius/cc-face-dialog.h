/*
 * cc-face-dialog.h
 *
 * SPDX-License-Identifier: MIT
 */

#pragma once

#include <act/act.h>
#include <adwaita.h>

#include "cc-list-row.h"

G_BEGIN_DECLS

#define CC_TYPE_FACE_DIALOG (cc_face_dialog_get_type ())
G_DECLARE_FINAL_TYPE (CcFaceDialog, cc_face_dialog, CC, FACE_DIALOG, AdwDialog)

CcFaceDialog *cc_face_dialog_new (void);
gboolean      cc_face_dialog_is_available (void);
/* Sets the row's label (On, Off, Not Set Up) once the service answers */
void          cc_face_dialog_update_row (CcListRow *row);

G_END_DECLS
