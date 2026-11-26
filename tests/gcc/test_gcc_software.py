# Apps (Explore, Installed, Settings) and Updates (cc-plinius-software.c): the app catalog from
# AppStream files in the program's root, the package state from a fake pacman and flatpak,
# package operations through a fake pkexec and plinius-pkg, the update check from a fake
# plinius-software --updates-json, restarts through fake systemd-run/systemctl/login1.
# Screenshots load from a web server on 127.0.0.1 (fake_http.py): the program's https
# addresses are rewritten to it, nothing reaches the network.

import json
import os

import pytest

from fake_http import LocalWeb, png
from fake_programs import Programs
from gcc_harness import (build_panel, call, click, dump, gcc_session, gcc_world,  # noqa: F401
                         interface_xml, select, set_active, set_text, settle, wait, wait_for)

LOGIN1 = "org.freedesktop.login1"
LOGIN1_PATH = "/org/freedesktop/login1"
LOGIN1_XML = """<node><interface name="org.freedesktop.login1.Manager">
 <method name="Reboot"><arg type="b" name="interactive" direction="in"/></method>
</interface></node>"""
PORTAL = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
PORTAL_XML = """<node><interface name="org.freedesktop.portal.OpenURI">
 <method name="OpenURI"><arg type="s" name="parent_window" direction="in"/>
  <arg type="s" name="uri" direction="in"/><arg type="a{sv}" name="options" direction="in"/>
  <arg type="o" name="handle" direction="out"/></method>
 <property name="version" type="u" access="read"/>
</interface></node>"""

HELPER = "/usr/lib/plinius-software/plinius-pkg"
SOFTWARE = "/usr/bin/plinius-software"
RESTART_HELPER = "/usr/lib/plinius-software/plinius-restart"
RESTART_RESULT = "/run/plinius-pkg/restart.json"
PATHS = [HELPER, SOFTWARE, RESTART_HELPER, RESTART_RESULT, "/usr/share/plinius-software/apps.conf",
         "/usr/share/plinius-apps/recommended.list", "/etc/pacman.conf"]

# Declarations for the copy of the panel file: where AppStream loads from, and the screenshots'
# https addresses moved to the local web server
HEADER = r'''
#pragma once
#include <appstream.h>
#include <libsoup/soup.h>
void gcc_pool_setup (AsPool *pool, AsPoolFlags flags);
SoupMessage *gcc_soup_message_new (const char *method, const char *uri);
'''

GLUE = r'''
#include <appstream.h>
#include <gio/gdesktopappinfo.h>
#include <libsoup/soup.h>
#include "gcc_driver.h"
#include "cc-plinius-software.h"

#define ROOT "@ROOT@"

/* The driver's default panel: a page with a header bar */
GType gcc_test_panel_get_type (void);

/* The panel's catalog: only the test's AppStream files (the OS catalog and Flathub's in the
 * program's root), never the host's */
void
gcc_pool_setup (AsPool *pool, AsPoolFlags flags)
{
    as_pool_set_load_std_data_locations (pool, FALSE);
    as_pool_set_flags (pool, AS_POOL_FLAG_NONE);
    if (flags & AS_POOL_FLAG_LOAD_OS_CATALOG)
        as_pool_add_extra_data_location (pool, ROOT "/catalog/os", AS_FORMAT_STYLE_CATALOG);
    if (flags & AS_POOL_FLAG_LOAD_FLATPAK)
        as_pool_add_extra_data_location (pool, ROOT "/catalog/flatpak", AS_FORMAT_STYLE_CATALOG);
}

/* https://HOST/PATH -> the test's web server on 127.0.0.1, at /HOST/PATH */
SoupMessage *
gcc_soup_message_new (const char *method, const char *uri)
{
    const char *port = g_getenv ("PLINIUS_TEST_HTTP_PORT");
    g_autoptr(GUri) parsed = g_uri_parse (uri, SOUP_HTTP_URI_FLAGS, NULL);
    g_autofree char *local = NULL;

    if (!port)
        g_error ("a request without the test's web server: %s", uri);
    if (!parsed)
        return NULL;
    local = g_strdup_printf ("http://127.0.0.1:%s/%s%s", port, g_uri_get_host (parsed), g_uri_get_path (parsed));
    return soup_message_new (method, local);
}

static GtkWidget *app_page, *launch_button, *permissions;

static GtkWidget *
find_descendant (GtkWidget *w, GType type)
{
    if (G_TYPE_CHECK_INSTANCE_TYPE (w, type))
        return w;
    for (GtkWidget *c = gtk_widget_get_first_child (w); c; c = gtk_widget_get_next_sibling (c)) {
        GtkWidget *found = find_descendant (c, type);

        if (found)
            return found;
    }
    return NULL;
}
static GtkSearchEntry *search;

/* The Updates panel or GNOME's Apps panel (its parts our file uses) */
CcPanel *
gcc_test_create_panel (GccTest *t)
{
    CcPanel *panel;

    if (!g_strcmp0 (gcc_test_param (t, "panel"), "updates")) {
        /* The shell gives a panel its title */
        CcPanel *updates = g_object_new (cc_updates_panel_get_type (), "window", t->window, NULL);

        adw_navigation_page_set_title (ADW_NAVIGATION_PAGE (updates), "Updates");
        return updates;
    }
    panel = g_object_new (gcc_test_panel_get_type (), "window", t->window, NULL);
    return panel;
}

/* As cc-applications-panel.blp has it: a header bar and the search bar on top, the stack of the
 * app list as the content, Default Apps in a group of its page */
void
gcc_test_setup (GccTest *t)
{
    AdwToolbarView *toolbar;
    GtkWidget *search_bar, *stack, *apps, *defaults, *row, *list;
    static GDBusConnection *held;

    /* Another part of Settings using the system bus all along */
    if (gcc_test_param (t, "hold_system_bus"))
        held = g_bus_get_sync (G_BUS_TYPE_SYSTEM, NULL, NULL);
    if (!g_strcmp0 (gcc_test_param (t, "panel"), "updates"))
        return;
    toolbar = ADW_TOOLBAR_VIEW (adw_navigation_page_get_child (ADW_NAVIGATION_PAGE (t->panel)));
    search_bar = gtk_search_bar_new ();
    search = GTK_SEARCH_ENTRY (gtk_search_entry_new ());
    gtk_search_bar_set_child (GTK_SEARCH_BAR (search_bar), GTK_WIDGET (search));
    gtk_search_bar_connect_entry (GTK_SEARCH_BAR (search_bar), GTK_EDITABLE (search));
    gtk_search_bar_set_search_mode (GTK_SEARCH_BAR (search_bar), TRUE);

    stack = gtk_stack_new ();
    apps = adw_preferences_page_new ();
    defaults = adw_preferences_group_new ();
    row = adw_action_row_new ();
    adw_preferences_row_set_title (ADW_PREFERENCES_ROW (row), "Default Apps");
    adw_preferences_group_add (ADW_PREFERENCES_GROUP (defaults), row);
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (apps), ADW_PREFERENCES_GROUP (defaults));
    list = adw_preferences_group_new ();
    adw_preferences_group_set_title (ADW_PREFERENCES_GROUP (list), "Installed Apps");
    adw_preferences_page_add (ADW_PREFERENCES_PAGE (apps), ADW_PREFERENCES_GROUP (list));
    gtk_stack_add_named (GTK_STACK (stack), apps, "apps-page");

    if (!g_strcmp0 (gcc_test_param (t, "search"), "inside")) {
        /* A search bar without a header bar next to it */
        GtkWidget *box = gtk_box_new (GTK_ORIENTATION_VERTICAL, 0);

        gtk_box_append (GTK_BOX (box), search_bar);
        gtk_box_append (GTK_BOX (box), stack);
        adw_toolbar_view_set_content (toolbar, box);
    } else {
        adw_toolbar_view_add_top_bar (toolbar, search_bar);
        adw_toolbar_view_set_content (toolbar, stack);
    }
    t->page = NULL;
    cc_plinius_software_setup_apps_panel (t->panel, stack, gcc_test_param (t, "no_defaults") ? NULL : defaults,
                                          GTK_EDITABLE (search));
}

/* GNOME's page of an installed app: Open in a wrap box below the name, the permissions group */
static void
open_app (GccTest *t, GAppInfo *info)
{
    if (!app_page) {
        GtkWidget *toolbar = adw_toolbar_view_new ();
        GtkWidget *page = adw_preferences_page_new ();
        GtkWidget *head_group = adw_preferences_group_new ();
        GtkWidget *head = gtk_box_new (GTK_ORIENTATION_VERTICAL, 6);
        GtkWidget *wrap = adw_wrap_box_new ();

        launch_button = gtk_button_new_with_label ("Open");
        adw_wrap_box_append (ADW_WRAP_BOX (wrap), launch_button);
        gtk_box_append (GTK_BOX (head), gtk_label_new ("App Head"));
        gtk_box_append (GTK_BOX (head), wrap);
        adw_preferences_group_add (ADW_PREFERENCES_GROUP (head_group), head);
        adw_preferences_page_add (ADW_PREFERENCES_PAGE (page), ADW_PREFERENCES_GROUP (head_group));
        permissions = adw_preferences_group_new ();
        adw_preferences_group_set_title (ADW_PREFERENCES_GROUP (permissions), "Permissions");
        adw_preferences_page_add (ADW_PREFERENCES_PAGE (page), ADW_PREFERENCES_GROUP (permissions));
        adw_toolbar_view_add_top_bar (ADW_TOOLBAR_VIEW (toolbar), adw_header_bar_new ());
        adw_toolbar_view_set_content (ADW_TOOLBAR_VIEW (toolbar), page);
        /* The panel's template keeps its page, also while it is not shown */
        app_page = g_object_ref_sink (GTK_WIDGET (adw_navigation_page_new_with_tag (toolbar, "App Settings",
                                                                                     "app-settings")));
    }
    if (!gtk_widget_get_parent (app_page))
        cc_panel_push_subpage (t->panel, ADW_NAVIGATION_PAGE (app_page));
    cc_plinius_software_update_app_page (t->panel, info, launch_button, permissions);
}

gboolean
gcc_test_call (GccTest *t, const char *name, JsonArray *args, JsonBuilder *result)
{
    if (g_str_equal (name, "open_app")) {
        const char *id = json_array_get_string_element (args, 0);
        g_autoptr(GAppInfo) info = NULL;

        if (g_str_has_prefix (id, "/"))
            info = g_app_info_create_from_commandline (id, "Loose", G_APP_INFO_CREATE_NONE, NULL);
        else
            info = G_APP_INFO (g_desktop_app_info_new (id));
        g_assert (info != NULL);
        open_app (t, info);
        json_builder_add_null_value (result);
        return TRUE;
    }
    if (g_str_equal (name, "tab")) {
        AdwViewStack *tabs = ADW_VIEW_STACK (find_descendant (GTK_WIDGET (t->panel), ADW_TYPE_VIEW_STACK));

        adw_view_stack_set_visible_child_name (tabs, json_array_get_string_element (args, 0));
        json_builder_add_null_value (result);
        return TRUE;
    }
    if (g_str_equal (name, "notify_active")) {
        /* Back in Settings after the phone was unlocked */
        g_object_notify (G_OBJECT (t->window), "is-active");
        json_builder_add_boolean_value (result, gtk_window_is_active (GTK_WINDOW (t->window)));
        return TRUE;
    }
    if (g_str_equal (name, "explore_visible")) {
        json_builder_add_boolean_value (result, cc_plinius_software_explore_visible (t->panel));
        return TRUE;
    }
    if (g_str_equal (name, "explore_visible_elsewhere")) {
        /* A panel without our tabs */
        g_autoptr(CcPanel) other = g_object_ref_sink (g_object_new (gcc_test_panel_get_type (), NULL));

        json_builder_add_boolean_value (result, cc_plinius_software_explore_visible (other));
        return TRUE;
    }
    if (g_str_equal (name, "static_init")) {
        cc_updates_panel_static_init_func ();
        json_builder_add_null_value (result);
        return TRUE;
    }
    if (g_str_equal (name, "close_request")) {
        gboolean handled = FALSE;

        g_signal_emit_by_name (t->window, "close-request", &handled);
        json_builder_add_boolean_value (result, handled);
        return TRUE;
    }
    return FALSE;
}
'''


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    out = tmp_path_factory.mktemp("software")
    root = out / "software-root"
    header = out / "gcc_software.h"
    header.write_text(HEADER)
    reps = [(f'"{p}"', f'"{{root}}{p}"') for p in PATHS]
    reps += [("as_pool_set_flags (pool, ", "gcc_pool_setup (pool, "),
             ('soup_message_new ("GET", url)', 'gcc_soup_message_new ("GET", url)')]
    return build_panel(gcc_session, out, "software", ["cc-plinius-software.c", "cc-plinius-webapps.c"],
                       GLUE.replace("@ROOT@", str(root)),
                       replacements={"cc-plinius-software.c": reps,
                                     "cc-plinius-webapps.c": [('"/usr/lib/firefox', '"{root}/usr/lib/firefox'),
                                                              ('"/usr/lib/plinius-webapps/',
                                                               '"{root}/usr/lib/plinius-webapps/')]},
                       # Only in the file's header comment
                       allow=("/usr/lib/plinius-software/plinius-restart apply",),
                       pkgs=("appstream", "libsoup-3.0", "gdk-pixbuf-2.0"),
                       flags=["-include", str(header)])


