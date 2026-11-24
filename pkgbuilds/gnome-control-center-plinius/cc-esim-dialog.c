/*
 * cc-esim-dialog.c
 *
 * Plinius: eSIM management (eUICC profiles) for a modem in the Mobile Network panel.
 * The work is done by lpac (package lpac) with its "ofono" APDU backend: oFono owns the
 * modem and passes the APDUs to the eUICC (org.plinius.ofono.EuiccChannel, package
 * ofono-sailfish). Every lpac call prints one JSON result line; this dialog is only the UI.
 *
 * lpac checks the TLS certificate and the name of the SM-DP+ only with a trust store: each call
 * gets one of the system's CAs and the GSMA CI roots of the lpac package (TRUST_STORE_SOURCES,
 * written to the runtime directory). When the check fails, the dialog asks before it runs the
 * call again without it: someone on the network could otherwise read and change the exchange
 * with the server (the EID, the matching ID; the eUICC still checks the profile itself).
 * Our own strings use the gettext domain plinius-control-center (not GNOME's po/).
 *
 * SPDX-License-Identifier: MIT
 */

#undef G_LOG_DOMAIN
#define G_LOG_DOMAIN "cc-esim-dialog"

#include "config.h"

#include <adwaita.h>
#include <gio/gio.h>
#include <glib/gi18n.h>
#include <gtk/gtk.h>
#include <json-glib/json-glib.h>
#include <string.h>

#include "cc-esim-dialog.h"

#define PLINIUS_DOMAIN "plinius-control-center"
#define P_(String) g_dgettext (PLINIUS_DOMAIN, String)

#define LPAC "lpac"
#define OFONO_SERVICE "org.ofono"
#define OFONO_MANAGER_INTERFACE "org.ofono.Manager"
#define EUICC_CHANNEL_INTERFACE "org.plinius.ofono.EuiccChannel"
#define ACTIVATION_CODE_PREFIX "LPA:1$"
/* Concatenated into TRUST_STORE_NAME in the runtime directory: lpac takes one PEM file, which
 * replaces the system's trust store; SM-DP+ servers have certificates below a GSMA CI */
#define SYSTEM_CA_BUNDLE "/etc/ssl/certs/ca-certificates.crt"
#define GSMA_CI_BUNDLE "/usr/share/lpac/gsma-ci-bundle.pem"
#define TRUST_STORE_NAME "plinius-lpac-trust.pem"

typedef void (*EsimDoneFunc) (CcEsimDialog *self, JsonNode *data, const char *error);

struct _CcEsimDialog {
    AdwDialog parent_instance;

    AdwToastOverlay *toast_overlay;
    AdwNavigationView *navigation_view;
    GtkStack *main_stack;
    AdwStatusPage *error_page;
    AdwPreferencesGroup *profiles_group;
    AdwSpinner *busy_spinner;
    AdwActionRow *eid_row;
    AdwActionRow *memory_row;
    AdwActionRow *version_row;

    AdwNavigationPage *profile_page;
    AdwEntryRow *label_row;
    AdwActionRow *provider_row;
    AdwActionRow *name_row;
    AdwActionRow *iccid_row;
    AdwActionRow *class_row;
    AdwSwitchRow *active_row;
    AdwButtonRow *delete_row;

    AdwEntryRow *code_row;
    AdwEntryRow *confirmation_row;
    AdwButtonRow *download_row;
    GtkWidget *download_box;
    GtkLabel *download_label;

    char *modem;            /* oFono modem path, e.g. /ril_1 */
    GCancellable *cancellable;
    GPtrArray *profile_rows;
    JsonNode *profiles;     /* data of "lpac profile list" */
    char *selected_iccid;   /* profile shown on the profile page */
    gboolean busy;
    gboolean updating;      /* filling the profile page, not a user change */
};

G_DEFINE_TYPE (CcEsimDialog, cc_esim_dialog, ADW_TYPE_DIALOG)

/* ----- Running lpac ----- */

/* The system's CAs and the GSMA CI roots in one file, or NULL (then lpac verifies against the
 * system's store, which SM-DP+ certificates below a GSMA CI do not pass) */
static char *
trust_store (void)
{
    g_autofree char *path = g_build_filename (g_get_user_runtime_dir (), TRUST_STORE_NAME, NULL);
    g_autofree char *system = NULL;
    g_autofree char *gsma = NULL;
    g_autofree char *both = NULL;
    g_autoptr(GError) error = NULL;

    if (!g_file_get_contents (GSMA_CI_BUNDLE, &gsma, NULL, NULL))
        return NULL;
    if (!g_file_get_contents (SYSTEM_CA_BUNDLE, &system, NULL, NULL))
        system = g_strdup ("");
    both = g_strconcat (system, "\n", gsma, NULL);
    if (!g_file_set_contents_full (path, both, -1, G_FILE_SET_CONTENTS_CONSISTENT, 0644, &error)) {
        g_warning ("Could not write %s: %s", path, error->message);
        return NULL;
    }
    return g_steal_pointer (&path);
}

/* @verify: check the server's certificate and name (see above); FALSE only after the user
 * agreed to go on without */
static GSubprocess *
esim_spawn (const char  *modem,
            GPtrArray   *argv,
            gboolean     verify,
            GError     **error)
{
    g_autoptr(GSubprocessLauncher) launcher = NULL;
    g_autofree char *store = verify ? trust_store () : NULL;

    launcher = g_subprocess_launcher_new (G_SUBPROCESS_FLAGS_STDOUT_PIPE | G_SUBPROCESS_FLAGS_STDERR_PIPE);
    g_subprocess_launcher_unsetenv (launcher, "LPAC_HTTP_TRUST_STORE");
    if (store)
        g_subprocess_launcher_setenv (launcher, "LPAC_HTTP_TRUST_STORE", store, TRUE);
    g_subprocess_launcher_setenv (launcher, "LPAC_HTTP_SSL_VERIFY", verify ? "1" : "0", TRUE);
    g_subprocess_launcher_setenv (launcher, "LPAC_APDU", "ofono", TRUE);
    g_subprocess_launcher_setenv (launcher, "LPAC_APDU_OFONO_MODEM", modem, TRUE);
    /* lpac's messages are English identifiers anyway */
    g_subprocess_launcher_setenv (launcher, "LC_ALL", "C.UTF-8", TRUE);
    g_ptr_array_add (argv, NULL);
    return g_subprocess_launcher_spawnv (launcher, (const char * const *) argv->pdata, error);
}

