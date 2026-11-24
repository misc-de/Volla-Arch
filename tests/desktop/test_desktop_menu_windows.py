# The windows of plinius-desktop's bar menus, built against a recording GTK 3 stand-in
# (desk_fake_gtk3): what the settings and app menus show, what a click or a key does, how their
# main() opens and closes them, the details card (info_popup) and the click catcher of
# desk_common, and the Wi-Fi and battery cards' entry points. Settings and sound go to tmp_path
# and stub programs; nothing opens a window.

import os
import subprocess
import sys
import types

import pytest
from gi.repository import Gio, GLib

import desktop_fakes as fakes
from conftest import PKG, load_script
from desk_fake_gtk3 import Button, ComboBoxText, Grid, ToggleButton, Toolkit, spawn_marked

DESK = PKG / "plinius-desktop"


@pytest.fixture
def tk(monkeypatch):
    """A fake toolkit, in gi.repository for the whole test (desk_common imports while it runs)."""
    toolkit = Toolkit()
    toolkit.signals = fakes.recorder()
    toolkit.install(monkeypatch, GLib=fakes.Proxy(GLib, unix_signal_add=toolkit.signals))
    return toolkit


@pytest.fixture
def common(tmp_path, monkeypatch, fake_bin, tk):
    """A fresh desk_common (the menus import it by that name) with its files in tmp_path and
    only stub programs in PATH."""
    monkeypatch.syspath_prepend(str(DESK))
    # load_script registers the module: the one before it comes back after the test
    monkeypatch.setitem(sys.modules, "desk_common", sys.modules.get("desk_common"))
    mod = load_script(DESK / "desk_common.py", "desk_common")
    runtime = tmp_path / "run"
    (runtime / "plinius-desktop").mkdir(parents=True)
    conf = tmp_path / "config" / "plinius-desktop"
    monkeypatch.setattr(mod, "DIR", str(conf))
    monkeypatch.setattr(mod, "SETTINGS", str(conf / "settings.ini"))
    monkeypatch.setattr(mod, "RUNTIME_DIR", str(runtime / "plinius-desktop"))
    monkeypatch.setattr(mod, "CTL_SOCKET", str(runtime / "plinius-desktop" / "wayvnc.sock"))
    monkeypatch.setattr(mod, "OUTPUT_FILE", str(runtime / "plinius-desktop" / "output"))
    monkeypatch.setattr(mod, "system_keyboard", lambda: "us")
    monkeypatch.setattr(mod, "bar_position", lambda: "top")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("PATH", fake_bin.env(isolated=True)["PATH"])
    mod.runtime = runtime
    mod.fake_bin = fake_bin
    mod.locked = False
    mod.closed_others = []
    monkeypatch.setattr(mod, "close_when_locked", lambda quit: not mod.locked)
    real_close = mod.close_other_menus
    monkeypatch.setattr(mod, "close_other_menus",
                        lambda own: mod.closed_others.append(own) or real_close(own))
    return mod


def by_namespace(tk, name):
    """The window whose layer-shell namespace is name."""
    found = [w for w in tk.windows if tk.placement(w).get("set_namespace") == [(name,)]]
    assert len(found) == 1, name
    return found[0]


def combos(window):
    return window.find(ComboBoxText)


# ===== desk-settings.py =====

@pytest.fixture
def settings_mod(common, tk):
    mod = load_script(DESK / "desk-settings.py", "desk_settings_window_under_test")
    assert mod.Gtk is tk.gtk and mod.common is common
    return mod


def test_settings_menu_shows_the_stored_settings(settings_mod, common, tk):
    common.save({"resolution": "2560x1440", "scale": "1.25", "sound": "desktop",
                 "keyboard": "de-nodeadkeys"})
    menu = settings_mod.Menu()
    resolution, scale, keyboard = combos(menu)
    assert resolution.items == common.RESOLUTIONS and resolution.get_active_id() == "2560x1440"
    assert scale.items == common.SCALES and scale.get_active_id() == "1.25"
    # The phone's own layout is not in the list: it is added so it stays selected
    assert keyboard.items == common.LAYOUTS + [("de-nodeadkeys", "de-nodeadkeys")]
    assert keyboard.get_active_id() == "de-nodeadkeys"
    assert menu.sound_desktop.get_active() and not menu.sound_phone.get_active()
    assert [w.get_label() for w in menu.find(ToggleButton)] == ["Phone", "Desktop"]
    assert menu.texts()[:5] == ["Desktop", "Resolution", "Scale", "Sound", "Keyboard"]
    # The reconnect note waits for a keyboard change
    assert menu.keyboard_note.called("set_no_show_all") == [(True,)]
    assert "desk-settings" in menu.classes


