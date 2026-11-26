# The "Web Apps" tab of Settings > Apps (cc-plinius-webapps.c): the list of the web apps in
# ~/.local/share/plinius-webapps of the test's home, the page that adds or edits one (address,
# name, icon from the website or a file, browser, Identify As, Frameless, deleting cache and
# cookies), Add's search in the catalog of plinius-webapps with Manually beside it, what saving
# writes (webapp.ini, the icon, the Firefox profile's user.js and userChrome.css, Chromium's
# launch.sh, the desktop file), Reset Website Data and Delete. The catalog's helper is a stand-in
# (fake_programs.py) answering as plinius-webapps-catalog does.
#
# The browsers are stand-ins in fake_bin (firefox with an application.ini next to it, chromium
# answering --version). The website is a web server on 127.0.0.1 (fake_http.py); requests of the
# panel to any other host are moved to that server (a header forced in with -include), so nothing
# reaches the network. Which addresses the panel takes as public: the test's servers on
# 127.0.0.2 / 127.0.0.3 can be "seen" as other addresses (PLINIUS_TEST_SEEN), the connection
# itself stays on loopback.

import json
import os
import shutil
import time
from pathlib import Path

import pytest

from fake_file_chooser import FileChooserPortal
from fake_http import LocalWeb, png, png_header_only
from fake_programs import Programs
from gcc_harness import (back, build_panel, call, click, dump, enter, gcc_session, gcc_world,  # noqa: F401
                         push, select, set_active, set_text, settle, wait, wait_for)

HEADER = r'''
#pragma once
#include <libsoup/soup.h>
SoupMessage *gcc_soup_message_new_from_uri (const char *method, GUri *uri);
GInetAddress *gcc_seen_address (GInetAddress *address);
'''

GLUE = r'''
#include <libsoup/soup.h>
#include <sys/stat.h>
#include <unistd.h>
#include "gcc_driver.h"
#include "cc-plinius-webapps.h"

GType gcc_test_panel_get_type (void);

static GtkWidget *tab, *add_button;
static AdwToolbarView *toolbar;

/* Only the test's servers: loopback addresses as they are, any other host moved to the server of
 * PLINIUS_TEST_HTTP_PORT on 127.0.0.1 at /HOST/PATH */
SoupMessage *
gcc_soup_message_new_from_uri (const char *method, GUri *uri)
{
    const char *host = g_uri_get_host (uri);
    const char *port = g_getenv ("PLINIUS_TEST_HTTP_PORT");
    g_autofree char *local = NULL;

    if (host && g_hostname_is_ip_address (host) && g_str_has_prefix (host, "127."))
        return soup_message_new_from_uri (method, uri);
    if (!port)
        g_error ("a request without the test's web server: %s", g_uri_to_string (uri));
    local = g_strdup_printf ("http://127.0.0.1:%s/%s%s%s%s", port, host ? host : "", g_uri_get_path (uri),
                             g_uri_get_query (uri) ? "?" : "", g_uri_get_query (uri) ? g_uri_get_query (uri) : "");
    return soup_message_new (method, local);
}

/* The address the panel sees for a connection: PLINIUS_TEST_SEEN="127.0.0.2=8.8.8.8;..." */
GInetAddress *
gcc_seen_address (GInetAddress *address)
{
    static GHashTable *seen;
    g_autofree char *text = g_inet_address_to_string (address);
    const char *map = g_getenv ("PLINIUS_TEST_SEEN");
    g_auto(GStrv) pairs = g_strsplit (map ? map : "", ";", -1);

    if (!seen)
        seen = g_hash_table_new_full (g_str_hash, g_str_equal, g_free, g_object_unref);
    for (char **p = pairs; *p; p++) {
        g_auto(GStrv) kv = g_strsplit (*p, "=", 2);

        if (kv[0] && kv[1] && g_str_equal (kv[0], text)) {
            if (!g_hash_table_contains (seen, text))
                g_hash_table_insert (seen, g_strdup (text), g_inet_address_new_from_string (kv[1]));
            return g_hash_table_lookup (seen, text);
        }
    }
    return address;
}

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

/* As the Apps panel has it: the tab as the content, Add in the header bar */
void
gcc_test_setup (GccTest *t)
{
    GtkWidget *header;

    toolbar = ADW_TOOLBAR_VIEW (adw_navigation_page_get_child (ADW_NAVIGATION_PAGE (t->panel)));
    header = find_descendant (GTK_WIDGET (toolbar), ADW_TYPE_HEADER_BAR);
    tab = cc_plinius_webapps_new_tab (t->panel);
    adw_toolbar_view_set_content (toolbar, tab);
    add_button = cc_plinius_webapps_new_add_button (tab);
    adw_header_bar_pack_end (ADW_HEADER_BAR (header), add_button);
    t->page = NULL;
}

gboolean
gcc_test_call (GccTest *t, const char *name, JsonArray *args, JsonBuilder *result)
{
    if (g_str_equal (name, "drop_tab")) {
        /* The Apps panel goes: the tab is destroyed, work still running must not touch it */
        adw_toolbar_view_set_content (toolbar, NULL);
        tab = NULL;
        json_builder_add_null_value (result);
        return TRUE;
    }
    if (g_str_equal (name, "symlink")) {
        const char *target = json_array_get_string_element (args, 0);
        const char *path = json_array_get_string_element (args, 1);

        unlink (path);
        json_builder_add_boolean_value (result, symlink (target, path) == 0);
        return TRUE;
    }
    if (g_str_equal (name, "chmod")) {
        json_builder_add_boolean_value (result, chmod (json_array_get_string_element (args, 0),
                                                       json_array_get_int_element (args, 1)) == 0);
        return TRUE;
    }
    if (g_str_equal (name, "chmod_app_dirs")) {
        /* Every folder in the data folder (the web apps' own) */
        const char *root = json_array_get_string_element (args, 0);
        g_autoptr(GDir) dir = g_dir_open (root, 0, NULL);
        const char *entry;

        while (dir && (entry = g_dir_read_name (dir))) {
            g_autofree char *path = g_build_filename (root, entry, NULL);

            chmod (path, json_array_get_int_element (args, 1));
        }
        json_builder_add_null_value (result);
        return TRUE;
    }
    if (g_str_equal (name, "add_without_tab")) {
        /* Add for a widget that is no tab of ours: nothing opens */
        g_autoptr(GtkWidget) other = g_object_ref_sink (gtk_label_new ("other"));
        GtkWidget *button = g_object_ref_sink (cc_plinius_webapps_new_add_button (other));

        g_signal_emit_by_name (button, "clicked");
        json_builder_add_string_value (result, adw_navigation_page_get_tag (adw_navigation_view_get_visible_page (t->nav)));
        g_object_unref (button);
        return TRUE;
    }
    return FALSE;
}
'''


@pytest.fixture(scope="module")
def program(gcc_session, tmp_path_factory):
    out = tmp_path_factory.mktemp("webapps")
    header = out / "gcc_webapps.h"
    header.write_text(HEADER)
    reps = [('"/usr/lib/firefox', '"{root}/usr/lib/firefox'),
            ('"/usr/lib/plinius-webapps/', '"{root}/usr/lib/plinius-webapps/'),
            ("soup_message_new_from_uri (SOUP_METHOD_GET, uri)",
             "gcc_soup_message_new_from_uri (SOUP_METHOD_GET, uri)"),
            ("address_is_public (g_inet_socket_address_get_address (G_INET_SOCKET_ADDRESS (remote)))",
             "address_is_public (gcc_seen_address (g_inet_socket_address_get_address (G_INET_SOCKET_ADDRESS (remote))))")]
    return build_panel(gcc_session, out, "webapps", ["cc-plinius-webapps.c"], GLUE,
                       replacements={"cc-plinius-webapps.c": reps},
                       pkgs=("libsoup-3.0", "gdk-pixbuf-2.0"), flags=["-include", str(header)])


# ----- The phone the tab meets -----

def image(fmt, width, height, rgba=(255, 0, 0, 255)):
    """An image of one colour in a format of GdkPixbuf's (png, jpeg, ico, bmp)."""
    import gi
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf
    # JPEG and BMP have no alpha
    alpha = fmt not in ("jpeg", "bmp")
    pb = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, alpha, 8, width, height)
    r, g, b, a = rgba
    pb.fill((r << 24) | (g << 16) | (b << 8) | a)
    ok, data = pb.save_to_bufferv(fmt, [], [])
    assert ok
    return bytes(data)


def svg(width=64, height=64, colour="#00ff00"):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">'
            f'<rect width="{width}" height="{height}" fill="{colour}"/></svg>')


