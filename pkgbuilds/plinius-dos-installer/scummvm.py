"""Finding an installed game with ScummVM, adding it, and fetching its icon."""

import configparser
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

DATA = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
INI = CONFIG / "scummvm/scummvm.ini"
# Single icons fetched for one game; plinius-scummvm-launchers takes them over the icon packs
ICONS = DATA / "plinius-dos-installer/scummvm-icons-fetched"
# ScummVM's icon collection (GPL-2.0), the source of its icon packs. One game's icon is a few
# hundred KB, the packs together several hundred MB.
ICON_URL = "https://raw.githubusercontent.com/scummvm/scummvm-icons/master/icons/{}.png"

# No window: detection and --add work without a display this way
ENV = dict(os.environ, SDL_VIDEODRIVER="dummy")
# ScummVM appends variant, platform and language in one pair of parentheses at the end
VARIANT_RE = re.compile(r"\s*\([^()]*\)\s*$")
ROW_RE = re.compile(r"^(\S+):(\S+)\s+(.+?)\s{2,}(/.*)$")


@dataclass
class Detection:
    engine: str
    gameid: str
    description: str
    path: str


def detect(path):
    if not shutil.which("scummvm"):
        return []
    out = subprocess.run(["scummvm", "--detect", "--recursive", f"--path={path}"],
                         env=ENV, capture_output=True, text=True, timeout=300).stdout
    found = []
    for line in out.splitlines():
        m = ROW_RE.match(line.rstrip())
        if m:
            found.append(Detection(*m.groups()))
    return found


def choose(found):
    """One entry per game: the one in the deepest directory. A game that comes with another
    (Maniac Mansion inside Day of the Tentacle) is detected in its own folder there, while
    the parent folder's match for it is a stray one."""
    best = {}
    for d in found:
        key = (d.engine, d.gameid)
        if key not in best or d.path.count("/") > best[key].path.count("/"):
            best[key] = d
    return list(best.values())


def _targets():
    parser = configparser.RawConfigParser(strict=False, interpolation=None)
    try:
        parser.read(INI, encoding="utf-8")
    except configparser.Error:
        return {}
    return {s: dict(parser[s]) for s in parser.sections() if s != "scummvm"}


def add(d):
    """Adds the game to ScummVM. Returns (target, new): new is False when the game was there
    already. ScummVM takes a game in one version only once, wherever its files are, and then
    skips it; target is the existing entry in that case, None if there is none."""
    before = _targets()
    subprocess.run(["scummvm", "--add", f"--game={d.engine}:{d.gameid}", f"--path={d.path}"],
                   env=ENV, capture_output=True, timeout=300)
    new = [t for t in _targets() if t not in before]
    if new:
        return new[0], True
    same = [t for t, v in before.items()
            if v.get("gameid") == d.gameid and v.get("engineid", d.engine) == d.engine]
    return (same[0] if same else None), False


def fetch_icon(d, log=print):
    """Fetches the game's icon once; True when there is one now."""
    name = f"{d.engine}-{d.gameid}".lower()
    dest = ICONS / f"{name}.png"
    if dest.exists():
        return True
    try:
        with urllib.request.urlopen(ICON_URL.format(name), timeout=20) as r:
            data = r.read(4 * 1024 * 1024)
    except (urllib.error.URLError, OSError) as e:
        log(f"no icon for {name}: {e}")
        return False
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        log(f"no icon for {name}: not a PNG")
        return False
    ICONS.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, dest)
    log(f"icon {name} fetched")
    return True