def test_settings_menu_sits_at_the_bars_right_end_on_the_virtual_screen(settings_mod, common, tk,
                                                                        monkeypatch):
    monkeypatch.setattr(common, "bar_position", lambda: "bottom")
    menu = settings_mod.Menu()
    place = tk.placement(menu)
    assert place["set_namespace"] == [("plinius-desk-settings",)]
    assert place["set_layer"] == [("overlay",)]
    assert place["set_anchor"] == [("right", True), ("bottom", True)]
    assert place["set_monitor"] == [(tk.monitors[1],)]
    assert menu.called("set_visual") == [("rgba-visual",)]


def test_settings_menu_without_an_rgba_visual(common, monkeypatch):
    tk = Toolkit(rgba=False)
    tk.install(monkeypatch)
    mod = load_script(DESK / "desk-settings.py", "desk_settings_window_under_test")
    menu = mod.Menu()
    assert menu.called("set_visual") == [] and menu.called("set_app_paintable") == [(True,)]


def test_settings_menu_keyboard_in_the_list_is_not_added_twice(settings_mod, common):
    common.save({"resolution": "1920x1080", "scale": "1.5", "sound": "phone", "keyboard": "fr"})
    menu = settings_mod.Menu()
    keyboard = combos(menu)[2]
    assert keyboard.items == common.LAYOUTS and keyboard.get_active_id() == "fr"
    assert menu.sound_phone.get_active() and not menu.sound_desktop.get_active()


def stored(common):
    return common.load()


def test_settings_menu_changes_resolution_and_scale(settings_mod, common):
    with open(common.OUTPUT_FILE, "w") as f:
        f.write("HEADLESS-1\n")
    common.fake_bin.stub("wlr-randr")
    menu = settings_mod.Menu()
    resolution, scale, _keyboard = combos(menu)
    resolution.set_active_id("3840x2160")
    scale.set_active_id("2")
    assert stored(common)["resolution"] == "3840x2160" and stored(common)["scale"] == "2"
    # Only the resolution is applied at once; the session applies the scale
    assert common.fake_bin.calls("wlr-randr") == [
        ["--output", "HEADLESS-1", "--custom-mode", "3840x2160"]]


def pactl_stub(common, sinks="0\thal_output\n", inputs="4\t0\t3\tPipeWire\n"):
    common.fake_bin.stub("pactl", f"""
case "$*" in
"list sinks short") printf '{sinks}' ;;
"list sink-inputs short") printf '{inputs}' ;;
esac
exit 0""")


def test_settings_menu_sound_to_the_desktop_and_back(settings_mod, common):
    pactl_stub(common, sinks="0\thal_output\n1\tplinius_desktop\n")
    menu = settings_mod.Menu()
    menu.sound_desktop.click()
    assert menu.sound_desktop.get_active() and not menu.sound_phone.get_active()
    assert stored(common)["sound"] == "desktop"
    menu.sound_phone.click()
    assert menu.sound_phone.get_active() and not menu.sound_desktop.get_active()
    assert stored(common)["sound"] == "phone"
    defaults = [c[1] for c in common.fake_bin.calls("pactl") if c[0] == "set-default-sink"]
    assert defaults == ["plinius_desktop", "hal_output"]


def test_settings_menu_pressed_sound_button_stays_pressed(settings_mod, common):
    pactl_stub(common)
    menu = settings_mod.Menu()
    menu.sound_phone.click()  # unpress the pressed one
    assert menu.sound_phone.get_active() and not menu.sound_desktop.get_active()


def test_settings_menu_sound_without_a_viewer_is_not_stored(settings_mod, common):
    pactl_stub(common)
    common.fake_bin.stub("wayvncctl", "echo '[]'")
    menu = settings_mod.Menu()
    menu.sound_desktop.click()
    assert not os.path.exists(common.SETTINGS) and stored(common)["sound"] == "phone"
    assert [c for c in common.fake_bin.calls("pactl") if c[0] != "list"] == []


def test_settings_menu_keyboard_change_offers_a_reconnect(settings_mod, common, tk, monkeypatch):
    menu = settings_mod.Menu()
    keyboard = combos(menu)[2]
    keyboard.set_active_id("ch")
    assert stored(common)["keyboard"] == "ch"
    assert menu.keyboard_note.called("set_no_show_all") == [(True,), (False,)]
    assert menu.keyboard_note.shown
    started = fakes.recorder()
    monkeypatch.setattr(settings_mod, "subprocess", fakes.Proxy(subprocess, Popen=started))
    reconnect, = menu.keyboard_note.find(Button)
    assert reconnect.get_label() == "Reconnect now"
    reconnect.click()
    assert started.calls == [(["systemctl", "--user", "restart", "plinius-desktop.service"],)]
    assert tk.quits == 1


@pytest.fixture
def pidfile(settings_mod, common, monkeypatch):
    path = common.runtime / "plinius-desk-settings.pid"
    monkeypatch.setattr(settings_mod, "PIDFILE", str(path))
    return path