/* @name of @object if it is an object; NULL if it is missing or something else (where
 * json_object_get_object_member () logs a critical) */
static JsonObject *
object_member (JsonObject *object,
               const char *name)
{
    JsonNode *node = json_object_get_member (object, name);

    return node != NULL && JSON_NODE_HOLDS_OBJECT (node) ? json_node_get_object (node) : NULL;
}

/*
 * lpac prints progress lines and ends with {"type":"lpa","payload":{"code":0,...,"data":...}}.
 * Returns the data of a successful result, or NULL and the failure in @error_out.
 */
static JsonNode *
esim_parse (const char  *output,
            char       **error_out)
{
    g_auto(GStrv) lines = NULL;

    if (output == NULL) {
        *error_out = g_strdup ("no output");
        return NULL;
    }

    lines = g_strsplit (output, "\n", -1);
    for (int i = g_strv_length (lines) - 1; i >= 0; i--) {
        g_autoptr(JsonParser) parser = json_parser_new ();
        JsonObject *root, *payload;
        JsonNode *reason;

        if (*g_strstrip (lines[i]) == '\0' || !json_parser_load_from_data (parser, lines[i], -1, NULL))
            continue;
        if (!JSON_NODE_HOLDS_OBJECT (json_parser_get_root (parser)))
            continue;
        root = json_node_get_object (json_parser_get_root (parser));
        if (g_strcmp0 (json_object_get_string_member_with_default (root, "type", ""), "lpa") != 0)
            continue;

        payload = object_member (root, "payload");
        if (payload == NULL)
            break;

        if (json_object_get_int_member_with_default (payload, "code", -1) == 0) {
            JsonNode *data = json_object_get_member (payload, "data");

            return data ? json_node_copy (data) : json_node_new (JSON_NODE_NULL);
        }

        /* "message" names the step that failed, "data" often says why */
        reason = json_object_get_member (payload, "data");
        if (reason != NULL && JSON_NODE_HOLDS_VALUE (reason) &&
            json_node_get_value_type (reason) == G_TYPE_STRING &&
            *json_node_get_string (reason) != '\0')
            *error_out = g_strdup_printf ("%s: %s",
                                          json_object_get_string_member_with_default (payload, "message", "?"),
                                          json_node_get_string (reason));
        else
            *error_out = g_strdup (json_object_get_string_member_with_default (payload, "message", "?"));
        return NULL;
    }

    *error_out = g_strdup ("no result");
    return NULL;
}

typedef struct {
    CcEsimDialog *self;
    EsimDoneFunc done;
    GStrv argv;
    gboolean verify;
} RunData;

static void
run_data_free (RunData *run)
{
    g_clear_object (&run->self);
    g_strfreev (run->argv);
    g_free (run);
}

G_DEFINE_AUTOPTR_CLEANUP_FUNC (RunData, run_data_free)

static void set_busy (CcEsimDialog *self, gboolean busy);
static void esim_run_argv (CcEsimDialog *self, EsimDoneFunc done, const char * const *args, gboolean verify);

/* What curl says (lpac's stderr, "curl_easy_perform() failed: <curl's message>") when the
 * certificate or the name of the server does not check out: "SSL certificate OpenSSL verify
 * result: …" (curl 8, before "SSL certificate problem: …"), "SSL: no alternative certificate
 * subject name matches target hostname …", or without a message "SSL peer certificate or SSH
 * remote key was not OK". Not a refused connection or a broken handshake. */
static gboolean
tls_not_verified (const char *errors)
{
    return errors && (strstr (errors, "SSL certificate") || strstr (errors, "SSL peer certificate") ||
                      strstr (errors, "certificate subject name"));
}

static void
unverified_response_cb (AdwAlertDialog *alert,
                        const char     *response,
                        RunData        *run)
{
    if (g_str_equal (response, "continue"))
        esim_run_argv (run->self, run->done, (const char * const *) run->argv, FALSE);
    else
        run->done (run->self, NULL, P_("The server could not be verified"));
}

static void
ask_unverified (RunData *run)
{
    AdwDialog *alert = adw_alert_dialog_new (P_("Server Not Verified"), NULL);

    adw_alert_dialog_set_body (ADW_ALERT_DIALOG (alert),
                               P_("The identity of the eSIM server could not be confirmed. Someone on this network "
                                  "could read or change what the phone exchanges with it. The eSIM still checks the "
                                  "profile itself. Continue anyway?"));
    adw_alert_dialog_add_responses (ADW_ALERT_DIALOG (alert),
                                    "cancel", _("_Cancel"),
                                    "continue", P_("Continue _Unverified"),
                                    NULL);
    adw_alert_dialog_set_response_appearance (ADW_ALERT_DIALOG (alert), "continue", ADW_RESPONSE_DESTRUCTIVE);
    adw_alert_dialog_set_default_response (ADW_ALERT_DIALOG (alert), "cancel");
    adw_alert_dialog_set_close_response (ADW_ALERT_DIALOG (alert), "cancel");
    g_signal_connect_data (alert, "response", G_CALLBACK (unverified_response_cb), run,
                           (GClosureNotify) (void (*) (void)) run_data_free, 0);
    adw_dialog_present (alert, GTK_WIDGET (run->self));
}

