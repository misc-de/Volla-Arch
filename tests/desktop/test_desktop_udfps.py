# plinius-udfps: panel size from DRM, the brightness boost through logind, where the circle goes,
# the finger's press/release protocol towards biomd, the drawing, and following biomd. GTK is
# faked; drawing goes to a real cairo image surface.

import types

import cairo
import pytest
from gi.repository import GLib

import desktop_fakes as fakes
from conftest import PKG
from desktop_mainrun import run_as_main

UDFPS = PKG / "plinius-udfps" / "plinius-udfps.py"


@pytest.fixture(scope="module")
def mod():
    return fakes.load_gui(UDFPS, "plinius_udfps_under_test")


@pytest.fixture
def glib(mod, monkeypatch):
    fake = fakes.FakeGLib()
    monkeypatch.setattr(mod, "GLib", fake)
    return fake


# ----- display_mode -----

def drm(tmp_path, cards):
    for name, (status, modes) in cards.items():
        d = tmp_path / "drm" / name
        d.mkdir(parents=True)
        (d / "status").write_text(status + "\n")
        if modes is not None:
            (d / "modes").write_text(modes)


def redirect_glob(mod, monkeypatch, tmp_path):
    real = mod.glob.glob
    monkeypatch.setattr(mod, "glob", types.SimpleNamespace(glob=lambda pattern: real(
        pattern.replace("/sys/class/drm", str(tmp_path / "drm")).replace(
            "/sys/class/backlight", str(tmp_path / "backlight")))))


def test_display_mode_of_the_connected_panel(mod, tmp_path, monkeypatch):
    drm(tmp_path, {"card0-Writeback-1": ("disconnected", "1024x768\n"),
                   "card0-DSI-1": ("connected", "1080x2400\n720x1600\n")})
    redirect_glob(mod, monkeypatch, tmp_path)
    assert mod.display_mode() == (1080, 2400)


def test_display_mode_skips_unreadable_panels(mod, tmp_path, monkeypatch):
    drm(tmp_path, {"card0-DSI-1": ("connected", None), "card0-DSI-2": ("connected", "junk\n"),
                   "card0-DSI-3": ("connected", "1080x2340\n")})
    redirect_glob(mod, monkeypatch, tmp_path)
    assert mod.display_mode() == (1080, 2340)


def test_display_mode_none(mod, tmp_path, monkeypatch):
    drm(tmp_path, {"card0-DSI-1": ("disconnected", "1080x2400\n")})
    redirect_glob(mod, monkeypatch, tmp_path)
    assert mod.display_mode() is None


# ----- Backlight -----

class SystemBus:
    def __init__(self):
        self.calls = []

    def call(self, name, path, iface, method, params, *rest):
        self.calls.append((name, path, iface, method, params.unpack()))


@pytest.fixture
def backlight_dir(mod, tmp_path, monkeypatch):
    d = tmp_path / "backlight" / "panel0-backlight"
    d.mkdir(parents=True)
    (d / "brightness").write_text("812\n")
    (d / "max_brightness").write_text("2047\n")
    monkeypatch.setattr(mod, "BACKLIGHT_DIR", str(tmp_path / "backlight"))
    return d


def test_boost_and_restore(mod, backlight_dir):
    bus = SystemBus()
    light = mod.Backlight(bus)
    assert light.name == "panel0-backlight"
    light.boost()
    light.boost()  # a second press while boosted keeps the saved level
    light.restore()
    light.restore()
    target = ("org.freedesktop.login1", "/org/freedesktop/login1/session/auto",
              "org.freedesktop.login1.Session", "SetBrightness")
    assert bus.calls == [target + (("backlight", "panel0-backlight", 2047),),
                         target + (("backlight", "panel0-backlight", 812),)]


def test_boost_without_a_readable_level(mod, backlight_dir):
    (backlight_dir / "brightness").write_text("n/a\n")
    bus = SystemBus()
    light = mod.Backlight(bus)
    light.boost()
    light.restore()
    assert bus.calls == []