def test_settings_main_opens_the_menu_and_cleans_up(settings_mod, common, tk, pidfile):
    seen = {}

    def while_open():
        seen["pid"] = pidfile.read_text()
        seen["menu"] = by_namespace(tk, "plinius-desk-settings")
    tk.on_main = while_open
    assert settings_mod.main() is None
    assert tk.mains == 1 and seen["pid"] == str(os.getpid())
    assert not pidfile.exists()
    assert common.closed_others == ["plinius-desk-settings.pid"]
    menu = seen["menu"]
    assert menu.shown and [h[0] for h in menu.handlers] == ["destroy"]
    # Dark theme, the menu's CSS, a SIGTERM handler that quits
    assert ("settings", "set_property", ("gtk-application-prefer-dark-theme", True)) in tk.log
    (_, provider, priority), = [e for e in tk.log if e[0] == "add_provider_for_screen"]
    assert provider.data == settings_mod.CSS and priority == 600
    assert tk.signals.calls == [(GLib.PRIORITY_DEFAULT, 15, tk.gtk.main_quit)]
    # The click catcher came first and quits on a click
    catcher = by_namespace(tk, "plinius-desk-catcher")
    assert tk.windows.index(catcher) < tk.windows.index(menu)
    menu.emit("destroy")
    assert tk.quits == 1


def test_settings_main_survives_a_vanished_pidfile(settings_mod, tk, pidfile):
    tk.on_main = pidfile.unlink
    settings_mod.main()
    assert tk.mains == 1


def test_settings_main_stays_closed_when_locked(settings_mod, common, tk, pidfile):
    common.locked = True
    settings_mod.main()
    assert tk.mains == 0 and tk.windows == [] and not pidfile.exists()


def test_settings_main_only_on_the_virtual_screen(settings_mod, common, tk, pidfile):
    tk.monitors.pop()  # only the phone's panel
    settings_mod.main()
    assert tk.mains == 0 and tk.windows == [] and common.closed_others == []


def test_settings_main_closes_an_open_menu(settings_mod, tk, monkeypatch):
    monkeypatch.setattr(settings_mod, "toggle_running", lambda: True)
    settings_mod.main()
    assert tk.mains == 0 and tk.windows == []


def test_settings_as_a_program(common, tk):
    common.locked = True
    with pytest.raises(SystemExit) as exit_:
        tk.run_main(DESK / "desk-settings.py")
    assert exit_.value.code is None and tk.mains == 0


# ===== desk-apps.py =====

class App:
    def __init__(self, app_id, name, categories="", description=None, icon=None, fails=False):
        self.app_id, self.name, self.categories = app_id, name, categories
        self.description, self.icon, self.fails = description, icon, fails
        self.launched = []

    def get_id(self):
        return self.app_id

    def get_display_name(self):
        return self.name

    get_name = get_display_name

    def should_show(self):
        return True

    def get_categories(self):
        return self.categories

    def get_description(self):
        return self.description

    def get_keywords(self):
        return []

    def get_icon(self):
        return self.icon

    def launch(self, files, context):
        self.launched.append((files, context))
        if self.fails:
            raise GLib.Error("no such program")


APPS = [App("firefox.desktop", "Firefox", "Network;", "Web Browser", icon="firefox-icon"),
        App("org.gnome.Calls.desktop", "Calls", "Telephony;"),
        App("ark.desktop", "Ark", "Utility;", "Archive tool")]


class AppSettings:
    db = {}

    def __init__(self, values):
        self.values = values

    @classmethod
    def new(cls, schema):
        return cls(cls.db.get(schema, {}))

    @classmethod
    def new_with_path(cls, schema, path):
        return cls(cls.db.get(path, {}))

    def get_strv(self, key):
        return self.values.get(key, [])

    def get_string(self, key):
        return self.values.get(key, "")

    def get_boolean(self, key):
        return False


class SchemaSource:
    def __init__(self, known):
        self.known = known

    def lookup(self, schema, recursive):
        return object() if schema in self.known else None


@pytest.fixture
def apps_mod(common, tk, monkeypatch):
    mod = load_script(DESK / "desk-apps.py", "desk_apps_window_under_test")
    assert mod.Gtk is tk.gtk
    AppSettings.db = {"mobi.phosh.shell": {"favorites": ["firefox.desktop"]},
                      "org.gnome.desktop.app-folders": {"folder-children": ["Tools"]},
                      mod.FOLDERS_PATH + "Tools/": {"name": "Tools", "apps": ["ark.desktop"]}}
    mod.apps = [App(a.app_id, a.name, a.categories, a.description, a.icon) for a in APPS]
    mod.schemas = SchemaSource({"mobi.phosh.shell"})
    monkeypatch.setattr(mod, "Gio", fakes.Proxy(
        Gio, AppInfo=types.SimpleNamespace(get_all=lambda: mod.apps), Settings=AppSettings,
        SettingsSchemaSource=types.SimpleNamespace(get_default=lambda: mod.schemas)))
    monkeypatch.setattr(mod, "GLib", fakes.Proxy(GLib, unix_signal_add=tk.signals))
    return mod


def shown(menu):
    """(name, description) of the rows in the app list."""
    out = []
    for row in menu.app_list.children:
        labels = row.texts()
        out.append((labels[0], labels[1] if len(labels) > 1 else None))
    return out


