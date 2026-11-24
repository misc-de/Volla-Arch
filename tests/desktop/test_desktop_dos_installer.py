# plinius-dos-installer's window (app.py): the icons of the games, where the file chooser starts,
# the questions the installer asks in the window, playing and removing games, choosing what to
# install, the pages while it installs and when it is done. GTK and libadwaita are recording
# fakes; the installer itself, DOSBox and ScummVM are not run (tests/apps/test_apps_dos_installer
# tests them). Every path the package keeps points into tmp_path.

import importlib
import importlib.util
import json
import subprocess
import sys
import threading
import types
from pathlib import Path

import gi
import pytest
from gi.repository import Gio, GLib

import desktop_fakes as fakes
from conftest import PKG, load_script

DOS = PKG / "plinius-dos-installer"


# ----- recording stand-ins for GTK and libadwaita -----

class W:
    """A widget: records its constructor arguments, the methods called on it and the handlers
    connected to it; children and a few properties behave like GTK's."""

    def __init__(self, *args, **kw):
        self.args, self.kw = args, kw
        self.calls, self.handlers = [], {}
        self.child = kw.get("child")
        self.active = False
        self.value = 0
        self.selected = kw.get("selected", 0)
        self.text = kw.get("text", "")

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def method(*a, **kw):
            self.calls.append((name, a))
            return W()
        return method

    def connect(self, signal, callback, *extra):
        self.handlers.setdefault(signal, []).append((callback, extra))
        return len(self.handlers)

    def emit(self, signal, *args):
        return [cb(self, *args, *extra) for cb, extra in self.handlers[signal]]

    def set_child(self, child):
        self.calls.append(("set_child", (child,)))
        self.child = child

    def get_child(self):
        return self.child

    def set_active(self, value):
        self.active = value

    def get_active(self):
        return self.active

    def get_value(self):
        return self.value

    def get_selected(self):
        return self.selected

    def get_text(self):
        return self.text

    def called(self, name):
        return [a for n, a in self.calls if n == name]


class _WMeta(type):
    def __getattr__(cls, name):
        if name.startswith("__"):
            raise AttributeError(name)
        sub = _WMeta(name, (W,), {})
        setattr(cls, name, sub)
        return sub


def wnamespace(name):
    return _WMeta(name, (W,), {})


def kind(widget):
    return type(widget).__name__


# ----- the package, with app.py under the fakes -----

@pytest.fixture(scope="module")
def m(tmp_path_factory):
    root = tmp_path_factory.mktemp("dosinstall-import")
    with pytest.MonkeyPatch.context() as mp:
        for var in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME",
                    "PLINIUS_DOS_GAMES"):
            mp.setenv(var, str(root / var.lower()))
        if "dosinstall" not in sys.modules:
            spec = importlib.util.spec_from_file_location(
                "dosinstall", DOS / "__init__.py", submodule_search_locations=[str(DOS)])
            pkg = importlib.util.module_from_spec(spec)
            sys.modules["dosinstall"] = pkg
            spec.loader.exec_module(pkg)
        names = {n: wnamespace(n) for n in ("Gtk", "Adw", "Gdk")}
        real = gi.require_version
        mp.setattr(gi, "require_version",
                   lambda ns, version: None if ns in names else real(ns, version))
        repository = sys.modules["gi.repository"]
        for n, obj in names.items():
            mp.setattr(repository, n, obj, raising=False)
            mp.setitem(sys.modules, "gi.repository." + n, obj)
        sys.modules.pop("dosinstall.app", None)
        app = importlib.import_module("dosinstall.app")
    return types.SimpleNamespace(app=app, library=sys.modules["dosinstall.library"],
                                 dosgames=sys.modules["dosinstall.dosgames"],
                                 scummvm=sys.modules["dosinstall.scummvm"],
                                 sources=sys.modules["dosinstall.sources"],
                                 install=sys.modules["dosinstall.install"])


class SyncThread:
    """threading.Thread that runs its target at start()."""

    def __init__(self, target, name=None, daemon=None):
        self.target = target

    def start(self):
        self.target()