def test_no_backlight(mod, tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "BACKLIGHT_DIR", str(tmp_path / "none"))
    bus = SystemBus()
    light = mod.Backlight(bus)
    light.boost()
    assert light.name is None and bus.calls == []


# ----- the target -----

class Shell:
    """GtkLayerShell that records margins and the monitor."""

    class Edge:
        LEFT, BOTTOM = "left", "bottom"

    def __init__(self):
        self.margins = {}
        self.monitor = None

    def set_monitor(self, window, monitor):
        self.monitor = monitor

    def set_margin(self, window, edge, value):
        self.margins[edge] = value


class Geometry:
    def __init__(self, width, height):
        self.width, self.height = width, height


class Monitor:
    def __init__(self, width_mm, height_mm, width=540, height=1200):
        self.mm = (width_mm, height_mm)
        self.geometry = Geometry(width, height)

    def get_width_mm(self):
        return self.mm[0]

    def get_height_mm(self):
        return self.mm[1]

    def get_geometry(self):
        return self.geometry


class Display:
    def __init__(self, monitors, primary=None):
        self.monitors, self.primary = monitors, primary

    def get_n_monitors(self):
        return len(self.monitors)

    def get_monitor(self, i):
        return self.monitors[i] if i < len(self.monitors) else None

    def get_primary_monitor(self):
        return self.primary


class Area:
    def __init__(self, width=0):
        self.size = None
        self.draws = 0
        self.width = width
        self.clock = None

    def set_size_request(self, w, h):
        self.size = (w, h)

    def queue_draw(self):
        self.draws += 1

    def get_frame_clock(self):
        return self.clock

    def get_allocated_width(self):
        return self.width


class Proxy:
    def __init__(self):
        self.calls = []

    def call(self, method, params, flags, timeout, cancel, done):
        self.calls.append((method, params.unpack() if params is not None else None))


def target(mod, monkeypatch, monitors=None, x=540, y=2100, r=90, panel=(1080, 2400)):
    shell = Shell()
    monkeypatch.setattr(mod, "GtkLayerShell", shell)
    t = mod.Target.__new__(mod.Target)
    t.fp = Proxy()
    t.backlight = types.SimpleNamespace(boosts=0, restores=0)
    t.backlight.boost = lambda: setattr(t.backlight, "boosts", t.backlight.boosts + 1)
    t.backlight.restore = lambda: setattr(t.backlight, "restores", t.backlight.restores + 1)
    t.sx, t.sy, t.sr = x, y, r
    t.panel_w, t.panel_h = panel
    t.state = mod.STATE_IDLE
    t.progress = 0
    t.pressed = False
    t.ui_ready_source = 0
    t.area = Area()
    t.visible = None
    t.hide = lambda: setattr(t, "visible", False)
    t.show_all = lambda: setattr(t, "visible", True)
    display = Display(monitors if monitors is not None else [Monitor(0, 0, 1920, 1080),
                                                             Monitor(68, 151)])
    t.get_display = lambda: display
    t.shell = shell
    return t


def test_place_in_logical_pixels_on_the_phones_panel(mod, monkeypatch):
    t = target(mod, monkeypatch)
    assert mod.Target.place(t) is True
    # Panel 1080 px shown 540 wide: ratio 2; circle 180 px -> 90, left edge (540-90)/2,
    # bottom (2400-2100-90)/2
    assert t.area.size == (90, 90)
    assert t.shell.margins == {"left": 225, "bottom": 105}
    assert t.shell.monitor is t.get_display().monitors[1]


def test_place_refuses_landscape(mod, monkeypatch):
    t = target(mod, monkeypatch, monitors=[Monitor(68, 151, 1200, 540)])
    assert mod.Target.place(t) is False
    assert t.area.size is None


