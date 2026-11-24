/*
 * cc-plinius-dnsfilter.c
 *
 * Plinius: "DNS Filter" on the Network page, a group of its own at the top whose row opens the
 * pages of plinius-dnsfilter (org.plinius.DnsFilter on the system bus):
 * - the filter's switch, a pause, the last 24 hours at a glance;
 * - Query Log: every name asked once, the one asked last on top and live; a tap opens the
 *   name's page with Block or Unblock (an own rule, or removing the own rule that decided it)
 *   and its queries by app, each app's newest first;
 * - Statistics: totals, the most asked and most blocked names (a tap opens the name's page),
 *   apps, clients and servers;
 * - Blocklists: switch, update or remove subscribed lists, add one from the suggestions or by
 *   its web address;
 * - Own Rules: block or allow a domain (with or without its subdomains), remove rules;
 * - DNS Servers: the network's servers or own ones (plain, DNS over TLS, DNS over HTTPS) with a
 *   test, suggestions;
 * - Settings: answer for blocked names, hotspot and USB devices, fixed and encrypted DNS of
 *   apps, how long the log and the statistics are kept.
 * The service decides who may change what (the user in front of the phone, no PIN). It leaves
 * while the filter is off; D-Bus starts it again when these pages ask, Watch() keeps it while
 * they are open and sends the live log.
 *
 * Nothing of this shows without plinius-dnsfilter. Our own strings use the gettext domain
 * plinius-control-center (not GNOME's po/).
 *
 * SPDX-License-Identifier: MIT
 */

#undef G_LOG_DOMAIN
#define G_LOG_DOMAIN "cc-plinius-dnsfilter"

#include <gio/gdesktopappinfo.h>
#include <glib/gi18n.h>

#include "cc-list-row.h"
#include "cc-plinius-dnsfilter.h"

#define PLINIUS_DOMAIN "plinius-control-center"
#define P_(String) g_dgettext (PLINIUS_DOMAIN, String)

#define SERVICE "/usr/bin/plinius-dnsfilter"
#define BUS_NAME "org.plinius.DnsFilter"
#define OBJECT_PATH "/org/plinius/DnsFilter"
#define INTERFACE "org.plinius.DnsFilter"

#define TAG_MAIN "plinius-dnsfilter"
#define TAG_LOG "plinius-dnsfilter-log"
#define TAG_STATS "plinius-dnsfilter-stats"
#define TAG_LISTS "plinius-dnsfilter-lists"
#define TAG_ADD_LIST "plinius-dnsfilter-add-list"
#define TAG_RULES "plinius-dnsfilter-rules"
#define TAG_SERVERS "plinius-dnsfilter-servers"
#define TAG_SETTINGS "plinius-dnsfilter-settings"
#define TAG_NAME "plinius-dnsfilter-name"
/* The statistics' lists, each a page of its own; their tags start with TAG_STATS */
#define TAG_STATS_ASKED TAG_STATS "-asked"
#define TAG_STATS_BLOCKED TAG_STATS "-blocked"
#define TAG_STATS_APPS TAG_STATS "-apps"
#define TAG_STATS_DEVICES TAG_STATS "-devices"
#define TAG_STATS_SERVERS TAG_STATS "-servers"
/* The names one app or device asked, from the Apps and Devices pages */
#define TAG_STATS_WHO TAG_STATS "-who"
/* The end of a rule that holds for one app only (plinius-dnsfilter) */
#define APP_MODIFIER "$app="

/* Log rows (names, or one name's queries) fetched at once, and kept while the log runs live */
#define LOG_PAGE 150
#define LOG_MAX_ROWS 500
#define REFRESH_S 10

/* The last field is the app that asked (plinius-dnsfilter's appid.h), "" if unknown */
#define LOG_TYPE "(uxsqssussssuqbs)"
#define LOG_TYPE_BORROWED "(ux&sq&s&su&s&s&s&suqb&s)"
/* GetLog: the search is the whole name */
#define LOG_EXACT 4

/* Status of a query, as plinius-dnsfilter counts them */
enum {
    ST_PASSED,
    ST_ALLOWED_RULE,
    ST_ALLOWED_LIST,
    ST_ALLOWED_SYSTEM,
    ST_BLOCKED_LIST,
    ST_BLOCKED_RULE,
    ST_UNFILTERED,
    ST_FAILED,
    ST_BLOCKED_SPECIAL,
};

static gboolean
status_blocked (guint32 status)
{
    return status == ST_BLOCKED_LIST || status == ST_BLOCKED_RULE || status == ST_BLOCKED_SPECIAL;
}

/* Servers one can add with a tap; all answered when tested (November 2026) */
static const struct {
    const char *name, *spec;
} suggested_servers[] = {
    { "Quad9", "tls://dns.quad9.net" },
    { "Quad9", "https://dns.quad9.net/dns-query" },
    { "Cloudflare", "tls://one.one.one.one" },
    { "Cloudflare", "https://cloudflare-dns.com/dns-query" },
    { "Mullvad", "https://dns.mullvad.net/dns-query" },
    { "DNS4EU", "https://unfiltered.joindns4.eu/dns-query" },
    { "Digitalcourage", "tls://dns3.digitalcourage.de" },
    { "Google", "tls://dns.google" },
};

/* Descriptions of the suggested lists: plinius-dnsfilter sends them in English, these marks
 * put them into our translations */
G_GNUC_UNUSED static const char *const catalog_descriptions[] = {
    N_("Ads, tracking, metrics, telemetry, phishing, malware; rarely breaks anything"),
    N_("The basics, for a phone that must never miss anything"),
    N_("Stricter; now and then a site needs an exception"),
    N_("Aggressive; expect exceptions"),
    N_("Malware, phishing, scams and command and control servers"),
    N_("Encrypted DNS and VPN services apps could use to get past the filter"),
    N_("Ads and tracking, made not to break anything"),
    N_("Ads, tracking, malware and more"),
    N_("Ads and tracking, put together from several lists"),
    N_("Ads and malware, in hosts format"),
};

typedef struct DnsFilter DnsFilter;

/* A row that names a domain (log, statistics, the name's page, an app's or device's names); those
 * on the name's page and on the app's or device's page have the button to block or unblock it */
typedef struct {
    DnsFilter *f;
    GtkWidget *row;
    GtkWidget *button; /* NULL without */
    char *name;
    char *app; /* the button blocks or unblocks for this app only; NULL: for all */
    char *detail; /* time, who asked and how often, of a log row */
    guint32 status;
    char *reason;
    char *via;
    guint32 id; /* of a log row */
    guint32 count, blocked; /* queries of the name in the log, and how many were blocked */
} NameRow;

struct DnsFilter {
    int ref; /* each page holds one */
    CcPanel *panel;
    GDBusProxy *proxy;
    GCancellable *cancel;
    gboolean syncing;
    guint watching; /* pages that are shown */
    guint refresh;

    /* Row on the Network page */
    GtkWidget *network_row;

    /* Main page */
    AdwSwitchRow *enable_row;
    AdwActionRow *pause_row;
    GtkWidget *pause_button, *resume_button;
    GtkWidget *chart;
    GArray *chart_total, *chart_blocked;
    GArray *chart_data[2];
    GtkLabel *queries_value, *blocked_value, *average_value;
    AdwActionRow *lists_nav, *rules_nav, *servers_nav;

    /* Query log */
    GtkWidget *log_search;
    AdwToggleGroup *log_filter;
    AdwPreferencesGroup *log_group;
    GtkListBox *log_list; /* a list box: the name asked last goes on top */
    GPtrArray *log_rows; /* NameRow*, top first */
    GHashTable *log_names; /* name → its NameRow in log_rows */
    GtkWidget *log_page, *log_more;
    GtkWidget *log_empty, *log_empty_hint; /* the placeholder's title and its note on a log in memory */
    guint log_generation; /* answers to an older GetLogNames are dropped */
    gboolean log_shown;
    gboolean log_keep; /* a name's page is open on top: the list stays as it is */

    /* One name's queries */
    char *detail_name;
    GtkWidget *detail_page;
    AdwPreferencesGroup *detail_head_group;
    GPtrArray *detail_names; /* the head row's NameRow (0 or 1) */
    GtkListBox *detail_list; /* an expander row per app, the one asked last on top */
    GHashTable *detail_groups; /* app key → its DetailGroup (owned by the expander row) */
    GtkWidget *detail_more;
    GtkWidget *detail_empty_hint;
    guint32 detail_oldest;
    guint detail_rows, detail_generation;
    guint detail_days; /* of the statistics for who asked; 0: all that are kept */
    gboolean detail_shown;
    gboolean stats_shown, catalog_shown;

    /* Statistics */
    AdwToggleGroup *stats_range;
    GtkWidget *stats_chart;
    GArray *stats_total, *stats_blocked;
    GArray *stats_data[2];
    GtkLabel *s_queries, *s_blocked, *s_cached, *s_average, *s_rules;
    AdwPreferencesGroup *top_group, *blocked_group, *apps_group, *clients_group, *servers_group;
    /* Rows to their pages, the top entry as subtitle */
    AdwActionRow *top_nav, *blocked_nav, *apps_nav, *clients_nav, *upstreams_nav;
    GPtrArray *stats_rows; /* (group, row) pairs */
    GPtrArray *stats_names; /* NameRow* of the top lists */

    /* The names of one app or device */
    char *who_key; /* app key, or "\t<kind>\t<client>" of a device */
    char *who_title;
    GtkWidget *who_page;
    AdwPreferencesGroup *who_blocked_group, *who_allowed_group;
    GPtrArray *who_rows; /* (group, row) pairs */
    guint who_generation;
    gboolean who_shown;

    /* Blocklists */
    AdwPreferencesGroup *lists_group;
    GPtrArray *list_rows;
    AdwComboRow *interval_row;
    AdwSwitchRow *mobile_row;
    AdwPreferencesGroup *catalog_group;
    GPtrArray *catalog_rows;
    AdwEntryRow *list_name, *list_url;
    AdwPreferencesGroup *add_list_group;

    /* Own rules */
    AdwEntryRow *rule_entry;
    AdwToggleGroup *rule_kind;
    AdwSwitchRow *rule_subdomains;
    AdwPreferencesGroup *new_rule_group, *rules_group;
    GPtrArray *rule_rows;

    /* DNS servers */
    GtkCheckButton *network_check, *custom_check;
    AdwActionRow *network_source_row;
    AdwPreferencesGroup *custom_group, *suggest_group;
    GPtrArray *server_rows, *suggest_rows;
    AdwEntryRow *server_entry;
    AdwSwitchRow *local_row;

    /* Settings */
    AdwComboRow *block_mode_row, *log_size_row, *stats_days_row;
    AdwSwitchRow *shared_row, *redirect_row, *encrypted_row, *keep_log_row;
};

static void refresh_all (DnsFilter *f);
static void on_done_refresh (DnsFilter *f, GVariant *result, GError *error, gpointer data);
static void refresh_stats (DnsFilter *f);
static void reload_log (DnsFilter *f);
static void name_row_update (NameRow *nr);
static void dns_filter_unref (DnsFilter *f);
static void detail_check_groups (DnsFilter *f);
static void load_who (DnsFilter *f);

/* ----- D-Bus ----- */

static GVariant *
prop (DnsFilter *f, const char *name)
{
    return f->proxy ? g_dbus_proxy_get_cached_property (f->proxy, name) : NULL;
}

static gboolean
prop_bool (DnsFilter *f, const char *name)
{
    g_autoptr(GVariant) v = prop (f, name);

    return v && g_variant_is_of_type (v, G_VARIANT_TYPE_BOOLEAN) && g_variant_get_boolean (v);
}

static guint32
prop_uint (DnsFilter *f, const char *name)
{
    g_autoptr(GVariant) v = prop (f, name);

    return v && g_variant_is_of_type (v, G_VARIANT_TYPE_UINT32) ? g_variant_get_uint32 (v) : 0;
}

static char *
prop_string (DnsFilter *f, const char *name)
{
    g_autoptr(GVariant) v = prop (f, name);

    return g_strdup (v && g_variant_is_of_type (v, G_VARIANT_TYPE_STRING) ? g_variant_get_string (v, NULL) : "");
}

static GStrv
prop_strv (DnsFilter *f, const char *name)
{
    g_autoptr(GVariant) v = prop (f, name);

    return v && g_variant_is_of_type (v, G_VARIANT_TYPE_STRING_ARRAY) ? g_variant_dup_strv (v, NULL)
                                                                       : g_new0 (char *, 1);
}

/* Errors of the service, without GDBus' prefix */
static char *
error_text (GError *error)
{
    g_autofree char *remote = g_dbus_error_get_remote_error (error);

    if (remote)
        g_dbus_error_strip_remote_error (error);
    return g_strdup (error->message);
}

typedef void (*DoneFunc) (DnsFilter *f, GVariant *result, GError *error, gpointer data);

typedef struct {
    DnsFilter *f;
    DoneFunc done;
    gpointer data;
} Call;

static void
on_call_done (GObject *source, GAsyncResult *res, gpointer user_data)
{
    Call *c = user_data;
    g_autoptr(GError) error = NULL;
    g_autoptr(GVariant) result = g_dbus_connection_call_finish (G_DBUS_CONNECTION (source), res, &error);

    if (g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED)) {
        g_free (c);
        return;
    }
    if (error)
        g_warning ("plinius-dnsfilter: %s", error->message);
    if (c->done)
        c->done (c->f, result, error, c->data);
    g_free (c);
}

static void
call (DnsFilter *f, const char *method, GVariant *params, DoneFunc done, gpointer data)
{
    Call *c;

    if (!f->proxy) {
        if (params)
            g_variant_unref (g_variant_ref_sink (params));
        return;
    }
    c = g_new0 (Call, 1);
    c->f = f;
    c->done = done;
    c->data = data;
    g_dbus_connection_call (g_dbus_proxy_get_connection (f->proxy), BUS_NAME, OBJECT_PATH, INTERFACE, method, params,
                            NULL, G_DBUS_CALL_FLAGS_NONE, 30000, f->cancel, on_call_done, c);
}

static void
set_prop_full (DnsFilter *f, const char *name, GVariant *value, DoneFunc done, gpointer data)
{
    Call *c = g_new0 (Call, 1);

    if (!f->proxy || f->syncing) {
        g_variant_unref (g_variant_ref_sink (value));
        g_free (c);
        return;
    }
    c->f = f;
    c->done = done;
    c->data = data;
    g_dbus_connection_call (g_dbus_proxy_get_connection (f->proxy), BUS_NAME, OBJECT_PATH,
                            "org.freedesktop.DBus.Properties", "Set", g_variant_new ("(ssv)", INTERFACE, name, value),
                            NULL, G_DBUS_CALL_FLAGS_NONE, 10000, f->cancel, on_call_done, c);
}

static void
set_prop (DnsFilter *f, const char *name, GVariant *value)
{
    set_prop_full (f, name, value, NULL, NULL);
}

/* ----- Small helpers ----- */

static GtkWidget *
subpage_new (GtkWidget *content, const char *title, const char *tag)
{
    GtkWidget *toolbar = adw_toolbar_view_new ();

    adw_toolbar_view_add_top_bar (ADW_TOOLBAR_VIEW (toolbar), adw_header_bar_new ());
    adw_toolbar_view_set_content (ADW_TOOLBAR_VIEW (toolbar), content);
    return GTK_WIDGET (adw_navigation_page_new_with_tag (toolbar, title, tag));
}

static GtkWidget *
nav_row_new (const char *title, const char *icon, const char *tag)
{
    GtkWidget *row = g_object_new (CC_TYPE_LIST_ROW, "show-arrow", TRUE, "use-underline", TRUE, "action-name",
                                   "navigation.push", NULL);

    if (icon)
        g_object_set (row, "icon-name", icon, NULL);
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), title);
    gtk_actionable_set_action_target (GTK_ACTIONABLE (row), "s", tag);
    return row;
}

static GtkLabel *
value_row_new (AdwPreferencesGroup *group, const char *title)
{
    GtkWidget *row = adw_action_row_new ();
    GtkWidget *label = gtk_label_new ("–");

    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), title);
    gtk_widget_add_css_class (label, "dim-label");
    gtk_widget_add_css_class (label, "numeric");
    adw_action_row_add_suffix (ADW_ACTION_ROW (row), label);
    adw_preferences_group_add (group, row);
    return GTK_LABEL (label);
}

static GtkWidget *
flat_button_new (const char *label, const char *icon, const char *tooltip)
{
    GtkWidget *button = label ? gtk_button_new_with_label (label) : gtk_button_new_from_icon_name (icon);

    gtk_widget_set_valign (button, GTK_ALIGN_CENTER);
    gtk_widget_add_css_class (button, "flat");
    if (tooltip)
        gtk_widget_set_tooltip_text (button, tooltip);
    return button;
}

/* Removes the rows a group got from a refresh */
static void
clear_rows (AdwPreferencesGroup *group, GPtrArray *rows)
{
    for (guint i = 0; i < rows->len; i++)
        adw_preferences_group_remove (group, g_ptr_array_index (rows, i));
    g_ptr_array_set_size (rows, 0);
}

static char *
format_count (guint64 n)
{
    /* Thousands separators of the language ("207 301", "207.301") */
    return g_strdup_printf ("%'" G_GUINT64_FORMAT, n);
}

static char *
format_ms (guint32 us)
{
    if (us == 0)
        return g_strdup ("–");
    if (us < 10000)
        return g_strdup_printf (P_("%.1f ms"), us / 1000.0);
    return g_strdup_printf (P_("%u ms"), (us + 500) / 1000);
}

/* Blocked queries and their share, "683 (14 %)" */
static char *
blocked_text (guint64 blocked, guint64 queries)
{
    g_autofree char *n = format_count (blocked);

    if (!queries)
        return g_steal_pointer (&n);
    /* Translators: blocked queries and their share of all, "683 (14 %)" */
    return g_strdup_printf (P_("%s (%.0f %%)"), n, 100.0 * blocked / queries);
}

static void
set_label_take (GtkLabel *label, char *text)
{
    gtk_label_set_text (label, text);
    g_free (text);
}

static char *
format_time (gint64 unix_s, gboolean with_seconds)
{
    g_autoptr(GDateTime) t = g_date_time_new_from_unix_local (unix_s);
    g_autoptr(GDateTime) now = g_date_time_new_now_local ();

    if (!t)
        return g_strdup ("");
    if (g_date_time_get_year (t) == g_date_time_get_year (now) &&
        g_date_time_get_day_of_year (t) == g_date_time_get_day_of_year (now))
        return g_date_time_format (t, with_seconds ? "%X" : "%R");
    /* Translators: date and time of a query or an update older than today, strftime format */
    return g_date_time_format (t, P_("%e %b, %R"));
}

/* System services by what they do */
static const struct {
    const char *unit, *title;
} friendly_units[] = {
    { "NetworkManager.service", N_("Network") },
    { "geoclue.service", N_("Location") },
    { "plinius-software.service", N_("Apps") },
    { "packagekit.service", N_("Apps") },
    { "flatpak-system-helper.service", N_("Apps") },
};

