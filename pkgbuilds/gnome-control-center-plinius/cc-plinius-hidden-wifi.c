/*
 * cc-plinius-hidden-wifi.c
 *
 * Plinius: "Connect to Hidden Networks…" as a form page in the list style of the other pages,
 * instead of libnma's NMAWifiDialog with its labels left of the fields. The page asks for the
 * network name, the security (none, WPA & WPA2 Personal, WPA3 Personal) and the password;
 * "Connect" is a pill below the form, back (the arrow and the back swipe) cancels.
 *
 * The other kinds of security are a second page of the same style ("Other Security"), pushed on
 * top of the first one, so that back leads to the first page again: WEP with a key or a
 * passphrase, LEAP, Dynamic WEP and WPA & WPA2 / WPA3 Enterprise with PEAP, TTLS, TLS, PWD or
 * LEAP. libnma's dialog (which offered the same) was opened in place of the first page instead,
 * so back from it led past the first page to Wi-Fi. Not offered: EAP-FAST (PAC file and
 * provisioning), WPA3 Enterprise 192-bit (Suite B), certificates from a smart card (PKCS#11 URIs)
 * and asking for the password every time; the connection editor still has the first ones for a
 * connection once it exists.
 *
 * The connections are made as libnma's dialog makes them: a connection that matches an existing
 * one activates that one, otherwise NetworkManager adds it and fills in the rest. Interfaces that
 * can only run an access point (ap0 of the MediaTek driver, panel_wifi_device_is_ap_only()) are
 * no choice; with more than one Wi-Fi adapter left the first page asks which.
 *
 * The labels are libnma's strings, so its translations apply; our own few use the gettext
 * domain plinius-control-center (not GNOME's po/).
 *
 * SPDX-License-Identifier: MIT
 */

#undef G_LOG_DOMAIN
#define G_LOG_DOMAIN "cc-plinius-hidden-wifi"

#include <config.h>
#include <glib/gi18n.h>
#include <string.h>

#include "cc-plinius-hidden-wifi.h"
#include "panel-common.h"
#include "ui-helpers.h"

#define PLINIUS_DOMAIN "plinius-control-center"
#define P_(String) g_dgettext (PLINIUS_DOMAIN, String)
/* libnma's own strings and translations: the page says what its dialog said */
#define NMA_DOMAIN "libnma"
#define NMA_(String) g_dgettext (NMA_DOMAIN, String)

typedef enum {
    SECURITY_NONE,
    SECURITY_WPA_PSK,
    SECURITY_SAE,
} Security;

struct _CcPliniusHiddenWifi {
    AdwDialog parent_instance;

    NMClient *client;
    GPtrArray *devices; /* NMDeviceWifi, in the order of device_row */

    AdwEntryRow *name_row;
    AdwComboRow *security_row;
    AdwPasswordEntryRow *password_row;
    AdwComboRow *device_row;
    GtkLabel *name_error_label;
    GtkButton *connect_button;
};

G_DEFINE_FINAL_TYPE (CcPliniusHiddenWifi, cc_plinius_hidden_wifi, ADW_TYPE_DIALOG)

/* ---- shared by both pages ---- */

static const char *
row_text (gpointer row)
{
    return gtk_editable_get_text (GTK_EDITABLE (row));
}

static gboolean
row_filled (gpointer row)
{
    const char *text = row_text (row);

    return text && *text;
}

/* NULL for an empty row: optional settings stay unset */
static const char *
row_text_or_null (gpointer row)
{
    return row_filled (row) ? row_text (row) : NULL;
}

/* An SSID has 1 to 32 bytes; too long says so below the form */
static gboolean
check_ssid (AdwEntryRow *row, GtkLabel *error_label)
{
    const char *ssid = row_text (row);
    gboolean too_long = ssid && strlen (ssid) > 32;

    if (too_long) {
        gtk_label_set_label (error_label, _("Network name is too long"));
        widget_set_error (GTK_WIDGET (row));
    } else {
        widget_unset_error (GTK_WIDGET (row));
    }
    gtk_widget_set_visible (GTK_WIDGET (error_label), too_long);

    return ssid && *ssid && !too_long;
}

/* Red only once something was typed: an empty field is not yet wrong */
static void
mark_row (gpointer row, gboolean valid)
{
    if (row_filled (row) && !valid)
        widget_set_error (GTK_WIDGET (row));
    else
        widget_unset_error (GTK_WIDGET (row));
}

static AdwEntryRow *
add_entry_row (AdwPreferencesGroup *group, const char *title)
{
    GtkWidget *row = adw_entry_row_new ();

    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), title);
    adw_preferences_row_set_use_underline (ADW_PREFERENCES_ROW (row), TRUE);
    adw_preferences_group_add (group, row);

    return ADW_ENTRY_ROW (row);
}

static AdwPasswordEntryRow *
add_password_row (AdwPreferencesGroup *group, const char *title)
{
    GtkWidget *row = adw_password_entry_row_new ();

    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), title);
    adw_preferences_row_set_use_underline (ADW_PREFERENCES_ROW (row), TRUE);
    adw_preferences_group_add (group, row);

    return ADW_PASSWORD_ENTRY_ROW (row);
}

/* @names: NULL-terminated, already translated */
static AdwComboRow *
add_combo_row (AdwPreferencesGroup *group, const char *title, const char *const *names)
{
    GtkWidget *row = adw_combo_row_new ();

    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), title);
    adw_preferences_row_set_use_underline (ADW_PREFERENCES_ROW (row), TRUE);
    if (names) {
        g_autoptr(GtkStringList) model = gtk_string_list_new (names);

        adw_combo_row_set_model (ADW_COMBO_ROW (row), G_LIST_MODEL (model));
    }
    adw_preferences_group_add (group, row);

    return ADW_COMBO_ROW (row);
}

static AdwPreferencesGroup *
add_group (AdwPreferencesPage *page)
{
    GtkWidget *group = adw_preferences_group_new ();

    adw_preferences_page_add (page, ADW_PREFERENCES_GROUP (group));

    return ADW_PREFERENCES_GROUP (group);
}

/* Dim small text below a group's rows, as the hotspot's hints */
static GtkLabel *
add_hint_label (AdwPreferencesGroup *group)
{
    GtkLabel *label = GTK_LABEL (gtk_label_new (NULL));

    gtk_label_set_wrap (label, TRUE);
    gtk_label_set_xalign (label, 0.0);
    gtk_widget_set_margin_top (GTK_WIDGET (label), 6);
    gtk_widget_add_css_class (GTK_WIDGET (label), "dim-label");
    gtk_widget_add_css_class (GTK_WIDGET (label), "caption");
    gtk_widget_set_visible (GTK_WIDGET (label), FALSE);
    adw_preferences_group_add (group, GTK_WIDGET (label));

    return label;
}