@pytest.fixture
def env(m, tmp_path, monkeypatch):
    app = m.app
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(app, "STATE", tmp_path / "state/plinius-dos-installer/state.json")
    monkeypatch.setattr(app, "SCUMMVM_ICONS", tmp_path / "data/scummvm-icons")
    monkeypatch.setattr(m.dosgames, "ICONS", tmp_path / "data/icons")
    monkeypatch.setattr(m.dosgames, "APPS", tmp_path / "data/applications")
    monkeypatch.setattr(m.library, "GAMES", home / "Games/dos")
    monkeypatch.setattr(m.scummvm, "INI", tmp_path / "config/scummvm/scummvm.ini")
    glib = fakes.FakeGLib()
    monkeypatch.setattr(app, "GLib", glib)
    real_gio = Gio
    monkeypatch.setattr(app, "Gio", types.SimpleNamespace(
        File=real_gio.File, ApplicationFlags=real_gio.ApplicationFlags,
        ListStore=wnamespace("ListStore")))
    popen = []

    def fake_popen(cmd, **kw):
        popen.append((cmd, kw))
        return types.SimpleNamespace(pid=1)
    monkeypatch.setattr(app.subprocess, "Popen", fake_popen)
    runs = []
    monkeypatch.setattr(app.subprocess, "run", lambda cmd, **kw: runs.append(cmd))
    # Only the app's threads run at once (the module, not threading itself, is replaced)
    monkeypatch.setattr(app, "threading", types.SimpleNamespace(Thread=SyncThread,
                                                                Event=threading.Event))
    return types.SimpleNamespace(home=home, glib=glib, popen=popen, runs=runs, tmp=tmp_path)


def new_window(m):
    return m.app.Window(W())


def toasts(win):
    return [a[0].kw["title"] for a in win.toasts.called("add_toast")]


def run_idle(glib):
    """Runs the idle sources like the main loop would, oldest first."""
    for sid in sorted(glib.of_kind("idle")):
        glib.fire(sid)


def touch(path, data=b"x"):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def own_game(m, title, kind_="dosbox", targets=()):
    folder = m.library.GAMES / title
    folder.mkdir(parents=True)
    game = m.library.Game(title, kind_, folder, list(targets), "", "GAME.EXE")
    m.library.save(game)
    return game


# ----- icons, the file chooser's folder -----

def test_game_icon(m, env):
    app = m.app
    scumm = m.library.Game("DOTT", "scummvm", None, ["tentacle", "maniac"])
    assert app.game_icon(scumm) == "org.scummvm.scummvm"
    icon = touch(app.SCUMMVM_ICONS / "org.plinius.ScummVM.maniac-0123abcd.png")
    assert app.game_icon(scumm) == str(icon)
    game = own_game(m, "Keen")
    assert app.game_icon(game) == "dosbox"
    file = touch(m.dosgames.ICONS / f"{m.dosgames._app_id(game)}.png")
    assert app.game_icon(game) == str(file)
    assert app.game_icon(m.library.Game("x", "dosbox")) == "dosbox"


def test_game_icon_with_an_unreadable_icon_folder(m, env, monkeypatch):
    def denied(self, pattern):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(type(m.app.SCUMMVM_ICONS), "glob", denied)
    game = m.library.Game("DOTT", "scummvm", None, ["tentacle"])
    assert m.app.game_icon(game) == "org.scummvm.scummvm"


def test_icon_widget(m):
    image = m.app.icon_widget("/data/icon.png", 48)
    assert kind(image) == "new_from_file" and image.args == ("/data/icon.png",)
    assert image.called("set_pixel_size") == [(48,)]
    image = m.app.icon_widget("dosbox", 32)
    assert kind(image) == "new_from_icon_name" and image.args == ("dosbox",)


def test_start_folder(m, env):
    app = m.app
    assert app.start_folder() == env.home
    app.STATE.parent.mkdir(parents=True)
    games = env.tmp / "sd/Games"
    games.mkdir(parents=True)
    app.STATE.write_text(json.dumps({"folder": str(games)}))
    assert app.start_folder() == games
    for text in (json.dumps({"folder": str(env.tmp / "gone")}), "{broken", "[]", "{}",
                 json.dumps({"folder": None})):
        app.STATE.write_text(text)
        assert app.start_folder() == env.home


@pytest.mark.parametrize("path, is_folder, remembered", [
    # One level above the game's folder, where the other games are
    ("{home}/Games/DOTT/disk1.img", False, "{home}/Games"),
    ("{home}/Games/DOTT", True, "{home}/Games"),
    ("{tmp}/sd/Games/X/a.zip", False, "{tmp}/sd/Games"),
    # Never above the home folder
    ("{home}/disk1.img", False, "{home}"),
    ("{home}/Game", True, "{home}"),
    ("/", True, "{home}"),
])
def test_remember_folder(m, env, path, is_folder, remembered):
    fill = dict(home=env.home, tmp=env.tmp)
    m.app.remember_folder(path.format(**fill), is_folder)
    assert json.loads(m.app.STATE.read_text()) == {"folder": remembered.format(**fill)}


def test_remember_folder_without_a_writable_state(m, env):
    touch(m.app.STATE.parent.parent / "plinius-dos-installer")  # a file where the folder goes
    m.app.remember_folder(str(env.home / "Games/X"), True)
    assert not m.app.STATE.exists()


# ----- the installer's questions, answered in the window -----

