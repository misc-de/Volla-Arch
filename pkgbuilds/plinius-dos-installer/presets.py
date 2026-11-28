"""What the DOS Installer knows about games, so that they get to their first start without the
questions of their installers and setup programs.

DOSBox always offers the same hardware (Sound Blaster 16 at port 220, IRQ 7, DMA 1, high DMA 5,
a mouse), so the right answers are known before the game asks. They come from two places:

- Rules for whole families of games, here in the code: the DEICE installer of id Software's
  floppies (Doom, Doom II, Wolfenstein 3D, Heretic ...) and the Doom engine's configuration.
- A list of single games (presets.json): the files they are known by, settings for their
  configuration files, DOSBox settings, the start file, or DOS commands that install them.
  The list is fetched from plinius.cais.de before an installation, so a new game needs no
  package update. It is signed with the key of the package repository; an unsigned or older
  list is not used. The package brings a copy for installing without a network.

The list only holds data: files are written below C: only, DOSBox settings come from a short
list of known ones, and DOS commands that reach beyond C: and A: (MOUNT, Z:) are refused.
"""

import base64
import io
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

URL = "https://plinius.cais.de/dos/presets.json"
BUNDLED = Path(__file__).with_name("presets.json")
CACHE = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "plinius-dos-installer"
USER_AGENT = "Plinius Arch Linux"  # every request says who asks
FETCH_TIMEOUT = 6  # seconds; without a network the bundled list is used
SCHEMA = 1
# The repository's signing key (ASCII armored) and the fingerprints pacman trusts or revoked
KEYRING = "/usr/share/pacman/keyrings/plinius.gpg"
TRUSTED = "/usr/share/pacman/keyrings/plinius-trusted"
REVOKED = "/usr/share/pacman/keyrings/plinius-revoked"

# DOSBox settings a preset may change: section -> keys
DOSBOX_KEYS = {
    "cpu": {"core", "cputype", "cycles", "cycleup", "cycledown"},
    "dosbox": {"machine", "memsize"},
    "dos": {"xms", "ems", "umb"},
    "render": {"frameskip", "aspect"},
    "sblaster": {"sbtype", "oplmode", "oplemu"},
    "speaker": {"pcspeaker", "tandy"},
    "gus": {"gus"},
    "joystick": {"joysticktype"},
    "sdl": {"sensitivity", "output"},
}
# Settings that take only some values: the picture always goes through OpenGL, smoothed or with
# sharp pixels
DOSBOX_VALUES = {("sdl", "output"): {"opengl", "openglnb"}}
# DOS commands a preset may not run: they reach beyond the drives the installer mounts
REFUSED_COMMANDS = {"MOUNT", "IMGMOUNT", "BOOT", "CONFIG", "RESCAN", "MIXER", "INTRO",
                    "LOADROM", "KEYB", "IPX", "IPXNET", "SERIAL"}


class PresetError(Exception):
    pass


# Signature check, as plinius-cve-update does it


def _fingerprints(path):
    try:
        with open(path, encoding="utf-8") as f:
            return {line.split(":")[0].strip().upper() for line in f
                    if line.strip() and not line.startswith("#")}
    except OSError:
        return set()


def _dearmor(text):
    """The binary key data of an ASCII armored key block (gpgv reads binary keyrings only)."""
    lines = [line.strip() for line in text.splitlines()]
    begin = next(i for i, line in enumerate(lines) if line.startswith("-----BEGIN PGP"))
    end = next(i for i, line in enumerate(lines) if i > begin and line.startswith("-----END PGP"))
    body = lines[begin + 1:end]
    if "" in body:
        body = body[body.index("") + 1:]
    body = [line for line in body if line and not (line.startswith("=") and len(line) == 5)]
    return base64.b64decode("".join(body), validate=True)