/* "Connect" below the form, where the thumb is */
static GtkButton *
add_connect_button (AdwPreferencesPage *page)
{
    AdwPreferencesGroup *group = add_group (page);
    GtkWidget *box = gtk_box_new (GTK_ORIENTATION_VERTICAL, 0);
    GtkButton *button = GTK_BUTTON (gtk_button_new_with_mnemonic (NMA_("C_onnect")));

    gtk_widget_set_halign (GTK_WIDGET (button), GTK_ALIGN_CENTER);
    gtk_widget_add_css_class (GTK_WIDGET (button), "pill");
    gtk_widget_add_css_class (GTK_WIDGET (button), "suggested-action");
    gtk_box_append (GTK_BOX (box), GTK_WIDGET (button));
    adw_preferences_group_add (group, box);

    return button;
}

/* A form page: header bar with back, the rows in a preferences page */
static AdwPreferencesPage *
setup_form (AdwDialog *dialog, const char *title)
{
    GtkWidget *toolbar = adw_toolbar_view_new ();
    GtkWidget *page = adw_preferences_page_new ();

    adw_dialog_set_title (dialog, title);
    adw_dialog_set_content_width (dialog, 360);
    adw_dialog_set_content_height (dialog, 480);

    adw_toolbar_view_add_top_bar (ADW_TOOLBAR_VIEW (toolbar), adw_header_bar_new ());
    adw_toolbar_view_set_content (ADW_TOOLBAR_VIEW (toolbar), page);
    adw_dialog_set_child (dialog, toolbar);

    return ADW_PREFERENCES_PAGE (page);
}

/* The connection setting and the wireless setting of a hidden network called @ssid_text */
static NMConnection *
new_hidden_connection (const char *ssid_text)
{
    g_autoptr(GBytes) ssid = g_bytes_new (ssid_text, strlen (ssid_text));
    NMConnection *connection = nm_simple_connection_new ();
    NMSetting *setting;

    setting = nm_setting_connection_new ();
    g_object_set (setting,
                  NM_SETTING_CONNECTION_ID, ssid_text,
                  NM_SETTING_CONNECTION_TYPE, NM_SETTING_WIRELESS_SETTING_NAME,
                  NULL);
    nm_connection_add_setting (connection, setting);

    setting = nm_setting_wireless_new ();
    g_object_set (setting,
                  NM_SETTING_WIRELESS_SSID, ssid,
                  NM_SETTING_WIRELESS_MODE, NM_SETTING_WIRELESS_MODE_INFRA,
                  /* not in the scan list: probe for it by name */
                  NM_SETTING_WIRELESS_HIDDEN, TRUE,
                  NULL);
    nm_connection_add_setting (connection, setting);

    return connection;
}

static void
activate_existing_cb (GObject *source_object, GAsyncResult *res, gpointer user_data)
{
    g_autoptr(GError) error = NULL;

    if (!nm_client_activate_connection_finish (NM_CLIENT (source_object), res, &error))
        g_warning ("Failed to activate connection: (%d) %s", error->code, error->message);
}

static void
activate_new_cb (GObject *source_object, GAsyncResult *res, gpointer user_data)
{
    g_autoptr(GError) error = NULL;

    if (!nm_client_add_and_activate_connection_finish (NM_CLIENT (source_object), res, &error))
        g_warning ("Failed to add new connection: (%d) %s", error->code, error->message);
}

/* As libnma's dialog does (network-dialogs.c): a matching connection is used instead */
static void
activate_connection (NMClient *client, NMConnection *connection, NMDevice *device)
{
    const GPtrArray *all = nm_client_get_connections (client);

    for (guint i = 0; i < all->len; i++) {
        NMConnection *candidate = NM_CONNECTION (g_ptr_array_index (all, i));

        if (nm_connection_compare (connection, candidate,
                                   NM_SETTING_COMPARE_FLAG_FUZZY | NM_SETTING_COMPARE_FLAG_IGNORE_ID)) {
            nm_client_activate_connection_async (client, candidate, device, NULL, NULL, activate_existing_cb, NULL);
            return;
        }
    }

    nm_client_add_and_activate_connection_async (client, connection, device, NULL, NULL, activate_new_cb, NULL);
}

/* ---- the first page ---- */

static Security
selected_security (CcPliniusHiddenWifi *self)
{
    return (Security) adw_combo_row_get_selected (self->security_row);
}

static NMDevice *
selected_device (CcPliniusHiddenWifi *self)
{
    guint i = adw_combo_row_get_selected (self->device_row);

    if (i == GTK_INVALID_LIST_POSITION || i >= self->devices->len)
        return NULL;

    return g_ptr_array_index (self->devices, i);
}

static gboolean
password_is_valid (CcPliniusHiddenWifi *self, const char *password)
{
    switch (selected_security (self)) {
    case SECURITY_NONE:
        return TRUE;
    case SECURITY_WPA_PSK:
        /* 8 to 63 characters or 64 hex digits */
        return nm_utils_wpa_psk_valid (password);
    case SECURITY_SAE:
        /* SAE takes passwords of any length */
        return password && *password;
    default:
        return FALSE;
    }
}

static void
update (CcPliniusHiddenWifi *self)
{
    gboolean with_password = selected_security (self) != SECURITY_NONE;
    gboolean valid_ssid, valid_password;

    valid_ssid = check_ssid (self->name_row, self->name_error_label);

    gtk_widget_set_visible (GTK_WIDGET (self->password_row), with_password);
    valid_password = password_is_valid (self, row_text (self->password_row));
    mark_row (self->password_row, !with_password || valid_password);

    gtk_widget_set_sensitive (GTK_WIDGET (self->connect_button),
                              valid_ssid && valid_password && selected_device (self) != NULL);
}

static NMConnection *
build_connection (CcPliniusHiddenWifi *self)
{
    NMConnection *connection = new_hidden_connection (row_text (self->name_row));

    if (selected_security (self) != SECURITY_NONE) {
        NMSetting *setting = nm_setting_wireless_security_new ();

        g_object_set (setting,
                      NM_SETTING_WIRELESS_SECURITY_KEY_MGMT,
                      selected_security (self) == SECURITY_SAE ? "sae" : "wpa-psk",
                      NM_SETTING_WIRELESS_SECURITY_PSK, row_text (self->password_row),
                      NULL);
        nm_connection_add_setting (connection, setting);
    }

    return connection;
}

