# plinius-remote --desktop: the touchpad and keyboard work the phone's own desktop on a TV. The
# HID reports go to a virtual mouse and keyboard (uinput, faked here), the window is in full
# screen, and the app ends with the desktop on the TV (Schwupp's Source). GTK is faked as in the
# other remote tests; nothing reaches the kernel, BlueZ or Schwupp.

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

class Watch:
    def __init__(self):
        self.watched, self.unwatched = [], []

    def watch(self, bus_type, name, flags, appeared, vanished):
        self.watched.append((name, appeared, vanished))
        return 7

    def unwatch(self, watch_id):
        self.unwatched.append(watch_id)


@pytest.fixture
def desktop(mod, app, monkeypatch):  # noqa: F811
    watch = Watch()
    monkeypatch.setattr(mod.Gio, "bus_watch_name", watch.watch, raising=False)
    monkeypatch.setattr(mod.Gio, "bus_unwatch_name", watch.unwatch, raising=False)
    monkeypatch.setattr(mod, "LocalInput", lambda: mod.LocalInput.__new__(mod.LocalInput))
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
    app.watch, app.devices = watch, devices
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


def test_desktop_mode_goes_full_screen_and_feeds_the_virtual_devices(mod, desktop):  # noqa: F811
    desktop.props.active_window.props_["mapped"] = True
    start(desktop)
    (dev,) = desktop.devices
    assert dev.moved == 0                                   # not before the window is up
    fire_pointer(desktop)
    assert dev.moved == 1                                   # pointer onto the TV
    assert ("fullscreen", ()) in desktop.props.active_window.calls
    assert desktop.switch.props_["sensitive"] is False      # Bluetooth stays off
    assert desktop.title.props_["subtitle"] == "Desktop on the TV"
    assert "pointer on the TV" in desktop.pad_label.props_["label"]
    desktop.send_report(mod.RID_CONSUMER, b"\xe9\x00")
    assert dev.sent == [(mod.RID_CONSUMER, b"\xe9\x00")]
    # Bluetooth news does not overwrite it
    desktop.on_peripheral_changed()
    assert desktop.title.props_["subtitle"] == "Desktop on the TV"
    # A second start keeps the one device and the one watch
    start(desktop)
    assert len(desktop.devices) == 1 and len(desktop.watch.watched) == 1


def test_without_a_virtual_device_it_says_so(mod, desktop, monkeypatch):  # noqa: F811
    def fail():
        raise PermissionError("/dev/uinput")

    monkeypatch.setattr(mod, "LocalInput", fail)
    start(desktop)
    assert desktop.local is None and desktop.toasts
    assert ("fullscreen", ()) not in desktop.props.active_window.calls


class SessionBus:
    def __init__(self):
        self.subscribed = []

    def signal_subscribe(self, *args):
        self.subscribed.append(args)
        return 3


def changed(**props):
    return GLib.Variant("(sa{sv}as)", ("de.cais.Schwupp.Cast1",
                                       {k: GLib.Variant("s", v) for k, v in props.items()}, []))


def test_the_app_ends_with_the_desktop_on_the_tv(mod, desktop):  # noqa: F811
    start(desktop)
    (name, appeared, vanished), = desktop.watch.watched
    assert name == "de.cais.Schwupp"
    bus = SessionBus()
    appeared(bus, name, ":1.9")
    (sub,) = bus.subscribed
    assert sub[:5] == ("de.cais.Schwupp", "org.freedesktop.DBus.Properties",
                       "PropertiesChanged", "/de/cais/Schwupp/Cast", "de.cais.Schwupp.Cast1")
    on_changed = sub[6]
    on_changed(None, ":1.9", None, None, None, changed(State="streaming"))
    on_changed(None, ":1.9", None, None, None, changed(Source="desktop"))
    assert desktop.quits == []
    on_changed(None, ":1.9", None, None, None, changed(Source="screen"))   # switched or stopped
    assert desktop.quits == [True]


def test_the_app_ends_when_schwupp_goes(mod, desktop):  # noqa: F811
    start(desktop)
    (_name, _appeared, vanished), = desktop.watch.watched
    vanished(None, "de.cais.Schwupp")
    assert desktop.quits == [True]
    desktop.local = None
    vanished(None, "de.cais.Schwupp")                       # not in desktop mode: stays
    assert desktop.quits == [True]


def test_shutdown_closes_the_virtual_devices(mod, desktop, monkeypatch):  # noqa: F811
    start(desktop)
    (dev,) = desktop.devices
    monkeypatch.setattr(mod.Adw.Application, "do_shutdown", lambda self: None, raising=False)
    desktop.peripheral = None
    desktop.do_shutdown()
    assert dev.closed and desktop.local is None
    assert desktop.watch.unwatched == [7] and desktop.schwupp_watch == 0


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


