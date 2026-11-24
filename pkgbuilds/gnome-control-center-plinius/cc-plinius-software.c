/*
 * cc-plinius-software.c
 *
 * Plinius: the pages of plinius-software in Settings.
 *
 * - Apps gets four tabs: "Explore" (the app catalog: categories, search, an app's details,
 *   install), "Installed" (GNOME's list of apps; an app's page also shows its package and can
 *   remove it), "Web Apps" (websites as apps, cc-plinius-webapps.c) and "Settings" (Default
 *   Apps, which ratings Explore also shows, Flathub and FuriHub, Beta Updates).
 * - "Updates" is a panel of its own; its rows are on System and on Security
 *   (shell/cc-plinius-hubs.c).
 *
 * plinius-software stays the place for everything below the surface:
 * - Package operations: /usr/lib/plinius-software/plinius-pkg via pkexec (install, remove,
 *   upgrade, Flathub, FuriHub). Its exit codes are listed below; it prints pacman's output (LC_ALL=C)
 *   and "::plinius:: NAME" for its own sections, from which the progress is derived.
 * - After an update: which restart it needs (none, apps, Phosh, the phone), from
 *   /run/plinius-pkg/restart.json; restarting apps and Phosh is
 *   "/usr/lib/plinius-software/plinius-restart apply" (both written by plinius-software).
 * - The update check: "plinius-software --updates-json" (checkupdates on a copy of the
 *   databases, the limits of plinius-freigabe, Flathub). The daily check and its notification
 *   stay in plinius-software too; tapping the notification opens this Updates panel.
 * - The phone ratings: /usr/share/plinius-software/apps.conf, otherwise the app's own
 *   AppStream data (display_length), as in plinius-software.
 * - Which ratings Explore shows: ~/.config/plinius-software/settings.ini, shared with the app.
 *
 * The catalog (AppStream from archlinux-appstream-data, Flathub's and FuriHub's if on) is loaded
 * in a thread when Explore is first shown and kept only as long as a page uses it.
 *
 * FuriHub (https://furihub.github.io/repo/) is a Flatpak remote of the FuriLabs user community.
 * It has no runtimes, its apps take them from Flathub: plinius-pkg turns Flathub on with it and
 * keeps it on while FuriHub is (EXIT_FURIHUB_ON). AppStream marks every Flatpak component with
 * the origin "flatpak", so which remote an app comes from is taken from flatpak itself.
 *
 * Our own strings use the gettext domain plinius-control-center (not GNOME's po/). Most of
 * them are plinius-software's, with its translations.
 *
 * SPDX-License-Identifier: MIT
 */

#include "config.h"

#undef G_LOG_DOMAIN
#define G_LOG_DOMAIN "cc-plinius-software"

#include <adwaita.h>
#include <appstream.h>
#include <gio/gdesktopappinfo.h>
#include <glib/gi18n.h>
#include <json-glib/json-glib.h>
#include <libsoup/soup.h>
#include <malloc.h>
#include <unistd.h>

#include "cc-plinius-software.h"
#include "cc-plinius-webapps.h"
#include "shell/cc-application.h"
#include "shell/cc-shell-model.h"

#define PLINIUS_DOMAIN "plinius-control-center"
#define P_(String) g_dgettext (PLINIUS_DOMAIN, String)
#define NP_(Singular, Plural, N) g_dngettext (PLINIUS_DOMAIN, Singular, Plural, N)
/* N_(): marked for translation (xgettext -k N_), looked up with P_() where shown */

#define HELPER "/usr/lib/plinius-software/plinius-pkg"
#define SOFTWARE "/usr/bin/plinius-software"
/* After an update: which restart brings the new versions into use (written by plinius-pkg) */
#define RESTART_HELPER "/usr/lib/plinius-software/plinius-restart"
#define RESTART_RESULT "/run/plinius-pkg/restart.json"
#define RATINGS_FILE "/usr/share/plinius-software/apps.conf"
/* Apps of the selection that can be removed (pkgbuilds/plinius-apps/recommended.list) */
#define RECOMMENDED_FILE "/usr/share/plinius-apps/recommended.list"
/* Shorter screen side of the Plinius in logical pixels, as in AppStream display_length */
#define PHONE_WIDTH 360
#define SETTINGS_GROUP "Anzeige"
#define APPLICATION_ID "org.gnome.Settings"

/* Exit codes of plinius-pkg (see its header) and pkexec */
enum {
    EXIT_STALE_DB = 3,
    EXIT_STALE_FILE = 4,
    EXIT_FLATHUB_IN_USE = 5,
    EXIT_NOTHING_TO_DO = 6,
    EXIT_ONLY_FLATPAK = 7,
    EXIT_HELD = 8,
    EXIT_BUSY = 9,
    EXIT_LOW_BATTERY = 10,
    EXIT_FURIHUB_ON = 11,
    EXIT_REPO_UNVERIFIED = 12,
    EXIT_PKEXEC_DISMISSED = 126,
    EXIT_PKEXEC_NOT_AUTHORIZED = 127,
};

/* ----- Ratings and categories ----- */

typedef enum {
    RATING_PHONE,
    RATING_SCALED,
    RATING_DESKTOP,
    RATING_UNKNOWN,
    N_RATINGS
} Rating;

/* key: the rating keyword in apps.conf; extra: the key in settings.ini for a rating Explore
 * only shows on request (Settings tab) */
static const struct {
    const char *key;
    const char *label;
    const char *note;
    const char *style;
    const char *extra;
    const char *extra_label;
} ratings[N_RATINGS] = {
    { "phone", N_("For phones"), N_("The app fits the screen."), "success", NULL, NULL },
    { "scaled", N_("With limitations"), N_("Some windows are wider than the screen and are scaled down."), "warning",
      NULL, NULL },
    { "desktop", N_("For large screens"), N_("According to the app, it needs more space than the screen offers."),
      "error", "show-desktop", N_("Apps for large screens") },
    { "unknown", N_("Not specified"), N_("The app does not state a screen size. It may or may not fit the phone."),
      "dim-label", "show-unknown", N_("Apps not approved for mobile") },
};

/* Notes from apps.conf, listed here so xgettext finds them */
static const char *const conf_notes[] G_GNUC_UNUSED = { N_("With the mobile tweaks from mobile-config-firefox") };

#define CATEGORY_RECOMMENDED "recommended"

/* Groups under "Explore", built from the freedesktop.org main categories (AppStream
 * <categories>). An app can be in several groups; NULL = all apps. */
static const struct {
    const char *title;
    const char *icon;
    const char *categories[4];
} categories[] = {
    { N_("Recommended"), "starred-symbolic", { CATEGORY_RECOMMENDED } },
    { N_("Communication & Internet"), "web-browser-symbolic", { "Network" } },
    { N_("Music & Video"), "folder-music-symbolic", { "AudioVideo", "Audio", "Video" } },
    { N_("Office & Documents"), "x-office-document-symbolic", { "Office" } },
    { N_("Photos & Graphics"), "image-x-generic-symbolic", { "Graphics" } },
    { N_("Learning & Science"), "accessories-dictionary-symbolic", { "Education", "Science" } },
    { N_("Games"), "applications-games-symbolic", { "Game" } },
    { N_("Utilities"), "applications-utilities-symbolic", { "Utility" } },
    { N_("System"), "applications-system-symbolic", { "System", "Settings", "Development" } },
    { N_("All Apps"), "view-grid-symbolic", { NULL } },
};

static char *
settings_path (void)
{
    return g_build_filename (g_get_user_config_dir (), "plinius-software", "settings.ini", NULL);
}

/* Whether Explore shows @rating: phone and scaled always, the others as chosen */
static gboolean
rating_shown (Rating rating)
{
    g_autoptr(GKeyFile) keyfile = g_key_file_new ();
    g_autofree char *path = settings_path ();

    if (ratings[rating].extra == NULL)
        return TRUE;
    if (!g_key_file_load_from_file (keyfile, path, G_KEY_FILE_NONE, NULL))
        return FALSE;
    return g_key_file_get_boolean (keyfile, SETTINGS_GROUP, ratings[rating].extra, NULL);
}

static void
set_rating_shown (Rating rating, gboolean shown)
{
    g_autoptr(GKeyFile) keyfile = g_key_file_new ();
    g_autoptr(GError) error = NULL;
    g_autofree char *path = settings_path ();
    g_autofree char *dir = g_path_get_dirname (path);

    g_key_file_load_from_file (keyfile, path, G_KEY_FILE_KEEP_COMMENTS, NULL);
    g_key_file_set_boolean (keyfile, SETTINGS_GROUP, ratings[rating].extra, shown);
    g_mkdir_with_parents (dir, 0700);
    if (!g_key_file_save_to_file (keyfile, path, &error))
        g_warning ("Could not save %s: %s", path, error->message);
}

/* ----- Small helpers ----- */

/* stdout of @argv as lines; empty if it cannot be started */
static GStrv
run_lines (const char *const *argv)
{
    g_autofree char *out = NULL;

    if (!g_spawn_sync (NULL, (char **) argv, NULL, G_SPAWN_SEARCH_PATH | G_SPAWN_STDERR_TO_DEV_NULL, NULL, NULL, &out,
                       NULL, NULL, NULL) ||
        out == NULL)
        return g_new0 (char *, 1);
    g_strchomp (out);
    if (*out == '\0')
        return g_new0 (char *, 1);
    return g_strsplit (out, "\n", -1);
}

static gboolean
remote_enabled (const char *name)
{
    const char *argv[] = { "flatpak", "remotes", "--system", "--columns=name", NULL };
    g_auto(GStrv) remotes = run_lines (argv);

    return g_strv_contains ((const char *const *) remotes, name);
}

static gboolean
flathub_enabled (void)
{
    return remote_enabled ("flathub");
}

static gboolean
furihub_enabled (void)
{
    return remote_enabled ("furihub");
}

/* How a Flatpak remote is shown next to its apps */
static const char *
remote_title (const char *remote)
{
    if (remote == NULL || *remote == '\0')
        return "Flatpak";
    if (g_str_equal (remote, "flathub"))
        return "Flathub";
    if (g_str_equal (remote, "furihub"))
        return "FuriHub";
    return remote;
}

/* Channel of our repo [plinius] (docs/PAKETIERUNG.md, hosting), from the Server lines of its
 * section in pacman.conf, which plinius-pkg channel-beta / channel-stable rewrites: stable at
 * <repo>/<arch>, beta at <repo>/beta/<arch>. 1 beta, 0 stable, -1 no [plinius] (no switch). */
static int
plinius_channel (void)
{
    g_autofree char *text = NULL;
    g_auto(GStrv) lines = NULL;
    gboolean section = FALSE, found = FALSE;

    if (!g_file_get_contents ("/etc/pacman.conf", &text, NULL, NULL))
        return -1;
    lines = g_strsplit (text, "\n", -1);
    for (guint i = 0; lines[i]; i++) {
        const char *line = g_strstrip (lines[i]);

        if (line[0] == '[') {
            section = g_str_equal (line, "[plinius]");
            found |= section;
        } else if (section && g_str_has_prefix (line, "Server") && strstr (line, "/beta/")) {
            return 1;
        }
    }
    return found ? 0 : -1;
}

/* Adw.ActionRow without markup, so "Music & Video" or URLs with & stay readable */
static AdwActionRow *
plain_row (const char *title, const char *subtitle)
{
    AdwActionRow *row = ADW_ACTION_ROW (adw_action_row_new ());

    adw_preferences_row_set_use_markup (ADW_PREFERENCES_ROW (row), FALSE);
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), title);
    if (subtitle && *subtitle)
        adw_action_row_set_subtitle (row, subtitle);
    return row;
}

static GtkWidget *
pill (const char *label, const char *style)
{
    GtkWidget *button = gtk_button_new_with_label (label);

    gtk_widget_set_halign (button, GTK_ALIGN_CENTER);
    gtk_widget_add_css_class (button, "pill");
    if (style)
        gtk_widget_add_css_class (button, style);
    return button;
}

static GtkWidget *
wrapped_label (const char *text, const char *style)
{
    GtkWidget *label = gtk_label_new (text);

    gtk_label_set_wrap (GTK_LABEL (label), TRUE);
    gtk_label_set_justify (GTK_LABEL (label), GTK_JUSTIFY_CENTER);
    if (style)
        gtk_widget_add_css_class (label, style);
    return label;
}

/* @child centered at a phone-friendly maximum width, scrolling vertically */
static GtkWidget *
scrolled (GtkWidget *child)
{
    GtkWidget *clamp = adw_clamp_new ();
    GtkWidget *scroll = gtk_scrolled_window_new ();

    adw_clamp_set_maximum_size (ADW_CLAMP (clamp), 600);
    adw_clamp_set_child (ADW_CLAMP (clamp), child);
    gtk_widget_set_margin_start (clamp, 12);
    gtk_widget_set_margin_end (clamp, 12);
    gtk_widget_set_margin_top (clamp, 12);
    gtk_widget_set_margin_bottom (clamp, 24);
    gtk_scrolled_window_set_child (GTK_SCROLLED_WINDOW (scroll), clamp);
    gtk_scrolled_window_set_policy (GTK_SCROLLED_WINDOW (scroll), GTK_POLICY_NEVER, GTK_POLICY_AUTOMATIC);
    gtk_widget_set_vexpand (scroll, TRUE);
    return scroll;
}

/* A page with a header bar and @content */
static AdwNavigationPage *
navigation_page (const char *title, GtkWidget *content)
{
    GtkWidget *toolbar = adw_toolbar_view_new ();

    adw_toolbar_view_add_top_bar (ADW_TOOLBAR_VIEW (toolbar), adw_header_bar_new ());
    adw_toolbar_view_set_content (ADW_TOOLBAR_VIEW (toolbar), content);
    return adw_navigation_page_new (toolbar, title);
}

static void
open_panel (const char *panel_id)
{
    g_action_group_activate_action (G_ACTION_GROUP (g_application_get_default ()), "launch-panel",
                                    g_variant_new ("(s@av)", panel_id, g_variant_new_array (G_VARIANT_TYPE_VARIANT, NULL, 0)));
}

/* ----- One catalog entry ----- */

typedef enum {
    SOURCE_PACMAN,
    SOURCE_FLATPAK
} Source;

#define CCP_TYPE_APP (ccp_app_get_type ())
G_DECLARE_FINAL_TYPE (CcpApp, ccp_app, CCP, APP, GObject)

/* One catalog entry (pacman package or Flatpak ref) with everything the pages show */
struct _CcpApp {
    GObject parent_instance;

    AsComponent *cpt;
    char *id;
    char *name;
    char *summary;
    Source source;
    char *pkg;        /* pacman: package name, Flatpak: ref (app/ID/arch/branch) */
    char *flatpak_id; /* flatpak knows the app by the ID from the ref; the AppStream ID may differ */
    char *remote;     /* Flatpak: the remote it is installed from or would be installed from */
    char *version;
    gboolean installed;
    GStrv removal; /* packages to remove, NULL = cannot be removed */
    gboolean recommended;
    Rating rating;
    char *note;
    char *search; /* name, summary, package and keywords, casefolded */
    char *icon_name;
    char *desktop_id;
    gboolean hidden; /* installed, but not in the app grid (NoDisplay), e.g. a helper program */
};

G_DEFINE_FINAL_TYPE (CcpApp, ccp_app, G_TYPE_OBJECT)

static void
ccp_app_finalize (GObject *object)
{
    CcpApp *app = CCP_APP (object);

    g_clear_object (&app->cpt);
    g_free (app->id);
    g_free (app->name);
    g_free (app->summary);
    g_free (app->pkg);
    g_free (app->flatpak_id);
    g_free (app->remote);
    g_free (app->version);
    g_strfreev (app->removal);
    g_free (app->note);
    g_free (app->search);
    g_free (app->icon_name);
    g_free (app->desktop_id);
    G_OBJECT_CLASS (ccp_app_parent_class)->finalize (object);
}

static void
ccp_app_class_init (CcpAppClass *klass)
{
    G_OBJECT_CLASS (klass)->finalize = ccp_app_finalize;
}

static void
ccp_app_init (CcpApp *app)
{
}

/* Rating from the app's own AppStream data: the smallest required/recommended/supported
 * display_length on the shorter side decides; otherwise Purism's form_factor hint */
static Rating
classify (AsComponent *cpt)
{
    GPtrArray *lists[] = { as_component_get_requires (cpt), as_component_get_recommends (cpt),
                           as_component_get_supports (cpt) };
    gint min_width = G_MAXINT;

    for (gsize l = 0; l < G_N_ELEMENTS (lists); l++) {
        for (guint i = 0; lists[l] && i < lists[l]->len; i++) {
            AsRelation *rel = g_ptr_array_index (lists[l], i);
            gint px;

            if (as_relation_get_item_kind (rel) != AS_RELATION_ITEM_KIND_DISPLAY_LENGTH ||
                as_relation_get_display_side_kind (rel) == AS_DISPLAY_SIDE_KIND_LONGEST)
                continue;
            px = as_relation_get_value_px (rel);
            if (px <= 0)
                continue;
            if (as_relation_get_compare (rel) == AS_RELATION_COMPARE_GE)
                min_width = MIN (min_width, px);
            else if (as_relation_get_compare (rel) == AS_RELATION_COMPARE_GT)
                min_width = MIN (min_width, px + 1);
        }
    }
    if (min_width != G_MAXINT)
        return min_width <= PHONE_WIDTH ? RATING_PHONE : RATING_DESKTOP;
    if (g_strcmp0 (as_component_get_custom_value (cpt, "Purism::form_factor"), "mobile") == 0)
        return RATING_PHONE;
    return RATING_UNKNOWN;
}

/* Catalog icon whose size comes closest to @size */
static const char *
icon_file (AsComponent *cpt, guint size)
{
    GPtrArray *icons = as_component_get_icons (cpt);
    AsIcon *best = NULL;

    for (guint i = 0; icons && i < icons->len; i++) {
        AsIcon *icon = g_ptr_array_index (icons, i);

        if (as_icon_get_kind (icon) != AS_ICON_KIND_CACHED || as_icon_get_filename (icon) == NULL)
            continue;
        if (best == NULL || ABS ((gint) as_icon_get_width (icon) - (gint) size) <
                                ABS ((gint) as_icon_get_width (best) - (gint) size))
            best = icon;
    }
    return best ? as_icon_get_filename (best) : NULL;
}

/* Name of the icon in the icon theme; it usually only exists once the app is installed */
static const char *
stock_icon_name (AsComponent *cpt)
{
    GPtrArray *icons = as_component_get_icons (cpt);

    for (guint i = 0; icons && i < icons->len; i++) {
        AsIcon *icon = g_ptr_array_index (icons, i);

        if (as_icon_get_kind (icon) == AS_ICON_KIND_STOCK)
            return as_icon_get_name (icon);
    }
    return NULL;
}

/* One image per screenshot: the smallest thumbnail at least @width wide, else the original */
static GPtrArray *
screenshot_urls (AsComponent *cpt, guint width)
{
    GPtrArray *urls = g_ptr_array_new_with_free_func (g_free);
    GPtrArray *shots = as_component_get_screenshots_all (cpt);

    for (guint i = 0; shots && i < shots->len; i++) {
        AsScreenshot *shot = g_ptr_array_index (shots, i);
        GPtrArray *images;
        AsImage *thumb = NULL, *source = NULL;

        if (as_screenshot_get_media_kind (shot) != AS_SCREENSHOT_MEDIA_KIND_IMAGE)
            continue;
        images = as_screenshot_get_images_all (shot);
        for (guint j = 0; images && j < images->len; j++) {
            AsImage *image = g_ptr_array_index (images, j);
            const char *locale = as_image_get_locale (image);

            if (as_image_get_url (image) == NULL || (locale && *locale && g_strcmp0 (locale, "C") != 0))
                continue;
            if (as_image_get_kind (image) == AS_IMAGE_KIND_THUMBNAIL && as_image_get_width (image) >= width &&
                (thumb == NULL || as_image_get_width (image) < as_image_get_width (thumb)))
                thumb = image;
            else if (as_image_get_kind (image) == AS_IMAGE_KIND_SOURCE && source == NULL)
                source = image;
        }
        if (thumb || source)
            g_ptr_array_add (urls, g_strdup (as_image_get_url (thumb ? thumb : source)));
    }
    return urls;
}

