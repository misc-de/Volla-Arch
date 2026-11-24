/*
 * cc-plinius-crypt.c
 *
 * Plinius: "Encryption" page in Privacy & Security, for plinius-crypt (the system encrypted with
 * a password asked at startup). It shows the state (plinius-encrypt --status) and offers what fits
 * it: encrypt at the next restart, withdraw that request, or change the password.
 * Changes run plinius-encrypt through pkexec (polkit action org.plinius.crypt.manage); passwords go
 * to it on stdin, never on the command line, and its exit codes say what went wrong.
 * Our own strings use the gettext domain plinius-control-center (not GNOME's po/).
 *
 * SPDX-License-Identifier: MIT
 */

#undef G_LOG_DOMAIN
#define G_LOG_DOMAIN "cc-plinius-crypt"

#include <glib/gi18n.h>
#include <string.h>

#include "cc-plinius-crypt.h"

#define PLINIUS_DOMAIN "plinius-control-center"
#define P_(String) g_dgettext (PLINIUS_DOMAIN, String)

#define ENCRYPT_TOOL "/usr/bin/plinius-encrypt"

/* Exit codes of plinius-encrypt (see the top of the script) and of pkexec */
enum {
    EXIT_WRONG_PASSWORD   = 3,
    EXIT_UNTYPEABLE       = 4,
    EXIT_IS_PIN           = 5,
    EXIT_UNSUPPORTED      = 7,
    EXIT_PKEXEC_DISMISSED = 126,
    EXIT_PKEXEC_DENIED    = 127,
};

typedef struct {
    AdwNavigationPage *page;
    AdwActionRow      *state_row;
    GtkImage          *state_icon;
    AdwButtonRow      *encrypt_row;
    AdwButtonRow      *cancel_row;
    AdwButtonRow      *password_row;
    GCancellable      *cancellable;
    /* The open password dialog, if any */
    AdwDialog         *dialog;
    AdwPasswordEntryRow *current_entry;
    AdwPasswordEntryRow *new_entry;
    AdwPasswordEntryRow *repeat_entry;
    GtkLabel          *dialog_error;
    GtkWidget         *change_button;
    GtkWidget         *cancel_button;
    GtkWidget         *dialog_content;
    /* Spinner and note while plinius-encrypt changes the password */
    GtkWidget         *busy_bar;
} CryptPage;

static void
crypt_page_free (CryptPage *cp)
{
    g_cancellable_cancel (cp->cancellable);
    g_clear_object (&cp->cancellable);
    g_free (cp);
}

/* ----- Running plinius-encrypt ----- */

typedef void (*ToolDone) (CryptPage *cp, int status, const char *message);

typedef struct {
    CryptPage *cp;
    ToolDone   done;
} ToolCall;

static void
tool_done_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
    g_autofree ToolCall *call = user_data;
    GSubprocess *process = G_SUBPROCESS (source);
    g_autoptr(GError) error = NULL;
    g_autofree char *err = NULL;
    int status = 1;

    if (!g_subprocess_communicate_utf8_finish (process, result, NULL, &err, &error)) {
        /* The page is gone: nothing to update */
        if (g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
            return;
        g_warning ("plinius-encrypt: %s", error->message);
    } else if (g_subprocess_get_if_exited (process)) {
        status = g_subprocess_get_exit_status (process);
    }
    call->done (call->cp, status, g_strstrip (err ? err : g_strdup ("")));
}

/* Runs "pkexec plinius-encrypt OPTION..." with @input on stdin, then @done with the exit status
 * and the tool's message (stderr, already translated by plinius-crypt) */
static void
run_tool (CryptPage *cp, const char * const *options, const char *input, ToolDone done)
{
    g_autoptr(GPtrArray) argv = g_ptr_array_new ();
    g_autoptr(GSubprocess) process = NULL;
    g_autoptr(GError) error = NULL;
    ToolCall *call;

    g_ptr_array_add (argv, (gpointer) "pkexec");
    g_ptr_array_add (argv, (gpointer) ENCRYPT_TOOL);
    for (const char * const *o = options; *o; o++)
        g_ptr_array_add (argv, (gpointer) *o);
    g_ptr_array_add (argv, NULL);

    process = g_subprocess_newv ((const char * const *) argv->pdata,
                                 G_SUBPROCESS_FLAGS_STDIN_PIPE | G_SUBPROCESS_FLAGS_STDERR_PIPE |
                                 G_SUBPROCESS_FLAGS_STDOUT_SILENCE, &error);
    if (process == NULL) {
        done (cp, 1, error->message);
        return;
    }
    call = g_new0 (ToolCall, 1);
    call->cp = cp;
    call->done = done;
    g_subprocess_communicate_utf8_async (process, input, cp->cancellable, tool_done_cb, call);
}

