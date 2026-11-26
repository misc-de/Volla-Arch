/*
 * cc-plinius-webapps.c
 *
 * Plinius: the "Web Apps" tab of Settings > Apps.
 *
 * A web app is a website that starts like an app of its own: its own icon among the apps, its
 * own window in the overview, its own browser profile (logins and site data stay apart from
 * the browser and from other web apps). The browser is Firefox, or Chromium / Google Chrome
 * where installed (the page offers a choice when there is more than one). The tab lists them; a page adds or edits one: the address,
 * the name, the icon (found on the website or chosen from a file), whether the window is
 * frameless (no address bar, tabs or menu) and whether Firefox deletes the cache and the
 * cookies when the web app is closed. The switch of a row puts the web app among the apps or
 * takes it away again; Delete removes it with its profile.
 *
 * Add opens the search in the catalog of well-known websites (package plinius-webapps, published
 * signed on plinius.cais.de), like an app store: a search field and Manually at the top right
 * for the empty page; below the field the groups (communication, games, banks ...), a tap on one
 * lists its web apps on a page of its own; once something is typed, the matching web apps
 * alone, by name. Each web app shows its icon from the catalog (the helper names the file
 * after checking it). The search looks in the name, the address, the
 * entry's keywords and its group (its title and words such as "bank" or "games", in the phone's
 * language and in English). The catalog's helper (CATALOG_HELPER) prints the entries for the
 * phone's language and country with their starting settings (Frameless, Identify As, deleting
 * cache and cookies); the search page runs it twice, without the network first so the list is
 * there at once, then with the server, and shows a newer catalog when one came. Tapping an entry
 * opens the page of a new web app filled in with it and the catalog's icon (without one, the
 * icon is looked for on the website at once); adding it
 * goes back to the list of web apps, past the search. Every page goes back with a swipe.
 * Without the helper, Add opens the empty page directly.
 *
 * Everything lives in the user's home, nothing needs root:
 * - ~/.local/share/plinius-webapps/<id>/webapp.ini: name, address, options, the icon's file
 * - ~/.local/share/plinius-webapps/<id>/icon-<time>.png: the icon (a new name for each new icon,
 *   so Phosh does not show a cached old one)
 * - ~/.local/share/plinius-webapps/<id>/profile/: the Firefox profile. user.js and
 *   chrome/userChrome.css are written from the options on each save; everything else there is
 *   Firefox's. With Chromium or Chrome the profile is chromium/ or chrome/ instead.
 * - ~/.local/share/applications/plinius-webapp-<id>.desktop while the web app is switched on.
 *   Firefox: "firefox --name plinius-webapp-<id> --profile <profile> <address>". --name sets
 *   the window's Wayland app_id. Phosh raises an open window instead of starting the app again
 *   when the window's app_id leads to the tapped desktop file; without --name every window is
 *   "firefox", and web apps and Firefox would raise each other's windows. Firefox finds a running
 *   instance by the profile's path (D-Bus name), so a profile of its own is an instance of its
 *   own, also next to the Firefox preloaded after login.
 * - Chromium: "--user-data-dir=<profile>", --class=plinius-webapp-<id> for a normal window.
 *   A frameless window (--app=<address>) ignores --class: its app_id is
 *   "chrome-<host>_<path, / as _>-Default" (measured with Chromium 156), which becomes the
 *   desktop file's StartupWMClass. Chromium cannot delete cache and cookies itself when it
 *   quits; with one of those options the desktop file starts launch.sh in the web app's folder,
 *   which waits for the browser to quit and deletes them then. A start while the profile's
 *   browser runs hands the address over and returns at once (no deleting then).
 *
 * Identify As: what the browser tells websites it is (User-Agent). The browser's own by default;
 * else a desktop or mobile browser of the same kind (Firefox's or Chrome's form, an iPhone is
 * always Safari), with the major version of the installed browser so sites do not call it out of
 * date. Firefox: general.useragent.override, general.platform.override and general.oscpu.override
 * in user.js; back to the browser's own, those lines also go from prefs.js (Firefox keeps a value
 * user.js once set there). Chromium: --user-agent= (its client hints, navigator.userAgentData,
 * still name the real system). The string written is kept in webapp.ini; when the browser's
 * version changes, the tab writes the web app again.
 *
 * Measured with Firefox 159 and mobile-config-firefox 5.5 on the phone: the userChrome.css
 * below hides all toolbars also with mobile-config-firefox's layout (it loads its own CSS
 * through a chrome manifest and leaves the profile's userChrome.css alone).
 *
 * The cache and the cookies are deleted by Firefox itself when it shuts down
 * (privacy.sanitize.sanitizeOnShutdown with the clearOnShutdown prefs, old and _v2 names, so
 * Firefox's migration between them cannot turn an option back on or off).
 *
 * The icon: the page is loaded (at most MAX_PAGE_BYTES), its <link rel="icon">,
 * <link rel="apple-touch-icon"> and the web manifest's icons are collected with their sizes,
 * /apple-touch-icon.png and /favicon.ico are added as guesses, and the largest one that loads
 * as an image wins. The page's name for itself (manifest short_name or name, application-name,
 * og:site_name, <title>) fills an empty Name field.
 *
 * Images from the network are decoded in this process, so only PNG, JPEG, ICO and SVG are
 * taken (told by their first bytes, not by what the server says), and one that says it is
 * larger than MAX_ICON_SIDE is dropped before its pixels are decoded. If the page's server has
 * a public address, the redirects, the manifest and the icons may only reach public addresses
 * too, checked when the connection is made (after the name was resolved, before anything is
 * sent): a website must not make the phone send requests into the local network or to itself
 * (L7). A web app of the local network (a router, Home Assistant) still finds its icon.
 *
 * SPDX-License-Identifier: MIT
 */

#include "config.h"

#undef G_LOG_DOMAIN
#define G_LOG_DOMAIN "cc-plinius-webapps"

#include <adwaita.h>
#include <errno.h>
#include <gdk-pixbuf/gdk-pixbuf.h>
#include <glib/gstdio.h>
#include <json-glib/json-glib.h>
#include <libsoup/soup.h>
#include <signal.h>
#include <stdlib.h>
#include <string.h>

#include "cc-plinius-webapps.h"

#define PLINIUS_DOMAIN "plinius-control-center"
#define P_(String) g_dgettext (PLINIUS_DOMAIN, String)
/* A string for the translations, translated where it is shown */
#ifndef N_
#define N_(String) (String)
#endif

#define DATA_DIR_NAME "plinius-webapps"
#define CONFIG_FILE "webapp.ini"
#define CONFIG_GROUP "Web App"
/* Desktop file and app_id: plinius-webapp-<id> */
#define DESKTOP_PREFIX "plinius-webapp-"
/* Chromium web apps with a deleting option start through this script in their folder */
#define LAUNCH_SCRIPT "launch.sh"
/* Icon among the apps until the web app has one of its own */
#define FALLBACK_ICON "web-browser"
#define EDITOR_KEY "plinius-webapp-editor"
#define SEARCH_KEY "plinius-webapp-search"
/* The catalog of web apps (package plinius-webapps), JSON on stdout */
#define CATALOG_HELPER "/usr/lib/plinius-webapps/plinius-webapps-catalog"
/* What is read of its answer at most */
#define MAX_CATALOG_BYTES (4 * 1024 * 1024)

/* Icons are stored at most this large (pixels, square) */
#define ICON_SIZE 256
#define PREVIEW_SIZE 96
#define ROW_ICON_SIZE 32
/* Icons in the catalog, as the apps in Explore */
#define CATALOG_ICON_SIZE 48
/* What is read of a page, a manifest, an icon at most */
#define MAX_PAGE_BYTES (1024 * 1024)
#define MAX_MANIFEST_BYTES (256 * 1024)
#define MAX_ICON_BYTES (4 * 1024 * 1024)
/* Icon files tried at most, the largest first */
#define MAX_ICON_TRIES 8
/* An image from the network with a larger side is not decoded: a small PNG can claim a size
 * whose pixels take gigabytes (attack review 2026-11-12, L5) */
#define MAX_ICON_SIDE 4096
/* The loader learns the size from the header; the rest goes in only after it was checked */
#define ICON_CHUNK_BYTES 4096
/* Wait after typing in the address before looking for the icon */
#define FETCH_DELAY_MS 1000
/* Sites answer browsers with their real page, unknown clients sometimes with a block page */
#define USER_AGENT "Mozilla/5.0 (X11; Linux aarch64; rv:140.0) Gecko/20100101 Firefox/140.0"

/* ----- The browsers ----- */

typedef struct {
    const char *id;           /* in webapp.ini */
    const char *label;        /* a product name, not translated */
    const char *commands[3];  /* the first one in PATH is used */
    const char *profile_dir;  /* in the web app's folder */
    gboolean chromium;        /* Chromium's options instead of Firefox's */
} Engine;

static const Engine engines[] = {
    { "firefox", "Firefox", { "firefox", NULL }, "profile", FALSE },
    { "chromium", "Chromium", { "chromium", "chromium-browser", NULL }, "chromium", TRUE },
    { "chrome", "Google Chrome", { "google-chrome-stable", "google-chrome", NULL }, "chrome", TRUE },
};

/* The command of @engine in PATH, NULL if not installed */
static const char *
engine_command (const Engine *engine)
{
    for (const char *const *c = engine->commands; *c; c++) {
        g_autofree char *path = g_find_program_in_path (*c);

        if (path)
            return *c;
    }
    return NULL;
}

/* Unknown or missing ids are Firefox, the browser web apps had first */
static const Engine *
engine_by_id (const char *id)
{
    for (gsize i = 0; i < G_N_ELEMENTS (engines); i++)
        if (g_strcmp0 (engines[i].id, id) == 0)
            return &engines[i];
    return &engines[0];
}

/* For a new web app: the first installed one, in the order of engines[] */
static const Engine *
default_engine (void)
{
    for (gsize i = 0; i < G_N_ELEMENTS (engines); i++)
        if (engine_command (&engines[i]))
            return &engines[i];
    return &engines[0];
}

/* ----- What the browser tells websites it is ----- */

typedef enum {
    FORM_FIREFOX,
    FORM_CHROME,
} UaForm;

typedef struct {
    const char *id;     /* in webapp.ini; "" = the browser's own */
    const char *label;  /* translated with P_() */
    /* %1$d: the browser's major version */
    const char *firefox, *chrome;
    const char *platform, *oscpu; /* navigator.platform and navigator.oscpu for Firefox */
} Identity;

static const Identity identities[] = {
    { "", N_("Browser Default"), NULL, NULL, NULL, NULL },
    { "android", N_("Mobile (Android)"),
      "Mozilla/5.0 (Android 15; Mobile; rv:%1$d.0) Gecko/%1$d.0 Firefox/%1$d.0",
      "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/%1$d.0.0.0 Mobile "
      "Safari/537.36",
      "Linux armv8l", "Linux armv8l" },
    /* Safari: what sites expect of an iPhone; iOS 26 still says 18_6 */
    { "iphone", N_("Mobile (iPhone)"),
      "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
      "Version/26.0 Mobile/15E148 Safari/604.1",
      "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
      "Version/26.0 Mobile/15E148 Safari/604.1",
      "iPhone", "" },
    { "windows", N_("Desktop (Windows)"),
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:%1$d.0) Gecko/20100101 Firefox/%1$d.0",
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/%1$d.0.0.0 "
      "Safari/537.36",
      "Win32", "Windows NT 10.0; Win64; x64" },
    { "macos", N_("Desktop (macOS)"),
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:%1$d.0) Gecko/20100101 Firefox/%1$d.0",
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/%1$d.0.0.0 "
      "Safari/537.36",
      "MacIntel", "Intel Mac OS X 10.15" },
    { "linux", N_("Desktop (Linux)"),
      "Mozilla/5.0 (X11; Linux x86_64; rv:%1$d.0) Gecko/20100101 Firefox/%1$d.0",
      "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/%1$d.0.0.0 Safari/537.36",
      "Linux x86_64", "Linux x86_64" },
};

/* Unknown ids (and none) are the browser's own */
static const Identity *
identity_by_id (const char *id)
{
    for (gsize i = 0; i < G_N_ELEMENTS (identities); i++)
        if (g_strcmp0 (identities[i].id, id) == 0)
            return &identities[i];
    return &identities[0];
}

/* Versions of the browsers, found once per run of Settings */
#define FALLBACK_MAJOR 140

/* Firefox: [App] Version= of application.ini next to the real binary */
static int
firefox_major (const char *command)
{
    static int major;
    g_autofree char *path = command ? g_find_program_in_path (command) : NULL;
    g_autofree char *real = path ? realpath (path, NULL) : NULL;
    const char *dirs[] = { NULL, "/usr/lib/firefox", "/usr/lib/firefox-esr", NULL };

    if (major)
        return major;
    major = FALLBACK_MAJOR;
    if (real)
        dirs[0] = real;
    for (gsize i = real ? 0 : 1; dirs[i]; i++) {
        g_autofree char *dir = i == 0 ? g_path_get_dirname (dirs[0]) : g_strdup (dirs[i]);
        g_autofree char *ini = g_build_filename (dir, "application.ini", NULL);
        g_autoptr(GKeyFile) file = g_key_file_new ();
        g_autofree char *contents = NULL;
        g_autofree char *version = NULL;

        /* Mozilla's application.ini starts with ";" comments, which GKeyFile refuses: make them "#" */
        if (!g_file_get_contents (ini, &contents, NULL, NULL))
            continue;
        for (char *line = contents; line; line = strchr (line, '\n'), line = line ? line + 1 : NULL)
            if (*line == ';')
                *line = '#';
        if (!g_key_file_load_from_data (file, contents, -1, G_KEY_FILE_NONE, NULL))
            continue;
        version = g_key_file_get_string (file, "App", "Version", NULL);
        if (version && g_ascii_isdigit (version[0])) {
            major = (int) g_ascii_strtoull (version, NULL, 10);
            break;
        }
    }
    return major;
}

/* Chromium and Chrome: "<command> --version" says "Chromium 156.0.7444.59 …" */
static int
chrome_major (const char *command)
{
    static GHashTable *majors;
    g_autofree char *out = NULL;
    const char *argv[] = { command, "--version", NULL };
    int major = FALLBACK_MAJOR;

    if (!command)
        return major;
    if (!majors)
        majors = g_hash_table_new_full (g_str_hash, g_str_equal, g_free, NULL);
    if (g_hash_table_contains (majors, command))
        return GPOINTER_TO_INT (g_hash_table_lookup (majors, command));
    if (g_spawn_sync (NULL, (char **) argv, NULL, G_SPAWN_SEARCH_PATH | G_SPAWN_STDERR_TO_DEV_NULL, NULL, NULL, &out,
                      NULL, NULL, NULL) &&
        out) {
        for (const char *p = out; *p; p++)
            if (g_ascii_isdigit (*p) && (p == out || *(p - 1) == ' ')) {
                major = (int) g_ascii_strtoull (p, NULL, 10);
                break;
            }
    }
    g_hash_table_insert (majors, g_strdup (command), GINT_TO_POINTER (major));
    return major;
}

/* The User-Agent @identity gives in @engine; NULL for the browser's own */
static char *
identity_user_agent (const Identity *identity, const Engine *engine)
{
    const char *command = engine_command (engine);

    if (!identity->id[0])
        return NULL;
    if (engine->chromium)
        return g_strdup_printf (identity->chrome, chrome_major (command));
    return g_strdup_printf (identity->firefox, firefox_major (command));
}

/* ----- A web app and its files ----- */

typedef struct {
    char *id; /* 16 hex digits, also the folder's name */
    char *browser; /* Engine id */
    char *name;
    char *url;
    char *icon; /* the icon's file name in the web app's folder, NULL = none (yet) */
    gboolean frameless;
    gboolean clear_cache;
    gboolean clear_cookies;
    gboolean active; /* among the apps (a desktop file exists) */
    char *identity; /* Identity id, NULL = the browser's own */
    char *user_agent; /* what was written for it, NULL = nothing */
} WebApp;

static void
webapp_free (WebApp *app)
{
    if (!app)
        return;
    g_free (app->id);
    g_free (app->browser);
    g_free (app->name);
    g_free (app->url);
    g_free (app->icon);
    g_free (app->identity);
    g_free (app->user_agent);
    g_free (app);
}

G_DEFINE_AUTOPTR_CLEANUP_FUNC (WebApp, webapp_free)