# ----- The phone the panel meets -----

def component(cid, name, summary="", pkg=None, bundle=None, categories=(), icon=None, stock=None,
              desktop=None, homepage=None, description=None, version=None, screenshots=(),
              keywords=(), requires=(), recommends=(), supports=(), form_factor=None):
    """One <component> of an AppStream catalog. requires/recommends/supports:
    [(compare, px, side)] display_length relations (side None: shortest)."""
    x = [f'<component type="desktop-application"><id>{cid}</id><name>{name}</name>'
         f'<summary>{summary}</summary>']
    if pkg:
        x.append(f"<pkgname>{pkg}</pkgname>")
    if bundle:
        x.append(f'<bundle type="flatpak">{bundle}</bundle>')
    if categories:
        x.append("<categories>" + "".join(f"<category>{c}</category>" for c in categories) + "</categories>")
    if icon:
        x.append(f'<icon type="cached" width="64" height="64">{icon}</icon>')
    if stock:
        x.append(f'<icon type="stock">{stock}</icon>')
    if desktop:
        x.append(f'<launchable type="desktop-id">{desktop}</launchable>')
    if homepage:
        x.append(f'<url type="homepage">{homepage}</url>')
    if description:
        x.append(f"<description>{description}</description>")
    if version:
        x.append(f'<releases><release version="{version}" timestamp="1700000000"/></releases>')
    if screenshots:
        x.append("<screenshots>" + "".join(screenshots) + "</screenshots>")
    if keywords:
        x.append("<keywords>" + "".join(f"<keyword>{k}</keyword>" for k in keywords) + "</keywords>")
    for tag, rel in (("requires", requires), ("recommends", recommends), ("supports", supports)):
        if rel:
            items = "".join(f'<display_length compare="{c}"{f" side={chr(34)}{s}{chr(34)}" if s else ""}>{px}</display_length>'
                            for c, px, s in rel)
            x.append(f"<{tag}>{items}</{tag}>")
    if form_factor:
        x.append(f'<custom><value key="Purism::form_factor">{form_factor}</value></custom>')
    x.append("</component>")
    return "".join(x)


def shot(*images, kind="image"):
    """A <screenshot>; images: (kind, url, width, locale)."""
    inner = "".join(f'<image type="{k}"{f" width={chr(34)}{w}{chr(34)} height={chr(34)}{w}{chr(34)}" if w else ""}'
                    f'{f" xml:lang={chr(34)}{loc}{chr(34)}" if loc else ""}>{u}</image>'
                    for k, u, w, loc in images)
    if kind != "image":
        return f'<screenshot><video>{images[0][1]}</video></screenshot>'
    return f"<screenshot>{inner}</screenshot>"


class Phone:
    """The files and programs of the phone, in the program's root and the world's fake_bin."""

    def __init__(self, program, world):
        self.root = program.root
        self.world = world
        for d in list(self.root.iterdir()):
            import shutil
            shutil.rmtree(d) if d.is_dir() else d.unlink()
        self.progs = Programs(world.tmp / "programs")
        bin_ = world.fake_bin.path
        for name in ("pacman", "flatpak", "systemctl"):
            self.progs.program(bin_ / name)
        for name in ("pkexec", "systemd-run"):
            self.progs.program(bin_ / name)
            self.progs.rule(name, exec=True)
        self.progs.program(self.path(HELPER), "plinius-pkg")
        self.progs.program(self.path(SOFTWARE), "plinius-software")
        self.progs.program(self.path(RESTART_HELPER), "plinius-restart")
        self.web = None
        self.catalogs = {"os": [], "flatpak": []}
        self.sync, self.installed, self.info = [], [], []
        self.remotes, self.flatpaks, self.furihub = [], [], []
        world.extra_env.update({"http_proxy": "", "https_proxy": "", "HTTP_PROXY": "", "HTTPS_PROXY": "",
                                "no_proxy": "*", "all_proxy": "", "GIO_USE_PROXY_RESOLVER": "dummy"})
        self.updates()

    def path(self, p):
        return self.root / p.lstrip("/")

    def write(self, p, text="", mode=0o644):
        f = self.path(p)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        f.chmod(mode)
        return f

    # The catalog
    def app(self, *components, flatpak=False):
        self.catalogs["flatpak" if flatpak else "os"].extend(components)
        self._write_catalogs()

    def icon(self, name, size=64, flatpak=False):
        origin = "flathub" if flatpak else "test-os"
        f = self.path(f"/catalog/{'flatpak' if flatpak else 'os'}/icons/{origin}/{size}x{size}/{name}")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(png(size, size))

    def _write_catalogs(self):
        for kind, comps in self.catalogs.items():
            if not comps:
                continue
            origin = "flathub" if kind == "flatpak" else "test-os"
            self.write(f"/catalog/{kind}/xml/{origin}.xml",
                       f'<?xml version="1.0" encoding="UTF-8"?>\n<components version="1.0" origin="{origin}">'
                       + "".join(comps) + "</components>\n")

    # pacman
    def package(self, name, version="1.0-1", installed=False, repo="extra", required_by=(), provides=()):
        self.sync.append(f"{repo} {name} {version}" + (" [installed]" if installed else ""))
        if installed:
            self.installed.append(name)
            self.info.append(f"Name            : {name}\nVersion         : {version}\n"
                             f"Provides        : {' '.join(provides) or 'None'}\n"
                             f"Required By     : {' '.join(required_by) or 'None'}\n"
                             f"Optional For    : None\n")
        self._pacman()

    def _pacman(self):
        self.progs.clear("pacman")
        self.progs.rule("pacman", ["-Sl"], out="\n".join(self.sync) + "\n")
        self.progs.rule("pacman", ["-Qq"], out="\n".join(self.installed) + "\n")
        self.progs.rule("pacman", ["-Qi"], out="\n".join(self.info))
        for block in self.info:
            name = block.split("\n")[0].split(":", 1)[1].strip()
            version = block.split("\n")[1].split(":", 1)[1].strip()
            self.progs.rule("pacman", ["-Qi", name], out=block)
            self.progs.rule("pacman", ["-Q", name], out=f"{name} {version}\n")
        for path, owner in getattr(self, "owners", {}).items():
            self.progs.rule("pacman", ["-Qqo", path], out=f"{owner}\n")
        self.progs.rule("pacman", prefix=["-Qqo"], code=1)

    def owns(self, path, pkg):
        self.owners = getattr(self, "owners", {})
        self.owners[str(path)] = pkg
        self._pacman()

    # flatpak
    def flatpak(self, remotes=None, installed=None, furihub=None, info=None):
        if remotes is not None:
            self.remotes = remotes
        if installed is not None:
            self.flatpaks = installed
        if furihub is not None:
            self.furihub = furihub
        self.progs.clear("flatpak")
        self.progs.rule("flatpak", ["remotes", "--system", "--columns=name"],
                        out="".join(f"{r}\n" for r in self.remotes))
        self.progs.rule("flatpak", ["list", "--system", "--app", "--columns=application,origin"],
                        out="".join(f"{a}\t{o}\n" for a, o in self.flatpaks))
        self.progs.rule("flatpak", ["remote-ls", "--system", "--cached", "--app", "--columns=ref", "furihub"],
                        out="".join(f"{r}\n" for r in self.furihub))
        for app_id, (version, origin) in (info or {}).items():
            self.progs.rule("flatpak", ["info", "--system", app_id],
                            out=f"\nFlat - an app\n\n          ID: {app_id}\n     Version: {version}\n")
            self.progs.rule("flatpak", ["info", "--system", "--show-origin", app_id], out=f"{origin}\n")

    # plinius-software --updates-json
    def updates(self, *results, code=0, **result):
        """The answers of the update check: dicts (successive checks), or one built from result."""
        if not results:
            results = [dict({"code": code, "packages": [], "flatpaks": [], "last_check": None,
                             "repo_warning": None, "held": None}, **result)]
        self.progs.clear("plinius-software")
        self.progs.rule("plinius-software", ["--updates-json"],
                        outs=[r if isinstance(r, str) else json.dumps(r) for r in results])

    def helper(self, args=None, prefix=None, lines=(), code=0, **kw):
        """What plinius-pkg prints and exits with for args (or any call starting with prefix)."""
        self.progs.rule("plinius-pkg", args, prefix, first=True, out="".join(f"{line}\n" for line in lines),
                        code=code, **kw)

    def helper_calls(self):
        return self.progs.calls("plinius-pkg")

    def serve(self):
        """The web server for screenshots (https://HOST/PATH is /HOST/PATH there)."""
        if not self.web:
            self.web = LocalWeb()
            self.world.extra_env["PLINIUS_TEST_HTTP_PORT"] = str(self.web.port)
        return self.web

    def desktop(self, app_id, name, exec_="/bin/true", extra="", where=None):
        d = where or (self.world.data_dir / "applications")
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{app_id}.desktop"
        f.write_text(f"[Desktop Entry]\nType=Application\nName={name}\nExec={exec_}\n{extra}")
        return f


@pytest.fixture
def phone(program, gcc_world):
    p = Phone(program, gcc_world)
    yield p
    if p.web:
        p.web.close()


def software(phone, flathub=False, furihub=False):
    """plinius-software is installed (its helper is), Explore is there."""
    phone.flatpak(remotes=(["flathub"] if flathub else []) + (["furihub"] if furihub else []))
    if not phone.sync:
        phone._pacman()


def category_rows(screen):
    return {r["text"]: r.get("subtitle") for r in screen.find(type="AdwActionRow")
            if r.get("text") in CATEGORY_TITLES}