/* Name and icon of an app key of plinius-dnsfilter (app/<desktop id>, user/<unit>,
 * system/<unit>, android/uid_<N>); NULL for "" */
static char *
app_title (const char *key, GIcon **icon)
{
    const char *unit;
    const char *dot;

    *icon = NULL;
    if (!key || !*key)
        return NULL;
    unit = strchr (key, '/') ? strchr (key, '/') + 1 : key;
    if (g_str_has_prefix (key, "app/")) {
        g_autofree char *desktop = g_strconcat (unit, ".desktop", NULL);
        g_autoptr(GDesktopAppInfo) info = g_desktop_app_info_new (desktop);

        if (info) {
            GIcon *i = g_app_info_get_icon (G_APP_INFO (info));

            *icon = i ? g_object_ref (i) : g_themed_icon_new ("application-x-executable-symbolic");
            return g_strdup (g_app_info_get_display_name (G_APP_INFO (info)));
        }
        *icon = g_themed_icon_new ("application-x-executable-symbolic");
        return g_strdup (unit);
    }
    if (g_str_has_prefix (key, "android/")) {
        *icon = g_themed_icon_new ("phone-symbolic");
        if (g_str_has_prefix (unit, "uid_"))
            /* Translators: an app in the Android container, by its user id */
            return g_strdup_printf (P_("Android app %s"), unit + 4);
        return g_strdup (P_("Android"));
    }
    *icon = g_themed_icon_new ("application-x-executable-symbolic");
    if (!g_strcmp0 (key, "user/session.scope"))
        return g_strdup (P_("Session"));
    if (!g_strcmp0 (key, "kernel"))
        return g_strdup (P_("Linux Kernel"));
    for (guint i = 0; i < G_N_ELEMENTS (friendly_units); i++)
        if (!g_strcmp0 (unit, friendly_units[i].unit))
            return g_strdup (P_(friendly_units[i].title));
    /* A unit by its name without the type */
    dot = strrchr (unit, '.');
    return dot ? g_strndup (unit, dot - unit) : g_strdup (unit);
}

static char *
client_text (const char *kind, const char *name)
{
    if (!g_strcmp0 (kind, "phone"))
        return g_strdup (P_("This phone"));
    if (!g_strcmp0 (kind, "hotspot"))
        /* Translators: a device on the phone's hotspot, its name or address */
        return g_strdup_printf (P_("%s (Hotspot)"), name);
    if (!g_strcmp0 (kind, "usb"))
        /* Translators: a computer connected over USB, its address */
        return g_strdup_printf (P_("%s (USB)"), name);
    return g_strdup (name);
}

static const char *
type_name (guint16 type, char buf[16])
{
    switch (type) {
    case 1: return "A";
    case 2: return "NS";
    case 5: return "CNAME";
    case 6: return "SOA";
    case 12: return "PTR";
    case 15: return "MX";
    case 16: return "TXT";
    case 28: return "AAAA";
    case 33: return "SRV";
    case 64: return "SVCB";
    case 65: return "HTTPS";
    default:
        g_snprintf (buf, 16, "TYPE%u", type);
        return buf;
    }
}

/* What decided about a name, for the row */
static char *
status_text (guint32 status, const char *reason, const char *via)
{
    g_autofree char *base = NULL;

    if ((status == ST_ALLOWED_RULE || status == ST_BLOCKED_RULE) && reason && strstr (reason, APP_MODIFIER))
        return g_strdup (status == ST_ALLOWED_RULE ? P_("Allowed for this app") : P_("Blocked for this app"));
    switch (status) {
    case ST_ALLOWED_RULE: base = g_strdup (P_("Allowed by your rule")); break;
    case ST_ALLOWED_LIST: base = g_strdup_printf (P_("Allowed by %s"), reason); break;
    case ST_ALLOWED_SYSTEM: base = g_strdup (P_("Needed by the phone")); break;
    case ST_BLOCKED_LIST: base = g_strdup_printf (P_("Blocked by %s"), reason); break;
    case ST_BLOCKED_RULE: base = g_strdup (P_("Blocked by your rule")); break;
    case ST_UNFILTERED: base = g_strdup (P_("Not filtered")); break;
    case ST_FAILED: base = g_strdup (P_("No answer")); break;
    case ST_BLOCKED_SPECIAL: base = g_strdup (P_("Blocked: own encrypted DNS of the browser")); break;
    default: return g_strdup ("");
    }
    if (via && *via && status_blocked (status))
        /* Translators: "Blocked by <list>, via <the name it points to>" */
        return g_strdup_printf (P_("%s, via %s"), base, via);
    return g_steal_pointer (&base);
}

/* ----- Block and unblock ----- */

typedef enum {
    ACT_NONE,
    ACT_BLOCK,       /* add ||name^ */
    ACT_UNBLOCK,     /* add @@||name^ */
    ACT_REMOVE_RULE, /* remove the own rule that decided */
} Action;

static Action
action_for (guint32 status)
{
    switch (status) {
    case ST_PASSED:
    case ST_ALLOWED_LIST:
    case ST_FAILED:
        return ACT_BLOCK;
    case ST_BLOCKED_LIST:
        return ACT_UNBLOCK;
    case ST_BLOCKED_RULE:
    case ST_ALLOWED_RULE:
        return ACT_REMOVE_RULE;
    default:
        return ACT_NONE;
    }
}

static const char *
action_label (const NameRow *nr)
{
    guint32 status = nr->status;

    /* For one app: always possible, a rule for the app beats all others */
    if (nr->app)
        return status_blocked (status) ? P_("Unblock for App") : P_("Block for App");
    switch (action_for (status)) {
    case ACT_BLOCK: return P_("Block Globally");
    case ACT_UNBLOCK: return P_("Unblock");
    case ACT_REMOVE_RULE: return status_blocked (status) ? P_("Unblock") : P_("Remove Exception");
    default: return NULL;
    }
}

static void
name_row_free (NameRow *nr)
{
    g_free (nr->name);
    g_free (nr->app);
    g_free (nr->detail);
    g_free (nr->reason);
    g_free (nr->via);
    g_free (nr);
}

/* Every row of @name gets the new status */
static void
on_checked (DnsFilter *f, GVariant *result, GError *error, gpointer data)
{
    g_autofree char *name = data;
    guint32 status;
    const char *reason;
    /* The query log keeps what happened; its rows change with the next query */
    GPtrArray *lists[] = { f->stats_names, f->detail_names };

    if (!result)
        return;
    g_variant_get (result, "(u&s)", &status, &reason);
    for (guint l = 0; l < G_N_ELEMENTS (lists); l++) {
        for (guint i = 0; i < lists[l]->len; i++) {
            NameRow *nr = g_ptr_array_index (lists[l], i);

            /* Check is about all apps */
            if (g_strcmp0 (nr->name, name) || nr->app)
                continue;
            nr->status = status;
            g_free (nr->reason);
            nr->reason = g_strdup (reason);
            g_clear_pointer (&nr->via, g_free);
            name_row_update (nr);
        }
    }
}

static void
check_name (DnsFilter *f, const char *name)
{
    call (f, "Check", g_variant_new ("(s)", name), on_checked, g_strdup (name));
}

/* After a new or removed rule: what the name's rows, its apps and an app's page show */
static void
on_rule_changed (DnsFilter *f, GVariant *result, GError *error, gpointer data)
{
    g_autofree char *name = data;

    check_name (f, name);
    detail_check_groups (f);
    if (f->who_shown)
        load_who (f);
}

/* A rule for @app only: it beats the lists and the rules for all apps */
static void
add_app_rule (DnsFilter *f, const char *name, const char *app, gboolean block)
{
    g_autofree char *rule = g_strdup_printf ("%s||%s^" APP_MODIFIER "%s", block ? "" : "@@", name, app);

    call (f, "AddRule", g_variant_new ("(s)", rule), on_rule_changed, g_strdup (name));
}

/* Unblock in green and Block in red, both as text */
static void
action_button_style (GtkWidget *button, gboolean unblock)
{
    gtk_widget_add_css_class (button, "flat");
    gtk_widget_remove_css_class (button, "suggested-action");
    if (unblock) {
        gtk_widget_remove_css_class (button, "destructive-action");
        gtk_widget_add_css_class (button, "plinius-dns-unblock");
    } else {
        gtk_widget_remove_css_class (button, "plinius-dns-unblock");
        gtk_widget_add_css_class (button, "destructive-action");
    }
}

static void
on_name_action (GtkButton *button, NameRow *nr)
{
    DnsFilter *f = nr->f;
    g_autofree char *rule = NULL;

    if (nr->app) {
        add_app_rule (f, nr->name, nr->app, !status_blocked (nr->status));
        gtk_widget_set_sensitive (GTK_WIDGET (button), FALSE);
        return;
    }
    switch (action_for (nr->status)) {
    case ACT_BLOCK:
        rule = g_strdup_printf ("||%s^", nr->name);
        call (f, "AddRule", g_variant_new ("(s)", rule), on_rule_changed, g_strdup (nr->name));
        break;
    case ACT_UNBLOCK:
        rule = g_strdup_printf ("@@||%s^", nr->name);
        call (f, "AddRule", g_variant_new ("(s)", rule), on_rule_changed, g_strdup (nr->name));
        break;
    case ACT_REMOVE_RULE:
        call (f, "RemoveRule", g_variant_new ("(s)", nr->reason), on_rule_changed, g_strdup (nr->name));
        break;
    default:
        return;
    }
    gtk_widget_set_sensitive (GTK_WIDGET (button), FALSE);
}

static void
name_row_update (NameRow *nr)
{
    const char *label = action_label (nr);
    g_autofree char *status = status_text (nr->status, nr->reason, nr->via);

    if (status_blocked (nr->status))
        gtk_widget_add_css_class (nr->row, "plinius-dns-blocked");
    else
        gtk_widget_remove_css_class (nr->row, "plinius-dns-blocked");
    if (nr->detail) {
        g_autofree char *subtitle = *status ? g_strdup_printf ("%s\n%s", nr->detail, status) : g_strdup (nr->detail);

        adw_action_row_set_subtitle (ADW_ACTION_ROW (nr->row), subtitle);
    } else {
        adw_action_row_set_subtitle (ADW_ACTION_ROW (nr->row), status);
    }
    if (!nr->button)
        return;
    gtk_widget_set_visible (nr->button, label != NULL);
    gtk_widget_set_sensitive (nr->button, TRUE);
    if (label)
        gtk_button_set_label (GTK_BUTTON (nr->button), label);
    /* Block: red text; Unblock: green text */
    action_button_style (nr->button, status_blocked (nr->status));
}

static NameRow *
name_row_new (DnsFilter *f, GtkWidget *row, const char *name, guint32 status, const char *reason, const char *via,
              gboolean with_button)
{
    NameRow *nr = g_new0 (NameRow, 1);

    nr->f = f;
    nr->row = row;
    nr->name = g_strdup (name);
    nr->status = status;
    nr->reason = g_strdup (reason);
    nr->via = g_strdup (via);
    if (!with_button)
        return nr;
    nr->button = flat_button_new (P_("Block Globally"), NULL, NULL);
    g_signal_connect (nr->button, "clicked", G_CALLBACK (on_name_action), nr);
    adw_action_row_add_suffix (ADW_ACTION_ROW (row), nr->button);
    return nr;
}

/* ----- Chart: queries per hour, the blocked part in the accent color ----- */

static void
draw_chart (GtkDrawingArea *area, cairo_t *cr, int width, int height, gpointer user_data)
{
    GArray **data = user_data;
    GArray *total = data[0], *blocked = data[1];
    AdwStyleManager *sm = adw_style_manager_get_default ();
    g_autoptr(GdkRGBA) accent = adw_style_manager_get_accent_color_rgba (sm);
    GdkRGBA fg;
    guint32 max = 1;
    double bar;

    if (!total->len)
        return;
    gtk_widget_get_color (GTK_WIDGET (area), &fg);
    for (guint i = 0; i < total->len; i++)
        max = MAX (max, g_array_index (total, guint32, i));
    bar = (double) width / total->len;
    for (guint i = 0; i < total->len; i++) {
        double x = i * bar + 1, w = MAX (bar - 2, 1);
        double ht = height * (double) g_array_index (total, guint32, i) / max;
        double hb = height * (double) g_array_index (blocked, guint32, i) / max;

        cairo_set_source_rgba (cr, fg.red, fg.green, fg.blue, 0.18);
        cairo_rectangle (cr, x, height - ht, w, ht);
        cairo_fill (cr);
        cairo_set_source_rgba (cr, accent->red, accent->green, accent->blue, 1.0);
        cairo_rectangle (cr, x, height - hb, w, hb);
        cairo_fill (cr);
    }
}

/* A row of its own, so the chart is the first thing in the group's card (other widgets would go
 * after the rows) */
static GtkWidget *
chart_row_new (GtkWidget *area)
{
    GtkWidget *row = gtk_list_box_row_new ();

    gtk_list_box_row_set_activatable (GTK_LIST_BOX_ROW (row), FALSE);
    gtk_list_box_row_set_selectable (GTK_LIST_BOX_ROW (row), FALSE);
    gtk_list_box_row_set_child (GTK_LIST_BOX_ROW (row), area);
    return row;
}

static GtkWidget *
chart_new (GArray **data)
{
    GtkWidget *area = gtk_drawing_area_new ();

    gtk_drawing_area_set_content_height (GTK_DRAWING_AREA (area), 72);
    gtk_widget_set_hexpand (area, TRUE);
    gtk_widget_set_margin_top (area, 12);
    gtk_widget_set_margin_bottom (area, 12);
    gtk_widget_set_margin_start (area, 12);
    gtk_widget_set_margin_end (area, 12);
    gtk_drawing_area_set_draw_func (GTK_DRAWING_AREA (area), draw_chart, data, NULL);
    gtk_accessible_update_property (GTK_ACCESSIBLE (area), GTK_ACCESSIBLE_PROPERTY_LABEL,
                                    P_("Queries per hour, the blocked part highlighted"), -1);
    return area;
}

/* Fills the bars from the stats' series (one entry per hour with queries), @hours long */
static void
chart_fill (GArray *total, GArray *blocked, GVariant *series, guint hours)
{
    gint64 now_hour = g_get_real_time () / G_USEC_PER_SEC / 3600;
    guint buckets = hours > 72 ? hours / 24 : hours;
    guint per = hours / buckets;
    GVariantIter it;
    gint64 start;
    guint32 t, b, c, avg;

    g_array_set_size (total, 0);
    g_array_set_size (blocked, 0);
    g_array_set_size (total, buckets);
    g_array_set_size (blocked, buckets);
    memset (total->data, 0, buckets * sizeof (guint32));
    memset (blocked->data, 0, buckets * sizeof (guint32));
    if (!series)
        return;
    g_variant_iter_init (&it, series);
    while (g_variant_iter_next (&it, "(xuuuu)", &start, &t, &b, &c, &avg)) {
        gint64 age = now_hour - start / 3600;
        gint64 idx;

        if (age < 0 || age >= hours)
            continue;
        idx = buckets - 1 - age / per;
        g_array_index (total, guint32, idx) += t;
        g_array_index (blocked, guint32, idx) += b;
    }
}

/* ----- Main page ----- */

static void
update_status (DnsFilter *f)
{
    g_autofree char *status = prop_string (f, "Status");
    g_autoptr(GVariant) paused = prop (f, "PausedUntil");
    gint64 until = paused && g_variant_is_of_type (paused, G_VARIANT_TYPE_INT64) ? g_variant_get_int64 (paused) : 0;
    gboolean enabled = prop_bool (f, "Enabled");
    const char *text;

    if (!g_strcmp0 (status, "active"))
        text = P_("On");
    else if (!g_strcmp0 (status, "paused"))
        text = P_("Paused");
    else if (!g_strcmp0 (status, "no-network"))
        text = P_("No Network");
    else if (!g_strcmp0 (status, "error"))
        text = P_("Not Working");
    else
        text = P_("Off");
    if (f->network_row)
        cc_list_row_set_secondary_label (CC_LIST_ROW (f->network_row), text);

    f->syncing = TRUE;
    adw_switch_row_set_active (f->enable_row, enabled);
    f->syncing = FALSE;
    gtk_widget_set_visible (GTK_WIDGET (f->pause_row), enabled);
    gtk_widget_set_visible (f->resume_button, until > 0);
    gtk_widget_set_visible (f->pause_button, until == 0);
    if (until > 0) {
        g_autofree char *when = format_time (until, FALSE);
        g_autofree char *sub = g_strdup_printf (P_("Paused until %s"), when);

        adw_action_row_set_subtitle (f->pause_row, sub);
    } else if (!g_strcmp0 (status, "error")) {
        adw_action_row_set_subtitle (f->pause_row, P_("The filter could not start, the phone uses the network's DNS"));
    } else {
        adw_action_row_set_subtitle (f->pause_row, P_("Lets everything through for a while"));
    }
}

static void
update_nav_subtitles (DnsFilter *f)
{
    g_autoptr(GVariant) lists = prop (f, "Lists");
    guint active = 0;
    guint64 rules = 0;
    g_autofree char *count = NULL;
    g_autofree char *text = NULL;

    if (lists && g_variant_is_of_type (lists, G_VARIANT_TYPE ("aa{sv}"))) {
        GVariantIter it;
        GVariant *d;

        g_variant_iter_init (&it, lists);
        while ((d = g_variant_iter_next_value (&it))) {
            gboolean on = FALSE;
            guint32 n = 0;

            g_variant_lookup (d, "enabled", "b", &on);
            g_variant_lookup (d, "rules", "u", &n);
            if (on) {
                active++;
                rules += n;
            }
            g_variant_unref (d);
        }
    }
    count = format_count (rules);
    text = g_strdup_printf (g_dngettext (PLINIUS_DOMAIN, "%u list, %s rules", "%u lists, %s rules", active), active,
                            count);
    adw_action_row_set_subtitle (f->lists_nav, text);
    g_free (text);
    text = g_strdup_printf (g_dngettext (PLINIUS_DOMAIN, "%u rule", "%u rules", prop_uint (f, "RuleCount")),
                            prop_uint (f, "RuleCount"));
    adw_action_row_set_subtitle (f->rules_nav, text);
    adw_action_row_set_subtitle (f->servers_nav, prop_bool (f, "CustomServers") ? P_("Your servers")
                                                                                 : P_("From the network"));
}

static void
on_enable (DnsFilter *f)
{
    if (!f->syncing)
        set_prop (f, "Enabled", g_variant_new_boolean (adw_switch_row_get_active (f->enable_row)));
}

static void
on_pause_action (GSimpleAction *action, GVariant *param, gpointer user_data)
{
    DnsFilter *f = user_data;

    call (f, "Pause", g_variant_new ("(u)", g_variant_get_uint32 (param)), NULL, NULL);
}

static void
on_resume (DnsFilter *f)
{
    call (f, "Pause", g_variant_new ("(u)", 0), NULL, NULL);
}

