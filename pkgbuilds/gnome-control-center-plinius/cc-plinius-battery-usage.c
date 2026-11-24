/*
 * cc-plinius-battery-usage.c
 *
 * Plinius: the Battery page's own pages, each behind a row after the battery level.
 * - "Battery Usage": the estimated processor energy of each app and system service over the
 *   last 24 hours or 7 days, recorded by plinius-energy in RUN_FILE, beside the screen-on time
 *   and what the battery measurably lost while unplugged. Shows only with plinius-energy.
 * - "Battery Condition": the condition group of cc-plinius-battery.c.
 * Our own strings use the gettext domain plinius-control-center (not GNOME's po/).
 *
 * SPDX-License-Identifier: MIT
 */

#undef G_LOG_DOMAIN
#define G_LOG_DOMAIN "cc-plinius-battery-usage"

#include <gio/gdesktopappinfo.h>
#include <glib/gi18n.h>

#include "cc-list-row.h"
#include "cc-plinius-battery-usage.h"

#define PLINIUS_DOMAIN "plinius-control-center"
#define P_(String) g_dgettext (PLINIUS_DOMAIN, String)

#define ENERGY_TOOL "/usr/bin/plinius-energy"
#define RUN_FILE "/run/plinius-energy/usage"
#define USAGE_TAG "plinius-battery-usage"
#define CONDITION_TAG "plinius-battery-condition"

#define REFRESH_S 60
#define MAX_SYSTEM_ROWS 20
#define MAX_ANDROID_ROWS 10
/* For the energy in mAh when the battery's full energy is unknown */
#define NOMINAL_VOLTS 3.85

/* ----- What a record line belongs to ----- */

typedef struct {
    char *title;
    char *detail; /* the process behind a friendly title */
    GIcon *icon;
    gboolean is_app;
    double mj, ms, off_ms;
    GHashTable *parts; /* Android: process → Item */
} Item;

static void
item_free (Item *item)
{
    g_free (item->title);
    g_free (item->detail);
    g_clear_object (&item->icon);
    g_clear_pointer (&item->parts, g_hash_table_unref);
    g_free (item);
}

/* Processes better known by what they do */
static const struct {
    const char *process, *id, *title;
} friendly[] = {
    { "phosh", "shell", N_("Shell") },
    { "phoc", "compositor", N_("Compositor") },
    { "phosh-osk-stevia", "keyboard", N_("On-Screen Keyboard") },
    { "pipewire", "sound", N_("Sound") },
    { "pipewire-pulse", "sound", N_("Sound") },
    { "wireplumber", "sound", N_("Sound") },
    { "audio-hal-bridge", "sound", N_("Sound") },
    { "NetworkManager", "network", N_("Network") },
    { "wpa_supplicant", "network", N_("Network") },
    { "systemd-journald", "journal", N_("System Log") },
};

typedef struct {
    AdwNavigationPage *page;
    GtkWidget *condition_group;
    AdwToggleGroup *range;
    AdwPreferencesGroup *summary_group, *apps_group, *system_group;
    AdwActionRow *empty_row, *screen_row, *battery_row, *processor_row;
    GtkLabel *screen_value, *battery_value, *processor_value;
    GPtrArray *rows; /* (group, row) pairs of the last refresh */
    GHashTable *executables; /* program name → GAppInfo of a shown app */
    guint timer;
} UsagePage;

static void
usage_page_free (UsagePage *self)
{
    g_clear_handle_id (&self->timer, g_source_remove);
    g_clear_pointer (&self->rows, g_ptr_array_unref);
    g_clear_pointer (&self->executables, g_hash_table_unref);
    g_free (self);
}

