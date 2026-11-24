# desk_common.py of plinius-desktop: settings file, the virtual screen, sound through the tunnel,
# wlr-randr parsing, closing the menus. Programs come from stubs, files live in tmp_path.

import os
import subprocess

import pytest
from gi.repository import Gio, GLib

import desktop_fakes as fakes
from desk_fake_gtk3 import spawn_marked
from conftest import PKG, load_script

DESK = PKG / "plinius-desktop"


@pytest.fixture(scope="module")
def common():
    # Imported under its real name: the session and the menus "import desk_common"
    return load_script(DESK / "desk_common.py", "desk_common")


@pytest.fixture
def desk(common, tmp_path, monkeypatch, fake_bin):
    """desk_common with every path in tmp_path and only stub programs in PATH."""
    runtime = tmp_path / "run"
    runtime.mkdir()
    conf = tmp_path / "config" / "plinius-desktop"
    monkeypatch.setattr(common, "DIR", str(conf))
    monkeypatch.setattr(common, "SETTINGS", str(conf / "settings.ini"))
    monkeypatch.setattr(common, "RUNTIME_DIR", str(runtime / "plinius-desktop"))
    monkeypatch.setattr(common, "CTL_SOCKET", str(runtime / "plinius-desktop" / "wayvnc.sock"))
    monkeypatch.setattr(common, "OUTPUT_FILE", str(runtime / "plinius-desktop" / "output"))
    monkeypatch.setattr(common, "CLIENT_FILE", str(runtime / "plinius-desktop" / "client"))
    (runtime / "plinius-desktop").mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("PATH", fake_bin.env(isolated=True)["PATH"])
    common.fake_bin = fake_bin
    common.runtime = runtime
    return common


# ----- settings.ini -----

def test_load_gives_defaults_and_the_phone_keyboard(desk, monkeypatch):
    monkeypatch.setattr(desk, "system_keyboard", lambda: "de-nodeadkeys")
    assert desk.load() == {"resolution": "1920x1080", "scale": "1.5", "sound": "phone",
                           "keyboard": "de-nodeadkeys"}


def test_save_and_load_round_trip(desk, monkeypatch):
    monkeypatch.setattr(desk, "system_keyboard", lambda: pytest.fail("keyboard is stored"))
    values = {"resolution": "2560x1440", "scale": "2", "sound": "desktop", "keyboard": "fr"}
    desk.save(values)
    assert os.path.isfile(desk.SETTINGS)
    assert desk.load() == values


def test_load_keeps_defaults_for_missing_keys(desk, monkeypatch):
    os.makedirs(desk.DIR)
    with open(desk.SETTINGS, "w") as f:
        f.write("[desktop]\nscale = 1.25\nkeyboard = ch\n")
    values = desk.load()
    assert values["scale"] == "1.25" and values["resolution"] == "1920x1080"
    assert values["keyboard"] == "ch"


def test_load_ignores_other_sections(desk, monkeypatch):
    monkeypatch.setattr(desk, "system_keyboard", lambda: "us")
    os.makedirs(desk.DIR)
    with open(desk.SETTINGS, "w") as f:
        f.write("[other]\nscale = 3\n")
    assert desk.load()["scale"] == "1.5"


class FakeSettings:
    values = {}

    def __init__(self, schema):
        self.schema = schema

    @classmethod
    def new(cls, schema):
        if schema not in cls.values:
            raise RuntimeError("no schema")
        return cls(schema)

    def get_value(self, key):
        return self.values[self.schema][key]

    def get_string(self, key):
        return self.values[self.schema][key]


def test_system_keyboard_takes_the_first_xkb_layout(desk, monkeypatch):
    FakeSettings.values = {"org.gnome.desktop.input-sources": {"sources": GLib.Variant(
        "a(ss)", [("ibus", "anthy"), ("xkb", "de+nodeadkeys"), ("xkb", "us")])}}
    monkeypatch.setattr(Gio, "Settings", FakeSettings)
    assert desk.system_keyboard() == "de-nodeadkeys"


@pytest.mark.parametrize("values", [
    {"org.gnome.desktop.input-sources": {"sources": GLib.Variant("a(ss)", [])}},
    {},  # Settings.new fails
])
def test_system_keyboard_falls_back_to_us(desk, monkeypatch, values):
    FakeSettings.values = values
    monkeypatch.setattr(Gio, "Settings", FakeSettings)
    assert desk.system_keyboard() == "us"


