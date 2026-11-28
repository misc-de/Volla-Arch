# plinius-remote with its window built: the header switch, the pages, the menu, the keys of
# both keyboards and the touchpad's controllers, the pairing dialogs and the way the program
# starts and quits. GTK, Gdk, Gsk and Adw are replaced by recording widgets (W below) that keep
# their properties, children, signal handlers and controllers, so a test can tap a key or flip
# the switch and see what reaches the host. Nothing reaches BlueZ or a display.

import runpy
import sys
import types

import gi
import pytest

import desktop_fakes as fakes
from conftest import PKG

gi.require_version("Graphene", "1.0")
from gi.repository import Gio, GLib, Graphene  # noqa: E402

REMOTE = PKG / "plinius-remote" / "plinius-remote.py"


# ----- recording widgets -----

class _NS(type):
    """Missing class attributes (Gtk.Box, Gtk.Align.END ...) are created on first use and then
    stay the same object, so enum values compare by identity."""

    def __getattr__(cls, name):
        if name.startswith("__"):
            raise AttributeError(name)
        sub = _NS(name, (W,), {})
        setattr(cls, name, sub)
        return sub


# Methods without a result that are only recorded (in .calls)
ACTIONS = {"present", "popup", "unparent", "force_close", "add_toast", "add_response",
           "add_setter", "add_breakpoint", "add_top_bar", "add_bottom_bar", "pack_start",
           "pack_end", "load_from_string", "group", "snapshot_child", "fullscreen"}


class W(metaclass=_NS):
    """A widget (or controller, dialog ...): set_x/get_x keep property x, constructor keywords
    are properties too; children, handlers and controllers are kept for the tests."""

    def __init__(self, *args, **props):
        self.args = args
        self.props_ = dict(props)
        self.calls = []
        self.children = []
        self.named = {}
        self.handlers = {}
        self.blocked = set()
        self.controllers = []
        self.css = set(props.get("css_classes", ()))
        self.parent = None
        self.nat = 0               # natural height that measure() reports
        self.size = (0, 0)
        self.origin = (True, 0.0)  # compute_point(): ok and the x position in the window
        self.allocation = None
        self.props_.setdefault("child_visible", True)

    def __getattr__(self, name):
        if name.startswith("__") or name == "props_":
            raise AttributeError(name)
        if name.startswith("set_"):
            key = name[4:]

            def setter(*args):
                self.calls.append((name, args))
                self.props_[key] = args[0] if len(args) == 1 else args
            return setter
        if name.startswith("get_"):
            return lambda *_a: self.props_.get(name[4:])
        if name in ACTIONS:
            return lambda *args: self.calls.append((name, args))
        raise AttributeError(f"{type(self).__name__}.{name}")

    # signals
    def connect(self, signal, func, *extra):
        self.handlers.setdefault(signal, []).append((func, extra))
        return (signal, len(self.handlers[signal]) - 1)

    def handler_block(self, hid):
        self.blocked.add(hid)

    def handler_unblock(self, hid):
        self.blocked.discard(hid)

    def emit(self, signal, *args):
        result = None
        for i, (func, extra) in enumerate(self.handlers.get(signal, [])):
            if (signal, i) not in self.blocked:
                result = func(self, *args, *extra)
        return result

    # children
    def append(self, child):
        self.children.append(child)
        child.parent = self

    def insert(self, child, position):
        self.children.insert(position, child)
        child.parent = self

    def remove(self, child):
        self.children.remove(child)
        self.named = {n: c for n, c in self.named.items() if c is not child}

    def attach(self, child, col, row, width, height):
        child.cell = (col, row, width, height)
        self.append(child)

    def add_named(self, child, name):
        self.append(child)
        self.named[name] = child

    def add_titled_with_icon(self, child, name, title, icon):
        self.add_named(child, name)
        child.page = (title, icon)

    def get_child_by_name(self, name):
        return self.named.get(name)

    def set_child(self, child):
        self.props_["child"] = child
        child.parent = self

    def get_first_child(self):
        return self.children[0] if self.children else self.props_.get("child")

    def add_controller(self, controller):
        self.controllers.append(controller)

    def add_css_class(self, css):
        self.css.add(css)

    def remove_css_class(self, css):
        self.css.discard(css)

    # layout
    def set_parent(self, parent):
        self.parent = parent

    def get_root(self):
        w = self
        while w.parent is not None:
            w = w.parent
        return w

    def compute_point(self, _target, point):
        ok, x = self.origin
        return ok, Graphene.Point().init(x, point.y)

    def get_width(self):
        return self.size[0]

    def get_height(self):
        return self.size[1]

    def measure(self, _orientation, _for_size):
        return 0, self.nat, -1, -1

    def allocate(self, width, height, baseline, transform):
        self.allocation = (width, height, transform)


class ToggleButton(W):
    def get_active(self):
        return self.props_.get("active", False)

    def set_active(self, active):
        changed = active != self.get_active()
        self.props_["active"] = active
        if changed:
            self.emit("toggled")


class CheckButton(ToggleButton):
    """In a group only one is active, as with radio buttons."""

    def __init__(self, *args, group=None, **props):
        super().__init__(*args, **props)
        self.group = group.group if group else []
        self.group.append(self)

    def set_active(self, active):
        if active:
            for other in self.group:
                if other is not self:
                    ToggleButton.set_active(other, False)
        ToggleButton.set_active(self, active)