static void
connect_clicked_cb (CcPliniusHiddenWifi *self)
{
    g_autoptr(NMConnection) connection = NULL;
    NMDevice *device;

    if (!gtk_widget_get_sensitive (GTK_WIDGET (self->connect_button)))
        return;

    device = selected_device (self);
    g_return_if_fail (device != NULL);

    connection = build_connection (self);
    activate_connection (self->client, connection, device);

    adw_dialog_close (ADW_DIALOG (self));
}

/* ---- the second page: WEP, LEAP, 802.1X ---- */

typedef enum {
    OTHER_WEP_KEY,
    OTHER_WEP_PASSPHRASE,
    OTHER_LEAP,
    OTHER_DYNAMIC_WEP,
    OTHER_WPA_EAP,
    OTHER_WPA3_EAP,
} OtherSecurity;

/* in the order of eap_row */
typedef enum {
    EAP_PEAP,
    EAP_TTLS,
    EAP_TLS,
    EAP_PWD,
    EAP_LEAP,
} EapMethod;

static const char *const eap_names[] = { "peap", "ttls", "tls", "pwd", "leap" };

typedef struct {
    const char *label; /* libnma's msgid */
    const char *method;
    gboolean eap;      /* phase2-autheap instead of phase2-auth */
} InnerAuth;

/* As libnma offers them */
static const InnerAuth peap_inner[] = {
    { N_("MSCHAPv2"), "mschapv2", FALSE },
    { N_("MD5"), "md5", FALSE },
    { N_("GTC"), "gtc", FALSE },
};

static const InnerAuth ttls_inner[] = {
    { N_("PAP"), "pap", FALSE },
    { N_("MSCHAP"), "mschap", FALSE },
    { N_("MSCHAPv2 (no EAP)"), "mschapv2", FALSE },
    { N_("CHAP"), "chap", FALSE },
    { N_("MD5"), "md5", TRUE },
    { N_("GTC"), "gtc", TRUE },
    { N_("MSCHAPv2"), "mschapv2", TRUE },
};

typedef enum {
    FILE_CA_CERT,
    FILE_USER_CERT,
    FILE_USER_KEY,
} FileKind;

#define CC_TYPE_PLINIUS_HIDDEN_WIFI_OTHER (cc_plinius_hidden_wifi_other_get_type ())
G_DECLARE_FINAL_TYPE (CcPliniusHiddenWifiOther, cc_plinius_hidden_wifi_other, CC, PLINIUS_HIDDEN_WIFI_OTHER, AdwDialog)

struct _CcPliniusHiddenWifiOther {
    AdwDialog parent_instance;

    CcPliniusHiddenWifi *hidden; /* the first page: client and adapter */
    GCancellable *cancellable;   /* the file dialogs */

    AdwEntryRow *name_row;
    AdwComboRow *security_row;
    GtkLabel *name_error_label;

    AdwPreferencesGroup *wep_group;
    AdwPasswordEntryRow *wep_key_row;
    AdwComboRow *wep_index_row;
    AdwComboRow *wep_auth_row;

    AdwPreferencesGroup *leap_group;
    AdwEntryRow *leap_username_row;
    AdwPasswordEntryRow *leap_password_row;

    AdwPreferencesGroup *eap_group;
    AdwComboRow *eap_row;
    AdwComboRow *inner_row;
    AdwEntryRow *anonymous_row;
    AdwEntryRow *identity_row;
    AdwPasswordEntryRow *eap_password_row;
    AdwActionRow *cert_row;
    AdwActionRow *key_row;
    AdwPasswordEntryRow *key_password_row;

    AdwPreferencesGroup *server_group;
    AdwActionRow *ca_row;
    AdwSwitchRow *no_ca_row;
    AdwEntryRow *domain_row;
    AdwComboRow *peap_version_row;

    AdwPreferencesGroup *error_group;
    GtkLabel *error_label;
    GtkButton *connect_button;

    int inner_for; /* the EapMethod whose list inner_row shows, -1 for none */
    char *ca_path;
    char *cert_path;
    char *key_path;
    gboolean cert_is_pkcs12; /* certificate and key in one file */
};

G_DEFINE_FINAL_TYPE (CcPliniusHiddenWifiOther, cc_plinius_hidden_wifi_other, ADW_TYPE_DIALOG)

static OtherSecurity
other_security (CcPliniusHiddenWifiOther *self)
{
    return (OtherSecurity) adw_combo_row_get_selected (self->security_row);
}

static gboolean
other_uses_8021x (CcPliniusHiddenWifiOther *self)
{
    OtherSecurity security = other_security (self);

    return security == OTHER_DYNAMIC_WEP || security == OTHER_WPA_EAP || security == OTHER_WPA3_EAP;
}

static EapMethod
other_eap (CcPliniusHiddenWifiOther *self)
{
    return (EapMethod) adw_combo_row_get_selected (self->eap_row);
}

static const InnerAuth *
other_inner (CcPliniusHiddenWifiOther *self)
{
    guint i = adw_combo_row_get_selected (self->inner_row);

    if (self->inner_for == EAP_PEAP && i < G_N_ELEMENTS (peap_inner))
        return &peap_inner[i];
    if (self->inner_for == EAP_TTLS && i < G_N_ELEMENTS (ttls_inner))
        return &ttls_inner[i];

    return NULL;
}

/* The inner methods of PEAP or TTLS, kept as they are while the method stays */
static void
other_fill_inner (CcPliniusHiddenWifiOther *self, EapMethod method)
{
    g_autoptr(GtkStringList) names = NULL;
    const InnerAuth *list;
    guint n;

    if (self->inner_for == (int) method || (method != EAP_PEAP && method != EAP_TTLS))
        return;

    list = method == EAP_PEAP ? peap_inner : ttls_inner;
    n = method == EAP_PEAP ? G_N_ELEMENTS (peap_inner) : G_N_ELEMENTS (ttls_inner);
    names = gtk_string_list_new (NULL);
    for (guint i = 0; i < n; i++)
        gtk_string_list_append (names, NMA_(list[i].label));

    self->inner_for = method;
    adw_combo_row_set_model (self->inner_row, G_LIST_MODEL (names));
    /* MSCHAPv2 for PEAP, PAP for TTLS: libnma's first entries */
    adw_combo_row_set_selected (self->inner_row, 0);
}

