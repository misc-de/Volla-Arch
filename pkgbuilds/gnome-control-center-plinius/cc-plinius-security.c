/*
 * cc-plinius-security.c
 *
 * Plinius: the "Security" panel's pages (cc-security-panel.c), instead of fwupd's Device Security
 * (its host security checks are made for PC firmware; a phone has neither UEFI nor a TPM). Three
 * pages:
 *
 * - Security (the panel's own page): checks rated good / needs attention / at risk,
 *   from what the system keeps:
 *     screen lock     /var/lib/plinius-security/lock/<user> (pam_plinius_secstate.so, the PIN's
 *                     rating), ~/.config/plinius/unlock-pattern, plinius-pinguard in the lock
 *                     screen's PAM stack and switched on (growing wait after wrong tries)
 *     encryption      plinius-encrypt --status, /var/lib/plinius-crypt/strength (plinius-pwrate)
 *     boot chain      /run/plinius-bootcheck (plinius-bootcheck); after a change the row
 *                     reports tampering and leads to "plinius-boot-chain"
 *     bootloader      information only: it stays unlocked by design
 *     packages        SigLevel in /etc/pacman.conf
 *     access control  /sys/kernel/security/lsm, /sys/fs/selinux/enforce,
 *                     /sys/kernel/security/apparmor/profiles (root) or policy/profiles/<profile>/{name,mode}
 *                     (user) against the profiles
 *                     plinius-apparmor should have loaded (/usr/share/plinius-apparmor/expected);
 *                     leads to "plinius-access-control"
 *   and rows that lead to the other two pages.
 * - Connections (tag "plinius-connections"): which app connected where in the last 15 minutes
 *   up to a week, from plinius-netlogd (D-Bus org.plinius.NetLog1), internet and local network
 *   apart.
 * - Known Vulnerabilities (tag "plinius-vulnerabilities"): issues of the installed packages from
 *   the Arch Linux security tracker with our own assessments, /var/lib/plinius-cve/cve.json
 *   (plinius-cve-update), each with links to its CVE record and tracker entry.
 * - Boot Chain (tag "plinius-boot-chain"): what changed, what to do, and "Accept Changes", which
 *   runs "pkexec plinius-bootcheck accept PATH..." for the reported parts (PIN, see
 *   org.plinius.bootcheck.policy) and so ends the alarm.
 * - Access Control (tag "plinius-access-control"): which mandatory access control the kernel
 *   runs (AppArmor or SELinux) and, for AppArmor, each profile of plinius-apparmor with its mode.
 *
 * Our own strings use the gettext domain plinius-control-center (not GNOME's po/).
 *
 * SPDX-License-Identifier: MIT
 */

#undef G_LOG_DOMAIN
#define G_LOG_DOMAIN "cc-plinius-security"

#include <gio/gdesktopappinfo.h>
#include <glib/gi18n.h>
#include <json-glib/json-glib.h>
#include <string.h>
#include <sys/utsname.h>

#include "cc-plinius-security.h"

#define PLINIUS_DOMAIN "plinius-control-center"
#define P_(String) g_dgettext (PLINIUS_DOMAIN, String)
#define NP_(Singular, Plural, N) g_dngettext (PLINIUS_DOMAIN, Singular, Plural, N)

#define LOCK_DIR       "/var/lib/plinius-security/lock"
#define CRYPT_TOOL     "/usr/bin/plinius-encrypt"
#define CRYPT_STRENGTH "/var/lib/plinius-crypt/strength"
#define BOOTCHECK_RUN  "/run/plinius-bootcheck"
#define BOOTCHECK_TOOL "/usr/bin/plinius-bootcheck"
/* Exit codes of pkexec: prompt dismissed, not authorized */
#define PKEXEC_DISMISSED      126
#define PKEXEC_NOT_AUTHORIZED 127
#define PINGUARD_PAM   "/usr/lib/security/pam_plinius_pinguard.so"
/* Exists = the wait is switched on (Screen Lock page, plinius-pinguard-set) */
#define PINGUARD_ON    "/etc/plinius-pinguard/enabled"
#define CVE_FILE       "/var/lib/plinius-cve/cve.json"
/* Mandatory access control: all world-readable */
#define LSM_FILE          "/sys/kernel/security/lsm"
#define SELINUX_ENFORCE   "/sys/fs/selinux/enforce"
#define AA_PROFILES_FILE  "/sys/kernel/security/apparmor/profiles"
#define AA_POLICY_DIR     "/sys/kernel/security/apparmor/policy/profiles"
#define AA_EXPECTED_FILE  "/usr/share/plinius-apparmor/expected"

#define NETLOG_NAME  "org.plinius.NetLog1"
#define NETLOG_PATH  "/org/plinius/NetLog1"

/* Worst first, so that MIN () of two levels is the worse one */
typedef enum {
    LEVEL_BAD,
    LEVEL_WARNING,
    LEVEL_GOOD,
    LEVEL_INFO,  /* information only, not part of the overall rating */
} Level;

typedef struct {
    AdwNavigationPage *page;
    GCancellable      *cancellable;
    /* Device Security */
    AdwActionRow      *overall_row;
    GtkImage          *overall_icon;
    AdwPreferencesGroup *checks_group;
    GPtrArray         *check_rows;
    AdwActionRow      *connections_row;
    AdwActionRow      *vulnerabilities_row;
    GtkImage          *vulnerabilities_icon;
    Level              vulnerabilities_level;
    /* Connections */
    AdwNavigationPage *connections_page;
    AdwPreferencesPage *connections_prefs;
    AdwToggleGroup    *range_toggles;
    AdwPreferencesGroup *connections_summary;
    GPtrArray         *connection_groups;
    /* Known Vulnerabilities */
    AdwNavigationPage *vulnerabilities_page;
    AdwPreferencesPage *vulnerabilities_prefs;
    GPtrArray         *vulnerability_groups;
    /* Boot Chain */
    AdwNavigationPage *boot_page;
    AdwPreferencesPage *boot_prefs;
    GPtrArray         *boot_groups;
    AdwPreferencesGroup *accept_group;
    GtkWidget         *accept_button;
    gboolean           accepting;
    /* Access Control */
    AdwNavigationPage *mac_page;
    AdwPreferencesPage *mac_prefs;
    GPtrArray         *mac_groups;
} SecurityPage;

static void
security_page_free (SecurityPage *sp)
{
    g_cancellable_cancel (sp->cancellable);
    g_clear_object (&sp->cancellable);
    g_clear_pointer (&sp->check_rows, g_ptr_array_unref);
    g_clear_pointer (&sp->connection_groups, g_ptr_array_unref);
    g_clear_pointer (&sp->vulnerability_groups, g_ptr_array_unref);
    g_clear_pointer (&sp->boot_groups, g_ptr_array_unref);
    g_clear_pointer (&sp->mac_groups, g_ptr_array_unref);
    g_free (sp);
}

/* ----- Helpers ----- */

static void
set_level_icon (GtkImage *image, Level level)
{
    static const char * const icons[] = {
        [LEVEL_BAD] = "dialog-error-symbolic",
        [LEVEL_WARNING] = "dialog-warning-symbolic",
        [LEVEL_GOOD] = "object-select-symbolic",
        [LEVEL_INFO] = "dialog-information-symbolic",
    };
    static const char * const classes[] = {
        [LEVEL_BAD] = "error",
        [LEVEL_WARNING] = "warning",
        [LEVEL_GOOD] = "success",
        [LEVEL_INFO] = "dimmed",
    };

    gtk_image_set_from_icon_name (image, icons[level]);
    for (guint i = 0; i < G_N_ELEMENTS (classes); i++)
        gtk_widget_remove_css_class (GTK_WIDGET (image), classes[i]);
    gtk_widget_add_css_class (GTK_WIDGET (image), classes[level]);
}

/* Reads a key=value file (as written by plinius-pwrate and pam_plinius_secstate.so) */
static GHashTable *
read_keyfile (const char *path)
{
    g_autofree char *contents = NULL;
    g_auto(GStrv) lines = NULL;
    GHashTable *values;

    if (!g_file_get_contents (path, &contents, NULL, NULL))
        return NULL;
    values = g_hash_table_new_full (g_str_hash, g_str_equal, g_free, g_free);
    lines = g_strsplit (contents, "\n", -1);
    for (char **l = lines; *l; l++) {
        char *eq = strchr (*l, '=');
        if (eq)
            g_hash_table_insert (values, g_strndup (*l, eq - *l), g_strdup (eq + 1));
    }
    return values;
}

static gboolean
file_contains (const char *path, const char *text)
{
    g_autofree char *contents = NULL;

    return g_file_get_contents (path, &contents, NULL, NULL) && strstr (contents, text);
}

/* "14:02" today, otherwise date and time */
static char *
format_time (gint64 unix_time)
{
    g_autoptr(GDateTime) dt = g_date_time_new_from_unix_local (unix_time);
    g_autoptr(GDateTime) now = g_date_time_new_now_local ();

    if (!dt)
        return g_strdup ("");
    if (g_date_time_get_year (dt) == g_date_time_get_year (now) &&
        g_date_time_get_day_of_year (dt) == g_date_time_get_day_of_year (now))
        return g_date_time_format (dt, "%H:%M");
    return g_date_time_format (dt, "%x %H:%M");
}

static void
launch_uri (GtkWidget *widget, const char *uri)
{
    g_autoptr(GtkUriLauncher) launcher = gtk_uri_launcher_new (uri);

    gtk_uri_launcher_launch (launcher, GTK_WINDOW (gtk_widget_get_root (widget)), NULL, NULL, NULL);
}

static void
link_row_activated_cb (AdwActionRow *row)
{
    launch_uri (GTK_WIDGET (row), g_object_get_data (G_OBJECT (row), "uri"));
}

static GtkWidget *
link_row_new (const char *title, const char *uri)
{
    GtkWidget *row = adw_action_row_new ();

    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), title);
    adw_action_row_set_subtitle (ADW_ACTION_ROW (row), uri);
    adw_action_row_add_suffix (ADW_ACTION_ROW (row), gtk_image_new_from_icon_name ("adw-external-link-symbolic"));
    gtk_list_box_row_set_activatable (GTK_LIST_BOX_ROW (row), TRUE);
    g_object_set_data_full (G_OBJECT (row), "uri", g_strdup (uri), g_free);
    g_signal_connect (row, "activated", G_CALLBACK (link_row_activated_cb), NULL);
    return row;
}

