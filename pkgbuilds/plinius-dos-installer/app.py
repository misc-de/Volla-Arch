"""The DOS Installer's window: the games, and installing one from what the user picks."""

import gettext
import json
import os
import subprocess
import threading
import traceback
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import install as installer  # noqa: E402
from . import dosgames, library, presets, sources  # noqa: E402
from .scummvm import DATA  # noqa: E402

_ = gettext.translation("plinius-dos-installer", fallback=True).gettext

APP_ID = "org.plinius.DOSInstaller"
# Where the next search for a game starts
STATE = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") \
    / "plinius-dos-installer/state.json"
SCUMMVM_ICONS = DATA / "plinius-dos-installer/scummvm-icons"

SOURCES = [
    # kind, title, subtitle, icon, file patterns, several files, folder
    ("floppies", _("Floppy Disk Images"),
     _("Disk 1 is enough, the other disks in its folder come along"),
     "media-floppy-symbolic", ["*.img", "*.ima", "*.vfd", "*.flp"], True, False),
    ("cd", _("CD Image"), _(".iso, or .cue with its tracks"),
     "media-optical-symbolic", ["*.iso", "*.cue"], False, False),
    ("zip", _("ZIP Archive"), _("A packed game, such as a freeware download"),
     "package-x-generic-symbolic", ["*.zip"], False, False),
    ("gog", _("GOG Installer"), _("setup_….exe from GOG"),
     "application-x-executable-symbolic", ["*.exe"], False, False),
    ("folder", _("Folder"), _("The game's files, or a copy of its disks"),
     "folder-symbolic", None, False, True),
]


def game_icon(game):
    """The icon the game's launcher shows: a file, or an icon name."""
    if game.kind == "scummvm":
        for target in game.targets:
            try:
                for f in sorted(SCUMMVM_ICONS.glob(f"org.plinius.ScummVM.{target}-*")):
                    return str(f)
            except OSError:
                pass
        return "org.scummvm.scummvm"
    if game.folder:
        icon = dosgames.ICONS / f"{dosgames._app_id(game)}.png"
        if icon.exists():
            return str(icon)
    return "dosbox"


def icon_widget(icon, size):
    if icon.startswith("/"):
        image = Gtk.Image.new_from_file(icon)
    else:
        image = Gtk.Image.new_from_icon_name(icon)
    image.set_pixel_size(size)
    return image


def start_folder():
    """The home folder at first, later the folder remembered by remember_folder()."""
    try:
        folder = Path(json.loads(STATE.read_text())["folder"])
    except (OSError, ValueError, KeyError, TypeError):
        return Path.home()
    return folder if folder.is_dir() else Path.home()


def remember_folder(path, is_folder):
    """The next search starts one level above the game's folder, where the other games most
    likely are; never above the home folder."""
    game = Path(path) if is_folder else Path(path).parent
    base, home = game.parent, Path.home()
    if base in home.parents or base == home.parent:
        base = home if game == home or game in home.parents else game
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"folder": str(base)}) + "\n")
        os.replace(tmp, STATE)
    except OSError:
        pass


