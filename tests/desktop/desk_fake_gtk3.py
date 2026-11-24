# A recording stand-in for GTK 3, Gdk and GtkLayerShell, for the menus of plinius-desktop
# (desk-apps.py, desk-settings.py, the cards and the click catcher of desk_common.py).
#
# desktop_fakes.Stub takes any use and forgets it; these widgets keep what a program did with
# them (labels, children, the entries of a drop-down, the active toggle, connected handlers,
# the layer-shell placement), and emit() runs the connected handlers like GTK would, so a test
# can click through a menu and look at the result. Nothing here opens a window.

import runpy
import sys
import types

import gi
import pytest

from conftest import load_script


class Toolkit:
    """One fake GTK world: its namespaces (gtk, gdk, shell), the screen's monitors and what
    happened (log of global calls, main loop runs, quits)."""

    def __init__(self, monitor_widths=(68, 0), rgba=True):
        self.monitors = [Monitor(mm) for mm in monitor_widths]
        self.rgba = rgba
        self.log = []
        self.mains = 0
        self.quits = 0
        # Called while Gtk.main() "runs": the test's chance to click around
        self.on_main = None
        self.windows = []
        self.gtk = self._gtk()
        self.gdk = self._gdk()
        self.shell = self._shell()

    # -- namespaces ------------------------------------------------------------------------

    def _gtk(self):
        tk = self

        class Window(Widget):
            def __init__(self, *args, **props):
                super().__init__(*args, **props)
                tk.windows.append(self)

            def get_screen(self):
                return Screen(tk)

            def get_display(self):
                return Display(tk)

        def main():
            tk.mains += 1
            if tk.on_main:
                tk.on_main()

        def main_quit(*_args):
            tk.quits += 1

        class Settings:
            @staticmethod
            def get_default():
                return Recorder(tk.log, "settings")

        class StyleContext:
            @staticmethod
            def add_provider_for_screen(screen, provider, priority):
                tk.log.append(("add_provider_for_screen", provider, priority))

        class Image(Widget):
            @classmethod
            def new_from_icon_name(cls, name, size):
                image = cls()
                image.icon_name, image.icon_size = name, size
                return image

        ns = types.SimpleNamespace(
            Window=Window, Box=Widget, Label=Label, Button=Button, ToggleButton=ToggleButton,
            ComboBoxText=ComboBoxText, ListBox=ListBox, ListBoxRow=ListBoxRow,
            SearchEntry=Entry, ScrolledWindow=Widget, Grid=Grid, Image=Image,
            CssProvider=CssProvider, Settings=Settings, StyleContext=StyleContext,
            Orientation=types.SimpleNamespace(VERTICAL="vertical", HORIZONTAL="horizontal"),
            IconSize=types.SimpleNamespace(MENU="menu", DND="dnd"),
            PolicyType=types.SimpleNamespace(NEVER="never", AUTOMATIC="automatic"),
            STYLE_PROVIDER_PRIORITY_APPLICATION=600, main=main, main_quit=main_quit)
        return ns

    def _gdk(self):
        tk = self

        class DisplayClass:
            @staticmethod
            def get_default():
                return Display(tk)

        class ScreenClass:
            @staticmethod
            def get_default():
                return Screen(tk)

        return types.SimpleNamespace(
            Display=DisplayClass, Screen=ScreenClass, KEY_Escape=0xff1b, KEY_Return=0xff0d,
            EventMask=types.SimpleNamespace(BUTTON_PRESS_MASK=1 << 8, TOUCH_MASK=1 << 22))

    def _shell(self):
        tk = self
        ns = types.SimpleNamespace(
            Edge=types.SimpleNamespace(LEFT="left", RIGHT="right", TOP="top", BOTTOM="bottom"),
            Layer=types.SimpleNamespace(OVERLAY="overlay", TOP="top"),
            KeyboardMode=types.SimpleNamespace(NONE="none", EXCLUSIVE="exclusive"))
        for name in ("init_for_window", "set_namespace", "set_layer", "set_anchor", "set_margin",
                     "set_exclusive_zone", "set_keyboard_mode", "set_monitor"):
            setattr(ns, name, _shell_call(tk, name))
        return ns

    # -- what a test asks ------------------------------------------------------------------

    def placement(self, window):
        """The layer-shell calls for one window: {name: [args, ...]}."""
        out = {}
        for entry in self.log:
            if entry[0] == "shell" and entry[2] is window:
                out.setdefault(entry[1], []).append(entry[3])
        return out

    def namespaces(self):
        return {"Gtk": self.gtk, "Gdk": self.gdk, "GtkLayerShell": self.shell}

    def install(self, mp, **extra):
        """Puts the fake namespaces into gi.repository for code that imports them while it runs
        (desk_common's popups); gi.require_version accepts them."""
        names = dict(self.namespaces(), **extra)
        real = gi.require_version
        mp.setattr(gi, "require_version",
                   lambda ns, version: None if ns in names else real(ns, version))
        repository = sys.modules["gi.repository"]
        for n, obj in names.items():
            mp.setattr(repository, n, obj, raising=False)
            mp.setitem(sys.modules, "gi.repository." + n, obj)

    def load(self, path, name, **extra):
        """Import a program with this toolkit; the module keeps it after the import."""
        with pytest.MonkeyPatch.context() as mp:
            self.install(mp, **extra)
            return load_script(path, name)

    def run_main(self, path, **extra):
        """Run a program as __main__ with this toolkit; returns its globals."""
        with pytest.MonkeyPatch.context() as mp:
            self.install(mp, **extra)
            return runpy.run_path(str(path), run_name="__main__")