static void
set_file_subtitle (AdwActionRow *row, const char *path, const char *none)
{
    g_autofree char *name = path ? g_path_get_basename (path) : NULL;

    adw_action_row_set_subtitle (row, name ? name : none);
}

static void
other_update (CcPliniusHiddenWifiOther *self)
{
    OtherSecurity security = other_security (self);
    gboolean wep = security == OTHER_WEP_KEY || security == OTHER_WEP_PASSPHRASE;
    gboolean x8021 = other_uses_8021x (self);
    EapMethod method = other_eap (self);
    gboolean tunneled = method == EAP_PEAP || method == EAP_TTLS;
    gboolean with_server = tunneled || method == EAP_TLS;
    gboolean valid;

    valid = check_ssid (self->name_row, self->name_error_label);
    if (!self->hidden || !selected_device (self->hidden))
        valid = FALSE;

    gtk_widget_set_visible (GTK_WIDGET (self->wep_group), wep);
    gtk_widget_set_visible (GTK_WIDGET (self->leap_group), security == OTHER_LEAP);
    gtk_widget_set_visible (GTK_WIDGET (self->eap_group), x8021);
    gtk_widget_set_visible (GTK_WIDGET (self->server_group), x8021 && with_server);

    if (wep) {
        gboolean key_valid = nm_utils_wep_key_valid (row_text (self->wep_key_row),
                                                     security == OTHER_WEP_KEY ? NM_WEP_KEY_TYPE_KEY
                                                                               : NM_WEP_KEY_TYPE_PASSPHRASE);

        mark_row (self->wep_key_row, key_valid);
        valid = valid && key_valid;
    } else if (security == OTHER_LEAP) {
        valid = valid && row_filled (self->leap_username_row) && row_filled (self->leap_password_row);
    } else if (x8021) {
        other_fill_inner (self, method);

        gtk_widget_set_visible (GTK_WIDGET (self->inner_row), tunneled);
        gtk_widget_set_visible (GTK_WIDGET (self->anonymous_row), tunneled);
        gtk_widget_set_visible (GTK_WIDGET (self->eap_password_row), method != EAP_TLS);
        gtk_widget_set_visible (GTK_WIDGET (self->cert_row), method == EAP_TLS);
        /* a PKCS#12 file brings its key */
        gtk_widget_set_visible (GTK_WIDGET (self->key_row), method == EAP_TLS && !self->cert_is_pkcs12);
        gtk_widget_set_visible (GTK_WIDGET (self->key_password_row), method == EAP_TLS);
        gtk_widget_set_visible (GTK_WIDGET (self->peap_version_row), method == EAP_PEAP);
        /* TLS asks for an identity, the others for a user name (libnma's labels) */
        adw_preferences_row_set_title (ADW_PREFERENCES_ROW (self->identity_row),
                                       method == EAP_TLS ? NMA_("I_dentity") : NMA_("_Username"));
        gtk_widget_set_sensitive (GTK_WIDGET (self->ca_row), !adw_switch_row_get_active (self->no_ca_row));

        valid = valid && row_filled (self->identity_row);
        if (method == EAP_TLS)
            valid = valid && self->cert_path && self->key_path;
        else
            valid = valid && row_filled (self->eap_password_row);
        if (with_server)
            valid = valid && (self->ca_path || adw_switch_row_get_active (self->no_ca_row));
    }

    gtk_widget_set_sensitive (GTK_WIDGET (self->connect_button), valid);
}

/* What NetworkManager would say about a file, before the form is sent */
static gboolean
check_file (FileKind kind, const char *path, NMSetting8021xCKFormat *format, GError **error)
{
    g_autoptr(NMSetting8021x) scratch = NM_SETTING_802_1X (nm_setting_802_1x_new ());

    switch (kind) {
    case FILE_CA_CERT:
        return nm_setting_802_1x_set_ca_cert (scratch, path, NM_SETTING_802_1X_CK_SCHEME_PATH, format, error);
    case FILE_USER_CERT:
        return nm_setting_802_1x_set_client_cert (scratch, path, NM_SETTING_802_1X_CK_SCHEME_PATH, format, error);
    case FILE_USER_KEY:
        /* without the password only the kind of file is checked */
        return nm_setting_802_1x_set_private_key (scratch, path, NULL, NM_SETTING_802_1X_CK_SCHEME_PATH, format,
                                                  error);
    default:
        g_return_val_if_reached (FALSE);
    }
}

static void
other_show_error (CcPliniusHiddenWifiOther *self, const char *message)
{
    gtk_label_set_label (self->error_label, message ? message : "");
    gtk_widget_set_visible (GTK_WIDGET (self->error_label), message != NULL);
    gtk_widget_set_visible (GTK_WIDGET (self->error_group), message != NULL);
}

/* Any change: the message of the last attempt is old */
static void
other_changed (CcPliniusHiddenWifiOther *self)
{
    other_show_error (self, NULL);
    other_update (self);
}

static void
other_file_chosen (CcPliniusHiddenWifiOther *self, FileKind kind, const char *path)
{
    NMSetting8021xCKFormat format = NM_SETTING_802_1X_CK_FORMAT_UNKNOWN;
    g_autoptr(GError) error = NULL;
    AdwActionRow *row;

    row = kind == FILE_CA_CERT ? self->ca_row : kind == FILE_USER_CERT ? self->cert_row : self->key_row;

    if (!check_file (kind, path, &format, &error)) {
        widget_set_error (GTK_WIDGET (row));
        other_show_error (self, error->message);
        return;
    }
    widget_unset_error (GTK_WIDGET (row));
    other_show_error (self, NULL);

    switch (kind) {
    case FILE_CA_CERT:
        g_free (self->ca_path);
        self->ca_path = g_strdup (path);
        set_file_subtitle (self->ca_row, self->ca_path, NMA_("(None)"));
        break;
    case FILE_USER_CERT:
    case FILE_USER_KEY:
        /* PKCS#12 holds certificate and key: one file for both, as libnma does */
        if (format == NM_SETTING_802_1X_CK_FORMAT_PKCS12) {
            self->cert_is_pkcs12 = TRUE;
            g_free (self->cert_path);
            self->cert_path = g_strdup (path);
            g_free (self->key_path);
            self->key_path = g_strdup (path);
        } else if (kind == FILE_USER_CERT) {
            /* a key from the PKCS#12 file before does not go with this certificate */
            if (self->cert_is_pkcs12)
                g_clear_pointer (&self->key_path, g_free);
            self->cert_is_pkcs12 = FALSE;
            g_free (self->cert_path);
            self->cert_path = g_strdup (path);
        } else {
            g_free (self->key_path);
            self->key_path = g_strdup (path);
        }
        set_file_subtitle (self->cert_row, self->cert_path, NMA_("No certificate set"));
        set_file_subtitle (self->key_row, self->key_path, NMA_("No key set"));
        break;
    default:
        g_assert_not_reached ();
    }

    other_update (self);
}