static void
on_main_stats (DnsFilter *f, GVariant *result, GError *error, gpointer data)
{
    g_autoptr(GVariant) stats = NULL;
    g_autoptr(GVariant) series = NULL;
    guint64 queries = 0, blocked = 0;
    guint32 average = 0;
    g_autofree char *q = NULL;
    g_autofree char *b = NULL;
    g_autofree char *a = NULL;

    if (!result)
        return;
    g_variant_get (result, "(@a{sv})", &stats);
    g_variant_lookup (stats, "queries", "t", &queries);
    g_variant_lookup (stats, "blocked", "t", &blocked);
    g_variant_lookup (stats, "average-us", "u", &average);
    series = g_variant_lookup_value (stats, "series", G_VARIANT_TYPE ("a(xuuuu)"));
    chart_fill (f->chart_total, f->chart_blocked, series, 24);
    gtk_widget_queue_draw (f->chart);
    q = format_count (queries);
    gtk_label_set_text (f->queries_value, q);
    b = blocked_text (blocked, queries);
    gtk_label_set_text (f->blocked_value, b);
    a = format_ms (average);
    gtk_label_set_text (f->average_value, a);
}

static void
refresh_main (DnsFilter *f)
{
    update_status (f);
    update_nav_subtitles (f);
    call (f, "GetStats", g_variant_new ("(u)", 1), on_main_stats, NULL);
}

static GtkWidget *
main_page_new (DnsFilter *f)
{
    AdwPreferencesPage *page = ADW_PREFERENCES_PAGE (adw_preferences_page_new ());
    AdwPreferencesGroup *group;
    GtkWidget *row, *menu_button;
    g_autoptr(GMenu) menu = g_menu_new ();
    g_autoptr(GSimpleActionGroup) actions = g_simple_action_group_new ();
    g_autoptr(GSimpleAction) pause = g_simple_action_new ("pause", G_VARIANT_TYPE_UINT32);
    GtkWidget *navpage;

    group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    f->enable_row = ADW_SWITCH_ROW (adw_switch_row_new ());
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (f->enable_row), P_("_Block Ads and Trackers"));
    adw_preferences_row_set_use_underline (ADW_PREFERENCES_ROW (f->enable_row), TRUE);
    adw_action_row_set_subtitle (ADW_ACTION_ROW (f->enable_row),
                                 P_("For every connection, the hotspot and USB devices included"));
    g_signal_connect_swapped (f->enable_row, "notify::active", G_CALLBACK (on_enable), f);
    adw_preferences_group_add (group, GTK_WIDGET (f->enable_row));

    f->pause_row = ADW_ACTION_ROW (adw_action_row_new ());
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (f->pause_row), P_("Pause"));
    g_menu_append (menu, P_("For 1 Minute"), "dnsfilter.pause(uint32 60)");
    g_menu_append (menu, P_("For 10 Minutes"), "dnsfilter.pause(uint32 600)");
    g_menu_append (menu, P_("For 1 Hour"), "dnsfilter.pause(uint32 3600)");
    g_menu_append (menu, P_("For 8 Hours"), "dnsfilter.pause(uint32 28800)");
    menu_button = gtk_menu_button_new ();
    /* Text, no icon */
    gtk_menu_button_set_label (GTK_MENU_BUTTON (menu_button), P_("Off"));
    gtk_menu_button_set_menu_model (GTK_MENU_BUTTON (menu_button), G_MENU_MODEL (menu));
    gtk_widget_set_valign (menu_button, GTK_ALIGN_CENTER);
    gtk_widget_add_css_class (menu_button, "flat");
    f->pause_button = menu_button;
    adw_action_row_add_suffix (f->pause_row, menu_button);
    f->resume_button = flat_button_new (P_("Resume"), NULL, NULL);
    gtk_widget_add_css_class (f->resume_button, "suggested-action");
    gtk_widget_add_css_class (f->resume_button, "pill");
    g_signal_connect_swapped (f->resume_button, "clicked", G_CALLBACK (on_resume), f);
    adw_action_row_add_suffix (f->pause_row, f->resume_button);
    adw_preferences_group_add (group, GTK_WIDGET (f->pause_row));
    adw_preferences_page_add (page, group);
    g_signal_connect (pause, "activate", G_CALLBACK (on_pause_action), f);
    g_action_map_add_action (G_ACTION_MAP (actions), G_ACTION (pause));

    group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_group_set_title (group, P_("Last 24 Hours"));
    f->chart_total = g_array_new (FALSE, TRUE, sizeof (guint32));
    f->chart_blocked = g_array_new (FALSE, TRUE, sizeof (guint32));
    f->chart_data[0] = f->chart_total;
    f->chart_data[1] = f->chart_blocked;
    f->chart = chart_new (f->chart_data);
    adw_preferences_group_add (group, chart_row_new (f->chart));
    f->queries_value = value_row_new (group, P_("Queries"));
    f->blocked_value = value_row_new (group, P_("Blocked"));
    f->average_value = value_row_new (group, P_("Average Response Time"));
    adw_preferences_page_add (page, group);

    group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    row = nav_row_new (P_("_Query Log"), "view-list-symbolic", TAG_LOG);
    adw_action_row_set_subtitle (ADW_ACTION_ROW (row), P_("Every name asked; tap one to block or unblock it"));
    adw_preferences_group_add (group, row);
    row = nav_row_new (P_("_Statistics"), "power-profile-balanced-symbolic", TAG_STATS);
    adw_preferences_group_add (group, row);
    row = nav_row_new (P_("Block_lists"), "view-paged-symbolic", TAG_LISTS);
    f->lists_nav = ADW_ACTION_ROW (row);
    adw_preferences_group_add (group, row);
    row = nav_row_new (P_("_Own Rules"), "document-edit-symbolic", TAG_RULES);
    f->rules_nav = ADW_ACTION_ROW (row);
    adw_preferences_group_add (group, row);
    row = nav_row_new (P_("_DNS Servers"), "network-server-symbolic", TAG_SERVERS);
    f->servers_nav = ADW_ACTION_ROW (row);
    adw_preferences_group_add (group, row);
    row = nav_row_new (P_("S_ettings"), "emblem-system-symbolic", TAG_SETTINGS);
    adw_preferences_group_add (group, row);
    adw_preferences_page_add (page, group);

    navpage = subpage_new (GTK_WIDGET (page), P_("DNS Filter"), TAG_MAIN);
    gtk_widget_insert_action_group (navpage, "dnsfilter", G_ACTION_GROUP (actions));
    return navpage;
}

/* ----- Query log: each name once, the name asked last on top ----- */

static guint
log_filter_value (DnsFilter *f)
{
    const char *name = adw_toggle_group_get_active_name (f->log_filter);

    if (!g_strcmp0 (name, "blocked"))
        return 1;
    if (!g_strcmp0 (name, "allowed"))
        return 2;
    return 0;
}

/* A list's placeholder: a title and, while the log is only in memory, why it may be short */
static void
placeholder_set (GtkListBox *list, const char *text, GtkWidget **title, GtkWidget **hint)
{
    GtkWidget *box = gtk_box_new (GTK_ORIENTATION_VERTICAL, 6);

    *title = gtk_label_new (text);
    gtk_label_set_wrap (GTK_LABEL (*title), TRUE);
    gtk_label_set_justify (GTK_LABEL (*title), GTK_JUSTIFY_CENTER);
    gtk_box_append (GTK_BOX (box), *title);
    *hint = gtk_label_new (P_("The log starts anew with each restart; “Keep after Restart” in Settings keeps it"));
    gtk_label_set_wrap (GTK_LABEL (*hint), TRUE);
    gtk_label_set_justify (GTK_LABEL (*hint), GTK_JUSTIFY_CENTER);
    gtk_widget_add_css_class (*hint, "caption");
    gtk_box_append (GTK_BOX (box), *hint);
    gtk_widget_add_css_class (box, "dim-label");
    gtk_widget_add_css_class (box, "cc-placeholder-row");
    gtk_list_box_set_placeholder (list, box);
}

/* Not gtk_list_box_remove_all (): that takes the placeholder too, and an empty list then shows nothing */
static void
list_box_clear (GtkListBox *list)
{
    GtkListBoxRow *row;

    while ((row = gtk_list_box_get_row_at_index (list, 0)))
        gtk_list_box_remove (list, GTK_WIDGET (row));
}

static void
update_placeholders (DnsFilter *f)
{
    gboolean keep = prop_bool (f, "KeepLog");

    if (f->log_empty) {
        gboolean narrowed = *gtk_editable_get_text (GTK_EDITABLE (f->log_search)) || log_filter_value (f);

        gtk_label_set_text (GTK_LABEL (f->log_empty), narrowed ? P_("No matching names") : P_("No queries yet"));
        gtk_widget_set_visible (f->log_empty_hint, !keep);
    }
    if (f->detail_empty_hint)
        gtk_widget_set_visible (f->detail_empty_hint, !keep);
}

/* One entry of the log, its strings borrowed from the variant */
typedef struct {
    guint32 id, status, duration;
    gint64 time_us;
    const char *name, *kind, *client, *reason, *via, *answer, *upstream, *app;
    guint16 type, rcode;
    gboolean cached;
} LogItem;

static void
log_item_get (GVariant *entry, LogItem *it)
{
    g_variant_get (entry, LOG_TYPE_BORROWED, &it->id, &it->time_us, &it->name, &it->type, &it->kind, &it->client,
                   &it->status, &it->reason, &it->via, &it->answer, &it->upstream, &it->duration, &it->rcode,
                   &it->cached, &it->app);
}

/* The app that asked, else the device (hotspot, USB) or "This phone" */
static char *
log_item_who (const LogItem *it)
{
    g_autoptr(GIcon) icon = NULL;
    char *who = app_title (it->app, &icon);

    return who ? who : client_text (it->kind, it->client);
}

/* The answer, the server and the time it took, for a long press or hover */
static char *
log_item_tooltip (const LogItem *it)
{
    g_autofree char *dur = format_ms (it->duration);

    return g_strdup_printf ("%s%s\n%s%s\n%s%s", P_("Answer: "), *it->answer ? it->answer : "–", P_("Server: "),
                            it->cached ? P_("from the cache") : (*it->upstream ? it->upstream : "–"),
                            P_("Response time: "), dur);
}

/* A name's row gets its newest query and the counts: blocked, partly blocked or what decided */
static void
log_name_row_set (NameRow *nr, const LogItem *it, guint32 count, guint32 blocked)
{
    g_autofree char *when = format_time (it->time_us / G_USEC_PER_SEC, TRUE);
    g_autofree char *who = log_item_who (it);
    g_autofree char *n = g_strdup_printf (g_dngettext (PLINIUS_DOMAIN, "%u query", "%u queries", count), count);
    g_autofree char *tooltip = log_item_tooltip (it);
    g_autofree char *status = NULL;
    g_autofree char *subtitle = NULL;

    nr->id = it->id;
    nr->count = count;
    nr->blocked = blocked;
    nr->status = it->status;
    g_free (nr->reason);
    nr->reason = g_strdup (it->reason);
    g_free (nr->via);
    nr->via = g_strdup (it->via);
    if (blocked && blocked < count)
        status = g_strdup (P_("Partially blocked"));
    else if (blocked && !status_blocked (it->status))
        status = g_strdup (P_("Blocked"));
    else
        status = status_text (it->status, it->reason, it->via);
    g_free (nr->detail);
    /* Translators: a name in the query log: time of its last query · who asked · how often */
    nr->detail = g_strdup_printf (P_("%s · %s · %s"), when, who, n);
    subtitle = *status ? g_strdup_printf ("%s\n%s", nr->detail, status) : g_strdup (nr->detail);
    adw_action_row_set_subtitle (ADW_ACTION_ROW (nr->row), subtitle);
    gtk_widget_set_tooltip_text (nr->row, tooltip);
    if (blocked && blocked == count)
        gtk_widget_add_css_class (nr->row, "plinius-dns-blocked");
    else
        gtk_widget_remove_css_class (nr->row, "plinius-dns-blocked");
}

static void
on_log_name_activated (AdwActionRow *row, NameRow *nr)
{
    DnsFilter *f = nr->f;

    g_free (f->detail_name);
    f->detail_name = g_strdup (nr->name);
    f->detail_days = 0;
    /* The list keeps its place while the name's page is on top */
    f->log_keep = TRUE;
    gtk_widget_activate_action (GTK_WIDGET (row), "navigation.push", "s", TAG_NAME);
}

/* A name in the list; Block and Unblock are on its page */
static NameRow *
log_name_row_new (DnsFilter *f, const LogItem *it, guint32 count, guint32 blocked, gboolean prepend)
{
    GtkWidget *row = adw_action_row_new ();
    NameRow *nr = g_new0 (NameRow, 1);

    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), it->name);
    adw_preferences_row_set_use_markup (ADW_PREFERENCES_ROW (row), FALSE);
    adw_action_row_set_title_lines (ADW_ACTION_ROW (row), 1);
    adw_action_row_set_subtitle_lines (ADW_ACTION_ROW (row), 3);
    gtk_list_box_row_set_activatable (GTK_LIST_BOX_ROW (row), TRUE);
    nr->f = f;
    nr->row = row;
    nr->name = g_strdup (it->name);
    adw_action_row_add_suffix (ADW_ACTION_ROW (row), gtk_image_new_from_icon_name ("go-next-symbolic"));
    g_signal_connect (row, "activated", G_CALLBACK (on_log_name_activated), nr);
    g_object_set_data_full (G_OBJECT (row), "name-row", nr, (GDestroyNotify) name_row_free);
    log_name_row_set (nr, it, count, blocked);
    g_hash_table_insert (f->log_names, nr->name, nr);
    if (prepend) {
        g_ptr_array_insert (f->log_rows, 0, nr);
        gtk_list_box_prepend (f->log_list, row);
    } else {
        g_ptr_array_add (f->log_rows, nr);
        gtk_list_box_append (f->log_list, row);
    }
    return nr;
}

/* A new query of the shown list: its name goes on top */
static void
log_name_seen (DnsFilter *f, const LogItem *it)
{
    NameRow *nr = g_hash_table_lookup (f->log_names, it->name);

    if (!nr) {
        log_name_row_new (f, it, 1, status_blocked (it->status) ? 1 : 0, TRUE);
        while (f->log_rows->len > LOG_MAX_ROWS) {
            NameRow *o = g_ptr_array_steal_index (f->log_rows, f->log_rows->len - 1);

            /* The next page starts behind the rows kept, so older names come again */
            g_hash_table_remove (f->log_names, o->name);
            gtk_list_box_remove (f->log_list, o->row);
        }
        return;
    }
    log_name_row_set (nr, it, nr->count + 1, nr->blocked + (status_blocked (it->status) ? 1 : 0));
    if (g_ptr_array_index (f->log_rows, 0) != nr) {
        g_object_ref (nr->row);
        g_ptr_array_remove (f->log_rows, nr);
        gtk_list_box_remove (f->log_list, nr->row);
        g_ptr_array_insert (f->log_rows, 0, nr);
        gtk_list_box_prepend (f->log_list, nr->row);
        g_object_unref (nr->row);
    }
}

static void
on_log_names (DnsFilter *f, GVariant *result, GError *error, gpointer data)
{
    g_autoptr(GVariant) names = NULL;
    GVariantIter it;
    GVariant *e;
    guint n = 0;

    if (!result || GPOINTER_TO_UINT (data) != f->log_generation)
        return;
    g_variant_get (result, "(@a(" LOG_TYPE "uu))", &names);
    g_variant_iter_init (&it, names);
    while ((e = g_variant_iter_next_value (&it))) {
        g_autoptr(GVariant) entry = g_variant_get_child_value (e, 0);
        guint32 count, blocked;
        LogItem item;

        g_variant_get_child (e, 1, "u", &count);
        g_variant_get_child (e, 2, "u", &blocked);
        log_item_get (entry, &item);
        /* A name that came live meanwhile is already there */
        if (!g_hash_table_contains (f->log_names, item.name))
            log_name_row_new (f, &item, count, blocked, FALSE);
        g_variant_unref (e);
        n++;
    }
    gtk_widget_set_visible (f->log_more, n == LOG_PAGE);
}

static void
clear_log_rows (DnsFilter *f)
{
    f->log_generation++;
    g_hash_table_remove_all (f->log_names);
    g_ptr_array_set_size (f->log_rows, 0);
    list_box_clear (f->log_list);
}

static void
get_log_names (DnsFilter *f)
{
    call (f, "GetLogNames",
          g_variant_new ("(uusu)", f->log_rows->len, LOG_PAGE, gtk_editable_get_text (GTK_EDITABLE (f->log_search)),
                         log_filter_value (f)),
          on_log_names, GUINT_TO_POINTER (f->log_generation));
}

static void
reload_log (DnsFilter *f)
{
    clear_log_rows (f);
    update_placeholders (f);
    get_log_names (f);
}

/* ----- One name's queries by app, each app's newest first ----- */

/* The queries of one app (or device) on a name's page: an expander row. An app's starts with
 * Block for App or Unblock for App. */
typedef struct {
    DnsFilter *f;
    GtkWidget *row;
    GtkListBox *list; /* the expander's own list, sorted newest first */
    GtkWidget *action_row, *action_button; /* apps only */
    char *key; /* app key, or "\t<kind>\t<client>" of a device */
    guint rows, blocked;
    guint stats_count; /* queries in the statistics, for an app or device the log no longer has */
    guint32 newest_id;
    gint64 newest_us;
    gboolean checked; /* status and reason are what the filter does for this app now */
    guint32 status;
    char *reason;
} DetailGroup;

static void
detail_group_free (DetailGroup *g)
{
    g_free (g->key);
    g_free (g->reason);
    g_free (g);
}

static gboolean
key_is_app (const char *key)
{
    return key && *key && key[0] != '\t';
}

/* The title of who asked: an app, else a device ("\t<kind>\t<client>") */
static char *
who_title (const char *key)
{
    g_autoptr(GIcon) icon = NULL;

    if (key_is_app (key)) {
        char *title = app_title (key, &icon);

        return title ? title : g_strdup (key);
    } else {
        g_auto(GStrv) parts = g_strsplit (key + 1, "\t", 2);

        return client_text (parts[0], parts[0] && parts[1] ? parts[1] : "");
    }
}

/* Newer queries have higher ids (plinius-dnsfilter counts them) */
static int
detail_query_sort (GtkListBoxRow *a, GtkListBoxRow *b, gpointer user_data)
{
    guint ia = GPOINTER_TO_UINT (g_object_get_data (G_OBJECT (a), "query-id"));
    guint ib = GPOINTER_TO_UINT (g_object_get_data (G_OBJECT (b), "query-id"));

    return ia > ib ? -1 : ia < ib;
}

/* The app that asked last on top */
static int
detail_group_sort (GtkListBoxRow *a, GtkListBoxRow *b, gpointer user_data)
{
    DetailGroup *ga = g_object_get_data (G_OBJECT (a), "detail-group");
    DetailGroup *gb = g_object_get_data (G_OBJECT (b), "detail-group");

    return ga->newest_id > gb->newest_id ? -1 : ga->newest_id < gb->newest_id;
}