def verify(data, signature):
    """True if signature is a valid signature of data by a key pacman trusts for this repo."""
    trusted = _fingerprints(TRUSTED) - _fingerprints(REVOKED)
    if not trusted:
        return False
    try:
        with open(KEYRING, encoding="ascii") as f:
            keyring = _dearmor(f.read())
    except (OSError, ValueError, StopIteration):
        return False
    with tempfile.TemporaryDirectory(prefix="plinius-dos-presets-") as tmp:
        paths = {}
        for name, content in (("keyring.gpg", keyring), ("data", data), ("data.sig", signature)):
            paths[name] = os.path.join(tmp, name)
            with open(paths[name], "wb") as f:
                f.write(content)
        try:
            result = subprocess.run(
                ["gpgv", "--homedir", tmp, "--status-fd", "1", "--keyring", paths["keyring.gpg"],
                 paths["data.sig"], paths["data"]],
                capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return False
    if result.returncode != 0:
        return False
    for line in result.stdout.splitlines():
        fields = line.split()
        if fields[:2] == ["[GNUPG:]", "VALIDSIG"] and len(fields) > 2:
            if fields[2].upper() in trusted or fields[-1].upper() in trusted:
                return True
    return False


# The list


def _parse(data):
    try:
        presets = json.loads(data)
    except ValueError as e:
        raise PresetError(f"not JSON: {e}") from None
    if not isinstance(presets, dict) or presets.get("schema") != SCHEMA \
            or not isinstance(presets.get("games"), list):
        raise PresetError("unknown kind of list")
    return presets


def _read_cache():
    try:
        data = (CACHE / "presets.json").read_bytes()
        signature = (CACHE / "presets.json.sig").read_bytes()
    except OSError:
        return None
    if not verify(data, signature):
        return None
    try:
        return _parse(data)
    except PresetError:
        return None


def _request(url):
    return urllib.request.Request(url, headers={"User-Agent": USER_AGENT})


def _fetch(log):
    """The list from the server if it is signed and newer than the kept one; else None."""
    try:
        with urllib.request.urlopen(_request(URL), timeout=FETCH_TIMEOUT) as r:
            data = r.read(4 << 20)
        with urllib.request.urlopen(_request(URL + ".sig"), timeout=FETCH_TIMEOUT) as r:
            signature = r.read(64 << 10)
    except OSError as e:
        log(f"presets: not fetched ({e})")
        return None
    if not verify(data, signature):
        log("presets: the fetched list fails the signature check, not used")
        return None
    try:
        presets = _parse(data)
    except PresetError as e:
        log(f"presets: the fetched list is not usable ({e})")
        return None
    kept = _read_cache()
    if kept and kept.get("updated", 0) > presets.get("updated", 0):
        log("presets: the fetched list is older than the kept one, not used")
        return kept
    CACHE.mkdir(parents=True, exist_ok=True)
    for name, content in (("presets.json", data), ("presets.json.sig", signature)):
        tmp = CACHE / (name + ".tmp")
        tmp.write_bytes(content)
        os.replace(tmp, CACHE / name)
    return presets


def load(fetch=True, log=print):
    """The newest usable list: fetched, kept from an earlier fetch, or the bundled one."""
    candidates = []
    if fetch:
        fetched = _fetch(log)
        if fetched:
            candidates.append(fetched)
    if not candidates:
        kept = _read_cache()
        if kept:
            candidates.append(kept)
    try:
        candidates.append(_parse(BUNDLED.read_bytes()))
    except (OSError, PresetError) as e:
        log(f"presets: bundled list not readable ({e})")
    if not candidates:
        return {"schema": SCHEMA, "updated": 0, "games": []}
    best = max(candidates, key=lambda p: p.get("updated", 0))
    log(f"presets: {len(best['games'])} games, list of {best.get('updated', 0)}")
    return best


# What a preset asks for


def _dos_path(path):
    """A path below C: as a relative host path; refuses absolute paths and '..'."""
    parts = [p for p in re.split(r"[\\/]+", path.strip()) if p not in ("", ".")]
    if not parts or any(p == ".." or ":" in p for p in parts):
        raise PresetError(f"path outside C: {path!r}")
    return parts


def _host_file(root, path):
    """The existing file for a DOS path below root, whatever the case of its names; or the
    path it would get (names in upper case) when it does not exist yet."""
    current = Path(root)
    for part in _dos_path(path):
        match = None
        try:
            for entry in os.listdir(current):
                if entry.upper() == part.upper():
                    match = entry
                    break
        except OSError:
            pass
        current = current / (match or part.upper())
    return current


# Words in front of a command that run the command after them
COMMAND_PREFIXES = {"LH", "LOADHIGH", "CALL"}
# What ends a command's name in DOSBox's shell, besides spaces
COMMAND_DELIMITERS = r"[\s/,;=+<>|\"]+"


def _command_names(text):
    """The names of the programs a command line runs: per part of a pipe, without "@",
    redirections, LH/LOADHIGH/CALL in front, the path and the extension ("@lh Z:\\MOUNT.COM d /"
    -> ["MOUNT"])."""
    names = []
    for part in text.split("|"):
        # Redirections may stand anywhere, also in front of the command
        part = re.sub(r"(>>?|<)\s*[^\s|<>]*", " ", part)
        words = [w for w in re.split(COMMAND_DELIMITERS, part.lstrip("@ \t").strip()) if w]
        while words:
            word = words.pop(0).lstrip("@")
            if not word:
                continue
            # "MOUNT." is MOUNT as well: DOS names end at their last dot without an extension
            name = re.split(r"[\\:]", word)[-1].upper().rstrip(".")
            name = re.sub(r"\.(COM|EXE|BAT)$", "", name).rstrip(".")
            if name in COMMAND_PREFIXES:
                continue
            names.append(name)
            break
    return names


def check_command(line):
    """Refuses DOS commands that reach beyond the installer's drives."""
    text = line.strip()
    if "\n" in text or "\r" in text:
        raise PresetError("one command per line")
    if any(n in REFUSED_COMMANDS for n in _command_names(text)) \
            or re.search(r"(^|[\s=+;,@|<>\"])z:", text, flags=re.I):
        raise PresetError(f"command not allowed: {line!r}")
    return text


def dosbox_settings(settings):
    """Only the known sections and keys, values as plain words."""
    out = {}
    for section, values in (settings or {}).items():
        allowed = DOSBOX_KEYS.get(section, set())
        for key, value in (values or {}).items():
            value = str(value)
            if key in allowed and re.fullmatch(r"[\w .,+-]{1,40}", value) \
                    and value in DOSBOX_VALUES.get((section, key), {value}):
                out.setdefault(section, {})[key] = value
    return out


def merge_dosbox(*settings):
    out = {}
    for s in settings:
        for section, values in (s or {}).items():
            out.setdefault(section, {}).update(values)
    return out


def dosbox_conf(settings):
    """The settings as DOSBox configuration text."""
    lines = []
    for section, values in dosbox_settings(settings).items():
        lines.append(f"[{section}]")
        lines += [f"{k}={v}" for k, v in values.items()]
        lines.append("")
    return "\n".join(lines)


# The touch controls (plinius-touch-controls) a preset may set: what the stick holds, the big
# buttons (the first where the thumb rests; five as on an arcade panel), the small ones along the
# top and more keys in rows below those. Key names are checked by plinius-touch-controls itself,
# which leaves out names it does not know.
TOUCH_STICKS = ("arrows", "wasd", "numpad")
TOUCH_LISTS = {"buttons": 5, "small": 4, "keys": 12}


def _weapons(n):
    return [str(i) for i in range(1, n + 1)]


# The controls a game gets by its start file, as far as its own settings leave them out. All
# keep the stick on the arrow keys (with_stick leaves the arrows out of a game's settings).
# The Doom engine: fire, open, sideways and run (the right Shift, its own key for it); Enter for
# the menu, Tab for the map, Y for the game's questions (quit, quick load).
_DOOM = {"buttons": ["ctrl", "space", "alt", "rshift"],
         "small": ["esc", "enter", "tab", "keyboard"], "keys": _weapons(7) + ["y"]}
TOUCH_GAMES = {
    # Doom, Doom II and Final Doom (Plutonia and TNT start with DOOM2.EXE)
    "DOOM.EXE": _DOOM, "DOOM2.EXE": _DOOM,
    # Heretic: Enter uses the item picked with [ and ], seven weapons
    "HERETIC.EXE": {"buttons": ["ctrl", "space", "alt", "enter"],
                    "small": ["esc", "tab", "y", "keyboard"],
                    "keys": _weapons(7) + ["rshift", "lbracket", "rbracket"]},
    # Hexen: as Heretic, four weapons, / jumps
    "HEXEN.EXE": {"buttons": ["ctrl", "space", "alt", "slash"],
                  "small": ["esc", "enter", "tab", "keyboard"],
                  "keys": _weapons(4) + ["lbracket", "rbracket", "rshift", "y"]},
    # Strife: eight weapons, A jumps, Enter uses the item picked with [ and ]
    "STRIFE1.EXE": {"buttons": ["ctrl", "space", "alt", "a"],
                    "small": ["esc", "enter", "tab", "keyboard"],
                    "keys": _weapons(8) + ["lbracket", "rbracket", "rshift", "y"]},
    # Wolfenstein 3D and Spear of Destiny: run is the right Shift only (CONFIG.WL6 as the game
    # writes it), four weapons, F8 and F9 save and load quickly
    "WOLF3D.EXE": {"buttons": ["ctrl", "space", "alt", "rshift"],
                   "small": ["esc", "enter", "y", "keyboard"],
                   "keys": _weapons(4) + ["f8", "f9"]},
    # Mortal Kombat: the second player's keys in its CMOS file (the arrows and the number pad),
    # as on the arcade panel: high punch, block, high kick above, low punch and low kick below.
    # F10 is its setup, the keyboard for the word from the manual it asks for. Its start keys
    # are in no file: F1 and F2 offered until tried on the phone.
    "MK.EXE": {"buttons": ["kp7", "kp5", "kp9", "kp1", "kp3"],
               "small": ["esc", "enter", "f10", "keyboard"], "keys": ["f1", "f2"]},
    # Duke Nukem, all three episodes: Ctrl jumps, Alt fires, up on the stick uses
    **{f"DN{n}.EXE": {"buttons": ["ctrl", "alt"], "small": ["esc", "enter", "y", "keyboard"]}
       for n in (1, 2, 3)},
}
TOUCH_GAMES["SPEAR.EXE"] = TOUCH_GAMES["WOLF3D.EXE"]


def touch_settings(settings):
    """Only a known stick and lists of plain key names."""
    out = {}
    if not isinstance(settings, dict):
        return out
    if settings.get("stick") in TOUCH_STICKS:
        out["stick"] = settings["stick"]
    for key, limit in TOUCH_LISTS.items():
        names = settings.get(key)
        if isinstance(names, list):
            names = [n.lower() for n in names if isinstance(n, str)]
            out[key] = [n for n in names if re.fullmatch(r"[a-z0-9]{1,10}", n)][:limit]
    return out


def with_stick(settings, stick):
    """The touch settings with the stick holding stick; the arrow keys are the controls' own and
    leave it out."""
    out = dict(touch_settings(settings))
    out.pop("stick", None)
    if stick in TOUCH_STICKS and stick != TOUCH_STICKS[0]:
        out["stick"] = stick
    return out


def game_touch(settings, program=""):
    """The touch settings of a game started with program: its own, and for what they leave out
    the controls its start file wants (TOUCH_GAMES)."""
    s = touch_settings(settings)
    known = TOUCH_GAMES.get((program or "").upper(), {})
    for key in TOUCH_LISTS:
        if key in known and key not in s:
            s[key] = list(known[key])
    return s


def touch_args(settings, program=""):
    """The settings of a game started with program as arguments of plinius-touch-controls."""
    s = game_touch(settings, program)
    args = ["--stick", s["stick"]] if "stick" in s else []
    for key in TOUCH_LISTS:
        if key in s:
            args += [f"--{key}", ",".join(s[key])]
    return args


# Configuration files


def _set_keyvalue(text, values):
    """Files of "name<whitespace>value" lines (the Doom engine's DEFAULT.CFG)."""
    lines = text.splitlines()
    seen = set()
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)(\S+)(\s+)(.*)$", line)
        if m and m.group(2) in values:
            lines[i] = f"{m.group(1)}{m.group(2)}{m.group(3)}{values[m.group(2)]}"
            seen.add(m.group(2))
    for key, value in values.items():
        if key not in seen:
            lines.append(f"{key}\t\t{value}")
    return "\r\n".join(lines) + "\r\n"


