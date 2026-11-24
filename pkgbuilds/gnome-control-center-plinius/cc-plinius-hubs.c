/*
 * cc-plinius-hubs.c
 *
 * Plinius: the panel list has one level of order on top of GNOME's. Some panels do not get a row
 * of their own in the list but one on the page of a "hub" panel (USB on Connected Devices, say).
 * They stay real panels: search still finds them, "gnome-control-center usb" and the quick
 * settings still open them. The table below is the only place that says what belongs where.
 *
 * SPDX-License-Identifier: MIT
 */

#include "config.h"

#include <gio/gio.h>

#include "cc-list-row.h"
#include "cc-plinius-hubs.h"
#include "shell/cc-application.h"
#include "shell/cc-panel-loader.h"
#include "shell/cc-shell-model.h"

typedef struct {
    const char *panel;
    const char *hub;
} HubChild;

/* In the order of the rows on the hub's page */
static const HubChild hub_children[] = {
    { "usb", "connected-devices" },
    { "streaming", "connected-devices" },
    { "printers", "connected-devices" },
    { "mouse", "connected-devices" },
    { "wacom", "connected-devices" },
    { "users", "accounts" },
    { "online-accounts", "accounts" },
    { "assistant", "keyboard" },
    /* A panel twice: its first hub is the one it opens on, the second only has a row to it */
    { "updates", "system" },
    { "updates", "security" },
};

/**
 * cc_plinius_hub_of:
 * @panel_id: a panel's id
 *
 * Returns: (nullable): the id of the panel whose page has a row for @panel_id, %NULL for a panel
 * with a row of its own in the panel list
 */
const char *
cc_plinius_hub_of (const char *panel_id)
{
    for (gsize i = 0; i < G_N_ELEMENTS (hub_children); i++)
        if (g_strcmp0 (hub_children[i].panel, panel_id) == 0)
            return hub_children[i].hub;
    return NULL;
}

/**
 * cc_plinius_hub_parameters:
 * @panel_id: a panel on a hub's page
 * @parameters: (nullable): the parameters for that panel (type "av")
 *
 * Returns: (transfer full): parameters for the hub: the page to open, then what was meant for the
 * panel. An empty first string is how Phosh's quick settings say "no parameters"; it is left out.
 */
GVariant *
cc_plinius_hub_parameters (const char *panel_id, GVariant *parameters)
{
    GVariantBuilder builder;

    g_variant_builder_init (&builder, G_VARIANT_TYPE ("av"));
    g_variant_builder_add (&builder, "v", g_variant_new_string (panel_id));

    for (gsize i = 0; parameters && i < g_variant_n_children (parameters); i++) {
        g_autoptr(GVariant) child = g_variant_get_child_value (parameters, i);
        g_autoptr(GVariant) value = g_variant_get_variant (child);

        if (g_variant_is_of_type (value, G_VARIANT_TYPE_STRING) && *g_variant_get_string (value, NULL) == '\0')
            continue;
        g_variant_builder_add (&builder, "v", value);
    }

    return g_variant_ref_sink (g_variant_builder_end (&builder));
}

/* The model's values for one panel; FALSE if the model does not have it */
static gboolean
get_panel_info (const char *panel_id, char **name, char **description, GIcon **icon, CcPanelVisibility *visibility)
{
    CcApplication *application = CC_APPLICATION (g_application_get_default ());
    GtkTreeModel *model = GTK_TREE_MODEL (cc_application_get_model (application));
    GtkTreeIter iter;
    gboolean valid = gtk_tree_model_get_iter_first (model, &iter);

    while (valid) {
        g_autofree char *id = NULL;

        gtk_tree_model_get (model, &iter, COL_ID, &id, -1);
        if (g_strcmp0 (id, panel_id) == 0) {
            gtk_tree_model_get (model, &iter, COL_NAME, name, COL_DESCRIPTION, description, COL_GICON, icon,
                                COL_VISIBILITY, visibility, -1);
            return TRUE;
        }
        valid = gtk_tree_model_iter_next (model, &iter);
    }
    return FALSE;
}

/**
 * cc_plinius_hub_panel_title:
 * @panel_id: a panel's id
 *
 * Returns: (transfer full) (nullable): the title for a panel that opens as a page of a hub. The
 * panel loader passes it on to panels of the list, a hub's page has to set it itself.
 */
