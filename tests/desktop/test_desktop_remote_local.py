# plinius-remote --desktop: the touchpad and keyboard work the phone's own desktop on a TV or over
# VNC. The HID reports go to a virtual mouse and keyboard (uinput, faked here), the window is an
# ordinary one on the phone, the app starts like this while such a desktop is up and ends when
# the desktop's screen goes away. GTK and the screens are faked as in the other remote tests;
# nothing reaches the kernel, BlueZ or the compositor.

import types

import pytest
from gi.repository import GLib

from test_desktop_remote_more import Bus, app, glib, mod  # noqa: F401 (fixtures)


class FakeUInput:
    def __init__(self, caps, name=None):
        self.caps, self.name = caps, name
        self.events, self.closed = [], False

    def write(self, kind, code, value):
        self.events.append((kind, code, value))

    def syn(self):
        self.events.append("syn")

    def close(self):
        self.closed = True


def _ecodes():
    """The kernel's event codes (EV_*, KEY_*, BTN_*, REL_*), as python-evdev has them."""
    names = {}
    for line in open("/usr/include/linux/input-event-codes.h"):
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "#define" and parts[1].startswith(
                ("EV_", "KEY_", "BTN_", "REL_")):
            try:
                names[parts[1]] = int(parts[2], 0)
            except ValueError:
                if parts[2] in names:            # an alias of another code
                    names[parts[1]] = names[parts[2]]
    return types.SimpleNamespace(**names)


ECODES = _ecodes()


def fake_evdev():
    return types.SimpleNamespace(ecodes=ECODES, UInput=FakeUInput)


@pytest.fixture
def local(mod):  # noqa: F811
    return mod.LocalInput(fake_evdev())


def keys(events, e):
    return [(code, value) for ev in events if ev != "syn" for kind, code, value in [ev]
            if kind == e.EV_KEY]


# ----- the virtual device -----

def test_the_device_has_every_key_button_and_axis(mod, local):  # noqa: F811
    e = local.ecodes
    caps = local.device.caps
    assert local.device.name == "Plinius Remote Input"
    assert {e.KEY_A, e.KEY_LEFTSHIFT, e.BTN_LEFT, e.BTN_EXTRA, e.KEY_PLAYPAUSE,
            e.KEY_HOMEPAGE} <= set(caps[e.EV_KEY])
    assert 0 not in caps[e.EV_KEY]
    assert caps[e.EV_REL] == [e.REL_X, e.REL_Y, e.REL_WHEEL, e.REL_HWHEEL]


def test_the_table_is_the_kernels(mod):  # noqa: F811
    e = fake_evdev().ecodes
    t = mod.HID_TO_EVDEV
    assert len(t) == 256
    assert (t[0x04], t[0x1D], t[0x1E], t[0x28], t[0x2C], t[0x4F]) == (
        e.KEY_A, e.KEY_Z, e.KEY_1, e.KEY_ENTER, e.KEY_SPACE, e.KEY_RIGHT)
    assert [t[u] for u in mod.MODIFIER_USAGES] == [
        e.KEY_LEFTCTRL, e.KEY_LEFTSHIFT, e.KEY_LEFTALT, e.KEY_LEFTMETA,
        e.KEY_RIGHTCTRL, e.KEY_RIGHTSHIFT, e.KEY_RIGHTALT, e.KEY_RIGHTMETA]


def test_keyboard_reports_press_and_release(mod, local):  # noqa: F811
    e = local.ecodes
    # Shift + a, then a alone, then nothing (an unknown usage is skipped)
    local.send(mod.RID_KEYBOARD, bytes([mod.MOD_SHIFT, 0, 0x04, 0x03, 0, 0, 0, 0]))
    local.send(mod.RID_KEYBOARD, bytes([0, 0, 0x04, 0, 0, 0, 0, 0]))
    local.send(mod.RID_KEYBOARD, bytes(8))
    assert keys(local.device.events, e) == [
        (e.KEY_LEFTSHIFT, 1), (e.KEY_A, 1), (e.KEY_LEFTSHIFT, 0), (e.KEY_A, 0)]
    assert local.device.events.count("syn") == 3