/* Removes the groups a page built last time */
static void
clear_groups (AdwPreferencesPage *prefs, GPtrArray *groups)
{
    for (guint i = 0; i < groups->len; i++)
        adw_preferences_page_remove (prefs, g_ptr_array_index (groups, i));
    g_ptr_array_set_size (groups, 0);
}

static AdwPreferencesGroup *
add_group (AdwPreferencesPage *prefs, GPtrArray *groups, const char *title, const char *description)
{
    GtkWidget *group = adw_preferences_group_new ();

    if (title)
        adw_preferences_group_set_title (ADW_PREFERENCES_GROUP (group), title);
    if (description)
        adw_preferences_group_set_description (ADW_PREFERENCES_GROUP (group), description);
    adw_preferences_page_add (prefs, ADW_PREFERENCES_GROUP (group));
    g_ptr_array_add (groups, group);
    return ADW_PREFERENCES_GROUP (group);
}

/* ----- Checks ----- */

typedef struct {
    const char *title;
    char       *subtitle;
    Level       level;
    /* Activating the row pushes this page of the panel */
    const char *page_tag;
} Check;

static void
check_screen_lock (Check *c)
{
    g_autofree char *rating_file = g_build_filename (LOCK_DIR, g_get_user_name (), NULL);
    g_autofree char *pattern_file = g_build_filename (g_get_user_config_dir (), "plinius", "unlock-pattern", NULL);
    g_autoptr(GHashTable) rating = read_keyfile (rating_file);
    gboolean waits = g_file_test (PINGUARD_PAM, G_FILE_TEST_EXISTS) &&
                     file_contains ("/etc/pam.d/phosh", "pam_plinius_pinguard") &&
                     g_file_test (PINGUARD_ON, G_FILE_TEST_EXISTS);
    const char *level = rating ? g_hash_table_lookup (rating, "level") : NULL;
    const char *text;

    c->title = P_("Screen Lock");
    /* Lock & Unlock as a page of this panel (cc-security-panel.c), so back leads here */
    c->page_tag = "lock";
    if (g_file_test (pattern_file, G_FILE_TEST_EXISTS)) {
        /* GNOME Settings only accepts patterns of at least 6 points */
        c->level = LEVEL_GOOD;
        text = P_("Unlock pattern of at least 6 points.");
    } else if (level == NULL) {
        c->level = LEVEL_WARNING;
        text = P_("The PIN is not rated yet. Set it again to rate it.");
    } else if (g_str_equal (level, "good")) {
        c->level = LEVEL_GOOD;
        text = P_("PIN of at least 6 digits.");
    } else {
        c->level = LEVEL_BAD;
        text = P_("The PIN is shorter than 6 digits. Choose a longer one.");
    }
    if (waits) {
        c->subtitle = g_strdup (text);
    } else {
        c->level = MIN (c->level, LEVEL_WARNING);
        c->subtitle = g_strconcat (text, " ", P_("Wrong tries do not make the lock screen wait, a short PIN can be guessed by trying."), NULL);
    }
}

static void
check_encryption (Check *c)
{
    g_autoptr(GSubprocess) process = NULL;
    g_autoptr(GHashTable) strength = NULL;
    g_autofree char *out = NULL;
    const char *state = "", *level;

    c->title = P_("Encryption");
    if (!g_file_test (CRYPT_TOOL, G_FILE_TEST_IS_EXECUTABLE)) {
        c->level = LEVEL_BAD;
        c->subtitle = g_strdup (P_("Not available: plinius-crypt is not installed. Whoever has the phone in hand can read its data."));
        return;
    }
    c->page_tag = "encryption";
    process = g_subprocess_new (G_SUBPROCESS_FLAGS_STDOUT_PIPE | G_SUBPROCESS_FLAGS_STDERR_SILENCE, NULL,
                                CRYPT_TOOL, "--status", NULL);
    if (process && g_subprocess_communicate_utf8 (process, NULL, NULL, &out, NULL, NULL) && out)
        state = g_strstrip (out);

    if (g_str_equal (state, "off")) {
        c->level = LEVEL_BAD;
        c->subtitle = g_strdup (P_("Not encrypted. Whoever has the phone in hand can read its data."));
        return;
    }
    if (g_str_equal (state, "requested")) {
        c->level = LEVEL_WARNING;
        c->subtitle = g_strdup (P_("Encryption at the next restart. Until then the data can be read."));
        return;
    }
    if (!g_str_equal (state, "encrypted")) {
        c->level = LEVEL_BAD;
        c->subtitle = g_strdup (P_("Not encrypted, and this system cannot be encrypted: it does not start with the encryption support."));
        return;
    }

    /* Encrypted: the password's strength decides. Its header can be attacked away from the
     * phone, without any wait between tries, so it has to be strong on its own. */
    strength = read_keyfile (CRYPT_STRENGTH);
    level = strength ? g_hash_table_lookup (strength, "level") : NULL;
    if (level == NULL) {
        c->level = LEVEL_WARNING;
        c->subtitle = g_strdup (P_("Encrypted. The password is not rated yet. Change it once to rate it."));
    } else if (g_str_equal (level, "good")) {
        c->level = LEVEL_GOOD;
        c->subtitle = g_strdup (P_("Encrypted with a strong password."));
    } else if (g_str_equal (level, "fair")) {
        c->level = LEVEL_WARNING;
        c->subtitle = g_strdup (P_("Encrypted, but the password could be found with enough computing power. "
                                   "Choose 12 or more mixed characters or 5 random words."));
    } else {
        const char *dict = g_hash_table_lookup (strength, "dictionary");
        c->level = LEVEL_BAD;
        c->subtitle = g_strdup (dict && g_str_equal (dict, "word")
                                ? P_("Encrypted, but the password is a dictionary word or close to one. Change the password.")
                                : P_("Encrypted, but the password is too short or too simple. Change the password."));
    }
}

/* What plinius-bootcheck reported as changed at this start, one path per element; NULL if
   nothing (or not checked yet) */
static GStrv
boot_chain_changes (void)
{
    g_autofree char *changed = NULL;
    g_auto(GStrv) lines = NULL;
    g_autoptr(GStrvBuilder) paths = g_strv_builder_new ();
    guint n = 0;

    if (!g_file_get_contents (BOOTCHECK_RUN "/changed", &changed, NULL, NULL))
        return NULL;
    lines = g_strsplit (changed, "\n", -1);
    for (guint i = 0; lines[i]; i++) {
        g_strstrip (lines[i]);
        if (*lines[i]) {
            g_strv_builder_add (paths, lines[i]);
            n++;
        }
    }
    return n ? g_strv_builder_end (paths) : NULL;
}

/* A name for a checked part (plinius-bootcheck's lists): the partition's role with its label
   ("Kernel (boot_b)"), an entry or file of the boot menu card, else the path itself */
static char *
boot_part_name (const char *path)
{
    static const struct {
        const char *partition;
        const char *name;  /* translated when shown */
    } parts[] = {
        { "lk", N_("Bootloader") },
        { "boot", N_("Kernel") },
        { "init_boot", N_("Ramdisk") },
        { "vendor_boot", N_("Vendor Ramdisk") },
        { "dtbo", N_("Device Tree") },
        { "vbmeta", N_("Boot Verification Data") },
        { "vbmeta_system", N_("Boot Verification Data") },
        { "vbmeta_vendor", N_("Boot Verification Data") },
    };
    const char *entries = "abm:/db/entries/";
    g_autofree char *label = NULL;
    g_autofree char *partition = NULL;
    gsize len;

    if (g_str_has_prefix (path, entries) && g_str_has_suffix (path, ".conf")) {
        g_autofree char *entry = g_strndup (path + strlen (entries), strlen (path) - strlen (entries) - strlen (".conf"));
        return g_strdup_printf (P_("Boot menu entry “%s”"), entry);
    }
    if (g_str_has_prefix (path, "abm:"))
        return g_strdup_printf (P_("Boot menu card: %s"), path + strlen ("abm:"));

    label = g_path_get_basename (path);
    len = strlen (label);
    /* the slot's suffix */
    partition = len > 2 && (g_str_has_suffix (label, "_a") || g_str_has_suffix (label, "_b"))
                ? g_strndup (label, len - 2) : g_strdup (label);
    for (guint i = 0; i < G_N_ELEMENTS (parts); i++)
        if (g_str_equal (partition, parts[i].partition))
            return g_strdup_printf ("%s (%s)", P_(parts[i].name), label);
    return g_strdup (path);
}

/* FALSE if plinius-bootcheck is not installed */
static gboolean
check_boot_chain (Check *c)
{
    g_auto(GStrv) changes = NULL;

    if (!g_file_test (BOOTCHECK_TOOL, G_FILE_TEST_IS_EXECUTABLE))
        return FALSE;
    c->title = P_("Boot Chain");
    changes = boot_chain_changes ();
    if (changes) {
        g_autoptr(GPtrArray) names = g_ptr_array_new_with_free_func (g_free);
        g_autofree char *list = NULL;

        for (guint i = 0; changes[i]; i++)
            g_ptr_array_add (names, boot_part_name (changes[i]));
        g_ptr_array_add (names, NULL);
        list = g_strjoinv (", ", (char **) names->pdata);
        c->level = LEVEL_BAD;
        c->subtitle = g_strdup_printf (P_("Tampering detected: %s changed since the last accepted state."), list);
        c->page_tag = "plinius-boot-chain";
    } else if (g_file_test (BOOTCHECK_RUN "/done", G_FILE_TEST_EXISTS)) {
        c->level = LEVEL_GOOD;
        c->subtitle = g_strdup (P_("Bootloader, kernel and ramdisk are unchanged since the last accepted state."));
    } else {
        c->level = LEVEL_WARNING;
        c->subtitle = g_strdup (P_("Not checked at this start yet."));
    }
    return TRUE;
}

static void
check_bootloader (Check *c)
{
    c->title = P_("Bootloader");
    c->level = LEVEL_INFO;
    c->subtitle = g_strdup (P_("Unlocked, so that this system can start. Whoever has the phone in hand can start a different system; "
                               "the encryption keeps the data safe, the boot chain check notices changes."));
}

/* Signature checks of pacman: a SigLevel with Never or Optional for packages lets unsigned
 * packages in. Each word applies to packages and databases, or with "Package"/"Database" in
 * front to one of them; a later word overrides an earlier one, as in pacman. */
