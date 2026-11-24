/*
 * cc-username-dialog.c
 *
 * Plinius: rename your own login name together with the home folder and all references.
 * The work is done by plinius-rename-user (via pkexec, from plinius-account) and, at the
 * next boot, by plinius-account-rename.service; this dialog is only the UI.
 * Our own strings use the gettext domain plinius-control-center (not GNOME's po/).
 *
 * SPDX-License-Identifier: MIT
 */

#undef G_LOG_DOMAIN
#define G_LOG_DOMAIN "cc-username-dialog"

#include "config.h"

#include <grp.h>
#include <unistd.h>

#include <act/act.h>
#include <adwaita.h>
#include <glib.h>
#include <glib/gi18n.h>
#include <gtk/gtk.h>

#include "cc-entry-feedback.h"
#include "cc-username-dialog.h"
#include "user-utils.h"

#define PLINIUS_DOMAIN "plinius-control-center"
#define P_(String) g_dgettext (PLINIUS_DOMAIN, String)

#define RENAME_HELPER "/usr/bin/plinius-rename-user"
#define RENAME_REQUEST "/var/lib/plinius-account/rename"
#define USERNAME_REGEX "^[a-z_][a-z0-9_-]{0,31}$"

/* pkexec exit codes: prompt dismissed, or not authorized */
#define PKEXEC_DISMISSED 126
#define PKEXEC_NOT_AUTHORIZED 127

struct _CcUsernameDialog {
    AdwDialog parent_instance;

    AdwActionRow *current_row;
    AdwPreferencesGroup *pending_group;
    AdwActionRow *pending_row;
    GtkButton *rename_button;
    AdwAlertDialog *restart_dialog;
    AdwToastOverlay *toast_overlay;
    CcEntryFeedback *username_feedback;
    AdwEntryRow *username_row;

    ActUser *user;
    GCancellable *cancellable;
    gchar *requested_name;
};

G_DEFINE_FINAL_TYPE (CcUsernameDialog, cc_username_dialog, ADW_TYPE_DIALOG)

/* Read the pending rename ("OLD NEW") written by plinius-rename-user */
static gboolean
read_pending (gchar **old_name, gchar **new_name)
{
    g_autofree gchar *contents = NULL;
    g_auto(GStrv) parts = NULL;

    if (!g_file_get_contents (RENAME_REQUEST, &contents, NULL, NULL))
        return FALSE;

    parts = g_strsplit_set (g_strstrip (contents), " \t\n", -1);
    if (g_strv_length (parts) != 2)
        return FALSE;

    *old_name = g_strdup (parts[0]);
    *new_name = g_strdup (parts[1]);

    return TRUE;
}

static gchar *
dup_new_username (CcUsernameDialog *self)
{
    return g_strstrip (g_strdup (gtk_editable_get_text (GTK_EDITABLE (self->username_row))));
}

/* Show the "scheduled" row if a rename of this user is pending */
static void
update_pending (CcUsernameDialog *self)
{
    g_autofree gchar *old_name = NULL;
    g_autofree gchar *new_name = NULL;
    gboolean pending;

    pending = read_pending (&old_name, &new_name)
              && g_strcmp0 (old_name, act_user_get_user_name (self->user)) == 0;
    gtk_widget_set_visible (GTK_WIDGET (self->pending_group), pending);
    if (pending) {
        g_autofree gchar *subtitle = g_strdup_printf ("%s → %s", old_name, new_name);
        adw_action_row_set_subtitle (self->pending_row, subtitle);
    }
}