CATEGORY_TITLES = ["Recommended", "Communication & Internet", "Music & Video", "Office & Documents",
                   "Photos & Graphics", "Learning & Science", "Games", "Utilities", "System", "All Apps"]


CATALOG_READY = wait_for(type="AdwActionRow", text="All Apps", timeout=20000)


def test_explore_counts_what_can_be_installed_by_category(program, gcc_world, phone):
    software(phone)
    phone.write("/usr/share/plinius-apps/recommended.list", "# the selection\nfirefox\n\n")
    phone.app(component("org.mozilla.firefox", "Firefox", "Web browser", pkg="firefox",
                        categories=["Network"], form_factor="mobile"),
              component("org.gnome.Maps", "Maps", "Find places", pkg="gnome-maps",
                        categories=["Utility", "Education"], requires=[("ge", 300, None)]),
              component("org.gnome.Music", "Music", "Play music", pkg="gnome-music",
                        categories=["AudioVideo"], recommends=[("ge", 360, "shortest")]),
              component("org.gnome.Clocks", "Clocks", "Time", pkg="gnome-clocks", categories=["Utility"],
                        supports=[("gt", 300, None)]),
              # Installed: not offered
              component("org.gnome.Weather", "Weather", "Forecasts", pkg="gnome-weather",
                        categories=["Utility"], form_factor="mobile"),
              # Not in the phone's repositories
              component("org.x86.Only", "X86 Only", "Elsewhere", pkg="x86-only", form_factor="mobile"),
              # Without a package
              component("org.no.Package", "No Package", "Nothing", form_factor="mobile"))
    for name in ("firefox", "gnome-maps", "gnome-music", "gnome-clocks"):
        phone.package(name)
    phone.package("gnome-weather", installed=True)
    r = gcc_world.run(program, [CATALOG_READY, dump("explore")])
    screen = r.dump("explore")
    assert category_rows(screen) == {"Recommended": "1 app", "Communication & Internet": "1 app",
                                     "Music & Video": "1 app", "Learning & Science": "1 app",
                                     "Utilities": "2 apps", "All Apps": "4 apps"}
    # The tabs, Explore first and shown
    switches = [b for b in screen.find(type="AdwViewSwitcherButton")]
    assert [b["children"][-1]["text"] for b in switches] == ["Explore", "Installed", "Web Apps", "Settings"]
    assert [b["active"] for b in switches] == [True, False, False, False]
    assert phone.progs.calls("pacman")[:2] == [["-Sl"], ["-Qq"]]
    assert not r.warnings()


def ratings_catalog(phone):
    software(phone)
    phone.write("/usr/share/plinius-software/apps.conf",
                "# id rating note\n"
                "org.conf.Phone phone\n"
                "org.conf.Scaled scaled With the mobile tweaks from mobile-config-firefox\n"
                "org.conf.Desktop desktop\n"
                "org.conf.Bogus excellent\n"
                "garbage\n")
    phone.app(component("org.conf.Phone", "Conf Phone", pkg="conf-phone", requires=[("ge", 1024, None)]),
              component("org.conf.Scaled", "Conf Scaled", pkg="conf-scaled"),
              component("org.conf.Desktop", "Conf Desktop", pkg="conf-desktop", form_factor="mobile"),
              component("org.conf.Bogus", "Conf Bogus", pkg="conf-bogus"),
              component("org.own.Wide", "Own Wide", pkg="own-wide",
                        requires=[("ge", 800, None), ("ge", 200, "longest"), ("lt", 100, None), ("ge", 0, None)]),
              component("org.own.Narrow", "Own Narrow", pkg="own-narrow", supports=[("gt", 359, None)]))
    for p in ("conf-phone", "conf-scaled", "conf-desktop", "conf-bogus", "own-wide", "own-narrow"):
        phone.package(p)


def app_rows(screen, page=None):
    """[(name, summary, badge, badge classes)] of the app list on screen (on the page titled
    page): an icon and three labels each."""
    if page:
        screen = screen.within(type=("AdwNavigationPage", "GccTestPanel"), text=page)
    nodes = [n for n in screen.all() if n.get("type") != "GtkImage"]
    rows, i = [], 0
    while i + 2 < len(nodes):
        a, b, c = nodes[i:i + 3]
        if (all(x.get("type") == "GtkLabel" for x in (a, b, c)) and not a.get("classes")
                and b.get("classes") == ["dim-label"]
                and set(c.get("classes", [])) & {"success", "warning", "error", "dim-label"}):
            rows.append((a.get("text"), b.get("text"), c.get("text"), c.get("classes", [])))
            i += 3
        else:
            i += 1
    return rows


def test_ratings_from_apps_conf_and_appstream_and_which_explore_shows(program, gcc_world, phone):
    ratings_catalog(phone)
    all_apps = click(type="AdwActionRow", text="All Apps")
    r = gcc_world.run(program, [CATALOG_READY, dump("explore"), all_apps, settle(200), dump("all")])
    # For phones and with limitations only, by default
    assert category_rows(r.dump("explore"))["All Apps"] == "3 apps"
    rows = app_rows(r.dump("all"), "All Apps")
    assert [(n, b) for n, _s, b, _c in rows] == [
        ("Conf Phone", "For phones"), ("Conf Scaled", "With limitations"), ("Own Narrow", "For phones")]
    assert [c for *_x, c in rows] == [["success"], ["warning"], ["success"]]
    assert not r.warnings()


def test_settings_show_more_ratings_and_remember_them(program, gcc_world, phone):
    ratings_catalog(phone)
    settings = phone.world.home / ".config/plinius-software/settings.ini"
    r = gcc_world.run(program, [
        CATALOG_READY, select_tab("settings"), dump("settings"),
        set_active(True, type="AdwSwitchRow", text="Apps not approved for mobile"),
        set_active(True, type="AdwSwitchRow", text="Apps for large screens"),
        select_tab("explore"), dump("explore"),
        click(type="AdwActionRow", text="All Apps"), settle(200), dump("all")])
    screen = r.dump("settings")
    group = screen.one(type="AdwPreferencesGroup", text="Also show under “Explore”")
    assert [(n["text"], n["active"]) for n in group.walk() if n.get("type") == "AdwSwitchRow"] == [
        ("Apps not approved for mobile", False), ("Apps for large screens", False)]
    # Default Apps moved here, above
    assert [g.get("text") for g in screen.find(type="AdwPreferencesGroup")][:1] == [None]
    assert screen.has(type="AdwActionRow", text="Default Apps")
    assert category_rows(r.dump("explore"))["All Apps"] == "6 apps"
    assert [(n, b) for n, _s, b, _c in app_rows(r.dump("all"), "All Apps")] == [
        ("Conf Bogus", "Not specified"), ("Conf Desktop", "For large screens"), ("Conf Phone", "For phones"),
        ("Conf Scaled", "With limitations"), ("Own Narrow", "For phones"), ("Own Wide", "For large screens")]
    text = settings.read_text()
    assert "[Anzeige]" in text and "show-unknown=true" in text and "show-desktop=true" in text


def select_tab(name):
    """Show a tab of the Apps panel (as tapping it in the switcher bar)."""
    return {"op": "call", "name": "tab", "args": [name], "label": f"tab-{name}"}


def search_catalog(phone):
    software(phone)
    phone.app(component("org.gnome.Maps", "Maps", "Find places", pkg="gnome-maps", form_factor="mobile",
                        keywords=["Navigation"]),
              component("org.gnome.Music", "Music", "Play songs", pkg="gnome-music", form_factor="mobile"),
              component("org.gnome.Weather", "Weather", "Forecasts for places", pkg="gnome-weather",
                        form_factor="mobile"))
    for p in ("gnome-maps", "gnome-music", "gnome-weather"):
        phone.package(p)


SEARCH = {"type": "GtkSearchEntry"}


def test_search_lists_matching_apps_by_every_word(program, gcc_world, phone):
    search_catalog(phone)
    r = gcc_world.run(program, [
        CATALOG_READY,
        set_text("PLACES", **SEARCH), settle(400), dump("places"),
        set_text("  gnome-music  ", **SEARCH), settle(400), dump("package"),
        set_text("navigation maps", **SEARCH), settle(400), dump("keyword"),
        set_text("places nowhere", **SEARCH), settle(400), dump("none"),
        set_text("   ", **SEARCH), settle(400), dump("blank"),
        call("explore_visible")])
    assert [a[0] for a in app_rows(r.dump("places"))] == ["Maps", "Weather"]
    assert [a[0] for a in app_rows(r.dump("package"))] == ["Music"]
    assert [a[0] for a in app_rows(r.dump("keyword"))] == ["Maps"]
    none = r.dump("none")
    assert none.has(type="AdwStatusPage", text="No Results")
    assert not app_rows(none)
    # Only blanks: the categories again
    assert category_rows(r.dump("blank"))["All Apps"] == "3 apps"
    assert r.value("explore_visible") is True


def test_search_on_installed_leaves_explore_alone(program, gcc_world, phone):
    search_catalog(phone)
    r = gcc_world.run(program, [
        CATALOG_READY, select_tab("installed"), call("explore_visible", label="installed"),
        set_text("maps", **SEARCH), settle(400), select_tab("explore"), settle(200), dump("explore"),
        select_tab("webapps"), dump("webapps"), call("explore_visible_elsewhere")])
    assert r.value("installed") is False
    # Back on Explore the search applies
    assert [a[0] for a in app_rows(r.dump("explore"))] == ["Maps"]
    # The search bar belongs to Explore and Installed; Add to Web Apps
    webapps = r.dump("webapps")
    assert not webapps.has(type="GtkSearchEntry")
    assert webapps.has(type="GtkButton", text="Add")
    assert not r.dump("explore").has(type="GtkButton", text="Add")
    assert r.value("explore_visible_elsewhere") is False


def open_first(page_title):
    """Tap the first app of the list on the page."""
    return {"op": "action", "target": {"type": "GtkListView", "within": {"type": "AdwNavigationPage",
                                                                          "text": page_title}},
            "name": "list.activate-item", "value": "uint32 0"}