static void
esim_run_finished_cb (GObject      *source,
                      GAsyncResult *result,
                      gpointer      user_data)
{
    g_autoptr(RunData) run = user_data;
    g_autoptr(GError) error = NULL;
    g_autofree char *output = NULL;
    g_autofree char *errors = NULL;
    g_autofree char *failure = NULL;
    g_autoptr(JsonNode) data = NULL;

    if (!g_subprocess_communicate_utf8_finish (G_SUBPROCESS (source), result, &output, &errors, &error)) {
        if (g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
            return;
        failure = g_strdup (error->message);
    } else {
        data = esim_parse (output, &failure);
    }

    set_busy (run->self, FALSE);
    if (data == NULL && run->verify && tls_not_verified (errors)) {
        g_message ("lpac: the server's certificate did not verify: %s", errors);
        ask_unverified (g_steal_pointer (&run));
        return;
    }
    run->done (run->self, data, failure);
}

/* Runs "lpac <args…>" (NULL-terminated @args, without "lpac") for the dialog's modem */
static void
esim_run_argv (CcEsimDialog       *self,
               EsimDoneFunc        done,
               const char * const *args,
               gboolean            verify)
{
    g_autoptr(GPtrArray) argv = g_ptr_array_new ();
    g_autoptr(GSubprocess) process = NULL;
    g_autoptr(GError) error = NULL;
    RunData *run;

    g_ptr_array_add (argv, (gpointer) LPAC);
    for (guint i = 0; args[i]; i++)
        g_ptr_array_add (argv, (gpointer) args[i]);

    process = esim_spawn (self->modem, argv, verify, &error);
    if (process == NULL) {
        done (self, NULL, error->message);
        return;
    }

    set_busy (self, TRUE);
    run = g_new0 (RunData, 1);
    run->self = g_object_ref (self);
    run->done = done;
    run->argv = g_strdupv ((char **) args);
    run->verify = verify;
    g_subprocess_communicate_utf8_async (process, NULL, self->cancellable, esim_run_finished_cb, run);
}

/* Runs "lpac <args…>" (NULL-terminated) for the dialog's modem, one call at a time */
static void
esim_run (CcEsimDialog *self,
          EsimDoneFunc  done,
          ...)
{
    g_autoptr(GPtrArray) args = g_ptr_array_new ();
    const char *arg;
    va_list ap;

    va_start (ap, done);
    while ((arg = va_arg (ap, const char *)) != NULL)
        g_ptr_array_add (args, (gpointer) arg);
    va_end (ap);
    g_ptr_array_add (args, NULL);
    esim_run_argv (self, done, (const char * const *) args->pdata, TRUE);
}

/* ----- Probing a modem for an eUICC (used by the Mobile Network panel) ----- */

static void
probe_lpac_cb (GObject      *source,
               GAsyncResult *result,
               gpointer      user_data)
{
    g_autoptr(GTask) task = user_data;
    g_autoptr(GError) error = NULL;
    g_autofree char *output = NULL;
    g_autofree char *failure = NULL;
    g_autoptr(JsonNode) data = NULL;

    if (!g_subprocess_communicate_utf8_finish (G_SUBPROCESS (source), result, &output, NULL, &error)) {
        g_task_return_error (task, g_steal_pointer (&error));
        return;
    }

    data = esim_parse (output, &failure);
    if (data == NULL) {
        /* No eUICC in this slot (an empty slot or a plain SIM card) */
        g_debug ("No eUICC on %s: %s", (char *) g_task_get_task_data (task), failure);
        g_task_return_pointer (task, NULL, NULL);
        return;
    }

    g_task_return_pointer (task, g_strdup (g_task_get_task_data (task)), g_free);
}

static void
probe_modems_cb (GObject      *source,
                 GAsyncResult *result,
                 gpointer      user_data)
{
    g_autoptr(GTask) task = user_data;
    const char *imei = g_object_get_data (G_OBJECT (task), "imei");
    g_autoptr(GVariant) reply = NULL;
    g_autoptr(GVariantIter) modems = NULL;
    g_autoptr(GPtrArray) argv = NULL;
    g_autoptr(GSubprocess) process = NULL;
    g_autoptr(GError) error = NULL;
    g_autofree char *found = NULL;
    const char *path;
    GVariant *props;

    reply = g_dbus_connection_call_finish (G_DBUS_CONNECTION (source), result, &error);
    if (reply == NULL) {
        g_task_return_error (task, g_steal_pointer (&error));
        return;
    }

    /* oFono's "Serial" is the IMEI, ModemManager's equipment identifier too */
    g_variant_get (reply, "(a(oa{sv}))", &modems);
    while (found == NULL && g_variant_iter_next (modems, "(&o@a{sv})", &path, &props)) {
        g_autofree const char **interfaces = NULL;
        const char *serial = NULL;

        if (g_variant_lookup (props, "Serial", "&s", &serial) && g_strcmp0 (serial, imei) == 0 &&
            g_variant_lookup (props, "Interfaces", "^a&s", &interfaces) &&
            g_strv_contains (interfaces, EUICC_CHANNEL_INTERFACE))
            found = g_strdup (path);
        g_variant_unref (props);
    }

    if (found == NULL) {
        g_task_return_pointer (task, NULL, NULL);
        return;
    }

    argv = g_ptr_array_new ();
    g_ptr_array_add (argv, (gpointer) LPAC);
    g_ptr_array_add (argv, (gpointer) "chip");
    g_ptr_array_add (argv, (gpointer) "info");
    process = esim_spawn (found, argv, TRUE, &error);
    if (process == NULL) {
        g_task_return_error (task, g_steal_pointer (&error));
        return;
    }

    g_task_set_task_data (task, g_steal_pointer (&found), g_free);
    g_subprocess_communicate_utf8_async (process, NULL, g_task_get_cancellable (task),
                                         probe_lpac_cb, g_object_ref (task));
}

/**
 * cc_esim_probe_async:
 * @imei: the modem's equipment identifier
 *
 * Looks for the oFono modem with this IMEI and checks whether its card is an eUICC.
 * cc_esim_probe_finish() returns the oFono modem path, or %NULL without an eUICC.
 */
void
cc_esim_probe_async (const char          *imei,
                     GCancellable        *cancellable,
                     GAsyncReadyCallback  callback,
                     gpointer             user_data)
{
    g_autoptr(GTask) task = g_task_new (NULL, cancellable, callback, user_data);
    g_autoptr(GDBusConnection) bus = NULL;
    g_autoptr(GError) error = NULL;
    g_autofree char *lpac = NULL;

    lpac = g_find_program_in_path (LPAC);
    if (lpac == NULL || imei == NULL || *imei == '\0') {
        g_task_return_pointer (task, NULL, NULL);
        return;
    }

    bus = g_bus_get_sync (G_BUS_TYPE_SYSTEM, cancellable, &error);
    if (bus == NULL) {
        g_task_return_error (task, g_steal_pointer (&error));
        return;
    }

    g_object_set_data_full (G_OBJECT (task), "imei", g_strdup (imei), g_free);
    g_dbus_connection_call (bus, OFONO_SERVICE, "/", OFONO_MANAGER_INTERFACE, "GetModems", NULL,
                            G_VARIANT_TYPE ("(a(oa{sv}))"), G_DBUS_CALL_FLAGS_NONE, -1, cancellable,
                            probe_modems_cb, g_object_ref (task));
}

char *
cc_esim_probe_finish (GAsyncResult  *result,
                      GError       **error)
{
    return g_task_propagate_pointer (G_TASK (result), error);
}

/* ----- Labels of the SIM cards ----- */

/* The key labels of org.plinius.sim maps the ICCID of a card to a label of one's own, which
 * Settings shows instead of the name of the network or provider. The same for both kinds: an
 * eSIM profile keeps it also as its nickname on the eUICC, a card in a slot only here. */
#define SIM_SCHEMA "org.plinius.sim"

GSettings *
cc_sim_label_settings (void)
{
    static GSettings *settings;
    static gboolean looked_up;

    if (!looked_up) {
        GSettingsSchemaSource *source = g_settings_schema_source_get_default ();
        g_autoptr(GSettingsSchema) schema = NULL;

        looked_up = TRUE;
        if (source != NULL)
            schema = g_settings_schema_source_lookup (source, SIM_SCHEMA, TRUE);
        if (schema != NULL)
            settings = g_settings_new_full (schema, NULL, NULL);
    }
    return settings;
}

/* lpac and oFono both drop the filler F of a 19-digit ICCID; also accept it, in upper case */
static char *
sim_label_key (const char *iccid)
{
    char *key;
    gsize len;

    if (iccid == NULL)
        return NULL;
    key = g_ascii_strup (iccid, -1);
    g_strstrip (key);
    len = strlen (key);
    while (len > 0 && key[len - 1] == 'F')
        key[--len] = '\0';
    if (len == 0)
        g_clear_pointer (&key, g_free);
    return key;
}

char *
cc_sim_label_dup (const char *iccid)
{
    GSettings *settings = cc_sim_label_settings ();
    g_autofree char *key = sim_label_key (iccid);
    g_autoptr(GVariant) labels = NULL;
    const char *label;

    if (settings == NULL || key == NULL)
        return NULL;
    labels = g_settings_get_value (settings, "labels");
    if (!g_variant_lookup (labels, key, "&s", &label) || *label == '\0')
        return NULL;
    return g_strdup (label);
}

/* Whether the card has an entry; an empty one means that its label was removed on purpose */
static gboolean
sim_label_known (const char *iccid)
{
    GSettings *settings = cc_sim_label_settings ();
    g_autofree char *key = sim_label_key (iccid);
    g_autoptr(GVariant) labels = NULL;
    const char *label;

    if (settings == NULL || key == NULL)
        return FALSE;
    labels = g_settings_get_value (settings, "labels");
    return g_variant_lookup (labels, key, "&s", &label);
}

/* An empty label keeps an empty entry (no label, and no nickname of the eUICC is taken over
 * again), %NULL removes the entry (for a deleted profile) */
void
cc_sim_label_set (const char *iccid,
                  const char *label)
{
    GSettings *settings = cc_sim_label_settings ();
    g_autofree char *key = sim_label_key (iccid);
    g_autofree char *text = label ? g_strstrip (g_strdup (label)) : NULL;
    g_autoptr(GVariant) labels = NULL;
    GVariantBuilder builder;
    GVariantIter iter;
    const char *name, *value;

    if (settings == NULL || key == NULL)
        return;

    labels = g_settings_get_value (settings, "labels");
    g_variant_builder_init (&builder, G_VARIANT_TYPE ("a{ss}"));
    g_variant_iter_init (&iter, labels);
    while (g_variant_iter_next (&iter, "{&s&s}", &name, &value))
        if (g_strcmp0 (name, key) != 0)
            g_variant_builder_add (&builder, "{ss}", name, value);
    if (text != NULL)
        g_variant_builder_add (&builder, "{ss}", key, text);
    g_settings_set_value (settings, "labels", g_variant_builder_end (&builder));
}

/* ----- Dialog ----- */

static void
show_toast (CcEsimDialog *self,
            const char   *title)
{
    adw_toast_overlay_add_toast (self->toast_overlay, adw_toast_new (title));
}

static void
show_failure (CcEsimDialog *self,
              const char   *what,
              const char   *error)
{
    g_autofree char *title = g_strdup_printf ("%s (%s)", what, error);

    g_warning ("%s: %s", what, error);
    show_toast (self, title);
}

static void
set_busy (CcEsimDialog *self,
          gboolean      busy)
{
    self->busy = busy;
    gtk_widget_set_visible (GTK_WIDGET (self->busy_spinner), busy);
    gtk_widget_set_sensitive (GTK_WIDGET (self->navigation_view), !busy);
}

static const char *
profile_string (JsonObject *profile,
                const char *member)
{
    const char *value;

    if (!json_object_has_member (profile, member) || json_object_get_null_member (profile, member))
        return NULL;
    value = json_object_get_string_member (profile, member);
    return (value && *value) ? value : NULL;
}

/* The label of one's own (also none on purpose), else the nickname on the eUICC */
static char *
profile_label (JsonObject *profile)
{
    const char *iccid = profile_string (profile, "iccid");

    if (sim_label_known (iccid))
        return cc_sim_label_dup (iccid);
    return g_strdup (profile_string (profile, "profileNickname"));
}

static char *
profile_title (JsonObject *profile)
{
    char *title;

    if ((title = profile_label (profile)) != NULL)
        return title;
    if (profile_string (profile, "profileName") != NULL)
        return g_strdup (profile_string (profile, "profileName"));
    return g_strdup (profile_string (profile, "iccid"));
}

static gboolean
profile_is_enabled (JsonObject *profile)
{
    return g_strcmp0 (profile_string (profile, "profileState"), "enabled") == 0;
}

static gboolean
profile_is_test (JsonObject *profile)
{
    return g_strcmp0 (profile_string (profile, "profileClass"), "test") == 0;
}

static JsonObject *
find_profile (CcEsimDialog *self,
              const char   *iccid)
{
    JsonArray *profiles;

    if (self->profiles == NULL || !JSON_NODE_HOLDS_ARRAY (self->profiles) || iccid == NULL)
        return NULL;

    profiles = json_node_get_array (self->profiles);
    for (guint i = 0; i < json_array_get_length (profiles); i++) {
        JsonObject *profile = json_array_get_object_element (profiles, i);

        if (g_strcmp0 (profile_string (profile, "iccid"), iccid) == 0)
            return profile;
    }
    return NULL;
}

/* Operator icon (base64 PNG or JPEG), or a SIM symbol */
static GtkWidget *
profile_icon (JsonObject *profile)
{
    const char *icon = profile_string (profile, "icon");
    GtkWidget *image;

    if (icon != NULL) {
        g_autoptr(GBytes) bytes = NULL;
        g_autoptr(GdkTexture) texture = NULL;
        guchar *decoded;
        gsize len;

        decoded = g_base64_decode (icon, &len);
        bytes = g_bytes_new_take (decoded, len);
        texture = gdk_texture_new_from_bytes (bytes, NULL);
        if (texture != NULL) {
            image = gtk_image_new_from_paintable (GDK_PAINTABLE (texture));
            gtk_image_set_pixel_size (GTK_IMAGE (image), 32);
            return image;
        }
    }

    image = gtk_image_new_from_icon_name ("auth-sim-symbolic");
    gtk_image_set_pixel_size (GTK_IMAGE (image), 32);
    return image;
}

static char *
profile_subtitle (JsonObject *profile)
{
    GString *subtitle = g_string_new (NULL);
    const char *provider = profile_string (profile, "serviceProviderName");

    if (profile_is_enabled (profile))
        g_string_append (subtitle, P_("Active"));
    if (provider != NULL)
        g_string_append_printf (subtitle, "%s%s", subtitle->len ? " · " : "", provider);
    if (profile_is_test (profile))
        g_string_append_printf (subtitle, "%s%s", subtitle->len ? " · " : "", P_("Test profile"));
    return g_string_free (subtitle, FALSE);
}

static void fill_profile_page (CcEsimDialog *self, JsonObject *profile);

static void
profile_row_activated_cb (CcEsimDialog *self,
                          AdwActionRow *row)
{
    const char *iccid = g_object_get_data (G_OBJECT (row), "iccid");
    JsonObject *profile = find_profile (self, iccid);

    if (profile == NULL)
        return;

    g_free (self->selected_iccid);
    self->selected_iccid = g_strdup (iccid);
    fill_profile_page (self, profile);
    adw_navigation_view_push_by_tag (self->navigation_view, "profile");
}

static void
rebuild_profile_rows (CcEsimDialog *self)
{
    JsonArray *profiles;

    for (guint i = 0; i < self->profile_rows->len; i++)
        adw_preferences_group_remove (self->profiles_group, g_ptr_array_index (self->profile_rows, i));
    g_ptr_array_set_size (self->profile_rows, 0);

    if (self->profiles == NULL || !JSON_NODE_HOLDS_ARRAY (self->profiles))
        return;

    profiles = json_node_get_array (self->profiles);
    for (guint i = 0; i < json_array_get_length (profiles); i++) {
        JsonObject *profile = json_array_get_object_element (profiles, i);
        g_autofree char *subtitle = profile_subtitle (profile);
        g_autofree char *title = profile_title (profile);
        GtkWidget *row = adw_action_row_new ();

        adw_preferences_row_set_use_markup (ADW_PREFERENCES_ROW (row), FALSE);
        adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), title);
        adw_action_row_set_subtitle (ADW_ACTION_ROW (row), subtitle);
        adw_action_row_add_prefix (ADW_ACTION_ROW (row), profile_icon (profile));
        if (profile_is_enabled (profile))
            adw_action_row_add_suffix (ADW_ACTION_ROW (row), gtk_image_new_from_icon_name ("object-select-symbolic"));
        adw_action_row_add_suffix (ADW_ACTION_ROW (row), gtk_image_new_from_icon_name ("go-next-symbolic"));
        gtk_list_box_row_set_activatable (GTK_LIST_BOX_ROW (row), TRUE);
        g_object_set_data_full (G_OBJECT (row), "iccid", g_strdup (profile_string (profile, "iccid")), g_free);
        g_signal_connect_object (row, "activated", G_CALLBACK (profile_row_activated_cb), self, G_CONNECT_SWAPPED);

        adw_preferences_group_add (self->profiles_group, row);
        g_ptr_array_add (self->profile_rows, row);
    }

    if (json_array_get_length (profiles) == 0)
        adw_preferences_group_set_description (self->profiles_group, P_("There are no profiles on this eSIM yet."));
    else
        adw_preferences_group_set_description (self->profiles_group, P_("Only one profile can be active at a time."));
}