/* An error the user should see, unless they just closed the authentication dialog */
static void
show_error (CryptPage *cp, int status, const char *heading, const char *message)
{
    AdwDialog *alert;

    if (status == EXIT_PKEXEC_DISMISSED || status == EXIT_PKEXEC_DENIED)
        return;
    alert = adw_alert_dialog_new (heading, message && *message ? message : NULL);
    adw_alert_dialog_add_response (ADW_ALERT_DIALOG (alert), "close", P_("_Close"));
    adw_dialog_present (alert, GTK_WIDGET (cp->page));
}

/* ----- State ----- */

static void
update_state (CryptPage *cp)
{
    g_autoptr(GSubprocess) process = NULL;
    g_autofree char *out = NULL;
    const char *state = "", *title, *subtitle, *icon;

    process = g_subprocess_new (G_SUBPROCESS_FLAGS_STDOUT_PIPE | G_SUBPROCESS_FLAGS_STDERR_SILENCE, NULL,
                                ENCRYPT_TOOL, "--status", NULL);
    if (process && g_subprocess_communicate_utf8 (process, NULL, NULL, &out, NULL, NULL) && out)
        state = g_strstrip (out);

    if (g_str_equal (state, "encrypted")) {
        title = P_("Encrypted");
        subtitle = P_("The phone asks for the encryption password at every start. Without it, nobody can read the data.");
        icon = "security-high-symbolic";
    } else if (g_str_equal (state, "requested")) {
        title = P_("Encryption at the Next Restart");
        subtitle = P_("At the next start the phone asks for a new encryption password, then encrypts. This takes a while, keep the phone charging.");
        icon = "security-medium-symbolic";
    } else if (g_str_equal (state, "off")) {
        title = P_("Not Encrypted");
        subtitle = P_("Whoever has the phone in hand can read the data from its storage.");
        icon = "security-low-symbolic";
    } else {
        title = P_("Not Available");
        subtitle = P_("This system does not start with the encryption support, so it cannot be encrypted.");
        icon = "dialog-information-symbolic";
    }
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (cp->state_row), title);
    adw_action_row_set_subtitle (cp->state_row, subtitle);
    gtk_image_set_from_icon_name (cp->state_icon, icon);
    gtk_widget_set_visible (GTK_WIDGET (cp->encrypt_row), g_str_equal (state, "off"));
    gtk_widget_set_visible (GTK_WIDGET (cp->cancel_row), g_str_equal (state, "requested"));
    gtk_widget_set_visible (GTK_WIDGET (cp->password_row), g_str_equal (state, "encrypted"));
}

/* ----- Encrypt, cancel ----- */

static void
request_done (CryptPage *cp, int status, const char *message)
{
    update_state (cp);
    if (status == 0)
        return;
    show_error (cp, status, status == EXIT_UNSUPPORTED ? P_("Encryption Not Possible") : P_("Encryption Failed"),
                message);
}

static void
encrypt_response_cb (AdwAlertDialog *alert, const char *response, CryptPage *cp)
{
    const char * const request[] = { "--request", NULL };

    if (g_str_equal (response, "encrypt"))
        run_tool (cp, request, NULL, request_done);
}

static void
encrypt_activated_cb (CryptPage *cp)
{
    AdwDialog *alert;

    alert = adw_alert_dialog_new (P_("Encrypt the Phone?"),
                                  P_("At the next restart the phone asks twice for a new encryption password and then encrypts the system. "
                                     "Depending on the amount of data this takes some minutes; keep the phone charging and do not switch it off.\n\n"
                                     "From then on the password is asked at every start. It cannot be recovered: "
                                     "without it, the data is lost. It must differ from the screen lock PIN."));
    adw_alert_dialog_add_responses (ADW_ALERT_DIALOG (alert),
                                    "cancel", P_("_Cancel"),
                                    "encrypt", P_("_Encrypt at Restart"),
                                    NULL);
    adw_alert_dialog_set_response_appearance (ADW_ALERT_DIALOG (alert), "encrypt", ADW_RESPONSE_SUGGESTED);
    adw_alert_dialog_set_close_response (ADW_ALERT_DIALOG (alert), "cancel");
    g_signal_connect (alert, "response", G_CALLBACK (encrypt_response_cb), cp);
    adw_dialog_present (alert, GTK_WIDGET (cp->page));
}