def pixbuf_facts(path):
    """(width, height, has_alpha, rgba of the centre, rgba of the top left corner)"""
    import gi
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf
    pb = GdkPixbuf.Pixbuf.new_from_file(str(path))
    n = pb.get_n_channels()
    pixels = pb.get_pixels()
    stride = pb.get_rowstride()

    def at(x, y):
        o = y * stride + x * n
        return tuple(pixels[o:o + n])
    return pb.get_width(), pb.get_height(), pb.get_has_alpha(), at(pb.get_width() // 2, pb.get_height() // 2), at(0, 0)


CATALOG_HELPER = "/usr/lib/plinius-webapps/plinius-webapps-catalog"


class Phone:
    """The test's home, browsers and web servers."""

    def __init__(self, program, world):
        self.program = program
        self.root = program.root
        for d in list(self.root.iterdir()):
            shutil.rmtree(d) if d.is_dir() else d.unlink()
        self.world = world
        self.data = world.home / ".local/share/plinius-webapps"
        self.apps_dir = world.home / ".local/share/applications"
        self.progs = Programs(world.tmp / "programs")
        # gdk-pixbuf decodes through glycin, which finds its loaders and the types of files
        # (shared-mime-info) in XDG_DATA_DIRS: the host's, read only
        for name in ("glycin-loaders", "mime"):
            if (Path("/usr/share") / name).is_dir():
                (world.data_dir / name).symlink_to(Path("/usr/share") / name)
        # and runs each loader in a bubblewrap sandbox
        if shutil.which("bwrap"):
            (world.fake_bin.path / "bwrap").symlink_to(shutil.which("bwrap"))
        self.webs = []
        self.web = self.server()
        world.extra_env.update({"PLINIUS_TEST_HTTP_PORT": str(self.web.port),
                                "http_proxy": "", "https_proxy": "", "HTTP_PROXY": "", "HTTPS_PROXY": "",
                                "no_proxy": "*", "all_proxy": "", "GIO_USE_PROXY_RESOLVER": "dummy"})

    def server(self, host="127.0.0.1"):
        web = LocalWeb(host)
        self.webs.append(web)
        return web

    def seen(self, **mapping):
        """Servers seen at other addresses: seen(**{"127.0.0.2": "8.8.8.8"})"""
        self.world.extra_env["PLINIUS_TEST_SEEN"] = ";".join(f"{k}={v}" for k, v in mapping.items())

    def path(self, p):
        return self.root / p.lstrip("/")

    # The browsers
    def firefox(self, version="159.0.1", ini=True, where="/opt/firefox"):
        """firefox in PATH, a link to the binary in where with application.ini next to it."""
        binary = self.path(f"{where}/firefox")
        self.progs.program(binary, "firefox")
        if ini:
            (binary.parent / "application.ini").write_text(
                "; This file is not meant to be edited\n[App]\nVendor=Mozilla\nName=Firefox\n"
                f"Version={version}\n")
        link = self.world.fake_bin.path / "firefox"
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(binary)
        return binary

    def chromium(self, out="Chromium 156.0.7444.59 Arch Linux\n", name="chromium"):
        self.progs.program(self.world.fake_bin.path / name, name)
        self.progs.rule(name, ["--version"], out=out)

    # Web apps written before the test
    def webapp(self, app_id, name="Site", url="https://site.example/", icon=None, active=True, browser=None,
               **more):
        d = self.data / app_id
        d.mkdir(parents=True, exist_ok=True)
        lines = ["[Web App]"]
        if browser:
            lines.append(f"Browser={browser}")
        if name is not None:
            lines.append(f"Name={name}")
        if url is not None:
            lines.append(f"URL={url}")
        if icon:
            lines.append(f"Icon={icon}")
            if "/" not in icon and not icon.startswith("."):
                (d / icon).write_bytes(png(48, 48, (0, 0, 255, 255)))
        lines.append(f"Active={'true' if active else 'false'}")
        for k, v in more.items():
            lines.append(f"{k}={v}")
        (d / "webapp.ini").write_text("\n".join(lines) + "\n")
        return d

    # The catalog's helper (plinius-webapps-catalog)
    def catalog(self, offline=None, online=None, **rule):
        """The helper answering --offline with offline and the run with the server with online
        (each a JSON text or a dict of a rule's answer)."""
        self.progs.program(self.path(CATALOG_HELPER), "catalog")
        for args, answer in ((["--offline"], offline), ([], online)):
            if answer is not None:
                self.progs.rule("catalog", args, **(answer if isinstance(answer, dict) else {"out": answer}), **rule)

    def catalog_calls(self):
        return self.progs.calls("catalog")

    def config(self, app_id):
        return keyfile(self.data / app_id / "webapp.ini", "Web App")

    def ids(self):
        return sorted(p.name for p in self.data.iterdir()) if self.data.exists() else []

    def desktop(self, app_id):
        f = self.apps_dir / f"plinius-webapp-{app_id}.desktop"
        return keyfile(f, "Desktop Entry") if f.exists() else None

    def running(self, app_id, pid=None, profile="profile", chromium=False):
        """A lock link as the browser makes it (pid: a live process by default)."""
        d = self.data / app_id / profile
        d.mkdir(parents=True, exist_ok=True)
        pid = os.getpid() if pid is None else pid
        lock = d / ("SingletonLock" if chromium else "lock")
        if lock.is_symlink() or lock.exists():
            lock.unlink()
        lock.symlink_to(f"phone-{pid}" if chromium else f"127.0.0.1:+{pid}")

    def close(self):
        for web in self.webs:
            web.close()


@pytest.fixture
def phone(program, gcc_world):
    p = Phone(program, gcc_world)
    yield p
    p.close()


def keyfile(path, group):
    """The group of a key file as GLib reads it (values unescaped)."""
    from gi.repository import GLib
    kf = GLib.KeyFile()
    kf.load_from_file(str(path), GLib.KeyFileFlags.NONE)
    return {k: kf.get_string(group, k) for k in kf.get_keys(group)[0]}


def group_description(screen):
    groups = [g for g in screen.find(type="AdwPreferencesGroup") if g.get("subtitle")]
    return groups[0]["subtitle"]


def app_rows(screen):
    """[(name, host, switch on)] of the list."""
    out = []
    for r in screen.find(type="AdwActionRow"):
        if r.get("text") == "No web apps yet":
            continue
        switches = [n for n in Node_walk(r) if n.get("type") == "GtkSwitch"]
        if switches:
            out.append((r["text"], r.get("subtitle"), switches[0]["active"]))
    return out


def Node_walk(n):
    yield n
    for c in n.get("children", ()):
        yield from Node_walk(c)


EDITOR = {"type": "AdwNavigationPage", "tag": "plinius-webapp"}


def editor_open(timeout=5000):
    return wait_for(timeout=timeout, **EDITOR)


def status_text(screen):
    """The line below the icon (dim-label), None when hidden."""
    page = screen.within(**EDITOR)
    labels = [n["text"] for n in page.all() if n.get("type") == "GtkLabel" and "dim-label" in n.get("classes", [])]
    return labels[0] if labels else None


def save_button(screen, text="Add"):
    return screen.within(**EDITOR).one(type="GtkButton", text=text)


# ----- The list -----

def test_no_browser_no_add(program, gcc_world, phone):
    r = gcc_world.run(program, [dump("tab")])
    s = r.dump("tab")
    assert group_description(s) == "Web apps need Firefox or Chromium."
    empty = s.one(type="AdwActionRow", text="No web apps yet")
    assert "dim-label" in empty.get("classes", [])
    assert s.one(type="GtkButton", text="Add").get("sensitive") is False
    assert not r.warnings()


def test_firefox_lists_the_web_apps_by_name(program, gcc_world, phone):
    phone.firefox()
    phone.webapp("00000000000000b1", name="Zebra", url="https://zebra.example/path?x=1")
    phone.webapp("00000000000000a1", name="Ärzte", url="http://doctors.example:8080/", active=False)
    phone.webapp("00000000000000c1", name="alpha <b>", url="https://alpha.example/", icon="icon-1.png")
    # Not ours: no valid id (upper case, too short, too long, not hex), or no name/address
    phone.webapp("00000000000000D1", name="Upper")
    phone.webapp("0000001", name="Short")
    phone.webapp("0" * 33, name="Long")
    phone.webapp("000000000000000g", name="Not hex")
    phone.webapp("00000000000000e1", name="")
    phone.webapp("00000000000000f1", url=None)
    (phone.data / "00000000000000aa").mkdir(parents=True)  # no webapp.ini
    r = gcc_world.run(program, [dump("tab")])
    s = r.dump("tab")
    assert group_description(s) == ("Websites as apps of their own, each with its own browser profile. "
                                    "Switched on, a web app has an icon among the apps.")
    # By g_utf8_collate (): code points in the C locale; the name is no markup
    assert app_rows(s) == [("Zebra", "zebra.example", True), ("alpha <b>", "alpha.example", True),
                           ("Ärzte", "doctors.example", False)]
    assert not s.has(type="AdwActionRow", text="No web apps yet")
    assert s.one(type="GtkButton", text="Add").get("sensitive") is not False
    assert not r.warnings()


# ----- Steps of the editor -----

LOOKING = "Looking for the website's icon…"
NO_ICON = "No icon found on the website"
ALERT = {"type": "AdwAlertDialog"}
ADDRESS = {"type": "AdwEntryRow", "text": "Address"}
NAME = {"type": "AdwEntryRow", "text": "Name"}


def open_add():
    return [click(type="GtkButton", text="Add"), editor_open()]


def look_up(url):
    """The address typed, Enter: the search runs at once, until it ends."""
    return [set_text(url, **ADDRESS), enter(**ADDRESS), wait_for(gone=True, type="GtkLabel", text=LOOKING,
                                                                  timeout=20000)]


def save(text="Add"):
    return [click(type="GtkButton", text=text, within=EDITOR), wait_for(gone=True, **EDITOR)]


def entry_value(screen, title):
    return screen.within(**EDITOR).one(type="AdwEntryRow", text=title)["value"]


def only_id(phone):
    ids = phone.ids()
    assert len(ids) == 1, ids
    return ids[0]


FIREFOX_PREFS_START = ("// Written by Settings (Apps > Web Apps) from the web app's options.\n"
                       "// Changes here are overwritten; set them in Settings instead.\n\n")


def prefs(path):
    """user_pref lines of a user.js as {key: value text}."""
    out = {}
    for line in Path(path).read_text().splitlines():
        if line.startswith("user_pref("):
            key, value = line[len("user_pref("):-2].split(", ", 1)
            out[json.loads(key)] = value
    return out


def page(title="Site", icons="", head=""):
    return f"<html><head><title>{title}</title>{head}{icons}</head><body>hello</body></html>"


RED, BLUE, GREEN = (255, 0, 0, 255), (0, 0, 255, 255), (0, 255, 0, 255)


# ----- Adding a web app -----

def test_add_finds_icon_and_name_and_writes_everything(program, gcc_world, phone):
    phone.firefox()
    web = phone.web
    web.route("/site/", page("My &amp; Site", '<link rel="icon" href="icon.png" sizes="64x64">'),
              content_type="text/html")
    web.route("/site/icon.png", png(64, 64, RED), content_type="image/png")
    url = web.url("/site/")
    r = gcc_world.run(program, [*open_add(), dump("empty"), *look_up(url), dump("found"), *save(), dump("list")])
    empty = r.dump("empty").within(**EDITOR)
    editor = r.dump("empty").one(**EDITOR)
    assert editor["text"] == "Add Web App"
    # Nothing to add without an address; a new one has no Reset or Delete
    assert save_button(r.dump("empty")).get("sensitive") is False
    assert not empty.has(type="GtkButton", text="Delete")
    assert not empty.has(type="GtkButton", text="Reset Website Data")
    # Firefox alone: no choice of browser; Browser Default, Frameless on, nothing deleted
    assert not empty.has(type="AdwComboRow", text="Browser")
    identity = empty.one(type="AdwComboRow", text="Identify As")
    assert identity["items"] == ["Browser Default", "Mobile (Android)", "Mobile (iPhone)", "Desktop (Windows)",
                                 "Desktop (macOS)", "Desktop (Linux)"]
    assert identity["selected"] == 0
    assert identity["subtitle"] == "How the browser presents itself to websites"
    assert [(n["text"], n["active"]) for n in empty.find(type="AdwSwitchRow")] == [
        ("Frameless", True), ("Delete Cache", False), ("Delete Cookies", False)]
    assert empty.has(type="AdwPreferencesGroup", subtitle="Each web app has its own browser profile: logins and "
                     "data stay apart from the browser and from other web apps.")
    found = r.dump("found")
    assert entry_value(found, "Name") == "My & Site"
    assert status_text(found) is None
    assert save_button(found).get("sensitive") is not False
    # The guess /apple-touch-icon.png (120 px) first, then the page's 64 px icon
    assert web.requests == ["/site/", "/apple-touch-icon.png", "/site/icon.png"]

    app_id = only_id(phone)
    assert len(app_id) == 16 and all(c in "0123456789abcdef" for c in app_id)
    conf = phone.config(app_id)
    icon = conf.pop("Icon")
    assert conf == {"Browser": "firefox", "Name": "My & Site", "URL": url, "Frameless": "true",
                    "ClearCache": "false", "ClearCookies": "false", "Active": "true"}
    assert icon.startswith("icon-") and icon.endswith(".png")
    assert pixbuf_facts(phone.data / app_id / icon)[:2] == (64, 64)
    profile = phone.data / app_id / "profile"
    user_js = (profile / "user.js").read_text()
    assert user_js.startswith(FIREFOX_PREFS_START)
    p = prefs(profile / "user.js")
    assert p["browser.startup.homepage_override.mstone"] == '"ignore"'
    assert p["toolkit.legacyUserProfileCustomizations.stylesheets"] == "true"
    assert p["browser.link.open_newwindow"] == "2"
    assert p["privacy.sanitize.sanitizeOnShutdown"] == "false"
    assert p["privacy.clearOnShutdown_v2.cache"] == "false"
    assert "general.useragent.override" not in p
    assert "#nav-bar" in (profile / "chrome/userChrome.css").read_text()
    entry = phone.desktop(app_id)
    assert entry == {"Type": "Application", "Name": "My & Site", "Comment": "127.0.0.1",
                     "Exec": f'"firefox" "--name" "plinius-webapp-{app_id}" "--profile" "{profile}" "{url}"',
                     "Icon": str(phone.data / app_id / icon), "Terminal": "false", "StartupNotify": "true",
                     "StartupWMClass": f"plinius-webapp-{app_id}", "Categories": "Network;",
                     "X-Purism-FormFactor": "Workstation;Mobile;", "X-Plinius-WebApp": app_id}
    assert app_rows(r.dump("list")) == [("My & Site", "127.0.0.1", True)]
    assert not r.warnings()


@pytest.mark.parametrize("typed, ok", [
    ("", False),
    ("exampl", False),               # no dot yet
    ("example.org", True),
    ("  example.org  ", True),
    ("exa mple.org", False),
    ("ftp://example.org", False),
    ("https://", False),
    ("localhost", True),
    ("http://[::1]:8080/x", True),
    ("http://%zz", False),
])
def test_address_decides_whether_add_is_possible(program, gcc_world, phone, typed, ok):
    phone.firefox()
    r = gcc_world.run(program, [*open_add(), set_text(typed, **ADDRESS), dump("typed")])
    assert (save_button(r.dump("typed")).get("sensitive") is not False) is ok


def test_saved_address_is_normalized_and_name_defaults_to_host(program, gcc_world, phone):
    phone.firefox()
    r = gcc_world.run(program, [*open_add(), set_text("  example.org/news  ", **ADDRESS),
                                set_text("   ", **NAME), *save()])
    conf = phone.config(only_id(phone))
    assert conf["URL"] == "https://example.org/news" and conf["Name"] == "example.org"
    # The site's server stands in for example.org: nothing there, no icon after saving either
    assert "/example.org/news" in phone.web.requests
    assert "Icon" not in conf
    assert phone.desktop(only_id(phone))["Icon"] == "web-browser"
    assert not r.warnings()


def test_typing_looks_for_the_icon_after_a_pause(program, gcc_world, phone):
    phone.firefox()
    phone.web.route("/p/", page("Paused"))
    url = phone.web.url("/p/")
    r = gcc_world.run(program, [*open_add(), set_text(url[:-1], **ADDRESS), set_text(url, **ADDRESS),
                                wait(500), call("symlink", "x", str(gcc_world.tmp / "mark")),
                                wait_for(type="GtkLabel", text=NO_ICON, timeout=10000), dump("after")])
    # One search, for the address typed last, a second after typing stopped
    assert phone.web.requests[0] == "/p/"
    assert phone.web.requests.count("/p/") == 1 and "/p" not in phone.web.requests
    assert entry_value(r.dump("after"), "Name") == "Paused"
    assert status_text(r.dump("after")) == NO_ICON


def test_enter_without_an_address_searches_nothing(program, gcc_world, phone):
    phone.firefox()
    r = gcc_world.run(program, [*open_add(), set_text("nothing", **ADDRESS), enter(**ADDRESS), wait(200),
                                dump("after")])
    assert status_text(r.dump("after")) is None
    assert phone.web.requests == []


def test_name_follows_the_website_until_one_is_typed(program, gcc_world, phone):
    phone.firefox()
    phone.web.route("/one/", page("Site One"))
    phone.web.route("/two/", page("Site Two"))
    phone.web.route("/three/", page("Site Three"))
    r = gcc_world.run(program, [*open_add(), *look_up(phone.web.url("/one/")), dump("one"),
                                *look_up(phone.web.url("/two/")), dump("two"), set_text("Mine", **NAME),
                                *look_up(phone.web.url("/three/")), dump("three")])
    assert entry_value(r.dump("one"), "Name") == "Site One"
    assert entry_value(r.dump("two"), "Name") == "Site Two"
    assert entry_value(r.dump("three"), "Name") == "Mine"


@pytest.mark.parametrize("head, name", [
    # application-name first, then og:site_name, then the title
    ('<meta property="og:site_name" content="OG Name"><meta name="application-name" content="App&#x20;Name">',
     "App Name"),
    ('<meta name="apple-mobile-web-app-title" content="Apple Title"><meta property="og:site_name" content="OG">',
     "Apple Title"),
    ('<meta property="og:site_name" content="OG Name"><meta name="other" content="x"><meta content="y">', "OG Name"),
    ("", "Hello & <Co> \"Q\" 'A' x AB &bogus; &#xD800; &longentityname;"),
])
def test_name_of_the_page(program, gcc_world, phone, head, name):
    phone.firefox()
    # Entities decoded (not unknown ones, invalid characters or more than 10 long), blanks collapsed
    title = ("  Hello\n\t &amp; &lt;Co&gt; &quot;Q&quot; &apos;A&apos; &nbsp;x &#65;&#x42; &bogus; &#xD800; "
             "&longentityname; ")
    phone.web.route("/", page(title, head=head))
    r = gcc_world.run(program, [*open_add(), *look_up(phone.web.url("/")), dump("found")])
    assert entry_value(r.dump("found"), "Name") == name


@pytest.mark.parametrize("title, name", [
    ("a" * 80, "a" * 60),
    (" \n\t ", ""),
])
def test_name_is_one_line_of_at_most_60(program, gcc_world, phone, title, name):
    phone.firefox()
    phone.web.route("/", page(title))
    r = gcc_world.run(program, [*open_add(), *look_up(phone.web.url("/")), dump("found")])
    assert entry_value(r.dump("found"), "Name") == name


def test_icons_are_tried_largest_first_at_most_eight(program, gcc_world, phone):
    phone.firefox()
    icons = ('<link rel="icon" href="/a16.png">'
             '<link rel="icon" href="/s.svg?v=1" type="image/svg+xml">'
             '<link rel="apple-touch-icon" href="/touch.png">'
             '<link rel="apple-touch-icon-precomposed" sizes="152x152" href="/pre.png">'
             "<LINK REL='shortcut icon' href=/short.png sizes=any>"
             '<link rel="icon" href="data:image/png;base64,AAAA">'
             '<link rel="icon" href="javascript:void(0)">'
             '<link rel="icon">'
             '<link rel="icon" href="/dup.png" sizes="16x16">'
             '<link rel="icon" href="/dup.png#large" sizes="300x300">'
             '<link rel="manifest" href="/m.json">'
             '<link rel="manifest" href="/second.json">')
    phone.web.route("/", page("Page Name", icons))
    phone.web.route("/m.json", json.dumps({
        "short_name": "", "name": "Manifest Name",
        "icons": [{"src": "/mono.png", "sizes": "1024x1024", "purpose": "monochrome"},
                  {"src": "/mask.png", "sizes": "1024x1024", "purpose": "maskable"},
                  {"src": "/any.png", "sizes": "192x192 384x384", "purpose": "any maskable"},
                  "not an object", {"sizes": "1x1"},
                  {"src": "m.svg", "type": "image/svg+xml"}]}))
    r = gcc_world.run(program, [*open_add(), *look_up(phone.web.url("/")), dump("found")])
    assert phone.web.requests == ["/", "/m.json", "/s.svg?v=1", "/short.png", "/mask.png", "/m.svg", "/any.png",
                                  "/dup.png", "/touch.png", "/pre.png"]
    assert entry_value(r.dump("found"), "Name") == "Manifest Name"
    assert status_text(r.dump("found")) == NO_ICON


@pytest.mark.parametrize("manifest", ["not json", "[1, 2]", json.dumps({"short_name": "Short", "icons": 3})])
def test_a_broken_manifest_leaves_the_page(program, gcc_world, phone, manifest):
    phone.firefox()
    phone.web.route("/", page("From Page", '<link rel="manifest" href="/m.json">'))
    phone.web.route("/m.json", manifest)
    phone.web.route("/favicon.ico", image("ico", 32, 32, BLUE))
    r = gcc_world.run(program, [*open_add(), *look_up(phone.web.url("/")), dump("found")])
    assert entry_value(r.dump("found"), "Name") == ("Short" if "Short" in manifest else "From Page")
    assert phone.web.requests == ["/", "/m.json", "/apple-touch-icon.png", "/favicon.ico"]
    assert status_text(r.dump("found")) is None


def test_manifest_on_another_scheme_is_not_loaded(program, gcc_world, phone):
    phone.firefox()
    phone.web.route("/", page("P", '<link rel="manifest" href="ftp://127.0.0.1/m.json">'))
    gcc_world.run(program, [*open_add(), *look_up(phone.web.url("/"))])
    assert phone.web.requests == ["/", "/apple-touch-icon.png", "/favicon.ico"]


def test_only_real_icons_of_a_sane_size_are_taken(program, gcc_world, phone):
    phone.firefox()
    icons = ('<link rel="icon" href="/text.png" sizes="900x900">'
             '<link rel="icon" href="/huge.png" sizes="800x800">'
             '<link rel="icon" href="/tiny.png" sizes="700x700">'
             '<link rel="icon" href="/bmp.png" sizes="600x600">'
             '<link rel="icon" href="/fourmb.png" sizes="500x500">'
             '<link rel="icon" href="/wide.png" sizes="400x400">'
             '<link rel="icon" href="/never.png" sizes="300x300">')
    web = phone.web
    web.route("/", page("Sizes", icons))
    web.route("/text.png", "<html>not an image</html>", content_type="image/png")
    # Says it is 5000 px wide: not decoded
    web.route("/huge.png", png_header_only(5000, 5000) + b"\0" * 100)
    web.route("/tiny.png", png(8, 8, GREEN))
    # Other loaders than PNG, JPEG, ICO and SVG are not used for the network
    web.route("/bmp.png", image("bmp", 64, 64, GREEN))
    # A full buffer: larger than any icon may be
    web.route("/fourmb.png", png(64, 64, GREEN) + b"\0" * (4 * 1024 * 1024))
    web.route("/wide.png", png(600, 300, RED))
    web.route("/never.png", png(64, 64, GREEN))
    r = gcc_world.run(program, [*open_add(), *look_up(web.url("/")), *save()])
    assert web.requests == ["/", "/text.png", "/huge.png", "/tiny.png", "/bmp.png", "/fourmb.png", "/wide.png"]
    app_id = only_id(phone)
    # Shrunk to 256 and centered on a transparent square
    w, h, alpha, centre, corner = pixbuf_facts(phone.data / app_id / phone.config(app_id)["Icon"])
    assert (w, h, alpha) == (256, 256, True)
    assert centre == RED and corner[3] == 0
    assert not r.warnings()


@pytest.mark.parametrize("body, size", [
    (lambda: image("jpeg", 40, 40, RED), 40),
    (lambda: image("ico", 32, 32, RED), 32),
    (lambda: svg(), 256),
    (lambda: "\n  " + svg(), 256),
    (lambda: "﻿" + svg(), 256),
    (lambda: png(1000, 1000, RED), 256),
    (lambda: png(100, 300, RED), 256),
])
def test_icon_formats(program, gcc_world, phone, body, size):
    phone.firefox()
    phone.web.route("/", page("F", '<link rel="icon" href="/i" sizes="256x256">'))
    phone.web.route("/i", body())
    gcc_world.run(program, [*open_add(), *look_up(phone.web.url("/")), *save()])
    app_id = only_id(phone)
    facts = pixbuf_facts(phone.data / app_id / phone.config(app_id)["Icon"])
    assert facts[:2] == (size, size)
    assert phone.web.requests == ["/", "/i"]


def test_text_with_svg_inside_is_no_icon(program, gcc_world, phone):
    phone.firefox()
    phone.web.route("/", page("F", '<link rel="icon" href="/i" sizes="64x64">'))
    phone.web.route("/i", "hello " + svg())
    r = gcc_world.run(program, [*open_add(), *look_up(phone.web.url("/")), dump("found")])
    assert status_text(r.dump("found")) == NO_ICON



# ----- Public websites may not reach into the local network (L7) -----

PUBLIC = "8.8.8.8"


@pytest.mark.parametrize("seen, allowed", [
    ("9.9.9.9", True),
    ("2001:4860::8888", True),
    ("::ffff:9.9.9.9", True),
    ("10.0.0.1", False),
    ("192.168.1.5", False),
    ("172.16.0.1", False),
    ("127.0.0.3", False),
    ("::1", False),
    ("0.0.0.0", False),
    ("169.254.1.1", False),
    ("fe80::1", False),
    ("224.0.0.1", False),
    ("100.64.0.1", False),
    ("100.127.255.1", False),
    ("100.128.0.1", True),
    ("0.1.2.3", False),
    ("240.0.0.1", False),
    ("192.0.0.8", False),
    ("198.18.0.1", False),
    ("198.19.0.1", False),
    ("198.20.0.1", True),
    ("fc00::1", False),
    ("fd12::1", False),
    ("::ffff:10.0.0.1", False),
])
def test_icon_of_a_public_site_only_from_public_addresses(program, gcc_world, phone, seen, allowed):
    phone.firefox()
    site, other = phone.server("127.0.0.2"), phone.server("127.0.0.3")
    phone.seen(**{"127.0.0.2": PUBLIC, "127.0.0.3": seen})
    site.route("/", page("Public", f'<link rel="icon" href="{other.url("/i.png")}" sizes="512x512">'))
    site.route("/favicon.ico", image("ico", 32, 32, BLUE))
    other.route("/i.png", png(64, 64, RED))
    gcc_world.run(program, [*open_add(), *look_up(site.url("/")), *save()])
    app_id = only_id(phone)
    colour = pixbuf_facts(phone.data / app_id / phone.config(app_id)["Icon"])[3]
    assert colour[:3] == (RED if allowed else BLUE)[:3]
    # Refused once connected, before anything was sent
    assert other.requests == (["/i.png"] if allowed else [])


@pytest.mark.parametrize("seen", ["192.168.1.5", None])
def test_a_local_site_may_load_from_anywhere(program, gcc_world, phone, seen):
    phone.firefox()
    site, other = phone.server("127.0.0.2"), phone.server("127.0.0.3")
    phone.seen(**({"127.0.0.2": seen} if seen else {}), **{"127.0.0.3": PUBLIC})
    site.route("/", page("Router", f'<link rel="icon" href="{other.url("/i.png")}" sizes="512x512">'))
    other.route("/i.png", png(64, 64, RED))
    gcc_world.run(program, [*open_add(), *look_up(site.url("/")), *save()])
    app_id = only_id(phone)
    assert pixbuf_facts(phone.data / app_id / phone.config(app_id)["Icon"])[3][:3] == RED[:3]


def test_a_second_search_remembers_the_server_is_public(program, gcc_world, phone):
    # The page's connection is kept open: the second search connects to the icon's server first
    phone.firefox()
    site, other = phone.server("127.0.0.2"), phone.server("127.0.0.3")
    phone.seen(**{"127.0.0.2": PUBLIC, "127.0.0.3": "10.0.0.1"})
    site.route("/", page("Public", f'<link rel="icon" href="{other.url("/i.png")}" sizes="512x512">'))
    other.route("/i.png", png(64, 64, RED))
    r = gcc_world.run(program, [*open_add(), *look_up(site.url("/")), dump("first"),
                                click(type="GtkButton", text="From Website"),
                                wait_for(gone=True, type="GtkLabel", text=LOOKING, timeout=20000), dump("second")])
    assert status_text(r.dump("first")) == NO_ICON
    assert status_text(r.dump("second")) == NO_ICON
    assert other.requests == []
    assert site.requests.count("/") == 2


def test_redirects_count_from_where_they_end(program, gcc_world, phone):
    phone.firefox()
    site = phone.server("127.0.0.2")
    phone.web.redirect("/start", site.url("/real/"))
    site.route("/real/", page("Moved", '<link rel="icon" href="icon.png" sizes="512x512">'))
    site.route("/real/icon.png", png(64, 64, RED))
    r = gcc_world.run(program, [*open_add(), *look_up(phone.web.url("/start")), dump("found"), *save()])
    assert entry_value(r.dump("found"), "Name") == "Moved"
    assert site.requests == ["/real/", "/real/icon.png"]
    # The address stays as typed
    assert phone.config(only_id(phone))["URL"] == phone.web.url("/start")


def test_a_public_page_redirecting_into_the_local_network_is_refused(program, gcc_world, phone):
    phone.firefox()
    site, lan = phone.server("127.0.0.2"), phone.server("127.0.0.3")
    phone.seen(**{"127.0.0.2": PUBLIC, "127.0.0.3": "192.168.1.1"})
    site.redirect("/", lan.url("/admin/"))
    lan.route("/admin/", page("Router Admin", '<link rel="icon" href="i.png">'))
    lan.route("/favicon.ico", image("ico", 32, 32, RED))
    r = gcc_world.run(program, [*open_add(), *look_up(site.url("/")), dump("found")])
    assert lan.requests == []
    assert status_text(r.dump("found")) == NO_ICON
    assert entry_value(r.dump("found"), "Name") == ""


# ----- While and after the search -----

def test_leaving_the_page_during_the_search_cancels_it(program, gcc_world, phone):
    phone.firefox()
    phone.web.route("/", page("Slow"), delay=1.5)
    r = gcc_world.run(program, [*open_add(), set_text(phone.web.url("/"), **ADDRESS), enter(**ADDRESS),
                                wait_for(type="GtkLabel", text=LOOKING), {"op": "back"}, wait(2500), dump("list")])
    assert not r.dump("list").has(**EDITOR)
    assert phone.ids() == []
    assert not r.warnings()


def test_saved_before_the_icon_was_found_gets_it_later(program, gcc_world, phone):
    phone.firefox()
    phone.web.route("/", page("Later", '<link rel="icon" href="/i.png" sizes="512x512">'))
    phone.web.route("/i.png", png(64, 64, RED), delay=1.0)
    r = gcc_world.run(program, [*open_add(), set_text(phone.web.url("/"), **ADDRESS), set_text("Later", **NAME),
                                *save(), dump("saved"), wait(2500), dump("later")])
    assert app_rows(r.dump("saved")) == [("Later", "127.0.0.1", True)]
    app_id = only_id(phone)
    conf = phone.config(app_id)
    assert pixbuf_facts(phone.data / app_id / conf["Icon"])[3][:3] == RED[:3]
    assert phone.desktop(app_id)["Icon"] == str(phone.data / app_id / conf["Icon"])
    assert app_rows(r.dump("later")) == [("Later", "127.0.0.1", True)]
    assert not r.warnings()


def test_the_late_icon_after_the_tab_is_gone(program, gcc_world, phone):
    phone.firefox()
    phone.web.route("/", page("Later", '<link rel="icon" href="/i.png" sizes="512x512">'))
    phone.web.route("/i.png", png(64, 64, RED), delay=1.0)
    r = gcc_world.run(program, [*open_add(), set_text(phone.web.url("/"), **ADDRESS), *save(), call("drop_tab"),
                                wait(2500)])
    assert "Icon" in phone.config(only_id(phone))
    assert not r.warnings()


def test_the_late_icon_of_a_web_app_deleted_meanwhile(program, gcc_world, phone):
    phone.firefox()
    phone.web.route("/", page("Later", '<link rel="icon" href="/i.png" sizes="512x512">'))
    phone.web.route("/i.png", png(64, 64, RED), delay=1.5)
    r = gcc_world.run(program, [*open_add(), set_text(phone.web.url("/"), **ADDRESS), *save(),
                                click(type="AdwActionRow", text="127.0.0.1"), editor_open(),
                                click(type="GtkButton", text="Delete", within=EDITOR), wait_for(**ALERT),
                                click(type="GtkButton", text="Delete", within=ALERT), wait(2500), dump("list")])
    assert phone.ids() == []
    assert r.dump("list").has(type="AdwActionRow", text="No web apps yet")
    assert not r.warnings()


# ----- An image from a file -----

def test_choose_image_from_a_file(program, gcc_world, phone):
    phone.firefox()
    phone.web.route("/", page("Site", '<link rel="icon" href="/i.png" sizes="512x512">'))
    phone.web.route("/i.png", png(64, 64, RED))
    chosen = gcc_world.home / "logo.bmp"
    chosen.write_bytes(image("bmp", 80, 40, GREEN))
    FileChooserPortal(gcc_world, [{"uris": [chosen.as_uri()]}]).start()
    r = gcc_world.run(program, [*open_add(), click(type="GtkButton", text="Choose Image…"), wait(1500),
                                dump("chosen"), set_text(phone.web.url("/"), **ADDRESS), wait(1500),
                                set_text("Logo", **NAME), *save()])
    assert status_text(r.dump("chosen")) is None
    # Typing the address no longer replaces a chosen image
    assert phone.web.requests == []
    app_id = only_id(phone)
    w, h, alpha, centre, corner = pixbuf_facts(phone.data / app_id / phone.config(app_id)["Icon"])
    assert (w, h) == (80, 80) and centre[:3] == GREEN[:3] and corner[3] == 0
    opened = [c for c in gcc_world.calls("org.freedesktop.portal.Desktop", "OpenFile")]
    assert len(opened) == 1 and opened[0]["args"][1] == "Choose an Icon"
    assert not r.warnings()


def test_from_website_after_a_chosen_image(program, gcc_world, phone):
    phone.firefox()
    phone.web.route("/", page("Site", '<link rel="icon" href="/i.png" sizes="512x512">'))
    phone.web.route("/i.png", png(64, 64, RED))
    chosen = gcc_world.home / "logo.png"
    chosen.write_bytes(png(64, 64, GREEN))
    FileChooserPortal(gcc_world, [{"uris": [chosen.as_uri()]}]).start()
    gcc_world.run(program, [*open_add(), set_text(phone.web.url("/"), **ADDRESS),
                            click(type="GtkButton", text="Choose Image…"), wait(1500),
                            click(type="GtkButton", text="From Website"),
                            wait_for(gone=True, type="GtkLabel", text=LOOKING, timeout=20000), *save()])
    app_id = only_id(phone)
    assert pixbuf_facts(phone.data / app_id / phone.config(app_id)["Icon"])[3][:3] == RED[:3]


@pytest.mark.parametrize("content", [b"not an image at all", b"\x89PNG\r\n\x1a\n" + b"\0" * (4 * 1024 * 1024 + 1)],
                         ids=["text", "too-large"])
def test_a_file_that_is_no_image(program, gcc_world, phone, content):
    phone.firefox()
    chosen = gcc_world.home / "notes.png"
    chosen.write_bytes(content)
    FileChooserPortal(gcc_world, [{"uris": [chosen.as_uri()]}]).start()
    r = gcc_world.run(program, [*open_add(), click(type="GtkButton", text="Choose Image…"),
                                wait_for(type="GtkLabel", text="This file is not an image", timeout=20000),
                                dump("chosen")])
    assert status_text(r.dump("chosen")) == "This file is not an image"


# A dismissed portal request is not tested: GTK 4.22.5's GtkFileDialog itself crashes on it here
# (double free also for a bare gtk_file_dialog_open () without the panel's code)
def test_choosing_from_the_pictures_folder_after_the_page_went(program, gcc_world, phone):
    phone.firefox()
    chosen = gcc_world.home / "logo.png"
    chosen.write_bytes(png(64, 64, GREEN))
    (gcc_world.home / "Pictures").mkdir()
    (gcc_world.home / ".config/user-dirs.dirs").write_text('XDG_PICTURES_DIR="$HOME/Pictures"\n')
    FileChooserPortal(gcc_world, [{"uris": [chosen.as_uri()], "delay_ms": 600}]).start()
    r = gcc_world.run(program, [*open_add(), click(type="GtkButton", text="Choose Image…"), {"op": "back"},
                                wait(1200), dump("list")])
    opened = gcc_world.calls("org.freedesktop.portal.Desktop", "OpenFile")
    # The pictures folder first
    assert bytes(opened[0]["args"][2]["current_folder"]).rstrip(b"\0").decode() == str(gcc_world.home / "Pictures")
    assert not r.dump("list").has(**EDITOR)
    assert phone.ids() == []
    assert not r.warnings()


def test_a_chosen_file_that_cannot_be_read(program, gcc_world, phone):
    phone.firefox()
    FileChooserPortal(gcc_world, [{"uris": [(gcc_world.home / "gone.png").as_uri()]}]).start()
    r = gcc_world.run(program, [*open_add(), click(type="GtkButton", text="Choose Image…"),
                                wait_for(type="GtkLabel", text="This file is not an image", timeout=20000),
                                dump("chosen")])
    assert status_text(r.dump("chosen")) == "This file is not an image"


# ----- The switch in the list -----

def test_switch_on_writes_the_desktop_file(program, gcc_world, phone):
    phone.firefox()
    app = "00000000000000a1"
    # As written in the key file: \\ is one backslash
    phone.webapp(app, name="Shop", url="https://shop.example/a%20b?q=$x&y=`z`\\\\\"", active=False)
    toggle = {"type": "GtkSwitch", "within": {"type": "AdwActionRow", "text": "Shop"}}
    r = gcc_world.run(program, [set_active(True, **toggle)])
    entry = phone.desktop(app)
    profile = phone.data / app / "profile"
    assert entry["Exec"] == (f'"firefox" "--name" "plinius-webapp-{app}" "--profile" "{profile}" '
                             '"https://shop.example/a%%20b?q=\\$x&y=\\`z\\`\\\\\\""')
    assert entry["Comment"] == "shop.example" and entry["Icon"] == "web-browser"
    assert phone.config(app)["Active"] == "true"
    assert not r.warnings()


def test_switch_off_removes_the_desktop_file(program, gcc_world, phone):
    phone.firefox()
    app = "00000000000000a1"
    phone.webapp(app, name="Shop", url="https://shop.example/")
    phone.apps_dir.mkdir(parents=True, exist_ok=True)
    (phone.apps_dir / f"plinius-webapp-{app}.desktop").write_text("[Desktop Entry]\nName=Shop\n")
    toggle = {"type": "GtkSwitch", "within": {"type": "AdwActionRow", "text": "Shop"}}
    r = gcc_world.run(program, [set_active(False, **toggle), dump("off")])
    assert phone.desktop(app) is None
    assert phone.config(app)["Active"] == "false"
    assert r.dump("off").one(type="GtkSwitch")["active"] is False


def test_switch_that_cannot_be_written_goes_back(program, gcc_world, phone):
    phone.firefox()
    app = "00000000000000a1"
    d = phone.webapp(app, name="Shop", url="https://shop.example/", active=False)
    d.chmod(0o500)
    try:
        toggle = {"type": "GtkSwitch", "within": {"type": "AdwActionRow", "text": "Shop"}}
        r = gcc_world.run(program, [set_active(True, **toggle), wait_for(**ALERT), dump("error")])
    finally:
        d.chmod(0o700)
    s = r.dump("error")
    alert = s.one(**ALERT)
    assert alert["text"] == "Could Not Change the Web App" and "Permission denied" in alert["subtitle"]
    assert s.one(type="GtkSwitch")["active"] is False
    assert phone.desktop(app) is None
    assert [m["message"] for m in r.warnings()] == [f"Could not switch web app {app}: {alert['subtitle']}"]


# ----- Editing a web app -----

def edit(name):
    return [click(type="AdwActionRow", text=name), editor_open()]


def test_editing_shows_the_web_app_and_saves_changes(program, gcc_world, phone):
    phone.firefox()
    app = "00000000000000a1"
    phone.webapp(app, name="Shop", url="https://shop.example/", icon="icon-1.png", Frameless="false",
                 ClearCache="true", ClearCookies="false")
    phone.web.route("/shop.example/", page("Should not be asked"))
    r = gcc_world.run(program, [*edit("Shop"), dump("editor"), set_text("https://shop.example/new", **ADDRESS),
                                wait(1500), set_text("Shop 2", **NAME), set_active(True, type="AdwSwitchRow",
                                                                                   text="Delete Cookies"),
                                *save("Save"), dump("list")])
    e = r.dump("editor").within(**EDITOR)
    assert r.dump("editor").one(**EDITOR)["text"] == "Shop"
    assert e.one(type="AdwEntryRow", text="Address")["value"] == "https://shop.example/"
    assert e.one(type="AdwEntryRow", text="Name")["value"] == "Shop"
    assert [(n["text"], n["active"]) for n in e.find(type="AdwSwitchRow")] == [
        ("Frameless", False), ("Delete Cache", True), ("Delete Cookies", False)]
    assert e.has(type="GtkButton", text="Reset Website Data") and e.has(type="GtkButton", text="Delete")
    assert "destructive-action" in e.one(type="GtkButton", text="Delete")["classes"]
    assert e.one(type="GtkButton", text="Save")
    # An icon of its own: a new address does not look for another
    assert phone.web.requests == []
    conf = phone.config(app)
    assert conf["Name"] == "Shop 2" and conf["URL"] == "https://shop.example/new"
    assert conf["Icon"] == "icon-1.png" and conf["Frameless"] == "false"
    assert conf["ClearCache"] == "true" and conf["ClearCookies"] == "true"
    profile = phone.data / app / "profile"
    p = prefs(profile / "user.js")
    assert p["privacy.sanitize.sanitizeOnShutdown"] == "true"
    assert p["privacy.clearOnShutdown.cache"] == "true" and p["privacy.clearOnShutdown_v2.cookiesAndStorage"] == "true"
    assert p["browser.link.open_newwindow"] == "3"
    assert not (profile / "chrome/userChrome.css").exists()
    assert phone.desktop(app)["Name"] == "Shop 2"
    assert app_rows(r.dump("list")) == [("Shop 2", "shop.example", True)]


def test_saving_follows_the_switch_not_the_page(program, gcc_world, phone):
    phone.firefox()
    app = "00000000000000a1"
    d = phone.webapp(app, name="Shop", url="https://shop.example/", icon="icon-1.png")
    text = (d / "webapp.ini").read_text().replace("Active=true", "Active=false")
    gcc_world.run(program, [*edit("Shop"), {"op": "write_file", "path": str(d / "webapp.ini"), "text": text},
                            *save("Save")])
    assert phone.config(app)["Active"] == "false"
    assert phone.desktop(app) is None


def test_a_new_icon_replaces_the_old_file(program, gcc_world, phone):
    phone.firefox()
    app = "00000000000000a1"
    site = phone.web.url("/")
    phone.webapp(app, name="Shop", url=site, icon="icon-1.png")
    phone.web.route("/", page("Shop", '<link rel="icon" href="/i.png" sizes="512x512">'))
    phone.web.route("/i.png", png(64, 64, RED))
    gcc_world.run(program, [*edit("Shop"), click(type="GtkButton", text="From Website"),
                            wait_for(gone=True, type="GtkLabel", text=LOOKING, timeout=20000), *save("Save")])
    icon = phone.config(app)["Icon"]
    assert icon != "icon-1.png" and not (phone.data / app / "icon-1.png").exists()
    assert pixbuf_facts(phone.data / app / icon)[3][:3] == RED[:3]


def test_an_icon_outside_the_folder_is_not_used(program, gcc_world, phone):
    phone.firefox()
    for i, icon in enumerate(("../../escape.png", ".hidden.png", "missing.png")):
        phone.webapp(f"00000000000000a{i}", name=f"App {i}", url="https://a.example/", icon=icon)
    (phone.data / "00000000000000a2/missing.png").unlink()
    r = gcc_world.run(program, [dump("list")])
    assert len(app_rows(r.dump("list"))) == 3
    images = [n for n in r.dump("list").find(type="GtkImage") if n.get("icon") == "web-browser"]
    assert len(images) == 3


def test_saving_fails_and_can_be_tried_again(program, gcc_world, phone):
    phone.firefox()
    phone.data.mkdir(parents=True)
    phone.data.chmod(0o500)
    try:
        r = gcc_world.run(program, [*open_add(), set_text("https://new.example/", **ADDRESS),
                                    click(type="GtkButton", text="Add", within=EDITOR), wait_for(**ALERT),
                                    dump("error"), {"op": "close_dialog"},
                                    call("chmod", str(phone.data), 0o700), *save()])
    finally:
        phone.data.chmod(0o700)
    alert = r.dump("error").one(**ALERT)
    assert alert["text"] == "Could Not Save the Web App" and "Permission denied" in alert["subtitle"]
    assert r.dump("error").has(**EDITOR)
    # The second try with a new id
    assert len(phone.ids()) == 1
    assert len([m for m in r.warnings() if m["message"].startswith("Could not save web app ")]) == 1


def test_add_for_a_widget_that_is_no_tab(program, gcc_world, phone):
    phone.firefox()
    r = gcc_world.run(program, [call("add_without_tab")])
    assert r.value("add_without_tab") == "test-panel"


# ----- Identify As -----

ANDROID_FF = "Mozilla/5.0 (Android 15; Mobile; rv:{0}.0) Gecko/{0}.0 Firefox/{0}.0"
ANDROID_CHROME = ("Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{0}.0.0.0 Mobile "
                  "Safari/537.36")
IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
          "Version/26.0 Mobile/15E148 Safari/604.1")
