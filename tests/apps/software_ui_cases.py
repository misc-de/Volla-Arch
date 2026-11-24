# The window of plinius-software with real GTK 4 widgets. Not collected by the test run itself:
# test_apps_software_ui.py runs this file in a pytest of its own on a private broadway display
# (see there), with HOME, the XDG directories and GSettings in its tmp_path.
#
# What is faked: the catalog (load_catalog) and the update check (find_updates) the window starts
# in threads return what a case sets; pkexec, flatpak and systemd-run are fake_bin stubs (pkexec
# prints the case's output and exits with its code). Everything else is real: the lists and
# their filters, the pages pushed, the buttons clicked, the plinius-pkg output read line by line
# from the stub, GSettings (memory backend), launchers started through GIO.

import os
import types

import pytest

if os.environ.get("GDK_BACKEND") != "broadway" or "PLINIUS_UI_LAUNCHED" not in os.environ:
    pytest.skip("runs inside test_apps_software_ui.py's broadway display", allow_module_level=True)

import shutil  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402

from conftest import PKG, load_script  # noqa: E402

SCRIPT = PKG / "plinius-software" / "plinius-software"
LAUNCHED = os.environ["PLINIUS_UI_LAUNCHED"]
# Found before a case's PATH has only the stubs
COMPILE_SCHEMAS = shutil.which("glib-compile-schemas")


@pytest.fixture(scope="module")
def mod():
    m = load_script(SCRIPT, "script_plinius_software_ui")
    m.Adw.init()
    GTK_WINDOW.append(m.Gtk.Window)
    return m


@pytest.fixture(scope="module")
def Gtk(mod):
    return mod.Gtk


def spin(mod, until, timeout=10):
    """Run the main context until until() is true."""
    ctx = mod.GLib.MainContext.default()
    end = time.monotonic() + timeout
    while not until():
        assert time.monotonic() < end, "timed out"
        if not ctx.iteration(False):
            time.sleep(0.002)


def wait(mod, seconds):
    """Run the main context for a while (for what has a delay of its own)."""
    end = time.monotonic() + seconds
    spin(mod, lambda: time.monotonic() >= end)


def png_bytes():
    """A 1x1 PNG."""
    import struct
    import zlib

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\0\xff\0\0\xff")) + chunk(b"IEND", b""))