class GuiUi(installer.Ui):
    """Answers the installer's questions in the window; runs in the worker thread and waits
    for the main loop."""

    def __init__(self, win):
        self.win = win

    def _ask(self, show, *args):
        done = threading.Event()
        box = {}

        def answer(value):
            box["value"] = value
            done.set()

        GLib.idle_add(lambda: show(*args, answer) and False)
        done.wait()
        return box.get("value")

    def status(self, text):
        GLib.idle_add(self.win.set_status, text)

    def log(self, text):
        print(text, flush=True)

    def before_installer(self, program, floppies=False):
        return self._ask(self.win.show_keyboard_hint, program, floppies)

    def choose_program(self, candidates, title):
        return self._ask(self.win.show_program_choice, candidates, title)

    def floppies(self, disks):
        GLib.idle_add(self.win.show_disks, disks)


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=_("DOS Installer"), default_width=420,
                         default_height=760)
        self.toasts = Adw.ToastOverlay()
        self.nav = Adw.NavigationView()
        self.toasts.set_child(self.nav)
        self.set_content(self.toasts)
        self.busy = False
        library.remove_leftovers()
        self.nav.add(self._library_page())
        self.refresh()

    # Library

    def _library_page(self):
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text=_("Install a Game"))
        add.connect("clicked", lambda *_a: self.nav.push(self._source_page()))
        header.pack_start(add)
        view.add_top_bar(header)
        self.stack = Gtk.Stack()
        empty = Adw.StatusPage(icon_name=APP_ID, title=_("No Games Yet"),
                               description=_("Install a DOS game from floppy disk images, a CD "
                                             "image, a ZIP archive, a GOG installer or a "
                                             "folder."))
        button = Gtk.Button(label=_("Install a Game"), halign=Gtk.Align.CENTER,
                            css_classes=["pill", "suggested-action"])
        button.connect("clicked", lambda *_a: self.nav.push(self._source_page()))
        empty.set_child(button)
        self.stack.add_named(empty, "empty")
        self.list_page = Adw.PreferencesPage()
        self.stack.add_named(self.list_page, "list")
        self.group = None
        view.set_content(self.stack)
        page = Adw.NavigationPage(title=_("DOS Installer"), tag="library", child=view)
        page.connect("showing", lambda *_a: self.refresh())
        return page

    def refresh(self):
        if self.group:
            self.list_page.remove(self.group)
        self.group = Adw.PreferencesGroup(title=_("Games"))
        self.list_page.add(self.group)
        games = library.games()
        for game in games:
            self.group.add(self._game_row(game))
        self.stack.set_visible_child_name("list" if games else "empty")

    def _game_row(self, game):
        row = Adw.ActionRow(title=GLib.markup_escape_text(game.title),
                            subtitle="ScummVM" if game.kind == "scummvm" else "DOSBox")
        row.add_prefix(icon_widget(game_icon(game), 48))
        play = Gtk.Button(icon_name="media-playback-start-symbolic", tooltip_text=_("Play"),
                          valign=Gtk.Align.CENTER, css_classes=["flat"])
        play.connect("clicked", lambda *_a: self.play(game))
        settings = Gtk.Button(icon_name="emblem-system-symbolic", tooltip_text=_("Settings"),
                              valign=Gtk.Align.CENTER, css_classes=["flat"])
        settings.connect("clicked", lambda *_a: self.nav.push(self._settings_page(game)))
        row.add_suffix(play)
        row.add_suffix(settings)
        row.set_activatable_widget(play)
        return row

    def _settings_page(self, game):
        """A game's own page: for a DOSBox game of the DOS Installer's own its controls (touch
        joystick, stick, mouse speed) and how DOSBox runs it (CPU speed, sharp pixels, its
        setup); ScummVM games have none of it. Below, uninstalling the game."""
        page = Adw.PreferencesPage()
        if game.kind == "dosbox" and game.folder is not None:
            page.add(self._controls_group(game))
            page.add(self._dosbox_group(game))
        group = Adw.PreferencesGroup()
        uninstall = Gtk.Button(label=_("Uninstall Game"), halign=Gtk.Align.CENTER,
                               css_classes=["pill", "destructive-action"])
        uninstall.connect("clicked", lambda *_a: self.ask_remove(game))
        group.add(uninstall)
        page.add(group)
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(page)
        return Adw.NavigationPage(title=game.title, child=view)

    def _controls_group(self, game):
        group = Adw.PreferencesGroup(title=_("Controls"))
        controls = Adw.SwitchRow(title=_("Touch Joystick"),
                                 subtitle=_("Stick and buttons beside the picture"),
                                 active=game.controls)
        controls.connect("notify::active", lambda r, _p: self.set_controls(game, r.get_active()))
        group.add(controls)
        # The keys the stick sends, as plinius-touch-controls names them (presets.TOUCH_STICKS)
        sticks = [_("Arrow Keys"), "WASD", _("Number Pad")]
        current = presets.touch_settings(game.touch).get("stick", presets.TOUCH_STICKS[0])
        stick = Adw.ComboRow(title=_("Stick"), subtitle=_("The keys it presses"),
                             model=Gtk.StringList.new(sticks),
                             selected=presets.TOUCH_STICKS.index(current))
        stick.connect("notify::selected", lambda r, _p: self.set_stick(
            game, presets.TOUCH_STICKS[r.get_selected()]))
        group.add(stick)
        # How far a swipe moves the mouse: DOSBox's sensitivity, both ways
        low, high = presets.SENSITIVITY
        speed = Adw.SpinRow(title=_("Mouse Speed"),
                            subtitle=_("How far a swipe moves the mouse, in percent"),
                            adjustment=Gtk.Adjustment.new(presets.sensitivity(game.dosbox),
                                                          low, high, 10, 100, 0),
                            digits=0)
        speed.connect("notify::value", lambda r, _p: self.set_mouse_speed(game, r.get_value()))
        group.add(speed)
        return group

    def _dosbox_group(self, game):
        group = Adw.PreferencesGroup(title=_("Game"))
        # The CPU speeds in the order of presets.CPU_SPEEDS; a speed of the game's preset that
        # is none of them stays selectable as it is
        speeds = list(presets.CPU_SPEEDS)
        names = [_("Automatic"), _("Slow (286)"), _("Medium (386)"), _("Fast (486)"),
                 _("Maximum")]
        current = presets.cpu_speed(game.dosbox)
        if current not in speeds:
            speeds.append(current)
            names.append(current)
        cpu = Adw.ComboRow(title=_("CPU Speed"),
                           subtitle=_("Slower if the game is too fast"),
                           model=Gtk.StringList.new(names), selected=speeds.index(current))
        cpu.connect("notify::selected",
                    lambda r, _p: self.set_cpu_speed(game, speeds[r.get_selected()]))
        group.add(cpu)
        sharp = Adw.SwitchRow(title=_("Sharp Pixels"),
                              subtitle=_("The picture without smoothing"),
                              active=presets.sharp_pixels(game.dosbox))
        sharp.connect("notify::active",
                      lambda r, _p: self.set_sharp_pixels(game, r.get_active()))
        group.add(sharp)
        found = dosgames.setup_program(game)
        if found:
            setup = Adw.ActionRow(title=_("Game Setup"),
                                  subtitle=_("Starts {program}, for sound and controls").format(
                                      program=found[1]),
                                  activatable=True)
            setup.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            setup.connect("activated", lambda *_a: self.play(game, setup=True))
            group.add(setup)
        return group

    def set_stick(self, game, stick):
        game.touch = presets.with_stick(game.touch, stick)
        self._save(game)

    def set_cpu_speed(self, game, cycles):
        game.dosbox = presets.with_cpu_speed(game.dosbox, cycles)
        self._save(game)

    def set_sharp_pixels(self, game, on):
        game.dosbox = presets.with_sharp_pixels(game.dosbox, on)
        self._save(game)

    def set_controls(self, game, on):
        game.controls = on
        self._save(game)

    def set_mouse_speed(self, game, percent):
        game.dosbox = presets.with_sensitivity(game.dosbox, percent)
        self._save(game)

    def _save(self, game):
        try:
            library.save(game)
        except OSError as e:
            self.toast(_("Could not save: {error}").format(error=e.strerror or e))

    def play(self, game, setup=False):
        """Starts the game; with setup a DOSBox game's own setup (dosgames.setup_program)."""
        if game.kind == "scummvm":
            cmd = ["scummvm", game.targets[-1] if len(game.targets) == 1 else
                   self._main_target(game)]
        else:
            cmd = ["plinius-dos-installer", "--setup" if setup else "--play",
                   dosgames._app_id(game)[len(dosgames.PREFIX):]]
        try:
            subprocess.Popen(cmd, start_new_session=True)
        except OSError as e:
            self.toast(_("Could not start: {error}").format(error=e.strerror))

    @staticmethod
    def _main_target(game):
        # The game the folder is named after; the others came along with it
        paths = {}
        ini = library._ini()
        for t in game.targets:
            paths[t] = ini.get(t, "path", fallback="").count("/") if ini.has_section(t) else 99
        return min(game.targets, key=lambda t: paths[t])

    def ask_remove(self, game):
        own = game.folder is not None
        if own and game.kind == "dosbox":
            body = _("Its folder is deleted, saved games included.")
        elif own:
            body = _("Its files are deleted. Saved games stay in ScummVM.")
        else:
            body = _("It is taken off the list; its files stay where they are.")
        dialog = Adw.AlertDialog(heading=_("Remove “{title}”?").format(title=game.title),
                                 body=body)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("remove", _("Remove"))
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def done(_d, response):
            if response != "remove":
                return
            try:
                library.remove(game)
                self._update_launchers()
            except OSError as e:
                self.toast(_("Could not remove: {error}").format(error=e.strerror or e))
            # Back from the game's settings page
            self.nav.pop_to_tag("library")
            self.refresh()

        dialog.connect("response", done)
        dialog.present(self)

    @staticmethod
    def _update_launchers():
        launchers = os.environ.get("PLINIUS_SCUMMVM_LAUNCHERS", "plinius-scummvm-launchers")
        try:
            subprocess.run([launchers], capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=5))

    # Choosing what to install

    def _source_page(self):
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(
            description=_("The DOS Installer finds the game by itself: it runs the installer "
                          "if there is one and adds the game to the app grid with its icon."))
        for kind, title, subtitle, icon, patterns, several, folder in SOURCES:
            row = Adw.ActionRow(title=title, subtitle=subtitle, activatable=True)
            row.add_prefix(Gtk.Image(icon_name=icon))
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.connect("activated", self._pick, title, patterns, several, folder)
            group.add(row)
        page.add(group)
        view.set_content(page)
        return Adw.NavigationPage(title=_("Install a Game"), child=view)

    def _pick(self, _row, title, patterns, several, folder):
        dialog = Gtk.FileDialog(title=title, modal=True)
        dialog.set_initial_folder(Gio.File.new_for_path(str(start_folder())))
        if patterns:
            flt = Gtk.FileFilter(name=title)
            for p in patterns:
                flt.add_suffix(p[2:])
            filters = Gio.ListStore.new(Gtk.FileFilter)
            filters.append(flt)
            dialog.set_filters(filters)
            dialog.set_default_filter(flt)

        def chosen(d, result):
            try:
                if folder:
                    files = [d.select_folder_finish(result)]
                elif several:
                    model = d.open_multiple_finish(result)
                    files = [model.get_item(i) for i in range(model.get_n_items())]
                else:
                    files = [d.open_finish(result)]
            except GLib.Error:
                return  # cancelled
            paths = [f.get_path() for f in files if f and f.get_path()]
            if not paths:
                return
            remember_folder(paths[0], folder)
            try:
                source = sources.classify(paths)
            except sources.SourceError:
                self.toast(_("These files do not go together."))
                return
            self.start(source)

        if folder:
            dialog.select_folder(self, None, chosen)
        elif several:
            dialog.open_multiple(self, None, chosen)
        else:
            dialog.open(self, None, chosen)

    # Installing

    def start(self, source):
        self.busy = True
        self.status_page = Adw.StatusPage(title=_("Getting Ready"),
                                          description=GLib.markup_escape_text(source.name))
        self.status_page.set_child(Adw.Spinner(width_request=48, height_request=48))
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar(show_back_button=False))
        view.set_content(self.status_page)
        page = Adw.NavigationPage(title=_("Installing"), child=view, can_pop=False)
        self.nav.push(page)
        ui = GuiUi(self)

        def work():
            try:
                game = installer.install(source, ui)
            except installer.Cancelled:
                GLib.idle_add(self.finished, None, None)
            except Exception as e:  # shown to the user, details on stderr
                traceback.print_exc()
                GLib.idle_add(self.finished, None, str(e) or type(e).__name__)
            else:
                GLib.idle_add(self.finished, game, None)

        threading.Thread(target=work, name="install", daemon=True).start()

    def set_status(self, text):
        self.status_page.set_title(text)

    def show_disks(self, disks):
        """While the installer runs from floppies: which disk is in, and a button for the next
        one in case the installer asks for a disk before it read the current one through."""
        spinner = Adw.Spinner(width_request=48, height_request=48)
        if disks is None:
            self.status_page.set_child(spinner)
            return
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, halign=Gtk.Align.CENTER)
        box.append(spinner)
        label = Gtk.Label(css_classes=["title-4"])
        box.append(label)
        button = Gtk.Button(label=_("Insert Next Disk"), css_classes=["pill"],
                            halign=Gtk.Align.CENTER)
        box.append(button)
        box.append(Gtk.Label(label=_("If the installer keeps asking for the same disk, insert "
                                     "the next one here."),
                             wrap=True, justify=Gtk.Justification.CENTER, max_width_chars=34,
                             css_classes=["dim-label"]))
        self.status_page.set_child(box)
        total = len(disks.disks)

        def update():
            if self.status_page.get_child() is not box:
                return False
            label.set_label(_("Disk {number} of {total}").format(number=disks.current + 1,
                                                                 total=total))
            button.set_sensitive(disks.current + 1 < total)
            return True

        def insert(*_a):
            try:
                disks.insert(disks.current + 1)
            except OSError as e:
                self.toast(_("Could not insert the disk: {error}").format(error=e.strerror or e))
            update()

        button.connect("clicked", insert)
        update()
        GLib.timeout_add(500, update)

    def _push_question(self, title, content):
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar(show_back_button=False))
        view.set_content(content)
        page = Adw.NavigationPage(title=title, child=view, can_pop=False)
        self.nav.push(page)
        return page

    def show_keyboard_hint(self, program, floppies, answer):
        parts = [_("DOSBox opens in fullscreen and starts {program}.").format(
                     program=GLib.markup_escape_text(program)),
                 _("The keyboard opens by itself, in its Terminal layout: only that one "
                   "reaches DOSBox.")]
        if floppies:
            parts.append(_("The disks change by themselves: when the installer asks for the "
                           "next disk, press Enter. Should it ask again, switch to the DOS "
                           "Installer and insert the next disk there."))
        parts.append(_("DOSBox closes when the installer is done."))
        status = Adw.StatusPage(icon_name="input-keyboard-symbolic",
                                title=_("The Installer Needs a Keyboard"),
                                description="\n\n".join(parts))
        buttons = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                          halign=Gtk.Align.CENTER)
        go = Gtk.Button(label=_("Start the Installer"), css_classes=["pill", "suggested-action"])
        cancel = Gtk.Button(label=_("Cancel"), css_classes=["pill"])
        buttons.append(go)
        buttons.append(cancel)
        status.set_child(buttons)
        self._push_question(_("Installer"), status)

        def reply(value):
            self.nav.pop()
            answer(value)

        go.connect("clicked", lambda *_a: reply(True))
        cancel.connect("clicked", lambda *_a: reply(False))

    def show_program_choice(self, candidates, title, answer):
        page_widget = Adw.PreferencesPage()
        name_group = Adw.PreferencesGroup(
            description=_("ScummVM does not know this game, so it runs in DOSBox."))
        entry = Adw.EntryRow(title=_("Name"), text=title)
        name_group.add(entry)
        page_widget.add(name_group)
        group = Adw.PreferencesGroup(title=_("Start File"))
        first = None
        buttons = []
        for dos_dir, name in candidates[:12]:
            check = Gtk.CheckButton(group=first)
            first = first or check
            row = Adw.ActionRow(title=name, subtitle=("C:\\" + dos_dir) if dos_dir else "C:\\",
                                activatable_widget=check)
            row.add_prefix(check)
            group.add(row)
            buttons.append((check, (dos_dir, name)))
        if first:
            first.set_active(True)
        else:
            group.set_description(_("No start file was found."))
        page_widget.add(group)
        done_group = Adw.PreferencesGroup()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER)
        add = Gtk.Button(label=_("Add the Game"), css_classes=["pill", "suggested-action"],
                         sensitive=bool(first))
        cancel = Gtk.Button(label=_("Cancel"), css_classes=["pill"])
        box.append(add)
        box.append(cancel)
        done_group.add(box)
        page_widget.add(done_group)
        self._push_question(_("Start File"), page_widget)

        def reply(ok):
            self.nav.pop()
            if not ok:
                answer(None)
                return
            choice = next(c for b, c in buttons if b.get_active())
            answer((choice, entry.get_text().strip() or title))

        add.connect("clicked", lambda *_a: reply(True))
        cancel.connect("clicked", lambda *_a: reply(False))

    def finished(self, game, error):
        self.busy = False
        self.refresh()
        if game is None:
            self.nav.pop_to_tag("library")
            if error:
                dialog = Adw.AlertDialog(heading=_("Installation Failed"), body=error)
                dialog.add_response("ok", _("OK"))
                dialog.present(self)
            return
        status = Adw.StatusPage(
            title=_("“{title}” Is Installed").format(title=game.title),
            description=_("You find it in the app grid."))
        icon = game_icon(game)
        if icon.startswith("/"):
            try:
                status.set_paintable(Gdk.Texture.new_from_filename(icon))
            except GLib.Error:
                status.set_icon_name(APP_ID)
        else:
            status.set_icon_name(icon)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER)
        play = Gtk.Button(label=_("Play"), css_classes=["pill", "suggested-action"])
        close = Gtk.Button(label=_("Done"), css_classes=["pill"])
        box.append(play)
        box.append(close)
        status.set_child(box)
        play.connect("clicked", lambda *_a: (self.play(game), self.nav.pop_to_tag("library")))
        close.connect("clicked", lambda *_a: self.nav.pop_to_tag("library"))
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar(show_back_button=False))
        view.set_content(status)
        self.nav.replace([self.nav.find_page("library"),
                          Adw.NavigationPage(title=_("Installed"), child=view)])


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        win = self.props.active_window or Window(self)
        win.present()


def main():
    return App().run(None)
