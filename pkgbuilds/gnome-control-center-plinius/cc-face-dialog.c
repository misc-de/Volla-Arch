/*
 * cc-face-dialog.c
 *
 * Plinius: face unlock for one's own account (plinius-face, D-Bus org.plinius.Face on the system
 * bus). Set up the face (the service asks for the PIN through polkit first), try it, switch face
 * unlock on or off, choose how strict it is, remove the face. The service only ever acts on the
 * caller's own face; this page just shows what it answers.
 *
 * Face unlock does not unlock the screen yet: it waits for the liveness checks against photos
 * and videos. The page says so instead of pretending.
 * Our own strings use the gettext domain plinius-control-center (not GNOME's po/).
 *
 * SPDX-License-Identifier: MIT
 */

#undef G_LOG_DOMAIN
#define G_LOG_DOMAIN "cc-face-dialog"

#include "config.h"

#include <unistd.h>

#include <adwaita.h>
#include <glib.h>
#include <gtk/gtk.h>

#include "cc-face-dialog.h"

#define PLINIUS_DOMAIN "plinius-control-center"
#define P_(String) g_dgettext (PLINIUS_DOMAIN, String)

#define FACE_BUS_NAME     "org.plinius.Face"
#define FACE_PATH         "/org/plinius/Face"
#define FACE_INTERFACE    "org.plinius.Face1"
#define FACE_ACTIVATION   "/usr/share/dbus-1/system-services/org.plinius.Face.service"
/* Verify takes up to a few seconds of camera time, plus loading the models once */
#define VERIFY_TIMEOUT_MS 30000

struct _CcFaceDialog {
    AdwDialog parent_instance;

    GCancellable *cancellable;
    GDBusConnection *bus;
    guint progress_signal;
    guint finished_signal;
    gboolean enrolled;
    gboolean enrolling;
    gboolean updating; /* widgets are set from the service, not by the user */
    /* Why the last step ended badly, on the face row instead of the state until the next one */
    char *face_note;

    AdwActionRow *face_row;
    GtkButton *setup_button;
    AdwActionRow *progress_row;
    GtkButton *cancel_button;
    AdwActionRow *try_row;
    GtkButton *try_button;
    AdwSwitchRow *unlock_row;
    AdwComboRow *strict_row;
    GtkWidget *remove_group;
};

G_DEFINE_TYPE (CcFaceDialog, cc_face_dialog, ADW_TYPE_DIALOG)

static void refresh (CcFaceDialog *self);

static void
call (CcFaceDialog *self, const char *method, GVariant *parameters, int timeout,
      GAsyncReadyCallback callback)
{
    if (!self->bus)
        return;
    g_dbus_connection_call (self->bus, FACE_BUS_NAME, FACE_PATH, FACE_INTERFACE, method, parameters,
                            NULL, G_DBUS_CALL_FLAGS_NONE, timeout, self->cancellable, callback, self);
}

/* The service's error, without the D-Bus prefix */
static const char *
error_text (GError *error)
{
    g_dbus_error_strip_remote_error (error);
    if (g_strcmp0 (error->message, "not authorized") == 0)
        return P_("Not authorized");
    if (g_strcmp0 (error->message, "the camera is in use for another request") == 0)
        return P_("The camera is busy, try again in a moment");
    return error->message;
}

static void
update_widgets (CcFaceDialog *self)
{
    gtk_widget_set_visible (GTK_WIDGET (self->progress_row), self->enrolling);
    gtk_widget_set_sensitive (GTK_WIDGET (self->setup_button), !self->enrolling);
    gtk_button_set_label (self->setup_button, self->enrolled ? P_("Set Up Again") : P_("Set Up"));
    adw_action_row_set_subtitle (self->face_row, self->face_note ? self->face_note
                                                 : self->enrolled ? P_("Set up") : P_("Not set up"));
    gtk_widget_set_visible (GTK_WIDGET (self->try_row), self->enrolled);
    gtk_widget_set_sensitive (GTK_WIDGET (self->try_button), !self->enrolling);
    gtk_widget_set_sensitive (GTK_WIDGET (self->unlock_row), self->enrolled && !self->enrolling);
    gtk_widget_set_visible (self->remove_group, self->enrolled && !self->enrolling);
}

