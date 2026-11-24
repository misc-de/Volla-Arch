/*
 * cc-plinius-hub-panels.c
 *
 * Plinius: the panels that are nothing but rows to other panels, Connected Devices and Accounts.
 * What belongs on them is in shell/cc-plinius-hubs.c.
 *
 * SPDX-License-Identifier: MIT
 */

#include "config.h"

#undef G_LOG_DOMAIN
#define G_LOG_DOMAIN "cc-plinius-hub-panels"

#include <adwaita.h>

#include "shell/cc-panel.h"
#include "shell/cc-plinius-hubs.h"

static void
hub_build (CcPanel *panel, const char *hub_id)
{
    GtkWidget *prefs = adw_preferences_page_new ();
    GtkWidget *toolbar = adw_toolbar_view_new ();

    cc_plinius_hub_add_rows (panel, ADW_PREFERENCES_PAGE (prefs), hub_id);

    adw_toolbar_view_add_top_bar (ADW_TOOLBAR_VIEW (toolbar), adw_header_bar_new ());
    adw_toolbar_view_set_content (ADW_TOOLBAR_VIEW (toolbar), prefs);
    adw_navigation_page_set_child (ADW_NAVIGATION_PAGE (panel), toolbar);
}

/* ----- Connected Devices ----- */

#define CC_TYPE_CONNECTED_DEVICES_PANEL (cc_connected_devices_panel_get_type ())
G_DECLARE_FINAL_TYPE (CcConnectedDevicesPanel, cc_connected_devices_panel, CC, CONNECTED_DEVICES_PANEL, CcPanel)

struct _CcConnectedDevicesPanel {
    CcPanel parent_instance;
};

CC_PANEL_REGISTER (CcConnectedDevicesPanel, cc_connected_devices_panel)

static void
cc_connected_devices_panel_class_init (CcConnectedDevicesPanelClass *klass)
{
}

static void
cc_connected_devices_panel_init (CcConnectedDevicesPanel *self)
{
    hub_build (CC_PANEL (self), "connected-devices");
}

/* ----- Accounts ----- */

#define CC_TYPE_ACCOUNTS_PANEL (cc_accounts_panel_get_type ())
G_DECLARE_FINAL_TYPE (CcAccountsPanel, cc_accounts_panel, CC, ACCOUNTS_PANEL, CcPanel)

struct _CcAccountsPanel {
    CcPanel parent_instance;
};

CC_PANEL_REGISTER (CcAccountsPanel, cc_accounts_panel)

static void
cc_accounts_panel_class_init (CcAccountsPanelClass *klass)
{
}

static void
cc_accounts_panel_init (CcAccountsPanel *self)
{
    hub_build (CC_PANEL (self), "accounts");
}