/* Shown apps by their program, to find the app behind a service (gnome-calls → Calls) */
static GHashTable *
executables_new (void)
{
    static const char *launchers[] = { "flatpak", "env", "sh", "bash", "python3", "gjs", "snap", NULL };
    GHashTable *table = g_hash_table_new_full (g_str_hash, g_str_equal, g_free, g_object_unref);
    GList *apps = g_app_info_get_all ();

    for (GList *l = apps; l; l = l->next) {
        const char *exe = g_app_info_get_executable (l->data);
        g_autofree char *name = exe ? g_path_get_basename (exe) : NULL;

        if (name == NULL || !g_app_info_should_show (l->data) || g_strv_contains (launchers, name) ||
            g_hash_table_contains (table, name))
            continue;
        g_hash_table_insert (table, g_steal_pointer (&name), g_object_ref (l->data));
    }
    g_list_free_full (apps, g_object_unref);
    return table;
}

static Item *
item_get (GHashTable *items, const char *id, const char *title, GIcon *icon, gboolean is_app)
{
    Item *item = g_hash_table_lookup (items, id);

    if (item == NULL) {
        item = g_new0 (Item, 1);
        item->title = g_strdup (title);
        item->icon = icon ? g_object_ref (icon) : NULL;
        item->is_app = is_app;
        g_hash_table_insert (items, g_strdup (id), item);
    }
    return item;
}

static Item *
app_item (GHashTable *items, GAppInfo *info)
{
    const char *id = g_app_info_get_id (info);

    return item_get (items, id ? id : g_app_info_get_display_name (info), g_app_info_get_display_name (info),
                     g_app_info_get_icon (info), TRUE);
}

/* The item of record key @key (app/<desktop id>, user/<unit>, system/<unit>, android/<process>,
 * kernel) with the process name @name */
static Item *
resolve (UsagePage *self, GHashTable *items, const char *key, const char *name)
{
    g_autoptr(GIcon) icon = NULL;
    const char *unit = strchr (key, '/') ? strchr (key, '/') + 1 : key;
    GAppInfo *by_exe = name && *name ? g_hash_table_lookup (self->executables, name) : NULL;
    Item *item;

    if (g_str_equal (key, "kernel")) {
        icon = g_themed_icon_new ("applications-system-symbolic");
        return item_get (items, key, P_("Linux Kernel"), icon, FALSE);
    }

    if (g_str_has_prefix (key, "android/")) {
        icon = g_themed_icon_new ("phone-symbolic");
        item = item_get (items, "android", P_("Android Services"), icon, FALSE);
        if (item->parts == NULL)
            item->parts = g_hash_table_new_full (g_str_hash, g_str_equal, g_free, (GDestroyNotify) item_free);
        return item;
    }

    if (g_str_has_prefix (key, "app/")) {
        g_autofree char *desktop = g_strconcat (unit, ".desktop", NULL);
        g_autoptr(GDesktopAppInfo) info = g_desktop_app_info_new (desktop);

        if (info && g_app_info_should_show (G_APP_INFO (info)))
            return app_item (items, G_APP_INFO (info));
        if (by_exe)
            return app_item (items, by_exe);
        icon = g_themed_icon_new ("application-x-executable-symbolic");
        if (info)
            return item_get (items, key, g_app_info_get_display_name (G_APP_INFO (info)), icon, FALSE);
        return item_get (items, key, name && *name ? name : unit, icon, FALSE);
    }

    /* A slice keeps the time of what ended inside it before the record knew it */
    if (g_str_has_suffix (unit, ".slice")) {
        icon = g_themed_icon_new ("process-stop-symbolic");
        return item_get (items, "ended", P_("Ended Programs"), icon, FALSE);
    }

    /* A service of an app, like the Calls daemon */
    if (g_str_has_prefix (key, "user/") && by_exe)
        return app_item (items, by_exe);

    icon = g_themed_icon_new ("application-x-executable-symbolic");
    for (guint i = 0; name && i < G_N_ELEMENTS (friendly); i++) {
        if (g_str_equal (name, friendly[i].process)) {
            item = item_get (items, friendly[i].id, P_(friendly[i].title), icon, FALSE);
            if (item->detail == NULL)
                item->detail = g_strdup (name);
            return item;
        }
    }
    if (name && *name)
        return item_get (items, key, name, icon, FALSE);
    {
        g_autofree char *title = g_strndup (unit, strrchr (unit, '.') ? strrchr (unit, '.') - unit : strlen (unit));
        return item_get (items, key, title, icon, FALSE);
    }
}