def test_gui_ui_asks_in_the_main_loop(m, env):
    asked = []

    class Win:
        def set_status(self, text):
            asked.append(("status", text))

        def show_keyboard_hint(self, program, floppies, answer):
            asked.append(("hint", program, floppies))
            answer(True)

        def show_program_choice(self, candidates, title, answer):
            asked.append(("choice", candidates, title))
            answer(((candidates[0]), title))

        def show_disks(self, disks):
            asked.append(("disks", disks))

    ui = m.app.GuiUi(Win())
    ui.status("Reading the disks")
    ui.floppies("the disks")
    run_idle(env.glib)
    assert asked == [("status", "Reading the disks"), ("disks", "the disks")]
    results = []
    # The worker thread waits until the main loop showed the question and it was answered
    worker = threading.Thread(target=lambda: results.append(
        (ui.before_installer("INSTALL.EXE", True),
         ui.choose_program([("KEEN", "KEEN.EXE")], "Keen"))))
    worker.start()
    for _ in range(2):
        for _try in range(500):
            if env.glib.of_kind("idle"):
                break
            worker.join(timeout=0.01)
        run_idle(env.glib)
    worker.join(timeout=5)
    assert results == [(True, (("KEEN", "KEEN.EXE"), "Keen"))]
    assert asked[2:] == [("hint", "INSTALL.EXE", True), ("choice", [("KEEN", "KEEN.EXE")], "Keen")]
    # Logged lines only go to stdout
    ui.log("presets: 1 games")


# ----- the library -----

def test_window_lists_the_games(m, env):
    stale = m.library.GAMES / ".installing-1-999999999"
    stale.mkdir(parents=True)
    win = new_window(m)
    # Leftovers of an installation that was killed are gone
    assert not stale.exists()
    assert win.stack.called("set_visible_child_name") == [("empty",)]
    own_game(m, "Keen & <Friends>")
    win.refresh()
    assert win.list_page.called("remove")  # the old group goes
    rows = [a[0] for a in win.group.called("add")]
    assert len(rows) == 1 and rows[0].kw == {"title": "Keen &amp; &lt;Friends&gt;",
                                             "subtitle": "DOSBox"}
    assert win.stack.called("set_visible_child_name")[-1] == ("list",)
    # The library page refreshes whenever it shows again
    page = win.nav.called("add")[0][0]
    page.emit("showing")
    assert len(win.stack.called("set_visible_child_name")) == 3


def test_add_buttons_open_the_source_page(m, env):
    win = new_window(m)
    page = win.nav.called("add")[0][0]
    view = page.kw["child"]
    header = view.called("add_top_bar")[0][0]
    add = header.called("pack_start")[0][0]
    add.emit("clicked")
    empty = win.stack.called("add_named")[0][0]
    empty.child.emit("clicked")
    pushed = [a[0] for a in win.nav.called("push")]
    assert [p.kw["title"] for p in pushed] == ["Install a Game", "Install a Game"]


def game_buttons(win):
    row = win.group.called("add")[0][0]
    play, settings = (a[0] for a in row.called("add_suffix"))
    return row, play, settings


def settings_page(win, settings):
    """Opens a game's settings: (the page, its rows, the uninstall button)."""
    settings.emit("clicked")
    page = win.nav.called("push")[-1][0]
    prefs = page.kw["child"].called("set_content")[0][0]
    groups = [a[0] for a in prefs.called("add")]
    items = [a[0] for g in groups for a in g.called("add")]
    return page, items[:-1], items[-1]


def titled(rows, title):
    return next(r for r in rows if r.kw.get("title") == title)


def reopen(m, win, title):
    """The rows of the game's page, opened anew from the list."""
    win.refresh()
    row = next(a[0] for a in win.group.called("add") if a[0].kw["title"] == title)
    return settings_page(win, row.called("add_suffix")[1][0])[1]


def test_play(m, env):
    app = m.app
    win = new_window(m)
    m.scummvm.INI.parent.mkdir(parents=True)
    m.scummvm.INI.write_text("[tentacle]\ngameid=tentacle\npath=/g/DOTT\n"
                             "[maniac]\ngameid=maniac\npath=/g/DOTT/MANIAC\n")
    win.play(m.library.Game("DOTT", "scummvm", None, ["maniac", "tentacle", "gone"]))
    win.play(m.library.Game("Loom", "scummvm", None, ["loom"]))
    game = own_game(m, "Keen")
    win.refresh()
    _row, play, _remove = game_buttons(win)
    play.emit("clicked")
    key = m.dosgames._app_id(game)[len(m.dosgames.PREFIX):]
    # The game the folder is named after (the topmost path); a single target as it is
    assert [c for c, _kw in env.popen] == [["scummvm", "tentacle"], ["scummvm", "loom"],
                                          ["plinius-dos-installer", "--play", key]]
    assert all(kw == {"start_new_session": True} for _c, kw in env.popen)

    def missing(cmd, **kw):
        raise FileNotFoundError(2, "No such file or directory")
    app.subprocess.Popen = missing  # undone by the env fixture's monkeypatch
    win.play(game)
    assert toasts(win) == ["Could not start: No such file or directory"]


