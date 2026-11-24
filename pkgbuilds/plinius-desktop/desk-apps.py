#!/usr/bin/python3
"""App menu of the virtual desktop.

Categories on the left: the favorites and folders of Phosh (the same settings as
on the phone) and "Other" for the apps in no folder; the apps of the picked one
on the right, by name as in Phosh's app grid. Typing searches all apps. A second
click on the button or Escape closes it.
"""
import os
import signal
import sys

import cairo
import gi
gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
gi.require_version("GtkLayerShell", "0.1")
from gi.repository import Gdk, Gio, GLib, Gtk, GtkLayerShell  # noqa: E402

import desk_common as common  # noqa: E402

_ = common._

PIDFILE = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "plinius-desk-apps.pid")
FOLDERS_PATH = "/org/gnome/desktop/app-folders/folders/"

CSS = b"""
window.desk-apps { background: transparent; }
.desk-card {
  background-color: rgba(20, 22, 28, 0.96);
  border: 1px solid rgba(255, 255, 255, 0.10);
  border-radius: 16px;
  padding: 14px;
  color: #eeeeee;
}
.desk-search { min-height: 38px; border-radius: 19px; font-size: 15px; }
.desk-categories, .desk-apps-list { background: transparent; }
.desk-categories row { padding: 9px 12px; border-radius: 10px; }
.desk-categories row:selected { background-color: #1793d1; color: #ffffff; }
.desk-apps-list row { padding: 6px 10px; border-radius: 10px; }
.desk-apps-list row:hover, .desk-categories row:hover { background-color: rgba(255, 255, 255, 0.08); }
.desk-apps-list row:selected { background-color: rgba(23, 147, 209, 0.45); }
.desk-app-name { font-size: 14px; }
.desk-app-desc { font-size: 11px; opacity: 0.6; }
.desk-sep { background-color: rgba(255, 255, 255, 0.10); min-width: 1px; }
"""


def toggle_running():
    """Close an open menu instead of opening a second one."""
    try:
        with open(PIDFILE) as f:
            pid = int(f.read().strip())
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            if b"desk-apps" in f.read():
                os.kill(pid, signal.SIGTERM)
                return True
    except (OSError, ValueError):
        pass
    return False


def virtual_monitor(display):
    """The virtual screen is the one without a physical size."""
    for i in range(display.get_n_monitors()):
        monitor = display.get_monitor(i)
        if monitor.get_width_mm() == 0:
            return monitor
    return None


def sort_key(app):
    return GLib.utf8_collate_key(GLib.utf8_casefold(app.get_display_name() or "", -1), -1)


def folder_name(name, translate):
    """A folder's name; with translate it names a .directory file."""
    if translate and name.endswith(".directory"):
        for base in GLib.get_system_data_dirs():
            path = os.path.join(base, "desktop-directories", name)
            keyfile = GLib.KeyFile()
            try:
                keyfile.load_from_file(path, GLib.KeyFileFlags.NONE)
                return keyfile.get_locale_string("Desktop Entry", "Name", None)
            except GLib.Error:
                continue
    return name


def phosh_schema():
    """Phosh's own settings: mobi.phosh.shell since Phosh 0.59, sm.puri.phosh before."""
    source = Gio.SettingsSchemaSource.get_default()
    for schema in ("mobi.phosh.shell", "sm.puri.phosh"):
        if source and source.lookup(schema, True):
            return schema
    return None


def load_categories():
    """[(name, icon, [apps])]: favorites, the folders, everything else."""
    apps = {app.get_id(): app for app in Gio.AppInfo.get_all() if app.should_show()}
    categories = []

    schema = phosh_schema()
    favorites = Gio.Settings.new(schema).get_strv("favorites") if schema else []
    categories.append((_("Favorites"), "starred-symbolic",
                       [apps[i] for i in favorites if i in apps]))

    in_folder = set()
    for folder in Gio.Settings.new("org.gnome.desktop.app-folders").get_strv("folder-children"):
        settings = Gio.Settings.new_with_path("org.gnome.desktop.app-folders.folder",
                                              FOLDERS_PATH + folder + "/")
        members = [apps[i] for i in settings.get_strv("apps") if i in apps]
        wanted = set(settings.get_strv("categories"))
        if wanted:
            for app in apps.values():
                cats = set((app.get_categories() or "").split(";"))
                if cats & wanted and app not in members:
                    members.append(app)
        in_folder.update(app.get_id() for app in members)
        categories.append((folder_name(settings.get_string("name"), settings.get_boolean("translate")),
                           "folder-symbolic", sorted(members, key=sort_key)))

    others = [app for i, app in apps.items() if i not in in_folder]
    categories.append((_("Other"), "view-app-grid-symbolic", sorted(others, key=sort_key)))
    return categories, sorted(apps.values(), key=sort_key)


