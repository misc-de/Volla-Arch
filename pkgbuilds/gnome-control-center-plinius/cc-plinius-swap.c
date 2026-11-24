/*
 * cc-plinius-swap.c
 *
 * Plinius: "Memory and Swap" in System. The swap file with its switch and size, how readily the
 * kernel swaps (vm.swappiness), what RAM and swap hold right now, the largest processes and a
 * button that moves everything from swap back to RAM.
 *
 * Changes run "pkexec plinius-swap-set ..." (plinius-swap package, no PIN, see
 * org.plinius.swap.policy). What the page shows comes straight from the kernel (/proc/meminfo,
 * /proc/swaps, /proc/sys/vm/swappiness) and /etc/plinius-swap.conf, so it tells what applies, also
 * after a refused change.
 *
 * Besides the file there is Android's zram (compressed RAM), which the kernel fills first. It
 * shows as a second swap area and is emptied with the file, the switch only covers the file.
 *
 * Nothing of this shows without plinius-swap. Our own strings use the gettext domain
 * plinius-control-center (not GNOME's po/).
 *
 * SPDX-License-Identifier: MIT
 */

#undef G_LOG_DOMAIN
#define G_LOG_DOMAIN "cc-plinius-swap"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/statvfs.h>

#include "cc-plinius-swap.h"

#define PLINIUS_DOMAIN "plinius-control-center"
#define P_(String) g_dgettext (PLINIUS_DOMAIN, String)

#define PAGE_TAG   "plinius-swap"
#define SET_TOOL   "/usr/lib/plinius-swap/plinius-swap-set"
#define CONF       "/etc/plinius-swap.conf"
#define SWAP_FILE  "/home/.plinius-swap/swapfile"
#define HOME_DIR   "/home"
#define ZRAM_STAT  "/sys/block/zram0/mm_stat"
#define SWAPPINESS "/proc/sys/vm/swappiness"

/* Defaults as in plinius-swap-set */
#define DEFAULT_SIZE_GIB   8
#define DEFAULT_SWAPPINESS 1

/* Limits of the rows, inside what the tool takes */
#define MIN_SIZE_GIB   1
#define MAX_SIZE_GIB   64
#define MIN_SWAPPINESS 1
#define MAX_SWAPPINESS 200

/* How often the numbers change while the page shows */
#define USAGE_SECONDS 3
/* The setting goes to the tool once the last step of the row is this long ago */
#define SWAPPINESS_DELAY_MS 800
#define N_PROCESSES 6

/* Exit codes of plinius-swap-set */
#define TOOL_NO_RAM      3
#define TOOL_NO_STORAGE  4
#define TOOL_NO_LOCATION 5
/* Exit codes of pkexec: prompt dismissed, not authorized */
#define PKEXEC_DISMISSED 126
#define PKEXEC_NOT_AUTHORIZED 127

typedef struct {
    GtkWidget *page;
    AdwToastOverlay *toast_overlay;

    AdwPreferencesGroup *settings_group;
    AdwSwitchRow *file_switch_row;
    AdwSpinRow *size_row;
    GtkWidget *size_button;
    AdwSpinRow *swappiness_row;

    AdwActionRow *ram_row;
    GtkLevelBar *ram_bar;
    AdwActionRow *cache_row;
    AdwActionRow *free_row;

    AdwActionRow *zram_row;
    GtkLevelBar *zram_bar;
    AdwActionRow *file_row;
    GtkLevelBar *file_bar;
    AdwActionRow *drain_row;
    GtkWidget *drain_button;
    GtkWidget *drain_spinner;

    AdwActionRow *process_rows[N_PROCESSES];

    guint usage_id;
    guint swappiness_id;
    gboolean syncing; /* the rows are set from the system, not by the user */
    gboolean size_edited; /* the size row holds the user's value, not applied yet */
    gboolean busy;    /* the tool is running */
    gboolean draining;
} SwapPage;

/* What /proc/swaps says of one area, in bytes */
typedef struct {
    guint64 size;
    guint64 used;
    gboolean active;
} SwapArea;

typedef struct {
    char *name;
    guint64 rss;
    guint64 swap;
} Process;

