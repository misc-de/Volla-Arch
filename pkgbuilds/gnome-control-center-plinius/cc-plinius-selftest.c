/*
 * cc-plinius-selftest.c
 *
 * Plinius: "Self-Test" in System, what plinius-health found a few minutes after the start
 * (/run/plinius-health/result, one line per check: "ok|fail|skip NAME: message"). Its
 * notification after a failed check opens this page (launch-panel system plinius-self-test).
 *
 * - One row per check with its state. The messages of plinius-health are English and technical:
 *   the page says in its own words what does not work, "Copy" puts the original lines on the
 *   clipboard (for a bug report).
 * - "Check Again": "pkexec plinius-health check" (org.plinius.health.check, no password).
 * - After a failed check, "Take Back Update": what the last update changed comes from
 *   "plinius-rollback summary" (lines when=, packages=, missing=; exit status 3 = earlier
 *   versions missing from the cache, 4 = no update). After a question it runs
 *   "pkexec plinius-rollback run --noconfirm --unit" (org.plinius.rollback.run, the PIN): in a
 *   system unit of its own, so that closing Settings cannot stop pacman in the middle.
 *
 * Without plinius-health the row stays hidden. Our own strings use the gettext domain
 * plinius-control-center (not GNOME's po/).
 *
 * SPDX-License-Identifier: MIT
 */

#undef G_LOG_DOMAIN
#define G_LOG_DOMAIN "cc-plinius-selftest"

#include <glib/gi18n.h>
#include <string.h>

#include "cc-plinius-selftest.h"

#define PLINIUS_DOMAIN "plinius-control-center"
#define P_(String) g_dgettext (PLINIUS_DOMAIN, String)
#define NP_(Singular, Plural, N) g_dngettext (PLINIUS_DOMAIN, Singular, Plural, N)

#define HEALTH_RUN    "/run/plinius-health"
#define HEALTH_TOOL   "/usr/bin/plinius-health"
#define ROLLBACK_TOOL "/usr/bin/plinius-rollback"
#define PAGE_TAG      "plinius-self-test"

/* Exit codes of pkexec: prompt dismissed, not authorized */
#define PKEXEC_DISMISSED      126
#define PKEXEC_NOT_AUTHORIZED 127
/* Of plinius-health check: a check failed (the check itself ran) */
#define HEALTH_FAILED         2
/* Of plinius-rollback: earlier versions missing from the cache, no update to take back */
#define ROLLBACK_MISSING      3
#define ROLLBACK_NOTHING      4

/* The checks of plinius-health, in its order; a check it adds later shows with its own name */
static const struct {
    const char *name;
    const char *title;
    const char *failed;  /* what does not work, for people */
} checks[] = {
    { "units", N_("System Services"), N_("A service of the system did not start.") },
    { "user_units", N_("Session Services"), N_("A service of your session did not start.") },
    { "container", N_("Android Container"),
      N_("The Android container is not running. Without it there is no modem, Bluetooth, camera or sensors.") },
    { "display", N_("Display"), N_("The display does not run on the graphics chip, or not at all.") },
    { "audio", N_("Audio"), N_("The speaker and microphone of the phone are missing.") },
    { "modem", N_("Modem"), N_("No modem: no calls, messages or mobile data.") },
    { "wifi", N_("Wi-Fi"), N_("The Wi-Fi adapter is missing.") },
    { "bluetooth", N_("Bluetooth"), N_("The Bluetooth adapter is missing.") },
};

typedef enum {
    STATE_OK,
    STATE_FAIL,
    STATE_SKIP,
} State;

typedef struct {
    AdwPreferencesPage *prefs;
    AdwToastOverlay    *toast_overlay;
    GCancellable       *cancellable;
    GPtrArray          *groups;
    char               *result;     /* the result file as read, for "Copy" */
    /* Check Again */
    AdwButtonRow       *check_row;
    gboolean            checking;
    /* Take Back Update */
    AdwPreferencesGroup *rollback_group;
    GtkWidget           *rollback_button;
    guint                rollback_packages;
    char                *rollback_when;
    gboolean             rolling_back;
    char                *rollback_done;  /* the outcome, shown until the next start of the page */
} SelfTestPage;

static void update_page (SelfTestPage *sp);