typedef struct {
    CcPliniusHiddenWifiOther *self; /* strong */
    FileKind kind;
} FileRequest;

static void
file_opened_cb (GObject *source, GAsyncResult *result, gpointer data)
{
    FileRequest *request = data;
    g_autoptr(GError) error = NULL;
    g_autoptr(GFile) file = gtk_file_dialog_open_finish (GTK_FILE_DIALOG (source), result, &error);
    g_autofree char *path = file ? g_file_get_path (file) : NULL;

    if (path)
        other_file_chosen (request->self, request->kind, path);
    else if (file)
        /* NetworkManager reads the file itself: it needs a path */
        g_warning ("Chosen certificate has no local path");
    else if (!g_error_matches (error, GTK_DIALOG_ERROR, GTK_DIALOG_ERROR_DISMISSED) &&
             !g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
        g_warning ("Could not choose a certificate: %s", error->message);

    g_object_unref (request->self);
    g_free (request);
}

static void
add_filter (GListStore *filters, const char *name, const char *const *patterns)
{
    g_autoptr(GtkFileFilter) filter = gtk_file_filter_new ();

    gtk_file_filter_set_name (filter, name);
    for (guint i = 0; patterns[i]; i++)
        gtk_file_filter_add_pattern (filter, patterns[i]);
    g_list_store_append (filters, filter);
}

static void
choose_file (CcPliniusHiddenWifiOther *self, FileKind kind)
{
    static const char *const cert_patterns[] = { "*.pem", "*.crt", "*.cer", "*.der", NULL };
    static const char *const key_patterns[] = { "*.der", "*.pem", "*.p12", "*.pfx", "*.key", NULL };
    static const char *const all_patterns[] = { "*", NULL };
    g_autoptr(GtkFileDialog) dialog = gtk_file_dialog_new ();
    g_autoptr(GListStore) filters = g_list_store_new (GTK_TYPE_FILE_FILTER);
    g_autofree char *title = NULL;
    const char *path = NULL;
    FileRequest *request;
    GtkRoot *root = gtk_widget_get_root (GTK_WIDGET (self->connect_button));

    /* libnma's titles: "CA" and "User" stay English there as well */
    switch (kind) {
    case FILE_CA_CERT:
        title = g_strdup_printf (NMA_("Choose a %s Certificate"), "CA");
        add_filter (filters, NMA_("PEM certificates (*.pem, *.crt, *.cer)"), cert_patterns);
        path = self->ca_path;
        break;
    case FILE_USER_CERT:
        title = g_strdup_printf (NMA_("Choose a %s Certificate"), "User");
        add_filter (filters, NMA_("PEM certificates (*.pem, *.crt, *.cer)"), cert_patterns);
        /* a PKCS#12 file is certificate and key at once */
        add_filter (filters, NMA_("DER, PEM, or PKCS#12 private keys (*.der, *.pem, *.p12, *.key)"), key_patterns);
        path = self->cert_path;
        break;
    case FILE_USER_KEY:
        title = g_strdup_printf (NMA_("Choose a key for %s Certificate"), "User");
        add_filter (filters, NMA_("DER, PEM, or PKCS#12 private keys (*.der, *.pem, *.p12, *.key)"), key_patterns);
        path = self->key_path;
        break;
    default:
        g_return_if_reached ();
    }
    add_filter (filters, NMA_("All files"), all_patterns);

    gtk_file_dialog_set_title (dialog, title);
    gtk_file_dialog_set_modal (dialog, TRUE);
    gtk_file_dialog_set_filters (dialog, G_LIST_MODEL (filters));
    if (path) {
        g_autoptr(GFile) file = g_file_new_for_path (path);

        gtk_file_dialog_set_initial_file (dialog, file);
    }

    request = g_new0 (FileRequest, 1);
    request->self = g_object_ref (self);
    request->kind = kind;
    gtk_file_dialog_open (dialog, GTK_IS_WINDOW (root) ? GTK_WINDOW (root) : NULL, self->cancellable,
                          file_opened_cb, request);
}

static void
ca_row_activated_cb (CcPliniusHiddenWifiOther *self)
{
    choose_file (self, FILE_CA_CERT);
}

static void
cert_row_activated_cb (CcPliniusHiddenWifiOther *self)
{
    choose_file (self, FILE_USER_CERT);
}

static void
key_row_activated_cb (CcPliniusHiddenWifiOther *self)
{
    choose_file (self, FILE_USER_KEY);
}

static gboolean
other_fill_8021x (CcPliniusHiddenWifiOther *self, NMConnection *connection, GError **error)
{
    NMSetting8021x *s8021x = NM_SETTING_802_1X (nm_setting_802_1x_new ());
    EapMethod method = other_eap (self);
    const InnerAuth *inner = other_inner (self);
    guint peap_version = adw_combo_row_get_selected (self->peap_version_row);

    /* the connection owns it from here, also when a file is refused below */
    nm_connection_add_setting (connection, NM_SETTING (s8021x));

    nm_setting_802_1x_add_eap_method (s8021x, eap_names[method]);
    g_object_set (s8021x, NM_SETTING_802_1X_IDENTITY, row_text (self->identity_row), NULL);

    if (method != EAP_TLS)
        g_object_set (s8021x, NM_SETTING_802_1X_PASSWORD, row_text (self->eap_password_row), NULL);

    if (method == EAP_PEAP || method == EAP_TTLS) {
        g_object_set (s8021x, NM_SETTING_802_1X_ANONYMOUS_IDENTITY, row_text_or_null (self->anonymous_row), NULL);
        if (inner)
            g_object_set (s8021x,
                          inner->eap ? NM_SETTING_802_1X_PHASE2_AUTHEAP : NM_SETTING_802_1X_PHASE2_AUTH,
                          inner->method,
                          NULL);
    }

    /* PEAP version: 0 is "Automatic" (unset), then "Version 0" and "Version 1" */
    if (method == EAP_PEAP && peap_version > 0 && peap_version != GTK_INVALID_LIST_POSITION)
        g_object_set (s8021x, NM_SETTING_802_1X_PHASE1_PEAPVER, peap_version == 1 ? "0" : "1", NULL);

    if (method == EAP_PEAP || method == EAP_TTLS || method == EAP_TLS) {
        g_object_set (s8021x, NM_SETTING_802_1X_DOMAIN_SUFFIX_MATCH, row_text_or_null (self->domain_row), NULL);
        /* "No CA certificate is required": none, as in libnma */
        if (!adw_switch_row_get_active (self->no_ca_row) &&
            !nm_setting_802_1x_set_ca_cert (s8021x, self->ca_path, NM_SETTING_802_1X_CK_SCHEME_PATH, NULL, error))
            return FALSE;
    }

    if (method == EAP_TLS) {
        if (!nm_setting_802_1x_set_client_cert (s8021x, self->cert_path, NM_SETTING_802_1X_CK_SCHEME_PATH, NULL,
                                                error))
            return FALSE;
        /* checks the password against the key, so a wrong one is said here */
        if (!nm_setting_802_1x_set_private_key (s8021x, self->key_path, row_text_or_null (self->key_password_row),
                                                NM_SETTING_802_1X_CK_SCHEME_PATH, NULL, error))
            return FALSE;
    }

    return TRUE;
}

/* The connection as libnma's security widgets fill it */
static NMConnection *
other_build_connection (CcPliniusHiddenWifiOther *self, GError **error)
{
    g_autoptr(NMConnection) connection = new_hidden_connection (row_text (self->name_row));
    NMSettingWirelessSecurity *ssec = NM_SETTING_WIRELESS_SECURITY (nm_setting_wireless_security_new ());
    OtherSecurity security = other_security (self);
    guint index;

    nm_connection_add_setting (connection, NM_SETTING (ssec));

    switch (security) {
    case OTHER_WEP_KEY:
    case OTHER_WEP_PASSPHRASE:
        index = adw_combo_row_get_selected (self->wep_index_row);
        if (index > 3)
            index = 0;
        g_object_set (ssec,
                      NM_SETTING_WIRELESS_SECURITY_KEY_MGMT, "none",
                      NM_SETTING_WIRELESS_SECURITY_WEP_TX_KEYIDX, index,
                      NM_SETTING_WIRELESS_SECURITY_AUTH_ALG,
                      adw_combo_row_get_selected (self->wep_auth_row) == 1 ? "shared" : "open",
                      NM_SETTING_WIRELESS_SECURITY_WEP_KEY_TYPE,
                      security == OTHER_WEP_KEY ? NM_WEP_KEY_TYPE_KEY : NM_WEP_KEY_TYPE_PASSPHRASE,
                      NULL);
        nm_setting_wireless_security_set_wep_key (ssec, index, row_text (self->wep_key_row));
        break;
    case OTHER_LEAP:
        g_object_set (ssec,
                      NM_SETTING_WIRELESS_SECURITY_KEY_MGMT, "ieee8021x",
                      NM_SETTING_WIRELESS_SECURITY_AUTH_ALG, "leap",
                      NM_SETTING_WIRELESS_SECURITY_LEAP_USERNAME, row_text (self->leap_username_row),
                      NM_SETTING_WIRELESS_SECURITY_LEAP_PASSWORD, row_text (self->leap_password_row),
                      NULL);
        break;
    case OTHER_DYNAMIC_WEP:
        g_object_set (ssec,
                      NM_SETTING_WIRELESS_SECURITY_KEY_MGMT, "ieee8021x",
                      NM_SETTING_WIRELESS_SECURITY_AUTH_ALG, "open",
                      NULL);
        break;
    case OTHER_WPA_EAP:
        g_object_set (ssec, NM_SETTING_WIRELESS_SECURITY_KEY_MGMT, "wpa-eap", NULL);
        break;
    case OTHER_WPA3_EAP:
        /* WPA3 Enterprise (not the 192-bit mode): WPA2 Enterprise with protected management frames */
        g_object_set (ssec,
                      NM_SETTING_WIRELESS_SECURITY_KEY_MGMT, "wpa-eap",
                      NM_SETTING_WIRELESS_SECURITY_PMF, NM_SETTING_WIRELESS_SECURITY_PMF_REQUIRED,
                      NULL);
        break;
    default:
        g_return_val_if_reached (NULL);
    }

    if (other_uses_8021x (self) && !other_fill_8021x (self, connection, error))
        return NULL;

    return g_steal_pointer (&connection);
}

static void
other_connect_clicked_cb (CcPliniusHiddenWifiOther *self)
{
    g_autoptr(NMConnection) connection = NULL;
    g_autoptr(CcPliniusHiddenWifi) hidden = NULL;
    g_autoptr(GError) error = NULL;
    NMDevice *device;

    if (!gtk_widget_get_sensitive (GTK_WIDGET (self->connect_button)) || !self->hidden)
        return;

    device = selected_device (self->hidden);
    g_return_if_fail (device != NULL);

    connection = other_build_connection (self, &error);
    if (!connection) {
        other_show_error (self, error ? error->message : NULL);
        return;
    }

    activate_connection (self->hidden->client, connection, device);

    /* Both pages go: closing the first takes this one along (as pages: one pop back to Wi-Fi) */
    hidden = g_object_ref (self->hidden);
    g_object_ref (self);
    adw_dialog_close (ADW_DIALOG (hidden));
    adw_dialog_close (ADW_DIALOG (self));
    g_object_unref (self);
}

static void
cc_plinius_hidden_wifi_other_dispose (GObject *object)
{
    CcPliniusHiddenWifiOther *self = CC_PLINIUS_HIDDEN_WIFI_OTHER (object);

    g_cancellable_cancel (self->cancellable);
    g_clear_object (&self->cancellable);
    g_clear_object (&self->hidden);

    G_OBJECT_CLASS (cc_plinius_hidden_wifi_other_parent_class)->dispose (object);
}

static void
cc_plinius_hidden_wifi_other_finalize (GObject *object)
{
    CcPliniusHiddenWifiOther *self = CC_PLINIUS_HIDDEN_WIFI_OTHER (object);

    g_free (self->ca_path);
    g_free (self->cert_path);
    g_free (self->key_path);

    G_OBJECT_CLASS (cc_plinius_hidden_wifi_other_parent_class)->finalize (object);
}

static void
cc_plinius_hidden_wifi_other_class_init (CcPliniusHiddenWifiOtherClass *klass)
{
    G_OBJECT_CLASS (klass)->dispose = cc_plinius_hidden_wifi_other_dispose;
    G_OBJECT_CLASS (klass)->finalize = cc_plinius_hidden_wifi_other_finalize;
}

static AdwActionRow *
add_file_row (AdwPreferencesGroup *group, const char *title, const char *none, GCallback activated,
              CcPliniusHiddenWifiOther *self)
{
    GtkWidget *row = adw_action_row_new ();

    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), title);
    adw_preferences_row_set_use_underline (ADW_PREFERENCES_ROW (row), TRUE);
    adw_action_row_set_subtitle (ADW_ACTION_ROW (row), none);
    adw_action_row_add_suffix (ADW_ACTION_ROW (row), gtk_image_new_from_icon_name ("document-open-symbolic"));
    gtk_list_box_row_set_activatable (GTK_LIST_BOX_ROW (row), TRUE);
    g_signal_connect_swapped (row, "activated", activated, self);
    adw_preferences_group_add (group, row);

    return ADW_ACTION_ROW (row);
}