def test_a_game_has_its_settings_beside_play(m, env, monkeypatch):
    win = new_window(m)
    game = own_game(m, "Keen")
    m.scummvm.INI.parent.mkdir(parents=True)
    m.scummvm.INI.write_text("[loom]\ngameid=loom\ndescription=Loom\npath=/g/LOOM\n")
    win.refresh()
    rows = {r.kw["title"]: r for r in (a[0] for a in win.group.called("add"))}
    keen, loom = rows["Keen"], rows["Loom"]
    # Play, then the settings; a tap on the row plays
    for r in (keen, loom):
        assert kind(r) == "ActionRow" and r.called("set_activatable_widget")
        assert [(a[0].kw["icon_name"], a[0].kw["tooltip_text"]) for a in r.called("add_suffix")] \
            == [("media-playback-start-symbolic", "Play"), ("emblem-system-symbolic", "Settings")]
    page, switches, uninstall = settings_page(win, keen.called("add_suffix")[1][0])
    assert page.kw["title"] == "Keen"
    assert uninstall.kw["label"] == "Uninstall Game"
    assert uninstall.kw["css_classes"] == ["pill", "destructive-action"]
    switch = switches[0]
    assert kind(switch) == "SwitchRow" and switch.kw == {
        "title": "Touch Joystick", "subtitle": "Stick and buttons beside the picture",
        "active": True}
    switch.set_active(False)
    switch.emit("notify::active", None)
    assert m.library.load(game.folder).controls is False
    # Shown as saved the next time
    win.refresh()
    keen = next(a[0] for a in win.group.called("add") if a[0].kw["title"] == "Keen")
    _page, (again, *_rest), _u = settings_page(win, keen.called("add_suffix")[1][0])
    assert again.kw["active"] is False
    # Saving fails: said so
    game.folder.chmod(0o500)
    try:
        switch.set_active(True)
        switch.emit("notify::active", None)
    finally:
        game.folder.chmod(0o700)
    assert toasts(win) == ["Could not save: Permission denied"]
    # ScummVM games have no touch joystick, only the uninstalling
    shown = []
    monkeypatch.setattr(win, "ask_remove", shown.append)
    _page, switches, uninstall = settings_page(win, loom.called("add_suffix")[1][0])
    assert switches == []
    uninstall.emit("clicked")
    assert [g.title for g in shown] == ["Loom"]


def test_a_games_mouse_speed(m, env):
    win = new_window(m)
    game = own_game(m, "Doom")
    game.dosbox = {"cpu": {"cycles": "max"}, "sdl": {"sensitivity": "300"}}
    m.library.save(game)
    win.refresh()
    _row, _play, settings = game_buttons(win)
    _page, rows, _u = settings_page(win, settings)
    speed = titled(rows, "Mouse Speed")
    # In percent from 10 to 1000, in steps of ten, starting at the game's own
    assert kind(speed) == "SpinRow"
    assert {k: v for k, v in speed.kw.items() if k != "adjustment"} == {
        "title": "Mouse Speed", "subtitle": "How far a swipe moves the mouse, in percent",
        "digits": 0}
    assert speed.kw["adjustment"].args == (300, 10, 1000, 10, 100, 0)
    speed.value = 450.0
    speed.emit("notify::value", None)
    assert m.library.load(game.folder).dosbox == {"cpu": {"cycles": "max"},
                                                  "sdl": {"sensitivity": "450"}}
    # Back to DOSBox's own speed: nothing of it stays in the record
    speed.value = 100.0
    speed.emit("notify::value", None)
    assert m.library.load(game.folder).dosbox == {"cpu": {"cycles": "max"}}
    # Shown as saved the next time
    win.refresh()
    _row, _play, settings = game_buttons(win)
    _page, rows, _u = settings_page(win, settings)
    again = titled(rows, "Mouse Speed")
    assert again.kw["adjustment"].args[0] == 100
    # Saving fails: said so
    game.folder.chmod(0o500)
    try:
        again.value = 200.0
        again.emit("notify::value", None)
    finally:
        game.folder.chmod(0o700)
    assert toasts(win) == ["Could not save: Permission denied"]


def test_a_games_page_has_controls_then_how_dosbox_runs_it(m, env):
    win = new_window(m)
    own_game(m, "Keen")
    win.refresh()
    _row, _play, settings = game_buttons(win)
    settings.emit("clicked")
    prefs = win.nav.called("push")[-1][0].kw["child"].called("set_content")[0][0]
    groups = [a[0] for a in prefs.called("add")]
    assert [g.kw.get("title") for g in groups] == ["Controls", "Game", None]
    assert [(kind(a[0]), a[0].kw["title"]) for a in groups[0].called("add")] == [
        ("SwitchRow", "Touch Joystick"), ("ComboRow", "Stick"), ("SpinRow", "Mouse Speed")]
    # No setup beside Keen: no row for it
    assert [(kind(a[0]), a[0].kw["title"]) for a in groups[1].called("add")] == [
        ("ComboRow", "CPU Speed"), ("SwitchRow", "Sharp Pixels")]


