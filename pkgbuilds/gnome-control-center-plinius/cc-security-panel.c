/*
 * cc-security-panel.c
 *
 * Plinius: "Security", a panel of its own below Privacy. Its page is the phone's own security
 * check (cc-plinius-security.c: screen lock, encryption, boot chain, software sources,
 * connections per app, known vulnerabilities), which used to be "Device Security" inside
 * Privacy & Security. The encryption page (plinius-crypt) is a page of this panel too, the
 * encryption check leads there. So is Lock & Unlock for the screen lock check: opened as a panel
 * of its own, back would lead to the panel list instead of here.
 *
 * SPDX-License-Identifier: MIT
 */

#include "config.h"

#undef G_LOG_DOMAIN
#define G_LOG_DOMAIN "cc-security-panel"

#include <adwaita.h>

#include "cc-plinius-crypt.h"
#include "cc-plinius-security.h"
#include "shell/cc-panel.h"
#include "shell/cc-panel-loader.h"
#include "shell/cc-plinius-hubs.h"

#define CC_TYPE_SECURITY_PANEL (cc_security_panel_get_type ())
G_DECLARE_FINAL_TYPE (CcSecurityPanel, cc_security_panel, CC, SECURITY_PANEL, CcPanel)

struct _CcSecurityPanel {
    CcPanel parent_instance;
};

CC_PANEL_REGISTER (CcSecurityPanel, cc_security_panel)

static void
cc_security_panel_class_init (CcSecurityPanelClass *klass)
{
}

static void
cc_security_panel_init (CcSecurityPanel *self)
{
    AdwNavigationPage *encryption_page = cc_plinius_crypt_page_new ();
    GType lock_type = cc_panel_loader_get_panel_type ("lock");

    cc_plinius_security_setup_panel (CC_PANEL (self));
    /* Updates: also a row on System, opens there (shell/cc-plinius-hubs.c) */
    cc_plinius_hub_add_rows (CC_PANEL (self),
                             ADW_PREFERENCES_PAGE (adw_toolbar_view_get_content (
                                 ADW_TOOLBAR_VIEW (adw_navigation_page_get_child (ADW_NAVIGATION_PAGE (self))))),
                             "security");

    /* Without plinius-crypt there is no page, and the check says so */
    if (encryption_page)
        cc_panel_add_subpage (CC_PANEL (self), "encryption", encryption_page);
    /* Built when the screen lock check is tapped; the title comes from the panel list's model */
    if (lock_type != G_TYPE_INVALID)
        cc_panel_add_static_subpage (CC_PANEL (self), "lock", lock_type);
}