static void
process_clear (Process *process)
{
    g_clear_pointer (&process->name, g_free);
}

/* One value of a "Key: 123 kB" file in bytes; 0 without that key */
static guint64
kib_value (const char *text, const char *key)
{
    g_autofree char *needle = g_strdup_printf ("%s:", key);
    const char *at = g_str_has_prefix (text, needle) ? text : NULL;

    if (at == NULL) {
        g_autofree char *line_needle = g_strdup_printf ("\n%s:", key);

        at = strstr (text, line_needle);
        if (at == NULL)
            return 0;
        at++;
    }
    return g_ascii_strtoull (at + strlen (needle), NULL, 10) * 1024;
}

/* The numbers of one key of /etc/plinius-swap.conf; a missing file or key gives the default */
static int
conf_int (const char *key, int def)
{
    g_autofree char *text = NULL;
    g_auto(GStrv) lines = NULL;
    int value = def;

    if (!g_file_get_contents (CONF, &text, NULL, NULL))
        return def;

    lines = g_strsplit (text, "\n", -1);
    for (char **l = lines; *l != NULL; l++) {
        g_auto(GStrv) kv = g_strsplit (*l, "=", 2);

        if (kv[0] != NULL && kv[1] != NULL && g_strcmp0 (g_strstrip (kv[0]), key) == 0)
            value = atoi (kv[1]);
    }
    return value;
}

/* The areas of /proc/swaps: Android's zram and our file; total also counts any other */
static void
read_swaps (SwapArea *zram, SwapArea *file, SwapArea *total)
{
    g_autofree char *text = NULL;
    g_auto(GStrv) lines = NULL;

    memset (zram, 0, sizeof *zram);
    memset (file, 0, sizeof *file);
    memset (total, 0, sizeof *total);
    if (!g_file_get_contents ("/proc/swaps", &text, NULL, NULL))
        return;

    lines = g_strsplit (text, "\n", -1);
    for (char **l = lines; *l != NULL; l++) {
        char name[256];
        char type[32];
        guint64 size, used;

        /* Filename Type Size Used Priority, sizes in KiB; the heading has no numbers */
        if (sscanf (*l, "%255s %31s %" G_GUINT64_FORMAT " %" G_GUINT64_FORMAT, name, type, &size, &used) != 4)
            continue;
        size *= 1024;
        used *= 1024;
        total->size += size;
        total->used += used;
        if (g_strcmp0 (name, SWAP_FILE) == 0)
            *file = (SwapArea) { size, used, TRUE };
        else if (strstr (name, "zram") != NULL)
            *zram = (SwapArea) { size, used, TRUE };
    }
}

static char *
format_size (guint64 bytes)
{
    return g_format_size_full (bytes, G_FORMAT_SIZE_IEC_UNITS);
}

static void
set_bar (GtkLevelBar *bar, guint64 part, guint64 whole)
{
    gtk_level_bar_set_value (bar, whole > 0 ? MIN (1.0, (double) part / (double) whole) : 0.0);
}

/* Largest first, by RAM and swap together */
static int
process_compare (gconstpointer a, gconstpointer b)
{
    const Process *pa = a, *pb = b;
    guint64 ta = pa->rss + pa->swap, tb = pb->rss + pb->swap;

    return (ta < tb) - (ta > tb);
}