static gboolean
siglevel_weak (const char *line)
{
    const char *value = strchr (line, '=');
    g_auto(GStrv) words = NULL;
    gboolean weak = FALSE;

    if (!value)
        return FALSE;
    words = g_strsplit_set (value + 1, " \t", -1);
    for (char **w = words; *w; w++) {
        const char *level = *w;

        if (g_str_has_prefix (level, "Database"))
            continue;
        if (g_str_has_prefix (level, "Package"))
            level += strlen ("Package");
        if (g_str_equal (level, "Never") || g_str_equal (level, "Optional"))
            weak = TRUE;
        else if (g_str_equal (level, "Required"))
            weak = FALSE;
    }
    return weak;
}

static void
check_packages (Check *c)
{
    g_autofree char *contents = NULL;
    g_auto(GStrv) lines = NULL;
    gboolean weak = FALSE;

    c->title = P_("Software Sources");
    if (!g_file_get_contents ("/etc/pacman.conf", &contents, NULL, NULL)) {
        c->level = LEVEL_WARNING;
        c->subtitle = g_strdup (P_("The package settings cannot be read."));
        return;
    }
    lines = g_strsplit (contents, "\n", -1);
    for (char **l = lines; *l; l++) {
        char *line = g_strstrip (*l);
        if (g_str_has_prefix (line, "SigLevel") && siglevel_weak (line))
            weak = TRUE;
    }
    if (weak) {
        c->level = LEVEL_BAD;
        c->subtitle = g_strdup (P_("Packages without a valid signature are accepted (SigLevel in /etc/pacman.conf)."));
    } else {
        c->level = LEVEL_GOOD;
        c->subtitle = g_strdup (P_("Updates are only installed with a valid signature."));
    }
}

/* ----- Access control ----- */

typedef enum { MAC_NONE, MAC_APPARMOR, MAC_SELINUX } MacKind;

typedef struct {
    MacKind     kind;
    gboolean    selinux_enforcing;
    /* AppArmor: mode of each loaded profile ("enforce", "complain", ...) by name, and the names
     * plinius-apparmor should have loaded */
    GHashTable *modes;
    GStrv       expected;
    guint       enforcing, complaining, missing;
} MacState;

static void
mac_state_clear (MacState *m)
{
    g_clear_pointer (&m->modes, g_hash_table_unref);
    g_clear_pointer (&m->expected, g_strfreev);
}

static void
mac_state_read (MacState *m)
{
    g_autofree char *lsm = NULL, *profiles = NULL, *expected = NULL, *enforce = NULL;
    g_auto(GStrv) lsms = NULL;

    memset (m, 0, sizeof *m);
    m->modes = g_hash_table_new_full (g_str_hash, g_str_equal, g_free, g_free);
    if (g_file_get_contents (LSM_FILE, &lsm, NULL, NULL)) {
        lsms = g_strsplit (g_strstrip (lsm), ",", -1);
        if (g_strv_contains ((const char * const *) lsms, "selinux"))
            m->kind = MAC_SELINUX;
        else if (g_strv_contains ((const char * const *) lsms, "apparmor"))
            m->kind = MAC_APPARMOR;
    }
    if (m->kind == MAC_SELINUX && g_file_get_contents (SELINUX_ENFORCE, &enforce, NULL, NULL))
        m->selinux_enforcing = g_ascii_strtoll (enforce, NULL, 10) == 1;

    /* "name (mode)" per line; the kernel only hands this file to root despite its 0444 mode */
    if (m->kind == MAC_APPARMOR && g_file_get_contents (AA_PROFILES_FILE, &profiles, NULL, NULL)) {
        g_auto(GStrv) lines = g_strsplit (profiles, "\n", -1);
        for (char **l = lines; *l; l++) {
            char *open = strrchr (*l, '(');
            char *close = open ? strchr (open, ')') : NULL;
            if (!open || !close || open == *l)
                continue;
            g_hash_table_replace (m->modes, g_strstrip (g_strndup (*l, open - *l)),
                                  g_strndup (open + 1, close - open - 1));
        }
    } else if (m->kind == MAC_APPARMOR) {
        /* As a user: one directory per top-level profile with readable "name" and "mode" */
        g_autoptr(GDir) dir = g_dir_open (AA_POLICY_DIR, 0, NULL);
        const char *entry;
        while (dir && (entry = g_dir_read_name (dir))) {
            g_autofree char *name_path = g_build_filename (AA_POLICY_DIR, entry, "name", NULL);
            g_autofree char *mode_path = g_build_filename (AA_POLICY_DIR, entry, "mode", NULL);
            g_autofree char *name = NULL, *mode = NULL;
            if (!g_file_get_contents (name_path, &name, NULL, NULL) ||
                !g_file_get_contents (mode_path, &mode, NULL, NULL))
                continue;
            g_strstrip (name);
            g_strstrip (mode);
            if (*name && *mode)
                g_hash_table_replace (m->modes, g_steal_pointer (&name), g_steal_pointer (&mode));
        }
    }
    if (g_file_get_contents (AA_EXPECTED_FILE, &expected, NULL, NULL))
        m->expected = g_strsplit (g_strstrip (expected), "\n", -1);
    else
        m->expected = g_new0 (char *, 1);
    for (char **e = m->expected; *e; e++) {
        const char *mode = g_hash_table_lookup (m->modes, *e);
        if (!**e)
            continue;
        if (!mode)
            m->missing++;
        else if (g_str_equal (mode, "enforce"))
            m->enforcing++;
        else
            m->complaining++;
    }
}

static void
check_access_control (Check *c)
{
    MacState m;

    mac_state_read (&m);
    c->title = P_("Access Control");
    c->page_tag = "plinius-access-control";
    if (m.kind == MAC_SELINUX) {
        c->level = m.selinux_enforcing ? LEVEL_GOOD : LEVEL_WARNING;
        c->subtitle = g_strdup (m.selinux_enforcing ? P_("SELinux enforces its policy.")
                                                    : P_("SELinux is on but only logs."));
    } else if (m.kind == MAC_NONE) {
        c->level = LEVEL_BAD;
        c->subtitle = g_strdup (P_("No access control. A program that is taken over can reach everything its user can."));
    } else if (m.enforcing + m.complaining == 0) {
        c->level = LEVEL_WARNING;
        c->subtitle = g_strdup (P_("AppArmor is on, but no program is confined yet."));
    } else if (m.complaining || m.missing) {
        c->level = LEVEL_WARNING;
        if (m.enforcing)
            c->subtitle = g_strdup_printf (NP_("AppArmor confines %u service; more only log what they do.",
                                               "AppArmor confines %u services; more only log what they do.",
                                               m.enforcing), m.enforcing);
        else
            c->subtitle = g_strdup_printf (NP_("AppArmor watches %u service without blocking it yet.",
                                               "AppArmor watches %u services without blocking them yet.",
                                               m.complaining), m.complaining);
    } else {
        c->level = LEVEL_GOOD;
        c->subtitle = g_strdup_printf (NP_("AppArmor confines %u service of the network and the radio.",
                                           "AppArmor confines %u services of the network and the radio.",
                                           m.enforcing), m.enforcing);
    }
    mac_state_clear (&m);
}

static void
check_row_activated_cb (AdwActionRow *row)
{
    const char *tag = g_object_get_data (G_OBJECT (row), "page-tag");

    gtk_widget_activate_action (GTK_WIDGET (row), "navigation.push", "s", tag);
}

static GtkWidget *
check_row_new (Check *c)
{
    GtkWidget *row = adw_action_row_new ();
    GtkWidget *icon = gtk_image_new ();

    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), c->title);
    adw_action_row_set_subtitle (ADW_ACTION_ROW (row), c->subtitle);
    set_level_icon (GTK_IMAGE (icon), c->level);
    adw_action_row_add_prefix (ADW_ACTION_ROW (row), icon);
    if (c->page_tag) {
        g_object_set_data (G_OBJECT (row), "page-tag", (gpointer) c->page_tag);
        adw_action_row_add_suffix (ADW_ACTION_ROW (row), gtk_image_new_from_icon_name ("go-next-symbolic"));
        gtk_list_box_row_set_activatable (GTK_LIST_BOX_ROW (row), TRUE);
        g_signal_connect (row, "activated", G_CALLBACK (check_row_activated_cb), NULL);
    }
    return row;
}

/* ----- Known vulnerabilities ----- */

typedef struct {
    guint total;
    guint affecting;      /* assessed: affects this system (also when mitigated) */
    guint severe;         /* assessed "affected" (not mitigated), High or Critical */
    guint unassessed;
} VulnSummary;

static JsonNode *
load_cve_file (void)
{
    g_autoptr(JsonParser) parser = json_parser_new ();

    if (!json_parser_load_from_file (parser, CVE_FILE, NULL))
        return NULL;
    return json_parser_steal_root (parser);
}

static const char *
member_string (JsonObject *o, const char *name)
{
    JsonNode *n = o ? json_object_get_member (o, name) : NULL;

    return n && JSON_NODE_HOLDS_VALUE (n) && json_node_get_value_type (n) == G_TYPE_STRING
           ? json_node_get_string (n) : NULL;
}

static JsonObject *
member_object (JsonObject *o, const char *name)
{
    JsonNode *n = o ? json_object_get_member (o, name) : NULL;

    return n && JSON_NODE_HOLDS_OBJECT (n) ? json_node_get_object (n) : NULL;
}

static gint64
member_int (JsonObject *o, const char *name)
{
    JsonNode *n = o ? json_object_get_member (o, name) : NULL;

    return n && JSON_NODE_HOLDS_VALUE (n) ? json_node_get_int (n) : 0;
}

static double
member_double (JsonObject *o, const char *name)
{
    JsonNode *n = o ? json_object_get_member (o, name) : NULL;

    return n && JSON_NODE_HOLDS_VALUE (n) ? json_node_get_double (n) : 0;
}

static JsonArray *
member_array (JsonObject *o, const char *name)
{
    JsonNode *n = o ? json_object_get_member (o, name) : NULL;

    return n && JSON_NODE_HOLDS_ARRAY (n) ? json_node_get_array (n) : NULL;
}

/* The kernel's own vulnerabilities (kernel CNA, filtered by the code it builds), next to the
   packages' ones: {"listed", "other", "issues": [...], ...} under "kernel" -> "cves" */
static JsonObject *
kernel_cves (JsonObject *o)
{
    return member_object (member_object (o, "kernel"), "cves");
}

static gboolean
is_severe (const char *severity)
{
    return severity && (g_str_equal (severity, "High") || g_str_equal (severity, "Critical"));
}