class FakeSchemaSource:
    known = True

    @classmethod
    def get_default(cls):
        return cls()

    def lookup(self, schema, recursive):
        return object() if self.known else None


@pytest.mark.parametrize("known, stored, expected", [
    (True, "bottom", "bottom"), (True, "top", "top"), (True, "left", "top"),
    (False, "bottom", "top")])
def test_bar_position(desk, monkeypatch, known, stored, expected):
    FakeSchemaSource.known = known
    FakeSettings.values = {"org.plinius.desktop": {"bar-position": stored}}
    monkeypatch.setattr(Gio, "SettingsSchemaSource", FakeSchemaSource)
    monkeypatch.setattr(Gio, "Settings", FakeSettings)
    assert desk.bar_position() == expected


def test_anchor_to_bar_uses_the_bars_edge(desk, monkeypatch):
    shell = fakes.namespace("GtkLayerShell")
    shell.calls = []
    shell.set_anchor = staticmethod(lambda w, e, on: shell.calls.append(("anchor", e, on)))
    shell.set_margin = staticmethod(lambda w, e, m: shell.calls.append(("margin", e, m)))
    import gi.repository
    monkeypatch.setattr(gi.repository, "GtkLayerShell", shell, raising=False)
    monkeypatch.setattr(desk, "bar_position", lambda: "bottom")
    desk.anchor_to_bar("window", shell.Edge.LEFT)
    assert shell.calls == [("anchor", shell.Edge.LEFT, True), ("anchor", shell.Edge.BOTTOM, True),
                           ("margin", shell.Edge.LEFT, 8), ("margin", shell.Edge.BOTTOM, 6)]


# ----- virtual screen -----

def test_virtual_output(desk):
    assert desk.virtual_output() is None
    with open(desk.OUTPUT_FILE, "w") as f:
        f.write("")
    assert desk.virtual_output() is None
    with open(desk.OUTPUT_FILE, "w") as f:
        f.write("HEADLESS-1\n")
    assert desk.virtual_output() == "HEADLESS-1"


def test_set_resolution_only_with_a_virtual_screen(desk):
    desk.fake_bin.stub("wlr-randr")
    desk.set_resolution("1280x720")
    assert desk.fake_bin.calls("wlr-randr") == []
    with open(desk.OUTPUT_FILE, "w") as f:
        f.write("HEADLESS-2")
    desk.set_resolution("1280x720")
    assert desk.fake_bin.calls("wlr-randr") == [["--output", "HEADLESS-2", "--custom-mode",
                                                 "1280x720"]]


WLR_RANDR = """\
HWCOMPOSER-1 "Unknown Unknown (HWCOMPOSER-1)"
  Physical size: 68x151 mm
  Enabled: yes
  Modes:
    1080x2400 px, 60.000000 Hz (preferred)
  Position: 0,0
  Transform: 90
  Scale: 2.000000
HEADLESS-1 "Headless output 1"
  Enabled: yes
  Modes:
    3840x2160 px (current)
    1920x1080 px, 60.000000 Hz
  Position: 1100,0
  Transform: normal
  Scale: 1.500000
HEADLESS-2 "Headless output 2"
  Enabled: no
"""


def test_outputs_parses_wlr_randr(desk):
    desk.fake_bin.stub("wlr-randr", f"cat <<'EOF'\n{WLR_RANDR}EOF")
    out = desk.outputs()
    assert out["HWCOMPOSER-1"] == {"enabled": True, "position": "0,0", "mode": None,
                                   "scale": 2.0}
    assert out["HEADLESS-1"] == {"enabled": True, "position": "1100,0", "mode": "3840x2160",
                                 "scale": 1.5}
    assert out["HEADLESS-2"] == {"enabled": False, "position": None, "mode": None, "scale": None}


def test_outputs_without_wlr_randr_output(desk):
    desk.fake_bin.stub("wlr-randr", "exit 1")
    assert desk.outputs() == {}


class FakeMonitor:
    def __init__(self, mm):
        self.mm = mm

    def get_width_mm(self):
        return self.mm