static void
sync_processes (SwapPage *self)
{
    g_autoptr(GDir) dir = g_dir_open ("/proc", 0, NULL);
    g_autoptr(GArray) processes = g_array_new (FALSE, TRUE, sizeof (Process));
    const char *entry;

    g_array_set_clear_func (processes, (GDestroyNotify) process_clear);
    if (dir == NULL)
        return;

    while ((entry = g_dir_read_name (dir)) != NULL) {
        g_autofree char *path = NULL;
        g_autofree char *text = NULL;
        Process process = { NULL, 0, 0 };
        const char *name;

        if (!g_ascii_isdigit (entry[0]))
            continue;
        path = g_build_filename ("/proc", entry, "status", NULL);
        /* A process can be gone by now; kernel threads have no memory of their own */
        if (!g_file_get_contents (path, &text, NULL, NULL))
            continue;
        process.rss = kib_value (text, "VmRSS");
        process.swap = kib_value (text, "VmSwap");
        if (process.rss + process.swap == 0 || !g_str_has_prefix (text, "Name:"))
            continue;
        name = text + strlen ("Name:");
        process.name = g_strstrip (g_strndup (name, strcspn (name, "\n")));
        g_array_append_val (processes, process);
    }
    g_array_sort (processes, process_compare);

    for (guint i = 0; i < N_PROCESSES; i++) {
        gboolean shown = i < processes->len;

        gtk_widget_set_visible (GTK_WIDGET (self->process_rows[i]), shown);
        if (shown) {
            Process *process = &g_array_index (processes, Process, i);
            g_autofree char *rss = format_size (process->rss);
            g_autofree char *swap = format_size (process->swap);
            /* Translators: memory of a process, the sizes of RAM and swap */
            g_autofree char *line = g_strdup_printf (P_("RAM %s, swap %s"), rss, swap);

            adw_preferences_row_set_title (ADW_PREFERENCES_ROW (self->process_rows[i]), process->name);
            adw_action_row_set_subtitle (self->process_rows[i], line);
        }
    }
}

/* Everything that changes all the time */
static void
sync_usage (SwapPage *self)
{
    g_autofree char *meminfo = NULL;
    g_autofree char *zram_stat = NULL;
    SwapArea zram, file, total;
    guint64 mem_total = 0, mem_available = 0, mem_free = 0, cache = 0, room = 0;

    if (g_file_get_contents ("/proc/meminfo", &meminfo, NULL, NULL)) {
        mem_total = kib_value (meminfo, "MemTotal");
        mem_available = kib_value (meminfo, "MemAvailable");
        mem_free = kib_value (meminfo, "MemFree");
        cache = kib_value (meminfo, "Cached") + kib_value (meminfo, "Buffers");
    }
    read_swaps (&zram, &file, &total);

    {
        g_autofree char *used = format_size (mem_total - MIN (mem_total, mem_available));
        g_autofree char *whole = format_size (mem_total);
        g_autofree char *line = g_strdup_printf (P_("%s of %s in use"), used, whole);
        g_autofree char *cache_text = format_size (cache);
        g_autofree char *free_text = format_size (mem_free);

        adw_action_row_set_subtitle (self->ram_row, line);
        set_bar (self->ram_bar, mem_total - MIN (mem_total, mem_available), mem_total);
        adw_action_row_set_subtitle (self->cache_row, cache_text);
        adw_action_row_set_subtitle (self->free_row, free_text);
    }

    gtk_widget_set_visible (GTK_WIDGET (self->zram_row), zram.active);
    if (zram.active) {
        g_autofree char *used = format_size (zram.used);
        g_autofree char *whole = format_size (zram.size);
        guint64 ram_used = 0;
        g_autofree char *line = NULL;

        /* mm_stat: original, compressed, memory really taken, ... */
        if (g_file_get_contents (ZRAM_STAT, &zram_stat, NULL, NULL)
            && sscanf (zram_stat, "%*u %*u %" G_GUINT64_FORMAT, &ram_used) == 1) {
            g_autofree char *taken = format_size (ram_used);

            /* Translators: swap in use, its size, the RAM this really takes */
            line = g_strdup_printf (P_("%s of %s, takes %s of RAM"), used, whole, taken);
        } else {
            line = g_strdup_printf (P_("%s of %s"), used, whole);
        }
        adw_action_row_set_subtitle (self->zram_row, line);
        set_bar (self->zram_bar, zram.used, zram.size);
    }

    gtk_widget_set_visible (GTK_WIDGET (self->file_row), file.active);
    if (file.active) {
        g_autofree char *used = format_size (file.used);
        g_autofree char *whole = format_size (file.size);
        g_autofree char *line = g_strdup_printf (P_("%s of %s"), used, whole);

        adw_action_row_set_subtitle (self->file_row, line);
        set_bar (self->file_bar, file.used, file.size);
    }

    /* Room as plinius-swap-set counts it: available memory less a twentieth of the RAM */
    if (mem_available > mem_total / 20)
        room = mem_available - mem_total / 20;
    gtk_widget_set_visible (self->drain_spinner, self->draining);
    gtk_widget_set_sensitive (self->drain_button, !self->busy && total.used > 0 && total.used <= room);
    if (self->draining) {
        adw_action_row_set_subtitle (self->drain_row, P_("Moving, this can take a while"));
    } else if (total.used == 0) {
        adw_action_row_set_subtitle (self->drain_row, P_("Swap is empty"));
    } else if (total.used > room) {
        g_autofree char *need = format_size (total.used);
        g_autofree char *have = format_size (room);
        /* Translators: what is in swap, what fits into RAM right now */
        g_autofree char *line = g_strdup_printf (P_("Not enough free memory: %s in swap, room for %s"), need, have);

        adw_action_row_set_subtitle (self->drain_row, line);
    } else {
        g_autofree char *need = format_size (total.used);
        g_autofree char *line = g_strdup_printf (P_("Brings %s back into RAM"), need);

        adw_action_row_set_subtitle (self->drain_row, line);
    }

    sync_processes (self);
}

