/*
 * cc-plinius-hardware.c
 *
 * Plinius: "Hardware" in System, the phone's chips and the versions of their firmware, next
 * to About (which keeps model, memory, processor and disk). Most of it is only readable by
 * root (firmware partitions, kernel log, Android's build.prop), so the device package collects
 * it at boot into the key file /run/plinius-hardware (plinius-hardware-info); without that file
 * the row stays hidden.
 *
 * Each group of the file is one row. The table below names the groups this page knows, in the
 * order shown; others are left out, so every title is translated. Keys of a group, all
 * optional, joined into the row's subtitle:
 *   Name      chip or component ("MediaTek MT6637")
 *   Version   version as the firmware reports it
 *   Firmware  firmware of a chip
 *   State     verified boot: "locked" or "unlocked"
 *   Slot      active A/B slot
 *   Date      build date, YYYY-MM-DD (shown as is: unambiguous in every language, and
 *             the form Android gives its patch level in)
 *   Patch     Android security patch level, YYYY-MM-DD
 *
 * Our own strings use the gettext domain plinius-control-center (not GNOME's po/).
 *
 * SPDX-License-Identifier: MIT
 */

#undef G_LOG_DOMAIN
#define G_LOG_DOMAIN "cc-plinius-hardware"

#include <glib/gi18n.h>

#include "cc-plinius-hardware.h"

#define PLINIUS_DOMAIN "plinius-control-center"
#define P_(String) g_dgettext (PLINIUS_DOMAIN, String)

#define HARDWARE_FILE "/run/plinius-hardware"
#define PAGE_TAG      "plinius-hardware"

typedef enum {
    SECTION_COMPONENTS,
    SECTION_BOOT,
    SECTION_RADIOS,
    SECTION_ANDROID,
    N_SECTIONS
} Section;

static const struct {
    const char *title;
    const char *description;
} sections[N_SECTIONS] = {
    [SECTION_COMPONENTS] = { N_("Components"), NULL },
    [SECTION_BOOT] = { N_("Boot and Security"), N_("Firmware that starts before Linux") },
    [SECTION_RADIOS] = { N_("Radios"), NULL },
    [SECTION_ANDROID] = { N_("Android Base"), N_("Vendor drivers that Linux uses through the Android container") },
};

static const struct {
    const char *group;
    Section section;
    const char *title;
} entries[] = {
    { "soc", SECTION_COMPONENTS, N_("System on Chip") },
    { "gpu", SECTION_COMPONENTS, N_("Graphics") },
    { "storage", SECTION_COMPONENTS, N_("Storage") },
    { "connectivity", SECTION_COMPONENTS, N_("Wi-Fi and Bluetooth") },
    { "touch", SECTION_COMPONENTS, N_("Touchscreen") },
    { "nfc", SECTION_COMPONENTS, N_("NFC") },
    { "wireless-charging", SECTION_COMPONENTS, N_("Wireless Charging") },
    { "audio", SECTION_COMPONENTS, N_("Audio") },
    { "preloader", SECTION_BOOT, N_("Preloader") },
    { "bootloader", SECTION_BOOT, N_("Bootloader") },
    { "atf", SECTION_BOOT, N_("Trusted Firmware-A") },
    { "tee", SECTION_BOOT, N_("Trusted OS") },
    { "trusty", SECTION_BOOT, N_("Trusted OS") },
    { "hypervisor", SECTION_BOOT, N_("Hypervisor") },
    { "spm", SECTION_BOOT, N_("Power Management") },
    { "verified-boot", SECTION_BOOT, N_("Verified Boot") },
    { "modem", SECTION_RADIOS, N_("Modem") },
    { "wifi", SECTION_RADIOS, N_("Wi-Fi") },
    { "bluetooth", SECTION_RADIOS, N_("Bluetooth") },
    { "vendor", SECTION_ANDROID, N_("Vendor") },
    { "bsp", SECTION_ANDROID, N_("MediaTek Software") },
    { "vendor-modules", SECTION_ANDROID, N_("Vendor Kernel Modules") },
    { "gpu-driver", SECTION_ANDROID, N_("Graphics Driver") },
    { "audio-params", SECTION_ANDROID, N_("Audio Parameters") },
    { "system", SECTION_ANDROID, N_("Android System") },
};

typedef struct {
    AdwPreferencesPage *prefs;
    AdwToastOverlay    *toast_overlay;
    GPtrArray          *groups;
    GString            *text;  /* everything shown, for "Copy" */
} HardwarePage;