class FakeDisplay:
    def __init__(self, *mms):
        self.monitors = [FakeMonitor(m) for m in mms]

    def get_n_monitors(self):
        return len(self.monitors)

    def get_monitor(self, i):
        return self.monitors[i]


def test_virtual_monitor_is_the_one_without_a_size(desk):
    display = FakeDisplay(68, 0)
    assert desk.virtual_monitor(display) is display.monitors[1]
    assert desk.virtual_monitor(FakeDisplay(68)) is None


# ----- viewer and sound -----

@pytest.mark.parametrize("address, plain", [
    ("::ffff:10.15.19.111", "10.15.19.111"), ("192.168.1.2", "192.168.1.2"),
    ("fe80::1", "fe80::1"), (None, None), ("", "")])
def test_plain_address(desk, address, plain):
    assert desk.plain_address(address) == plain


@pytest.mark.parametrize("reply, address", [
    ('[{"id": 1, "address": "::ffff:10.0.0.7"}, {"address": "10.0.0.8"}]', "10.0.0.7"),
    ("[]", None),
    ("Failed to connect", None),
])
def test_client_address(desk, reply, address):
    desk.fake_bin.stub("wayvncctl", f"echo '{reply}'")
    assert desk.client_address() == address
    assert desk.fake_bin.calls("wayvncctl") == [["-S", desk.CTL_SOCKET, "-j", "client-list"]]


def pactl_stub(desk, sinks="0\thal_output\tmodule\n", inputs="", modules=""):
    desk.fake_bin.stub("pactl", f"""
case "$*" in
"list sinks short") printf '{sinks}' ;;
"list sink-inputs short") printf '{inputs}' ;;
"list modules short") printf '{modules}' ;;
esac
exit 0""")


def pactl_calls(desk):
    return [c for c in desk.fake_bin.calls("pactl") if c[0] != "list"]


def test_sound_to_the_phone_moves_every_stream(desk):
    pactl_stub(desk, inputs="12\t0\t3\tPipeWire\n15\t0\t4\tPipeWire\n")
    assert desk.set_sound("phone") is True
    assert pactl_calls(desk) == [["set-default-sink", "hal_output"],
                                 ["move-sink-input", "12", "hal_output"],
                                 ["move-sink-input", "15", "hal_output"]]


def test_sound_to_the_desktop_opens_a_tunnel_to_the_viewer(desk):
    pactl_stub(desk, inputs="7\t0\t3\tPipeWire\n")
    desk.fake_bin.stub("wayvncctl", """echo '[{"address": "::ffff:192.168.5.20"}]'""")
    assert desk.set_sound("desktop") is True
    assert pactl_calls(desk) == [
        ["load-module", "module-tunnel-sink", "server=tcp:192.168.5.20:4713",
         "sink_name=plinius_desktop", "sink_properties=device.description=Desktop"],
        ["set-default-sink", "plinius_desktop"],
        ["move-sink-input", "7", "plinius_desktop"]]


def test_sound_to_the_desktop_reuses_the_tunnel(desk):
    pactl_stub(desk, sinks="0\thal_output\n5\tplinius_desktop\n")
    desk.fake_bin.stub("wayvncctl", "exit 1")
    assert desk.set_sound("desktop") is True
    assert desk.fake_bin.calls("wayvncctl") == []
    assert pactl_calls(desk) == [["set-default-sink", "plinius_desktop"]]


def test_sound_to_the_desktop_without_a_viewer_changes_nothing(desk):
    pactl_stub(desk)
    desk.fake_bin.stub("wayvncctl", "echo '[]'")
    assert desk.set_sound("desktop") is False
    assert pactl_calls(desk) == []


def test_drop_desktop_sound_unloads_only_our_tunnel(desk):
    pactl_stub(desk, modules=(
        "3\tmodule-tunnel-sink\tserver=tcp:10.0.0.9:4713 sink_name=other\\n"
        "9\tmodule-tunnel-sink\tserver=tcp:10.0.0.7:4713 sink_name=plinius_desktop\\n"
        "11\tmodule-null-sink\tsink_name=plinius_desktop_x\\n"))
    desk.drop_desktop_sound()
    assert pactl_calls(desk) == [["set-default-sink", "hal_output"], ["unload-module", "9"]]