/* The size row's button only shows when it would change something */
static void
sync_size_button (SwapPage *self)
{
    int current = conf_int ("size_gib", DEFAULT_SIZE_GIB);

    gtk_widget_set_visible (self->size_button, (int) adw_spin_row_get_value (self->size_row) != current);
}

/* The settings as the system says, without running the tool again. Not on the timer: the user may
 * be stepping through the rows. A value the user has changed and the tool has not got yet stays:
 * a stepped threshold waiting for its timer, a size not applied. */
static void
sync_settings (SwapPage *self)
{
    SwapArea zram, file, total;
    g_autofree char *swappiness = NULL;
    g_autofree char *size_subtitle = NULL;
    struct statvfs fs;

    read_swaps (&zram, &file, &total);

    self->syncing = TRUE;
    adw_switch_row_set_active (self->file_switch_row, file.active);
    if (!self->size_edited)
        adw_spin_row_set_value (self->size_row,
                                CLAMP (conf_int ("size_gib", DEFAULT_SIZE_GIB), MIN_SIZE_GIB, MAX_SIZE_GIB));
    if (self->swappiness_id != 0)
        ; /* the stepped value goes to the tool */
    else if (g_file_get_contents (SWAPPINESS, &swappiness, NULL, NULL))
        adw_spin_row_set_value (self->swappiness_row, CLAMP (atoi (swappiness), MIN_SWAPPINESS, MAX_SWAPPINESS));
    else
        adw_spin_row_set_value (self->swappiness_row, conf_int ("swappiness", DEFAULT_SWAPPINESS));
    self->syncing = FALSE;

    if (statvfs (HOME_DIR, &fs) == 0) {
        g_autofree char *free_text = format_size ((guint64) fs.f_bavail * fs.f_frsize);

        /* Translators: free storage */
        size_subtitle = g_strdup_printf (P_("In GiB, %s free on the storage"), free_text);
        adw_action_row_set_subtitle (ADW_ACTION_ROW (self->size_row), size_subtitle);
    }

    sync_size_button (self);
    gtk_widget_set_sensitive (GTK_WIDGET (self->settings_group), !self->busy);
}

static gboolean
usage_tick (gpointer user_data)
{
    sync_usage (user_data);
    return G_SOURCE_CONTINUE;
}

static void
on_showing (SwapPage *self)
{
    /* A size not applied is dropped when the page is left */
    self->size_edited = FALSE;
    sync_settings (self);
    sync_usage (self);
    if (self->usage_id == 0)
        self->usage_id = g_timeout_add_seconds (USAGE_SECONDS, usage_tick, self);
}

static void
on_hidden (SwapPage *self)
{
    g_clear_handle_id (&self->usage_id, g_source_remove);
}

static void
swap_page_free (SwapPage *self)
{
    g_clear_handle_id (&self->usage_id, g_source_remove);
    g_clear_handle_id (&self->swappiness_id, g_source_remove);
    g_free (self);
}

static void
show_toast (SwapPage *self, const char *text)
{
    adw_toast_overlay_add_toast (self->toast_overlay, adw_toast_new (text));
}