def test_category_page_and_an_apps_details(program, gcc_world, phone):
    software(phone)
    web = phone.serve()
    web.route("/shots.example.org/one-large.png", png(700, 400))
    web.route("/shots.example.org/one-small.png", png(300, 200))
    web.route("/shots.example.org/two.png", png(640, 480))
    phone.icon("gnome-maps_org.gnome.Maps.png")
    phone.app(component(
        "org.gnome.Maps", "Maps", "Find places", pkg="gnome-maps", categories=["Utility"], form_factor="mobile",
        icon="gnome-maps_org.gnome.Maps.png", homepage="https://apps.gnome.org/Maps?a=1&amp;b=2",
        description="<p>Find places around the world.</p><p>Plan routes.</p>",
        screenshots=[shot(("thumbnail", "https://shots.example.org/one-small.png", 300, None),
                          ("thumbnail", "https://shots.example.org/one-large.png", 700, None),
                          ("thumbnail", "https://shots.example.org/one-huge.png", 1400, None),
                          ("source", "https://shots.example.org/one-source.png", None, None)),
                     shot(("source", "https://shots.example.org/two.png", None, None),
                          ("source", "https://shots.example.org/two-de.png", None, "de")),
                     shot(("source", "https://shots.example.org/video.webm", None, None), kind="video"),
                     shot(("source", "http://plain.example.org/three.png", None, None)),
                     shot(("source", "https://shots.example.org/missing.png", None, None))]))
    phone.package("gnome-maps", "48.2-1")
    gcc_world.service("session", PORTAL, PORTAL_PATH, PORTAL_XML, properties={"version": "uint32 4"},
                      replies={"OpenURI": "(objectpath '/org/freedesktop/portal/desktop/request/1/1',)"})
    r = gcc_world.run(program, [
        CATALOG_READY, click(type="AdwActionRow", text="Utilities"), settle(200), dump("category"),
        open_first("Utilities"), wait_for(type="AdwSpinner", gone=True, timeout=10000), dump("details"),
        click(type="AdwActionRow", text="Website"), settle(300)])
    assert app_rows(r.dump("category"), "Utilities") == [("Maps", "Find places", "For phones", ["success"])]
    page = r.dump("details").within(type="AdwNavigationPage", text="Maps")
    assert page.has(type="GtkButton", text="Install", classes=["suggested-action"])
    assert not page.has(type="GtkButton", text="Remove")
    assert ("For phones", "The app fits the screen.") in page.rows()
    assert page.has(type="GtkLabel", text="Find places around the world.\n\nPlan routes.")
    details = page.within(type="AdwPreferencesGroup", text="Details")
    assert details.rows() == [("Package", "gnome-maps 48.2-1"), ("Website", "https://apps.gnome.org/Maps?a=1&b=2")]
    # One image per screenshot: the smallest thumbnail wide enough, else the original; no
    # video, no other language, nothing over plain http
    assert sorted(web.requests) == ["/shots.example.org/missing.png", "/shots.example.org/one-large.png",
                                    "/shots.example.org/two.png"]
    assert [n["text"] for n in page.find(type="GtkLabel", text="Image not available")] == \
        ["Image not available"] * 2
    assert [c["args"][1] for c in gcc_world.calls(PORTAL, "OpenURI")] == ["https://apps.gnome.org/Maps?a=1&b=2"]
    assert not r.warnings()


# ----- Installing and removing -----

PHOSH_SCHEMA = """<?xml version="1.0" encoding="UTF-8"?>
<schemalist>
  <schema id="{id}" path="/{path}/">
    <key name="force-adaptive" type="as"><default>['org.already.Here.desktop']</default></key>
  </schema>
</schemalist>
"""


def phosh_schema(world, schema_id="mobi.phosh.shell"):
    """Phosh's settings (force-adaptive), next to the session's schemas."""
    import subprocess
    d = world.tmp / "schemas"
    d.mkdir(exist_ok=True)
    (d / f"{schema_id}.gschema.xml").write_text(PHOSH_SCHEMA.format(id=schema_id, path=schema_id.replace(".", "/")))
    subprocess.run(["glib-compile-schemas", str(d)], check=True)
    world.extra_env["GSETTINGS_SCHEMA_DIR"] = f"{d}:{world.session.schemas}"


def maps_catalog(phone, installed=False, rating="mobile", **kw):
    software(phone)
    phone.app(component("org.gnome.Maps", "Maps", "Find places", pkg="gnome-maps", categories=["Utility"],
                        form_factor=rating, desktop="org.gnome.Maps.desktop", **kw))
    phone.package("gnome-maps", "48.2-1", installed=installed)


def details_of(name, category="All Apps"):
    return [CATALOG_READY, click(type="AdwActionRow", text=category), settle(200), open_first(category),
            wait_for(type="AdwNavigationPage", text=name)]


DOWNLOAD = ["resolving dependencies...", "", "Packages (3) gnome-maps-48.2-1 libshumate-1.4-1 geocode-glib-3.26-1",
            "", ":: Retrieving packages...", " gnome-maps-48.2-1-aarch64 downloading..."]
REST = [" libshumate-1.4-1-aarch64 downloading...", " geocode-glib-3.26-1-aarch64 downloading...",
        "checking keyring...", "checking package integrity...", ":: Processing package changes...",
        "installing libshumate...", "installing geocode-glib...", "installing gnome-maps...",
        ":: Running post-transaction hooks...", "(1/4) Arming ConditionNeedsUpdate...",
        "(2/4) Updating icon theme caches..."]


def progress(screen):
    page = screen.within(type="AdwNavigationPage", text="Installing Maps")
    bar = page.one(type="GtkProgressBar")
    # Status, step and count; the header bar's title last
    return [n["text"] for n in page.find(type="GtkLabel")][:-1], round(bar["fraction"], 3)


def test_install_shows_the_progress_and_opens_the_new_app(program, gcc_world, phone):
    maps_catalog(phone)
    phosh_schema(gcc_world)
    gcc_world.fake_bin.stub("maps-app")
    phone.desktop("org.gnome.Maps", "Maps", exec_="maps-app --new-window")
    release = gcc_world.tmp / "release"
    phone.helper(["install", "gnome-maps"], early="".join(f"{x}\n" for x in DOWNLOAD), wait=str(release),
                 lines=REST)
    r = gcc_world.run(program, details_of("Maps") + [
        click(type="GtkButton", text="Install"), wait_for(type="GtkLabel", text="1 of 3"), dump("download"),
        call("close_request", label="close while busy"), settle(), dump("asked"),
        {"op": "close_dialog"},
        {"op": "write_file", "path": str(release), "text": ""},
        wait_for(type="GtkLabel", text="Maps is installed", timeout=10000), dump("done"),
        {"op": "gsettings_get", "schema": "mobi.phosh.shell", "key": "force-adaptive", "label": "adaptive"},
        click(type="GtkButton", text="Open"), wait(300),
        call("close_request", label="close after"),
        click(type="GtkButton", text="Done"), settle(300), dump("home")])
    labels_, fraction = progress(r.dump("download"))
    assert labels_ == ["Installing Maps", "Step 1 of 4: Downloading packages", "1 of 3"]
    assert fraction == round(4 * (1 / 3) / 10, 3)
    assert r.value("close while busy") is True
    assert r.dump("asked").has(type="AdwAlertDialog", text="Please wait until the operation has finished")
    done = r.dump("done").within(type="AdwNavigationPage", text="Installing Maps")
    assert done.has(type="GtkImage", icon="object-select-symbolic", classes=["success"])
    assert done.has(type="GtkButton", text="Open", classes=["suggested-action"])
    assert done.has(type="GtkButton", text="Done")
    assert not done.has(type="GtkLabel", text="Details")
    assert not done.has(type="GtkProgressBar")
    assert phone.progs.calls("pkexec") == [[str(phone.path(HELPER)), "install", "gnome-maps"]]
    # Rated for phones: listed in Phosh's mobile overview
    assert r.value("adaptive") == "['org.already.Here.desktop', 'org.gnome.Maps.desktop']"
    assert gcc_world.fake_bin.calls("maps-app") == [["--new-window"]]
    assert r.value("close after") is False
    # Back on the panel; the catalog is read again
    assert r.dump("home").has(type="AdwActionRow", text="All Apps")
    assert phone.progs.calls("pacman").count(["-Sl"]) == 2
    assert not r.warnings()


def test_progress_through_checks_and_hooks(program, gcc_world, phone):
    maps_catalog(phone)
    release = gcc_world.tmp / "release"
    phone.helper(["install", "gnome-maps"], early="".join(f"{x}\n" for x in DOWNLOAD + REST),
                 wait=str(release))
    r = gcc_world.run(program, details_of("Maps") + [
        click(type="GtkButton", text="Install"), wait_for(type="GtkLabel", text="2 of 4"), dump("hooks"),
        {"op": "write_file", "path": str(release), "text": ""},
        wait_for(type="GtkLabel", text="Maps is installed", timeout=10000)])
    labels_, fraction = progress(r.dump("hooks"))
    assert labels_ == ["Installing Maps", "Step 4 of 4: Finishing up", "2 of 4"]
    assert fraction == round((4 + 1 + 4 + 0.5) / 10, 3)


def test_install_of_an_app_rated_unknown_leaves_phosh_alone(program, gcc_world, phone):
    maps_catalog(phone, rating=None)
    phosh_schema(gcc_world, "sm.puri.phosh")
    (gcc_world.home / ".config/plinius-software").mkdir(parents=True)
    (gcc_world.home / ".config/plinius-software/settings.ini").write_text("[Anzeige]\nshow-unknown=true\n")
    r = gcc_world.run(program, details_of("Maps") + [
        click(type="GtkButton", text="Install"), wait_for(type="GtkLabel", text="Maps is installed"),
        {"op": "gsettings_get", "schema": "sm.puri.phosh", "key": "force-adaptive", "label": "adaptive"}])
    assert r.value("adaptive") == "['org.already.Here.desktop']"


@pytest.mark.parametrize("extra, already", [("X-Purism-FormFactor=Workstation;Mobile;\n", False),
                                            ("X-KDE-FormFactor=desktop;MOBILE\n", False),
                                            ("", True)])
def test_phosh_list_only_gets_what_it_lacks(program, gcc_world, phone, extra, already):
    maps_catalog(phone)
    phosh_schema(gcc_world)
    phone.desktop("org.gnome.Maps", "Maps", extra=extra)
    steps = details_of("Maps")
    if already:
        steps.append({"op": "gsettings_set", "schema": "mobi.phosh.shell", "key": "force-adaptive",
                      "value": "['org.gnome.Maps.desktop']"})
    r = gcc_world.run(program, steps + [
        click(type="GtkButton", text="Install"), wait_for(type="GtkLabel", text="Maps is installed"),
        {"op": "gsettings_get", "schema": "mobi.phosh.shell", "key": "force-adaptive", "label": "adaptive"}])
    assert r.value("adaptive") == ("['org.gnome.Maps.desktop']" if already else "['org.already.Here.desktop']")


@pytest.mark.parametrize("code, title, text, buttons", [
    (8, "Waiting for approval", "Maps needs packages that are still being tested with the Plinius adaptations. "
     "It can be installed once they are approved.", ["Done"]),
    (9, "Another operation is running", "Wait until it has finished and try again.", ["Done"]),
    (3, "Update the system first", "The app belongs to a newer state than the installed system.",
     ["Go to Updates", "Done"]),
    (4, "Update the system first", "The app belongs to a newer state than the installed system.",
     ["Go to Updates", "Done"]),
    (1, "Installation failed", "See “Details” for more information", ["Done"]),
])
def test_install_failures_say_why(program, gcc_world, phone, code, title, text, buttons):
    maps_catalog(phone)
    phone.helper(["install", "gnome-maps"], lines=["error: failed to commit transaction", "\xff\xfe bad"],
                 code=code)
    steps = details_of("Maps") + [click(type="GtkButton", text="Install"),
                                  wait_for(type="GtkLabel", text=title), dump("result")]
    if "Go to Updates" in buttons:
        steps.append(click(type="GtkButton", text="Go to Updates"))
    r = gcc_world.run(program, steps)
    page = r.dump("result").within(type="AdwNavigationPage", text="Installing Maps")
    assert [n["text"] for n in page.find(type="GtkLabel")][:2] == [title, text]
    assert page.has(type="GtkImage", icon="dialog-warning-symbolic", classes=["error"])
    assert [b["text"] for b in page.find(type="GtkButton") if b.get("text")] == buttons
    # The log under Details, for errors
    assert page.has(type="GtkLabel", text="Details")
    if "Go to Updates" in buttons:
        assert r.app_actions == [{"action": "launch-panel", "parameter": "('updates', @av [])"}]


@pytest.mark.parametrize("code", [126, 127])
def test_dismissed_password_prompt_goes_back(program, gcc_world, phone, code):
    maps_catalog(phone)
    phone.helper(["install", "gnome-maps"], code=code)
    r = gcc_world.run(program, details_of("Maps") + [
        click(type="GtkButton", text="Install"), wait_for(type="GtkButton", text="Install"), settle(200),
        dump("back")])
    assert r.dump("back").has(type="AdwNavigationPage", text="Maps")
    assert not r.dump("back").has(type="AdwNavigationPage", text="Installing Maps")