def descendants(widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        yield from descendants(child)
        child = child.get_next_sibling()


def find(widget, cls):
    return [w for w in descendants(widget) if isinstance(w, cls)]


def visible(widget):
    """Visible and with every parent up to the window visible (the window itself is only
    visible once presented)."""
    while widget is not None and not isinstance(widget, GTK_WINDOW[0]):
        if not widget.get_visible():
            return False
        widget = widget.get_parent()
    return True


GTK_WINDOW = []


def button_labels(mod, widget):
    return [b.get_label() for b in find(widget, mod.Gtk.Button) if b.get_label() and visible(b)]


def button(mod, widget, label):
    found = [b for b in find(widget, mod.Gtk.Button) if b.get_label() == label and visible(b)]
    assert len(found) == 1, (label, button_labels(mod, widget))
    return found[0]


def label_texts(mod, widget):
    return [w.get_label() for w in find(widget, mod.Gtk.Label) if visible(w) and w.get_label()]


def items(model):
    return [model.get_item(i) for i in range(model.get_n_items())]


def components(mod, *bodies):
    """AppStream components from catalog XML <component> bodies."""
    AS = mod.AppStream
    xml = ('<?xml version="1.0"?><components version="1.0" origin="test">'
           + "".join(f'<component type="desktop-application">{b}</component>' for b in bodies)
           + "</components>")
    md = AS.Metadata()
    md.set_format_style(AS.FormatStyle.CATALOG)
    md.parse_data(xml, -1, AS.FormatKind.XML)
    box = md.get_components()
    return [box.index_safe(i) for i in range(box.get_size())]


def make_app(mod, app_id, name, rating="phone", installed=False, categories=(), pkg=None,
             source="pacman", removal=None, extra="", version="1.0-1", recommended=False):
    cats = "".join(f"<category>{c}</category>" for c in categories)
    cpt, = components(mod, f"<id>{app_id}</id><name>{name}</name><summary>About {name}</summary>"
                           f"<categories>{cats}</categories>{extra}")
    pkg = pkg or (f"app/{app_id}/aarch64/stable" if source == "flatpak" else name.lower())
    app = mod.App(cpt, source, pkg, version, (rating, ""), installed)
    app.removal = removal
    app.recommended = recommended
    return app


@pytest.fixture
def ui(mod, tmp_path, monkeypatch, fake_bin):
    """A window factory over a faked catalog and update check, plinius-pkg behind a pkexec stub."""
    monkeypatch.setattr(mod, "_", lambda s: s)
    monkeypatch.setattr(mod, "ngettext", lambda s, p, n: s if n == 1 else p)
    for name in ("SETTINGS", "LAST_CHECK", "HELD_SINCE", "RESTART_RESULT"):
        monkeypatch.setattr(mod, name, str(tmp_path / name.lower()))
    state = types.SimpleNamespace(apps=[], updates=(2, [], [], None), catalogs=0, checks=0, windows=[],
                                  flathub=tmp_path / "flathub-on", pkexec=tmp_path / "pkexec")

    def load_catalog():
        state.catalogs += 1
        if isinstance(state.apps, Exception):
            raise state.apps
        return list(state.apps)

    def find_updates():
        state.checks += 1
        return state.updates
    monkeypatch.setattr(mod, "load_catalog", load_catalog)
    monkeypatch.setattr(mod, "find_updates", find_updates)
    state.pkexec.mkdir()
    fake_bin.stub("flatpak", f'[ "$1" = remotes ] && [ -e "{state.flathub}" ] && echo flathub; exit 0')
    fake_bin.stub("pkexec", f'cat "{state.pkexec}/out" 2>/dev/null\n'
                            f'while [ -e "{state.pkexec}/hold" ]; do sleep 0.02; done\n'
                            f'exit "$(cat "{state.pkexec}/code" 2>/dev/null || echo 0)"')
    fake_bin.stub("systemd-run")
    monkeypatch.setenv("PATH", fake_bin.env(isolated=True)["PATH"])

    application = mod.Application()
    application.set_flags(mod.Gio.ApplicationFlags.NON_UNIQUE)
    assert application.register(None)
    state.application = application

    def window():
        win = mod.Window(application)
        state.windows.append(win)
        n = len(state.apps) if isinstance(state.apps, list) else 0
        spin(mod, lambda: win.check_button.get_sensitive()
             and (win.store.get_n_items() == n if n else
                  win.explore_stack.get_visible_child_name() == "nocatalog"))
        return win

    def helper(code, out=b""):
        """What the next plinius-pkg run prints and its exit code."""
        (state.pkexec / "out").write_bytes(out if isinstance(out, bytes) else out.encode())
        (state.pkexec / "code").write_text(str(code))

    def run(win, start):
        """Start an operation, wait until it is over: the page it pushed."""
        catalogs = state.catalogs
        start()
        page = win.nav.get_visible_page()
        spin(mod, lambda: not win.busy)
        if page.get_can_pop() and win.nav.get_visible_page() is page:
            spin(mod, lambda: state.catalogs > catalogs)  # the catalog is reloaded afterwards
        return page

    state.window, state.helper, state.run = window, helper, run
    state.pkexec_calls = lambda: fake_bin.calls("pkexec")
    state.fake_bin = fake_bin
    yield state
    for win in application.get_windows():
        win.destroy()
    spin(mod, lambda: not application.get_windows())


def launched():
    try:
        with open(LAUNCHED) as f:
            return f.read().splitlines()
    except OSError:
        return []


# --- Explore ------------------------------------------------------------------------------------

def catalog_apps(mod):
    return [
        make_app(mod, "org.a.Chat", "Chat", categories=["Network"], recommended=True),
        make_app(mod, "org.a.Game", "Game", rating="scaled", categories=["Game"]),
        make_app(mod, "org.a.Office", "Office", rating="desktop", categories=["Office"]),
        make_app(mod, "org.a.Tool", "Tool", rating="unknown", categories=["Utility"]),
        make_app(mod, "org.a.Mail", "Mail", installed=True, categories=["Network"], removal=["mail"]),
    ]


def category_rows(win):
    return {row.get_title(): (row.get_subtitle() if row.get_visible() else None) for row, _c in win.category_rows}


def test_explore_counts_the_offered_apps_per_group(mod, ui):
    ui.apps = catalog_apps(mod)
    win = ui.window()
    assert win.explore_stack.get_visible_child_name() == "categories"
    # Phone apps and those with limitations; installed ones are not offered; empty groups hidden
    assert category_rows(win) == {
        "Recommended": "1 app", "Communication & Internet": "1 app", "Music & Video": None,
        "Office & Documents": None, "Photos & Graphics": None, "Learning & Science": None,
        "Games": "1 app", "Utilities": None, "System": None, "All Apps": "2 apps"}
    # The menu adds apps for large screens; the choice is stored
    win.lookup_action("show-desktop").change_state(mod.GLib.Variant("b", True))
    assert category_rows(win)["Office & Documents"] == "1 app"
    assert category_rows(win)["All Apps"] == "3 apps"
    assert mod.load_shown() == {"phone", "scaled", "desktop"}
    assert win.lookup_action("show-desktop").get_state().get_boolean()
    win.lookup_action("show-unknown").change_state(mod.GLib.Variant("b", True))
    win.lookup_action("show-desktop").change_state(mod.GLib.Variant("b", False))
    assert category_rows(win)["Utilities"] == "1 app" and category_rows(win)["Office & Documents"] is None
    assert mod.load_shown() == {"phone", "scaled", "unknown"}
    # A new window starts with the stored choice
    assert ui.window().lookup_action("show-unknown").get_state().get_boolean()


def test_explore_search(mod, ui):
    ui.apps = catalog_apps(mod)
    win = ui.window()
    toggle, = [b for b in find(win, mod.Gtk.ToggleButton) if b.get_icon_name() == "system-search-symbolic"]
    win.stack.set_visible_child_name("updates")
    toggle.set_active(True)
    # Searching always happens on "Explore"
    assert win.stack.get_visible_child_name() == "explore" and win.search_bar.get_search_mode()
    win.search.set_text("about GAME")
    spin(mod, lambda: win.explore_stack.get_visible_child_name() == "list")
    assert [a.name for a in items(win.explore_model)] == ["Game"]
    win.search.set_text("mail")  # installed: not offered
    spin(mod, lambda: win.explore_stack.get_visible_child_name() == "empty")
    toggle.set_active(False)
    assert win.search.get_text() == "" and win.explore_stack.get_visible_child_name() == "categories"
    # A search mode without words shows the groups
    toggle.set_active(True)
    assert win.explore_stack.get_visible_child_name() == "categories"


def test_explore_without_a_catalog(mod, ui, capfd):
    win = ui.window()
    assert win.explore_stack.get_visible_child_name() == "nocatalog"
    # Searching does not leave the notice
    win.search_bar.set_search_mode(True)
    win.search.set_text("x")
    wait(mod, 0.4)  # longer than the entry's search delay
    assert win.explore_stack.get_visible_child_name() == "nocatalog"
    # A catalog that cannot be read is the same notice, not an endless spinner
    ui.apps = RuntimeError("broken catalog")
    win = ui.window()
    assert win.explore_stack.get_visible_child_name() == "nocatalog"
    assert "RuntimeError: broken catalog" in capfd.readouterr().err


def test_category_page_and_details(mod, ui):
    ui.apps = catalog_apps(mod)
    win = ui.window()
    row, = [r for r, c in win.category_rows if r.get_title() == "Communication & Internet"]
    row.emit("activated")
    page = win.nav.get_visible_page()
    assert isinstance(page, mod.CategoryPage) and page.get_title() == "Communication & Internet"
    view, = find(page, mod.Gtk.ListView)
    assert [a.name for a in items(view.get_model())] == ["Chat"]
    view.emit("activate", 0)
    detail = win.nav.get_visible_page()
    assert isinstance(detail, mod.DetailPage) and detail.get_title() == "Chat"
    assert button_labels(mod, detail) == ["Install"]


def test_rows_are_shown_for_the_apps(mod, ui):
    ui.apps = catalog_apps(mod)
    win = ui.window()
    win.present()
    win.stack.set_visible_child_name("installed")
    installed = win.stack.get_child_by_name("installed")
    spin(mod, lambda: any(r.app for r in find(installed, mod.AppRow)))
    assert [(r.name.get_text(), r.badge.get_text()) for r in find(installed, mod.AppRow) if r.app] == [
        ("Mail", "For phones")]


# --- Installed ----------------------------------------------------------------------------------

def test_installed_lists_what_can_be_removed_and_what_is_in_the_app_grid(mod, ui):
    hidden_dep = make_app(mod, "org.a.Hidden", "Hidden", installed=True,
                          extra="<launchable type='desktop-id'>org.plinius.uitest.Hidden.desktop</launchable>")
    assert hidden_dep.hidden
    removable_hidden = make_app(mod, "org.a.Helper", "Helper", installed=True, removal=["helper"],
                                extra="<launchable type='desktop-id'>org.plinius.uitest.Hidden.desktop</launchable>")
    system = make_app(mod, "org.a.System", "System App", installed=True)
    ui.apps = [hidden_dep, removable_hidden, system, make_app(mod, "org.a.New", "New")]
    win = ui.window()
    stack = win.stack.get_child_by_name("installed")
    assert stack.get_visible_child_name() == "list"
    view, = find(stack, mod.Gtk.ListView)
    assert [a.name for a in items(view.get_model())] == ["Helper", "System App"]
    ui.apps = [make_app(mod, "org.a.New", "New")]
    win = ui.window()
    assert win.stack.get_child_by_name("installed").get_visible_child_name() == "empty"


def test_app_row(mod, ui, tmp_path):
    removed = []
    row = mod.AppRow(on_remove=removed.append)
    app = make_app(mod, "org.a.Fox", "Fox", rating="scaled", installed=True, removal=["fox"])
    row.bind(app)
    assert (row.name.get_text(), row.summary.get_text(), row.badge.get_text()) == ("Fox", "About Fox", "With limitations")
    assert row.badge.has_css_class("warning") and row.remove_button.get_visible()
    row.remove_button.emit("clicked")
    assert removed == [app]
    # Recycled for an app that is part of the system, from Flathub
    flat = make_app(mod, "org.a.Flat", "Flat", rating="desktop", installed=True, source="flatpak")
    row.bind(flat)
    assert row.badge.get_text() == "For large screens · Part of the system · Flathub"
    assert row.badge.has_css_class("error") and not row.badge.has_css_class("warning")
    assert not row.remove_button.get_visible()
    # Explore's rows have no remove button
    plain = mod.AppRow()
    plain.bind(make_app(mod, "org.a.New", "New", rating="unknown"))
    assert plain.remove_button is None and plain.badge.get_text() == "Not specified"
    assert plain.icon.get_icon_name() == "application-x-executable"


def test_icons_theme_first_then_the_catalog(mod, ui, tmp_path, monkeypatch):
    icons = tmp_path / "icons"
    icons.mkdir()
    (icons / "org.plinius.uitest.icon.png").write_bytes(png_bytes())
    (tmp_path / "cat64.png").write_bytes(png_bytes())
    image = mod.Gtk.Image()
    mod.Gtk.IconTheme.get_for_display(image.get_display()).add_search_path(str(icons))
    app = make_app(mod, "org.a.Icon", "Icon", extra="<icon type='stock'>org.plinius.uitest.icon</icon>")
    mod.set_icon(image, app, 64)
    assert image.get_icon_name() == "org.plinius.uitest.icon" and image.icon_key is None
    cached = make_app(mod, "org.a.Cached", "Cached",
                      extra=f"<icon type='cached' width='64' height='64'>{tmp_path}/cat64.png</icon>")
    mod.set_icon(image, cached, 64)
    assert image.get_icon_name() == "application-x-executable"
    spin(mod, lambda: image.get_storage_type() == mod.Gtk.ImageType.PAINTABLE)
    assert image.get_paintable().get_width() == 1
    # A recycled row: the icon arriving late for the previous app is not shown
    gate = __import__("threading").Event()
    real = mod.load_file

    def slow(path):
        gate.wait(5)
        return real(path)
    monkeypatch.setattr(mod, "load_file", slow)
    (tmp_path / "late.png").write_bytes(png_bytes())
    late = make_app(mod, "org.a.Late", "Late",
                    extra=f"<icon type='cached' width='64' height='64'>{tmp_path}/late.png</icon>")
    mod.set_icon(image, late, 64)
    mod.set_icon(image, app, 64)
    shown = []
    mod.ICONS.get(str(tmp_path / "late.png"), slow, shown.append)
    gate.set()
    spin(mod, lambda: shown)
    assert image.get_storage_type() == mod.Gtk.ImageType.ICON_NAME
    assert image.get_icon_name() == "org.plinius.uitest.icon"


# --- Details ------------------------------------------------------------------------------------

class Win:
    """What DetailPage needs of the window: records the actions."""

    def __init__(self):
        self.calls = []

    def install(self, app):
        self.calls.append(("install", app.name))

    def confirm_remove(self, app):
        self.calls.append(("remove", app.name))


def test_detail_page_of_an_app_to_install(mod, ui, monkeypatch):
    shots = {"https://s/1.png": "ok", "https://s/2.png": OSError("gone")}

    def load_url(url):
        if isinstance(shots[url], Exception):
            raise shots[url]
        return mod.Gdk.Texture.new_from_bytes(mod.GLib.Bytes.new(png_bytes()))
    monkeypatch.setattr(mod, "load_url", load_url)
    app = make_app(mod, "org.a.Chat", "Chat", version="2.1-1", extra="""
        <description><p>Talk &amp; listen.</p><ul><li>Groups</li></ul></description>
        <url type="homepage">https://chat.example/?a=1&amp;b=2</url>
        <screenshots><screenshot type="default"><image type="source">https://s/1.png</image></screenshot>
        <screenshot><image type="source">https://s/2.png</image></screenshot></screenshots>""")
    win = ui.window()
    win.present()
    calls = []
    win.install = lambda a: calls.append(("install", a.name))
    page = mod.DetailPage(win, app)
    win.nav.push(page)
    assert page.get_title() == "Chat"
    texts = label_texts(mod, page)
    assert texts[:2] == ["Chat", "About Chat"]
    assert DESCRIPTION in texts
    button(mod, page, "Install").emit("clicked")
    assert calls == [("install", "Chat")]
    rows = {r.get_title(): r.get_subtitle() for r in find(page, mod.Adw.ActionRow)}
    assert rows == {"For phones": mod.NOTES["phone"], "Package": "chat 2.1-1",
                    "Website": "https://chat.example/?a=1&b=2"}
    # Screenshots: loaded one after another; one is not available
    stacks = find(page, mod.Gtk.Stack)
    spin(mod, lambda: all(s.get_visible_child_name() != "loading" for s in stacks))
    assert [s.get_visible_child_name() for s in stacks] == ["picture", "failed"]
    assert stacks[0].get_child_by_name("picture").get_paintable().get_width() == 1
    assert len(find(page, mod.Adw.CarouselIndicatorDots)) == 1
    # The website opens in the browser
    website, = [r for r in find(page, mod.Adw.ActionRow) if r.get_title() == "Website"]
    website.emit("activated")
    spin(mod, lambda: any(line.startswith("browser ") for line in launched()))
    assert "browser https://chat.example/?a=1&b=2" in launched()


DESCRIPTION = "Talk & listen.\n • Groups"


def test_detail_page_of_installed_apps(mod, ui):
    win = Win()
    # Installed, with a launcher, removable
    app = make_app(mod, "org.a.Open", "Opener", installed=True, removal=["opener"],
                   extra="<launchable type='desktop-id'>org.plinius.uitest.Open.desktop</launchable>"
                         "<screenshots><screenshot><image type='source'>http://s/one.png</image></screenshot></screenshots>")
    page = mod.DetailPage(win, app)
    assert button_labels(mod, page) == ["Open", "Remove"]
    assert find(page, mod.Adw.CarouselIndicatorDots) == []  # a single screenshot
    button(mod, page, "Remove").emit("clicked")
    assert win.calls == [("remove", "Opener")]
    before = launched()
    button(mod, page, "Open").emit("clicked")
    spin(mod, lambda: len(launched()) > len(before))
    assert launched()[-1] == "open"
    # Part of the system: no Remove, a hint instead; no launcher: no Open
    system = make_app(mod, "org.a.Sys", "Sys", installed=True)
    page = mod.DetailPage(win, system)
    assert button_labels(mod, page) == []
    assert "Part of the system (Phosh, phone) and cannot be removed." in label_texts(mod, page)
    # A Flatpak app: "Open" through flatpak run, the source instead of the package
    flat = make_app(mod, "org.a.Flat", "Flat", installed=True, source="flatpak", removal=["org.a.Flat"], version="")
    page = mod.DetailPage(win, flat)
    assert button_labels(mod, page) == ["Open", "Remove"]
    rows = {r.get_title(): r.get_subtitle() for r in find(page, mod.Adw.ActionRow)}
    assert rows["Source"] == "Flathub" and "Package" not in rows
    button(mod, page, "Open").emit("clicked")
    spin(mod, lambda: ui.fake_bin.calls("flatpak")[-1:] == [["run", "org.a.Flat"]])


# --- The progress page --------------------------------------------------------------------------

def test_operation_page_progress_and_result(mod, ui):
    page = mod.OperationPage("Updating the system", [mod.SYNC, mod.DOWNLOAD, mod.INSTALL, mod.HOOKS])
    assert not page.get_can_pop() and page.step.get_text() == "Preparing…"
    page.add_line("warning: something before the first section")
    assert page.step.get_text() == "Preparing…" and page.bar.get_fraction() == 0
    for line in (":: Synchronizing package databases...", "Packages (2) a-1 b-1", ":: Retrieving packages...",
                 " a-1-aarch64 downloading..."):
        page.add_line(line)
    assert page.step.get_text() == "Step 2 of 4: Downloading packages"
    assert page.detail.get_text() == "1 of 2"
    assert page.bar.get_fraction() == pytest.approx((1 + 4 * 0.5) / 10)
    page.add_line(":: Running post-transaction hooks...")
    assert page.step.get_text() == "Step 4 of 4: Finishing up" and page.detail.get_text() == ""
    buf = page.log.get_buffer()
    assert buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False).splitlines()[0] == \
        "warning: something before the first section"
    page.finish(False, "Update failed", "See “Details” for more information")
    assert page.get_can_pop() and page.icon.get_icon_name() == "dialog-warning-symbolic"
    assert page.icon.has_css_class("error") and not page.icon.has_css_class("dim-label")
    assert page.details.get_visible() and not page.bar.get_visible() and not page.detail.get_visible()
    assert (page.status.get_text(), page.step.get_text()) == ("Update failed", "See “Details” for more information")
    pressed = []
    page.add_button("Done", lambda: pressed.append("done"), "suggested-action")
    button(mod, page, "Done").emit("clicked")
    assert pressed == ["done"] and button(mod, page, "Done").has_css_class("suggested-action")
    # Without steps (Flatpak): the bar only pulses, the step text stays
    flat = mod.OperationPage("Installing Flat", None)
    flat.add_line("Installing app/org.a.Flat")
    assert flat.progress is None and flat.step.get_text() == "Preparing…"
    flat.finish(True, "Flat is installed")
    assert flat.icon.get_icon_name() == "object-select-symbolic" and flat.icon.has_css_class("success")
    assert not flat.details.get_visible() and not flat.step.get_visible()