def _set_ini(text, values):
    """INI files: values is {section: {key: value}}; keys keep their spelling and spacing."""
    lines = text.splitlines()
    for section, keys in values.items():
        start = next((i for i, l in enumerate(lines)
                      if l.strip().lower() == f"[{section.lower()}]"), None)
        if start is None:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(f"[{section}]")
            lines += [f"{k} = {v}" for k, v in keys.items()]
            continue
        end = next((i for i in range(start + 1, len(lines)) if lines[i].strip().startswith("[")),
                   len(lines))
        missing = dict(keys)
        for i in range(start + 1, end):
            m = re.match(r"^(\s*)([^=;#]+?)(\s*=\s*)(.*)$", lines[i])
            if m and m.group(2).strip() in missing:
                lines[i] = f"{m.group(1)}{m.group(2)}{m.group(3)}{missing.pop(m.group(2).strip())}"
        insert = end
        while insert > start + 1 and not lines[insert - 1].strip():
            insert -= 1
        lines[insert:insert] = [f"{k} = {v}" for k, v in missing.items()]
    return "\r\n".join(lines) + "\r\n"


def write_file(root, path, spec, log=print):
    """Writes or changes one file below root (drive C:) as spec says."""
    target = _host_file(root, path)
    if "text" in spec:
        content = spec["text"].replace("\r\n", "\n").replace("\n", "\r\n").encode("cp437")
    elif "base64" in spec:
        content = base64.b64decode(spec["base64"], validate=True)
    elif spec.get("format") in ("keyvalue", "ini"):
        try:
            old = target.read_bytes().decode("cp437")
        except OSError:
            old = ""
        fn = _set_keyvalue if spec["format"] == "keyvalue" else _set_ini
        content = fn(old, spec.get("set", {})).encode("cp437")
    else:
        raise PresetError(f"{path}: nothing to write")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    log(f"presets: wrote {path}")