/* ----- Formatting ----- */

static char *
format_duration (double ms)
{
    guint64 s = ms / 1000 + 0.5;

    if (s < 60)
        /* Translators: processor or screen time in seconds */
        return g_strdup_printf (P_("%u s"), (guint) s);
    if (s < 3600)
        /* Translators: processor or screen time in minutes */
        return g_strdup_printf (P_("%u min"), (guint) (s / 60));
    /* Translators: processor or screen time in hours and minutes */
    return g_strdup_printf (P_("%u h %u min"), (guint) (s / 3600), (guint) (s % 3600 / 60));
}

static char *
format_percent (double percent)
{
    if (percent > 0 && percent < 0.1)
        return g_strdup (P_("< 0.1 %"));
    if (percent < 10)
        return g_strdup_printf ("%.1f %%", percent);
    return g_strdup_printf ("%.0f %%", percent);
}

/* Energy as a share of a full battery, or in mAh when the battery's energy is unknown */
static char *
format_energy (UsagePage *self, double mj)
{
    double *full_wh = self->condition_group ? g_object_get_data (G_OBJECT (self->condition_group), "energy-full-wh") : NULL;

    if (full_wh && *full_wh > 0) {
        g_autofree char *percent = format_percent (mj / (*full_wh * 3600 * 1000) * 100);
        /* Translators: share of a full battery charge, e.g. "1.2 % of a charge" */
        return g_strdup_printf (P_("%s of a charge"), percent);
    }
    return g_strdup_printf ("%.0f mAh", mj / 3600 / NOMINAL_VOLTS);
}

static char *
item_subtitle (Item *item)
{
    g_autofree char *time = format_duration (item->ms);
    g_autofree char *text = NULL;

    if (item->off_ms >= item->ms * 0.99 && item->off_ms >= 1000) {
        /* Translators: processor time, all of it with the screen off, e.g. "12 min with the screen off" */
        text = g_strdup_printf (P_("%s with the screen off"), time);
    } else if (item->off_ms >= 1000) {
        g_autofree char *off = format_duration (item->off_ms);
        /* Translators: processor time, of it with the screen off, e.g. "12 min, 3 min with the screen off" */
        text = g_strdup_printf (P_("%s, %s with the screen off"), time, off);
    } else {
        text = g_strdup (time);
    }
    if (item->detail)
        return g_strdup_printf ("%s · %s", item->detail, text);
    return g_steal_pointer (&text);
}

/* ----- Rows ----- */

static void
add_share_suffix (GtkWidget *row, double share, double top_share, gboolean expander)
{
    GtkWidget *bar = gtk_level_bar_new_for_interval (0, 1);
    g_autofree char *text = format_percent (share * 100);
    GtkWidget *label = gtk_label_new (text);

    gtk_level_bar_set_value (GTK_LEVEL_BAR (bar), top_share > 0 ? share / top_share : 0);
    /* No colors for levels: it is a share, not a warning */
    gtk_level_bar_remove_offset_value (GTK_LEVEL_BAR (bar), GTK_LEVEL_BAR_OFFSET_LOW);
    gtk_level_bar_remove_offset_value (GTK_LEVEL_BAR (bar), GTK_LEVEL_BAR_OFFSET_HIGH);
    gtk_level_bar_remove_offset_value (GTK_LEVEL_BAR (bar), GTK_LEVEL_BAR_OFFSET_FULL);
    gtk_widget_set_size_request (bar, 48, -1);
    gtk_widget_set_valign (bar, GTK_ALIGN_CENTER);
    gtk_label_set_width_chars (GTK_LABEL (label), 6);
    gtk_label_set_xalign (GTK_LABEL (label), 1);
    gtk_widget_add_css_class (label, "numeric");
    gtk_widget_add_css_class (label, "dim-label");
    if (expander) {
        /* An expander row shows its suffixes in the opposite order */
        adw_expander_row_add_suffix (ADW_EXPANDER_ROW (row), label);
        adw_expander_row_add_suffix (ADW_EXPANDER_ROW (row), bar);
    } else {
        adw_action_row_add_suffix (ADW_ACTION_ROW (row), bar);
        adw_action_row_add_suffix (ADW_ACTION_ROW (row), label);
    }
}

