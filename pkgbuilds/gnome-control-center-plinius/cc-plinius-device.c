/*
 * cc-plinius-device.c
 *
 * Plinius: device facts and phone settings for the About and Display pages.
 * The device package describes the phone in /usr/share/plinius/device.conf (processor name,
 * base scale of the built-in panel); without that file Settings behaves as upstream.
 * The notch setting belongs to phosh-plinius (schema org.plinius.display), which moves the
 * top bar icons around the camera cutout; "Automatic" uses the panel data of the device.
 * Our own strings use the gettext domain plinius-control-center (not GNOME's po/).
 *
 * SPDX-License-Identifier: MIT
 */

#undef G_LOG_DOMAIN
#define G_LOG_DOMAIN "cc-plinius-device"

#include "cc-plinius-device.h"

#include <math.h>

#define PLINIUS_DOMAIN "plinius-control-center"
#define P_(String) g_dgettext (PLINIUS_DOMAIN, String)

#define DEVICE_CONF "/usr/share/plinius/device.conf"
#define DEVICE_TREE "/proc/device-tree"

#define NOTCH_SCHEMA "org.plinius.display"
#define NOTCH_KEY "notch"

/* Scale steps of the built-in panel, relative to its base scale */
static const double scale_steps[] = { 1.0, 1.25, 1.5, 1.75, 2.0 };

/* Values of the notch key, in the order of the combo row */
static const char *const notch_values[] = { "auto", "none", "left", "center", "right" };

static GKeyFile *
get_device_conf (void)
{
    static GKeyFile *conf = NULL;
    static gsize loaded = 0;

    if (g_once_init_enter (&loaded)) {
        g_autoptr(GError) error = NULL;

        conf = g_key_file_new ();
        if (!g_key_file_load_from_file (conf, DEVICE_CONF, G_KEY_FILE_NONE, &error)) {
            if (!g_error_matches (error, G_FILE_ERROR, G_FILE_ERROR_NOENT))
                g_warning ("Could not read %s: %s", DEVICE_CONF, error->message);
            g_clear_pointer (&conf, g_key_file_unref);
        }
        g_once_init_leave (&loaded, 1);
    }

    return conf;
}

/* /proc/cpuinfo on ARM has no model name, so the device package names the SoC */
char *
cc_plinius_device_get_processor (void)
{
    GKeyFile *conf = get_device_conf ();

    if (!conf)
        return NULL;

    return g_key_file_get_string (conf, "Device", "Processor", NULL);
}

static guint
read_cells (const char *path, guint fallback)
{
    g_autofree char *data = NULL;
    gsize len;

    if (!g_file_get_contents (path, &data, &len, NULL) || len != 4)
        return fallback;

    return GUINT32_FROM_BE (*(guint32 *) data);
}

/*
 * Installed RAM as the bootloader reports it in the device tree (memory nodes), the
 * counterpart of the DMI tables on PCs. MemTotal is smaller by the memory the firmware
 * reserves for the modem, the hypervisor and other co-processors.
 */
guint64
cc_plinius_device_get_installed_ram (void)
{
    g_autoptr(GDir) dir = NULL;
    const char *name;
    guint address_cells, size_cells;
    guint64 total = 0;

    dir = g_dir_open (DEVICE_TREE, 0, NULL);
    if (!dir)
        return 0;

    address_cells = read_cells (DEVICE_TREE "/#address-cells", 2);
    size_cells = read_cells (DEVICE_TREE "/#size-cells", 1);

    while ((name = g_dir_read_name (dir)) != NULL) {
        g_autofree char *type_path = NULL;
        g_autofree char *reg_path = NULL;
        g_autofree char *type = NULL;
        g_autofree char *reg = NULL;
        gsize len, entry, i;

        if (!g_str_has_prefix (name, "memory"))
            continue;

        type_path = g_build_filename (DEVICE_TREE, name, "device_type", NULL);
        if (!g_file_get_contents (type_path, &type, NULL, NULL) || g_strcmp0 (type, "memory") != 0)
            continue;

        reg_path = g_build_filename (DEVICE_TREE, name, "reg", NULL);
        if (!g_file_get_contents (reg_path, &reg, &len, NULL))
            continue;

        /* reg: (address, size) pairs of big-endian 32-bit cells */
        entry = (address_cells + size_cells) * 4;
        for (i = 0; entry > 0 && i + entry <= len; i += entry) {
            const guint8 *cell = (const guint8 *) reg + i + address_cells * 4;
            guint64 size = 0;
            guint b;

            for (b = 0; b < size_cells * 4; b++)
                size = (size << 8) | cell[b];
            total += size;
        }
    }

    return total;
}