WINDOWS_FF = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{0}.0) Gecko/20100101 Firefox/{0}.0"
IDENTITY = {"type": "AdwComboRow", "text": "Identify As"}


def site_data(profile_root):
    """What websites stored in each browser's profile, and what stays (cookies, add-ons)."""
    files = {
        "profile/storage/default/https+++site.example/ls/data.sqlite": "gone",
        "profile/storage/default/moz-extension+++1234/idb/filters": "stays",
        "profile/storage/temporary/x": "gone",
        "profile/storage/to-be-removed/y": "gone",
        "profile/webappsstore.sqlite": "gone",
        "profile/webappsstore.sqlite-wal": "gone",
        "profile/serviceworker.txt": "gone",
        "profile/gmp/widevine/license": "gone",
        "profile/cookies.sqlite": "stays",
        "chromium/Default/Local Storage/leveldb/000003.log": "gone",
        "chromium/Default/IndexedDB/x": "gone",
        "chromium/Default/Cookies": "stays",
        "chrome/Default/Service Worker/Database/y": "gone",
        "chrome/Default/File System/z": "gone",
        "chrome/Default/Preferences": "stays",
    }
    for rel in files:
        f = profile_root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(rel)
    return files


def assert_site_data(profile_root, files, cleared=True):
    for rel, fate in files.items():
        assert (profile_root / rel).exists() == (fate == "stays" or not cleared), rel


