/*
 * cc-plinius-battery.h
 *
 * SPDX-License-Identifier: MIT
 */

#pragma once

#include <adwaita.h>
#include <libupower-glib/upower.h>

G_BEGIN_DECLS

GtkWidget  *cc_plinius_battery_group_new     (void);
void        cc_plinius_battery_group_update  (GtkWidget *group, UpDevice *battery);

GDBusProxy *cc_plinius_charge_proxy_new      (void);
gboolean    cc_plinius_charge_get            (GDBusProxy *proxy, const char *property);
guint       cc_plinius_charge_get_level      (GDBusProxy *proxy, const char *property);
void        cc_plinius_charge_set            (GDBusProxy *proxy, const char *property, gboolean on);
GtkWidget  *cc_plinius_charge_fast_row_new   (void);
char       *cc_plinius_charge_limit_subtitle (GDBusProxy *proxy);

G_END_DECLS
