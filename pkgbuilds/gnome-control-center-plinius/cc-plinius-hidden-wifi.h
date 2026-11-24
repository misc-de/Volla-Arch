/*
 * cc-plinius-hidden-wifi.h
 *
 * SPDX-License-Identifier: MIT
 */

#pragma once

#include <adwaita.h>
#include <NetworkManager.h>

G_BEGIN_DECLS

#define CC_TYPE_PLINIUS_HIDDEN_WIFI (cc_plinius_hidden_wifi_get_type ())
G_DECLARE_FINAL_TYPE (CcPliniusHiddenWifi, cc_plinius_hidden_wifi, CC, PLINIUS_HIDDEN_WIFI, AdwDialog)

AdwDialog *cc_plinius_hidden_wifi_new (NMClient *client);

G_END_DECLS