def test_place_falls_back_to_the_primary_monitor(mod, monkeypatch):
    primary = Monitor(0, 0, 360, 800)
    t = target(mod, monkeypatch, monitors=[primary])
    t.get_display().primary = primary
    assert mod.Target.place(t) is True
    assert t.shell.monitor is primary and t.area.size == (60, 60)


def test_place_tiny_sensor_keeps_a_pixel(mod, monkeypatch):
    t = target(mod, monkeypatch, r=0.2)
    mod.Target.place(t)
    assert t.area.size == (1, 1)


def bind(mod, t):
    """The Target methods on the fake object, so they call each other."""
    for name in ("place", "set_state", "set_progress", "press", "release", "ui_ready", "call",
                 "on_touch", "on_button"):
        setattr(t, name, getattr(mod.Target, name).__get__(t))
    return t


def test_press_reports_down_then_ui_ready(mod, monkeypatch, glib):
    t = bind(mod, target(mod, monkeypatch))
    t.set_state(mod.STATE_IDENTIFYING)
    assert t.visible is True
    t.press()
    t.press()  # a second touch point changes nothing
    assert t.fp.calls == [("PointerDown", (540, 2100))]
    assert t.backlight.boosts == 1
    (sid,) = glib.of_kind("timeout")
    assert glib.sources[sid][1] == mod.UI_READY_DELAY_MS
    glib.fire(sid)
    assert t.fp.calls[-1] == ("UiReady", None) and t.ui_ready_source == 0
    t.release()
    t.release()
    assert t.fp.calls[-1] == ("PointerUp", None) and len(t.fp.calls) == 3
    assert t.backlight.restores == 1


def test_lift_before_ui_ready_cancels_it(mod, monkeypatch, glib):
    t = bind(mod, target(mod, monkeypatch))
    t.set_state(mod.STATE_ENROLLING)
    t.press()
    (sid,) = glib.of_kind("timeout")
    t.release()
    assert sid in glib.removed and glib.sources == {}
    assert [c[0] for c in t.fp.calls] == ["PointerDown", "PointerUp"]


def test_ui_ready_waits_for_the_painted_frame(mod, monkeypatch, glib):
    t = bind(mod, target(mod, monkeypatch))

    class Clock:
        def __init__(self):
            self.handlers = {}

        def connect(self, signal, func):
            self.handlers[signal] = func
            return 7

        def disconnect(self, handler):
            self.disconnected = handler
    t.area.clock = clock = Clock()
    t.set_state(mod.STATE_IDENTIFYING)
    t.press()
    assert glib.sources == {}
    clock.handlers["after-paint"](clock)
    assert clock.disconnected == 7 and len(glib.of_kind("timeout")) == 1


def test_no_press_while_idle(mod, monkeypatch, glib):
    t = bind(mod, target(mod, monkeypatch))
    t.press()
    assert t.fp.calls == [] and t.backlight.boosts == 0


def test_idle_lets_go_and_hides(mod, monkeypatch, glib):
    t = bind(mod, target(mod, monkeypatch))
    t.set_state(mod.STATE_ENROLLING)
    t.set_progress(40)
    t.press()
    t.set_state(mod.STATE_IDLE)
    assert t.visible is False and t.pressed is False
    assert t.fp.calls[-1] == ("PointerUp", None)
    # A new enrollment starts at zero
    t.set_state(mod.STATE_ENROLLING)
    assert t.progress == 0


def test_hidden_in_landscape(mod, monkeypatch, glib):
    t = bind(mod, target(mod, monkeypatch, monitors=[Monitor(68, 151, 1200, 540)]))
    t.set_state(mod.STATE_IDENTIFYING)
    assert t.visible is False


def test_touch_and_mouse_events(mod, monkeypatch, glib):
    t = bind(mod, target(mod, monkeypatch))
    t.set_state(mod.STATE_IDENTIFYING)
    E = mod.Gdk.EventType
    assert t.on_touch(None, types.SimpleNamespace(type=E.TOUCH_BEGIN)) is True
    assert t.pressed
    t.on_touch(None, types.SimpleNamespace(type=E.TOUCH_CANCEL))
    assert not t.pressed
    t.on_button(None, types.SimpleNamespace(type=E.BUTTON_PRESS))
    assert t.pressed
    t.on_button(None, types.SimpleNamespace(type=E.BUTTON_RELEASE))
    assert not t.pressed