def test_a_games_stick(m, env):
    win = new_window(m)
    game = own_game(m, "Keen")
    win.refresh()
    _row, _play, settings = game_buttons(win)
    stick = titled(settings_page(win, settings)[1], "Stick")
    assert stick.kw["subtitle"] == "The keys it presses"
    assert stick.kw["model"].args == (["Arrow Keys", "WASD", "Number Pad"],)
    # The arrow keys when the game has none of its own
    assert stick.kw["selected"] == 0
    stick.selected = 2
    stick.emit("notify::selected", None)
    assert m.library.load(game.folder).touch == {"stick": "numpad"}
    assert titled(reopen(m, win, "Keen"), "Stick").kw["selected"] == 2
    # Back to the arrow keys: the controls' own, nothing stays in the record
    stick.selected = 0
    stick.emit("notify::selected", None)
    assert m.library.load(game.folder).touch == {}


def test_a_games_cpu_speed(m, env):
    win = new_window(m)
    game = own_game(m, "Keen")
    win.refresh()
    _row, _play, settings = game_buttons(win)
    cpu = titled(settings_page(win, settings)[1], "CPU Speed")
    assert cpu.kw["subtitle"] == "Slower if the game is too fast"
    assert cpu.kw["model"].args == (["Automatic", "Slow (286)", "Medium (386)", "Fast (486)",
                                     "Maximum"],)
    assert cpu.kw["selected"] == 0
    cpu.selected = 1
    cpu.emit("notify::selected", None)
    assert m.library.load(game.folder).dosbox == {"cpu": {"cycles": "3000"}}
    cpu.selected = 4
    cpu.emit("notify::selected", None)
    assert m.library.load(game.folder).dosbox == {"cpu": {"cycles": "max"}}
    assert titled(reopen(m, win, "Keen"), "CPU Speed").kw["selected"] == 4
    cpu.selected = 0
    cpu.emit("notify::selected", None)
    assert m.library.load(game.folder).dosbox == {}
    # A speed of the game's preset that is none of the five: offered as it is, and kept
    game.dosbox = {"cpu": {"cycles": "fixed 5000"}}
    m.library.save(game)
    cpu = titled(reopen(m, win, "Keen"), "CPU Speed")
    assert cpu.kw["model"].args[0][5:] == ["fixed 5000"] and cpu.kw["selected"] == 5
    cpu.selected = 5
    cpu.emit("notify::selected", None)
    assert m.library.load(game.folder).dosbox == {"cpu": {"cycles": "fixed 5000"}}


def test_a_games_sharp_pixels(m, env):
    win = new_window(m)
    game = own_game(m, "Keen")
    win.refresh()
    _row, _play, settings = game_buttons(win)
    sharp = titled(settings_page(win, settings)[1], "Sharp Pixels")
    assert sharp.kw == {"title": "Sharp Pixels", "subtitle": "The picture without smoothing",
                        "active": False}
    sharp.set_active(True)
    sharp.emit("notify::active", None)
    assert m.library.load(game.folder).dosbox == {"sdl": {"output": "openglnb"}}
    assert titled(reopen(m, win, "Keen"), "Sharp Pixels").kw["active"] is True
    sharp.set_active(False)
    sharp.emit("notify::active", None)
    assert m.library.load(game.folder).dosbox == {}


def test_a_games_setup(m, env):
    win = new_window(m)
    game = own_game(m, "Keen")
    touch(game.folder / "game/SETUP.EXE")
    win.refresh()
    _row, _play, settings = game_buttons(win)
    setup = titled(settings_page(win, settings)[1], "Game Setup")
    assert kind(setup) == "ActionRow" and setup.kw == {
        "title": "Game Setup", "subtitle": "Starts SETUP.EXE, for sound and controls",
        "activatable": True}
    setup.emit("activated")
    key = m.dosgames._app_id(game)[len(m.dosgames.PREFIX):]
    assert [c for c, _kw in env.popen] == [["plinius-dos-installer", "--setup", key]]


@pytest.mark.parametrize("game, body", [
    (lambda m: own_game(m, "Keen"), "Its folder is deleted, saved games included."),
    (lambda m: own_game(m, "DOTT", "scummvm", ["tentacle"]),
     "Its files are deleted. Saved games stay in ScummVM."),
    (lambda m: m.library.Game("Loom", "scummvm", None, ["loom"]),
     "It is taken off the list; its files stay where they are."),
])
def test_ask_remove_explains_what_goes(m, env, game, body):
    win = new_window(m)
    win.ask_remove(game(m))
    dialog = DIALOGS[-1]
    assert dialog.kw["body"] == body
    assert dialog.called("add_response") == [("cancel", "Cancel"), ("remove", "Remove")]
    assert dialog.called("set_close_response") == [("cancel",)]