class Menu(Gtk.Window):
    def __init__(self, close=Gtk.main_quit):
        super().__init__()
        # What ends the menu: the main loop of its own process, or hiding the resident one
        self.close = close
        self.get_style_context().add_class("desk-apps")
        GtkLayerShell.init_for_window(self)
        GtkLayerShell.set_namespace(self, "plinius-desk-apps")
        GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
        # At the bar's left end, on the edge the bar is on
        common.anchor_to_bar(self, GtkLayerShell.Edge.LEFT)
        # The keyboard for the search while the menu is open
        GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.EXCLUSIVE)
        GtkLayerShell.set_monitor(self, virtual_monitor(self.get_display()))

        visual = self.get_screen().get_rgba_visual()
        if visual:
            self.set_visual(visual)
        self.set_app_paintable(True)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        card.get_style_context().add_class("desk-card")
        card.set_size_request(680, 560)
        self.add(card)
        self.card = card

        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text(_("Search apps"))
        self.search.get_style_context().add_class("desk-search")
        self.search.connect("search-changed", self.on_search)
        self.search.connect("activate", self.on_search_activate)
        card.pack_start(self.search, False, False, 0)

        body = Gtk.Box(spacing=10)
        card.pack_start(body, True, True, 0)

        self.category_list = Gtk.ListBox()
        self.category_list.get_style_context().add_class("desk-categories")
        self.category_list.set_size_request(200, -1)
        self.category_list.connect("row-selected", self.on_category)
        body.pack_start(self.category_list, False, False, 0)

        sep = Gtk.Box()
        sep.get_style_context().add_class("desk-sep")
        body.pack_start(sep, False, False, 0)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.app_list = Gtk.ListBox()
        self.app_list.get_style_context().add_class("desk-apps-list")
        self.app_list.set_activate_on_single_click(True)
        self.app_list.connect("row-activated", self.on_app)
        scrolled.add(self.app_list)
        body.pack_start(scrolled, True, True, 0)

        self.connect("key-press-event", self.on_key)
        self.refresh()

    def refresh(self):
        """The apps, favorites and folders as they are now, the search empty."""
        self.categories, self.all_apps = load_categories()
        self.search.set_text("")
        for child in self.category_list.get_children():
            self.category_list.remove(child)
        for name, icon, _apps in self.categories:
            row = Gtk.Box(spacing=10)
            row.pack_start(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.MENU), False, False, 0)
            row.pack_start(Gtk.Label(label=name, xalign=0), True, True, 0)
            self.category_list.add(row)
        # Favorites first, or the first category with apps
        start = next((i for i, c in enumerate(self.categories) if c[2]), 0)
        self.category_list.select_row(self.category_list.get_row_at_index(start))

    def show_apps(self, apps):
        for child in self.app_list.get_children():
            self.app_list.remove(child)
        for app in apps:
            row = Gtk.ListBoxRow()
            row.app = app
            box = Gtk.Box(spacing=12)
            icon = Gtk.Image()
            if app.get_icon():
                icon.set_from_gicon(app.get_icon(), Gtk.IconSize.DND)
            icon.set_pixel_size(32)
            box.pack_start(icon, False, False, 0)
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            name = Gtk.Label(label=app.get_display_name(), xalign=0)
            name.get_style_context().add_class("desk-app-name")
            text.pack_start(name, False, False, 0)
            if app.get_description():
                desc = Gtk.Label(label=app.get_description(), xalign=0)
                desc.set_ellipsize(3)  # Pango.EllipsizeMode.END
                desc.get_style_context().add_class("desk-app-desc")
                text.pack_start(desc, False, False, 0)
            box.pack_start(text, True, True, 0)
            row.add(box)
            self.app_list.add(row)
        self.app_list.show_all()

    def on_category(self, listbox, row):
        if row is None:
            return
        if self.search.get_text():
            self.search.set_text("")
        self.show_apps(self.categories[row.get_index()][2])

    def on_search(self, entry):
        text = GLib.utf8_casefold(entry.get_text().strip(), -1)
        if not text:
            row = self.category_list.get_selected_row()
            self.show_apps(self.categories[row.get_index() if row else 0][2])
            return
        found = []
        for app in self.all_apps:
            haystack = " ".join(filter(None, [app.get_display_name(), app.get_name(),
                                              app.get_description(), app.get_id(),
                                              " ".join(app.get_keywords() or [])]))
            if text in GLib.utf8_casefold(haystack, -1):
                found.append(app)
        self.show_apps(found)

    def on_search_activate(self, entry):
        row = self.app_list.get_row_at_index(0)
        if row:
            self.on_app(self.app_list, row)

    def on_app(self, listbox, row):
        context = Gdk.Display.get_default().get_app_launch_context()
        filename = row.app.get_filename() if hasattr(row.app, "get_filename") else None
        try:
            if filename:
                # Started in a scope of its own (common.start_in_scope) by gio, as GLib starts
                # it; an app started through D-Bus activation goes to a unit of the bus's and
                # leaves the scope empty. The activation token lets the window take the focus.
                env = dict(os.environ)
                token = context.get_startup_notify_id(row.app, [])
                if token:
                    env["XDG_ACTIVATION_TOKEN"] = env["DESKTOP_STARTUP_ID"] = token
                common.start_in_scope(row.app.get_id() or "app", ["gio", "launch", filename], env)
            else:
                row.app.launch([], context)
        except GLib.Error as e:
            print("launch failed:", e.message, flush=True)
        self.close()

    def on_key(self, widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self.close()
            return True
        return False


class Resident:
    """The menu of the whole session, built once and then only shown and hidden: a new process
    per click took 0.7 s to the first picture over VNC, showing the built menu 0.04 s.

    SIGUSR1 (the bar's button, desk-apps-toggle) shows or hides it, SIGUSR2 (another menu
    opens) hides it, SIGTERM (the session ends) ends it.
    """

    def __init__(self, monitor):
        self.monitor = monitor
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.menu = Menu(close=self.hide)
        self.catcher = None
        self.open = False
        self.warming = False
        self.menu.connect("draw", self.on_draw)

    def warm_up(self):
        """Shows both windows once, invisible and not in the way: a window's first appearance
        costs 0.7 s, after this warm-up even the first click shows the menu in 0.08 s."""
        self.warming = True
        self.catcher = common.click_catcher(self.monitor, self.hide)
        for window in (self.catcher, self.menu):
            # Clicks go through to the desktop below
            window.input_shape_combine_region(cairo.Region())
        self.menu.card.set_opacity(0)
        GtkLayerShell.set_keyboard_mode(self.menu, GtkLayerShell.KeyboardMode.NONE)
        self.menu.show_all()
        return False

    def on_draw(self, _widget, _cr):
        if self.warming:
            # Drawn once: everything is loaded, the windows can go again
            GLib.idle_add(self.end_warm_up)
        return False

    def end_warm_up(self, hide=True):
        if not self.warming:
            return False
        self.warming = False
        if hide:
            self.hide()
        for window in (self.catcher, self.menu):
            window.input_shape_combine_region(None)
        self.menu.card.set_opacity(1)
        GtkLayerShell.set_keyboard_mode(self.menu, GtkLayerShell.KeyboardMode.EXCLUSIVE)
        return False

    def toggle(self):
        if self.open:
            self.hide()
        else:
            self.show()
        return True

    def show(self):
        if common.screen_locked(self.bus):
            return
        # A click during the warm-up: the menu is up already, it only has to become visible
        self.end_warm_up(hide=False)
        common.close_other_menus("plinius-desk-apps.pid")
        self.menu.refresh()
        # The catcher first, so the menu stays above it
        if self.catcher is None:
            self.catcher = common.click_catcher(self.monitor, self.hide)
        else:
            self.catcher.show_all()
        self.menu.show_all()
        self.menu.search.grab_focus()
        self.open = True

    def hide(self):
        self.menu.hide()
        if self.catcher is not None:
            self.catcher.hide()
        self.open = False
        return True


def serve():
    """desk-apps.py --resident: started with the bar, waits hidden for its button."""
    monitor = virtual_monitor(Gdk.Display.get_default())
    if monitor is None:
        return
    setup_style()
    resident = Resident(monitor)
    # Hidden right away when the screen locks; it opens only unlocked anyway
    common.close_when_locked(resident.hide)
    path = os.path.join(os.path.dirname(PIDFILE), common.RESIDENT_APPS_PIDFILE)
    with open(path, "w") as f:
        f.write(str(os.getpid()))
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1, resident.toggle)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR2, resident.hide)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, Gtk.main_quit)
    GLib.idle_add(resident.warm_up)
    Gtk.main()
    try:
        os.unlink(path)
    except OSError:
        pass


def setup_style():
    Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", True)
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider,
                                             Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def main(argv=()):
    if "--resident" in argv:
        return serve()
    if toggle_running():
        return
    if not common.close_when_locked(Gtk.main_quit):
        return
    # Only on the virtual screen, never on the phone (e.g. VNC just ended)
    monitor = virtual_monitor(Gdk.Display.get_default())
    if monitor is None:
        return
    common.close_other_menus("plinius-desk-apps.pid")
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))
    setup_style()
    # A click next to the menu closes it
    common.click_catcher(monitor, Gtk.main_quit)
    menu = Menu()
    menu.connect("destroy", Gtk.main_quit)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, Gtk.main_quit)
    menu.show_all()
    menu.search.grab_focus()
    Gtk.main()
    try:
        os.unlink(PIDFILE)
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