def test_identify_as_writes_the_user_agent_and_resets_site_data(program, gcc_world, phone):
    phone.firefox("159.0.1")
    app = "00000000000000a1"
    d = phone.webapp(app, name="Shop", url="https://shop.example/", icon="icon-1.png")
    files = site_data(d)
    r = gcc_world.run(program, [*edit("Shop"), select(item="Mobile (Android)", **IDENTITY), *save("Save"), wait(500)])
    conf = phone.config(app)
    assert conf["IdentifyAs"] == "android" and conf["UserAgent"] == ANDROID_FF.format(159)
    p = prefs(d / "profile/user.js")
    assert p["general.useragent.override"] == json.dumps(ANDROID_FF.format(159))
    assert p["general.platform.override"] == '"Linux armv8l"' and p["general.oscpu.override"] == '"Linux armv8l"'
    assert_site_data(d, files)
    assert not r.warnings()


def test_iphone_has_no_oscpu(program, gcc_world, phone):
    phone.firefox()
    gcc_world.run(program, [*open_add(), set_text("https://shop.example/", **ADDRESS),
                            select(item="Mobile (iPhone)", **IDENTITY), *save()])
    app = only_id(phone)
    assert phone.config(app)["UserAgent"] == IPHONE
    p = prefs(phone.data / app / "profile/user.js")
    assert p["general.platform.override"] == '"iPhone"' and "general.oscpu.override" not in p