# ----- drawing -----

def draw(mod, t, size=100):
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    # Something already on the surface: the target must clear around the circle
    cr = cairo.Context(surface)
    cr.set_source_rgba(1, 0, 0, 1)
    cr.paint()
    t.area.width = size
    mod.Target.on_draw(t, t.area, cr)
    surface.flush()
    data = surface.get_data()
    stride = surface.get_stride()

    def pixel(x, y):
        b, g, r, a = data[y * stride + 4 * x: y * stride + 4 * x + 4]
        return r, g, b, a
    return pixel


def test_pressed_circle_is_plain_white(mod, monkeypatch):
    t = target(mod, monkeypatch)
    t.pressed = True
    pixel = draw(mod, t)
    assert pixel(50, 50) == (255, 255, 255, 255)
    assert pixel(1, 1) == (0, 0, 0, 0)  # transparent around it


def test_idle_circle_is_dim_with_a_ring(mod, monkeypatch):
    t = target(mod, monkeypatch)
    t.state = mod.STATE_IDENTIFYING
    pixel = draw(mod, t)
    assert pixel(1, 1) == (0, 0, 0, 0)
    r, g, b, a = pixel(50, 20)  # inside, away from the fingerprint arcs
    assert 0 < a < 255


def test_enrollment_progress_is_drawn(mod, monkeypatch):
    t = target(mod, monkeypatch)
    t.state = mod.STATE_ENROLLING
    t.progress = 0
    before = draw(mod, t)(96, 50)
    t.progress = 50  # a quarter turn clockwise from the top reaches the right side
    after = draw(mod, t)(96, 50)
    assert before != after
    assert after[2] > after[0]  # blue


# ----- following biomd -----

class FpProxy:
    def __init__(self, props):
        self.props = props
        self.handlers = []

    def get_cached_property(self, name):
        value = self.props.get(name)
        return None if value is None else GLib.Variant(*value)

    def connect(self, signal, handler):
        self.handlers.append((signal, handler))


class FakeTarget:
    made = []

    def __init__(self, fp, system, x, y, radius, panel):
        self.fp, self.sx, self.sy, self.sr, self.panel = fp, x, y, radius, panel
        self.states, self.progress = [], []
        FakeTarget.made.append(self)

    def set_state(self, state):
        self.states.append(state)

    def set_progress(self, progress):
        self.progress.append(progress)


def next_proxy(proxies):
    proxy = proxies.pop(0)
    if isinstance(proxy, Exception):
        raise proxy
    return proxy


@pytest.fixture
def biomd(mod, monkeypatch):
    """main() with the buses, the panel and the target faked; returns its bus callbacks."""
    watched = {}
    proxies = []
    gio = fakes.Proxy(
        mod.Gio, bus_get_sync=lambda *a: "system-bus",
        bus_watch_name_on_connection=lambda bus, name, flags, up, down: watched.update(
            bus=bus, name=name, up=up, down=down),
        DBusProxy=types.SimpleNamespace(new_sync=lambda *a: next_proxy(proxies)))
    monkeypatch.setattr(mod, "Gio", gio)
    monkeypatch.setattr(mod, "display_mode", lambda: (1080, 2400))
    monkeypatch.setattr(mod, "Target", FakeTarget)
    FakeTarget.made = []
    assert mod.main() == 0
    assert (watched["bus"], watched["name"]) == ("system-bus", "io.FuriOS.Biomd")
    watched["proxies"] = proxies
    return watched


UNDER_DISPLAY = {"SensorType": ("u", 3), "SensorLocation": ("(iii)", (540, 2100, 90)),
                 "State": ("u", 2)}