/* Whether @app belongs to the group categories[@group] */
static gboolean
in_category (CcpApp *app, gsize group)
{
    const char *const *wanted = categories[group].categories;
    GPtrArray *cats;

    if (wanted[0] == NULL)
        return TRUE;
    if (g_strcmp0 (wanted[0], CATEGORY_RECOMMENDED) == 0)
        return app->recommended;
    cats = as_component_get_categories (app->cpt);
    for (guint i = 0; cats && i < cats->len; i++)
        if (g_strv_contains (wanted, g_ptr_array_index (cats, i)))
            return TRUE;
    return FALSE;
}

/* ----- Loading the catalog (in a thread) ----- */

typedef struct {
    GHashTable *available; /* package -> version in the sync databases */
    GHashTable *installed; /* package set */
    GHashTable *required;  /* package -> GPtrArray of the packages that need it ("Required By") */
    GHashTable *provider;  /* provided name -> installed package ("Provides") */
    GHashTable *recommended;
    GHashTable *flatpaks;  /* installed Flatpak app ID -> its remote */
    GHashTable *furihub;   /* refs FuriHub has (plinius-pkg installs those from there) */
    GHashTable *ratings;   /* AppStream ID -> "rating\tnote" from apps.conf */
} PackageState;

static void
package_state_free (PackageState *state)
{
    g_hash_table_unref (state->available);
    g_hash_table_unref (state->installed);
    g_hash_table_unref (state->required);
    g_hash_table_unref (state->provider);
    g_hash_table_unref (state->recommended);
    g_hash_table_unref (state->flatpaks);
    g_hash_table_unref (state->furihub);
    g_hash_table_unref (state->ratings);
    g_free (state);
}

G_DEFINE_AUTOPTR_CLEANUP_FUNC (PackageState, package_state_free)

static GHashTable *
string_set (void)
{
    return g_hash_table_new_full (g_str_hash, g_str_equal, g_free, NULL);
}

/* From pacman -Qi: who needs a package ("Required By") and who provides a name ("Provides",
 * e.g. gnome-control-center -> gnome-control-center-plinius) */
static void
read_package_info (const char *const *argv, GHashTable *required, GHashTable *provider)
{
    g_auto(GStrv) lines = run_lines (argv);
    g_autofree char *name = NULL;
    g_autofree char *field = NULL;

    for (char **l = lines; *l; l++) {
        g_auto(GStrv) values = NULL;

        if (**l == ' ') {
            /* Continuation line of a long field */
            values = g_strsplit_set (g_strstrip (*l), " ", -1);
        } else {
            char *colon = strchr (*l, ':');
            g_autofree char *value = NULL;

            if (colon == NULL)
                continue;
            g_free (field);
            field = g_strstrip (g_strndup (*l, colon - *l));
            value = g_strstrip (g_strdup (colon + 1));
            if (g_strcmp0 (field, "Name") == 0) {
                g_free (name);
                name = g_steal_pointer (&value);
                continue;
            }
            values = g_strcmp0 (value, "None") == 0 ? g_new0 (char *, 1) : g_strsplit_set (value, " ", -1);
        }
        if (name == NULL)
            continue;
        for (char **v = values; *v; v++) {
            if (**v == '\0')
                continue;
            if (g_strcmp0 (field, "Required By") == 0) {
                GPtrArray *by = g_hash_table_lookup (required, name);

                if (by == NULL) {
                    by = g_ptr_array_new_with_free_func (g_free);
                    g_hash_table_insert (required, g_strdup (name), by);
                }
                g_ptr_array_add (by, g_strdup (*v));
            } else if (g_strcmp0 (field, "Provides") == 0 && provider) {
                g_autofree char *provided = g_strndup (*v, strcspn (*v, "=<>"));

                if (!g_hash_table_contains (provider, provided))
                    g_hash_table_insert (provider, g_steal_pointer (&provided), g_strdup (name));
            }
        }
    }
}

/* Packages removed together with @pkg, or NULL if another package needs it (e.g.
 * plinius-apps). Add-ons such as firefox-i18n-de (pkg-..., not needed otherwise) go along. */
static GStrv
removal (const char *pkg, GHashTable *required)
{
    g_autoptr(GStrvBuilder) builder = g_strv_builder_new ();
    g_autofree char *prefix = g_strconcat (pkg, "-", NULL);
    GPtrArray *by = g_hash_table_lookup (required, pkg);

    g_strv_builder_add (builder, pkg);
    for (guint i = 0; by && i < by->len; i++) {
        const char *other = g_ptr_array_index (by, i);
        GPtrArray *other_by = g_hash_table_lookup (required, other);

        if (!g_str_has_prefix (other, prefix) || (other_by && other_by->len))
            return NULL;
        g_strv_builder_add (builder, other);
    }
    return g_strv_builder_end (builder);
}

static PackageState *
read_package_state (gboolean flathub)
{
    PackageState *state = g_new0 (PackageState, 1);
    const char *sl[] = { "pacman", "-Sl", NULL };
    const char *qq[] = { "pacman", "-Qq", NULL };
    const char *qi[] = { "env", "LC_ALL=C", "pacman", "-Qi", NULL };
    const char *fl[] = { "flatpak", "list", "--system", "--app", "--columns=application,origin", NULL };
    /* --cached: the summary from the last catalog update, no download */
    const char *fh[] = { "flatpak", "remote-ls", "--system", "--cached", "--app", "--columns=ref", "furihub", NULL };
    g_auto(GStrv) available = run_lines (sl);
    g_auto(GStrv) installed = run_lines (qq);
    g_autofree char *recommended = NULL;
    g_autofree char *conf = NULL;

    state->available = g_hash_table_new_full (g_str_hash, g_str_equal, g_free, g_free);
    state->installed = string_set ();
    state->required = g_hash_table_new_full (g_str_hash, g_str_equal, g_free, (GDestroyNotify) g_ptr_array_unref);
    state->provider = g_hash_table_new_full (g_str_hash, g_str_equal, g_free, g_free);
    state->recommended = string_set ();
    state->flatpaks = g_hash_table_new_full (g_str_hash, g_str_equal, g_free, g_free);
    state->furihub = string_set ();
    state->ratings = g_hash_table_new_full (g_str_hash, g_str_equal, g_free, g_free);

    for (char **l = available; *l; l++) {
        g_auto(GStrv) parts = g_strsplit (*l, " ", 4);

        /* First repo wins, as in pacman */
        if (g_strv_length (parts) >= 3 && !g_hash_table_contains (state->available, parts[1]))
            g_hash_table_insert (state->available, g_strdup (parts[1]), g_strdup (parts[2]));
    }
    for (char **l = installed; *l; l++)
        g_hash_table_add (state->installed, g_strdup (*l));
    read_package_info (qi, state->required, state->provider);

    if (g_file_get_contents (RECOMMENDED_FILE, &recommended, NULL, NULL)) {
        g_auto(GStrv) lines = g_strsplit (recommended, "\n", -1);

        for (char **l = lines; *l; l++) {
            g_strstrip (*l);
            if (**l && **l != '#')
                g_hash_table_add (state->recommended, g_strdup (*l));
        }
    }
    if (flathub) {
        g_auto(GStrv) flatpaks = run_lines (fl);

        for (char **l = flatpaks; *l; l++) {
            g_auto(GStrv) parts = g_strsplit (*l, "\t", 2);

            if (parts[0] && *parts[0])
                g_hash_table_insert (state->flatpaks, g_strdup (parts[0]), g_strdup (parts[1] ? parts[1] : ""));
        }
        if (furihub_enabled ()) {
            g_auto(GStrv) refs = run_lines (fh);

            for (char **l = refs; *l; l++)
                g_hash_table_add (state->furihub, g_strdup (*l));
        }
    }
    /* apps.conf: "ID rating [note]"; invalid lines are skipped */
    if (g_file_get_contents (RATINGS_FILE, &conf, NULL, NULL)) {
        g_auto(GStrv) lines = g_strsplit (conf, "\n", -1);

        for (char **l = lines; *l; l++) {
            g_autoptr(GRegex) regex = g_regex_new ("^(\\S+)\\s+(\\S+)\\s*(.*)$", 0, 0, NULL);
            g_autoptr(GMatchInfo) match = NULL;

            if (**l == '#' || !g_regex_match (regex, *l, 0, &match))
                continue;
            {
                g_autofree char *id = g_match_info_fetch (match, 1);
                g_autofree char *rating = g_match_info_fetch (match, 2);
                g_autofree char *note = g_match_info_fetch (match, 3);

                for (gsize r = 0; r < N_RATINGS; r++)
                    if (g_strcmp0 (rating, ratings[r].key) == 0)
                        g_hash_table_insert (state->ratings, g_steal_pointer (&id),
                                             g_strdup_printf ("%zu\t%s", r, g_strstrip (note)));
            }
        }
    }
    return state;
}

static CcpApp *
app_new (AsComponent *cpt, PackageState *state)
{
    g_autoptr(CcpApp) app = g_object_new (CCP_TYPE_APP, NULL);
    AsBundle *bundle = as_component_get_bundle (cpt, AS_BUNDLE_KIND_FLATPAK);
    const char *rating = g_hash_table_lookup (state->ratings, as_component_get_id (cpt));
    AsLaunchable *launchable = as_component_get_launchable (cpt, AS_LAUNCHABLE_KIND_DESKTOP_ID);
    GPtrArray *keywords = as_component_get_keywords (cpt);
    g_autoptr(GString) search = g_string_new (NULL);

    if (bundle) {
        g_auto(GStrv) ref = g_strsplit (as_bundle_get_id (bundle), "/", 4);
        AsReleaseList *releases = as_component_get_releases_plain (cpt);

        if (g_strv_length (ref) < 2)
            return NULL;
        app->source = SOURCE_FLATPAK;
        app->pkg = g_strdup (as_bundle_get_id (bundle));
        app->flatpak_id = g_strdup (ref[1]);
        /* Version of the newest release in the catalog, "" if none */
        app->version = g_strdup (releases && as_release_list_len (releases)
                                     ? as_release_get_version (as_release_list_index (releases, 0))
                                     : "");
        if (app->version == NULL)
            app->version = g_strdup ("");
        app->installed = g_hash_table_contains (state->flatpaks, app->flatpak_id);
        /* Installed: where from; otherwise where plinius-pkg flatpak-install takes it from */
        if (app->installed)
            app->remote = g_strdup (g_hash_table_lookup (state->flatpaks, app->flatpak_id));
        else
            app->remote = g_strdup (g_hash_table_contains (state->furihub, app->pkg) ? "furihub" : "flathub");
        if (app->installed) {
            const char *one[] = { app->flatpak_id, NULL };

            app->removal = g_strdupv ((char **) one);
        }
    } else {
        const char *pkg = as_component_get_pkgname (cpt);

        /* The catalog comes from Arch x86_64; only show what actually exists in this state */
        if (pkg == NULL || !g_hash_table_contains (state->available, pkg))
            return NULL;
        /* A custom build is installed instead of the Arch package (provides): that one counts */
        if (!g_hash_table_contains (state->installed, pkg) && g_hash_table_contains (state->provider, pkg))
            pkg = g_hash_table_lookup (state->provider, pkg);
        app->source = SOURCE_PACMAN;
        app->pkg = g_strdup (pkg);
        app->version = g_strdup (g_hash_table_lookup (state->available, pkg));
        if (app->version == NULL)
            app->version = g_strdup ("");
        app->installed = g_hash_table_contains (state->installed, pkg);
        app->recommended = g_hash_table_contains (state->recommended, pkg);
        if (app->installed)
            app->removal = removal (pkg, state->required);
    }

    app->cpt = g_object_ref (cpt);
    app->id = g_strdup (as_component_get_id (cpt));
    app->name = g_strdup (as_component_get_name (cpt) ? as_component_get_name (cpt) : app->id);
    app->summary = g_strdup (as_component_get_summary (cpt) ? as_component_get_summary (cpt) : "");
    if (rating) {
        const char *tab = strchr (rating, '\t');

        app->rating = (Rating) g_ascii_strtoull (rating, NULL, 10);
        app->note = tab && tab[1] ? g_strdup (P_(tab + 1)) : NULL;
    } else {
        app->rating = classify (cpt);
    }
    if (app->note == NULL)
        app->note = g_strdup (P_(ratings[app->rating].note));

    g_string_append_printf (search, "%s %s %s", app->name, app->summary, app->pkg);
    for (guint i = 0; keywords && i < keywords->len; i++)
        g_string_append_printf (search, " %s", (const char *) g_ptr_array_index (keywords, i));
    app->search = g_utf8_casefold (search->str, -1);
    app->icon_name = g_strdup (stock_icon_name (cpt));
    if (launchable && as_launchable_get_entries (launchable)->len)
        app->desktop_id = g_strdup (g_ptr_array_index (as_launchable_get_entries (launchable), 0));
    if (app->installed && app->desktop_id) {
        g_autoptr(GDesktopAppInfo) info = g_desktop_app_info_new (app->desktop_id);

        app->hidden = info && !g_app_info_should_show (G_APP_INFO (info));
    }
    return g_steal_pointer (&app);
}

static gint
compare_apps (gconstpointer a, gconstpointer b)
{
    CcpApp *x = *(CcpApp **) a, *y = *(CcpApp **) b;
    g_autofree char *kx = g_utf8_collate_key (x->name, -1);
    g_autofree char *ky = g_utf8_collate_key (y->name, -1);

    return strcmp (kx, ky);
}

/* Runs in a thread: the AppStream catalog and the package state from the installed sync
 * database; with Flathub on, also the Flatpak catalogs (/var/lib/flatpak/appstream) */
static void
load_catalog_thread (GTask *task, gpointer source, gpointer data, GCancellable *cancellable)
{
    gboolean flathub = flathub_enabled ();
    g_autoptr(AsPool) pool = as_pool_new ();
    g_autoptr(AsComponentBox) box = NULL;
    g_autoptr(PackageState) state = NULL;
    g_autoptr(GHashTable) by_id = g_hash_table_new (g_str_hash, g_str_equal);
    g_autoptr(GPtrArray) apps = g_ptr_array_new_with_free_func (g_object_unref);
    GHashTableIter iter;
    gpointer value;
    GError *error = NULL;

    as_pool_set_flags (pool, AS_POOL_FLAG_LOAD_OS_CATALOG | (flathub ? AS_POOL_FLAG_LOAD_FLATPAK : 0));
    if (!as_pool_load (pool, cancellable, &error)) {
        g_task_return_error (task, error);
        return;
    }
    state = read_package_state (flathub);
    box = as_pool_get_components_by_kind (pool, AS_COMPONENT_KIND_DESKTOP_APP);
    for (guint i = 0; i < as_component_box_len (box); i++) {
        CcpApp *app = app_new (as_component_box_index (box, i), state);
        CcpApp *old;

        if (app == NULL)
            continue;
        /* If an app is in both sources, the installed one wins, otherwise the one from the
         * repositories */
        old = g_hash_table_lookup (by_id, app->id);
        if (old == NULL || app->installed > old->installed ||
            (app->installed == old->installed && app->source == SOURCE_PACMAN && old->source != SOURCE_PACMAN)) {
            g_hash_table_insert (by_id, app->id, app);
            g_ptr_array_add (apps, app);
        } else {
            g_object_unref (app);
        }
    }
    /* Only the winners */
    {
        g_autoptr(GPtrArray) result = g_ptr_array_new_with_free_func (g_object_unref);

        g_hash_table_iter_init (&iter, by_id);
        while (g_hash_table_iter_next (&iter, NULL, &value))
            g_ptr_array_add (result, g_object_ref (value));
        g_ptr_array_sort (result, compare_apps);
        /* Free the pool and the losers before returning, so that the trim after the swap
         * (catalog_loaded) finds their memory free */
        g_clear_pointer (&by_id, g_hash_table_unref);
        g_clear_pointer (&apps, g_ptr_array_unref);
        g_clear_object (&box);
        g_clear_object (&pool);
        g_task_return_pointer (task, g_steal_pointer (&result), (GDestroyNotify) g_ptr_array_unref);
    }
}

static void
trim_thread (GTask *task, gpointer source, gpointer data, GCancellable *cancellable)
{
    malloc_trim (0);
    g_task_return_boolean (task, TRUE);
}

/* Every load fills a worker thread's malloc arena with tens of MB of small AppStream objects.
 * When they are freed, a few live chunks keep glibc from shrinking the arena, so after some
 * reloads the long-running service held ~500 MB of free memory. Hand it back to the system,
 * in a thread because walking the arenas takes a moment. */
static void
trim_freed_memory (void)
{
    g_autoptr(GTask) task = g_task_new (NULL, NULL, NULL, NULL);

    g_task_run_in_thread (task, trim_thread);
}

/* ----- The catalog, shared by all pages while one of them uses it ----- */

#define CCP_TYPE_CATALOG (ccp_catalog_get_type ())
G_DECLARE_FINAL_TYPE (CcpCatalog, ccp_catalog, CCP, CATALOG, GObject)

typedef enum {
    CATALOG_EMPTY,
    CATALOG_LOADING,
    CATALOG_READY,
    CATALOG_FAILED
} CatalogState;

struct _CcpCatalog {
    GObject parent_instance;

    GListStore *store;
    CatalogState state;
    gboolean reload_pending;
    GCancellable *cancellable;
    /* Icons: decoded one after another in a thread (Arch ships them as JPEG XL), kept */
    GThreadPool *icon_pool;
    GHashTable *icons;   /* path -> GdkTexture, or the path alone while loading/failed */
    GHashTable *waiting; /* path -> GPtrArray of IconRequest */
    SoupSession *soup;
};

G_DEFINE_FINAL_TYPE (CcpCatalog, ccp_catalog, G_TYPE_OBJECT)

enum {
    SIGNAL_CHANGED,
    N_SIGNALS
};
static guint catalog_signals[N_SIGNALS];

static CcpCatalog *shared_catalog;

typedef struct {
    GWeakRef image;
    char *path;
} IconRequest;

static void
icon_request_free (IconRequest *request)
{
    g_weak_ref_clear (&request->image);
    g_free (request->path);
    g_free (request);
}

typedef struct {
    CcpCatalog *catalog;
    char *path;
    GdkTexture *texture;
} IconJob;

static gboolean
icon_done (gpointer data)
{
    IconJob *job = data;
    GPtrArray *requests = NULL;

    g_hash_table_steal_extended (job->catalog->waiting, job->path, NULL, (gpointer *) &requests);
    if (job->texture)
        g_hash_table_insert (job->catalog->icons, g_strdup (job->path), g_object_ref (job->texture));
    for (guint i = 0; requests && i < requests->len; i++) {
        IconRequest *request = g_ptr_array_index (requests, i);
        g_autoptr(GtkImage) image = g_weak_ref_get (&request->image);
        const char *key = image ? g_object_get_data (G_OBJECT (image), "plinius-icon") : NULL;

        /* Recycled list rows: only if the row still shows this app */
        if (image && job->texture && g_strcmp0 (key, job->path) == 0)
            gtk_image_set_from_paintable (image, GDK_PAINTABLE (job->texture));
    }
    g_clear_pointer (&requests, g_ptr_array_unref);
    g_clear_object (&job->texture);
    g_object_unref (job->catalog);
    g_free (job->path);
    g_free (job);
    return G_SOURCE_REMOVE;
}