def test_without_pkexec_the_install_cannot_start(program, gcc_world, phone):
    maps_catalog(phone)
    (gcc_world.fake_bin.path / "pkexec").unlink()
    r = gcc_world.run(program, details_of("Maps") + [
        click(type="GtkButton", text="Install"), wait_for(type="GtkLabel", text="Could not start")])
    page = r.screen.within(type="AdwNavigationPage", text="Installing Maps")
    assert any("pkexec" in t for t in page.texts())
    assert page.has(type="GtkButton", text="Done")


# ----- An installed app's page (Installed) -----

def answer(label):
    """Tap a response of the alert dialog shown."""
    return click(type="GtkButton", text=label, within={"type": "AdwAlertDialog"})


def package_group(screen):
    page = screen.within(type="AdwNavigationPage", text="App Settings")
    return page


def test_installed_app_page_shows_its_package_and_removes_it_with_its_addons(program, gcc_world, phone):
    software(phone)
    f = phone.desktop("org.mozilla.firefox", "Firefox")
    phone.owns(f, "firefox")
    phone.package("firefox", "140.0-1", installed=True, required_by=["firefox-i18n-de"])
    phone.package("firefox-i18n-de", "140.0-1", installed=True)
    phone.helper(prefix=["remove"], lines=[":: Processing package changes...", "removing firefox-i18n-de...",
                                           "removing firefox...", ":: Running post-transaction hooks...",
                                           "(1/1) Updating the desktop file MIME type cache..."])
    r = gcc_world.run(program, [
        settle(), call("open_app", "org.mozilla.firefox.desktop"), wait_for(type="GtkButton", text="Remove"),
        dump("page"), call("explore_visible"),
        click(type="GtkButton", text="Remove"), wait_for(type="AdwAlertDialog"), dump("ask"),
        {"op": "close_dialog"}, settle(),
        click(type="GtkButton", text="Remove"), wait_for(type="AdwAlertDialog"),
        answer("Remove"),
        wait_for(type="GtkLabel", text="Firefox has been removed", timeout=10000), dump("done")])
    page = package_group(r.dump("page"))
    assert page.within(type="AdwPreferencesGroup", text="Details").rows() == [("Package", "firefox 140.0-1")]
    assert page.has(type="GtkButton", text="Remove", classes=["destructive-action"])
    assert not page.has(type="GtkLabel", text="Part of the system (Phosh, phone) and cannot be removed.")
    # The page belongs to Installed
    assert r.value("explore_visible") is False
    ask = r.dump("ask").one(type="AdwAlertDialog")
    assert ask["text"] == "Remove Firefox?"
    assert ask["subtitle"] == ("The app and dependencies that are no longer needed will be removed. "
                               "Your files and settings are kept.")
    # Cancelled once, then removed with its add-on
    assert phone.helper_calls() == [["remove", "firefox", "firefox-i18n-de"]]
    done = r.dump("done").within(type="AdwNavigationPage", text="Removing Firefox")
    assert done.has(type="GtkButton", text="Done")
    calls = phone.progs.calls("pacman")
    assert ["-Qqo", str(f)] in calls and ["-Qi", "firefox"] in calls and ["-Qi", "firefox-i18n-de"] in calls


@pytest.mark.parametrize("code, title", [(1, "Removal failed"), (9, "Another operation is running")])
def test_failed_removal(program, gcc_world, phone, code, title):
    software(phone)
    f = phone.desktop("org.gnome.Maps", "Maps")
    phone.owns(f, "gnome-maps")
    phone.package("gnome-maps", "48.2-1", installed=True)
    phone.helper(prefix=["remove"], code=code)
    gcc_world.run(program, [
        settle(), call("open_app", "org.gnome.Maps.desktop"), wait_for(type="GtkButton", text="Remove"),
        click(type="GtkButton", text="Remove"), wait_for(type="AdwAlertDialog"),
        answer("Remove"), wait_for(type="GtkLabel", text=title, timeout=10000)])
    assert phone.helper_calls() == [["remove", "gnome-maps"]]


def test_what_another_package_needs_is_part_of_the_system(program, gcc_world, phone):
    software(phone)
    f = phone.desktop("org.gnome.Calls", "Calls")
    phone.owns(f, "gnome-calls")
    # Needed by plinius-apps; and an add-on that is itself needed
    phone.package("gnome-calls", "48-1", installed=True, required_by=["plinius-apps"])
    phone.package("plinius-apps", "1-1", installed=True)
    g = phone.desktop("org.gnome.Clocks", "Clocks")
    phone.owns(g, "gnome-clocks")
    phone.package("gnome-clocks", "48-1", installed=True, required_by=["gnome-clocks-extra"])
    phone.package("gnome-clocks-extra", "1-1", installed=True, required_by=["other"])
    r = gcc_world.run(program, [
        settle(), call("open_app", "org.gnome.Calls.desktop"),
        wait_for(type="GtkLabel", text="Part of the system (Phosh, phone) and cannot be removed."), dump("calls"),
        call("open_app", "org.gnome.Clocks.desktop", label="open clocks"),
        wait_for(type="AdwActionRow", subtitle="gnome-clocks 48-1"), dump("clocks")])
    for label in ("calls", "clocks"):
        page = package_group(r.dump(label))
        assert not page.has(type="GtkButton", text="Remove")
        assert page.has(type="GtkLabel", text="Part of the system (Phosh, phone) and cannot be removed.")
    assert package_group(r.dump("calls")).within(type="AdwPreferencesGroup", text="Details").rows() == [
        ("Package", "gnome-calls 48-1")]


def test_flatpak_app_page_shows_its_source(program, gcc_world, phone):
    software(phone, flathub=True)
    exports = gcc_world.tmp / "flatpak" / "exports" / "share"
    gcc_world.extra_env["XDG_DATA_DIRS"] = f"{gcc_world.data_dir}:{exports}"
    phone.desktop("org.example.Flat", "Flat", where=exports / "applications")
    phone.desktop("org.example.Bare", "Bare", where=exports / "applications")
    phone.flatpak(info={"org.example.Flat": ("1.2", "furihub")})
    phone.helper(prefix=["flatpak-remove"])
    r = gcc_world.run(program, [
        settle(), call("open_app", "org.example.Flat.desktop"), wait_for(type="AdwActionRow", text="Source"),
        dump("flat"), click(type="GtkButton", text="Remove"), wait_for(type="AdwAlertDialog"),
        answer("Remove"), wait_for(type="GtkLabel", text="Flat has been removed", timeout=10000),
        click(type="GtkButton", text="Done"), settle(),
        call("open_app", "org.example.Bare.desktop", label="open bare"),
        wait_for(type="AdwActionRow", text="Source"), dump("bare")])
    assert package_group(r.dump("flat")).within(type="AdwPreferencesGroup", text="Details").rows() == [
        ("Source", "FuriHub 1.2")]
    assert phone.helper_calls() == [["flatpak-remove", "org.example.Flat"]]
    # flatpak knows nothing of it: the source without a version
    assert package_group(r.dump("bare")).within(type="AdwPreferencesGroup", text="Details").rows() == [
        ("Source", "Flatpak")]


def test_apps_not_from_a_package_and_settings_itself_show_nothing(program, gcc_world, phone):
    software(phone)
    phone.desktop("org.local.Script", "Script")
    phone.desktop("org.gnome.Settings", "Settings")
    r = gcc_world.run(program, [
        settle(), call("open_app", "org.local.Script.desktop"), settle(500), dump("local"),
        call("open_app", "org.gnome.Settings.desktop", label="open settings"), settle(300), dump("settings"),
        call("open_app", "/bin/true --loose", label="open loose"), settle(300), dump("loose")])
    for label in ("local", "settings", "loose"):
        page = package_group(r.dump(label))
        assert not page.has(type="AdwPreferencesGroup", text="Details"), label
        assert not page.has(type="GtkButton", text="Remove"), label
    # Settings and the app without a file are not even looked up
    assert [c for c in phone.progs.calls("pacman") if c[0] == "-Qqo"] == [
        ["-Qqo", str(gcc_world.data_dir / "applications" / "org.local.Script.desktop")]]


def test_another_app_while_the_first_is_looked_up(program, gcc_world, phone):
    software(phone)
    f = phone.desktop("org.slow.App", "Slow")
    g = phone.desktop("org.fast.App", "Fast")
    phone.owns(g, "fast")
    phone.package("fast", "2-1", installed=True)
    phone.progs.rule("pacman", ["-Qqo", str(f)], first=True, out="slow\n", sleep=0.6)
    r = gcc_world.run(program, [
        settle(), call("open_app", "org.slow.App.desktop"), call("open_app", "org.fast.App.desktop", label="b"),
        wait(1200), dump("page")])
    assert package_group(r.dump("page")).within(type="AdwPreferencesGroup", text="Details").rows() == [
        ("Package", "fast 2-1")]


def test_without_plinius_software_the_app_page_stays_gnomes(program, gcc_world, phone):
    phone.path(HELPER).unlink()
    f = phone.desktop("org.gnome.Maps", "Maps")
    phone.owns(f, "gnome-maps")
    r = gcc_world.run(program, [settle(), dump("tabs"), call("open_app", "org.gnome.Maps.desktop"),
                                wait_for(type="GtkButton", text="Remove"), call("explore_visible")])
    tabs = [b["children"][-1]["text"] for b in r.dump("tabs").find(type="AdwViewSwitcherButton")]
    assert tabs == ["Installed", "Web Apps", "Settings"]
    assert r.value("explore_visible") is False


# ----- Flatpak apps in Explore -----

def flatpak_catalog(phone, furihub=False):
    software(phone, flathub=True, furihub=furihub)
    phone.app(component("org.example.Flat", "Flat", "A Flatpak", bundle="app/org.example.Flat/aarch64/stable",
                        form_factor="mobile", version="2.0", desktop="org.example.Flat.desktop"),
              component("org.example.Furi", "Furi", "From FuriHub", bundle="app/org.example.Furi/aarch64/stable",
                        form_factor="mobile", categories=["Game"]),
              # Also a pacman package: that one wins
              component("org.gnome.Maps", "Maps (Flatpak)", "Flat maps", bundle="app/org.gnome.Maps/aarch64/stable",
                        form_factor="mobile"),
              # Installed as Flatpak: wins over the package
              component("org.gnome.Music", "Music (Flatpak)", "Flat music",
                        bundle="app/org.gnome.Music/aarch64/stable", form_factor="mobile"),
              component("org.broken.Ref", "Broken", "No ref", bundle="broken", form_factor="mobile"),
              flatpak=True)
    phone.app(component("org.gnome.Maps", "Maps", "Find places", pkg="gnome-maps", form_factor="mobile"),
              component("org.gnome.Music", "Music", "Play music", pkg="gnome-music", form_factor="mobile"),
              # Provided by a build of ours
              component("org.gnome.Settings", "Settings", "Settings", pkg="gnome-control-center",
                        form_factor="mobile"))
    phone.package("gnome-maps")
    phone.package("gnome-music")
    phone.package("gnome-control-center", "51.1-1")
    phone.package("gnome-control-center-plinius", "51.1-96", installed=True, provides=["gnome-control-center=51.1"],
                  repo="plinius")
    phone.flatpak(installed=[("org.gnome.Music", "flathub")],
                  furihub=["app/org.example.Furi/aarch64/stable"] if furihub else [])