def test_apps_menu_lists_categories_and_shows_the_favorites(apps_mod, tk):
    menu = apps_mod.Menu()
    assert [row.texts() for row in menu.category_list.children] == [
        ["Favorites"], ["Tools"], ["Other"]]
    icons = [row.children[0].children[0].icon_name for row in menu.category_list.children]
    assert icons == ["starred-symbolic", "folder-symbolic", "view-app-grid-symbolic"]
    assert menu.category_list.get_selected_row().get_index() == 0
    assert shown(menu) == [("Firefox", "Web Browser")]
    row, = menu.app_list.children
    image = row.children[0].children[0]
    assert image.called("set_from_gicon") == [("firefox-icon", "dnd")]
    assert image.called("set_pixel_size") == [(32,)]
    place = tk.placement(menu)
    assert place["set_namespace"] == [("plinius-desk-apps",)]
    assert place["set_anchor"] == [("left", True), ("top", True)]
    assert place["set_keyboard_mode"] == [("exclusive",)]
    assert place["set_monitor"] == [(tk.monitors[1],)]


def test_apps_menu_starts_at_the_first_category_with_apps(apps_mod):
    AppSettings.db["mobi.phosh.shell"] = {"favorites": []}
    menu = apps_mod.Menu()
    assert menu.category_list.get_selected_row().get_index() == 1
    assert shown(menu) == [("Ark", "Archive tool")]


def test_apps_menu_without_any_app(apps_mod):
    apps_mod.apps = []
    menu = apps_mod.Menu()
    assert menu.category_list.get_selected_row().get_index() == 0 and shown(menu) == []


def test_apps_menu_without_phoshs_schema_has_no_favorites(apps_mod):
    apps_mod.schemas = SchemaSource(set())
    assert apps_mod.phosh_schema() is None
    categories, _all = apps_mod.load_categories()
    assert categories[0][0] == "Favorites" and categories[0][2] == []


@pytest.mark.parametrize("known, schema", [({"mobi.phosh.shell", "sm.puri.phosh"}, "mobi.phosh.shell"),
                                           ({"sm.puri.phosh"}, "sm.puri.phosh")])
def test_phosh_schema_new_name_before_old(apps_mod, known, schema):
    apps_mod.schemas = SchemaSource(known)
    assert apps_mod.phosh_schema() == schema


def test_phosh_schema_without_a_schema_source(apps_mod):
    apps_mod.schemas = None
    assert apps_mod.phosh_schema() is None


def test_apps_menu_search_and_category(apps_mod):
    menu = apps_mod.Menu()
    menu.search.type("call")
    assert shown(menu) == [("Calls", None)]
    # Picking a category empties the search and shows that category
    menu.category_list.select_row(menu.category_list.get_row_at_index(2))
    assert menu.search.get_text() == ""
    assert shown(menu) == [("Calls", None), ("Firefox", "Web Browser")]


def test_apps_menu_enter_starts_the_first_found_app(apps_mod, tk):
    menu = apps_mod.Menu()
    menu.search.type("archive")
    menu.search.emit("activate")
    ark = [a for a in apps_mod.apps if a.app_id == "ark.desktop"][0]
    assert ark.launched == [([], "launch-context")] and tk.quits == 1


def test_apps_menu_enter_with_nothing_found_does_nothing(apps_mod, tk):
    menu = apps_mod.Menu()
    menu.search.type("no such app")
    menu.search.emit("activate")
    assert tk.quits == 0 and all(a.launched == [] for a in apps_mod.apps)


def test_apps_menu_a_failed_launch_still_closes(apps_mod, tk, capsys):
    apps_mod.apps[0].fails = True
    menu = apps_mod.Menu()
    menu.app_list.activate(menu.app_list.get_row_at_index(0))
    assert apps_mod.apps[0].launched == [([], "launch-context")] and tk.quits == 1
    assert "launch failed: no such program" in capsys.readouterr().out


class DesktopApp(App):
    """A Gio.DesktopAppInfo: it has a desktop file, which gio launch starts."""

    def get_filename(self):
        return f"/usr/share/applications/{self.app_id}"


@pytest.mark.parametrize("token", [None, "token-1"])
def test_apps_menu_starts_a_desktop_app_in_a_scope_of_its_own(apps_mod, common, tk, monkeypatch, token):
    started = fakes.recorder()
    monkeypatch.setattr(common, "start_in_scope", started)
    tk.activation_token = token
    firefox = DesktopApp("firefox.desktop", "Firefox", "Network;")
    apps_mod.apps[0] = firefox
    menu = apps_mod.Menu()
    menu.app_list.activate(menu.app_list.get_row_at_index(0))
    # Not started by GLib in the menu's own cgroup, but by gio in the scope (V2 of 22.11.)
    assert firefox.launched == []
    (app_id, argv, env), = started.calls
    assert (app_id, argv) == ("firefox.desktop", ["gio", "launch", "/usr/share/applications/firefox.desktop"])
    assert ("get_startup_notify_id", "firefox.desktop", []) in tk.log
    # The window takes the focus with the launch context's token
    if token:
        assert env["XDG_ACTIVATION_TOKEN"] == env["DESKTOP_STARTUP_ID"] == token
    else:
        assert env.get("XDG_ACTIVATION_TOKEN") == os.environ.get("XDG_ACTIVATION_TOKEN")
    assert env["PATH"] == os.environ["PATH"] and tk.quits == 1


