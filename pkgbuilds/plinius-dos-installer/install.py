"""From what the user picked to a game in the app grid.

1. Get at the files: unpack a ZIP, GOG installer or ISO; unpack floppies into one folder.
2. Does ScummVM know the game as it is? Then copy it to ~/Games/dos/<Title>/game.
3. Otherwise run its installer in DOSBox: the disks as A: (swapped by themselves) or the CD
   as D:, and ~/Games/dos/<Title>/c as C:. Afterwards ask ScummVM again.
4. ScummVM game: add it, fetch its icon, write its launcher. Else a DOS game: pick its start
   file (the likely one first) and write a DOSBox launcher.

The steps that need the user (the keyboard hint before the installer, the start file) go
through ui; the app answers them in its window, the command line on its own.
"""

import errno
import gettext
import os
from collections.abc import Mapping
import shutil
import time

from . import dosbox, dosgames, library, presets, scummvm, sources
from .finish import finish
from .floppies import FloppySet

_ = gettext.translation("plinius-dos-installer", fallback=True).gettext


class Cancelled(Exception):
    pass


class Ui:
    """What the installer asks; this one answers without asking, for the command line."""

    def status(self, text):
        print(time.strftime("%H:%M:%S"), text, flush=True)

    def log(self, text):
        self.status(text)

    def before_installer(self, program, floppies=False):
        """Return False to cancel. floppies: the installer runs from floppy disks, which
        DOSBox swaps by itself."""
        return True

    def floppies(self, disks):
        """The installer runs from these disks (a floppies.FloppySet); None once it is done."""

    def choose_program(self, candidates, title):
        """((DOS folder, file), title) for a DOS game, or None to cancel."""
        return (candidates[0], title) if candidates else None


def _copy_tree(src, dest):
    shutil.copytree(src, dest, symlinks=False, dirs_exist_ok=True)


def _short(description):
    return scummvm.VARIANT_RE.sub("", description) or description


def install(source, ui=None, title=None):
    """Installs source; returns the library.Game."""
    ui = ui or Ui()
    library.GAMES.mkdir(parents=True, exist_ok=True)
    staging = library.GAMES / f"{library.STAGING}{int(time.time())}-{os.getpid()}"
    tmp = staging / "tmp"
    tmp.mkdir(parents=True)
    # The game's folder once the staging folder became it: a later failure (ScummVM, the CD
    # copy) removes it again, it holds nothing but what this installation put there
    moved = []
    try:
        game = _install(source, ui, title or source.name, staging, tmp, moved)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        for folder in moved:
            shutil.rmtree(folder, ignore_errors=True)
        raise
    shutil.rmtree(tmp, ignore_errors=True)
    return game


class _FolderFiles(Mapping):
    """The files of a folder as DOS paths (upper case, "/"-separated, two levels deep), read
    only when asked for: the rules look at a few of them."""

    def __init__(self, folder):
        self.paths = {}
        for dirpath, dirs, names in os.walk(folder):
            rel = os.path.relpath(dirpath, folder)
            if rel != "." and rel.count(os.sep) >= 1:
                dirs[:] = []
            prefix = "" if rel == "." else rel.replace(os.sep, "/").upper() + "/"
            for n in names:
                self.paths[prefix + n.upper()] = os.path.join(dirpath, n)

    def __getitem__(self, key):
        with open(self.paths[key], "rb") as f:
            return f.read()

    def __iter__(self):
        return iter(self.paths)

    def __len__(self):
        return len(self.paths)


def _disk_files(images):
    """The files of all disks; a file on several disks with its version from the first."""
    files = {}
    for disk in images.disks:
        for path, data in disk.files.items():
            files.setdefault(path, data)
    return files


