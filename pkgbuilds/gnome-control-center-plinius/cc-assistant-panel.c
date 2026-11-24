/*
 * cc-assistant-panel.c
 *
 * Plinius: "Assistant Button", a panel of its own in the sidebar. It holds the actions for a
 * short, a double and a long press (cc-plinius-buttons.*, done by plinius-assist) and is only
 * there when plinius-assist's schema is installed.
 * Our own strings use the gettext domain plinius-control-center (not GNOME's po/).
 *
 * SPDX-License-Identifier: MIT
 */

#include "config.h"

#include <adwaita.h>

#include "cc-assistant-panel.h"
#include "panels/common/cc-plinius-buttons.h"
#include "shell/cc-application.h"

#define PLINIUS_DOMAIN "plinius-control-center"
#define P_(String) g_dgettext (PLINIUS_DOMAIN, String)

#define ASSIST_SCHEMA "org.plinius.assistant-button"

struct _CcAssistantPanel {
    CcPanel parent_instance;
};

G_DEFINE_TYPE (CcAssistantPanel, cc_assistant_panel, CC_TYPE_PANEL)

static void
cc_assistant_panel_class_init (CcAssistantPanelClass *klass)
{
}

static void
cc_assistant_panel_init (CcAssistantPanel *self)
{
    GtkWidget *toolbar = adw_toolbar_view_new ();
    GtkWidget *page = adw_preferences_page_new ();

    adw_navigation_page_set_title (ADW_NAVIGATION_PAGE (self), P_("Assistant Button"));
    adw_toolbar_view_add_top_bar (ADW_TOOLBAR_VIEW (toolbar), adw_header_bar_new ());
    adw_toolbar_view_set_content (ADW_TOOLBAR_VIEW (toolbar), page);
    adw_navigation_page_set_child (ADW_NAVIGATION_PAGE (self), toolbar);

    cc_plinius_buttons_setup (ADW_PREFERENCES_PAGE (page));
}

/* Only with plinius-assist, which brings the schema and does the work */
void
cc_assistant_panel_static_init_func (void)
{
    GSettingsSchemaSource *source = g_settings_schema_source_get_default ();
    g_autoptr(GSettingsSchema) schema = NULL;
    CcApplication *application = CC_APPLICATION (g_application_get_default ());

    if (source)
        schema = g_settings_schema_source_lookup (source, ASSIST_SCHEMA, TRUE);
    cc_shell_model_set_panel_visibility (cc_application_get_model (application), "assistant",
                                         schema ? CC_PANEL_VISIBLE : CC_PANEL_HIDDEN);
}