def test_identify_as_is_refused_while_the_web_app_is_open(program, gcc_world, phone):
    phone.firefox()
    app = "00000000000000a1"
    d = phone.webapp(app, name="Shop", url="https://shop.example/", icon="icon-1.png")
    files = site_data(d)
    phone.running(app)
    before = (d / "webapp.ini").read_text()
    r = gcc_world.run(program, [*edit("Shop"), select(item="Desktop (Windows)", **IDENTITY),
                                click(type="GtkButton", text="Save", within=EDITOR), wait_for(**ALERT),
                                dump("refused")])
    alert = r.dump("refused").one(**ALERT)
    assert (alert["text"], alert["subtitle"]) == ("“Shop” Is Open", "Close the web app first, then change Identify As.")
    assert r.dump("refused").has(**EDITOR)
    assert (d / "webapp.ini").read_text() == before
    assert_site_data(d, files, cleared=False)


def test_back_to_the_browser_default_forgets_prefs_js(program, gcc_world, phone):
    phone.firefox("159.0")
    app = "00000000000000a1"
    d = phone.webapp(app, name="Shop", url="https://shop.example/", icon="icon-1.png", IdentifyAs="android",
                     UserAgent=ANDROID_FF.format(159))
    (d / "profile").mkdir()
    (d / "profile/prefs.js").write_text('user_pref("general.useragent.override", "x");\nuser_pref("browser.x", 1);\n'
                                        'user_pref("general.platform.override", "y");\n'
                                        'user_pref("general.oscpu.override", "z");\n')
    gcc_world.run(program, [*edit("Shop"), dump("editor"), select(index=0, **IDENTITY), *save("Save"), wait(300)])
    conf = phone.config(app)
    assert "IdentifyAs" not in conf and "UserAgent" not in conf
    assert (d / "profile/prefs.js").read_text() == 'user_pref("browser.x", 1);\n'
    assert "general.useragent.override" not in prefs(d / "profile/user.js")


@pytest.mark.parametrize("running", [True, False])
def test_prefs_js_is_left_alone_while_firefox_runs_or_without_lines(program, gcc_world, phone, running):
    phone.firefox()
    app = "00000000000000a1"
    d = phone.webapp(app, name="Shop", url="https://shop.example/", icon="icon-1.png")
    (d / "profile").mkdir()
    text = 'user_pref("general.useragent.override", "x");\n' if running else 'user_pref("browser.x", 1);\n'
    (d / "profile/prefs.js").write_text(text)
    if running:
        phone.running(app)
    gcc_world.run(program, [*edit("Shop"), *save("Save")])
    assert (d / "profile/prefs.js").read_text() == text


def test_the_list_writes_a_web_app_again_for_a_new_browser_version(program, gcc_world, phone):
    phone.firefox("160.2")
    new, running, locked = "00000000000000a1", "00000000000000a2", "00000000000000a3"
    for app in (new, running, locked):
        phone.webapp(app, name=f"App {app[-1]}", url="https://a.example/", IdentifyAs="windows",
                     UserAgent=WINDOWS_FF.format(159))
    phone.running(running)
    (phone.data / locked).chmod(0o500)
    try:
        r = gcc_world.run(program, [dump("list")])
    finally:
        (phone.data / locked).chmod(0o700)
    assert phone.config(new)["UserAgent"] == WINDOWS_FF.format(160)
    p = prefs(phone.data / new / "profile/user.js")
    assert p["general.useragent.override"] == json.dumps(WINDOWS_FF.format(160))
    assert p["general.oscpu.override"] == '"Windows NT 10.0; Win64; x64"'
    assert phone.desktop(new) is not None
    # Its browser runs: next time
    assert phone.config(running)["UserAgent"] == WINDOWS_FF.format(159)
    assert phone.config(locked)["UserAgent"] == WINDOWS_FF.format(159)
    warnings = [m["message"] for m in r.warnings()]
    assert len(warnings) == 1 and warnings[0].startswith(f"Could not update web app {locked}: ")
    assert len(app_rows(r.dump("list"))) == 3


FIREFOX_INI = "; comment\n[App]\nName=Firefox\nVersion={}\n"


@pytest.mark.parametrize("layout, major", [
    ("next to the binary", 159),
    ("usr-lib", 128),
    ("esr", 115),
    ("none", 140),
    ("not a number", 140),
    ("broken ini", 150),
])
def test_firefox_version_for_the_user_agent(program, gcc_world, phone, layout, major):
    if layout == "next to the binary":
        phone.firefox("159.0.1")
    elif layout == "not a number":
        phone.firefox("beta")
    elif layout == "broken ini":
        binary = phone.firefox()
        (binary.parent / "application.ini").write_text("[App\nVersion=1\n")
        phone.path("/usr/lib/firefox").mkdir(parents=True)
        phone.path("/usr/lib/firefox/application.ini").write_text(FIREFOX_INI.format("150.0"))
    else:
        # A firefox in PATH that is no link: application.ini where Arch installs it
        phone.progs.program(gcc_world.fake_bin.path / "firefox", "firefox")
        for where, version in (("usr-lib", "/usr/lib/firefox"), ("esr", "/usr/lib/firefox-esr")):
            if layout == where or (layout == "esr" and where == "esr"):
                phone.path(version).mkdir(parents=True)
                phone.path(version).joinpath("application.ini").write_text(
                    FIREFOX_INI.format("128.5" if where == "usr-lib" else "115.0esr"))
    gcc_world.run(program, [*open_add(), set_text("https://shop.example/", **ADDRESS),
                            select(item="Mobile (Android)", **IDENTITY), *save()])
    assert phone.config(only_id(phone))["UserAgent"] == ANDROID_FF.format(major)


# ----- Chromium and Chrome -----

BROWSER = {"type": "AdwComboRow", "text": "Browser"}


def test_browser_choice_with_more_than_one(program, gcc_world, phone):
    phone.firefox()
    phone.chromium()
    r = gcc_world.run(program, [*open_add(), dump("new")])
    row = r.dump("new").within(**EDITOR).one(**BROWSER)
    assert row["items"] == ["Firefox", "Chromium"] and row["selected"] == 0


def test_a_web_app_of_a_browser_no_longer_installed_keeps_it_offered(program, gcc_world, phone):
    phone.chromium()
    app = "00000000000000a1"
    phone.webapp(app, name="Shop", url="https://shop.example/", icon="icon-1.png", browser="firefox", active=False)
    toggle = {"type": "GtkSwitch", "within": {"type": "AdwActionRow", "text": "Shop"}}
    r = gcc_world.run(program, [*edit("Shop"), dump("editor"), {"op": "back"}, set_active(True, **toggle)])
    row = r.dump("editor").within(**EDITOR).one(**BROWSER)
    assert row["items"] == ["Firefox", "Chromium"] and row["selected"] == 0
    # Started as firefox, also while not installed
    assert phone.desktop(app)["Exec"].startswith('"firefox" "--name"')


@pytest.mark.parametrize("command, browser, label", [
    ("chromium", "chromium", "Chromium"),
    ("chromium-browser", "chromium", "Chromium"),
    ("google-chrome-stable", "chrome", "Google Chrome"),
    ("google-chrome", "chrome", "Google Chrome"),
])
def test_only_one_chromium_is_the_default_without_a_choice(program, gcc_world, phone, command, browser, label):
    phone.chromium(name=command, out="Google Chrome 157.0.1.2\n")
    r = gcc_world.run(program, [*open_add(), dump("new"), set_text("https://shop.example/", **ADDRESS),
                                set_active(False, type="AdwSwitchRow", text="Frameless"),
                                select(item="Mobile (Android)", **IDENTITY), *save()])
    assert not r.dump("new").within(**EDITOR).has(**BROWSER)
    app = only_id(phone)
    conf = phone.config(app)
    assert conf["Browser"] == browser and conf["UserAgent"] == ANDROID_CHROME.format(157)
    profile = phone.data / app / browser
    assert profile.is_dir() and not (phone.data / app / "profile").exists()
    assert list(profile.iterdir()) == []
    entry = phone.desktop(app)
    assert entry["Exec"] == (f'"{command}" "--user-data-dir={profile}" "--no-first-run" '
                             f'"--user-agent={ANDROID_CHROME.format(157)}" "--class=plinius-webapp-{app}" '
                             '"--new-window" "https://shop.example/"')
    assert entry["StartupWMClass"] == f"plinius-webapp-{app}"
    assert phone.progs.calls(command) == [["--version"]]


@pytest.mark.parametrize("out, major", [
    ("Chromium 156.0.7444.59 Arch Linux\n", 156),
    ("v12 Chromium 99.1\n", 99),
    ("no version here\n", 140),
])
def test_chromium_version_for_the_user_agent(program, gcc_world, phone, out, major):
    phone.chromium(out=out)
    gcc_world.run(program, [*open_add(), set_text("https://shop.example/", **ADDRESS),
                            select(item="Mobile (Android)", **IDENTITY), *save()])
    assert phone.config(only_id(phone))["UserAgent"] == ANDROID_CHROME.format(major)


def test_two_chromium_web_apps_ask_the_version_once(program, gcc_world, phone):
    phone.chromium()
    for app in ("00000000000000a1", "00000000000000a2"):
        phone.webapp(app, name=f"App {app[-1]}", url="https://a.example/", browser="chromium", IdentifyAs="linux")
    # And one whose browser is missing: the fallback version
    phone.webapp("00000000000000a3", name="Chrome App", url="https://a.example/", browser="chrome", IdentifyAs="linux")
    gcc_world.run(program, [dump("list")])
    assert phone.progs.calls("chromium") == [["--version"]]
    linux = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{}.0.0.0 "
             "Safari/537.36")
    assert phone.config("00000000000000a1")["UserAgent"] == linux.format(156)
    assert phone.config("00000000000000a3")["UserAgent"] == linux.format(140)


def test_frameless_chromium_with_deleting_starts_through_launch_sh(program, gcc_world, phone):
    import subprocess
    phone.firefox()
    phone.chromium()
    url = "https://shop.example/store/app"
    gcc_world.run(program, [*open_add(), set_text(url, **ADDRESS), select(item="Chromium", **BROWSER),
                            set_active(True, type="AdwSwitchRow", text="Delete Cache"),
                            set_active(True, type="AdwSwitchRow", text="Delete Cookies"), *save()])
    app = only_id(phone)
    profile = phone.data / app / "chromium"
    script = phone.data / app / "launch.sh"
    entry = phone.desktop(app)
    assert entry["Exec"] == f'"{script}"'
    assert entry["StartupWMClass"] == "chrome-shop.example__store_app-Default"
    assert script.stat().st_mode & 0o777 == 0o700
    text = script.read_text()
    assert text.startswith("#!/bin/sh\n") and f"profile='{profile}'" in text
    assert f"\nif running; then\n\texec 'chromium' '--user-data-dir={profile}' '--no-first-run' '--app={url}'\nfi\n" in text

    default = profile / "Default"
    kept = ["Preferences", "History"]
    gone = ["Cache/a", "Code Cache/js/b", "GPUCache/c", "Cookies", "Local Storage/leveldb/d", "IndexedDB/e"]
    for rel in kept + gone:
        f = default / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(rel)
    env = gcc_world.fake_bin.env(isolated=True)
    # Open already: the address goes to the running browser, nothing is deleted
    phone.running(app, profile="chromium", chromium=True)
    subprocess.run(["sh", str(script)], env=env, check=True)
    assert all((default / rel).exists() for rel in kept + gone)
    (profile / "SingletonLock").unlink()
    subprocess.run(["sh", str(script)], env=env, check=True)
    assert all((default / rel).exists() for rel in kept)
    assert not any((default / rel.split("/")[0]).exists() for rel in gone)
    assert phone.progs.calls("chromium")[-1] == [f"--user-data-dir={profile}", "--no-first-run", f"--app={url}"]