def test_biomd_with_an_under_display_sensor(biomd):
    fp = FpProxy(UNDER_DISPLAY)
    biomd["proxies"].append(fp)
    biomd["up"](None, "io.FuriOS.Biomd", ":1.5")
    (t,) = FakeTarget.made
    assert (t.sx, t.sy, t.sr, t.panel, t.states) == (540, 2100, 90, (1080, 2400), [2])
    (signal, handler), = fp.handlers
    handler(fp, ":1.5", "StateChanged", GLib.Variant("(u)", (1,)))
    handler(fp, ":1.5", "EnrollmentProgressChanged", GLib.Variant("(u)", (35,)))
    # Signals of an older proxy (before a biomd restart) are ignored
    handler(FpProxy({}), ":1.4", "StateChanged", GLib.Variant("(u)", (0,)))
    assert t.states == [2, 1] and t.progress == [35]
    biomd["down"](None, "io.FuriOS.Biomd")
    assert t.states[-1] == 0


def test_biomd_restart_reuses_the_target(biomd):
    biomd["proxies"] += [FpProxy(UNDER_DISPLAY),
                         FpProxy(dict(UNDER_DISPLAY, SensorLocation=("(iii)", (500, 2000, 80)),
                                      State=None))]
    biomd["up"](None, "io.FuriOS.Biomd", ":1.5")
    biomd["up"](None, "io.FuriOS.Biomd", ":1.9")
    (t,) = FakeTarget.made
    assert (t.sx, t.sy, t.sr) == (500, 2000, 80) and t.fp is not None
    assert t.states == [2, 0]


@pytest.mark.parametrize("props", [
    {"SensorType": ("u", 1), "SensorLocation": ("(iii)", (540, 2100, 90))},  # rear sensor
    {"SensorType": ("u", 3), "SensorLocation": ("(iii)", (540, 2100, 0))},
    {"State": ("u", 0)},  # an older biomd
])
def test_biomd_without_a_sensor_under_the_display(biomd, props):
    biomd["proxies"].append(FpProxy(props))
    biomd["up"](None, "io.FuriOS.Biomd", ":1.5")
    assert FakeTarget.made == []
    biomd["down"](None, "io.FuriOS.Biomd")  # nothing to hide


def test_main_without_a_panel_size(mod, monkeypatch):
    monkeypatch.setattr(mod, "Gio", fakes.Proxy(mod.Gio, bus_get_sync=lambda *a: "bus"))
    monkeypatch.setattr(mod, "display_mode", lambda: None)
    assert mod.main() == 0


# ----- the window itself -----

class RecordingShell:
    """GtkLayerShell recording every call made for a window."""

    class Layer:
        OVERLAY = "overlay"

    class Edge:
        LEFT, BOTTOM = "left", "bottom"

    class KeyboardMode:
        NONE = "none"

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda window, *args: self.calls.append((name,) + args)


class DrawingArea:
    def __init__(self):
        self.handlers = {}
        self.events = None

    def connect(self, signal, handler):
        self.handlers[signal] = handler

    def add_events(self, mask):
        self.events = mask


@pytest.mark.parametrize("rgba", [True, False])
def test_target_window_sits_over_everything(mod, monkeypatch, tmp_path, rgba):
    shell = RecordingShell()
    monkeypatch.setattr(mod, "GtkLayerShell", shell)
    monkeypatch.setattr(mod.Gtk, "DrawingArea", DrawingArea)
    monkeypatch.setattr(mod, "BACKLIGHT_DIR", str(tmp_path / "none"))
    visuals = []
    added = []
    screen = types.SimpleNamespace(get_rgba_visual=lambda: "rgba" if rgba else None)
    monkeypatch.setattr(mod.Target, "get_screen", lambda self: screen, raising=False)
    monkeypatch.setattr(mod.Target, "set_visual", lambda self, v: visuals.append(v),
                        raising=False)
    monkeypatch.setattr(mod.Target, "add", lambda self, child: added.append(child), raising=False)
    fp = Proxy()
    t = mod.Target(fp, SystemBus(), 540, 2100, 90, (1080, 2400))
    assert (t.fp, t.sx, t.sy, t.sr, t.panel_w, t.panel_h) == (fp, 540, 2100, 90, 1080, 2400)
    assert t.state == mod.STATE_IDLE and not t.pressed and t.backlight.name is None
    assert shell.calls == [("init_for_window",), ("set_namespace", "plinius-udfps"),
                           ("set_layer", "overlay"), ("set_anchor", "left", True),
                           ("set_anchor", "bottom", True), ("set_exclusive_zone", -1),
                           ("set_keyboard_mode", "none")]
    assert visuals == (["rgba"] if rgba else [])
    assert added == [t.area]
    assert t.area.handlers == {"draw": t.on_draw, "touch-event": t.on_touch,
                               "button-press-event": t.on_button,
                               "button-release-event": t.on_button}