static void
selftest_page_free (SelfTestPage *sp)
{
    g_cancellable_cancel (sp->cancellable);
    g_clear_object (&sp->cancellable);
    g_ptr_array_unref (sp->groups);
    g_free (sp->result);
    g_free (sp->rollback_when);
    g_free (sp->rollback_done);
    g_free (sp);
}

/* ----- Helpers ----- */

static GtkWidget *
add_group (SelfTestPage *sp, const char *title, const char *description)
{
    GtkWidget *group = adw_preferences_group_new ();

    if (title)
        adw_preferences_group_set_title (ADW_PREFERENCES_GROUP (group), title);
    if (description)
        adw_preferences_group_set_description (ADW_PREFERENCES_GROUP (group), description);
    adw_preferences_page_add (sp->prefs, ADW_PREFERENCES_GROUP (group));
    g_ptr_array_add (sp->groups, group);
    return group;
}

static GtkWidget *
text_row_new (const char *title, const char *subtitle)
{
    GtkWidget *row = adw_action_row_new ();

    adw_preferences_row_set_use_markup (ADW_PREFERENCES_ROW (row), FALSE);
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), title);
    adw_action_row_set_subtitle (ADW_ACTION_ROW (row), subtitle);
    adw_action_row_set_subtitle_lines (ADW_ACTION_ROW (row), 0);
    return row;
}

static GtkWidget *
state_icon_new (State state, int size)
{
    static const char * const icons[] = {
        [STATE_OK] = "object-select-symbolic",
        [STATE_FAIL] = "dialog-error-symbolic",
        [STATE_SKIP] = "dialog-information-symbolic",
    };
    static const char * const classes[] = {
        [STATE_OK] = "success",
        [STATE_FAIL] = "error",
        [STATE_SKIP] = "dimmed",
    };
    GtkWidget *icon = gtk_image_new_from_icon_name (icons[state]);

    gtk_image_set_pixel_size (GTK_IMAGE (icon), size);
    gtk_widget_add_css_class (icon, classes[state]);
    return icon;
}

static void
toast (SelfTestPage *sp, const char *text)
{
    adw_toast_overlay_add_toast (sp->toast_overlay, adw_toast_new (text));
}

/* "20.11.26 06:13" from pacman.log's "2026-11-20T06:13:02+0100", else as it is */
static char *
format_when (const char *when)
{
    g_autoptr(GDateTime) dt = g_date_time_new_from_iso8601 (when, NULL);
    g_autoptr(GDateTime) local = dt ? g_date_time_to_local (dt) : NULL;

    return local ? g_date_time_format (local, "%x %H:%M") : g_strdup (when);
}

/* The exit status of a finished process, -1 if it did not exit */
static int
exit_status (GSubprocess *process, GError *error)
{
    if (error || !g_subprocess_get_if_exited (process))
        return -1;
    return g_subprocess_get_exit_status (process);
}

/* ----- The checks ----- */

/* The unit list of "failed units: a.service b.service" (units, user_units), else NULL */
static char *
failed_units (const char *message)
{
    const char *colon = strstr (message, "units: ");
    g_auto(GStrv) units = NULL;

    if (!colon)
        return NULL;
    units = g_strsplit (colon + strlen ("units: "), " ", -1);
    return g_strjoinv (", ", units);
}

static void
add_check_row (GtkWidget *group, const char *name, State state, const char *message)
{
    const char *title = name;
    const char *failed = NULL;
    g_autofree char *subtitle = NULL;
    GtkWidget *row;

    for (guint i = 0; i < G_N_ELEMENTS (checks); i++) {
        if (g_str_equal (checks[i].name, name)) {
            title = P_(checks[i].title);
            failed = P_(checks[i].failed);
            break;
        }
    }

    if (state == STATE_OK) {
        subtitle = g_strdup (P_("Working"));
    } else if (state == STATE_SKIP) {
        subtitle = g_strdup (P_("Not checked: nobody was logged in."));
    } else {
        g_autofree char *units = failed_units (message);

        if (units)
            /* Translators: the services that did not start, e.g. "Not started: netlogd.service" */
            subtitle = g_strdup_printf (P_("Not started: %s"), units);
        else
            /* a check of a newer plinius-health: its own words */
            subtitle = g_strdup (failed ? failed : message);
    }

    row = text_row_new (title, subtitle);
    adw_action_row_add_suffix (ADW_ACTION_ROW (row), state_icon_new (state, 16));
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), row);
}