/*
 * systemd reports "vm-other" because the device tree has a hypervisor node (MediaTek's
 * firmware hypervisor). Linux still runs directly on the phone, so the row would only
 * confuse; it also hides the firmware version.
 */
gboolean
cc_plinius_device_is_virtualization_hidden (const char *virt)
{
    return g_strcmp0 (virt, "vm-other") == 0;
}

static double
get_scale_base (void)
{
    GKeyFile *conf = get_device_conf ();
    double base;

    if (!conf)
        return 0;

    base = g_key_file_get_double (conf, "Display", "ScaleBase", NULL);

    return base > 0 ? base : 0;
}

/*
 * Scale steps of the built-in panel: 100 % to 200 % of the base scale in steps of 25 %.
 * Each step moves to the nearest scale that gives whole logical pixels in both directions
 * (as Phosh's own steps do), otherwise app windows get blurry edges.
 * Returns NULL without a base scale, then the compositor's steps apply.
 */
GArray *
cc_plinius_device_get_scales (int width, int height)
{
    GArray *scales;
    double base = get_scale_base ();
    guint i;

    if (base == 0 || width <= 0 || height <= 0)
        return NULL;

    scales = g_array_new (FALSE, FALSE, sizeof (double));
    for (i = 0; i < G_N_ELEMENTS (scale_steps); i++) {
        double target = base * scale_steps[i];
        double scale = target;
        int logical = round (width / target);
        int d;

        for (d = 0; d <= 8; d++) {
            if (logical + d > 0 && (gint64) height * (logical + d) % width == 0) {
                scale = (double) width / (logical + d);
                break;
            }
            if (logical - d > 0 && (gint64) height * (logical - d) % width == 0) {
                scale = (double) width / (logical - d);
                break;
            }
        }
        g_array_append_val (scales, scale);
    }

    return scales;
}

/* Label of a built-in panel scale relative to the base scale, rounded to 5 % */
char *
cc_plinius_device_make_scale_string (double scale)
{
    double base = get_scale_base ();

    if (base == 0)
        return NULL;

    return g_strdup_printf ("%d %%", (int) round (scale / base * 20) * 5);
}

static gboolean
notch_get_mapping (GValue *value, GVariant *variant, gpointer user_data)
{
    const char *notch = g_variant_get_string (variant, NULL);
    guint i;

    for (i = 0; i < G_N_ELEMENTS (notch_values); i++) {
        if (g_str_equal (notch, notch_values[i])) {
            g_value_set_uint (value, i);
            return TRUE;
        }
    }

    return FALSE;
}

static GVariant *
notch_set_mapping (const GValue *value, const GVariantType *expected_type, gpointer user_data)
{
    guint i = g_value_get_uint (value);

    if (i >= G_N_ELEMENTS (notch_values))
        return NULL;

    return g_variant_new_string (notch_values[i]);
}

/*
 * Fills the "Notch" row and binds it to org.plinius.display. Returns FALSE if phosh-plinius
 * (which owns the schema) is not installed; the row stays hidden then.
 */
gboolean
cc_plinius_device_setup_notch_row (AdwComboRow *row)
{
    GSettingsSchemaSource *source = g_settings_schema_source_get_default ();
    g_autoptr(GSettingsSchema) schema = NULL;
    g_autoptr(GSettings) settings = NULL;
    g_autoptr(GtkStringList) names = NULL;

    if (source)
        schema = g_settings_schema_source_lookup (source, NOTCH_SCHEMA, TRUE);
    if (!schema)
        return FALSE;

    /* Same order as notch_values */
    names = gtk_string_list_new (NULL);
    gtk_string_list_append (names, P_("Automatic"));
    gtk_string_list_append (names, P_("None"));
    gtk_string_list_append (names, P_("Left"));
    gtk_string_list_append (names, P_("Center"));
    gtk_string_list_append (names, P_("Right"));

    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_("Notch"));
    adw_action_row_set_subtitle (ADW_ACTION_ROW (row), P_("Top bar icons make room for the camera"));
    adw_combo_row_set_model (row, G_LIST_MODEL (names));

    /* The binding keeps its own reference on the settings */
    settings = g_settings_new_full (schema, NULL, NULL);
    g_settings_bind_with_mapping (settings, NOTCH_KEY, row, "selected", G_SETTINGS_BIND_DEFAULT,
                                  notch_get_mapping, notch_set_mapping, NULL, NULL);

    return TRUE;
}