/* "12:04:31 · 7 queries", below it whether they were blocked */
static void
detail_group_update (DetailGroup *g)
{
    g_autofree char *when = format_time (g->newest_us / G_USEC_PER_SEC, TRUE);
    g_autofree char *n = g_strdup_printf (g_dngettext (PLINIUS_DOMAIN, "%u query", "%u queries", g->rows), g->rows);
    g_autofree char *subtitle = NULL;
    const char *status = NULL;

    g_autofree char *now = NULL;
    gboolean red = g->blocked && g->blocked == g->rows;

    if (g->blocked && g->blocked < g->rows)
        status = P_("Partially blocked");
    else if (g->blocked)
        status = P_("Blocked");
    /* An app: what the filter does for it now, red when it is blocked */
    if (g->checked) {
        now = status_text (g->status, g->reason, NULL);
        if (*now || !status_blocked (g->status))
            status = *now ? now : NULL;
        red = status_blocked (g->status);
        gtk_button_set_label (GTK_BUTTON (g->action_button),
                              red ? P_("Unblock for App") : P_("Block for App"));
        action_button_style (g->action_button, red);
        gtk_widget_set_sensitive (g->action_button, TRUE);
    }
    if (g->rows) {
        /* Translators: an app on a name's page: time of its last query · how often it asked */
        subtitle = g_strdup_printf (P_("%s · %s"), when, n);
    } else {
        /* Only in the statistics: the log no longer has its queries */
        subtitle = g_strdup_printf (g_dngettext (PLINIUS_DOMAIN, "%u query", "%u queries", g->stats_count),
                                    g->stats_count);
    }
    if (status) {
        g_autofree char *line = g_strdup_printf ("%s\n%s", subtitle, status);

        adw_expander_row_set_subtitle (ADW_EXPANDER_ROW (g->row), line);
    } else {
        adw_expander_row_set_subtitle (ADW_EXPANDER_ROW (g->row), subtitle);
    }
    if (red)
        gtk_widget_add_css_class (g->row, "plinius-dns-blocked");
    else
        gtk_widget_remove_css_class (g->row, "plinius-dns-blocked");
}

/* A row goes into the group's expander; the expander's own list box gets sorted, so live and
 * older queries both fall into place and Block for App stays on top */
static void
detail_group_add_row (DetailGroup *g, GtkWidget *row)
{
    adw_expander_row_add_row (ADW_EXPANDER_ROW (g->row), row);
    if (!g->list && GTK_IS_LIST_BOX (gtk_widget_get_parent (row))) {
        g->list = GTK_LIST_BOX (gtk_widget_get_parent (row));
        gtk_list_box_set_sort_func (g->list, detail_query_sort, NULL, NULL);
    }
}

typedef struct {
    char *key;
    guint generation;
} GroupCheck;

static void
on_group_checked (DnsFilter *f, GVariant *result, GError *error, gpointer data)
{
    GroupCheck *c = data;
    DetailGroup *g = c->generation == f->detail_generation ? g_hash_table_lookup (f->detail_groups, c->key) : NULL;
    g_autoptr(GVariant) results = NULL;
    const char *reason;

    if (g && result) {
        g_variant_get (result, "(@a(us))", &results);
        if (g_variant_n_children (results)) {
            g_variant_get_child (results, 0, "(u&s)", &g->status, &reason);
            g_free (g->reason);
            g->reason = g_strdup (reason);
            g->checked = TRUE;
            detail_group_update (g);
        }
    }
    g_free (c->key);
    g_free (c);
}

/* What the filter does with the name for this app now (CheckNames, plinius-dnsfilter 0.1-8) */
static void
detail_check_group (DnsFilter *f, DetailGroup *g)
{
    const char *names[] = { f->detail_name, NULL };
    GroupCheck *c;

    if (!g->action_row || !f->detail_name)
        return;
    c = g_new0 (GroupCheck, 1);
    c->key = g_strdup (g->key);
    c->generation = f->detail_generation;
    call (f, "CheckNames", g_variant_new ("(s^as)", g->key, names), on_group_checked, c);
}

static void
detail_check_groups (DnsFilter *f)
{
    GHashTableIter iter;
    DetailGroup *g;

    if (!f->detail_shown)
        return;
    g_hash_table_iter_init (&iter, f->detail_groups);
    while (g_hash_table_iter_next (&iter, NULL, (gpointer *) &g))
        detail_check_group (f, g);
}

static void
on_group_action (GtkButton *button, DetailGroup *g)
{
    if (!g->checked || !g->f->detail_name)
        return;
    gtk_widget_set_sensitive (GTK_WIDGET (button), FALSE);
    add_app_rule (g->f, g->f->detail_name, g->key, !status_blocked (g->status));
}

/* Block for App or Unblock for App, the first row of an app's expander, at its end */
static void
detail_group_add_action (DetailGroup *g)
{
    GtkWidget *row = gtk_list_box_row_new ();
    GtkWidget *box = gtk_box_new (GTK_ORIENTATION_HORIZONTAL, 0);

    g->action_button = gtk_button_new_with_label (P_("Block for App"));
    action_button_style (g->action_button, FALSE);
    /* Until the filter said what it does for the app */
    gtk_widget_set_sensitive (g->action_button, FALSE);
    g_signal_connect (g->action_button, "clicked", G_CALLBACK (on_group_action), g);
    gtk_widget_set_halign (box, GTK_ALIGN_END);
    gtk_widget_set_margin_top (box, 6);
    gtk_widget_set_margin_bottom (box, 6);
    gtk_widget_set_margin_end (box, 6);
    gtk_box_append (GTK_BOX (box), g->action_button);
    gtk_list_box_row_set_child (GTK_LIST_BOX_ROW (row), box);
    gtk_list_box_row_set_activatable (GTK_LIST_BOX_ROW (row), FALSE);
    /* Before every query (detail_query_sort: higher ids first) */
    g_object_set_data (G_OBJECT (row), "query-id", GUINT_TO_POINTER (G_MAXUINT));
    g->action_row = row;
    detail_group_add_row (g, row);
}

/* The group of an app (key: its key) or a device (key: "\t<kind>\t<client>") */
static DetailGroup *
detail_group_get_key (DnsFilter *f, const char *key)
{
    DetailGroup *g = g_hash_table_lookup (f->detail_groups, key);
    g_autofree char *who = NULL;

    if (g)
        return g;
    g = g_new0 (DetailGroup, 1);
    g->f = f;
    g->key = g_strdup (key);
    g->row = adw_expander_row_new ();
    who = who_title (key);
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (g->row), who);
    adw_preferences_row_set_use_markup (ADW_PREFERENCES_ROW (g->row), FALSE);
    adw_expander_row_set_subtitle_lines (ADW_EXPANDER_ROW (g->row), 2);
    g_object_set_data_full (G_OBJECT (g->row), "detail-group", g, (GDestroyNotify) detail_group_free);
    g_hash_table_insert (f->detail_groups, g->key, g);
    gtk_list_box_append (f->detail_list, g->row);
    if (key_is_app (key)) {
        detail_group_add_action (g);
        detail_check_group (f, g);
    }
    return g;
}

/* The app's group of a query: by the app that asked, else by the device (hotspot, USB) */
static DetailGroup *
detail_group_get (DnsFilter *f, const LogItem *it)
{
    g_autofree char *key = *it->app ? g_strdup (it->app) : g_strdup_printf ("\t%s\t%s", it->kind, it->client);

    return detail_group_get_key (f, key);
}

/* The oldest query on the page goes when there are too many */
static gboolean
detail_drop_oldest (DnsFilter *f)
{
    GHashTableIter iter;
    DetailGroup *g, *oldest = NULL;
    GtkListBoxRow *oldest_row = NULL;
    guint oldest_id = G_MAXUINT;

    g_hash_table_iter_init (&iter, f->detail_groups);
    while (g_hash_table_iter_next (&iter, NULL, (gpointer *) &g)) {
        /* Block for App is the first row of an app's list */
        GtkListBoxRow *last = g->list && g->rows ? gtk_list_box_get_row_at_index (g->list, g->rows - (g->action_row ? 0 : 1))
                                                 : NULL;
        guint id = last ? GPOINTER_TO_UINT (g_object_get_data (G_OBJECT (last), "query-id")) : G_MAXUINT;

        if (id < oldest_id) {
            oldest_id = id;
            oldest = g;
            oldest_row = last;
        }
    }
    if (!oldest)
        return FALSE;
    if (g_object_get_data (G_OBJECT (oldest_row), "query-blocked"))
        oldest->blocked--;
    adw_expander_row_remove (ADW_EXPANDER_ROW (oldest->row), GTK_WIDGET (oldest_row));
    oldest->rows--;
    f->detail_rows--;
    if (oldest->rows == 0 && !oldest->stats_count) {
        g_hash_table_remove (f->detail_groups, oldest->key);
        gtk_list_box_remove (f->detail_list, oldest->row);
    } else {
        detail_group_update (oldest);
    }
    return TRUE;
}

static void
detail_row_add (DnsFilter *f, GVariant *entry, gboolean live)
{
    LogItem it;
    GtkWidget *row = adw_action_row_new ();
    DetailGroup *g;
    g_autofree char *when = NULL;
    g_autofree char *status = NULL;
    g_autofree char *tooltip = NULL;
    g_autofree char *subtitle = NULL;
    char tbuf[16];

    log_item_get (entry, &it);
    when = format_time (it.time_us / G_USEC_PER_SEC, TRUE);
    status = status_text (it.status, it.reason, it.via);
    tooltip = log_item_tooltip (&it);
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), when);
    adw_action_row_set_subtitle_lines (ADW_ACTION_ROW (row), 3);
    /* Record type · what was decided or the answer */
    subtitle = g_strdup_printf ("%s · %s%s", type_name (it.type, tbuf), *status ? "" : P_("Answer: "),
                                *status ? status : (*it.answer ? it.answer : "–"));
    adw_action_row_set_subtitle (ADW_ACTION_ROW (row), subtitle);
    gtk_widget_set_tooltip_text (row, tooltip);
    g_object_set_data (G_OBJECT (row), "query-id", GUINT_TO_POINTER (it.id));
    if (status_blocked (it.status)) {
        gtk_widget_add_css_class (row, "plinius-dns-blocked");
        g_object_set_data (G_OBJECT (row), "query-blocked", GINT_TO_POINTER (TRUE));
    }

    g = detail_group_get (f, &it);
    detail_group_add_row (g, row);
    g->rows++;
    if (status_blocked (it.status))
        g->blocked++;
    if (it.id > g->newest_id) {
        g->newest_id = it.id;
        g->newest_us = it.time_us;
    }
    detail_group_update (g);
    gtk_list_box_row_changed (GTK_LIST_BOX_ROW (g->row));

    f->detail_rows++;
    if (live) {
        if (f->detail_rows > LOG_MAX_ROWS) {
            /* Older ones loaded with "more" may make more than one too many */
            while (f->detail_rows > LOG_MAX_ROWS && detail_drop_oldest (f))
                ;
            /* Older ones would leave a gap */
            f->detail_oldest = 0;
            gtk_widget_set_visible (f->detail_more, FALSE);
        }
    } else {
        f->detail_oldest = it.id;
    }
}

/* Only one app asked: its queries show at once */
static void
detail_expand_single (DnsFilter *f)
{
    GHashTableIter iter;
    DetailGroup *g;

    if (g_hash_table_size (f->detail_groups) != 1)
        return;
    g_hash_table_iter_init (&iter, f->detail_groups);
    if (g_hash_table_iter_next (&iter, NULL, (gpointer *) &g))
        adw_expander_row_set_expanded (ADW_EXPANDER_ROW (g->row), TRUE);
}

static void
on_detail_log (DnsFilter *f, GVariant *result, GError *error, gpointer data)
{
    g_autoptr(GVariant) entries = NULL;
    GVariantIter it;
    GVariant *e;
    guint n = 0;

    if (!result || GPOINTER_TO_UINT (data) != f->detail_generation)
        return;
    g_variant_get (result, "(@a" LOG_TYPE ")", &entries);
    g_variant_iter_init (&it, entries);
    while ((e = g_variant_iter_next_value (&it))) {
        const char *name;

        /* A service without exact search (older than GetLogNames) gives all names that contain it */
        g_variant_get_child (e, 2, "&s", &name);
        if (!g_strcmp0 (name, f->detail_name))
            detail_row_add (f, e, FALSE);
        g_variant_unref (e);
        n++;
    }
    gtk_widget_set_visible (f->detail_more, n == LOG_PAGE);
    detail_expand_single (f);
}

/* Who asked the name, from the statistics: apps and devices whose queries the log no longer has
 * (it starts anew with each restart, the statistics stay) */
static void
on_detail_askers (DnsFilter *f, GVariant *result, GError *error, gpointer data)
{
    g_autoptr(GVariant) askers = NULL;
    GVariantIter it;
    const char *key;
    guint32 count;

    if (!result || GPOINTER_TO_UINT (data) != f->detail_generation)
        return;
    g_variant_get (result, "(@a(su))", &askers);
    g_variant_iter_init (&it, askers);
    while (g_variant_iter_next (&it, "(&su)", &key, &count)) {
        DetailGroup *g = detail_group_get_key (f, key);

        g->stats_count = count;
        detail_group_update (g);
    }
    detail_expand_single (f);
}

static void
get_detail_log (DnsFilter *f)
{
    call (f, "GetLog", g_variant_new ("(uusu)", f->detail_oldest, LOG_PAGE, f->detail_name, LOG_EXACT),
          on_detail_log, GUINT_TO_POINTER (f->detail_generation));
}

static void
on_detail_more (DnsFilter *f)
{
    if (f->detail_oldest)
        get_detail_log (f);
}

static void
clear_detail (DnsFilter *f)
{
    f->detail_generation++;
    if (f->detail_names->len) {
        NameRow *nr = g_ptr_array_index (f->detail_names, 0);

        g_ptr_array_set_size (f->detail_names, 0);
        adw_preferences_group_remove (f->detail_head_group, nr->row);
    }
    /* The groups go with their rows */
    g_hash_table_remove_all (f->detail_groups);
    list_box_clear (f->detail_list);
    f->detail_rows = 0;
    f->detail_oldest = 0;
    gtk_widget_set_visible (f->detail_more, FALSE);
}

static void
load_detail (DnsFilter *f)
{
    NameRow *log_nr, *nr;
    GtkWidget *row;

    clear_detail (f);
    if (!f->detail_name)
        return;
    adw_navigation_page_set_title (ADW_NAVIGATION_PAGE (f->detail_page), f->detail_name);
    /* The name with Block or Unblock; the status comes from Check */
    log_nr = g_hash_table_lookup (f->log_names, f->detail_name);
    row = adw_action_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), f->detail_name);
    adw_preferences_row_set_use_markup (ADW_PREFERENCES_ROW (row), FALSE);
    adw_action_row_set_subtitle_lines (ADW_ACTION_ROW (row), 3);
    /* A log row's status may come from a rule for its app; the button is about all apps */
    if (log_nr && log_nr->reason && strstr (log_nr->reason, APP_MODIFIER))
        log_nr = NULL;
    nr = name_row_new (f, row, f->detail_name, log_nr ? log_nr->status : ST_PASSED, log_nr ? log_nr->reason : "",
                       log_nr ? log_nr->via : "", TRUE);
    g_object_set_data_full (G_OBJECT (row), "name-row", nr, (GDestroyNotify) name_row_free);
    name_row_update (nr);
    g_ptr_array_add (f->detail_names, nr);
    adw_preferences_group_add (f->detail_head_group, row);
    check_name (f, f->detail_name);
    update_placeholders (f);
    get_detail_log (f);
    call (f, "GetAskersOf",
          g_variant_new ("(us)", f->detail_days ? f->detail_days : MAX (prop_uint (f, "StatsDays"), 1), f->detail_name),
          on_detail_askers, GUINT_TO_POINTER (f->detail_generation));
}

static GtkWidget *
detail_page_new (DnsFilter *f)
{
    AdwPreferencesPage *page = ADW_PREFERENCES_PAGE (adw_preferences_page_new ());
    AdwPreferencesGroup *group;
    GtkWidget *title;

    f->detail_names = g_ptr_array_new ();
    f->detail_groups = g_hash_table_new (g_str_hash, g_str_equal);
    f->detail_head_group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_page_add (page, f->detail_head_group);

    group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_group_set_title (group, P_("Queries"));
    f->detail_list = GTK_LIST_BOX (gtk_list_box_new ());
    gtk_list_box_set_selection_mode (f->detail_list, GTK_SELECTION_NONE);
    gtk_list_box_set_sort_func (f->detail_list, detail_group_sort, NULL, NULL);
    gtk_widget_add_css_class (GTK_WIDGET (f->detail_list), "boxed-list");
    placeholder_set (f->detail_list, P_("No queries in the log"), &title, &f->detail_empty_hint);
    adw_preferences_group_add (group, GTK_WIDGET (f->detail_list));
    adw_preferences_page_add (page, group);

    group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    f->detail_more = adw_button_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (f->detail_more), P_("Show Older Queries"));
    gtk_widget_set_visible (f->detail_more, FALSE);
    g_signal_connect_swapped (f->detail_more, "activated", G_CALLBACK (on_detail_more), f);
    adw_preferences_group_add (group, f->detail_more);
    adw_preferences_page_add (page, group);

    f->detail_page = subpage_new (GTK_WIDGET (page), P_("Queries"), TAG_NAME);
    return f->detail_page;
}

/* The live log: new queries that match what is shown */
static void
on_signal (GDBusProxy *proxy, const char *sender, const char *signal, GVariant *params, DnsFilter *f)
{
    g_autoptr(GVariant) entries = NULL;
    g_autofree char *needle = NULL;
    const char *search;
    guint filter;
    GVariantIter it;
    GVariant *e;

    if (g_strcmp0 (signal, "Logged") || (!f->log_shown && !f->detail_shown))
        return;
    search = gtk_editable_get_text (GTK_EDITABLE (f->log_search));
    needle = g_utf8_casefold (search, -1);
    filter = log_filter_value (f);
    g_variant_get (params, "(@a" LOG_TYPE ")", &entries);
    g_variant_iter_init (&it, entries);
    /* Oldest first, so the newest ends on top */
    while ((e = g_variant_iter_next_value (&it))) {
        LogItem item;
        g_autoptr(GIcon) icon = NULL;
        g_autofree char *title = NULL;
        g_autofree char *title_folded = NULL;
        g_autofree char *app_folded = NULL;

        log_item_get (e, &item);
        if (f->detail_shown && !g_strcmp0 (item.name, f->detail_name))
            detail_row_add (f, e, TRUE);
        if (!f->log_shown) {
            g_variant_unref (e);
            continue;
        }
        title = app_title (item.app, &icon);
        title_folded = title ? g_utf8_casefold (title, -1) : NULL;
        app_folded = g_utf8_casefold (item.app, -1);
        if (!(filter == 1 && !status_blocked (item.status)) && !(filter == 2 && status_blocked (item.status)) &&
            !(*search && !strstr (item.name, needle) && !strstr (app_folded, needle) &&
              !(title_folded && strstr (title_folded, needle))))
            log_name_seen (f, &item);
        g_variant_unref (e);
    }
}