/* Adds the rows of the result to @group; the number of failed checks */
static guint
add_check_rows (GtkWidget *group, const char *result)
{
    g_auto(GStrv) lines = g_strsplit (result, "\n", -1);
    guint failed = 0;

    for (guint i = 0; lines[i]; i++) {
        g_autofree char *name = NULL;
        g_autofree char *message = NULL;
        const char *line = lines[i], *space, *colon;
        State state;

        if (g_str_has_prefix (line, "ok "))
            state = STATE_OK;
        else if (g_str_has_prefix (line, "fail "))
            state = STATE_FAIL;
        else if (g_str_has_prefix (line, "skip "))
            state = STATE_SKIP;
        else
            continue;
        space = strchr (line, ' ');
        colon = strchr (space, ':');
        if (!colon || colon == space + 1)
            continue;
        name = g_strndup (space + 1, colon - space - 1);
        message = g_strstrip (g_strdup (colon + 1));
        add_check_row (group, name, state, message);
        if (state == STATE_FAIL)
            failed++;
    }
    return failed;
}

/* ----- Check Again ----- */

static void
check_done_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
    GSubprocess *process = G_SUBPROCESS (source);
    SelfTestPage *sp = user_data;
    g_autoptr(GError) error = NULL;
    int status;

    /* cancelled: the page is gone, and sp with it */
    if (!g_subprocess_wait_finish (process, result, &error) &&
        g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
        return;

    sp->checking = FALSE;
    status = exit_status (process, error);
    update_page (sp);
    if (status == 0 || status == HEALTH_FAILED || status == PKEXEC_DISMISSED)
        return;
    toast (sp, status == PKEXEC_NOT_AUTHORIZED ? P_("Not checked: not authorized")
                                                : P_("The self-test could not run"));
}

static void
check_activated_cb (SelfTestPage *sp)
{
    g_autoptr(GSubprocess) process = NULL;
    g_autoptr(GError) error = NULL;

    if (sp->checking)
        return;
    process = g_subprocess_new (G_SUBPROCESS_FLAGS_STDOUT_SILENCE | G_SUBPROCESS_FLAGS_STDERR_SILENCE, &error,
                                "pkexec", HEALTH_TOOL, "check", NULL);
    if (!process) {
        g_warning ("Could not run pkexec %s check: %s", HEALTH_TOOL, error->message);
        toast (sp, P_("The self-test could not run"));
        return;
    }
    sp->checking = TRUE;
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (sp->check_row), P_("Checking…"));
    gtk_widget_set_sensitive (GTK_WIDGET (sp->check_row), FALSE);
    g_subprocess_wait_async (process, sp->cancellable, check_done_cb, sp);
}

static void
copy_activated_cb (SelfTestPage *sp)
{
    gdk_clipboard_set_text (gtk_widget_get_clipboard (GTK_WIDGET (sp->prefs)), sp->result ? sp->result : "");
    toast (sp, P_("Copied to clipboard"));
}

/* ----- Take Back Update ----- */

static void
rollback_run_done_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
    GSubprocess *process = G_SUBPROCESS (source);
    SelfTestPage *sp = user_data;
    g_autoptr(GError) error = NULL;
    int status;

    if (!g_subprocess_wait_finish (process, result, &error) &&
        g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
        return;

    sp->rolling_back = FALSE;
    status = exit_status (process, error);
    g_clear_pointer (&sp->rollback_done, g_free);
    if (status == 0)
        sp->rollback_done = g_strdup (P_("The update was taken back. Restart the phone to load the earlier versions."));
    else if (status == PKEXEC_NOT_AUTHORIZED)
        sp->rollback_done = g_strdup (P_("Not taken back: not authorized."));
    else if (status != PKEXEC_DISMISSED)
        /* Translators: plinius-rollback ended with this exit status; nothing or not all changed */
        sp->rollback_done = g_strdup_printf (P_("Not taken back: the rollback stopped with status %d."), status);
    update_page (sp);
}