static WebApp *
webapp_copy (const WebApp *app)
{
    WebApp *copy = g_memdup2 (app, sizeof (WebApp));

    copy->id = g_strdup (app->id);
    copy->browser = g_strdup (app->browser);
    copy->name = g_strdup (app->name);
    copy->url = g_strdup (app->url);
    copy->icon = g_strdup (app->icon);
    copy->identity = g_strdup (app->identity);
    copy->user_agent = g_strdup (app->user_agent);
    return copy;
}

/* A folder name we wrote ourselves; nothing else in the data folder is touched */
static gboolean
valid_id (const char *id)
{
    gsize len = strlen (id);

    if (len < 8 || len > 32)
        return FALSE;
    for (const char *p = id; *p; p++)
        if (!g_ascii_isxdigit (*p) || g_ascii_isupper (*p))
            return FALSE;
    return TRUE;
}

/* ~/.local/share/plinius-webapps[/<id>[/<file>]] */
static char *
app_path (const char *id, const char *file)
{
    return g_build_filename (g_get_user_data_dir (), DATA_DIR_NAME, id, file, NULL);
}

static char *
app_id_of (const WebApp *app)
{
    return g_strconcat (DESKTOP_PREFIX, app->id, NULL);
}

static char *
desktop_path (const char *id)
{
    g_autofree char *file = g_strconcat (DESKTOP_PREFIX, id, ".desktop", NULL);

    return g_build_filename (g_get_user_data_dir (), "applications", file, NULL);
}

static gboolean
browser_available (void)
{
    for (gsize i = 0; i < G_N_ELEMENTS (engines); i++)
        if (engine_command (&engines[i]))
            return TRUE;
    return FALSE;
}

static char *
profile_path (const WebApp *app)
{
    return app_path (app->id, engine_by_id (app->browser)->profile_dir);
}

static gboolean
set_errno_error (GError **error, int saved_errno, const char *path)
{
    g_set_error (error, G_IO_ERROR, g_io_error_from_errno (saved_errno), "%s: %s", path, g_strerror (saved_errno));
    return FALSE;
}

static gboolean
make_dir (const char *path, GError **error)
{
    if (g_mkdir_with_parents (path, 0700) < 0)
        return set_errno_error (error, errno, path);
    return TRUE;
}

static gboolean
remove_file (const char *path, GError **error)
{
    if (g_unlink (path) < 0 && errno != ENOENT)
        return set_errno_error (error, errno, path);
    return TRUE;
}

static WebApp *
webapp_load (const char *id)
{
    g_autoptr(GKeyFile) config = g_key_file_new ();
    g_autofree char *path = app_path (id, CONFIG_FILE);
    WebApp *app;

    if (!g_key_file_load_from_file (config, path, G_KEY_FILE_NONE, NULL))
        return NULL;
    app = g_new0 (WebApp, 1);
    app->id = g_strdup (id);
    app->browser = g_key_file_get_string (config, CONFIG_GROUP, "Browser", NULL);
    app->name = g_key_file_get_string (config, CONFIG_GROUP, "Name", NULL);
    app->url = g_key_file_get_string (config, CONFIG_GROUP, "URL", NULL);
    app->icon = g_key_file_get_string (config, CONFIG_GROUP, "Icon", NULL);
    app->frameless = g_key_file_get_boolean (config, CONFIG_GROUP, "Frameless", NULL);
    app->clear_cache = g_key_file_get_boolean (config, CONFIG_GROUP, "ClearCache", NULL);
    app->clear_cookies = g_key_file_get_boolean (config, CONFIG_GROUP, "ClearCookies", NULL);
    app->active = g_key_file_get_boolean (config, CONFIG_GROUP, "Active", NULL);
    app->identity = g_key_file_get_string (config, CONFIG_GROUP, "IdentifyAs", NULL);
    app->user_agent = g_key_file_get_string (config, CONFIG_GROUP, "UserAgent", NULL);
    if (!app->name || !*app->name || !app->url || !*app->url) {
        webapp_free (app);
        return NULL;
    }
    /* Only a file of the web app's own folder */
    if (app->icon && (!*app->icon || strchr (app->icon, '/') || app->icon[0] == '.'))
        g_clear_pointer (&app->icon, g_free);
    return app;
}

static int
compare_apps (gconstpointer a, gconstpointer b)
{
    const WebApp *x = *(WebApp * const *) a;
    const WebApp *y = *(WebApp * const *) b;

    return g_utf8_collate (x->name, y->name);
}

/* All web apps, by name */
static GPtrArray *
webapps_load_all (void)
{
    GPtrArray *apps = g_ptr_array_new_with_free_func ((GDestroyNotify) webapp_free);
    g_autofree char *root = app_path (NULL, NULL);
    g_autoptr(GDir) dir = g_dir_open (root, 0, NULL);
    const char *name;

    while (dir && (name = g_dir_read_name (dir))) {
        WebApp *app;

        if (valid_id (name) && (app = webapp_load (name)))
            g_ptr_array_add (apps, app);
    }
    g_ptr_array_sort (apps, compare_apps);
    return apps;
}

static char *
new_id (void)
{
    for (;;) {
        char *id = g_strdup_printf ("%08x%08x", g_random_int (), g_random_int ());
        g_autofree char *dir = app_path (id, NULL);

        if (!g_file_test (dir, G_FILE_TEST_EXISTS))
            return id;
        g_free (id);
    }
}

static gboolean
webapp_write_config (const WebApp *app, GError **error)
{
    g_autoptr(GKeyFile) config = g_key_file_new ();
    g_autofree char *path = app_path (app->id, CONFIG_FILE);

    g_key_file_set_string (config, CONFIG_GROUP, "Browser", engine_by_id (app->browser)->id);
    g_key_file_set_string (config, CONFIG_GROUP, "Name", app->name);
    g_key_file_set_string (config, CONFIG_GROUP, "URL", app->url);
    if (app->icon)
        g_key_file_set_string (config, CONFIG_GROUP, "Icon", app->icon);
    g_key_file_set_boolean (config, CONFIG_GROUP, "Frameless", app->frameless);
    g_key_file_set_boolean (config, CONFIG_GROUP, "ClearCache", app->clear_cache);
    g_key_file_set_boolean (config, CONFIG_GROUP, "ClearCookies", app->clear_cookies);
    g_key_file_set_boolean (config, CONFIG_GROUP, "Active", app->active);
    if (identity_by_id (app->identity)->id[0])
        g_key_file_set_string (config, CONFIG_GROUP, "IdentifyAs", identity_by_id (app->identity)->id);
    if (app->user_agent)
        g_key_file_set_string (config, CONFIG_GROUP, "UserAgent", app->user_agent);
    return g_key_file_save_to_file (config, path, error);
}

/* --- The Firefox profile --- */

static void
pref_bool (GString *prefs, const char *key, gboolean value)
{
    g_string_append_printf (prefs, "user_pref(\"%s\", %s);\n", key, value ? "true" : "false");
}

static void
pref_int (GString *prefs, const char *key, int value)
{
    g_string_append_printf (prefs, "user_pref(\"%s\", %d);\n", key, value);
}

/* Only for our own constant values: no escaping */
static void
pref_string (GString *prefs, const char *key, const char *value)
{
    g_string_append_printf (prefs, "user_pref(\"%s\", \"%s\");\n", key, value);
}

/* The prefs Identify As sets; back to the browser's own they also go from prefs.js */
static const char *const identity_prefs[] = {
    "general.useragent.override",
    "general.platform.override",
    "general.oscpu.override",
};

/* user.js: read by Firefox on every start, wins over prefs.js */
static char *
profile_prefs (const WebApp *app)
{
    GString *prefs = g_string_new ("// Written by Settings (Apps > Web Apps) from the web app's options.\n"
                                   "// Changes here are overwritten; set them in Settings instead.\n\n");
    gboolean sanitize = app->clear_cache || app->clear_cookies;

    /* No first-run pages, no questions: the web app opens its address and nothing else */
    pref_bool (prefs, "browser.shell.checkDefaultBrowser", FALSE);
    pref_bool (prefs, "browser.aboutwelcome.enabled", FALSE);
    pref_string (prefs, "browser.startup.homepage_override.mstone", "ignore");
    pref_string (prefs, "startup.homepage_welcome_url", "");
    pref_string (prefs, "startup.homepage_welcome_url.additional", "");
    pref_string (prefs, "datareporting.policy.firstRunURL", "");
    pref_int (prefs, "browser.startup.page", 0);
    pref_bool (prefs, "browser.sessionstore.resume_from_crash", FALSE);
    pref_bool (prefs, "browser.tabs.warnOnClose", FALSE);
    pref_bool (prefs, "browser.translations.automaticallyPopup", FALSE);

    /* Identify As (the strings are ours: no quotes or backslashes in them) */
    if (app->user_agent) {
        const Identity *identity = identity_by_id (app->identity);

        pref_string (prefs, identity_prefs[0], app->user_agent);
        if (identity->platform && *identity->platform)
            pref_string (prefs, identity_prefs[1], identity->platform);
        if (identity->oscpu && *identity->oscpu)
            pref_string (prefs, identity_prefs[2], identity->oscpu);
    }

    /* Frameless: userChrome.css hides the toolbars. Without a tab bar a new tab could not be
     * seen, so links that want one open in a window of their own (in the overview). */
    pref_bool (prefs, "toolkit.legacyUserProfileCustomizations.stylesheets", app->frameless);
    pref_int (prefs, "browser.link.open_newwindow", app->frameless ? 2 : 3);

    /* When closing: Firefox deletes what is switched on, and only that */
    pref_bool (prefs, "privacy.sanitize.sanitizeOnShutdown", sanitize);
    pref_int (prefs, "privacy.sanitize.timeSpan", 0);
    pref_bool (prefs, "privacy.sanitize.clearOnShutdown.hasMigratedToNewPrefs2", TRUE);
    pref_bool (prefs, "privacy.sanitize.clearOnShutdown.hasMigratedToNewPrefs3", TRUE);
    pref_bool (prefs, "privacy.clearOnShutdown.cache", app->clear_cache);
    pref_bool (prefs, "privacy.clearOnShutdown_v2.cache", app->clear_cache);
    pref_bool (prefs, "privacy.clearOnShutdown.cookies", app->clear_cookies);
    pref_bool (prefs, "privacy.clearOnShutdown.offlineApps", app->clear_cookies);
    pref_bool (prefs, "privacy.clearOnShutdown_v2.cookiesAndStorage", app->clear_cookies);
    pref_bool (prefs, "privacy.clearOnShutdown.history", FALSE);
    pref_bool (prefs, "privacy.clearOnShutdown.downloads", FALSE);
    pref_bool (prefs, "privacy.clearOnShutdown.formdata", FALSE);
    pref_bool (prefs, "privacy.clearOnShutdown.sessions", FALSE);
    pref_bool (prefs, "privacy.clearOnShutdown.siteSettings", FALSE);
    pref_bool (prefs, "privacy.clearOnShutdown_v2.historyFormDataAndDownloads", FALSE);
    pref_bool (prefs, "privacy.clearOnShutdown_v2.browsingHistoryAndDownloads", FALSE);
    pref_bool (prefs, "privacy.clearOnShutdown_v2.formdata", FALSE);
    pref_bool (prefs, "privacy.clearOnShutdown_v2.downloads", FALSE);
    pref_bool (prefs, "privacy.clearOnShutdown_v2.siteSettings", FALSE);
    return g_string_free (prefs, FALSE);
}

/* Frameless: no menu, tabs, address bar or bookmarks; the page fills the window */
static const char frameless_css[] =
    "/* Written by Settings (Apps > Web Apps): the web app is frameless. */\n"
    "#toolbar-menubar, #TabsToolbar, #nav-bar, #PersonalToolbar, #sidebar-box {\n"
    "  visibility: collapse !important;\n"
    "}\n"
    "#navigator-toolbox {\n"
    "  min-height: 0 !important;\n"
    "  max-height: 0 !important;\n"
    "  border: 0 !important;\n"
    "  padding: 0 !important;\n"
    "  margin: 0 !important;\n"
    "}\n"
    "#browser, #appcontent, #tabbrowser-tabbox {\n"
    "  margin: 0 !important;\n"
    "  padding: 0 !important;\n"
    "}\n";

/* Back to the browser's own: Firefox keeps in prefs.js what user.js once set, so those lines go
 * there too (only while the profile's Firefox is not running; it writes prefs.js when it quits) */
static void
forget_identity_prefs (const char *profile)
{
    g_autofree char *path = g_build_filename (profile, "prefs.js", NULL);
    g_autofree char *text = NULL;
    g_auto(GStrv) lines = NULL;
    g_autoptr(GString) out = g_string_new (NULL);
    gboolean changed = FALSE;

    if (!g_file_get_contents (path, &text, NULL, NULL))
        return;
    lines = g_strsplit (text, "\n", -1);
    for (guint i = 0; lines[i]; i++) {
        gboolean drop = FALSE;

        for (gsize p = 0; p < G_N_ELEMENTS (identity_prefs); p++) {
            g_autofree char *start = g_strdup_printf ("user_pref(\"%s\",", identity_prefs[p]);

            if (g_str_has_prefix (lines[i], start))
                drop = TRUE;
        }
        if (drop) {
            changed = TRUE;
            continue;
        }
        g_string_append (out, lines[i]);
        if (lines[i + 1])
            g_string_append_c (out, '\n');
    }
    if (changed)
        g_file_set_contents (path, out->str, out->len, NULL);
}

static gboolean webapp_running (const WebApp *app);

static gboolean
webapp_write_profile (const WebApp *app, GError **error)
{
    g_autofree char *profile = profile_path (app);
    g_autofree char *chrome = g_build_filename (profile, "chrome", NULL);
    g_autofree char *user_js = g_build_filename (profile, "user.js", NULL);
    g_autofree char *css = g_build_filename (chrome, "userChrome.css", NULL);
    g_autofree char *prefs = profile_prefs (app);

    /* Chromium keeps its settings itself; the folder is all it needs */
    if (engine_by_id (app->browser)->chromium)
        return make_dir (profile, error);
    if (!make_dir (chrome, error) || !g_file_set_contents (user_js, prefs, -1, error))
        return FALSE;
    if (!app->user_agent && !webapp_running (app))
        forget_identity_prefs (profile);
    if (app->frameless)
        return g_file_set_contents (css, frameless_css, -1, error);
    return remove_file (css, error);
}

/* --- The desktop file --- */

/* One argument of Exec= (desktop entry specification): quoted, with ", `, $ and \ escaped
 * and % doubled (addresses often contain %XX) */
static void
append_exec_arg (GString *exec, const char *arg)
{
    if (exec->len)
        g_string_append_c (exec, ' ');
    g_string_append_c (exec, '"');
    for (const char *p = arg; *p; p++) {
        if (strchr ("\"`$\\", *p))
            g_string_append_c (exec, '\\');
        else if (*p == '%')
            g_string_append_c (exec, '%');
        g_string_append_c (exec, *p);
    }
    g_string_append_c (exec, '"');
}

static char *
url_host (const char *url)
{
    g_autoptr(GUri) uri = g_uri_parse (url, SOUP_HTTP_URI_FLAGS, NULL);
    const char *host = uri ? g_uri_get_host (uri) : NULL;

    return g_strdup (host && *host ? host : url);
}

/* The app_id of Chromium's --app window: "chrome-" + host + "_" + path with / as _ + "-Default" */
static char *
chromium_app_class (const char *url)
{
    g_autoptr(GUri) uri = g_uri_parse (url, SOUP_HTTP_URI_FLAGS, NULL);
    const char *path = uri ? g_uri_get_path (uri) : NULL;
    g_autofree char *name = NULL;

    if (!uri || !g_uri_get_host (uri))
        return NULL;
    name = g_strconcat (g_uri_get_host (uri), "_", path && *path ? path : "/", NULL);
    g_strdelimit (name, "/", '_');
    return g_strconcat ("chrome-", name, "-Default", NULL);
}