def test_apps_menu_an_app_that_does_not_start_still_closes(apps_mod, common, tk, monkeypatch, capsys):
    monkeypatch.setenv("PATH", "/nonexistent")   # no systemd-run
    apps_mod.apps[0] = DesktopApp("firefox.desktop", "Firefox", "Network;")
    menu = apps_mod.Menu()
    menu.app_list.activate(menu.app_list.get_row_at_index(0))
    assert tk.quits == 1 and "launch failed:" in capsys.readouterr().out


def test_apps_menu_escape_closes(apps_mod, tk):
    menu = apps_mod.Menu()
    assert menu.emit("key-press-event", types.SimpleNamespace(keyval=ord("a"))) is False
    assert tk.quits == 0
    assert menu.emit("key-press-event", types.SimpleNamespace(keyval=tk.gdk.KEY_Escape)) is True
    assert tk.quits == 1


@pytest.fixture
def apps_pidfile(apps_mod, common, monkeypatch):
    path = common.runtime / "plinius-desk-apps.pid"
    monkeypatch.setattr(apps_mod, "PIDFILE", str(path))
    return path


def test_apps_main_opens_the_menu_with_the_search_focused(apps_mod, common, tk, apps_pidfile):
    seen = {}

    def while_open():
        seen["pid"] = apps_pidfile.read_text()
        seen["menu"] = by_namespace(tk, "plinius-desk-apps")
    tk.on_main = while_open
    apps_mod.main()
    menu = seen["menu"]
    assert seen["pid"] == str(os.getpid()) and not apps_pidfile.exists()
    assert menu.shown and menu.search.focused
    assert common.closed_others == ["plinius-desk-apps.pid"]
    (_, provider, _prio), = [e for e in tk.log if e[0] == "add_provider_for_screen"]
    assert provider.data == apps_mod.CSS
    assert tk.signals.calls == [(GLib.PRIORITY_DEFAULT, 15, tk.gtk.main_quit)]
    by_namespace(tk, "plinius-desk-catcher")


def test_apps_main_survives_a_vanished_pidfile(apps_mod, tk, apps_pidfile):
    tk.on_main = apps_pidfile.unlink
    apps_mod.main()
    assert tk.mains == 1


@pytest.mark.parametrize("why", ["running", "locked", "no-screen"])
def test_apps_main_opens_nothing(apps_mod, common, tk, apps_pidfile, monkeypatch, why):
    if why == "running":
        monkeypatch.setattr(apps_mod, "toggle_running", lambda: True)
    elif why == "locked":
        common.locked = True
    else:
        tk.monitors[1].mm = 120  # a real monitor, no virtual screen
    apps_mod.main()
    assert tk.mains == 0 and tk.windows == [] and not apps_pidfile.exists()


# ----- the resident app menu (desk-apps.py --resident) -----

@pytest.fixture
def resident_mod(apps_mod, common, monkeypatch):
    monkeypatch.setattr(apps_mod, "Gio", fakes.Proxy(apps_mod.Gio, bus_get_sync=lambda *a: "bus",
                                                     BusType=Gio.BusType))
    monkeypatch.setattr(common, "screen_locked", lambda bus: bus == "bus" and common.locked)
    apps_mod.idles = fakes.recorder()
    monkeypatch.setattr(apps_mod, "GLib", fakes.Proxy(apps_mod.GLib, idle_add=apps_mod.idles))
    return apps_mod


def test_resident_menu_warms_up_invisible_and_click_through(resident_mod, tk):
    resident = resident_mod.Resident(tk.monitors[1])
    menu = resident.menu
    assert resident.warm_up() is False
    catcher = by_namespace(tk, "plinius-desk-catcher")
    assert resident.warming and menu.shown and not resident.open
    for window in (catcher, menu):
        (region,), = window.called("input_shape_combine_region")
        assert isinstance(region, resident_mod.cairo.Region)
    assert menu.card.called("set_opacity") == [(0,)]
    assert tk.placement(menu)["set_keyboard_mode"] == [("exclusive",), ("none",)]
    # Drawn once: it goes again, visible and clickable for the first real click
    assert menu.emit("draw", "cr") is False
    assert resident_mod.idles.calls == [(resident.end_warm_up,)]
    assert resident.end_warm_up() is False
    assert not resident.warming and menu.called("hide") == [()]
    assert catcher.called("hide") == [()]
    for window in (catcher, menu):
        assert window.called("input_shape_combine_region")[-1] == (None,)
    assert menu.card.called("set_opacity") == [(0,), (1,)]
    assert tk.placement(menu)["set_keyboard_mode"][-1] == ("exclusive",)
    # Drawn again, or a late idle call: nothing more happens
    menu.emit("draw", "cr")
    assert resident.end_warm_up() is False
    assert resident_mod.idles.calls == [(resident.end_warm_up,)]
    assert menu.called("hide") == [()]


