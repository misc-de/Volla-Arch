/*
 * cc-plinius-software.h
 *
 * Plinius: installing apps and updating the system in Settings (plinius-software's pages).
 *
 * SPDX-License-Identifier: MIT
 */

#pragma once

#include <adwaita.h>

#include "shell/cc-panel.h"

G_BEGIN_DECLS

void cc_plinius_software_setup_apps_panel (CcPanel     *panel,
                                           GtkWidget   *installed_page,
                                           GtkWidget   *default_apps_group,
                                           GtkEditable *search_entry);

gboolean cc_plinius_software_explore_visible (CcPanel *panel);

void cc_plinius_software_update_app_page (CcPanel   *panel,
                                          GAppInfo  *info,
                                          GtkWidget *launch_button,
                                          GtkWidget *page_group);

GType cc_updates_panel_get_type (void);

void cc_updates_panel_static_init_func (void);

G_END_DECLS