static void
cancel_done (CryptPage *cp, int status, const char *message)
{
    update_state (cp);
    if (status != 0)
        show_error (cp, status, P_("Cancelling Failed"), message);
}

static void
cancel_activated_cb (CryptPage *cp)
{
    const char * const cancel[] = { "--cancel", NULL };

    run_tool (cp, cancel, NULL, cancel_done);
}

/* ----- Change password ----- */

static void
dialog_set_error (CryptPage *cp, const char *text, AdwPasswordEntryRow *row)
{
    AdwPasswordEntryRow *rows[] = { cp->current_entry, cp->new_entry, cp->repeat_entry };

    gtk_label_set_text (cp->dialog_error, text ? text : "");
    gtk_widget_set_visible (GTK_WIDGET (cp->dialog_error), text != NULL);
    for (guint i = 0; i < G_N_ELEMENTS (rows); i++) {
        if (rows[i] == row)
            gtk_widget_add_css_class (GTK_WIDGET (rows[i]), "error");
        else
            gtk_widget_remove_css_class (GTK_WIDGET (rows[i]), "error");
    }
}

/* Change only with all three fields filled and the new password twice the same */
static void
dialog_entries_changed_cb (CryptPage *cp)
{
    const char *current = gtk_editable_get_text (GTK_EDITABLE (cp->current_entry));
    const char *new = gtk_editable_get_text (GTK_EDITABLE (cp->new_entry));
    const char *repeat = gtk_editable_get_text (GTK_EDITABLE (cp->repeat_entry));
    gboolean mismatch = *repeat && !g_str_equal (new, repeat);

    dialog_set_error (cp, mismatch ? P_("The new passwords do not match.") : NULL,
                      mismatch ? cp->repeat_entry : NULL);
    gtk_widget_set_sensitive (cp->change_button, *current && *new && *repeat && !mismatch);
}

/* While the password changes (some seconds: the old and the new key are both derived with
 * argon2id), the fields and buttons are locked and a spinner with a note says that it works */
static void
dialog_set_busy (CryptPage *cp, gboolean busy)
{
    gtk_widget_set_sensitive (cp->dialog_content, !busy);
    gtk_widget_set_sensitive (cp->cancel_button, !busy);
    gtk_widget_set_visible (cp->busy_bar, busy);
    adw_dialog_set_can_close (cp->dialog, !busy);
    if (busy)
        gtk_widget_set_sensitive (cp->change_button, FALSE);
    else
        dialog_entries_changed_cb (cp);
}

static void
change_done (CryptPage *cp, int status, const char *message)
{
    if (cp->dialog == NULL)
        return;
    dialog_set_busy (cp, FALSE);
    switch (status) {
    case 0:
        adw_dialog_close (cp->dialog);
        update_state (cp);
        return;
    case EXIT_PKEXEC_DISMISSED:
    case EXIT_PKEXEC_DENIED:
        return;
    case EXIT_WRONG_PASSWORD:
        dialog_set_error (cp, P_("The current password is wrong."), cp->current_entry);
        return;
    case EXIT_UNTYPEABLE:
        /* plinius-encrypt names the characters the keyboard at startup lacks */
        dialog_set_error (cp, message, cp->new_entry);
        return;
    case EXIT_IS_PIN:
        dialog_set_error (cp, P_("The new password is the screen lock PIN. Choose a different one, so that the PIN alone does not open the phone."),
                          cp->new_entry);
        return;
    default:
        dialog_set_error (cp, *message ? message : P_("Changing the password failed."), NULL);
        return;
    }
}

static void
change_clicked_cb (CryptPage *cp)
{
    const char * const change[] = { "--change-password", "--stdin", NULL };
    g_autofree char *input = NULL;

    /* Current password on the first line, the new one on the second */
    input = g_strdup_printf ("%s\n%s\n", gtk_editable_get_text (GTK_EDITABLE (cp->current_entry)),
                             gtk_editable_get_text (GTK_EDITABLE (cp->new_entry)));
    dialog_set_error (cp, NULL, NULL);
    dialog_set_busy (cp, TRUE);
    run_tool (cp, change, input, change_done);
    /* Do not keep the passwords around longer than needed */
    memset (input, 0, strlen (input));
}