static void
rollback_confirmed_cb (SelfTestPage *sp)
{
    g_autoptr(GSubprocess) process = NULL;
    g_autoptr(GError) error = NULL;

    if (sp->rolling_back)
        return;
    process = g_subprocess_new (G_SUBPROCESS_FLAGS_STDOUT_SILENCE | G_SUBPROCESS_FLAGS_STDERR_SILENCE, &error,
                                "pkexec", ROLLBACK_TOOL, "run", "--noconfirm", "--unit", NULL);
    if (!process) {
        g_warning ("Could not run pkexec %s run: %s", ROLLBACK_TOOL, error->message);
        g_free (sp->rollback_done);
        sp->rollback_done = g_strdup_printf (P_("Not taken back: %s"), error->message);
        update_page (sp);
        return;
    }
    sp->rolling_back = TRUE;
    g_clear_pointer (&sp->rollback_done, g_free);
    update_page (sp);
    g_subprocess_wait_async (process, sp->cancellable, rollback_run_done_cb, sp);
}

static void
rollback_clicked_cb (SelfTestPage *sp)
{
    g_autofree char *when = format_when (sp->rollback_when);
    g_autofree char *body = NULL;
    AdwDialog *dialog;

    /* Translators: %s is the date and time of the update, the number follows it */
    body = g_strdup_printf (NP_("The update of %s goes back: one package returns to its earlier version. "
                                "Restart the phone afterwards.",
                                "The update of %s goes back: %u packages return to their earlier versions. "
                                "Restart the phone afterwards.", sp->rollback_packages),
                            when, sp->rollback_packages);
    dialog = adw_alert_dialog_new (P_("Take Back the Last Update?"), body);
    adw_alert_dialog_add_responses (ADW_ALERT_DIALOG (dialog), "cancel", _("_Cancel"),
                                    "rollback", P_("_Take Back"), NULL);
    adw_alert_dialog_set_response_appearance (ADW_ALERT_DIALOG (dialog), "rollback", ADW_RESPONSE_DESTRUCTIVE);
    adw_alert_dialog_set_default_response (ADW_ALERT_DIALOG (dialog), "cancel");
    adw_alert_dialog_set_close_response (ADW_ALERT_DIALOG (dialog), "cancel");
    g_signal_connect_swapped (dialog, "response::rollback", G_CALLBACK (rollback_confirmed_cb), sp);
    adw_dialog_present (dialog, GTK_WIDGET (sp->prefs));
}

/* "plinius-rollback summary" answered: what the group says and whether the button shows */
static void
rollback_summary_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
    GSubprocess *process = G_SUBPROCESS (source);
    SelfTestPage *sp = user_data;
    g_autoptr(GError) error = NULL;
    g_autofree char *out = NULL;
    g_autofree char *when = NULL;
    g_autofree char *text = NULL;
    g_auto(GStrv) lines = NULL;
    guint packages = 0, missing = 0;
    int status;

    if (!g_subprocess_communicate_utf8_finish (process, result, &out, NULL, &error) &&
        g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
        return;
    /* the page was shown again meanwhile: its group is a new one */
    if (!sp->rollback_group || g_object_get_data (G_OBJECT (process), "group") != sp->rollback_group)
        return;

    status = exit_status (process, error);
    lines = g_strsplit (out ? out : "", "\n", -1);
    for (guint i = 0; lines[i]; i++) {
        if (g_str_has_prefix (lines[i], "when="))
            g_set_str (&sp->rollback_when, lines[i] + strlen ("when="));
        else if (g_str_has_prefix (lines[i], "packages="))
            packages = (guint) g_ascii_strtoull (lines[i] + strlen ("packages="), NULL, 10);
        else if (g_str_has_prefix (lines[i], "missing="))
            missing = (guint) g_ascii_strtoull (lines[i] + strlen ("missing="), NULL, 10);
    }
    sp->rollback_packages = packages;
    when = sp->rollback_when ? format_when (sp->rollback_when) : NULL;

    if (status == 0 && when && packages > 0) {
        /* Translators: %s is the date and time of the update, the number follows it */
        text = g_strdup_printf (NP_("The update of %s changed one package. Its earlier version is still on the "
                                    "phone. If the problem began with this update, take it back. Asks for your PIN.",
                                    "The update of %s changed %u packages. Their earlier versions are still on the "
                                    "phone. If the problem began with this update, take it back. Asks for your PIN.",
                                    packages),
                                when, packages);
        gtk_widget_set_visible (sp->rollback_button, TRUE);
    } else if (status == ROLLBACK_MISSING && when) {
        text = g_strdup_printf (NP_("The update of %s cannot be taken back: the earlier version of one package is no "
                                    "longer on the phone.",
                                    "The update of %s cannot be taken back: the earlier versions of %u packages are "
                                    "no longer on the phone.", missing),
                                when, missing);
    } else if (status == ROLLBACK_NOTHING) {
        text = g_strdup (P_("There is no update to take back."));
    } else {
        text = g_strdup (P_("The last update could not be read."));
    }
    adw_preferences_group_set_description (sp->rollback_group, text);
}