static void
icon_work (gpointer data, gpointer user_data)
{
    IconJob *job = data;

    /* Since GTK 4.24 Gtk.IconTheme no longer loads JPEG XL from a file, Gdk.Texture via glycin does */
    job->texture = gdk_texture_new_from_filename (job->path, NULL);
    g_main_context_invoke (NULL, icon_done, job);
}

static void
ccp_catalog_dispose (GObject *object)
{
    CcpCatalog *catalog = CCP_CATALOG (object);

    g_cancellable_cancel (catalog->cancellable);
    G_OBJECT_CLASS (ccp_catalog_parent_class)->dispose (object);
}

static void
ccp_catalog_finalize (GObject *object)
{
    CcpCatalog *catalog = CCP_CATALOG (object);

    /* Jobs hold a reference, so none is pending here */
    g_thread_pool_free (catalog->icon_pool, TRUE, FALSE);
    g_clear_object (&catalog->store);
    g_clear_object (&catalog->cancellable);
    g_clear_pointer (&catalog->icons, g_hash_table_unref);
    g_clear_pointer (&catalog->waiting, g_hash_table_unref);
    g_clear_object (&catalog->soup);
    G_OBJECT_CLASS (ccp_catalog_parent_class)->finalize (object);
    trim_freed_memory ();
}

static void
ccp_catalog_class_init (CcpCatalogClass *klass)
{
    GObjectClass *object_class = G_OBJECT_CLASS (klass);

    object_class->dispose = ccp_catalog_dispose;
    object_class->finalize = ccp_catalog_finalize;
    catalog_signals[SIGNAL_CHANGED] =
        g_signal_new ("changed", G_TYPE_FROM_CLASS (klass), G_SIGNAL_RUN_LAST, 0, NULL, NULL, NULL, G_TYPE_NONE, 0);
}

static void
ccp_catalog_init (CcpCatalog *catalog)
{
    catalog->store = g_list_store_new (CCP_TYPE_APP);
    catalog->cancellable = g_cancellable_new ();
    catalog->icon_pool = g_thread_pool_new (icon_work, NULL, 1, FALSE, NULL);
    catalog->icons = g_hash_table_new_full (g_str_hash, g_str_equal, g_free, g_object_unref);
    catalog->waiting = g_hash_table_new_full (g_str_hash, g_str_equal, g_free, (GDestroyNotify) g_ptr_array_unref);
    catalog->soup = soup_session_new_with_options ("timeout", 30, "user-agent", "plinius-software", NULL);
}

/* The catalog all pages share (new reference); created on first use */
static CcpCatalog *
ccp_catalog_get (void)
{
    if (shared_catalog)
        return g_object_ref (shared_catalog);
    shared_catalog = g_object_new (CCP_TYPE_CATALOG, NULL);
    g_object_add_weak_pointer (G_OBJECT (shared_catalog), (gpointer *) &shared_catalog);
    return shared_catalog;
}

static void ccp_catalog_load (CcpCatalog *catalog);