static void
dialog_closed_cb (CryptPage *cp)
{
    cp->dialog = NULL;
}

static AdwPasswordEntryRow *
add_password_entry (CryptPage *cp, AdwPreferencesGroup *group, const char *title)
{
    GtkWidget *row = adw_password_entry_row_new ();

    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), title);
    g_signal_connect_swapped (row, "changed", G_CALLBACK (dialog_entries_changed_cb), cp);
    adw_preferences_group_add (group, row);
    return ADW_PASSWORD_ENTRY_ROW (row);
}

static void
password_activated_cb (CryptPage *cp)
{
    GtkWidget *toolbar, *header, *page, *group, *cancel;

    cp->dialog = adw_dialog_new ();
    adw_dialog_set_title (cp->dialog, P_("Change Password"));
    adw_dialog_set_content_width (cp->dialog, 420);

    header = adw_header_bar_new ();
    adw_header_bar_set_show_start_title_buttons (ADW_HEADER_BAR (header), FALSE);
    adw_header_bar_set_show_end_title_buttons (ADW_HEADER_BAR (header), FALSE);
    cancel = cp->cancel_button = gtk_button_new_with_mnemonic (P_("_Cancel"));
    g_signal_connect_swapped (cancel, "clicked", G_CALLBACK (adw_dialog_close), cp->dialog);
    adw_header_bar_pack_start (ADW_HEADER_BAR (header), cancel);
    cp->change_button = gtk_button_new_with_mnemonic (P_("C_hange"));
    gtk_widget_add_css_class (cp->change_button, "suggested-action");
    gtk_widget_set_sensitive (cp->change_button, FALSE);
    g_signal_connect_swapped (cp->change_button, "clicked", G_CALLBACK (change_clicked_cb), cp);
    adw_header_bar_pack_end (ADW_HEADER_BAR (header), cp->change_button);

    page = adw_preferences_page_new ();
    group = adw_preferences_group_new ();
    adw_preferences_group_set_description (ADW_PREFERENCES_GROUP (group),
                                           P_("The password is asked at every start, on the keyboard shown there. It must differ from the screen lock PIN."));
    cp->current_entry = add_password_entry (cp, ADW_PREFERENCES_GROUP (group), P_("Current Password"));
    cp->new_entry = add_password_entry (cp, ADW_PREFERENCES_GROUP (group), P_("New Password"));
    cp->repeat_entry = add_password_entry (cp, ADW_PREFERENCES_GROUP (group), P_("Repeat New Password"));
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (page), ADW_PREFERENCES_GROUP (group));

    cp->dialog_error = GTK_LABEL (gtk_label_new (NULL));
    gtk_label_set_wrap (cp->dialog_error, TRUE);
    gtk_label_set_xalign (cp->dialog_error, 0);
    gtk_widget_add_css_class (GTK_WIDGET (cp->dialog_error), "error");
    gtk_widget_set_visible (GTK_WIDGET (cp->dialog_error), FALSE);
    group = adw_preferences_group_new ();
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), GTK_WIDGET (cp->dialog_error));
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (page), ADW_PREFERENCES_GROUP (group));

    /* Below the fields, outside the page so that it is not greyed out with them */
    {
        GtkWidget *spinner = adw_spinner_new ();
        GtkWidget *note = gtk_label_new (P_("Changing the password. This takes a few seconds, do not switch the phone off."));

        gtk_widget_set_size_request (spinner, 24, 24);
        gtk_label_set_wrap (GTK_LABEL (note), TRUE);
        gtk_label_set_xalign (GTK_LABEL (note), 0);
        gtk_widget_set_hexpand (note, TRUE);
        cp->busy_bar = gtk_box_new (GTK_ORIENTATION_HORIZONTAL, 12);
        gtk_widget_set_margin_start (cp->busy_bar, 18);
        gtk_widget_set_margin_end (cp->busy_bar, 18);
        gtk_widget_set_margin_top (cp->busy_bar, 12);
        gtk_widget_set_margin_bottom (cp->busy_bar, 18);
        gtk_box_append (GTK_BOX (cp->busy_bar), spinner);
        gtk_box_append (GTK_BOX (cp->busy_bar), note);
        gtk_widget_set_visible (cp->busy_bar, FALSE);
    }
    cp->dialog_content = page;

    toolbar = adw_toolbar_view_new ();
    adw_toolbar_view_add_top_bar (ADW_TOOLBAR_VIEW (toolbar), header);
    adw_toolbar_view_add_bottom_bar (ADW_TOOLBAR_VIEW (toolbar), cp->busy_bar);
    adw_toolbar_view_set_content (ADW_TOOLBAR_VIEW (toolbar), page);
    adw_dialog_set_child (cp->dialog, toolbar);
    g_signal_connect_swapped (cp->dialog, "closed", G_CALLBACK (dialog_closed_cb), cp);
    adw_dialog_present (cp->dialog, GTK_WIDGET (cp->page));
}