def test_flathub_apps_join_explore_with_their_source(program, gcc_world, phone):
    flatpak_catalog(phone, furihub=True)
    r = gcc_world.run(program, [CATALOG_READY, click(type="AdwActionRow", text="All Apps"), settle(200),
                                dump("all")])
    assert app_rows(r.dump("all"), "All Apps") == [
        ("Flat", "A Flatpak", "For phones · Flathub", ["success"]),
        ("Furi", "From FuriHub", "For phones · FuriHub", ["success"]),
        ("Maps", "Find places", "For phones", ["success"])]


def test_flatpak_install_opens_through_flatpak_run(program, gcc_world, phone):
    flatpak_catalog(phone)
    phone.helper(prefix=["flatpak-install"], lines=["Installing app/org.example.Flat", "\xff"])
    r = gcc_world.run(program, details_of("Flat") + [
        dump("details"), click(type="GtkButton", text="Install"),
        wait_for(type="GtkLabel", text="Flat is installed", timeout=10000), click(type="GtkButton", text="Open"),
        wait(300)])
    details = r.dump("details").within(type="AdwNavigationPage", text="Flat")
    assert details.within(type="AdwPreferencesGroup", text="Details").rows() == [("Source", "Flathub 2.0")]
    assert phone.helper_calls() == [["flatpak-install", "app/org.example.Flat/aarch64/stable"]]
    assert ["run", "org.example.Flat"] in phone.progs.calls("flatpak")


def test_flatpak_without_a_release_shows_only_its_source(program, gcc_world, phone):
    flatpak_catalog(phone, furihub=True)
    r = gcc_world.run(program, details_of("Furi", "Games") + [dump("details")])
    details = r.dump("details").within(type="AdwNavigationPage", text="Furi")
    assert details.within(type="AdwPreferencesGroup", text="Details").rows() == [("Source", "FuriHub")]


def test_without_any_catalog_explore_says_so(program, gcc_world, phone):
    software(phone)
    r = gcc_world.run(program, [wait_for(type="AdwStatusPage", text="No App Catalog", timeout=20000),
                                dump("explore")])
    page = r.dump("explore").one(type="AdwStatusPage")
    assert page["subtitle"] == "The catalog could not be loaded. Is the package archlinux-appstream-data missing?"


def test_a_broken_catalog_file_is_no_catalog_either(program, gcc_world, phone):
    software(phone)
    phone.write("/catalog/os/xml/test-os.xml", "<components><component>")
    r = gcc_world.run(program, [wait_for(type="AdwStatusPage", text="No App Catalog", timeout=20000)])
    assert r.returncode == 0


# ----- Settings: sources and the update channel -----

def sources(screen):
    group = screen.one(type="AdwPreferencesGroup", text="Software Sources")
    return [(n["text"], n.get("subtitle"), n["active"]) for n in Screen_nodes(group) if n["type"] == "AdwSwitchRow"]


def Screen_nodes(node):
    from gcc_harness import Node
    return list(Node(node).walk())


PLINIUS_STABLE = "[options]\nArchitecture = aarch64\n\n[core]\nInclude = mirrorlist\n\n[plinius]\n" \
                 "Server = https://plinius.example.org/repo/$arch\n"
PLINIUS_BETA = "[options]\n[plinius]\n  Server = https://plinius.example.org/repo/beta/$arch  \n[extra]\n"


def test_settings_tab_shows_the_sources(program, gcc_world, phone):
    software(phone, flathub=True)
    phone.write("/etc/pacman.conf", PLINIUS_BETA)
    r = gcc_world.run(program, [settle(), select_tab("settings"), dump("settings")])
    assert sources(r.dump("settings")) == [
        ("Flathub", "Flatpak apps, independent of system updates", True),
        ("FuriHub", "Phone apps from the FuriLabs community, needs Flathub", False),
        ("Beta Updates", "New Plinius versions before their release", True)]


@pytest.mark.parametrize("conf", [None, "[options]\n[core]\nServer = https://example.org/beta/x\n"])
def test_no_beta_switch_without_our_repo(program, gcc_world, phone, conf):
    software(phone)
    if conf:
        phone.write("/etc/pacman.conf", conf)
    r = gcc_world.run(program, [settle(), select_tab("settings"), dump("settings")])
    assert [s[0] for s in sources(r.dump("settings"))] == ["Flathub", "FuriHub"]


def test_settings_without_default_apps_group(program, gcc_world, phone):
    software(phone)
    r = gcc_world.run(program, [settle(), select_tab("settings"), dump("settings")], params={"no_defaults": "1"})
    assert not r.dump("settings").has(type="AdwActionRow", text="Default Apps")


def test_settings_without_plinius_software_keep_default_apps_only(program, gcc_world, phone):
    phone.path(HELPER).unlink()
    r = gcc_world.run(program, [settle(), select_tab("settings"), dump("settings")])
    screen = r.dump("settings")
    assert screen.has(type="AdwActionRow", text="Default Apps")
    assert not screen.has(type="AdwPreferencesGroup", text="Software Sources")


SOURCE_CASES = [
    # row, turn on?, dialog heading, helper args, code, result title, row after
    ("Flathub", True, "Turn On Flathub?", ["flathub-enable"], 0, "Flathub is on", True),
    ("Flathub", True, "Turn On Flathub?", ["flathub-enable"], 1, "Could not turn on Flathub", False),
    ("Flathub", True, "Turn On Flathub?", ["flathub-enable"], 9, "Another operation is running", False),
    ("Flathub", False, None, ["flathub-disable"], 0, "Flathub is off", False),
    ("Flathub", False, None, ["flathub-disable"], 5, "Apps from Flathub are still installed", True),
    ("Flathub", False, None, ["flathub-disable"], 11, "FuriHub needs Flathub", True),
    ("Flathub", False, None, ["flathub-disable"], 1, "Could not turn off Flathub", True),
    ("FuriHub", True, "Turn On FuriHub?", ["furihub-enable"], 0, "FuriHub is on", True),
    ("FuriHub", True, "Turn On FuriHub?", ["furihub-enable"], 1, "Could not turn on FuriHub", False),
    ("FuriHub", False, None, ["furihub-disable"], 0, "FuriHub is off", False),
    ("FuriHub", False, None, ["furihub-disable"], 5, "Apps from FuriHub are still installed", True),
    ("FuriHub", False, None, ["furihub-disable"], 1, "Could not turn off FuriHub", True),
    ("Beta Updates", True, "Turn On Beta Updates?", ["channel-beta"], 0, "Beta updates are on", True),
    ("Beta Updates", True, "Turn On Beta Updates?", ["channel-beta"], 1, "Could not change the update channel", False),
    ("Beta Updates", False, None, ["channel-stable"], 0, "Beta updates are off", False),
    ("Beta Updates", False, None, ["channel-stable"], 9, "Another operation is running", True),
]


@pytest.mark.parametrize("row, on, heading, args, code, title, after", SOURCE_CASES)
def test_source_switches_follow_plinius_pkg(program, gcc_world, phone, row, on, heading, args, code, title, after):
    flathub = (row == "Flathub" and not on) or row == "FuriHub"
    furihub = row == "FuriHub" and not on
    software(phone, flathub=flathub, furihub=furihub)
    beta = row == "Beta Updates" and not on
    phone.write("/etc/pacman.conf", PLINIUS_BETA if beta else PLINIUS_STABLE)
    # What plinius-pkg changes on success
    write = {}
    if code == 0 and row == "Beta Updates":
        write = {str(phone.path("/etc/pacman.conf")): PLINIUS_STABLE if beta else PLINIUS_BETA}
    phone.helper(args, lines=["::plinius:: flathub"], code=code, write=write)
    if code == 0 and row in ("Flathub", "FuriHub"):
        remotes = (["flathub"] if (row == "Flathub" and on) or row == "FuriHub" else []) + \
                  (["furihub"] if row == "FuriHub" and on else [])
        phone.progs.rule("flatpak", ["remotes", "--system", "--columns=name"], first=True,
                         outs=[f"{'flathub' if flathub else ''}\n{'furihub' if furihub else ''}\n"] * 2
                         + ["".join(f"{x}\n" for x in remotes)])
    steps = [settle(), select_tab("settings"), set_active(on, type="AdwSwitchRow", text=row)]
    if heading:
        steps += [wait_for(type="AdwAlertDialog"), dump("ask"), answer("Turn On")]
    steps += [wait_for(type="GtkLabel", text=title, timeout=10000), dump("result"),
              click(type="GtkButton", text="Done"), settle(), dump("after")]
    r = gcc_world.run(program, steps)
    if heading:
        assert r.dump("ask").one(type="AdwAlertDialog")["text"] == heading
    assert phone.helper_calls() == [args]
    switches = {n: a for n, _s, a in sources(r.dump("after"))}
    assert switches[row] is after
    if title == "Beta updates are on":
        assert r.dump("result").has(type="GtkButton", text="Go to Updates")


@pytest.mark.parametrize("row", ["Flathub", "FuriHub", "Beta Updates"])
def test_cancelled_turn_on_leaves_the_switch_off(program, gcc_world, phone, row):
    software(phone)
    phone.write("/etc/pacman.conf", PLINIUS_STABLE)
    r = gcc_world.run(program, [settle(), select_tab("settings"), set_active(True, type="AdwSwitchRow", text=row),
                                wait_for(type="AdwAlertDialog"), answer("Cancel"), settle(), dump("after")])
    assert {n: a for n, _s, a in sources(r.dump("after"))}[row] is False
    assert phone.helper_calls() == []


def test_furihub_on_turns_flathub_on_too(program, gcc_world, phone):
    software(phone)
    phone.helper(["furihub-enable"], code=1)
    phone.progs.rule("flatpak", ["remotes", "--system", "--columns=name"], first=True,
                     outs=["", "", "flathub\n"])
    r = gcc_world.run(program, [settle(), select_tab("settings"), set_active(True, type="AdwSwitchRow", text="FuriHub"),
                                wait_for(type="AdwAlertDialog"), answer("Turn On"),
                                wait_for(type="GtkLabel", text="Could not turn on FuriHub", timeout=10000),
                                click(type="GtkButton", text="Done"), settle(), dump("after")])
    assert {n: a for n, _s, a in sources(r.dump("after"))} == {"Flathub": True, "FuriHub": False}


# ----- Updates -----

UPDATES = {"panel": "updates"}
CHECKED = wait_for(type="GtkButton", text="Check Again", timeout=10000)


def status(screen):
    """Title, description and the buttons shown on the Updates page."""
    page = screen.within(type="CcUpdatesPanel")
    texts = [n["text"] for n in page.find(type="GtkLabel")
             if n["text"] not in ("Updates", "Test Panel")]
    buttons = [n["text"] for n in page.find(type="GtkButton") if n.get("text")]
    return texts, buttons


def test_updates_lists_packages_and_flatpaks(program, gcc_world, phone):
    phone.updates(code=0, packages=[["linux-plinius", "6.1.192-19", "6.1.192-20"], ["short", "1"], None],
                  flatpaks=[["org.example.Flat", "2.0", "furihub"], ["org.example.Old", "1.1"], ["x"],
                            ["org.kde.App", "3", "kdeapps"],
                            ["org.example.Bare", "", ""]],
                  last_check="Today, 10:00")
    r = gcc_world.run(program, [settle(), CHECKED, settle(), dump("page")], params=UPDATES)
    screen = r.dump("page")
    texts, buttons = status(screen)
    assert texts[:2] == ["5 updates available", "The whole system is always updated at once.\nChecked: Today, 10:00"]
    assert buttons == ["Check Again", "Update Now"]
    assert screen.within(type="AdwPreferencesGroup", text="Packages").rows() == [
        ("linux-plinius", "6.1.192-19 → 6.1.192-20"), ("org.example.Flat", "FuriHub 2.0"),
        ("org.example.Old", "Flathub 1.1"), ("org.kde.App", "kdeapps 3"), ("org.example.Bare", "Flatpak")]
    assert phone.progs.calls("plinius-software") == [["--updates-json"]]