static GtkWidget *
icon_new (Item *item)
{
    GtkWidget *image = item->icon ? gtk_image_new_from_gicon (item->icon)
                                  : gtk_image_new_from_icon_name ("application-x-executable-symbolic");

    if (item->is_app) {
        gtk_image_set_pixel_size (GTK_IMAGE (image), 32);
        gtk_widget_add_css_class (image, "lowres-icon");
    }
    return image;
}

static void
add_row (UsagePage *self, AdwPreferencesGroup *group, GtkWidget *row)
{
    adw_preferences_group_add (group, row);
    g_ptr_array_add (self->rows, group);
    g_ptr_array_add (self->rows, row);
}

static gint
compare_items (gconstpointer a, gconstpointer b)
{
    const Item *x = *(Item **) a, *y = *(Item **) b;

    return x->mj > y->mj ? -1 : x->mj < y->mj;
}

/* The items of @table sorted by energy, largest first */
static GPtrArray *
sorted_items (GHashTable *table)
{
    GPtrArray *list = g_ptr_array_new ();
    GHashTableIter iter;
    gpointer value;

    g_hash_table_iter_init (&iter, table);
    while (g_hash_table_iter_next (&iter, NULL, &value))
        if (((Item *) value)->mj > 0)
            g_ptr_array_add (list, value);
    g_ptr_array_sort (list, compare_items);
    return list;
}

static GtkWidget *
item_row_new (Item *item, double total_mj, double top_mj)
{
    g_autofree char *subtitle = item_subtitle (item);
    GtkWidget *row;

    if (item->parts) {
        g_autoptr(GPtrArray) parts = sorted_items (item->parts);

        row = adw_expander_row_new ();
        adw_expander_row_set_subtitle (ADW_EXPANDER_ROW (row), subtitle);
        adw_expander_row_add_prefix (ADW_EXPANDER_ROW (row), icon_new (item));
        add_share_suffix (row, item->mj / total_mj, top_mj / total_mj, TRUE);
        for (guint i = 0; i < parts->len && i < MAX_ANDROID_ROWS; i++) {
            Item *part = g_ptr_array_index (parts, i);
            g_autofree char *part_subtitle = item_subtitle (part);
            GtkWidget *child = adw_action_row_new ();

            adw_preferences_row_set_use_markup (ADW_PREFERENCES_ROW (child), FALSE);
            adw_preferences_row_set_title (ADW_PREFERENCES_ROW (child), part->title);
            adw_action_row_set_subtitle (ADW_ACTION_ROW (child), part_subtitle);
            add_share_suffix (child, part->mj / total_mj, top_mj / total_mj, FALSE);
            adw_expander_row_add_row (ADW_EXPANDER_ROW (row), child);
        }
    } else {
        row = adw_action_row_new ();
        adw_action_row_set_subtitle (ADW_ACTION_ROW (row), subtitle);
        adw_action_row_add_prefix (ADW_ACTION_ROW (row), icon_new (item));
        add_share_suffix (row, item->mj / total_mj, top_mj / total_mj, FALSE);
    }
    /* A process can name itself anything */
    adw_preferences_row_set_use_markup (ADW_PREFERENCES_ROW (row), FALSE);
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), item->title);
    return row;
}

/* ----- Reading the record ----- */

