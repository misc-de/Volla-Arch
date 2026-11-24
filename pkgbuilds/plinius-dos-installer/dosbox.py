"""Runs a DOS installer in DOSBox 0.74 with the phone's settings."""

import contextlib
import os
import shutil
import subprocess
import tempfile
import threading

from . import keyboard, presets
from .floppies import FloppySet

# DOSBox's window gets a fixed app id, under which Stevia keeps the Terminal layout for it. ALARM's
# SDL 1.2 is sdl12-compat on SDL 3 (Wayland), older stacks use SDL 2 or X11: one name for each.
ENV = dict(os.environ, SDL_APP_ID=keyboard.APP_ID, SDL_VIDEO_WAYLAND_WMCLASS=keyboard.APP_ID,
           SDL_VIDEO_X11_WMCLASS=keyboard.APP_ID)

# Fullscreen in a 4:3 mode, the shape of the picture: sdl12-compat keeps the mode a fixed
# logical size and scales it into the window, and does not tell DOSBox when the phone turns.
# With the desktop size the mode had the shape of the orientation DOSBox started in, and the
# other one squeezed it small into the middle; a 4:3 mode fills the screen's width upright and
# its height lying, also after a turn. 1440x1080 is 1:1 lying on the 1080 px panel.
# Keysyms instead of scancodes: Stevia sends made-up keycodes with its own keymap, which
# SDL 1.2 cannot turn into scancodes.
PHONE_CONFIG = """\
[sdl]
fullscreen=true
fullresolution=1440x1080
output=opengl
usescancodes=false

[render]
aspect=true
"""


def _quote(path):
    # DOSBox's MOUNT takes a path in double quotes; it has no escape for a quote inside
    if '"' in path:
        raise ValueError(f"path with a double quote: {path}")
    return f'"{path}"'


def write_config(path, autoexec, settings=None):
    """settings: further DOSBox settings ({section: {key: value}}, see presets)."""
    with open(path, "w") as f:
        f.write(PHONE_CONFIG)
        if settings:
            f.write("\n" + presets.dosbox_conf(settings))
        f.write("\n[autoexec]\n")
        f.write("\n".join(autoexec) + "\n")


INSTALLER_NAMES = ("INSTALL", "SETUP", "INSTALAR", "INSTALLA", "INSTALLE")
PROGRAM_EXTS = (".BAT", ".COM", ".EXE")


def find_installer(files):
    """The installer among the files of disk 1 (DOS paths, upper case), or None. A batch file
    goes first: it is how many disks start the real installer with the right options."""
    for name in INSTALLER_NAMES:
        for ext in PROGRAM_EXTS:
            if name + ext in files:
                return name + ext
    programs = [p for p in files if "/" not in p and p.endswith(PROGRAM_EXTS)]
    return programs[0] if len(programs) == 1 else None


# Installing on its own (a preset's commands): as fast as DOSBox goes, nobody types
AUTOMATIC = {"cpu": {"cycles": "max"}}


def install_from_floppies(images, target, program=None, log=print, work=None, started=None,
                          plan=None):
    """Mounts the floppy images as A: and target as C:, runs program on A: (found by itself
    when None) and waits until DOSBox is closed. Returns DOSBox's exit code and the disks.
    started(disks) is called once DOSBox runs, so a disk can also be inserted by hand.
    plan (a presets.InstallPlan) prepares the drives first; with commands of its own they run
    instead of the installer, without the keyboard."""
    os.makedirs(target, exist_ok=True)
    work = work or tempfile.mkdtemp(prefix="plinius-dos-installer-")
    drive = os.path.join(work, "a")
    disks = FloppySet(images, drive, log=log)
    automatic = bool(plan and plan.automatic)
    if plan and plan.program:
        program = plan.program
    if program is None and not automatic:
        program = find_installer(disks.disks[0].files)
        if program is None:
            shutil.rmtree(work, ignore_errors=True)
            raise FileNotFoundError("no installer on disk 1")
        log(f"installer: {program}")
    disks.build()
    disks.insert(0)
    if plan:
        presets.prepare_drive(plan, drive, target, log=log)
    config = os.path.join(work, "install.conf")
    mount_a = f"mount a {_quote(drive)} -t floppy"
    if disks.label:
        mount_a += f" -label {disks.label[:11].replace(' ', '_')}"
    steps = plan.commands if automatic else ["a:", program]
    write_config(config, [
        f"mount c {_quote(os.path.abspath(target))}",
        mount_a,
        *steps,
        # Back to the app once the installer is done
        "exit",
    ], AUTOMATIC if automatic else None)
    proc = subprocess.Popen(["dosbox", "-userconf", "-conf", config], env=ENV)
    stop = threading.Event()
    watcher = threading.Thread(target=disks.watch, args=(stop, lambda: [proc.pid]),
                               name="floppies", daemon=True)
    watcher.start()
    if started and not automatic:
        started(disks)
    try:
        with keyboard.ForDosbox(log) if not automatic else contextlib.nullcontext():
            code = proc.wait()
    finally:
        stop.set()
        watcher.join(timeout=2)
        shutil.rmtree(work, ignore_errors=True)
    log(f"DOSBox ended with {code}")
    return code, disks


def install_from_drive(target, program, cd=None, folder=None, log=print, plan=None):
    """Runs program from D:, which is the CD image cd or the folder, with target as C:.
    Returns DOSBox's exit code once it is closed. plan as for install_from_floppies."""
    os.makedirs(target, exist_ok=True)
    work = tempfile.mkdtemp(prefix="plinius-dos-installer-")
    if plan:
        presets.prepare_drive(plan, None, target, log=log)
    automatic = bool(plan and plan.automatic)
    config = os.path.join(work, "install.conf")
    mount_d = f"imgmount d {_quote(cd)} -t iso" if cd else f"mount d {_quote(folder)}"
    if plan and plan.program:
        program = plan.program
    steps = plan.commands if automatic else ["d:", program]
    write_config(config, [f"mount c {_quote(os.path.abspath(target))}", mount_d, *steps,
                          "exit"], AUTOMATIC if automatic else None)
    try:
        with keyboard.ForDosbox(log) if not automatic else contextlib.nullcontext():
            code = subprocess.run(["dosbox", "-userconf", "-conf", config], env=ENV).returncode
    finally:
        shutil.rmtree(work, ignore_errors=True)
    log(f"DOSBox ended with {code}")
    return code