# ----- the layer surface and the preload -----

class FakeLayer:
    class Layer:
        OVERLAY = "overlay"

    class KeyboardMode:
        NONE = "none"

    class Edge:
        TOP, BOTTOM, LEFT, RIGHT = "top", "bottom", "left", "right"

    supported = True

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        if name == "is_supported":
            return lambda: FakeLayer.supported
        return lambda *args: self.calls.append((name, args[1:]))


@pytest.fixture
def layer(mod, monkeypatch):  # noqa: F811
    fake = FakeLayer()
    FakeLayer.supported = True
    import gi
    from gi import repository
    monkeypatch.setattr(gi, "require_version", lambda *a: None)
    monkeypatch.setattr(repository, "Gtk4LayerShell", fake, raising=False)
    import sys
    monkeypatch.setitem(sys.modules, "gi.repository.Gtk4LayerShell", fake)
    return fake


def test_desktop_mode_is_a_layer_without_keyboard_focus(mod, app, layer, monkeypatch):  # noqa: F811
    monkeypatch.setattr(mod, "phone_monitor", lambda: "DSI-1")
    win = app.props.active_window
    assert app.as_layer(win) is True and app.layered
    calls = dict((name, args) for name, args in layer.calls if name != "set_anchor")
    assert calls["set_keyboard_mode"] == ("none",)          # typing goes to the TV window
    assert calls["set_layer"] == ("overlay",) and calls["set_monitor"] == ("DSI-1",)
    assert calls["set_namespace"] == ("plinius-remote",)
    assert sorted(a[0] for n, a in layer.calls if n == "set_anchor") == [
        "bottom", "left", "right", "top"]


def test_layer_without_support_or_library(mod, app, layer, monkeypatch):  # noqa: F811
    monkeypatch.setattr(mod, "phone_monitor", lambda: None)
    win = app.props.active_window
    FakeLayer.supported = False
    assert app.as_layer(win) is False
    FakeLayer.supported = True
    assert app.as_layer(win) is True                        # no phone screen known: any
    assert "set_monitor" not in [n for n, _a in layer.calls]
    import gi

    def missing(*a):
        raise ValueError("Namespace Gtk4LayerShell not available")

    monkeypatch.setattr(gi, "require_version", missing)
    assert app.as_layer(win) is False


def test_the_phone_monitor_is_the_built_in_one(mod, monkeypatch):  # noqa: F811
    class Mon:
        def __init__(self, connector):
            self.connector = connector

        def get_connector(self):
            return self.connector

    class Monitors:
        items = [Mon("HEADLESS-3"), Mon(None), Mon("DSI-1")]

        def get_n_items(self):
            return len(self.items)

        def get_item(self, i):
            return self.items[i]

    display = types.SimpleNamespace(get_monitors=lambda: Monitors())
    monkeypatch.setattr(mod.Gdk, "Display", types.SimpleNamespace(get_default=lambda: display),
                        raising=False)
    assert mod.phone_monitor().get_connector() == "DSI-1"
    Monitors.items = Monitors.items[:2]
    assert mod.phone_monitor() is None


def test_desktop_starts_again_with_the_layer_library(mod, tmp_path, monkeypatch):  # noqa: F811
    lib = tmp_path / "libgtk4-layer-shell.so"
    lib.write_text("")
    monkeypatch.setattr(mod, "LAYER_SHELL_LIB", str(lib))
    calls = []

    def execv(path, argv, env):
        calls.append((argv[2:], env["LD_PRELOAD"]))

    mod.preload_layer_shell(["/usr/bin/plinius-remote", "--desktop"], {"LD_PRELOAD": "x.so"},
                            execv)
    assert calls == [(["--desktop"], f"{lib}:x.so")]
    # Once preloaded, or without --desktop, or without the library: no restart
    mod.preload_layer_shell(["p", "--desktop"], {"LD_PRELOAD": str(lib)}, execv)
    mod.preload_layer_shell(["p", "--keyboard"], {}, execv)
    monkeypatch.setattr(mod, "LAYER_SHELL_LIB", str(tmp_path / "missing.so"))
    mod.preload_layer_shell(["p", "--desktop"], {}, execv)
    assert len(calls) == 1


def test_a_layer_needs_no_full_screen(mod, desktop):  # noqa: F811
    desktop.layered = True
    desktop.props.active_window.props_["mapped"] = True
    start(desktop)
    assert ("fullscreen", ()) not in desktop.props.active_window.calls


def test_desktop_instance_is_its_own_app(mod, monkeypatch):  # noqa: F811
    monkeypatch.setattr(mod, "default_layout", lambda: "us")
    a = mod.RemoteApp(desktop=True)
    assert a.app_props["application_id"] == "org.plinius.Remote.Desktop" and a.desktop_mode