# Rules for families of games


def _deice(files):
    """id Software's DEICE floppies: DEICE.EXE and a .DAT (PATH=, SIZE=, LINE0= title) on disk
    1, the self-extracting archive cut into NAME.1 .. NAME.n over the disks. DEICE only puts
    the pieces together and starts the archive, after asking for drive, folder and whether to
    create it; the batch file then runs SETUP. Here the pieces are put together on C: right
    away and only the archive runs in DOSBox: no questions, no setup."""
    if "DEICE.EXE" not in files:
        return None
    for name, data in files.items():
        if "/" in name or not name.endswith(".DAT"):
            continue
        try:
            info = dict(line.split("=", 1) for line in data.decode("cp437").splitlines()
                        if "=" in line)
        except ValueError:
            continue
        if "PATH" not in info or "SIZE" not in info:
            continue
        stem = name[:-4]
        parts = []
        n = 1
        while f"{stem}.{n}" in files:
            parts.append(files[f"{stem}.{n}"])
            n += 1
        try:
            size = int(info["SIZE"].strip())
        except ValueError:
            # Not a DEICE disk as this rule knows it: its installer runs as it is
            return None
        if not parts or sum(map(len, parts)) != size:
            return None
        folder = "/".join(_dos_path(info["PATH"]))
        title = re.sub(r"\*+|\bv?\d+(\.\d+)+\b", "", info.get("LINE0", "")).strip(" -")
        # "WOLFENSTEIN 3D" -> "Wolfenstein 3D", "DOOM II: Hell on Earth" stays
        if title.isupper():
            title = " ".join(w if any(c.isdigit() for c in w) or re.fullmatch(r"[IVX]+", w)
                             else w.capitalize() for w in title.split())
        return {"folder": folder, "archive": f"{stem}.EXE", "data": b"".join(parts),
                "title": title or None}
    return None