static GtkWidget *
log_page_new (DnsFilter *f)
{
    AdwPreferencesPage *page = ADW_PREFERENCES_PAGE (adw_preferences_page_new ());
    AdwPreferencesGroup *group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    GtkWidget *box = gtk_box_new (GTK_ORIENTATION_VERTICAL, 12);
    AdwToggle *t;

    f->log_search = gtk_search_entry_new ();
    gtk_search_entry_set_placeholder_text (GTK_SEARCH_ENTRY (f->log_search), P_("Search names"));
    gtk_box_append (GTK_BOX (box), f->log_search);
    f->log_filter = ADW_TOGGLE_GROUP (adw_toggle_group_new ());
    t = adw_toggle_new ();
    adw_toggle_set_name (t, "all");
    adw_toggle_set_label (t, P_("All"));
    adw_toggle_group_add (f->log_filter, t);
    t = adw_toggle_new ();
    adw_toggle_set_name (t, "blocked");
    adw_toggle_set_label (t, P_("Blocked"));
    adw_toggle_group_add (f->log_filter, t);
    t = adw_toggle_new ();
    adw_toggle_set_name (t, "allowed");
    adw_toggle_set_label (t, P_("Allowed"));
    adw_toggle_group_add (f->log_filter, t);
    adw_toggle_group_set_active_name (f->log_filter, "all");
    adw_toggle_group_set_homogeneous (f->log_filter, TRUE);
    gtk_box_append (GTK_BOX (box), GTK_WIDGET (f->log_filter));
    adw_preferences_group_add (group, box);
    adw_preferences_page_add (page, group);

    f->log_group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    f->log_list = GTK_LIST_BOX (gtk_list_box_new ());
    gtk_list_box_set_selection_mode (f->log_list, GTK_SELECTION_NONE);
    gtk_widget_add_css_class (GTK_WIDGET (f->log_list), "boxed-list");
    placeholder_set (f->log_list, P_("No queries yet"), &f->log_empty, &f->log_empty_hint);
    adw_preferences_group_add (f->log_group, GTK_WIDGET (f->log_list));
    adw_preferences_page_add (page, f->log_group);
    f->log_rows = g_ptr_array_new ();
    f->log_names = g_hash_table_new (g_str_hash, g_str_equal);

    group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    f->log_more = adw_button_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (f->log_more), P_("Show Older Queries"));
    g_signal_connect_swapped (f->log_more, "activated", G_CALLBACK (get_log_names), f);
    adw_preferences_group_add (group, f->log_more);
    adw_preferences_page_add (page, group);

    g_signal_connect_swapped (f->log_search, "search-changed", G_CALLBACK (reload_log), f);
    g_signal_connect_swapped (f->log_filter, "notify::active", G_CALLBACK (reload_log), f);
    f->log_page = subpage_new (GTK_WIDGET (page), P_("Query Log"), TAG_LOG);
    return f->log_page;
}

/* ----- Statistics ----- */

static guint
stats_days (DnsFilter *f)
{
    const char *name = adw_toggle_group_get_active_name (f->stats_range);

    if (!g_strcmp0 (name, "week"))
        return 7;
    if (!g_strcmp0 (name, "month"))
        return 30;
    return 1;
}

static void
stats_add_row (DnsFilter *f, AdwPreferencesGroup *group, GtkWidget *row)
{
    adw_preferences_group_add (group, row);
    g_ptr_array_add (f->stats_rows, group);
    g_ptr_array_add (f->stats_rows, row);
}

static void
on_stats_name_activated (AdwActionRow *row, NameRow *nr)
{
    DnsFilter *f = nr->f;

    g_free (f->detail_name);
    f->detail_name = g_strdup (nr->name);
    /* Who asked: over the range the statistics show */
    f->detail_days = stats_days (f);
    gtk_widget_activate_action (GTK_WIDGET (row), "navigation.push", "s", TAG_NAME);
}

/* The most asked or blocked names; Block and Unblock are on a name's page */
static void
stats_add_names (DnsFilter *f, AdwPreferencesGroup *group, GVariant *list, gboolean blocked)
{
    GVariantIter it;
    const char *name;
    guint32 count;

    g_variant_iter_init (&it, list);
    while (g_variant_iter_next (&it, "(&su)", &name, &count)) {
        GtkWidget *row = adw_action_row_new ();
        g_autofree char *c = format_count (count);
        GtkWidget *label = gtk_label_new (c);
        NameRow *nr;

        adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), name);
        adw_preferences_row_set_use_markup (ADW_PREFERENCES_ROW (row), FALSE);
        adw_action_row_set_title_lines (ADW_ACTION_ROW (row), 1);
        gtk_widget_add_css_class (label, "dim-label");
        gtk_widget_add_css_class (label, "numeric");
        adw_action_row_add_suffix (ADW_ACTION_ROW (row), label);
        adw_action_row_add_suffix (ADW_ACTION_ROW (row), gtk_image_new_from_icon_name ("go-next-symbolic"));
        gtk_list_box_row_set_activatable (GTK_LIST_BOX_ROW (row), TRUE);
        /* Until Check answers: what the list says */
        nr = name_row_new (f, row, name, blocked ? ST_BLOCKED_LIST : ST_PASSED, "", NULL, FALSE);
        g_signal_connect (row, "activated", G_CALLBACK (on_stats_name_activated), nr);
        g_object_set_data_full (G_OBJECT (row), "name-row", nr, (GDestroyNotify) name_row_free);
        g_ptr_array_add (f->stats_names, nr);
        name_row_update (nr);
        check_name (f, name);
        stats_add_row (f, group, row);
    }
}

/* The lists' pages are titled with the range picked on the Statistics page */
static void
stats_update_titles (DnsFilter *f)
{
    AdwPreferencesGroup *groups[] = { f->top_group, f->blocked_group, f->apps_group, f->clients_group,
                                      f->servers_group };
    guint days = stats_days (f);
    const char *title = days == 7 ? P_("Last 7 Days") : days == 30 ? P_("Last 30 Days") : P_("Last 24 Hours");

    for (guint i = 0; i < G_N_ELEMENTS (groups); i++)
        adw_preferences_group_set_title (groups[i], title);
}

/* A list's row on the Statistics page: its top entry and count, or None */
static void
stats_nav_subtitle (AdwActionRow *row, GVariant *list, const char *kind)
{
    g_autofree char *text = NULL;
    g_autofree char *who = NULL;
    g_autofree char *c = NULL;
    const char *key;
    guint32 count, avg;

    if (!list || !g_variant_n_children (list)) {
        adw_action_row_set_subtitle (row, P_("None"));
        return;
    }
    if (!g_strcmp0 (kind, "server")) {
        g_variant_get_child (list, 0, "(&suu)", &key, &count, &avg);
        /* The servers come unsorted: the one that answered most */
        for (gsize i = 1; i < g_variant_n_children (list); i++) {
            const char *k;
            guint32 n, a;

            g_variant_get_child (list, i, "(&suu)", &k, &n, &a);
            if (n > count) {
                key = k;
                count = n;
            }
        }
    } else {
        g_variant_get_child (list, 0, "(&su)", &key, &count);
    }
    if (!g_strcmp0 (kind, "app")) {
        g_autoptr(GIcon) icon = NULL;

        who = app_title (key, &icon);
    } else if (!g_strcmp0 (kind, "client")) {
        g_auto(GStrv) parts = g_strsplit (key, "\t", 2);

        who = client_text (parts[0], parts[1] ? parts[1] : "");
    }
    c = format_count (count);
    text = g_strdup_printf ("%s · %s", who ? who : key, c);
    adw_action_row_set_subtitle (row, text);
}

/* ----- The names one app or device asked: blocked and allowed, each with its button ----- */

static void
on_who_activated (AdwActionRow *row, DnsFilter *f)
{
    g_free (f->who_key);
    f->who_key = g_strdup (g_object_get_data (G_OBJECT (row), "who-key"));
    g_free (f->who_title);
    f->who_title = g_strdup (g_object_get_data (G_OBJECT (row), "who-title"));
    gtk_widget_activate_action (GTK_WIDGET (row), "navigation.push", "s", TAG_STATS_WHO);
}

/* A row of the Apps or Devices page opens the names it asked */
static void
stats_who_activatable (DnsFilter *f, GtkWidget *row, const char *key, const char *title)
{
    g_object_set_data_full (G_OBJECT (row), "who-key", g_strdup (key), g_free);
    g_object_set_data_full (G_OBJECT (row), "who-title", g_strdup (title), g_free);
    adw_action_row_add_suffix (ADW_ACTION_ROW (row), gtk_image_new_from_icon_name ("go-next-symbolic"));
    gtk_list_box_row_set_activatable (GTK_LIST_BOX_ROW (row), TRUE);
    g_signal_connect (row, "activated", G_CALLBACK (on_who_activated), f);
}

static void
clear_who (DnsFilter *f)
{
    f->who_generation++;
    for (guint i = 0; i + 1 < f->who_rows->len; i += 2)
        adw_preferences_group_remove (g_ptr_array_index (f->who_rows, i), g_ptr_array_index (f->who_rows, i + 1));
    g_ptr_array_set_size (f->who_rows, 0);
}

typedef struct {
    guint generation;
    GVariant *names; /* a(su) */
} WhoNames;

static void
on_who_checked (DnsFilter *f, GVariant *result, GError *error, gpointer data)
{
    WhoNames *w = data;
    g_autoptr(GVariant) results = NULL;
    gboolean app = key_is_app (f->who_key);
    guint n_blocked = 0, n_allowed = 0;

    if (w->generation != f->who_generation || !result) {
        g_variant_unref (w->names);
        g_free (w);
        return;
    }
    g_variant_get (result, "(@a(us))", &results);
    for (gsize i = 0; i < g_variant_n_children (w->names) && i < g_variant_n_children (results); i++) {
        const char *name, *reason;
        guint32 count, status;
        GtkWidget *row = adw_action_row_new ();
        AdwPreferencesGroup *group;
        NameRow *nr;

        g_variant_get_child (w->names, i, "(&su)", &name, &count);
        g_variant_get_child (results, i, "(u&s)", &status, &reason);
        adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), name);
        adw_preferences_row_set_use_markup (ADW_PREFERENCES_ROW (row), FALSE);
        adw_action_row_set_title_lines (ADW_ACTION_ROW (row), 1);
        adw_action_row_set_subtitle_lines (ADW_ACTION_ROW (row), 3);
        gtk_list_box_row_set_activatable (GTK_LIST_BOX_ROW (row), TRUE);
        /* An app's page blocks for the app; a device's for all (rules are per app, not per device) */
        nr = name_row_new (f, row, name, status, reason, NULL, TRUE);
        nr->app = app ? g_strdup (f->who_key) : NULL;
        nr->detail = g_strdup_printf (g_dngettext (PLINIUS_DOMAIN, "%u query", "%u queries", count), count);
        g_signal_connect (row, "activated", G_CALLBACK (on_stats_name_activated), nr);
        g_object_set_data_full (G_OBJECT (row), "name-row", nr, (GDestroyNotify) name_row_free);
        name_row_update (nr);
        group = status_blocked (status) ? f->who_blocked_group : f->who_allowed_group;
        if (status_blocked (status))
            n_blocked++;
        else
            n_allowed++;
        adw_preferences_group_add (group, row);
        g_ptr_array_add (f->who_rows, group);
        g_ptr_array_add (f->who_rows, row);
    }
    gtk_widget_set_visible (GTK_WIDGET (f->who_blocked_group), n_blocked > 0);
    gtk_widget_set_visible (GTK_WIDGET (f->who_allowed_group), n_allowed > 0 || n_blocked == 0);
    adw_preferences_group_set_description (f->who_allowed_group, n_allowed || n_blocked ? NULL : P_("None"));
    g_variant_unref (w->names);
    g_free (w);
}

static void
on_who_names (DnsFilter *f, GVariant *result, GError *error, gpointer data)
{
    g_autoptr(GPtrArray) names = NULL;
    WhoNames *w;
    GVariantIter it;
    const char *name;
    guint32 count;

    if (GPOINTER_TO_UINT (data) != f->who_generation)
        return;
    if (!result) {
        gtk_widget_set_visible (GTK_WIDGET (f->who_blocked_group), FALSE);
        gtk_widget_set_visible (GTK_WIDGET (f->who_allowed_group), TRUE);
        adw_preferences_group_set_description (f->who_allowed_group, P_("None"));
        return;
    }
    w = g_new0 (WhoNames, 1);
    w->generation = f->who_generation;
    g_variant_get (result, "(@a(su))", &w->names);
    names = g_ptr_array_new ();
    g_variant_iter_init (&it, w->names);
    while (g_variant_iter_next (&it, "(&su)", &name, &count))
        g_ptr_array_add (names, (gpointer) name);
    g_ptr_array_add (names, NULL);
    /* What the filter does with each name for this app (or for all, for a device) */
    call (f, "CheckNames",
          g_variant_new ("(s^as)", key_is_app (f->who_key) ? f->who_key : "", (char **) names->pdata), on_who_checked,
          w);
}

static void
load_who (DnsFilter *f)
{
    clear_who (f);
    if (!f->who_key)
        return;
    adw_navigation_page_set_title (ADW_NAVIGATION_PAGE (f->who_page), f->who_title ? f->who_title : "");
    call (f, "GetNamesOf", g_variant_new ("(us)", stats_days (f), f->who_key), on_who_names,
          GUINT_TO_POINTER (f->who_generation));
}

static GtkWidget *
who_page_new (DnsFilter *f)
{
    AdwPreferencesPage *page = ADW_PREFERENCES_PAGE (adw_preferences_page_new ());

    f->who_rows = g_ptr_array_new ();
    f->who_blocked_group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_group_set_title (f->who_blocked_group, P_("Blocked"));
    gtk_widget_set_visible (GTK_WIDGET (f->who_blocked_group), FALSE);
    adw_preferences_page_add (page, f->who_blocked_group);
    f->who_allowed_group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_group_set_title (f->who_allowed_group, P_("Allowed"));
    adw_preferences_page_add (page, f->who_allowed_group);
    f->who_page = subpage_new (GTK_WIDGET (page), P_("Apps"), TAG_STATS_WHO);
    return f->who_page;
}

static void
on_stats (DnsFilter *f, GVariant *result, GError *error, gpointer data)
{
    g_autoptr(GVariant) stats = NULL;
    g_autoptr(GVariant) series = NULL;
    g_autoptr(GVariant) top = NULL;
    g_autoptr(GVariant) blocked_top = NULL;
    g_autoptr(GVariant) clients = NULL;
    g_autoptr(GVariant) apps = NULL;
    g_autoptr(GVariant) ups = NULL;
    guint64 queries = 0, blocked = 0, cached = 0;
    guint32 average = 0, rules = 0, days = stats_days (f);
    GVariantIter it;
    const char *key;
    guint32 count, avg;

    for (guint i = 0; i + 1 < f->stats_rows->len; i += 2)
        adw_preferences_group_remove (g_ptr_array_index (f->stats_rows, i), g_ptr_array_index (f->stats_rows, i + 1));
    g_ptr_array_set_size (f->stats_rows, 0);
    g_ptr_array_set_size (f->stats_names, 0);
    if (!result)
        return;
    g_variant_get (result, "(@a{sv})", &stats);
    g_variant_lookup (stats, "queries", "t", &queries);
    g_variant_lookup (stats, "blocked", "t", &blocked);
    g_variant_lookup (stats, "cached", "t", &cached);
    g_variant_lookup (stats, "average-us", "u", &average);
    g_variant_lookup (stats, "rules", "u", &rules);
    series = g_variant_lookup_value (stats, "series", G_VARIANT_TYPE ("a(xuuuu)"));
    chart_fill (f->stats_total, f->stats_blocked, series, days * 24);
    gtk_widget_queue_draw (f->stats_chart);

    set_label_take (f->s_queries, format_count (queries));
    set_label_take (f->s_blocked, blocked_text (blocked, queries));
    set_label_take (f->s_cached, format_count (cached));
    set_label_take (f->s_average, format_ms (average));
    set_label_take (f->s_rules, format_count (rules));

    stats_update_titles (f);
    top = g_variant_lookup_value (stats, "top-domains", G_VARIANT_TYPE ("a(su)"));
    blocked_top = g_variant_lookup_value (stats, "top-blocked", G_VARIANT_TYPE ("a(su)"));
    stats_nav_subtitle (f->top_nav, top, NULL);
    stats_nav_subtitle (f->blocked_nav, blocked_top, NULL);
    if (top)
        stats_add_names (f, f->top_group, top, FALSE);
    if (blocked_top)
        stats_add_names (f, f->blocked_group, blocked_top, TRUE);

    apps = g_variant_lookup_value (stats, "top-apps", G_VARIANT_TYPE ("a(su)"));
    stats_nav_subtitle (f->apps_nav, apps, "app");
    if (apps) {
        g_variant_iter_init (&it, apps);
        while (g_variant_iter_next (&it, "(&su)", &key, &count)) {
            g_autoptr(GIcon) icon = NULL;
            g_autofree char *title = app_title (key, &icon);
            GtkWidget *row = adw_action_row_new ();
            g_autofree char *c = format_count (count);
            GtkWidget *label = gtk_label_new (c);
            GtkWidget *image = gtk_image_new_from_gicon (icon);

            gtk_image_set_icon_size (GTK_IMAGE (image), GTK_ICON_SIZE_LARGE);
            adw_action_row_add_prefix (ADW_ACTION_ROW (row), image);
            adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), title ? title : key);
            adw_preferences_row_set_use_markup (ADW_PREFERENCES_ROW (row), FALSE);
            gtk_widget_add_css_class (label, "dim-label");
            gtk_widget_add_css_class (label, "numeric");
            adw_action_row_add_suffix (ADW_ACTION_ROW (row), label);
            stats_who_activatable (f, row, key, title ? title : key);
            stats_add_row (f, f->apps_group, row);
        }
    }
    clients = g_variant_lookup_value (stats, "top-clients", G_VARIANT_TYPE ("a(su)"));
    stats_nav_subtitle (f->clients_nav, clients, "client");
    if (clients) {
        g_variant_iter_init (&it, clients);
        while (g_variant_iter_next (&it, "(&su)", &key, &count)) {
            g_auto(GStrv) parts = g_strsplit (key, "\t", 2);
            g_autofree char *who = client_text (parts[0], parts[1] ? parts[1] : "");
            GtkWidget *row = adw_action_row_new ();
            g_autofree char *c = format_count (count);
            GtkWidget *label = gtk_label_new (c);

            adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), who);
            adw_preferences_row_set_use_markup (ADW_PREFERENCES_ROW (row), FALSE);
            gtk_widget_add_css_class (label, "dim-label");
            gtk_widget_add_css_class (label, "numeric");
            adw_action_row_add_suffix (ADW_ACTION_ROW (row), label);
            {
                g_autofree char *who_key = g_strconcat ("\t", key, NULL);

                stats_who_activatable (f, row, who_key, who);
            }
            stats_add_row (f, f->clients_group, row);
        }
    }
    ups = g_variant_lookup_value (stats, "upstreams", G_VARIANT_TYPE ("a(suu)"));
    stats_nav_subtitle (f->upstreams_nav, ups, "server");
    if (ups) {
        g_variant_iter_init (&it, ups);
        while (g_variant_iter_next (&it, "(&suu)", &key, &count, &avg)) {
            GtkWidget *row = adw_action_row_new ();
            g_autofree char *c = format_count (count);
            g_autofree char *ms = format_ms (avg);
            /* Translators: answers of a DNS server and its average time, "312 answers, 18 ms" */
            g_autofree char *sub = g_strdup_printf (g_dngettext (PLINIUS_DOMAIN, "%s answer, %s", "%s answers, %s", count),
                                                    c, ms);

            adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), key);
            adw_preferences_row_set_use_markup (ADW_PREFERENCES_ROW (row), FALSE);
            adw_action_row_set_subtitle (ADW_ACTION_ROW (row), sub);
            stats_add_row (f, f->servers_group, row);
        }
    }
}

