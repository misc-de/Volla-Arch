/*
 * cc-username-dialog.h
 *
 * Plinius: rename your own login name (UI for plinius-rename-user).
 *
 * SPDX-License-Identifier: MIT
 */

#pragma once

#include <act/act.h>
#include <adwaita.h>

G_BEGIN_DECLS

#define CC_TYPE_USERNAME_DIALOG (cc_username_dialog_get_type ())
G_DECLARE_FINAL_TYPE (CcUsernameDialog, cc_username_dialog, CC, USERNAME_DIALOG, AdwDialog);

CcUsernameDialog *cc_username_dialog_new (ActUser *user);
gboolean cc_username_dialog_available (ActUser *user);
gchar *cc_username_dialog_get_pending (ActUser *user);

G_END_DECLS
