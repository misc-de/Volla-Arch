# The screencast chooser of xdg-desktop-portal-wlr-plinius (pkgbuilds/xdg-desktop-portal-wlr-plinius/
# chooser): reading the sources xdpw offers, the quick setting's consent stamp, the rows of the
# question and its answers (a row, Cancel, closing, the timeout). GTK and libadwaita are recording
# fakes; GLib's timeouts are driven by hand. Nothing opens a window.

import os
import subprocess
import sys
import time
import types

import gi
import pytest
from gi.repository import Gio

import desktop_fakes as fakes
from conftest import PKG, load_script

CHOOSER = PKG / "xdg-desktop-portal-wlr-plinius" / "chooser"

DSI = "Monitor: DSI-1 Unknown Unknown"
VNC = "Monitor: HEADLESS-1 Headless output 1"
FIREFOX = "Window: Start page — Mozilla Firefox (firefox)"


@pytest.fixture
def chooser():
    return load_script(CHOOSER, "screencast_chooser")


# ----- what xdpw offers -----

def test_parse_reads_monitors_and_windows(chooser):
    lines = [DSI + "\n", VNC + "\n", FIREFOX + "\n", "Window: no app id\n",
             "Monitor: \n", "something else\n", "\n"]
    assert chooser.parse(lines) == [
        ("monitor", "DSI-1", DSI),
        ("monitor", "HEADLESS-1", VNC),
        ("window", ("Start page — Mozilla Firefox", "firefox"), FIREFOX),
        ("window", ("no app id", ""), "Window: no app id"),
    ]


def test_parse_keeps_a_title_with_parentheses(chooser):
    line = "Window: Notes (draft) (org.gnome.TextEditor)"
    assert chooser.parse([line]) == [
        ("window", ("Notes (draft)", "org.gnome.TextEditor"), line)]


def test_built_in_is_the_phone_display_wherever_it_is_listed(chooser):
    sources = chooser.parse([VNC, FIREFOX, DSI])
    assert chooser.built_in(sources) == DSI
    assert chooser.built_in(chooser.parse([VNC, FIREFOX])) is None
    assert chooser.built_in([]) is None


@pytest.mark.parametrize("kind, name, expected", [
    ("monitor", "DSI-1", ("Phone Screen", "", "phone-symbolic")),
    ("monitor", "eDP-1", ("Phone Screen", "", "phone-symbolic")),
    ("monitor", "HEADLESS-1",
     ("Desktop", "The extended desktop over VNC", "video-display-symbolic")),
    ("monitor", "HDMI-A-1", ("Screen HDMI-A-1", "", "video-display-symbolic")),
    ("window", ("Inbox", "org.gnome.Geary"), ("Inbox", "org.gnome.Geary", "focus-windows-symbolic")),
    ("window", ("", "org.gnome.Geary"), ("org.gnome.Geary", "", "focus-windows-symbolic")),
    ("window", ("", ""), ("Window", "", "focus-windows-symbolic")),
])
def test_describe(chooser, kind, name, expected):
    assert chooser.describe(kind, name) == expected


# ----- the quick setting's consent -----

def test_stamp_path(chooser):
    assert chooser.stamp_path({"XDG_RUNTIME_DIR": "/run/user/1000"}) == \
        chooser.Path("/run/user/1000/plinius-screencast-consent")
    assert chooser.stamp_path({}) == \
        chooser.Path(f"/run/user/{os.getuid()}/plinius-screencast-consent")


def _stamp(tmp_path, age, content=""):
    stamp = tmp_path / "plinius-screencast-consent"
    stamp.write_text(content)
    t = time.time() - age
    os.utime(stamp, (t, t))
    return stamp


def test_take_stamp_fresh_once(chooser, tmp_path):
    stamp = _stamp(tmp_path, 3)
    assert chooser.take_stamp(stamp) == ""
    assert not stamp.exists()
    # One stamp, one screencast
    assert chooser.take_stamp(stamp) is None


def test_take_stamp_names_a_screen(chooser, tmp_path):
    assert chooser.take_stamp(_stamp(tmp_path, 1, "HEADLESS-1\n")) == "HEADLESS-1"