def test_resident_menu_click_during_the_warm_up(resident_mod, tk):
    resident = resident_mod.Resident(tk.monitors[1])
    resident.warm_up()
    resident.toggle()
    menu = resident.menu
    # Made visible where it is, not hidden first, and the warm-up's catcher is reused
    assert resident.open and not resident.warming and menu.called("hide") == []
    assert menu.card.called("set_opacity") == [(0,), (1,)]
    assert by_namespace(tk, "plinius-desk-catcher") is resident.catcher
    assert menu.search.focused


def test_resident_menu_shows_and_hides_without_a_new_process(resident_mod, common, tk):
    resident = resident_mod.Resident(tk.monitors[1])
    menu = resident.menu
    assert not menu.shown and resident.catcher is None
    assert resident.toggle() is True
    assert resident.open and menu.shown and menu.search.focused
    catcher = by_namespace(tk, "plinius-desk-catcher")
    assert tk.placement(catcher)["set_monitor"] == [(tk.monitors[1],)]
    assert common.closed_others == ["plinius-desk-apps.pid"]
    # A second click hides menu and catcher; the process and its windows stay
    resident.toggle()
    assert not resident.open and menu.called("hide") == [()] and catcher.called("hide") == [()]
    resident.toggle()
    assert resident.open and by_namespace(tk, "plinius-desk-catcher") is catcher
    assert tk.quits == 0


def test_resident_menu_closes_by_hiding(resident_mod, tk):
    resident = resident_mod.Resident(tk.monitors[1])
    resident.toggle()
    menu = resident.menu
    menu.emit("key-press-event", types.SimpleNamespace(keyval=tk.gdk.KEY_Escape))
    assert not resident.open and tk.quits == 0
    resident.toggle()
    menu.app_list.activate(menu.app_list.get_row_at_index(0))
    assert resident_mod.apps[0].launched == [([], "launch-context")]
    assert not resident.open and tk.quits == 0
    # A click next to it, through the catcher
    resident.toggle()
    resident.catcher.emit("button-press-event", None)
    assert not resident.open


def test_resident_menu_shows_the_apps_as_they_are_now(resident_mod, tk):
    resident = resident_mod.Resident(tk.monitors[1])
    menu = resident.menu
    resident.toggle()
    menu.search.type("call")
    resident.hide()
    # Installed while it was hidden, and favorites changed
    resident_mod.apps.append(App("org.gnome.Maps.desktop", "Maps", "Utility;"))
    AppSettings.db["mobi.phosh.shell"] = {"favorites": ["org.gnome.Maps.desktop"]}
    resident.toggle()
    assert menu.search.get_text() == ""
    assert [row.texts() for row in menu.category_list.children] == [
        ["Favorites"], ["Tools"], ["Other"]]
    assert shown(menu) == [("Maps", None)]


def test_resident_menu_stays_hidden_while_locked(resident_mod, common, tk):
    resident = resident_mod.Resident(tk.monitors[1])
    common.locked = True
    resident.toggle()
    assert not resident.open and not resident.menu.shown and resident.catcher is None
    assert common.closed_others == []


def test_resident_menu_hide_before_any_show(resident_mod, tk):
    resident = resident_mod.Resident(tk.monitors[1])
    assert resident.hide() is True and not resident.open


def test_serve_waits_hidden_for_its_signals(resident_mod, common, tk, monkeypatch):
    path = common.runtime / common.RESIDENT_APPS_PIDFILE
    monkeypatch.setattr(resident_mod, "PIDFILE", str(common.runtime / "plinius-desk-apps.pid"))
    hides = []
    monkeypatch.setattr(common, "close_when_locked", lambda quit: hides.append(quit) or True)
    seen = {}

    def while_waiting():
        seen["pid"] = path.read_text()
        seen["menu"] = by_namespace(tk, "plinius-desk-apps")
    tk.on_main = while_waiting
    resident_mod.main(["--resident"])
    assert seen["pid"] == str(os.getpid()) and not path.exists()
    assert not seen["menu"].shown
    (_, provider, _prio), = [e for e in tk.log if e[0] == "add_provider_for_screen"]
    assert provider.data == resident_mod.CSS
    (p1, usr1, toggle), (p2, usr2, hide), (p3, term, quit) = tk.signals.calls
    assert (usr1, usr2, term) == (10, 12, 15)
    assert p1 == p2 == p3 == GLib.PRIORITY_DEFAULT and quit is tk.gtk.main_quit
    assert toggle.__self__ is hide.__self__ and toggle.__name__ == "toggle"
    assert hide.__name__ == "hide" and hides == [hide]
    # The warm-up waits for the main loop
    (warm,), = resident_mod.idles.calls
    assert warm.__self__ is toggle.__self__ and warm.__name__ == "warm_up"