class Switch(ToggleButton):
    def set_active(self, active):
        changed = active != self.get_active()
        self.props_["active"] = active
        if changed:
            self.emit("notify::active", None)


class Stack(W):
    def set_visible_child_name(self, name):
        changed = name != self.props_.get("visible_child_name")
        self.props_["visible_child_name"] = name
        if changed:
            self.emit("notify::visible-child-name", None)


class Adjustment:
    def __init__(self):
        self.value, self.upper = 0, 640

    def get_upper(self):
        return self.upper

    def set_value(self, value):
        self.value = value


class ScrolledWindow(W):
    def __init__(self, *args, **props):
        super().__init__(*args, **props)
        self.adj = Adjustment()
        self.props_["child"].parent = self

    def get_vadjustment(self):
        return self.adj


class Scale(W):
    @classmethod
    def new_with_range(cls, orientation, lower, upper, step):
        return cls(range=(lower, upper, step))


class Popover(W):
    def measure(self, _orientation, _for_size):
        # As wide as its buttons
        return 0, 40 * len(self.props_["child"].children), -1, -1

    def popdown(self):
        self.calls.append(("popdown", ()))
        self.emit("closed")


class StyleContext(W):
    providers = []

    @classmethod
    def add_provider_for_display(cls, display, provider, priority):
        cls.providers.append(provider)


class Transform:
    """Records the steps of a Gsk.Transform."""

    def __init__(self, ops=()):
        self.ops = list(ops)

    @classmethod
    def new(cls):
        return cls()

    def translate(self, point):
        return Transform(self.ops + [("translate", point.x, point.y)])

    def rotate(self, angle):
        return Transform(self.ops + [("rotate", angle)])


class Application:
    exit_code = 0

    def __init__(self, **props):
        self.app_props = props
        self.props = types.SimpleNamespace(active_window=None)
        self.quits = 0
        self.ran = None
        self.shut_down = False

    def activate(self):
        self.do_activate()

    def run(self, argv):
        self.ran = list(argv)
        return self.exit_code

    def quit(self):
        self.quits += 1

    def do_shutdown(self):
        self.shut_down = True


class ApplicationWindow(W):
    def present(self):
        self.calls.append(("present", ()))
        self.props_["application"].props.active_window = self


Gtk = _NS("Gtk", (W,), {"ToggleButton": ToggleButton, "CheckButton": CheckButton,
                         "Switch": Switch, "Stack": Stack, "ScrolledWindow": ScrolledWindow,
                         "Scale": Scale, "Popover": Popover, "StyleContext": StyleContext})
Adw = _NS("Adw", (W,), {"Application": Application, "ApplicationWindow": ApplicationWindow,
                         "ViewStack": Stack})
Gdk = _NS("Gdk", (W,), {})
Gsk = _NS("Gsk", (W,), {"Transform": Transform})
NAMESPACES = {"Gtk": Gtk, "Adw": Adw, "Gdk": Gdk, "Gsk": Gsk}


def walk(widget):
    yield widget
    kids = list(widget.children)
    child = widget.props_.get("child")
    if isinstance(child, W) and child not in kids:
        kids.append(child)
    for kid in kids:
        yield from walk(kid)


def label_of(widget):
    first = widget.get_first_child()
    if isinstance(first, W) and first.props_.get("label") is not None:
        return first.props_["label"]
    return widget.props_.get("label")


def find(root, label, cls=None):
    """Widgets under root with this label (their own or their first child's)."""
    return [w for w in walk(root) if (cls is None or isinstance(w, cls))
            and w.props_.get("can_target", True) is not False and label_of(w) == label]


def gesture(widget):
    (g,) = [c for c in widget.controllers if type(c).__name__ == "GestureDrag"]
    return g


def press(widget):
    gesture(widget).emit("drag-begin", 0.0, 0.0)


def lift(widget):
    gesture(widget).emit("drag-end", 0.0, 0.0)


def tap(widget):
    press(widget)
    lift(widget)


# ----- the module and the system bus -----

@pytest.fixture(scope="module")
def mod():
    gi.require_version("Graphene", "1.0")
    return fakes.load_gui(REMOTE, "plinius_remote_window_under_test", **NAMESPACES)


@pytest.fixture
def glib(mod, monkeypatch):
    fake = fakes.FakeGLib()
    monkeypatch.setattr(mod, "GLib", fake)
    return fake


class Bus:
    """The system bus: asynchronous calls wait in .pending until answered."""

    def __init__(self):
        self.pending, self.emitted, self.sync, self.registered = [], [], [], {}

    def signal_subscribe(self, *args):
        return 1

    def call(self, name, path, iface, method, params, reply, flags, timeout, cancel, finish):
        self.pending.append(types.SimpleNamespace(
            method=method, params=params.unpack() if params is not None else None,
            finish=finish))

    def call_finish(self, res):
        if isinstance(res, Exception):
            raise res
        return res

    def answer(self, value=None):
        call = self.pending.pop(0)
        call.finish(self, value or GLib.Variant("()", ()))
        return call

    def run(self):
        done = []
        while self.pending:
            done.append(self.answer().method)
        return done

    def call_sync(self, name, path, iface, method, params, *rest):
        self.sync.append(method)

    def emit_signal(self, dest, path, iface, signal, params):
        self.emitted.append((path, params.unpack()))

    def register_object_with_closures2(self, path, *rest):
        self.registered[len(self.registered) + 1] = path
        return len(self.registered)

    def unregister_object(self, reg_id):
        self.registered.pop(reg_id, None)


