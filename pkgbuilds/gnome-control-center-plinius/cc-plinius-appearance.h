/*
 * cc-plinius-appearance.h
 *
 * Plinius: the rows Style and Background of the Appearance page (see cc-plinius-appearance.c).
 *
 * SPDX-License-Identifier: MIT
 */

#pragma once

#include <adwaita.h>

G_BEGIN_DECLS

void cc_plinius_appearance_style_row_setup (AdwActionRow *row);
void cc_plinius_appearance_background_row_setup (AdwActionRow *row);
void cc_plinius_appearance_background_row_set_uri (AdwActionRow *row, const char *uri);

G_END_DECLS
