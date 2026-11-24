"""After the installer: make the game show up in the app grid with its own icon."""

import os
import shutil
import subprocess

from . import scummvm

# The launcher writer that comes with the DOS Installer; another one can be given for tests
LAUNCHERS = os.environ.get("PLINIUS_SCUMMVM_LAUNCHERS", "plinius-scummvm-launchers")


def finish(target, log=print, fetch_icons=True):
    """Looks for ScummVM games in target, adds them and writes their launchers. Returns the
    added targets; empty when ScummVM knows none of it (a DOS game for DOSBox, then)."""
    if not shutil.which("scummvm"):
        log("ScummVM is not installed")
        return []
    games = scummvm.choose(scummvm.detect(target))
    added = []
    for d in games:
        name, new = scummvm.add(d)
        if not name:
            log(f"ScummVM did not add {d.description}")
            continue
        log(f"{d.description}: ScummVM target {name}" + ("" if new else " (was there already)"))
        added.append(name)
        if fetch_icons:
            scummvm.fetch_icon(d, log=log)
    if added:
        r = subprocess.run([LAUNCHERS], capture_output=True, text=True)
        log((r.stdout + r.stderr).strip() or f"{LAUNCHERS} ended with {r.returncode}")
    return added