/* ----- Page ----- */

static AdwButtonRow *
add_button_row (AdwPreferencesGroup *group, const char *title, GCallback callback, CryptPage *cp)
{
    GtkWidget *row = adw_button_row_new ();

    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), title);
    adw_preferences_row_set_use_underline (ADW_PREFERENCES_ROW (row), TRUE);
    g_signal_connect_swapped (row, "activated", callback, cp);
    adw_preferences_group_add (group, row);
    return ADW_BUTTON_ROW (row);
}

/**
 * cc_plinius_crypt_page_new:
 *
 * The "Encryption" subpage (tag "encryption"), or %NULL without plinius-crypt.
 */
AdwNavigationPage *
cc_plinius_crypt_page_new (void)
{
    GtkWidget *toolbar, *page, *group;
    CryptPage *cp;

    if (!g_file_test (ENCRYPT_TOOL, G_FILE_TEST_IS_EXECUTABLE))
        return NULL;

    cp = g_new0 (CryptPage, 1);
    cp->cancellable = g_cancellable_new ();

    page = adw_preferences_page_new ();
    group = adw_preferences_group_new ();
    cp->state_row = ADW_ACTION_ROW (adw_action_row_new ());
    cp->state_icon = GTK_IMAGE (gtk_image_new ());
    adw_action_row_add_prefix (cp->state_row, GTK_WIDGET (cp->state_icon));
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), GTK_WIDGET (cp->state_row));
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (page), ADW_PREFERENCES_GROUP (group));

    group = adw_preferences_group_new ();
    cp->encrypt_row = add_button_row (ADW_PREFERENCES_GROUP (group), P_("_Encrypt…"),
                                      G_CALLBACK (encrypt_activated_cb), cp);
    gtk_widget_add_css_class (GTK_WIDGET (cp->encrypt_row), "suggested-action");
    cp->cancel_row = add_button_row (ADW_PREFERENCES_GROUP (group), P_("_Cancel Encryption"),
                                     G_CALLBACK (cancel_activated_cb), cp);
    cp->password_row = add_button_row (ADW_PREFERENCES_GROUP (group), P_("Change _Password…"),
                                       G_CALLBACK (password_activated_cb), cp);
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (page), ADW_PREFERENCES_GROUP (group));

    toolbar = adw_toolbar_view_new ();
    adw_toolbar_view_add_top_bar (ADW_TOOLBAR_VIEW (toolbar), adw_header_bar_new ());
    adw_toolbar_view_set_content (ADW_TOOLBAR_VIEW (toolbar), page);

    cp->page = ADW_NAVIGATION_PAGE (adw_navigation_page_new_with_tag (toolbar, P_("Encryption"), "encryption"));
    g_object_set_data_full (G_OBJECT (cp->page), "plinius-crypt", cp, (GDestroyNotify) crypt_page_free);
    /* The state can change outside (plinius-encrypt in a terminal): read it whenever the page shows */
    g_signal_connect_swapped (cp->page, "showing", G_CALLBACK (update_state), cp);
    update_state (cp);
    return cp->page;
}

/**
 * cc_plinius_crypt_row_setup:
 * @row: the row on the Privacy & Security page that opens the subpage
 *
 * Gives @row its texts (our gettext domain) and shows it.
 */
void
cc_plinius_crypt_row_setup (AdwActionRow *row)
{
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_("_Encryption"));
    adw_action_row_set_subtitle (row, P_("Password at startup for the data on the phone"));
    gtk_widget_set_visible (GTK_WIDGET (row), TRUE);
}