def _shell_call(tk, name):
    def call(window, *args):
        tk.log.append(("shell", name, window, args))
    return call


class Recorder:
    """Any method call is logged as (tag, name, args)."""

    def __init__(self, log, tag):
        self._log, self._tag = log, tag

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *args: self._log.append((self._tag, name, args))


class Monitor:
    def __init__(self, mm):
        self.mm = mm

    def get_width_mm(self):
        return self.mm


class Display:
    def __init__(self, tk):
        self.tk = tk

    def get_n_monitors(self):
        return len(self.tk.monitors)

    def get_monitor(self, i):
        return self.tk.monitors[i]

    def get_app_launch_context(self):
        return LaunchContext(self.tk)


class LaunchContext(str):
    """GdkAppLaunchContext, equal to "launch-context": its activation token is tk.activation_token"""

    def __new__(cls, tk):
        context = super().__new__(cls, "launch-context")
        context.tk = tk
        return context

    def get_startup_notify_id(self, app, files):
        self.tk.log.append(("get_startup_notify_id", app.get_id(), files))
        return getattr(self.tk, "activation_token", None)


class Screen:
    def __init__(self, tk):
        self.tk = tk

    def get_rgba_visual(self):
        return "rgba-visual" if self.tk.rgba else None


class StyleContext:
    def __init__(self, widget):
        self.widget = widget

    def add_class(self, name):
        self.widget.classes.append(name)


class CssProvider:
    def __init__(self):
        self.data = None

    def load_from_data(self, data):
        self.data = data