static void
cc_plinius_hidden_wifi_other_init (CcPliniusHiddenWifiOther *self)
{
    const char *const securities[] = {
        NMA_("WEP 40/128-bit Key (Hex or ASCII)"),
        NMA_("WEP 128-bit Passphrase"),
        NMA_("LEAP"),
        NMA_("Dynamic WEP (802.1x)"),
        NMA_("WPA & WPA2 Enterprise"),
        P_("WPA3 Enterprise"),
        NULL,
    };
    const char *const methods[] = {
        NMA_("Protected EAP (PEAP)"),
        NMA_("Tunneled TLS"),
        NMA_("TLS"),
        NMA_("PWD"),
        NMA_("LEAP"),
        NULL,
    };
    const char *const wep_indices[] = { NMA_("1 (Default)"), "2", "3", "4", NULL };
    const char *const wep_auths[] = { NMA_("Open System"), NMA_("Shared Key"), NULL };
    const char *const peap_versions[] = { NMA_("Automatic"), NMA_("Version 0"), NMA_("Version 1"), NULL };
    g_autofree char *ca_title = g_strdup_printf (NMA_("%s _certificate"), "CA");
    g_autofree char *cert_title = g_strdup_printf (NMA_("%s _certificate"), "User");
    g_autofree char *key_title = g_strdup_printf (NMA_("%s private _key"), "User");
    g_autofree char *key_password_title = g_strdup_printf (NMA_("%s key _password"), "User");
    AdwPreferencesPage *page;
    AdwPreferencesGroup *group;

    self->cancellable = g_cancellable_new ();
    self->inner_for = -1;

    page = setup_form (ADW_DIALOG (self), P_("Other Security"));

    /* Name and security; the name is the first page's (bound in _new()) */
    group = add_group (page);
    self->name_row = add_entry_row (group, NMA_("_Network name"));
    self->security_row = add_combo_row (group, NMA_("Wi-Fi _security"), securities);
    /* why one comes here most of the time */
    adw_combo_row_set_selected (self->security_row, OTHER_WPA_EAP);
    self->name_error_label = add_hint_label (group);

    /* WEP: key, its index and the authentication */
    self->wep_group = add_group (page);
    self->wep_key_row = add_password_row (self->wep_group, NMA_("_Key"));
    self->wep_index_row = add_combo_row (self->wep_group, NMA_("WEP inde_x"), wep_indices);
    self->wep_auth_row = add_combo_row (self->wep_group, NMA_("Au_thentication"), wep_auths);

    /* LEAP: user name and password */
    self->leap_group = add_group (page);
    self->leap_username_row = add_entry_row (self->leap_group, NMA_("_Username"));
    self->leap_password_row = add_password_row (self->leap_group, NMA_("_Password"));

    /* 802.1X: the method and who signs in */
    self->eap_group = add_group (page);
    self->eap_row = add_combo_row (self->eap_group, NMA_("Au_thentication"), methods);
    self->inner_row = add_combo_row (self->eap_group, NMA_("_Inner authentication"), NULL);
    self->anonymous_row = add_entry_row (self->eap_group, NMA_("Anony_mous identity"));
    self->identity_row = add_entry_row (self->eap_group, NMA_("_Username"));
    self->eap_password_row = add_password_row (self->eap_group, NMA_("_Password"));
    self->cert_row = add_file_row (self->eap_group, cert_title, NMA_("No certificate set"),
                                   G_CALLBACK (cert_row_activated_cb), self);
    self->key_row = add_file_row (self->eap_group, key_title, NMA_("No key set"),
                                  G_CALLBACK (key_row_activated_cb), self);
    self->key_password_row = add_password_row (self->eap_group, key_password_title);

    /* 802.1X: how the network proves itself */
    self->server_group = add_group (page);
    self->ca_row = add_file_row (self->server_group, ca_title, NMA_("(None)"), G_CALLBACK (ca_row_activated_cb), self);
    self->no_ca_row = ADW_SWITCH_ROW (adw_switch_row_new ());
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (self->no_ca_row), NMA_("No CA certificate is _required"));
    adw_preferences_row_set_use_underline (ADW_PREFERENCES_ROW (self->no_ca_row), TRUE);
    adw_preferences_group_add (self->server_group, GTK_WIDGET (self->no_ca_row));
    self->domain_row = add_entry_row (self->server_group, NMA_("_Domain"));
    gtk_widget_set_tooltip_text (GTK_WIDGET (self->domain_row), NMA_("Suffix of the server certificate name."));
    self->peap_version_row = add_combo_row (self->server_group, NMA_("PEAP _version"), peap_versions);
    /* what NetworkManager said about a file or the form, below the rows shown; a group of its
     * own, shown with the message only */
    self->error_group = add_group (page);
    self->error_label = add_hint_label (self->error_group);
    gtk_widget_set_visible (GTK_WIDGET (self->error_group), FALSE);

    self->connect_button = add_connect_button (page);
    adw_dialog_set_default_widget (ADW_DIALOG (self), GTK_WIDGET (self->connect_button));

    {
        gpointer entries[] = {
            self->name_row, self->wep_key_row, self->leap_username_row, self->leap_password_row,
            self->anonymous_row, self->identity_row, self->eap_password_row, self->key_password_row,
            self->domain_row,
        };
        gpointer combos[] = {
            self->security_row, self->wep_index_row, self->wep_auth_row, self->eap_row, self->inner_row,
            self->peap_version_row,
        };

        for (guint i = 0; i < G_N_ELEMENTS (entries); i++) {
            g_signal_connect_swapped (entries[i], "changed", G_CALLBACK (other_changed), self);
            /* Enter in a field: connect, if the form is complete */
            g_signal_connect_swapped (entries[i], "entry-activated", G_CALLBACK (other_connect_clicked_cb), self);
        }
        for (guint i = 0; i < G_N_ELEMENTS (combos); i++)
            g_signal_connect_swapped (combos[i], "notify::selected", G_CALLBACK (other_changed), self);
    }
    g_signal_connect_swapped (self->no_ca_row, "notify::active", G_CALLBACK (other_changed), self);
    g_signal_connect_swapped (self->connect_button, "clicked", G_CALLBACK (other_connect_clicked_cb), self);
}