@pytest.mark.parametrize("age", [16, 600, -5])
def test_take_stamp_too_old_or_from_the_future(chooser, tmp_path, age):
    stamp = _stamp(tmp_path, age, "HEADLESS-1")
    assert chooser.take_stamp(stamp) is None
    assert not stamp.exists()


def test_take_stamp_that_cannot_be_removed_still_counts(chooser, tmp_path):
    stamp = tmp_path / "plinius-screencast-consent"
    stamp.mkdir()
    assert chooser.take_stamp(stamp, now=stamp.stat().st_mtime + 1) == ""


def test_monitor_by_name(chooser):
    sources = chooser.parse([DSI, VNC, FIREFOX])
    assert chooser.monitor(sources, "HEADLESS-1") == VNC
    # Windows are no screens, and a missing screen is no other one
    assert chooser.monitor(sources, "firefox") is None
    assert chooser.monitor(sources, "HEADLESS-2") is None


# ----- main -----

class Out:
    def __init__(self):
        self.text = ""

    def write(self, s):
        self.text += s


def _main(chooser, tmp_path, lines, answer=None):
    asked = []

    def ask(sources):
        asked.append(sources)
        return answer

    out = Out()
    rc = chooser.main(stdin=lines, stdout=out, env={"XDG_RUNTIME_DIR": str(tmp_path)}, ask=ask)
    return rc, out.text, asked


def test_main_with_consent_shares_the_phone_without_asking(chooser, tmp_path):
    stamp = _stamp(tmp_path, 1)
    assert _main(chooser, tmp_path, [VNC, DSI]) == (0, DSI + "\n", [])
    assert not stamp.exists()


def test_main_with_consent_but_no_phone_display_asks(chooser, tmp_path):
    _stamp(tmp_path, 1)
    rc, text, asked = _main(chooser, tmp_path, [VNC], answer=VNC)
    assert (rc, text) == (0, VNC + "\n")
    assert asked == [[("monitor", "HEADLESS-1", VNC)]]


def test_main_without_consent_asks(chooser, tmp_path):
    rc, text, asked = _main(chooser, tmp_path, [DSI, VNC], answer=VNC)
    assert (rc, text) == (0, VNC + "\n")
    assert len(asked) == 1


def test_main_with_an_old_stamp_asks(chooser, tmp_path):
    _stamp(tmp_path, 60)
    rc, text, asked = _main(chooser, tmp_path, [DSI], answer=DSI)
    assert (rc, text, len(asked)) == (0, DSI + "\n", 1)


def test_main_cancelled_prints_nothing(chooser, tmp_path):
    assert _main(chooser, tmp_path, [DSI], answer=None) == \
        (1, "", [[("monitor", "DSI-1", DSI)]])


def test_main_with_a_named_screen_shares_it_without_asking(chooser, tmp_path):
    stamp = _stamp(tmp_path, 1, "HEADLESS-1")
    assert _main(chooser, tmp_path, [DSI, VNC]) == (0, VNC + "\n", [])
    assert not stamp.exists()


def test_main_with_a_named_screen_that_is_gone_shares_nothing(chooser, tmp_path):
    # Better no picture than the phone's screen in place of the desktop
    _stamp(tmp_path, 1, "HEADLESS-1")
    assert _main(chooser, tmp_path, [DSI]) == (1, "", [])


def test_main_with_nothing_to_share_does_not_ask(chooser, tmp_path):
    _stamp(tmp_path, 1)
    assert _main(chooser, tmp_path, ["\n"]) == (1, "", [])


def test_runs_as_a_program(tmp_path):
    # Nothing offered: no window, no line, exit status 1 (not 127, which xdpw takes as failure)
    env = {"PATH": os.environ["PATH"], "XDG_RUNTIME_DIR": str(tmp_path)}
    r = subprocess.run([sys.executable, "-P", str(CHOOSER)], input="", env=env,
                       capture_output=True, text=True, timeout=60)
    assert (r.returncode, r.stdout, r.stderr) == (1, "", "")


# ----- the question -----

class Widget:
    """Records its properties, the handlers connected to it and the methods called on it."""

    made = []

    def __init__(self, **kw):
        self.kw = kw
        self.handlers = {}
        self.calls = []
        Widget.made.append(self)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *a, **kw: self.calls.append((name, a))

    def connect(self, signal, callback):
        self.handlers[signal] = callback

    def emit(self, signal, *args):
        return self.handlers[signal](self, *args)