/* The browser's command line of @app, NULL if its browser is not installed */
static GStrv
launch_argv (const WebApp *app)
{
    const Engine *engine = engine_by_id (app->browser);
    const char *command = engine_command (engine);
    g_autoptr(GStrvBuilder) argv = g_strv_builder_new ();
    g_autofree char *app_id = app_id_of (app);
    g_autofree char *profile = profile_path (app);

    if (!command)
        command = engine->commands[0];
    g_strv_builder_add (argv, command);
    if (!engine->chromium) {
        g_strv_builder_addv (argv, (const char *[]) { "--name", app_id, "--profile", profile, app->url, NULL });
    } else {
        g_autofree char *data_dir = g_strconcat ("--user-data-dir=", profile, NULL);
        g_autofree char *class = g_strconcat ("--class=", app_id, NULL);
        g_autofree char *app_url = g_strconcat ("--app=", app->url, NULL);

        g_strv_builder_addv (argv, (const char *[]) { data_dir, "--no-first-run", NULL });
        if (app->user_agent) {
            g_autofree char *ua = g_strconcat ("--user-agent=", app->user_agent, NULL);

            g_strv_builder_add (argv, ua);
        }
        if (app->frameless)
            g_strv_builder_add (argv, app_url);
        else
            g_strv_builder_addv (argv, (const char *[]) { class, "--new-window", app->url, NULL });
    }
    return g_strv_builder_end (argv);
}

/* Chromium with a deleting option: start, wait for the browser to quit, delete */
static char *
launch_script (const WebApp *app, const char *const *argv)
{
    g_autofree char *profile = profile_path (app);
    g_autofree char *quoted = g_shell_quote (profile);
    g_autoptr(GString) command = g_string_new (NULL);
    GString *script = g_string_new ("#!/bin/sh\n"
                                    "# Written by Settings (Apps > Web Apps) from the web app's options; changes here\n"
                                    "# are overwritten. Starts the web app; once its browser has quit, deletes what the\n"
                                    "# options say (Chromium cannot do that itself).\n");

    for (const char *const *a = argv; *a; a++) {
        g_autofree char *arg = g_shell_quote (*a);

        g_string_append_printf (command, "%s%s", command->len ? " " : "", arg);
    }
    g_string_append_printf (script, "profile=%s\n", quoted);
    g_string_append (script, "# SingletonLock points to <host>-<pid> while the profile's browser runs\n"
                             "running() {\n"
                             "\tlock=$(readlink \"$profile/SingletonLock\" 2>/dev/null) || return 1\n"
                             "\tkill -0 \"${lock##*-}\" 2>/dev/null\n"
                             "}\n"
                             "# Open already: the browser takes the address over, this start ends at once\n");
    g_string_append_printf (script, "if running; then\n\texec %s\nfi\n%s\n", command->str, command->str);
    g_string_append (script, "running && exit 0\n"
                             "cd \"$profile/Default\" 2>/dev/null || exit 0\n");
    if (app->clear_cache)
        g_string_append (script, "rm -rf -- Cache 'Code Cache' GPUCache DawnGraphiteCache DawnWebGPUCache\n");
    if (app->clear_cookies)
        g_string_append (script, "rm -rf -- Cookies Cookies-journal 'Local Storage' 'Session Storage' IndexedDB "
                                 "'Service Worker' WebStorage 'File System' blob_storage databases "
                                 "'Shared Dictionary' SharedStorage SharedStorage-wal\n");
    return g_string_free (script, FALSE);
}

static gboolean
webapp_write_desktop_file (const WebApp *app, GError **error)
{
    g_autoptr(GKeyFile) entry = g_key_file_new ();
    g_autoptr(GString) exec = g_string_new (NULL);
    g_autofree char *app_id = app_id_of (app);
    g_auto(GStrv) argv = launch_argv (app);
    g_autofree char *script = app_path (app->id, LAUNCH_SCRIPT);
    g_autofree char *wm_class = NULL;
    const Engine *engine = engine_by_id (app->browser);
    g_autofree char *icon = app->icon ? app_path (app->id, app->icon) : g_strdup (FALLBACK_ICON);
    g_autofree char *host = url_host (app->url);
    g_autofree char *path = desktop_path (app->id);
    g_autofree char *dir = g_path_get_dirname (path);
    const char *group = G_KEY_FILE_DESKTOP_GROUP;

    if (engine->chromium && (app->clear_cache || app->clear_cookies)) {
        g_autofree char *content = launch_script (app, (const char *const *) argv);

        if (!g_file_set_contents_full (script, content, -1, G_FILE_SET_CONTENTS_CONSISTENT, 0700, error))
            return FALSE;
        append_exec_arg (exec, script);
    } else {
        if (!remove_file (script, error))
            return FALSE;
        for (char **a = argv; *a; a++)
            append_exec_arg (exec, *a);
    }
    if (engine->chromium && app->frameless)
        wm_class = chromium_app_class (app->url);

    g_key_file_set_string (entry, group, G_KEY_FILE_DESKTOP_KEY_TYPE, G_KEY_FILE_DESKTOP_TYPE_APPLICATION);
    g_key_file_set_string (entry, group, G_KEY_FILE_DESKTOP_KEY_NAME, app->name);
    g_key_file_set_string (entry, group, G_KEY_FILE_DESKTOP_KEY_COMMENT, host);
    g_key_file_set_string (entry, group, G_KEY_FILE_DESKTOP_KEY_EXEC, exec->str);
    g_key_file_set_string (entry, group, G_KEY_FILE_DESKTOP_KEY_ICON, icon);
    g_key_file_set_boolean (entry, group, G_KEY_FILE_DESKTOP_KEY_TERMINAL, FALSE);
    g_key_file_set_boolean (entry, group, G_KEY_FILE_DESKTOP_KEY_STARTUP_NOTIFY, TRUE);
    g_key_file_set_string (entry, group, G_KEY_FILE_DESKTOP_KEY_STARTUP_WM_CLASS, wm_class ? wm_class : app_id);
    g_key_file_set_string (entry, group, G_KEY_FILE_DESKTOP_KEY_CATEGORIES, "Network;");
    /* Websites fit the phone's screen: also shown when Phosh shows only mobile apps */
    g_key_file_set_string (entry, group, "X-Purism-FormFactor", "Workstation;Mobile;");
    g_key_file_set_string (entry, group, "X-Plinius-WebApp", app->id);

    if (g_mkdir_with_parents (dir, 0755) < 0)
        return set_errno_error (error, errno, dir);
    return g_key_file_save_to_file (entry, path, error);
}

/* Switched on: the desktop file is there (icon among the apps), off: it is not */
static gboolean
webapp_sync_desktop_file (const WebApp *app, GError **error)
{
    g_autofree char *path = NULL;

    if (app->active)
        return webapp_write_desktop_file (app, error);
    path = desktop_path (app->id);
    return remove_file (path, error);
}

/* Writes everything of @app; @icon (if not NULL) becomes its new icon */
static gboolean
webapp_save (WebApp *app, GdkPixbuf *icon, GError **error)
{
    g_autofree char *dir = app_path (app->id, NULL);

    /* The User-Agent with the browser's version now */
    g_free (app->user_agent);
    app->user_agent = identity_user_agent (identity_by_id (app->identity), engine_by_id (app->browser));

    if (!make_dir (dir, error))
        return FALSE;
    if (icon) {
        g_autofree char *file = g_strdup_printf ("icon-%" G_GINT64_FORMAT ".png", g_get_real_time ());
        g_autofree char *path = app_path (app->id, file);

        if (!gdk_pixbuf_save (icon, path, "png", error, NULL))
            return FALSE;
        if (app->icon) {
            g_autofree char *old = app_path (app->id, app->icon);

            remove_file (old, NULL);
        }
        g_free (app->icon);
        app->icon = g_steal_pointer (&file);
    }
    return webapp_write_config (app, error) && webapp_write_profile (app, error) &&
           webapp_sync_desktop_file (app, error);
}

/* A browser runs with a profile of @app: Firefox's "lock" link points to "<address>:+<pid>",
 * Chromium's "SingletonLock" to "<host>-<pid>" (all profiles, also one of an earlier browser) */
static gboolean
webapp_running (const WebApp *app)
{
    for (gsize i = 0; i < G_N_ELEMENTS (engines); i++) {
        g_autofree char *lock = g_build_filename (g_get_user_data_dir (), DATA_DIR_NAME, app->id, engines[i].profile_dir,
                                                  engines[i].chromium ? "SingletonLock" : "lock", NULL);
        g_autofree char *target = g_file_read_link (lock, NULL);
        const char *sep = target ? strrchr (target, engines[i].chromium ? '-' : '+') : NULL;
        char *end = NULL;
        long pid;

        if (!sep)
            continue;
        pid = strtol (sep + 1, &end, 10);
        if (pid > 0 && !*end && (kill ((pid_t) pid, 0) == 0 || errno == EPERM))
            return TRUE;
    }
    return FALSE;
}

/* Deletes @file and, for a folder, everything in it; links are removed, not followed */
static gboolean
remove_tree (GFile *file, GError **error)
{
    g_autoptr(GFileEnumerator) children = g_file_enumerate_children (file, G_FILE_ATTRIBUTE_STANDARD_TYPE,
                                                                     G_FILE_QUERY_INFO_NOFOLLOW_SYMLINKS, NULL, NULL);

    while (children) {
        GFileInfo *info;
        GFile *child;

        if (!g_file_enumerator_iterate (children, &info, &child, NULL, error))
            return FALSE;
        if (!info)
            break;
        if (g_file_info_get_file_type (info) == G_FILE_TYPE_DIRECTORY) {
            if (!remove_tree (child, error))
                return FALSE;
        } else if (!g_file_delete (child, NULL, error)) {
            return FALSE;
        }
    }
    return g_file_delete (file, NULL, error);
}

/* In a thread (a profile can hold many files): the desktop file first, so the icon is gone even
 * if deleting the folder fails */
static void
delete_thread (GTask *task, gpointer source, gpointer task_data, GCancellable *cancellable)
{
    const char *id = task_data;
    g_autofree char *desktop = desktop_path (id);
    g_autofree char *dir = app_path (id, NULL);
    g_autoptr(GFile) file = g_file_new_for_path (dir);
    g_autoptr(GError) error = NULL;

    if (!remove_file (desktop, &error) ||
        (!remove_tree (file, &error) && !g_error_matches (error, G_IO_ERROR, G_IO_ERROR_NOT_FOUND))) {
        g_task_return_error (task, g_steal_pointer (&error));
        return;
    }
    g_task_return_boolean (task, TRUE);
}

/* --- Website data, without cookies --- */

/* What websites stored in a profile besides cookies (and so logins): local storage, IndexedDB,
 * service workers and media licenses with the CDM's identity. A website that once saw another
 * browser or CDM can keep refusing until this is gone (Netflix: E100 after Identify As changed). */
static const char *const firefox_site_data[] = {
    "storage/temporary", "storage/to-be-removed", "webappsstore.sqlite", "webappsstore.sqlite-wal",
    "webappsstore.sqlite-shm", "serviceworker.txt", "gmp",
};
static const char *const chromium_site_data[] = {
    "Default/Local Storage", "Default/Session Storage", "Default/IndexedDB",
    "Default/Service Worker", "Default/File System", "Default/WebStorage",
};

static gboolean
remove_path (const char *path, GError **error)
{
    g_autoptr(GFile) file = g_file_new_for_path (path);
    g_autoptr(GError) local = NULL;
    gboolean dir = g_file_test (path, G_FILE_TEST_IS_DIR) && !g_file_test (path, G_FILE_TEST_IS_SYMLINK);

    if (dir ? remove_tree (file, &local) : g_file_delete (file, NULL, &local))
        return TRUE;
    if (g_error_matches (local, G_IO_ERROR, G_IO_ERROR_NOT_FOUND))
        return TRUE;
    g_propagate_error (error, g_steal_pointer (&local));
    return FALSE;
}

/* Firefox's storage/default holds one folder per site; those of add-ons (uBlock Origin's filter
 * lists, which take a minute to build again) stay */
static gboolean
clear_firefox_site_storage (const char *profile, GError **error)
{
    g_autofree char *dir_path = g_build_filename (profile, "storage", "default", NULL);
    g_autoptr(GDir) dir = g_dir_open (dir_path, 0, NULL);
    const char *name;

    while (dir && (name = g_dir_read_name (dir))) {
        g_autofree char *path = NULL;

        if (g_str_has_prefix (name, "moz-extension+++"))
            continue;
        path = g_build_filename (dir_path, name, NULL);
        if (!remove_path (path, error))
            return FALSE;
    }
    return TRUE;
}

/* Thread safe: only paths. Call only while no browser runs with the web app's profiles. */
static gboolean
webapp_clear_site_data (const char *id, GError **error)
{
    for (gsize i = 0; i < G_N_ELEMENTS (engines); i++) {
        g_autofree char *profile = app_path (id, engines[i].profile_dir);
        const char *const *items = engines[i].chromium ? chromium_site_data : firefox_site_data;
        gsize n = engines[i].chromium ? G_N_ELEMENTS (chromium_site_data) : G_N_ELEMENTS (firefox_site_data);

        if (!g_file_test (profile, G_FILE_TEST_IS_DIR))
            continue;
        if (!engines[i].chromium && !clear_firefox_site_storage (profile, error))
            return FALSE;
        for (gsize j = 0; j < n; j++) {
            g_autofree char *path = g_build_filename (profile, items[j], NULL);

            if (!remove_path (path, error))
                return FALSE;
        }
    }
    return TRUE;
}

static void
clear_site_data_thread (GTask *task, gpointer source, gpointer task_data, GCancellable *cancellable)
{
    g_autoptr(GError) error = NULL;

    if (!webapp_clear_site_data (task_data, &error))
        g_task_return_error (task, g_steal_pointer (&error));
    else
        g_task_return_boolean (task, TRUE);
}

/* "example.org" → "https://example.org"; NULL for anything that is no http(s) address */
static char *
normalize_url (const char *text)
{
    g_autofree char *trimmed = g_strstrip (g_strdup (text ? text : ""));
    g_autofree char *full = NULL;
    g_autoptr(GUri) uri = NULL;
    const char *scheme, *host;

    if (!*trimmed || strpbrk (trimmed, " \t\r\n"))
        return NULL;
    full = strstr (trimmed, "://") ? g_strdup (trimmed) : g_strconcat ("https://", trimmed, NULL);
    uri = g_uri_parse (full, SOUP_HTTP_URI_FLAGS, NULL);
    if (!uri)
        return NULL;
    scheme = g_uri_get_scheme (uri);
    host = g_uri_get_host (uri);
    if ((g_strcmp0 (scheme, "https") != 0 && g_strcmp0 (scheme, "http") != 0) || !host || !*host)
        return NULL;
    /* "exampl" while typing is no address yet; IPv6 literals and localhost are */
    if (!strchr (host, '.') && !strchr (host, ':') && g_ascii_strcasecmp (host, "localhost") != 0)
        return NULL;
    return g_uri_to_string (uri);
}

/* ----- Finding the website's icon and name ----- */

typedef struct {
    char *url;
    int size;  /* expected edge length; SVG and "any" count as large */
    guint order;
} Candidate;

static void
candidate_free (Candidate *c)
{
    g_free (c->url);
    g_free (c);
}

typedef struct _Fetch Fetch;
/* @icon and @name may be NULL; with @cancelled nothing of the caller may be touched */
typedef void (*FetchDone) (GdkPixbuf *icon, const char *name, gboolean cancelled, gpointer data);
typedef void (*BodyDone) (Fetch *f, GBytes *body);

struct _Fetch {
    GCancellable *cancellable;
    SoupMessage *message; /* the current request */
    GUri *base;           /* the page's address after redirects */
    char *name;
    char *manifest;
    GPtrArray *candidates;
    guint next;
    gsize limit;
    guint8 *buffer;
    BodyDone body_done;
    FetchDone done;
    gpointer data;
    /* Cancels the current request only, when it reached an address not allowed; cancelled
     * with @cancellable too (cancel_id) */
    GCancellable *request;
    gulong cancel_id;
    gboolean refused;
    /* Set by the first connection: the page's server is public, so is everything after it */
    gboolean decided;
    gboolean public_only;
};

/* Not the local network, not the phone itself: loopback, private, link-local, CGNAT, ULA,
 * multicast and reserved ranges, also as IPv4-mapped IPv6 */