# --- Operations through plinius-pkg -------------------------------------------------------------

INSTALL_OUTPUT = """\
Packages (1) chat-1.0-1
:: Retrieving packages...
 chat-1.0-1-aarch64 downloading...
checking keyring...
:: Processing package changes...
installing chat...
:: Running post-transaction hooks...
(1/1) Arming ConditionNeedsUpdate...
"""


def log_text(page):
    buf = page.log.get_buffer()
    return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)


def test_install_and_open(mod, ui):
    chat = make_app(mod, "org.a.Chat", "Chat", extra="<launchable type='desktop-id'>org.plinius.uitest.Open.desktop</launchable>")
    ui.apps = [chat]
    win = ui.window()
    settings = mod.Gio.Settings.new("mobi.phosh.shell")
    settings.set_strv("force-adaptive", ["org.other.desktop"])
    ui.helper(0, INSTALL_OUTPUT)
    page = ui.run(win, lambda: win.install(chat))
    assert ui.pkexec_calls() == [[mod.HELPER, "install", "chat"]]
    assert page.get_title() == "Installing Chat" and page.status.get_text() == "Chat is installed"
    assert page.progress.key() == "hooks" and page.progress.count() == (1, 1)
    assert log_text(page) == INSTALL_OUTPUT
    # Rated for phones: added to Phosh's mobile filter, once
    assert settings.get_strv("force-adaptive") == ["org.other.desktop", "org.plinius.uitest.Open.desktop"]
    # Back leads to the overview
    assert [p.get_tag() for p in items(win.nav.get_navigation_stack())] == ["main", None]
    assert button_labels(mod, page) == ["Open", "Done"]
    before = launched()
    button(mod, page, "Open").emit("clicked")
    spin(mod, lambda: len(launched()) > len(before))
    assert launched()[-1] == "open"
    button(mod, page, "Done").emit("clicked")
    assert win.nav.get_visible_page() is win.main_page
    ui.run(win, lambda: win.install(chat))
    assert settings.get_strv("force-adaptive") == ["org.other.desktop", "org.plinius.uitest.Open.desktop"]