/* Result of GNOME's asynchronous username check (usermod) */
static void
username_checked_cb (GObject *source_object, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(CcUsernameDialog) self = CC_USERNAME_DIALOG (user_data);
    g_autoptr(GError) error = NULL;
    g_autofree gchar *tip = NULL;
    g_autofree gchar *username = NULL;
    g_autofree gchar *current = NULL;
    gboolean valid;

    valid = is_valid_username_finish (result, &tip, &username, &error);
    if (g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
        return;
    if (error != NULL) {
        /* plinius-rename-user checks again on its own anyway */
        g_warning ("Could not check username by usermod: %s", error->message);
        valid = TRUE;
    }

    /* The user kept typing meanwhile: this result is stale */
    current = dup_new_username (self);
    if (g_strcmp0 (username, current) != 0)
        return;

    gtk_widget_set_sensitive (GTK_WIDGET (self->rename_button), valid && self->requested_name == NULL);
    if (tip && *tip != '\0')
        cc_entry_feedback_update (self->username_feedback, "dialog-error-symbolic", tip);
    else if (valid)
        cc_entry_feedback_update (self->username_feedback, "check-outlined-symbolic", P_("This username is available."));
    else
        cc_entry_feedback_reset (self->username_feedback);
}

/* Validate the new name locally (syntax, group, home folder), then ask GNOME's check */
static void
username_changed_cb (CcUsernameDialog *self)
{
    g_autofree gchar *name = dup_new_username (self);
    const gchar *problem = NULL;
    g_autofree gchar *home = NULL;

    g_cancellable_cancel (self->cancellable);
    g_clear_object (&self->cancellable);
    gtk_widget_set_sensitive (GTK_WIDGET (self->rename_button), FALSE);

    if (*name == '\0' || g_strcmp0 (name, act_user_get_user_name (self->user)) == 0) {
        cc_entry_feedback_reset (self->username_feedback);
        return;
    }

    home = g_build_filename ("/home", name, NULL);
    if (!g_regex_match_simple (USERNAME_REGEX, name, 0, 0))
        problem = P_("Lower case letters, numbers, hyphens and underscores, starting with a letter, at most 32 characters.");
    else if (getgrnam (name) != NULL)
        problem = P_("A group with this name already exists.");
    else if (g_file_test (home, G_FILE_TEST_EXISTS))
        problem = P_("A home folder with this name already exists.");

    if (problem) {
        cc_entry_feedback_update (self->username_feedback, "dialog-error-symbolic", problem);
        return;
    }

    /* Whether a user of that name exists is checked by GNOME itself (usermod), with its own message */
    cc_entry_feedback_update (self->username_feedback, CC_ENTRY_LOADING, P_("Checking username…"));
    self->cancellable = g_cancellable_new ();
    is_valid_username_async (name, self->cancellable, username_checked_cb, g_object_ref (self));
}

static void
reboot_done_cb (GObject *source_object, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(CcUsernameDialog) self = CC_USERNAME_DIALOG (user_data);
    g_autoptr(GVariant) ret = NULL;
    g_autoptr(GError) error = NULL;

    ret = g_dbus_connection_call_finish (G_DBUS_CONNECTION (source_object), result, &error);
    if (ret == NULL) {
        g_warning ("Could not restart: %s", error->message);
        adw_toast_overlay_add_toast (self->toast_overlay, adw_toast_new (P_("Restarting failed")));
    }
}

/* "Restart now": reboot via logind so the rename is carried out */
static void
restart_response_cb (CcUsernameDialog *self)
{
    g_autoptr(GDBusConnection) bus = NULL;
    g_autoptr(GError) error = NULL;

    bus = g_bus_get_sync (G_BUS_TYPE_SYSTEM, NULL, &error);
    if (bus == NULL) {
        g_warning ("Could not connect to the system bus: %s", error->message);
        return;
    }

    /* Interactive: if other sessions are open, polkit asks for the password */
    g_dbus_connection_call (bus, "org.freedesktop.login1", "/org/freedesktop/login1",
                            "org.freedesktop.login1.Manager", "Reboot", g_variant_new ("(b)", TRUE), NULL,
                            G_DBUS_CALL_FLAGS_ALLOW_INTERACTIVE_AUTHORIZATION, -1, NULL, reboot_done_cb,
                            g_object_ref (self));
}

/* Show the helper's stderr as a toast, or fallback; silent if pkexec was dismissed or denied */
static void
show_helper_error (CcUsernameDialog *self, GSubprocess *process, const gchar *stderr_buf, const gchar *fallback)
{
    g_autofree gchar *message = NULL;
    /* Killed by a signal: no exit status to ask for */
    gint status = g_subprocess_get_if_exited (process) ? g_subprocess_get_exit_status (process) : -1;

    if (status == PKEXEC_DISMISSED || status == PKEXEC_NOT_AUTHORIZED)
        return;

    message = g_strstrip (g_strdup (stderr_buf ? stderr_buf : ""));
    adw_toast_overlay_add_toast (self->toast_overlay, adw_toast_new (*message != '\0' ? message : fallback));
}

/* plinius-rename-user finished: on success, offer to restart */
static void
rename_done_cb (GObject *source_object, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(CcUsernameDialog) self = CC_USERNAME_DIALOG (user_data);
    GSubprocess *process = G_SUBPROCESS (source_object);
    g_autoptr(GError) error = NULL;
    g_autofree gchar *stderr_buf = NULL;
    g_autofree gchar *body = NULL;
    g_autofree gchar *new_name = g_steal_pointer (&self->requested_name);

    if (!g_subprocess_communicate_utf8_finish (process, result, NULL, &stderr_buf, &error)) {
        g_warning ("Could not run %s: %s", RENAME_HELPER, error->message);
        username_changed_cb (self);
        return;
    }

    if (!g_subprocess_get_if_exited (process) || g_subprocess_get_exit_status (process) != 0) {
        show_helper_error (self, process, stderr_buf, P_("Renaming failed"));
        username_changed_cb (self);
        return;
    }

    update_pending (self);

    /* Translators: %1$s is the current, %2$s the new username */
    body = g_strdup_printf (P_("On restart, %1$s will be renamed to %2$s. After that, you log in as %2$s."),
                            act_user_get_user_name (self->user), new_name);
    adw_alert_dialog_set_body (self->restart_dialog, body);
    adw_dialog_present (ADW_DIALOG (self->restart_dialog), GTK_WIDGET (self));

    gtk_editable_set_text (GTK_EDITABLE (self->username_row), "");
}

/* Schedule the rename via pkexec plinius-rename-user OLD NEW */
static void
rename_button_clicked_cb (CcUsernameDialog *self)
{
    g_autoptr(GSubprocess) process = NULL;
    g_autoptr(GError) error = NULL;

    g_autofree gchar *name = NULL;

    if (!gtk_widget_get_sensitive (GTK_WIDGET (self->rename_button)))
        return;

    /* pkexec asks for the password through Phosh's polkit agent */
    name = dup_new_username (self);
    process = g_subprocess_new (G_SUBPROCESS_FLAGS_STDOUT_SILENCE | G_SUBPROCESS_FLAGS_STDERR_PIPE, &error, "pkexec",
                                RENAME_HELPER, act_user_get_user_name (self->user), name, NULL);
    if (process == NULL) {
        g_warning ("Could not run %s: %s", RENAME_HELPER, error->message);
        return;
    }

    self->requested_name = g_steal_pointer (&name);
    gtk_widget_set_sensitive (GTK_WIDGET (self->rename_button), FALSE);
    g_subprocess_communicate_utf8_async (process, NULL, NULL, rename_done_cb, g_object_ref (self));
}

static void
cancel_done_cb (GObject *source_object, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(CcUsernameDialog) self = CC_USERNAME_DIALOG (user_data);
    GSubprocess *process = G_SUBPROCESS (source_object);
    g_autoptr(GError) error = NULL;
    g_autofree gchar *stderr_buf = NULL;

    if (!g_subprocess_communicate_utf8_finish (process, result, NULL, &stderr_buf, &error)) {
        g_warning ("Could not run %s: %s", RENAME_HELPER, error->message);
        return;
    }

    if (!g_subprocess_get_if_exited (process) || g_subprocess_get_exit_status (process) != 0)
        show_helper_error (self, process, stderr_buf, P_("Undoing failed"));

    update_pending (self);
}

/* "Undo": drop the pending rename via pkexec plinius-rename-user --cancel */
static void
cancel_pending_cb (CcUsernameDialog *self)
{
    g_autoptr(GSubprocess) process = NULL;
    g_autoptr(GError) error = NULL;

    process = g_subprocess_new (G_SUBPROCESS_FLAGS_STDOUT_SILENCE | G_SUBPROCESS_FLAGS_STDERR_PIPE, &error, "pkexec",
                                RENAME_HELPER, "--cancel", NULL);
    if (process == NULL) {
        g_warning ("Could not run %s: %s", RENAME_HELPER, error->message);
        return;
    }

    g_subprocess_communicate_utf8_async (process, NULL, NULL, cancel_done_cb, g_object_ref (self));
}

static void
cc_username_dialog_dispose (GObject *object)
{
    CcUsernameDialog *self = CC_USERNAME_DIALOG (object);

    g_cancellable_cancel (self->cancellable);
    g_clear_object (&self->cancellable);
    g_clear_object (&self->user);
    g_clear_pointer (&self->requested_name, g_free);

    G_OBJECT_CLASS (cc_username_dialog_parent_class)->dispose (object);
}

static void
cc_username_dialog_class_init (CcUsernameDialogClass *klass)
{
    GtkWidgetClass *widget_class = GTK_WIDGET_CLASS (klass);
    GObjectClass *object_class = G_OBJECT_CLASS (klass);

    object_class->dispose = cc_username_dialog_dispose;

    /* Before the template: GtkBuilder translates it with the domain from the .blp */
    bindtextdomain (PLINIUS_DOMAIN, GNOMELOCALEDIR);
    bind_textdomain_codeset (PLINIUS_DOMAIN, "UTF-8");

    g_type_ensure (CC_TYPE_ENTRY_FEEDBACK);

    gtk_widget_class_set_template_from_resource (widget_class,
                                                 "/org/gnome/control-center/system/users/cc-username-dialog.ui");

    gtk_widget_class_bind_template_child (widget_class, CcUsernameDialog, current_row);
    gtk_widget_class_bind_template_child (widget_class, CcUsernameDialog, pending_group);
    gtk_widget_class_bind_template_child (widget_class, CcUsernameDialog, pending_row);
    gtk_widget_class_bind_template_child (widget_class, CcUsernameDialog, rename_button);
    gtk_widget_class_bind_template_child (widget_class, CcUsernameDialog, restart_dialog);
    gtk_widget_class_bind_template_child (widget_class, CcUsernameDialog, toast_overlay);
    gtk_widget_class_bind_template_child (widget_class, CcUsernameDialog, username_feedback);
    gtk_widget_class_bind_template_child (widget_class, CcUsernameDialog, username_row);

    gtk_widget_class_bind_template_callback (widget_class, cancel_pending_cb);
    gtk_widget_class_bind_template_callback (widget_class, rename_button_clicked_cb);
    gtk_widget_class_bind_template_callback (widget_class, restart_response_cb);
    gtk_widget_class_bind_template_callback (widget_class, username_changed_cb);
}

static void
cc_username_dialog_init (CcUsernameDialog *self)
{
    gtk_widget_init_template (GTK_WIDGET (self));
}

CcUsernameDialog *
cc_username_dialog_new (ActUser *user)
{
    CcUsernameDialog *self;

    g_return_val_if_fail (ACT_IS_USER (user), NULL);

    self = g_object_new (CC_TYPE_USERNAME_DIALOG, NULL);
    self->user = g_object_ref (user);

    adw_action_row_set_subtitle (self->current_row, act_user_get_user_name (user));
    update_pending (self);

    return self;
}

/* Only for your own local account, and only if plinius-account is installed */
gboolean
cc_username_dialog_available (ActUser *user)
{
    return act_user_get_uid (user) == getuid ()
           && act_user_is_local_account (user)
           && !act_user_uses_homed (user)
           && g_file_test (RENAME_HELPER, G_FILE_TEST_IS_EXECUTABLE);
}

/* The pending new name of this user, or NULL */
gchar *
cc_username_dialog_get_pending (ActUser *user)
{
    g_autofree gchar *old_name = NULL;
    g_autofree gchar *new_name = NULL;

    if (!read_pending (&old_name, &new_name) || g_strcmp0 (old_name, act_user_get_user_name (user)) != 0)
        return NULL;

    return g_steal_pointer (&new_name);
}