static void
refresh_stats (DnsFilter *f)
{
    call (f, "GetStats", g_variant_new ("(u)", stats_days (f)), on_stats, NULL);
}

static void
on_clear_stats_response (AdwAlertDialog *dialog, const char *response, DnsFilter *f)
{
    if (!g_strcmp0 (response, "clear"))
        call (f, "ClearStats", NULL, on_done_refresh, NULL);
}

static void
on_clear_stats (DnsFilter *f, GtkWidget *button)
{
    AdwDialog *dialog = adw_alert_dialog_new (P_("Clear Statistics?"), P_("All counts are lost."));

    adw_alert_dialog_add_responses (ADW_ALERT_DIALOG (dialog), "cancel", _("_Cancel"), "clear", P_("_Clear"), NULL);
    adw_alert_dialog_set_response_appearance (ADW_ALERT_DIALOG (dialog), "clear", ADW_RESPONSE_DESTRUCTIVE);
    g_signal_connect (dialog, "response", G_CALLBACK (on_clear_stats_response), f);
    adw_dialog_present (dialog, button);
}

static AdwActionRow *
stats_nav_new (AdwPreferencesGroup *group, const char *title, const char *tag)
{
    GtkWidget *row = nav_row_new (title, NULL, tag);

    adw_action_row_set_subtitle (ADW_ACTION_ROW (row), "");
    adw_preferences_group_add (group, row);
    return ADW_ACTION_ROW (row);
}

/* A page with one of the statistics' lists; its rows come with the statistics */
static GtkWidget *
stats_list_page_new (const char *title, const char *tag, AdwPreferencesGroup **group)
{
    AdwPreferencesPage *page = ADW_PREFERENCES_PAGE (adw_preferences_page_new ());

    *group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_page_add (page, *group);
    return subpage_new (GTK_WIDGET (page), title, tag);
}

static GtkWidget *
stats_page_new (DnsFilter *f)
{
    AdwPreferencesPage *page = ADW_PREFERENCES_PAGE (adw_preferences_page_new ());
    AdwPreferencesGroup *group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    AdwToggle *t;
    GtkWidget *row;

    f->stats_rows = g_ptr_array_new ();
    f->stats_names = g_ptr_array_new ();
    f->stats_range = ADW_TOGGLE_GROUP (adw_toggle_group_new ());
    t = adw_toggle_new ();
    adw_toggle_set_name (t, "day");
    adw_toggle_set_label (t, P_("24 Hours"));
    adw_toggle_group_add (f->stats_range, t);
    t = adw_toggle_new ();
    adw_toggle_set_name (t, "week");
    adw_toggle_set_label (t, P_("7 Days"));
    adw_toggle_group_add (f->stats_range, t);
    t = adw_toggle_new ();
    adw_toggle_set_name (t, "month");
    adw_toggle_set_label (t, P_("30 Days"));
    adw_toggle_group_add (f->stats_range, t);
    adw_toggle_group_set_active_name (f->stats_range, "day");
    adw_toggle_group_set_homogeneous (f->stats_range, TRUE);
    adw_preferences_group_add (group, GTK_WIDGET (f->stats_range));
    adw_preferences_page_add (page, group);
    group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    f->stats_total = g_array_new (FALSE, TRUE, sizeof (guint32));
    f->stats_blocked = g_array_new (FALSE, TRUE, sizeof (guint32));
    f->stats_data[0] = f->stats_total;
    f->stats_data[1] = f->stats_blocked;
    f->stats_chart = chart_new (f->stats_data);
    adw_preferences_group_add (group, chart_row_new (f->stats_chart));
    f->s_queries = value_row_new (group, P_("Queries"));
    f->s_blocked = value_row_new (group, P_("Blocked"));
    f->s_cached = value_row_new (group, P_("Answered from the Cache"));
    f->s_average = value_row_new (group, P_("Average Response Time"));
    f->s_rules = value_row_new (group, P_("Rules of the Lists"));
    adw_preferences_page_add (page, group);

    /* The lists on pages of their own, the top entry below each */
    group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    f->top_nav = stats_nav_new (group, P_("Most Asked"), TAG_STATS_ASKED);
    f->blocked_nav = stats_nav_new (group, P_("Most Blocked"), TAG_STATS_BLOCKED);
    f->apps_nav = stats_nav_new (group, P_("Apps"), TAG_STATS_APPS);
    f->clients_nav = stats_nav_new (group, P_("Devices"), TAG_STATS_DEVICES);
    f->upstreams_nav = stats_nav_new (group, P_("DNS Servers"), TAG_STATS_SERVERS);
    adw_preferences_page_add (page, group);

    group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    row = adw_button_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_("Clear Statistics"));
    gtk_widget_add_css_class (row, "destructive-action");
    g_signal_connect_swapped (row, "activated", G_CALLBACK (on_clear_stats), f);
    adw_preferences_group_add (group, row);
    adw_preferences_page_add (page, group);

    g_signal_connect_swapped (f->stats_range, "notify::active", G_CALLBACK (refresh_stats), f);
    return subpage_new (GTK_WIDGET (page), P_("Statistics"), TAG_STATS);
}

/* ----- Blocklists ----- */

static void
on_list_switch (GtkSwitch *sw, GParamSpec *pspec, DnsFilter *f)
{
    const char *id = g_object_get_data (G_OBJECT (sw), "list-id");

    if (!f->syncing)
        call (f, "SetListEnabled", g_variant_new ("(sb)", id, gtk_switch_get_active (sw)), NULL, NULL);
}

static void
on_list_remove (GtkButton *button, DnsFilter *f)
{
    const char *id = g_object_get_data (G_OBJECT (button), "list-id");

    call (f, "RemoveList", g_variant_new ("(s)", id), NULL, NULL);
}

static void
refresh_lists (DnsFilter *f)
{
    g_autoptr(GVariant) lists = prop (f, "Lists");
    GVariantIter it;
    GVariant *d;

    clear_rows (f->lists_group, f->list_rows);
    if (!lists || !g_variant_is_of_type (lists, G_VARIANT_TYPE ("aa{sv}")))
        return;
    f->syncing = TRUE;
    g_variant_iter_init (&it, lists);
    while ((d = g_variant_iter_next_value (&it))) {
        const char *id = "", *name = "", *url = "", *error = NULL;
        gboolean enabled = FALSE, downloading = FALSE;
        guint32 rules = 0;
        gint64 updated = 0;
        GtkWidget *row = adw_action_row_new ();
        GtkWidget *sw = gtk_switch_new ();
        GtkWidget *remove = flat_button_new (NULL, "user-trash-symbolic", P_("Remove List"));
        g_autofree char *sub = NULL;

        g_variant_lookup (d, "id", "&s", &id);
        g_variant_lookup (d, "name", "&s", &name);
        g_variant_lookup (d, "url", "&s", &url);
        g_variant_lookup (d, "enabled", "b", &enabled);
        g_variant_lookup (d, "downloading", "b", &downloading);
        g_variant_lookup (d, "rules", "u", &rules);
        g_variant_lookup (d, "updated", "x", &updated);
        g_variant_lookup (d, "error", "&s", &error);
        if (downloading) {
            sub = g_strdup (P_("Downloading…"));
        } else if (error && *error) {
            sub = g_strdup_printf (P_("Not updated: %s"), error);
        } else if (updated) {
            g_autofree char *n = format_count (rules);
            g_autofree char *when = format_time (updated, FALSE);

            /* Translators: rules of a blocklist and when it was downloaded, "207 301 rules, updated 16:26" */
            sub = g_strdup_printf (g_dngettext (PLINIUS_DOMAIN, "%s rule, updated %s", "%s rules, updated %s", rules), n,
                                   when);
        } else {
            sub = g_strdup (P_("Not downloaded yet"));
        }
        adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), name);
        adw_preferences_row_set_use_markup (ADW_PREFERENCES_ROW (row), FALSE);
        adw_action_row_set_subtitle (ADW_ACTION_ROW (row), sub);
        gtk_widget_set_tooltip_text (row, url);
        gtk_switch_set_active (GTK_SWITCH (sw), enabled);
        gtk_widget_set_valign (sw, GTK_ALIGN_CENTER);
        g_object_set_data_full (G_OBJECT (sw), "list-id", g_strdup (id), g_free);
        g_object_set_data_full (G_OBJECT (remove), "list-id", g_strdup (id), g_free);
        g_signal_connect (sw, "notify::active", G_CALLBACK (on_list_switch), f);
        g_signal_connect (remove, "clicked", G_CALLBACK (on_list_remove), f);
        adw_action_row_add_suffix (ADW_ACTION_ROW (row), remove);
        adw_action_row_add_suffix (ADW_ACTION_ROW (row), sw);
        adw_action_row_set_activatable_widget (ADW_ACTION_ROW (row), sw);
        adw_preferences_group_add (f->lists_group, row);
        g_ptr_array_add (f->list_rows, row);
        g_variant_unref (d);
    }
    adw_preferences_group_set_description (f->lists_group, f->list_rows->len ? NULL
                                                                           : P_("No lists. Add one to block anything."));
    f->syncing = FALSE;
}

static const guint intervals[] = { 0, 6, 12, 24, 72, 168 };

static void
on_interval (DnsFilter *f)
{
    guint i = adw_combo_row_get_selected (f->interval_row);

    if (i < G_N_ELEMENTS (intervals))
        set_prop (f, "UpdateInterval", g_variant_new_uint32 (intervals[i]));
}

static void
on_update_now (DnsFilter *f)
{
    call (f, "UpdateLists", NULL, NULL, NULL);
}

static GtkWidget *
lists_page_new (DnsFilter *f)
{
    AdwPreferencesPage *page = ADW_PREFERENCES_PAGE (adw_preferences_page_new ());
    AdwPreferencesGroup *group;
    GtkWidget *add, *row;
    g_autoptr(GtkStringList) model = gtk_string_list_new (NULL);

    f->list_rows = g_ptr_array_new ();
    f->lists_group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_group_set_title (f->lists_group, P_("Subscribed Lists"));
    add = gtk_button_new_from_icon_name ("list-add-symbolic");
    gtk_widget_add_css_class (add, "flat");
    gtk_widget_set_tooltip_text (add, P_("Add List"));
    gtk_actionable_set_action_name (GTK_ACTIONABLE (add), "navigation.push");
    gtk_actionable_set_action_target (GTK_ACTIONABLE (add), "s", TAG_ADD_LIST);
    adw_preferences_group_set_header_suffix (f->lists_group, add);
    adw_preferences_page_add (page, f->lists_group);

    group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_group_set_title (group, P_("Updates"));
    gtk_string_list_append (model, P_("Never"));
    gtk_string_list_append (model, P_("Every 6 Hours"));
    gtk_string_list_append (model, P_("Every 12 Hours"));
    gtk_string_list_append (model, P_("Daily"));
    gtk_string_list_append (model, P_("Every 3 Days"));
    gtk_string_list_append (model, P_("Weekly"));
    f->interval_row = ADW_COMBO_ROW (adw_combo_row_new ());
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (f->interval_row), P_("Update Lists"));
    adw_combo_row_set_model (f->interval_row, G_LIST_MODEL (model));
    adw_preferences_group_add (group, GTK_WIDGET (f->interval_row));
    f->mobile_row = ADW_SWITCH_ROW (adw_switch_row_new ());
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (f->mobile_row), P_("Also on Mobile Data"));
    adw_action_row_set_subtitle (ADW_ACTION_ROW (f->mobile_row),
                                 P_("Otherwise lists are updated on Wi-Fi only; a new list comes right away"));
    adw_preferences_group_add (group, GTK_WIDGET (f->mobile_row));
    row = adw_button_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_("Update Now"));
    adw_button_row_set_start_icon_name (ADW_BUTTON_ROW (row), "view-refresh-symbolic");
    g_signal_connect_swapped (row, "activated", G_CALLBACK (on_update_now), f);
    adw_preferences_group_add (group, row);
    adw_preferences_page_add (page, group);

    g_signal_connect_swapped (f->interval_row, "notify::selected", G_CALLBACK (on_interval), f);
    return subpage_new (GTK_WIDGET (page), P_("Blocklists"), TAG_LISTS);
}

/* Add List: suggestions and an own address */

static void
on_list_added (DnsFilter *f, GVariant *result, GError *error, gpointer data)
{
    if (error) {
        g_autofree char *text = error_text (error);

        adw_preferences_group_set_description (f->add_list_group, text);
        return;
    }
    adw_preferences_group_set_description (f->add_list_group, NULL);
}

static void
on_catalog_add (GtkButton *button, DnsFilter *f)
{
    call (f, "AddList", g_variant_new ("(ss)", (const char *) g_object_get_data (G_OBJECT (button), "id"), ""),
          on_list_added, NULL);
    gtk_widget_set_sensitive (GTK_WIDGET (button), FALSE);
}

static void
on_catalog (DnsFilter *f, GVariant *result, GError *error, gpointer data)
{
    g_autoptr(GVariant) lists = NULL;
    GVariantIter it;
    GVariant *d;

    clear_rows (f->catalog_group, f->catalog_rows);
    if (!result)
        return;
    g_variant_get (result, "(@aa{sv})", &lists);
    g_variant_iter_init (&it, lists);
    while ((d = g_variant_iter_next_value (&it))) {
        const char *id = "", *name = "", *description = "";
        gboolean added = FALSE;
        GtkWidget *row = adw_action_row_new ();

        g_variant_lookup (d, "id", "&s", &id);
        g_variant_lookup (d, "name", "&s", &name);
        g_variant_lookup (d, "description", "&s", &description);
        g_variant_lookup (d, "added", "b", &added);
        adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), name);
        adw_preferences_row_set_use_markup (ADW_PREFERENCES_ROW (row), FALSE);
        /* The descriptions come from the service in English; ours are translated here */
        adw_action_row_set_subtitle (ADW_ACTION_ROW (row), P_(description));
        if (added) {
            GtkWidget *icon = gtk_image_new_from_icon_name ("object-select-symbolic");

            gtk_widget_set_tooltip_text (icon, P_("Subscribed"));
            adw_action_row_add_suffix (ADW_ACTION_ROW (row), icon);
        } else {
            GtkWidget *button = flat_button_new (P_("Add"), NULL, NULL);

            gtk_widget_add_css_class (button, "pill");
            g_object_set_data_full (G_OBJECT (button), "id", g_strdup (id), g_free);
            g_signal_connect (button, "clicked", G_CALLBACK (on_catalog_add), f);
            adw_action_row_add_suffix (ADW_ACTION_ROW (row), button);
        }
        adw_preferences_group_add (f->catalog_group, row);
        g_ptr_array_add (f->catalog_rows, row);
        g_variant_unref (d);
    }
}

static void
on_own_list_added (DnsFilter *f, GVariant *result, GError *error, gpointer data)
{
    on_list_added (f, result, error, data);
    if (!error) {
        gtk_editable_set_text (GTK_EDITABLE (f->list_url), "");
        gtk_editable_set_text (GTK_EDITABLE (f->list_name), "");
        gtk_widget_activate_action (GTK_WIDGET (f->list_url), "navigation.pop", NULL);
    }
}

static void
on_add_own_list (DnsFilter *f)
{
    const char *url = gtk_editable_get_text (GTK_EDITABLE (f->list_url));

    if (!*url)
        return;
    call (f, "AddList", g_variant_new ("(ss)", url, gtk_editable_get_text (GTK_EDITABLE (f->list_name))),
          on_own_list_added, NULL);
}

static GtkWidget *
add_list_page_new (DnsFilter *f)
{
    AdwPreferencesPage *page = ADW_PREFERENCES_PAGE (adw_preferences_page_new ());
    GtkWidget *row;

    f->catalog_rows = g_ptr_array_new ();
    f->catalog_group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_group_set_title (f->catalog_group, P_("Suggested Lists"));
    adw_preferences_page_add (page, f->catalog_group);

    f->add_list_group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_group_set_title (f->add_list_group, P_("Other List"));
    f->list_name = ADW_ENTRY_ROW (adw_entry_row_new ());
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (f->list_name), P_("Name"));
    adw_preferences_group_add (f->add_list_group, GTK_WIDGET (f->list_name));
    f->list_url = ADW_ENTRY_ROW (adw_entry_row_new ());
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (f->list_url), P_("Web Address"));
    adw_entry_row_set_input_purpose (f->list_url, GTK_INPUT_PURPOSE_URL);
    adw_preferences_group_add (f->add_list_group, GTK_WIDGET (f->list_url));
    row = adw_button_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_("Add List"));
    adw_button_row_set_start_icon_name (ADW_BUTTON_ROW (row), "list-add-symbolic");
    g_signal_connect_swapped (row, "activated", G_CALLBACK (on_add_own_list), f);
    adw_preferences_group_add (f->add_list_group, row);
    adw_preferences_page_add (page, f->add_list_group);
    {
        AdwPreferencesGroup *group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());

        adw_preferences_group_set_description (
            group, P_("Lists in hosts format, as plain domains or in the Adblock syntax of DNS blocklists. Rules for web "
                      "pages (element hiding) are skipped."));
        adw_preferences_page_add (page, group);
    }
    return subpage_new (GTK_WIDGET (page), P_("Add List"), TAG_ADD_LIST);
}

/* ----- Own rules ----- */

static void
on_rule_remove (GtkButton *button, DnsFilter *f)
{
    call (f, "RemoveRule", g_variant_new ("(s)", (const char *) g_object_get_data (G_OBJECT (button), "rule")),
          on_done_refresh, NULL);
}