def test_install_failures(mod, ui):
    chat = make_app(mod, "org.a.Chat", "Chat")
    ui.apps = [chat]
    win = ui.window()
    expect = {
        mod.HELD: ("Waiting for approval", "Chat needs packages that are still being tested with the Plinius "
                   "adaptations. It can be installed once they are approved.", ["Done"]),
        mod.BUSY: ("Another operation is running", "Wait until it has finished and try again.", ["Done"]),
        mod.STALE_DB: ("Update the system first", "The app belongs to a newer state than the installed system.",
                       ["Go to Updates", "Done"]),
        1: ("Installation failed", "See “Details” for more information", ["Done"]),
    }
    for code, (title, text, buttons) in expect.items():
        ui.helper(code, "error: failed to commit transaction\n")
        page = ui.run(win, lambda: win.install(chat))
        assert (page.status.get_text(), page.step.get_text()) == (title, text), code
        assert button_labels(mod, page) == buttons and page.details.get_visible()
        assert "error: failed to commit transaction" in log_text(page)
    # "Go to Updates" leads there
    win.stack.set_visible_child_name("explore")
    ui.helper(mod.STALE_FILE)
    page = ui.run(win, lambda: win.install(chat))
    button(mod, page, "Go to Updates").emit("clicked")
    assert win.nav.get_visible_page() is win.main_page and win.stack.get_visible_child_name() == "updates"


