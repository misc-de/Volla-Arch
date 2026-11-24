/*
 * cc-plinius-webapps.h
 *
 * Plinius: the "Web Apps" tab of Settings > Apps (websites as apps of their own, each with its
 * own Firefox profile).
 *
 * SPDX-License-Identifier: MIT
 */

#pragma once

#include <adwaita.h>

#include "shell/cc-panel.h"

G_BEGIN_DECLS

GtkWidget *cc_plinius_webapps_new_tab (CcPanel *panel);
GtkWidget *cc_plinius_webapps_new_add_button (GtkWidget *tab);

G_END_DECLS