static const char *
assessment_status (JsonObject *issue)
{
    return member_string (member_object (issue, "assessment"), "status");
}

static VulnSummary
summarize (JsonArray *issues)
{
    VulnSummary s = { 0 };

    for (guint i = 0; issues && i < json_array_get_length (issues); i++) {
        JsonObject *issue = json_array_get_object_element (issues, i);
        const char *status = assessment_status (issue);

        s.total++;
        if (status == NULL) {
            s.unassessed++;
        } else if (g_str_equal (status, "affected") || g_str_equal (status, "mitigated")) {
            s.affecting++;
            if (g_str_equal (status, "affected") && is_severe (member_string (issue, "severity")))
                s.severe++;
        }
    }
    return s;
}

static void
update_vulnerabilities_row (SecurityPage *sp, JsonNode *root)
{
    JsonObject *o = root && JSON_NODE_HOLDS_OBJECT (root) ? json_node_get_object (root) : NULL;
    JsonNode *issues_node = o ? json_object_get_member (o, "issues") : NULL;
    JsonArray *issues = issues_node && JSON_NODE_HOLDS_ARRAY (issues_node) ? json_node_get_array (issues_node) : NULL;
    VulnSummary s = summarize (issues);
    /* The kernel's issues from a high rating on carry assessments like the packages' ones, so
       they count in the same sums: "2 in packages" next to "6 kernel issues" read as 2 in all */
    VulnSummary k = summarize (o ? member_array (kernel_cves (o), "issues") : NULL);
    guint total = s.total + k.total;
    g_autofree char *text = NULL;

    if (!o) {
        sp->vulnerabilities_level = LEVEL_WARNING;
        text = g_strdup (P_("Not checked yet. The list is loaded once a day."));
    } else if (total == 0) {
        sp->vulnerabilities_level = LEVEL_GOOD;
        text = g_strdup (P_("No known vulnerabilities in the installed packages."));
    } else {
        g_autofree char *affecting = g_strdup_printf (NP_("%u affects this system", "%u affect this system",
                                                          s.affecting + k.affecting), s.affecting + k.affecting);
        g_autofree char *unassessed = g_strdup_printf (NP_("%u not assessed", "%u not assessed",
                                                           s.unassessed + k.unassessed), s.unassessed + k.unassessed);
        if (k.total > 0) {
            g_autofree char *all = g_strdup_printf (NP_("%u known vulnerability", "%u known vulnerabilities", total),
                                                    total);
            g_autofree char *where = g_strdup_printf (P_("%u in installed packages, %u in the kernel"),
                                                      s.total, k.total);
            text = g_strdup_printf ("%s (%s): %s, %s", all, where, affecting, unassessed);
        } else {
            g_autofree char *all = g_strdup_printf (NP_("%u known vulnerability in installed packages",
                                                        "%u known vulnerabilities in installed packages", total),
                                                    total);
            text = g_strdup_printf ("%s: %s, %s", all, affecting, unassessed);
        }
        /* A severe issue that really affects the phone is a risk; the rest wants a look. The
           kernel's issues only ask for a look: they are fixed by a newer kernel, not by an update
           of the packages, and one rated high there is mostly far from an attack on this phone */
        if (s.severe > 0)
            sp->vulnerabilities_level = LEVEL_BAD;
        else if (s.affecting > 0 || s.unassessed > 0 || k.total > 0)
            sp->vulnerabilities_level = LEVEL_WARNING;
        else
            sp->vulnerabilities_level = LEVEL_GOOD;
    }
    adw_action_row_set_subtitle (sp->vulnerabilities_row, text);
    set_level_icon (sp->vulnerabilities_icon, sp->vulnerabilities_level);
}

/* Sort: affecting first, then by severity, newest CVE first */
static int
severity_rank (const char *severity)
{
    static const char * const order[] = { "Critical", "High", "Medium", "Low" };

    for (guint i = 0; severity && i < G_N_ELEMENTS (order); i++)
        if (g_str_equal (severity, order[i]))
            return i;
    return G_N_ELEMENTS (order);
}

static int
compare_issues (gconstpointer a, gconstpointer b)
{
    JsonObject *x = *(JsonObject **) a, *y = *(JsonObject **) b;
    int r = severity_rank (member_string (x, "severity")) - severity_rank (member_string (y, "severity"));

    if (r)
        return r;
    return -g_strcmp0 (member_string (x, "cve"), member_string (y, "cve"));
}

static const char *
assessment_label (const char *status)
{
    if (status == NULL)
        return P_("Not assessed");
    if (g_str_equal (status, "affected"))
        return P_("Affects this system");
    if (g_str_equal (status, "mitigated"))
        return P_("Mitigated");
    if (g_str_equal (status, "fixed"))
        return P_("Fixed here");
    return P_("Does not affect this system");
}

static GtkWidget *
issue_row_new (JsonObject *issue)
{
    const char *cve = member_string (issue, "cve");
    const char *group = member_string (issue, "group");
    const char *package = member_string (issue, "package");
    const char *installed = member_string (issue, "installed");
    const char *severity = member_string (issue, "severity");
    const char *type = member_string (issue, "type");
    const char *fixed = member_string (issue, "fixed");
    const char *affected = member_string (issue, "affected");
    JsonObject *assessment = member_object (issue, "assessment");
    const char *status = member_string (assessment, "status");
    const char *note = member_string (assessment, "note");
    g_autoptr(GString) subtitle = g_string_new (package ? package : "");
    g_autofree char *uri = NULL;
    GtkWidget *row, *child, *label;

    if (installed)
        g_string_append_printf (subtitle, " %s", installed);
    if (severity && !g_str_equal (severity, "Unknown"))
        g_string_append_printf (subtitle, " · %s", severity);
    if (type && *type && !g_str_equal (type, "unknown"))
        g_string_append_printf (subtitle, " · %s", type);

    row = adw_expander_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), cve ? cve : "");
    adw_preferences_row_set_use_markup (ADW_PREFERENCES_ROW (row), FALSE);
    adw_expander_row_set_subtitle (ADW_EXPANDER_ROW (row), subtitle->str);
    label = gtk_label_new (assessment_label (status));
    gtk_widget_add_css_class (label, "caption");
    if (status == NULL)
        gtk_widget_add_css_class (label, "dimmed");
    else if (g_str_equal (status, "affected"))
        gtk_widget_add_css_class (label, "error");
    else if (g_str_equal (status, "mitigated"))
        gtk_widget_add_css_class (label, "warning");
    else
        gtk_widget_add_css_class (label, "success");
    adw_expander_row_add_suffix (ADW_EXPANDER_ROW (row), label);

    /* Our assessment */
    child = adw_action_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (child), assessment_label (status));
    adw_preferences_row_set_use_markup (ADW_PREFERENCES_ROW (child), FALSE);
    adw_action_row_set_subtitle (ADW_ACTION_ROW (child),
                                 note ? note : P_("Nobody has checked yet whether and how this issue affects this system."));
    gtk_widget_add_css_class (child, "property");
    adw_expander_row_add_row (ADW_EXPANDER_ROW (row), child);

    /* What the tracker says about versions */
    child = adw_action_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (child), P_("Fix"));
    adw_preferences_row_set_use_markup (ADW_PREFERENCES_ROW (child), FALSE);
    if (fixed) {
        g_autofree char *text = g_strdup_printf (P_("Fixed in %s; an update is due."), fixed);
        adw_action_row_set_subtitle (ADW_ACTION_ROW (child), text);
    } else if (affected) {
        g_autofree char *text = g_strdup_printf (P_("Affected since %s, no fixed version known."), affected);
        adw_action_row_set_subtitle (ADW_ACTION_ROW (child), text);
    } else {
        adw_action_row_set_subtitle (ADW_ACTION_ROW (child), P_("No fixed version known."));
    }
    gtk_widget_add_css_class (child, "property");
    adw_expander_row_add_row (ADW_EXPANDER_ROW (row), child);

    if (cve && g_str_has_prefix (cve, "CVE-")) {
        uri = g_strconcat ("https://www.cve.org/CVERecord?id=", cve, NULL);
        adw_expander_row_add_row (ADW_EXPANDER_ROW (row), link_row_new (P_("CVE Record"), uri));
        g_clear_pointer (&uri, g_free);
    }
    if (group && g_str_has_prefix (group, "AVG-")) {
        uri = g_strconcat ("https://security.archlinux.org/", group, NULL);
        adw_expander_row_add_row (ADW_EXPANDER_ROW (row), link_row_new (P_("Arch Linux Security Tracker"), uri));
    }
    return row;
}

/* Kernel issues: highest score first, newest CVE first */
static int
compare_kernel_issues (gconstpointer a, gconstpointer b)
{
    JsonObject *x = *(JsonObject **) a, *y = *(JsonObject **) b;
    double dx = member_double (x, "score"), dy = member_double (y, "score");

    if (dx != dy)
        return dx < dy ? 1 : -1;
    return -g_strcmp0 (member_string (x, "cve"), member_string (y, "cve"));
}

/* A plain row per kernel issue: there are hundreds, an expander row each would make the page
   slow to build. The subject of the fix says what the issue is, the row opens the CVE record. */
static GtkWidget *
kernel_issue_row_new (JsonObject *issue)
{
    const char *cve = member_string (issue, "cve");
    const char *title = member_string (issue, "title");
    const char *type = member_string (issue, "type");
    const char *fixed = member_string (issue, "fixed");
    const char *status = assessment_status (issue);
    g_autoptr(GString) subtitle = g_string_new (NULL);
    g_autofree char *fix = fixed ? g_strdup_printf (P_("fixed in %s"), fixed) : g_strdup (P_("no fix yet"));
    GtkWidget *row;

    if (title && *title)
        g_string_append_printf (subtitle, "%s\n", title);
    g_string_append_printf (subtitle, "CVSS %.1f", member_double (issue, "score"));
    if (type && *type)
        g_string_append_printf (subtitle, " · %s", type);
    g_string_append_printf (subtitle, " · %s", fix);

    row = adw_action_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), cve ? cve : "");
    adw_preferences_row_set_use_markup (ADW_PREFERENCES_ROW (row), FALSE);
    adw_action_row_set_subtitle (ADW_ACTION_ROW (row), subtitle->str);
    if (status) {
        GtkWidget *label = gtk_label_new (assessment_label (status));

        gtk_widget_add_css_class (label, "caption");
        gtk_widget_add_css_class (label, g_str_equal (status, "affected") ? "error" : "warning");
        adw_action_row_add_suffix (ADW_ACTION_ROW (row), label);
    }
    if (cve && g_str_has_prefix (cve, "CVE-")) {
        adw_action_row_add_suffix (ADW_ACTION_ROW (row), gtk_image_new_from_icon_name ("adw-external-link-symbolic"));
        gtk_list_box_row_set_activatable (GTK_LIST_BOX_ROW (row), TRUE);
        g_object_set_data_full (G_OBJECT (row), "uri",
                                g_strconcat ("https://www.cve.org/CVERecord?id=", cve, NULL), g_free);
        g_signal_connect (row, "activated", G_CALLBACK (link_row_activated_cb), NULL);
    }
    return row;
}

