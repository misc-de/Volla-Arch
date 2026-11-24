"""DOS games that ScummVM does not know: a launcher that starts them in DOSBox."""

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from . import keyboard, presets
from .dosbox import ENV, PHONE_CONFIG, _quote
from .scummvm import DATA

APPS = DATA / "applications"
ICONS = DATA / "plinius-dos-installer/icons"
PREFIX = "org.plinius.DOSGame."
MARKER = "X-Plinius-DOS-Game"
SCUMMVM_PACK = Path("/usr/share/scummvm/gui-icons.dat")

PROGRAM_EXTS = (".EXE", ".COM", ".BAT")
# Never the game itself: installers, sound setup, uninstallers, DOS extenders
NOT_THE_GAME = re.compile(r"^(INSTALL|SETUP|UNINST|UNINSTAL|SETSOUND|SOUND|SNDSETUP|CONFIG|"
                          r"DOS4GW|DOS32A|PMODEW|CWSDPMI|README|VESA|UNIVBE|IMS|MIDPAK|"
                          r"DIGPAK|LOADPATS|CATALOG)\b", re.I)


def drive(folder):
    """The folder that is C: for the game: c/ after an installer, game/ otherwise."""
    return folder / "c" if (folder / "c").is_dir() else folder / "game"


def candidates(root):
    """Possible start files below root as (DOS folder, file), the likely ones first: a file
    named like its folder, then .BAT before .EXE before .COM, then by size."""
    found = []
    for dirpath, _dirs, files in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if depth > 2:
            continue
        dos_dir = "" if rel == "." else rel.replace(os.sep, "\\").upper()
        for f in files:
            stem, ext = os.path.splitext(f.upper())
            if ext not in PROGRAM_EXTS or NOT_THE_GAME.match(stem):
                continue
            size = os.path.getsize(os.path.join(dirpath, f))
            folder_name = os.path.basename(dos_dir)
            score = (stem != folder_name[:8], depth, {".BAT": 0, ".EXE": 1, ".COM": 2}[ext], -size)
            found.append((score, dos_dir, f.upper()))
    return [(d, f) for _s, d, f in sorted(found)]


def _app_id(game):
    base = re.sub(r"[^A-Za-z0-9]+", "_", game.folder.name).strip("_")[:40] or "game"
    digest = hashlib.sha256(game.folder.name.encode()).hexdigest()[:8]
    return f"{PREFIX}{base}_{digest}"


def _icon(app_id):
    """ScummVM's PC icon for DOS games, as a file of their own; the DOSBox icon without it."""
    try:
        with zipfile.ZipFile(SCUMMVM_PACK) as z:
            data = z.read("icons/platforms/pc.png")
    except (OSError, KeyError, zipfile.BadZipFile):
        return "dosbox"
    ICONS.mkdir(parents=True, exist_ok=True)
    path = ICONS / f"{app_id}.png"
    path.write_bytes(data)
    return str(path)


def _escape(text):
    return text.replace("\\", "\\\\").replace("\n", " ").replace("\r", "")


def write_launcher(game):
    app_id = _app_id(game)
    icon = _icon(app_id)
    APPS.mkdir(parents=True, exist_ok=True)
    # Exec goes through the DOS Installer: the folder name may hold spaces and quotes
    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={_escape(game.title)}\n"
        "Comment=MS-DOS game\n"
        "Comment[de]=MS-DOS-Spiel\n"
        f"Exec=plinius-dos-installer --play {app_id[len(PREFIX):]}\n"
        f"Icon={_escape(icon)}\n"
        "Terminal=false\n"
        "StartupNotify=false\n"
        "Categories=Game;\n"
        "X-Purism-FormFactor=Workstation;Mobile;\n"
        f"{MARKER}={game.folder.name}\n"
    )
    path = APPS / f"{app_id}.desktop"
    tmp = path.with_name("." + path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
    return path


def remove_launcher(game):
    if not game.folder:
        return
    app_id = _app_id(game)
    for p in (APPS / f"{app_id}.desktop", ICONS / f"{app_id}.png"):
        try:
            p.unlink()
        except FileNotFoundError:
            pass


def find_by_launcher(key, games):
    for g in games:
        if g.folder and _app_id(g)[len(PREFIX):] == key:
            return g
    return None


# The game's own setup for sound and controls, the likeliest name first; INSTALL last, as some
# games install again with it
SETUP_NAMES = ("SETUP", "SETSOUND", "SNDSETUP", "SOUND", "CONFIG", "INSTALL")


def setup_program(game):
    """The game's setup as (DOS folder, file): beside the game, else in the
    top folder of C:; None without one."""
    if not game.folder:
        return None
    c = drive(game.folder)
    for dos_dir in dict.fromkeys((game.dir, "")):
        host = presets._host_file(c, dos_dir) if dos_dir else c
        try:
            files = {f.upper() for f in os.listdir(host) if (host / f).is_file()}
        except OSError:
            continue
        for name in SETUP_NAMES:
            for ext in PROGRAM_EXTS:
                if name + ext in files:
                    return dos_dir, name + ext
    return None


def play(game, setup=False):
    """Starts the game in DOSBox and waits until it is closed; with setup its setup instead
    (setup_program), when it has one."""
    c = drive(game.folder)
    folder, program = (setup and setup_program(game)) or (game.dir, game.program)
    # The mouse driver's sensitivity: a small program, on a drive of its own only until it ran
    mouse = presets.mouse_speed(game.mouse)
    tools = tempfile.mkdtemp(prefix="plinius-dos-mouse-") if mouse else None
    if tools:
        Path(tools, "MOUSE.COM").write_bytes(presets.mouse_program(mouse))
    with tempfile.NamedTemporaryFile("w", prefix="plinius-dos-game-", suffix=".conf",
                                     delete=False) as f:
        f.write(PHONE_CONFIG)
        if game.dosbox:
            f.write("\n" + presets.dosbox_conf(game.dosbox))
        f.write("\n[autoexec]\n")
        f.write(f"mount c {_quote(str(c))}\n")
        if tools:
            f.write(f"mount y {_quote(tools)}\ny:\\mouse.com\nmount -u y\n")
        if game.cd:
            f.write(f"imgmount d {_quote(str(game.folder / game.cd))} -t iso\n")
        f.write("c:\n")
        if folder:
            f.write(f"cd \\{folder}\n")
        f.write(f"{program}\nexit\n")
        conf = f.name
    controls = touch_controls(game)
    try:
        return subprocess.run(["dosbox", "-userconf", "-conf", conf], env=ENV).returncode
    finally:
        os.unlink(conf)
        stop_touch_controls(controls)
        if tools:
            shutil.rmtree(tools, ignore_errors=True)


# Stick and buttons on the screen beside the picture, shown while DOSBox's window is active
TOUCH_CONTROLS = "plinius-touch-controls"


def touch_controls(game):
    """Starts the touch controls for the game; None when they are off for it or not
    installed."""
    exe = shutil.which(TOUCH_CONTROLS)
    if not game.controls or not exe:
        return None
    try:
        return subprocess.Popen([exe, "--app-id", keyboard.APP_ID,
                                 *presets.touch_args(game.touch, game.program)])
    except OSError:
        return None


def stop_touch_controls(proc):
    if proc is None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