static void
fill_profile_page (CcEsimDialog *self,
                   JsonObject   *profile)
{
    const char *klass = profile_string (profile, "profileClass");
    g_autofree char *label = profile_label (profile);
    g_autofree char *title = profile_title (profile);
    const char *type;

    if (g_strcmp0 (klass, "test") == 0)
        type = P_("Test profile");
    else if (g_strcmp0 (klass, "provisioning") == 0)
        type = P_("Provisioning profile");
    else
        type = P_("Operator profile");

    self->updating = TRUE;
    adw_navigation_page_set_title (self->profile_page, title);
    gtk_editable_set_text (GTK_EDITABLE (self->label_row), label ? label : "");
    adw_action_row_set_subtitle (self->provider_row, profile_string (profile, "serviceProviderName"));
    adw_action_row_set_subtitle (self->name_row, profile_string (profile, "profileName"));
    adw_action_row_set_subtitle (self->iccid_row, profile_string (profile, "iccid"));
    adw_action_row_set_subtitle (self->class_row, type);
    adw_switch_row_set_active (self->active_row, profile_is_enabled (profile));
    /* SGP.22: an enabled profile cannot be deleted */
    gtk_widget_set_sensitive (GTK_WIDGET (self->delete_row), !profile_is_enabled (profile));
    self->updating = FALSE;
}