static void
refresh (UsagePage *self)
{
    g_autoptr(GHashTable) items = g_hash_table_new_full (g_str_hash, g_str_equal, g_free, (GDestroyNotify) item_free);
    g_autoptr(GPtrArray) apps = NULL, system = NULL;
    g_autoptr(GPtrArray) sorted = NULL;
    g_autofree char *text = NULL;
    g_auto(GStrv) lines = NULL;
    gint64 now_hour = g_get_real_time () / G_USEC_PER_SEC / 3600;
    gboolean week = g_strcmp0 (adw_toggle_group_get_active_name (self->range), "week") == 0;
    gint64 first_hour = now_hour - (week ? 7 * 24 : 24) + 1;
    double screen_ms = 0, drain_mj = 0, drain_ms = 0, total_mj = 0, top_mj = 0;
    gboolean have_data;

    for (guint i = 0; i + 1 < self->rows->len; i += 2)
        adw_preferences_group_remove (g_ptr_array_index (self->rows, i), g_ptr_array_index (self->rows, i + 1));
    g_ptr_array_set_size (self->rows, 0);
    if (self->executables == NULL)
        self->executables = executables_new ();

    have_data = g_file_get_contents (RUN_FILE, &text, NULL, NULL);
    lines = g_strsplit (have_data ? text : "", "\n", -1);
    for (char **line = lines; *line; line++) {
        g_auto(GStrv) f = NULL;
        double on_mj, off_mj, on_ms, off_ms;
        Item *item;

        if (**line == '#' || **line == '\0')
            continue;
        f = g_strsplit (*line, "\t", 7);
        if (g_strv_length (f) < 6 || g_ascii_strtoll (f[0], NULL, 10) < first_hour)
            continue;
        on_mj = g_ascii_strtod (f[2], NULL);
        off_mj = g_ascii_strtod (f[3], NULL);
        on_ms = g_ascii_strtod (f[4], NULL);
        off_ms = g_ascii_strtod (f[5], NULL);

        if (g_str_equal (f[1], "@screen")) {
            screen_ms += on_ms;
            continue;
        }
        if (g_str_equal (f[1], "@battery")) {
            drain_mj += on_mj + off_mj;
            drain_ms += on_ms + off_ms;
            continue;
        }
        item = resolve (self, items, f[1], f[6]);
        item->mj += on_mj + off_mj;
        item->ms += on_ms + off_ms;
        item->off_ms += off_ms;
        total_mj += on_mj + off_mj;
        if (item->parts) {
            /* The process is in the key */
            const char *process = f[1] + strlen ("android/");
            Item *part = item_get (item->parts, process, process, NULL, FALSE);

            part->mj += on_mj + off_mj;
            part->ms += on_ms + off_ms;
            part->off_ms += off_ms;
        }
    }

    gtk_widget_set_visible (GTK_WIDGET (self->empty_row), total_mj <= 0);
    gtk_widget_set_visible (GTK_WIDGET (self->screen_row), total_mj > 0);
    gtk_widget_set_visible (GTK_WIDGET (self->processor_row), total_mj > 0);
    gtk_widget_set_visible (GTK_WIDGET (self->battery_row), total_mj > 0);
    gtk_widget_set_visible (GTK_WIDGET (self->apps_group), FALSE);
    gtk_widget_set_visible (GTK_WIDGET (self->system_group), FALSE);
    if (total_mj <= 0)
        return;

    {
        g_autofree char *screen = format_duration (screen_ms);
        g_autofree char *processor = format_energy (self, total_mj);

        gtk_label_set_text (self->screen_value, screen);
        gtk_label_set_text (self->processor_value, processor);
    }
    if (drain_ms >= 60 * 1000) {
        g_autofree char *drain = format_energy (self, drain_mj);
        g_autofree char *during = format_duration (drain_ms);
        /* Translators: how long the phone ran on its battery, e.g. "Measured over 5 h 10 min unplugged" */
        g_autofree char *subtitle = g_strdup_printf (P_("Measured over %s unplugged"), during);

        gtk_label_set_text (self->battery_value, drain);
        adw_action_row_set_subtitle (self->battery_row, subtitle);
    } else {
        gtk_label_set_text (self->battery_value, "");
        adw_action_row_set_subtitle (self->battery_row, P_("Not unplugged in this time"));
    }

    sorted = sorted_items (items);
    top_mj = sorted->len > 0 ? ((Item *) g_ptr_array_index (sorted, 0))->mj : 0;
    apps = g_ptr_array_new ();
    system = g_ptr_array_new ();
    for (guint i = 0; i < sorted->len; i++) {
        Item *item = g_ptr_array_index (sorted, i);

        /* Under a second of processor time is not worth a row (apps opened once, idle clocks) */
        if (item->ms < 1000)
            continue;
        g_ptr_array_add (item->is_app ? apps : system, item);
    }
    for (guint i = 0; i < apps->len; i++)
        add_row (self, self->apps_group, item_row_new (g_ptr_array_index (apps, i), total_mj, top_mj));
    for (guint i = 0; i < system->len && i < MAX_SYSTEM_ROWS; i++)
        add_row (self, self->system_group, item_row_new (g_ptr_array_index (system, i), total_mj, top_mj));
    gtk_widget_set_visible (GTK_WIDGET (self->apps_group), apps->len > 0);
    gtk_widget_set_visible (GTK_WIDGET (self->system_group), system->len > 0);
}

