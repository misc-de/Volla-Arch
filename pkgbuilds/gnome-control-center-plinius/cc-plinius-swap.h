/*
 * cc-plinius-swap.h
 *
 * SPDX-License-Identifier: MIT
 */

#pragma once

#include <adwaita.h>

#include "shell/cc-panel.h"

G_BEGIN_DECLS

gboolean cc_plinius_swap_add_page  (CcPanel *panel);
void     cc_plinius_swap_row_setup (AdwActionRow *row);

G_END_DECLS