@pytest.mark.parametrize("cache, cookies", [(True, False), (False, True)])
def test_launch_sh_deletes_only_what_is_switched_on(program, gcc_world, phone, cache, cookies):
    phone.chromium()
    steps = [*open_add(), set_text("https://shop.example/", **ADDRESS)]
    if cache:
        steps.append(set_active(True, type="AdwSwitchRow", text="Delete Cache"))
    if cookies:
        steps.append(set_active(True, type="AdwSwitchRow", text="Delete Cookies"))
    gcc_world.run(program, [*steps, *save()])
    text = (phone.data / only_id(phone) / "launch.sh").read_text()
    assert ("rm -rf -- Cache 'Code Cache'" in text) is cache
    assert ("rm -rf -- Cookies Cookies-journal" in text) is cookies


def test_without_deleting_launch_sh_goes(program, gcc_world, phone):
    phone.chromium()
    app, odd = "00000000000000a1", "00000000000000a2"
    d = phone.webapp(app, name="Shop", url="https://shop.example", browser="chromium", Frameless="true",
                     active=False)
    (d / "launch.sh").write_text("old")
    # An address that is none (written by hand): the row and the desktop file name it as it is
    phone.webapp(odd, name="Odd", url="not an address", browser="chromium", Frameless="true", active=False)
    r = gcc_world.run(program, [dump("list"),
                                set_active(True, type="GtkSwitch", within={"type": "AdwActionRow", "text": "Shop"}),
                                set_active(True, type="GtkSwitch", within={"type": "AdwActionRow", "text": "Odd"})])
    assert not (d / "launch.sh").exists()
    entry = phone.desktop(app)
    # Frameless: the app window's class, from the host and the path ("/" when there is none)
    assert entry["StartupWMClass"] == "chrome-shop.example__-Default"
    assert entry["Exec"].endswith('"--app=https://shop.example"')
    assert ("Odd", "not an address", False) in app_rows(r.dump("list"))
    entry = phone.desktop(odd)
    assert entry["StartupWMClass"] == f"plinius-webapp-{odd}" and entry["Comment"] == "not an address"


# ----- Reset Website Data -----

RESET = [click(type="GtkButton", text="Reset Website Data", within=EDITOR), wait_for(**ALERT)]


def test_reset_website_data_asks_and_keeps_cookies(program, gcc_world, phone):
    phone.firefox()
    app = "00000000000000a1"
    d = phone.webapp(app, name="Shop", url="https://shop.example/", icon="icon-1.png")
    files = site_data(d)
    outside = gcc_world.tmp / "outside"
    (outside / "keep").mkdir(parents=True)
    (d / "profile/storage/temporary").rename(d / "profile/temporary-was")
    (d / "profile/storage/temporary").symlink_to(outside)
    files.pop("profile/storage/temporary/x")
    r = gcc_world.run(program, [*edit("Shop"), *RESET, dump("asked"),
                                click(type="GtkButton", text="Reset", within=ALERT), wait(500), dump("done")])
    alert = r.dump("asked").one(**ALERT)
    assert alert["text"] == "Reset Website Data?"
    assert alert["subtitle"] == ("“Shop” forgets what the website stored on this phone, except cookies: you stay "
                                 "signed in. This helps when the website keeps failing, for example after "
                                 "Identify As changed.")
    assert_site_data(d, files)
    # A link is removed, not followed
    assert not (d / "profile/storage/temporary").is_symlink() and (outside / "keep").is_dir()
    assert not r.dump("done").has(**ALERT) and r.dump("done").has(**EDITOR)
    assert not r.warnings()


def test_reset_cancelled_keeps_everything(program, gcc_world, phone):
    phone.firefox()
    app = "00000000000000a1"
    d = phone.webapp(app, name="Shop", url="https://shop.example/", icon="icon-1.png")
    files = site_data(d)
    gcc_world.run(program, [*edit("Shop"), *RESET, click(type="GtkButton", text="Cancel", within=ALERT), wait(300)])
    assert_site_data(d, files, cleared=False)


@pytest.mark.parametrize("lock", ["firefox", "chromium", "eperm"])
def test_reset_refused_while_open(program, gcc_world, phone, lock):
    phone.firefox()
    app = "00000000000000a1"
    d = phone.webapp(app, name="Shop", url="https://shop.example/", icon="icon-1.png")
    files = site_data(d)
    if lock == "chromium":
        phone.running(app, profile="chromium", chromium=True)
    else:
        # pid 1 is not ours to signal: it runs all the same
        phone.running(app, pid=1 if lock == "eperm" else None)
    r = gcc_world.run(program, [*edit("Shop"), click(type="GtkButton", text="Reset Website Data", within=EDITOR),
                                wait_for(**ALERT), dump("refused")])
    alert = r.dump("refused").one(**ALERT)
    assert (alert["text"], alert["subtitle"]) == ("“Shop” Is Open",
                                                  "Close the web app first, then reset its website data.")
    assert_site_data(d, files, cleared=False)


def test_reset_refused_when_opened_while_asking(program, gcc_world, phone):
    phone.firefox()
    app = "00000000000000a1"
    d = phone.webapp(app, name="Shop", url="https://shop.example/", icon="icon-1.png")
    files = site_data(d)
    r = gcc_world.run(program, [*edit("Shop"), *RESET,
                                call("symlink", f"127.0.0.1:+{os.getpid()}", str(d / "profile/lock")),
                                click(type="GtkButton", text="Reset", within=ALERT),
                                wait_for(**ALERT), dump("refused")])
    alert = r.dump("refused").one(**ALERT)
    assert alert["text"] == "“Shop” Is Open"
    assert_site_data(d, files, cleared=False)


@pytest.mark.parametrize("target", ["phone-abc", "phone-0", "127.0.0.1:+999999999", "no separator"])
def test_a_stale_lock_is_no_running_browser(program, gcc_world, phone, target):
    phone.firefox()
    app = "00000000000000a1"
    d = phone.webapp(app, name="Shop", url="https://shop.example/", icon="icon-1.png")
    files = site_data(d)
    lock = d / ("chromium/SingletonLock" if target.startswith("phone") else "profile/lock")
    lock.symlink_to(target)
    gcc_world.run(program, [*edit("Shop"), *RESET, click(type="GtkButton", text="Reset", within=ALERT), wait(500)])
    assert_site_data(d, files)


@pytest.mark.parametrize("tab", ["shown", "gone"])
def test_reset_that_fails(program, gcc_world, phone, tab):
    phone.firefox()
    app = "00000000000000a1"
    d = phone.webapp(app, name="Shop", url="https://shop.example/", icon="icon-1.png")
    for rel in ("profile/storage/default/https+++site/x", "chromium/Default/IndexedDB/locked/y"):
        (d / rel).parent.mkdir(parents=True)
        (d / rel).write_text("x")
    locked = [d / "profile/storage/default/https+++site", d / "chromium/Default/IndexedDB/locked"]
    variant = locked[0] if tab == "shown" else locked[1]
    variant.chmod(0o500)
    try:
        steps = [*edit("Shop"), *RESET]
        if tab == "gone":
            steps.append(call("drop_tab"))
        r = gcc_world.run(program, [*steps, click(type="GtkButton", text="Reset", within=ALERT), wait(600),
                                    dump("after")])
    finally:
        variant.chmod(0o700)
    warnings = [m["message"] for m in r.warnings()]
    assert len(warnings) == 1 and warnings[0].startswith("Could not reset the website data of web app Shop: ")
    alerts = r.dump("after").find(**ALERT)
    if tab == "shown":
        assert [a["text"] for a in alerts] == ["Could Not Reset the Website Data"]
        assert "Permission denied" in alerts[0]["subtitle"]
    else:
        assert alerts == []


def test_identity_change_whose_reset_fails_says_so(program, gcc_world, phone):
    phone.firefox()
    app = "00000000000000a1"
    d = phone.webapp(app, name="Shop", url="https://shop.example/", icon="icon-1.png")
    (d / "profile/gmp/x").mkdir(parents=True)
    (d / "profile/gmp/x/y").write_text("y")
    (d / "profile/gmp/x").chmod(0o500)
    try:
        r = gcc_world.run(program, [*edit("Shop"), select(item="Desktop (Linux)", **IDENTITY), *save("Save"),
                                    wait_for(**ALERT), dump("after")])
    finally:
        (d / "profile/gmp/x").chmod(0o700)
    assert r.dump("after").one(**ALERT)["text"] == "Could Not Reset the Website Data"
    assert phone.config(app)["IdentifyAs"] == "linux"


# ----- Delete -----

DELETE = [click(type="GtkButton", text="Delete", within=EDITOR), wait_for(**ALERT)]


def test_delete_asks_and_removes_everything(program, gcc_world, phone):
    phone.firefox()
    app, other = "00000000000000a1", "00000000000000a2"
    d = phone.webapp(app, name="Shop", url="https://shop.example/", icon="icon-1.png")
    phone.webapp(other, name="Other", url="https://other.example/")
    site_data(d)
    outside = gcc_world.tmp / "outside"
    outside.mkdir()
    (outside / "keep").write_text("k")
    (d / "profile/linked").symlink_to(outside)
    phone.apps_dir.mkdir(parents=True, exist_ok=True)
    (phone.apps_dir / f"plinius-webapp-{app}.desktop").write_text("[Desktop Entry]\n")
    r = gcc_world.run(program, [*edit("Shop"), *DELETE, dump("asked"),
                                click(type="GtkButton", text="Delete", within=ALERT),
                                wait_for(gone=True, type="AdwActionRow", text="Shop"), dump("list")])
    alert = r.dump("asked").one(**ALERT)
    assert alert["text"] == "Delete Web App?"
    assert alert["subtitle"] == ("“Shop” is removed with its profile: logins, cookies and all other data the website "
                                 "stored on this phone.")
    assert phone.ids() == [other]
    assert phone.desktop(app) is None
    assert (outside / "keep").read_text() == "k"
    assert app_rows(r.dump("list")) == [("Other", "other.example", True)]
    assert not r.warnings()


def test_delete_cancelled(program, gcc_world, phone):
    phone.firefox()
    app = "00000000000000a1"
    phone.webapp(app, name="Shop", url="https://shop.example/", icon="icon-1.png")
    r = gcc_world.run(program, [*edit("Shop"), *DELETE, click(type="GtkButton", text="Cancel", within=ALERT),
                                wait(300), dump("still")])
    assert phone.ids() == [app]
    assert r.dump("still").has(**EDITOR)


def test_delete_refused_while_open(program, gcc_world, phone):
    phone.firefox()
    app = "00000000000000a1"
    phone.webapp(app, name="Shop", url="https://shop.example/", icon="icon-1.png")
    phone.running(app)
    r = gcc_world.run(program, [*edit("Shop"), click(type="GtkButton", text="Delete", within=EDITOR),
                                wait_for(**ALERT), dump("refused")])
    alert = r.dump("refused").one(**ALERT)
    assert (alert["text"], alert["subtitle"]) == ("“Shop” Is Open", "Close the web app first, then delete it.")
    assert phone.ids() == [app]


@pytest.mark.parametrize("tab", ["shown", "gone"])
def test_delete_that_fails(program, gcc_world, phone, tab):
    phone.firefox()
    app = "00000000000000a1"
    d = phone.webapp(app, name="Shop", url="https://shop.example/", icon="icon-1.png")
    (d / "profile/locked").mkdir(parents=True)
    (d / "profile/locked/file").write_text("x")
    phone.apps_dir.mkdir(parents=True, exist_ok=True)
    (phone.apps_dir / f"plinius-webapp-{app}.desktop").write_text("[Desktop Entry]\n")
    (d / "profile/locked").chmod(0o500)
    try:
        steps = [*edit("Shop"), *DELETE]
        if tab == "gone":
            steps.append(call("drop_tab"))
        r = gcc_world.run(program, [*steps, click(type="GtkButton", text="Delete", within=ALERT), wait(600),
                                    dump("after")])
    finally:
        (d / "profile/locked").chmod(0o700)
    warnings = [m["message"] for m in r.warnings()]
    assert len(warnings) == 1 and warnings[0].startswith("Could not delete web app Shop: ")
    # The icon among the apps is gone first
    assert phone.desktop(app) is None
    assert (d / "profile/locked/file").exists()
    alerts = r.dump("after").find(**ALERT)
    if tab == "shown":
        assert [a["text"] for a in alerts] == ["Could Not Delete the Web App"]
    else:
        assert alerts == []


def test_delete_when_the_desktop_file_cannot_go(program, gcc_world, phone):
    phone.firefox()
    app = "00000000000000a1"
    phone.webapp(app, name="Shop", url="https://shop.example/", icon="icon-1.png")
    phone.apps_dir.mkdir(parents=True, exist_ok=True)
    (phone.apps_dir / f"plinius-webapp-{app}.desktop").write_text("[Desktop Entry]\n")
    phone.apps_dir.chmod(0o500)
    try:
        r = gcc_world.run(program, [*edit("Shop"), *DELETE, click(type="GtkButton", text="Delete", within=ALERT),
                                    wait_for(**ALERT), dump("after")])
    finally:
        phone.apps_dir.chmod(0o700)
    assert r.dump("after").one(**ALERT)["text"] == "Could Not Delete the Web App"
    assert phone.ids() == [app]


def test_saving_after_the_tab_is_gone(program, gcc_world, phone):
    phone.firefox()
    app = "00000000000000a1"
    phone.webapp(app, name="Shop", url="https://shop.example/", icon="icon-1.png")
    r = gcc_world.run(program, [*edit("Shop"), call("drop_tab"), set_text("Shop 2", **NAME),
                                click(type="GtkButton", text="Save", within=EDITOR), wait(300)])
    assert phone.config(app)["Name"] == "Shop 2"
    assert not r.warnings()


# ----- More of what can go wrong -----

def test_browser_gone_after_the_tab_opened(program, gcc_world, phone):
    phone.firefox()
    gcc_world.run(program, [{"op": "remove_file", "path": str(gcc_world.fake_bin.path / "firefox")},
                  *open_add(), set_text("https://shop.example/", **ADDRESS), *save()])
    # Firefox, the browser web apps had first
    assert phone.config(only_id(phone))["Browser"] == "firefox"
    assert phone.desktop(only_id(phone))["Exec"].startswith('"firefox" ')