static void
hardware_page_free (HardwarePage *hp)
{
    g_ptr_array_unref (hp->groups);
    g_string_free (hp->text, TRUE);
    g_free (hp);
}

/* The row's subtitle from the keys of @group, NULL if the group has none of them */
static char *
make_subtitle (GKeyFile *file, const char *group)
{
    g_autoptr(GPtrArray) parts = g_ptr_array_new_with_free_func (g_free);
    g_autofree char *name = g_key_file_get_string (file, group, "Name", NULL);
    g_autofree char *version = g_key_file_get_string (file, group, "Version", NULL);
    g_autofree char *firmware = g_key_file_get_string (file, group, "Firmware", NULL);
    g_autofree char *state = g_key_file_get_string (file, group, "State", NULL);
    g_autofree char *slot = g_key_file_get_string (file, group, "Slot", NULL);
    g_autofree char *date = g_key_file_get_string (file, group, "Date", NULL);
    g_autofree char *patch = g_key_file_get_string (file, group, "Patch", NULL);

    if (name != NULL)
        g_ptr_array_add (parts, g_steal_pointer (&name));
    if (version != NULL)
        g_ptr_array_add (parts, g_steal_pointer (&version));
    if (firmware != NULL)
        /* Translators: firmware version of a chip, e.g. "Firmware 17" */
        g_ptr_array_add (parts, g_strdup_printf (P_("Firmware %s"), firmware));
    if (g_strcmp0 (state, "unlocked") == 0)
        g_ptr_array_add (parts, g_strdup (P_("Bootloader unlocked")));
    else if (g_strcmp0 (state, "locked") == 0)
        g_ptr_array_add (parts, g_strdup (P_("Bootloader locked")));
    else if (state != NULL)
        g_ptr_array_add (parts, g_steal_pointer (&state));
    if (slot != NULL)
        /* Translators: active A/B slot of the system partitions, e.g. "Slot b" */
        g_ptr_array_add (parts, g_strdup_printf (P_("Slot %s"), slot));
    if (date != NULL)
        g_ptr_array_add (parts, g_steal_pointer (&date));
    if (patch != NULL)
        /* Translators: Android security patch level, followed by a date (YYYY-MM-DD) */
        g_ptr_array_add (parts, g_strdup_printf (P_("Security patch %s"), patch));

    if (parts->len == 0)
        return NULL;

    g_ptr_array_add (parts, NULL);
    return g_strjoinv (" · ", (char **) parts->pdata);
}

static GtkWidget *
property_row_new (const char *title, const char *subtitle)
{
    GtkWidget *row = adw_action_row_new ();

    adw_preferences_row_set_use_markup (ADW_PREFERENCES_ROW (row), FALSE);
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), title);
    adw_action_row_set_subtitle (ADW_ACTION_ROW (row), subtitle);
    adw_action_row_set_subtitle_selectable (ADW_ACTION_ROW (row), TRUE);
    gtk_widget_add_css_class (row, "property");
    return row;
}

static void
add_group (HardwarePage *hp, GtkWidget *group)
{
    adw_preferences_page_add (hp->prefs, ADW_PREFERENCES_GROUP (group));
    g_ptr_array_add (hp->groups, group);
}

static void
copy_activated_cb (AdwButtonRow *button, HardwarePage *hp)
{
    gdk_clipboard_set_text (gtk_widget_get_clipboard (GTK_WIDGET (button)), hp->text->str);
    adw_toast_overlay_add_toast (hp->toast_overlay, adw_toast_new (P_("Copied to clipboard")));
}