static void
on_rules (DnsFilter *f, GVariant *result, GError *error, gpointer data)
{
    g_autofree const char **rules = NULL;

    clear_rows (f->rules_group, f->rule_rows);
    if (!result)
        return;
    g_variant_get (result, "(^a&s)", &rules);
    for (guint i = 0; rules[i]; i++) {
        const char *r = rules[i];
        gboolean allow = g_str_has_prefix (r, "@@");
        gboolean sub;
        g_autofree char *domain = NULL;
        GtkWidget *row = adw_action_row_new ();
        GtkWidget *icon, *remove;
        const char *what;

        if (allow)
            r += 2;
        sub = g_str_has_prefix (r, "||");
        domain = g_strdup (r + (sub ? 2 : (r[0] == '|' ? 1 : 0)));
        domain[strcspn (domain, "^$")] = '\0';
        if (allow)
            what = sub ? P_("Allowed, with subdomains") : P_("Allowed, this name only");
        else
            what = sub ? P_("Blocked, with subdomains") : P_("Blocked, this name only");
        adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), domain);
        adw_preferences_row_set_use_markup (ADW_PREFERENCES_ROW (row), FALSE);
        if (strstr (r, APP_MODIFIER)) {
            g_autoptr(GIcon) app_icon = NULL;
            const char *key = strstr (r, APP_MODIFIER) + strlen (APP_MODIFIER);
            g_autofree char *app = app_title (key, &app_icon);
            /* Translators: an own rule for one app: "Blocked, with subdomains · only Firefox" */
            g_autofree char *line = g_strdup_printf (P_("%s · only %s"), what, app ? app : key);

            adw_action_row_set_subtitle (ADW_ACTION_ROW (row), line);
        } else {
            adw_action_row_set_subtitle (ADW_ACTION_ROW (row), what);
        }
        icon = gtk_image_new_from_icon_name (allow ? "object-select-symbolic" : "action-unavailable-symbolic");
        adw_action_row_add_prefix (ADW_ACTION_ROW (row), icon);
        remove = flat_button_new (NULL, "user-trash-symbolic", P_("Remove Rule"));
        g_object_set_data_full (G_OBJECT (remove), "rule", g_strdup (rules[i]), g_free);
        g_signal_connect (remove, "clicked", G_CALLBACK (on_rule_remove), f);
        adw_action_row_add_suffix (ADW_ACTION_ROW (row), remove);
        adw_preferences_group_add (f->rules_group, row);
        g_ptr_array_add (f->rule_rows, row);
    }
    adw_preferences_group_set_description (
        f->rules_group, f->rule_rows->len ? NULL : P_("No rules yet. Add one here, or block and unblock names right in the query log."));
}

static void
refresh_rules (DnsFilter *f)
{
    call (f, "GetRules", NULL, on_rules, NULL);
}

static void
on_rule_added (DnsFilter *f, GVariant *result, GError *error, gpointer data)
{
    if (error) {
        g_autofree char *text = error_text (error);

        gtk_widget_add_css_class (GTK_WIDGET (f->rule_entry), "error");
        adw_preferences_group_set_description (f->new_rule_group, text);
        return;
    }
    gtk_widget_remove_css_class (GTK_WIDGET (f->rule_entry), "error");
    adw_preferences_group_set_description (f->new_rule_group, NULL);
    gtk_editable_set_text (GTK_EDITABLE (f->rule_entry), "");
    refresh_all (f);
}

static void
on_add_rule (DnsFilter *f)
{
    const char *text = gtk_editable_get_text (GTK_EDITABLE (f->rule_entry));
    g_autofree char *host = NULL;
    g_autofree char *ascii = NULL;
    g_autofree char *rule = NULL;
    gboolean allow = !g_strcmp0 (adw_toggle_group_get_active_name (f->rule_kind), "allow");
    gboolean sub = adw_switch_row_get_active (f->rule_subdomains);

    while (*text == ' ')
        text++;
    if (!*text)
        return;
    /* Written as a rule already: as it is */
    if (text[0] == '|' || g_str_has_prefix (text, "@@")) {
        call (f, "AddRule", g_variant_new ("(s)", text), on_rule_added, NULL);
        return;
    }
    /* A pasted web address gives its host */
    if (strstr (text, "://")) {
        g_autoptr(GUri) uri = g_uri_parse (text, G_URI_FLAGS_NONE, NULL);

        host = g_strdup (uri && g_uri_get_host (uri) ? g_uri_get_host (uri) : text);
    } else {
        host = g_strdup (text);
    }
    g_strstrip (host);
    ascii = g_hostname_to_ascii (host);
    rule = g_strdup_printf ("%s%s%s^", allow ? "@@" : "", sub ? "||" : "|", ascii ? ascii : host);
    call (f, "AddRule", g_variant_new ("(s)", rule), on_rule_added, NULL);
}

static GtkWidget *
rules_page_new (DnsFilter *f)
{
    AdwPreferencesPage *page = ADW_PREFERENCES_PAGE (adw_preferences_page_new ());
    GtkWidget *row;
    AdwToggle *t;

    f->rule_rows = g_ptr_array_new ();
    f->new_rule_group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    f->rule_kind = ADW_TOGGLE_GROUP (adw_toggle_group_new ());
    t = adw_toggle_new ();
    adw_toggle_set_name (t, "block");
    adw_toggle_set_label (t, P_("Block"));
    adw_toggle_group_add (f->rule_kind, t);
    t = adw_toggle_new ();
    adw_toggle_set_name (t, "allow");
    adw_toggle_set_label (t, P_("Allow"));
    adw_toggle_group_add (f->rule_kind, t);
    adw_toggle_group_set_active_name (f->rule_kind, "block");
    adw_toggle_group_set_homogeneous (f->rule_kind, TRUE);
    {
        AdwPreferencesGroup *kind = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());

        adw_preferences_group_set_title (kind, P_("New Rule"));
        adw_preferences_group_add (kind, GTK_WIDGET (f->rule_kind));
        adw_preferences_page_add (page, kind);
    }
    f->rule_entry = ADW_ENTRY_ROW (adw_entry_row_new ());
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (f->rule_entry), P_("Domain"));
    adw_entry_row_set_input_purpose (f->rule_entry, GTK_INPUT_PURPOSE_URL);
    adw_entry_row_set_activates_default (f->rule_entry, FALSE);
    g_signal_connect_swapped (f->rule_entry, "entry-activated", G_CALLBACK (on_add_rule), f);
    adw_preferences_group_add (f->new_rule_group, GTK_WIDGET (f->rule_entry));
    f->rule_subdomains = ADW_SWITCH_ROW (adw_switch_row_new ());
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (f->rule_subdomains), P_("Include Subdomains"));
    adw_switch_row_set_active (f->rule_subdomains, TRUE);
    adw_preferences_group_add (f->new_rule_group, GTK_WIDGET (f->rule_subdomains));
    row = adw_button_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_("Add Rule"));
    adw_button_row_set_start_icon_name (ADW_BUTTON_ROW (row), "list-add-symbolic");
    g_signal_connect_swapped (row, "activated", G_CALLBACK (on_add_rule), f);
    adw_preferences_group_add (f->new_rule_group, row);
    adw_preferences_page_add (page, f->new_rule_group);

    f->rules_group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_group_set_title (f->rules_group, P_("Your Rules"));
    adw_preferences_page_add (page, f->rules_group);
    {
        AdwPreferencesGroup *group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());

        adw_preferences_group_set_description (
            group, P_("Your rules come before the lists: an allowed domain is never blocked by a list."));
        adw_preferences_page_add (page, group);
    }
    return subpage_new (GTK_WIDGET (page), P_("Own Rules"), TAG_RULES);
}

/* ----- DNS servers ----- */

static const char *
protocol_name (const char *spec)
{
    if (g_str_has_prefix (spec, "tls://"))
        return P_("DNS over TLS");
    if (g_str_has_prefix (spec, "https://"))
        return P_("DNS over HTTPS");
    return P_("Plain DNS");
}

static void
set_servers (DnsFilter *f, GStrv servers, DoneFunc done)
{
    set_prop_full (f, "Servers", g_variant_new_strv ((const char *const *) servers, -1), done, NULL);
}

static void
on_server_set (DnsFilter *f, GVariant *result, GError *error, gpointer data)
{
    if (error) {
        g_autofree char *text = error_text (error);

        gtk_widget_add_css_class (GTK_WIDGET (f->server_entry), "error");
        adw_preferences_group_set_description (f->custom_group, text);
        return;
    }
    gtk_widget_remove_css_class (GTK_WIDGET (f->server_entry), "error");
    gtk_editable_set_text (GTK_EDITABLE (f->server_entry), "");
    adw_preferences_group_set_description (f->custom_group, NULL);
}

static void
add_server (DnsFilter *f, const char *spec)
{
    g_auto(GStrv) servers = prop_strv (f, "Servers");
    g_autoptr(GStrvBuilder) b = g_strv_builder_new ();
    g_auto(GStrv) next = NULL;

    if (!*spec || g_strv_contains ((const char *const *) servers, spec))
        return;
    g_strv_builder_addv (b, (const char **) servers);
    g_strv_builder_add (b, spec);
    next = g_strv_builder_end (b);
    set_servers (f, next, on_server_set);
    /* The first own server makes sense of the choice */
    if (!servers[0] && !prop_bool (f, "CustomServers"))
        set_prop (f, "CustomServers", g_variant_new_boolean (TRUE));
}

static void
on_server_entry (DnsFilter *f)
{
    g_autofree char *spec = g_strstrip (g_strdup (gtk_editable_get_text (GTK_EDITABLE (f->server_entry))));

    add_server (f, spec);
}

static void
on_server_remove (GtkButton *button, DnsFilter *f)
{
    const char *spec = g_object_get_data (G_OBJECT (button), "spec");
    g_auto(GStrv) servers = prop_strv (f, "Servers");
    g_autoptr(GStrvBuilder) b = g_strv_builder_new ();
    g_auto(GStrv) next = NULL;

    for (guint i = 0; servers[i]; i++)
        if (g_strcmp0 (servers[i], spec))
            g_strv_builder_add (b, servers[i]);
    next = g_strv_builder_end (b);
    set_servers (f, next, NULL);
}

static void
on_tested (DnsFilter *f, GVariant *result, GError *error, gpointer data)
{
    GtkWidget *row = data;
    gboolean ok;
    guint32 ms;
    const char *message;
    g_autofree char *text = NULL;

    if (result) {
        g_variant_get (result, "(bu&s)", &ok, &ms, &message);
        if (ok)
            /* Translators: result of testing a DNS server */
            text = g_strdup_printf (P_("Answers in %u ms"), ms);
        else
            text = g_strdup_printf (P_("Does not work: %s"), message);
        adw_action_row_set_subtitle (ADW_ACTION_ROW (row), text);
    }
    g_object_unref (row);
}

static void
on_server_test (GtkButton *button, DnsFilter *f)
{
    GtkWidget *row = gtk_widget_get_ancestor (GTK_WIDGET (button), ADW_TYPE_ACTION_ROW);

    adw_action_row_set_subtitle (ADW_ACTION_ROW (row), P_("Testing…"));
    call (f, "TestServer", g_variant_new ("(s)", (const char *) g_object_get_data (G_OBJECT (button), "spec")),
          on_tested, g_object_ref (row));
}

static void
on_suggest_add (GtkButton *button, DnsFilter *f)
{
    add_server (f, g_object_get_data (G_OBJECT (button), "spec"));
}

static void
refresh_servers (DnsFilter *f)
{
    g_auto(GStrv) servers = prop_strv (f, "Servers");
    g_auto(GStrv) network = prop_strv (f, "NetworkServers");
    g_autofree char *net_text = NULL;
    gboolean custom = prop_bool (f, "CustomServers");

    f->syncing = TRUE;
    gtk_check_button_set_active (custom ? f->custom_check : f->network_check, TRUE);
    adw_switch_row_set_active (f->local_row, prop_bool (f, "LocalViaNetwork"));
    f->syncing = FALSE;
    net_text = network[0] ? g_strjoinv (", ", network) : g_strdup (P_("No network right now"));
    adw_action_row_set_subtitle (f->network_source_row, net_text);

    clear_rows (f->custom_group, f->server_rows);
    for (guint i = 0; servers[i]; i++) {
        GtkWidget *row = adw_action_row_new ();
        GtkWidget *test = flat_button_new (P_("Test"), NULL, NULL);
        GtkWidget *remove = flat_button_new (NULL, "user-trash-symbolic", P_("Remove Server"));

        adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), servers[i]);
        adw_preferences_row_set_use_markup (ADW_PREFERENCES_ROW (row), FALSE);
        adw_action_row_set_subtitle (ADW_ACTION_ROW (row), protocol_name (servers[i]));
        g_object_set_data_full (G_OBJECT (test), "spec", g_strdup (servers[i]), g_free);
        g_object_set_data_full (G_OBJECT (remove), "spec", g_strdup (servers[i]), g_free);
        g_signal_connect (test, "clicked", G_CALLBACK (on_server_test), f);
        g_signal_connect (remove, "clicked", G_CALLBACK (on_server_remove), f);
        adw_action_row_add_suffix (ADW_ACTION_ROW (row), test);
        adw_action_row_add_suffix (ADW_ACTION_ROW (row), remove);
        adw_preferences_group_add (f->custom_group, row);
        g_ptr_array_add (f->server_rows, row);
    }
    /* The entry stays the last row */
    g_object_ref (f->server_entry);
    adw_preferences_group_remove (f->custom_group, GTK_WIDGET (f->server_entry));
    adw_preferences_group_add (f->custom_group, GTK_WIDGET (f->server_entry));
    g_object_unref (f->server_entry);

    clear_rows (f->suggest_group, f->suggest_rows);
    for (guint i = 0; i < G_N_ELEMENTS (suggested_servers); i++) {
        GtkWidget *row, *add;

        if (g_strv_contains ((const char *const *) servers, suggested_servers[i].spec))
            continue;
        row = adw_action_row_new ();
        add = flat_button_new (NULL, "list-add-symbolic", P_("Add Server"));
        /* Translators: a suggested DNS server: its operator and protocol, "Quad9, DNS over TLS" */
        {
            g_autofree char *title = g_strdup_printf (P_("%s, %s"), suggested_servers[i].name,
                                                      protocol_name (suggested_servers[i].spec));

            adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), title);
        }
        adw_preferences_row_set_use_markup (ADW_PREFERENCES_ROW (row), FALSE);
        adw_action_row_set_subtitle (ADW_ACTION_ROW (row), suggested_servers[i].spec);
        g_object_set_data (G_OBJECT (add), "spec", (gpointer) suggested_servers[i].spec);
        g_signal_connect (add, "clicked", G_CALLBACK (on_suggest_add), f);
        adw_action_row_add_suffix (ADW_ACTION_ROW (row), add);
        adw_preferences_group_add (f->suggest_group, row);
        g_ptr_array_add (f->suggest_rows, row);
    }
    gtk_widget_set_visible (GTK_WIDGET (f->suggest_group), f->suggest_rows->len > 0);
}

static void
on_source (DnsFilter *f)
{
    if (!f->syncing)
        set_prop (f, "CustomServers", g_variant_new_boolean (gtk_check_button_get_active (f->custom_check)));
}

static void
on_local (DnsFilter *f)
{
    if (!f->syncing)
        set_prop (f, "LocalViaNetwork", g_variant_new_boolean (adw_switch_row_get_active (f->local_row)));
}

static GtkWidget *
servers_page_new (DnsFilter *f)
{
    AdwPreferencesPage *page = ADW_PREFERENCES_PAGE (adw_preferences_page_new ());
    AdwPreferencesGroup *group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    AdwActionRow *row;

    f->server_rows = g_ptr_array_new ();
    f->suggest_rows = g_ptr_array_new ();

    f->network_check = GTK_CHECK_BUTTON (gtk_check_button_new ());
    f->custom_check = GTK_CHECK_BUTTON (gtk_check_button_new ());
    gtk_check_button_set_group (f->custom_check, f->network_check);
    row = ADW_ACTION_ROW (adw_action_row_new ());
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_("From the Network"));
    adw_action_row_add_prefix (row, GTK_WIDGET (f->network_check));
    adw_action_row_set_activatable_widget (row, GTK_WIDGET (f->network_check));
    adw_preferences_group_add (group, GTK_WIDGET (row));
    f->network_source_row = row;
    row = ADW_ACTION_ROW (adw_action_row_new ());
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_("Your Servers"));
    adw_action_row_set_subtitle (row, P_("Encrypted with DNS over TLS or HTTPS, or plain"));
    adw_action_row_add_prefix (row, GTK_WIDGET (f->custom_check));
    adw_action_row_set_activatable_widget (row, GTK_WIDGET (f->custom_check));
    adw_preferences_group_add (group, GTK_WIDGET (row));
    adw_preferences_page_add (page, group);

    f->custom_group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_group_set_title (f->custom_group, P_("Your Servers"));
    f->server_entry = ADW_ENTRY_ROW (adw_entry_row_new ());
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (f->server_entry), P_("Add Server"));
    adw_entry_row_set_show_apply_button (f->server_entry, TRUE);
    adw_entry_row_set_input_purpose (f->server_entry, GTK_INPUT_PURPOSE_URL);
    g_signal_connect_swapped (f->server_entry, "apply", G_CALLBACK (on_server_entry), f);
    adw_preferences_group_add (f->custom_group, GTK_WIDGET (f->server_entry));
    adw_preferences_page_add (page, f->custom_group);

    f->suggest_group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_group_set_title (f->suggest_group, P_("Suggestions"));
    adw_preferences_page_add (page, f->suggest_group);

    group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    f->local_row = ADW_SWITCH_ROW (adw_switch_row_new ());
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (f->local_row), P_("Local Names from the Network"));
    adw_action_row_set_subtitle (ADW_ACTION_ROW (f->local_row),
                                 P_("Names of the home network (like router.lan) and of a VPN go to the network's server"));
    adw_preferences_group_add (group, GTK_WIDGET (f->local_row));
    adw_preferences_group_set_description (
        group, P_("Write a server as an address (9.9.9.9), as tls://name for DNS over TLS or as https://name/dns-query "
                  "for DNS over HTTPS. On the login page of a public Wi-Fi the network's server is used until you are "
                  "signed in."));
    adw_preferences_page_add (page, group);

    g_signal_connect_swapped (f->custom_check, "notify::active", G_CALLBACK (on_source), f);
    g_signal_connect_swapped (f->local_row, "notify::active", G_CALLBACK (on_local), f);
    return subpage_new (GTK_WIDGET (page), P_("DNS Servers"), TAG_SERVERS);
}

/* ----- Settings ----- */

static const char *const block_modes[] = { "null-ip", "nxdomain", "refused" };
static const guint log_sizes[] = { 1000, 5000, 20000 };
static const guint stat_days[] = { 1, 7, 30, 90 };

static guint
index_of (const guint *values, guint n, guint value, guint fallback)
{
    for (guint i = 0; i < n; i++)
        if (values[i] == value)
            return i;
    return fallback;
}

static void
refresh_settings (DnsFilter *f)
{
    g_autofree char *mode = prop_string (f, "BlockMode");

    f->syncing = TRUE;
    for (guint i = 0; i < G_N_ELEMENTS (block_modes); i++)
        if (!g_strcmp0 (mode, block_modes[i]))
            adw_combo_row_set_selected (f->block_mode_row, i);
    adw_switch_row_set_active (f->shared_row, prop_bool (f, "SharedClients"));
    adw_switch_row_set_active (f->redirect_row, prop_bool (f, "RedirectDns"));
    adw_switch_row_set_active (f->encrypted_row, prop_bool (f, "BlockEncryptedDns"));
    adw_switch_row_set_active (f->keep_log_row, prop_bool (f, "KeepLog"));
    adw_combo_row_set_selected (f->log_size_row, index_of (log_sizes, G_N_ELEMENTS (log_sizes), prop_uint (f, "LogSize"), 1));
    adw_combo_row_set_selected (f->stats_days_row,
                                index_of (stat_days, G_N_ELEMENTS (stat_days), prop_uint (f, "StatsDays"), 1));
    adw_combo_row_set_selected (f->interval_row,
                                index_of (intervals, G_N_ELEMENTS (intervals), prop_uint (f, "UpdateInterval"), 3));
    adw_switch_row_set_active (f->mobile_row, prop_bool (f, "UpdateOnMobileData"));
    f->syncing = FALSE;
}