@pytest.mark.parametrize("break_it", ["profile", "launch.sh", "launch.sh stays", "applications", "icon"])
def test_saving_an_existing_web_app_that_fails(program, gcc_world, phone, break_it):
    phone.firefox()
    phone.chromium()
    app = "00000000000000a1"
    browser = "chromium" if break_it.startswith("launch.sh") else "firefox"
    d = phone.webapp(app, name="Shop", url=phone.web.url("/"), icon="icon-1.png", browser=browser,
                     ClearCache="true" if break_it == "launch.sh" else "false")
    phone.web.route("/", page("Shop", '<link rel="icon" href="/i.png" sizes="512x512">'))
    phone.web.route("/i.png", png(64, 64, RED))
    locked = None
    if break_it == "profile":
        locked = d / "profile"
        locked.mkdir()
    elif break_it.startswith("launch.sh"):
        # A folder where the script goes: it can neither be written nor removed
        (d / "launch.sh/x").mkdir(parents=True)
    elif break_it == "applications":
        phone.apps_dir.parent.mkdir(parents=True, exist_ok=True)
        phone.apps_dir.write_text("not a folder")
    elif break_it == "icon":
        locked = d
    steps = [*edit("Shop")]
    if break_it == "icon":
        steps += [click(type="GtkButton", text="From Website"),
                  wait_for(gone=True, type="GtkLabel", text=LOOKING, timeout=20000)]
    if locked:
        locked.chmod(0o500)
    try:
        r = gcc_world.run(program, [*steps, click(type="GtkButton", text="Save", within=EDITOR), wait_for(**ALERT),
                                    dump("error")])
    finally:
        if locked:
            locked.chmod(0o700)
    alert = r.dump("error").one(**ALERT)
    assert alert["text"] == "Could Not Save the Web App"
    assert r.dump("error").has(**EDITOR)
    warnings = [m["message"] for m in r.warnings()]
    assert len(warnings) == 1 and warnings[0].startswith(f"Could not save web app {app}: ")
    # The id stays: it is no new one
    assert phone.ids() == [app]


@pytest.mark.parametrize("slow", ["page", "manifest", "icon"])
def test_a_new_search_cancels_the_running_one(program, gcc_world, phone, slow):
    phone.firefox()
    phone.web.route("/", page("First", '<link rel="manifest" href="/m.json">'),
                    delay=1.0 if slow == "page" else 0)
    phone.web.route("/m.json", json.dumps({"icons": [{"src": "/i.png", "sizes": "512x512"}]}),
                    delay=1.0 if slow == "manifest" else 0)
    phone.web.route("/i.png", png(64, 64, RED), delay=1.0 if slow == "icon" else 0)
    phone.web.route("/other/", page("Second"))
    r = gcc_world.run(program, [*open_add(), set_text(phone.web.url("/"), **ADDRESS), enter(**ADDRESS), wait(400),
                                *look_up(phone.web.url("/other/")), wait(1500), dump("after")])
    # Only the second search's answer counts
    assert entry_value(r.dump("after"), "Name") == "Second"
    assert status_text(r.dump("after")) == NO_ICON
    assert not r.warnings()


def test_choosing_an_image_cancels_the_search(program, gcc_world, phone):
    phone.firefox()
    phone.web.route("/", page("Site", '<link rel="icon" href="/i.png" sizes="512x512">'))
    phone.web.route("/i.png", png(64, 64, RED), delay=1.5)
    chosen = gcc_world.home / "logo.png"
    chosen.write_bytes(png(64, 64, GREEN))
    FileChooserPortal(gcc_world, [{"uris": [chosen.as_uri()]}]).start()
    r = gcc_world.run(program, [*open_add(), set_text(phone.web.url("/"), **ADDRESS), enter(**ADDRESS), wait(300),
                                click(type="GtkButton", text="Choose Image…"), wait(2000), *save()])
    app = only_id(phone)
    assert pixbuf_facts(phone.data / app / phone.config(app)["Icon"])[3][:3] == GREEN[:3]
    # GTK's own bug, not the panel's (gtkpathbar.c up to GTK main, 4.24.1 and 4.22.5 alike):
    # GtkFileDialog keeps a hidden GtkFileChooserWidget even when the portal answers. When it goes
    # away while its path bar still walks up the folders, the cancelled query ends in
    # gtk_path_bar_set_file_finish (FALSE), which calls gtk_box_remove () on the disposed
    # buttons box. Here the search running alongside slows that walk down enough to hit it.
    assert not r.warnings(ignore=[r"^gtk_box_remove: assertion 'GTK_IS_BOX \(box\)' failed$"])


@pytest.mark.parametrize("width, height", [(4097, 1), (1, 4097)])
def test_an_icon_claiming_more_than_4096_pixels_is_dropped(program, gcc_world, phone, width, height):
    phone.firefox()
    phone.web.route("/", page("F", '<link rel="icon" href="/i.png" sizes="512x512">'))
    phone.web.route("/i.png", png(width, height, RED))
    phone.web.route("/favicon.ico", image("ico", 32, 32, BLUE))
    gcc_world.run(program, [*open_add(), *look_up(phone.web.url("/")), *save()])
    app = only_id(phone)
    assert pixbuf_facts(phone.data / app / phone.config(app)["Icon"])[3][:3] == BLUE[:3]


def test_the_late_icon_that_cannot_be_stored(program, gcc_world, phone):
    phone.firefox()
    phone.web.route("/", page("Later", '<link rel="icon" href="/i.png" sizes="512x512">'))
    phone.web.route("/i.png", png(64, 64, RED), delay=1.5)
    r = gcc_world.run(program, [*open_add(), set_text(phone.web.url("/"), **ADDRESS), *save(),
                                call("chmod_app_dirs", str(phone.data), 0o500), wait(2500),
                                call("chmod_app_dirs", str(phone.data), 0o700)])
    app = only_id(phone)
    assert "Icon" not in phone.config(app)
    warnings = [m["message"] for m in r.warnings()]
    assert len(warnings) == 1 and warnings[0].startswith(f"Could not store the icon of web app {app}: ")


# ----- Adding from the catalog -----

SEARCH = {"type": "AdwNavigationPage", "tag": "plinius-webapp-search"}
CATEGORY = {"type": "AdwNavigationPage", "tag": "plinius-webapp-category"}
SEARCH_ENTRY = {"type": "GtkSearchEntry"}


def entry(id, name, url, category="shopping", keywords=(), **settings):
    """An entry as the helper prints it."""
    e = {"id": id, "name": name, "url": url, "category": category, "keywords": list(keywords),
         "frameless": True, "clear_cache": False, "clear_cookies": False, "identify_as": "", "icon": ""}
    e.update(settings)
    return e


def answer(*apps, updated=1):
    return json.dumps({"updated": updated, "language": "de", "country": "DE", "apps": list(apps)})


SHOPS = [entry("mail", "Mail", "https://mail.example/", "communication", ["email"]),
         entry("amazon", "Amazon", "https://www.amazon.de/", keywords=["einkaufen"]),
         entry("kleinanzeigen", "Kleinanzeigen", "https://www.kleinanzeigen.de/"),
         entry("db", "Deutsche Bahn", "https://www.bahn.de/", "travel", ["zug"]),
         entry("oeffi", "Öffi", "https://oeffi.example/", "travel")]


def open_search():
    return [click(type="GtkButton", text="Add"), wait_for(**SEARCH)]


def open_category(title):
    return [click(type="AdwActionRow", text=title, within=SEARCH), wait_for(**CATEGORY)]


def rows(screen, page=SEARCH):
    """[(title, subtitle)] of the rows of a page."""
    return [(r["text"], r.get("subtitle")) for r in screen.within(**page).find(type="AdwActionRow")]


def names(screen, page=SEARCH):
    return [t for t, _ in rows(screen, page)]


def row_icons(screen, page=SEARCH):
    """{title: the icon name of its picture, None for an image from a file}"""
    out = {}
    for r in screen.within(**page).find(type="AdwActionRow"):
        icons = [n["icon"] for n in Node_walk(r) if n.get("type") == "GtkImage" and n.get("icon")]
        out[r["text"]] = icons[0] if icons else None
    return out


def test_add_opens_the_search_with_manually_beside_it(program, gcc_world, phone):
    phone.firefox()
    phone.catalog(answer(*SHOPS), answer(*SHOPS))
    r = gcc_world.run(program, [*open_search(), wait_for(type="AdwActionRow", text="Shopping"), dump("search"),
                                click(type="GtkButton", text="Manually", within=SEARCH), editor_open(), dump("editor"),
                                set_text("https://shop.example/", **ADDRESS), *save(), wait_for(gone=True, **SEARCH),
                                dump("list")])
    search = r.dump("search")
    # The first page after Add: the search field, the groups below it, Manually at the top right
    assert search.one(**SEARCH)["text"] == "Add Web App"
    assert search.within(**SEARCH).has(type="GtkButton", text="Manually")
    assert entry_value(r.dump("editor"), "Address") == "" and entry_value(r.dump("editor"), "Name") == ""
    # Saved: back to the list, past the search
    assert app_rows(r.dump("list")) == [("shop.example", "shop.example", True)]
    assert phone.catalog_calls() == [["--offline"], []]
    assert not r.warnings()


def test_without_the_catalog_add_opens_the_empty_page(program, gcc_world, phone):
    phone.firefox()
    r = gcc_world.run(program, [*open_add(), dump("editor")])
    assert not r.dump("editor").has(**SEARCH)
    assert r.dump("editor").one(**EDITOR)["text"] == "Add Web App"


def test_nothing_typed_shows_the_groups_only(program, gcc_world, phone):
    phone.firefox()
    many = [*SHOPS, entry("otto", "Otto", "https://otto.example/"), entry("zalando", "Zalando", "https://z.example/")]
    phone.catalog(answer(*many), answer(*many))
    r = gcc_world.run(program, [*open_search(), wait_for(type="AdwActionRow", text="Shopping"), dump("search")])
    s = r.dump("search")
    # The groups that have web apps, in the helper's order, with the first three of each; no web app rows
    assert rows(s) == [("Communication", "Mail"), ("Shopping", "Amazon, Kleinanzeigen, Otto, …"),
                       ("Travel & Maps", "Deutsche Bahn, Öffi")]
    assert row_icons(s) == {"Communication": "mail-unread-symbolic", "Shopping": "emoji-objects-symbolic",
                            "Travel & Maps": "emoji-travel-symbolic"}
    assert not s.within(**SEARCH).has(type="AdwPreferencesGroup", text="Shopping")
    assert not r.warnings()


def test_a_group_lists_its_web_apps_on_a_page_of_its_own(program, gcc_world, phone):
    phone.firefox()
    icon = gcc_world.home / "amazon.png"
    icon.write_bytes(png(128, 128, RED))
    apps = [entry("amazon", "Amazon", "https://www.amazon.de/", icon=str(icon)),
            entry("ka", "Kleinanzeigen", "https://www.kleinanzeigen.de/"),
            entry("broken", "Broken Icon", "https://b.example/", icon=str(gcc_world.home / "missing.png")),
            entry("relative", "Relative Icon", "https://r.example/", icon="amazon.png"),
            entry("db", "Deutsche Bahn", "https://www.bahn.de/", "travel")]
    phone.catalog(answer(*apps), answer(*apps))
    r = gcc_world.run(program, [*open_search(), wait_for(type="AdwActionRow", text="Shopping"),
                                *open_category("Shopping"), dump("group"), back(), wait_for(**SEARCH), dump("back")])
    g = r.dump("group")
    assert g.one(**CATEGORY)["text"] == "Shopping"
    assert rows(g, CATEGORY) == [("Amazon", "www.amazon.de"), ("Kleinanzeigen", "www.kleinanzeigen.de"),
                                 ("Broken Icon", "b.example"), ("Relative Icon", "r.example")]
    # The catalog's icon in front of the name; without a usable one the generic icon
    assert row_icons(g, CATEGORY) == {"Amazon": None, "Kleinanzeigen": "web-browser", "Broken Icon": "web-browser",
                                      "Relative Icon": "web-browser"}
    # Back (a swipe) to the groups
    assert names(r.dump("back")) == ["Shopping", "Travel & Maps"]
    assert not r.warnings()


def test_typing_shows_the_web_apps_alone_by_name(program, gcc_world, phone):
    phone.firefox()
    phone.catalog(answer(*SHOPS), answer(*SHOPS))
    r = gcc_world.run(program, [*open_search(), wait_for(type="AdwActionRow", text="Shopping"),
                                set_text("e", **SEARCH_ENTRY), wait(500), dump("found"),
                                set_text("", **SEARCH_ENTRY), wait(500), dump("again")])
    f = r.dump("found")
    # Every web app with a word starting with "e" (email, einkaufen, example...), by name, no groups
    assert rows(f) == [("Amazon", "www.amazon.de"), ("Mail", "mail.example"), ("Öffi", "oeffi.example")]
    assert row_icons(f) == {"Amazon": "web-browser", "Mail": "web-browser", "Öffi": "web-browser"}
    assert names(r.dump("again")) == ["Communication", "Shopping", "Travel & Maps"]


@pytest.mark.parametrize("query, found", [
    ("bahn", ["Deutsche Bahn"]),          # a word of the name
    ("deutsche bah", ["Deutsche Bahn"]),  # every word, the last one begun
    ("einkaufen", ["Amazon"]),            # a keyword
    ("AMAZON.DE", ["Amazon"]),            # the host, in any case
    ("kleinanz", ["Kleinanzeigen"]),
    ("offi", ["Öffi"]),                   # without its accents
    ("  zug ", ["Deutsche Bahn"]),
])
def test_search_finds_by_name_host_and_keywords(program, gcc_world, phone, query, found):
    phone.firefox()
    phone.catalog(answer(*SHOPS), answer(*SHOPS))
    r = gcc_world.run(program, [*open_search(), wait_for(type="AdwActionRow", text="Shopping"),
                                set_text(query, **SEARCH_ENTRY), wait(500), dump("found")])
    assert names(r.dump("found")) == found


KINDS = [entry("bank", "Example Bank", "https://bank.example/", "finance"),
         entry("pay", "PayExample", "https://pay.example/", "finance"),
         entry("chess", "Chess Club", "https://chess.example/", "games"),
         entry("quiz", "Quiz", "https://quiz.example/", "games"),
         entry("shop", "Shop", "https://shop.example/")]