def test_install_cancelled_at_the_password_or_not_started(mod, ui):
    chat = make_app(mod, "org.a.Chat", "Chat", rating="desktop",
                    extra="<launchable type='desktop-id'>org.plinius.uitest.Desktop.desktop</launchable>")
    ui.apps = [chat]
    win = ui.window()
    for code in mod.PKEXEC_CANCELLED:
        ui.helper(code)
        catalogs = ui.catalogs
        win.install(chat)
        spin(mod, lambda: not win.busy)
        # The page goes away again, nothing is reloaded
        assert win.nav.get_visible_page() is win.main_page and ui.catalogs == catalogs
    # Rated for large screens: not added to Phosh's mobile filter
    settings = mod.Gio.Settings.new("mobi.phosh.shell")
    settings.reset("force-adaptive")
    ui.helper(0)
    ui.run(win, lambda: win.install(chat))
    assert settings.get_strv("force-adaptive") == []
    # Without pkexec
    os.remove(ui.fake_bin.path / "pkexec")
    win.install(chat)
    page = win.nav.get_visible_page()
    assert not win.busy and page.status.get_text() == "Could not start"
    assert "pkexec" in page.step.get_text()
    button(mod, page, "Done").emit("clicked")
    assert win.nav.get_visible_page() is win.main_page


