/*
 * cc-plinius-bt-contacts.h
 *
 * SPDX-License-Identifier: MIT
 */

#pragma once

#include <adwaita.h>
#include <shell/cc-panel.h>

G_BEGIN_DECLS

void cc_plinius_bt_contacts_add (CcPanel *panel, AdwToolbarView *toolbar);
GtkWidget *cc_plinius_bt_contacts_row_new (CcPanel *panel, gboolean on_privacy);

G_END_DECLS