class App(Widget):
    """Adw.Application: run() activates, then lets the test act like the person at the phone."""

    act = None

    def run(self, argv):
        self.argv = argv
        self.quits = 0
        self.emit("activate")
        App.act(self)
        return 0

    def quit(self):
        self.quits += 1


def _kind(name):
    return type(name, (Widget,), {})


@pytest.fixture
def ui(monkeypatch, chooser):
    """Fake Gtk/Adw while ask() imports them; returns a function to find made widgets."""
    Widget.made = []
    glib = fakes.FakeGLib()
    Adw = types.SimpleNamespace(
        Application=App, **{n: _kind(n) for n in (
            "ApplicationWindow", "HeaderBar", "ActionRow", "StatusPage", "Clamp", "ToolbarView")})
    Gtk = types.SimpleNamespace(
        SelectionMode=types.SimpleNamespace(NONE="none"),
        **{n: _kind(n) for n in ("Button", "ListBox", "Image")})
    real = gi.require_version
    monkeypatch.setattr(gi, "require_version",
                        lambda ns, v: None if ns in ("Gtk", "Adw") else real(ns, v))
    repository = sys.modules["gi.repository"]
    for name, obj in (("Adw", Adw), ("Gtk", Gtk), ("GLib", glib)):
        monkeypatch.setattr(repository, name, obj, raising=False)
        monkeypatch.setitem(sys.modules, "gi.repository." + name, obj)

    def find(kind):
        return [w for w in Widget.made if type(w).__name__ == kind]
    find.glib = glib
    return find


def _ask(chooser, ui, act, sources=(DSI, VNC, FIREFOX)):
    App.act = act
    return chooser.ask(chooser.parse(sources))


def test_ask_shows_a_row_per_source(chooser, ui):
    def act(app):
        assert app.kw["application_id"] == "org.plinius.ScreencastChooser"
        # Several questions at once each get a window
        assert app.kw["flags"] == Gio.ApplicationFlags.NON_UNIQUE
        rows = ui("ActionRow")
        assert [(r.kw["title"], r.kw["subtitle"]) for r in rows] == [
            ("Phone Screen", ""), ("Desktop", "The extended desktop over VNC"),
            ("Start page — Mozilla Firefox", "firefox")]
        assert all(r.kw["activatable"] for r in rows)
        (win,) = ui("ApplicationWindow")
        assert win.kw["title"] == "Share Screen"
        assert ("present", ()) in win.calls
        (page,) = ui("StatusPage")
        assert page.kw["title"] == "Share Your Screen?"
        assert "after a minute" in page.kw["description"]
        (button,) = ui("Button")
        assert button.kw["label"] == "Cancel"
        app.quit()

    assert _ask(chooser, ui, act) is None


def test_ask_tap_on_a_row_shares_it(chooser, ui):
    def act(app):
        ui("ActionRow")[1].emit("activated")
        assert app.quits == 1

    assert _ask(chooser, ui, act) == VNC


def test_ask_cancel(chooser, ui):
    def act(app):
        ui("Button")[0].emit("clicked")
        assert app.quits == 1

    assert _ask(chooser, ui, act) is None


def test_ask_closing_the_window_cancels(chooser, ui):
    def act(app):
        # False: GTK closes the window
        assert ui("ApplicationWindow")[0].emit("close-request") is False

    assert _ask(chooser, ui, act) is None


def test_ask_cancels_itself_after_the_timeout(chooser, ui):
    def act(app):
        (sid,) = ui.glib.of_kind("seconds")
        assert ui.glib.sources[sid][1] == chooser.TIMEOUT == 60
        assert ui.glib.fire(sid) is False
        assert app.quits == 1

    assert _ask(chooser, ui, act) is None


def test_ask_first_answer_wins(chooser, ui):
    def act(app):
        ui("ActionRow")[0].emit("activated")
        # The timeout or a second tap before the window is gone change nothing
        ui.glib.fire(ui.glib.of_kind("seconds")[0])
        ui("ActionRow")[2].emit("activated")

    assert _ask(chooser, ui, act) == DSI