static void
catalog_loaded (GObject *source, GAsyncResult *result, gpointer data)
{
    g_autoptr(CcpCatalog) catalog = data;
    g_autoptr(GError) error = NULL;
    g_autoptr(GPtrArray) apps = g_task_propagate_pointer (G_TASK (result), &error);

    if (g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
        return;
    if (apps == NULL) {
        /* Otherwise "Explore" would be stuck at the spinner */
        g_warning ("Could not load the app catalog: %s", error->message);
        catalog->state = CATALOG_FAILED;
        g_list_store_remove_all (catalog->store);
    } else {
        catalog->state = apps->len ? CATALOG_READY : CATALOG_FAILED;
        g_list_store_splice (catalog->store, 0, g_list_model_get_n_items (G_LIST_MODEL (catalog->store)), apps->pdata,
                             apps->len);
    }
    /* The previous catalog is gone now (unless a page still shows one of its apps) */
    trim_freed_memory ();
    g_signal_emit (catalog, catalog_signals[SIGNAL_CHANGED], 0);
    if (catalog->reload_pending) {
        catalog->reload_pending = FALSE;
        ccp_catalog_load (catalog);
    }
}

/* Load the catalog in the background, again after an operation */
static void
ccp_catalog_load (CcpCatalog *catalog)
{
    g_autoptr(GTask) task = NULL;

    if (catalog->state == CATALOG_LOADING) {
        catalog->reload_pending = TRUE;
        return;
    }
    catalog->state = CATALOG_LOADING;
    task = g_task_new (NULL, catalog->cancellable, catalog_loaded, g_object_ref (catalog));
    g_task_run_in_thread (task, load_catalog_thread);
}

static void
ccp_catalog_ensure (CcpCatalog *catalog)
{
    if (catalog->state == CATALOG_EMPTY)
        ccp_catalog_load (catalog);
}

/* After an operation: whoever uses the catalog sees the new state */
static void
reload_shared_catalog (void)
{
    if (shared_catalog && shared_catalog->state != CATALOG_EMPTY)
        ccp_catalog_load (shared_catalog);
}

/* Show the theme icon or a placeholder at once, then the catalog icon */
static void
set_app_icon (GtkImage *image, CcpApp *app, guint size)
{
    GtkIconTheme *theme = gtk_icon_theme_get_for_display (gtk_widget_get_display (GTK_WIDGET (image)));
    const char *path = icon_file (app->cpt, size);
    CcpCatalog *catalog = shared_catalog;
    GdkTexture *texture;
    GPtrArray *requests;
    IconRequest *request;
    IconJob *job;

    gtk_image_set_from_icon_name (image, app->icon_name && gtk_icon_theme_has_icon (theme, app->icon_name)
                                             ? app->icon_name
                                             : "application-x-executable");
    g_object_set_data_full (G_OBJECT (image), "plinius-icon", g_strdup (path), g_free);
    if (path == NULL || catalog == NULL)
        return;

    texture = g_hash_table_lookup (catalog->icons, path);
    if (texture) {
        gtk_image_set_from_paintable (image, GDK_PAINTABLE (texture));
        return;
    }
    request = g_new0 (IconRequest, 1);
    g_weak_ref_init (&request->image, image);
    request->path = g_strdup (path);
    requests = g_hash_table_lookup (catalog->waiting, path);
    if (requests) {
        g_ptr_array_add (requests, request);
        return;
    }
    requests = g_ptr_array_new_with_free_func ((GDestroyNotify) icon_request_free);
    g_ptr_array_add (requests, request);
    g_hash_table_insert (catalog->waiting, g_strdup (path), requests);
    job = g_new0 (IconJob, 1);
    job->catalog = g_object_ref (catalog);
    job->path = g_strdup (path);
    g_thread_pool_push (catalog->icon_pool, job, NULL);
}

/* ----- Progress of plinius-pkg ----- */

/* Steps of an operation: key (a section marker of the output), title, weight in the overall progress */
typedef struct {
    const char *key;
    const char *title;
    guint weight;
} Step;

static const Step STEP_SYNC = { "sync", N_("Fetching package lists"), 1 };
static const Step STEP_DOWNLOAD = { "download", N_("Downloading packages"), 4 };
static const Step STEP_CHECK = { "check", N_("Checking packages"), 1 };
static const Step STEP_INSTALL = { "install", N_("Installing packages"), 4 };
static const Step STEP_REMOVE = { "install", N_("Removing packages"), 2 };
static const Step STEP_HOOKS = { "hooks", N_("Finishing up"), 1 };
static const Step STEP_FLATPAK_UPDATE = { "flatpak", N_("Updating Flatpak apps"), 3 };
static const Step STEP_FLATHUB_CATALOG = { "flathub", N_("Loading the Flathub catalog"), 3 };
static const Step STEP_FURIHUB_CATALOG = { "furihub", N_("Loading the FuriHub catalog"), 1 };

/* pacman output with LC_ALL=C and --noprogressbar (plinius-pkg): sections and counters */
static const struct {
    const char *pattern;
    const char *key; /* NULL: the key is the first group */
} markers[] = {
    { "^:: Synchronizing package databases", "sync" },
    { "^:: Retrieving packages", "download" },
    { "^(checking keyring|checking package integrity|loading package files|checking for file conflicts|"
      "checking available disk space)",
      "check" },
    { "^:: Processing package changes", "install" },
    { "^:: Running post-transaction hooks", "hooks" },
    { "^::plinius:: (\\w+)", NULL },
};

typedef struct {
    GPtrArray *steps; /* const Step * */
    gint index;
    guint done;  /* done in the current step */
    guint total; /* packages according to "Packages (N)" */
    guint hooks; /* hooks according to "(i/N)" */
} Progress;

static const char *
progress_key (Progress *p)
{
    return p->index >= 0 ? ((const Step *) g_ptr_array_index (p->steps, p->index))->key : NULL;
}

static void
progress_enter (Progress *p, const char *key)
{
    /* Skipped steps (e.g. nothing to download) count as done; never go backwards */
    for (guint i = p->index + 1; i < p->steps->len; i++) {
        if (g_strcmp0 (((const Step *) g_ptr_array_index (p->steps, i))->key, key) == 0) {
            p->index = i;
            p->done = 0;
            return;
        }
    }
}

static gboolean
line_matches (const char *pattern, const char *line, GMatchInfo **match)
{
    g_autoptr(GRegex) regex = g_regex_new (pattern, 0, 0, NULL);

    return g_regex_match (regex, line, 0, match);
}

/* One output line: a section marker switches steps, other lines count */
static void
progress_feed (Progress *p, const char *line)
{
    g_autoptr(GMatchInfo) match = NULL;
    const char *key = progress_key (p);

    for (gsize i = 0; i < G_N_ELEMENTS (markers); i++) {
        g_autoptr(GMatchInfo) m = NULL;

        if (line_matches (markers[i].pattern, line, &m)) {
            g_autofree char *group = markers[i].key ? NULL : g_match_info_fetch (m, 1);

            progress_enter (p, markers[i].key ? markers[i].key : group);
            return;
        }
    }
    if (line_matches ("^Packages \\((\\d+)\\)", line, &match)) {
        g_autofree char *n = g_match_info_fetch (match, 1);

        p->total = g_ascii_strtoull (n, NULL, 10);
    } else if (g_strcmp0 (key, "download") == 0 && line_matches ("^ \\S+ downloading\\.\\.\\.$", line, NULL)) {
        p->done++;
    } else if (g_strcmp0 (key, "install") == 0 &&
               line_matches ("^(installing|upgrading|reinstalling|downgrading|removing) \\S+\\.\\.\\.$", line, NULL)) {
        p->done++;
    } else if (g_strcmp0 (key, "hooks") == 0) {
        g_clear_pointer (&match, g_match_info_unref);
        if (line_matches ("^\\(\\s*(\\d+)/(\\d+)\\)", line, &match)) {
            g_autofree char *i = g_match_info_fetch (match, 1);
            g_autofree char *n = g_match_info_fetch (match, 2);

            p->done = g_ascii_strtoull (i, NULL, 10);
            p->hooks = g_ascii_strtoull (n, NULL, 10);
        }
    }
}

/* (done, total) in the current step, total 0 = unknown */
static void
progress_count (Progress *p, guint *done, guint *total)
{
    const char *key = progress_key (p);

    *done = p->done;
    if (g_strcmp0 (key, "download") == 0 || g_strcmp0 (key, "install") == 0)
        *total = p->total;
    else if (g_strcmp0 (key, "hooks") == 0)
        *total = p->hooks;
    else
        *total = 0;
}

/* Overall progress 0..1: finished steps by weight plus the share of the current one */
static double
progress_fraction (Progress *p)
{
    guint all = 0, done_weight = 0, n, total;

    for (guint i = 0; i < p->steps->len; i++) {
        guint w = ((const Step *) g_ptr_array_index (p->steps, i))->weight;

        all += w;
        if ((gint) i < p->index)
            done_weight += w;
    }
    progress_count (p, &n, &total);
    if (p->index >= 0 && total)
        return (done_weight + ((const Step *) g_ptr_array_index (p->steps, p->index))->weight *
                                  MIN ((double) n / total, 1.0)) / all;
    return (double) done_weight / all;
}

/* ----- Operations: plinius-pkg via pkexec, with a progress page ----- */

typedef struct _Operation Operation;

/* Sets the result on the page; TRUE leaves out the "Done" button */
typedef gboolean (*OperationDone) (Operation *op, gint code);

struct _Operation {
    GWeakRef home; /* the page "Back" and "Done" lead to (the panel) */
    AdwNavigationPage *page;
    GtkWidget *icon;
    GtkWidget *status;
    GtkWidget *step;
    GtkWidget *bar;
    GtkWidget *detail;
    GtkWidget *buttons;
    GtkWidget *details;
    GtkTextBuffer *log;
    Progress progress;
    gboolean with_progress;
    GSubprocess *proc;
    GDataInputStream *stream;
    OperationDone done;
    CcpApp *app;  /* install/remove from Explore */
    char *name;   /* the app's name for the result texts */
    char **launch; /* install: desktop ID or Flatpak ID to open */
};

static gboolean operation_busy;

static void
operation_free (Operation *op)
{
    g_weak_ref_clear (&op->home);
    g_clear_object (&op->page);
    g_clear_pointer (&op->progress.steps, g_ptr_array_unref);
    g_clear_object (&op->proc);
    g_clear_object (&op->stream);
    g_clear_object (&op->app);
    g_free (op->name);
    g_strfreev (op->launch);
    g_free (op);
}

static AdwNavigationView *
navigation_of (GtkWidget *widget)
{
    return ADW_NAVIGATION_VIEW (gtk_widget_get_ancestor (widget, ADW_TYPE_NAVIGATION_VIEW));
}

/* Back to the panel's page */
static void
go_home (Operation *op)
{
    g_autoptr(AdwNavigationPage) home = g_weak_ref_get (&op->home);
    AdwNavigationView *nav = navigation_of (GTK_WIDGET (op->page));

    if (nav && home)
        adw_navigation_view_pop_to_page (nav, home);
}

static void
operation_add_button (Operation *op, const char *label, const char *style, GCallback callback, gpointer data)
{
    GtkWidget *button = pill (label, style);

    g_signal_connect_swapped (button, "clicked", callback, data);
    gtk_box_append (GTK_BOX (op->buttons), button);
}

static void
done_clicked (Operation *op)
{
    go_home (op);
}

static void
operation_add_done (Operation *op)
{
    operation_add_button (op, P_("Done"), NULL, G_CALLBACK (done_clicked), op);
}

/* Show the result; the log stays reachable under "Details" on failure */
static void
operation_finish (Operation *op, gboolean ok, const char *title, const char *text)
{
    adw_navigation_page_set_can_pop (op->page, TRUE);
    gtk_image_set_from_icon_name (GTK_IMAGE (op->icon), ok ? "object-select-symbolic" : "dialog-warning-symbolic");
    gtk_widget_remove_css_class (op->icon, "dim-label");
    gtk_widget_add_css_class (op->icon, ok ? "success" : "error");
    gtk_label_set_text (GTK_LABEL (op->status), title);
    gtk_label_set_text (GTK_LABEL (op->step), text ? text : "");
    gtk_widget_set_visible (op->step, text && *text);
    gtk_widget_set_visible (op->bar, FALSE);
    gtk_widget_set_visible (op->detail, FALSE);
    gtk_widget_set_visible (op->details, !ok);
}

static void
operation_add_line (Operation *op, const char *line)
{
    GtkTextIter end;
    guint done, total;
    const Step *step;
    g_autofree char *text = NULL;

    gtk_text_buffer_get_end_iter (op->log, &end);
    gtk_text_buffer_insert (op->log, &end, line, -1);
    gtk_text_buffer_insert (op->log, &end, "\n", 1);
    if (!op->with_progress) {
        gtk_progress_bar_pulse (GTK_PROGRESS_BAR (op->bar));
        return;
    }
    progress_feed (&op->progress, line);
    if (op->progress.index < 0)
        return;
    step = g_ptr_array_index (op->progress.steps, op->progress.index);
    /* Translators: "Step 2 of 4: Downloading packages" */
    text = g_strdup (P_("Step {current} of {total}: {title}"));
    {
        g_autofree char *current = g_strdup_printf ("%d", op->progress.index + 1);
        g_autofree char *count = g_strdup_printf ("%u", op->progress.steps->len);
        g_autoptr(GString) s = g_string_new (text);

        g_string_replace (s, "{current}", current, 0);
        g_string_replace (s, "{total}", count, 0);
        g_string_replace (s, "{title}", P_(step->title), 0);
        gtk_label_set_text (GTK_LABEL (op->step), s->str);
    }
    progress_count (&op->progress, &done, &total);
    if (total) {
        g_autoptr(GString) s = g_string_new (P_("{done} of {total}"));
        g_autofree char *d = g_strdup_printf ("%u", done);
        g_autofree char *t = g_strdup_printf ("%u", total);

        g_string_replace (s, "{done}", d, 0);
        g_string_replace (s, "{total}", t, 0);
        gtk_label_set_text (GTK_LABEL (op->detail), s->str);
    } else {
        gtk_label_set_text (GTK_LABEL (op->detail), "");
    }
    gtk_progress_bar_set_fraction (GTK_PROGRESS_BAR (op->bar), progress_fraction (&op->progress));
}

/* plinius-software's texts have {name} placeholders, and so have their translations */
static char *
with_name (const char *text, const char *name)
{
    g_autoptr(GString) s = g_string_new (text);

    g_string_replace (s, "{name}", name, 0);
    return g_string_free (g_steal_pointer (&s), FALSE);
}

static void
go_to_updates (Operation *op)
{
    open_panel ("updates");
}

/* Failures any operation can have; TRUE if @code is one of them */
static gboolean
common_failure (Operation *op, gint code)
{
    if (code == EXIT_BUSY) {
        operation_finish (op, FALSE, P_("Another operation is running"), P_("Wait until it has finished and try again."));
        return TRUE;
    }
    if (code == EXIT_STALE_DB || code == EXIT_STALE_FILE) {
        /* The installed database state is outdated: send to Updates */
        operation_finish (op, FALSE, P_("Update the system first"),
                          P_("The app belongs to a newer state than the installed system."));
        operation_add_button (op, P_("Go to Updates"), "suggested-action", G_CALLBACK (go_to_updates), op);
        return TRUE;
    }
    return FALSE;
}

/* Keep the window open while an operation runs */
static gboolean
window_close_request (GtkWindow *window)
{
    AdwDialog *dialog;

    if (!operation_busy)
        return FALSE;
    dialog = adw_alert_dialog_new (P_("Please wait until the operation has finished"), NULL);
    adw_alert_dialog_add_response (ADW_ALERT_DIALOG (dialog), "ok", P_("OK"));
    adw_dialog_present (dialog, GTK_WIDGET (window));
    return TRUE;
}

static void operation_read_line (Operation *op);

static void
operation_exited (GObject *source, GAsyncResult *result, gpointer data)
{
    Operation *op = data;
    g_autoptr(GError) error = NULL;
    g_autoptr(AdwNavigationPage) home = g_weak_ref_get (&op->home);
    AdwNavigationView *nav = navigation_of (GTK_WIDGET (op->page));
    gint code = 1;

    if (g_subprocess_wait_finish (G_SUBPROCESS (source), result, &error) && g_subprocess_get_if_exited (op->proc))
        code = g_subprocess_get_exit_status (op->proc);
    operation_busy = FALSE;

    if (code == EXIT_PKEXEC_DISMISSED || code == EXIT_PKEXEC_NOT_AUTHORIZED) {
        adw_navigation_page_set_can_pop (op->page, TRUE);
        if (nav && adw_navigation_view_get_visible_page (nav) == op->page)
            adw_navigation_view_pop (nav);
        operation_free (op);
        return;
    }

    /* Back leads to the panel; detail pages would show an outdated state after the operation */
    if (nav && home) {
        GListModel *stack = adw_navigation_view_get_navigation_stack (nav);
        g_autoptr(GPtrArray) pages = g_ptr_array_new_with_free_func (g_object_unref);

        for (guint i = 0; i < g_list_model_get_n_items (stack); i++) {
            AdwNavigationPage *page = g_list_model_get_item (stack, i);

            g_ptr_array_add (pages, page);
            if (page == home)
                break;
        }
        g_object_unref (stack);
        if (pages->len && g_ptr_array_index (pages, pages->len - 1) == home) {
            g_ptr_array_add (pages, g_object_ref (op->page));
            adw_navigation_view_replace (nav, (AdwNavigationPage **) pages->pdata, pages->len);
        }
    }
    if (!op->done (op, code))
        operation_add_done (op);
    reload_shared_catalog ();
    /* The page keeps the operation for its buttons */
    g_object_set_data_full (G_OBJECT (op->page), "plinius-operation", op, (GDestroyNotify) operation_free);
}

static void
operation_line_read (GObject *source, GAsyncResult *result, gpointer data)
{
    Operation *op = data;
    g_autoptr(GError) error = NULL;
    g_autofree char *line = g_data_input_stream_read_line_finish_utf8 (op->stream, result, NULL, &error);

    if (line == NULL && error == NULL) {
        g_subprocess_wait_async (op->proc, NULL, operation_exited, op);
        return;
    }
    /* Not valid UTF-8, but the line has been consumed anyway */
    operation_add_line (op, line ? line : "\xef\xbf\xbd");
    operation_read_line (op);
}

static void
operation_read_line (Operation *op)
{
    g_data_input_stream_read_line_async (op->stream, G_PRIORITY_DEFAULT, NULL, operation_line_read, op);
}

/* Run plinius-pkg via pkexec with @args, with progress on a page pushed on @panel. @steps
 * NULL: the bar only pulses (Flatpak). */
static void
align_to_expander (GtkExpander *expander, GParamSpec *pspec, GtkWidget *box)
{
    gtk_widget_set_valign (box, gtk_expander_get_expanded (expander) ? GTK_ALIGN_START : GTK_ALIGN_CENTER);
}

static Operation *
operation_new (CcPanel *panel, const char *title, const Step *const *steps)
{
    Operation *op = g_new0 (Operation, 1);
    GtkWidget *box = gtk_box_new (GTK_ORIENTATION_VERTICAL, 12);
    GtkWidget *log_view, *log_scroll;

    g_weak_ref_init (&op->home, panel);
    op->with_progress = steps != NULL;
    op->progress.steps = g_ptr_array_new ();
    op->progress.index = -1;
    for (gsize i = 0; steps && steps[i]; i++)
        g_ptr_array_add (op->progress.steps, (gpointer) steps[i]);

    op->icon = gtk_image_new_from_icon_name ("software-update-available-symbolic");
    gtk_image_set_pixel_size (GTK_IMAGE (op->icon), 64);
    gtk_widget_set_margin_top (op->icon, 24);
    gtk_widget_add_css_class (op->icon, "dim-label");
    op->status = wrapped_label (title, "title-2");
    op->step = wrapped_label (P_("Preparing…"), NULL);
    op->bar = gtk_progress_bar_new ();
    gtk_widget_set_margin_top (op->bar, 6);
    op->detail = wrapped_label ("", "dim-label");
    gtk_widget_add_css_class (op->detail, "caption");
    op->buttons = gtk_box_new (GTK_ORIENTATION_VERTICAL, 12);
    gtk_widget_set_margin_top (op->buttons, 12);
    /* Only someone tracking down an error needs the log */
    log_view = gtk_text_view_new ();
    gtk_text_view_set_editable (GTK_TEXT_VIEW (log_view), FALSE);
    gtk_text_view_set_cursor_visible (GTK_TEXT_VIEW (log_view), FALSE);
    gtk_text_view_set_monospace (GTK_TEXT_VIEW (log_view), TRUE);
    gtk_text_view_set_wrap_mode (GTK_TEXT_VIEW (log_view), GTK_WRAP_WORD_CHAR);
    op->log = gtk_text_view_get_buffer (GTK_TEXT_VIEW (log_view));
    log_scroll = gtk_scrolled_window_new ();
    gtk_scrolled_window_set_child (GTK_SCROLLED_WINDOW (log_scroll), log_view);
    gtk_scrolled_window_set_min_content_height (GTK_SCROLLED_WINDOW (log_scroll), 240);
    gtk_widget_add_css_class (log_scroll, "card");
    op->details = gtk_expander_new (P_("Details"));
    gtk_expander_set_child (GTK_EXPANDER (op->details), log_scroll);
    gtk_widget_set_visible (op->details, FALSE);

    gtk_box_append (GTK_BOX (box), op->icon);
    gtk_box_append (GTK_BOX (box), op->status);
    gtk_box_append (GTK_BOX (box), op->step);
    gtk_box_append (GTK_BOX (box), op->bar);
    gtk_box_append (GTK_BOX (box), op->detail);
    gtk_box_append (GTK_BOX (box), op->buttons);
    gtk_box_append (GTK_BOX (box), op->details);
    /* Status, progress and buttons in the middle of the page, within reach of the thumb; from the
     * top once the log is open, so that it has the room below */
    gtk_widget_set_valign (box, GTK_ALIGN_CENTER);
    g_signal_connect_object (op->details, "notify::expanded", G_CALLBACK (align_to_expander), box, 0);
    op->page = g_object_ref_sink (navigation_page (title, scrolled (box)));
    /* It cannot be left until the operation is done */
    adw_navigation_page_set_can_pop (op->page, FALSE);
    return op;
}

static void
operation_start (Operation *op, CcPanel *panel, const char *const *args, OperationDone done)
{
    g_autoptr(GStrvBuilder) builder = g_strv_builder_new ();
    g_auto(GStrv) argv = NULL;
    g_autoptr(GError) error = NULL;
    GtkRoot *root = gtk_widget_get_root (GTK_WIDGET (panel));

    op->done = done;
    if (root && !g_object_get_data (G_OBJECT (root), "plinius-close-guard")) {
        g_signal_connect (root, "close-request", G_CALLBACK (window_close_request), NULL);
        g_object_set_data (G_OBJECT (root), "plinius-close-guard", GINT_TO_POINTER (TRUE));
    }
    cc_panel_push_subpage (panel, op->page);

    g_strv_builder_add_many (builder, "pkexec", HELPER, NULL);
    g_strv_builder_addv (builder, (const char **) args);
    argv = g_strv_builder_end (builder);
    op->proc = g_subprocess_newv ((const char *const *) argv, G_SUBPROCESS_FLAGS_STDOUT_PIPE | G_SUBPROCESS_FLAGS_STDERR_MERGE,
                                  &error);
    if (op->proc == NULL) {
        operation_finish (op, FALSE, P_("Could not start"), error->message);
        operation_add_done (op);
        g_object_set_data_full (G_OBJECT (op->page), "plinius-operation", op, (GDestroyNotify) operation_free);
        return;
    }
    operation_busy = TRUE;
    op->stream = g_data_input_stream_new (g_subprocess_get_stdout_pipe (op->proc));
    operation_read_line (op);
}

/* Start an installed app via its launcher, for new Flatpak apps via flatpak run */
static void
launch_app (const char *desktop_id, const char *flatpak_id)
{
    g_autoptr(GDesktopAppInfo) info = desktop_id ? g_desktop_app_info_new (desktop_id) : NULL;

    if (info) {
        g_app_info_launch (G_APP_INFO (info), NULL, NULL, NULL);
    } else if (flatpak_id) {
        /* The running session only finds the launchers of new Flatpak apps after the next login */
        const char *argv[] = { "flatpak", "run", flatpak_id, NULL };
        g_autoptr(GSubprocess) proc = g_subprocess_newv (argv, G_SUBPROCESS_FLAGS_NONE, NULL);
    }
}

static void
launch_clicked (Operation *op)
{
    launch_app (op->launch[0][0] ? op->launch[0] : NULL, op->launch[1][0] ? op->launch[1] : NULL);
}

/* Phosh's app overview only shows apps with X-Purism-FormFactor=Mobile in its mobile filter.
 * Apps rated phone-ready here are added to force-adaptive, or they would be missing. */
static void
show_in_phosh (CcpApp *app)
{
    GSettingsSchemaSource *source = g_settings_schema_source_get_default ();
    /* mobi.phosh.shell since Phosh 0.59, sm.puri.phosh before */
    g_autoptr(GSettingsSchema) schema = source ? g_settings_schema_source_lookup (source, "mobi.phosh.shell", TRUE) : NULL;
    g_autoptr(GDesktopAppInfo) info = NULL;
    g_autoptr(GSettings) settings = NULL;
    g_auto(GStrv) ids = NULL;
    g_autoptr(GStrvBuilder) builder = NULL;
    g_auto(GStrv) new_ids = NULL;

    if (schema == NULL && source)
        schema = g_settings_schema_source_lookup (source, "sm.puri.phosh", TRUE);
    if ((app->rating != RATING_PHONE && app->rating != RATING_SCALED) || app->desktop_id == NULL || schema == NULL)
        return;
    info = g_desktop_app_info_new (app->desktop_id);
    if (info) {
        g_autofree char *purism = g_desktop_app_info_get_string (info, "X-Purism-FormFactor");
        g_autofree char *kde = g_desktop_app_info_get_string (info, "X-KDE-FormFactor");
        g_autofree char *factors = g_utf8_casefold (g_strconcat (purism ? purism : "", ";", kde ? kde : "", NULL), -1);

        if (strstr (factors, "mobile"))
            return;
    }
    settings = g_settings_new_full (schema, NULL, NULL);
    ids = g_settings_get_strv (settings, "force-adaptive");
    if (g_strv_contains ((const char *const *) ids, app->desktop_id))
        return;
    builder = g_strv_builder_new ();
    g_strv_builder_addv (builder, (const char **) ids);
    g_strv_builder_add (builder, app->desktop_id);
    new_ids = g_strv_builder_end (builder);
    g_settings_set_strv (settings, "force-adaptive", (const char *const *) new_ids);
}

static gboolean
install_done (Operation *op, gint code)
{
    if (code == 0) {
        g_autofree char *title = with_name (P_("{name} is installed"), op->name);

        if (op->app)
            show_in_phosh (op->app);
        operation_finish (op, TRUE, title, NULL);
        if (op->launch && (op->launch[0][0] || op->launch[1][0]))
            operation_add_button (op, P_("Open"), "suggested-action", G_CALLBACK (launch_clicked), op);
    } else if (code == EXIT_HELD) {
        g_autofree char *text = with_name (P_("{name} needs packages that are still being tested with the Plinius "
                                              "adaptations. It can be installed once they are approved."),
                                           op->name);

        operation_finish (op, FALSE, P_("Waiting for approval"), text);
    } else if (!common_failure (op, code)) {
        operation_finish (op, FALSE, P_("Installation failed"), P_("See “Details” for more information"));
    }
    return FALSE;
}

static void
install_app (CcPanel *panel, CcpApp *app)
{
    const Step *const steps[] = { &STEP_DOWNLOAD, &STEP_CHECK, &STEP_INSTALL, &STEP_HOOKS, NULL };
    g_autofree char *title = with_name (P_("Installing {name}"), app->name);
    gboolean flatpak = app->source == SOURCE_FLATPAK;
    Operation *op = operation_new (panel, title, flatpak ? NULL : steps);
    const char *args[] = { flatpak ? "flatpak-install" : "install", app->pkg, NULL };
    const char *launch[] = { app->desktop_id ? app->desktop_id : "", flatpak ? app->flatpak_id : "", NULL };

    op->app = g_object_ref (app);
    op->name = g_strdup (app->name);
    op->launch = g_strdupv ((char **) launch);
    operation_start (op, panel, args, install_done);
}

static gboolean
remove_done (Operation *op, gint code)
{
    if (code == 0) {
        g_autofree char *title = with_name (P_("{name} has been removed"), op->name);

        operation_finish (op, TRUE, title, NULL);
    } else if (!common_failure (op, code)) {
        operation_finish (op, FALSE, P_("Removal failed"), P_("See “Details” for more information"));
    }
    return FALSE;
}

typedef struct {
    GWeakRef panel;
    char *name;
    gboolean flatpak;
    GStrv removal;
} RemoveRequest;

static void
remove_request_free (RemoveRequest *request)
{
    g_weak_ref_clear (&request->panel);
    g_free (request->name);
    g_strfreev (request->removal);
    g_free (request);
}

static void
remove_response (AdwAlertDialog *dialog, const char *response, RemoveRequest *request)
{
    const Step *const steps[] = { &STEP_REMOVE, &STEP_HOOKS, NULL };
    g_autoptr(CcPanel) panel = g_weak_ref_get (&request->panel);
    g_autoptr(GStrvBuilder) builder = NULL;
    g_auto(GStrv) args = NULL;
    g_autofree char *title = NULL;
    Operation *op;

    if (g_strcmp0 (response, "remove") != 0 || panel == NULL)
        return;
    builder = g_strv_builder_new ();
    g_strv_builder_add (builder, request->flatpak ? "flatpak-remove" : "remove");
    g_strv_builder_addv (builder, (const char **) request->removal);
    args = g_strv_builder_end (builder);
    title = with_name (P_("Removing {name}"), request->name);
    op = operation_new (panel, title, request->flatpak ? NULL : steps);
    op->name = g_strdup (request->name);
    operation_start (op, panel, (const char *const *) args, remove_done);
}

/* Ask before removing an app */
static void
confirm_remove (CcPanel *panel, const char *name, gboolean flatpak, GStrv packages)
{
    g_autofree char *heading = with_name (P_("Remove {name}?"), name);
    AdwDialog *dialog = adw_alert_dialog_new (heading, P_("The app and dependencies that are no longer needed will be "
                                                          "removed. Your files and settings are kept."));
    RemoveRequest *request = g_new0 (RemoveRequest, 1);

    g_weak_ref_init (&request->panel, panel);
    request->name = g_strdup (name);
    request->flatpak = flatpak;
    request->removal = g_strdupv (packages);
    adw_alert_dialog_add_response (ADW_ALERT_DIALOG (dialog), "cancel", P_("Cancel"));
    adw_alert_dialog_add_response (ADW_ALERT_DIALOG (dialog), "remove", P_("Remove"));
    adw_alert_dialog_set_response_appearance (ADW_ALERT_DIALOG (dialog), "remove", ADW_RESPONSE_DESTRUCTIVE);
    g_signal_connect_data (dialog, "response", G_CALLBACK (remove_response), request,
                           (GClosureNotify) (void (*) (void)) remove_request_free, 0);
    adw_dialog_present (dialog, GTK_WIDGET (panel));
}

/* ----- The Apps panel: tabs Explore | Installed | Web Apps | Settings ----- */

typedef struct {
    CcPanel *panel; /* owner */
    CcpCatalog *catalog;
    gulong catalog_handler;
    GtkEditable *search;
    GtkWidget *search_bar;
    GtkWidget *webapps_add;  /* Add in the header bar, only on Web Apps */
    AdwViewStack *tabs;
    GtkStack *explore_stack;
    GtkCustomFilter *filter;
    GtkFilterListModel *results;
    GtkWidget *category_rows[G_N_ELEMENTS (categories)];
    gboolean shown[N_RATINGS];
    AdwSwitchRow *rating_rows[N_RATINGS];
    AdwSwitchRow *flathub_row;
    gboolean flathub_guard;
    AdwSwitchRow *furihub_row;
    gboolean furihub_guard;
    /* Beta updates: the channel of [plinius] */
    AdwSwitchRow *beta_row;
    gboolean beta_guard;
    /* An installed app's page */
    GtkWidget *remove_button;
    GtkWidget *system_hint;
    GtkWidget *package_group;
    AdwActionRow *package_row;
    GCancellable *app_cancellable;
    char *app_name;
    gboolean app_flatpak;
    GStrv app_removal;
} Software;

static void
software_free (Software *sw)
{
    if (sw->catalog_handler)
        g_signal_handler_disconnect (sw->catalog, sw->catalog_handler);
    g_clear_object (&sw->catalog);
    g_clear_object (&sw->filter);
    g_clear_object (&sw->results);
    g_cancellable_cancel (sw->app_cancellable);
    g_clear_object (&sw->app_cancellable);
    g_free (sw->app_name);
    g_strfreev (sw->app_removal);
    g_free (sw);
}

static Software *
software_of (CcPanel *panel)
{
    return g_object_get_data (G_OBJECT (panel), "plinius-software");
}

/* What "Explore" offers: not installed, rating selected (Settings tab) */
static gboolean
offered (Software *sw, CcpApp *app)
{
    return !app->installed && sw->shown[app->rating];
}

static char **
search_words (Software *sw)
{
    g_autofree char *folded = g_utf8_casefold (gtk_editable_get_text (sw->search), -1);

    return g_strsplit_set (g_strstrip (folded), " \t", -1);
}

static gboolean
searching (Software *sw)
{
    g_autofree char *text = g_strdup (gtk_editable_get_text (sw->search));

    return *g_strstrip (text) != '\0';
}

static gboolean
explore_filter (gpointer item, gpointer data)
{
    Software *sw = data;
    CcpApp *app = item;
    g_auto(GStrv) words = NULL;

    if (!offered (sw, app))
        return FALSE;
    words = search_words (sw);
    for (char **w = words; *w; w++)
        if (**w && strstr (app->search, *w) == NULL)
            return FALSE;
    return TRUE;
}

/* Switch "Explore" between spinner, categories, results and empty state; update the counts */
static void
update_explore (Software *sw)
{
    GListModel *store = G_LIST_MODEL (sw->catalog->store);
    guint n_apps = g_list_model_get_n_items (store);
    guint counts[G_N_ELEMENTS (categories)] = { 0 };

    if (sw->catalog->state == CATALOG_FAILED) {
        gtk_stack_set_visible_child_name (sw->explore_stack, "nocatalog");
        return;
    }
    if (n_apps == 0) {
        gtk_stack_set_visible_child_name (sw->explore_stack, "loading");
        return;
    }
    if (searching (sw)) {
        gtk_stack_set_visible_child_name (sw->explore_stack, g_list_model_get_n_items (G_LIST_MODEL (sw->results))
                                                                 ? "list"
                                                                 : "empty");
        return;
    }
    for (guint i = 0; i < n_apps; i++) {
        g_autoptr(CcpApp) app = g_list_model_get_item (store, i);

        if (!offered (sw, app))
            continue;
        for (gsize g = 0; g < G_N_ELEMENTS (categories); g++)
            if (in_category (app, g))
                counts[g]++;
    }
    for (gsize g = 0; g < G_N_ELEMENTS (categories); g++) {
        g_autofree char *text = g_strdup (NP_("{n} app", "{n} apps", counts[g]));
        g_autofree char *n = g_strdup_printf ("%u", counts[g]);
        g_autoptr(GString) s = g_string_new (text);

        g_string_replace (s, "{n}", n, 0);
        adw_action_row_set_subtitle (ADW_ACTION_ROW (sw->category_rows[g]), s->str);
        /* Hide empty groups, e.g. "Recommended" once everything is installed */
        gtk_widget_set_visible (sw->category_rows[g], counts[g] > 0);
    }
    gtk_stack_set_visible_child_name (sw->explore_stack, "categories");
}

static void
refilter (Software *sw)
{
    gtk_filter_changed (GTK_FILTER (sw->filter), GTK_FILTER_CHANGE_DIFFERENT);
    update_explore (sw);
}

/* --- App rows of the lists --- */

static void
row_setup (GtkSignalListItemFactory *factory, GtkListItem *item)
{
    GtkWidget *box = gtk_box_new (GTK_ORIENTATION_HORIZONTAL, 12);
    GtkWidget *texts = gtk_box_new (GTK_ORIENTATION_VERTICAL, 0);
    GtkWidget *icon = gtk_image_new ();
    GtkWidget *name = gtk_label_new (NULL);
    GtkWidget *summary = gtk_label_new (NULL);
    GtkWidget *badge = gtk_label_new (NULL);

    gtk_widget_set_margin_top (box, 6);
    gtk_widget_set_margin_bottom (box, 6);
    gtk_image_set_pixel_size (GTK_IMAGE (icon), 48);
    gtk_widget_set_valign (texts, GTK_ALIGN_CENTER);
    gtk_widget_set_hexpand (texts, TRUE);
    gtk_label_set_xalign (GTK_LABEL (name), 0);
    gtk_label_set_ellipsize (GTK_LABEL (name), PANGO_ELLIPSIZE_END);
    gtk_widget_add_css_class (name, "heading");
    gtk_label_set_xalign (GTK_LABEL (summary), 0);
    gtk_label_set_ellipsize (GTK_LABEL (summary), PANGO_ELLIPSIZE_END);
    gtk_widget_add_css_class (summary, "dim-label");
    gtk_label_set_xalign (GTK_LABEL (badge), 0);
    gtk_widget_add_css_class (badge, "caption");
    gtk_box_append (GTK_BOX (texts), name);
    gtk_box_append (GTK_BOX (texts), summary);
    gtk_box_append (GTK_BOX (texts), badge);
    gtk_box_append (GTK_BOX (box), icon);
    gtk_box_append (GTK_BOX (box), texts);
    g_object_set_data (G_OBJECT (box), "icon", icon);
    g_object_set_data (G_OBJECT (box), "name", name);
    g_object_set_data (G_OBJECT (box), "summary", summary);
    g_object_set_data (G_OBJECT (box), "badge", badge);
    gtk_list_item_set_child (item, box);
}

static void
row_bind (GtkSignalListItemFactory *factory, GtkListItem *item)
{
    GtkWidget *box = gtk_list_item_get_child (item);
    CcpApp *app = gtk_list_item_get_item (item);
    GtkWidget *badge = g_object_get_data (G_OBJECT (box), "badge");
    g_autoptr(GString) text = g_string_new (P_(ratings[app->rating].label));

    set_app_icon (g_object_get_data (G_OBJECT (box), "icon"), app, 64);
    gtk_label_set_text (g_object_get_data (G_OBJECT (box), "name"), app->name);
    gtk_label_set_text (g_object_get_data (G_OBJECT (box), "summary"), app->summary);
    for (gsize r = 0; r < N_RATINGS; r++)
        gtk_widget_remove_css_class (badge, ratings[r].style);
    gtk_widget_add_css_class (badge, ratings[app->rating].style);
    if (app->source == SOURCE_FLATPAK)
        g_string_append_printf (text, " · %s", remote_title (app->remote));
    gtk_label_set_text (GTK_LABEL (badge), text->str);
}

static void push_detail_page (Software *sw, CcpApp *app);

static void
list_activated (GtkListView *view, guint position, Software *sw)
{
    GListModel *model = G_LIST_MODEL (gtk_list_view_get_model (view));
    g_autoptr(CcpApp) app = g_list_model_get_item (model, position);

    push_detail_page (sw, app);
}

/* Scrollable list of @model; activating a row opens the app's details */
static GtkWidget *
app_list (Software *sw, GListModel *model)
{
    GtkListItemFactory *factory = gtk_signal_list_item_factory_new ();
    GtkWidget *view, *scroll;

    g_signal_connect (factory, "setup", G_CALLBACK (row_setup), NULL);
    g_signal_connect (factory, "bind", G_CALLBACK (row_bind), NULL);
    view = gtk_list_view_new (GTK_SELECTION_MODEL (gtk_no_selection_new (g_object_ref (model))), factory);
    gtk_list_view_set_single_click_activate (GTK_LIST_VIEW (view), TRUE);
    gtk_widget_add_css_class (view, "navigation-sidebar");
    g_signal_connect (view, "activate", G_CALLBACK (list_activated), sw);
    scroll = gtk_scrolled_window_new ();
    gtk_scrolled_window_set_child (GTK_SCROLLED_WINDOW (scroll), view);
    gtk_scrolled_window_set_policy (GTK_SCROLLED_WINDOW (scroll), GTK_POLICY_NEVER, GTK_POLICY_AUTOMATIC);
    gtk_widget_set_vexpand (scroll, TRUE);
    return scroll;
}

/* --- A category --- */

typedef struct {
    Software *sw;
    gsize group;
} CategoryFilter;

static gboolean
category_filter (gpointer item, gpointer data)
{
    CategoryFilter *cf = data;

    return offered (cf->sw, item) && in_category (item, cf->group);
}

static void
category_activated (AdwActionRow *row, Software *sw)
{
    CategoryFilter *cf = g_new0 (CategoryFilter, 1);
    GtkCustomFilter *filter;
    g_autoptr(GtkFilterListModel) model = NULL;

    cf->sw = sw;
    cf->group = GPOINTER_TO_SIZE (g_object_get_data (G_OBJECT (row), "plinius-group"));
    filter = gtk_custom_filter_new (category_filter, cf, g_free);
    model = gtk_filter_list_model_new (g_object_ref (G_LIST_MODEL (sw->catalog->store)), GTK_FILTER (filter));
    cc_panel_push_subpage (sw->panel, navigation_page (P_(categories[cf->group].title), app_list (sw, G_LIST_MODEL (model))));
}

/* --- An app's details --- */

static void
detail_install (GtkButton *button, CcpApp *app)
{
    install_app (CC_PANEL (g_object_get_data (G_OBJECT (button), "plinius-panel")), app);
}

static void
detail_open (GtkButton *button, CcpApp *app)
{
    launch_app (app->desktop_id, app->source == SOURCE_FLATPAK ? app->flatpak_id : NULL);
}

static void
detail_remove (GtkButton *button, CcpApp *app)
{
    confirm_remove (CC_PANEL (g_object_get_data (G_OBJECT (button), "plinius-panel")), app->name,
                    app->source == SOURCE_FLATPAK, app->removal);
}

static void
website_activated (AdwActionRow *row, const char *uri)
{
    g_autoptr(GtkUriLauncher) launcher = gtk_uri_launcher_new (uri);

    gtk_uri_launcher_launch (launcher, GTK_WINDOW (gtk_widget_get_root (GTK_WIDGET (row))), NULL, NULL, NULL);
}

typedef struct {
    GWeakRef stack;
    GWeakRef picture;
} ShotRequest;

static void
screenshot_loaded (GObject *source, GAsyncResult *result, gpointer data)
{
    ShotRequest *request = data;
    g_autoptr(GBytes) bytes = soup_session_send_and_read_finish (SOUP_SESSION (source), result, NULL);
    g_autoptr(GtkStack) stack = g_weak_ref_get (&request->stack);
    g_autoptr(GtkPicture) picture = g_weak_ref_get (&request->picture);
    SoupMessage *message = soup_session_get_async_result_message (SOUP_SESSION (source), result);
    g_autoptr(GdkTexture) texture = NULL;

    if (bytes && message && soup_message_get_status (message) == SOUP_STATUS_OK)
        texture = gdk_texture_new_from_bytes (bytes, NULL);
    if (stack && picture) {
        if (texture)
            gtk_picture_set_paintable (picture, GDK_PAINTABLE (texture));
        gtk_stack_set_visible_child_name (stack, texture ? "picture" : "failed");
    }
    g_weak_ref_clear (&request->stack);
    g_weak_ref_clear (&request->picture);
    g_free (request);
}

/* Screenshots from the network, as linked in AppStream (the respective app's server) */
static GtkWidget *
screenshots (Software *sw, GPtrArray *urls)
{
    GtkWidget *carousel = adw_carousel_new ();
    GtkWidget *box = gtk_box_new (GTK_ORIENTATION_VERTICAL, 6);

    adw_carousel_set_spacing (ADW_CAROUSEL (carousel), 12);
    adw_carousel_set_allow_scroll_wheel (ADW_CAROUSEL (carousel), FALSE);
    for (guint i = 0; i < urls->len; i++) {
        const char *url = g_ptr_array_index (urls, i);
        GtkWidget *stack = gtk_stack_new ();
        GtkWidget *picture = gtk_picture_new ();
        GtkWidget *failed = gtk_label_new (P_("Image not available"));
        ShotRequest *request;
        SoupMessage *message;

        gtk_widget_set_size_request (stack, -1, 320);
        gtk_widget_set_hexpand (stack, TRUE);
        gtk_stack_add_named (GTK_STACK (stack), adw_spinner_new (), "loading");
        gtk_picture_set_content_fit (GTK_PICTURE (picture), GTK_CONTENT_FIT_CONTAIN);
        gtk_picture_set_can_shrink (GTK_PICTURE (picture), TRUE);
        gtk_stack_add_named (GTK_STACK (stack), picture, "picture");
        gtk_widget_add_css_class (failed, "dim-label");
        gtk_stack_add_named (GTK_STACK (stack), failed, "failed");
        adw_carousel_append (ADW_CAROUSEL (carousel), stack);

        /* Only https, so nobody on the network can swap the image or see which app is being looked at */
        message = g_str_has_prefix (url, "https://") ? soup_message_new ("GET", url) : NULL;
        if (message == NULL) {
            gtk_stack_set_visible_child_name (GTK_STACK (stack), "failed");
            continue;
        }
        request = g_new0 (ShotRequest, 1);
        g_weak_ref_init (&request->stack, stack);
        g_weak_ref_init (&request->picture, picture);
        soup_session_send_and_read_async (sw->catalog->soup, message, G_PRIORITY_LOW, sw->catalog->cancellable,
                                          screenshot_loaded, request);
        g_object_unref (message);
    }
    gtk_box_append (GTK_BOX (box), carousel);
    if (urls->len > 1) {
        GtkWidget *dots = adw_carousel_indicator_dots_new ();

        adw_carousel_indicator_dots_set_carousel (ADW_CAROUSEL_INDICATOR_DOTS (dots), ADW_CAROUSEL (carousel));
        gtk_box_append (GTK_BOX (box), dots);
    }
    return box;
}

/* App details: install/open/remove, screenshots, rating, description, package info */
static void
push_detail_page (Software *sw, CcpApp *app)
{
    GtkWidget *box = gtk_box_new (GTK_ORIENTATION_VERTICAL, 18);
    GtkWidget *head = gtk_box_new (GTK_ORIENTATION_VERTICAL, 6);
    GtkWidget *actions = gtk_box_new (GTK_ORIENTATION_VERTICAL, 12);
    GtkWidget *icon = gtk_image_new ();
    GtkWidget *group = adw_preferences_group_new ();
    GtkWidget *info = adw_preferences_group_new ();
    AdwActionRow *rating, *package;
    g_autoptr(GPtrArray) urls = screenshot_urls (app->cpt, 624);
    const char *description = as_component_get_description (app->cpt);
    const char *homepage = as_component_get_url (app->cpt, AS_URL_KIND_HOMEPAGE);

    gtk_image_set_pixel_size (GTK_IMAGE (icon), 96);
    set_app_icon (GTK_IMAGE (icon), app, 128);
    gtk_box_append (GTK_BOX (head), icon);
    gtk_box_append (GTK_BOX (head), wrapped_label (app->name, "title-1"));
    gtk_box_append (GTK_BOX (head), wrapped_label (app->summary, "dim-label"));
    gtk_box_append (GTK_BOX (box), head);

    if (!app->installed) {
        GtkWidget *button = pill (P_("Install"), "suggested-action");

        g_object_set_data (G_OBJECT (button), "plinius-panel", sw->panel);
        g_signal_connect_data (button, "clicked", G_CALLBACK (detail_install), g_object_ref (app),
                               (GClosureNotify) (void (*) (void)) g_object_unref, 0);
        gtk_box_append (GTK_BOX (actions), button);
    } else {
        if (app->desktop_id || app->source == SOURCE_FLATPAK) {
            GtkWidget *button = pill (P_("Open"), "suggested-action");

            g_signal_connect_data (button, "clicked", G_CALLBACK (detail_open), g_object_ref (app),
                                   (GClosureNotify) (void (*) (void)) g_object_unref, 0);
            gtk_box_append (GTK_BOX (actions), button);
        }
        if (app->removal) {
            GtkWidget *button = pill (P_("Remove"), "destructive-action");

            g_object_set_data (G_OBJECT (button), "plinius-panel", sw->panel);
            g_signal_connect_data (button, "clicked", G_CALLBACK (detail_remove), g_object_ref (app),
                                   (GClosureNotify) (void (*) (void)) g_object_unref, 0);
            gtk_box_append (GTK_BOX (actions), button);
        } else {
            GtkWidget *hint = wrapped_label (P_("Part of the system (Phosh, phone) and cannot be removed."), "dim-label");

            gtk_widget_add_css_class (hint, "caption");
            gtk_box_append (GTK_BOX (actions), hint);
        }
    }
    gtk_box_append (GTK_BOX (box), actions);

    if (urls->len)
        gtk_box_append (GTK_BOX (box), screenshots (sw, urls));

    rating = plain_row (P_(ratings[app->rating].label), app->note);
    gtk_widget_add_css_class (GTK_WIDGET (rating), "property");
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), GTK_WIDGET (rating));
    gtk_box_append (GTK_BOX (box), group);

    if (description) {
        g_autofree char *text = as_markup_convert (description, AS_MARKUP_KIND_TEXT, NULL);

        if (text) {
            GtkWidget *label = gtk_label_new (g_strstrip (text));

            gtk_label_set_wrap (GTK_LABEL (label), TRUE);
            gtk_label_set_xalign (GTK_LABEL (label), 0);
            gtk_label_set_selectable (GTK_LABEL (label), TRUE);
            gtk_box_append (GTK_BOX (box), label);
        }
    }

    adw_preferences_group_set_title (ADW_PREFERENCES_GROUP (info), P_("Details"));
    if (app->source == SOURCE_FLATPAK) {
        g_autofree char *text = g_strstrip (g_strdup_printf ("%s %s", remote_title (app->remote), app->version));

        package = plain_row (P_("Source"), text);
    } else {
        g_autofree char *text = g_strdup_printf ("%s %s", app->pkg, app->version);

        package = plain_row (P_("Package"), text);
    }
    gtk_widget_add_css_class (GTK_WIDGET (package), "property");
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (info), GTK_WIDGET (package));
    if (homepage) {
        AdwActionRow *row = plain_row (P_("Website"), homepage);

        gtk_list_box_row_set_activatable (GTK_LIST_BOX_ROW (row), TRUE);
        adw_action_row_add_suffix (row, gtk_image_new_from_icon_name ("adw-external-link-symbolic"));
        g_signal_connect_data (row, "activated", G_CALLBACK (website_activated), g_strdup (homepage),
                               (GClosureNotify) (void (*) (void)) g_free, 0);
        adw_preferences_group_add (ADW_PREFERENCES_GROUP (info), GTK_WIDGET (row));
    }
    gtk_box_append (GTK_BOX (box), info);

    cc_panel_push_subpage (sw->panel, navigation_page (app->name, scrolled (box)));
}