/* Reads the file again on every showing: the collector may finish after Settings started */
static void
update_page (HardwarePage *hp)
{
    g_autoptr(GKeyFile) file = g_key_file_new ();
    g_autoptr(GError) error = NULL;
    GtkWidget *groups[N_SECTIONS] = { NULL };
    GtkWidget *group;
    guint i;

    for (i = 0; i < hp->groups->len; i++)
        adw_preferences_page_remove (hp->prefs, g_ptr_array_index (hp->groups, i));
    g_ptr_array_set_size (hp->groups, 0);
    g_string_truncate (hp->text, 0);

    if (!g_key_file_load_from_file (file, HARDWARE_FILE, G_KEY_FILE_NONE, &error)) {
        if (!g_error_matches (error, G_FILE_ERROR, G_FILE_ERROR_NOENT))
            g_warning ("Could not read %s: %s", HARDWARE_FILE, error->message);
        group = adw_preferences_group_new ();
        adw_preferences_group_add (ADW_PREFERENCES_GROUP (group),
                                   property_row_new (P_("Not collected yet"),
                                                     P_("The versions are read once after the phone starts.")));
        add_group (hp, group);
        return;
    }

    /* The groups are created in the order of the sections, rows in the order of the table */
    for (i = 0; i < G_N_ELEMENTS (entries); i++) {
        g_autofree char *subtitle = make_subtitle (file, entries[i].group);
        Section s = entries[i].section;
        const char *title = P_(entries[i].title);

        if (subtitle == NULL)
            continue;

        if (groups[s] == NULL) {
            groups[s] = adw_preferences_group_new ();
            adw_preferences_group_set_title (ADW_PREFERENCES_GROUP (groups[s]), P_(sections[s].title));
            if (sections[s].description != NULL)
                adw_preferences_group_set_description (ADW_PREFERENCES_GROUP (groups[s]),
                                                       P_(sections[s].description));
        }
        adw_preferences_group_add (ADW_PREFERENCES_GROUP (groups[s]), property_row_new (title, subtitle));
        g_string_append_printf (hp->text, "%s: %s\n", title, subtitle);
    }

    for (i = 0; i < N_SECTIONS; i++) {
        if (groups[i] != NULL)
            add_group (hp, groups[i]);
    }

    if (hp->text->len > 0) {
        GtkWidget *button = adw_button_row_new ();

        adw_button_row_set_start_icon_name (ADW_BUTTON_ROW (button), "edit-copy-symbolic");
        adw_preferences_row_set_title (ADW_PREFERENCES_ROW (button), P_("_Copy"));
        adw_preferences_row_set_use_underline (ADW_PREFERENCES_ROW (button), TRUE);
        g_signal_connect (button, "activated", G_CALLBACK (copy_activated_cb), hp);
        group = adw_preferences_group_new ();
        adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), button);
        add_group (hp, group);
    }
}


/**
 * cc_plinius_hardware_add_page:
 * @panel: the System panel
 *
 * Adds the page "plinius-hardware" to @panel when the device package collects the versions.
 * Returns whether it did, i.e. whether the "Hardware" row should show.
 */
gboolean
cc_plinius_hardware_add_page (CcPanel *panel)
{
    HardwarePage *hp;
    GtkWidget *toolbar, *page;

    if (!g_file_test (HARDWARE_FILE, G_FILE_TEST_EXISTS)
        && !g_file_test ("/usr/lib/plinius/plinius-hardware-info", G_FILE_TEST_IS_EXECUTABLE))
        return FALSE;

    hp = g_new0 (HardwarePage, 1);
    hp->groups = g_ptr_array_new ();
    hp->text = g_string_new (NULL);
    hp->prefs = ADW_PREFERENCES_PAGE (adw_preferences_page_new ());
    hp->toast_overlay = ADW_TOAST_OVERLAY (adw_toast_overlay_new ());
    adw_toast_overlay_set_child (hp->toast_overlay, GTK_WIDGET (hp->prefs));

    toolbar = adw_toolbar_view_new ();
    adw_toolbar_view_add_top_bar (ADW_TOOLBAR_VIEW (toolbar), adw_header_bar_new ());
    adw_toolbar_view_set_content (ADW_TOOLBAR_VIEW (toolbar), GTK_WIDGET (hp->toast_overlay));
    page = GTK_WIDGET (adw_navigation_page_new_with_tag (toolbar, P_("Hardware"), PAGE_TAG));
    g_object_set_data_full (G_OBJECT (page), PAGE_TAG, hp, (GDestroyNotify) hardware_page_free);
    g_signal_connect_swapped (page, "showing", G_CALLBACK (update_page), hp);

    cc_panel_add_subpage (panel, PAGE_TAG, ADW_NAVIGATION_PAGE (page));
    return TRUE;
}

/**
 * cc_plinius_hardware_row_setup:
 * @row: the "Hardware" row on the System page
 *
 * Gives @row our title and subtitle (our gettext domain), points it at our page and shows it.
 */
void
cc_plinius_hardware_row_setup (AdwActionRow *row)
{
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_("_Hardware"));
    adw_action_row_set_subtitle (row, P_("Chips and firmware versions"));
    gtk_actionable_set_action_target (GTK_ACTIONABLE (row), "s", PAGE_TAG);
    gtk_widget_set_visible (GTK_WIDGET (row), TRUE);
}