@pytest.mark.parametrize("query, found", [
    ("bank", ["Example Bank", "PayExample"]),   # a word of the group, and a name
    ("banks", ["Example Bank", "PayExample"]),  # the group's title
    ("payments", ["Example Bank", "PayExample"]),
    ("games", ["Chess Club", "Quiz"]),
    ("chess", ["Chess Club", "Quiz"]),          # the group's words count for every entry in it
    ("quiz", ["Quiz"]),
    ("games quiz", ["Quiz"]),
    ("shop", ["Shop"]),
])
def test_search_finds_by_the_kind_of_website(program, gcc_world, phone, query, found):
    phone.firefox()
    phone.catalog(answer(*KINDS), answer(*KINDS))
    r = gcc_world.run(program, [*open_search(), wait_for(type="AdwActionRow", text="Games"),
                                set_text(query, **SEARCH_ENTRY), wait(500), dump("found")])
    assert names(r.dump("found")) == found


def test_nothing_found_offers_entering_manually(program, gcc_world, phone):
    phone.firefox()
    phone.catalog(answer(*SHOPS), answer(*SHOPS))
    r = gcc_world.run(program, [*open_search(), wait_for(type="AdwActionRow", text="Shopping"),
                                set_text("nothing like it", **SEARCH_ENTRY), wait(500), dump("none"),
                                click(type="GtkButton", text="Enter Manually"), editor_open(),
                                set_text("https://own.example/", **ADDRESS), *save(), wait_for(gone=True, **SEARCH),
                                dump("list")])
    status = r.dump("none").within(**SEARCH).one(type="AdwStatusPage")
    assert (status["text"], status["subtitle"]) == ("No Web Apps Found", "Add the website with its address instead.")
    assert not rows(r.dump("none"))
    assert app_rows(r.dump("list")) == [("own.example", "own.example", True)]


def test_an_entry_fills_in_the_page_with_its_icon_and_adds_it(program, gcc_world, phone):
    phone.firefox("159.0.1")
    phone.web.route("/shop.example/start", page("The Website's Own Title"))
    icon = gcc_world.home / "shop.png"
    icon.write_bytes(png(128, 128, BLUE))
    shop = entry("shop", "Shop", "https://shop.example/start", frameless=False, clear_cookies=True,
                 identify_as="android", icon=str(icon))
    phone.catalog(answer(shop), answer(shop))
    r = gcc_world.run(program, [*open_search(), wait_for(type="AdwActionRow", text="Shopping"),
                                *open_category("Shopping"), click(type="AdwActionRow", text="Shop", within=CATEGORY),
                                editor_open(), wait(1500), dump("editor"), set_text("https://shop.example/other", **ADDRESS),
                                wait(1500), *save(), wait_for(gone=True, **CATEGORY), dump("list")])
    ed = r.dump("editor")
    assert ed.one(**EDITOR)["text"] == "Add Web App"
    assert entry_value(ed, "Address") == "https://shop.example/start" and entry_value(ed, "Name") == "Shop"
    e = ed.within(**EDITOR)
    assert e.one(**IDENTITY)["selected_text"] == "Mobile (Android)"
    assert [(n["text"], n["active"]) for n in e.find(type="AdwSwitchRow")] == [
        ("Frameless", False), ("Delete Cache", False), ("Delete Cookies", True)]
    assert status_text(ed) is None
    # The catalog's icon: the website is not asked, also not for another address
    assert phone.web.requests == []
    app = only_id(phone)
    conf = phone.config(app)
    assert conf["Name"] == "Shop" and conf["URL"] == "https://shop.example/other"
    assert conf["Frameless"] == "false" and conf["ClearCache"] == "false" and conf["ClearCookies"] == "true"
    assert conf["IdentifyAs"] == "android" and conf["UserAgent"] == ANDROID_FF.format(159)
    assert pixbuf_facts(phone.data / app / conf["Icon"])[3][:3] == BLUE[:3]
    # Back to the list, past the group and the search
    assert not r.dump("list").has(**SEARCH) and not r.dump("list").has(**CATEGORY)
    assert app_rows(r.dump("list")) == [("Shop", "shop.example", True)]
    assert not r.warnings()


def test_an_entry_without_icon_looks_on_the_website(program, gcc_world, phone):
    phone.firefox()
    phone.web.route("/shop.example/", page("The Website's Own Title",
                                           '<link rel="icon" href="/shop.example/i.png" sizes="64x64">'))
    phone.web.route("/shop.example/i.png", png(64, 64, RED))
    shop = entry("shop", "Shop", "https://shop.example/", icon=str(gcc_world.home / "missing.png"))
    phone.catalog(answer(shop), answer(shop))
    r = gcc_world.run(program, [*open_search(), wait_for(type="AdwActionRow", text="Shopping"),
                                set_text("shop", **SEARCH_ENTRY), wait(500),
                                click(type="AdwActionRow", text="Shop", within=SEARCH), editor_open(),
                                wait_for(gone=True, type="GtkLabel", text=LOOKING, timeout=20000), dump("editor"),
                                *save(), wait_for(gone=True, **SEARCH)])
    # The catalog's name stays, not the website's title
    assert entry_value(r.dump("editor"), "Name") == "Shop"
    app = only_id(phone)
    assert pixbuf_facts(phone.data / app / phone.config(app)["Icon"])[3][:3] == RED[:3]
    assert not r.warnings()


def test_the_same_entry_twice_is_two_web_apps(program, gcc_world, phone):
    phone.firefox()
    shop = entry("shop", "Shop", "https://shop.example/")
    phone.catalog(answer(shop), answer(shop))
    steps = [*open_search(), wait_for(type="AdwActionRow", text="Shopping"), *open_category("Shopping"),
             click(type="AdwActionRow", text="Shop", within=CATEGORY), editor_open(), *save(),
             wait_for(gone=True, **SEARCH)]
    gcc_world.run(program, [*steps, *steps])
    assert len(phone.ids()) == 2


@pytest.mark.parametrize("online, shown", [
    ({"out": answer(entry("b", "Newer", "https://b.example/", "news"), updated=2)}, ["News"]),
    ({"out": answer(entry("b", "Older", "https://b.example/", "news"), updated=0)}, ["Shopping"]),
    ({"out": answer(entry("b", "Same Time", "https://b.example/", "news"), updated=1)}, ["Shopping"]),
    ({"code": 1}, ["Shopping"]),
])
def test_a_newer_catalog_from_the_server_replaces_the_list(program, gcc_world, phone, online, shown):
    phone.firefox()
    phone.catalog(answer(entry("a", "Offline", "https://a.example/"), updated=1), dict(online, sleep=1))
    r = gcc_world.run(program, [*open_search(), wait_for(type="AdwActionRow", text="Shopping"), wait(2000),
                                dump("search")])
    assert names(r.dump("search")) == shown
    assert len(phone.catalog_calls()) == 2
    assert [m["message"] for m in r.warnings()] == (
        ["The catalog of web apps could not be listed"] if "code" in online else [])


def test_the_newer_catalog_keeps_what_was_typed(program, gcc_world, phone):
    phone.firefox()
    phone.catalog(answer(entry("a", "Alpha", "https://a.example/"), updated=1),
                  {"out": answer(entry("a", "Alpha", "https://a.example/"), entry("a2", "Alpha Two", "https://a2.example/"),
                                 entry("z", "Zulu", "https://z.example/"), updated=2), "sleep": 1})
    r = gcc_world.run(program, [*open_search(), wait_for(type="AdwActionRow", text="Shopping"),
                                set_text("alpha", **SEARCH_ENTRY), wait_for(type="AdwActionRow", text="Alpha Two"),
                                dump("search")])
    assert names(r.dump("search")) == ["Alpha", "Alpha Two"]


def test_an_open_group_keeps_its_web_apps_when_a_newer_catalog_comes(program, gcc_world, phone):
    phone.firefox()
    phone.catalog(answer(entry("a", "Alpha", "https://a.example/"), updated=1),
                  {"out": answer(entry("z", "Zulu", "https://z.example/"), updated=2), "sleep": 1})
    r = gcc_world.run(program, [*open_search(), wait_for(type="AdwActionRow", text="Shopping"),
                                *open_category("Shopping"), wait(2000), click(type="AdwActionRow", text="Alpha"),
                                editor_open(), dump("editor"), back(), wait_for(**CATEGORY), back(),
                                wait_for(**SEARCH), *open_category("Shopping"), dump("group")])
    # The page shows what it opened with; the next one the newer catalog
    assert entry_value(r.dump("editor"), "Name") == "Alpha"
    assert names(r.dump("group"), CATEGORY) == ["Zulu"]
    assert not r.warnings()


@pytest.mark.parametrize("out, warning", [
    ({"code": 1}, "The catalog of web apps could not be listed"),
    ("not json", "The catalog of web apps is not readable"),
    ("[]", "The catalog of web apps is not readable"),
    ('{"apps": 3}', "The catalog of web apps is not readable"),
])
def test_without_any_answer_the_list_is_not_available(program, gcc_world, phone, out, warning):
    phone.firefox()
    phone.catalog(out, out)
    r = gcc_world.run(program, [*open_search(), wait_for(type="AdwStatusPage", within=SEARCH), wait(500), dump("none")])
    status = r.dump("none").within(**SEARCH).one(type="AdwStatusPage")
    assert status["text"] == "The List Is Not Available"
    assert [m["message"] for m in r.warnings()] == [warning, warning]


def test_entries_the_page_cannot_use_are_left_out(program, gcc_world, phone):
    phone.firefox()
    good = entry("good", "Good", "https://good.example/")
    text = json.dumps({"updated": "soon", "apps": [
        3, entry("noname", "", "https://x.example/"), entry("ftp", "FTP", "ftp://x.example/"),
        entry("nohost", "No Host", "https://"), entry("other", "Other", "https://o.example/", "weather"),
        {"name": "No Address", "category": "shopping"}, {"name": 5, "url": "https://n.example/", "category": "news"},
        dict(good, frameless="yes", identify_as="spaceship", keywords="not a list", icon=3),
        entry("plain", "Plain Host", "plain.example", "news", [3, "word"])]})
    phone.catalog(text, text)
    r = gcc_world.run(program, [*open_search(), wait_for(type="AdwActionRow", text="Shopping"), dump("groups"),
                                *open_category("Shopping"), click(type="AdwActionRow", text="Good", within=CATEGORY),
                                editor_open(), dump("editor"), back(), wait_for(**CATEGORY), back(),
                                wait_for(**SEARCH), set_text("word", **SEARCH_ENTRY), wait(500), dump("search")])
    assert names(r.dump("groups")) == ["News", "Shopping"]
    e = r.dump("editor").within(**EDITOR)
    # Settings the page does not know fall back to those of a new web app
    assert e.one(**IDENTITY)["selected_text"] == "Browser Default"
    assert [(n["text"], n["active"]) for n in e.find(type="AdwSwitchRow")][0] == ("Frameless", True)
    assert rows(r.dump("search")) == [("Plain Host", "plain.example")]
    assert not r.warnings()


def test_a_helper_that_cannot_start(program, gcc_world, phone):
    phone.firefox()
    helper = phone.path(CATALOG_HELPER)
    helper.parent.mkdir(parents=True, exist_ok=True)
    helper.write_text("#!/nonexistent/interpreter\n")
    helper.chmod(0o755)
    r = gcc_world.run(program, [*open_search(), wait_for(type="AdwStatusPage", within=SEARCH), dump("none")])
    assert r.dump("none").within(**SEARCH).one(type="AdwStatusPage")["text"] == "The List Is Not Available"
    warnings = [m["message"] for m in r.warnings()]
    assert len(warnings) == 2 and all(w.startswith(f"Could not start {helper}: ") for w in warnings)


def test_a_too_large_answer_is_not_used(program, gcc_world, phone):
    phone.firefox()
    big = answer(entry("a", "A" * 10, "https://a.example/", keywords=["x" * 1024] * 4200))
    phone.catalog(big, big)
    r = gcc_world.run(program, [*open_search(), wait_for(type="AdwStatusPage", within=SEARCH), dump("none")])
    assert r.dump("none").within(**SEARCH).one(type="AdwStatusPage")["text"] == "The List Is Not Available"
    assert [m["message"] for m in r.warnings()] == ["The catalog of web apps is too large"] * 2


def test_leaving_the_search_while_the_catalog_loads(program, gcc_world, phone):
    # The run with the server still going (the spinner of the first run would keep the test's
    # settling busy, so the slow one is the second): leaving ends it, its answer is never used
    phone.firefox()
    phone.catalog(answer(*SHOPS), {"out": answer(entry("late", "Late", "https://late.example/"), updated=9),
                                   "sleep": 20})
    start = time.monotonic()
    r = gcc_world.run(program, [*open_search(), wait_for(type="AdwActionRow", text="Shopping"), back(),
                                wait(500), dump("list")])
    # The helper did not hold up closing the window either
    assert time.monotonic() - start < 15
    assert phone.catalog_calls() == [["--offline"], []]
    assert not r.dump("list").has(**SEARCH)
    assert not r.warnings()


def test_back_returns_page_by_page(program, gcc_world, phone):
    phone.firefox()
    phone.catalog(answer(*SHOPS), answer(*SHOPS))
    r = gcc_world.run(program, [*open_search(), wait_for(type="AdwActionRow", text="Shopping"),
                                set_text("bahn", **SEARCH_ENTRY), wait(500),
                                click(type="GtkButton", text="Manually", within=SEARCH), editor_open(), back(),
                                wait_for(**SEARCH), dump("typed"), set_text("", **SEARCH_ENTRY), wait(500),
                                *open_category("Shopping"), click(type="AdwActionRow", text="Amazon", within=CATEGORY),
                                editor_open(), back(), wait_for(**CATEGORY), dump("group"), back(), wait_for(**SEARCH),
                                back(), wait_for(gone=True, **SEARCH), dump("list")])
    # What was typed is still there; each back goes one page
    assert names(r.dump("typed")) == ["Deutsche Bahn"]
    assert names(r.dump("group"), CATEGORY) == ["Amazon", "Kleinanzeigen"]
    assert not r.dump("list").has(**SEARCH)
    assert phone.ids() == []