static GtkWidget *
property_row_new (const char *title, const char *subtitle)
{
    GtkWidget *row = adw_action_row_new ();

    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), title);
    adw_action_row_set_subtitle (ADW_ACTION_ROW (row), subtitle);
    gtk_widget_add_css_class (row, "property");
    return row;
}

/* Our own kernel (plinius-kernel) names itself "<version>-plinius<n>"; any other one is the
   phone maker's (the vendor boot image) */
static gboolean
kernel_is_own_build (const char *release)
{
    return release && strstr (release, "-plinius") != NULL;
}

/* The build date of the running kernel @release, as a local date, or NULL. The kernel keeps it
   in its version, written by `LC_ALL=C date`: "#1 SMP PREEMPT Sat Nov  8 19:40:00 CET 2026".
   Only the day counts, the time zone is left out. */
static char *
kernel_build_date (const char *release)
{
    static const char * const months[] = { "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                                           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec" };
    struct utsname uts;
    g_auto(GStrv) words = NULL;
    guint n;

    /* the list may describe a kernel that no longer runs */
    if (uname (&uts) != 0 || g_strcmp0 (uts.release, release) != 0)
        return NULL;

    words = g_strsplit_set (uts.version, " \t", -1);
    n = g_strv_length (words);
    for (guint i = 0; i + 1 < n; i++) {
        for (guint m = 0; m < G_N_ELEMENTS (months); m++) {
            g_autoptr(GDateTime) dt = NULL;
            guint64 day, year;
            guint next = i + 1;

            if (!g_str_equal (words[i], months[m]))
                continue;
            /* "Nov  8": the double space gives an empty word */
            while (next < n && !*words[next])
                next++;
            if (next >= n || !g_ascii_string_to_unsigned (words[next], 10, 1, 31, &day, NULL) ||
                !g_ascii_string_to_unsigned (words[n - 1], 10, 2000, 9999, &year, NULL))
                return NULL;
            dt = g_date_time_new_local (year, m + 1, day, 12, 0, 0);
            return dt ? g_date_time_format (dt, "%x") : NULL;
        }
    }
    return NULL;
}

/* The issues' rows are built when the list is opened the first time: there are hundreds, most
   people never open it */
static void
kernel_issues_expanded_cb (AdwExpanderRow *row, GParamSpec *pspec, gpointer user_data)
{
    JsonArray *issues = g_object_get_data (G_OBJECT (row), "issues");
    g_autoptr(GPtrArray) sorted = NULL;

    if (!issues || !adw_expander_row_get_expanded (row))
        return;

    sorted = g_ptr_array_new ();
    for (guint i = 0; i < json_array_get_length (issues); i++) {
        JsonNode *n = json_array_get_element (issues, i);

        if (JSON_NODE_HOLDS_OBJECT (n))
            g_ptr_array_add (sorted, json_node_get_object (n));
    }
    g_ptr_array_sort (sorted, compare_kernel_issues);
    for (guint i = 0; i < sorted->len; i++)
        adw_expander_row_add_row (row, kernel_issue_row_new (g_ptr_array_index (sorted, i)));

    /* built once: the rows copied what they show, the array can go */
    g_clear_pointer (&sorted, g_ptr_array_unref);
    g_object_set_data (G_OBJECT (row), "issues", NULL);
}

/* The kernel's section: where the list comes from, how many issues, then the ones with a high
   rating in a row to open, as they are many and not assessed one by one */
static void
add_kernel_section (SecurityPage *sp, JsonObject *o)
{
    JsonObject *kernel = member_object (o, "kernel");
    JsonObject *cves = kernel_cves (o);
    const char *error = member_string (kernel, "cves_error");
    JsonArray *issues = member_array (cves, "issues");
    AdwPreferencesGroup *group;
    GtkWidget *row;
    g_autofree char *text = NULL;

    if (!cves && !error)
        return;
    group = add_group (sp->vulnerabilities_prefs, sp->vulnerability_groups, P_("Kernel"), NULL);
    if (!cves) {
        text = g_strdup_printf (P_("Not loaded yet: %s"), error);
        adw_preferences_group_add (group, property_row_new (P_("Kernel List"), text));
        return;
    }

    {
        g_autofree char *generated = format_time (member_int (cves, "generated"));

        text = g_strdup_printf (P_("%s, from the Linux kernel CNA"), generated);
        if (error) {
            g_autofree char *with_error = g_strdup_printf (P_("%s. Last download failed: %s"), text, error);
            g_free (text);
            text = g_steal_pointer (&with_error);
        }
        adw_preferences_group_add (group, property_row_new (P_("Kernel List"), text));
        g_clear_pointer (&text, g_free);
    }
    {
        const char *release = member_string (kernel, "release");
        g_autofree char *version = g_strndup (release ? release : "", strcspn (release ? release : "", "-"));

        /* Only the issues in the code this kernel builds; those fixed only in newer branches are
           left out by plinius-cve-update and not mentioned */
        text = kernel_is_own_build (release)
               ? g_strdup_printf (P_("%u in the own-build kernel %s"), (guint) member_int (cves, "built"), version)
               : g_strdup_printf (P_("%u in kernel %s"), (guint) member_int (cves, "built"), version);
        adw_preferences_group_add (group, property_row_new (P_("Known Issues"), text));
        g_clear_pointer (&text, g_free);
    }
    text = g_strdup_printf (P_("%u with a CVSS score of 7 or more; %u more with a lower or no score"),
                            (guint) member_int (cves, "listed"), (guint) member_int (cves, "other"));
    if (!issues || json_array_get_length (issues) == 0) {
        adw_preferences_group_add (group, property_row_new (P_("Issues with a High Rating"), text));
        return;
    }

    row = adw_expander_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_("Issues with a High Rating"));
    adw_expander_row_set_subtitle (ADW_EXPANDER_ROW (row), text);
    /* the file's root goes after this update; the row keeps the issues until it is opened */
    g_object_set_data_full (G_OBJECT (row), "issues", json_array_ref (issues), (GDestroyNotify) json_array_unref);
    g_signal_connect (row, "notify::expanded", G_CALLBACK (kernel_issues_expanded_cb), NULL);
    adw_preferences_group_add (group, row);
}

static void
update_vulnerabilities_page (SecurityPage *sp)
{
    g_autoptr(JsonNode) root = load_cve_file ();
    JsonObject *o = root && JSON_NODE_HOLDS_OBJECT (root) ? json_node_get_object (root) : NULL;
    JsonNode *issues_node = o ? json_object_get_member (o, "issues") : NULL;
    JsonArray *issues = issues_node && JSON_NODE_HOLDS_ARRAY (issues_node) ? json_node_get_array (issues_node) : NULL;
    JsonObject *kernel = member_object (o, "kernel");
    g_autoptr(GPtrArray) affecting = g_ptr_array_new ();
    g_autoptr(GPtrArray) unassessed = g_ptr_array_new ();
    g_autoptr(GPtrArray) unaffecting = g_ptr_array_new ();
    struct {
        GPtrArray  *list;
        const char *title;
    } sections[] = {
        { affecting, P_("Affect This System") },
        { unassessed, P_("Not Assessed") },
        { unaffecting, P_("Do Not Affect This System") },
    };
    AdwPreferencesGroup *group;
    GtkWidget *row;

    clear_groups (sp->vulnerabilities_prefs, sp->vulnerability_groups);

    group = add_group (sp->vulnerabilities_prefs, sp->vulnerability_groups, NULL, NULL);
    row = adw_action_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_("Last Checked"));
    if (o) {
        g_autofree char *checked = format_time (member_int (o, "checked"));
        const char *error = member_string (o, "error");
        g_autofree char *text = NULL;
        gint64 downloaded = member_int (o, "downloaded");

        if (downloaded) {
            g_autofree char *list_time = format_time (downloaded);
            text = g_strdup_printf (P_("%s, list of the tracker from %s"), checked, list_time);
        } else {
            text = g_strdup (checked);
        }
        if (error) {
            g_autofree char *with_error = g_strdup_printf (P_("%s. Last download failed: %s"), text, error);
            g_free (text);
            text = g_steal_pointer (&with_error);
        }
        adw_action_row_set_subtitle (ADW_ACTION_ROW (row), text);
    } else {
        adw_action_row_set_subtitle (ADW_ACTION_ROW (row), P_("Not yet. The list is loaded once a day while online."));
    }
    gtk_widget_add_css_class (row, "property");
    adw_preferences_group_add (group, row);

    if (kernel) {
        const char *release = member_string (kernel, "release");
        const char *patch = member_string (kernel, "vendor_security_patch");
        g_autofree char *text = NULL;

        if (!patch)
            patch = member_string (kernel, "security_patch");
        /* Our own kernel with the day it was built (its release names the Linux version); the
           Android patch level belongs to the vendor partition, the maker's firmware and
           drivers, not to the kernel */
        if (kernel_is_own_build (release)) {
            g_autofree char *built = kernel_build_date (release);

            text = built ? g_strdup_printf (P_("%s, own build of %s"), release, built)
                         : g_strdup_printf (P_("%s, own build"), release);
        } else {
            text = g_strdup_printf (P_("%s, from the phone maker"), release ? release : "");
        }
        adw_preferences_group_add (group, property_row_new (P_("Kernel"), text));

        if (patch) {
            g_autofree char *vendor = g_strdup_printf (P_("Security patch level %s, firmware and drivers from the phone maker"), patch);

            adw_preferences_group_add (group, property_row_new (P_("Vendor System"), vendor));
        }
    }

    for (guint i = 0; issues && i < json_array_get_length (issues); i++) {
        JsonObject *issue = json_array_get_object_element (issues, i);
        const char *status = assessment_status (issue);

        if (status == NULL)
            g_ptr_array_add (unassessed, issue);
        else if (g_str_equal (status, "affected") || g_str_equal (status, "mitigated"))
            g_ptr_array_add (affecting, issue);
        else
            g_ptr_array_add (unaffecting, issue);
    }
    for (guint s = 0; s < G_N_ELEMENTS (sections); s++) {
        if (sections[s].list->len == 0)
            continue;
        g_ptr_array_sort (sections[s].list, compare_issues);
        group = add_group (sp->vulnerabilities_prefs, sp->vulnerability_groups, sections[s].title, NULL);
        for (guint i = 0; i < sections[s].list->len; i++)
            adw_preferences_group_add (group, issue_row_new (g_ptr_array_index (sections[s].list, i)));
    }
    if (o && (!issues || json_array_get_length (issues) == 0)) {
        group = add_group (sp->vulnerabilities_prefs, sp->vulnerability_groups, NULL, NULL);
        row = adw_action_row_new ();
        adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_("No known vulnerabilities in the installed packages."));
        adw_preferences_group_add (group, row);
    }
    if (o)
        add_kernel_section (sp, o);
}