def test_output_that_is_not_utf8(mod, ui):
    chat = make_app(mod, "org.a.Chat", "Chat")
    ui.apps = [chat]
    win = ui.window()
    ui.helper(1, b"first\n\xff\xfe broken\nlast\n")
    page = ui.run(win, lambda: win.install(chat))
    assert log_text(page).splitlines() == ["first", "�", "last"]


def test_install_an_app_without_a_launcher(mod, ui):
    tool = make_app(mod, "org.a.Tool", "Tool")
    ui.apps = [tool]
    win = ui.window()
    ui.helper(0)
    page = ui.run(win, lambda: win.install(tool))
    assert page.status.get_text() == "Tool is installed" and button_labels(mod, page) == ["Done"]


def test_install_a_flatpak(mod, ui):
    flat = make_app(mod, "org.a.Flat", "Flat", source="flatpak")
    ui.apps = [flat]
    win = ui.window()
    ui.helper(0, "Installing app/org.a.Flat/aarch64/stable\n")
    page = ui.run(win, lambda: win.install(flat))
    assert ui.pkexec_calls() == [[mod.HELPER, "flatpak-install", "app/org.a.Flat/aarch64/stable"]]
    assert page.progress is None and page.status.get_text() == "Flat is installed"
    assert button_labels(mod, page) == ["Open", "Done"]


def test_remove_after_asking(mod, ui):
    fox = make_app(mod, "org.a.Fox", "Firefox", installed=True, removal=["firefox", "firefox-i18n-de"])
    ui.apps = [fox]
    win = ui.window()
    win.present()
    win.confirm_remove(fox)
    dialog = win.get_visible_dialog()
    assert dialog.get_heading() == "Remove Firefox?"
    assert dialog.get_response_appearance("remove") == mod.Adw.ResponseAppearance.DESTRUCTIVE
    dialog.emit("response", "cancel")
    dialog.force_close()
    assert ui.pkexec_calls() == []
    win.confirm_remove(fox)
    dialog = win.get_visible_dialog()
    ui.helper(0, ":: Processing package changes...\nremoving firefox-i18n-de...\n")
    page = ui.run(win, lambda: dialog.emit("response", "remove"))
    dialog.force_close()
    assert ui.pkexec_calls() == [[mod.HELPER, "remove", "firefox", "firefox-i18n-de"]]
    assert page.get_title() == "Removing Firefox" and page.status.get_text() == "Firefox has been removed"
    assert [s[0] for s in page.progress.steps] == ["install", "hooks"]
    for code, title in ((1, "Removal failed"), (mod.BUSY, "Another operation is running")):
        ui.helper(code)
        assert ui.run(win, lambda: win.remove(fox)).status.get_text() == title
    flat = make_app(mod, "org.a.Flat", "Flat", installed=True, source="flatpak", removal=["org.a.Flat"])
    ui.helper(0)
    page = ui.run(win, lambda: win.remove(flat))
    assert ui.pkexec_calls()[-1] == [mod.HELPER, "flatpak-remove", "org.a.Flat"] and page.progress is None


# --- Updates ------------------------------------------------------------------------------------