def test_mouse_reports_move_scroll_and_click(mod, local):  # noqa: F811
    e = local.ecodes
    report = bytes([1]) + (5).to_bytes(2, "little", signed=True) \
        + (-3).to_bytes(2, "little", signed=True) + (2).to_bytes(1, "little", signed=True) \
        + (-1).to_bytes(1, "little", signed=True)
    local.send(mod.RID_MOUSE, report)
    local.send(mod.RID_MOUSE, bytes([4]) + bytes(6))      # left up, middle down, no motion
    ev = [x for x in local.device.events if x != "syn"]
    assert ev == [(e.EV_KEY, e.BTN_LEFT, 1), (e.EV_REL, e.REL_X, 5), (e.EV_REL, e.REL_Y, -3),
                  (e.EV_REL, e.REL_WHEEL, 2), (e.EV_REL, e.REL_HWHEEL, -1),
                  (e.EV_KEY, e.BTN_LEFT, 0), (e.EV_KEY, e.BTN_MIDDLE, 1)]


def test_media_keys(mod, local):  # noqa: F811
    e = local.ecodes
    local.send(mod.RID_CONSUMER, mod.CC_VOL_UP.to_bytes(2, "little"))
    local.send(mod.RID_CONSUMER, bytes(2))                 # the release report: nothing
    local.send(mod.RID_CONSUMER, (0x123).to_bytes(2, "little"))  # no Linux key: nothing
    assert local.device.events == [(e.EV_KEY, e.KEY_VOLUMEUP, 1), "syn",
                                   (e.EV_KEY, e.KEY_VOLUMEUP, 0), "syn"]


def test_the_pointer_goes_to_the_middle_of_the_tv(mod, local):  # noqa: F811
    e = local.ecodes
    local.pointer_to_tv()
    # The TV lies right of the phone: against the far edges, then half a TV back
    assert local.device.events == [(e.EV_REL, e.REL_X, 100000), (e.EV_REL, e.REL_Y, -100000),
                                   "syn", (e.EV_REL, e.REL_X, -960), (e.EV_REL, e.REL_Y, 540),
                                   "syn"]


def test_closing_lets_go_of_held_keys(mod, local):  # noqa: F811
    e = local.ecodes
    local.send(mod.RID_KEYBOARD, bytes([mod.MOD_CTRL, 0, 0, 0, 0, 0, 0, 0]))
    local.device.events.clear()
    local.close()
    assert local.device.events == [(e.EV_KEY, e.KEY_LEFTCTRL, 0), "syn"]
    assert local.device.closed and local.keys == set()


def test_the_real_evdev_is_imported_when_none_is_given(mod, monkeypatch):  # noqa: F811
    import sys
    monkeypatch.setitem(sys.modules, "evdev", fake_evdev())
    assert isinstance(mod.LocalInput().device, FakeUInput)


# ----- the app in desktop mode -----

class Monitor:
    def __init__(self, connector):
        self.connector = connector

    def get_connector(self):
        return self.connector


class Monitors:
    """Gdk's list of screens, with its items-changed signal."""

    def __init__(self, *connectors):
        self.items = [Monitor(c) for c in connectors]
        self.handlers, self.disconnected = [], []

    def get_n_items(self):
        return len(self.items)

    def get_item(self, i):
        return self.items[i]

    def connect(self, signal, handler):
        assert signal == "items-changed"
        self.handlers.append(handler)
        return 11

    def disconnect(self, handler_id):
        self.disconnected.append(handler_id)

    def change(self, *connectors):
        self.items = [Monitor(c) for c in connectors]
        for handler in self.handlers:
            handler(self, 0, 1, 1)


@pytest.fixture
def screens(mod, monkeypatch):  # noqa: F811
    """The phone's panel and plinius-desktop's virtual screen."""
    monitors = Monitors("DSI-1", "HEADLESS-1")
    display = types.SimpleNamespace(get_monitors=lambda: monitors)
    monkeypatch.setattr(mod.Gdk, "Display", types.SimpleNamespace(get_default=lambda: display),
                        raising=False)
    return monitors


@pytest.fixture
def desktop(mod, app, screens, monkeypatch):  # noqa: F811
    devices = []

    def make():
        dev = FakeLocal()
        devices.append(dev)
        return dev

    monkeypatch.setattr(mod, "LocalInput", make)
    app.quits = []
    monkeypatch.setattr(app, "quit", lambda: app.quits.append(True))
    app.toasts = []
    monkeypatch.setattr(app, "toast", lambda text: app.toasts.append(text))
    app.devices, app.screens = devices, screens
    return app


class FakeLocal:
    def __init__(self):
        self.sent, self.moved, self.closed = [], 0, False

    def send(self, rid, data):
        self.sent.append((rid, bytes(data)))

    def pointer_to_tv(self):
        self.moved += 1

    def close(self):
        self.closed = True


