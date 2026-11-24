/*
 * cc-plinius-hubs.h
 *
 * Plinius: which panels live on another panel's page.
 *
 * SPDX-License-Identifier: MIT
 */

#pragma once

#include <adwaita.h>

#include "shell/cc-panel.h"

G_BEGIN_DECLS

const char *cc_plinius_hub_of (const char *panel_id);

GVariant *cc_plinius_hub_parameters (const char *panel_id, GVariant *parameters);

void cc_plinius_hub_add_rows (CcPanel *panel, AdwPreferencesPage *page, const char *hub_id);

char *cc_plinius_hub_panel_title (const char *panel_id);

G_END_DECLS