/* @note: why a step ended badly (shown until the next step), NULL for a new step */
static void
set_face_note (CcFaceDialog *self, const char *note)
{
    g_free (self->face_note);
    self->face_note = g_strdup (note);
    if (note)
        adw_action_row_set_subtitle (self->face_row, note);
}

static void
on_user (GObject *source, GAsyncResult *res, gpointer data)
{
    g_autoptr (GError) error = NULL;
    g_autoptr (GVariant) reply = g_dbus_connection_call_finish (G_DBUS_CONNECTION (source), res, &error);
    CcFaceDialog *self;
    gboolean enabled;
    const char *strictness;

    if (g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
        return;
    self = CC_FACE_DIALOG (data);
    if (!reply) {
        adw_action_row_set_subtitle (self->face_row, error_text (error));
        return;
    }
    g_variant_get (reply, "(bb&s)", &self->enrolled, &enabled, &strictness);
    self->updating = TRUE;
    adw_switch_row_set_active (self->unlock_row, enabled);
    adw_combo_row_set_selected (self->strict_row, g_strcmp0 (strictness, "balanced") == 0 ? 1 : 0);
    self->updating = FALSE;
    update_widgets (self);
}

static void
refresh (CcFaceDialog *self)
{
    call (self, "GetUser", NULL, -1, on_user);
}

/* Replies that only need a refresh, or the error shown */
static void
on_done (GObject *source, GAsyncResult *res, gpointer data)
{
    g_autoptr (GError) error = NULL;
    g_autoptr (GVariant) reply = g_dbus_connection_call_finish (G_DBUS_CONNECTION (source), res, &error);
    CcFaceDialog *self;

    if (g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
        return;
    self = CC_FACE_DIALOG (data);
    if (!reply)
        set_face_note (self, error_text (error));
    refresh (self);
}

/* ----- Enrolling ----- */

static const char *
hint_text (const char *hint)
{
    if (g_strcmp0 (hint, "no-face") == 0)
        return P_("Look straight at the phone");
    if (g_strcmp0 (hint, "too-far") == 0)
        return P_("Hold the phone a little closer");
    if (g_strcmp0 (hint, "tilted") == 0)
        return P_("Hold the phone upright");
    if (g_strcmp0 (hint, "unclear") == 0)
        return P_("More light, please");
    return P_("Keep looking at the phone");
}

static void
on_enroll_signal (GDBusConnection *bus, const char *sender, const char *path, const char *iface,
                  const char *signal, GVariant *parameters, gpointer data)
{
    CcFaceDialog *self = CC_FACE_DIALOG (data);
    guint32 uid;

    if (g_strcmp0 (signal, "EnrollProgress") == 0) {
        guint32 percent;
        const char *hint;
        g_autofree char *subtitle = NULL;

        g_variant_get (parameters, "(uu&s)", &uid, &percent, &hint);
        if (uid != getuid () || !self->enrolling)
            return;
        /* TRANSLATORS: a hint how to hold the phone, then how far enrolling is */
        subtitle = g_strdup_printf (P_("%s – %u %%"), hint_text (hint), percent);
        adw_action_row_set_subtitle (self->progress_row, subtitle);
    } else if (g_strcmp0 (signal, "EnrollFinished") == 0) {
        gboolean success;
        const char *message;

        g_variant_get (parameters, "(ub&s)", &uid, &success, &message);
        if (uid != getuid ())
            return;
        self->enrolling = FALSE;
        set_face_note (self, success ? NULL :
                       g_strcmp0 (message, "cancelled") == 0 ? P_("Cancelled") :
                       P_("Not set up: the camera did not see the face clearly enough"));
        update_widgets (self);
        if (success)
            refresh (self);
    }
}

static void
on_enroll_started (GObject *source, GAsyncResult *res, gpointer data)
{
    g_autoptr (GError) error = NULL;
    g_autoptr (GVariant) reply = g_dbus_connection_call_finish (G_DBUS_CONNECTION (source), res, &error);
    CcFaceDialog *self;

    if (g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
        return;
    self = CC_FACE_DIALOG (data);
    if (!reply) {
        self->enrolling = FALSE;
        set_face_note (self, error_text (error));
        update_widgets (self);
        return;
    }
    adw_action_row_set_subtitle (self->progress_row, hint_text (""));
}

static void
on_setup_clicked (CcFaceDialog *self)
{
    self->enrolling = TRUE;
    set_face_note (self, NULL);
    adw_action_row_set_subtitle (self->progress_row, P_("Enter your PIN, then look at the phone"));
    update_widgets (self);
    /* The service first asks for the PIN (polkit), which may take a while */
    call (self, "Enroll", NULL, G_MAXINT, on_enroll_started);
}

static void
on_cancel_clicked (CcFaceDialog *self)
{
    call (self, "Cancel", NULL, -1, NULL);
}

/* ----- Trying ----- */

static void
on_verified (GObject *source, GAsyncResult *res, gpointer data)
{
    g_autoptr (GError) error = NULL;
    g_autoptr (GVariant) reply = g_dbus_connection_call_finish (G_DBUS_CONNECTION (source), res, &error);
    g_autofree char *text = NULL;
    CcFaceDialog *self;
    gboolean recognized;
    double similarity;

    if (g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
        return;
    self = CC_FACE_DIALOG (data);
    gtk_widget_set_sensitive (GTK_WIDGET (self->try_button), TRUE);
    gtk_widget_set_sensitive (GTK_WIDGET (self->setup_button), TRUE);
    if (!reply) {
        adw_action_row_set_subtitle (self->try_row, error_text (error));
        return;
    }
    g_variant_get (reply, "(bd)", &recognized, &similarity);
    /* TRANSLATORS: %d is how alike the face is to the stored one, in percent */
    text = g_strdup_printf (recognized ? P_("Recognized (%d %% alike)") : P_("Not recognized (%d %% alike)"),
                            (int) (similarity * 100 + 0.5));
    adw_action_row_set_subtitle (self->try_row, text);
}

static void
on_try_clicked (CcFaceDialog *self)
{
    gtk_widget_set_sensitive (GTK_WIDGET (self->try_button), FALSE);
    gtk_widget_set_sensitive (GTK_WIDGET (self->setup_button), FALSE);
    adw_action_row_set_subtitle (self->try_row, P_("Look at the phone"));
    call (self, "Verify", NULL, VERIFY_TIMEOUT_MS, on_verified);
}

/* ----- Settings ----- */

static void
on_unlock_changed (CcFaceDialog *self)
{
    if (self->updating)
        return;
    set_face_note (self, NULL);
    /* Switching on asks for the PIN; the switch follows the service's answer */
    call (self, "SetEnabled", g_variant_new ("(b)", adw_switch_row_get_active (self->unlock_row)),
          G_MAXINT, on_done);
}

static void
on_strict_changed (CcFaceDialog *self)
{
    if (self->updating)
        return;
    set_face_note (self, NULL);
    /* "balanced" asks for the PIN (polkit): no time limit, like switching face unlock on */
    call (self, "SetStrictness",
          g_variant_new ("(s)", adw_combo_row_get_selected (self->strict_row) == 1 ? "balanced" : "strict"),
          G_MAXINT, on_done);
}

static void
on_remove_response (AdwAlertDialog *alert, const char *response, CcFaceDialog *self)
{
    if (g_strcmp0 (response, "remove") != 0)
        return;
    adw_action_row_set_subtitle (self->try_row, "");
    set_face_note (self, NULL);
    call (self, "Delete", NULL, -1, on_done);
}

static void
on_remove_clicked (CcFaceDialog *self)
{
    AdwDialog *alert = adw_alert_dialog_new (P_("Remove Face?"),
                                             P_("Face unlock stops working until the face is set up again."));

    adw_alert_dialog_add_responses (ADW_ALERT_DIALOG (alert), "cancel", P_("Cancel"), "remove", P_("Remove"), NULL);
    adw_alert_dialog_set_response_appearance (ADW_ALERT_DIALOG (alert), "remove", ADW_RESPONSE_DESTRUCTIVE);
    adw_alert_dialog_set_close_response (ADW_ALERT_DIALOG (alert), "cancel");
    g_signal_connect_object (alert, "response", G_CALLBACK (on_remove_response), self, 0);
    adw_dialog_present (alert, GTK_WIDGET (self));
}

/* ----- Setup ----- */

static void
on_bus (GObject *source, GAsyncResult *res, gpointer data)
{
    g_autoptr (GError) error = NULL;
    GDBusConnection *bus = g_bus_get_finish (res, &error);
    CcFaceDialog *self;

    if (!bus) {
        if (!g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
            g_warning ("system bus: %s", error->message);
        return;
    }
    self = CC_FACE_DIALOG (data);
    self->bus = bus;
    self->progress_signal = g_dbus_connection_signal_subscribe (
        bus, FACE_BUS_NAME, FACE_INTERFACE, "EnrollProgress", FACE_PATH, NULL,
        G_DBUS_SIGNAL_FLAGS_NONE, on_enroll_signal, self, NULL);
    self->finished_signal = g_dbus_connection_signal_subscribe (
        bus, FACE_BUS_NAME, FACE_INTERFACE, "EnrollFinished", FACE_PATH, NULL,
        G_DBUS_SIGNAL_FLAGS_NONE, on_enroll_signal, self, NULL);
    refresh (self);
}

static void
on_cancel_sent (GObject *source, GAsyncResult *res, gpointer data)
{
    g_autoptr (GVariant) reply = g_dbus_connection_call_finish (G_DBUS_CONNECTION (source), res, NULL);
}

static void
cc_face_dialog_dispose (GObject *object)
{
    CcFaceDialog *self = CC_FACE_DIALOG (object);

    /* Closing while enrolling stops the camera */
    if (self->enrolling && self->bus)
        /* With a callback the call holds the bus until it is answered: dropped below, it could
         * go before the message is sent */
        g_dbus_connection_call (self->bus, FACE_BUS_NAME, FACE_PATH, FACE_INTERFACE, "Cancel", NULL,
                                NULL, G_DBUS_CALL_FLAGS_NONE, -1, NULL, on_cancel_sent, NULL);
    self->enrolling = FALSE;
    g_cancellable_cancel (self->cancellable);
    g_clear_object (&self->cancellable);
    if (self->bus) {
        g_dbus_connection_signal_unsubscribe (self->bus, self->progress_signal);
        g_dbus_connection_signal_unsubscribe (self->bus, self->finished_signal);
    }
    g_clear_object (&self->bus);
    g_clear_pointer (&self->face_note, g_free);

    G_OBJECT_CLASS (cc_face_dialog_parent_class)->dispose (object);
}

static void
cc_face_dialog_class_init (CcFaceDialogClass *klass)
{
    G_OBJECT_CLASS (klass)->dispose = cc_face_dialog_dispose;
}

static GtkButton *
row_button (AdwActionRow *row, const char *label)
{
    GtkWidget *button = gtk_button_new_with_label (label);

    gtk_widget_set_valign (button, GTK_ALIGN_CENTER);
    adw_action_row_add_suffix (row, button);
    return GTK_BUTTON (button);
}

static void
cc_face_dialog_init (CcFaceDialog *self)
{
    GtkWidget *toolbar = adw_toolbar_view_new ();
    GtkWidget *page = adw_preferences_page_new ();
    GtkWidget *group, *button;
    GtkStringList *levels;

    self->cancellable = g_cancellable_new ();

    adw_dialog_set_title (ADW_DIALOG (self), P_("Face Unlock"));
    adw_dialog_set_content_width (ADW_DIALOG (self), 360);
    adw_toolbar_view_add_top_bar (ADW_TOOLBAR_VIEW (toolbar), adw_header_bar_new ());
    adw_toolbar_view_set_content (ADW_TOOLBAR_VIEW (toolbar), page);
    adw_dialog_set_child (ADW_DIALOG (self), toolbar);

    /* The face: set up, progress, try it */
    group = adw_preferences_group_new ();
    adw_preferences_group_set_description (
        ADW_PREFERENCES_GROUP (group),
        P_("The phone recognizes your face with the front camera, computed on its own AI unit. "
           "Nothing leaves the phone, and only a description of the face is stored, no pictures."));
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (page), ADW_PREFERENCES_GROUP (group));

    self->face_row = ADW_ACTION_ROW (adw_action_row_new ());
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (self->face_row), P_("Face"));
    self->setup_button = row_button (self->face_row, P_("Set Up"));
    g_signal_connect_swapped (self->setup_button, "clicked", G_CALLBACK (on_setup_clicked), self);
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), GTK_WIDGET (self->face_row));

    self->progress_row = ADW_ACTION_ROW (adw_action_row_new ());
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (self->progress_row), P_("Setting Up"));
    self->cancel_button = row_button (self->progress_row, P_("Cancel"));
    g_signal_connect_swapped (self->cancel_button, "clicked", G_CALLBACK (on_cancel_clicked), self);
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), GTK_WIDGET (self->progress_row));

    self->try_row = ADW_ACTION_ROW (adw_action_row_new ());
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (self->try_row), P_("Try It"));
    self->try_button = row_button (self->try_row, P_("Try"));
    g_signal_connect_swapped (self->try_button, "clicked", G_CALLBACK (on_try_clicked), self);
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), GTK_WIDGET (self->try_row));

    /* Unlocking and how strict */
    group = adw_preferences_group_new ();
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (page), ADW_PREFERENCES_GROUP (group));
    self->unlock_row = ADW_SWITCH_ROW (adw_switch_row_new ());
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (self->unlock_row), P_("Unlock the Screen"));
    adw_action_row_set_subtitle (
        ADW_ACTION_ROW (self->unlock_row),
        P_("A good photo or video of you may unlock it too: the checks against photos are still to "
           "come"));
    g_signal_connect_swapped (self->unlock_row, "notify::active", G_CALLBACK (on_unlock_changed), self);
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), GTK_WIDGET (self->unlock_row));

    levels = gtk_string_list_new (NULL);
    gtk_string_list_append (levels, P_("Strict"));
    gtk_string_list_append (levels, P_("Balanced"));
    self->strict_row = ADW_COMBO_ROW (adw_combo_row_new ());
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (self->strict_row), P_("Strictness"));
    adw_action_row_set_subtitle (
        ADW_ACTION_ROW (self->strict_row),
        P_("Strict asks for the PIN more often, Balanced lets a similar face through more often"));
    adw_combo_row_set_model (self->strict_row, G_LIST_MODEL (levels));
    g_object_unref (levels);
    g_signal_connect_swapped (self->strict_row, "notify::selected", G_CALLBACK (on_strict_changed), self);
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), GTK_WIDGET (self->strict_row));

    /* What face unlock never does */
    group = adw_preferences_group_new ();
    adw_preferences_group_set_description (
        ADW_PREFERENCES_GROUP (group),
        P_("After a restart and after several failed tries the PIN is needed. The face never "
           "replaces the PIN for administrator rights, the keyring or the encryption password."));
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (page), ADW_PREFERENCES_GROUP (group));

    self->remove_group = adw_preferences_group_new ();
    button = gtk_button_new_with_label (P_("Remove Face"));
    gtk_widget_set_halign (button, GTK_ALIGN_CENTER);
    gtk_widget_add_css_class (button, "pill");
    gtk_widget_add_css_class (button, "destructive-action");
    g_signal_connect_swapped (button, "clicked", G_CALLBACK (on_remove_clicked), self);
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (self->remove_group), button);
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (page), ADW_PREFERENCES_GROUP (self->remove_group));

    update_widgets (self);
    g_bus_get (G_BUS_TYPE_SYSTEM, self->cancellable, on_bus, self);
}