INSTALLER_STEMS = ("INSTALL", "SETUP", "INSTALAR", "INSTALLA", "INSTALLE")


def _sfx_zips(files):
    """Disks with self-extracting ZIP archives and no installer (Apogee's Duke Nukem: one
    archive per episode, "copy it to C:\\DUKE1 and run it"): [(folder, archive, content)],
    each folder named after its archive without "ZIP" (DUKEZIP1.EXE -> DUKE1). The archives
    run in DOSBox: PKZIP 1 packed with methods that Python does not unpack. None if there is
    an installer or no such archive."""
    top = [n for n in files if "/" not in n]
    if any(os.path.splitext(n)[0] in INSTALLER_STEMS and n.endswith((".EXE", ".COM", ".BAT"))
           for n in top):
        return None
    out = []
    for name in sorted(top):
        if not name.endswith(".EXE"):
            continue
        data = files[name]
        if len(data) < 32 * 1024 or not zipfile.is_zipfile(io.BytesIO(data)):
            continue
        stem = name[:-4]
        out.append((stem.replace("ZIP", "") or stem, name, data))
    return out or None


# Doom engine: the main data file, the configuration file next to it, the program
IDTECH1 = {
    "DOOM.WAD": ("DEFAULT.CFG", "DOOM.EXE"), "DOOM1.WAD": ("DEFAULT.CFG", "DOOM.EXE"),
    "DOOM2.WAD": ("DEFAULT.CFG", "DOOM2.EXE"), "PLUTONIA.WAD": ("DEFAULT.CFG", "DOOM2.EXE"),
    "TNT.WAD": ("DEFAULT.CFG", "DOOM2.EXE"), "HERETIC.WAD": ("HERETIC.CFG", "HERETIC.EXE"),
    "HERETIC1.WAD": ("HERETIC.CFG", "HERETIC.EXE"), "HEXEN.WAD": ("HEXEN.CFG", "HEXEN.EXE"),
    "STRIFE1.WAD": ("STRIFE.CFG", "STRIFE1.EXE"),
}
# Sound Blaster as DOSBox has it (port 0x220 = 544); music through its FM synthesizer, as the
# phone has no MIDI synthesizer behind DOSBox's MPU-401. A touch is the left mouse button:
# firing goes to the middle one, which a finger never presses, and stays on Ctrl. Moving the
# finger still turns and walks, at the menu's highest mouse speed (see TOUCH_TURN).
IDTECH1_CONFIG = {
    "snd_musicdevice": "3", "snd_sfxdevice": "3", "snd_sbport": "544", "snd_sbirq": "7",
    "snd_sbdma": "1", "snd_mport": "-1", "use_mouse": "1", "mouseb_fire": "2",
    "mouseb_strafe": "1", "mouseb_forward": "2", "mouse_sensitivity": "9",
}
# Turning by swiping: DOSBox gives about one mickey per pixel the finger moves, and both games
# were made for a mouse that moves a few centimetres. Doom turns 8192 mickeys for a full circle
# at its default speed 5 (angleturn 8 per mickey), Wolfenstein 3D 5760 (1.25 per mickey, 20
# per degree): a swipe across the 1440 px picture turned Doom by 63 degrees. Both games walk
# with the same mickeys up and down, so turning alone gets faster through the mouse driver
# (TOUCH_MOUSE). Tried on the phone in Doom: three times DOSBox's mickeys and Doom's speed 9
# (x4.2 both ways), the driver's sideways sensitivity at its highest (x3.06 more); about a
# circle and a half per 1000 px sideways, walking as before. Wolfenstein turns as fast.
TOUCH_TURN = {"sdl": {"sensitivity": "300"}}
TOUCH_MOUSE = [100, 50]