def test_place_without_any_monitor(mod, monkeypatch):
    t = target(mod, monkeypatch, monitors=[])
    assert mod.Target.place(t) is False
    assert t.shell.monitor is None and t.area.size is None


def test_other_events_change_nothing(mod, monkeypatch, glib):
    t = bind(mod, target(mod, monkeypatch))
    t.set_state(mod.STATE_IDENTIFYING)
    E = mod.Gdk.EventType
    assert t.on_touch(None, types.SimpleNamespace(type=E.TOUCH_UPDATE)) is True
    assert t.on_button(None, types.SimpleNamespace(type=E.DOUBLE_BUTTON_PRESS)) is True
    assert not t.pressed and t.fp.calls == []


def test_ui_ready_after_the_finger_is_gone_says_nothing(mod, monkeypatch, glib):
    t = bind(mod, target(mod, monkeypatch))
    t.ui_ready_source = 5
    assert t.ui_ready() is False
    assert t.ui_ready_source == 0 and t.fp.calls == []


def test_a_failed_call_to_biomd_is_logged(mod, monkeypatch, capsys):
    t = bind(mod, target(mod, monkeypatch))
    answers = []

    class Fp:
        def call(self, method, params, flags, timeout, cancel, done):
            answers.append(done)

        def call_finish(self, result):
            if result == "bad":
                raise GLib.Error("biomd is gone")
    fp = t.fp = Fp()
    t.call("PointerUp", None)
    t.call("UiReady", None)
    answers[0](fp, "ok")
    answers[1](fp, "bad")
    assert capsys.readouterr().err == "plinius-udfps: UiReady: biomd is gone\n"


def test_biomd_unknown_signals_and_proxy_errors(biomd, capsys):
    fp = FpProxy(UNDER_DISPLAY)
    biomd["proxies"].append(fp)
    biomd["up"](None, "io.FuriOS.Biomd", ":1.5")
    (t,) = FakeTarget.made
    (_signal, handler), = fp.handlers
    handler(fp, ":1.5", "Identified", GLib.Variant("(u)", (1,)))
    assert t.states == [2] and t.progress == []

    # biomd restarting and not answering yet: the target stays as it was
    biomd["proxies"].append(GLib.Error("not activatable"))
    biomd["up"](None, "io.FuriOS.Biomd", ":1.6")
    assert "plinius-udfps: biomd: not activatable" in capsys.readouterr().err
    assert len(FakeTarget.made) == 1 and t.fp is fp


# ----- the program as the session starts it -----

def test_program_without_a_layer_shell_exits_1(mod, capsys):
    shell = types.SimpleNamespace(is_supported=lambda: False)
    inits = []
    gtk = fakes.namespace("Gtk")
    gtk.init = lambda argv: inits.append(list(argv))
    code = run_as_main(UDFPS, Gtk=gtk, Gdk=fakes.namespace("Gdk"), GtkLayerShell=shell)
    assert code == 1 and inits == [[str(UDFPS)]]
    assert capsys.readouterr().err == "plinius-udfps: the compositor has no layer shell\n"