def test_serve_survives_a_vanished_pidfile(resident_mod, common, tk):
    tk.on_main = (common.runtime / common.RESIDENT_APPS_PIDFILE).unlink
    resident_mod.serve()
    assert tk.mains == 1


def test_serve_without_a_virtual_screen(resident_mod, common, tk):
    tk.monitors[1].mm = 120
    resident_mod.serve()
    assert tk.mains == 0 and tk.windows == []
    assert not (common.runtime / common.RESIDENT_APPS_PIDFILE).exists()


def test_apps_as_a_program(common, tk):
    common.locked = True
    with pytest.raises(SystemExit) as exit_:
        tk.run_main(DESK / "desk-apps.py")
    assert exit_.value.code is None and tk.mains == 0


# ===== desk_common: the click catcher and the details card =====

def test_click_catcher_covers_the_screen_and_reports_clicks(common, tk):
    clicks = fakes.recorder()
    window = common.click_catcher("monitor", clicks)
    place = tk.placement(window)
    assert place["set_namespace"] == [("plinius-desk-catcher",)]
    assert place["set_layer"] == [("overlay",)]
    assert sorted(place["set_anchor"]) == [("bottom", True), ("left", True), ("right", True),
                                           ("top", True)]
    # Keeps clear of the bar and takes no keyboard
    assert place["set_exclusive_zone"] == [(0,)]
    assert place["set_keyboard_mode"] == [("none",)]
    assert place["set_monitor"] == [("monitor",)]
    assert window.called("set_visual") == [("rgba-visual",)] and window.shown
    assert window.called("add_events") == [(tk.gdk.EventMask.BUTTON_PRESS_MASK
                                             | tk.gdk.EventMask.TOUCH_MASK,)]
    assert window.emit("button-press-event", "event") is True
    assert window.emit("touch-event", "event") is True
    assert clicks.calls == [(), ()]
    # It draws nothing but clear pixels
    cr = []
    recording = types.SimpleNamespace(set_source_rgba=lambda *a: cr.append(("rgba", a)),
                                      set_operator=lambda op: cr.append(("op", op)),
                                      paint=lambda: cr.append("paint"))
    assert window.emit("draw", recording) is True
    assert cr == [("rgba", (0, 0, 0, 0)), ("op", 1), "paint"]


def test_click_catcher_without_an_rgba_visual(common, monkeypatch):
    tk = Toolkit(rgba=False)
    tk.install(monkeypatch)
    window = common.click_catcher("monitor", lambda: None)
    assert window.called("set_visual") == []


def popup(common, rows=lambda: [("Level", "57 %"), ("State", "Charging")], pid="plinius-desk-battery.pid"):
    return common.info_popup(pid, "Battery", rows, "Power Settings",
                             ["gnome-control-center", "power"])


def test_info_popup_shows_the_rows_and_opens_the_settings(common, tk, monkeypatch):
    started = fakes.recorder()
    monkeypatch.setattr(common, "start_in_scope", started)
    pidfile = common.runtime / "plinius-desk-battery.pid"
    seen = {}

    def while_open():
        seen["pid"] = pidfile.read_text()
        card = by_namespace(tk, "plinius-desk-info")
        seen["card"] = card
        grid, = card.find(Grid)
        seen["rows"] = grid.rows()
        seen["wrap"] = [v.called("set_line_wrap") for (c, _r), v in grid.cells.items() if c == 1]
        button, = card.find(Button)
        seen["button"] = button.get_label()
        button.click()
    tk.on_main = while_open
    popup(common)
    assert seen["pid"] == str(os.getpid()) and not pidfile.exists()
    card = seen["card"]
    assert card.texts()[0] == "Battery"
    assert seen["rows"] == [("Level", "57 %"), ("State", "Charging")]
    assert seen["wrap"] == [[(True,)], [(True,)]]
    assert seen["button"] == "Power Settings"
    # The settings outlive the desktop session, in a scope of their own (desk_common.start_in_scope)
    assert started.calls == [("gnome-control-center", ["gnome-control-center", "power"])] and tk.quits == 1
    place = tk.placement(card)
    assert place["set_anchor"] == [("right", True), ("top", True)]
    assert place["set_monitor"] == [(tk.monitors[1],)]
    assert common.closed_others == ["plinius-desk-battery.pid"]
    assert tk.signals.calls == [(GLib.PRIORITY_DEFAULT, 15, tk.gtk.main_quit)]
    (_, provider, _prio), = [e for e in tk.log if e[0] == "add_provider_for_screen"]
    assert provider.data == common.INFO_CSS
    card.emit("destroy")
    assert tk.quits == 2


