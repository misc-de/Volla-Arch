/*
 * cc-plinius-device.h
 *
 * Plinius: device facts and phone settings for the About and Display pages.
 *
 * SPDX-License-Identifier: MIT
 */

#pragma once

#include <adwaita.h>

G_BEGIN_DECLS

char *cc_plinius_device_get_processor (void);
guint64 cc_plinius_device_get_installed_ram (void);
gboolean cc_plinius_device_is_virtualization_hidden (const char *virt);

GArray *cc_plinius_device_get_scales (int width, int height);
char *cc_plinius_device_make_scale_string (double scale);

gboolean cc_plinius_device_setup_notch_row (AdwComboRow *row);

G_END_DECLS