@pytest.mark.parametrize("result, texts, update_now", [
    ({"code": 0, "last_check": "Today, 10:00"}, ["The system is up to date", "Last checked: Today, 10:00"], False),
    ({"code": 2}, ["The system is up to date"], False),
    ({"code": 1, "last_check": "Yesterday"},
     ["Check failed", "No connection to the server? You can still update.\nLast successful check: Yesterday"], True),
    ({"code": 1}, ["Check failed", "No connection to the server? You can still update."], True),
    ({"code": 1, "packages": [["a", "1", "2"]], "last_check": "Yesterday"},
     ["1 update available", "The whole system is always updated at once."], True),
    ({"code": 0, "last_check": "Today", "held": {"title": "2 updates wait for approval",
                                                 "text": "Plinius has not yet approved x 1."}},
     ["2 updates wait for approval", "Plinius has not yet approved x 1.\nLast checked: Today"], False),
    ({"code": 0, "held": {"title": "2 updates wait for approval"}}, ["2 updates wait for approval"], False),
    ({"code": 0, "held": {"title": "1 update waits for approval"}, "flatpaks": [["org.a.B", "1", "flathub"]],
      "last_check": "Today"},
     ["1 update available", "1 update waits for approval\nChecked: Today"], True),
    ({"code": 0, "held": "odd"}, ["The system is up to date"], False),
    ("not json", ["Check failed", "No connection to the server? You can still update."], True),
    ("[1, 2]", ["Check failed", "No connection to the server? You can still update."], True),
])
def test_update_check_results(program, gcc_world, phone, result, texts, update_now):
    if isinstance(result, dict):
        phone.updates(**result)
    else:
        phone.updates(result)
    r = gcc_world.run(program, [settle(), CHECKED, settle(), dump("page")], params=UPDATES)
    shown, buttons = status(r.dump("page"))
    assert shown[:len(texts)] == texts
    assert len(shown) == len(texts)
    assert ("Update Now" in buttons) is update_now


def test_without_plinius_software_the_check_fails(program, gcc_world, phone):
    phone.path(SOFTWARE).unlink()
    r = gcc_world.run(program, [settle(), CHECKED, dump("page")], params=UPDATES)
    assert status(r.dump("page"))[0][0] == "Check failed"


def test_check_again_and_the_update_servers_warning(program, gcc_world, phone):
    warning = "The update server's state is 3 days old. Updates may be held back."
    phone.updates({"code": 0, "repo_warning": warning, "packages": [], "flatpaks": []},
                  {"code": 1},
                  {"code": 0, "repo_warning": None},
                  {"code": 0, "repo_warning": ""})
    steps = [settle(), CHECKED, dump("warned")]
    for label in ("failed", "fine", "empty"):
        steps += [click(type="GtkButton", text="Check Again"), settle(300), CHECKED, dump(label)]
    r = gcc_world.run(program, steps, params=UPDATES)
    assert r.dump("warned").has(type="AdwBanner", text=warning)
    # A failed check says nothing about the stamp: the warning stays
    assert r.dump("failed").has(type="AdwBanner", text=warning)
    assert status(r.dump("failed"))[0][0] == "Check failed"
    assert len(phone.progs.calls("plinius-software")) == 4


def test_updates_panel_is_listed_only_with_plinius_software(program, gcc_world, phone):
    r = gcc_world.run(program, [settle(), call("static_init")], params=UPDATES)
    phone.path(HELPER).unlink()
    r2 = gcc_world.run(program, [settle(), call("static_init")], params=UPDATES)
    assert r.visibility == {"updates": "visible"}
    assert r2.visibility == {"updates": "hidden"}


def upgrade(phone, code=0, restart=None, lines=(":: Starting full system upgrade...",), **kw):
    phone.updates({"code": 0, "packages": [["a", "1", "2"]], "flatpaks": []}, {"code": 0})
    write = {str(phone.path(RESTART_RESULT)): json.dumps(restart)} if restart is not None else {}
    phone.helper(["upgrade"], lines=lines, code=code, write=write, **kw)


UPDATE_NOW = [settle(), CHECKED, click(type="GtkButton", text="Update Now")]


@pytest.mark.parametrize("flathub, steps", [(False, 5), (True, 6)])
def test_update_steps_with_and_without_flathub(program, gcc_world, phone, flathub, steps):
    phone.flatpak(remotes=["flathub"] if flathub else [])
    release = gcc_world.tmp / "release"
    upgrade(phone, restart={"level": "none"}, early=":: Synchronizing package databases...\n", wait=str(release),
            lines=[":: Starting full system upgrade...", "::plinius:: flatpak", "Updating org.example.Flat"])
    r = gcc_world.run(program, UPDATE_NOW + [
        wait_for(type="GtkLabel", contains="Fetching package lists"), dump("sync"),
        {"op": "write_file", "path": str(release), "text": ""},
        wait_for(type="GtkLabel", text="The system has been updated", timeout=10000), dump("done"),
        click(type="GtkButton", text="Done"), settle(), CHECKED, dump("after")], params=UPDATES)
    assert r.dump("sync").has(type="GtkLabel", text=f"Step 1 of {steps}: Fetching package lists")
    done = r.dump("done").within(type="AdwNavigationPage", text="Updating the system")
    assert done.has(type="GtkLabel", text="Everything that is running already uses the new versions.")
    assert phone.helper_calls() == [["upgrade"]]
    # Checked again after the update
    assert status(r.dump("after"))[0][0] == "The system is up to date"
    assert len(phone.progs.calls("plinius-software")) == 2


@pytest.mark.parametrize("code, title, text", [
    (6, "The system is already up to date", None),
    (10, "Battery too low", "Connect the charger or charge the phone to at least 10 %, then update. An update "
     "interrupted by an empty battery can leave the system unable to start."),
    (12, "Update stopped", "The update server's package list does not match its signed state. Nothing was "
     "installed. Try again later; if it stays like this, the server may not be trustworthy."),
    (9, "Another operation is running", "Wait until it has finished and try again."),
    (1, "Update failed", "See “Details” for more information"),
])
def test_update_results(program, gcc_world, phone, code, title, text):
    upgrade(phone, code=code)
    r = gcc_world.run(program, UPDATE_NOW + [wait_for(type="GtkLabel", text=title, timeout=10000), dump("done")],
                      params=UPDATES)
    page = r.dump("done").within(type="AdwNavigationPage", text="Updating the system")
    shown = [n["text"] for n in page.find(type="GtkLabel")]
    assert shown[0] == title
    if text:
        assert shown[1] == text
    assert page.has(type="GtkButton", text="Done")


ME = os.getuid()


def test_only_flatpaks_updated_need_no_restart(program, gcc_world, phone):
    upgrade(phone, code=7, restart={"level": "apps", "apps": [{"id": "org.other.App", "uid": ME + 1}]})
    r = gcc_world.run(program, UPDATE_NOW + [
        wait_for(type="GtkLabel", text="The Flatpak apps have been updated", timeout=10000)], params=UPDATES)
    assert r.screen.has(type="GtkLabel", text="Everything that is running already uses the new versions.")


def test_restart_apps_after_asking(program, gcc_world, phone):
    phone.desktop("org.gnome.Maps", "Maps")
    upgrade(phone, restart={"level": "apps", "apps": [
        {"id": "org.gnome.Maps", "uid": ME}, {"id": "org.gnome.Maps", "uid": ME},
        {"id": "org.unknown.App", "uid": ME}, {"uid": ME}, {"id": "org.other.App", "uid": ME + 1}]})
    phone.progs.rule("plinius-restart", prefix=["apply"], code=0)
    r = gcc_world.run(program, UPDATE_NOW + [
        wait_for(type="GtkButton", text="Restart Apps", timeout=10000), dump("offer"),
        click(type="GtkButton", text="Restart Apps"), wait_for(type="AdwAlertDialog"), dump("ask"),
        answer("Cancel"), settle(),
        click(type="GtkButton", text="Restart Apps"), wait_for(type="AdwAlertDialog"),
        answer("Restart Apps"), wait_for(type="GtkButton", text="Done", timeout=10000), dump("done")],
        params=UPDATES)
    offer = r.dump("offer").within(type="AdwNavigationPage", text="Updating the system")
    assert offer.has(type="GtkLabel", text="Restart Maps, org.unknown.App to use the new versions. "
                                           "Unsaved changes in them are lost.")
    ask = r.dump("ask").one(type="AdwAlertDialog")
    assert (ask["text"], ask["subtitle"]) == ("Restart Apps?", "Maps, org.unknown.App close and open again. "
                                                               "Unsaved changes in them are lost.")
    assert phone.progs.calls("systemd-run") == [["--user", "--scope", "--collect", "--quiet", "--",
                                                 str(phone.path(RESTART_HELPER)), "apply", "apps",
                                                 str(phone.path(RESTART_RESULT))]]
    assert phone.progs.calls("plinius-restart") == [["apply", "apps", str(phone.path(RESTART_RESULT))]]
    assert r.dump("done").has(type="GtkLabel", text="The apps have been restarted. Everything that is running "
                                                    "now uses the new versions.")


def test_a_failed_restart_can_be_tried_again(program, gcc_world, phone):
    phone.desktop("org.gnome.Maps", "Maps")
    upgrade(phone, restart={"level": "apps", "apps": [{"id": "org.gnome.Maps", "uid": ME}]})
    phone.progs.rule("plinius-restart", prefix=["apply"], code=1)
    r = gcc_world.run(program, UPDATE_NOW + [
        wait_for(type="GtkButton", text="Restart Apps", timeout=10000),
        click(type="GtkButton", text="Restart Apps"), wait_for(type="AdwAlertDialog"), answer("Restart Apps"),
        wait(800), dump("after")], params=UPDATES)
    button = r.dump("after").one(type="GtkButton", text="Restart Apps")
    assert button.get("sensitive", True) is True
    assert [m["message"] for m in r.warnings()] == ["The restart failed: Child process exited with code 1"]
    assert phone.progs.calls("plinius-restart") == [["apply", "apps", str(phone.path(RESTART_RESULT))]]


def test_without_systemd_run_the_restart_does_not_start(program, gcc_world, phone):
    phone.desktop("org.gnome.Maps", "Maps")
    upgrade(phone, restart={"level": "apps", "apps": [{"id": "org.gnome.Maps", "uid": ME}]})
    (gcc_world.fake_bin.path / "systemd-run").unlink()
    r = gcc_world.run(program, UPDATE_NOW + [
        wait_for(type="GtkButton", text="Restart Apps", timeout=10000),
        click(type="GtkButton", text="Restart Apps"), wait_for(type="AdwAlertDialog"), answer("Restart Apps"),
        settle(300)], params=UPDATES)
    assert [m["message"] for m in r.warnings()][0].startswith("Could not start the restart: ")


def shell_units(*units):
    return [{"unit": u, "uid": uid, "kind": "service"} for u, uid in units]