static gboolean
on_timer (gpointer data)
{
    refresh (data);
    return G_SOURCE_CONTINUE;
}

static void
on_showing (UsagePage *self)
{
    refresh (self);
    g_clear_handle_id (&self->timer, g_source_remove);
    self->timer = g_timeout_add_seconds (REFRESH_S, on_timer, self);
}

static void
on_hidden (UsagePage *self)
{
    g_clear_handle_id (&self->timer, g_source_remove);
    /* Apps installed meanwhile are found the next time */
    g_clear_pointer (&self->executables, g_hash_table_unref);
}

/* ----- Pages ----- */

static AdwActionRow *
value_row_new (AdwPreferencesGroup *group, const char *title, GtkLabel **value)
{
    GtkWidget *row = adw_action_row_new ();
    GtkWidget *label = gtk_label_new (NULL);

    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), title);
    gtk_widget_add_css_class (label, "dim-label");
    gtk_widget_add_css_class (label, "numeric");
    adw_action_row_add_suffix (ADW_ACTION_ROW (row), label);
    adw_preferences_group_add (group, row);
    *value = GTK_LABEL (label);
    return ADW_ACTION_ROW (row);
}

static GtkWidget *
subpage_new (GtkWidget *content, const char *title, const char *tag)
{
    GtkWidget *toolbar = adw_toolbar_view_new ();

    adw_toolbar_view_add_top_bar (ADW_TOOLBAR_VIEW (toolbar), adw_header_bar_new ());
    adw_toolbar_view_set_content (ADW_TOOLBAR_VIEW (toolbar), content);
    return GTK_WIDGET (adw_navigation_page_new_with_tag (toolbar, title, tag));
}