ADAPTER = "/org/bluez/hci0"
HOST = "/org/bluez/hci0/dev_11_22_33_44_55_66"


def bluez_objects():
    v = GLib.Variant
    return v("(a{oa{sa{sv}}})", ({
        ADAPTER: {"org.bluez.Adapter1": {"Address": v("s", "AA:BB"), "Alias": v("s", "Volla"),
                                         "Powered": v("b", True), "Pairable": v("b", True)},
                  "org.bluez.GattManager1": {}, "org.bluez.LEAdvertisingManager1": {}},
        HOST: {"org.bluez.Device1": {"Alias": v("s", "Laptop"), "Connected": v("b", False)}},
    },))


@pytest.fixture
def app(mod, glib, monkeypatch, tmp_path):
    """The app after activate(), BlueZ not there yet; reports go to app.sent."""
    bus = Bus()
    monkeypatch.setattr(mod, "Gio", fakes.Proxy(mod.Gio, bus_get_sync=lambda *a: bus,
                                                bus_watch_name_on_connection=lambda *a: 1))
    monkeypatch.setattr(mod, "default_layout", lambda: "us")
    monkeypatch.setattr(mod, "SETTINGS_FILE", str(tmp_path / "settings.ini"))
    monkeypatch.setattr(mod, "BATTERY", str(tmp_path / "capacity"))
    (tmp_path / "capacity").write_text("80\n")
    StyleContext.providers.clear()
    a = mod.RemoteApp()
    a.activate()
    a.bus_, a.glib, a.tmp = bus, glib, tmp_path
    return a


def capture(app, mod):
    """Reports to the host land in app.sent instead of the (switched off) peripheral."""
    sent = []
    app.reports = mod.Reports(lambda rid, data: sent.append((rid, bytes(data))))
    app.sent = sent
    return sent


def flush(app):
    for _ in range(200):
        timers = [s for s in app.glib.of_kind("timeout")
                  if app.glib.sources[s][2] == app.reports.on_timer]
        if not timers:
            return app.sent
        app.glib.fire(timers[0])
    raise AssertionError("reports never drained")


def keyboard_reports(sent):
    return [data for rid, data in sent if rid == 1]


def bluez_up(app):
    app.peripheral.on_bluez_appeared(None, "org.bluez", ":1.7")
    assert app.bus_.answer(bluez_objects()).method == "GetManagedObjects"


# ----- the window -----

def test_activate_builds_the_window_once(mod, app):
    win = app.props.active_window
    assert win.props_["title"] == "Remote Input" and ("present", ()) in win.calls
    assert win.props_["focus"] is None  # nothing opens the on-screen keyboard by itself
    assert set(app.stack.named) == {"touchpad", "keyboard"}
    assert app.stack.named["keyboard"].page == ("Keyboard", "input-keyboard-symbolic")
    (provider,) = StyleContext.providers
    assert provider.calls == [("load_from_string", (mod.CSS,))]
    # Battery level every minute
    (timer,) = app.glib.of_kind("seconds")
    assert app.glib.sources[timer][1] == 60 and app.glib.fire(timer) is True
    # Without BlueZ: the switch cannot be used
    assert app.switch.props_["sensitive"] is False
    assert app.title.props_["subtitle"] == "Bluetooth is not available"
    assert app.pad_label.props_["label"] == "Bluetooth is not available on this phone right now."
    peripheral = app.peripheral
    app.activate()
    assert app.peripheral is peripheral
    assert [c for c in win.calls if c[0] == "present"] == [("present", ())] * 2


@pytest.mark.parametrize("args, page", [(["--keyboard"], "keyboard"),
                                        (["--touchpad"], "touchpad"), ([], None)])
def test_command_line_opens_a_page(mod, app, args, page):
    app.stack.props_.pop("visible_child_name", None)
    line = types.SimpleNamespace(get_arguments=lambda: ["plinius-remote"] + args)
    assert app.do_command_line(line) == 0
    assert app.stack.props_.get("visible_child_name") == page


def test_state_shown_in_the_header_and_on_the_pages(mod, app):
    p = app.peripheral
    bluez_up(app)
    assert app.switch.props_["sensitive"] is True and app.switch.get_active() is False
    assert app.title.props_["subtitle"] == "Not visible"
    assert app.pad_label.props_["label"].startswith("Turn on the switch")
    assert app.kb_label.props_["label"] == app.pad_label.props_["label"]
    # The switch starts the peripheral; while it starts, nothing to say on the pad
    app.switch.set_active(True)
    assert p.state == p.STARTING and app.title.props_["subtitle"] == "Starting…"
    assert app.pad_label.props_["label"] == ""
    assert app.bus_.run() == ["RegisterAgent", "RequestDefaultAgent", "RegisterApplication",
                              "RegisterAdvertisement"]
    assert app.title.props_["subtitle"] == "Visible as “Volla Remote”"
    assert "pair with “Volla Remote”" in app.pad_label.props_["label"]
    # A host subscribes without naming itself: connected to nobody in particular
    p.notify_count = 1
    p.update_connection()
    assert app.title.props_["subtitle"] == "Connected" and app.kb_label.props_["label"] == ""
    assert app.pad_label.props_["label"].startswith("Move with one finger")
    p.host = HOST
    p.changed()
    assert app.title.props_["subtitle"] == "Connected to Laptop"
    assert app.kb_label.props_["label"] == "Typing goes to Laptop."
    # The switch follows the state without starting anything itself
    assert app.switch.get_active() is True
    app.switch.set_active(False)
    assert p.state == p.STOPPING and app.title.props_["subtitle"] == "Stopping…"
    assert app.switch.props_["sensitive"] is False
    app.bus_.run()
    assert p.state == p.OFF and app.switch.get_active() is False


