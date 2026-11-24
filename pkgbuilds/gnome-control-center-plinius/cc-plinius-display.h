/*
 * cc-plinius-display.h
 *
 * SPDX-License-Identifier: MIT
 */

#pragma once

#include <adwaita.h>

G_BEGIN_DECLS

gboolean   cc_plinius_display_setup (GtkBox *box);
GtkWidget *cc_plinius_display_rotation_row_new (void);
gboolean   cc_plinius_display_setup_app_overview (AdwPreferencesPage *page);
gboolean   cc_plinius_display_setup_search (AdwPreferencesPage *page);

G_END_DECLS