# ----- closing the menus -----

@pytest.fixture
def children():
    procs = []

    def spawn(*marker):
        # A process of our own whose command line carries the marker, like a menu would
        p = spawn_marked(*marker)
        procs.append(p)
        return p
    yield spawn
    for p in procs:
        if p.poll() is None:
            p.kill()
        p.wait()


def write_pid(desk, name, pid):
    with open(os.path.join(desk.runtime, name), "w") as f:
        f.write(f"{pid}\n")


def test_close_other_menus_ends_only_other_menus(desk, children):
    apps = children("desk-apps.py")
    settings = children("desk-settings.py")
    stranger = children("no-menu")
    write_pid(desk, "plinius-desk-apps.pid", apps.pid)
    write_pid(desk, "plinius-desk-settings.pid", settings.pid)
    # A stale pid file that now names an unrelated process must not end it
    write_pid(desk, "plinius-desk-network.pid", stranger.pid)
    with open(os.path.join(desk.runtime, "plinius-desk-battery.pid"), "w") as f:
        f.write("garbage")
    desk.close_other_menus("plinius-desk-settings.pid")
    assert apps.wait(timeout=10) == -15
    assert settings.poll() is None and stranger.poll() is None


def test_close_other_menus_only_hides_the_resident_app_menu(desk, children):
    resident = children("desk-apps.py", "--resident")
    write_pid(desk, desk.RESIDENT_APPS_PIDFILE, resident.pid)
    # Its own opening leaves it alone
    desk.close_other_menus("plinius-desk-apps.pid")
    assert resident.poll() is None
    # Another menu opening hides it: SIGUSR2, not the end
    desk.close_other_menus("plinius-desk-settings.pid")
    assert resident.wait(timeout=10) == -12


def test_close_other_menus_leaves_a_one_shot_app_menu_in_the_resident_file(desk, children):
    # A stale resident pid file naming a menu without --resident must not reach it
    apps = children("desk-apps.py")
    write_pid(desk, desk.RESIDENT_APPS_PIDFILE, apps.pid)
    desk.close_other_menus("plinius-desk-apps.pid")
    assert apps.poll() is None


# ----- the app button -----

def run_toggle(desk, env_extra=None):
    menu = desk.fake_bin.stub("desk-apps.py", "echo started")
    env = desk.fake_bin.env(isolated=True, XDG_RUNTIME_DIR=str(desk.runtime),
                            DESK_APPS=str(menu), **(env_extra or {}))
    return subprocess.run([str(DESK / "desk-apps-toggle")], env=env, capture_output=True,
                          text=True, timeout=30)


def test_toggle_signals_the_resident_menu(desk, children):
    resident = children("desk-apps.py", "--resident")
    write_pid(desk, desk.RESIDENT_APPS_PIDFILE, resident.pid)
    result = run_toggle(desk)
    assert result.returncode == 0 and result.stdout == ""
    assert resident.wait(timeout=10) == -10
    assert desk.fake_bin.calls("desk-apps.py") == []


@pytest.mark.parametrize("pidfile", [None, "garbage", "stranger", "gone"])
def test_toggle_starts_the_menu_without_a_resident(desk, children, pidfile):
    if pidfile == "stranger":
        stranger = children("no-menu")
        write_pid(desk, desk.RESIDENT_APPS_PIDFILE, stranger.pid)
    elif pidfile == "gone":
        gone = children("desk-apps.py", "--resident")
        gone.kill()
        gone.wait()
        write_pid(desk, desk.RESIDENT_APPS_PIDFILE, gone.pid)
    elif pidfile:
        with open(os.path.join(desk.runtime, desk.RESIDENT_APPS_PIDFILE), "w") as f:
            f.write(pidfile)
    result = run_toggle(desk)
    assert result.returncode == 0 and result.stdout == "started\n"
    assert desk.fake_bin.calls("desk-apps.py") == [[]]
    if pidfile == "stranger":
        assert stranger.poll() is None


def test_close_all_menus(desk, children):
    battery = children("desk-battery.py")
    write_pid(desk, "plinius-desk-battery.pid", battery.pid)
    desk.close_all_menus()
    assert battery.wait(timeout=10) == -15


# ----- the lock screen -----