def test_updates_tab_and_upgrade(mod, ui):
    ui.updates = (0, [("glibc", "2.43-1", "2.44-1")], [("org.a.Flat", "3.0", "flathub")], None)
    win = ui.window()
    rows = [(r.get_title(), r.get_subtitle()) for r in win.update_rows]
    assert rows == [("glibc", "2.43-1 → 2.44-1"), ("org.a.Flat", "Flathub 3.0")]
    assert win.update_status.title.get_label() == "2 updates available"
    assert win.updates_page.get_badge_number() == 2 and win.updates_page.get_needs_attention()
    assert win.upgrade_button.get_visible() and win.update_list.get_visible()
    # Check again: the list is replaced
    ui.updates = (1, [], [], None)
    checks = ui.checks
    win.check_button.emit("clicked")
    assert not win.check_button.get_sensitive()
    assert win.update_status.title.get_label() == "Checking for updates…"
    spin(mod, lambda: ui.checks > checks and win.check_button.get_sensitive())
    assert win.update_rows == [] and win.update_status.title.get_label() == "Check failed"
    assert win.upgrade_button.get_visible()  # the upgrade fetches the databases itself
    # The update offers the restart it needs
    with open(mod.RESTART_RESULT, "w") as f:
        f.write('{"level": "ui", "apps": []}')
    ui.updates = (2, [], [], None)
    ui.helper(0, ":: Synchronizing package databases...\n")
    page = ui.run(win, lambda: win.upgrade_button.emit("clicked"))
    assert ui.pkexec_calls() == [[mod.HELPER, "upgrade"]]
    assert page.get_title() == "Updating the system" and page.status.get_text() == "The system has been updated"
    assert [s[0] for s in page.progress.steps] == ["sync", "download", "check", "install", "hooks"]
    # Offered instead of "Done"
    assert button_labels(mod, page) == ["Restart Phosh"]
    button(mod, page, "Restart Phosh").emit("clicked")
    spin(mod, lambda: ui.fake_bin.calls("systemd-run"))
    assert ui.fake_bin.calls("systemd-run") == [["--user", "--scope", "--collect", "--quiet", "--", mod.RESTART,
                                                 "apply", "ui", mod.RESTART_RESULT]]
    # The Updates tab is checked again
    spin(mod, lambda: win.update_status.title.get_label() == "The system is up to date")


def test_upgrade_results(mod, ui):
    win = ui.window()
    expect = {
        mod.NOTHING_TO_DO: ("The system is already up to date", "", ["Done"]),
        mod.ONLY_FLATPAK: ("The Flatpak apps have been updated", "Restart the phone to run the new versions.",
                           ["Restart Phone"]),
        mod.LOW_BATTERY: ("Battery too low", "Connect the charger or charge the phone to at least 10 %, then "
                          "update. An update interrupted by an empty battery can leave the system unable to start.",
                          ["Done"]),
        mod.STALE_DB: ("Update the system first", "The app belongs to a newer state than the installed system.",
                       ["Go to Updates", "Done"]),
    }
    for code, (title, text, buttons) in expect.items():
        ui.helper(code)
        page = ui.run(win, win.upgrade)
        assert (page.status.get_text(), page.step.get_text(), button_labels(mod, page)) == (title, text, buttons)


def test_upgrade_with_flathub_also_updates_flatpaks(mod, ui):
    ui.flathub.touch()
    win = ui.window()
    assert win.flathub.get_state().get_boolean()
    ui.helper(mod.NOTHING_TO_DO)
    page = ui.run(win, win.upgrade)
    assert page.progress.steps[-1] == mod.FLATPAK_UPDATE


def test_window_stays_open_while_an_operation_runs(mod, ui):
    chat = make_app(mod, "org.a.Chat", "Chat")
    ui.apps = [chat]
    win = ui.window()
    win.present()
    hold = ui.pkexec / "hold"
    hold.touch()
    ui.helper(0, "Packages (1) chat\n")
    win.install(chat)
    page = win.nav.get_visible_page()
    spin(mod, lambda: "Packages (1)" in log_text(page))
    assert win.busy and not page.get_can_pop()
    assert win.emit("close-request") is True
    toast_label = [w for w in find(win.toasts, mod.Gtk.Label)
                   if w.get_label() == "Please wait until the operation has finished"]
    assert toast_label
    hold.unlink()
    spin(mod, lambda: not win.busy)
    assert win.emit("close-request") is False


# --- Flathub ------------------------------------------------------------------------------------

def test_turn_on_flathub_after_asking(mod, ui):
    win = ui.window()
    win.present()
    action = win.lookup_action("flathub")
    assert not action.get_state().get_boolean()
    action.change_state(mod.GLib.Variant("b", True))
    dialog = win.get_visible_dialog()
    assert dialog.get_heading() == "Turn On Flathub?"
    # The check mark only follows plinius-pkg's result
    assert not action.get_state().get_boolean()
    dialog.emit("response", "cancel")
    dialog.force_close()
    assert ui.pkexec_calls() == []
    for code, title, state in ((1, "Could not turn on Flathub", False),
                               (mod.BUSY, "Another operation is running", False),
                               (0, "Flathub is on", True)):
        action.change_state(mod.GLib.Variant("b", True))
        dialog = win.get_visible_dialog()
        ui.helper(code)
        page = ui.run(win, lambda: dialog.emit("response", "enable"))
        dialog.force_close()
        assert ui.pkexec_calls()[-1] == [mod.HELPER, "flathub-enable"]
        assert page.status.get_text() == title and action.get_state().get_boolean() is state
        assert page.progress.steps[-1] == mod.FLATHUB_CATALOG
    assert page.step.get_text().startswith("Apps from Flathub appear under “Explore”")