@pytest.mark.parametrize("apps, text", [
    ([], "Restart Phosh to use the new versions. The phone locks; open apps stay open."),
    ([{"id": "org.gnome.Maps", "uid": ME}], "Restart Phosh to use the new versions. The phone locks; open apps "
     "stay open. Maps also restart. Unsaved changes in them are lost."),
])
def test_restart_phosh_after_asking(program, gcc_world, phone, apps, text):
    phone.desktop("org.gnome.Maps", "Maps")
    upgrade(phone, restart={"level": "ui", "apps": apps, "units": shell_units(("mobi.phosh.Shell.service", ME),
                                                                              ("other.service", ME + 1))})
    phone.progs.rule("systemctl", prefix=["--user", "show"], out="1000\n")
    r = gcc_world.run(program, UPDATE_NOW + [
        wait_for(type="GtkButton", text="Restart Phosh", timeout=10000), settle(300), dump("offer"),
        click(type="GtkButton", text="Restart Phosh"), wait_for(type="AdwAlertDialog"), dump("ask"),
        answer("Restart Phosh"), wait_for(type="GtkButton", text="Done", timeout=10000), dump("done")],
        params=UPDATES)
    assert r.dump("offer").has(type="GtkLabel", text=text)
    ask = r.dump("ask").one(type="AdwAlertDialog")
    assert ask["text"] == "Restart Phosh?"
    assert ask["subtitle"].startswith("Phosh restarts and the phone locks. Open apps stay open.")
    assert ("Maps also restart." in ask["subtitle"]) is bool(apps)
    assert phone.progs.calls("plinius-restart") == [["apply", "ui", str(phone.path(RESTART_RESULT))]]
    assert r.dump("done").has(type="GtkLabel", text="Phosh has been restarted. Everything that is running now "
                                                    "uses the new versions.")
    # Phosh alone waits: its start is watched
    assert phone.progs.calls("systemctl")[0] == ["--user", "show", "--property=ExecMainStartTimestampMonotonic",
                                                 "--value", "mobi.phosh.Shell.service"]


@pytest.mark.parametrize("apps, after, button", [
    ([{"id": "org.gnome.Maps", "uid": ME}],
     "Phosh has been restarted. Restart Maps to use their new versions. Unsaved changes in them are lost.",
     "Restart Apps"),
    ([], "Phosh has been restarted. Everything that is running now uses the new versions.", "Done"),
])
def test_phosh_restarted_from_the_power_menu(program, gcc_world, phone, apps, after, button):
    phone.desktop("org.gnome.Maps", "Maps")
    upgrade(phone, restart={"level": "ui", "apps": apps, "units": shell_units(("mobi.phosh.Shell.service", ME))})
    phone.progs.rule("systemctl", prefix=["--user", "show"], outs=["1000\n", "1000\n", "0\n", "2000\n"])
    activate = call("notify_active")
    r = gcc_world.run(program, UPDATE_NOW + [
        wait_for(type="GtkButton", text="Restart Phosh", timeout=10000), settle(300),
        activate, settle(300), dump("same"), activate, settle(300), dump("stopped"),
        activate, wait_for(type="GtkButton", text=button, timeout=5000), dump("after")], params=UPDATES)
    for label in ("same", "stopped"):
        assert r.dump(label).has(type="GtkButton", text="Restart Phosh"), label
    assert r.dump("after").has(type="GtkLabel", text=after)
    assert len(phone.progs.calls("systemctl")) == 4


def test_phosh_with_other_units_is_not_watched(program, gcc_world, phone):
    upgrade(phone, restart={"level": "ui", "units": shell_units(("mobi.phosh.Shell.service", ME),
                                                                ("feedbackd.service", ME))})
    gcc_world.run(program, UPDATE_NOW + [wait_for(type="GtkButton", text="Restart Phosh", timeout=10000),
                                             call("notify_active"), settle(300)], params=UPDATES)
    assert phone.progs.calls("systemctl") == []


def restart_phone(program, gcc_world, phone, restart, params, **replies):
    upgrade(phone, restart=restart if restart != "broken" else None)
    if restart == "broken":
        phone.write(RESTART_RESULT, "{nope")
    gcc_world.service("system", LOGIN1, LOGIN1_PATH, LOGIN1_XML, replies=replies)
    r = gcc_world.run(program, UPDATE_NOW + [
        wait_for(type="GtkButton", text="Restart Phone", timeout=10000), dump("offer"),
        click(type="GtkButton", text="Restart Phone"), wait_for(type="AdwAlertDialog"), dump("ask"),
        answer("Restart Phone"), settle(300),
        # Behind the panel's call on the same connection: once answered, that one has arrived
        {"op": "dbus", "bus": "system", "dest": LOGIN1, "path": "/", "iface": "org.freedesktop.DBus.Peer",
         "method": "Ping", "label": "barrier"}], params=params)
    assert r.dump("offer").has(type="GtkLabel", text="Restart the phone to run the new versions.")
    ask = r.dump("ask").one(type="AdwAlertDialog")
    assert (ask["text"], ask["subtitle"]) == ("Restart Phone?", "All apps close. Unsaved changes in them are lost.")
    assert [c["args"] for c in gcc_world.calls(LOGIN1, "Reboot")] == [[True]]
    return r


@pytest.mark.parametrize("restart", [{"level": "reboot"}, None, "broken"])
def test_restart_the_phone_after_asking(program, gcc_world, phone, restart):
    # As in Settings, where other parts keep the system bus open
    restart_phone(program, gcc_world, phone, restart, dict(UPDATES, hold_system_bus="1"))


def test_restart_the_phone_reaches_logind_on_its_own(program, gcc_world, phone):
    r = restart_phone(program, gcc_world, phone, {"level": "reboot"}, UPDATES)
    assert not r.warnings()


def test_a_refused_restart_of_the_phone_is_said(program, gcc_world, phone):
    r = restart_phone(program, gcc_world, phone, {"level": "reboot"}, UPDATES,
                      Reboot={"error": "org.freedesktop.DBus.Error.AccessDenied", "message": "not allowed"})
    assert [m["message"] for m in r.warnings()] == [
        "Restarting the phone failed: GDBus.Error:org.freedesktop.DBus.Error.AccessDenied: not allowed"]


def test_search_bar_without_a_header_bar_gets_no_add_button(program, gcc_world, phone):
    software(phone)
    r = gcc_world.run(program, [settle(), select_tab("webapps"), dump("webapps")], params={"search": "inside"})
    assert not r.dump("webapps").has(type="GtkButton", text="Add")
    assert r.dump("webapps").has(type="AdwActionRow", text="No web apps yet")


def test_an_operation_reloads_the_catalog(program, gcc_world, phone):
    """Flathub off: Explore reads the catalog and the package state again."""
    software(phone, flathub=True)
    phone.app(component("org.gnome.Maps", "Maps", "Find places", pkg="gnome-maps", form_factor="mobile",
                        desktop="org.gnome.Maps.desktop"))
    phone.desktop("org.gnome.Maps", "Maps", extra="NoDisplay=true\n")
    phone.package("gnome-maps", installed=True)
    phone.progs.rule("pacman", ["-Sl"], first=True, out="extra gnome-maps 1.0-1 [installed]\n", sleep=1.0)
    phone.helper(["flathub-disable"])
    r = gcc_world.run(program, [
        select_tab("settings"), set_active(False, type="AdwSwitchRow", text="Flathub"),
        wait_for(type="GtkLabel", text="Flathub is off", timeout=10000), click(type="GtkButton", text="Done"),
        select_tab("explore"), wait(3000), dump("explore")])
    assert phone.progs.calls("pacman").count(["-Sl"]) == 2
    # Everything installed: no app is offered
    assert category_rows(r.dump("explore")) == {}


def test_icons_of_the_catalog_replace_the_placeholder(program, gcc_world, phone):
    software(phone)
    for size in (64, 128):
        phone.icon("shared.png", size)
    phone.app(*(component(f"org.icon.App{i}", f"App {i}", "Shares an icon", pkg=f"app{i}", form_factor="mobile",
                          icon="shared.png", stock="no-such-themed-icon", categories=["Game"])
                for i in range(3)))
    for i in range(3):
        phone.package(f"app{i}")
    r = gcc_world.run(program, [CATALOG_READY, click(type="AdwActionRow", text="Games"), wait(1000), dump("games"),
                                open_first("Games"), wait(500), dump("details")])
    page = r.dump("games").within(type="AdwNavigationPage", text="Games")
    assert len(app_rows(r.dump("games"), "Games")) == 3
    # The catalog's picture, not the placeholder nor the missing themed icon
    assert not page.has(type="GtkImage", icon="application-x-executable")
    assert not r.dump("details").within(type="AdwNavigationPage", text="App 0").has(
        type="GtkImage", icon="application-x-executable")


def test_package_info_with_wrapped_fields(program, gcc_world, phone):
    software(phone)
    f = phone.desktop("org.big.App", "Big")
    phone.owns(f, "big")
    phone.package("big", "1-1", installed=True)
    for addon in ("big-lang-de", "big-lang-fr", "big-extra"):
        phone.package(addon, installed=True)
    # After the packages: phone.package() writes pacman's rules anew
    phone.progs.rule("pacman", ["-Qi", "big"], first=True,
                     out="Required By     : big-docs\nName            : big\nVersion         : 1-1\n"
                         "a line without a colon\n"
                         "Required By     : big-lang-de\n                  big-lang-fr  big-extra\n"
                         "Provides        : None\n")
    phone.helper(prefix=["remove"])
    gcc_world.run(program, [settle(), call("open_app", "org.big.App.desktop"),
                                wait_for(type="GtkButton", text="Remove"), click(type="GtkButton", text="Remove"),
                                wait_for(type="AdwAlertDialog"), answer("Remove"),
                                wait_for(type="GtkLabel", text="Big has been removed", timeout=10000)])
    # The wrapped line counts, the one before the package's name does not
    assert phone.helper_calls() == [["remove", "big", "big-lang-de", "big-lang-fr", "big-extra"]]


def test_without_flatpak_both_stores_are_off(program, gcc_world, phone):
    software(phone)
    (gcc_world.fake_bin.path / "flatpak").unlink()
    r = gcc_world.run(program, [settle(), select_tab("settings"), dump("settings")])
    assert sources(r.dump("settings")) == [
        ("Flathub", "Flatpak apps, independent of system updates", False),
        ("FuriHub", "Phone apps from the FuriLabs community, needs Flathub", False)]


def test_a_setting_that_cannot_be_saved_says_so(program, gcc_world, phone):
    ratings_catalog(phone)
    (gcc_world.home / ".config/plinius-software").write_text("a file, not a folder")
    r = gcc_world.run(program, [CATALOG_READY, select_tab("settings"),
                                set_active(True, type="AdwSwitchRow", text="Apps for large screens"),
                                select_tab("explore"), dump("explore")])
    # Shown for now, but not kept
    assert category_rows(r.dump("explore"))["All Apps"] == "5 apps"
    assert [m["message"] for m in r.warnings()][0].startswith("Could not save ")


def test_app_page_of_a_panel_without_tabs_is_left_alone(program, gcc_world, phone):
    f = phone.desktop("org.gnome.Maps", "Maps")
    phone.owns(f, "gnome-maps")
    r = gcc_world.run(program, [settle(), call("open_app", "org.gnome.Maps.desktop"), settle(300), dump("page")],
                      params=UPDATES)
    page = r.dump("page").within(type="AdwNavigationPage", text="App Settings")
    assert not page.has(type="GtkButton", text="Remove")
    assert not [c for c in phone.progs.calls("pacman") if c[:1] == ["-Qqo"]]