# Every Adw.AlertDialog made by a test, newest last
DIALOGS = []


@pytest.fixture(autouse=True)
def record_dialogs(m, monkeypatch):
    DIALOGS.clear()

    class AlertDialog(W):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            DIALOGS.append(self)
    monkeypatch.setattr(m.app.Adw, "AlertDialog", AlertDialog)


def test_remove_a_game(m, env, monkeypatch):
    monkeypatch.setenv("PLINIUS_SCUMMVM_LAUNCHERS", "/opt/test/launchers")
    game = own_game(m, "Keen")
    win = new_window(m)
    _row, _play, settings = game_buttons(win)
    _page, _switches, uninstall = settings_page(win, settings)
    uninstall.emit("clicked")
    dialog = DIALOGS[-1]
    dialog.emit("response", "cancel")
    assert game.folder.exists() and env.runs == [] and not win.nav.called("pop_to_tag")
    dialog.emit("response", "remove")
    assert not game.folder.exists() and env.runs == [["/opt/test/launchers"]]
    # Back from the settings page to the list
    assert win.nav.called("pop_to_tag") == [("library",)]
    assert win.stack.called("set_visible_child_name")[-1] == ("empty",)


def test_remove_errors(m, env, monkeypatch):
    win = new_window(m)
    game = m.library.Game("Loom", "scummvm", None, ["loom"])

    def denied(_game):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(m.library, "remove", denied)
    win.ask_remove(game)
    DIALOGS[-1].emit("response", "remove")
    assert toasts(win) == ["Could not remove: Permission denied"]

    def odd(_game):
        raise OSError("odd")
    monkeypatch.setattr(m.library, "remove", odd)
    win.ask_remove(game)
    DIALOGS[-1].emit("response", "remove")
    assert toasts(win)[-1] == "Could not remove: odd"


@pytest.mark.parametrize("error", [FileNotFoundError(2, "missing"),
                                   subprocess.TimeoutExpired("x", 60)])
def test_update_launchers_failing_is_no_error(m, monkeypatch, error):
    def run(cmd, **kw):
        raise error
    monkeypatch.setattr(m.app.subprocess, "run", run)
    m.app.Window._update_launchers()


# ----- choosing what to install -----

def source_rows(win):
    page = win._source_page()
    content = page.kw["child"].called("set_content")[0][0]
    group = content.called("add")[0][0]
    return [a[0] for a in group.called("add")]


def test_source_page_offers_every_kind(m, env):
    win = new_window(m)
    rows = source_rows(win)
    assert [r.kw["title"] for r in rows] == ["Floppy Disk Images", "CD Image", "ZIP Archive",
                                            "GOG Installer", "Folder"]
    assert all(r.kw["activatable"] for r in rows)


class Chooser(W):
    """Gtk.FileDialog: remembers how it was opened."""

    last = None

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        Chooser.last = self


def picked(*paths):
    return [types.SimpleNamespace(get_path=lambda p=p: p) for p in paths]


def test_pick_floppies(m, env, monkeypatch):
    monkeypatch.setattr(m.app.Gtk, "FileDialog", Chooser)
    started = []
    monkeypatch.setattr(m.app.Window, "start", lambda self, source: started.append(source))
    win = new_window(m)
    rows = source_rows(win)
    rows[0].emit("activated")
    d = Chooser.last
    assert d.kw == {"title": "Floppy Disk Images", "modal": True}
    folder = d.called("set_initial_folder")[0][0]
    assert folder.get_path() == str(env.home)
    flt = d.called("set_default_filter")[0][0]
    assert flt.called("add_suffix") == [("img",), ("ima",), ("vfd",), ("flp",)]
    parent, cancel, chosen = d.called("open_multiple")[0]
    assert parent is win and cancel is None
    disk1 = touch(env.home / "Games/DOTT/disk1.img")
    disk2 = touch(env.home / "Games/DOTT/disk2.img")
    files = picked(str(disk1))
    d.open_multiple_finish = lambda result: types.SimpleNamespace(
        get_n_items=lambda: len(files), get_item=lambda i: files[i])
    chosen(d, "result")
    assert started == [m.sources.Source("floppies", [str(disk1), str(disk2)])]
    # The next search starts above the game's folder
    assert json.loads(m.app.STATE.read_text()) == {"folder": str(env.home / "Games")}