def _install(source, ui, title, staging, tmp, moved):
    images, cd, files = None, None, None
    ui.status(_("Looking up the game"))
    known = presets.load(log=ui.log)
    if source.kind == "floppies":
        ui.status(_("Reading the disks"))
        images = FloppySet(source.paths, str(tmp / "a"), log=ui.log)
        images.build()
        files = tmp / "a"
    elif source.kind == "cd":
        cd = source.paths[0]
        if cd.lower().endswith(".iso"):
            ui.status(_("Reading the CD"))
            files = sources.unpack_cd(cd, str(tmp / "cd"), log=ui.log)
    else:
        ui.status(_("Unpacking"))
        files = _unwrap(sources.unpack(source, str(tmp / "files"), log=ui.log))

    # A rule or preset that knows the disks or folder decides; ScummVM is not asked about
    # them as they are (it took the product catalog on Duke Nukem's disks for the game)
    plan = None
    if images:
        plan = presets.install_plan(known, _disk_files(images), log=ui.log)
    elif files:
        plan = presets.install_plan(known, _FolderFiles(files), log=ui.log)

    # 2. Runs as it is?
    if files and not (plan and plan.automatic):
        ui.status(_("Looking for the game"))
        found = scummvm.choose(scummvm.detect(str(files)))
        if found:
            title = _short(_main(found).description)
            folder = library.new_folder(title)
            ui.status(_("Copying the game"))
            _copy_tree(files, staging / "game")
            return _finish_scummvm(staging, folder, title, ui, moved)

    # 3. Installer, or what a rule or preset does instead
    c = staging / "c"
    c.mkdir()
    if plan and plan.title:
        title = plan.title
    if images:
        program = (plan and plan.program) or dosbox.find_installer(images.disks[0].files)
    else:
        # Without the files (a .cue CD) the installer can only be guessed
        program = (plan and plan.program) or \
            (dosbox.find_installer(_listing(files)) if files else "INSTALL")
    if not program and not (plan and plan.automatic):
        return _as_dos_game(staging, files, title, ui, moved, known=known)
    if plan and plan.automatic:
        ui.status(_("Installing {title}").format(title=title))
    else:
        if not ui.before_installer(program, floppies=bool(images)):
            raise Cancelled()
        ui.status(_("The installer is running"))
    if images:
        try:
            dosbox.install_from_floppies(source.paths, str(c), program, log=ui.log,
                                         work=str(tmp / "dosbox"), started=ui.floppies,
                                         plan=plan)
        finally:
            ui.floppies(None)
    else:
        dosbox.install_from_drive(str(c), program, cd=cd, folder=None if cd else str(files),
                                  log=ui.log, plan=plan)
    if not any(c.iterdir()):
        raise RuntimeError(_("The installer did not install anything."))

    ui.status(_("Looking for the game"))
    found = scummvm.choose(scummvm.detect(str(c)))
    if found:
        title = _short(_main(found).description)
        return _finish_scummvm(staging, library.new_folder(title), title, ui, moved)
    return _as_dos_game(staging, None, title, ui, moved, cd=cd, known=known, plan=plan)


def _main(found):
    """The game a source is about: the one in the topmost folder. Games that come along with
    it sit deeper (Maniac Mansion in Day of the Tentacle's MANIAC folder)."""
    return min(found, key=lambda d: d.path.count("/"))


def _unwrap(folder):
    """An archive often holds one folder with the game in it: that folder is the game, so its
    installer is found and it becomes C: or D: itself. Not for CDs, whose installers expect
    their paths on D: as they are."""
    while True:
        entries = os.listdir(folder)
        if len(entries) != 1 or not os.path.isdir(os.path.join(folder, entries[0])):
            return folder
        folder = os.path.join(folder, entries[0])


def _listing(folder):
    return {f.upper(): 1 for f in os.listdir(folder) if os.path.isfile(os.path.join(folder, f))}


def _move(staging, folder, moved):
    """The staging folder becomes the game's folder. Never onto one that is there (rename
    would replace an empty folder), so the cleanup after a failure only takes what this
    installation made."""
    if os.path.lexists(folder):
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(folder))
    os.rename(staging, folder)
    moved.append(folder)
    return folder


def _finish_scummvm(staging, folder, title, ui, moved):
    shutil.rmtree(staging / "tmp", ignore_errors=True)
    if (staging / "c").is_dir() and not any((staging / "c").iterdir()):
        (staging / "c").rmdir()
    folder = _move(staging, folder, moved)
    ui.status(_("Adding the game"))
    root = dosgames.drive(folder)
    targets = finish(str(root), log=ui.log)
    if not targets:
        raise RuntimeError(_("ScummVM did not take the game."))
    game = library.Game(title, "scummvm", folder, targets)
    library.save(game)
    return game


def _as_dos_game(staging, files, title, ui, moved, cd=None, known=None, plan=None):
    if files:
        ui.status(_("Copying the game"))
        _copy_tree(files, staging / "game")
    root = staging / "c" if (staging / "c").is_dir() and any((staging / "c").iterdir()) \
        else staging / "game"
    # Configured for DOSBox's hardware and the touch screen, as far as the rules know the game
    result = presets.after_install(known or {"games": []}, str(root), plan, log=ui.log)
    if result.program:
        choice, title = result.program, result.title or title
    else:
        answer = ui.choose_program(dosgames.candidates(root), result.title or title)
        if not answer:
            raise Cancelled()
        choice, title = answer
    folder = _move(staging, library.new_folder(title), moved)
    shutil.rmtree(folder / "tmp", ignore_errors=True)
    if (folder / "c").is_dir() and not any((folder / "c").iterdir()):
        (folder / "c").rmdir()
    game = library.Game(title, "dosbox", folder, [], choice[0], choice[1],
                        dosbox=result.dosbox, touch=result.touch,
                        mouse=result.mouse)
    if cd:
        # The game may want its CD while it runs: a copy goes along, a .cue with its tracks
        dest = folder / "cd"
        dest.mkdir()
        for f in sources.cd_files(cd):
            shutil.copy2(f, dest)
        game.cd = os.path.join("cd", os.path.basename(cd))
    library.save(game)
    dosgames.write_launcher(game)
    return game