def test_turn_off_flathub(mod, ui):
    ui.flathub.touch()
    win = ui.window()
    action = win.lookup_action("flathub")
    assert action.get_state().get_boolean()
    for code, title, text, state in (
            (mod.FLATHUB_IN_USE, "Apps from Flathub are still installed", "Remove them under “Installed” first.", True),
            (mod.STALE_FILE, "Update the system first", "The app belongs to a newer state than the installed system.", True),
            (1, "Could not turn off Flathub", "See “Details” for more information", True),
            (0, "Flathub is off", "", False)):
        ui.helper(code)
        # No question: plinius-pkg refuses while Flathub apps are installed
        page = ui.run(win, lambda: action.change_state(mod.GLib.Variant("b", False)))
        assert ui.pkexec_calls()[-1] == [mod.HELPER, "flathub-disable"]
        assert (page.status.get_text(), page.step.get_text()) == (title, text)
        assert action.get_state().get_boolean() is state and page.progress is None


# --- Phosh's mobile filter ----------------------------------------------------------------------

def test_show_in_phosh(mod, ui, tmp_path, monkeypatch):
    settings = mod.Gio.Settings.new("mobi.phosh.shell")
    settings.reset("force-adaptive")
    for desktop_id in ("org.plinius.uitest.Mobile.desktop", "org.plinius.uitest.KdeMobile.desktop"):
        mod.show_in_phosh(make_app(mod, "org.a.M", "M", extra=f"<launchable type='desktop-id'>{desktop_id}</launchable>"))
    # Already marked mobile by their launchers; no launcher at all: nothing to add
    mod.show_in_phosh(make_app(mod, "org.a.N", "N"))
    assert settings.get_strv("force-adaptive") == []
    # A launcher that is not installed (yet) is added all the same
    mod.show_in_phosh(make_app(mod, "org.a.S", "S", rating="scaled",
                               extra="<launchable type='desktop-id'>org.not.installed.desktop</launchable>"))
    assert settings.get_strv("force-adaptive") == ["org.not.installed.desktop"]
    # Phosh before 0.59 has its settings under sm.puri.phosh; without either, nothing happens
    old = tmp_path / "old-schemas"
    old.mkdir()
    (old / "old.gschema.xml").write_text(
        '<schemalist><schema id="sm.puri.phosh" path="/sm/puri/phosh/">'
        '<key name="force-adaptive" type="as"><default>[]</default></key></schema></schemalist>')
    subprocess.run([COMPILE_SCHEMAS, str(old)], check=True)
    empty = tmp_path / "no-schemas"  # no Phosh at all
    empty.mkdir()
    (empty / "other.gschema.xml").write_text(
        '<schemalist><schema id="org.plinius.other" path="/org/plinius/other/">'
        '<key name="x" type="b"><default>false</default></key></schema></schemalist>')
    subprocess.run([COMPILE_SCHEMAS, str(empty)], check=True)
    real_gio = mod.Gio
    real = real_gio.SettingsSchemaSource
    source = {}

    class Gio:
        """The real Gio, with the schema source of the case."""
        SettingsSchemaSource = types.SimpleNamespace(get_default=lambda: source["now"])

        def __getattr__(self, name):
            return getattr(real_gio, name)
    monkeypatch.setattr(mod, "Gio", Gio())
    app = make_app(mod, "org.a.O", "O", extra="<launchable type='desktop-id'>org.plinius.uitest.Desktop.desktop</launchable>")
    source["now"] = real.new_from_directory(str(empty), None, True)
    mod.show_in_phosh(app)
    source["now"] = None
    mod.show_in_phosh(app)
    source["now"] = real.new_from_directory(str(old), None, True)
    mod.show_in_phosh(app)
    old_settings = real_gio.Settings.new_full(source["now"].lookup("sm.puri.phosh", False), None, None)
    assert old_settings.get_strv("force-adaptive") == ["org.plinius.uitest.Desktop.desktop"]
    assert settings.get_strv("force-adaptive") == ["org.not.installed.desktop"]


# --- The application ----------------------------------------------------------------------------

def test_application_opens_one_window_and_the_updates_page(mod, ui):
    application = ui.application
    application.activate()
    win, = application.get_windows()
    ui.windows.append(win)
    spin(mod, lambda: win.check_button.get_sensitive())
    assert win.stack.get_visible_child_name() == "explore"
    application.activate()
    assert application.get_windows() == [win]
    application.activate_action("show-updates", None)
    assert win.stack.get_visible_child_name() == "updates" and application.get_windows() == [win]
    # From the notification without a window
    win.destroy()
    spin(mod, lambda: not application.get_windows())
    application.activate_action("show-updates", None)
    win, = application.get_windows()
    ui.windows.append(win)
    assert win.stack.get_visible_child_name() == "updates"
    spin(mod, lambda: win.check_button.get_sensitive())


def test_run_as_a_program_opens_the_window(mod, ui, monkeypatch):
    """`plinius-software` without arguments: the application runs until it is quit."""
    import runpy
    import sys
    monkeypatch.setattr(mod.AppStream.Pool, "load", lambda self, cancellable: True)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT)])
    seen = []

    def quit_when_shown():
        for win in mod.Gtk.Window.list_toplevels():
            app = win.get_application() if hasattr(win, "check_button") else None
            if app and app.get_application_id() == mod.APP_ID and win.check_button.get_sensitive():
                seen.append((win.get_title(), app is not ui.application))
                app.quit()
                return False
        return True
    mod.GLib.timeout_add(20, quit_when_shown)
    with pytest.raises(SystemExit) as stop:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert stop.value.code == 0 and seen == [("Software", True)]