static AdwDialog *
cc_plinius_hidden_wifi_other_new (CcPliniusHiddenWifi *hidden)
{
    CcPliniusHiddenWifiOther *self = g_object_new (CC_TYPE_PLINIUS_HIDDEN_WIFI_OTHER, NULL);

    self->hidden = g_object_ref (hidden);
    /* one name for both pages: typed on either, it is the same network */
    g_object_bind_property (hidden->name_row, "text", self->name_row, "text",
                            G_BINDING_BIDIRECTIONAL | G_BINDING_SYNC_CREATE);
    other_update (self);

    return ADW_DIALOG (self);
}

/* ---- the first page, continued ---- */

static void
other_security_activated_cb (CcPliniusHiddenWifi *self)
{
    /* On top of this page, not instead of it: back (arrow or swipe) leads here again. Presented
     * for this dialog, which as a page is in no window: cc-plinius-dialog-pages.c pushes it
     * above this page then */
    adw_dialog_present (cc_plinius_hidden_wifi_other_new (self), GTK_WIDGET (self));
}

static void
fill_devices (CcPliniusHiddenWifi *self)
{
    g_autoptr(GtkStringList) names = gtk_string_list_new (NULL);
    const GPtrArray *devices = nm_client_get_devices (self->client);

    for (guint i = 0; i < devices->len; i++) {
        NMDevice *device = g_ptr_array_index (devices, i);

        if (!NM_IS_DEVICE_WIFI (device) || !nm_device_get_managed (device) || panel_wifi_device_is_ap_only (device))
            continue;

        g_ptr_array_add (self->devices, g_object_ref (device));
        gtk_string_list_append (names, nm_device_get_iface (device));
    }

    adw_combo_row_set_model (self->device_row, G_LIST_MODEL (names));
    /* one adapter (the phone): nothing to choose */
    gtk_widget_set_visible (GTK_WIDGET (self->device_row), self->devices->len > 1);
}