def test_pick_single_file_and_folder(m, env, monkeypatch):
    monkeypatch.setattr(m.app.Gtk, "FileDialog", Chooser)
    started = []
    monkeypatch.setattr(m.app.Window, "start", lambda self, source: started.append(source))
    win = new_window(m)
    rows = source_rows(win)
    rows[2].emit("activated")
    d = Chooser.last
    chosen = d.called("open")[0][2]
    d.open_finish = lambda result: picked("/x/keen.zip")[0]
    chosen(d, "result")
    rows[4].emit("activated")
    d = Chooser.last
    assert d.called("set_default_filter") == []          # a folder: no file filter
    chosen = d.called("select_folder")[0][2]
    d.select_folder_finish = lambda result: picked(str(env.home))[0]
    chosen(d, "result")
    assert started == [m.sources.Source("zip", ["/x/keen.zip"]),
                       m.sources.Source("folder", [str(env.home)])]


def test_pick_cancelled_empty_or_not_matching(m, env, monkeypatch):
    monkeypatch.setattr(m.app.Gtk, "FileDialog", Chooser)
    started = []
    monkeypatch.setattr(m.app.Window, "start", lambda self, source: started.append(source))
    win = new_window(m)
    rows = source_rows(win)
    rows[1].emit("activated")
    d = Chooser.last
    chosen = d.called("open")[0][2]

    def cancelled(result):
        raise GLib.Error("Dismissed by user")
    d.open_finish = cancelled
    chosen(d, "result")
    d.open_finish = lambda result: types.SimpleNamespace(get_path=lambda: None)  # not local
    chosen(d, "result")
    d.open_finish = lambda result: picked("/x/readme.txt")[0]
    chosen(d, "result")
    assert started == [] and toasts(win) == ["These files do not go together."]


# ----- installing -----

def test_start_installs_in_the_worker(m, env, monkeypatch):
    win = new_window(m)
    game = own_game(m, "Keen")
    got = []
    monkeypatch.setattr(m.app.installer, "install", lambda source, ui: got.append(
        (source, ui)) or game)
    finished = []
    monkeypatch.setattr(m.app.Window, "finished", lambda self, g, e: finished.append((g, e)))
    source = m.sources.Source("zip", ["/x/Keen <1>.zip"])
    win.start(source)
    assert win.busy and win.status_page.kw["description"] == "Keen &lt;1&gt;"
    assert isinstance(got[0][1], m.app.GuiUi) and got[0][1].win is win
    pushed = win.nav.called("push")[-1][0]
    assert pushed.kw["can_pop"] is False
    run_idle(env.glib)
    assert finished == [(game, None)]
    win.set_status("Unpacking")
    assert win.status_page.called("set_title") == [("Unpacking",)]


@pytest.mark.parametrize("error, shown", [
    (lambda m: m.install.Cancelled(), None),
    (lambda m: RuntimeError("The installer did not install anything."),
     "The installer did not install anything."),
    (lambda m: KeyError(), "KeyError"),
])
def test_start_reports_errors(m, env, monkeypatch, capsys, error, shown):
    win = new_window(m)

    def install(source, ui):
        raise error(m)
    monkeypatch.setattr(m.app.installer, "install", install)
    finished = []
    monkeypatch.setattr(m.app.Window, "finished", lambda self, g, e: finished.append((g, e)))
    win.start(m.sources.Source("zip", ["/x/a.zip"]))
    run_idle(env.glib)
    assert finished == [(None, shown)]


def test_finished_with_an_error_or_cancelled(m, env):
    win = new_window(m)
    win.busy = True
    win.finished(None, None)
    assert not win.busy and win.nav.called("pop_to_tag") == [("library",)] and DIALOGS == []
    win.finished(None, "7z is missing")
    assert DIALOGS[-1].kw == {"heading": "Installation Failed", "body": "7z is missing"}
    assert DIALOGS[-1].called("present") == [(win,)]


def finished_page(win):
    pages = win.nav.called("replace")[-1][0]
    view = pages[1].kw["child"]
    return pages, view.called("set_content")[0][0]


def test_finished_shows_the_game(m, env, monkeypatch):
    win = new_window(m)
    game = own_game(m, "Keen")
    win.finished(game, None)
    pages, status = finished_page(win)
    assert pages[0] is not None and pages[1].kw["title"] == "Installed"
    assert status.kw["title"] == "“Keen” Is Installed"
    assert status.called("set_icon_name") == [("dosbox",)]
    play, done = (a[0] for a in status.child.called("append"))
    play.emit("clicked")
    assert env.popen[-1][0][:2] == ["plinius-dos-installer", "--play"]
    done.emit("clicked")
    assert win.nav.called("pop_to_tag") == [("library",), ("library",)]
    # With the game's own icon file
    touch(m.dosgames.ICONS / f"{m.dosgames._app_id(game)}.png", b"\x89PNG")
    win.finished(game, None)
    _pages, status = finished_page(win)
    assert kind(status.called("set_paintable")[0][0]) == "new_from_filename"

    def broken(path):
        raise GLib.Error("not an image")
    monkeypatch.setattr(m.app.Gdk.Texture, "new_from_filename", broken)
    win.finished(game, None)
    _pages, status = finished_page(win)
    assert status.called("set_icon_name") == [(m.app.APP_ID,)]