static GtkWidget *
usage_page_new (GtkWidget *condition_group)
{
    UsagePage *self = g_new0 (UsagePage, 1);
    AdwPreferencesPage *prefs = ADW_PREFERENCES_PAGE (adw_preferences_page_new ());
    AdwPreferencesGroup *group;
    AdwToggle *toggle;
    GtkWidget *row;

    self->condition_group = condition_group;
    self->rows = g_ptr_array_new ();

    group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    self->range = ADW_TOGGLE_GROUP (adw_toggle_group_new ());
    toggle = adw_toggle_new ();
    adw_toggle_set_name (toggle, "day");
    adw_toggle_set_label (toggle, P_("Last 24 Hours"));
    adw_toggle_group_add (self->range, toggle);
    toggle = adw_toggle_new ();
    adw_toggle_set_name (toggle, "week");
    adw_toggle_set_label (toggle, P_("Last 7 Days"));
    adw_toggle_group_add (self->range, toggle);
    adw_toggle_group_set_active_name (self->range, "day");
    adw_toggle_group_set_homogeneous (self->range, TRUE);
    adw_preferences_group_add (group, GTK_WIDGET (self->range));
    adw_preferences_page_add (prefs, group);

    self->summary_group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_page_add (prefs, self->summary_group);
    row = adw_action_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_("Nothing Recorded Yet"));
    adw_action_row_set_subtitle (ADW_ACTION_ROW (row),
                                 P_("The record starts with the phone, usage shows here within a minute."));
    adw_preferences_group_add (self->summary_group, row);
    self->empty_row = ADW_ACTION_ROW (row);
    self->screen_row = value_row_new (self->summary_group, P_("Screen On"), &self->screen_value);
    self->battery_row = value_row_new (self->summary_group, P_("Battery Used"), &self->battery_value);
    self->processor_row = value_row_new (self->summary_group, P_("Processor"), &self->processor_value);
    adw_action_row_set_subtitle (self->processor_row, P_("Estimated, all apps and services together"));

    self->apps_group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_group_set_title (self->apps_group, P_("Apps"));
    adw_preferences_group_set_description (self->apps_group, P_("Share of the processor's energy"));
    adw_preferences_page_add (prefs, self->apps_group);
    self->system_group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_group_set_title (self->system_group, P_("System"));
    adw_preferences_page_add (prefs, self->system_group);

    group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_group_set_description (
        group, P_("Estimated from the time each program ran at each processor speed and the processor's power at that "
                  "speed. The screen, the radios and the graphics are not part of the app values, so together they "
                  "come to less than the battery used."));
    adw_preferences_page_add (prefs, group);

    self->page = ADW_NAVIGATION_PAGE (subpage_new (GTK_WIDGET (prefs), P_("Battery Usage"), USAGE_TAG));
    g_object_set_data_full (G_OBJECT (self->page), USAGE_TAG, self, (GDestroyNotify) usage_page_free);
    g_signal_connect_swapped (self->page, "showing", G_CALLBACK (on_showing), self);
    g_signal_connect_swapped (self->page, "hidden", G_CALLBACK (on_hidden), self);
    g_signal_connect_swapped (self->range, "notify::active", G_CALLBACK (refresh), self);
    return GTK_WIDGET (self->page);
}

static GtkWidget *
nav_row_new (const char *title, const char *subtitle, const char *icon, const char *tag)
{
    GtkWidget *row = g_object_new (CC_TYPE_LIST_ROW, "show-arrow", TRUE, "use-underline", TRUE, "icon-name", icon,
                                   "action-name", "navigation.push", NULL);

    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), title);
    adw_action_row_set_subtitle (ADW_ACTION_ROW (row), subtitle);
    gtk_actionable_set_action_target (GTK_ACTIONABLE (row), "s", tag);
    return row;
}

GtkWidget *
cc_plinius_battery_pages_new (CcPanel *panel, GtkWidget *condition_group)
{
    GtkWidget *group = adw_preferences_group_new ();
    AdwPreferencesPage *prefs = ADW_PREFERENCES_PAGE (adw_preferences_page_new ());
    GtkWidget *row;

    if (g_file_test (ENERGY_TOOL, G_FILE_TEST_IS_EXECUTABLE)) {
        cc_panel_add_subpage (panel, USAGE_TAG, ADW_NAVIGATION_PAGE (usage_page_new (condition_group)));
        row = nav_row_new (P_("Battery _Usage"), P_("Which apps use the most energy"), "org.gnome.Settings-power-symbolic",
                           USAGE_TAG);
        adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), row);
    }

    /* The page has the title */
    adw_preferences_group_set_title (ADW_PREFERENCES_GROUP (condition_group), "");
    adw_preferences_page_add (prefs, ADW_PREFERENCES_GROUP (condition_group));
    cc_panel_add_subpage (panel, CONDITION_TAG,
                          ADW_NAVIGATION_PAGE (subpage_new (GTK_WIDGET (prefs), P_("Battery Condition"), CONDITION_TAG)));
    row = nav_row_new (P_("Battery _Condition"), P_("Health, charge cycles, temperature"), "battery-level-80-symbolic",
                       CONDITION_TAG);
    /* Only with a battery, like the group itself */
    g_object_bind_property (condition_group, "visible", row, "visible", G_BINDING_SYNC_CREATE);
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), row);
    return group;
}