def test_leaving_the_host_lets_go_of_everything(mod, app):
    bluez_up(app)
    p = app.peripheral
    app.switch.set_active(True)
    app.bus_.run()
    p.notify_count = 1
    p.update_connection()
    assert p.state == p.CONNECTED
    app.hold_toggle.set_active(True)
    app.reports.timer = 99  # a report just went out: the rest waits
    app.reports.key(0, 0x04)
    assert app.reports.buttons == 1 and app.reports.queue
    p.notify_count = 0
    p.update_connection()
    assert p.state == p.VISIBLE
    # The key waiting to go out is dropped; only the button's release is left to send
    assert app.reports.queue == [(mod.RID_MOUSE, bytes(7))] and app.reports.buttons == 0
    assert app.hold_toggle.get_active() is False


def test_reports_reach_the_peripheral(mod, app):
    bluez_up(app)
    app.switch.set_active(True)
    app.bus_.run()
    app.send_report(mod.RID_CONSUMER, b"\xe9\x00")
    (path, (_iface, changed, _inv)), = app.bus_.emitted
    assert path == app.peripheral.reports[mod.RID_CONSUMER].path
    assert bytes(changed["Value"]) == b"\xe9\x00"
    (app.tmp / "capacity").write_text("42\n")
    assert app.on_battery_timer() is True
    assert app.bus_.emitted[-1][0] == app.peripheral.battery.path


def test_errors_become_toasts(mod, app):
    bluez_up(app)
    app.peripheral.error("Bluetooth was switched off")
    ((name, (toast,)),) = app.toast_overlay.calls
    assert name == "add_toast" and toast.props_["title"] == "Bluetooth was switched off"


# ----- pairing questions -----

def test_pair_confirmation_dialog(mod, app):
    answers = []
    app.on_pair("confirm", "Laptop", "001234", answers.append)
    d = app.dialog
    assert d.props_["heading"] == "Pair with Laptop?"
    assert d.props_["body"] == "Make sure Laptop shows the same code:\n\n001234"
    assert [c[1] for c in d.calls if c[0] == "add_response"] == [("reject", "Reject"),
                                                                ("pair", "Pair")]
    assert d.props_["close_response"] == "reject"
    assert ("present", (app.props.active_window,)) in d.calls
    d.emit("response", "pair")
    assert answers == [True] and app.dialog is None


def test_pair_authorization_rejected(mod, app):
    answers = []
    app.on_pair("authorize", "Laptop", None, answers.append)
    assert app.dialog.props_["body"] == "Laptop can then use this phone as mouse and keyboard."
    app.dialog.emit("response", "reject")
    assert answers == [False]


def test_passkey_dialog_stays_for_repeated_digits(mod, app):
    answers = []
    app.on_pair("display", "TV", "000042", answers.append)
    d = app.dialog
    assert d.props_["heading"] == "Pair with TV"
    assert d.props_["body"] == "Type this code on TV:\n\n000042"
    assert [c[1] for c in d.calls if c[0] == "add_response"] == [("ok", "Close")]
    app.on_pair("display", "TV", "000042", answers.append)  # BlueZ again, one digit typed
    assert app.dialog is d and ("force_close", ()) not in d.calls
    # A new question closes the old dialog; its late response answers no one here
    app.on_pair("confirm", "TV", "111111", answers.append)
    assert ("force_close", ()) in d.calls and app.dialog is not d
    d.emit("response", "ok")
    assert app.dialog is not None and answers == [False]
    app.on_pair_cancel()
    assert app.dialog is None
    app.on_pair_cancel()  # nothing open: nothing to do


def test_pairing_through_the_agent_opens_the_dialog(mod, app):
    bluez_up(app)
    inv = fakes.Invocation()
    app.peripheral.on_method(None, ":1.7", mod.AGENT_PATH, mod.AGENT_IFACE,
                             "RequestConfirmation", GLib.Variant("(ou)", (HOST, 7)), inv)
    assert app.dialog.props_["body"].endswith("000007") and inv.answered == 0
    app.dialog.emit("response", "pair")
    assert inv.answered == 1 and inv.error is None


# ----- the menu -----

def menu_box(app):
    (menu,) = [w for w in walk_header(app) if type(w).__name__ == "MenuButton"]
    return menu.props_["popover"].props_["child"]


def walk_header(app):
    win = app.props.active_window
    toolbar = win.props_["content"]
    (header,) = [args[0] for name, args in toolbar.calls if name == "add_top_bar"]
    return [args[0] for name, args in header.calls if name in ("pack_start", "pack_end")]


