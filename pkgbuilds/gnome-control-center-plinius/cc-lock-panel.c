/*
 * cc-lock-panel.c
 *
 * Plinius: Lock & Unlock, everything about getting past the lock screen on one page: the ways to
 * unlock (password, pattern, fingerprint, face) and when and how the screen locks.
 *
 * Nothing is written twice. The rows to unlock stay the user page's, which shows only those here
 * and only the others under Your Account (CC_USER_PAGE_SECTION_*). The lock options stay the
 * Screen Lock page's, which hands its groups over and stays alive behind them, its settings
 * bindings belong to it.
 *
 * Our own strings use the gettext domain plinius-control-center (not GNOME's po/).
 *
 * SPDX-License-Identifier: MIT
 */

#include "config.h"

#undef G_LOG_DOMAIN
#define G_LOG_DOMAIN "cc-lock-panel"

#include <act/act.h>
#include <adwaita.h>
#include <polkit/polkit.h>
#include <unistd.h>

#include "../../privacy/screen/cc-screen-page.h"
#include "cc-lock-panel.h"
#include "cc-user-page.h"

#define PLINIUS_DOMAIN "plinius-control-center"
#define P_(String) g_dgettext (PLINIUS_DOMAIN, String)

#define USER_ACCOUNTS_PERMISSION "org.gnome.controlcenter.user-accounts.administration"

extern GResource *cc_privacy_get_resource (void);

struct _CcLockPanel {
    CcPanel parent_instance;

    CcUserPage *user_page;
    CcScreenPage *screen_page;
    ActUserManager *user_manager;
    GPermission *permission;
};

CC_PANEL_REGISTER (CcLockPanel, cc_lock_panel)

/* As on Your Account: the manager's list holds loaded users only, get_user_by_id () may hand out
 * one that is still loading and never says when it is done here */
static void
users_loaded (CcLockPanel *self)
{
    g_autoptr(GSList) users = act_user_manager_list_users (self->user_manager);

    for (GSList *l = users; l; l = l->next) {
        ActUser *user = ACT_USER (l->data);

        if (act_user_get_uid (user) == getuid ()) {
            cc_user_page_set_user (self->user_page, user, self->permission);
            break;
        }
    }
}

static void
on_user_changed (CcLockPanel *self, ActUser *user)
{
    if (act_user_get_uid (user) == getuid ())
        cc_user_page_set_user (self->user_page, user, self->permission);
}

static void
cc_lock_panel_dispose (GObject *object)
{
    CcLockPanel *self = CC_LOCK_PANEL (object);

    g_clear_object (&self->screen_page);
    g_clear_object (&self->permission);

    G_OBJECT_CLASS (cc_lock_panel_parent_class)->dispose (object);
}

static void
cc_lock_panel_class_init (CcLockPanelClass *klass)
{
    G_OBJECT_CLASS (klass)->dispose = cc_lock_panel_dispose;
}

static void
cc_lock_panel_init (CcLockPanel *self)
{
    g_autoptr(GError) error = NULL;
    gboolean is_loaded = FALSE;

    /* The Screen Lock page's template is in the Privacy panel's resources */
    g_resources_register (cc_privacy_get_resource ());

    self->user_page = cc_user_page_new ();
    cc_user_page_set_section (self->user_page, CC_USER_PAGE_SECTION_UNLOCK);
    adw_navigation_page_set_title (ADW_NAVIGATION_PAGE (self->user_page), P_("Lock & Unlock"));
    adw_navigation_page_set_child (ADW_NAVIGATION_PAGE (self), GTK_WIDGET (self->user_page));

    self->screen_page = g_object_ref_sink (g_object_new (CC_TYPE_SCREEN_PAGE, NULL));
    cc_screen_page_move_groups (self->screen_page, cc_user_page_get_preferences_page (self->user_page));

    /* Same permission as on Your Account: the page's rows follow it */
    self->permission = (GPermission *) polkit_permission_new_sync (USER_ACCOUNTS_PERMISSION, NULL, NULL, &error);
    if (self->permission == NULL) {
        g_warning ("Cannot create '%s' permission: %s", USER_ACCOUNTS_PERMISSION, error->message);
        return;
    }

    self->user_manager = act_user_manager_get_default ();
    g_signal_connect_object (self->user_manager, "notify::is-loaded", G_CALLBACK (users_loaded), self,
                             G_CONNECT_SWAPPED);
    g_signal_connect_object (self->user_manager, "user-changed", G_CALLBACK (on_user_changed), self, G_CONNECT_SWAPPED);

    g_object_get (self->user_manager, "is-loaded", &is_loaded, NULL);
    if (is_loaded)
        users_loaded (self);
}