static void
cc_plinius_hidden_wifi_dispose (GObject *object)
{
    CcPliniusHiddenWifi *self = CC_PLINIUS_HIDDEN_WIFI (object);

    g_clear_object (&self->client);
    g_clear_pointer (&self->devices, g_ptr_array_unref);

    G_OBJECT_CLASS (cc_plinius_hidden_wifi_parent_class)->dispose (object);
}

static void
cc_plinius_hidden_wifi_class_init (CcPliniusHiddenWifiClass *klass)
{
    G_OBJECT_CLASS (klass)->dispose = cc_plinius_hidden_wifi_dispose;
}

static void
cc_plinius_hidden_wifi_init (CcPliniusHiddenWifi *self)
{
    const char *const securities[] = {
        g_dpgettext2 (NMA_DOMAIN, "Wifi/wired security", "None"),
        NMA_("WPA & WPA2 Personal"),
        P_("WPA3 Personal"),
        NULL,
    };
    AdwPreferencesPage *page;
    AdwPreferencesGroup *group;
    GtkWidget *other_row;

    self->devices = g_ptr_array_new_with_free_func (g_object_unref);

    page = setup_form (ADW_DIALOG (self), NMA_("Connect to Hidden Wi-Fi Network"));

    /* Name, security and password, with libnma's explaining sentence above */
    group = add_group (page);
    adw_preferences_group_set_description (group,
                                           NMA_("Enter the name and security details of the hidden Wi-Fi "
                                                "network you wish to connect to."));

    self->name_row = add_entry_row (group, NMA_("_Network name"));
    self->security_row = add_combo_row (group, NMA_("Wi-Fi _security"), securities);
    /* what most networks use */
    adw_combo_row_set_selected (self->security_row, SECURITY_WPA_PSK);
    self->password_row = add_password_row (group, NMA_("_Password"));
    self->device_row = add_combo_row (group, NMA_("Wi-Fi _adapter"), NULL);
    /* below the list, as the hotspot's hints */
    self->name_error_label = add_hint_label (group);

    /* WEP, LEAP, 802.1X: the page above */
    group = add_group (page);
    other_row = adw_action_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (other_row), P_("Other Security"));
    adw_action_row_set_subtitle (ADW_ACTION_ROW (other_row), P_("WEP, LEAP or Enterprise (802.1X)"));
    adw_action_row_add_suffix (ADW_ACTION_ROW (other_row), gtk_image_new_from_icon_name ("go-next-symbolic"));
    gtk_list_box_row_set_activatable (GTK_LIST_BOX_ROW (other_row), TRUE);
    g_signal_connect_swapped (other_row, "activated", G_CALLBACK (other_security_activated_cb), self);
    adw_preferences_group_add (group, other_row);

    self->connect_button = add_connect_button (page);
    adw_dialog_set_default_widget (ADW_DIALOG (self), GTK_WIDGET (self->connect_button));

    g_signal_connect_swapped (self->name_row, "changed", G_CALLBACK (update), self);
    g_signal_connect_swapped (self->password_row, "changed", G_CALLBACK (update), self);
    g_signal_connect_swapped (self->security_row, "notify::selected", G_CALLBACK (update), self);
    g_signal_connect_swapped (self->device_row, "notify::selected", G_CALLBACK (update), self);
    /* Enter in a field: connect, if the form is complete */
    g_signal_connect_swapped (self->name_row, "entry-activated", G_CALLBACK (connect_clicked_cb), self);
    g_signal_connect_swapped (self->password_row, "entry-activated", G_CALLBACK (connect_clicked_cb), self);
    g_signal_connect_swapped (self->connect_button, "clicked", G_CALLBACK (connect_clicked_cb), self);
}

AdwDialog *
cc_plinius_hidden_wifi_new (NMClient *client)
{
    CcPliniusHiddenWifi *self;

    g_return_val_if_fail (NM_IS_CLIENT (client), NULL);

    self = g_object_new (CC_TYPE_PLINIUS_HIDDEN_WIFI, NULL);
    self->client = g_object_ref (client);
    fill_devices (self);
    update (self);

    return ADW_DIALOG (self);
}