static gboolean
address_is_public (GInetAddress *address)
{
    const guint8 *b = g_inet_address_to_bytes (address);
    g_autoptr(GInetAddress) v4 = NULL;

    if (g_inet_address_get_family (address) == G_SOCKET_FAMILY_IPV6) {
        static const guint8 mapped[12] = { 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0xff, 0xff };

        if (memcmp (b, mapped, sizeof mapped) == 0) {
            v4 = g_inet_address_new_from_bytes (b + 12, G_SOCKET_FAMILY_IPV4);
            address = v4;
            b = g_inet_address_to_bytes (address);
        }
    }
    if (g_inet_address_get_is_any (address) || g_inet_address_get_is_loopback (address) ||
        g_inet_address_get_is_link_local (address) || g_inet_address_get_is_site_local (address) ||
        g_inet_address_get_is_multicast (address))
        return FALSE;
    if (g_inet_address_get_family (address) == G_SOCKET_FAMILY_IPV4)
        return !(b[0] == 0 || (b[0] == 100 && (b[1] & 0xc0) == 64) || b[0] >= 240 ||
                 (b[0] == 192 && b[1] == 0 && b[2] == 0) || (b[0] == 198 && (b[1] & 0xfe) == 18));
    return (b[0] & 0xfe) != 0xfc;
}

/* What the first connection of each server was (host:port → public), for a page whose request
 * reuses a connection kept open from an earlier search and so connects nowhere */
static GHashTable *
known_servers (void)
{
    static GHashTable *servers;

    if (!servers)
        servers = g_hash_table_new_full (g_str_hash, g_str_equal, g_free, NULL);
    return servers;
}

static char *
server_key (GUri *uri)
{
    g_autofree char *host = g_ascii_strdown (g_uri_get_host (uri) ? g_uri_get_host (uri) : "", -1);
    int port = g_uri_get_port (uri);

    if (port < 0)
        port = g_strcmp0 (g_uri_get_scheme (uri), "https") == 0 ? 443 : 80;
    return g_strdup_printf ("%s:%d", host, port);
}

static void
network_event (SoupMessage *message, GSocketClientEvent event, GIOStream *connection, gpointer data)
{
    Fetch *f = data;
    g_autoptr(GSocketAddress) remote = NULL;
    gboolean public;

    if (event != G_SOCKET_CLIENT_CONNECTED || !G_IS_SOCKET_CONNECTION (connection))
        return;
    remote = g_socket_connection_get_remote_address (G_SOCKET_CONNECTION (connection), NULL);
    if (!G_IS_INET_SOCKET_ADDRESS (remote))
        return;
    public = address_is_public (g_inet_socket_address_get_address (G_INET_SOCKET_ADDRESS (remote)));
    g_hash_table_insert (known_servers (), server_key (soup_message_get_uri (message)), GINT_TO_POINTER (public));
    if (!f->decided) {
        f->decided = TRUE;
        f->public_only = public;
    } else if (f->public_only && !public) {
        f->refused = TRUE;
        g_cancellable_cancel (f->request);
    }
}

static void
cancel_request (GCancellable *cancellable, gpointer data)
{
    Fetch *f = data;

    if (f->request)
        g_cancellable_cancel (f->request);
}

static SoupSession *
fetch_session (void)
{
    static SoupSession *session;

    if (!session)
        session = soup_session_new_with_options ("timeout", 15, "idle-timeout", 30, "user-agent", USER_AGENT, NULL);
    return session;
}

static void
fetch_finish (Fetch *f, GdkPixbuf *icon)
{
    f->done (icon, f->name, g_cancellable_is_cancelled (f->cancellable), f->data);
    if (f->cancel_id)
        g_cancellable_disconnect (f->cancellable, f->cancel_id);
    g_clear_object (&f->request);
    g_clear_object (&f->cancellable);
    g_clear_object (&f->message);
    g_clear_pointer (&f->base, g_uri_unref);
    g_free (f->name);
    g_free (f->manifest);
    g_free (f->buffer);
    g_ptr_array_unref (f->candidates);
    g_free (f);
}

static void
body_read (GObject *source, GAsyncResult *result, gpointer data)
{
    Fetch *f = data;
    gsize length = 0;
    g_autoptr(GBytes) body = NULL;

    if (g_input_stream_read_all_finish (G_INPUT_STREAM (source), result, &length, NULL))
        body = g_bytes_new_take (g_steal_pointer (&f->buffer), length);
    g_clear_pointer (&f->buffer, g_free);
    /* A longer body is cut off here (the page), or refused by the caller (an icon) */
    g_input_stream_close_async (G_INPUT_STREAM (source), G_PRIORITY_LOW, NULL, NULL, NULL);
    f->body_done (f, body);
}

static void
response_received (GObject *source, GAsyncResult *result, gpointer data)
{
    Fetch *f = data;
    g_autoptr(GInputStream) stream = soup_session_send_finish (SOUP_SESSION (source), result, NULL);

    if (!stream || f->refused || !SOUP_STATUS_IS_SUCCESSFUL (soup_message_get_status (f->message))) {
        f->body_done (f, NULL);
        return;
    }
    f->buffer = g_malloc (f->limit);
    g_input_stream_read_all_async (stream, f->buffer, f->limit, G_PRIORITY_LOW, f->cancellable, body_read, f);
}

/* GET @uri, at most @limit bytes; @done gets NULL for any failure */
static void
fetch_body (Fetch *f, GUri *uri, gsize limit, BodyDone done)
{
    g_clear_object (&f->message);
    g_clear_object (&f->request);
    f->message = soup_message_new_from_uri (SOUP_METHOD_GET, uri);
    f->request = g_cancellable_new ();
    f->refused = FALSE;
    f->limit = limit;
    f->body_done = done;
    g_signal_connect (f->message, "network-event", G_CALLBACK (network_event), f);
    if (g_cancellable_is_cancelled (f->cancellable))
        g_cancellable_cancel (f->request);
    soup_session_send_async (fetch_session (), f->message, G_PRIORITY_LOW, f->request, response_received, f);
}

/* --- Reading the page --- */

static char *
decode_entities (const char *text)
{
    g_autofree char *valid = g_utf8_make_valid (text, -1);
    GString *out = g_string_new (NULL);

    for (const char *p = valid; *p;) {
        const char *semi = *p == '&' ? strchr (p, ';') : NULL;

        if (semi && semi - p <= 10) {
            g_autofree char *entity = g_strndup (p + 1, semi - p - 1);
            gunichar c = 0;

            if (entity[0] == '#')
                c = (entity[1] == 'x' || entity[1] == 'X') ? strtoul (entity + 2, NULL, 16) : strtoul (entity + 1, NULL, 10);
            else if (g_str_equal (entity, "amp"))
                c = '&';
            else if (g_str_equal (entity, "lt"))
                c = '<';
            else if (g_str_equal (entity, "gt"))
                c = '>';
            else if (g_str_equal (entity, "quot"))
                c = '"';
            else if (g_str_equal (entity, "apos"))
                c = '\'';
            else if (g_str_equal (entity, "nbsp"))
                c = ' ';
            if (c && g_unichar_validate (c)) {
                g_string_append_unichar (out, c);
                p = semi + 1;
                continue;
            }
        }
        g_string_append_c (out, *p++);
    }
    return g_string_free (out, FALSE);
}

/* Single line, trimmed, at most 60 characters; NULL if nothing is left */
static char *
clean_name (const char *text)
{
    g_autofree char *decoded = NULL;
    g_auto(GStrv) words = NULL;
    g_autofree char *joined = NULL;

    if (!text)
        return NULL;
    decoded = decode_entities (text);
    words = g_strsplit_set (decoded, " \t\r\n", -1);
    for (char **w = words, **out = words; ; w++) {
        if (*w && !**w) {
            g_free (*w);
            continue;
        }
        *out++ = *w;
        if (!*w)
            break;
    }
    joined = g_strjoinv (" ", words);
    if (!*joined)
        return NULL;
    return g_utf8_substring (joined, 0, MIN (g_utf8_strlen (joined, -1), 60));
}

static gboolean
is_svg (const char *href, const char *type)
{
    g_autofree char *path = NULL;

    if (type && strstr (type, "svg"))
        return TRUE;
    path = g_strndup (href, strcspn (href, "?#"));
    return g_str_has_suffix (path, ".svg") || g_str_has_suffix (path, ".SVG");
}

/* "192x192 512x512" → 512, "any" → large, nothing → @fallback */
static int
parse_sizes (const char *sizes, int fallback)
{
    g_auto(GStrv) parts = NULL;
    int best = 0;

    if (!sizes || !*sizes)
        return fallback;
    parts = g_strsplit_set (sizes, " \t\r\n", -1);
    for (char **p = parts; *p; p++) {
        if (g_ascii_strcasecmp (*p, "any") == 0)
            best = MAX (best, 2 * ICON_SIZE);
        else
            best = MAX (best, atoi (*p));
    }
    return best > 0 ? best : fallback;
}

static void
add_candidate (Fetch *f, GUri *base, const char *href, int size)
{
    g_autoptr(GUri) uri = NULL;
    const char *scheme;
    Candidate *c;

    if (!base || !href || !*href || g_str_has_prefix (href, "data:"))
        return;
    uri = g_uri_parse_relative (base, href, SOUP_HTTP_URI_FLAGS, NULL);
    scheme = uri ? g_uri_get_scheme (uri) : NULL;
    if (g_strcmp0 (scheme, "https") != 0 && g_strcmp0 (scheme, "http") != 0)
        return;
    c = g_new0 (Candidate, 1);
    c->url = g_uri_to_string_partial (uri, G_URI_HIDE_FRAGMENT);
    for (guint i = 0; i < f->candidates->len; i++) {
        Candidate *other = g_ptr_array_index (f->candidates, i);

        if (g_str_equal (other->url, c->url)) {
            other->size = MAX (other->size, size);
            candidate_free (c);
            return;
        }
    }
    c->size = size;
    c->order = f->candidates->len;
    g_ptr_array_add (f->candidates, c);
}

/* The largest first; equal sizes in the page's order */
static int
compare_candidates (gconstpointer a, gconstpointer b)
{
    const Candidate *x = *(Candidate * const *) a;
    const Candidate *y = *(Candidate * const *) b;

    if (x->size != y->size)
        return x->size > y->size ? -1 : 1;
    return x->order < y->order ? -1 : (x->order > y->order);
}

/* name="value", name='value', name=value → lower-case name → decoded value */
static GHashTable *
parse_attributes (GRegex *attr_regex, const char *text)
{
    GHashTable *attrs = g_hash_table_new_full (g_str_hash, g_str_equal, g_free, g_free);
    g_autoptr(GMatchInfo) match = NULL;

    g_regex_match (attr_regex, text, 0, &match);
    while (g_match_info_matches (match)) {
        g_autofree char *key = g_match_info_fetch (match, 1);
        g_autofree char *value = NULL;

        for (int i = 2; i <= 4 && !value; i++) {
            int start = -1, end = -1;

            if (g_match_info_fetch_pos (match, i, &start, &end) && start >= 0)
                value = g_match_info_fetch (match, i);
        }
        g_hash_table_replace (attrs, g_ascii_strdown (key, -1), decode_entities (value ? value : ""));
        g_match_info_next (match, NULL);
    }
    return attrs;
}

static gboolean
has_token (const char *list, const char *token)
{
    g_auto(GStrv) tokens = g_strsplit_set (list ? list : "", " \t\r\n", -1);

    for (char **t = tokens; *t; t++)
        if (g_ascii_strcasecmp (*t, token) == 0)
            return TRUE;
    return FALSE;
}

static void
parse_page (Fetch *f, GBytes *page)
{
    static GRegex *tag_regex, *attr_regex, *title_regex;
    gsize length;
    const char *data = g_bytes_get_data (page, &length);
    g_autofree char *text = g_strndup (data, length);
    g_autoptr(GMatchInfo) match = NULL;
    g_autofree char *app_name = NULL, *site_name = NULL, *title = NULL;

    /* RAW: pages need not be UTF-8; what is kept is made valid in decode_entities() */
    if (!tag_regex) {
        tag_regex = g_regex_new ("<(link|meta)\\b([^>]*)>", G_REGEX_CASELESS | G_REGEX_RAW | G_REGEX_OPTIMIZE, 0, NULL);
        attr_regex = g_regex_new ("([a-zA-Z_:-]+)\\s*=\\s*(?:\"([^\"]*)\"|'([^']*)'|([^\\s\"'>]+))",
                                  G_REGEX_RAW | G_REGEX_OPTIMIZE, 0, NULL);
        title_regex = g_regex_new ("<title[^>]*>([^<]*)</title", G_REGEX_CASELESS | G_REGEX_RAW, 0, NULL);
    }

    g_regex_match (tag_regex, text, 0, &match);
    while (g_match_info_matches (match)) {
        g_autofree char *tag = g_match_info_fetch (match, 1);
        g_autofree char *inner = g_match_info_fetch (match, 2);
        g_autoptr(GHashTable) attrs = parse_attributes (attr_regex, inner);

        if (g_ascii_strcasecmp (tag, "link") == 0) {
            const char *rel = g_hash_table_lookup (attrs, "rel");
            const char *href = g_hash_table_lookup (attrs, "href");
            const char *sizes = g_hash_table_lookup (attrs, "sizes");

            if (href && *href) {
                if (has_token (rel, "manifest") && !f->manifest) {
                    g_autoptr(GUri) uri = g_uri_parse_relative (f->base, href, SOUP_HTTP_URI_FLAGS, NULL);

                    if (uri)
                        f->manifest = g_uri_to_string (uri);
                } else if (has_token (rel, "apple-touch-icon") || has_token (rel, "apple-touch-icon-precomposed")) {
                    add_candidate (f, f->base, href, parse_sizes (sizes, 180));
                } else if (has_token (rel, "icon")) {
                    add_candidate (f, f->base, href,
                                   is_svg (href, g_hash_table_lookup (attrs, "type")) ? 2 * ICON_SIZE : parse_sizes (sizes, 32));
                }
            }
        } else {
            const char *key = g_hash_table_lookup (attrs, "name");
            const char *content = g_hash_table_lookup (attrs, "content");

            if (!key)
                key = g_hash_table_lookup (attrs, "property");
            if (key && content) {
                if (!app_name && (g_ascii_strcasecmp (key, "application-name") == 0 ||
                                  g_ascii_strcasecmp (key, "apple-mobile-web-app-title") == 0))
                    app_name = clean_name (content);
                else if (!site_name && g_ascii_strcasecmp (key, "og:site_name") == 0)
                    site_name = clean_name (content);
            }
        }
        g_match_info_next (match, NULL);
    }

    g_clear_pointer (&match, g_match_info_unref);
    if (g_regex_match (title_regex, text, 0, &match)) {
        g_autofree char *raw = g_match_info_fetch (match, 1);

        title = clean_name (raw);
    }
    g_free (f->name);
    f->name = g_strdup (app_name ? app_name : site_name ? site_name : title);
}

/* The web manifest: its short name (what fits under an icon) and its icons */
static void
parse_manifest (Fetch *f, GBytes *body, GUri *manifest_uri)
{
    g_autoptr(JsonParser) parser = json_parser_new ();
    gsize length;
    const char *data = g_bytes_get_data (body, &length);
    JsonNode *root;
    JsonObject *object;
    const char *name;

    if (!json_parser_load_from_data (parser, data, length, NULL))
        return;
    root = json_parser_get_root (parser);
    if (!root || !JSON_NODE_HOLDS_OBJECT (root))
        return;
    object = json_node_get_object (root);
    name = json_object_get_string_member_with_default (object, "short_name", NULL);
    if (!name || !*name)
        name = json_object_get_string_member_with_default (object, "name", NULL);
    if (name && *name) {
        g_autofree char *cleaned = clean_name (name);

        if (cleaned) {
            g_free (f->name);
            f->name = g_steal_pointer (&cleaned);
        }
    }
    if (json_object_has_member (object, "icons") &&
        JSON_NODE_HOLDS_ARRAY (json_object_get_member (object, "icons"))) {
        JsonArray *icons = json_object_get_array_member (object, "icons");

        for (guint i = 0; i < json_array_get_length (icons); i++) {
            JsonNode *node = json_array_get_element (icons, i);
            JsonObject *icon;
            const char *src, *purpose;
            int size;

            if (!JSON_NODE_HOLDS_OBJECT (node))
                continue;
            icon = json_node_get_object (node);
            src = json_object_get_string_member_with_default (icon, "src", NULL);
            purpose = json_object_get_string_member_with_default (icon, "purpose", NULL);
            if (!src)
                continue;
            /* monochrome: a silhouette for notifications, not an app icon */
            if (purpose && !has_token (purpose, "any") && has_token (purpose, "monochrome"))
                continue;
            size = is_svg (src, json_object_get_string_member_with_default (icon, "type", NULL))
                       ? 2 * ICON_SIZE
                       : parse_sizes (json_object_get_string_member_with_default (icon, "sizes", NULL), 144);
            /* maskable only: drawn for a crop with a wide margin, worse than a normal one */
            if (purpose && !has_token (purpose, "any") && has_token (purpose, "maskable"))
                size /= 2;
            add_candidate (f, manifest_uri, src, size);
        }
    }
}