def start(app):  # noqa: F811
    line = types.SimpleNamespace(get_arguments=lambda: ["plinius-remote", "--desktop"])
    assert app.do_command_line(line) == 0


def fire_pointer(app):
    """The pointer goes to the TV a moment after the window is up."""
    (timer,) = [s for s in app.glib.of_kind("timeout")
                if app.glib.sources[s][2] == app.pointer_to_tv]
    assert app.glib.sources[timer][1] == 500
    assert app.glib.fire(timer) is False


def gone_timers(app):
    return [s for s in app.glib.of_kind("timeout")
            if app.glib.sources[s][2] == app.on_screen_gone]


def test_desktop_mode_is_a_window_that_feeds_the_virtual_devices(mod, desktop):  # noqa: F811
    desktop.props.active_window.props_["mapped"] = True
    start(desktop)
    (dev,) = desktop.devices
    assert dev.moved == 0                                   # not before the window is up
    fire_pointer(desktop)
    assert dev.moved == 1                                   # pointer onto the TV
    # An ordinary window: the phone's bars, overview and other apps stay usable beside it
    assert ("fullscreen", ()) not in desktop.props.active_window.calls
    assert desktop.switch.props_["sensitive"] is False      # Bluetooth stays off
    assert desktop.title.props_["subtitle"] == "Desktop"
    assert "pointer on the desktop" in desktop.pad_label.props_["label"]
    desktop.send_report(mod.RID_CONSUMER, b"\xe9\x00")
    assert dev.sent == [(mod.RID_CONSUMER, b"\xe9\x00")]
    # Bluetooth news does not overwrite it
    desktop.on_peripheral_changed()
    assert desktop.title.props_["subtitle"] == "Desktop"
    # A second start (the app's icon while it runs) keeps the one device and the one watch
    start(desktop)
    assert len(desktop.devices) == 1 and len(desktop.screens.handlers) == 1


def test_without_a_virtual_device_it_says_so(mod, desktop, monkeypatch):  # noqa: F811
    def fail():
        raise PermissionError("/dev/uinput")

    monkeypatch.setattr(mod, "LocalInput", fail)
    start(desktop)
    assert desktop.local is None and desktop.toasts
    assert desktop.screens.handlers == []


def test_the_app_ends_when_the_desktops_screen_goes(mod, desktop):  # noqa: F811
    start(desktop)
    desktop.screens.change("DSI-1", "HEADLESS-1")           # another screen came and went
    assert gone_timers(desktop) == []
    desktop.screens.change("DSI-1")                         # TV or VNC ended
    (timer,) = gone_timers(desktop)
    assert desktop.glib.sources[timer][1] == mod.SCREEN_GONE_MS
    desktop.screens.change()                                # still gone: one timer only
    assert gone_timers(desktop) == [timer] and desktop.quits == []
    desktop.screens.change("DSI-1")
    assert desktop.glib.fire(timer) is False
    assert desktop.quits == [True]


def test_a_screen_back_in_a_moment_keeps_the_app(mod, desktop):  # noqa: F811
    start(desktop)
    desktop.screens.change("DSI-1")
    (timer,) = gone_timers(desktop)
    desktop.screens.change("DSI-1", "HEADLESS-2")           # a new layout: back at once
    assert timer in desktop.glib.removed and desktop.screen_gone == 0
    # A timer that was already due runs anyway: the screen is there, the app stays
    assert desktop.on_screen_gone() is False and desktop.quits == []


def test_shutdown_closes_the_virtual_devices(mod, desktop, monkeypatch):  # noqa: F811
    start(desktop)
    (dev,) = desktop.devices
    desktop.screens.change("DSI-1")
    (timer,) = gone_timers(desktop)
    monkeypatch.setattr(mod.Adw.Application, "do_shutdown", lambda self: None, raising=False)
    desktop.peripheral = None
    desktop.do_shutdown()
    assert dev.closed and desktop.local is None
    assert desktop.screens.disconnected == [11] and desktop.monitors_handler == 0
    assert timer in desktop.glib.removed and desktop.screen_gone == 0


def test_the_pointer_waits_for_the_window(mod, desktop):  # noqa: F811
    win = desktop.props.active_window
    handlers = []
    win.connect = lambda signal, handler: handlers.append((signal, handler))
    start(desktop)
    ((signal, on_map),) = handlers
    assert signal == "map"
    on_map(win)
    fire_pointer(desktop)
    assert desktop.devices[0].moved == 1
    desktop.local = None
    assert desktop.pointer_to_tv() is False                 # gone by then: nothing


# ----- which start is the desktop's -----

