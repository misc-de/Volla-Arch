"""The installed games: what the DOS Installer put into ~/Games/dos, and ScummVM's list.

Every game the DOS Installer installs gets a folder ~/Games/dos/<Title>/ with its files (c/ is
drive C: for an installer, game/ the files of a game that needed none) and plinius-game.json:

    {"title": ..., "kind": "scummvm" | "dosbox", "targets": [ScummVM targets],
     "dir": DOS folder of the game, "program": its start file, "cd": its CD image,
     "dosbox": DOSBox settings of its own, {section: {key: value}},
     "touch": its touch controls, "mouse": the mouse driver's sensitivity, [x, y] or [],
     "controls": whether the touch controls show while it runs (true when missing)}

ScummVM games that were added some other way show up as well, without a folder of their own;
removing one of them only takes it out of ScummVM.
"""

import configparser
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import presets, scummvm

GAMES = Path(os.environ.get("PLINIUS_DOS_GAMES") or Path.home() / "Games/dos")
RECORD = "plinius-game.json"


@dataclass
class Game:
    title: str
    kind: str
    folder: Path = None          # None: not installed by the DOS Installer
    targets: list = field(default_factory=list)
    dir: str = ""
    program: str = ""
    # CD image the game mounts as D: while it runs, relative to folder
    cd: str = ""
    # DOSBox settings for this game (presets.dosbox_settings)
    dosbox: dict = field(default_factory=dict)
    # The touch controls for this game (presets.touch_settings)
    touch: dict = field(default_factory=dict)
    # The mouse driver's sensitivity, set before the game starts (presets.mouse_speed)
    mouse: list = field(default_factory=list)
    # The touch controls show while the game runs; a switch per game in the library
    controls: bool = True

    @property
    def id(self):
        return self.folder.name if self.folder else self.targets[0]


def slug(title):
    """A folder name for the title: letters, digits, spaces and a few signs."""
    s = re.sub(r"[^\w .,'()&+-]", "", title, flags=re.UNICODE).strip(" .")
    return s[:80] or "DOS Game"


def new_folder(title):
    GAMES.mkdir(parents=True, exist_ok=True)
    base = slug(title)
    folder, n = GAMES / base, 2
    while folder.exists():
        folder, n = GAMES / f"{base} ({n})", n + 1
    return folder


STAGING = ".installing-"


def remove_leftovers(log=print):
    """Folders of installations that ended without cleaning up (the app was closed or killed
    while an installer ran): .installing-<time>-<pid>, whose process is gone."""
    try:
        entries = os.listdir(GAMES)
    except OSError:
        return
    for name in entries:
        m = re.fullmatch(re.escape(STAGING) + r"\d+-(\d+)", name)
        if not m:
            continue
        try:
            with open(f"/proc/{m.group(1)}/cmdline", "rb") as f:
                alive = b"dos" in f.read().lower()
        except OSError:
            alive = False
        if not alive:
            shutil.rmtree(GAMES / name, ignore_errors=True)
            log(f"removed the leftover {name}")


def save(game):
    data = {"title": game.title, "kind": game.kind, "targets": game.targets,
            "dir": game.dir, "program": game.program, "cd": game.cd, "dosbox": game.dosbox,
            "touch": game.touch, "mouse": game.mouse, "controls": game.controls}
    tmp = game.folder / (RECORD + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    os.replace(tmp, game.folder / RECORD)


def load(folder):
    try:
        data = json.loads((folder / RECORD).read_text())
    except (OSError, ValueError):
        return None
    return Game(data.get("title") or folder.name, data.get("kind", "dosbox"), folder,
                list(data.get("targets", [])), data.get("dir", ""), data.get("program", ""),
                data.get("cd", ""), data.get("dosbox") or {}, data.get("touch") or {},
                presets.mouse_speed(data.get("mouse")), data.get("controls") is not False)


def _ini():
    parser = configparser.RawConfigParser(strict=False, interpolation=None)
    try:
        parser.read(scummvm.INI, encoding="utf-8")
    except configparser.Error:
        pass
    return parser


def games():
    """All games, the DOS Installer's first, then the rest of ScummVM's, sorted by title."""
    found, covered = [], set()
    try:
        folders = sorted(p for p in GAMES.iterdir() if p.is_dir())
    except FileNotFoundError:
        folders = []
    for folder in folders:
        g = load(folder)
        if g:
            found.append(g)
            covered.update(g.targets)
    ini = _ini()
    for target in ini.sections():
        if target == "scummvm" or target in covered or not ini.has_option(target, "gameid"):
            continue
        title = ini.get(target, "description", fallback=target)
        found.append(Game(re.sub(r"\s*\([^()]*\)\s*$", "", title) or title, "scummvm",
                          targets=[target]))
    return sorted(found, key=lambda g: g.title.casefold())


def remove_targets(targets):
    """Takes the targets out of scummvm.ini. ScummVM has no command for it; the file is
    rewritten line by line, so everything else in it stays as it was."""
    if not targets or not scummvm.INI.exists():
        return
    drop = {t.lower() for t in targets}
    out, skipping = [], False
    for line in scummvm.INI.read_text(encoding="utf-8", errors="replace").splitlines(True):
        m = re.match(r"^\s*\[(.+)\]\s*$", line)
        if m:
            skipping = m.group(1).lower() in drop
        if not skipping:
            out.append(line)
    tmp = scummvm.INI.with_name(scummvm.INI.name + ".tmp")
    tmp.write_text("".join(out), encoding="utf-8")
    os.replace(tmp, scummvm.INI)


def remove(game, delete_files=True):
    """Removes the game from the list; with delete_files also its folder, but only one the DOS
    Installer made (it has the record file) and only below ~/Games/dos."""
    remove_targets(game.targets)
    if game.kind == "dosbox":
        from .dosgames import remove_launcher
        remove_launcher(game)
    if delete_files and game.folder and (game.folder / RECORD).exists():
        folder = game.folder.resolve()
        if folder.parent == GAMES.resolve():
            shutil.rmtree(folder)