/* ----- Connections ----- */

static const struct {
    const char *name;
    const char *label;
    guint       seconds;
} ranges[] = {
    { "15m", N_("15 min"), 15 * 60 },
    { "30m", N_("30 min"), 30 * 60 },
    { "1h", N_("1 h"), 3600 },
    { "1d", N_("Day"), 86400 },
    { "1w", N_("Week"), 7 * 86400 },
};

static guint
selected_seconds (SecurityPage *sp)
{
    const char *name = sp->range_toggles ? adw_toggle_group_get_active_name (sp->range_toggles) : NULL;

    for (guint i = 0; name && i < G_N_ELEMENTS (ranges); i++)
        if (g_str_equal (name, ranges[i].name))
            return ranges[i].seconds;
    return 3600;
}

static const char *
proto_name (guint proto)
{
    switch (proto) {
    case 6: return "TCP";
    case 17: return "UDP";
    case 1: return "ICMP";
    case 58: return "ICMPv6";
    default: return "IP";
    }
}

/* One app (or service, or program) and where it connected to */
typedef struct {
    char      *title;
    char      *kind;      /* "System service", ... or NULL for apps */
    GIcon     *icon;
    guint      count;
    GPtrArray *rows;      /* GVariant (ssuqssuuxx), referenced */
} AppGroup;

static void
app_group_free (AppGroup *g)
{
    g_free (g->title);
    g_free (g->kind);
    g_clear_object (&g->icon);
    g_ptr_array_unref (g->rows);
    g_free (g);
}

static GDesktopAppInfo *
find_desktop_app (const char *id)
{
    g_autofree char *file = g_strconcat (id, ".desktop", NULL);
    g_autofree char *lower = NULL;
    GDesktopAppInfo *info = g_desktop_app_info_new (file);

    if (!info) {
        lower = g_ascii_strdown (file, -1);
        info = g_desktop_app_info_new (lower);
    }
    return info;
}

/* Title, kind and icon of the app key that plinius-netlogd derived from the cgroup */
static AppGroup *
app_group_new (const char *app, const char *exe)
{
    AppGroup *g = g_new0 (AppGroup, 1);

    g->rows = g_ptr_array_new_with_free_func ((GDestroyNotify) g_variant_unref);
    if (g_str_has_prefix (app, "service:")) {
        g->title = g_strdup (app + strlen ("service:"));
        g->kind = g_strdup (P_("Service"));
        g->icon = g_themed_icon_new ("applications-system-symbolic");
    } else if (g_str_equal (app, "android")) {
        g->title = g_strdup (P_("Android Container"));
        g->kind = g_strdup (P_("Vendor services of the phone"));
        g->icon = g_themed_icon_new ("phone-symbolic");
    } else if (g_str_equal (app, "session") || g_str_equal (app, "unknown")) {
        g->title = g_strdup (exe);
        g->kind = g_strdup (P_("Program"));
        g->icon = g_themed_icon_new ("utilities-terminal-symbolic");
    } else {
        g_autoptr(GDesktopAppInfo) info = find_desktop_app (app);
        if (info) {
            g->title = g_strdup (g_app_info_get_display_name (G_APP_INFO (info)));
            if (g_app_info_get_icon (G_APP_INFO (info)))
                g->icon = g_object_ref (g_app_info_get_icon (G_APP_INFO (info)));
        } else {
            g->title = g_strdup (app);
        }
        if (!g->icon)
            g->icon = g_themed_icon_new ("application-x-executable");
    }
    return g;
}

static int
compare_groups (gconstpointer a, gconstpointer b)
{
    const AppGroup *x = *(AppGroup **) a, *y = *(AppGroup **) b;

    return x->count == y->count ? g_utf8_collate (x->title, y->title) : (x->count < y->count ? 1 : -1);
}

static GtkWidget *
destination_row_new (GVariant *v)
{
    const char *app, *exe, *addr, *host;
    guint32 proto, scope, count;
    guint16 port;
    gint64 first, last;
    g_autofree char *when = NULL;
    g_autofree char *subtitle = NULL;
    g_autofree char *times = NULL;
    g_autofree char *times_last = NULL;
    GtkWidget *row;

    g_variant_get (v, "(&s&suq&s&suuxx)", &app, &exe, &proto, &port, &addr, &host, &scope, &count, &first, &last);
    when = format_time (last);
    times = g_strdup_printf (NP_("%u time", "%u times", count), count);
    /* Translators: "12 times, last 14:02" */
    times_last = g_strdup_printf (P_("%s, last %s"), times, when);
    subtitle = g_strdup_printf ("%s%s%s %u · %s · %s", *host ? addr : "", *host ? " · " : "",
                                proto_name (proto), port, exe, times_last);
    row = adw_action_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), *host ? host : addr);
    adw_preferences_row_set_use_markup (ADW_PREFERENCES_ROW (row), FALSE);
    adw_action_row_set_subtitle (ADW_ACTION_ROW (row), subtitle);
    adw_action_row_set_subtitle_selectable (ADW_ACTION_ROW (row), TRUE);
    return row;
}

static void
add_app_groups (SecurityPage *sp, GPtrArray *groups, const char *title, const char *description)
{
    AdwPreferencesGroup *group;

    if (groups->len == 0)
        return;
    g_ptr_array_sort (groups, compare_groups);
    group = add_group (sp->connections_prefs, sp->connection_groups, title, description);
    for (guint i = 0; i < groups->len; i++) {
        AppGroup *g = g_ptr_array_index (groups, i);
        g_autoptr(GHashTable) destinations = g_hash_table_new (g_str_hash, g_str_equal);
        g_autofree char *counts = NULL;
        g_autofree char *n_conn = NULL;
        g_autofree char *n_dest = NULL;
        GtkWidget *row, *icon;

        for (guint r = 0; r < g->rows->len; r++) {
            const char *addr;
            g_variant_get_child (g_ptr_array_index (g->rows, r), 4, "&s", &addr);
            g_hash_table_add (destinations, (gpointer) addr);
        }
        n_conn = g_strdup_printf (NP_("%u connection", "%u connections", g->count), g->count);
        n_dest = g_strdup_printf (NP_("%u destination", "%u destinations", g_hash_table_size (destinations)),
                                  g_hash_table_size (destinations));
        counts = g_strdup_printf ("%s · %s", n_conn, n_dest);
        row = adw_expander_row_new ();
        adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), g->title);
        adw_preferences_row_set_use_markup (ADW_PREFERENCES_ROW (row), FALSE);
        if (g->kind) {
            g_autofree char *with_kind = g_strdup_printf ("%s · %s", g->kind, counts);
            adw_expander_row_set_subtitle (ADW_EXPANDER_ROW (row), with_kind);
        } else {
            adw_expander_row_set_subtitle (ADW_EXPANDER_ROW (row), counts);
        }
        icon = gtk_image_new_from_gicon (g->icon);
        gtk_image_set_icon_size (GTK_IMAGE (icon), GTK_ICON_SIZE_LARGE);
        adw_expander_row_add_prefix (ADW_EXPANDER_ROW (row), icon);
        for (guint r = 0; r < g->rows->len; r++)
            adw_expander_row_add_row (ADW_EXPANDER_ROW (row), destination_row_new (g_ptr_array_index (g->rows, r)));
        adw_preferences_group_add (group, row);
    }
}

static void
show_connections_message (SecurityPage *sp, const char *text)
{
    AdwPreferencesGroup *group = add_group (sp->connections_prefs, sp->connection_groups, NULL, NULL);
    GtkWidget *row = adw_action_row_new ();

    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), text);
    adw_preferences_group_add (group, row);
}

static void
query_done_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
    SecurityPage *sp = user_data;
    g_autoptr(GVariant) reply = NULL;
    g_autoptr(GVariant) rows = NULL;
    g_autoptr(GError) error = NULL;
    g_autoptr(GHashTable) by_app = NULL;
    g_autoptr(GPtrArray) internet = NULL;
    g_autoptr(GPtrArray) lan = NULL;
    GVariantIter iter;
    GVariant *v;

    reply = g_dbus_connection_call_finish (G_DBUS_CONNECTION (source), result, &error);
    if (g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
        return;
    clear_groups (sp->connections_prefs, sp->connection_groups);
    if (!reply) {
        g_debug ("plinius-netlogd: %s", error->message);
        show_connections_message (sp, P_("Connections are not recorded: the recording service is not running."));
        return;
    }

    /* Apps by their id; services, sessions and the container by app and program, so that
     * "ssh" and "curl" in one session stay apart */
    by_app = g_hash_table_new_full (g_str_hash, g_str_equal, g_free, (GDestroyNotify) app_group_free);
    internet = g_ptr_array_new ();
    lan = g_ptr_array_new ();
    rows = g_variant_get_child_value (reply, 0);
    g_variant_iter_init (&iter, rows);
    while ((v = g_variant_iter_next_value (&iter))) {
        const char *app, *exe;
        guint32 scope, count;
        g_autofree char *key = NULL;
        AppGroup *g;

        g_variant_get (v, "(&s&suqssuuxx)", &app, &exe, NULL, NULL, NULL, NULL, &scope, &count, NULL, NULL);
        if (g_str_has_prefix (app, "service:") || strchr (app, '.'))
            key = g_strdup_printf ("%u\t%s", scope, app);
        else
            key = g_strdup_printf ("%u\t%s\t%s", scope, app, exe);
        g = g_hash_table_lookup (by_app, key);
        if (!g) {
            g = app_group_new (app, exe);
            g_hash_table_insert (by_app, g_strdup (key), g);
            g_ptr_array_add (scope == 2 ? internet : lan, g);
        }
        g->count += count;
        g_ptr_array_add (g->rows, v);
    }

    if (internet->len == 0 && lan->len == 0) {
        show_connections_message (sp, P_("No connections in this time."));
        return;
    }
    add_app_groups (sp, internet, P_("Internet"), NULL);
    add_app_groups (sp, lan, P_("Local Network"),
                    P_("Devices in the same network, e.g. the router for name lookups, printers, the phone operator's own network."));
}