def test_the_desktops_screen_is_any_but_the_phones_own(mod, screens):  # noqa: F811
    assert mod.desktop_screen().get_connector() == "HEADLESS-1"
    screens.items = [Monitor("DPI-1"), Monitor(None), Monitor(""), Monitor("DSI-1")]
    assert mod.desktop_screen() is None                     # no connector: not a desktop


def test_no_display_no_desktop(mod, monkeypatch):  # noqa: F811
    monkeypatch.setattr(mod.Gdk, "Display", types.SimpleNamespace(get_default=lambda: None),
                        raising=False)
    assert mod.desktop_screen() is None


@pytest.mark.parametrize("argv, init, screen, wanted", [
    (["plinius-remote", "--desktop"], None, None, True),    # Phosh's cast quick setting
    (["plinius-remote"], True, "HEADLESS-1", True),         # the app's icon while a desktop is up
    (["plinius-remote", "--touchpad"], True, "HEADLESS-1", True),
    (["plinius-remote"], True, None, False),                # no desktop: Bluetooth
    (["plinius-remote"], False, "HEADLESS-1", False),       # no display at all
])
def test_a_start_while_a_desktop_is_up_works_that_desktop(mod, monkeypatch, argv, init, screen,
                                                          wanted):  # noqa: F811
    inits = []
    monkeypatch.setattr(mod.Gtk, "init_check", lambda: inits.append(True) or init,
                        raising=False)
    monkeypatch.setattr(mod, "desktop_screen", lambda: screen and Monitor(screen))
    assert mod.wants_desktop(argv) is wanted
    assert inits == ([] if init is None else [True])


def test_main_starts_the_desktop_instance_while_a_desktop_is_up(mod, monkeypatch):  # noqa: F811
    monkeypatch.setattr(mod, "default_layout", lambda: "us")
    monkeypatch.setattr(mod, "wants_desktop", lambda argv: True)
    made = []
    real = mod.RemoteApp
    monkeypatch.setattr(mod, "RemoteApp", lambda **kw: made.append(real(**kw)) or made[-1])
    mod.main()
    (a,) = made
    assert a.app_props["application_id"] == "org.plinius.Remote.Desktop" and a.desktop_mode


def test_desktop_instance_is_its_own_app(mod, monkeypatch):  # noqa: F811
    monkeypatch.setattr(mod, "default_layout", lambda: "us")
    a = mod.RemoteApp(desktop=True)
    assert a.app_props["application_id"] == "org.plinius.Remote.Desktop" and a.desktop_mode


def test_the_desktop_window_has_a_hidden_entry_for_the_overview(mod):  # noqa: F811
    from conftest import PKG
    from gi.repository import GLib as RealGLib
    pkg = PKG / "plinius-remote"
    kf = RealGLib.KeyFile()
    kf.load_from_file(str(pkg / "org.plinius.Remote.Desktop.desktop"), RealGLib.KeyFileFlags.NONE)
    entry = "Desktop Entry"
    assert kf.get_boolean(entry, "NoDisplay") is True       # not in the app grid
    assert kf.get_string(entry, "Exec") == "plinius-remote --desktop"
    main = RealGLib.KeyFile()
    main.load_from_file(str(pkg / "org.plinius.Remote.desktop"), RealGLib.KeyFileFlags.NONE)
    for key in ("Name", "Icon"):
        assert kf.get_string(entry, key) == main.get_string(entry, key)
    pkgbuild = (pkg / "PKGBUILD").read_text()
    assert "install -Dm644 org.plinius.Remote.Desktop.desktop" in pkgbuild
    assert "gtk4-layer-shell" not in pkgbuild               # no layer surface any more


def test_no_own_acceleration_on_the_phones_desktop(mod, desktop):  # noqa: F811
    """phoc accelerates the virtual mouse itself; twice felt sluggish when moving slowly."""
    moves = []
    desktop.reports.move = lambda dx, dy: moves.append((dx, dy))
    desktop.speed = 1.5
    desktop.on_drag_begin(None, 0.0, 0.0)
    desktop.glib.advance_ms(10)
    desktop.on_drag_update(None, 40.0, 0.0)             # a quick swipe
    desktop.local = object()
    desktop.glib.advance_ms(10)
    desktop.on_drag_update(None, 80.0, 0.0)             # the same, on the desktop
    bluetooth, local = moves
    assert bluetooth[0] > 40.0 * 1.5                    # accelerated for a Bluetooth host
    assert local == (40.0 * 1.5, 0.0)                   # speed only on the phone's desktop