def test_show_disks(m, env, monkeypatch):
    win = new_window(m)
    win.status_page = W()

    class Disks:
        disks = [1, 2]
        current = 0
        fail = None

        def insert(self, index):
            if self.fail:
                raise self.fail
            self.current = index
    disks = Disks()
    win.show_disks(disks)
    box = win.status_page.child
    _spinner, label, button, _hint = (a[0] for a in box.called("append"))
    assert label.called("set_label") == [("Disk 1 of 2",)]
    assert button.called("set_sensitive") == [(True,)]
    sid = env.glib.of_kind("timeout")[0]
    assert env.glib.sources[sid][1] == 500
    button.emit("clicked")
    assert disks.current == 1 and label.called("set_label")[-1] == ("Disk 2 of 2",)
    assert button.called("set_sensitive")[-1] == (False,)
    disks.fail = PermissionError(13, "Permission denied")
    button.emit("clicked")
    assert toasts(win) == ["Could not insert the disk: Permission denied"]
    disks.fail = OSError("odd")
    button.emit("clicked")
    assert toasts(win)[-1] == "Could not insert the disk: odd"
    assert env.glib.fire(sid) is True
    # Once the installer is done the page shows a spinner again and the updates stop
    win.show_disks(None)
    assert kind(win.status_page.child) == "Spinner"
    assert env.glib.fire(sid) is False


def question(win):
    page = win.nav.called("push")[-1][0]
    return page, page.kw["child"].called("set_content")[0][0]


@pytest.mark.parametrize("floppies", [False, True])
def test_keyboard_hint(m, env, floppies):
    win = new_window(m)
    answers = []
    win.show_keyboard_hint("SETUP <1>.EXE", floppies, answers.append)
    page, status = question(win)
    assert page.kw["title"] == "Installer" and page.kw["can_pop"] is False
    text = status.kw["description"]
    assert "starts SETUP &lt;1&gt;.EXE." in text
    assert ("The disks change by themselves" in text) == floppies
    go, cancel = (a[0] for a in status.child.called("append"))
    go.emit("clicked")
    cancel.emit("clicked")
    assert answers == [True, False] and len(win.nav.called("pop")) == 2


def test_program_choice(m, env):
    win = new_window(m)
    answers = []
    candidates = [("KEEN", "KEEN.EXE")] + [("", f"P{i}.EXE") for i in range(14)]
    win.show_program_choice(candidates, "Keen", answers.append)
    _page, widget = question(win)
    name_group, group, done_group = (a[0] for a in widget.called("add"))
    entry = name_group.called("add")[0][0]
    rows = [a[0] for a in group.called("add")]
    # At most twelve, the likely one ticked
    assert len(rows) == 12 and rows[0].kw["subtitle"] == "C:\\KEEN" and rows[1].kw["subtitle"] == "C:\\"
    checks = [r.kw["activatable_widget"] for r in rows]
    assert checks[0].active and checks[1].kw["group"] is checks[0]
    add, cancel = (a[0] for a in done_group.called("add")[0][0].called("append"))
    assert add.kw["sensitive"] is True
    add.emit("clicked")
    checks[0].active, checks[2].active = False, True
    entry.text = "  Commander Keen  "
    add.emit("clicked")
    entry.text = "   "
    add.emit("clicked")
    cancel.emit("clicked")
    assert answers == [(("KEEN", "KEEN.EXE"), "Keen"), (("", "P1.EXE"), "Commander Keen"),
                       (("", "P1.EXE"), "Keen"), None]


def test_program_choice_without_candidates(m, env):
    win = new_window(m)
    win.show_program_choice([], "Keen", lambda a: None)
    _page, widget = question(win)
    group = widget.called("add")[1][0]
    done_group = widget.called("add")[2][0]
    assert group.called("set_description") == [("No start file was found.",)]
    add = done_group.called("add")[0][0].called("append")[0][0]
    assert add.kw["sensitive"] is False


# ----- the application -----

def test_app_activates_one_window(m, env):
    app = m.app.App()
    assert app.kw["application_id"] == "org.plinius.DOSInstaller"
    app.props = types.SimpleNamespace(active_window=None)
    app.do_activate()
    app.props.active_window = existing = W()
    app.do_activate()
    assert existing.called("present") == [()]
    assert m.app.main() is not None


def test_launcher_script_opens_the_window(m, monkeypatch):
    monkeypatch.setattr(m.app, "main", lambda: 0)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "argv", ["plinius-dos-installer"])
    with pytest.raises(SystemExit) as e:
        load_script(DOS / "plinius-dos-installer", "plinius_dos_installer_window")
    assert e.value.code == 0