class Widget:
    """Keeps children, CSS classes, handlers and every other call (calls: [(name, args)])."""

    def __init__(self, *args, **props):
        self.args = args
        self.props = dict(props)
        self.children = []
        self.classes = []
        self.handlers = []
        self.calls = []
        self.shown = False
        self.parent = None

    def connect(self, signal, func, *extra):
        self.handlers.append((signal, func, extra))
        return len(self.handlers)

    def emit(self, signal, *args):
        result = None
        for name, func, extra in list(self.handlers):
            if name == signal:
                result = func(self, *args, *extra)
        return result

    def get_style_context(self):
        return StyleContext(self)

    def pack_start(self, child, *_args):
        child.parent = self
        self.children.append(child)

    def add(self, child):
        child.parent = self
        self.children.append(child)

    def remove(self, child):
        self.children.remove(child)
        child.parent = None

    def get_children(self):
        return list(self.children)

    def show_all(self):
        self.shown = True

    def called(self, name):
        return [args for n, args in self.calls if n == name]

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def find(self, kind):
        return [w for w in self.walk() if isinstance(w, kind)]

    def texts(self):
        return [w.get_label() for w in self.find(Label)]

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def method(*args):
            self.calls.append((name, args))
        return method


class Label(Widget):
    def get_label(self):
        return self.props.get("label")


class Button(Widget):
    def get_label(self):
        return self.props.get("label")

    def click(self):
        return self.emit("clicked")


class ToggleButton(Button):
    def __init__(self, *args, **props):
        super().__init__(*args, **props)
        self.active = False

    def get_active(self):
        return self.active

    def set_active(self, active):
        # Like GTK: "toggled" only on a change
        if bool(active) != self.active:
            self.active = bool(active)
            self.emit("toggled")

    def click(self):
        self.set_active(not self.active)


class ComboBoxText(Widget):
    def __init__(self, *args, **props):
        super().__init__(*args, **props)
        self.items = []
        self.active_id = None

    def append(self, key, text):
        self.items.append((key, text))

    def set_active_id(self, key):
        if key not in dict(self.items):
            return False
        if key != self.active_id:
            self.active_id = key
            self.emit("changed")
        return True

    def get_active_id(self):
        return self.active_id


class Entry(Widget):
    def __init__(self, *args, **props):
        super().__init__(*args, **props)
        self.text = ""
        self.focused = False

    def get_text(self):
        return self.text

    def set_text(self, text):
        self.text = text

    def type(self, text):
        """The user typed: GTK emits search-changed (after its short delay)."""
        self.text = text
        self.emit("search-changed")

    def grab_focus(self):
        self.focused = True


class ListBoxRow(Widget):
    def get_index(self):
        return self.parent.children.index(self) if self.parent else -1


class ListBox(Widget):
    def __init__(self, *args, **props):
        super().__init__(*args, **props)
        self.selected = None

    def add(self, child):
        # GTK 3 wraps any other widget in a row
        if not isinstance(child, ListBoxRow):
            row = ListBoxRow()
            row.add(child)
            child = row
        super().add(child)

    def get_row_at_index(self, i):
        return self.children[i] if 0 <= i < len(self.children) else None

    def select_row(self, row):
        self.selected = row
        self.emit("row-selected", row)

    def get_selected_row(self):
        return self.selected

    def activate(self, row):
        return self.emit("row-activated", row)


class Grid(Widget):
    def __init__(self, *args, **props):
        super().__init__(*args, **props)
        self.cells = {}

    def attach(self, child, column, row, width, height):
        child.parent = self
        self.children.append(child)
        self.cells[(column, row)] = child

    def rows(self):
        n = 1 + max((r for _c, r in self.cells), default=-1)
        return [(self.cells[(0, r)].get_label(), self.cells[(1, r)].get_label())
                for r in range(n)]


def spawn_marked(*marker):
    """A process of the test's own whose command line carries marker (like a menu would), once
    the kernel shows it: right after exec, /proc/PID/cmdline can still read empty, and the
    menus' "is it ours" check would then (rightly) leave it alone."""
    import subprocess
    import time
    p = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()", *marker],
                         stdin=subprocess.PIPE)
    want = marker[0].encode() if marker else b"sys.stdin.read"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with open(f"/proc/{p.pid}/cmdline", "rb") as f:
                if want in f.read():
                    return p
        except OSError:
            pass
        time.sleep(0.002)
    p.kill()
    p.wait()
    raise AssertionError(f"{marker}: command line never showed up")