# DOSBox's mouse sensitivity in percent, both ways (0.74 takes 1 to 1000); a game's page sets it
# in steps of ten from 10 to 1000
SENSITIVITY = (10, 1000)
SENSITIVITY_DEFAULT = 100


def sensitivity(settings):
    """The game's mouse sensitivity in percent (DOSBox's 100 when unset), within SENSITIVITY."""
    value = dosbox_settings(settings).get("sdl", {}).get("sensitivity")
    try:
        value = int(value)
    except (TypeError, ValueError):
        return SENSITIVITY_DEFAULT
    return max(SENSITIVITY[0], min(SENSITIVITY[1], value))


def with_setting(settings, section, key, value):
    """A copy of the DOSBox settings with key set to value; None takes it out, and with it a
    section left empty."""
    out = {name: dict(values) for name, values in (settings or {}).items()}
    values = out.setdefault(section, {})
    if value is None:
        values.pop(key, None)
    else:
        values[key] = str(value)
    if not values:
        del out[section]
    return out


def with_sensitivity(settings, percent):
    """The settings with the mouse sensitivity set to percent; DOSBox's own 100 leaves it out."""
    percent = max(SENSITIVITY[0], min(SENSITIVITY[1], int(round(percent))))
    return with_setting(settings, "sdl", "sensitivity",
                        None if percent == SENSITIVITY_DEFAULT else percent)


# The CPU speeds a game's page offers, as DOSBox's cycles. "auto" is DOSBox's own: 3000 for
# games in real mode, as many as the phone manages for those in protected mode. The fixed ones
# are roughly a 286, a 386 and a 486; "max" all the phone can give, which costs battery.
CPU_SPEEDS = ("auto", "3000", "8000", "25000", "max")


def cpu_speed(settings):
    """The game's cycles setting as it stands ("auto" when unset)."""
    return dosbox_settings(settings).get("cpu", {}).get("cycles", "auto")


def with_cpu_speed(settings, cycles):
    return with_setting(settings, "cpu", "cycles", None if cycles == "auto" else cycles)


def sharp_pixels(settings):
    """Whether the picture is drawn without smoothing (DOSBox's openglnb)."""
    return dosbox_settings(settings).get("sdl", {}).get("output") == "openglnb"


def with_sharp_pixels(settings, on):
    return with_setting(settings, "sdl", "output", "openglnb" if on else None)


# The mouse driver's sensitivity (INT 33h function 1Ah), sideways and up and down: 1 to 100,
# 50 is as it comes. DOSBox's driver keeps it when a game resets the mouse, so a small program
# sets it before the game starts.
def mouse_speed(value):
    """[sideways, up and down] when value is two whole numbers from 1 to 100, else []."""
    if isinstance(value, (list, tuple)) and len(value) == 2 and all(
            isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 100 for v in value):
        return [value[0], value[1]]
    return []


def mouse_program(speed):
    """A DOS .COM program that sets the mouse driver's sensitivity to speed (mouse_speed):
    mov ax,1Ah; mov bx,x; mov cx,y; mov dx,50 (double speed threshold); int 33h; ret."""
    x, y = mouse_speed(speed) or (50, 50)
    return (b"\xb8\x1a\x00\xbb" + struct.pack("<H", x) + b"\xb9" + struct.pack("<H", y)
            + b"\xba\x32\x00\xcd\x33\xc3")


def _idtech1(root):
    """Doom engine games below root: (DOS folder, program, config file) for each."""
    found = []
    for dirpath, dirs, files in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        if rel != "." and rel.count(os.sep) >= 2:
            dirs[:] = []
        upper = {f.upper(): f for f in files}
        for wad, (cfg, exe) in IDTECH1.items():
            if wad in upper and exe in upper:
                dos_dir = "" if rel == "." else rel.replace(os.sep, "\\").upper()
                found.append((dos_dir, exe, cfg))
                break
    return found