static void
notifications_done_cb (CcEsimDialog *self,
                       JsonNode     *data,
                       const char   *error)
{
    /* Without internet the notifications stay on the eUICC and go out next time */
    if (error != NULL)
        g_debug ("Sending the eSIM notifications failed: %s", error);
}

/* A nickname set elsewhere (another phone, an older Settings) becomes the card's label, so that
 * the SIM page shows it too; a label of one's own stays, also a removed one */
static void
adopt_nicknames (CcEsimDialog *self)
{
    JsonArray *profiles;

    if (!JSON_NODE_HOLDS_ARRAY (self->profiles))
        return;

    profiles = json_node_get_array (self->profiles);
    for (guint i = 0; i < json_array_get_length (profiles); i++) {
        JsonObject *profile = json_array_get_object_element (profiles, i);
        const char *iccid = profile_string (profile, "iccid");
        const char *nickname = profile_string (profile, "profileNickname");

        if (nickname != NULL && !sim_label_known (iccid))
            cc_sim_label_set (iccid, nickname);
    }
}

static void
profiles_loaded_cb (CcEsimDialog *self,
                    JsonNode     *data,
                    const char   *error)
{
    JsonObject *profile;

    if (data == NULL) {
        show_failure (self, P_("Reading the profiles failed"), error);
        /* Not even read once: the spinner would stay for good, the error page can try again */
        if (g_strcmp0 (gtk_stack_get_visible_child_name (self->main_stack), "loading") == 0) {
            adw_status_page_set_description (self->error_page, error);
            gtk_stack_set_visible_child_name (self->main_stack, "error");
        }
        return;
    }

    g_clear_pointer (&self->profiles, json_node_unref);
    self->profiles = json_node_ref (data);
    adopt_nicknames (self);
    rebuild_profile_rows (self);
    gtk_stack_set_visible_child_name (self->main_stack, "content");

    profile = find_profile (self, self->selected_iccid);
    if (profile != NULL)
        fill_profile_page (self, profile);
}