static void
add_rollback_group (SelfTestPage *sp)
{
    g_autoptr(GSubprocess) process = NULL;
    g_autoptr(GError) error = NULL;

    sp->rollback_group = ADW_PREFERENCES_GROUP (add_group (sp, P_("Last Update"), NULL));
    sp->rollback_button = gtk_button_new_with_mnemonic (P_("_Take Back Update"));
    gtk_widget_set_halign (sp->rollback_button, GTK_ALIGN_CENTER);
    gtk_widget_add_css_class (sp->rollback_button, "pill");
    gtk_widget_add_css_class (sp->rollback_button, "destructive-action");
    gtk_widget_set_visible (sp->rollback_button, FALSE);
    g_signal_connect_swapped (sp->rollback_button, "clicked", G_CALLBACK (rollback_clicked_cb), sp);
    adw_preferences_group_add (sp->rollback_group, sp->rollback_button);

    if (sp->rolling_back) {
        adw_preferences_group_set_description (sp->rollback_group,
                                               P_("Taking back the update… Do not switch off the phone."));
        return;
    }
    if (sp->rollback_done) {
        adw_preferences_group_set_description (sp->rollback_group, sp->rollback_done);
        return;
    }

    adw_preferences_group_set_description (sp->rollback_group, P_("Looking for the last update…"));
    process = g_subprocess_new (G_SUBPROCESS_FLAGS_STDOUT_PIPE | G_SUBPROCESS_FLAGS_STDERR_SILENCE, &error,
                                ROLLBACK_TOOL, "summary", NULL);
    if (!process) {
        g_warning ("Could not run %s summary: %s", ROLLBACK_TOOL, error->message);
        adw_preferences_group_set_description (sp->rollback_group, P_("The last update could not be read."));
        return;
    }
    g_object_set_data (G_OBJECT (process), "group", sp->rollback_group);
    g_subprocess_communicate_utf8_async (process, NULL, sp->cancellable, rollback_summary_cb, sp);
}

/* ----- The page ----- */