def test_menu_changes_are_saved(mod, app):
    box = menu_box(app)
    (scale,) = [w for w in box.children if isinstance(w, Scale)]
    assert scale.props_["range"] == (0.5, 4.0, 0.1) and scale.props_["value"] == 1.6
    scale.set_value(2.345)
    scale.emit("value-changed")
    assert app.speed == 2.3
    (natural,) = find(box, "Natural scrolling")
    natural.set_active(False)
    assert app.natural is False
    kf = GLib.KeyFile()
    kf.load_from_file(str(app.tmp / "settings.ini"), GLib.KeyFileFlags.NONE)
    assert kf.get_double("touchpad", "speed") == 2.3
    assert kf.get_boolean("touchpad", "natural-scrolling") is False


def test_key_labels_follow_the_layout(mod, app):
    box = menu_box(app)
    (us,) = find(box, "English (US)", CheckButton)
    (de,) = find(box, "German", CheckButton)
    assert us.get_active() and not de.get_active()
    grid = app.keyboard_bin.props_["child"]
    assert find(grid, "Ctrl") and not find(grid, "Strg")
    de.set_active(True)
    assert app.layout == "de" and us.get_active() is False
    grid = app.keyboard_bin.props_["child"]
    assert find(grid, "Strg") and find(grid, "Pos1") and find(grid, "AltGr")
    assert app.phone.layout == "de"
    assert "layout=de" in (app.tmp / "settings.ini").read_text()
    # The layout it already has: nothing is rebuilt
    app.set_layout("de")
    assert app.keyboard_bin.props_["child"] is grid


# ----- the full keyboard (landscape) -----

def test_full_keyboard_keys(mod, app):
    grid = app.keyboard_bin.props_["child"]
    labels = [label_of(w) for w in grid.children]
    assert labels[:13] == ["Esc"] + [f"F{i}" for i in range(1, 13)]
    (f1,) = find(grid, "F1")
    assert f1.css == {"key", "fkey"}
    # 16 key units per row, four grid columns each
    rows = {}
    for w in grid.children:
        col, row, width, _h = w.cell
        rows[row] = max(rows.get(row, 0), col + width)
    assert rows[0] == rows[1] == 64 and max(rows.values()) == 64
    capture(app, mod)
    (a,) = find(grid, "a")
    (b,) = find(grid, "b")
    (ctrl,) = [w for w in find(grid, "Ctrl") if w.cell[0] == 0]
    ctrl.set_active(True)
    press(a)
    assert "pressed" in a.css
    press(b)
    press(b)  # already down
    lift(a)
    lift(a)  # already up
    lift(b)
    assert "pressed" not in a.css and ctrl.get_active() is False
    assert keyboard_reports(flush(app)) == [
        bytes([mod.MOD_CTRL, 0, 0x04, 0, 0, 0, 0, 0]),
        bytes([mod.MOD_CTRL, 0, 0x04, 0x05, 0, 0, 0, 0]),
        bytes([mod.MOD_CTRL, 0, 0x05, 0, 0, 0, 0, 0]), bytes(8)]
    # A key still down when the page turns lets go on the host
    press(a)
    app.kb_stack.set_visible_child_name("landscape")
    assert keyboard_reports(flush(app))[-1] == bytes(8) and app.held_keys == []


def test_full_keyboard_without_reports(mod, app):
    app.reports = None
    grid = app.keyboard_bin.props_["child"]
    (a,) = find(grid, "a")
    tap(a)
    assert app.held_keys == []


def test_media_keys(mod, app):
    capture(app, mod)
    landscape = app.kb_stack.named["landscape"]
    (up,) = [w for w in landscape.media.children if w.props_["tooltip_text"] == "Volume up"]
    up.emit("clicked")
    (back,) = [w for w in app.phone.children[0].children if w.props_["tooltip_text"] == "Back"]
    back.emit("clicked")
    assert [d for r, d in flush(app) if r == 3] == [b"\xe9\x00", bytes(2), b"\x24\x02",
                                                     bytes(2)]
    app.reports = None
    up.emit("clicked")
    back.emit("clicked")  # no host: nothing to send, nothing breaks


def test_special_labels(mod, app):
    assert app.special_label(mod.KEY_CAPS) == "⇪" and app.special_label(0x99) == "?"
    app.layout = "de"
    assert app.special_label(mod.KEY_PAGEDOWN) == "Bild↓"


# ----- the landscape page's own layout -----

def test_landscape_keyboard_turns_in_a_portrait_window(mod, app):
    lk = app.kb_stack.named["landscape"]
    assert lk.media.parent is lk and lk.keyboard.parent is lk
    lk.media.nat, lk.keyboard.nat = 50, 240
    lk.do_size_allocate(400, 800, -1)
    w, h, t = lk.keyboard.allocation
    assert (w, h) == (788, 388 - 56)
    # A quarter turn: the keyboard's top along the right edge
    assert t.ops == [("translate", 394.0, 6.0), ("rotate", 90), ("translate", 0.0, 56.0)]
    assert lk.media.allocation[2].ops[-1] == ("translate", 0.0, 0.0)
    lk.do_size_allocate(800, 300, -1)
    w, h, t = lk.keyboard.allocation
    assert t.ops == [("translate", 6.0, 6.0), ("translate", 0.0, 0.0)] and h == 288
    snapshot = object()
    lk.do_snapshot(snapshot)
    assert lk.calls == [("snapshot_child", (lk.keyboard, snapshot))]
    assert lk.do_measure(None, -1) == (0, 0, -1, -1)
    lk.do_dispose()
    assert ("unparent", ()) in lk.media.calls and ("unparent", ()) in lk.keyboard.calls