def test_info_popup_with_a_text_instead_of_rows(common, tk):
    pidfile = common.runtime / "plinius-desk-network.pid"
    seen = {}

    def while_open():
        card = by_namespace(tk, "plinius-desk-info")
        seen["grids"] = card.find(Grid)
        seen["texts"] = card.texts()
        pidfile.unlink()  # gone already: no complaint at the end
    tk.on_main = while_open
    popup(common, rows=lambda: "Not connected", pid="plinius-desk-network.pid")
    assert seen["grids"] == [] and seen["texts"] == ["Battery", "Not connected"]


def test_info_popup_without_an_rgba_visual(common, monkeypatch):
    tk = Toolkit(rgba=False)
    tk.install(monkeypatch, GLib=fakes.Proxy(GLib, unix_signal_add=lambda *a: None))
    popup(common)
    card = by_namespace(tk, "plinius-desk-info")
    assert card.called("set_visual") == [] and tk.mains == 1


def test_info_popup_second_click_closes_the_open_card(common, tk):
    card = spawn_marked("desk-battery.py")
    try:
        (common.runtime / "plinius-desk-battery.pid").write_text(f"{card.pid}\n")
        popup(common)
        assert card.wait(timeout=10) == -15
    finally:
        if card.poll() is None:
            card.kill()
        card.wait()
    assert tk.windows == [] and tk.mains == 0


def test_info_popup_stale_pidfile_of_another_process_opens_a_new_card(common, tk):
    other = spawn_marked("unrelated")
    try:
        (common.runtime / "plinius-desk-battery.pid").write_text(f"{other.pid}\n")
        popup(common)
        assert other.poll() is None
    finally:
        other.kill()
        other.wait()
    assert tk.mains == 1 and by_namespace(tk, "plinius-desk-info")


@pytest.mark.parametrize("why", ["locked", "no-screen"])
def test_info_popup_opens_nothing(common, tk, why):
    if why == "locked":
        common.locked = True
    else:
        tk.monitors.pop()
    popup(common)
    assert tk.windows == [] and tk.mains == 0
    assert not (common.runtime / "plinius-desk-battery.pid").exists()


def test_outputs_skips_indented_lines_before_the_first_output(common):
    common.fake_bin.stub("wlr-randr", "printf '  stray line\\nHEADLESS-1 x\\n  Enabled: yes\\n'")
    assert common.outputs() == {"HEADLESS-1": {"enabled": True, "position": None, "mode": None,
                                               "scale": None}}


# ===== desk-network.py and desk-battery.py as programs =====

@pytest.fixture
def popups(common, monkeypatch):
    calls = []

    def info_popup(pidname, title, rows, button_label, button_cmd):
        calls.append((pidname, title, rows(), button_label, button_cmd))
    monkeypatch.setattr(common, "info_popup", info_popup)
    return calls


def test_network_card_as_a_program_when_not_connected(common, popups):
    common.fake_bin.stub("nmcli", "exit 0")
    with pytest.raises(SystemExit) as exit_:
        import runpy
        runpy.run_path(str(DESK / "desk-network.py"), run_name="__main__")
    assert exit_.value.code is None
    assert popups == [("plinius-desk-network.pid", "Wi-Fi", "Not connected", "Wi-Fi Settings",
                       ["gnome-control-center", "wifi"])]


def test_network_details_without_a_connected_device_or_band(common, popups):
    common.fake_bin.stub("nmcli", r"""
case "$*" in
*IN-USE*) echo '*:Cafe:30:unknown::' ;;
*DEVICE*) echo 'wlan0:wifi:disconnected' ;;
esac""")
    network = load_script(DESK / "desk-network.py", "desk_network_window_under_test")
    # No frequency to name a band, no security (an open network), no device with addresses
    assert network.details() == [("Network", "Cafe"), ("Signal", "30 %"), ("Band", "unknown"),
                                 ("Security", "Open")]
    network.main()
    assert popups[0][2] == network.details()


def test_battery_card_as_a_program(common, popups, monkeypatch):
    import locale
    import runpy
    monkeypatch.setattr(locale, "setlocale", lambda *a: "C")
    common.fake_bin.stub("upower", "exit 0")
    with pytest.raises(SystemExit):
        runpy.run_path(str(DESK / "desk-battery.py"), run_name="__main__")
    assert popups == [("plinius-desk-battery.pid", "Battery", "No battery", "Power Settings",
                       ["gnome-control-center", "power"])]


def test_apps_menu_without_an_rgba_visual(common, monkeypatch):
    tk = Toolkit(rgba=False)
    tk.install(monkeypatch)
    mod = load_script(DESK / "desk-apps.py", "desk_apps_window_under_test")
    monkeypatch.setattr(mod, "Gio", fakes.Proxy(
        Gio, AppInfo=types.SimpleNamespace(get_all=lambda: []), Settings=AppSettings,
        SettingsSchemaSource=types.SimpleNamespace(get_default=lambda: None)))
    menu = mod.Menu()
    assert menu.called("set_visual") == [] and menu.called("set_app_paintable") == [(True,)]