char *
cc_plinius_hub_panel_title (const char *panel_id)
{
    g_autofree char *description = NULL;
    g_autoptr(GIcon) icon = NULL;
    CcPanelVisibility visibility;
    char *name = NULL;

    if (!get_panel_info (panel_id, &name, &description, &icon, &visibility))
        return NULL;
    return name;
}

/* The rows follow the panels' visibility, a mouse can come and go. The group goes with its rows. */
static void
sync_rows (AdwPreferencesGroup *group)
{
    GPtrArray *rows = g_object_get_data (G_OBJECT (group), "plinius-hub-rows");
    gboolean any = FALSE;

    for (guint i = 0; i < rows->len; i++) {
        GtkWidget *row = g_ptr_array_index (rows, i);
        g_autofree char *name = NULL;
        g_autofree char *description = NULL;
        g_autoptr(GIcon) icon = NULL;
        CcPanelVisibility visibility = CC_PANEL_HIDDEN;
        gboolean visible = get_panel_info (g_object_get_data (G_OBJECT (row), "plinius-hub-panel"), &name, &description,
                                           &icon, &visibility) &&
                           visibility == CC_PANEL_VISIBLE;

        gtk_widget_set_visible (row, visible);
        any |= visible;
    }
    gtk_widget_set_visible (GTK_WIDGET (group), any);
}

static GtkWidget *
row_new (const char *panel_id)
{
    g_autofree char *name = NULL;
    g_autofree char *description = NULL;
    g_autofree char *title = NULL;
    g_autofree char *subtitle = NULL;
    g_autoptr(GIcon) icon = NULL;
    CcPanelVisibility visibility;
    const char *icon_name = NULL;
    GtkWidget *row;

    if (!get_panel_info (panel_id, &name, &description, &icon, &visibility))
        return NULL;

    if (G_IS_THEMED_ICON (icon))
        icon_name = g_themed_icon_get_names (G_THEMED_ICON (icon))[0];

    /* The row's texts are markup, the names are not */
    title = g_markup_escape_text (name, -1);
    subtitle = g_markup_escape_text (description ? description : "", -1);
    row = g_object_new (CC_TYPE_LIST_ROW, "title", title, "subtitle", subtitle, "icon-name", icon_name, "show-arrow", TRUE,
                        "action-name", "navigation.push", "action-target", g_variant_new_string (panel_id), NULL);
    g_object_set_data_full (G_OBJECT (row), "plinius-hub-panel", g_strdup (panel_id), g_free);
    return row;
}

/**
 * cc_plinius_hub_add_rows:
 * @panel: the hub
 * @page: where the rows go, in a group of their own at the end
 * @hub_id: the hub's id
 *
 * Adds a row for each panel on the hub's page and makes the panels pages of the hub. They are only
 * built when opened.
 */
void
cc_plinius_hub_add_rows (CcPanel *panel, AdwPreferencesPage *page, const char *hub_id)
{
    CcApplication *application = CC_APPLICATION (g_application_get_default ());
    AdwPreferencesGroup *group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    GPtrArray *rows = g_ptr_array_new ();

    g_object_set_data_full (G_OBJECT (group), "plinius-hub-rows", rows, (GDestroyNotify) g_ptr_array_unref);

    for (gsize i = 0; i < G_N_ELEMENTS (hub_children); i++) {
        GType type;
        GtkWidget *row;

        if (g_strcmp0 (hub_children[i].hub, hub_id) != 0)
            continue;

        type = cc_panel_loader_get_panel_type (hub_children[i].panel);
        row = row_new (hub_children[i].panel);
        /* Not built in or no desktop file: it has no model row either */
        if (type == G_TYPE_INVALID || row == NULL)
            continue;

        cc_panel_add_static_subpage (panel, hub_children[i].panel, type);
        adw_preferences_group_add (group, row);
        g_ptr_array_add (rows, row);
    }

    adw_preferences_page_add (page, group);
    sync_rows (group);
    g_signal_connect_object (cc_application_get_model (application), "row-changed", G_CALLBACK (sync_rows), group,
                             G_CONNECT_SWAPPED);
}