# ----- the portrait keyboard -----

def phone_key(app, label, layer="letters"):
    layer_grid = app.phone.layers.named[layer]
    found = find(layer_grid, label)
    assert len(found) == 1, (label, found)
    return found[0]


def test_portrait_keyboard_is_built_for_the_layout(mod, app):
    phone = app.phone
    assert set(phone.layers.named) == {"letters", "symbols", "symbols2"}
    assert phone.layers.props_["visible_child_name"] == "letters"
    nav = phone.children[1]
    assert [label_of(w) for w in nav.children] == ["Esc", "⇥", "Ctrl", "Alt", "←", "↑", "↓",
                                                   "→"]
    # Ten letters in the top row, Shift and Backspace around the third
    top = [w for w in phone.layers.named["letters"].children if w.cell[1] == 0]
    assert "".join(label_of(w) for w in top) == "qwertyuiop"
    assert phone_key(app, "⇧").cell == (0, 2, 3, 1)
    assert phone_key(app, "English (US)").cell[1] == 3
    # The second symbol layer has the navigation keys
    assert [label_of(w) for w in phone.layers.named["symbols2"].children
            if w.cell[1] == 1] == ["Home", "End", "PgUp", "PgDn", "Ins", "Del"]
    phone.set_layout("de")
    assert [label_of(w) for w in nav.children][2] == "Strg"
    assert phone_key(app, "German").cell[1] == 3
    assert [label_of(w) for w in phone.layers.named["symbols2"].children
            if w.cell[1] == 1] == ["Pos1", "Ende", "Bild↑", "Bild↓", "Einfg", "Entf"]
    # The old layers are gone, not kept next to the new ones
    assert len(phone.layers.children) == 3


def test_layer_switches(mod, app):
    phone = app.phone
    tap(phone_key(app, "123"))
    assert phone.layers.props_["visible_child_name"] == "symbols"
    tap(phone_key(app, "={<", "symbols"))
    assert phone.layers.props_["visible_child_name"] == "symbols2"
    tap(phone_key(app, "123", "symbols2"))
    assert phone.layers.props_["visible_child_name"] == "symbols"
    tap(phone_key(app, "ABC", "symbols"))
    assert phone.layers.props_["visible_child_name"] == "letters"


def test_shift_relabels_the_letters(mod, app):
    shift = phone_key(app, "⇧")
    q = phone_key(app, "q")
    tap(shift)
    assert label_of(shift) == "⬆" and "latched" in shift.css and label_of(q) == "Q"
    app.glib.advance_ms(100)
    tap(shift)
    assert label_of(shift) == "⇪" and "locked" in shift.css and "latched" not in shift.css
    tap(shift)
    assert label_of(shift) == "⇧" and not {"locked", "latched"} & shift.css
    assert label_of(q) == "q"


def test_typing_on_the_portrait_keyboard(mod, app):
    sent = capture(app, mod)
    tap(phone_key(app, "h"))
    tap(phone_key(app, "i"))
    enter = phone_key(app, "⏎")
    tap(enter)
    assert keyboard_reports(flush(app)) == [
        bytes([0, 0, 0x0B]) + bytes(5), bytes(8), bytes([0, 0, 0x0C]) + bytes(5), bytes(8),
        bytes([0, 0, 0x28]) + bytes(5), bytes(8)]
    # The history shows the entry; tapping it types it again
    h = app.history
    assert [row.props_["tooltip_text"] for row, _st in h.entries] == ["hi⏎"]
    assert h.props_["visible"] is True and app.kb_label.props_["visible"] is False
    sent.clear()
    h.list.emit("row-activated", h.entries[0][0])
    assert keyboard_reports(flush(app)) == [
        bytes([0, 0, 0x0B]) + bytes(5), bytes(8), bytes([0, 0, 0x0C]) + bytes(5), bytes(8),
        bytes([0, 0, 0x28]) + bytes(5), bytes(8)]
    # Each change scrolls the history down to the newest entry
    for sid in app.glib.of_kind("idle"):
        app.glib.fire(sid)
    assert h.scrolled.adj.value == 640
    # The clear button empties it, and the hint comes back
    (clear,) = [w for w in h.children[0].children
                if w.props_.get("tooltip_text") == "Clear history"]
    clear.emit("clicked")
    assert h.empty() and h.props_["visible"] is False and app.kb_label.props_["visible"] is True
    assert h.current_row.props_["visible"] is False


def test_tab_and_arrows_in_the_history(mod, app):
    capture(app, mod)
    nav = app.phone.children[1]
    tab, left = nav.children[1], nav.children[4]
    tap(phone_key(app, "x"))
    tap(tab)
    assert app.history.current == [("x", [(0x1B, 0)]), ("⇥", [(mod.KEY_TAB, 0)])]
    tap(left)  # an arrow ends the entry without being kept
    assert app.history.current == [] and [st for _r, st in app.history.entries] == [
        [(0x1B, 0), (mod.KEY_TAB, 0)]]


def test_keys_without_a_host(mod, app):
    app.reports = None
    tap(phone_key(app, "h"))
    nav = app.phone.children[1]
    tap(nav.children[0])
    app.phone.replay([(0x04, 0)])
    assert app.history.empty() and app.phone.held == []