static void
run_done_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(GtkWidget) page = user_data;
    SwapPage *self = g_object_get_data (G_OBJECT (page), PAGE_TAG);
    g_autoptr(GError) error = NULL;
    int status = -1;

    if (!g_subprocess_wait_finish (G_SUBPROCESS (source), result, &error))
        g_warning ("%s failed: %s", SET_TOOL, error->message);
    else if (g_subprocess_get_if_exited (G_SUBPROCESS (source)))
        status = g_subprocess_get_exit_status (G_SUBPROCESS (source));

    switch (status) {
    case 0:
    /* A dismissed or refused prompt is the user's answer, not a failure */
    case PKEXEC_DISMISSED:
    case PKEXEC_NOT_AUTHORIZED:
        break;
    case TOOL_NO_RAM:
        show_toast (self, P_("Not enough free memory for what is in swap. Close some apps and try again."));
        break;
    case TOOL_NO_STORAGE:
        show_toast (self, P_("Not enough free storage for a swap file of this size."));
        break;
    case TOOL_NO_LOCATION:
        show_toast (self, P_("No swap file possible: it would be outside the encryption or on an image file."));
        break;
    default:
        g_warning ("%s exited with %d", SET_TOOL, status);
        show_toast (self, P_("Could not change swap."));
        break;
    }

    /* Also on failure: the rows go back to what is true */
    self->busy = FALSE;
    self->draining = FALSE;
    sync_settings (self);
    sync_usage (self);
}

/* One call at a time */
static void
set_busy (SwapPage *self)
{
    self->busy = TRUE;
    gtk_widget_set_sensitive (GTK_WIDGET (self->settings_group), FALSE);
    gtk_widget_set_sensitive (self->drain_button, FALSE);
}

static gboolean
sync_idle (gpointer user_data)
{
    SwapPage *self = g_object_get_data (G_OBJECT (user_data), PAGE_TAG);

    self->busy = FALSE;
    sync_settings (self);
    sync_usage (self);
    return G_SOURCE_REMOVE;
}

/* Runs the tool, one call at a time; @argument may be NULL */
static gboolean
run_tool (SwapPage *self, const char *command, const char *argument)
{
    g_autoptr(GSubprocess) process = NULL;
    g_autoptr(GError) error = NULL;

    process = g_subprocess_new (G_SUBPROCESS_FLAGS_STDOUT_SILENCE | G_SUBPROCESS_FLAGS_STDERR_SILENCE, &error,
                                "pkexec", SET_TOOL, command, argument, NULL);
    set_busy (self);
    if (process == NULL) {
        g_warning ("Cannot run pkexec %s: %s", SET_TOOL, error->message);
        /* Put back once this change is over: a switch set back inside its own change notifies
         * again, which would try once more */
        g_idle_add_full (G_PRIORITY_DEFAULT, sync_idle, g_object_ref (self->page), g_object_unref);
        return FALSE;
    }
    g_subprocess_wait_async (process, NULL, run_done_cb, g_object_ref (self->page));
    return TRUE;
}

static void
on_file_switch_changed (SwapPage *self)
{
    if (self->syncing || self->busy)
        return;
    run_tool (self, adw_switch_row_get_active (self->file_switch_row) ? "enable" : "disable", NULL);
}

static void
on_size_value_changed (SwapPage *self)
{
    if (self->syncing)
        return;
    self->size_edited = TRUE;
    sync_size_button (self);
}

static void
on_size_apply_clicked (SwapPage *self)
{
    g_autofree char *size = g_strdup_printf ("%d", (int) adw_spin_row_get_value (self->size_row));

    if (self->busy)
        return;
    /* From now on the row shows what the tool made of it */
    self->size_edited = FALSE;
    run_tool (self, "size", size);
}

/* The tool once the user has stopped stepping; waits while another call runs */
static gboolean
swappiness_timeout (gpointer user_data)
{
    SwapPage *self = user_data;
    g_autofree char *value = NULL;

    if (self->busy)
        return G_SOURCE_CONTINUE;
    self->swappiness_id = 0;
    value = g_strdup_printf ("%d", (int) adw_spin_row_get_value (self->swappiness_row));
    run_tool (self, "swappiness", value);
    return G_SOURCE_REMOVE;
}