/* After a change on the eUICC: show the new state, then report it to the operators */
static void
changed_profiles_loaded_cb (CcEsimDialog *self,
                            JsonNode     *data,
                            const char   *error)
{
    profiles_loaded_cb (self, data, error);
    esim_run (self, notifications_done_cb, "notification", "process", "-a", "-r", NULL);
}

static void
reload_after_change (CcEsimDialog *self)
{
    esim_run (self, changed_profiles_loaded_cb, "profile", "list", NULL);
}

static void
chip_info_cb (CcEsimDialog *self,
              JsonNode     *data,
              const char   *error)
{
    JsonObject *info, *info2, *resource;
    g_autofree char *memory = NULL;

    if (data == NULL || !JSON_NODE_HOLDS_OBJECT (data)) {
        g_warning ("Reading the eSIM failed: %s", error);
        adw_status_page_set_description (self->error_page, error);
        gtk_stack_set_visible_child_name (self->main_stack, "error");
        return;
    }

    info = json_node_get_object (data);
    adw_action_row_set_subtitle (self->eid_row, json_object_get_string_member_with_default (info, "eidValue", ""));

    info2 = object_member (info, "EUICCInfo2");
    if (info2 != NULL) {
        adw_action_row_set_subtitle (self->version_row,
                                     json_object_get_string_member_with_default (info2, "profileVersion", ""));
        resource = object_member (info2, "extCardResource");
        if (resource != NULL) {
            memory = g_format_size (json_object_get_int_member_with_default (resource, "freeNonVolatileMemory", 0));
            adw_action_row_set_subtitle (self->memory_row, memory);
        }
    }

    esim_run (self, profiles_loaded_cb, "profile", "list", NULL);
}

static void
load (CcEsimDialog *self)
{
    gtk_stack_set_visible_child_name (self->main_stack, "loading");
    esim_run (self, chip_info_cb, "chip", "info", NULL);
}