/* --- Explore --- */

static GtkWidget *
status_page (const char *icon, const char *title, const char *description)
{
    GtkWidget *page = adw_status_page_new ();

    adw_status_page_set_icon_name (ADW_STATUS_PAGE (page), icon);
    adw_status_page_set_title (ADW_STATUS_PAGE (page), title);
    if (description)
        adw_status_page_set_description (ADW_STATUS_PAGE (page), description);
    return page;
}

static GtkWidget *
build_explore (Software *sw)
{
    GtkWidget *spinner = adw_spinner_new ();
    GtkWidget *categories_page = adw_preferences_page_new ();
    GtkWidget *group = adw_preferences_group_new ();

    sw->explore_stack = GTK_STACK (gtk_stack_new ());
    gtk_widget_set_vexpand (GTK_WIDGET (sw->explore_stack), TRUE);
    gtk_stack_add_named (sw->explore_stack, spinner, "loading");

    /* One category per row: icon, name, number of apps, arrow */
    for (gsize g = 0; g < G_N_ELEMENTS (categories); g++) {
        AdwActionRow *row = plain_row (P_(categories[g].title), NULL);
        GtkWidget *icon = gtk_image_new_from_icon_name (categories[g].icon);

        gtk_list_box_row_set_activatable (GTK_LIST_BOX_ROW (row), TRUE);
        adw_action_row_add_prefix (row, icon);
        adw_action_row_add_suffix (row, gtk_image_new_from_icon_name ("go-next-symbolic"));
        g_object_set_data (G_OBJECT (row), "plinius-group", GSIZE_TO_POINTER (g));
        g_signal_connect (row, "activated", G_CALLBACK (category_activated), sw);
        adw_preferences_group_add (ADW_PREFERENCES_GROUP (group), GTK_WIDGET (row));
        sw->category_rows[g] = GTK_WIDGET (row);
    }
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (categories_page), ADW_PREFERENCES_GROUP (group));
    gtk_stack_add_named (sw->explore_stack, categories_page, "categories");

    sw->filter = gtk_custom_filter_new (explore_filter, sw, NULL);
    sw->results = gtk_filter_list_model_new (g_object_ref (G_LIST_MODEL (sw->catalog->store)),
                                             GTK_FILTER (g_object_ref (sw->filter)));
    gtk_stack_add_named (sw->explore_stack, app_list (sw, G_LIST_MODEL (sw->results)), "list");
    gtk_stack_add_named (sw->explore_stack, status_page ("system-search-symbolic", P_("No Results"), NULL), "empty");
    gtk_stack_add_named (sw->explore_stack,
                         status_page ("dialog-warning-symbolic", P_("No App Catalog"),
                                      P_("The catalog could not be loaded. Is the package archlinux-appstream-data missing?")),
                         "nocatalog");
    gtk_stack_set_visible_child_name (sw->explore_stack, "loading");
    return GTK_WIDGET (sw->explore_stack);
}

/* --- Settings tab --- */

static void
rating_row_changed (AdwSwitchRow *row, GParamSpec *pspec, Software *sw)
{
    Rating rating = GPOINTER_TO_INT (g_object_get_data (G_OBJECT (row), "plinius-rating"));

    sw->shown[rating] = adw_switch_row_get_active (row);
    set_rating_shown (rating, sw->shown[rating]);
    refilter (sw);
}

static void
set_flathub_row (Software *sw, gboolean active)
{
    sw->flathub_guard = TRUE;
    adw_switch_row_set_active (sw->flathub_row, active);
    sw->flathub_guard = FALSE;
}

static gboolean
flathub_enable_done (Operation *op, gint code)
{
    CcPanel *panel = g_weak_ref_get (&op->home);
    Software *sw = panel ? software_of (panel) : NULL;

    if (sw)
        set_flathub_row (sw, code == 0);
    g_clear_object (&panel);
    if (code == 0)
        operation_finish (op, TRUE, P_("Flathub is on"),
                          P_("Apps from Flathub appear under “Explore” marked “Flathub”. The app grid shows their "
                             "launchers after you log in again."));
    else if (!common_failure (op, code))
        operation_finish (op, FALSE, P_("Could not turn on Flathub"), P_("See “Details” for more information"));
    return FALSE;
}