/* Read again on every showing and after Check Again: the check runs minutes after the start */
static void
update_page (SelfTestPage *sp)
{
    g_autoptr(GError) error = NULL;
    g_autofree char *result = NULL;
    GtkWidget *status_group, *checks_group, *group, *row, *button;
    guint failed;

    for (guint i = 0; i < sp->groups->len; i++)
        adw_preferences_page_remove (sp->prefs, g_ptr_array_index (sp->groups, i));
    g_ptr_array_set_size (sp->groups, 0);
    sp->rollback_group = NULL;
    sp->rollback_button = NULL;
    g_clear_pointer (&sp->result, g_free);

    status_group = add_group (sp, NULL, NULL);
    if (g_file_test (HEALTH_RUN "/done", G_FILE_TEST_EXISTS) &&
        !g_file_get_contents (HEALTH_RUN "/result", &result, NULL, &error))
        g_warning ("Could not read %s: %s", HEALTH_RUN "/result", error->message);

    if (result) {
        checks_group = add_group (sp, P_("Checks"), NULL);
        failed = add_check_rows (checks_group, result);
        sp->result = g_steal_pointer (&result);
        if (failed) {
            g_autofree char *text = g_strdup_printf (NP_("One check failed.", "%u checks failed.", failed), failed);
            row = text_row_new (P_("Problem Found"), text);
        } else {
            row = text_row_new (P_("Everything Works"), P_("All checks after the start were fine."));
        }
        adw_action_row_add_prefix (ADW_ACTION_ROW (row), state_icon_new (failed ? STATE_FAIL : STATE_OK, 32));
    } else {
        failed = 0;
        row = text_row_new (sp->checking ? P_("Checking…") : P_("Not Checked Yet"),
                            P_("The self-test runs a few minutes after the phone starts."));
        adw_action_row_add_prefix (ADW_ACTION_ROW (row), state_icon_new (STATE_SKIP, 32));
    }
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (status_group), row);

    /* Check Again and Copy below the checks */
    group = add_group (sp, NULL, NULL);
    button = adw_button_row_new ();
    sp->check_row = ADW_BUTTON_ROW (button);
    adw_button_row_set_start_icon_name (ADW_BUTTON_ROW (button), "view-refresh-symbolic");
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (button), sp->checking ? P_("Checking…") : P_("Check _Again"));
    adw_preferences_row_set_use_underline (ADW_PREFERENCES_ROW (button), TRUE);
    gtk_widget_set_sensitive (button, !sp->checking);
    g_signal_connect_swapped (button, "activated", G_CALLBACK (check_activated_cb), sp);
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), button);
    if (sp->result) {
        button = adw_button_row_new ();
        adw_button_row_set_start_icon_name (ADW_BUTTON_ROW (button), "edit-copy-symbolic");
        adw_preferences_row_set_title (ADW_PREFERENCES_ROW (button), P_("_Copy"));
        adw_preferences_row_set_use_underline (ADW_PREFERENCES_ROW (button), TRUE);
        g_signal_connect_swapped (button, "activated", G_CALLBACK (copy_activated_cb), sp);
        adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), button);
    }

    /* Only when something broke: then an update is the likely cause. Also while a rollback runs
       or its outcome is to be read, whatever a check found meanwhile. */
    if ((failed || sp->rolling_back || sp->rollback_done) && g_file_test (ROLLBACK_TOOL, G_FILE_TEST_IS_EXECUTABLE))
        add_rollback_group (sp);
}

/**
 * cc_plinius_selftest_add_page:
 * @panel: the System panel
 *
 * Adds the page "plinius-self-test" to @panel when plinius-health is installed. Returns
 * whether it did, i.e. whether the "Self-Test" row should show.
 */
gboolean
cc_plinius_selftest_add_page (CcPanel *panel)
{
    SelfTestPage *sp;
    GtkWidget *toolbar, *page;

    if (!g_file_test (HEALTH_TOOL, G_FILE_TEST_IS_EXECUTABLE))
        return FALSE;

    sp = g_new0 (SelfTestPage, 1);
    sp->cancellable = g_cancellable_new ();
    sp->groups = g_ptr_array_new ();
    sp->prefs = ADW_PREFERENCES_PAGE (adw_preferences_page_new ());
    sp->toast_overlay = ADW_TOAST_OVERLAY (adw_toast_overlay_new ());
    adw_toast_overlay_set_child (sp->toast_overlay, GTK_WIDGET (sp->prefs));

    toolbar = adw_toolbar_view_new ();
    adw_toolbar_view_add_top_bar (ADW_TOOLBAR_VIEW (toolbar), adw_header_bar_new ());
    adw_toolbar_view_set_content (ADW_TOOLBAR_VIEW (toolbar), GTK_WIDGET (sp->toast_overlay));
    page = GTK_WIDGET (adw_navigation_page_new_with_tag (toolbar, P_("Self-Test"), PAGE_TAG));
    g_object_set_data_full (G_OBJECT (page), PAGE_TAG, sp, (GDestroyNotify) selftest_page_free);
    g_signal_connect_swapped (page, "showing", G_CALLBACK (update_page), sp);

    cc_panel_add_subpage (panel, PAGE_TAG, ADW_NAVIGATION_PAGE (page));
    return TRUE;
}

/**
 * cc_plinius_selftest_row_setup:
 * @row: the "Self-Test" row on the System page
 *
 * Gives @row our title and subtitle (our gettext domain), points it at our page and shows it.
 */
void
cc_plinius_selftest_row_setup (AdwActionRow *row)
{
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_("_Self-Test"));
    adw_action_row_set_subtitle (row, P_("Whether the phone's parts work after the start"));
    gtk_actionable_set_action_target (GTK_ACTIONABLE (row), "s", PAGE_TAG);
    gtk_widget_set_visible (GTK_WIDGET (row), TRUE);
}