# Wolfenstein 3D and Spear of Destiny: CONFIG.<ext> next to the data files, binary. Layout
# from WL_MAIN.C (ReadConfig) and measured on a CONFIG.WL6 the game wrote: 7 high scores of
# 66 bytes, then 16-bit words: sound, music, digitized sound, mouse/joystick/joypad enabled,
# joystick progressive and port, dirscan[4], buttonscan[8], buttonmouse[4], buttonjoy[4],
# view size, mouse speed.
WOLF3D = {"WL6": "WOLF3D.EXE", "WL1": "WOLF3D.EXE", "SOD": "SPEAR.EXE", "SDM": "SPEAR.EXE"}
WOLF3D_SIZE = 522
WOLF3D_MOUSE = 502
# Buttons: 0 attack, 1 strafe, 3 use. A touch (left) opens doors, firing goes to the middle
# button and stays on Ctrl. Not "no button" (-1) for the left one: the game writes in front
# of its button table then.
WOLF3D_BUTTONS = (3, 1, 0, -1)
WOLF3D_SCORES = ("id software-'92", "Adrian Carmack", "John Carmack", "Kevin Cloud",
                 "Tom Hall", "John Romero", "Jay Wilbur")


def _wolf3d_default():
    """The configuration the game writes on its first exit, with the buttons above."""
    out = b"".join(name.encode().ljust(58, b"\0") + struct.pack("<lHH", 10000, 1, 0)
                   for name in WOLF3D_SCORES)
    out += struct.pack("<3h5h4h8h4h4h2h", 2, 1, 3, 1, 0, 0, 0, 0, 72, 77, 80, 75,
                       29, 56, 54, 57, 2, 3, 4, 5, *WOLF3D_BUTTONS, 0, 1, 3, 2, 15, 5)
    return out


def _wolf3d(root, log):
    """Wolfenstein 3D games below root: (DOS folder, program); their configuration set."""
    found = []
    for dirpath, dirs, files in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        if rel != "." and rel.count(os.sep) >= 2:
            dirs[:] = []
        upper = {f.upper(): f for f in files}
        for ext, exe in WOLF3D.items():
            if f"VSWAP.{ext}" in upper and exe in upper:
                cfg = os.path.join(dirpath, upper.get(f"CONFIG.{ext}", f"CONFIG.{ext}"))
                try:
                    with open(cfg, "rb") as f:
                        data = bytearray(f.read())
                except OSError:
                    data = bytearray(_wolf3d_default())
                if len(data) == WOLF3D_SIZE:
                    struct.pack_into("<4h", data, WOLF3D_MOUSE, *WOLF3D_BUTTONS)
                    with open(cfg, "wb") as f:
                        f.write(data)
                    log(f"presets: wrote {os.path.relpath(cfg, root)}")
                dos_dir = "" if rel == "." else rel.replace(os.sep, "\\").upper()
                found.append((dos_dir, exe))
                break
    return found


# The plans


@dataclass
class InstallPlan:
    """How to install a game that a rule or a preset knows."""
    title: str = None
    # Runs instead of the installer, on C: with the disks as A: (no keyboard needed)
    commands: list = field(default_factory=list)
    # Written onto C: before DOSBox starts: DOS path -> bytes
    files: dict = field(default_factory=dict)
    # Instead of the installer the app found, and lines dropped from batch files on A:
    program: str = None
    drop: dict = field(default_factory=dict)
    # C: folders made beforehand, so the installer does not ask whether to make them
    mkdirs: list = field(default_factory=list)
    preset: dict = None

    @property
    def automatic(self):
        return bool(self.commands)


@dataclass
class Result:
    """What the rules and presets found after the installation."""
    title: str = None
    program: tuple = None     # (DOS folder, file)
    dosbox: dict = field(default_factory=dict)
    # The touch controls (touch_settings)
    touch: dict = field(default_factory=dict)
    # The mouse driver's sensitivity (mouse_speed)
    mouse: list = field(default_factory=list)


def _matches(files, wanted, size_of):
    """wanted: DOS paths that must all be there, or {path: size} to tell versions apart.
    size_of(path) gives the size of a file in files."""
    if not wanted:
        return False
    sizes = wanted if isinstance(wanted, dict) else dict.fromkeys(wanted)
    for path, size in sizes.items():
        key = path.upper().replace("\\", "/")
        if key not in files:
            return False
        if size is not None and size_of(key) != int(size):
            return False
    return True


def find(presets, source_files=None, installed=None):
    """The preset for a game: by the files of its disk 1 or source folder (DOS path ->
    content), or by the files on C: after the installation (DOS path -> host path)."""
    for game in presets.get("games", []):
        if source_files is not None and _matches(
                source_files, game.get("source"), lambda k: len(source_files[k])):
            return game
        if installed is not None and _matches(
                installed, game.get("installed"), lambda k: os.path.getsize(installed[k])):
            return game
    return None