/* --- Loading an icon --- */

typedef struct {
    gboolean from_network;
    gboolean too_large;
} IconLoad;

static void
icon_size_prepared (GdkPixbufLoader *loader, int width, int height, gpointer data)
{
    IconLoad *load = data;
    GdkPixbufFormat *format = gdk_pixbuf_loader_get_format (loader);
    int side = MAX (width, height);
    gboolean scalable = format && gdk_pixbuf_format_is_scalable (format);

    /* Dropped after the header; shrunk anyway, for a loader that decodes it all at once */
    if (load->from_network && !scalable && side > MAX_ICON_SIDE)
        load->too_large = TRUE;

    /* Large images shrink while loading, SVGs are drawn at the stored size */
    if (side > 0 && (side > ICON_SIZE || scalable))
        gdk_pixbuf_loader_set_size (loader, MAX (1, width * ICON_SIZE / side), MAX (1, height * ICON_SIZE / side));
}

/* At most ICON_SIZE, square (a wide logo centered on a transparent square) */
static GdkPixbuf *
square_icon (GdkPixbuf *source)
{
    g_autoptr(GdkPixbuf) scaled = NULL;
    g_autoptr(GdkPixbuf) with_alpha = NULL;
    int width = gdk_pixbuf_get_width (source);
    int height = gdk_pixbuf_get_height (source);
    int side = MAX (width, height);
    GdkPixbuf *square;

    if (side > ICON_SIZE) {
        width = MAX (1, width * ICON_SIZE / side);
        height = MAX (1, height * ICON_SIZE / side);
        side = ICON_SIZE;
        scaled = gdk_pixbuf_scale_simple (source, width, height, GDK_INTERP_HYPER);
        source = scaled;
    }
    if (width == height)
        return g_object_ref (source);
    with_alpha = gdk_pixbuf_add_alpha (source, FALSE, 0, 0, 0);
    square = gdk_pixbuf_new (GDK_COLORSPACE_RGB, TRUE, 8, side, side);
    gdk_pixbuf_fill (square, 0);
    gdk_pixbuf_copy_area (with_alpha, 0, 0, width, height, square, (side - width) / 2, (side - height) / 2);
    return square;
}

/* The loader for an image from the network, by its first bytes: PNG, JPEG, ICO or SVG only, not
 * the other loaders installed (ANI, BMP, TGA, XPM … have had memory errors) */
static const char *
network_icon_type (GBytes *bytes)
{
    gsize size;
    const guint8 *b = g_bytes_get_data (bytes, &size);
    g_autofree char *head = NULL;

    if (size >= 8 && memcmp (b, "\x89PNG\r\n\x1a\n", 8) == 0)
        return "png";
    if (size >= 3 && b[0] == 0xff && b[1] == 0xd8 && b[2] == 0xff)
        return "jpeg";
    if (size >= 4 && b[0] == 0 && b[1] == 0 && b[2] == 1 && b[3] == 0)
        return "ico";
    head = g_strndup ((const char *) b, MIN (size, 1024));
    if (strstr (head, "<svg") && (g_str_has_prefix (g_strchug (head), "<") || g_str_has_prefix (head, "\xef\xbb\xbf")))
        return "svg";
    return NULL;
}

/* @from_network: one of the types of network_icon_type(), at most MAX_ICON_SIDE; an image the
 * user chose may be of any type the loaders know */
static GdkPixbuf *
icon_from_bytes (GBytes *bytes, gboolean from_network)
{
    g_autoptr(GdkPixbufLoader) loader = NULL;
    IconLoad load = { from_network, FALSE };
    gsize size, offset = 0;
    const guint8 *data = g_bytes_get_data (bytes, &size);
    GdkPixbuf *pixbuf;

    if (from_network) {
        const char *type = network_icon_type (bytes);

        if (!type)
            return NULL;
        loader = gdk_pixbuf_loader_new_with_type (type, NULL);
    } else {
        loader = gdk_pixbuf_loader_new ();
    }
    if (!loader)
        return NULL;
    g_signal_connect (loader, "size-prepared", G_CALLBACK (icon_size_prepared), &load);
    while (offset < size) {
        gsize n = MIN (ICON_CHUNK_BYTES, size - offset);

        if (!gdk_pixbuf_loader_write (loader, data + offset, n, NULL) || load.too_large) {
            gdk_pixbuf_loader_close (loader, NULL);
            return NULL;
        }
        offset += n;
    }
    if (!gdk_pixbuf_loader_close (loader, NULL) || load.too_large)
        return NULL;
    pixbuf = gdk_pixbuf_loader_get_pixbuf (loader);
    if (!pixbuf || gdk_pixbuf_get_width (pixbuf) < 16 || gdk_pixbuf_get_height (pixbuf) < 16)
        return NULL;
    return square_icon (pixbuf);
}

/* --- The steps: page → manifest → icons, the largest first --- */

static void fetch_next_icon (Fetch *f);

static void
icon_done (Fetch *f, GBytes *body)
{
    g_autoptr(GdkPixbuf) icon = NULL;

    if (g_cancellable_is_cancelled (f->cancellable)) {
        fetch_finish (f, NULL);
        return;
    }
    /* A full buffer: the file is larger than any icon should be, cut off */
    if (body && g_bytes_get_size (body) < MAX_ICON_BYTES)
        icon = icon_from_bytes (body, TRUE);
    if (icon)
        fetch_finish (f, icon);
    else
        fetch_next_icon (f);
}

static void
fetch_next_icon (Fetch *f)
{
    g_autoptr(GUri) uri = NULL;

    while (!uri && f->next < MIN (f->candidates->len, MAX_ICON_TRIES)) {
        Candidate *c = g_ptr_array_index (f->candidates, f->next++);

        uri = g_uri_parse (c->url, SOUP_HTTP_URI_FLAGS, NULL);
    }
    if (!uri) {
        fetch_finish (f, NULL);
        return;
    }
    fetch_body (f, uri, MAX_ICON_BYTES, icon_done);
}

static void
fetch_icons (Fetch *f)
{
    /* Where many sites have them without saying so */
    add_candidate (f, f->base, "/apple-touch-icon.png", 120);
    add_candidate (f, f->base, "/favicon.ico", 16);
    g_ptr_array_sort (f->candidates, compare_candidates);
    f->next = 0;
    fetch_next_icon (f);
}

static void
manifest_done (Fetch *f, GBytes *body)
{
    if (g_cancellable_is_cancelled (f->cancellable)) {
        fetch_finish (f, NULL);
        return;
    }
    if (body)
        parse_manifest (f, body, soup_message_get_uri (f->message));
    fetch_icons (f);
}

static void
page_done (Fetch *f, GBytes *body)
{
    if (g_cancellable_is_cancelled (f->cancellable)) {
        fetch_finish (f, NULL);
        return;
    }
    /* Relative addresses count from where the redirects ended */
    if (f->message && soup_message_get_uri (f->message)) {
        g_uri_unref (f->base);
        f->base = g_uri_ref (soup_message_get_uri (f->message));
    }
    if (body)
        parse_page (f, body);
    if (f->manifest) {
        g_autoptr(GUri) uri = g_uri_parse (f->manifest, SOUP_HTTP_URI_FLAGS, NULL);

        if (uri && (g_strcmp0 (g_uri_get_scheme (uri), "https") == 0 || g_strcmp0 (g_uri_get_scheme (uri), "http") == 0)) {
            fetch_body (f, uri, MAX_MANIFEST_BYTES, manifest_done);
            return;
        }
    }
    fetch_icons (f);
}

static gboolean
fetch_failed_idle (gpointer data)
{
    fetch_finish (data, NULL);
    return G_SOURCE_REMOVE;
}

/* Looks for the icon and the name of the website at @url (normalized); @done is always called,
 * also when @cancellable is cancelled (then with cancelled = TRUE) */
static void
fetch_start (const char *url, GCancellable *cancellable, FetchDone done, gpointer data)
{
    Fetch *f = g_new0 (Fetch, 1);

    f->cancellable = g_object_ref (cancellable);
    f->candidates = g_ptr_array_new_with_free_func ((GDestroyNotify) candidate_free);
    f->done = done;
    f->data = data;
    f->base = g_uri_parse (url, SOUP_HTTP_URI_FLAGS, NULL);
    if (!f->base) {
        g_idle_add (fetch_failed_idle, f);
        return;
    }
    {
        g_autofree char *key = server_key (f->base);
        gpointer public;

        /* A connection still open from an earlier search connects nowhere: decided by then */
        if (g_hash_table_lookup_extended (known_servers (), key, NULL, &public)) {
            f->decided = TRUE;
            f->public_only = GPOINTER_TO_INT (public);
        }
    }
    f->cancel_id = g_cancellable_connect (f->cancellable, G_CALLBACK (cancel_request), f, NULL);
    fetch_body (f, f->base, MAX_PAGE_BYTES, page_done);
}

/* ----- The tab: the list of web apps ----- */

typedef struct {
    gboolean alive; /* the tab still exists */
    CcPanel *panel;
    GtkWidget *page;
    AdwPreferencesGroup *group;
    GtkWidget *empty_row;
    GPtrArray *rows;
    GPtrArray *apps; /* WebApp, the rows show them */
} WebApps;

static void
webapps_clear (WebApps *w)
{
    g_ptr_array_unref (w->rows);
    g_clear_pointer (&w->apps, g_ptr_array_unref);
}

static WebApps *
webapps_ref (WebApps *w)
{
    return g_rc_box_acquire (w);
}

static void
webapps_unref (WebApps *w)
{
    g_rc_box_release_full (w, (GDestroyNotify) webapps_clear);
}

/* The tab is gone (its page destroyed): pending work only finishes its files */
static void
webapps_detach (WebApps *w)
{
    w->alive = FALSE;
    webapps_unref (w);
}

static void
show_error (GtkWidget *parent, const char *heading, const char *message)
{
    AdwDialog *alert = adw_alert_dialog_new (heading, message);

    adw_alert_dialog_add_response (ADW_ALERT_DIALOG (alert), "close", P_("Close"));
    adw_dialog_present (alert, parent);
}

static GtkWidget *
icon_image (const WebApp *app, int size)
{
    GtkWidget *image = NULL;

    if (app && app->icon) {
        g_autofree char *path = app_path (app->id, app->icon);

        if (g_file_test (path, G_FILE_TEST_IS_REGULAR))
            image = gtk_image_new_from_file (path);
    }
    if (!image)
        image = gtk_image_new_from_icon_name (FALLBACK_ICON);
    gtk_image_set_pixel_size (GTK_IMAGE (image), size);
    gtk_widget_add_css_class (image, "icon-dropshadow");
    return image;
}

static void webapps_refresh (WebApps *w);
static void open_editor (WebApps *w, const WebApp *app);

static void
active_toggled (GtkSwitch *toggle, GParamSpec *pspec, WebApps *w)
{
    WebApp *app = g_object_get_data (G_OBJECT (toggle), "webapp");
    gboolean active = gtk_switch_get_active (toggle);
    g_autoptr(GError) error = NULL;

    if (!app || app->active == active)
        return;
    app->active = active;
    if (webapp_write_config (app, &error) && webapp_sync_desktop_file (app, &error))
        return;
    g_warning ("Could not switch web app %s: %s", app->id, error->message);
    show_error (GTK_WIDGET (toggle), P_("Could Not Change the Web App"), error->message);
    app->active = !active;
    g_signal_handlers_block_by_func (toggle, active_toggled, w);
    gtk_switch_set_active (toggle, !active);
    g_signal_handlers_unblock_by_func (toggle, active_toggled, w);
}

static void
row_activated (AdwActionRow *row, WebApps *w)
{
    WebApp *app = g_object_get_data (G_OBJECT (row), "webapp");

    if (app)
        open_editor (w, app);
}

static GtkWidget *
app_row (WebApps *w, WebApp *app)
{
    GtkWidget *row = adw_action_row_new ();
    GtkWidget *toggle = gtk_switch_new ();
    g_autofree char *host = url_host (app->url);

    adw_preferences_row_set_use_markup (ADW_PREFERENCES_ROW (row), FALSE);
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), app->name);
    adw_action_row_set_subtitle (ADW_ACTION_ROW (row), host);
    adw_action_row_add_prefix (ADW_ACTION_ROW (row), icon_image (app, ROW_ICON_SIZE));

    /* On: an icon among the apps */
    gtk_switch_set_active (GTK_SWITCH (toggle), app->active);
    gtk_widget_set_valign (toggle, GTK_ALIGN_CENTER);
    gtk_widget_set_tooltip_text (toggle, P_("Show among the apps"));
    gtk_accessible_update_property (GTK_ACCESSIBLE (toggle), GTK_ACCESSIBLE_PROPERTY_LABEL, P_("Show among the apps"), -1);
    g_object_set_data (G_OBJECT (toggle), "webapp", app);
    g_signal_connect (toggle, "notify::active", G_CALLBACK (active_toggled), w);
    adw_action_row_add_suffix (ADW_ACTION_ROW (row), toggle);
    adw_action_row_add_suffix (ADW_ACTION_ROW (row), gtk_image_new_from_icon_name ("go-next-symbolic"));

    gtk_list_box_row_set_activatable (GTK_LIST_BOX_ROW (row), TRUE);
    g_object_set_data (G_OBJECT (row), "webapp", app);
    g_signal_connect (row, "activated", G_CALLBACK (row_activated), w);
    return row;
}

static void
webapps_refresh (WebApps *w)
{
    for (guint i = 0; i < w->rows->len; i++)
        adw_preferences_group_remove (w->group, g_ptr_array_index (w->rows, i));
    g_ptr_array_set_size (w->rows, 0);
    g_clear_pointer (&w->apps, g_ptr_array_unref);

    w->apps = webapps_load_all ();
    for (guint i = 0; i < w->apps->len; i++) {
        WebApp *app = g_ptr_array_index (w->apps, i);
        g_autofree char *ua = identity_user_agent (identity_by_id (app->identity), engine_by_id (app->browser));

        /* The browser has a new version (or the web app was written without its User-Agent) */
        if (g_strcmp0 (ua, app->user_agent) && !webapp_running (app)) {
            g_autoptr(GError) error = NULL;

            if (!webapp_save (app, NULL, &error))
                g_warning ("Could not update web app %s: %s", app->id, error->message);
        }
    }
    for (guint i = 0; i < w->apps->len; i++) {
        GtkWidget *row = app_row (w, g_ptr_array_index (w->apps, i));

        adw_preferences_group_add (w->group, row);
        g_ptr_array_add (w->rows, row);
    }
    gtk_widget_set_visible (w->empty_row, w->apps->len == 0);
}

/* ----- The page of one web app (also for adding one) ----- */

typedef struct {
    WebApps *w;   /* reference */
    WebApp *app;  /* a copy; a new one has no id yet */
    gboolean is_new;
    AdwNavigationPage *page;
    GtkImage *icon_image;
    GtkWidget *spinner;
    GtkLabel *icon_status;
    AdwEntryRow *url_row;
    AdwEntryRow *name_row;
    AdwComboRow *browser_row; /* NULL with only one browser to choose */
    AdwComboRow *identity_row;
    const Engine *row_engines[G_N_ELEMENTS (engines)];
    AdwSwitchRow *frameless_row;
    AdwSwitchRow *cache_row;
    AdwSwitchRow *cookies_row;
    GtkWidget *save_button;
    GdkPixbuf *icon;        /* found or chosen, saved with the web app; NULL = keep the old one */
    gboolean icon_from_file; /* chosen: typing an address no longer replaces it */
    char *auto_name;        /* what the website called itself and we filled in */
    guint fetch_timeout;
    GCancellable *fetch_cancellable;
    AdwNavigationPage *origin; /* weak: the search it was opened from, saving goes back past it */
} Editor;

static void
editor_free (Editor *e)
{
    g_clear_handle_id (&e->fetch_timeout, g_source_remove);
    if (e->fetch_cancellable)
        g_cancellable_cancel (e->fetch_cancellable);
    g_clear_object (&e->fetch_cancellable);
    g_clear_object (&e->icon);
    g_free (e->auto_name);
    if (e->origin)
        g_object_remove_weak_pointer (G_OBJECT (e->origin), (gpointer *) &e->origin);
    webapp_free (e->app);
    webapps_unref (e->w);
    g_free (e);
}