static void
on_swappiness_value_changed (SwapPage *self)
{
    if (self->syncing)
        return;
    g_clear_handle_id (&self->swappiness_id, g_source_remove);
    self->swappiness_id = g_timeout_add (SWAPPINESS_DELAY_MS, swappiness_timeout, self);
}

static void
on_drain_clicked (SwapPage *self)
{
    if (self->busy)
        return;
    self->draining = run_tool (self, "drain", NULL);
    sync_usage (self);
}

static GtkLevelBar *
add_bar (AdwActionRow *row)
{
    GtkWidget *bar = gtk_level_bar_new_for_interval (0.0, 1.0);

    /* One colour: a nearly empty swap is nothing to warn about */
    gtk_level_bar_remove_offset_value (GTK_LEVEL_BAR (bar), GTK_LEVEL_BAR_OFFSET_LOW);
    gtk_level_bar_remove_offset_value (GTK_LEVEL_BAR (bar), GTK_LEVEL_BAR_OFFSET_HIGH);
    gtk_level_bar_remove_offset_value (GTK_LEVEL_BAR (bar), GTK_LEVEL_BAR_OFFSET_FULL);
    gtk_widget_set_size_request (bar, 80, -1);
    gtk_widget_set_valign (bar, GTK_ALIGN_CENTER);
    adw_action_row_add_suffix (row, bar);
    return GTK_LEVEL_BAR (bar);
}

static AdwActionRow *
add_info_row (AdwPreferencesGroup *group, const char *title)
{
    GtkWidget *row = adw_action_row_new ();

    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), title);
    /* The title is the small label, the value what one reads */
    gtk_widget_add_css_class (row, "property");
    adw_preferences_group_add (group, row);
    return ADW_ACTION_ROW (row);
}

static AdwSpinRow *
add_spin_row (AdwPreferencesGroup *group, const char *title, const char *subtitle, int min, int max)
{
    GtkWidget *row = adw_spin_row_new_with_range (min, max, 1);

    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), title);
    adw_action_row_set_subtitle (ADW_ACTION_ROW (row), subtitle);
    adw_preferences_group_add (group, row);
    return ADW_SPIN_ROW (row);
}

/**
 * cc_plinius_swap_add_page:
 * @panel: the System panel
 *
 * Adds the page "plinius-swap" to @panel when plinius-swap is installed. Returns whether it did,
 * i.e. whether the row should show.
 */