static void
on_setting_switch (AdwSwitchRow *row, GParamSpec *pspec, DnsFilter *f)
{
    set_prop (f, g_object_get_data (G_OBJECT (row), "property"), g_variant_new_boolean (adw_switch_row_get_active (row)));
}

static void
on_setting_combo (AdwComboRow *row, GParamSpec *pspec, DnsFilter *f)
{
    guint i = adw_combo_row_get_selected (row);

    if (row == f->block_mode_row && i < G_N_ELEMENTS (block_modes))
        set_prop (f, "BlockMode", g_variant_new_string (block_modes[i]));
    else if (row == f->log_size_row && i < G_N_ELEMENTS (log_sizes))
        set_prop (f, "LogSize", g_variant_new_uint32 (log_sizes[i]));
    else if (row == f->stats_days_row && i < G_N_ELEMENTS (stat_days))
        set_prop (f, "StatsDays", g_variant_new_uint32 (stat_days[i]));
}

static AdwSwitchRow *
setting_switch_new (DnsFilter *f, AdwPreferencesGroup *group, const char *property, const char *title,
                    const char *subtitle)
{
    AdwSwitchRow *row = ADW_SWITCH_ROW (adw_switch_row_new ());

    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), title);
    if (subtitle)
        adw_action_row_set_subtitle (ADW_ACTION_ROW (row), subtitle);
    g_object_set_data (G_OBJECT (row), "property", (gpointer) property);
    g_signal_connect (row, "notify::active", G_CALLBACK (on_setting_switch), f);
    adw_preferences_group_add (group, GTK_WIDGET (row));
    return row;
}

static AdwComboRow *
setting_combo_new (DnsFilter *f, AdwPreferencesGroup *group, const char *title, const char *const *items)
{
    AdwComboRow *row = ADW_COMBO_ROW (adw_combo_row_new ());
    g_autoptr(GtkStringList) model = gtk_string_list_new (items);

    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), title);
    adw_combo_row_set_model (row, G_LIST_MODEL (model));
    g_signal_connect (row, "notify::selected", G_CALLBACK (on_setting_combo), f);
    adw_preferences_group_add (group, GTK_WIDGET (row));
    return row;
}

static void
on_clear_log_response (AdwAlertDialog *dialog, const char *response, DnsFilter *f)
{
    if (!g_strcmp0 (response, "clear"))
        call (f, "ClearLog", NULL, on_done_refresh, NULL);
}

static void
on_clear_log (DnsFilter *f, GtkWidget *button)
{
    AdwDialog *dialog = adw_alert_dialog_new (P_("Clear Query Log?"), P_("All recorded queries are deleted."));

    adw_alert_dialog_add_responses (ADW_ALERT_DIALOG (dialog), "cancel", _("_Cancel"), "clear", P_("_Clear"), NULL);
    adw_alert_dialog_set_response_appearance (ADW_ALERT_DIALOG (dialog), "clear", ADW_RESPONSE_DESTRUCTIVE);
    g_signal_connect (dialog, "response", G_CALLBACK (on_clear_log_response), f);
    adw_dialog_present (dialog, button);
}

static GtkWidget *
settings_page_new (DnsFilter *f)
{
    AdwPreferencesPage *page = ADW_PREFERENCES_PAGE (adw_preferences_page_new ());
    AdwPreferencesGroup *group;
    GtkWidget *row;
    const char *modes[] = { "0.0.0.0", P_("Does Not Exist"), P_("Refused"), NULL };
    const char *sizes[] = { P_("1,000 Queries"), P_("5,000 Queries"), P_("20,000 Queries"), NULL };
    const char *days[] = { P_("1 Day"), P_("7 Days"), P_("30 Days"), P_("90 Days"), NULL };

    group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_group_set_title (group, P_("Blocking"));
    f->block_mode_row = setting_combo_new (f, group, P_("Answer for Blocked Names"), modes);
    adw_action_row_set_subtitle (ADW_ACTION_ROW (f->block_mode_row), P_("0.0.0.0 works best for apps"));
    adw_preferences_page_add (page, group);

    group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_group_set_title (group, P_("What Is Filtered"));
    f->shared_row = setting_switch_new (f, group, "SharedClients", P_("Hotspot and USB Devices"),
                                        P_("Devices that use the phone's hotspot or its USB connection"));
    f->redirect_row = setting_switch_new (f, group, "RedirectDns", P_("Apps with a Fixed DNS Server"),
                                          P_("Questions to another server (like 8.8.8.8) are answered by the filter"));
    f->encrypted_row = setting_switch_new (f, group, "BlockEncryptedDns", P_("Refuse DNS over TLS"),
                                           P_("Apps and devices then use plain DNS, which the filter sees"));
    adw_preferences_page_add (page, group);

    group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_group_set_title (group, P_("Query Log"));
    f->keep_log_row = setting_switch_new (f, group, "KeepLog", P_("Keep after Restart"),
                                          P_("Otherwise the log is only kept in memory"));
    f->log_size_row = setting_combo_new (f, group, P_("Keep the Last"), sizes);
    row = adw_button_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_("Clear Query Log"));
    gtk_widget_add_css_class (row, "destructive-action");
    g_signal_connect_swapped (row, "activated", G_CALLBACK (on_clear_log), f);
    adw_preferences_group_add (group, row);
    adw_preferences_page_add (page, group);

    group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_group_set_title (group, P_("Statistics"));
    f->stats_days_row = setting_combo_new (f, group, P_("Keep For"), days);
    adw_preferences_page_add (page, group);

    return subpage_new (GTK_WIDGET (page), P_("Settings"), TAG_SETTINGS);
}

/* ----- Wiring ----- */

static void
refresh_all (DnsFilter *f)
{
    refresh_main (f);
    refresh_lists (f);
    refresh_servers (f);
    refresh_settings (f);
    refresh_rules (f);
    /* Pages that are open (also those shown before the service answered) */
    if (f->log_shown)
        reload_log (f);
    if (f->detail_shown)
        load_detail (f);
    if (f->stats_shown)
        refresh_stats (f);
    if (f->who_shown)
        load_who (f);
    if (f->catalog_shown)
        call (f, "GetCatalog", NULL, on_catalog, NULL);
}

static void
on_done_refresh (DnsFilter *f, GVariant *result, GError *error, gpointer data)
{
    refresh_all (f);
}

static void
on_properties_changed (GDBusProxy *proxy, GVariant *changed, GStrv invalidated, DnsFilter *f)
{
    GVariantIter it;
    const char *name;

    g_variant_iter_init (&it, changed);
    while (g_variant_iter_next (&it, "{&sv}", &name, NULL)) {
        if (!g_strcmp0 (name, "Lists")) {
            refresh_lists (f);
            update_nav_subtitles (f);
        } else if (!g_strcmp0 (name, "RuleCount")) {
            refresh_rules (f);
            update_nav_subtitles (f);
        } else if (!g_strcmp0 (name, "Servers") || !g_strcmp0 (name, "CustomServers") ||
                   !g_strcmp0 (name, "NetworkServers") || !g_strcmp0 (name, "LocalViaNetwork")) {
            refresh_servers (f);
            update_nav_subtitles (f);
        } else if (!g_strcmp0 (name, "Status") || !g_strcmp0 (name, "Enabled") || !g_strcmp0 (name, "PausedUntil")) {
            update_status (f);
        } else {
            refresh_settings (f);
            update_placeholders (f);
        }
    }
}

static void
on_owner (DnsFilter *f)
{
    /* The service came back (started on demand): its properties are loaded anew */
    g_autofree char *owner = g_dbus_proxy_get_name_owner (f->proxy);

    if (owner)
        refresh_all (f);
}

static gboolean
on_refresh_timer (gpointer user_data)
{
    DnsFilter *f = user_data;

    call (f, "GetStats", g_variant_new ("(u)", 1), on_main_stats, NULL);
    return G_SOURCE_CONTINUE;
}

/* While a page is shown the service stays (Watch) and the numbers move */
static void
on_page_showing (AdwNavigationPage *page, DnsFilter *f)
{
    const char *tag = adw_navigation_page_get_tag (page);

    if (f->watching++ == 0) {
        call (f, "Watch", NULL, NULL, NULL);
        f->refresh = g_timeout_add_seconds (REFRESH_S, on_refresh_timer, f);
    }
    if (!g_strcmp0 (tag, TAG_MAIN))
        refresh_main (f);
    else if (!g_strcmp0 (tag, TAG_LOG)) {
        f->log_shown = TRUE;
        /* Back from a name's page: the list kept its place */
        if (f->log_keep)
            f->log_keep = FALSE;
        else
            reload_log (f);
    } else if (!g_strcmp0 (tag, TAG_NAME)) {
        f->detail_shown = TRUE;
        load_detail (f);
    } else if (!g_strcmp0 (tag, TAG_STATS_WHO)) {
        /* Also back from a name's page: a button there may have changed what it shows */
        f->stats_shown = TRUE;
        f->who_shown = TRUE;
        load_who (f);
    } else if (g_str_has_prefix (tag, TAG_STATS)) {
        /* The Statistics page and the pages of its lists */
        f->stats_shown = TRUE;
        refresh_stats (f);
    } else if (!g_strcmp0 (tag, TAG_LISTS))
        refresh_lists (f);
    else if (!g_strcmp0 (tag, TAG_ADD_LIST)) {
        f->catalog_shown = TRUE;
        call (f, "GetCatalog", NULL, on_catalog, NULL);
    }
    else if (!g_strcmp0 (tag, TAG_RULES))
        refresh_rules (f);
    else if (!g_strcmp0 (tag, TAG_SERVERS))
        refresh_servers (f);
    else if (!g_strcmp0 (tag, TAG_SETTINGS))
        refresh_settings (f);
}

static void
on_page_hidden (AdwNavigationPage *page, DnsFilter *f)
{
    const char *tag = adw_navigation_page_get_tag (page);

    if (!g_strcmp0 (tag, TAG_LOG)) {
        /* Under a name's page it stays, and live */
        if (!f->log_keep) {
            f->log_shown = FALSE;
            clear_log_rows (f);
        }
    } else if (!g_strcmp0 (tag, TAG_NAME)) {
        GtkWidget *view = gtk_widget_get_ancestor (GTK_WIDGET (page), ADW_TYPE_NAVIGATION_VIEW);

        f->detail_shown = FALSE;
        clear_detail (f);
        /* Not back to the list (further back at once): the list goes too */
        if (f->log_keep && (!view || adw_navigation_view_get_visible_page (ADW_NAVIGATION_VIEW (view)) !=
                                         ADW_NAVIGATION_PAGE (f->log_page))) {
            f->log_keep = FALSE;
            f->log_shown = FALSE;
            clear_log_rows (f);
        }
    } else if (g_str_has_prefix (tag, TAG_STATS)) {
        GtkWidget *view = gtk_widget_get_ancestor (GTK_WIDGET (page), ADW_TYPE_NAVIGATION_VIEW);

        /* Its rows stay until it is shown again (one of them may be what was just tapped); answers
         * still on their way are dropped */
        if (!g_strcmp0 (tag, TAG_STATS_WHO)) {
            f->who_shown = FALSE;
            f->who_generation++;
        }
        AdwNavigationPage *visible = view ? adw_navigation_view_get_visible_page (ADW_NAVIGATION_VIEW (view)) : NULL;

        /* Still shown when one went to a list's page or back from it */
        if (!visible || !g_str_has_prefix (adw_navigation_page_get_tag (visible) ?: "", TAG_STATS))
            f->stats_shown = FALSE;
    } else if (!g_strcmp0 (tag, TAG_ADD_LIST)) {
        f->catalog_shown = FALSE;
    }
    if (f->watching && --f->watching == 0) {
        call (f, "Unwatch", NULL, NULL, NULL);
        g_clear_handle_id (&f->refresh, g_source_remove);
    }
}

static void
add_page (DnsFilter *f, GtkWidget *page)
{
    f->ref++;
    g_object_set_data_full (G_OBJECT (page), "plinius-dnsfilter", f, (GDestroyNotify) dns_filter_unref);
    cc_panel_add_subpage (f->panel, adw_navigation_page_get_tag (ADW_NAVIGATION_PAGE (page)), ADW_NAVIGATION_PAGE (page));
    g_signal_connect (page, "showing", G_CALLBACK (on_page_showing), f);
    g_signal_connect (page, "hidden", G_CALLBACK (on_page_hidden), f);
}

static void
dns_filter_unref (DnsFilter *f)
{
    if (--f->ref > 0)
        return;
    g_cancellable_cancel (f->cancel);
    g_clear_object (&f->cancel);
    g_clear_handle_id (&f->refresh, g_source_remove);
    if (f->proxy) {
        g_signal_handlers_disconnect_by_data (f->proxy, f);
        g_clear_object (&f->proxy);
    }
    g_clear_pointer (&f->log_rows, g_ptr_array_unref);
    g_clear_pointer (&f->log_names, g_hash_table_unref);
    g_clear_pointer (&f->detail_names, g_ptr_array_unref);
    g_clear_pointer (&f->detail_groups, g_hash_table_unref);
    g_clear_pointer (&f->detail_name, g_free);
    g_clear_pointer (&f->stats_rows, g_ptr_array_unref);
    g_clear_pointer (&f->stats_names, g_ptr_array_unref);
    g_clear_pointer (&f->who_rows, g_ptr_array_unref);
    g_clear_pointer (&f->who_key, g_free);
    g_clear_pointer (&f->who_title, g_free);
    g_clear_pointer (&f->list_rows, g_ptr_array_unref);
    g_clear_pointer (&f->catalog_rows, g_ptr_array_unref);
    g_clear_pointer (&f->rule_rows, g_ptr_array_unref);
    g_clear_pointer (&f->server_rows, g_ptr_array_unref);
    g_clear_pointer (&f->suggest_rows, g_ptr_array_unref);
    g_clear_pointer (&f->chart_total, g_array_unref);
    g_clear_pointer (&f->chart_blocked, g_array_unref);
    g_clear_pointer (&f->stats_total, g_array_unref);
    g_clear_pointer (&f->stats_blocked, g_array_unref);
    g_free (f);
}

static void
on_proxy (GObject *source, GAsyncResult *res, gpointer user_data)
{
    DnsFilter *f = user_data;
    g_autoptr(GError) error = NULL;
    GDBusProxy *proxy = g_dbus_proxy_new_for_bus_finish (res, &error);

    if (!proxy) {
        if (!g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
            g_warning ("plinius-dnsfilter: %s", error->message);
        dns_filter_unref (f);
        return;
    }
    if (g_cancellable_is_cancelled (f->cancel)) {
        g_object_unref (proxy);
        dns_filter_unref (f);
        return;
    }
    f->proxy = proxy;
    g_signal_connect (f->proxy, "g-properties-changed", G_CALLBACK (on_properties_changed), f);
    g_signal_connect (f->proxy, "g-signal", G_CALLBACK (on_signal), f);
    g_signal_connect_swapped (f->proxy, "notify::g-name-owner", G_CALLBACK (on_owner), f);
    refresh_all (f);
    if (f->watching)
        call (f, "Watch", NULL, NULL, NULL);
    dns_filter_unref (f);
}

static void
on_mobile (DnsFilter *f)
{
    if (!f->syncing)
        set_prop (f, "UpdateOnMobileData", g_variant_new_boolean (adw_switch_row_get_active (f->mobile_row)));
}

static void
install_css (void)
{
    static gboolean done;
    g_autoptr(GtkCssProvider) css = NULL;

    if (done)
        return;
    done = TRUE;
    css = gtk_css_provider_new ();
    /* A blocked name's row: its title in the error color. An app's expander row on a name's page is
     * red when the app is blocked; its queries inside are red only when they were blocked. Unblock
     * is green text, as Block is red text. */
    gtk_css_provider_load_from_string (css, "row.plinius-dns-blocked .title { color: var(--error-color); }"
                                            "row.expander.plinius-dns-blocked list.nested .title { color: inherit; }"
                                            "row.expander list.nested row.plinius-dns-blocked .title"
                                            " { color: var(--error-color); }"
                                            "button.flat.plinius-dns-unblock { color: var(--success-color); }");
    gtk_style_context_add_provider_for_display (gdk_display_get_default (), GTK_STYLE_PROVIDER (css),
                                                GTK_STYLE_PROVIDER_PRIORITY_APPLICATION);
}

GtkWidget *
cc_plinius_dnsfilter_group_new (CcPanel *panel)
{
    DnsFilter *f;
    GtkWidget *group;

    if (!g_file_test (SERVICE, G_FILE_TEST_IS_EXECUTABLE))
        return NULL;
    f = g_new0 (DnsFilter, 1);
    f->panel = panel;
    f->cancel = g_cancellable_new ();
    install_css ();

    add_page (f, main_page_new (f));
    add_page (f, log_page_new (f));
    add_page (f, detail_page_new (f));
    add_page (f, stats_page_new (f));
    add_page (f, stats_list_page_new (P_("Most Asked"), TAG_STATS_ASKED, &f->top_group));
    add_page (f, stats_list_page_new (P_("Most Blocked"), TAG_STATS_BLOCKED, &f->blocked_group));
    add_page (f, stats_list_page_new (P_("Apps"), TAG_STATS_APPS, &f->apps_group));
    add_page (f, stats_list_page_new (P_("Devices"), TAG_STATS_DEVICES, &f->clients_group));
    add_page (f, stats_list_page_new (P_("DNS Servers"), TAG_STATS_SERVERS, &f->servers_group));
    add_page (f, who_page_new (f));
    add_page (f, lists_page_new (f));
    add_page (f, add_list_page_new (f));
    add_page (f, rules_page_new (f));
    add_page (f, servers_page_new (f));
    add_page (f, settings_page_new (f));
    g_signal_connect_swapped (f->mobile_row, "notify::active", G_CALLBACK (on_mobile), f);

    /* A group of its own at the top, titled like the others */
    group = adw_preferences_group_new ();
    adw_preferences_group_set_title (ADW_PREFERENCES_GROUP (group), P_("DNS Filter"));
    f->network_row = nav_row_new (P_("DNS _Filter"), "security-high-symbolic", TAG_MAIN);
    adw_action_row_set_subtitle (ADW_ACTION_ROW (f->network_row), P_("Filter ads, trackers and malware"));
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), f->network_row);
    f->ref++;
    g_object_set_data_full (G_OBJECT (group), "plinius-dnsfilter", f, (GDestroyNotify) dns_filter_unref);

    /* Properties come with the proxy; the bus starts the service if it is not running */
    f->ref++;
    g_dbus_proxy_new_for_bus (G_BUS_TYPE_SYSTEM, G_DBUS_PROXY_FLAGS_NONE, NULL, BUS_NAME, OBJECT_PATH, INTERFACE,
                              f->cancel, on_proxy, f);
    return group;
}