/* Back from the page: past the search to the list */
static void
editor_done (Editor *e)
{
    GtkWidget *nav = gtk_widget_get_ancestor (GTK_WIDGET (e->page), ADW_TYPE_NAVIGATION_VIEW);
    AdwNavigationPage *before = NULL;

    if (e->origin && nav)
        before = adw_navigation_view_get_previous_page (ADW_NAVIGATION_VIEW (nav), e->origin);
    if (before)
        adw_navigation_view_pop_to_page (ADW_NAVIGATION_VIEW (nav), before);
    else
        gtk_widget_activate_action (GTK_WIDGET (e->page), "navigation.pop", NULL);
}

static void
editor_update (Editor *e)
{
    g_autofree char *url = normalize_url (gtk_editable_get_text (GTK_EDITABLE (e->url_row)));

    gtk_widget_set_sensitive (e->save_button, url != NULL);
}

static void
editor_status (Editor *e, gboolean busy, const char *text)
{
    gtk_widget_set_visible (e->spinner, busy);
    gtk_label_set_text (e->icon_status, text ? text : "");
    gtk_widget_set_visible (GTK_WIDGET (e->icon_status), text != NULL);
}

static void
editor_set_icon (Editor *e, GdkPixbuf *icon)
{
    g_autoptr(GdkTexture) texture = gdk_texture_new_for_pixbuf (icon);

    g_set_object (&e->icon, icon);
    gtk_image_set_from_paintable (e->icon_image, GDK_PAINTABLE (texture));
}

static void
editor_fetched (GdkPixbuf *icon, const char *name, gboolean cancelled, gpointer data)
{
    Editor *e = data;
    const char *current;

    if (cancelled)
        return;
    if (icon)
        editor_set_icon (e, icon);
    editor_status (e, FALSE, icon ? NULL : P_("No icon found on the website"));

    /* The website's name for itself, unless one was typed */
    current = gtk_editable_get_text (GTK_EDITABLE (e->name_row));
    if (name && *name && (!*current || g_strcmp0 (current, e->auto_name) == 0)) {
        g_free (e->auto_name);
        e->auto_name = g_strdup (name);
        gtk_editable_set_text (GTK_EDITABLE (e->name_row), name);
    }
}

static void
editor_fetch (Editor *e)
{
    g_autofree char *url = normalize_url (gtk_editable_get_text (GTK_EDITABLE (e->url_row)));

    g_clear_handle_id (&e->fetch_timeout, g_source_remove);
    if (e->fetch_cancellable)
        g_cancellable_cancel (e->fetch_cancellable);
    g_clear_object (&e->fetch_cancellable);
    if (!url) {
        editor_status (e, FALSE, NULL);
        return;
    }
    e->fetch_cancellable = g_cancellable_new ();
    editor_status (e, TRUE, P_("Looking for the website's icon…"));
    fetch_start (url, e->fetch_cancellable, editor_fetched, e);
}

static gboolean
fetch_timeout_cb (gpointer data)
{
    Editor *e = data;

    e->fetch_timeout = 0;
    editor_fetch (e);
    return G_SOURCE_REMOVE;
}

/* Typing an address: look for the icon once typing pauses, unless the icon was chosen or the
 * web app already has one ("From Website" fetches anyway) */
static void
url_changed (Editor *e)
{
    g_autofree char *url = normalize_url (gtk_editable_get_text (GTK_EDITABLE (e->url_row)));

    editor_update (e);
    g_clear_handle_id (&e->fetch_timeout, g_source_remove);
    if (!url || e->icon_from_file || (e->app->icon && !e->icon))
        return;
    e->fetch_timeout = g_timeout_add (FETCH_DELAY_MS, fetch_timeout_cb, e);
}

static void
from_website_clicked (Editor *e)
{
    e->icon_from_file = FALSE;
    editor_fetch (e);
}

static void
image_chosen (GObject *source, GAsyncResult *result, gpointer data)
{
    g_autoptr(AdwNavigationPage) page = data;
    Editor *e = g_object_get_data (G_OBJECT (page), EDITOR_KEY);
    g_autoptr(GFile) file = gtk_file_dialog_open_finish (GTK_FILE_DIALOG (source), result, NULL);
    g_autoptr(GBytes) bytes = NULL;
    g_autoptr(GdkPixbuf) icon = NULL;

    if (!file || !e)
        return;
    bytes = g_file_load_bytes (file, NULL, NULL, NULL);
    if (bytes && g_bytes_get_size (bytes) <= MAX_ICON_BYTES)
        icon = icon_from_bytes (bytes, FALSE);
    if (!icon) {
        editor_status (e, FALSE, P_("This file is not an image"));
        return;
    }
    /* The chosen image wins over a search still running */
    g_clear_handle_id (&e->fetch_timeout, g_source_remove);
    if (e->fetch_cancellable)
        g_cancellable_cancel (e->fetch_cancellable);
    e->icon_from_file = TRUE;
    editor_set_icon (e, icon);
    editor_status (e, FALSE, NULL);
}

static void
choose_image_clicked (Editor *e)
{
    g_autoptr(GtkFileDialog) dialog = gtk_file_dialog_new ();
    g_autoptr(GtkFileFilter) filter = gtk_file_filter_new ();
    g_autoptr(GListStore) filters = g_list_store_new (GTK_TYPE_FILE_FILTER);
    GtkRoot *root = gtk_widget_get_root (GTK_WIDGET (e->page));
    g_autofree char *pictures = g_strdup (g_get_user_special_dir (G_USER_DIRECTORY_PICTURES));
    g_autoptr(GFile) folder = g_file_new_for_path (pictures && g_file_test (pictures, G_FILE_TEST_IS_DIR) ? pictures : g_get_home_dir ());

    gtk_file_filter_set_name (filter, P_("Images"));
    gtk_file_filter_add_pixbuf_formats (filter);
    g_list_store_append (filters, filter);
    gtk_file_dialog_set_filters (dialog, G_LIST_MODEL (filters));
    gtk_file_dialog_set_default_filter (dialog, filter);
    gtk_file_dialog_set_title (dialog, P_("Choose an Icon"));
    gtk_file_dialog_set_initial_folder (dialog, folder);
    /* The page (and with it the editor) lives until the answer */
    gtk_file_dialog_open (dialog, GTK_IS_WINDOW (root) ? GTK_WINDOW (root) : NULL, NULL, image_chosen,
                          g_object_ref (e->page));
}

/* --- Saving, and the icon looked for afterwards --- */

typedef struct {
    WebApps *w;
    char *id;
} IconJob;

/* A web app saved without an icon gets the website's as soon as it is found */
static void
late_icon_fetched (GdkPixbuf *icon, const char *name, gboolean cancelled, gpointer data)
{
    IconJob *job = data;

    if (icon && !cancelled) {
        g_autoptr(WebApp) app = webapp_load (job->id);
        g_autoptr(GError) error = NULL;

        if (app && !app->icon && !webapp_save (app, icon, &error))
            g_warning ("Could not store the icon of web app %s: %s", job->id, error->message);
        if (app && job->w->alive)
            webapps_refresh (job->w);
    }
    webapps_unref (job->w);
    g_free (job->id);
    g_free (job);
}

/* --- Resetting the website data --- */

typedef struct {
    WebApps *w;
    char *name;
} SiteDataJob;

static void
site_data_cleared (GObject *source, GAsyncResult *result, gpointer data)
{
    SiteDataJob *job = data;
    g_autoptr(GError) error = NULL;

    if (!g_task_propagate_boolean (G_TASK (result), &error)) {
        g_warning ("Could not reset the website data of web app %s: %s", job->name, error->message);
        if (job->w->alive)
            show_error (job->w->page, P_("Could Not Reset the Website Data"), error->message);
    }
    webapps_unref (job->w);
    g_free (job->name);
    g_free (job);
}

/* In a thread: a profile can hold many files */
static void
clear_site_data_start (WebApps *w, const WebApp *app)
{
    SiteDataJob *job = g_new0 (SiteDataJob, 1);
    g_autoptr(GTask) task = NULL;

    job->w = webapps_ref (w);
    job->name = g_strdup (app->name);
    task = g_task_new (NULL, NULL, site_data_cleared, job);
    g_task_set_task_data (task, g_strdup (app->id), g_free);
    g_task_run_in_thread (task, clear_site_data_thread);
}

static void
reset_response (AdwAlertDialog *dialog, const char *response, gpointer data)
{
    AdwNavigationPage *page = data; /* the signal's reference, dropped with the dialog */
    Editor *e = g_object_get_data (G_OBJECT (page), EDITOR_KEY);

    if (!e || g_strcmp0 (response, "reset") != 0)
        return;
    /* Opened again while the question was shown */
    if (webapp_running (e->app)) {
        g_autofree char *heading = g_strdup_printf (P_("“%s” Is Open"), e->app->name);

        show_error (GTK_WIDGET (e->page), heading, P_("Close the web app first, then reset its website data."));
        return;
    }
    clear_site_data_start (e->w, e->app);
}

static void
reset_clicked (Editor *e)
{
    AdwDialog *alert;

    if (webapp_running (e->app)) {
        g_autofree char *heading = g_strdup_printf (P_("“%s” Is Open"), e->app->name);

        show_error (GTK_WIDGET (e->page), heading, P_("Close the web app first, then reset its website data."));
        return;
    }
    alert = adw_alert_dialog_new (P_("Reset Website Data?"), NULL);
    adw_alert_dialog_format_body (ADW_ALERT_DIALOG (alert),
                                  P_("“%s” forgets what the website stored on this phone, except cookies: you stay "
                                     "signed in. This helps when the website keeps failing, for example after "
                                     "Identify As changed."),
                                  e->app->name);
    adw_alert_dialog_add_response (ADW_ALERT_DIALOG (alert), "cancel", P_("Cancel"));
    adw_alert_dialog_add_response (ADW_ALERT_DIALOG (alert), "reset", P_("Reset"));
    adw_alert_dialog_set_response_appearance (ADW_ALERT_DIALOG (alert), "reset", ADW_RESPONSE_DESTRUCTIVE);
    adw_alert_dialog_set_default_response (ADW_ALERT_DIALOG (alert), "cancel");
    adw_alert_dialog_set_close_response (ADW_ALERT_DIALOG (alert), "cancel");
    g_signal_connect_data (alert, "response", G_CALLBACK (reset_response), g_object_ref (e->page),
                           (GClosureNotify) (void (*) (void)) g_object_unref, 0);
    adw_dialog_present (alert, GTK_WIDGET (e->page));
}

static void
save_clicked (Editor *e)
{
    g_autofree char *url = normalize_url (gtk_editable_get_text (GTK_EDITABLE (e->url_row)));
    g_autofree char *name = g_strstrip (g_strdup (gtk_editable_get_text (GTK_EDITABLE (e->name_row))));
    g_autoptr(GError) error = NULL;
    WebApp *app = e->app;
    const char *identity = identities[adw_combo_row_get_selected (e->identity_row)].id;
    gboolean identity_changed = !e->is_new && g_strcmp0 (identity_by_id (app->identity)->id, identity) != 0;

    if (!url)
        return;
    /* Another identity resets the website data (below), which must not go from under a running browser */
    if (identity_changed && webapp_running (app)) {
        g_autofree char *heading = g_strdup_printf (P_("“%s” Is Open"), app->name);

        show_error (GTK_WIDGET (e->page), heading, P_("Close the web app first, then change Identify As."));
        return;
    }
    g_free (app->url);
    app->url = g_steal_pointer (&url);
    g_free (app->name);
    app->name = *name ? g_steal_pointer (&name) : url_host (app->url);
    if (e->browser_row) {
        g_free (app->browser);
        app->browser = g_strdup (e->row_engines[adw_combo_row_get_selected (e->browser_row)]->id);
    }
    g_free (app->identity);
    app->identity = g_strdup (identity);
    app->frameless = adw_switch_row_get_active (e->frameless_row);
    app->clear_cache = adw_switch_row_get_active (e->cache_row);
    app->clear_cookies = adw_switch_row_get_active (e->cookies_row);
    if (e->is_new) {
        app->id = new_id ();
        app->active = TRUE;
    } else {
        /* The switch in the list is the one to follow, not the state when the page opened */
        g_autoptr(WebApp) stored = webapp_load (app->id);

        if (stored)
            app->active = stored->active;
    }

    if (!webapp_save (app, e->icon, &error)) {
        g_warning ("Could not save web app %s: %s", app->id, error->message);
        show_error (GTK_WIDGET (e->page), P_("Could Not Save the Web App"), error->message);
        if (e->is_new)
            g_clear_pointer (&app->id, g_free);
        return;
    }
    g_clear_object (&e->icon);

    /* What the website stored about the old identity would contradict the new one; cookies stay */
    if (identity_changed)
        clear_site_data_start (e->w, app);

    /* Still without an icon (saved before the search ended, or nothing found yet): keep looking */
    if (!app->icon) {
        IconJob *job = g_new0 (IconJob, 1);
        g_autoptr(GCancellable) cancellable = g_cancellable_new ();

        job->w = webapps_ref (e->w);
        job->id = g_strdup (app->id);
        fetch_start (app->url, cancellable, late_icon_fetched, job);
    }
    if (e->w->alive)
        webapps_refresh (e->w);
    editor_done (e);
}

/* --- Deleting --- */

typedef struct {
    WebApps *w;
    char *name;
} DeleteJob;

static void
delete_done (GObject *source, GAsyncResult *result, gpointer data)
{
    DeleteJob *job = data;
    g_autoptr(GError) error = NULL;

    if (!g_task_propagate_boolean (G_TASK (result), &error))
        g_warning ("Could not delete web app %s: %s", job->name, error->message);
    if (job->w->alive) {
        webapps_refresh (job->w);
        if (error)
            show_error (job->w->page, P_("Could Not Delete the Web App"), error->message);
    }
    webapps_unref (job->w);
    g_free (job->name);
    g_free (job);
}

static void
delete_response (AdwAlertDialog *dialog, const char *response, gpointer data)
{
    AdwNavigationPage *page = data; /* the signal's reference, dropped with the dialog */
    Editor *e = g_object_get_data (G_OBJECT (page), EDITOR_KEY);
    g_autoptr(GTask) task = NULL;
    DeleteJob *job;

    if (!e || g_strcmp0 (response, "delete") != 0)
        return;
    job = g_new0 (DeleteJob, 1);
    job->w = webapps_ref (e->w);
    job->name = g_strdup (e->app->name);
    task = g_task_new (NULL, NULL, delete_done, job);
    g_task_set_task_data (task, g_strdup (e->app->id), g_free);
    g_task_run_in_thread (task, delete_thread);
    gtk_widget_activate_action (GTK_WIDGET (page), "navigation.pop", NULL);
}

static void
delete_clicked (Editor *e)
{
    AdwDialog *alert;

    /* Its profile is in use: deleting it under Firefox would leave a broken web app running */
    if (webapp_running (e->app)) {
        g_autofree char *heading = g_strdup_printf (P_("“%s” Is Open"), e->app->name);

        show_error (GTK_WIDGET (e->page), heading, P_("Close the web app first, then delete it."));
        return;
    }
    alert = adw_alert_dialog_new (P_("Delete Web App?"), NULL);
    adw_alert_dialog_format_body (ADW_ALERT_DIALOG (alert),
                                  P_("“%s” is removed with its profile: logins, cookies and all other data "
                                     "the website stored on this phone."),
                                  e->app->name);
    adw_alert_dialog_add_response (ADW_ALERT_DIALOG (alert), "cancel", P_("Cancel"));
    adw_alert_dialog_add_response (ADW_ALERT_DIALOG (alert), "delete", P_("Delete"));
    adw_alert_dialog_set_response_appearance (ADW_ALERT_DIALOG (alert), "delete", ADW_RESPONSE_DESTRUCTIVE);
    adw_alert_dialog_set_default_response (ADW_ALERT_DIALOG (alert), "cancel");
    adw_alert_dialog_set_close_response (ADW_ALERT_DIALOG (alert), "cancel");
    g_signal_connect_data (alert, "response", G_CALLBACK (delete_response), g_object_ref (e->page),
                           (GClosureNotify) (void (*) (void)) g_object_unref, 0);
    adw_dialog_present (alert, GTK_WIDGET (e->page));
}