gboolean
cc_plinius_swap_add_page (CcPanel *panel)
{
    SwapPage *self;
    AdwPreferencesPage *prefs;
    AdwPreferencesGroup *group;
    GtkWidget *toolbar;

    if (!g_file_test (SET_TOOL, G_FILE_TEST_IS_EXECUTABLE))
        return FALSE;

    self = g_new0 (SwapPage, 1);
    prefs = ADW_PREFERENCES_PAGE (adw_preferences_page_new ());

    group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    self->settings_group = group;
    adw_preferences_group_set_title (group, P_("Settings"));
    adw_preferences_page_add (prefs, group);
    self->file_switch_row = ADW_SWITCH_ROW (adw_switch_row_new ());
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (self->file_switch_row), P_("Swap File"));
    adw_action_row_set_subtitle (ADW_ACTION_ROW (self->file_switch_row),
                                 P_("On the storage, used once the compressed RAM is full"));
    adw_preferences_group_add (group, GTK_WIDGET (self->file_switch_row));
    self->size_row = add_spin_row (group, P_("Size"), "", MIN_SIZE_GIB, MAX_SIZE_GIB);
    self->size_button = gtk_button_new_with_label (P_("Apply"));
    gtk_widget_set_valign (self->size_button, GTK_ALIGN_CENTER);
    gtk_widget_add_css_class (self->size_button, "suggested-action");
    adw_action_row_add_suffix (ADW_ACTION_ROW (self->size_row), self->size_button);
    self->swappiness_row = add_spin_row (
        group, P_("Threshold (Swappiness)"),
        P_("The lower, the later memory moves to swap. 1: only when memory runs short, 60: Linux default, 100: Android default"),
        MIN_SWAPPINESS, MAX_SWAPPINESS);

    group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_group_set_title (group, P_("Memory"));
    adw_preferences_page_add (prefs, group);
    self->ram_row = add_info_row (group, P_("RAM"));
    self->ram_bar = add_bar (self->ram_row);
    self->cache_row = add_info_row (group, P_("Cache, given back when needed"));
    self->free_row = add_info_row (group, P_("Free"));

    group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_group_set_title (group, P_("Swap"));
    adw_preferences_page_add (prefs, group);
    self->zram_row = add_info_row (group, P_("Compressed in RAM (zram)"));
    self->zram_bar = add_bar (self->zram_row);
    self->file_row = add_info_row (group, P_("Swap File"));
    self->file_bar = add_bar (self->file_row);
    self->drain_row = ADW_ACTION_ROW (adw_action_row_new ());
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (self->drain_row), P_("Move Swap to RAM"));
    self->drain_spinner = adw_spinner_new ();
    adw_action_row_add_suffix (self->drain_row, self->drain_spinner);
    self->drain_button = gtk_button_new_with_label (P_("Move"));
    gtk_widget_set_valign (self->drain_button, GTK_ALIGN_CENTER);
    adw_action_row_add_suffix (self->drain_row, self->drain_button);
    adw_preferences_group_add (group, GTK_WIDGET (self->drain_row));

    group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_group_set_title (group, P_("Largest Processes"));
    adw_preferences_page_add (prefs, group);
    for (int i = 0; i < N_PROCESSES; i++) {
        self->process_rows[i] = ADW_ACTION_ROW (adw_action_row_new ());
        /* A process can name itself anything */
        adw_preferences_row_set_use_markup (ADW_PREFERENCES_ROW (self->process_rows[i]), FALSE);
        adw_preferences_group_add (group, GTK_WIDGET (self->process_rows[i]));
    }

    self->toast_overlay = ADW_TOAST_OVERLAY (adw_toast_overlay_new ());
    adw_toast_overlay_set_child (self->toast_overlay, GTK_WIDGET (prefs));
    toolbar = adw_toolbar_view_new ();
    adw_toolbar_view_add_top_bar (ADW_TOOLBAR_VIEW (toolbar), adw_header_bar_new ());
    adw_toolbar_view_set_content (ADW_TOOLBAR_VIEW (toolbar), GTK_WIDGET (self->toast_overlay));
    self->page = GTK_WIDGET (adw_navigation_page_new_with_tag (toolbar, P_("Memory and Swap"), PAGE_TAG));
    g_object_set_data_full (G_OBJECT (self->page), PAGE_TAG, self, (GDestroyNotify) swap_page_free);

    g_signal_connect_swapped (self->page, "showing", G_CALLBACK (on_showing), self);
    g_signal_connect_swapped (self->page, "hidden", G_CALLBACK (on_hidden), self);
    g_signal_connect_swapped (self->file_switch_row, "notify::active", G_CALLBACK (on_file_switch_changed), self);
    g_signal_connect_swapped (self->size_row, "notify::value", G_CALLBACK (on_size_value_changed), self);
    g_signal_connect_swapped (self->size_button, "clicked", G_CALLBACK (on_size_apply_clicked), self);
    g_signal_connect_swapped (self->swappiness_row, "notify::value", G_CALLBACK (on_swappiness_value_changed), self);
    g_signal_connect_swapped (self->drain_button, "clicked", G_CALLBACK (on_drain_clicked), self);

    cc_panel_add_subpage (panel, PAGE_TAG, ADW_NAVIGATION_PAGE (self->page));
    return TRUE;
}

/**
 * cc_plinius_swap_row_setup:
 * @row: the row on the System page
 *
 * Gives @row our title and subtitle (our gettext domain), points it at our page and shows it.
 */
void
cc_plinius_swap_row_setup (AdwActionRow *row)
{
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_("_Memory and Swap"));
    adw_action_row_set_subtitle (row, P_("What RAM and swap hold, swap file and threshold"));
    gtk_actionable_set_action_target (GTK_ACTIONABLE (row), "s", PAGE_TAG);
    gtk_widget_set_visible (GTK_WIDGET (row), TRUE);
}
