# The bar's menus of plinius-desktop: the Wi-Fi and battery cards (nmcli/upower parsing), the
# app menu's categories and search, the settings menu's handlers. GTK is faked (see
# desktop_fakes), the menus' windows are never built.

import locale
import sys
import types

import pytest
from gi.repository import GLib

import desktop_fakes as fakes
from desk_fake_gtk3 import spawn_marked
from conftest import PKG, load_script

DESK = PKG / "plinius-desktop"


@pytest.fixture(scope="module")
def mods():
    with pytest.MonkeyPatch.context() as mp:
        mp.syspath_prepend(str(DESK))
        common = load_script(DESK / "desk_common.py", "desk_common")
        # desk-battery sets the process locale from the environment when it starts
        mp.setattr(locale, "setlocale", lambda *a: "C")
        battery = load_script(DESK / "desk-battery.py", "desk_battery_under_test")
        network = load_script(DESK / "desk-network.py", "desk_network_under_test")
        apps = fakes.load_gui(DESK / "desk-apps.py", "desk_apps_under_test")
        settings = fakes.load_gui(DESK / "desk-settings.py", "desk_settings_under_test")
    return types.SimpleNamespace(common=common, battery=battery, network=network, apps=apps,
                                 settings=settings)


# ----- desk-network.py -----

@pytest.mark.parametrize("line, fields", [
    ("*:Home:72", ["*", "Home", "72"]),
    (r"*:My\:Net:5", ["*", "My:Net", "5"]),
    (r"a\\b:c", ["a\\b", "c"]),
    (":::", ["", "", "", ""]),
    ("", [""]),
])
def test_nmcli_split(mods, line, fields):
    assert mods.network.split(line) == fields


def nmcli_answers(answers):
    def nmcli(*args):
        return answers[args[1]] if args[0] == "-f" else pytest.fail(args)
    return nmcli


def test_network_details_none_when_not_connected(mods, monkeypatch):
    monkeypatch.setattr(mods.network, "nmcli", nmcli_answers(
        {"IN-USE,SSID,SIGNAL,FREQ,SECURITY,RATE": [" :Other:40:2412 MHz:WPA2:130 Mbit/s"]}))
    assert mods.network.details() is None


CONNECTED = {
    "IN-USE,SSID,SIGNAL,FREQ,SECURITY,RATE": [
        " :Neighbour:40:2437 MHz:WPA2:130 Mbit/s",
        r"*:Home\:5G:78:5180 MHz:WPA2 WPA3:866 Mbit/s"],
    "DEVICE,TYPE,STATE": ["ap0:wifi:connected (externally)", "wlan0:wifi:connected",
                          "ccmni0:gsm:connected"],
    "IP4.ADDRESS,IP4.GATEWAY,IP4.DNS": ["IP4.ADDRESS[1]:192.0.2.20/24",
                                        "IP4.GATEWAY:192.0.2.1",
                                        "IP4.DNS[1]:192.0.2.1", "IP4.DNS[2]:9.9.9.9"],
}


def test_network_details_of_the_connection(mods, monkeypatch):
    monkeypatch.setattr(mods.network, "nmcli", nmcli_answers(CONNECTED))
    assert mods.network.details() == [
        ("Network", "Home:5G"), ("Signal", "78 %"), ("Band", "5 GHz (5180 MHz)"),
        ("Security", "WPA2 WPA3"), ("Rate", "866 Mbit/s"),
        ("IP address", "192.0.2.20/24"), ("Gateway", "192.0.2.1"),
        ("DNS", "192.0.2.1, 9.9.9.9")]


def test_nmcli_runs_terse(mods, fake_bin, monkeypatch):
    fake_bin.stub("nmcli", r"printf 'a:b\n\nc:d\n'")
    monkeypatch.setenv("PATH", fake_bin.env(isolated=True)["PATH"])
    assert mods.network.nmcli("-f", "DEVICE", "dev") == ["a:b", "c:d"]
    assert fake_bin.calls("nmcli") == [["-t", "-f", "DEVICE", "dev"]]


# ----- desk-battery.py -----

UPOWER_I = """\
  native-path:          battery
  power supply:         yes
  updated:              Sa 14 Nov 2026 10:00:00 CET (5 seconds ago)
  battery
    present:             yes
    state:               discharging
    energy-rate:         4,417 W
    voltage:             3,953 V
    charge-cycles:       312
    time to empty:       2,4 hours
    percentage:          57%
    capacity:            92,5%
    technology:          lithium-polymer
    temperature:         31,6 degrees C
  History (charge):
    1700000000	57,000	discharging
"""