def long_press(app, mod, label):
    key = phone_key(app, label)
    press(key)
    (timer,) = [s for s in app.glib.of_kind("timeout")
                if app.glib.sources[s][1] == mod.LONG_PRESS_MS]
    app.glib.fire(timer)
    return key


def test_long_press_offers_the_alternates(mod, app):
    capture(app, mod)
    app.set_layout("de")
    app.stack.size = (360, 720)  # the window's content
    a = phone_key(app, "a")
    assert a.get_root() is app.stack
    a.size, a.origin = (36, 50), (True, 2.0)  # the first key of its row, at the left edge
    long_press(app, mod, "a")
    popover = app.phone.popover
    row = popover.props_["child"]
    assert [b.props_["label"] for b in row.children] == ["a", "ä", "â", "à", "á"]
    assert popover.parent is a and ("popup", ()) in popover.calls
    # Moved right so its 200 px stay inside the window, still pointing at the key
    rect = popover.props_["pointing_to"]
    assert (rect.x, rect.y, rect.width, rect.height) == (100 - 2 - 1, 0, 2, 50)
    lift(a)  # the long press itself typed nothing
    assert keyboard_reports(flush(app)) == []
    (ae,) = [b for b in row.children if b.props_["label"] == "ä"]
    ae.emit("clicked")
    assert ("popdown", ()) in popover.calls
    assert keyboard_reports(flush(app)) == [bytes([0, 0, 0x34]) + bytes(5), bytes(8)]
    for sid in app.glib.of_kind("idle"):  # the history scrolls, the closed popover goes
        app.glib.fire(sid)
    assert app.phone.popover is None and ("unparent", ()) in popover.calls
    assert app.history.current[-1][0] == "ä"


def test_quick_tap_on_a_key_with_alternates(mod, app):
    capture(app, mod)
    app.set_layout("de")
    tap(phone_key(app, "a"))
    assert keyboard_reports(flush(app)) == [bytes([0, 0, 0x04]) + bytes(5), bytes(8)]
    # With Shift the alternates are capitals too
    tap(phone_key(app, "⇧"))
    a = phone_key(app, "A")
    a.origin = (False, 0.0)  # not placed yet: the popover stays where GTK puts it
    long_press(app, mod, "A")
    popover = app.phone.popover
    assert [b.props_["label"] for b in popover.props_["child"].children] == \
        ["A", "Ä", "Â", "À", "Á"]
    assert "pointing_to" not in popover.props_


def test_release_drops_what_waits(mod, app):
    capture(app, mod)
    app.set_layout("de")
    phone = app.phone
    long_press(app, mod, "e")
    first = phone.popover
    phone.release()
    assert ("popdown", ()) in first.calls
    # A popover closed after another opened does not clear the newer one
    long_press(app, mod, "o")
    second = phone.popover
    phone.drop_popover(first)
    assert phone.popover is second and ("unparent", ()) in first.calls
    # A key waiting for its finger is dropped, its timer too
    press(phone_key(app, "u"))
    (timer,) = [s for s in app.glib.of_kind("timeout")
                if app.glib.sources[s][1] == mod.LONG_PRESS_MS]
    phone.release()
    assert phone.pending is None and timer in app.glib.removed
    # Keys held when the host went away are just forgotten
    press(app.phone.children[1].children[0])
    app.reports = None
    phone.release()
    assert phone.held == []


# ----- the touchpad's controllers and buttons -----

class Event:
    def __init__(self, kind, seq=1, pos=(0.0, 0.0)):
        self.kind, self.seq, self.pos = kind, seq, pos

    def get_event_type(self):
        return self.kind

    def get_event_sequence(self):
        return self.seq

    def get_position(self):
        return (True, *self.pos)


def test_touchpad_controllers(mod, app):
    capture(app, mod)
    pad = app.pad
    legacy, drag, zoom = pad.controllers
    assert [type(c).__name__ for c in pad.controllers] == ["EventControllerLegacy",
                                                         "GestureDrag", "GestureZoom"]
    # Neither grouped nor claimed: GestureDrag ended at a second finger and took a grouped
    # GestureZoom with it, and a claimed touch was denied to GestureZoom (two fingers made a
    # right click instead of scrolling)
    assert not any(name == "group" for name, _args in zoom.calls + drag.calls)
    # One finger tapped: a click
    drag.emit("drag-begin", 10.0, 10.0)
    assert "touched" in pad.css and "state" not in drag.props_
    app.glib.advance_ms(50)
    drag.emit("drag-end", 0.0, 0.0)
    assert "touched" not in pad.css
    assert [d[0] for r, d in flush(app) if r == 2] == [1, 0]
    # A cancelled touch only ends
    app.glib.advance_ms(1000)
    drag.emit("drag-begin", 10.0, 10.0)
    drag.emit("cancel", None)
    assert "touched" not in pad.css and app.long_press == 0
    zoom.props_["bounding_box_center"] = (True, 5.0, 5.0)
    zoom.emit("begin", None)
    assert app.zoom_center == (5.0, 5.0)
    zoom.emit("cancel", None)
    assert app.zoom_center is None
    # The legacy controller only counts touches, other events pass
    assert legacy.emit("event", Event(mod.Gdk.EventType.BUTTON_PRESS)) is False
    assert app.touches == {}
    assert legacy.emit("event", Event(mod.Gdk.EventType.TOUCH_BEGIN)) is False
    assert app.touches == {1: (0.0, 0.0)}