CcFaceDialog *
cc_face_dialog_new (void)
{
    return g_object_new (CC_TYPE_FACE_DIALOG, NULL);
}

/* plinius-face is installed (its service can be started through D-Bus) */
gboolean
cc_face_dialog_is_available (void)
{
    return g_file_test (FACE_ACTIVATION, G_FILE_TEST_EXISTS);
}

static void
on_row_user (GObject *source, GAsyncResult *res, gpointer data)
{
    g_autoptr (CcListRow) row = data;
    g_autoptr (GError) error = NULL;
    g_autoptr (GVariant) reply = g_dbus_connection_call_finish (G_DBUS_CONNECTION (source), res, &error);
    gboolean enrolled, enabled;
    const char *strictness;

    if (!reply)
        return;
    g_variant_get (reply, "(bb&s)", &enrolled, &enabled, &strictness);
    cc_list_row_set_secondary_label (row, !enrolled ? P_("Not Set Up") : enabled ? P_("On") : P_("Off"));
}

static void
on_row_bus (GObject *source, GAsyncResult *res, gpointer data)
{
    g_autoptr (GDBusConnection) bus = g_bus_get_finish (res, NULL);

    if (!bus) {
        g_object_unref (data);
        return;
    }
    g_dbus_connection_call (bus, FACE_BUS_NAME, FACE_PATH, FACE_INTERFACE, "GetUser", NULL, NULL,
                            G_DBUS_CALL_FLAGS_NONE, -1, NULL, on_row_user, data);
}

void
cc_face_dialog_update_row (CcListRow *row)
{
    g_bus_get (G_BUS_TYPE_SYSTEM, NULL, on_row_bus, g_object_ref (row));
}