static void
summary_done_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
    SecurityPage *sp = user_data;
    g_autoptr(GVariant) reply = NULL;
    g_autoptr(GError) error = NULL;
    g_autofree char *connections = NULL;
    g_autofree char *destinations = NULL;
    g_autofree char *apps = NULL;
    g_autofree char *text = NULL;
    guint32 n_conn, n_dest, n_apps, n_lan;

    reply = g_dbus_connection_call_finish (G_DBUS_CONNECTION (source), result, &error);
    if (g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
        return;
    if (!reply) {
        adw_action_row_set_subtitle (sp->connections_row, P_("Not recorded: the recording service is not running."));
        return;
    }
    g_variant_get (reply, "(uuuu)", &n_conn, &n_dest, &n_apps, &n_lan);
    connections = g_strdup_printf (NP_("%u connection", "%u connections", n_conn), n_conn);
    destinations = g_strdup_printf (NP_("%u destination", "%u destinations", n_dest), n_dest);
    apps = g_strdup_printf (NP_("%u app", "%u apps", n_apps), n_apps);
    /* Translators: "Last hour, internet: 23 connections · 9 destinations · 4 apps" */
    text = g_strdup_printf (P_("Last hour, internet: %s · %s · %s"), connections, destinations, apps);
    adw_action_row_set_subtitle (sp->connections_row, text);
}

static void
netlog_call (SecurityPage *sp, const char *method, guint seconds, const char *reply_type, GAsyncReadyCallback cb)
{
    g_autoptr(GDBusConnection) bus = g_bus_get_sync (G_BUS_TYPE_SYSTEM, NULL, NULL);

    if (!bus)
        return;
    g_dbus_connection_call (bus, NETLOG_NAME, NETLOG_PATH, NETLOG_NAME, method, g_variant_new ("(u)", seconds),
                            G_VARIANT_TYPE (reply_type), G_DBUS_CALL_FLAGS_NONE, 10000,
                            sp->cancellable, cb, sp);
}

static void
update_connections_page (SecurityPage *sp)
{
    netlog_call (sp, "Query", selected_seconds (sp), "(a(ssuqssuuxx))", query_done_cb);
}

/* ----- Boot Chain ----- */

static void update_main_page (SecurityPage *sp);
static AdwNavigationPage *navigation_page_new (GtkWidget *content, const char *title, const char *tag);

static GtkWidget *
text_row_new (const char *title, const char *subtitle)
{
    GtkWidget *row = adw_action_row_new ();

    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), title);
    adw_action_row_set_subtitle (ADW_ACTION_ROW (row), subtitle);
    adw_action_row_set_subtitle_lines (ADW_ACTION_ROW (row), 0);
    return row;
}

static void
accept_done_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
    GSubprocess *process = G_SUBPROCESS (source);
    SecurityPage *sp = user_data;
    g_autoptr(GError) error = NULL;
    g_autofree char *err = NULL;
    int status;

    /* cancelled: the panel is gone, and sp with it */
    if (!g_subprocess_communicate_utf8_finish (process, result, NULL, &err, &error) &&
        g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
        return;

    sp->accepting = FALSE;
    if (sp->accept_button)
        gtk_widget_set_sensitive (sp->accept_button, TRUE);
    status = error ? -1 : g_subprocess_get_if_exited (process) ? g_subprocess_get_exit_status (process) : -1;
    if (status == 0) {
        /* alarm over: back to the checks, which read the state again on showing */
        gtk_widget_activate_action (GTK_WIDGET (sp->boot_prefs), "navigation.pop", NULL);
        update_main_page (sp);
        return;
    }
    if (status == PKEXEC_DISMISSED || !sp->accept_group)
        return;
    if (status == PKEXEC_NOT_AUTHORIZED) {
        adw_preferences_group_set_description (sp->accept_group, P_("Not accepted: not authorized."));
    } else {
        g_autofree char *text = g_strdup_printf (P_("Not accepted: %s"),
                                                 err && *g_strstrip (err) ? err : error ? error->message : "?");
        adw_preferences_group_set_description (sp->accept_group, text);
    }
}

/* "pkexec plinius-bootcheck accept PATH...": only the reported parts become the accepted state */
static void
accept_clicked_cb (SecurityPage *sp)
{
    g_auto(GStrv) changes = boot_chain_changes ();
    g_autoptr(GStrvBuilder) builder = NULL;
    g_auto(GStrv) argv = NULL;
    g_autoptr(GSubprocess) process = NULL;
    g_autoptr(GError) error = NULL;

    if (sp->accepting || !changes || !sp->accept_group)
        return;

    builder = g_strv_builder_new ();
    g_strv_builder_add_many (builder, "pkexec", BOOTCHECK_TOOL, "accept", NULL);
    g_strv_builder_addv (builder, (const char **) changes);
    argv = g_strv_builder_end (builder);
    process = g_subprocess_newv ((const char * const *) argv,
                                 G_SUBPROCESS_FLAGS_STDOUT_SILENCE | G_SUBPROCESS_FLAGS_STDERR_PIPE, &error);
    if (!process) {
        g_autofree char *text = g_strdup_printf (P_("Not accepted: %s"), error->message);
        adw_preferences_group_set_description (sp->accept_group, text);
        return;
    }
    sp->accepting = TRUE;
    gtk_widget_set_sensitive (sp->accept_button, FALSE);
    g_subprocess_communicate_utf8_async (process, NULL, sp->cancellable, accept_done_cb, sp);
}

/* Read on showing: the alarm may have been accepted elsewhere (plinius-bootcheck accept) */
static void
update_boot_page (SecurityPage *sp)
{
    g_auto(GStrv) changes = boot_chain_changes ();
    AdwPreferencesGroup *group;
    GtkWidget *row, *icon;

    clear_groups (sp->boot_prefs, sp->boot_groups);
    sp->accept_group = NULL;
    sp->accept_button = NULL;

    group = add_group (sp->boot_prefs, sp->boot_groups, NULL, NULL);
    icon = gtk_image_new ();
    if (changes) {
        row = text_row_new (P_("Tampering Detected"),
                            P_("At this start, parts of the start system differ from the last accepted state. "
                               "The phone has already started with them."));
        set_level_icon (GTK_IMAGE (icon), LEVEL_BAD);
    } else {
        row = text_row_new (P_("No Changes"),
                            P_("Bootloader, kernel and ramdisk are unchanged since the last accepted state."));
        set_level_icon (GTK_IMAGE (icon), LEVEL_GOOD);
    }
    gtk_image_set_pixel_size (GTK_IMAGE (icon), 32);
    adw_action_row_add_prefix (ADW_ACTION_ROW (row), icon);
    adw_preferences_group_add (group, row);
    if (!changes)
        return;

    group = add_group (sp->boot_prefs, sp->boot_groups, P_("Changed Parts"), NULL);
    for (guint i = 0; changes[i]; i++) {
        g_autofree char *name = boot_part_name (changes[i]);

        row = text_row_new (name, changes[i]);
        adw_preferences_row_set_use_markup (ADW_PREFERENCES_ROW (row), FALSE);
        adw_preferences_group_add (group, row);
    }

    group = add_group (sp->boot_prefs, sp->boot_groups, P_("What to Do"), NULL);
    adw_preferences_group_add (group, text_row_new (P_("You Changed It"),
        P_("A kernel or ramdisk update, a new boot menu entry or an update of Volla OS that you installed "
           "yourself explains the change. Then accept it below.")));
    adw_preferences_group_add (group, text_row_new (P_("You Did Not"),
        P_("Someone may have had the phone and may know your encryption password. Do not accept the change "
           "and enter no more passwords: restore the start system from a backup, then change the encryption password.")));

    /* last, below the explanation */
    sp->accept_group = add_group (sp->boot_prefs, sp->boot_groups, NULL,
                                  P_("Takes the current state as the accepted one and ends the alarm. Asks for your PIN."));
    sp->accept_button = gtk_button_new_with_mnemonic (P_("_Accept Changes"));
    gtk_widget_set_halign (sp->accept_button, GTK_ALIGN_CENTER);
    gtk_widget_add_css_class (sp->accept_button, "pill");
    gtk_widget_add_css_class (sp->accept_button, "destructive-action");
    gtk_widget_set_sensitive (sp->accept_button, !sp->accepting);
    g_signal_connect_swapped (sp->accept_button, "clicked", G_CALLBACK (accept_clicked_cb), sp);
    adw_preferences_group_add (sp->accept_group, sp->accept_button);
}

static void
build_boot_page (SecurityPage *sp)
{
    GtkWidget *prefs = adw_preferences_page_new ();

    sp->boot_prefs = ADW_PREFERENCES_PAGE (prefs);
    sp->boot_page = navigation_page_new (prefs, P_("Boot Chain"), "plinius-boot-chain");
    g_signal_connect_swapped (sp->boot_page, "showing", G_CALLBACK (update_boot_page), sp);
}

/* ----- Access Control page ----- */

/* "plinius.cupsd" -> "cupsd" */
static const char *
profile_short_name (const char *name)
{
    return g_str_has_prefix (name, "plinius.") ? name + strlen ("plinius.") : name;
}

