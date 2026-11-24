/*
 * cc-plinius-battery-usage.h
 *
 * SPDX-License-Identifier: MIT
 */

#pragma once

#include <adwaita.h>

#include "shell/cc-panel.h"

G_BEGIN_DECLS

GtkWidget *cc_plinius_battery_pages_new (CcPanel *panel, GtkWidget *condition_group);

G_END_DECLS
