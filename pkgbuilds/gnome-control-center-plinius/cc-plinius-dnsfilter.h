/*
 * cc-plinius-dnsfilter.h
 *
 * SPDX-License-Identifier: MIT
 */

#pragma once

#include <adwaita.h>

#include "shell/cc-panel.h"

G_BEGIN_DECLS

/* The "DNS Filter" group for the Network page (a row that opens the filter's pages), or NULL
 * without plinius-dnsfilter */
GtkWidget *cc_plinius_dnsfilter_group_new (CcPanel *panel);

G_END_DECLS