static void
update_mac_page (SecurityPage *sp)
{
    AdwPreferencesGroup *group;
    MacState m;
    const char *what;

    mac_state_read (&m);
    clear_groups (sp->mac_prefs, sp->mac_groups);

    group = add_group (sp->mac_prefs, sp->mac_groups, NULL,
                       P_("Access control limits each confined program to what it needs, also when it runs as root: "
                          "a service taken over through the network cannot read your data or reach other services."));
    if (m.kind == MAC_SELINUX)
        what = m.selinux_enforcing ? P_("SELinux, enforcing") : P_("SELinux, only logging");
    else if (m.kind == MAC_APPARMOR)
        what = P_("AppArmor");
    else
        what = P_("None");
    adw_preferences_group_add (group, text_row_new (P_("In the Kernel"), what));

    if (m.kind == MAC_APPARMOR && m.expected && m.expected[0]) {
        group = add_group (sp->mac_prefs, sp->mac_groups, P_("Profiles"),
                           P_("“Logging only”: the profile records what the service does and blocks nothing yet, "
                              "until its rules are complete."));
        for (char **e = m.expected; *e; e++) {
            const char *mode;
            GtkWidget *row;

            if (!**e)
                continue;
            mode = g_hash_table_lookup (m.modes, *e);
            row = text_row_new (profile_short_name (*e),
                                !mode ? P_("Not loaded")
                                : g_str_equal (mode, "enforce") ? P_("Enforced")
                                : g_str_equal (mode, "complain") ? P_("Logging only")
                                : mode);
            adw_preferences_row_set_use_markup (ADW_PREFERENCES_ROW (row), FALSE);
            adw_preferences_group_add (group, row);
        }
    }
    mac_state_clear (&m);
}

static void
build_mac_page (SecurityPage *sp)
{
    GtkWidget *prefs = adw_preferences_page_new ();

    sp->mac_prefs = ADW_PREFERENCES_PAGE (prefs);
    sp->mac_page = navigation_page_new (prefs, P_("Access Control"), "plinius-access-control");
    g_signal_connect_swapped (sp->mac_page, "showing", G_CALLBACK (update_mac_page), sp);
}

/* ----- Device Security ----- */

static void
update_main_page (SecurityPage *sp)
{
    Check checks[8] = { 0 };
    guint n = 0;
    Level overall = LEVEL_GOOD;
    g_autoptr(JsonNode) cve = NULL;
    const char *title, *subtitle;

    check_screen_lock (&checks[n++]);
    check_encryption (&checks[n++]);
    if (check_boot_chain (&checks[n]))
        n++;
    check_packages (&checks[n++]);
    check_access_control (&checks[n++]);
    check_bootloader (&checks[n++]);

    for (guint i = 0; i < sp->check_rows->len; i++)
        adw_preferences_group_remove (sp->checks_group, g_ptr_array_index (sp->check_rows, i));
    g_ptr_array_set_size (sp->check_rows, 0);
    for (guint i = 0; i < n; i++) {
        GtkWidget *row = check_row_new (&checks[i]);
        adw_preferences_group_add (sp->checks_group, row);
        g_ptr_array_add (sp->check_rows, row);
        if (checks[i].level != LEVEL_INFO)
            overall = MIN (overall, checks[i].level);
        g_free (checks[i].subtitle);
    }

    cve = load_cve_file ();
    update_vulnerabilities_row (sp, cve);
    overall = MIN (overall, sp->vulnerabilities_level);

    switch (overall) {
    case LEVEL_BAD:
        title = P_("At Risk");
        subtitle = P_("Some of the checks below leave the phone's data open. The red rows say what to change.");
        break;
    case LEVEL_WARNING:
        title = P_("Needs Attention");
        subtitle = P_("Nothing is open, but some of the checks below could be better.");
        break;
    default:
        title = P_("Protected");
        subtitle = P_("All checks below are fine.");
        break;
    }
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (sp->overall_row), title);
    adw_action_row_set_subtitle (sp->overall_row, subtitle);
    set_level_icon (sp->overall_icon, overall);

    netlog_call (sp, "Summary", 3600, "(uuuu)", summary_done_cb);
}

static AdwActionRow *
nav_row_new (AdwPreferencesGroup *group, const char *title, const char *icon_name, const char *tag, GtkImage **icon)
{
    GtkWidget *row = adw_action_row_new ();
    GtkWidget *image = gtk_image_new_from_icon_name (icon_name);

    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), title);
    adw_action_row_add_prefix (ADW_ACTION_ROW (row), image);
    adw_action_row_add_suffix (ADW_ACTION_ROW (row), gtk_image_new_from_icon_name ("go-next-symbolic"));
    gtk_list_box_row_set_activatable (GTK_LIST_BOX_ROW (row), TRUE);
    gtk_actionable_set_action_name (GTK_ACTIONABLE (row), "navigation.push");
    gtk_actionable_set_action_target (GTK_ACTIONABLE (row), "s", tag);
    adw_preferences_group_add (group, row);
    if (icon)
        *icon = GTK_IMAGE (image);
    return ADW_ACTION_ROW (row);
}

static AdwNavigationPage *
navigation_page_new (GtkWidget *content, const char *title, const char *tag)
{
    GtkWidget *toolbar = adw_toolbar_view_new ();

    adw_toolbar_view_add_top_bar (ADW_TOOLBAR_VIEW (toolbar), adw_header_bar_new ());
    adw_toolbar_view_set_content (ADW_TOOLBAR_VIEW (toolbar), content);
    return ADW_NAVIGATION_PAGE (adw_navigation_page_new_with_tag (toolbar, title, tag));
}

static GtkWidget *
build_main_page (SecurityPage *sp)
{
    GtkWidget *prefs = adw_preferences_page_new ();
    GtkWidget *group;

    group = adw_preferences_group_new ();
    sp->overall_row = ADW_ACTION_ROW (adw_action_row_new ());
    sp->overall_icon = GTK_IMAGE (gtk_image_new ());
    gtk_image_set_pixel_size (sp->overall_icon, 32);
    adw_action_row_add_prefix (sp->overall_row, GTK_WIDGET (sp->overall_icon));
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), GTK_WIDGET (sp->overall_row));
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (prefs), ADW_PREFERENCES_GROUP (group));

    group = adw_preferences_group_new ();
    adw_preferences_group_set_title (ADW_PREFERENCES_GROUP (group), P_("Checks"));
    sp->checks_group = ADW_PREFERENCES_GROUP (group);
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (prefs), ADW_PREFERENCES_GROUP (group));

    group = adw_preferences_group_new ();
    adw_preferences_group_set_title (ADW_PREFERENCES_GROUP (group), P_("Network and Software"));
    sp->connections_row = nav_row_new (ADW_PREFERENCES_GROUP (group), P_("Connections"),
                                       "network-transmit-receive-symbolic", "plinius-connections", NULL);
    sp->vulnerabilities_row = nav_row_new (ADW_PREFERENCES_GROUP (group), P_("Known Vulnerabilities"),
                                           "dialog-warning-symbolic", "plinius-vulnerabilities",
                                           &sp->vulnerabilities_icon);
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (prefs), ADW_PREFERENCES_GROUP (group));

    return prefs;
}

static void
range_changed_cb (SecurityPage *sp)
{
    update_connections_page (sp);
}

static void
build_connections_page (SecurityPage *sp)
{
    GtkWidget *prefs = adw_preferences_page_new ();
    GtkWidget *group, *toggles;

    group = adw_preferences_group_new ();
    adw_preferences_group_set_description (ADW_PREFERENCES_GROUP (group),
                                           P_("Which app connected where. Recorded on this phone only, for a week."));
    toggles = adw_toggle_group_new ();
    for (guint i = 0; i < G_N_ELEMENTS (ranges); i++) {
        AdwToggle *toggle = adw_toggle_new ();
        adw_toggle_set_name (toggle, ranges[i].name);
        adw_toggle_set_label (toggle, P_(ranges[i].label));
        adw_toggle_group_add (ADW_TOGGLE_GROUP (toggles), toggle);
    }
    adw_toggle_group_set_active_name (ADW_TOGGLE_GROUP (toggles), "1h");
    adw_toggle_group_set_homogeneous (ADW_TOGGLE_GROUP (toggles), TRUE);
    g_signal_connect_swapped (toggles, "notify::active-name", G_CALLBACK (range_changed_cb), sp);
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), toggles);
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (prefs), ADW_PREFERENCES_GROUP (group));
    sp->range_toggles = ADW_TOGGLE_GROUP (toggles);
    sp->connections_prefs = ADW_PREFERENCES_PAGE (prefs);

    sp->connections_page = navigation_page_new (prefs, P_("Connections"), "plinius-connections");
    g_signal_connect_swapped (sp->connections_page, "showing", G_CALLBACK (update_connections_page), sp);
}

static void
build_vulnerabilities_page (SecurityPage *sp)
{
    GtkWidget *prefs = adw_preferences_page_new ();

    sp->vulnerabilities_prefs = ADW_PREFERENCES_PAGE (prefs);
    sp->vulnerabilities_page = navigation_page_new (prefs, P_("Known Vulnerabilities"), "plinius-vulnerabilities");
    g_signal_connect_swapped (sp->vulnerabilities_page, "showing", G_CALLBACK (update_vulnerabilities_page), sp);
}

/**
 * cc_plinius_security_setup_panel:
 * @panel: the Security panel
 *
 * Makes the checks @panel's own page and adds the pages "plinius-connections",
 * "plinius-vulnerabilities", "plinius-boot-chain" and "plinius-access-control".
 */
void
cc_plinius_security_setup_panel (CcPanel *panel)
{
    SecurityPage *sp = g_new0 (SecurityPage, 1);
    GtkWidget *toolbar = adw_toolbar_view_new ();

    sp->cancellable = g_cancellable_new ();
    sp->check_rows = g_ptr_array_new ();
    sp->connection_groups = g_ptr_array_new ();
    sp->vulnerability_groups = g_ptr_array_new ();
    sp->boot_groups = g_ptr_array_new ();
    sp->mac_groups = g_ptr_array_new ();

    adw_toolbar_view_add_top_bar (ADW_TOOLBAR_VIEW (toolbar), adw_header_bar_new ());
    adw_toolbar_view_set_content (ADW_TOOLBAR_VIEW (toolbar), build_main_page (sp));
    adw_navigation_page_set_child (ADW_NAVIGATION_PAGE (panel), toolbar);
    sp->page = ADW_NAVIGATION_PAGE (panel);
    /* Everything can change outside (a new PIN, a boot check, a download): read on showing */
    g_signal_connect_swapped (panel, "showing", G_CALLBACK (update_main_page), sp);
    update_main_page (sp);

    build_connections_page (sp);
    build_vulnerabilities_page (sp);
    build_boot_page (sp);
    build_mac_page (sp);
    g_object_set_data_full (G_OBJECT (panel), "plinius-security", sp, (GDestroyNotify) security_page_free);

    cc_panel_add_subpage (panel, "plinius-connections", sp->connections_page);
    cc_panel_add_subpage (panel, "plinius-vulnerabilities", sp->vulnerabilities_page);
    cc_panel_add_subpage (panel, "plinius-boot-chain", sp->boot_page);
    cc_panel_add_subpage (panel, "plinius-access-control", sp->mac_page);
}