static gboolean
flathub_disable_done (Operation *op, gint code)
{
    CcPanel *panel = g_weak_ref_get (&op->home);
    Software *sw = panel ? software_of (panel) : NULL;

    if (sw)
        set_flathub_row (sw, code != 0);
    g_clear_object (&panel);
    if (code == 0)
        operation_finish (op, TRUE, P_("Flathub is off"), NULL);
    else if (code == EXIT_FLATHUB_IN_USE)
        operation_finish (op, FALSE, P_("Apps from Flathub are still installed"), P_("Remove them under “Installed” first."));
    else if (code == EXIT_FURIHUB_ON)
        operation_finish (op, FALSE, P_("FuriHub needs Flathub"),
                          P_("FuriHub’s apps take their runtimes from Flathub. Turn off FuriHub first."));
    else if (!common_failure (op, code))
        operation_finish (op, FALSE, P_("Could not turn off Flathub"), P_("See “Details” for more information"));
    return FALSE;
}

static void
flathub_response (AdwAlertDialog *dialog, const char *response, Software *sw)
{
    const Step *const steps[] = { &STEP_DOWNLOAD, &STEP_CHECK, &STEP_INSTALL, &STEP_HOOKS, &STEP_FLATHUB_CATALOG, NULL };
    const char *args[] = { "flathub-enable", NULL };

    if (g_strcmp0 (response, "enable") != 0) {
        set_flathub_row (sw, FALSE);
        return;
    }
    operation_start (operation_new (sw->panel, P_("Turning on Flathub"), steps), sw->panel, args, flathub_enable_done);
}

/* The switch only follows the result of plinius-pkg */
static void
flathub_row_changed (AdwSwitchRow *row, GParamSpec *pspec, Software *sw)
{
    if (sw->flathub_guard)
        return;
    if (adw_switch_row_get_active (row)) {
        AdwDialog *dialog =
            adw_alert_dialog_new (P_("Turn On Flathub?"),
                                  P_("Flathub is an app store for Flatpak apps, independent of system updates. The apps "
                                     "run in a sandbox and bring their own runtimes, so the first app downloads a few "
                                     "hundred MB.\n\nOn the Plinius, Flatpak apps do not yet have graphics acceleration via "
                                     "libhybris; they render in software and run slower."));

        adw_alert_dialog_add_response (ADW_ALERT_DIALOG (dialog), "cancel", P_("Cancel"));
        adw_alert_dialog_add_response (ADW_ALERT_DIALOG (dialog), "enable", P_("Turn On"));
        adw_alert_dialog_set_response_appearance (ADW_ALERT_DIALOG (dialog), "enable", ADW_RESPONSE_SUGGESTED);
        adw_alert_dialog_set_close_response (ADW_ALERT_DIALOG (dialog), "cancel");
        g_signal_connect (dialog, "response", G_CALLBACK (flathub_response), sw);
        adw_dialog_present (dialog, GTK_WIDGET (sw->panel));
    } else {
        const char *args[] = { "flathub-disable", NULL };

        operation_start (operation_new (sw->panel, P_("Turning off Flathub"), NULL), sw->panel, args, flathub_disable_done);
    }
}

/* --- FuriHub: Flatpak apps of the FuriLabs user community, runtimes from Flathub --- */

static void
set_furihub_row (Software *sw, gboolean active)
{
    sw->furihub_guard = TRUE;
    adw_switch_row_set_active (sw->furihub_row, active);
    sw->furihub_guard = FALSE;
}

static gboolean
furihub_enable_done (Operation *op, gint code)
{
    CcPanel *panel = g_weak_ref_get (&op->home);
    Software *sw = panel ? software_of (panel) : NULL;

    if (sw) {
        set_furihub_row (sw, code == 0);
        /* plinius-pkg turns Flathub on first; it may be on even if FuriHub then failed */
        set_flathub_row (sw, flathub_enabled ());
    }
    g_clear_object (&panel);
    if (code == 0)
        operation_finish (op, TRUE, P_("FuriHub is on"),
                          P_("Apps from FuriHub appear under “Explore” marked “FuriHub”. The app grid shows their "
                             "launchers after you log in again."));
    else if (!common_failure (op, code))
        operation_finish (op, FALSE, P_("Could not turn on FuriHub"), P_("See “Details” for more information"));
    return FALSE;
}

static gboolean
furihub_disable_done (Operation *op, gint code)
{
    CcPanel *panel = g_weak_ref_get (&op->home);
    Software *sw = panel ? software_of (panel) : NULL;

    if (sw)
        set_furihub_row (sw, code != 0);
    g_clear_object (&panel);
    if (code == 0)
        operation_finish (op, TRUE, P_("FuriHub is off"), NULL);
    else if (code == EXIT_FLATHUB_IN_USE)
        operation_finish (op, FALSE, P_("Apps from FuriHub are still installed"), P_("Remove them under “Installed” first."));
    else if (!common_failure (op, code))
        operation_finish (op, FALSE, P_("Could not turn off FuriHub"), P_("See “Details” for more information"));
    return FALSE;
}

static void
furihub_response (AdwAlertDialog *dialog, const char *response, Software *sw)
{
    const Step *const steps[] = { &STEP_DOWNLOAD,        &STEP_CHECK,           &STEP_INSTALL, &STEP_HOOKS,
                                  &STEP_FLATHUB_CATALOG, &STEP_FURIHUB_CATALOG, NULL };
    const char *args[] = { "furihub-enable", NULL };

    if (g_strcmp0 (response, "enable") != 0) {
        set_furihub_row (sw, FALSE);
        return;
    }
    operation_start (operation_new (sw->panel, P_("Turning on FuriHub"), steps), sw->panel, args, furihub_enable_done);
}

/* The switch only follows the result of plinius-pkg */
static void
furihub_row_changed (AdwSwitchRow *row, GParamSpec *pspec, Software *sw)
{
    if (sw->furihub_guard)
        return;
    if (adw_switch_row_get_active (row)) {
        AdwDialog *dialog =
            adw_alert_dialog_new (P_("Turn On FuriHub?"),
                                  P_("FuriHub is a Flatpak app store of the FuriLabs user community, with apps made for "
                                     "Linux phones. Its apps take their runtimes from Flathub, so Flathub is turned on "
                                     "as well.\n\nThe apps are written for FuriOS; some rely on its services, such as "
                                     "the camera, and may not work on the Plinius."));

        adw_alert_dialog_add_response (ADW_ALERT_DIALOG (dialog), "cancel", P_("Cancel"));
        adw_alert_dialog_add_response (ADW_ALERT_DIALOG (dialog), "enable", P_("Turn On"));
        adw_alert_dialog_set_response_appearance (ADW_ALERT_DIALOG (dialog), "enable", ADW_RESPONSE_SUGGESTED);
        adw_alert_dialog_set_close_response (ADW_ALERT_DIALOG (dialog), "cancel");
        g_signal_connect (dialog, "response", G_CALLBACK (furihub_response), sw);
        adw_dialog_present (dialog, GTK_WIDGET (sw->panel));
    } else {
        const char *args[] = { "furihub-disable", NULL };

        operation_start (operation_new (sw->panel, P_("Turning off FuriHub"), NULL), sw->panel, args, furihub_disable_done);
    }
}

/* --- Beta updates: the channel of [plinius] --- */

static void
set_beta_row (Software *sw, gboolean active)
{
    sw->beta_guard = TRUE;
    adw_switch_row_set_active (sw->beta_row, active);
    sw->beta_guard = FALSE;
}

/* The switch follows pacman.conf, whatever plinius-pkg did; Updates checks the new channel when
 * it is opened */
static gboolean
channel_done (Operation *op, gint code)
{
    CcPanel *panel = g_weak_ref_get (&op->home);
    Software *sw = panel ? software_of (panel) : NULL;
    gboolean beta = plinius_channel () == 1;

    if (sw && sw->beta_row)
        set_beta_row (sw, beta);
    g_clear_object (&panel);
    if (code == 0 && beta) {
        operation_finish (op, TRUE, P_("Beta updates are on"), P_("The next update brings the beta versions."));
        operation_add_button (op, P_("Go to Updates"), "suggested-action", G_CALLBACK (go_to_updates), op);
    } else if (code == 0)
        operation_finish (op, TRUE, P_("Beta updates are off"),
                          P_("Packages from beta stay installed until the release has the same or a newer version."));
    else if (!common_failure (op, code))
        operation_finish (op, FALSE, P_("Could not change the update channel"), P_("See “Details” for more information"));
    return FALSE;
}

static void
channel_start (Software *sw, gboolean beta)
{
    const char *args[] = { beta ? "channel-beta" : "channel-stable", NULL };

    operation_start (operation_new (sw->panel, beta ? P_("Turning on beta updates") : P_("Turning off beta updates"), NULL),
                     sw->panel, args, channel_done);
}

static void
beta_response (AdwAlertDialog *dialog, const char *response, Software *sw)
{
    if (g_strcmp0 (response, "enable") == 0)
        channel_start (sw, TRUE);
    else
        set_beta_row (sw, FALSE);
}

static void
beta_row_changed (AdwSwitchRow *row, GParamSpec *pspec, Software *sw)
{
    AdwDialog *dialog;

    if (sw->beta_guard)
        return;
    if (!adw_switch_row_get_active (row)) {
        channel_start (sw, FALSE);
        return;
    }
    dialog = adw_alert_dialog_new (P_("Turn On Beta Updates?"),
                                   P_("Beta updates bring new versions of the Plinius packages before they are released "
                                      "for everyone. They are tested on fewer phones and can contain errors.\n\nPackages "
                                      "of Arch Linux ARM are not affected. If you turn beta off later, the beta versions "
                                      "stay installed until the release catches up."));
    adw_alert_dialog_add_response (ADW_ALERT_DIALOG (dialog), "cancel", P_("Cancel"));
    adw_alert_dialog_add_response (ADW_ALERT_DIALOG (dialog), "enable", P_("Turn On"));
    adw_alert_dialog_set_response_appearance (ADW_ALERT_DIALOG (dialog), "enable", ADW_RESPONSE_SUGGESTED);
    adw_alert_dialog_set_close_response (ADW_ALERT_DIALOG (dialog), "cancel");
    g_signal_connect (dialog, "response", G_CALLBACK (beta_response), sw);
    adw_dialog_present (dialog, GTK_WIDGET (sw->panel));
}

static GtkWidget *
build_settings (Software *sw, GtkWidget *default_apps_group, gboolean software)
{
    GtkWidget *page = adw_preferences_page_new ();
    GtkWidget *shown = adw_preferences_group_new ();
    GtkWidget *sources = adw_preferences_group_new ();

    if (default_apps_group) {
        GtkWidget *old = gtk_widget_get_ancestor (default_apps_group, ADW_TYPE_PREFERENCES_PAGE);

        g_object_ref (default_apps_group);
        if (old)
            adw_preferences_page_remove (ADW_PREFERENCES_PAGE (old), ADW_PREFERENCES_GROUP (default_apps_group));
        adw_preferences_page_add (ADW_PREFERENCES_PAGE (page), ADW_PREFERENCES_GROUP (default_apps_group));
        g_object_unref (default_apps_group);
    }
    if (!software)
        return page;

    adw_preferences_group_set_title (ADW_PREFERENCES_GROUP (shown), P_("Also show under “Explore”"));
    /* In the order of plinius-software's menu */
    for (gsize i = 0; i < 2; i++) {
        Rating r = i == 0 ? RATING_UNKNOWN : RATING_DESKTOP;
        AdwSwitchRow *row = ADW_SWITCH_ROW (adw_switch_row_new ());

        adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_(ratings[r].extra_label));
        adw_switch_row_set_active (row, sw->shown[r]);
        g_object_set_data (G_OBJECT (row), "plinius-rating", GINT_TO_POINTER (r));
        g_signal_connect (row, "notify::active", G_CALLBACK (rating_row_changed), sw);
        adw_preferences_group_add (ADW_PREFERENCES_GROUP (shown), GTK_WIDGET (row));
        sw->rating_rows[r] = row;
    }
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (page), ADW_PREFERENCES_GROUP (shown));

    adw_preferences_group_set_title (ADW_PREFERENCES_GROUP (sources), P_("Software Sources"));
    sw->flathub_row = ADW_SWITCH_ROW (adw_switch_row_new ());
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (sw->flathub_row), "Flathub");
    adw_action_row_set_subtitle (ADW_ACTION_ROW (sw->flathub_row), P_("Flatpak apps, independent of system updates"));
    set_flathub_row (sw, flathub_enabled ());
    g_signal_connect (sw->flathub_row, "notify::active", G_CALLBACK (flathub_row_changed), sw);
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (sources), GTK_WIDGET (sw->flathub_row));
    sw->furihub_row = ADW_SWITCH_ROW (adw_switch_row_new ());
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (sw->furihub_row), "FuriHub");
    adw_action_row_set_subtitle (ADW_ACTION_ROW (sw->furihub_row), P_("Phone apps from the FuriLabs community, needs Flathub"));
    set_furihub_row (sw, furihub_enabled ());
    g_signal_connect (sw->furihub_row, "notify::active", G_CALLBACK (furihub_row_changed), sw);
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (sources), GTK_WIDGET (sw->furihub_row));
    /* Only with our repo [plinius] in pacman.conf (not in the preview, which has plinius-local) */
    if (plinius_channel () >= 0) {
        sw->beta_row = ADW_SWITCH_ROW (adw_switch_row_new ());
        adw_preferences_row_set_title (ADW_PREFERENCES_ROW (sw->beta_row), P_("Beta Updates"));
        adw_action_row_set_subtitle (ADW_ACTION_ROW (sw->beta_row), P_("New Plinius versions before their release"));
        set_beta_row (sw, plinius_channel () == 1);
        g_signal_connect (sw->beta_row, "notify::active", G_CALLBACK (beta_row_changed), sw);
        adw_preferences_group_add (ADW_PREFERENCES_GROUP (sources), GTK_WIDGET (sw->beta_row));
    }
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (page), ADW_PREFERENCES_GROUP (sources));
    return page;
}

/* --- Tabs and search --- */

static gboolean
explore_tab_visible (Software *sw)
{
    return g_strcmp0 (adw_view_stack_get_visible_child_name (sw->tabs), "explore") == 0;
}

static void
tab_changed (Software *sw)
{
    const char *tab = adw_view_stack_get_visible_child_name (sw->tabs);

    /* One search field: apps of the catalog on Explore, installed apps on Installed */
    gtk_widget_set_visible (sw->search_bar, g_strcmp0 (tab, "explore") == 0 || g_strcmp0 (tab, "installed") == 0);
    if (sw->webapps_add)
        gtk_widget_set_visible (sw->webapps_add, g_strcmp0 (tab, "webapps") == 0);
    if (g_strcmp0 (tab, "explore") == 0) {
        ccp_catalog_ensure (sw->catalog);
        refilter (sw);
    }
}

static void
search_changed (Software *sw)
{
    if (explore_tab_visible (sw))
        refilter (sw);
}

/* The panel's header bar: the top bar next to the search bar */
static AdwHeaderBar *
panel_header_bar (GtkWidget *search_bar)
{
    GtkWidget *parent = search_bar ? gtk_widget_get_parent (search_bar) : NULL;

    for (GtkWidget *c = parent ? gtk_widget_get_first_child (parent) : NULL; c; c = gtk_widget_get_next_sibling (c))
        if (ADW_IS_HEADER_BAR (c))
            return ADW_HEADER_BAR (c);
    return NULL;
}

/**
 * cc_plinius_software_setup_apps_panel:
 * @panel: the Apps panel
 * @installed_page: the panel's content so far (the list of apps), becomes the "Installed" tab
 * @default_apps_group: the group with the Default Apps row, moves to the "Settings" tab
 * @search_entry: the panel's search field, searches the catalog on "Explore"
 *
 * Gives the Apps panel the tabs Explore, Installed, Web Apps and Settings. Without
 * plinius-software only Installed, Web Apps and Settings (with Default Apps) are left.
 */
void
cc_plinius_software_setup_apps_panel (CcPanel *panel, GtkWidget *installed_page, GtkWidget *default_apps_group,
                                      GtkEditable *search_entry)
{
    AdwToolbarView *toolbar = ADW_TOOLBAR_VIEW (gtk_widget_get_ancestor (installed_page, ADW_TYPE_TOOLBAR_VIEW));
    gboolean software = g_file_test (HELPER, G_FILE_TEST_IS_EXECUTABLE);
    Software *sw = g_new0 (Software, 1);
    GtkWidget *switcher, *webapps;
    AdwViewStackPage *explore;
    AdwHeaderBar *header;

    sw->panel = panel;
    sw->catalog = ccp_catalog_get ();
    sw->search = search_entry;
    sw->search_bar = gtk_widget_get_ancestor (GTK_WIDGET (search_entry), GTK_TYPE_SEARCH_BAR);
    for (gsize r = 0; r < N_RATINGS; r++)
        sw->shown[r] = rating_shown (r);
    g_object_set_data_full (G_OBJECT (panel), "plinius-software", sw, (GDestroyNotify) software_free);

    sw->tabs = ADW_VIEW_STACK (adw_view_stack_new ());
    explore = adw_view_stack_add_titled_with_icon (sw->tabs, build_explore (sw), "explore", P_("Explore"),
                                                   "system-search-symbolic");
    adw_view_stack_page_set_visible (explore, software);
    g_object_ref (installed_page);
    adw_toolbar_view_set_content (toolbar, NULL);
    adw_view_stack_add_titled_with_icon (sw->tabs, installed_page, "installed", P_("Installed"), "view-list-symbolic");
    g_object_unref (installed_page);
    webapps = cc_plinius_webapps_new_tab (panel);
    adw_view_stack_add_titled_with_icon (sw->tabs, webapps, "webapps", P_("Web Apps"), "web-browser-symbolic");
    header = panel_header_bar (sw->search_bar);
    if (header) {
        sw->webapps_add = cc_plinius_webapps_new_add_button (webapps);
        adw_header_bar_pack_end (header, sw->webapps_add);
    }
    adw_view_stack_add_titled_with_icon (sw->tabs, build_settings (sw, default_apps_group, software), "settings",
                                         P_("Settings"), "emblem-system-symbolic");
    adw_toolbar_view_set_content (toolbar, GTK_WIDGET (sw->tabs));

    switcher = adw_view_switcher_bar_new ();
    adw_view_switcher_bar_set_stack (ADW_VIEW_SWITCHER_BAR (switcher), sw->tabs);
    adw_view_switcher_bar_set_reveal (ADW_VIEW_SWITCHER_BAR (switcher), TRUE);
    adw_toolbar_view_add_bottom_bar (toolbar, switcher);

    sw->catalog_handler = g_signal_connect_swapped (sw->catalog, "changed", G_CALLBACK (update_explore), sw);
    g_signal_connect_swapped (sw->tabs, "notify::visible-child-name", G_CALLBACK (tab_changed), sw);
    g_signal_connect_swapped (search_entry, "search-changed", G_CALLBACK (search_changed), sw);
    adw_view_stack_set_visible_child_name (sw->tabs, software ? "explore" : "installed");
    tab_changed (sw);
}

/**
 * cc_plinius_software_explore_visible:
 * @panel: the Apps panel
 *
 * Returns: whether the "Explore" tab is shown; the search field then belongs to it
 */
gboolean
cc_plinius_software_explore_visible (CcPanel *panel)
{
    Software *sw = software_of (panel);

    return sw && explore_tab_visible (sw);
}

/* --- An installed app's page: its package, Remove --- */

typedef struct {
    Source source;
    char *pkg;
    char *version;
    char *flatpak_id;
    char *remote;
    GStrv removal;
} AppPackage;

static void
app_package_free (AppPackage *p)
{
    g_free (p->pkg);
    g_free (p->version);
    g_free (p->flatpak_id);
    g_free (p->remote);
    g_strfreev (p->removal);
    g_free (p);
}