def upower_stub(fake_bin, monkeypatch, info, devices="/org/freedesktop/UPower/devices/battery_battery"):
    fake_bin.stub("upower", f"""[ "$1" = -e ] && {{ echo /org/freedesktop/UPower/devices/DisplayDevice; echo {devices}; exit 0; }}
cat <<'EOF'
{info}EOF""")
    monkeypatch.setenv("PATH", fake_bin.env(isolated=True)["PATH"])


@pytest.fixture
def numeric_locale():
    """Sets LC_NUMERIC for one test and puts it back: other tests in this process may have
    loaded programs that set the locale from the environment."""
    real = sys.modules["locale"]
    old = real.setlocale(real.LC_NUMERIC)

    def use(name):
        try:
            real.setlocale(real.LC_NUMERIC, name)
        except real.Error:
            pytest.skip(f"no {name} locale on this host")
    yield use
    real.setlocale(real.LC_NUMERIC, old)


def test_battery_details(numeric_locale, mods, fake_bin, monkeypatch):
    upower_stub(fake_bin, monkeypatch, UPOWER_I)
    numeric_locale("C")
    assert mods.battery.details() == [
        ("Level", "57 %"), ("State", "On battery"), ("Time left", "2,4 hours"),
        ("Power", "4.42 W"), ("Voltage", "3.95 V"), ("Temperature", "32 °C"),
        ("Health", "92 %"), ("Charge cycles", "312"), ("Technology", "Lithium Polymer")]


def test_battery_details_leave_out_what_upower_lacks(mods, fake_bin, monkeypatch):
    upower_stub(fake_bin, monkeypatch, "  state:  pending-charge\n  percentage:  80%\n"
                "  time to full:  1,0 hours\n  energy-rate:  n/a\n")
    assert mods.battery.details() == [("Level", "80 %"), ("State", "Plugged in, not charging"),
                                      ("Full in", "1,0 hours")]


def test_battery_details_unknown_state_is_shown_as_is(mods, fake_bin, monkeypatch):
    upower_stub(fake_bin, monkeypatch, "  state:  weird\n")
    assert mods.battery.details() == [("State", "weird")]


def test_no_battery(mods, fake_bin, monkeypatch):
    upower_stub(fake_bin, monkeypatch, "", devices="/org/freedesktop/UPower/devices/line_power_ac")
    assert mods.battery.details() == "No battery"


def test_battery_numbers_in_the_phones_language(numeric_locale, mods, fake_bin, monkeypatch):
    upower_stub(fake_bin, monkeypatch, UPOWER_I)
    numeric_locale("de_DE.UTF-8")
    rows = dict(mods.battery.details())
    assert rows["Power"] == "4,42 W" and rows["Voltage"] == "3,95 V"


# ----- desk-apps.py -----

class App:
    def __init__(self, app_id, name, categories="", show=True, description=None, keywords=()):
        self.app_id, self.name, self.categories, self.show = app_id, name, categories, show
        self.description, self.keywords = description, list(keywords)

    def get_id(self):
        return self.app_id

    def get_display_name(self):
        return self.name

    def get_name(self):
        return self.name

    def should_show(self):
        return self.show

    def get_categories(self):
        return self.categories

    def get_description(self):
        return self.description

    def get_keywords(self):
        return self.keywords

    def __repr__(self):
        return self.app_id


APPS = [App("org.gnome.Calls.desktop", "Calls", "GTK;Telephony;"),
        App("firefox.desktop", "Firefox", "Network;WebBrowser;", keywords=["internet", "web"]),
        App("org.gnome.Settings.desktop", "Settings", "GNOME;Settings;"),
        App("emilia.desktop", "Émilia", "AudioVideo;Audio;"),
        App("ark.desktop", "ark", "Utility;Archiving;", description="Archive tool"),
        App("hidden.desktop", "Hidden", "Utility;", show=False)]


class FolderSettings:
    db = {}

    def __init__(self, values):
        self.values = values

    @classmethod
    def new(cls, schema):
        return cls(cls.db[schema])

    @classmethod
    def new_with_path(cls, schema, path):
        assert schema == "org.gnome.desktop.app-folders.folder"
        return cls(cls.db[path])

    def get_strv(self, key):
        return self.values.get(key, [])

    def get_string(self, key):
        return self.values.get(key, "")

    def get_boolean(self, key):
        return self.values.get(key, False)