static void
retry_clicked_cb (CcEsimDialog *self)
{
    load (self);
}

static void
copy_eid_clicked_cb (CcEsimDialog *self)
{
    GdkClipboard *clipboard = gtk_widget_get_clipboard (GTK_WIDGET (self));

    gdk_clipboard_set_text (clipboard, adw_action_row_get_subtitle (self->eid_row));
    show_toast (self, P_("EID copied"));
}

static void
switch_done_cb (CcEsimDialog *self,
                JsonNode     *data,
                const char   *error)
{
    if (data == NULL)
        show_failure (self, P_("Switching the profile failed"), error);
    else
        show_toast (self, P_("Done. The network may take a moment."));

    /* Also after a failure: the switch shows the real state again */
    reload_after_change (self);
}

static void
active_row_changed_cb (CcEsimDialog *self)
{
    if (self->updating || self->busy || self->selected_iccid == NULL)
        return;

    /* Refresh flag 1: the eUICC restarts, so the modem sees the new profile at once */
    if (adw_switch_row_get_active (self->active_row))
        esim_run (self, switch_done_cb, "profile", "enable", self->selected_iccid, "1", NULL);
    else
        esim_run (self, switch_done_cb, "profile", "disable", self->selected_iccid, "1", NULL);
}

static void
label_done_cb (CcEsimDialog *self,
               JsonNode     *data,
               const char   *error)
{
    if (data == NULL)
        show_failure (self, P_("Renaming the profile failed"), error);
    esim_run (self, profiles_loaded_cb, "profile", "list", NULL);
}

/* The label goes to the settings first (the SIM page shows it at once, also when lpac fails),
 * then to the eUICC as the profile's nickname */
static void
label_apply_cb (CcEsimDialog *self)
{
    g_autofree char *label = g_strstrip (g_strdup (gtk_editable_get_text (GTK_EDITABLE (self->label_row))));

    if (self->selected_iccid == NULL)
        return;

    cc_sim_label_set (self->selected_iccid, label);
    /* Without a name lpac removes the nickname */
    if (*label == '\0')
        esim_run (self, label_done_cb, "profile", "nickname", self->selected_iccid, NULL);
    else
        esim_run (self, label_done_cb, "profile", "nickname", self->selected_iccid, label, NULL);
}

static void
delete_done_cb (CcEsimDialog *self,
                JsonNode     *data,
                const char   *error)
{
    if (data == NULL) {
        show_failure (self, P_("Deleting the profile failed"), error);
    } else {
        cc_sim_label_set (self->selected_iccid, NULL);
        g_clear_pointer (&self->selected_iccid, g_free);
        adw_navigation_view_pop_to_tag (self->navigation_view, "main");
        show_toast (self, P_("Profile deleted"));
    }
    reload_after_change (self);
}

static void
delete_response_cb (CcEsimDialog *self,
                    const char   *response)
{
    if (g_strcmp0 (response, "delete") == 0 && self->selected_iccid != NULL)
        esim_run (self, delete_done_cb, "profile", "delete", self->selected_iccid, NULL);
}

static void
delete_row_activated_cb (CcEsimDialog *self)
{
    JsonObject *profile = find_profile (self, self->selected_iccid);
    g_autofree char *title = NULL;
    AdwDialog *alert;

    if (profile == NULL)
        return;
    title = profile_title (profile);

    alert = adw_alert_dialog_new (P_("Delete Profile?"), NULL);
    adw_alert_dialog_format_body (ADW_ALERT_DIALOG (alert),
                                  P_("“%s” will be removed from the eSIM for good. To use it again, you need a new activation code from the provider."),
                                  title);
    adw_alert_dialog_add_responses (ADW_ALERT_DIALOG (alert),
                                    "cancel", _("_Cancel"),
                                    "delete", _("_Delete"),
                                    NULL);
    adw_alert_dialog_set_response_appearance (ADW_ALERT_DIALOG (alert), "delete", ADW_RESPONSE_DESTRUCTIVE);
    adw_alert_dialog_set_default_response (ADW_ALERT_DIALOG (alert), "cancel");
    g_signal_connect_object (alert, "response", G_CALLBACK (delete_response_cb), self, G_CONNECT_SWAPPED);
    adw_dialog_present (alert, GTK_WIDGET (self));
}

static void
add_row_activated_cb (CcEsimDialog *self)
{
    gtk_editable_set_text (GTK_EDITABLE (self->code_row), "");
    gtk_editable_set_text (GTK_EDITABLE (self->confirmation_row), "");
    gtk_widget_set_visible (self->download_box, FALSE);
    adw_navigation_view_push_by_tag (self->navigation_view, "add");
}

static const char *
activation_code (CcEsimDialog *self)
{
    const char *code = gtk_editable_get_text (GTK_EDITABLE (self->code_row));

    while (g_ascii_isspace (*code))
        code++;
    return code;
}

static void
add_input_changed_cb (CcEsimDialog *self)
{
    const char *code = activation_code (self);

    gtk_widget_set_sensitive (GTK_WIDGET (self->download_row),
                              g_ascii_strncasecmp (code, ACTIVATION_CODE_PREFIX, strlen (ACTIVATION_CODE_PREFIX)) == 0 &&
                              strlen (code) > strlen (ACTIVATION_CODE_PREFIX));
}

static void
download_done_cb (CcEsimDialog *self,
                  JsonNode     *data,
                  const char   *error)
{
    gtk_widget_set_visible (self->download_box, FALSE);

    if (data == NULL) {
        show_failure (self, P_("Downloading the profile failed"), error);
        return;
    }

    adw_navigation_view_pop_to_tag (self->navigation_view, "main");
    show_toast (self, P_("Profile added. Switch it on to use it."));
    reload_after_change (self);
}