/* Runs in a thread: package (pacman -Qo) or Flatpak of a desktop file, version, what removing takes */
static void
app_package_thread (GTask *task, gpointer source, gpointer data, GCancellable *cancellable)
{
    const char *path = data;
    AppPackage *p = g_new0 (AppPackage, 1);
    g_autofree char *base = g_path_get_basename (path);

    if (strstr (path, "/flatpak/exports/")) {
        g_autofree char *id = g_strndup (base, strlen (base) - strlen (".desktop"));
        const char *argv[] = { "flatpak", "info", "--system", id, NULL };
        const char *origin[] = { "flatpak", "info", "--system", "--show-origin", id, NULL };
        g_auto(GStrv) lines = run_lines (argv);
        g_auto(GStrv) remote = run_lines (origin);
        const char *one[] = { id, NULL };

        p->source = SOURCE_FLATPAK;
        p->flatpak_id = g_strdup (id);
        p->remote = g_strdup (remote[0]);
        for (char **l = lines; *l; l++) {
            g_autofree char *line = g_strstrip (g_strdup (*l));

            /* "Version: 1.2" (the field name in the system language, so only the first) */
            if (p->version == NULL && g_str_has_prefix (line, "Version:"))
                p->version = g_strstrip (g_strdup (line + strlen ("Version:")));
        }
        p->removal = g_strdupv ((char **) one);
    } else {
        const char *qo[] = { "pacman", "-Qqo", path, NULL };
        g_auto(GStrv) owner = run_lines (qo);
        g_autoptr(GHashTable) required = NULL;

        if (owner[0] == NULL) {
            app_package_free (p);
            g_task_return_pointer (task, NULL, NULL);
            return;
        }
        p->source = SOURCE_PACMAN;
        p->pkg = g_strdup (owner[0]);
        {
            const char *q[] = { "pacman", "-Q", p->pkg, NULL };
            g_auto(GStrv) version = run_lines (q);
            g_auto(GStrv) parts = version[0] ? g_strsplit (version[0], " ", 2) : NULL;

            p->version = g_strdup (parts && parts[1] ? parts[1] : "");
        }
        /* Who needs the package, and who needs those that need it */
        required = g_hash_table_new_full (g_str_hash, g_str_equal, g_free, (GDestroyNotify) g_ptr_array_unref);
        {
            const char *qi[] = { "env", "LC_ALL=C", "pacman", "-Qi", p->pkg, NULL };
            GPtrArray *by;

            read_package_info (qi, required, NULL);
            by = g_hash_table_lookup (required, p->pkg);
            for (guint i = 0; by && i < by->len; i++) {
                const char *other[] = { "env", "LC_ALL=C", "pacman", "-Qi", g_ptr_array_index (by, i), NULL };

                read_package_info (other, required, NULL);
            }
        }
        p->removal = removal (p->pkg, required);
    }
    g_task_return_pointer (task, p, (GDestroyNotify) app_package_free);
}

static void
app_remove_clicked (Software *sw)
{
    if (sw->app_removal)
        confirm_remove (sw->panel, sw->app_name, sw->app_flatpak, sw->app_removal);
}