def install_plan(presets, files, log=print):
    """files: DOS path -> content of all disks (or the source folder). None: the installer
    runs as it is."""
    preset = find(presets, source_files=files)
    if preset and preset.get("install"):
        spec = preset["install"]
        plan = InstallPlan(title=preset.get("title"), preset=preset)
        plan.commands = [check_command(c) for c in spec.get("commands", [])]
        plan.program = spec.get("program")
        if plan.program:
            check_command(plan.program)
        plan.drop = {k.upper(): list(v) for k, v in spec.get("drop", {}).items()}
        plan.mkdirs = ["/".join(_dos_path(d)) for d in spec.get("mkdirs", [])]
        log(f"presets: installing {preset.get('id')} as the list says")
        return plan
    deice = _deice(files)
    if deice:
        folder = deice["folder"]
        log(f"presets: DEICE floppies, {deice['archive']} into C:\\{folder}")
        return InstallPlan(
            title=(preset or {}).get("title") or deice["title"], preset=preset,
            files={f"{folder}/{deice['archive']}": deice["data"]},
            commands=["C:", "cd \\" + folder.replace("/", "\\"), deice["archive"],
                      "del " + deice["archive"]])
    sfx = _sfx_zips(files)
    if sfx:
        log(f"presets: self-extracting ZIP archives into {', '.join(f for f, _a, _d in sfx)}")
        plan = InstallPlan(title=(preset or {}).get("title"), preset=preset)
        plan.commands.append("C:")
        for folder, archive, data in sfx:
            plan.files[f"{folder}/{archive}"] = data
            plan.commands += [f"cd \\{folder}", archive, f"del {archive}"]
        return plan
    if preset:
        return InstallPlan(title=preset.get("title"), preset=preset)
    return None


def prepare_drive(plan, a_dir, c_dir, log=print):
    """Before DOSBox starts: files and folders on C:, batch lines dropped on A:."""
    for d in plan.mkdirs:
        os.makedirs(os.path.join(c_dir, *d.split("/")), exist_ok=True)
    for path, data in plan.files.items():
        target = os.path.join(c_dir, *_dos_path(path))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as f:
            f.write(data)
    for name, patterns in plan.drop.items():
        if not a_dir:
            break
        target = _host_file(a_dir, name)
        try:
            text = target.read_bytes().decode("cp437")
        except OSError:
            continue
        keep = [line for line in text.splitlines()
                if not any(line.strip().lower() == p.lower() for p in patterns)]
        target.write_bytes(("\r\n".join(keep) + "\r\n").encode("cp437"))
        log(f"presets: {name} without {', '.join(patterns)}")


def after_install(presets, root, plan=None, log=print):
    """Applies the rules and the game's preset to the installed game on root (drive C:)."""
    result = Result()
    installed = {}
    for dirpath, _dirs, files in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        prefix = "" if rel == "." else rel.replace(os.sep, "/").upper() + "/"
        for f in files:
            installed[prefix + f.upper()] = os.path.join(dirpath, f)
    preset = (plan and plan.preset) or find(presets, installed=installed)

    for dos_dir, exe, cfg in _idtech1(root):
        path = (dos_dir.replace("\\", "/") + "/" if dos_dir else "") + cfg
        write_file(root, path, {"format": "keyvalue", "set": IDTECH1_CONFIG}, log)
        if result.program is None:
            result.program = (dos_dir, exe)
            result.dosbox = merge_dosbox({"cpu": {"cycles": "max"}}, TOUCH_TURN)
            result.mouse = list(TOUCH_MOUSE)

    for dos_dir, exe in _wolf3d(root, log):
        if result.program is None:
            result.program = (dos_dir, exe)
            result.dosbox = merge_dosbox(TOUCH_TURN)
            result.mouse = list(TOUCH_MOUSE)

    if preset:
        for path, spec in preset.get("files", {}).items():
            try:
                write_file(root, path, spec, log)
            except (PresetError, ValueError, UnicodeError) as e:
                log(f"presets: {path} not written: {e}")
        if preset.get("program"):
            parts = _dos_path(preset["program"])
            if _host_file(root, "/".join(parts)).is_file():
                result.program = ("\\".join(parts[:-1]).upper(), parts[-1].upper())
        result.dosbox = merge_dosbox(result.dosbox, dosbox_settings(preset.get("dosbox")))
        result.title = preset.get("title")
        result.touch = touch_settings(preset.get("touch"))
        result.mouse = mouse_speed(preset.get("mouse")) or result.mouse
    if plan and plan.title and not result.title:
        result.title = plan.title
    return result


if __name__ == "__main__":
    # python3 -m dosinstall.presets: shows which list would be used
    p = load(fetch="--offline" not in sys.argv)
    print(json.dumps({"updated": p.get("updated"), "games": [g.get("id") for g in p["games"]]}))