class FakeBus:
    def __init__(self, active=None):
        self.active = active
        self.subscriptions = []

    def call_sync(self, name, path, iface, method, params, reply, flags, timeout, cancel):
        assert (name, method) == ("org.gnome.ScreenSaver", "GetActive")
        if self.active is None:
            raise GLib.Error("no screensaver")
        return GLib.Variant("(b)", (self.active,))

    def signal_subscribe(self, *args):
        self.subscriptions.append(args)
        return 1


@pytest.mark.parametrize("active, result", [(True, False), (False, True), (None, True)])
def test_close_when_locked(desk, monkeypatch, active, result):
    bus = FakeBus(active)
    monkeypatch.setattr(Gio, "bus_get_sync", lambda *a: bus)
    quit = fakes.recorder()
    assert desk.close_when_locked(quit) is result
    # Subscribed in every case: the resident app menu outlives a lock at its start
    (sender, iface, signal, path, _arg0, _flags, callback), = bus.subscriptions
    assert (iface, signal) == ("org.gnome.ScreenSaver", "ActiveChanged")
    callback(bus, ":1.2", path, iface, signal, GLib.Variant("(b)", (False,)))
    assert quit.calls == []
    callback(bus, ":1.2", path, iface, signal, GLib.Variant("(b)", (True,)))
    assert quit.calls == [()]


@pytest.mark.parametrize("active, locked", [(True, True), (False, False), (None, False)])
def test_screen_locked(desk, active, locked):
    assert desk.screen_locked(FakeBus(active)) is locked


# ----- programs started from the desktop: a scope of their own -----

@pytest.mark.parametrize("app_id, unit", [
    ("org.mozilla.firefox.desktop", "app-desktop-org.mozilla.firefox-4242.scope"),
    ("gnome-control-center", "app-desktop-gnome\\x2dcontrol\\x2dcenter-4242.scope"),
    ("my app/ä.desktop", "app-desktop-my\\x20app\\x2f\\xc3\\xa4-4242.scope"),
    (".desktop", "app-desktop-app-4242.scope"),
])
def test_scope_name_escapes_like_systemd(desk, app_id, unit):
    assert desk.scope_name(app_id, 4242) == unit


def test_a_program_starts_in_its_scope_before_it_runs(desk, tmp_path):
    """Attack review 2026-11-22, V2: moved into its scope only after it was started, a program
    left the children it made before in plinius-desktop.service (a user service, allowed
    everything by plinius-appaccess). systemd-run --scope runs it once it is in the scope."""
    ran = tmp_path / "ran"
    # systemd-run as it is: the program only after the scope is there (the stub: after its log)
    desk.fake_bin.stub("systemd-run", 'while [ "$1" != -- ]; do shift; done; shift; exec "$@"')
    desk.fake_bin.stub("firefox", f'printf "%s %s" "$*" "$DESK_TEST" > "{ran}"')
    env = dict(os.environ, DESK_TEST="token")
    p = desk.start_in_scope("org.mozilla.firefox.desktop", ["firefox", "--new-window"], env)
    assert p.wait(timeout=10) == 0
    (call,) = desk.fake_bin.calls("systemd-run")
    unit = call[4]
    assert call[:4] == ["--user", "--scope", "--collect", "--quiet"]
    assert call[5:] == ["--", "firefox", "--new-window"]
    prefix = f"--unit=app-desktop-org.mozilla.firefox-{os.getpid()}"
    assert unit.startswith(prefix) and unit.endswith(".scope")
    # Only digits after the app id: plinius-appaccess and plinius-netlog read it up to the last dash
    assert unit[len("--unit=app-desktop-org.mozilla.firefox-"):-len(".scope")].isdigit()
    assert ran.read_text() == "--new-window token"
    # A second start gets a scope of its own
    desk.start_in_scope("org.mozilla.firefox.desktop", ["firefox"]).wait(timeout=10)
    assert desk.fake_bin.calls("systemd-run")[1][4] != unit


def test_a_program_that_cannot_be_started_only_says_so(desk, monkeypatch, capsys):
    monkeypatch.setenv("PATH", str(desk.fake_bin.path))   # no systemd-run at all
    assert desk.start_in_scope("gnome-control-center", ["gnome-control-center"]) is None
    assert "launch failed:" in capsys.readouterr().out