@pytest.fixture
def fake_apps(mods, monkeypatch):
    FolderSettings.db = {
        "mobi.phosh.shell": {"favorites": ["firefox.desktop", "gone.desktop",
                                           "org.gnome.Calls.desktop"]},
        "org.gnome.desktop.app-folders": {"folder-children": ["Tools", "Media"]},
        "/org/gnome/desktop/app-folders/folders/Tools/": {
            "name": "Tools", "apps": ["org.gnome.Settings.desktop", "hidden.desktop"],
            "categories": ["Utility"]},
        "/org/gnome/desktop/app-folders/folders/Media/": {
            "name": "Media", "categories": ["Audio", "Video"]},
    }
    gio = fakes.Proxy(mods.apps.Gio, AppInfo=types.SimpleNamespace(get_all=lambda: APPS),
                      Settings=FolderSettings)
    monkeypatch.setattr(mods.apps, "Gio", gio)
    monkeypatch.setattr(mods.apps, "phosh_schema", lambda: "mobi.phosh.shell")
    return mods.apps


def ids(apps):
    return [a.get_id() for a in apps]


def test_categories_favorites_folders_and_the_rest(fake_apps):
    categories, all_apps = fake_apps.load_categories()
    names = [(name, icon) for name, icon, _ in categories]
    assert names == [("Favorites", "starred-symbolic"), ("Tools", "folder-symbolic"),
                     ("Media", "folder-symbolic"), ("Other", "view-app-grid-symbolic")]
    # Favorites keep Phosh's order and skip apps that are gone or hidden
    assert ids(categories[0][2]) == ["firefox.desktop", "org.gnome.Calls.desktop"]
    # A folder takes its listed apps and those of its categories, sorted by name
    assert ids(categories[1][2]) == ["ark.desktop", "org.gnome.Settings.desktop"]
    assert ids(categories[2][2]) == ["emilia.desktop"]
    # "Other": in no folder (favorites are no folder)
    assert ids(categories[3][2]) == ["org.gnome.Calls.desktop", "firefox.desktop"]
    # All apps sorted by name, ignoring case (collation of accents depends on the locale)
    assert [i for i in ids(all_apps) if i != "emilia.desktop"] == [
        "ark.desktop", "org.gnome.Calls.desktop", "firefox.desktop", "org.gnome.Settings.desktop"]


def test_folder_name_from_a_directory_file(mods, tmp_path, monkeypatch):
    d = tmp_path / "share" / "desktop-directories"
    d.mkdir(parents=True)
    (d / "X-GNOME-Utilities.directory").write_text(
        "[Desktop Entry]\nName=Utilities\nType=Directory\n")
    monkeypatch.setattr(mods.apps, "GLib", fakes.Proxy(
        GLib, get_system_data_dirs=lambda: [str(tmp_path / "nothing"), str(tmp_path / "share")]))
    assert mods.apps.folder_name("X-GNOME-Utilities.directory", True) == "Utilities"
    # Without translate the stored name is shown as it is
    assert mods.apps.folder_name("X-GNOME-Utilities.directory", False) == \
        "X-GNOME-Utilities.directory"
    assert mods.apps.folder_name("Missing.directory", True) == "Missing.directory"


class Entry:
    def __init__(self, text):
        self.text = text

    def get_text(self):
        return self.text

    def set_text(self, text):
        self.text = text


class Row:
    def __init__(self, index):
        self.index = index

    def get_index(self):
        return self.index


def menu(fake_apps, text="", selected=0):
    categories, all_apps = fake_apps.load_categories()
    shown = []
    return types.SimpleNamespace(
        categories=categories, all_apps=all_apps, search=Entry(text),
        category_list=types.SimpleNamespace(get_selected_row=lambda: Row(selected)
                                            if selected is not None else None),
        show_apps=lambda apps: shown.append(ids(apps)), shown=shown)


@pytest.mark.parametrize("text, found", [
    ("fire", ["firefox.desktop"]),
    ("WEB", ["firefox.desktop"]),               # keywords, any case
    ("archive", ["ark.desktop"]),               # description
    ("gnome.calls", ["org.gnome.Calls.desktop"]),  # the id
    ("émil", ["emilia.desktop"]),
    ("nothing like it", []),
])
def test_search_looks_through_all_apps(fake_apps, text, found):
    m = menu(fake_apps, text)
    fake_apps.Menu.on_search(m, m.search)
    assert m.shown == [found]


@pytest.mark.parametrize("selected, shown", [(2, ["emilia.desktop"]),
                                             (None, ["firefox.desktop", "org.gnome.Calls.desktop"])])
def test_empty_search_shows_the_category_again(fake_apps, selected, shown):
    m = menu(fake_apps, "  ", selected)
    fake_apps.Menu.on_search(m, m.search)
    assert m.shown == [shown]