static void
app_package_ready (GObject *source, GAsyncResult *result, gpointer data)
{
    Software *sw = data;
    g_autoptr(GError) error = NULL;
    AppPackage *p = g_task_propagate_pointer (G_TASK (result), &error);

    if (g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
        return;
    if (p == NULL) {
        /* Not from a package (e.g. ~/.local/share/applications) */
        gtk_widget_set_visible (sw->package_group, FALSE);
        return;
    }
    if (p->source == SOURCE_FLATPAK) {
        g_autofree char *text =
            g_strstrip (g_strdup_printf ("%s %s", remote_title (p->remote), p->version ? p->version : ""));

        adw_preferences_row_set_title (ADW_PREFERENCES_ROW (sw->package_row), P_("Source"));
        adw_action_row_set_subtitle (sw->package_row, text);
    } else {
        g_autofree char *text = g_strdup_printf ("%s %s", p->pkg, p->version);

        adw_preferences_row_set_title (ADW_PREFERENCES_ROW (sw->package_row), P_("Package"));
        adw_action_row_set_subtitle (sw->package_row, text);
    }
    gtk_widget_set_visible (sw->package_group, TRUE);
    g_strfreev (sw->app_removal);
    sw->app_removal = g_steal_pointer (&p->removal);
    sw->app_flatpak = p->source == SOURCE_FLATPAK;
    /* What plinius-apps (or another package) needs cannot be removed */
    gtk_widget_set_visible (sw->remove_button, sw->app_removal != NULL);
    gtk_widget_set_visible (sw->system_hint, sw->app_removal == NULL);
    app_package_free (p);
}

/**
 * cc_plinius_software_update_app_page:
 * @panel: the Apps panel
 * @info: the app the page is about
 * @launch_button: the page's Open button; Remove goes next to it
 * @page_group: a group on the page; the package group goes onto the same page
 *
 * Adds what plinius-software showed for an installed app: package and version, Remove, or
 * that it is part of the system.
 */
void
cc_plinius_software_update_app_page (CcPanel *panel, GAppInfo *info, GtkWidget *launch_button, GtkWidget *page_group)
{
    Software *sw = software_of (panel);
    const char *path = G_IS_DESKTOP_APP_INFO (info) ? g_desktop_app_info_get_filename (G_DESKTOP_APP_INFO (info)) : NULL;
    g_autoptr(GTask) task = NULL;

    if (sw == NULL)
        return;
    /* An app's page belongs to "Installed", also when a link opened it: Back leads there */
    adw_view_stack_set_visible_child_name (sw->tabs, "installed");
    if (sw->remove_button == NULL) {
        GtkWidget *wrap = gtk_widget_get_parent (launch_button);
        GtkWidget *head = gtk_widget_get_parent (wrap);
        GtkWidget *page = gtk_widget_get_ancestor (page_group, ADW_TYPE_PREFERENCES_PAGE);

        sw->remove_button = gtk_button_new_with_label (P_("Remove"));
        gtk_widget_add_css_class (sw->remove_button, "pill");
        gtk_widget_add_css_class (sw->remove_button, "destructive-action");
        gtk_button_set_can_shrink (GTK_BUTTON (sw->remove_button), TRUE);
        g_signal_connect_swapped (sw->remove_button, "clicked", G_CALLBACK (app_remove_clicked), sw);
        if (ADW_IS_WRAP_BOX (wrap))
            adw_wrap_box_append (ADW_WRAP_BOX (wrap), sw->remove_button);
        sw->system_hint = wrapped_label (P_("Part of the system (Phosh, phone) and cannot be removed."), "dim-label");
        gtk_widget_add_css_class (sw->system_hint, "caption");
        gtk_widget_set_margin_bottom (sw->system_hint, 12);
        if (GTK_IS_BOX (head))
            gtk_box_append (GTK_BOX (head), sw->system_hint);

        sw->package_group = adw_preferences_group_new ();
        adw_preferences_group_set_title (ADW_PREFERENCES_GROUP (sw->package_group), P_("Details"));
        sw->package_row = plain_row (P_("Package"), NULL);
        gtk_widget_add_css_class (GTK_WIDGET (sw->package_row), "property");
        adw_preferences_group_add (ADW_PREFERENCES_GROUP (sw->package_group), GTK_WIDGET (sw->package_row));
        if (page)
            adw_preferences_page_add (ADW_PREFERENCES_PAGE (page), ADW_PREFERENCES_GROUP (sw->package_group));
    }

    g_cancellable_cancel (sw->app_cancellable);
    g_clear_object (&sw->app_cancellable);
    g_clear_pointer (&sw->app_removal, g_strfreev);
    g_free (sw->app_name);
    sw->app_name = g_strdup (g_app_info_get_display_name (info));
    gtk_widget_set_visible (sw->remove_button, FALSE);
    gtk_widget_set_visible (sw->system_hint, FALSE);
    gtk_widget_set_visible (sw->package_group, FALSE);
    /* Settings does not remove itself */
    if (path == NULL || g_strcmp0 (g_app_info_get_id (info), APPLICATION_ID ".desktop") == 0)
        return;

    sw->app_cancellable = g_cancellable_new ();
    task = g_task_new (NULL, sw->app_cancellable, app_package_ready, sw);
    g_task_set_task_data (task, g_strdup (path), g_free);
    g_task_run_in_thread (task, app_package_thread);
}

/* ----- The Updates panel ----- */

#define CC_TYPE_UPDATES_PANEL (cc_updates_panel_get_type ())
G_DECLARE_FINAL_TYPE (CcUpdatesPanel, cc_updates_panel, CC, UPDATES_PANEL, CcPanel)

struct _CcUpdatesPanel {
    CcPanel parent_instance;

    GtkWidget *icon;
    GtkWidget *title;
    GtkWidget *description;
    /* Below the title while checking for updates */
    GtkWidget *spinner;
    GtkWidget *upgrade_button;
    GtkWidget *check_button;
    /* Below the header bar: what plinius-software found wrong with the update server's stamp
     * (an old, foreign or unsigned state: updates may be held back) */
    GtkWidget *repo_warning;
    AdwPreferencesGroup *list;
    GPtrArray *rows;
    GCancellable *cancellable;
    gboolean checking;
};

CC_PANEL_REGISTER (CcUpdatesPanel, cc_updates_panel)

static void
updates_set_status (CcUpdatesPanel *self, const char *title, const char *description)
{
    gtk_label_set_text (GTK_LABEL (self->title), title);
    gtk_label_set_text (GTK_LABEL (self->description), description ? description : "");
    gtk_widget_set_visible (self->description, description && *description);
}

static char *
with_placeholder (const char *text, const char *placeholder, const char *value)
{
    g_autoptr(GString) s = g_string_new (text);

    g_string_replace (s, placeholder, value, 0);
    return g_string_free (g_steal_pointer (&s), FALSE);
}

static void
updates_clear_list (CcUpdatesPanel *self)
{
    for (guint i = 0; i < self->rows->len; i++)
        adw_preferences_group_remove (self->list, g_ptr_array_index (self->rows, i));
    g_ptr_array_set_size (self->rows, 0);
}

static void
updates_add_row (CcUpdatesPanel *self, const char *title, const char *subtitle)
{
    AdwActionRow *row = plain_row (title, subtitle);

    adw_preferences_group_add (self->list, GTK_WIDGET (row));
    g_ptr_array_add (self->rows, row);
}

/* The result of plinius-software --updates-json. "Update Now" stays available when the check
 * failed: the upgrade fetches fresh databases itself. Packages waiting for approval
 * (plinius-freigabe) are left out there already: updates the user cannot get are not shown,
 * but "held" says that they wait (with its own title and text, localized by plinius-software
 * 0.12-23 and later) instead of "up to date". */
static void
updates_show (CcUpdatesPanel *self, JsonObject *result)
{
    gint64 code = result ? json_object_get_int_member_with_default (result, "code", 1) : 1;
    JsonArray *packages = result && json_object_has_member (result, "packages")
                              ? json_object_get_array_member (result, "packages")
                              : NULL;
    JsonArray *flatpaks = result && json_object_has_member (result, "flatpaks")
                              ? json_object_get_array_member (result, "flatpaks")
                              : NULL;
    const char *last = result && json_object_has_member (result, "last_check") &&
                               !json_object_get_null_member (result, "last_check")
                           ? json_object_get_string_member (result, "last_check")
                           : NULL;
    JsonNode *held_node = result ? json_object_get_member (result, "held") : NULL;
    JsonObject *held = held_node && JSON_NODE_HOLDS_OBJECT (held_node) ? json_node_get_object (held_node) : NULL;
    const char *held_title = held ? json_object_get_string_member_with_default (held, "title", NULL) : NULL;
    const char *held_text = held ? json_object_get_string_member_with_default (held, "text", NULL) : NULL;
    guint n = 0;
    g_autoptr(GString) description = g_string_new (NULL);
    gboolean ok = code == 0 || code == 2;

    /* Only a check that reached the server says anything about its stamp: after a failed one the
     * banner keeps what the last good one found */
    if (ok) {
        const char *warning = json_object_has_member (result, "repo_warning") &&
                                      !json_object_get_null_member (result, "repo_warning")
                                  ? json_object_get_string_member (result, "repo_warning")
                                  : NULL;

        if (warning && *warning)
            adw_banner_set_title (ADW_BANNER (self->repo_warning), warning);
        adw_banner_set_revealed (ADW_BANNER (self->repo_warning), warning && *warning);
    }

    self->checking = FALSE;
    gtk_widget_set_visible (self->spinner, FALSE);
    gtk_widget_set_sensitive (self->check_button, TRUE);
    updates_clear_list (self);

    for (guint i = 0; packages && i < json_array_get_length (packages); i++) {
        JsonArray *p = json_array_get_array_element (packages, i);
        g_autofree char *subtitle = NULL;

        if (p == NULL || json_array_get_length (p) < 3)
            continue;
        subtitle = g_strdup_printf ("%s → %s", json_array_get_string_element (p, 1),
                                    json_array_get_string_element (p, 2));
        updates_add_row (self, json_array_get_string_element (p, 0), subtitle);
        n++;
    }
    for (guint i = 0; flatpaks && i < json_array_get_length (flatpaks); i++) {
        JsonArray *f = json_array_get_array_element (flatpaks, i);
        g_autofree char *subtitle = NULL;

        if (f == NULL || json_array_get_length (f) < 2)
            continue;
        /* [name, version, remote]; plinius-software before 0.12-16 sends no remote */
        subtitle = g_strstrip (g_strdup_printf (
            "%s %s", remote_title (json_array_get_length (f) >= 3 ? json_array_get_string_element (f, 2) : "flathub"),
            json_array_get_string_element (f, 1)));
        updates_add_row (self, json_array_get_string_element (f, 0), subtitle);
        n++;
    }

    if (!ok && n == 0) {
        g_string_append (description, P_("No connection to the server? You can still update."));
        if (last) {
            g_autofree char *text = with_placeholder (P_("Last successful check: {when}"), "{when}", last);

            g_string_append_printf (description, "\n%s", text);
        }
        updates_set_status (self, P_("Check failed"), description->str);
    } else if (n) {
        g_autofree char *count = g_strdup_printf ("%u", n);
        g_autofree char *title = with_placeholder (NP_("{n} update available", "{n} updates available", n), "{n}", count);

        g_string_append (description, held_title && *held_title
                                          ? held_title
                                          : P_("The whole system is always updated at once."));
        if (last && ok) {
            g_autofree char *text = with_placeholder (P_("Checked: {when}"), "{when}", last);

            g_string_append_printf (description, "\n%s", text);
        }
        updates_set_status (self, title, description->str);
    } else if (held_title && *held_title) {
        /* Nothing to install, but system updates wait for approval: not "up to date" */
        g_string_append (description, held_text ? held_text : "");
        if (last) {
            g_autofree char *text = with_placeholder (P_("Last checked: {when}"), "{when}", last);

            g_string_append_printf (description, "%s%s", description->len ? "\n" : "", text);
        }
        updates_set_status (self, held_title, description->str);
    } else {
        g_autofree char *text = last ? with_placeholder (P_("Last checked: {when}"), "{when}", last) : NULL;

        updates_set_status (self, P_("The system is up to date"), text);
    }
    gtk_widget_set_visible (GTK_WIDGET (self->list), n > 0);
    gtk_widget_set_visible (self->upgrade_button, n > 0 || !ok);
}

static void
updates_checked (GObject *source, GAsyncResult *result, gpointer data)
{
    g_autoptr(GError) error = NULL;
    g_autofree char *out = NULL;
    g_autoptr(JsonParser) parser = NULL;
    CcUpdatesPanel *self;
    JsonNode *root;

    if (!g_subprocess_communicate_utf8_finish (G_SUBPROCESS (source), result, &out, NULL, &error)) {
        if (g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
            return;
    }
    self = CC_UPDATES_PANEL (data);
    parser = json_parser_new ();
    if (out && json_parser_load_from_data (parser, out, -1, NULL) && (root = json_parser_get_root (parser)) &&
        JSON_NODE_HOLDS_OBJECT (root))
        updates_show (self, json_node_get_object (root));
    else
        updates_show (self, NULL);
}

/* Check for updates in the background */
static void
updates_check (CcUpdatesPanel *self)
{
    const char *argv[] = { SOFTWARE, "--updates-json", NULL };
    g_autoptr(GSubprocess) proc = NULL;

    if (self->checking)
        return;
    self->checking = TRUE;
    gtk_widget_set_sensitive (self->check_button, FALSE);
    gtk_widget_set_visible (self->spinner, TRUE);
    updates_set_status (self, P_("Checking for updates…"), NULL);
    proc = g_subprocess_newv (argv, G_SUBPROCESS_FLAGS_STDOUT_PIPE | G_SUBPROCESS_FLAGS_STDERR_SILENCE, NULL);
    if (proc == NULL) {
        updates_show (self, NULL);
        return;
    }
    g_subprocess_communicate_utf8_async (proc, NULL, self->cancellable, updates_checked, self);
}

static void
reboot_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(GError) error = NULL;
    g_autoptr(GVariant) reply = g_dbus_connection_call_finish (G_DBUS_CONNECTION (source), result, &error);

    if (!reply)
        g_warning ("Restarting the phone failed: %s", error->message);
}

static void
reboot_now (void)
{
    /* interactive: polkit may ask */
    g_autoptr(GDBusConnection) bus = g_bus_get_sync (G_BUS_TYPE_SYSTEM, NULL, NULL);

    /* With a callback the call holds the connection until logind answers: without one the
     * message is only queued, and a connection nothing else holds goes before sending it */
    if (bus)
        g_dbus_connection_call (bus, "org.freedesktop.login1", "/org/freedesktop/login1", "org.freedesktop.login1.Manager",
                                "Reboot", g_variant_new ("(b)", TRUE), NULL, G_DBUS_CALL_FLAGS_NONE, -1, NULL,
                                reboot_cb, NULL);
}

/*
 * After the restart: the page no longer offers it. Once Phosh is back, a second tap would only
 * lock the phone again, and the page must not make anyone think the update still waits. The
 * level and the shell's start (restart_watch) stay on the page while the offer stands.
 */
#define SHELL_UNIT "mobi.phosh.Shell.service"

static void restart_apps_clicked (Operation *op);

static void
restart_settled (Operation *op, gboolean by_us)
{
    const char *level = g_object_get_data (G_OBJECT (op->page), "plinius-restart-level");
    const char *apps = g_object_get_data (G_OBJECT (op->page), "plinius-restart-apps");
    g_autofree char *title = g_strdup (gtk_label_get_text (GTK_LABEL (op->status)));
    GtkWidget *child;

    if (level == NULL)
        return;
    while ((child = gtk_widget_get_first_child (op->buttons)))
        gtk_box_remove (GTK_BOX (op->buttons), child);
    gtk_widget_set_sensitive (op->buttons, TRUE);

    /* Restart UI in the power menu restarts only Phosh: the apps still wait */
    if (g_str_equal (level, "ui") && !by_us && apps) {
        g_autofree char *text = with_placeholder (P_("Phosh has been restarted. Restart {apps} to use their new "
                                                     "versions. Unsaved changes in them are lost."), "{apps}", apps);

        g_object_set_data (G_OBJECT (op->page), "plinius-restart-level", (gpointer) "apps");
        operation_finish (op, TRUE, title, text);
        operation_add_button (op, P_("Restart Apps"), "suggested-action", G_CALLBACK (restart_apps_clicked), op);
        return;
    }
    g_object_set_data (G_OBJECT (op->page), "plinius-restart-level", NULL);
    operation_finish (op, TRUE, title,
                      g_str_equal (level, "ui")
                          ? P_("Phosh has been restarted. Everything that is running now uses the new versions.")
                          : P_("The apps have been restarted. Everything that is running now uses the new "
                               "versions."));
    operation_add_done (op);
}

static void
restart_applied (GObject *source, GAsyncResult *result, gpointer data)
{
    g_autoptr(AdwNavigationPage) page = data;
    Operation *op = g_object_get_data (G_OBJECT (page), "plinius-operation");
    g_autoptr(GError) error = NULL;

    if (op == NULL)
        return;
    g_object_set_data (G_OBJECT (page), "plinius-restart-running", NULL);
    if (!g_subprocess_wait_check_finish (G_SUBPROCESS (source), result, &error)) {
        /* Nothing is claimed: the offer stays and can be tried again */
        g_warning ("The restart failed: %s", error->message);
        gtk_widget_set_sensitive (op->buttons, TRUE);
        return;
    }
    restart_settled (op, TRUE);
}

/* Run "plinius-restart apply LEVEL" in a scope of its own: it may close Settings too and opens
 * it again. Settings that stays open says when it is done. */
static void
restart_apply (AdwNavigationPage *page, const char *level)
{
    const char *argv[] = { "systemd-run", "--user", "--scope", "--collect", "--quiet", "--",
                           RESTART_HELPER, "apply", level, RESTART_RESULT, NULL };
    Operation *op = g_object_get_data (G_OBJECT (page), "plinius-operation");
    g_autoptr(GError) error = NULL;
    g_autoptr(GSubprocess) proc = g_subprocess_newv (argv, G_SUBPROCESS_FLAGS_NONE, &error);

    if (proc == NULL) {
        g_warning ("Could not start the restart: %s", error->message);
        return;
    }
    if (op == NULL)
        return;
    /* No second tap while it runs */
    gtk_widget_set_sensitive (op->buttons, FALSE);
    g_object_set_data (G_OBJECT (page), "plinius-restart-running", GINT_TO_POINTER (TRUE));
    g_subprocess_wait_check_async (proc, NULL, restart_applied, g_object_ref (page));
}

/* The answer to the confirmation: its response ID is the restart level, "cancel" does nothing */
static void
restart_response (AdwAlertDialog *dialog, const char *response, AdwNavigationPage *page)
{
    if (g_str_equal (response, "reboot"))
        reboot_now ();
    else if (g_str_equal (response, "apps") || g_str_equal (response, "ui"))
        restart_apply (page, response);
}

/* When the shell's process started (systemd's monotonic clock), "" if it does not run */
static void
shell_start_read (GObject *source, GAsyncResult *result, gpointer data)
{
    g_autoptr(AdwNavigationPage) page = data;
    g_autofree char *out = NULL;
    const char *before = g_object_get_data (G_OBJECT (page), "plinius-restart-shell-start");
    Operation *op;

    if (!g_subprocess_communicate_utf8_finish (G_SUBPROCESS (source), result, &out, NULL, NULL) || out == NULL)
        return;
    g_strstrip (out);
    if (before == NULL) {
        g_object_set_data_full (G_OBJECT (page), "plinius-restart-shell-start", g_steal_pointer (&out), g_free);
        return;
    }
    op = g_object_get_data (G_OBJECT (page), "plinius-operation");
    /* Started again since the offer (and running): Phosh has been restarted elsewhere */
    if (op && *out && !g_str_equal (out, "0") && !g_str_equal (out, before) &&
        !g_object_get_data (G_OBJECT (page), "plinius-restart-running") &&
        g_strcmp0 (g_object_get_data (G_OBJECT (page), "plinius-restart-level"), "ui") == 0)
        restart_settled (op, FALSE);
}

static void
shell_start_query (AdwNavigationPage *page)
{
    const char *argv[] = { "systemctl", "--user", "show", "--property=ExecMainStartTimestampMonotonic", "--value",
                           SHELL_UNIT, NULL };
    g_autoptr(GSubprocess) proc = g_subprocess_newv (argv, G_SUBPROCESS_FLAGS_STDOUT_PIPE |
                                                     G_SUBPROCESS_FLAGS_STDERR_SILENCE, NULL);

    if (proc)
        g_subprocess_communicate_utf8_async (proc, NULL, NULL, shell_start_read, g_object_ref (page));
}

/* Back in Settings (after unlocking the phone): was Phosh restarted in the meantime? */
static void
restart_window_active (GtkWindow *window, GParamSpec *pspec, AdwNavigationPage *page)
{
    /* Only while the page is still in the window */
    if (gtk_window_is_active (window) && gtk_widget_get_root (GTK_WIDGET (page)) == GTK_ROOT (window) &&
        g_object_get_data (G_OBJECT (page), "plinius-restart-shell-start") &&
        g_strcmp0 (g_object_get_data (G_OBJECT (page), "plinius-restart-level"), "ui") == 0)
        shell_start_query (page);
}

/*
 * While "Restart Phosh" is offered, notice a restart of Phosh from the power menu (Restart UI).
 * Only if Phosh is all that waits besides the apps: other parts of the session it leaves as
 * they were.
 */
static void
restart_watch (Operation *op, gboolean shell_only)
{
    GtkRoot *root = gtk_widget_get_root (GTK_WIDGET (op->page));

    if (!shell_only || !GTK_IS_WINDOW (root))
        return;
    shell_start_query (op->page);
    g_signal_connect_object (root, "notify::is-active", G_CALLBACK (restart_window_active), op->page, 0);
}

/* Whether the shell is the only part of this user's session that waits for a restart */
static gboolean
restart_shell_only (JsonObject *result)
{
    JsonArray *units = json_object_has_member (result, "units") ? json_object_get_array_member (result, "units") : NULL;
    gboolean shell = FALSE;

    for (guint i = 0; units && i < json_array_get_length (units); i++) {
        JsonObject *unit = json_array_get_object_element (units, i);
        const char *name = unit ? json_object_get_string_member_with_default (unit, "unit", "") : "";

        if (unit == NULL || json_object_get_int_member_with_default (unit, "uid", -1) != (gint64) getuid ())
            continue;
        if (!g_str_equal (name, SHELL_UNIT))
            return FALSE;
        shell = TRUE;
    }
    return shell;
}

/*
 * Ask before a restart after an update: a tap on the button alone must not close apps, lock
 * the phone or restart it. LEVEL is "apps", "ui" or "reboot"; the names of the apps it also
 * closes were left on the page by upgrade_offer_restart().
 */
static void
restart_confirm (Operation *op, const char *level)
{
    const char *apps = g_object_get_data (G_OBJECT (op->page), "plinius-restart-apps");
    const char *heading, *label;
    g_autoptr(GString) body = g_string_new (NULL);
    AdwDialog *dialog;

    if (g_str_equal (level, "apps")) {
        g_autofree char *text = with_placeholder (P_("{apps} close and open again. Unsaved changes in them are "
                                                     "lost."), "{apps}", apps ? apps : "");

        heading = P_("Restart Apps?");
        label = P_("Restart Apps");
        g_string_append (body, text);
    } else if (g_str_equal (level, "ui")) {
        heading = P_("Restart Phosh?");
        label = P_("Restart Phosh");
        g_string_append (body, P_("Phosh restarts and the phone locks. Open apps stay open."));
        if (apps) {
            g_autofree char *also = with_placeholder (P_("{apps} also restart. Unsaved changes in them are "
                                                         "lost."), "{apps}", apps);

            g_string_append_printf (body, " %s", also);
        }
    } else {
        heading = P_("Restart Phone?");
        label = P_("Restart Phone");
        g_string_append (body, P_("All apps close. Unsaved changes in them are lost."));
    }

    dialog = adw_alert_dialog_new (heading, body->str);
    adw_alert_dialog_add_response (ADW_ALERT_DIALOG (dialog), "cancel", P_("Cancel"));
    adw_alert_dialog_add_response (ADW_ALERT_DIALOG (dialog), level, label);
    adw_alert_dialog_set_response_appearance (ADW_ALERT_DIALOG (dialog), level, ADW_RESPONSE_SUGGESTED);
    /* Enter or a tap beside the dialog must not restart anything */
    adw_alert_dialog_set_default_response (ADW_ALERT_DIALOG (dialog), "cancel");
    adw_alert_dialog_set_close_response (ADW_ALERT_DIALOG (dialog), "cancel");
    g_signal_connect_data (dialog, "response", G_CALLBACK (restart_response), g_object_ref (op->page),
                           (GClosureNotify) (void (*) (void)) g_object_unref, 0);
    adw_dialog_present (dialog, GTK_WIDGET (op->page));
}

static void
restart_apps_clicked (Operation *op)
{
    restart_confirm (op, "apps");
}

static void
restart_ui_clicked (Operation *op)
{
    restart_confirm (op, "ui");
}

static void
reboot_clicked (Operation *op)
{
    restart_confirm (op, "reboot");
}

/* The names of this user's apps that a restart closes and opens again, comma-separated */
static char *
restart_app_names (JsonObject *result)
{
    JsonArray *apps = json_object_has_member (result, "apps") ? json_object_get_array_member (result, "apps") : NULL;
    g_autoptr(GPtrArray) names = g_ptr_array_new_with_free_func (g_free);

    for (guint i = 0; apps && i < json_array_get_length (apps); i++) {
        JsonObject *app = json_array_get_object_element (apps, i);
        const char *id = app ? json_object_get_string_member_with_default (app, "id", NULL) : NULL;
        g_autofree char *desktop_id = NULL;
        g_autoptr(GDesktopAppInfo) info = NULL;
        g_autofree char *name = NULL;

        if (id == NULL || json_object_get_int_member_with_default (app, "uid", -1) != (gint64) getuid ())
            continue;
        desktop_id = g_strconcat (id, ".desktop", NULL);
        info = g_desktop_app_info_new (desktop_id);
        name = g_strdup (info ? g_app_info_get_display_name (G_APP_INFO (info)) : id);
        if (!g_ptr_array_find_with_equal_func (names, name, g_str_equal, NULL))
            g_ptr_array_add (names, g_steal_pointer (&name));
    }
    if (names->len == 0)
        return NULL;
    g_ptr_array_add (names, NULL);
    return g_strjoinv (", ", (char **) names->pdata);
}

/*
 * After an update: offer the restart it needs. plinius-pkg wrote which running programs still
 * use replaced files: none, apps (close and open them again), ui (the shell, the keyboard and
 * the session's services, in place) or reboot. Without that answer: a reboot, as before.
 * Returns TRUE if it added its own button.
 */
static gboolean
upgrade_offer_restart (Operation *op, const char *title)
{
    g_autoptr(JsonParser) parser = json_parser_new ();
    JsonNode *root;
    const char *level = "reboot";
    g_autofree char *apps = NULL;
    gboolean shell_only = FALSE;

    if (json_parser_load_from_file (parser, RESTART_RESULT, NULL) && (root = json_parser_get_root (parser)) &&
        JSON_NODE_HOLDS_OBJECT (root)) {
        level = json_object_get_string_member_with_default (json_node_get_object (root), "level", "reboot");
        apps = restart_app_names (json_node_get_object (root));
        shell_only = restart_shell_only (json_node_get_object (root));
    }

    /* For the confirmation of the restart (restart_confirm) */
    g_object_set_data_full (G_OBJECT (op->page), "plinius-restart-apps", g_strdup (apps), g_free);
    if (g_str_equal (level, "none") || (g_str_equal (level, "apps") && apps == NULL)) {
        operation_finish (op, TRUE, title, P_("Everything that is running already uses the new versions."));
        return FALSE;
    }
    if (g_str_equal (level, "apps")) {
        g_autofree char *text = with_placeholder (P_("Restart {apps} to use the new versions. Unsaved changes "
                                                     "in them are lost."), "{apps}", apps);

        operation_finish (op, TRUE, title, text);
        operation_add_button (op, P_("Restart Apps"), "suggested-action", G_CALLBACK (restart_apps_clicked), op);
        g_object_set_data (G_OBJECT (op->page), "plinius-restart-level", (gpointer) "apps");
        return TRUE;
    }
    if (g_str_equal (level, "ui")) {
        g_autoptr(GString) text = g_string_new (P_("Restart Phosh to use the new versions. The phone locks; "
                                                   "open apps stay open."));

        if (apps) {
            g_autofree char *also = with_placeholder (P_("{apps} also restart. Unsaved changes in them are "
                                                         "lost."), "{apps}", apps);

            g_string_append_printf (text, " %s", also);
        }
        operation_finish (op, TRUE, title, text->str);
        operation_add_button (op, P_("Restart Phosh"), "suggested-action", G_CALLBACK (restart_ui_clicked), op);
        g_object_set_data (G_OBJECT (op->page), "plinius-restart-level", (gpointer) "ui");
        restart_watch (op, shell_only);
        return TRUE;
    }
    /* Only offer the restart; back is at the top left */
    operation_finish (op, TRUE, title, P_("Restart the phone to run the new versions."));
    operation_add_button (op, P_("Restart Phone"), "suggested-action", G_CALLBACK (reboot_clicked), op);
    return TRUE;
}

static gboolean
upgrade_done (Operation *op, gint code)
{
    g_autoptr(CcPanel) panel = g_weak_ref_get (&op->home);

    if (panel && CC_IS_UPDATES_PANEL (panel))
        updates_check (CC_UPDATES_PANEL (panel));
    if (code == 0)
        return upgrade_offer_restart (op, P_("The system has been updated"));
    if (code == EXIT_NOTHING_TO_DO)
        operation_finish (op, TRUE, P_("The system is already up to date"), NULL);
    else if (code == EXIT_ONLY_FLATPAK)
        return upgrade_offer_restart (op, P_("The Flatpak apps have been updated"));
    else if (code == EXIT_LOW_BATTERY)
        operation_finish (op, FALSE, P_("Battery too low"),
                          P_("Connect the charger or charge the phone to at least 10 %, then update. An update "
                             "interrupted by an empty battery can leave the system unable to start."));
    else if (code == EXIT_REPO_UNVERIFIED)
        operation_finish (op, FALSE, P_("Update stopped"),
                          P_("The update server's package list does not match its signed state. Nothing was "
                             "installed. Try again later; if it stays like this, the server may not be "
                             "trustworthy."));
    else if (!common_failure (op, code))
        operation_finish (op, FALSE, P_("Update failed"), P_("See “Details” for more information"));
    return FALSE;
}

/* Update the whole system (pacman -Syu, then Flatpak apps if Flathub is on) */
static void
upgrade_clicked (CcUpdatesPanel *self)
{
    const Step *const with_flatpak[] = { &STEP_SYNC, &STEP_DOWNLOAD, &STEP_CHECK, &STEP_INSTALL, &STEP_HOOKS,
                                         &STEP_FLATPAK_UPDATE, NULL };
    const char *args[] = { "upgrade", NULL };
    Operation *op;

    op = operation_new (CC_PANEL (self), P_("Updating the system"), with_flatpak);
    /* Without Flathub there is no Flatpak step */
    if (!flathub_enabled ())
        g_ptr_array_remove_index (op->progress.steps, op->progress.steps->len - 1);
    operation_start (op, CC_PANEL (self), args, upgrade_done);
}

static void
cc_updates_panel_dispose (GObject *object)
{
    CcUpdatesPanel *self = CC_UPDATES_PANEL (object);

    g_cancellable_cancel (self->cancellable);
    g_clear_object (&self->cancellable);
    g_clear_pointer (&self->rows, g_ptr_array_unref);
    G_OBJECT_CLASS (cc_updates_panel_parent_class)->dispose (object);
}

static void
cc_updates_panel_class_init (CcUpdatesPanelClass *klass)
{
    G_OBJECT_CLASS (klass)->dispose = cc_updates_panel_dispose;
}

/* Status and buttons in the middle of the page (within reach of the thumb, e.g. for "Check
 * Again") while no packages are listed; with a list, from the top */
static void
align_to_list (GtkWidget *list, GParamSpec *pspec, GtkWidget *box)
{
    gtk_widget_set_valign (box, gtk_widget_get_visible (list) ? GTK_ALIGN_START : GTK_ALIGN_CENTER);
}

static void
cc_updates_panel_init (CcUpdatesPanel *self)
{
    GtkWidget *box = gtk_box_new (GTK_ORIENTATION_VERTICAL, 12);

    self->cancellable = g_cancellable_new ();
    self->rows = g_ptr_array_new ();

    /* Icon, title and description like Adw.StatusPage, but scrolling with the list */
    self->icon = gtk_image_new_from_icon_name ("software-update-available-symbolic");
    gtk_image_set_pixel_size (GTK_IMAGE (self->icon), 96);
    gtk_widget_set_margin_top (self->icon, 24);
    gtk_widget_set_margin_bottom (self->icon, 12);
    gtk_widget_add_css_class (self->icon, "dim-label");
    self->title = wrapped_label (P_("Checking for updates…"), "title-2");
    self->description = wrapped_label ("", NULL);
    gtk_widget_set_visible (self->description, FALSE);
    self->spinner = adw_spinner_new ();
    gtk_widget_set_size_request (self->spinner, 32, 32);
    gtk_widget_set_halign (self->spinner, GTK_ALIGN_CENTER);
    gtk_widget_set_margin_top (self->spinner, 12);
    gtk_widget_set_visible (self->spinner, FALSE);
    self->upgrade_button = pill (P_("Update Now"), "suggested-action");
    gtk_widget_set_visible (self->upgrade_button, FALSE);
    g_signal_connect_swapped (self->upgrade_button, "clicked", G_CALLBACK (upgrade_clicked), self);
    self->check_button = pill (P_("Check Again"), NULL);
    /* One empty line above it, so that it does not read as part of the status */
    gtk_widget_set_margin_top (self->check_button, 24);
    g_signal_connect_swapped (self->check_button, "clicked", G_CALLBACK (updates_check), self);
    self->repo_warning = adw_banner_new ("");
    adw_banner_set_use_markup (ADW_BANNER (self->repo_warning), FALSE);
    self->list = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_group_set_title (self->list, P_("Packages"));
    gtk_widget_set_visible (GTK_WIDGET (self->list), FALSE);

    gtk_box_append (GTK_BOX (box), self->icon);
    gtk_box_append (GTK_BOX (box), self->title);
    gtk_box_append (GTK_BOX (box), self->description);
    gtk_box_append (GTK_BOX (box), self->spinner);
    /* "Check Again" first, "Update Now" below it (wish of the phone's user) */
    gtk_box_append (GTK_BOX (box), self->check_button);
    gtk_box_append (GTK_BOX (box), self->upgrade_button);
    gtk_box_append (GTK_BOX (box), GTK_WIDGET (self->list));
    gtk_widget_set_valign (box, GTK_ALIGN_CENTER);
    g_signal_connect_object (self->list, "notify::visible", G_CALLBACK (align_to_list), box, 0);

    {
        GtkWidget *toolbar = adw_toolbar_view_new ();

        adw_toolbar_view_add_top_bar (ADW_TOOLBAR_VIEW (toolbar), adw_header_bar_new ());
        adw_toolbar_view_add_top_bar (ADW_TOOLBAR_VIEW (toolbar), self->repo_warning);
        adw_toolbar_view_set_content (ADW_TOOLBAR_VIEW (toolbar), scrolled (box));
        adw_navigation_page_set_child (ADW_NAVIGATION_PAGE (self), toolbar);
    }
    updates_check (self);
}

/* Listed (on System and Security) only with plinius-software */
void
cc_updates_panel_static_init_func (void)
{
    CcApplication *application = CC_APPLICATION (g_application_get_default ());

    cc_shell_model_set_panel_visibility (cc_application_get_model (application), "updates",
                                         g_file_test (HELPER, G_FILE_TEST_IS_EXECUTABLE) ? CC_PANEL_VISIBLE
                                                                                          : CC_PANEL_HIDDEN);
}