/* --- Building the page --- */

static AdwEntryRow *
entry_row (AdwPreferencesGroup *group, const char *title, const char *text)
{
    AdwEntryRow *row = ADW_ENTRY_ROW (adw_entry_row_new ());

    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), title);
    gtk_editable_set_text (GTK_EDITABLE (row), text ? text : "");
    adw_preferences_group_add (group, GTK_WIDGET (row));
    return row;
}

static AdwSwitchRow *
switch_row (AdwPreferencesGroup *group, const char *title, const char *subtitle, gboolean active)
{
    AdwSwitchRow *row = ADW_SWITCH_ROW (adw_switch_row_new ());

    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), title);
    adw_action_row_set_subtitle (ADW_ACTION_ROW (row), subtitle);
    adw_switch_row_set_active (row, active);
    adw_preferences_group_add (group, GTK_WIDGET (row));
    return row;
}

static AdwPreferencesGroup *
add_group (GtkWidget *page, const char *title, const char *description)
{
    AdwPreferencesGroup *group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());

    if (title)
        adw_preferences_group_set_title (group, title);
    if (description)
        adw_preferences_group_set_description (group, description);
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (page), group);
    return group;
}

static GtkWidget *
pill_button (const char *label, const char *style)
{
    GtkWidget *button = gtk_button_new_with_label (label);

    gtk_widget_set_halign (button, GTK_ALIGN_CENTER);
    gtk_widget_add_css_class (button, "pill");
    if (style)
        gtk_widget_add_css_class (button, style);
    return button;
}

/* Browser: only with more than one to choose (installed ones, and the web app's own) */
static void
add_browser_row (Editor *e, AdwPreferencesGroup *group)
{
    const Engine *current = engine_by_id (e->app->browser);
    g_autoptr(GtkStringList) labels = gtk_string_list_new (NULL);
    guint n = 0, selected = 0;

    for (gsize i = 0; i < G_N_ELEMENTS (engines); i++) {
        if (&engines[i] != current && !engine_command (&engines[i]))
            continue;
        if (&engines[i] == current)
            selected = n;
        e->row_engines[n++] = &engines[i];
        gtk_string_list_append (labels, engines[i].label);
    }
    if (n < 2)
        return;
    e->browser_row = ADW_COMBO_ROW (adw_combo_row_new ());
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (e->browser_row), P_("Browser"));
    adw_combo_row_set_model (e->browser_row, G_LIST_MODEL (labels));
    adw_combo_row_set_selected (e->browser_row, selected);
    adw_preferences_group_add (group, GTK_WIDGET (e->browser_row));
}

/* Identify As: the browser's own, or a desktop or mobile browser */
static void
add_identity_row (Editor *e, AdwPreferencesGroup *group)
{
    g_autoptr(GtkStringList) labels = gtk_string_list_new (NULL);
    const Identity *current = identity_by_id (e->app->identity);
    guint selected = 0;

    for (gsize i = 0; i < G_N_ELEMENTS (identities); i++) {
        gtk_string_list_append (labels, P_(identities[i].label));
        if (&identities[i] == current)
            selected = i;
    }
    e->identity_row = ADW_COMBO_ROW (adw_combo_row_new ());
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (e->identity_row), P_("Identify As"));
    adw_action_row_set_subtitle (ADW_ACTION_ROW (e->identity_row), P_("How the browser presents itself to websites"));
    adw_combo_row_set_model (e->identity_row, G_LIST_MODEL (labels));
    adw_combo_row_set_selected (e->identity_row, selected);
    adw_preferences_group_add (group, GTK_WIDGET (e->identity_row));
}

/* The page of @app, or of a new web app: empty, or filled in from @template (a catalog entry,
 * without id) with the icon file @icon. @origin: the search it was opened from when adding */
static void
open_editor_full (WebApps *w, const WebApp *app, const WebApp *template, const char *icon,
                  AdwNavigationPage *origin)
{
    Editor *e = g_new0 (Editor, 1);
    GtkWidget *toolbar, *header, *prefs, *box, *status, *buttons, *button;
    AdwPreferencesGroup *group;

    e->w = webapps_ref (w);
    e->is_new = app == NULL;
    if (app) {
        e->app = webapp_copy (app);
    } else if (template) {
        e->app = webapp_copy (template);
        g_clear_pointer (&e->app->id, g_free);
        g_free (e->app->browser);
        e->app->browser = g_strdup (default_engine ()->id);
    } else {
        e->app = g_new0 (WebApp, 1);
        e->app->browser = g_strdup (default_engine ()->id);
        e->app->frameless = TRUE;
    }
    if (origin) {
        e->origin = origin;
        g_object_add_weak_pointer (G_OBJECT (origin), (gpointer *) &e->origin);
    }

    prefs = adw_preferences_page_new ();

    /* The icon, and where it comes from */
    group = add_group (prefs, NULL, NULL);
    box = gtk_box_new (GTK_ORIENTATION_VERTICAL, 12);
    e->icon_image = GTK_IMAGE (icon_image (app, PREVIEW_SIZE));
    gtk_widget_set_halign (GTK_WIDGET (e->icon_image), GTK_ALIGN_CENTER);
    gtk_box_append (GTK_BOX (box), GTK_WIDGET (e->icon_image));
    status = gtk_box_new (GTK_ORIENTATION_HORIZONTAL, 6);
    gtk_widget_set_halign (status, GTK_ALIGN_CENTER);
    e->spinner = adw_spinner_new ();
    gtk_widget_set_visible (e->spinner, FALSE);
    gtk_box_append (GTK_BOX (status), e->spinner);
    e->icon_status = GTK_LABEL (gtk_label_new (NULL));
    gtk_label_set_wrap (e->icon_status, TRUE);
    gtk_label_set_justify (e->icon_status, GTK_JUSTIFY_CENTER);
    gtk_widget_add_css_class (GTK_WIDGET (e->icon_status), "dim-label");
    gtk_widget_add_css_class (GTK_WIDGET (e->icon_status), "caption");
    gtk_widget_set_visible (GTK_WIDGET (e->icon_status), FALSE);
    gtk_box_append (GTK_BOX (status), GTK_WIDGET (e->icon_status));
    gtk_box_append (GTK_BOX (box), status);
    buttons = gtk_box_new (GTK_ORIENTATION_HORIZONTAL, 12);
    gtk_widget_set_halign (buttons, GTK_ALIGN_CENTER);
    button = gtk_button_new_with_label (P_("From Website"));
    g_signal_connect_swapped (button, "clicked", G_CALLBACK (from_website_clicked), e);
    gtk_box_append (GTK_BOX (buttons), button);
    button = gtk_button_new_with_label (P_("Choose Image…"));
    g_signal_connect_swapped (button, "clicked", G_CALLBACK (choose_image_clicked), e);
    gtk_box_append (GTK_BOX (buttons), button);
    gtk_box_append (GTK_BOX (box), buttons);
    adw_preferences_group_add (group, box);

    /* Address and name */
    group = add_group (prefs, NULL,
                       P_("Each web app has its own browser profile: logins and data stay apart from the "
                          "browser and from other web apps."));
    e->url_row = entry_row (group, P_("Address"), e->app->url);
    adw_entry_row_set_input_purpose (e->url_row, GTK_INPUT_PURPOSE_URL);
    e->name_row = entry_row (group, P_("Name"), e->app->name);

    /* What the web app runs in, above Frameless */
    group = add_group (prefs, NULL, NULL);
    add_browser_row (e, group);
    add_identity_row (e, group);
    e->frameless_row = switch_row (group, P_("Frameless"), P_("Without address bar, tabs and menu"), e->app->frameless);

    group = add_group (prefs, P_("When Closing"), NULL);
    e->cache_row = switch_row (group, P_("Delete Cache"), P_("Pages and images stored for a faster start"),
                               e->app->clear_cache);
    e->cookies_row = switch_row (group, P_("Delete Cookies"), P_("Also other site data; signing in again each time"),
                                 e->app->clear_cookies);

    /* Reset Website Data and Delete below the form */
    if (!e->is_new) {
        group = add_group (prefs, NULL, NULL);
        box = gtk_box_new (GTK_ORIENTATION_VERTICAL, 12);
        button = pill_button (P_("Reset Website Data"), NULL);
        g_signal_connect_swapped (button, "clicked", G_CALLBACK (reset_clicked), e);
        gtk_box_append (GTK_BOX (box), button);
        button = pill_button (P_("Delete"), "destructive-action");
        g_signal_connect_swapped (button, "clicked", G_CALLBACK (delete_clicked), e);
        gtk_box_append (GTK_BOX (box), button);
        adw_preferences_group_add (group, box);
    }

    /* Add or Save at the top right, like the other pages that were dialogs */
    header = adw_header_bar_new ();
    e->save_button = gtk_button_new_with_label (e->is_new ? P_("Add") : P_("Save"));
    gtk_widget_add_css_class (e->save_button, "suggested-action");
    g_signal_connect_swapped (e->save_button, "clicked", G_CALLBACK (save_clicked), e);
    adw_header_bar_pack_end (ADW_HEADER_BAR (header), e->save_button);

    toolbar = adw_toolbar_view_new ();
    adw_toolbar_view_add_top_bar (ADW_TOOLBAR_VIEW (toolbar), header);
    adw_toolbar_view_set_content (ADW_TOOLBAR_VIEW (toolbar), prefs);
    e->page = adw_navigation_page_new_with_tag (toolbar, e->is_new ? P_("Add Web App") : app->name, "plinius-webapp");
    g_object_set_data_full (G_OBJECT (e->page), EDITOR_KEY, e, (GDestroyNotify) editor_free);

    g_signal_connect_swapped (e->url_row, "changed", G_CALLBACK (url_changed), e);
    g_signal_connect_swapped (e->url_row, "entry-activated", G_CALLBACK (editor_fetch), e);
    editor_update (e);
    cc_panel_push_subpage (w->panel, e->page);
    /* From the catalog: its icon, else the website's looked for at once */
    if (template) {
        g_autoptr(GdkPixbuf) pixbuf = icon && *icon ? gdk_pixbuf_new_from_file (icon, NULL) : NULL;

        /* Kept like a chosen image: a changed address does not replace it, From Website does */
        if (pixbuf) {
            editor_set_icon (e, pixbuf);
            e->icon_from_file = TRUE;
        } else {
            editor_fetch (e);
        }
    }
}

static void
open_editor (WebApps *w, const WebApp *app)
{
    open_editor_full (w, app, NULL, NULL, NULL);
}

/* ----- Adding: the search in the catalog, or Manually ----- */

typedef struct {
    WebApp app;      /* name, address and starting settings; no id */
    char *category;
    char *icon;      /* the icon's file, checked by the helper; NULL = none */
    char *haystack;  /* name, host and keywords: what the search looks in */
} CatalogEntry;

static void
catalog_entry_free (CatalogEntry *c)
{
    g_free (c->app.name);
    g_free (c->app.url);
    g_free (c->app.identity);
    g_free (c->category);
    g_free (c->icon);
    g_free (c->haystack);
    g_free (c);
}

/* The groups in the order the helper sorts them; the search also finds an entry by its group's
 * title and words, translated and in English */
typedef struct {
    const char *id;
    const char *title; /* translated with P_() */
    const char *words; /* translated with P_(): words one searches for the group by */
    const char *icon;
} Category;

static const Category categories[] = {
    { "communication", N_("Communication"), N_("mail email chat messenger calls"), "mail-unread-symbolic" },
    { "social", N_("Social Networks"), N_("social network community friends photos"), "emoji-people-symbolic" },
    { "news", N_("News"), N_("news newspaper magazine headlines"), "x-office-document-symbolic" },
    { "media", N_("Video & Music"), N_("video music streaming tv films series podcasts radio"),
      "applications-multimedia-symbolic" },
    { "games", N_("Games"), N_("games gaming play chess puzzle"), "applications-games-symbolic" },
    { "shopping", N_("Shopping"), N_("shopping shop store buy marketplace classifieds"), "emoji-objects-symbolic" },
    { "travel", N_("Travel & Maps"), N_("travel maps train bus flight hotel tickets navigation"),
      "emoji-travel-symbolic" },
    { "office", N_("Knowledge & Tools"),
      N_("tools office documents translate dictionary encyclopedia ai calendar cloud"),
      "applications-utilities-symbolic" },
    { "finance", N_("Banks & Payments"), N_("bank banking money payments account transfer finance"),
      "auth-smartcard-symbolic" },
};

static const Category *
category_by_id (const char *id)
{
    for (gsize i = 0; i < G_N_ELEMENTS (categories); i++)
        if (g_strcmp0 (categories[i].id, id) == 0)
            return &categories[i];
    return NULL;
}

static const char *
category_title (const char *id)
{
    const Category *category = category_by_id (id);

    return category ? P_(category->title) : NULL;
}

static const char *
member_string (JsonObject *o, const char *name)
{
    JsonNode *node = json_object_get_member (o, name);

    return node && JSON_NODE_HOLDS_VALUE (node) && json_node_get_value_type (node) == G_TYPE_STRING
        ? json_node_get_string (node) : NULL;
}

static gboolean
member_bool (JsonObject *o, const char *name, gboolean fallback)
{
    JsonNode *node = json_object_get_member (o, name);

    return node && JSON_NODE_HOLDS_VALUE (node) && json_node_get_value_type (node) == G_TYPE_BOOLEAN
        ? json_node_get_boolean (node) : fallback;
}

/* The helper's answer: its entries (CatalogEntry) and the catalog's time; NULL if unusable.
 * Entries without name, a web address or a known group are left out. */
static GPtrArray *
catalog_parse (const char *text, gint64 *updated)
{
    g_autoptr(JsonParser) parser = json_parser_new ();
    g_autoptr(GPtrArray) entries = g_ptr_array_new_with_free_func ((GDestroyNotify) catalog_entry_free);
    JsonNode *root;
    JsonObject *top;
    JsonNode *apps, *node;

    if (!text || !json_parser_load_from_data (parser, text, -1, NULL))
        return NULL;
    root = json_parser_get_root (parser);
    if (!root || !JSON_NODE_HOLDS_OBJECT (root))
        return NULL;
    top = json_node_get_object (root);
    apps = json_object_get_member (top, "apps");
    if (!apps || !JSON_NODE_HOLDS_ARRAY (apps))
        return NULL;
    node = json_object_get_member (top, "updated");
    *updated = node && JSON_NODE_HOLDS_VALUE (node) && json_node_get_value_type (node) == G_TYPE_INT64
        ? json_node_get_int (node) : 0;

    for (guint i = 0; i < json_array_get_length (json_node_get_array (apps)); i++) {
        JsonObject *o;
        JsonNode *keywords;
        const char *name, *url, *category, *identity, *icon;
        g_autofree char *normalized = NULL, *host = NULL;
        g_autoptr(GString) haystack = NULL;
        CatalogEntry *c;

        node = json_array_get_element (json_node_get_array (apps), i);
        if (!JSON_NODE_HOLDS_OBJECT (node))
            continue;
        o = json_node_get_object (node);
        name = member_string (o, "name");
        url = member_string (o, "url");
        category = member_string (o, "category");
        normalized = url ? normalize_url (url) : NULL;
        if (!name || !*name || !normalized || !category_title (category))
            continue;
        identity = member_string (o, "identify_as");
        icon = member_string (o, "icon");
        host = url_host (normalized);

        c = g_new0 (CatalogEntry, 1);
        c->app.name = g_strdup (name);
        c->app.url = g_steal_pointer (&normalized);
        c->app.frameless = member_bool (o, "frameless", TRUE);
        c->app.clear_cache = member_bool (o, "clear_cache", FALSE);
        c->app.clear_cookies = member_bool (o, "clear_cookies", FALSE);
        c->app.identity = g_strdup (identity_by_id (identity)->id);
        c->category = g_strdup (category);
        c->icon = icon && g_path_is_absolute (icon) ? g_strdup (icon) : NULL;
        haystack = g_string_new (name);
        g_string_append_printf (haystack, " %s", host);
        keywords = json_object_get_member (o, "keywords");
        if (keywords && JSON_NODE_HOLDS_ARRAY (keywords)) {
            JsonArray *list = json_node_get_array (keywords);

            for (guint k = 0; k < json_array_get_length (list); k++) {
                JsonNode *kw = json_array_get_element (list, k);

                if (JSON_NODE_HOLDS_VALUE (kw) && json_node_get_value_type (kw) == G_TYPE_STRING)
                    g_string_append_printf (haystack, " %s", json_node_get_string (kw));
            }
        }
        {
            const Category *group = category_by_id (category);

            g_string_append_printf (haystack, " %s %s %s %s", P_(group->title), group->title, P_(group->words),
                                    group->words);
        }
        c->haystack = g_string_free (g_steal_pointer (&haystack), FALSE);
        g_ptr_array_add (entries, c);
    }
    return g_steal_pointer (&entries);
}