def test_picking_a_category_clears_the_search(fake_apps):
    m = menu(fake_apps, "fire")
    fake_apps.Menu.on_category(m, None, Row(1))
    assert m.search.text == "" and m.shown == [["ark.desktop", "org.gnome.Settings.desktop"]]
    fake_apps.Menu.on_category(m, None, None)
    assert len(m.shown) == 1


@pytest.fixture
def own_process():
    procs = []

    def spawn(marker):
        p = spawn_marked(marker)
        procs.append(p)
        return p
    yield spawn
    for p in procs:
        if p.poll() is None:
            p.kill()
        p.wait()


@pytest.mark.parametrize("module, marker", [("apps", "desk-apps.py"),
                                            ("settings", "desk-settings.py")])
def test_toggle_running_closes_the_open_menu(mods, tmp_path, monkeypatch, own_process, module,
                                             marker):
    mod = getattr(mods, module)
    pidfile = tmp_path / "menu.pid"
    monkeypatch.setattr(mod, "PIDFILE", str(pidfile))
    assert mod.toggle_running() is False
    other = own_process("unrelated")
    pidfile.write_text(str(other.pid))
    assert mod.toggle_running() is False and other.poll() is None
    open_menu = own_process(marker)
    pidfile.write_text(str(open_menu.pid))
    assert mod.toggle_running() is True
    assert open_menu.wait(timeout=10) == -15


# ----- desk-settings.py -----

class Toggle:
    def __init__(self, active):
        self.active = active
        self.sets = []

    def get_active(self):
        return self.active

    def set_active(self, active):
        self.active = active
        self.sets.append(active)


@pytest.fixture
def settings_menu(mods, monkeypatch):
    common = mods.settings.common
    saved, sounds, sizes = [], [], []
    monkeypatch.setattr(common, "save", lambda values: saved.append(dict(values)))
    monkeypatch.setattr(common, "set_sound", lambda target: sounds.append(target) or True)
    monkeypatch.setattr(common, "set_resolution", sizes.append)
    m = types.SimpleNamespace(settings={"resolution": "1920x1080", "scale": "1.5",
                                        "sound": "phone", "keyboard": "us"},
                              sound_phone=Toggle(True), sound_desktop=Toggle(False),
                              saved=saved, sounds=sounds, sizes=sizes)
    m.store = lambda key, value: mods.settings.Menu.store(m, key, value)
    return m


def test_sound_to_the_desktop(mods, settings_menu):
    m = settings_menu
    m.sound_desktop.active = True
    mods.settings.Menu.on_sound(m, m.sound_desktop, "desktop")
    assert m.sound_phone.active is False and m.sounds == ["desktop"]
    assert m.saved[-1]["sound"] == "desktop"


def test_one_sound_button_stays_pressed(mods, settings_menu):
    m = settings_menu
    m.sound_phone.active = False  # the user unpressed the pressed one
    mods.settings.Menu.on_sound(m, m.sound_phone, "phone")
    assert m.sound_phone.active is True and m.sounds == [] and m.saved == []


def test_sound_not_stored_when_it_cannot_switch(mods, settings_menu, monkeypatch):
    m = settings_menu
    monkeypatch.setattr(mods.settings.common, "set_sound", lambda target: False)
    m.sound_desktop.active = True
    mods.settings.Menu.on_sound(m, m.sound_desktop, "desktop")
    assert m.saved == []


def test_resolution_is_stored_and_applied(mods, settings_menu):
    m = settings_menu
    combo = types.SimpleNamespace(get_active_id=lambda: "2560x1600")
    mods.settings.Menu.on_resolution(m, combo)
    assert m.sizes == ["2560x1600"] and m.saved[-1]["resolution"] == "2560x1600"
    mods.settings.Menu.on_scale(m, types.SimpleNamespace(get_active_id=lambda: "1.75"))
    assert m.saved[-1] == {"resolution": "2560x1600", "scale": "1.75", "sound": "phone",
                           "keyboard": "us"}


def test_resolutions_and_scales_are_well_formed(mods):
    common = mods.common
    for key, _label in common.RESOLUTIONS:
        w, h = key.split("x")
        assert int(w) > int(h) > 0
    assert all(float(k) >= 1 for k, _ in common.SCALES)
    assert common.DEFAULTS["resolution"] in dict(common.RESOLUTIONS)
    assert common.DEFAULTS["scale"] in dict(common.SCALES)
    assert len({k for k, _ in common.LAYOUTS}) == len(common.LAYOUTS)