def test_pad_buttons(mod, app):
    capture(app, mod)
    bar = app.pad.parent.children[1]
    left, hold, right = bar.children
    assert hold is app.hold_toggle
    press(left)
    press(right)
    assert "pressed" in left.css and app.reports.buttons == 3
    lift(left)
    lift(right)
    assert "pressed" not in right.css and app.reports.buttons == 0
    hold.set_active(True)
    assert "holding" in app.pad.css and app.reports.buttons == 1
    hold.set_active(False)
    assert "holding" not in app.pad.css and app.reports.buttons == 0
    app.reports = None
    tap(left)  # no host: the button only looks pressed
    assert "pressed" not in left.css


# ----- settings, layout, start and quit -----

def test_saving_settings_can_fail(mod, app, monkeypatch, capsys):
    (app.tmp / "file").write_text("")
    monkeypatch.setattr(mod, "SETTINGS_FILE", str(app.tmp / "file" / "settings.ini"))
    app.save_settings()
    assert "plinius-remote: saving settings failed" in capsys.readouterr().err


class SchemaSource:
    def __init__(self, has_schema):
        self.has_schema = has_schema

    def lookup(self, schema_id, recursive):
        return object() if self.has_schema and schema_id == \
            "org.gnome.desktop.input-sources" else None


@pytest.mark.parametrize("source, sources, layout", [
    (None, None, "us"),
    (SchemaSource(False), None, "us"),
    (SchemaSource(True), [], "us"),
    (SchemaSource(True), [("xkb", "de+nodeadkeys"), ("xkb", "us")], "de"),
    (SchemaSource(True), [("xkb", "de")], "de"),
    (SchemaSource(True), [("xkb", "us"), ("xkb", "de")], "us"),
])
def test_default_layout(mod, monkeypatch, source, sources, layout):
    read = []

    def settings(schema_id):
        read.append(schema_id)
        return types.SimpleNamespace(get_value=lambda key: GLib.Variant("a(ss)", sources))
    monkeypatch.setattr(mod, "Gio", fakes.Proxy(
        mod.Gio, SettingsSchemaSource=types.SimpleNamespace(get_default=lambda: source),
        Settings=settings))
    assert mod.default_layout() == layout
    assert read == ([] if sources is None else ["org.gnome.desktop.input-sources"])


def test_shutdown_puts_bluez_back(mod, app):
    bluez_up(app)
    app.switch.set_active(True)
    app.bus_.run()
    app.do_shutdown()
    assert app.bus_.sync == ["UnregisterAdvertisement", "UnregisterApplication",
                             "UnregisterAgent"]
    assert app.peripheral.state == app.peripheral.OFF and app.shut_down


def test_shutdown_before_the_window(mod, monkeypatch):
    monkeypatch.setattr(mod, "default_layout", lambda: "us")
    a = mod.RemoteApp()
    a.do_shutdown()
    assert a.shut_down and a.peripheral is None
    a.on_peripheral_changed()  # no peripheral yet: nothing to show


def test_main_quits_cleanly_on_signals(mod, monkeypatch):
    import signal
    handlers = []
    monkeypatch.setattr(mod, "GLib", fakes.Proxy(
        GLib, unix_signal_add=lambda prio, sig, func: handlers.append((sig, func))))
    monkeypatch.setattr(mod, "default_layout", lambda: "us")
    monkeypatch.setattr(Application, "exit_code", 3)
    # No screen besides the phone's: the Bluetooth app
    monkeypatch.setattr(Gdk, "Display", types.SimpleNamespace(get_default=lambda: None),
                        raising=False)
    made = []
    real = mod.RemoteApp
    monkeypatch.setattr(mod, "RemoteApp", lambda **kw: made.append(real(**kw)) or made[-1])
    assert mod.main() == 3
    (a,) = made
    assert a.ran == sys.argv and a.app_props["application_id"] == "org.plinius.Remote"
    assert [sig for sig, _f in handlers] == [signal.SIGTERM, signal.SIGINT]
    assert handlers[1][1]() is False and a.quits == 1


def test_run_as_a_program_exits_with_the_apps_code(mod, monkeypatch):
    """The script itself: main()'s result is the exit status. GLib's signal handlers and the
    phone's input sources are kept out of the test process."""
    handlers = []
    glib = fakes.Proxy(GLib, unix_signal_add=lambda *a: handlers.append(a))
    gio = fakes.Proxy(Gio, SettingsSchemaSource=types.SimpleNamespace(get_default=lambda: None))
    repository = sys.modules["gi.repository"]
    real_require = gi.require_version
    monkeypatch.setattr(gi, "require_version",
                        lambda ns, v: None if ns in NAMESPACES else real_require(ns, v))
    for name, obj in dict(NAMESPACES, GLib=glib, Gio=gio).items():
        monkeypatch.setattr(repository, name, obj, raising=False)
        monkeypatch.setitem(sys.modules, "gi.repository." + name, obj)
    monkeypatch.setattr(Application, "exit_code", 5)
    monkeypatch.setattr(Gdk, "Display", types.SimpleNamespace(get_default=lambda: None),
                        raising=False)
    with pytest.raises(SystemExit) as exit_:
        runpy.run_path(str(REMOTE), run_name="__main__")
    assert exit_.value.code == 5 and len(handlers) == 2
