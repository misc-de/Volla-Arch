/*
 * cc-plinius-accent.h
 *
 * SPDX-License-Identifier: MIT
 */

#pragma once

#include <adwaita.h>

G_BEGIN_DECLS

gboolean cc_plinius_accent_setup (GtkWidget *accent_box);
gboolean cc_plinius_accent_sync  (GtkWidget *accent_box);

/* Shared with the background color (cc-plinius-bgcolor.c) */
typedef void (*CcPliniusColorSelected) (const GdkRGBA *rgba, gpointer data);

void  cc_plinius_color_page_present (GtkWidget *parent, const char *title, const GdkRGBA *rgba,
                                     CcPliniusColorSelected selected, gpointer data, GDestroyNotify destroy);
void  cc_plinius_color_wheel_style  (GtkWidget *button);
char *cc_plinius_color_to_hex       (const GdkRGBA *rgba);

G_END_DECLS
