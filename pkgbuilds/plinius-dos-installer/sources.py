"""What the user picked, and how to get files out of it.

Floppy images (.img/.ima/.vfd, several for one game), a CD image (.iso, or .cue with its
.bin), a ZIP archive, a GOG installer (setup_*.exe) or a folder. A folder, a ZIP and a GOG
installer end up as a folder of files; floppies and CDs stay images, because their installer
may need the disk itself.
"""

import os
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass, field

FLOPPY_EXTS = (".img", ".ima", ".vfd", ".flp")
CD_EXTS = (".iso", ".cue")


class SourceError(Exception):
    pass


@dataclass
class Source:
    kind: str            # floppies, cd, zip, gog, folder
    paths: list = field(default_factory=list)

    @property
    def name(self):
        """A first guess at the game's name, from the file or folder name."""
        base = os.path.basename(self.paths[0].rstrip("/"))
        base = re.sub(r"\.(img|ima|vfd|flp|iso|cue|zip|exe)$", "", base, flags=re.I)
        base = re.sub(r"^setup_", "", base, flags=re.I)
        # "dott_disk1", "Game (Disk 1 of 7)" -> the part before the disk number
        base = re.sub(r"[\s_\-]*[\(\[]?(disk|disc|cd)\s*[\d]+.*$", "", base, flags=re.I)
        base = re.sub(r"[\s_\-]+\d+$", "", base) if self.kind == "floppies" else base
        # GOG: setup_game_name_1.0.2_(12345).exe
        base = re.sub(r"[\s_]+[\d.]+(_\(\d+\))?$", "", base) if self.kind == "gog" else base
        base = re.sub(r"[\s_\-]+(cd|cdrom)$", "", base, flags=re.I)
        name = re.sub(r"[_]+", " ", base).strip()
        if not name and self.kind in ("floppies", "cd"):
            # "disk1.img" says nothing: the folder it is in usually does
            folder = os.path.basename(os.path.dirname(os.path.abspath(self.paths[0])))
            name = re.sub(r"[_]+", " ", folder).strip()
        return name or "DOS Game"


def _disk_match(name):
    """Where the disk number is in a file name: after "disk"/"disc" ("Game (Disk 1 of 3)"),
    else the last number ("mi2-1.img")."""
    m = re.search(r"(?:disk|disc)[\s_\-]*(\d+)", name, flags=re.I)
    if m:
        return m.span(1)
    nums = list(re.finditer(r"\d+", name))
    return nums[-1].span() if nums else None


def _disk_number(path):
    """Sort key: the disk number in the file name, so disk10 comes after disk9."""
    base = os.path.basename(path)
    span = _disk_match(base)
    return (int(base[span[0]:span[1]]) if span else 0, path)


def _set_pattern(path):
    """The name of a disk image with its disk number taken out ("disk1.img" -> "disk#.img"),
    or None without a number. Images with the same pattern in one folder are one set."""
    base = os.path.basename(path)
    span = _disk_match(base)
    if not span:
        return None
    return (base[:span[0]] + "#" + base[span[1]:]).lower()


def floppy_set(paths):
    """The chosen images and the other disks of their sets, from the same folders. Choosing
    several files is hard to find on a phone, so disk 1 alone brings the others along."""
    found = {os.path.realpath(p): p for p in paths}
    for p in paths:
        pattern = _set_pattern(p)
        folder = os.path.dirname(os.path.abspath(p))
        if pattern is None:
            continue
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            other = os.path.join(folder, name)
            if name.lower().endswith(FLOPPY_EXTS) and _set_pattern(name) == pattern \
                    and os.path.isfile(other):
                found.setdefault(os.path.realpath(other), other)
    return sorted(found.values(), key=_disk_number)


def classify(paths):
    """Turns what the file chooser returned into a Source."""
    if not paths:
        raise SourceError("nothing chosen")
    if len(paths) == 1 and os.path.isdir(paths[0]):
        return Source("folder", paths)
    lower = [p.lower() for p in paths]
    if all(p.endswith(FLOPPY_EXTS) for p in lower):
        return Source("floppies", floppy_set(paths))
    if all(p.endswith(CD_EXTS) for p in lower):
        return Source("cd", sorted(paths, key=_disk_number))
    if len(paths) == 1 and lower[0].endswith(".zip"):
        return Source("zip", paths)
    if len(paths) == 1 and lower[0].endswith(".exe"):
        return Source("gog", paths)
    raise SourceError("unknown mix of files")


def _safe_member(root, name):
    dest = os.path.realpath(os.path.join(root, name))
    if not dest.startswith(os.path.realpath(root) + os.sep):
        raise SourceError(f"archive entry outside its folder: {name}")
    return dest


def _drop_outside_links(root, log=print):
    """Removes the symbolic links below root that point out of it: 7-Zip unpacks those of an
    ISO or archive as they are, and DOSBox (D:) or the copy into the game's folder would
    follow them to the user's own files."""
    top = os.path.realpath(root)
    for dirpath, dirs, names in os.walk(root):
        for name in dirs + names:
            path = os.path.join(dirpath, name)
            if os.path.islink(path) and os.path.commonpath(
                    [top, os.path.realpath(path)]) != top:
                os.unlink(path)
                log(f"left out {os.path.relpath(path, root)}: a link out of the unpacked files")


def unpack(source, work, log=print):
    """For a ZIP or GOG installer: unpacks it into work and returns that folder. A folder is
    returned as it is; images are not unpacked here."""
    if source.kind == "folder":
        return source.paths[0]
    os.makedirs(work, exist_ok=True)
    if source.kind == "zip":
        with zipfile.ZipFile(source.paths[0]) as z:
            for info in z.infolist():
                dest = _safe_member(work, info.filename)
                if info.is_dir():
                    os.makedirs(dest, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with z.open(info) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
        log(f"unpacked {source.paths[0]}")
        return work
    if source.kind == "gog":
        if not shutil.which("innoextract"):
            raise SourceError("innoextract is missing")
        r = subprocess.run(["innoextract", "--gog", "--silent", "--progress=0", "-d", work,
                            source.paths[0]], capture_output=True, text=True)
        if r.returncode:
            raise SourceError((r.stderr or r.stdout).strip() or "innoextract failed")
        _drop_outside_links(work, log)
        log(f"unpacked {source.paths[0]}")
        return work
    raise SourceError(f"{source.kind} is not unpacked")


def unpack_cd(iso, work, log=print):
    """The files of an ISO image, for ScummVM: CD games usually run from their data files."""
    if not shutil.which("7z"):
        raise SourceError("7z is missing")
    r = subprocess.run(["7z", "x", "-y", f"-o{work}", iso], capture_output=True, text=True)
    if r.returncode:
        raise SourceError((r.stderr or r.stdout).strip() or "7z failed")
    _drop_outside_links(work, log)
    log(f"unpacked {iso}")
    return work


def cd_files(image):
    """The image and, for a .cue, the track files it names (in its folder)."""
    files = [image]
    if image.lower().endswith(".cue"):
        folder = os.path.dirname(image)
        with open(image, encoding="latin-1") as f:
            for line in f:
                m = re.match(r'\s*FILE\s+"?(.+?)"?\s+\w+\s*$', line, re.I)
                if m:
                    track = os.path.join(folder, os.path.basename(m.group(1)))
                    if not os.path.exists(track):
                        raise SourceError(f"{os.path.basename(image)} names a missing file: "
                                          f"{m.group(1)}")
                    files.append(track)
    return files