typedef struct {
    WebApps *w;                 /* reference */
    AdwNavigationPage *page;
    GtkEditable *entry;
    GtkStack *stack;            /* "loading", "results", "empty" */
    AdwPreferencesPage *results;
    AdwPreferencesGroup *group; /* the groups or the web apps found, now on the results page */
    AdwStatusPage *empty;
    GPtrArray *entries;         /* CatalogEntry, NULL until the first answer */
    gint64 updated;
    gboolean online;            /* the run with the server has been started */
    GCancellable *cancellable;
    GSubprocess *helper;
} Search;

static void
search_free (Search *s)
{
    g_cancellable_cancel (s->cancellable);
    if (s->helper)
        g_subprocess_force_exit (s->helper);
    g_clear_object (&s->helper);
    g_clear_object (&s->cancellable);
    g_clear_pointer (&s->entries, g_ptr_array_unref);
    webapps_unref (s->w);
    g_free (s);
}

static void
catalog_row_activated (AdwActionRow *row, Search *s)
{
    CatalogEntry *c = g_object_get_data (G_OBJECT (row), "catalog-entry");

    if (c)
        open_editor_full (s->w, NULL, &c->app, c->icon, s->page);
}

static GtkWidget *
catalog_icon (CatalogEntry *c)
{
    GtkWidget *image = NULL;

    if (c->icon && g_file_test (c->icon, G_FILE_TEST_IS_REGULAR))
        image = gtk_image_new_from_file (c->icon);
    if (!image)
        image = gtk_image_new_from_icon_name (FALLBACK_ICON);
    gtk_image_set_pixel_size (GTK_IMAGE (image), CATALOG_ICON_SIZE);
    gtk_widget_set_margin_top (image, 6);
    gtk_widget_set_margin_bottom (image, 6);
    return image;
}

/* A web app of the catalog: its icon, the name, the address */
static GtkWidget *
catalog_row (Search *s, CatalogEntry *c)
{
    GtkWidget *row = adw_action_row_new ();
    g_autofree char *host = url_host (c->app.url);

    adw_preferences_row_set_use_markup (ADW_PREFERENCES_ROW (row), FALSE);
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), c->app.name);
    adw_action_row_set_subtitle (ADW_ACTION_ROW (row), host);
    adw_action_row_add_prefix (ADW_ACTION_ROW (row), catalog_icon (c));
    adw_action_row_add_suffix (ADW_ACTION_ROW (row), gtk_image_new_from_icon_name ("go-next-symbolic"));
    gtk_list_box_row_set_activatable (GTK_LIST_BOX_ROW (row), TRUE);
    g_object_set_data (G_OBJECT (row), "catalog-entry", c);
    g_signal_connect (row, "activated", G_CALLBACK (catalog_row_activated), s);
    return row;
}

/* The page of a group: its web apps. It keeps the entries it shows, also when a newer catalog
 * replaces them on the search page meanwhile. */
static void
category_activated (AdwActionRow *row, Search *s)
{
    const Category *category = g_object_get_data (G_OBJECT (row), "category");
    GtkWidget *toolbar, *prefs;
    AdwPreferencesGroup *group;
    AdwNavigationPage *page;

    if (!category || !s->entries)
        return;
    prefs = adw_preferences_page_new ();
    group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (prefs), group);
    for (guint i = 0; i < s->entries->len; i++) {
        CatalogEntry *c = g_ptr_array_index (s->entries, i);

        if (g_strcmp0 (c->category, category->id) == 0)
            adw_preferences_group_add (group, catalog_row (s, c));
    }
    toolbar = adw_toolbar_view_new ();
    adw_toolbar_view_add_top_bar (ADW_TOOLBAR_VIEW (toolbar), adw_header_bar_new ());
    adw_toolbar_view_set_content (ADW_TOOLBAR_VIEW (toolbar), prefs);
    page = adw_navigation_page_new_with_tag (toolbar, P_(category->title), "plinius-webapp-category");
    g_object_set_data_full (G_OBJECT (page), "catalog-entries", g_ptr_array_ref (s->entries),
                            (GDestroyNotify) g_ptr_array_unref);
    cc_panel_push_subpage (s->w->panel, page);
}

/* A group: its symbol, its title and the first of its web apps */
static GtkWidget *
category_row (Search *s, const Category *category, const char *examples)
{
    GtkWidget *row = adw_action_row_new ();
    GtkWidget *image = gtk_image_new_from_icon_name (category->icon);

    adw_preferences_row_set_use_markup (ADW_PREFERENCES_ROW (row), FALSE);
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), P_(category->title));
    adw_action_row_set_subtitle (ADW_ACTION_ROW (row), examples);
    adw_action_row_set_subtitle_lines (ADW_ACTION_ROW (row), 1);
    adw_action_row_add_prefix (ADW_ACTION_ROW (row), image);
    adw_action_row_add_suffix (ADW_ACTION_ROW (row), gtk_image_new_from_icon_name ("go-next-symbolic"));
    gtk_list_box_row_set_activatable (GTK_LIST_BOX_ROW (row), TRUE);
    g_object_set_data (G_OBJECT (row), "category", (gpointer) category);
    g_signal_connect (row, "activated", G_CALLBACK (category_activated), s);
    return row;
}

static int
compare_entry_names (gconstpointer a, gconstpointer b)
{
    const CatalogEntry *x = *(CatalogEntry *const *) a, *y = *(CatalogEntry *const *) b;

    return g_utf8_collate (x->app.name, y->app.name);
}

/* Nothing typed: the groups that have web apps. Something typed: the matching web apps alone,
 * by name. */
static void
search_show (Search *s)
{
    g_autofree char *query = g_strstrip (g_strdup (gtk_editable_get_text (s->entry)));
    AdwPreferencesGroup *group;
    guint shown = 0;

    if (!s->entries) {
        gtk_stack_set_visible_child_name (s->stack, "loading");
        return;
    }
    if (s->group)
        adw_preferences_page_remove (s->results, s->group);
    group = s->group = ADW_PREFERENCES_GROUP (adw_preferences_group_new ());
    adw_preferences_page_add (s->results, group);
    /* The rows point into these entries */
    g_object_set_data_full (G_OBJECT (group), "catalog-entries", g_ptr_array_ref (s->entries),
                            (GDestroyNotify) g_ptr_array_unref);

    if (!*query) {
        for (gsize k = 0; k < G_N_ELEMENTS (categories); k++) {
            g_autoptr(GString) examples = g_string_new (NULL);
            guint n = 0;

            for (guint i = 0; i < s->entries->len; i++) {
                CatalogEntry *c = g_ptr_array_index (s->entries, i);

                if (g_strcmp0 (c->category, categories[k].id) != 0)
                    continue;
                if (n < 3)
                    g_string_append_printf (examples, "%s%s", n ? ", " : "", c->app.name);
                n++;
            }
            if (!n)
                continue;
            if (n > 3)
                g_string_append (examples, ", …");
            adw_preferences_group_add (group, category_row (s, &categories[k], examples->str));
            shown++;
        }
    } else {
        g_autoptr(GPtrArray) found = g_ptr_array_new ();

        for (guint i = 0; i < s->entries->len; i++) {
            CatalogEntry *c = g_ptr_array_index (s->entries, i);

            if (g_str_match_string (query, c->haystack, TRUE))
                g_ptr_array_add (found, c);
        }
        g_ptr_array_sort (found, compare_entry_names);
        for (guint i = 0; i < found->len; i++)
            adw_preferences_group_add (group, catalog_row (s, g_ptr_array_index (found, i)));
        shown = found->len;
    }
    if (shown) {
        gtk_stack_set_visible_child_name (s->stack, "results");
        return;
    }
    adw_status_page_set_title (s->empty, s->entries->len ? P_("No Web Apps Found") : P_("The List Is Not Available"));
    gtk_stack_set_visible_child_name (s->stack, "empty");
}

static void search_run (Search *s, gboolean online);

/* An answer of the helper (@entries NULL: none); after the first one, ask with the server */
static void
search_answer (Search *s, GPtrArray *answer, gint64 updated)
{
    g_autoptr(GPtrArray) entries = answer; /* taken over */

    /* The first answer, or a newer catalog from the server */
    if (entries && (!s->entries || updated > s->updated)) {
        g_clear_pointer (&s->entries, g_ptr_array_unref);
        s->entries = g_steal_pointer (&entries);
        s->updated = updated;
        search_show (s);
    }
    if (!s->online) {
        search_run (s, TRUE);
    } else if (!s->entries) {
        s->entries = g_ptr_array_new_with_free_func ((GDestroyNotify) catalog_entry_free);
        search_show (s);
    }
}

static void
helper_answered (GObject *source, GAsyncResult *result, gpointer data)
{
    Search *s = data;
    g_autoptr(GError) error = NULL;
    g_autofree char *out = NULL;
    g_autoptr(GPtrArray) entries = NULL;
    gint64 updated = 0;

    if (!g_subprocess_communicate_utf8_finish (G_SUBPROCESS (source), result, &out, NULL, &error)) {
        /* The page went: s is gone */
        if (g_error_matches (error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
            return;
        g_warning ("Could not read the catalog of web apps: %s", error->message);
    } else if (!g_subprocess_get_successful (G_SUBPROCESS (source))) {
        g_warning ("The catalog of web apps could not be listed");
    } else if (out && strlen (out) > MAX_CATALOG_BYTES) {
        g_warning ("The catalog of web apps is too large");
    } else if (!(entries = catalog_parse (out, &updated))) {
        g_warning ("The catalog of web apps is not readable");
    }
    g_clear_object (&s->helper);
    search_answer (s, g_steal_pointer (&entries), updated);
}

static void
search_run (Search *s, gboolean online)
{
    g_autoptr(GError) error = NULL;
    const char *argv[] = { CATALOG_HELPER, online ? NULL : "--offline", NULL };

    s->online = online;
    s->helper = g_subprocess_newv (argv, G_SUBPROCESS_FLAGS_STDOUT_PIPE, &error);
    if (!s->helper) {
        g_warning ("Could not start %s: %s", CATALOG_HELPER, error->message);
        search_answer (s, NULL, 0);
        return;
    }
    g_subprocess_communicate_utf8_async (s->helper, NULL, s->cancellable, helper_answered, s);
}

static void
manually_clicked (Search *s)
{
    open_editor_full (s->w, NULL, NULL, NULL, s->page);
}

/* Add: the search field, the entries right below it, Manually at the top right */
static void
open_search (WebApps *w)
{
    Search *s = g_new0 (Search, 1);
    GtkWidget *toolbar, *header, *clamp, *entry, *spinner, *button;

    s->w = webapps_ref (w);
    s->cancellable = g_cancellable_new ();

    entry = gtk_search_entry_new ();
    s->entry = GTK_EDITABLE (entry);
    gtk_search_entry_set_placeholder_text (GTK_SEARCH_ENTRY (entry), P_("Name, address or kind, e.g. bank"));
    gtk_accessible_update_property (GTK_ACCESSIBLE (entry), GTK_ACCESSIBLE_PROPERTY_LABEL, P_("Search Web Apps"), -1);
    clamp = adw_clamp_new ();
    gtk_widget_set_margin_start (clamp, 12);
    gtk_widget_set_margin_end (clamp, 12);
    gtk_widget_set_margin_bottom (clamp, 6);
    adw_clamp_set_child (ADW_CLAMP (clamp), entry);

    s->stack = GTK_STACK (gtk_stack_new ());
    spinner = adw_spinner_new ();
    gtk_widget_set_size_request (spinner, 32, 32);
    gtk_widget_set_halign (spinner, GTK_ALIGN_CENTER);
    gtk_widget_set_valign (spinner, GTK_ALIGN_CENTER);
    gtk_stack_add_named (s->stack, spinner, "loading");
    s->results = ADW_PREFERENCES_PAGE (adw_preferences_page_new ());
    gtk_stack_add_named (s->stack, GTK_WIDGET (s->results), "results");
    s->empty = ADW_STATUS_PAGE (adw_status_page_new ());
    adw_status_page_set_icon_name (s->empty, "system-search-symbolic");
    adw_status_page_set_description (s->empty, P_("Add the website with its address instead."));
    button = pill_button (P_("Enter Manually"), NULL);
    g_signal_connect_swapped (button, "clicked", G_CALLBACK (manually_clicked), s);
    adw_status_page_set_child (s->empty, button);
    gtk_stack_add_named (s->stack, GTK_WIDGET (s->empty), "empty");
    gtk_stack_set_visible_child_name (s->stack, "loading");

    /* Manually at the top right, where the page of a web app has Add */
    header = adw_header_bar_new ();
    button = gtk_button_new_with_label (P_("Manually"));
    g_signal_connect_swapped (button, "clicked", G_CALLBACK (manually_clicked), s);
    adw_header_bar_pack_end (ADW_HEADER_BAR (header), button);

    toolbar = adw_toolbar_view_new ();
    adw_toolbar_view_add_top_bar (ADW_TOOLBAR_VIEW (toolbar), header);
    adw_toolbar_view_add_top_bar (ADW_TOOLBAR_VIEW (toolbar), clamp);
    adw_toolbar_view_set_content (ADW_TOOLBAR_VIEW (toolbar), GTK_WIDGET (s->stack));
    /* The tag of the empty page's predecessor: Add leads here, a swipe goes back to the list */
    s->page = adw_navigation_page_new_with_tag (toolbar, P_("Add Web App"), "plinius-webapp-search");
    g_object_set_data_full (G_OBJECT (s->page), SEARCH_KEY, s, (GDestroyNotify) search_free);
    g_signal_connect_swapped (entry, "search-changed", G_CALLBACK (search_show), s);

    cc_panel_push_subpage (w->panel, s->page);
    search_run (s, FALSE);
}

static void
add_clicked (GtkWidget *tab)
{
    WebApps *w = g_object_get_data (G_OBJECT (tab), "plinius-webapps");

    if (!w)
        return;
    if (g_file_test (CATALOG_HELPER, G_FILE_TEST_IS_EXECUTABLE))
        open_search (w);
    else
        open_editor (w, NULL);
}

/**
 * cc_plinius_webapps_new_add_button:
 * @tab: the tab from cc_plinius_webapps_new_tab()
 *
 * Returns: "Add" for the panel's header bar: the search in the catalog, Manually beside it
 */
GtkWidget *
cc_plinius_webapps_new_add_button (GtkWidget *tab)
{
    GtkWidget *button = gtk_button_new_with_label (P_("Add"));

    gtk_widget_add_css_class (button, "suggested-action");
    gtk_widget_set_valign (button, GTK_ALIGN_CENTER);
    gtk_widget_set_sensitive (button, browser_available ());
    g_signal_connect_object (button, "clicked", G_CALLBACK (add_clicked), tab, G_CONNECT_SWAPPED);
    return button;
}

/**
 * cc_plinius_webapps_new_tab:
 * @panel: the Apps panel
 *
 * Returns: the "Web Apps" tab: the list of web apps (Add sits in the panel's header bar,
 * see cc_plinius_webapps_new_add_button())
 */
GtkWidget *
cc_plinius_webapps_new_tab (CcPanel *panel)
{
    WebApps *w = g_rc_box_new0 (WebApps);
    gboolean browser = browser_available ();

    w->alive = TRUE;
    w->panel = panel;
    w->rows = g_ptr_array_new ();
    w->page = adw_preferences_page_new ();

    w->group = add_group (w->page, NULL,
                          browser ? P_("Websites as apps of their own, each with its own browser profile. "
                                       "Switched on, a web app has an icon among the apps.")
                                  : P_("Web apps need Firefox or Chromium."));
    w->empty_row = adw_action_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (w->empty_row), P_("No web apps yet"));
    gtk_widget_add_css_class (w->empty_row, "dim-label");
    adw_preferences_group_add (w->group, w->empty_row);

    g_object_set_data_full (G_OBJECT (w->page), "plinius-webapps", w, (GDestroyNotify) webapps_detach);
    webapps_refresh (w);
    return w->page;
}