static void
download_row_activated_cb (CcEsimDialog *self)
{
    g_autofree char *code = g_strstrip (g_strdup (activation_code (self)));
    const char *confirmation = gtk_editable_get_text (GTK_EDITABLE (self->confirmation_row));

    gtk_label_set_text (self->download_label, P_("Downloading the profile. This can take a minute."));
    gtk_widget_set_visible (self->download_box, TRUE);

    if (*confirmation != '\0')
        esim_run (self, download_done_cb, "profile", "download", "-a", code, "-c", confirmation, NULL);
    else
        esim_run (self, download_done_cb, "profile", "download", "-a", code, NULL);
}

/* The dialog's lpac calls end with it: each one holds the dialog until lpac is done, so
 * dispose would come only after them */
static void
cc_esim_dialog_closed (AdwDialog *dialog)
{
    CcEsimDialog *self = CC_ESIM_DIALOG (dialog);

    g_cancellable_cancel (self->cancellable);
    g_clear_object (&self->cancellable);
    self->cancellable = g_cancellable_new ();

    if (ADW_DIALOG_CLASS (cc_esim_dialog_parent_class)->closed)
        ADW_DIALOG_CLASS (cc_esim_dialog_parent_class)->closed (dialog);
}

static void
cc_esim_dialog_dispose (GObject *object)
{
    CcEsimDialog *self = CC_ESIM_DIALOG (object);

    g_cancellable_cancel (self->cancellable);
    g_clear_object (&self->cancellable);

    G_OBJECT_CLASS (cc_esim_dialog_parent_class)->dispose (object);
}

static void
cc_esim_dialog_finalize (GObject *object)
{
    CcEsimDialog *self = CC_ESIM_DIALOG (object);

    g_clear_pointer (&self->profile_rows, g_ptr_array_unref);
    g_clear_pointer (&self->profiles, json_node_unref);
    g_free (self->selected_iccid);
    g_free (self->modem);

    G_OBJECT_CLASS (cc_esim_dialog_parent_class)->finalize (object);
}

static void
cc_esim_dialog_class_init (CcEsimDialogClass *klass)
{
    GObjectClass *object_class = G_OBJECT_CLASS (klass);
    GtkWidgetClass *widget_class = GTK_WIDGET_CLASS (klass);

    object_class->dispose = cc_esim_dialog_dispose;
    object_class->finalize = cc_esim_dialog_finalize;
    ADW_DIALOG_CLASS (klass)->closed = cc_esim_dialog_closed;

    gtk_widget_class_set_template_from_resource (widget_class, "/org/gnome/control-center/wwan/cc-esim-dialog.ui");

    gtk_widget_class_bind_template_child (widget_class, CcEsimDialog, toast_overlay);
    gtk_widget_class_bind_template_child (widget_class, CcEsimDialog, navigation_view);
    gtk_widget_class_bind_template_child (widget_class, CcEsimDialog, main_stack);
    gtk_widget_class_bind_template_child (widget_class, CcEsimDialog, error_page);
    gtk_widget_class_bind_template_child (widget_class, CcEsimDialog, profiles_group);
    gtk_widget_class_bind_template_child (widget_class, CcEsimDialog, busy_spinner);
    gtk_widget_class_bind_template_child (widget_class, CcEsimDialog, eid_row);
    gtk_widget_class_bind_template_child (widget_class, CcEsimDialog, memory_row);
    gtk_widget_class_bind_template_child (widget_class, CcEsimDialog, version_row);
    gtk_widget_class_bind_template_child (widget_class, CcEsimDialog, profile_page);
    gtk_widget_class_bind_template_child (widget_class, CcEsimDialog, label_row);
    gtk_widget_class_bind_template_child (widget_class, CcEsimDialog, provider_row);
    gtk_widget_class_bind_template_child (widget_class, CcEsimDialog, name_row);
    gtk_widget_class_bind_template_child (widget_class, CcEsimDialog, iccid_row);
    gtk_widget_class_bind_template_child (widget_class, CcEsimDialog, class_row);
    gtk_widget_class_bind_template_child (widget_class, CcEsimDialog, active_row);
    gtk_widget_class_bind_template_child (widget_class, CcEsimDialog, delete_row);
    gtk_widget_class_bind_template_child (widget_class, CcEsimDialog, code_row);
    gtk_widget_class_bind_template_child (widget_class, CcEsimDialog, confirmation_row);
    gtk_widget_class_bind_template_child (widget_class, CcEsimDialog, download_row);
    gtk_widget_class_bind_template_child (widget_class, CcEsimDialog, download_box);
    gtk_widget_class_bind_template_child (widget_class, CcEsimDialog, download_label);

    gtk_widget_class_bind_template_callback (widget_class, retry_clicked_cb);
    gtk_widget_class_bind_template_callback (widget_class, add_row_activated_cb);
    gtk_widget_class_bind_template_callback (widget_class, copy_eid_clicked_cb);
    gtk_widget_class_bind_template_callback (widget_class, label_apply_cb);
    gtk_widget_class_bind_template_callback (widget_class, active_row_changed_cb);
    gtk_widget_class_bind_template_callback (widget_class, delete_row_activated_cb);
    gtk_widget_class_bind_template_callback (widget_class, add_input_changed_cb);
    gtk_widget_class_bind_template_callback (widget_class, download_row_activated_cb);
}

static void
cc_esim_dialog_init (CcEsimDialog *self)
{
    gtk_widget_init_template (GTK_WIDGET (self));

    self->cancellable = g_cancellable_new ();
    self->profile_rows = g_ptr_array_new ();
}

AdwDialog *
cc_esim_dialog_new (const char *ofono_modem)
{
    CcEsimDialog *self = g_object_new (CC_TYPE_ESIM_DIALOG, NULL);

    self->modem = g_strdup (ofono_modem);
    load (self);
    return ADW_DIALOG (self);
}
