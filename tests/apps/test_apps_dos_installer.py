# plinius-dos-installer: what the user picked (sources), the FAT reader, the floppy set that swaps
# its disks by itself, the DOSBox configuration and installer runs, the signed list of presets
# and its rules, ScummVM detection and icons, the game library, the DOSBox launchers, the whole
# installation flow, the command line and plinius-scummvm-launchers.
#
# The modules form the package "dosinstall" (as below /usr/lib/plinius-dos-installer); it is
# imported from the repository with every path it keeps (~/.local/share, ~/.config, ~/Games/dos,
# the cache, the keyring) pointed into tmp_path. DOSBox, ScummVM, 7z, innoextract and the network
# are fakes; the signature tests use throw-away keys (apps_gpgkeys).

import base64
import contextlib
import importlib
import importlib.util
import io
import json
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
import types
import urllib.error
import zipfile
from pathlib import Path

import pytest

from apps_gpgkeys import gpg_keys  # noqa: F401  (fixture)
from conftest import PKG, load_script

DOS = PKG / "plinius-dos-installer"


# ----- the package -----

def dosinstall(tmp_root):
    """The package from the repository (once per process); its import-time paths point below
    tmp_root, each test points them at its own tmp_path again (fixture home)."""
    if "dosinstall" not in sys.modules:
        with pytest.MonkeyPatch.context() as mp:
            for var in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME",
                        "PLINIUS_DOS_GAMES"):
                mp.setenv(var, str(tmp_root / var.lower()))
            spec = importlib.util.spec_from_file_location(
                "dosinstall", DOS / "__init__.py", submodule_search_locations=[str(DOS)])
            pkg = importlib.util.module_from_spec(spec)
            sys.modules["dosinstall"] = pkg
            spec.loader.exec_module(pkg)
            for name in ("sources", "fat", "floppies", "keyboard", "presets", "dosbox",
                         "scummvm", "library", "dosgames", "finish", "install", "__main__"):
                importlib.import_module(f"dosinstall.{name}")
    return types.SimpleNamespace(**{n: sys.modules[f"dosinstall.{n}"] for n in (
        "sources", "fat", "floppies", "keyboard", "presets", "dosbox", "scummvm", "library",
        "dosgames", "finish", "install")}, main=sys.modules["dosinstall.__main__"])


@pytest.fixture(scope="module")
def m(tmp_path_factory):
    return dosinstall(tmp_path_factory.mktemp("dosinstall-import"))


@pytest.fixture(autouse=True)
def private_tempdir(tmp_path, monkeypatch):
    """tempfile's files and folders (DOSBox configurations, work folders) go below tmp_path."""
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))
    return tmp


class Home(types.SimpleNamespace):
    pass


@pytest.fixture
def home(m, tmp_path, monkeypatch):
    """Every path the package writes to, below tmp_path; no network, temp files in tmp_path."""
    h = Home(data=tmp_path / "data", config=tmp_path / "config", games=tmp_path / "Games/dos",
             cache=tmp_path / "cache", keys=tmp_path / "keys")
    monkeypatch.setattr(m.scummvm, "DATA", h.data)
    monkeypatch.setattr(m.scummvm, "CONFIG", h.config)
    monkeypatch.setattr(m.scummvm, "INI", h.config / "scummvm/scummvm.ini")
    monkeypatch.setattr(m.scummvm, "ICONS", h.data / "plinius-dos-installer/scummvm-icons-fetched")
    monkeypatch.setattr(m.dosgames, "APPS", h.data / "applications")
    monkeypatch.setattr(m.dosgames, "ICONS", h.data / "plinius-dos-installer/icons")
    monkeypatch.setattr(m.dosgames, "SCUMMVM_PACK", tmp_path / "share/gui-icons.dat")
    monkeypatch.setattr(m.library, "GAMES", h.games)
    monkeypatch.setattr(m.presets, "CACHE", h.cache)
    monkeypatch.setattr(m.presets, "KEYRING", str(h.keys / "plinius.gpg"))
    monkeypatch.setattr(m.presets, "TRUSTED", str(h.keys / "plinius-trusted"))
    monkeypatch.setattr(m.presets, "REVOKED", str(h.keys / "plinius-revoked"))
    monkeypatch.setattr(m.finish, "LAUNCHERS", str(tmp_path / "no-launchers"))

    def no_network(*_a, **_kw):
        raise AssertionError("network access in a test")
    monkeypatch.setattr(m.presets.urllib.request, "urlopen", no_network)
    return h


class Log(list):
    def __call__(self, text):
        self.append(text)

    def has(self, part):
        return any(part in line for line in self)


@pytest.fixture
def log():
    return Log()


# ----- FAT images -----

def name11(name):
    stem, _, ext = name.rpartition(".") if "." in name.lstrip(".") else (name, "", "")
    return stem.encode("cp437").ljust(8, b" ")[:8] + ext.encode("cp437").ljust(3, b" ")[:3]


def dir_entry(raw_name, attr, first, size):
    return raw_name + bytes([attr]) + b"\0" * 14 + struct.pack("<HI", first, size)


def fat_image(path, tree, *, label=None, fat16=False, total32=False, patch=None):
    """Writes a FAT12 (or FAT16) floppy image of tree: name -> bytes (a file) or dict (a
    folder); a name starting with "!" maps to a raw 32-byte directory entry. patch: cluster ->
    FAT value applied at the end (broken chains)."""
    if fat16:
        bps, spc, reserved, fats, root_entries, total, spf = 512, 1, 1, 2, 512, 8400, 33
    else:
        bps, spc, reserved, fats, root_entries, total, spf = 512, 1, 1, 2, 224, 2880, 9
    data = bytearray(total * bps)
    struct.pack_into("<HBHBHHBH", data, 11, bps, spc, reserved, fats, root_entries,
                     0 if total32 else total, 0xF0, spf)
    if total32:
        struct.pack_into("<I", data, 32, total)
    fat_start = reserved * bps
    root_start = fat_start + fats * spf * bps
    data_start = root_start + root_entries * 32
    cs = spc * bps
    table = {}
    state = {"next": 2}

    def store(content):
        if not content:
            return 0
        n = (len(content) + cs - 1) // cs
        first = state["next"]
        for i in range(n):
            c = first + i
            table[c] = c + 1 if i < n - 1 else (0xFFFF if fat16 else 0xFFF)
            off = data_start + (c - 2) * cs
            data[off:off + cs] = content[i * cs:(i + 1) * cs].ljust(cs, b"\0")
        state["next"] += n
        return first

    def entries(subtree, top):
        out = b""
        if top and label:
            out += dir_entry(label.encode().ljust(11, b" "), 0x08, 0, 0)
        if not top:
            out += dir_entry(b".          ", 0x10, 0, 0) + dir_entry(b"..         ", 0x10, 0, 0)
        for name, value in subtree.items():
            if name.startswith("!"):
                out += value
            elif isinstance(value, dict):
                out += dir_entry(name11(name), 0x10, store(entries(value, False)), 0)
            else:
                out += dir_entry(name11(name), 0x20, store(value), len(value))
        return out

    root = entries(tree, True)
    data[root_start:root_start + len(root)] = root
    for c, v in {**table, **(patch or {})}.items():
        if fat16:
            struct.pack_into("<H", data, fat_start + c * 2, v)
        else:
            off = fat_start + c + c // 2
            if c & 1:
                data[off] = (data[off] & 0x0F) | ((v & 0x0F) << 4)
                data[off + 1] = (v >> 4) & 0xFF
            else:
                data[off] = v & 0xFF
                data[off + 1] = (data[off + 1] & 0xF0) | ((v >> 8) & 0x0F)
    Path(path).write_bytes(bytes(data))
    return str(path)


# ----- sources: what was picked -----

@pytest.mark.parametrize("kind, path, name", [
    ("floppies", "/x/dott_disk1.img", "dott"),
    ("floppies", "/x/Game (Disk 1 of 7).img", "Game"),
    ("floppies", "/x/mi2-1.img", "mi2"),
    # "disk1.img" says nothing: the folder does
    ("floppies", "/x/Monkey_Island/disk1.img", "Monkey Island"),
    ("gog", "/x/setup_day_of_the_tentacle_1.0.2_(12345).exe", "day of the tentacle"),
    ("cd", "/x/Game_CD.iso", "Game"),
    ("zip", "/x/keen.zip", "keen"),
    ("folder", "/x/My Game/", "My Game"),
    ("zip", "/x/.zip", "DOS Game"),
])
def test_source_name(m, kind, path, name):
    assert m.sources.Source(kind, [path]).name == name


def touch(path, data=b"x"):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


def test_floppy_set_brings_the_other_disks_in_disk_order(m, tmp_path):
    d = tmp_path / "game"
    disks = [touch(d / f"game_disk{n}.img") for n in (1, 2, 9, 10)]
    touch(d / "other_disk1.img")          # another set
    touch(d / "game_disk3.txt")           # not an image
    (d / "game_disk4.img").mkdir()        # not a file
    assert m.sources.floppy_set([disks[0]]) == disks


def test_floppy_set_without_a_number_or_folder(m, tmp_path):
    single = touch(tmp_path / "game.img")
    assert m.sources.floppy_set([single]) == [single]
    gone = str(tmp_path / "missing/disk1.img")
    assert m.sources.floppy_set([gone]) == [gone]


def test_floppy_set_counts_a_disk_once(m, tmp_path):
    d1 = touch(tmp_path / "set/disk1.img")
    d2 = touch(tmp_path / "set/disk2.img")
    link = tmp_path / "set/link1.img"
    link.symlink_to(d1)
    # The same file chosen twice under two names: one disk
    found = m.sources.floppy_set([d1, str(link)])
    assert [os.path.realpath(p) for p in found] == [d1, d2]


def test_classify(m, tmp_path):
    s = m.sources
    folder = tmp_path / "folder"
    folder.mkdir()
    assert s.classify([str(folder)]) == s.Source("folder", [str(folder)])
    one = touch(tmp_path / "f/disk1.IMG")
    touch(tmp_path / "f/disk2.img")
    assert s.classify([one]).kind == "floppies"
    assert s.classify([one]).paths == [one, str(tmp_path / "f/disk2.img")]
    assert s.classify(["/x/b_cd2.iso", "/x/b_cd1.cue"]) == \
        s.Source("cd", ["/x/b_cd1.cue", "/x/b_cd2.iso"])
    assert s.classify(["/x/a.ZIP"]).kind == "zip"
    assert s.classify(["/x/setup_a.exe"]).kind == "gog"
    for bad in ([], ["/x/a.zip", "/x/b.zip"], ["/x/a.img", "/x/a.iso"], ["/x/readme.txt"]):
        with pytest.raises(s.SourceError):
            s.classify(bad)


def make_zip(path, members):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            if name.endswith("/"):
                z.writestr(zipfile.ZipInfo(name), b"")
            else:
                z.writestr(name, data)
    return str(path)


def test_unpack_zip(m, tmp_path, log):
    src = make_zip(tmp_path / "game.zip", {"GAME/": b"", "GAME/GAME.EXE": b"MZ", "README": b"hi",
                                           "GAME/DATA/LEVEL1": b"1"})
    work = tmp_path / "work"
    out = m.sources.unpack(m.sources.Source("zip", [src]), str(work), log=log)
    assert out == str(work)
    assert (work / "GAME/GAME.EXE").read_bytes() == b"MZ"
    assert (work / "GAME/DATA/LEVEL1").read_bytes() == b"1"
    assert (work / "README").read_bytes() == b"hi"
    assert log == [f"unpacked {src}"]


@pytest.mark.parametrize("evil", ["../outside.txt", "a/../../outside.txt", "/abs/outside.txt",
                                  "./"])
def test_unpack_zip_refuses_entries_outside_the_folder(m, tmp_path, evil):
    src = make_zip(tmp_path / "evil.zip", {evil: b"pwned"})
    work = tmp_path / "deep/work"
    with pytest.raises(m.sources.SourceError):
        m.sources.unpack(m.sources.Source("zip", [src]), str(work))
    assert not (tmp_path / "deep/outside.txt").exists()
    assert not (tmp_path / "outside.txt").exists()
    assert not Path("/abs/outside.txt").exists()


def test_unpack_zip_refuses_a_symlinked_folder_out(m, tmp_path):
    # A folder in the work directory that is a link elsewhere does not let entries through
    work = tmp_path / "work"
    work.mkdir()
    (tmp_path / "elsewhere").mkdir()
    (work / "link").symlink_to(tmp_path / "elsewhere")
    src = make_zip(tmp_path / "evil.zip", {"link/x": b"pwned"})
    with pytest.raises(m.sources.SourceError):
        m.sources.unpack(m.sources.Source("zip", [src]), str(work))
    assert list((tmp_path / "elsewhere").iterdir()) == []


def test_unpack_folder_and_images(m, tmp_path):
    s = m.sources
    assert s.unpack(s.Source("folder", ["/some/folder"]), str(tmp_path / "w")) == "/some/folder"
    with pytest.raises(s.SourceError, match="floppies is not unpacked"):
        s.unpack(s.Source("floppies", ["/x/a.img"]), str(tmp_path / "w"))


class Run:
    """Stands in for subprocess.run: records the commands, answers from a list."""

    def __init__(self, *answers, effect=None):
        self.answers = list(answers)
        self.calls = []
        self.effect = effect

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if self.effect:
            self.effect(cmd, kw)
        code, out, err = self.answers.pop(0) if self.answers else (0, "", "")
        return types.SimpleNamespace(returncode=code, stdout=out, stderr=err)


def which(*present):
    return lambda name: f"/usr/bin/{name}" if name in present else None


def test_unpack_gog(m, tmp_path, monkeypatch, log):
    s = m.sources
    src = s.Source("gog", ["/x/setup_game.exe"])
    monkeypatch.setattr(s.shutil, "which", which())
    with pytest.raises(s.SourceError, match="innoextract is missing"):
        s.unpack(src, str(tmp_path / "w"))
    monkeypatch.setattr(s.shutil, "which", which("innoextract"))
    run = Run((0, "", ""), (1, "", "not an Inno Setup installer\n"), (1, "", ""))
    monkeypatch.setattr(s.subprocess, "run", run)
    assert s.unpack(src, str(tmp_path / "w"), log=log) == str(tmp_path / "w")
    assert run.calls[0][0] == ["innoextract", "--gog", "--silent", "--progress=0", "-d",
                               str(tmp_path / "w"), "/x/setup_game.exe"]
    assert log == ["unpacked /x/setup_game.exe"]
    with pytest.raises(s.SourceError, match="not an Inno Setup installer"):
        s.unpack(src, str(tmp_path / "w"))
    with pytest.raises(s.SourceError, match="innoextract failed"):
        s.unpack(src, str(tmp_path / "w"))


def test_unpack_cd(m, tmp_path, monkeypatch, log):
    s = m.sources
    monkeypatch.setattr(s.shutil, "which", which())
    with pytest.raises(s.SourceError, match="7z is missing"):
        s.unpack_cd("/x/a.iso", str(tmp_path / "cd"))
    monkeypatch.setattr(s.shutil, "which", which("7z"))
    run = Run((0, "", ""), (2, "", ""), (2, "", "Can not open the file as archive"))
    monkeypatch.setattr(s.subprocess, "run", run)
    assert s.unpack_cd("/x/a.iso", str(tmp_path / "cd"), log=log) == str(tmp_path / "cd")
    assert run.calls[0][0] == ["7z", "x", "-y", f"-o{tmp_path / 'cd'}", "/x/a.iso"]
    with pytest.raises(s.SourceError, match="7z failed"):
        s.unpack_cd("/x/a.iso", str(tmp_path / "cd"))
    with pytest.raises(s.SourceError, match="Can not open"):
        s.unpack_cd("/x/a.iso", str(tmp_path / "cd"))


def unpacked_with_links(work, outside):
    """What 7-Zip or innoextract might leave: links into and out of the unpacked files."""
    touch(work / "DATA/X", b"x")
    touch(outside / "secret", b"key")
    (work / "out").symlink_to(outside / "secret")
    (work / "DATA/updir").symlink_to(outside)
    (work / "abs").symlink_to("/etc")
    (work / "in").symlink_to("DATA/X")
    (work / "DATA/back").symlink_to("..")


def check_links_dropped(work, outside, log):
    assert sorted(p.name for p in work.iterdir()) == ["DATA", "in"]
    assert sorted(p.name for p in (work / "DATA").iterdir()) == ["X", "back"]
    assert (work / "in").read_bytes() == b"x"
    # Only the links went, not what they pointed at
    assert (outside / "secret").read_bytes() == b"key"
    assert sorted(t for t in log if t.startswith("left out")) == [
        "left out DATA/updir: a link out of the unpacked files",
        "left out abs: a link out of the unpacked files",
        "left out out: a link out of the unpacked files"]


def test_unpack_cd_drops_links_out_of_the_cd(m, tmp_path, monkeypatch, log):
    s = m.sources
    work = tmp_path / "cd"
    monkeypatch.setattr(s.shutil, "which", which("7z"))
    monkeypatch.setattr(s.subprocess, "run", Run(
        effect=lambda cmd, kw: unpacked_with_links(work, tmp_path / "home")))
    assert s.unpack_cd("/x/a.iso", str(work), log=log) == str(work)
    check_links_dropped(work, tmp_path / "home", log)
    assert log[-1] == "unpacked /x/a.iso"


def test_unpack_gog_drops_links_out_of_the_installer(m, tmp_path, monkeypatch, log):
    s = m.sources
    work = tmp_path / "w"
    monkeypatch.setattr(s.shutil, "which", which("innoextract"))
    monkeypatch.setattr(s.subprocess, "run", Run(
        effect=lambda cmd, kw: unpacked_with_links(work, tmp_path / "home")))
    s.unpack(s.Source("gog", ["/x/setup_game.exe"]), str(work), log=log)
    check_links_dropped(work, tmp_path / "home", log)


def test_cd_files_of_a_cue_stay_in_its_folder(m, tmp_path):
    d = tmp_path / "cd"
    touch(d / "track01.bin")
    touch(d / "Track 02.wav")
    touch(tmp_path / "outside.bin")
    cue = d / "game.cue"
    cue.write_text('FILE "track01.bin" BINARY\n  TRACK 01 MODE1/2352\n'
                   'FILE "Track 02.wav" WAVE\nfile track01.bin binary\n', encoding="latin-1")
    assert m.sources.cd_files(str(cue)) == [str(cue), str(d / "track01.bin"),
                                            str(d / "Track 02.wav"), str(d / "track01.bin")]
    # A path in the .cue only names a file next to it, never one elsewhere
    cue.write_text('FILE "../outside.bin" BINARY\n')
    with pytest.raises(m.sources.SourceError, match="names a missing file"):
        m.sources.cd_files(str(cue))
    assert m.sources.cd_files("/x/game.iso") == ["/x/game.iso"]


# ----- fat -----

def test_fat12_files_folders_and_label(m, tmp_path):
    big = bytes(range(256)) * 9  # 2304 bytes, five clusters
    img = fat_image(tmp_path / "d.img", {
        "INSTALL.EXE": b"MZ" + b"\1" * 600, "EMPTY.TXT": b"", "NOEXT": b"n",
        "SOUND": {"SB.DRV": big, "DEEP": {"X.DAT": b"x"}},
        "!deleted": dir_entry(b"\xe5ONE    TXT", 0x20, 0, 1),
        "!lfn": dir_entry(b"Along name\0", 0x0F, 0, 0),
        "!e5": dir_entry(b"\x05BC     TXT", 0x20, 0, 0),
    }, label="GAME DISK 1")
    image = m.fat.read_image(img)
    assert image.label == "GAME DISK 1"
    assert image.files["INSTALL.EXE"] == b"MZ" + b"\1" * 600
    assert image.files["EMPTY.TXT"] == b"" and image.files["NOEXT"] == b"n"
    assert image.files["SOUND/SB.DRV"] == big
    assert image.files["SOUND/DEEP/X.DAT"] == b"x"
    # 0x05 stands for a name starting with 0xE5 (σ in code page 437)
    assert "ΣBC.TXT" in image.files
    assert image.dirs == ["SOUND", "SOUND/DEEP"]
    assert set(image.files) == {"INSTALL.EXE", "EMPTY.TXT", "NOEXT", "SOUND/SB.DRV",
                                "SOUND/DEEP/X.DAT", "ΣBC.TXT"}


def test_fat_full_folder_and_a_label_below_the_root(m, tmp_path):
    # 14 files and "." and "..": the folder's cluster is full, no end entry follows
    sub = {f"F{i:02}.TXT": bytes([i]) for i in range(13)}
    sub["!label"] = dir_entry(b"NOT A LABEL", 0x08, 0, 0)
    img = fat_image(tmp_path / "d.img", {"SUB": sub})
    image = m.fat.read_image(img)
    assert sorted(image.files) == [f"SUB/F{i:02}.TXT" for i in range(13)]
    assert image.label == ""


def test_fat16_and_a_32_bit_sector_count(m, tmp_path):
    img = fat_image(tmp_path / "hd.img", {"GAME.EXE": b"g" * 1500}, fat16=True, total32=True)
    assert m.fat.read_image(img).files == {"GAME.EXE": b"g" * 1500}


def test_fat_listing_ends_at_an_empty_entry(m, tmp_path):
    img = fat_image(tmp_path / "d.img", {"A.TXT": b"a", "!end": b"\0" * 32, "B.TXT": b"b"})
    assert set(m.fat.read_image(img).files) == {"A.TXT"}


def test_fat_errors(m, tmp_path):
    fat = m.fat
    small = tmp_path / "small.img"
    small.write_bytes(b"\0" * 100)
    with pytest.raises(fat.FatError, match="too small"):
        fat.read_image(str(small))
    blank = tmp_path / "blank.img"
    blank.write_bytes(b"\0" * 2048)
    with pytest.raises(fat.FatError, match="no FAT file system"):
        fat.read_image(str(blank))
    # A chain that runs into itself, and one that points past the disk
    loop = fat_image(tmp_path / "loop.img", {"A.DAT": b"a" * 1200}, patch={4: 2})
    with pytest.raises(fat.FatError, match="broken cluster chain"):
        fat.read_image(loop)
    past = fat_image(tmp_path / "past.img", {"A.DAT": b"a" * 600}, patch={3: 0xF00})
    with pytest.raises(fat.FatError, match="broken cluster chain"):
        fat.read_image(past)


def test_fat_folder_that_contains_itself(m, tmp_path):
    # A folder entry pointing back at the folder's own cluster: no endless recursion
    img = fat_image(tmp_path / "self.img", {"LOOP": {"!self": dir_entry(name11("LOOP"), 0x10, 2, 0)}})
    with pytest.raises(m.fat.FatError, match="nested too deep"):
        m.fat.read_image(img)


# ----- floppies: one drive A: for all disks -----

BIG = 64 * 1024


def floppy_disks(tmp_path, n=2):
    """n disks: DISK.ID differs, README.TXT is the same, each has a large file of its own."""
    images = []
    for i in range(1, n + 1):
        images.append(fat_image(tmp_path / f"disk{i}.img", {
            "DISK.ID": f"disk {i}".encode(), "README.TXT": b"same",
            f"DATA{i}.PAK": bytes([i]) * BIG, f"SMALL{i}.TXT": b"s",
            **({"INSTALL.EXE": b"MZ", "SUB": {"S.TXT": b"x"}} if i == 1 else {})},
            label=f"DISK{i}"))
    return images


def test_floppy_set_build_and_insert(m, tmp_path, log):
    disks = m.floppies.FloppySet(floppy_disks(tmp_path, 3), str(tmp_path / "a"), log=log)
    assert disks.label == "DISK1"
    assert set(disks.varying) == {"DISK.ID"}
    assert disks.owned == [{"DATA1.PAK"}, {"DATA2.PAK"}, {"DATA3.PAK"}]
    disks.build()
    a = tmp_path / "a"
    assert (a / "DISK.ID").read_bytes() == b"disk 1"
    assert (a / "DATA3.PAK").read_bytes() == b"\3" * BIG
    assert (a / "SUB/S.TXT").read_bytes() == b"x"
    assert log[-1] == "3 disks, 10 files, 1 differ between disks: DISK.ID"
    inode = (a / "DISK.ID").stat().st_ino
    disks.insert(2)
    assert (a / "DISK.ID").read_bytes() == b"disk 3"
    # In place: DOSBox keeps the directory cached
    assert (a / "DISK.ID").stat().st_ino == inode
    assert disks.current == 2 and log[-1] == "disk 3 of 3 inserted"
    disks.insert(3)
    disks.insert(-1)
    assert disks.current == 2


def test_insert_keeps_a_file_the_disk_does_not_have(m, tmp_path):
    images = [fat_image(tmp_path / "d1.img", {"DISK.ID": b"1"}),
              fat_image(tmp_path / "d2.img", {"DISK.ID": b"2"}),
              fat_image(tmp_path / "d3.img", {"OTHER": b"3"})]
    disks = m.floppies.FloppySet(images, str(tmp_path / "a"), log=lambda _t: None)
    disks.build()
    disks.insert(1)
    disks.insert(2)
    # Disk 3 has no DISK.ID: the one in the drive stays as it was
    assert (tmp_path / "a/DISK.ID").read_bytes() == b"2" and disks.current == 2


def test_floppy_set_moves_on_when_the_disk_was_read(m, tmp_path, log):
    disks = m.floppies.FloppySet(floppy_disks(tmp_path), str(tmp_path / "a"), log=log)
    disks.build()
    disks._file_read("DATA2.PAK")   # another disk's file: nothing
    disks._file_read("SMALL1.TXT")  # too small to count
    assert disks.current == 0
    disks._file_read("DATA1.PAK")
    assert disks.current == 1 and "disk 1 read" in log
    # The last disk stays in the drive
    disks._file_read("DATA2.PAK")
    assert disks.current == 1


@pytest.mark.parametrize("name", ["../X.TXT", "A\\..\\X", "A\0B"])
def test_floppy_image_cannot_write_outside_its_drive(m, tmp_path, name):
    # A crafted image: DOS never puts "/", "\\" or NUL into a name, the reader refuses it
    img = fat_image(tmp_path / "evil.img", {name: b"pwned", "INSTALL.EXE": b"MZ"})
    with pytest.raises(m.fat.FatError, match="not a DOS file name"):
        m.fat.read_image(img)
    with pytest.raises(m.fat.FatError):
        m.floppies.FloppySet([img], str(tmp_path / "work/a")).build()
    assert not (tmp_path / "work/X.TXT").exists() and not (tmp_path / "work/a").exists()


@pytest.mark.parametrize("path", ["../X.TXT", "SUB/../../X.TXT", "/X.TXT", "SUB//X", ".",
                                  "A\\X", "A\0B"])
def test_floppy_set_refuses_a_path_out_of_its_drive(m, tmp_path, monkeypatch, path):
    # Whatever a reader hands over: the drive folder is the only place written to
    image = m.fat.FatImage("x.img", files={path: b"pwned", "OK.TXT": b"ok"})
    monkeypatch.setattr(m.floppies, "read_image", lambda _p: image)
    disks = m.floppies.FloppySet(["x.img"], str(tmp_path / "work/a"), log=lambda _t: None)
    with pytest.raises(m.fat.FatError, match="not a file of the drive"):
        disks.build()
    assert not (tmp_path / "work/X.TXT").exists() and not Path("/X.TXT").exists()


def test_floppy_set_refuses_a_folder_out_of_its_drive(m, tmp_path, monkeypatch):
    image = m.fat.FatImage("x.img", files={"OK.TXT": b"ok"}, dirs=["../OUT"])
    monkeypatch.setattr(m.floppies, "read_image", lambda _p: image)
    with pytest.raises(m.fat.FatError, match="not a file of the drive"):
        m.floppies.FloppySet(["x.img"], str(tmp_path / "work/a")).build()
    assert not (tmp_path / "work/OUT").exists()


def test_floppy_set_does_not_follow_a_link_out_of_its_drive(m, tmp_path, monkeypatch):
    (tmp_path / "work/a").mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "work/a/SUB").symlink_to(tmp_path / "elsewhere")
    image = m.fat.FatImage("x.img", files={"SUB/X.TXT": b"pwned"}, dirs=[])
    monkeypatch.setattr(m.floppies, "read_image", lambda _p: image)
    with pytest.raises(m.fat.FatError, match="not a file of the drive"):
        m.floppies.FloppySet(["x.img"], str(tmp_path / "work/a")).build()
    assert list((tmp_path / "elsewhere").iterdir()) == []


class FakeProc:
    """/proc/<pid>/fd and fdinfo below tmp_path; floppies' os and open see it at /proc."""

    def __init__(self, m, tmp_path, monkeypatch):
        self.root = tmp_path / "proc"
        real_os = m.floppies.os

        def mapped(path):
            path = os.fspath(path)
            return str(self.root) + path[len("/proc"):] if path.startswith("/proc/") else path

        fake_os = types.SimpleNamespace(**{k: getattr(real_os, k) for k in dir(real_os)
                                           if not k.startswith("__")})
        fake_os.listdir = lambda p: real_os.listdir(mapped(p))
        fake_os.readlink = lambda p: real_os.readlink(mapped(p))
        monkeypatch.setattr(m.floppies, "os", fake_os)
        monkeypatch.setattr(m.floppies, "open", lambda p, *a, **kw: open(mapped(p), *a, **kw),
                            raising=False)

    def fd(self, pid, fd, target, pos):
        (self.root / str(pid) / "fd").mkdir(parents=True, exist_ok=True)
        (self.root / str(pid) / "fdinfo").mkdir(parents=True, exist_ok=True)
        link = self.root / str(pid) / "fd" / str(fd)
        if not link.is_symlink():
            link.symlink_to(target)
        (self.root / str(pid) / "fdinfo" / str(fd)).write_text(f"pos:\t{pos}\nflags:\t0100000\n")


def test_sample_notes_the_furthest_position(m, tmp_path, monkeypatch):
    proc = FakeProc(m, tmp_path, monkeypatch)
    disks = m.floppies.FloppySet(floppy_disks(tmp_path), str(tmp_path / "a"))
    disks.build()
    proc.fd(7, 3, tmp_path / "a/DATA1.PAK", 40000)
    proc.fd(7, 4, tmp_path / "disk1.img", 99)        # not on the drive
    proc.fd(7, 5, tmp_path / "a/SUB/S.TXT", 1)
    (proc.root / "7/fdinfo/5").write_text("garbage\n")  # unreadable position
    proc.fd(7, 6, tmp_path / "a/DISK.ID", 5)
    (proc.root / "7/fdinfo/6").unlink()                 # closed meanwhile
    disks._sample(lambda: [7, 8])                       # 8 is gone
    assert disks._pos == {"DATA1.PAK": 40000}
    proc.fd(7, 3, tmp_path / "a/DATA1.PAK", 100)        # back to the start: the furthest stays
    disks._sample(lambda: [7])
    assert disks._pos == {"DATA1.PAK": 40000}


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def run_watch(disks):
    stop = threading.Event()
    errors = []

    def target():
        try:
            disks.watch(stop, lambda: [7])
        except Exception as e:  # noqa: BLE001 - reported by the test
            errors.append(e)
    t = threading.Thread(target=target, daemon=True)
    t.start()
    time.sleep(0.1)  # the watches are in place
    return stop, t, errors


def read_through(path):
    with open(path, "rb") as f:
        f.read()


def test_watch_inserts_the_next_disk_once_its_file_was_read(m, tmp_path, monkeypatch, log):
    proc = FakeProc(m, tmp_path, monkeypatch)
    disks = m.floppies.FloppySet(floppy_disks(tmp_path), str(tmp_path / "a"), log=log)
    disks.build()
    stop, t, errors = run_watch(disks)
    try:
        # DOSBox listing the drive: an event without a file name
        os.listdir(tmp_path / "a")
        # A peek at the header (DOTT's installer): the disk stays in
        proc.fd(7, 3, tmp_path / "a/DATA1.PAK", 1000)
        read_through(tmp_path / "a/DATA1.PAK")
        time.sleep(0.2)
        assert disks.current == 0
        # The files that differ between the disks are only "checked"
        read_through(tmp_path / "a/DISK.ID")
        assert wait_for(lambda: log.has("installer checked DISK.ID"))
        # Read through: disk 2 goes in
        proc.fd(7, 3, tmp_path / "a/DATA1.PAK", BIG)
        read_through(tmp_path / "a/DATA1.PAK")
        assert wait_for(lambda: disks.current == 1)
        assert (tmp_path / "a/DISK.ID").read_bytes() == b"disk 2"
    finally:
        stop.set()
        t.join(timeout=2)
    assert not t.is_alive() and errors == []


def test_watch_fails_without_inotify(m, tmp_path, monkeypatch):
    disks = m.floppies.FloppySet(floppy_disks(tmp_path), str(tmp_path / "a"))
    disks.build()
    libc = types.SimpleNamespace(inotify_init1=lambda flags: -1)
    monkeypatch.setattr(m.floppies.ctypes, "CDLL", lambda *a, **kw: libc)
    with pytest.raises(OSError, match="inotify_init1"):
        disks.watch(threading.Event(), lambda: [])
    closed = []
    libc = types.SimpleNamespace(inotify_init1=lambda flags: 99,
                                 inotify_add_watch=lambda fd, path, mask: -1)
    monkeypatch.setattr(m.floppies.ctypes, "CDLL", lambda *a, **kw: libc)
    monkeypatch.setattr(m.floppies.os, "close", closed.append)
    with pytest.raises(OSError, match="inotify_add_watch"):
        disks.watch(threading.Event(), lambda: [])


# ----- keyboard: Stevia for DOSBox -----

class FakeSettings:
    store = {}

    def __init__(self, schema, path):
        self.key = (schema, path)

    def get_value(self, name):
        from gi.repository import GLib
        return GLib.Variant("(ss)", self.store.get((self.key, name), ("", "")))

    def set_value(self, name, value):
        self.store[(self.key, name)] = value.unpack()


class FakeBus:
    def __init__(self, visible=True, fail=False):
        self.calls = []
        self.visible = visible
        self.fail = fail

    def call_sync(self, name, path, iface, method, args, reply, flags, timeout, cancel):
        from gi.repository import GLib
        self.calls.append((name, path, iface, method, args.unpack()))
        if self.fail:
            raise GLib.Error("no such name")
        if method == "Get":
            return GLib.Variant("(v)", (GLib.Variant("b", self.visible),))
        return GLib.Variant("()", ())


@pytest.fixture
def fake_gio(m, monkeypatch):
    from gi.repository import Gio
    state = types.SimpleNamespace(schema=True, synced=0, bus=FakeBus())
    FakeSettings.store = {}

    class Source:
        def lookup(self, schema, recursive):
            return object() if state.schema and schema == m.keyboard.SCHEMA else None

    def sync():
        state.synced += 1

    fake = types.SimpleNamespace(
        SettingsSchemaSource=types.SimpleNamespace(get_default=lambda: Source()),
        Settings=types.SimpleNamespace(new_with_path=FakeSettings, sync=sync),
        bus_get_sync=lambda bus_type, cancel: state.bus,
        BusType=Gio.BusType, DBusCallFlags=Gio.DBusCallFlags)
    monkeypatch.setattr(m.keyboard, "Gio", fake)
    return state


def test_terminal_layout_for_dosbox(m, fake_gio, log):
    key = ((m.keyboard.SCHEMA, "/mobi/phosh/osk/application/dosbox/"), "override-layout")
    m.keyboard.use_terminal_layout(log)
    assert FakeSettings.store[key] == ("terminal", "") and fake_gio.synced == 1
    assert log == ["keyboard: Terminal layout for DOSBox"]
    m.keyboard.use_terminal_layout(log)   # set already: not written again
    assert fake_gio.synced == 1 and len(log) == 1
    fake_gio.schema = False               # Stevia not installed
    FakeSettings.store.clear()
    m.keyboard.use_terminal_layout(log)
    assert FakeSettings.store == {}


def test_osk_visibility_over_dbus(m, fake_gio):
    k = m.keyboard
    assert k.set_visible(True) is True
    assert fake_gio.bus.calls[-1] == ("sm.puri.OSK0", "/sm/puri/OSK0", "sm.puri.OSK0",
                                      "SetVisible", (True,))
    assert k.is_visible() is True
    assert fake_gio.bus.calls[-1][2:] == ("org.freedesktop.DBus.Properties", "Get",
                                          ("sm.puri.OSK0", "Visible"))
    fake_gio.bus = FakeBus(visible=False)
    assert k.is_visible() is False
    fake_gio.bus = FakeBus(fail=True)
    assert k.set_visible(False) is False and k.is_visible() is False


def test_for_dosbox_unfolds_and_folds_the_keyboard(m, fake_gio, monkeypatch, log):
    monkeypatch.setattr(m.keyboard, "SHOW_AFTER", 0.01)
    monkeypatch.setattr(m.keyboard, "RETRY_AFTER", 0.01)
    fake_gio.bus = FakeBus(visible=False)
    with m.keyboard.ForDosbox(log) as kb:
        kb._thread.join(timeout=2)
    methods = [(c[3], c[4]) for c in fake_gio.bus.calls]
    # Shown, not visible after the retry time: shown again; folded at the end
    assert methods == [("SetVisible", (True,)), ("Get", ("sm.puri.OSK0", "Visible")),
                       ("SetVisible", (True,)), ("SetVisible", (False,))]


def test_for_dosbox_keyboard_that_stayed_up(m, fake_gio, monkeypatch, log):
    monkeypatch.setattr(m.keyboard, "SHOW_AFTER", 0.01)
    monkeypatch.setattr(m.keyboard, "RETRY_AFTER", 0.01)
    with m.keyboard.ForDosbox(log) as kb:
        kb._thread.join(timeout=2)
    assert [c[3] for c in fake_gio.bus.calls] == ["SetVisible", "Get", "SetVisible"]
    assert fake_gio.bus.calls[-1][4] == (False,)


def test_for_dosbox_without_a_keyboard(m, fake_gio, monkeypatch, log):
    monkeypatch.setattr(m.keyboard, "SHOW_AFTER", 0.01)
    fake_gio.bus = FakeBus(fail=True)

    def broken(_log):
        from gi.repository import GLib
        raise GLib.Error("dconf is gone")
    monkeypatch.setattr(m.keyboard, "use_terminal_layout", broken)
    with m.keyboard.ForDosbox(log) as kb:
        kb._thread.join(timeout=2)
    assert log == ["keyboard: dconf is gone", "keyboard: no on-screen keyboard on the session bus"]


def test_for_dosbox_ending_before_the_keyboard_shows(m, fake_gio, monkeypatch, log):
    monkeypatch.setattr(m.keyboard, "SHOW_AFTER", 30)
    with m.keyboard.ForDosbox(log):
        pass
    # Only folded: DOSBox was gone before the keyboard was due
    assert [c[3:] for c in fake_gio.bus.calls] == [("SetVisible", (False,))]


# ----- dosbox -----

def test_quote_and_config(m, tmp_path):
    d = m.dosbox
    assert d._quote("/home/u/My Game") == '"/home/u/My Game"'
    with pytest.raises(ValueError):
        d._quote('/home/u/say "hi"')
    conf = tmp_path / "c.conf"
    d.write_config(conf, ["mount c x", "c:"], {"cpu": {"cycles": "max"}, "evil": {"x": "y"}})
    text = conf.read_text()
    assert text.startswith(d.PHONE_CONFIG)
    assert "usescancodes=false" in text and "fullresolution=1440x1080" in text
    assert "\n[cpu]\ncycles=max\n" in text and "evil" not in text
    assert text.endswith("\n[autoexec]\nmount c x\nc:\n")
    d.write_config(conf, ["exit"])
    assert conf.read_text() == d.PHONE_CONFIG + "\n[autoexec]\nexit\n"


@pytest.mark.parametrize("files, found", [
    ({"SETUP.BAT": 1, "INSTALL.EXE": 1}, "INSTALL.EXE"),
    ({"INSTALL.EXE": 1, "INSTALL.BAT": 1}, "INSTALL.BAT"),
    ({"INSTALAR.COM": 1}, "INSTALAR.COM"),
    ({"GAME.EXE": 1, "README.TXT": 1, "SUB/OTHER.EXE": 1}, "GAME.EXE"),
    ({"GAME.EXE": 1, "EDITOR.EXE": 1}, None),
    ({"README.TXT": 1}, None),
])
def test_find_installer(m, files, found):
    assert m.dosbox.find_installer(files) == found


class Popen:
    """subprocess.Popen for DOSBox: notes the command and the configuration it was given."""

    instances = []

    def __init__(self, cmd, env=None, **_kw):
        self.cmd = cmd
        self.env = env
        self.pid = 4242
        self.config = Path(cmd[-1]).read_text()
        self.code = 0
        Popen.instances.append(self)

    def wait(self):
        return self.code


@pytest.fixture
def dosbox_run(m, monkeypatch):
    """DOSBox, the keyboard and the drive watcher as fakes."""
    Popen.instances = []
    state = types.SimpleNamespace(keyboard=[], watched=[], runs=[], code=0)

    class Keyboard:
        def __init__(self, log):
            pass

        def __enter__(self):
            state.keyboard.append("enter")

        def __exit__(self, *exc):
            state.keyboard.append("exit")

    def watch(disks, stop, pids):
        state.watched.append(pids())
        stop.wait(5)

    def run(cmd, env=None, **_kw):
        state.runs.append((cmd, Path(cmd[-1]).read_text(), env))
        return types.SimpleNamespace(returncode=state.code)

    monkeypatch.setattr(m.dosbox.keyboard, "ForDosbox", Keyboard)
    monkeypatch.setattr(m.floppies.FloppySet, "watch", watch)
    monkeypatch.setattr(m.dosbox.subprocess, "Popen", Popen)
    monkeypatch.setattr(m.dosbox.subprocess, "run", run)
    return state


def autoexec(config):
    return config.split("[autoexec]\n", 1)[1].splitlines()


def test_install_from_floppies(m, tmp_path, dosbox_run, log):
    images = floppy_disks(tmp_path)
    target = tmp_path / "game/c"
    work = tmp_path / "work"
    seen = []
    code, disks = m.dosbox.install_from_floppies(images, str(target), log=log, work=str(work),
                                                 started=seen.append)
    assert code == 0 and seen == [disks] and disks.current == 0
    p = Popen.instances[0]
    assert p.cmd[:3] == ["dosbox", "-userconf", "-conf"]
    assert p.env["SDL_APP_ID"] == "dosbox" and p.env["SDL_VIDEO_WAYLAND_WMCLASS"] == "dosbox"
    assert autoexec(p.config) == [f'mount c "{target}"',
                                  f'mount a "{work / "a"}" -t floppy -label DISK1',
                                  "a:", "INSTALL.EXE", "exit"]
    assert "[cpu]" not in p.config
    assert dosbox_run.keyboard == ["enter", "exit"] and dosbox_run.watched == [[4242]]
    assert "installer: INSTALL.EXE" in log and log[-1] == "DOSBox ended with 0"
    assert target.is_dir() and not work.exists()


def test_install_from_floppies_label_and_program(m, tmp_path, dosbox_run, log):
    img = fat_image(tmp_path / "d.img", {"GO.BAT": b"x", "RUN.EXE": b"y"},
                    label="MY  GAME  1")
    code, _ = m.dosbox.install_from_floppies([img], str(tmp_path / "c"), "GO.BAT", log=log)
    lines = autoexec(Popen.instances[0].config)
    # At most 11 characters, no spaces
    assert lines[1].endswith("-t floppy -label MY__GAME__1") and lines[3] == "GO.BAT"
    # The temporary work folder went into tempfile's directory and is gone again
    assert list(Path(tempfile.gettempdir()).iterdir()) == []


def test_install_from_floppies_without_an_installer(m, tmp_path, dosbox_run):
    img = fat_image(tmp_path / "d.img", {"A.EXE": b"1", "B.EXE": b"2"})
    work = tmp_path / "work"
    with pytest.raises(FileNotFoundError, match="no installer on disk 1"):
        m.dosbox.install_from_floppies([img], str(tmp_path / "c"), work=str(work))
    assert not work.exists() and Popen.instances == []


def test_install_from_floppies_with_a_plan(m, tmp_path, dosbox_run, log):
    img = fat_image(tmp_path / "d.img", {"A.EXE": b"1", "B.EXE": b"2", "GO.BAT": b"a\r\nb\r\n"})
    plan = m.presets.InstallPlan(commands=["C:", "cd \\GAME", "GAME.EXE"],
                                 files={"GAME/GAME.EXE": b"MZ"}, mkdirs=["SAVE"],
                                 drop={"GO.BAT": ["b"]})
    started = []
    m.dosbox.install_from_floppies([img], str(tmp_path / "c"), log=log, plan=plan,
                                   started=started.append, work=str(tmp_path / "w"))
    p = Popen.instances[0]
    assert autoexec(p.config)[2:] == ["C:", "cd \\GAME", "GAME.EXE", "exit"]
    assert "[cpu]\ncycles=max" in p.config
    # Automatic: no keyboard, nothing to insert by hand
    assert dosbox_run.keyboard == [] and started == []
    assert (tmp_path / "c/GAME/GAME.EXE").read_bytes() == b"MZ"
    assert (tmp_path / "c/SAVE").is_dir()
    assert "presets: GO.BAT without b" in log
    # A plan's program replaces the one found on the disk
    Popen.instances.clear()
    plan = m.presets.InstallPlan(program="B.EXE")
    m.dosbox.install_from_floppies([img], str(tmp_path / "c"), "A.EXE", plan=plan)
    assert autoexec(Popen.instances[0].config)[2:] == ["a:", "B.EXE", "exit"]


def test_install_from_floppies_cleans_up_when_dosbox_fails(m, tmp_path, dosbox_run, monkeypatch):
    img = fat_image(tmp_path / "d.img", {"INSTALL.EXE": b"1"})

    class Broken(Popen):
        def wait(self):
            raise KeyboardInterrupt
    monkeypatch.setattr(m.dosbox.subprocess, "Popen", Broken)
    with pytest.raises(KeyboardInterrupt):
        m.dosbox.install_from_floppies([img], str(tmp_path / "c"), work=str(tmp_path / "w"))
    assert not (tmp_path / "w").exists() and dosbox_run.keyboard == ["enter", "exit"]


def test_install_from_drive(m, tmp_path, dosbox_run, log):
    dosbox_run.code = 3
    code = m.dosbox.install_from_drive(str(tmp_path / "c"), "INSTALL.EXE", cd="/x/game.iso",
                                       log=log)
    assert code == 3 and log == ["DOSBox ended with 3"]
    cmd, config, env = dosbox_run.runs[0]
    assert autoexec(config) == [f'mount c "{tmp_path / "c"}"', 'imgmount d "/x/game.iso" -t iso',
                                "d:", "INSTALL.EXE", "exit"]
    assert dosbox_run.keyboard == ["enter", "exit"] and env["SDL_APP_ID"] == "dosbox"
    plan = m.presets.InstallPlan(commands=["d:", "COPY X C:\\"], files={"A/B": b"1"})
    m.dosbox.install_from_drive(str(tmp_path / "c"), "SETUP.EXE", folder="/x/folder", plan=plan)
    _cmd, config, _env = dosbox_run.runs[1]
    assert autoexec(config)[1:] == ['mount d "/x/folder"', "d:", "COPY X C:\\", "exit"]
    assert "cycles=max" in config and dosbox_run.keyboard == ["enter", "exit"]
    assert (tmp_path / "c/A/B").read_bytes() == b"1"
    m.dosbox.install_from_drive(str(tmp_path / "c"), "SETUP.EXE", folder="/x/f",
                                plan=m.presets.InstallPlan(program="GO.BAT"))
    assert autoexec(dosbox_run.runs[2][1])[2:] == ["d:", "GO.BAT", "exit"]
    assert list(Path(tempfile.gettempdir()).iterdir()) == []


# ----- presets: the signed list -----

@pytest.fixture
def keyring(m, home, gpg_keys):
    keys, trusted, attacker = gpg_keys
    home.keys.mkdir()
    Path(m.presets.KEYRING).write_bytes(keys.export(trusted, attacker))
    Path(m.presets.TRUSTED).write_text(f"# trusted\n{trusted}:4:\n")
    Path(m.presets.REVOKED).write_text("")
    return types.SimpleNamespace(keys=keys, trusted=trusted, attacker=attacker)


def preset_list(updated, games=()):
    return json.dumps({"schema": 1, "updated": updated, "games": list(games)}).encode()


def test_verify(m, keyring):
    p = m.presets
    data = preset_list(5)
    good = keyring.keys.sign(keyring.trusted, data)
    assert p.verify(data, good)
    assert not p.verify(data + b" ", good)
    assert not p.verify(data, b"garbage")
    assert not p.verify(data, keyring.keys.sign(keyring.attacker, data))
    Path(p.REVOKED).write_text(f"{keyring.trusted}\n")
    assert not p.verify(data, good)


def test_verify_without_trust_keyring_or_gpgv(m, keyring, monkeypatch):
    p = m.presets
    data = preset_list(5)
    good = keyring.keys.sign(keyring.trusted, data)
    Path(p.KEYRING).write_text("not armored\n")
    assert not p.verify(data, good)
    Path(p.KEYRING).unlink()
    assert not p.verify(data, good)
    Path(p.KEYRING).write_bytes(keyring.keys.export(keyring.trusted))

    def no_gpgv(*_a, **_kw):
        raise FileNotFoundError("gpgv")
    monkeypatch.setattr(p.subprocess, "run", no_gpgv)
    assert not p.verify(data, good)
    Path(p.TRUSTED).unlink()
    assert not p.verify(data, good)


def test_dearmor(m):
    body = base64.b64encode(b"\x99keydata").decode()
    text = (f"-----BEGIN PGP PUBLIC KEY BLOCK-----\nComment: x\n\n{body}\n=abcd\n"
            "-----END PGP PUBLIC KEY BLOCK-----\n")
    assert m.presets._dearmor(text) == b"\x99keydata"
    bare = f"-----BEGIN PGP PUBLIC KEY BLOCK-----\n{body}\n-----END PGP PUBLIC KEY BLOCK-----"
    assert m.presets._dearmor(bare) == b"\x99keydata"
    with pytest.raises(StopIteration):
        m.presets._dearmor("no key here")


@pytest.mark.parametrize("data", [b"{", b"[]", b'{"schema": 2, "games": []}',
                                  b'{"schema": 1, "games": {}}'])
def test_parse_refuses_other_lists(m, data):
    with pytest.raises(m.presets.PresetError):
        m.presets._parse(data)


class Server:
    """urllib.request.urlopen for the list and its signature."""

    def __init__(self, files=None, error=None):
        self.files = files or {}
        self.error = error
        self.reads = []

    def __call__(self, url, timeout=None):
        if self.error:
            raise self.error
        server = self

        class Answer(io.BytesIO):
            def read(self, n=-1):
                server.reads.append((url, n, timeout))
                return super().read(n)
        return Answer(self.files[url])


def serve(m, monkeypatch, keyring, data, signer=None):
    sig = keyring.keys.sign(signer or keyring.trusted, data)
    server = Server({m.presets.URL: data, m.presets.URL + ".sig": sig})
    monkeypatch.setattr(m.presets.urllib.request, "urlopen", server)
    return server


def test_load_fetches_a_newer_signed_list_and_keeps_it(m, home, keyring, monkeypatch, log):
    p = m.presets
    data = preset_list(2_000_000_000, [{"id": "new"}])
    server = serve(m, monkeypatch, keyring, data)
    best = p.load(log=log)
    assert best["games"] == [{"id": "new"}]
    assert (home.cache / "presets.json").read_bytes() == data
    assert p.verify(data, (home.cache / "presets.json.sig").read_bytes())
    assert log[-1] == "presets: 1 games, list of 2000000000"
    # Limited reads, the short timeout
    assert [(n, t) for _u, n, t in server.reads] == [(4 << 20, 6), (64 << 10, 6)]
    # Without a network the kept copy is used
    monkeypatch.setattr(p.urllib.request, "urlopen", Server(error=urllib.error.URLError("down")))
    log.clear()
    assert p.load(log=log)["games"] == [{"id": "new"}]
    assert log[0].startswith("presets: not fetched")
    assert p.load(fetch=False, log=log)["games"] == [{"id": "new"}]


def test_load_refuses_bad_or_older_lists(m, home, keyring, monkeypatch, log):
    p = m.presets
    bundled = p._parse(p.BUNDLED.read_bytes())
    # Signed by someone else
    serve(m, monkeypatch, keyring, preset_list(2_000_000_000, [{"id": "evil"}]), keyring.attacker)
    assert p.load(log=log) == bundled
    assert "presets: the fetched list fails the signature check, not used" in log
    assert not home.cache.exists()
    # Signed, but not a list of games
    serve(m, monkeypatch, keyring, b'{"schema": 9}')
    assert p.load(log=log) == bundled and log.has("the fetched list is not usable")
    # Newer kept copy than the fetched one: the kept one stays
    newer = preset_list(2_100_000_000, [{"id": "kept"}])
    home.cache.mkdir()
    (home.cache / "presets.json").write_bytes(newer)
    (home.cache / "presets.json.sig").write_bytes(keyring.keys.sign(keyring.trusted, newer))
    serve(m, monkeypatch, keyring, preset_list(2_000_000_000, [{"id": "older"}]))
    assert p.load(log=log)["games"] == [{"id": "kept"}]
    assert "presets: the fetched list is older than the kept one, not used" in log
    assert (home.cache / "presets.json").read_bytes() == newer
    # A list older than the bundled one loses against it
    serve(m, monkeypatch, keyring, preset_list(1, [{"id": "ancient"}]))
    (home.cache / "presets.json").unlink()
    assert p.load(log=log) == bundled


def test_kept_copy_is_checked_again(m, home, keyring, log):
    p = m.presets
    home.cache.mkdir()
    data = preset_list(2_000_000_000, [{"id": "kept"}])
    (home.cache / "presets.json").write_bytes(data + b" ")
    (home.cache / "presets.json.sig").write_bytes(keyring.keys.sign(keyring.trusted, data))
    assert p.load(fetch=False, log=log) == p._parse(p.BUNDLED.read_bytes())
    signed_junk = b"not json"
    (home.cache / "presets.json").write_bytes(signed_junk)
    (home.cache / "presets.json.sig").write_bytes(keyring.keys.sign(keyring.trusted, signed_junk))
    assert p._read_cache() is None


def test_load_without_any_list(m, home, monkeypatch, tmp_path, log):
    monkeypatch.setattr(m.presets, "BUNDLED", tmp_path / "missing.json")
    assert m.presets.load(fetch=False, log=log) == {"schema": 1, "updated": 0, "games": []}
    assert log[0].startswith("presets: bundled list not readable")


def test_bundled_list(m):
    bundled = m.presets._parse(m.presets.BUNDLED.read_bytes())
    for game in bundled["games"]:
        assert game["id"] and game["title"]


# ----- presets: what a preset may ask for -----

@pytest.mark.parametrize("path, parts", [
    ("GAME\\X.EXE", ["GAME", "X.EXE"]), ("\\GAME//SUB\\.\\X", ["GAME", "SUB", "X"]),
    ("  a/b  ", ["a", "b"]),
])
def test_dos_path(m, path, parts):
    assert m.presets._dos_path(path) == parts


@pytest.mark.parametrize("path", ["..\\X", "GAME\\..\\..\\X", "C:\\X", "Z:X", "", "\\", "."])
def test_dos_path_refuses_paths_outside_c(m, path):
    with pytest.raises(m.presets.PresetError):
        m.presets._dos_path(path)


def test_host_file_matches_any_case(m, tmp_path):
    touch(tmp_path / "Game/Sound.Cfg")
    assert m.presets._host_file(tmp_path, "GAME\\SOUND.CFG") == tmp_path / "Game/Sound.Cfg"
    assert m.presets._host_file(tmp_path, "game/new/x.cfg") == tmp_path / "Game/NEW/X.CFG"
    assert m.presets._host_file(tmp_path / "none", "a") == tmp_path / "none/A"


@pytest.mark.parametrize("line", ["cd \\DUKE1", "DUKEZIP1.EXE", "del DUKEZIP1.EXE", "C:",
                                  "copy a:\\*.* c:\\game", "  setup /s  "])
def test_check_command_allows_the_drives_of_the_installer(m, line):
    assert m.presets.check_command(line) == line.strip()


@pytest.mark.parametrize("line", ["mount d /home", "IMGMOUNT d x.iso", "boot a.img",
                                  "config -set cpu cycles=1", "z:", "copy x z:\\",
                                  "copy a:x+z:y c:", "keyb gr", "mixer master 0",
                                  "serial 1 nullmodem", "dir\nmount d /"])
def test_check_command_refuses_others(m, line):
    with pytest.raises(m.presets.PresetError):
        m.presets.check_command(line)


@pytest.mark.parametrize("line", ["@mount d /", "mount.com d /", "lh mount d /",
                                  "loadhigh imgmount d /x.iso -t iso", "Z:\\MOUNT.COM d /",
                                  "\\MOUNT d /", "..\\mount.exe d /", "MoUnT d /",
                                  "call mount.bat d /", "lh @ mount d /", "mount. d /",
                                  "mount/u d", "mount,d /", '"mount" d /', "dir | mount d /",
                                  "> nul mount d /", "dir >nul|imgmount d x.iso",
                                  "@\tboot a.img", "copy x @z:"])
def test_check_command_refuses_mount_in_disguise(m, line):
    with pytest.raises(m.presets.PresetError, match="command not allowed"):
        m.presets.check_command(line)


@pytest.mark.parametrize("line", ["copy a:\\config.sys c:\\", "type mount.txt",
                                  "lh game.exe", "dir > list.txt", "@echo off",
                                  "a:\\install.exe /auto", "copy a:\\mixer.dat c:\\game"])
def test_check_command_allows_these_words_as_arguments(m, line):
    # The refused names count as the program a line runs, not as a file named after them
    assert m.presets.check_command(line) == line


def test_command_names(m):
    names = m.presets._command_names
    assert names("@lh Z:\\MOUNT.COM d /") == ["MOUNT"]
    assert names("dir /w | more") == ["DIR", "MORE"]
    assert names("lh") == [] and names("") == [] and names("@") == []


def test_dosbox_settings_only_known_keys_and_plain_values(m):
    p = m.presets
    settings = {"cpu": {"cycles": 20000, "core": "dynamic", "evil": "x"},
                "sdl": {"fullscreen": "false", "sensitivity": 250},
                "dos": {"ems": "true\n[autoexec]\nmount d /"},
                "render": {"frameskip": "x" * 41}, "speaker": None, "sblaster": {"sbtype": "sb16"}}
    assert p.dosbox_settings(settings) == {"cpu": {"cycles": "20000", "core": "dynamic"},
                                           "sdl": {"sensitivity": "250"},
                                           "sblaster": {"sbtype": "sb16"}}
    assert p.dosbox_settings(None) == {}
    assert p.dosbox_conf(settings) == ("[cpu]\ncycles=20000\ncore=dynamic\n\n[sdl]\n"
                                       "sensitivity=250\n\n[sblaster]\nsbtype=sb16\n")
    assert p.merge_dosbox({"cpu": {"cycles": "max"}}, None, {"cpu": {"core": "auto"},
                                                             "dos": {"xms": "true"}}) == \
        {"cpu": {"cycles": "max", "core": "auto"}, "dos": {"xms": "true"}}


def test_set_keyvalue(m):
    old = "snd_sfxdevice\t\t0\r\nmouse_sensitivity   5\r\nsnd_musicdevice 1\r\n"
    out = m.presets._set_keyvalue(old, {"snd_sfxdevice": "3", "snd_musicdevice": "3",
                                        "use_mouse": "1"})
    assert out == ("snd_sfxdevice\t\t3\r\nmouse_sensitivity   5\r\nsnd_musicdevice 3\r\n"
                   "use_mouse\t\t1\r\n")
    assert m.presets._set_keyvalue("", {"a": "1"}) == "a\t\t1\r\n"


def test_set_ini(m):
    old = "; settings\n[Sound]\nCard = 0\nPort=200\n\n[Video]\nMode = vga\n"
    out = m.presets._set_ini(old, {"sound": {"Card": "6", "Irq": "7"}, "Mouse": {"On": "1"}})
    assert out.split("\r\n") == ["; settings", "[Sound]", "Card = 6", "Port=200", "Irq = 7", "",
                                 "[Video]", "Mode = vga", "", "[Mouse]", "On = 1", ""]
    assert m.presets._set_ini("", {"A": {"k": "v"}}) == "[A]\r\nk = v\r\n"


def test_write_file(m, tmp_path, log):
    p = m.presets
    p.write_file(tmp_path, "GAME\\README.TXT", {"text": "Ä line\nnext\r\nend"}, log)
    assert (tmp_path / "GAME/README.TXT").read_bytes() == "Ä line\r\nnext\r\nend".encode("cp437")
    p.write_file(tmp_path, "game/logo.bin", {"base64": base64.b64encode(b"\0\1").decode()})
    assert (tmp_path / "GAME/LOGO.BIN").read_bytes() == b"\0\1"
    touch(tmp_path / "Game/Default.cfg", b"use_mouse 0\r\n")
    p.write_file(tmp_path, "GAME\\DEFAULT.CFG", {"format": "keyvalue", "set": {"use_mouse": "1"}})
    assert (tmp_path / "Game/Default.cfg").read_bytes() == b"use_mouse 1\r\n"
    p.write_file(tmp_path, "GAME\\SOUND.INI", {"format": "ini", "set": {"SB": {"Irq": "7"}}})
    assert (tmp_path / "Game/SOUND.INI").read_bytes() == b"[SB]\r\nIrq = 7\r\n"
    assert log == ["presets: wrote GAME\\README.TXT"]
    with pytest.raises(p.PresetError, match="nothing to write"):
        p.write_file(tmp_path, "X", {"format": "xml"})
    with pytest.raises(p.PresetError, match="outside C:"):
        p.write_file(tmp_path / "c", "..\\escape.txt", {"text": "x"})
    assert not (tmp_path / "escape.txt").exists() and not (tmp_path / "ESCAPE.TXT").exists()


# ----- presets: rules for families of games -----

def deice_files(dat, pieces=(b"ab", b"cde"), extra=None):
    files = {"DEICE.EXE": b"MZ", "DOOMS.DAT": dat.encode("cp437"), "README.TXT": b"r",
             "SUB/OTHER.DAT": b"PATH=X\r\nSIZE=1\r\n"}
    for i, piece in enumerate(pieces, 1):
        files[f"DOOMS.{i}"] = piece
    files.update(extra or {})
    return files


def test_deice(m):
    p = m.presets
    plan = p._deice(deice_files("PATH=\\DOOM\r\nSIZE=5\r\nLINE0=***  DOOM v1.9  ***\r\n"))
    assert plan == {"folder": "DOOM", "archive": "DOOMS.EXE", "data": b"abcde", "title": "Doom"}
    assert p._deice(deice_files("PATH=ID\\WOLF3D\r\nSIZE=5\r\nLINE0=WOLFENSTEIN 3D v1.4\r\n"))[
        "title"] == "Wolfenstein 3D"
    assert p._deice(deice_files("PATH=DOOM2\r\nSIZE=5\r\nLINE0=DOOM II: Hell on Earth\r\n"))[
        "title"] == "DOOM II: Hell on Earth"
    assert p._deice(deice_files("PATH=D\r\nSIZE=5\r\nLINE0=DOOM II\r\n"))["title"] == "Doom II"
    assert p._deice(deice_files("PATH=D\r\nSIZE=5\r\n"))["title"] is None


def test_deice_refuses_what_does_not_fit(m):
    p = m.presets
    assert p._deice({"DOOMS.DAT": b"PATH=D\r\nSIZE=5\r\n"}) is None        # no DEICE.EXE
    assert p._deice(deice_files("PATH=D\r\nSIZE=6\r\n")) is None           # pieces too short
    assert p._deice(deice_files("PATH=D\r\nSIZE=5\r\n", pieces=())) is None
    assert p._deice(deice_files("SIZE=5\r\n")) is None                     # no PATH
    with pytest.raises(p.PresetError):                                     # beyond C:
        p._deice(deice_files("PATH=..\\..\\X\r\nSIZE=5\r\n"))


@pytest.mark.parametrize("size", ["five", "", "5.0", "0x5"])
def test_deice_with_a_size_that_is_no_number(m, size):
    # Not the DEICE disk the rule knows: no plan, the disk's own installer runs
    assert m.presets._deice(deice_files(f"PATH=DOOM\r\nSIZE={size}\r\n")) is None
    assert m.presets.install_plan({"games": []}, deice_files(
        f"PATH=DOOM\r\nSIZE={size}\r\n"), log=lambda _t: None) is None


def sfx(data=b"x"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr("GAME.EXE", data * (40 * 1024))
    return buf.getvalue()


def test_sfx_zips(m):
    p = m.presets
    files = {"DUKEZIP1.EXE": sfx(), "ZIP.EXE": sfx(), "SMALL.EXE": b"MZ", "NOTZIP.EXE": b"M" * 40000,
             "README.TXT": b"r", "SUB/DEEPZIP.EXE": sfx()}
    assert p._sfx_zips(files) == [("DUKE1", "DUKEZIP1.EXE", files["DUKEZIP1.EXE"]),
                                  ("ZIP", "ZIP.EXE", files["ZIP.EXE"])]
    assert p._sfx_zips(dict(files, **{"INSTALL.BAT": b"x"})) is None
    assert p._sfx_zips({"README.TXT": b"r"}) is None


def test_idtech1_and_wolf3d_are_found_and_configured(m, tmp_path, log):
    p = m.presets
    root = tmp_path / "c"
    touch(root / "DOOM/doom.wad")
    touch(root / "DOOM/DOOM.EXE")
    touch(root / "DOOM.WAD")          # no program next to it
    touch(root / "A/B/C/D/DOOM2.WAD")
    touch(root / "A/B/C/D/DOOM2.EXE")  # too deep
    assert p._idtech1(str(root)) == [("DOOM", "DOOM.EXE", "DEFAULT.CFG")]
    touch(root / "WOLF3D/VSWAP.WL6")
    touch(root / "WOLF3D/WOLF3D.EXE")
    touch(root / "SPEAR/vswap.sod")
    touch(root / "SPEAR/SPEAR.EXE")
    touch(root / "SPEAR/CONFIG.SOD", b"\0" * 100)     # not the known layout: left alone
    found = p._wolf3d(str(root), log)
    assert sorted(found) == [("SPEAR", "SPEAR.EXE"), ("WOLF3D", "WOLF3D.EXE")]
    cfg = (root / "WOLF3D/CONFIG.WL6").read_bytes()
    assert len(cfg) == p.WOLF3D_SIZE
    assert struct.unpack_from("<4h", cfg, p.WOLF3D_MOUSE) == p.WOLF3D_BUTTONS
    assert cfg.startswith(b"id software-'92\0")
    assert (root / "SPEAR/CONFIG.SOD").read_bytes() == b"\0" * 100
    assert log == ["presets: wrote WOLF3D/CONFIG.WL6"]
    # A configuration the game wrote keeps everything but the mouse buttons
    written = bytearray(cfg)
    written[0:4] = b"ME\0\0"
    struct.pack_into("<4h", written, p.WOLF3D_MOUSE, 0, 1, -1, -1)
    (root / "WOLF3D/CONFIG.WL6").write_bytes(written)
    p._wolf3d(str(root), log)
    again = (root / "WOLF3D/CONFIG.WL6").read_bytes()
    assert again[:4] == b"ME\0\0" and struct.unpack_from("<4h", again, 502) == (3, 1, 0, -1)


def test_find_by_source_or_installed_files(m, tmp_path):
    p = m.presets
    games = {"games": [{"id": "none"},
                       {"id": "v1", "source": {"GAME.EXE": 3}},
                       {"id": "v2", "source": {"game.exe": 4, "sub\\x": None}},
                       {"id": "inst", "installed": ["GAME\\RUN.EXE"]}]}
    assert p.find(games, source_files={"GAME.EXE": b"abc"})["id"] == "v1"
    assert p.find(games, source_files={"GAME.EXE": b"abcd", "SUB/X": b""})["id"] == "v2"
    assert p.find(games, source_files={"GAME.EXE": b"abcd"}) is None
    run = touch(tmp_path / "RUN.EXE")
    assert p.find(games, installed={"GAME/RUN.EXE": run})["id"] == "inst"
    assert p.find(games, installed={}) is None
    assert p.find({}, source_files={}) is None


def test_install_plan_from_a_preset(m, log):
    p = m.presets
    presets = {"games": [{"id": "g", "title": "Game", "source": ["DISK.ID"], "install": {
        "commands": ["C:", "cd \\GAME"], "program": "SETUP.EXE /auto",
        "drop": {"go.bat": ["pause"]}, "mkdirs": ["GAME\\SAVE"]}}]}
    plan = p.install_plan(presets, {"DISK.ID": b"1"}, log=log)
    assert plan.title == "Game" and plan.commands == ["C:", "cd \\GAME"] and plan.automatic
    assert plan.program == "SETUP.EXE /auto" and plan.drop == {"GO.BAT": ["pause"]}
    assert plan.mkdirs == ["GAME/SAVE"] and plan.preset is presets["games"][0]
    assert log == ["presets: installing g as the list says"]
    bad = {"games": [{"id": "b", "source": ["DISK.ID"], "install": {"commands": ["mount d /"]}}]}
    with pytest.raises(p.PresetError):
        p.install_plan(bad, {"DISK.ID": b"1"})
    bad = {"games": [{"id": "b", "source": ["DISK.ID"], "install": {"program": "z:\\x"}}]}
    with pytest.raises(p.PresetError):
        p.install_plan(bad, {"DISK.ID": b"1"})
    only = {"games": [{"id": "o", "source": ["DISK.ID"], "install": {"commands": ["C:"]}}]}
    plan = p.install_plan(only, {"DISK.ID": b"1"})
    assert plan.commands == ["C:"] and plan.program is None and plan.title is None
    # A preset without install steps names the game only
    plain = {"games": [{"id": "p", "title": "Plain", "source": ["DISK.ID"]}]}
    plan = p.install_plan(plain, {"DISK.ID": b"1"})
    assert plan.title == "Plain" and not plan.automatic and plan.program is None
    assert p.install_plan(plain, {"OTHER": b"1"}) is None


def test_install_plan_from_the_rules(m, log):
    p = m.presets
    plan = p.install_plan({"games": []}, deice_files("PATH=ID\\DOOM\r\nSIZE=5\r\nLINE0=DOOM\r\n"),
                          log=log)
    assert plan.title == "Doom" and plan.files == {"ID/DOOM/DOOMS.EXE": b"abcde"}
    assert plan.commands == ["C:", "cd \\ID\\DOOM", "DOOMS.EXE", "del DOOMS.EXE"]
    assert log == ["presets: DEICE floppies, DOOMS.EXE into C:\\ID/DOOM"]
    named = {"games": [{"id": "duke", "title": "Duke Nukem", "source": ["DUKEZIP1.EXE"]}]}
    files = {"DUKEZIP1.EXE": sfx(b"1"), "DUKEZIP2.EXE": sfx(b"2")}
    plan = p.install_plan(named, files, log=log)
    assert plan.title == "Duke Nukem" and plan.preset["id"] == "duke"
    assert plan.files == {"DUKE1/DUKEZIP1.EXE": files["DUKEZIP1.EXE"],
                          "DUKE2/DUKEZIP2.EXE": files["DUKEZIP2.EXE"]}
    assert plan.commands == ["C:", "cd \\DUKE1", "DUKEZIP1.EXE", "del DUKEZIP1.EXE",
                             "cd \\DUKE2", "DUKEZIP2.EXE", "del DUKEZIP2.EXE"]
    assert log[-1] == "presets: self-extracting ZIP archives into DUKE1, DUKE2"


def test_bundled_duke_nukem_preset_matches_its_disk(m):
    p = m.presets
    bundled = p._parse(p.BUNDLED.read_bytes())
    sizes = bundled["games"][0]["source"]
    files = {name: b"\0" * size for name, size in sizes.items()}
    assert p.find(bundled, source_files=files)["id"] == "duke-nukem-1"
    files["DUKEZIP1.EXE"] += b"\0"
    assert p.find(bundled, source_files=files) is None


def test_prepare_drive(m, tmp_path, log):
    p = m.presets
    a, c = tmp_path / "a", tmp_path / "c"
    touch(a / "Install.Bat", b"@echo off\r\nPAUSE\r\ncd x\r\n  pause  \r\ninstall.exe\r\n")
    plan = p.InstallPlan(mkdirs=["GAME/SAVE"], files={"GAME/X.EXE": b"MZ"},
                         drop={"INSTALL.BAT": ["pause", "cd X"], "MISSING.BAT": ["x"]})
    p.prepare_drive(plan, str(a), str(c), log=log)
    assert (c / "GAME/SAVE").is_dir() and (c / "GAME/X.EXE").read_bytes() == b"MZ"
    assert (a / "Install.Bat").read_bytes() == b"@echo off\r\ninstall.exe\r\n"
    assert log == ["presets: INSTALL.BAT without pause, cd X"]
    # Without A: (a CD or folder) nothing is dropped
    p.prepare_drive(p.InstallPlan(drop={"INSTALL.BAT": ["@echo off"]}), None, str(c))
    assert (a / "Install.Bat").read_bytes() == b"@echo off\r\ninstall.exe\r\n"


def test_after_install_rules(m, tmp_path, log):
    p = m.presets
    root = tmp_path / "c"
    touch(root / "DOOM/DOOM.WAD")
    touch(root / "DOOM/DOOM.EXE")
    touch(root / "DOOM2/DOOM2.WAD")
    touch(root / "DOOM2/DOOM2.EXE")
    touch(root / "WOLF/VSWAP.WL1")
    touch(root / "WOLF/WOLF3D.EXE")
    result = p.after_install({"games": []}, str(root), log=log)
    # Both get their configuration, the first one found (in the folder's order) is the start file
    assert (root / "DOOM2/DEFAULT.CFG").exists()
    assert result.program in {("DOOM", "DOOM.EXE"), ("DOOM2", "DOOM2.EXE")}
    # Swiping turns them faster than the mouse they were made for
    assert result.dosbox == {"cpu": {"cycles": "max"}, "sdl": {"sensitivity": "300"}}
    assert result.mouse == [100, 50]
    cfg = (root / "DOOM/DEFAULT.CFG").read_bytes().decode()
    assert "snd_sbport\t\t544\r\n" in cfg and "mouseb_fire\t\t2\r\n" in cfg
    assert "mouse_sensitivity\t\t9\r\n" in cfg
    # Wolfenstein alone
    root2 = tmp_path / "c2"
    touch(root2 / "VSWAP.WL1")
    touch(root2 / "WOLF3D.EXE")
    result = p.after_install({"games": []}, str(root2))
    assert result.program == ("", "WOLF3D.EXE")
    assert result.dosbox == {"sdl": {"sensitivity": "300"}}
    # The rule's settings are copies: a game's own do not change them
    result.dosbox["sdl"]["sensitivity"] = "1"
    assert p.TOUCH_TURN == {"sdl": {"sensitivity": "300"}}
    # Turning faster than walking: the mouse driver's sideways sensitivity
    assert result.mouse == [100, 50]
    result.mouse[0] = 1
    assert p.TOUCH_MOUSE == [100, 50]


def test_after_install_preset(m, tmp_path, log):
    p = m.presets
    root = tmp_path / "c"
    touch(root / "Game/Run.exe")
    preset = {"id": "g", "title": "The Game", "installed": ["GAME\\RUN.EXE"],
              "program": "game\\run.exe", "dosbox": {"cpu": {"cycles": "3000"}, "bad": {"a": "b"}},
              "files": {"GAME\\SOUND.CFG": {"text": "sb 220"}, "..\\X": {"text": "x"},
                        "GAME\\EURO.TXT": {"text": "€"}, "GAME\\B64": {"base64": "!!"}}}
    result = p.after_install({"games": [preset]}, str(root), log=log)
    assert result.program == ("GAME", "RUN.EXE") and result.title == "The Game"
    assert result.dosbox == {"cpu": {"cycles": "3000"}}
    assert (root / "Game/SOUND.CFG").read_bytes() == b"sb 220"
    # Files that cannot be written are noted, the rest goes on
    assert log.has("presets: ..\\X not written") and log.has("presets: GAME\\EURO.TXT not written")
    assert log.has("presets: GAME\\B64 not written")
    assert not (tmp_path / "X").exists()
    # The program a preset names must be there
    preset["program"] = "GAME\\MISSING.EXE"
    assert p.after_install({"games": [preset]}, str(root)).program is None
    # The plan's preset and title when no preset matches the installed files
    plan = p.InstallPlan(title="From The Plan")
    assert p.after_install({"games": []}, str(root), plan).title == "From The Plan"
    plan = p.InstallPlan(title="Plan", preset={"title": "Preset", "files": {}})
    assert p.after_install({"games": []}, str(root), plan).title == "Preset"


# ----- scummvm -----

DETECT = """\
GameID                         Description                                          Full Path
------------------------------ ---------------------------------------------------- ---------
scumm:tentacle                 Day of the Tentacle (DOS/English)                    /g/DOTT
scumm:maniac                   Maniac Mansion (Enhanced/DOS/English)                /g/DOTT
scumm:maniac                   Maniac Mansion (Enhanced/DOS/English)                /g/DOTT/MANIAC
scumm:maniac                   Maniac Mansion (Enhanced/DOS/English)                /g
not a row
"""


def test_detect_and_choose(m, monkeypatch):
    s = m.scummvm
    monkeypatch.setattr(s.shutil, "which", which())
    assert s.detect("/g") == []
    monkeypatch.setattr(s.shutil, "which", which("scummvm"))
    run = Run((0, DETECT, ""))
    monkeypatch.setattr(s.subprocess, "run", run)
    found = s.detect("/g")
    assert run.calls[0][0] == ["scummvm", "--detect", "--recursive", "--path=/g"]
    assert run.calls[0][1]["env"]["SDL_VIDEODRIVER"] == "dummy"
    assert found[0] == s.Detection("scumm", "tentacle", "Day of the Tentacle (DOS/English)", "/g/DOTT")
    assert len(found) == 4
    # One per game, the one in the deepest folder
    assert [(d.gameid, d.path) for d in s.choose(found)] == [("tentacle", "/g/DOTT"),
                                                              ("maniac", "/g/DOTT/MANIAC")]


def test_add(m, home, monkeypatch):
    s = m.scummvm
    s.INI.parent.mkdir(parents=True)
    s.INI.write_text("[scummvm]\nversionLanguage=en\n\n[tentacle]\ngameid=tentacle\n"
                     "engineid=scumm\npath=/old/DOTT\n")
    d = s.Detection("scumm", "tentacle", "DOTT", "/g/DOTT")

    def adds(cmd, kw):
        if "--path=/g/new" in cmd:
            with open(s.INI, "a") as f:
                f.write("\n[tentacle-1]\ngameid=tentacle\nengineid=scumm\npath=/g/new\n")
    run = Run(effect=adds)
    monkeypatch.setattr(s.subprocess, "run", run)
    assert s.add(d) == ("tentacle", False)  # ScummVM skipped it: the entry it has
    assert run.calls[0][0] == ["scummvm", "--add", "--game=scumm:tentacle", "--path=/g/DOTT"]
    assert s.add(s.Detection("scumm", "tentacle", "DOTT", "/g/new")) == ("tentacle-1", True)
    assert s.add(s.Detection("scumm", "monkey", "MI", "/g/mi")) == (None, False)
    s.INI.write_text("[broken\n")
    assert s._targets() == {}


def png(width=1, height=1):
    import gi
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf
    pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, width, height)
    pixbuf.fill(0x1793d1ff)
    ok, data = pixbuf.save_to_bufferv("png", [], [])
    return bytes(data)


def test_fetch_icon(m, home, monkeypatch, log):
    s = m.scummvm
    d = s.Detection("SCUMM", "Tentacle", "DOTT", "/g")
    server = Server({s.ICON_URL.format("scumm-tentacle"): png()})
    monkeypatch.setattr(s.urllib.request, "urlopen", server)
    assert s.fetch_icon(d, log=log)
    assert (s.ICONS / "scumm-tentacle.png").read_bytes() == png()
    assert server.reads == [(s.ICON_URL.format("scumm-tentacle"), 4 << 20, 20)]
    assert log == ["icon scumm-tentacle fetched"]
    assert s.fetch_icon(d, log=log) and len(server.reads) == 1   # once
    other = s.Detection("scumm", "monkey", "MI", "/g")
    monkeypatch.setattr(s.urllib.request, "urlopen",
                        Server({s.ICON_URL.format("scumm-monkey"): b"<html>404</html>"}))
    assert not s.fetch_icon(other, log=log) and log[-1] == "no icon for scumm-monkey: not a PNG"
    monkeypatch.setattr(s.urllib.request, "urlopen", Server(error=urllib.error.URLError("down")))
    assert not s.fetch_icon(other, log=log) and log[-1].startswith("no icon for scumm-monkey:")
    assert sorted(p.name for p in s.ICONS.iterdir()) == ["scumm-tentacle.png"]


# ----- finish -----

def test_finish(m, home, monkeypatch, fake_bin, log):
    f = m.finish
    monkeypatch.setattr(f.shutil, "which", which())
    assert f.finish("/g", log=log) == [] and log == ["ScummVM is not installed"]
    monkeypatch.setattr(f.shutil, "which", which("scummvm"))
    found = [m.scummvm.Detection("scumm", "tentacle", "DOTT", "/g/DOTT"),
             m.scummvm.Detection("scumm", "maniac", "MM", "/g/DOTT/MANIAC"),
             m.scummvm.Detection("scumm", "zak", "Zak", "/g/ZAK")]
    monkeypatch.setattr(f.scummvm, "detect", lambda path: found if path == "/g" else [])
    added = {"tentacle": ("tentacle", True), "maniac": ("maniac", False), "zak": (None, False)}
    monkeypatch.setattr(f.scummvm, "add", lambda d: added[d.gameid])
    icons = []
    monkeypatch.setattr(f.scummvm, "fetch_icon", lambda d, log: icons.append(d.gameid))
    stub = fake_bin.stub("plinius-scummvm-launchers", "echo '2 games, 3 files changed'")
    monkeypatch.setattr(f, "LAUNCHERS", str(stub))
    log.clear()
    assert f.finish("/g", log=log) == ["tentacle", "maniac"]
    assert icons == ["tentacle", "maniac"]
    assert log == ["DOTT: ScummVM target tentacle", "MM: ScummVM target maniac (was there already)",
                   "ScummVM did not add Zak", "2 games, 3 files changed"]
    assert fake_bin.calls("plinius-scummvm-launchers") == [[]]
    # Silent launcher writer: its exit status is noted instead
    monkeypatch.setattr(f, "LAUNCHERS", str(fake_bin.stub("quiet", "exit 4")))
    log.clear()
    assert f.finish("/g", log=log, fetch_icons=False) == ["tentacle", "maniac"]
    assert log[-1].endswith("quiet ended with 4") and icons == ["tentacle", "maniac"]
    assert f.finish("/other", log=log) == []


# ----- library -----

@pytest.mark.parametrize("title, folder", [
    ("Day of the Tentacle", "Day of the Tentacle"), ("../../.ssh", "ssh"), ("..", "DOS Game"),
    ("A/B\\C:D*?\"<>|", "ABCD"), ("Commander Keen: Goodbye, Galaxy!", "Commander Keen Goodbye, Galaxy"),
    ("x" * 100, "x" * 80), ("Über (DE) & more+", "Über (DE) & more+"),
])
def test_slug(m, title, folder):
    assert m.library.slug(title) == folder


def test_new_folder_never_takes_an_existing_one(m, home):
    lib = m.library
    assert lib.new_folder("Doom") == home.games / "Doom"
    (home.games / "Doom").mkdir()
    (home.games / "Doom (2)").mkdir()
    assert lib.new_folder("Doom") == home.games / "Doom (3)"


def test_save_and_load(m, home):
    lib = m.library
    folder = home.games / "Doom"
    folder.mkdir(parents=True)
    game = lib.Game("Doom", "dosbox", folder, [], "DOOM", "DOOM.EXE", "cd/doom.cue",
                    {"cpu": {"cycles": "max"}})
    lib.save(game)
    assert lib.load(folder) == game
    assert game.id == "Doom" and lib.Game("x", "scummvm", targets=["t1"]).id == "t1"
    (folder / lib.RECORD).write_text("{}")
    assert lib.load(folder) == lib.Game("Doom", "dosbox", folder, [], "", "", "", {})
    (folder / lib.RECORD).write_text("{broken")
    assert lib.load(folder) is None
    assert lib.load(home.games / "missing") is None


def test_games_lists_own_and_scummvm_games(m, home):
    lib = m.library
    for title, kind, targets in (("zork", "dosbox", []), ("Day of the Tentacle", "scummvm",
                                                          ["tentacle", "maniac"])):
        folder = home.games / title
        folder.mkdir(parents=True)
        lib.save(lib.Game(title, kind, folder, targets))
    (home.games / "not a game").mkdir()
    (home.games / "file").write_text("")
    m.scummvm.INI.parent.mkdir(parents=True)
    m.scummvm.INI.write_text(
        "[scummvm]\ngameid=x\n[tentacle]\ngameid=tentacle\n[maniac]\ngameid=maniac\n"
        "[monkey]\ngameid=monkey\ndescription=The Secret of Monkey Island (VGA/DOS/English)\n"
        "[loom]\ngameid=loom\n[nogame]\npath=/x\n")
    assert [(g.title, g.kind, g.targets) for g in lib.games()] == [
        ("Day of the Tentacle", "scummvm", ["tentacle", "maniac"]),
        ("loom", "scummvm", ["loom"]),
        ("The Secret of Monkey Island", "scummvm", ["monkey"]),
        ("zork", "dosbox", [])]
    m.scummvm.INI.write_text("[broken\n")
    home.games.joinpath("zork", lib.RECORD).unlink()
    assert [g.title for g in lib.games()] == ["Day of the Tentacle"]


def test_games_without_anything(m, home):
    assert m.library.games() == []


def test_remove_targets_keeps_the_rest_of_the_file(m, home):
    lib = m.library
    m.scummvm.INI.parent.mkdir(parents=True)
    text = ("; my settings\n[scummvm]\nlastselectedgame=tentacle\n\n[Tentacle]\ngameid=tentacle\n\n"
            "[monkey]\ngameid=monkey\n  [maniac]  \ngameid=maniac\n")
    m.scummvm.INI.write_text(text)
    lib.remove_targets(["tentacle", "maniac"])
    assert m.scummvm.INI.read_text() == ("; my settings\n[scummvm]\nlastselectedgame=tentacle\n\n"
                                         "[monkey]\ngameid=monkey\n")
    lib.remove_targets([])
    m.scummvm.INI.unlink()
    lib.remove_targets(["monkey"])
    assert not m.scummvm.INI.exists()


def test_remove_deletes_only_its_own_folders(m, home, tmp_path):
    lib = m.library
    own = home.games / "Doom"
    own.mkdir(parents=True)
    game = lib.Game("Doom", "dosbox", own, [])
    lib.save(game)
    m.dosgames.write_launcher(game)
    lib.remove(game, delete_files=False)
    assert own.exists() and not list(m.dosgames.APPS.iterdir())
    lib.remove(game)
    assert not own.exists()
    # Without the record file: not the DOS Installer's
    foreign = home.games / "Mine"
    foreign.mkdir()
    lib.remove(lib.Game("Mine", "dosbox", foreign, []))
    assert foreign.exists()
    # A record outside ~/Games/dos, or a link pointing there from it
    outside = tmp_path / "elsewhere/Quake"
    outside.mkdir(parents=True)
    lib.save(lib.Game("Quake", "dosbox", outside, []))
    lib.remove(lib.Game("Quake", "dosbox", outside, []))
    (home.games / "Link").symlink_to(outside)
    lib.remove(lib.Game("Link", "dosbox", home.games / "Link", []))
    assert (outside / lib.RECORD).exists()
    # A ScummVM game added elsewhere: only its targets go
    m.scummvm.INI.parent.mkdir(parents=True)
    m.scummvm.INI.write_text("[loom]\ngameid=loom\n")
    lib.remove(lib.Game("Loom", "scummvm", targets=["loom"]))
    assert m.scummvm.INI.read_text() == ""


def test_remove_leftovers(m, home, monkeypatch, tmp_path, log):
    lib = m.library
    for name in (".installing-100-11", ".installing-100-22", ".installing-100-33", "Doom",
                 ".installing-x-1"):
        (home.games / name).mkdir(parents=True)
    proc = tmp_path / "proc"
    touch(proc / "22/cmdline", b"python3\0-I\0/usr/bin/plinius-dos-installer\0")
    touch(proc / "33/cmdline", b"firefox\0")   # the number came back for another program
    monkeypatch.setattr(lib, "open", lambda p, *a: open(str(p).replace("/proc", str(proc), 1), *a),
                        raising=False)
    lib.remove_leftovers(log=log)
    assert sorted(p.name for p in home.games.iterdir()) == [".installing-100-22",
                                                            ".installing-x-1", "Doom"]
    assert sorted(log) == ["removed the leftover .installing-100-11",
                           "removed the leftover .installing-100-33"]
    monkeypatch.setattr(lib, "GAMES", tmp_path / "none")
    lib.remove_leftovers(log=log)


# ----- dosgames: launchers for DOSBox -----

def test_candidates_likely_ones_first(m, tmp_path):
    root = tmp_path / "c"
    for name, size in (("KEEN/KEEN.EXE", 10), ("KEEN/KEEN.BAT", 1), ("KEEN/EDIT.EXE", 900),
                       ("KEEN/INSTALL.EXE", 5), ("KEEN/SETSOUND.EXE", 5), ("KEEN/DOS4GW.EXE", 5),
                       ("KEEN/readme.txt", 5), ("TOOLS/BIG.EXE", 500), ("TOOLS/SMALL.COM", 1),
                       ("GO.BAT", 1), ("A/B/C/DEEP.EXE", 1), ("A/B/NEAR.EXE", 1)):
        touch(root / name, b"x" * size)
    assert m.dosgames.candidates(root) == [
        ("KEEN", "KEEN.BAT"), ("KEEN", "KEEN.EXE"), ("", "GO.BAT"), ("KEEN", "EDIT.EXE"),
        ("TOOLS", "BIG.EXE"), ("TOOLS", "SMALL.COM"), ("A\\B", "NEAR.EXE")]


def test_drive(m, tmp_path):
    assert m.dosgames.drive(tmp_path) == tmp_path / "game"
    (tmp_path / "c").mkdir()
    assert m.dosgames.drive(tmp_path) == tmp_path / "c"


def test_write_and_find_launcher(m, home):
    dg = m.dosgames
    folder = home.games / "Commander Keen: Vol. 1 (2)"
    folder.mkdir(parents=True)
    game = m.library.Game("Keen\nExec=rm -rf ~ \\o/", "dosbox", folder, [], "KEEN", "KEEN.EXE")
    path = dg.write_launcher(game)
    app_id = dg._app_id(game)
    assert app_id.startswith("org.plinius.DOSGame.Commander_Keen_Vol_1_2_")
    assert path == dg.APPS / f"{app_id}.desktop"
    entry = dict(line.split("=", 1) for line in path.read_text().splitlines()[1:])
    # One line per key: the title cannot add an Exec= of its own
    assert entry["Name"] == "Keen Exec=rm -rf ~ \\\\o/"
    assert entry["Exec"] == f"plinius-dos-installer --play {app_id[len(dg.PREFIX):]}"
    assert entry["Icon"] == "dosbox" and entry[dg.MARKER] == folder.name
    assert sum(line.startswith("Exec=") for line in path.read_text().splitlines()) == 1
    key = app_id[len(dg.PREFIX):]
    other = m.library.Game("x", "scummvm", targets=["t"])
    assert dg.find_by_launcher(key, [other, game]) is game
    assert dg.find_by_launcher("nothing", [other, game]) is None
    # The same title in another folder gets another id
    assert dg._app_id(m.library.Game("x", "dosbox", home.games / "Commander Keen Vol. 1 (2)")) \
        != app_id
    assert dg._app_id(m.library.Game("x", "dosbox", home.games / "???")).startswith(
        "org.plinius.DOSGame.game_")


def test_launcher_icon_from_scummvm_pack(m, home):
    dg = m.dosgames
    dg.SCUMMVM_PACK.parent.mkdir(parents=True)
    with zipfile.ZipFile(dg.SCUMMVM_PACK, "w") as z:
        z.writestr("icons/platforms/pc.png", png())
    folder = home.games / "Doom"
    folder.mkdir(parents=True)
    game = m.library.Game("Doom", "dosbox", folder)
    path = dg.write_launcher(game)
    icon = dg.ICONS / f"{dg._app_id(game)}.png"
    assert f"Icon={icon}\n" in path.read_text() and icon.read_bytes() == png()
    dg.remove_launcher(game)
    dg.remove_launcher(game)
    assert not path.exists() and not icon.exists()
    dg.remove_launcher(m.library.Game("x", "scummvm", targets=["t"]))
    dg.SCUMMVM_PACK.write_bytes(b"not a zip")
    assert dg._icon("x") == "dosbox"


def test_play(m, home, monkeypatch):
    dg = m.dosgames
    folder = home.games / "Doom"
    (folder / "c").mkdir(parents=True)
    runs = []

    def run(cmd, env=None):
        runs.append((cmd, Path(cmd[-1]).read_text(), env))
        return types.SimpleNamespace(returncode=7)
    monkeypatch.setattr(dg.subprocess, "run", run)
    game = m.library.Game("Doom", "dosbox", folder, [], "ID\\DOOM", "DOOM.EXE", "cd/doom.cue",
                          {"cpu": {"cycles": "max"}, "sdl": {"output": "surface"}})
    assert dg.play(game) == 7
    cmd, conf, env = runs[0]
    assert cmd[:3] == ["dosbox", "-userconf", "-conf"] and env["SDL_APP_ID"] == "dosbox"
    assert "[cpu]\ncycles=max" in conf and "surface" not in conf
    assert autoexec(conf) == [f'mount c "{folder / "c"}"',
                              f'imgmount d "{folder / "cd/doom.cue"}" -t iso', "c:",
                              "cd \\ID\\DOOM", "DOOM.EXE", "exit"]
    # The configuration is gone afterwards
    assert not Path(cmd[-1]).exists()
    game = m.library.Game("Keen", "dosbox", home.games / "Keen", [], "", "KEEN.EXE")
    dg.play(game)
    assert autoexec(runs[1][1]) == [f'mount c "{home.games / "Keen/game"}"', "c:", "KEEN.EXE",
                                    "exit"]
    assert "[cpu]" not in runs[1][1]


class Controls:
    """subprocess.Popen for plinius-touch-controls: what it was started with, how it ended."""

    made = []

    def __init__(self, cmd, stubborn=False):
        self.cmd = cmd
        self.stubborn = stubborn
        self.events = []
        Controls.made.append(self)

    def terminate(self):
        self.events.append("terminate")

    def kill(self):
        self.events.append("kill")

    def wait(self, timeout=None):
        self.events.append(("wait", timeout))
        if self.stubborn and timeout is not None:
            raise subprocess.TimeoutExpired(self.cmd, timeout)
        return 0


def test_play_with_the_touch_controls(m, home, monkeypatch):
    dg = m.dosgames
    folder = home.games / "Duke"
    (folder / "c").mkdir(parents=True)
    order = []
    monkeypatch.setattr(dg.shutil, "which", lambda name: f"/usr/bin/{name}")
    Controls.made = []

    def popen(cmd):
        order.append("controls")
        return Controls(cmd)
    monkeypatch.setattr(dg.subprocess, "Popen", popen)

    def run(cmd, env=None):
        order.append("dosbox")
        # DOSBox runs while the controls are there
        assert Controls.made[-1].events == []
        return types.SimpleNamespace(returncode=0)
    monkeypatch.setattr(dg.subprocess, "run", run)
    game = m.library.Game("Duke", "dosbox", folder, [], "DUKE1", "DN1.EXE",
                          touch={"stick": "wasd", "buttons": ["ctrl", "alt"], "small": ["esc"]})
    assert dg.play(game) == 0
    assert order == ["controls", "dosbox"]
    c = Controls.made[-1]
    assert c.cmd == ["/usr/bin/plinius-touch-controls", "--app-id", "dosbox", "--stick", "wasd",
                     "--buttons", "ctrl,alt", "--small", "esc"]
    assert c.events == ["terminate", ("wait", 2)]
    # Without settings of its own the controls' defaults; one that does not end is killed
    monkeypatch.setattr(dg.subprocess, "Popen", lambda cmd: Controls(cmd, stubborn=True))
    dg.play(m.library.Game("Keen", "dosbox", folder, [], "", "KEEN.EXE"))
    c = Controls.made[-1]
    assert c.cmd == ["/usr/bin/plinius-touch-controls", "--app-id", "dosbox"]
    assert c.events == ["terminate", ("wait", 2), "kill", ("wait", None)]


def test_play_without_the_touch_controls(m, home, monkeypatch):
    dg = m.dosgames
    folder = home.games / "Keen"
    (folder / "game").mkdir(parents=True)
    monkeypatch.setattr(dg.subprocess, "run", lambda cmd, env=None: types.SimpleNamespace(
        returncode=0))
    started = []
    monkeypatch.setattr(dg.subprocess, "Popen", lambda cmd: started.append(cmd))
    # Not installed: DOSBox alone
    monkeypatch.setattr(dg.shutil, "which", lambda name: None)
    assert dg.play(m.library.Game("Keen", "dosbox", folder, [], "", "KEEN.EXE")) == 0
    assert started == []

    # Switched off for this game: DOSBox alone
    monkeypatch.setattr(dg.shutil, "which", lambda name: "/usr/bin/" + name)
    assert dg.play(m.library.Game("Keen", "dosbox", folder, [], "", "KEEN.EXE",
                                  controls=False)) == 0
    assert started == []

    # Installed but not startable: DOSBox alone too
    def broken(cmd):
        raise OSError("exec format error")
    monkeypatch.setattr(dg.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(dg.subprocess, "Popen", broken)
    assert dg.play(m.library.Game("Keen", "dosbox", folder, [], "", "KEEN.EXE")) == 0


def test_touch_settings_from_a_preset(m):
    p = m.presets
    assert p.touch_settings(None) == {} and p.touch_settings(["stick"]) == {}
    assert p.touch_settings({"stick": "joystick", "buttons": "ctrl", "small": None}) == {}
    assert p.touch_settings({"stick": "numpad", "buttons": ["Ctrl", "alt; rm", 7, "f10", "a",
                                                            "b", "c"],
                             "small": ["esc", "keyboard", "x" * 11]}) == {
        "stick": "numpad", "buttons": ["ctrl", "f10", "a", "b"], "small": ["esc", "keyboard"]}
    assert p.touch_args({"stick": "arrows", "buttons": [], "bad": 1}) == [
        "--stick", "arrows", "--buttons", ""]
    assert p.touch_args({}) == []


def test_doom_gets_its_weapon_keys(m, home, monkeypatch):
    p, dg = m.presets, m.dosgames
    weapons = ["1", "2", "3", "4", "5", "6", "7"]
    # By the start file, whatever case it is written in
    assert p.game_touch({}, "DOOM2.EXE") == {"keys": weapons}
    assert p.game_touch({"stick": "wasd"}, "doom.exe") == {"stick": "wasd", "keys": weapons}
    assert p.touch_args({}, "DOOM.EXE") == ["--keys", "1,2,3,4,5,6,7"]
    # A game's own keys win, also none at all; other games get none
    assert p.game_touch({"keys": ["F1", "tab"]}, "DOOM2.EXE") == {"keys": ["f1", "tab"]}
    assert p.game_touch({"keys": []}, "DOOM2.EXE") == {"keys": []}
    assert p.game_touch({}, "HERETIC.EXE") == {} and p.game_touch({}, None) == {}
    assert p.touch_settings({"keys": [str(n) for n in range(10)]})["keys"] == [
        "0", "1", "2", "3", "4", "5", "6", "7"]
    # Played: the controls get them
    folder = home.games / "Doom II"
    (folder / "c").mkdir(parents=True)
    monkeypatch.setattr(dg.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(dg.subprocess, "run", lambda cmd, env=None: types.SimpleNamespace(
        returncode=0))
    Controls.made = []
    monkeypatch.setattr(dg.subprocess, "Popen", lambda cmd: Controls(cmd))
    assert dg.play(m.library.Game("Doom II", "dosbox", folder, [], "DOOM2", "DOOM2.EXE")) == 0
    assert Controls.made[-1].cmd == ["/usr/bin/plinius-touch-controls", "--app-id", "dosbox",
                                     "--keys", "1,2,3,4,5,6,7"]


def test_after_install_takes_the_presets_touch_controls(m, tmp_path):
    p = m.presets
    root = tmp_path / "c"
    touch(root / "DUKE1/DN1.EXE")
    preset = {"id": "duke", "title": "Duke", "installed": ["DUKE1\\DN1.EXE"],
              "touch": {"stick": "arrows", "buttons": ["ctrl", "alt"], "small": ["esc", "f1"]}}
    result = p.after_install({"games": [preset]}, str(root))
    assert result.touch == {"stick": "arrows", "buttons": ["ctrl", "alt"], "small": ["esc", "f1"]}
    del preset["touch"]
    assert p.after_install({"games": [preset]}, str(root)).touch == {}
    assert p.after_install({"games": []}, str(root)).touch == {}


def test_save_and_load_the_touch_controls(m, home):
    lib = m.library
    folder = home.games / "Duke"
    folder.mkdir(parents=True)
    game = lib.Game("Duke", "dosbox", folder, [], "DUKE1", "DN1.EXE",
                    touch={"stick": "wasd", "buttons": ["space"]})
    lib.save(game)
    assert lib.load(folder).touch == {"stick": "wasd", "buttons": ["space"]}
    # On unless switched off; a record from before the switch has them on
    assert lib.load(folder).controls is True
    game.controls = False
    lib.save(game)
    assert lib.load(folder).controls is False
    record = folder / lib.RECORD
    data = json.loads(record.read_text())
    del data["controls"]
    record.write_text(json.dumps(data))
    assert lib.load(folder).controls is True


def test_mouse_speed_and_its_program(m):
    p = m.presets
    assert p.mouse_speed([100, 50]) == [100, 50] and p.mouse_speed((1, 100)) == [1, 100]
    for bad in (None, [], [50], [0, 50], [50, 101], [50, "50"], [True, 50], [50.0, 50],
                {"x": 50}, [50, 50, 50]):
        assert p.mouse_speed(bad) == []
    # The program tried on the phone in Doom: INT 33h function 1Ah, sideways 100, up/down 50
    assert p.mouse_program([100, 50]) == bytes.fromhex(
        "b81a00 bb6400 b93200 ba3200 cd33 c3")
    assert p.mouse_program(None) == bytes.fromhex("b81a00 bb3200 b93200 ba3200 cd33 c3")


def test_sensitivity_of_a_game(m):
    p = m.presets
    # DOSBox's own 100 when unset or not a number, within the range of the game's page
    for settings, percent in ((None, 100), ({}, 100), ({"sdl": {"sensitivity": "300"}}, 300),
                              ({"sdl": {"sensitivity": "five"}}, 100),
                              ({"sdl": {"sensitivity": "5"}}, 10),
                              ({"sdl": {"sensitivity": "4000"}}, 1000),
                              ({"cpu": {"cycles": "max"}}, 100)):
        assert p.sensitivity(settings) == percent
    # Set beside the other settings, which stay as they were; rounded and kept in the range
    before = {"cpu": {"cycles": "max"}, "sdl": {"sensitivity": "300"}}
    assert p.with_sensitivity(before, 450.4) == {"cpu": {"cycles": "max"},
                                                 "sdl": {"sensitivity": "450"}}
    assert before["sdl"] == {"sensitivity": "300"}
    assert p.with_sensitivity(None, 2) == {"sdl": {"sensitivity": "10"}}
    assert p.with_sensitivity({}, 9999) == {"sdl": {"sensitivity": "1000"}}
    # 100 is DOSBox's own: left out, an empty section with it
    assert p.with_sensitivity(before, 100) == {"cpu": {"cycles": "max"}}
    # What DOSBox gets
    assert p.dosbox_conf(p.with_sensitivity({}, 250)) == "[sdl]\nsensitivity=250\n"


def test_cpu_speed_sharp_pixels_and_stick_of_a_game(m):
    p = m.presets
    # A setting in or out, the others as they were; a section left empty goes with it
    before = {"cpu": {"cycles": "max"}, "dos": {"ems": "false"}}
    assert p.with_setting(before, "cpu", "cycles", None) == {"dos": {"ems": "false"}}
    assert p.with_setting(before, "sdl", "output", "openglnb") == {
        **before, "sdl": {"output": "openglnb"}}
    assert before == {"cpu": {"cycles": "max"}, "dos": {"ems": "false"}}
    assert p.with_setting(None, "cpu", "cycles", None) == {}
    # CPU speed: DOSBox's auto when unset, which leaves it out again
    assert p.cpu_speed(None) == "auto" and p.cpu_speed(before) == "max"
    assert p.with_cpu_speed({}, "8000") == {"cpu": {"cycles": "8000"}}
    assert p.with_cpu_speed(before, "auto") == {"dos": {"ems": "false"}}
    assert p.CPU_SPEEDS[0] == "auto" and p.CPU_SPEEDS[-1] == "max"
    # Sharp pixels: OpenGL without smoothing; no other output gets through
    assert p.sharp_pixels({"sdl": {"output": "openglnb"}}) is True
    assert p.sharp_pixels({"sdl": {"output": "opengl"}}) is False and p.sharp_pixels({}) is False
    assert p.with_sharp_pixels({}, True) == {"sdl": {"output": "openglnb"}}
    assert p.with_sharp_pixels({"sdl": {"output": "openglnb"}}, False) == {}
    assert p.dosbox_conf({"sdl": {"output": "openglnb"}}) == "[sdl]\noutput=openglnb\n"
    for other in ("surface", "overlay", "ddraw", "opengl nb"):
        assert p.dosbox_settings({"sdl": {"output": other}}) == {}
    # The stick: the arrow keys are the controls' own; unknown ones are not kept
    touch = {"buttons": ["space"], "stick": "wasd"}
    assert p.with_stick(touch, "numpad") == {"buttons": ["space"], "stick": "numpad"}
    assert p.with_stick(touch, "arrows") == {"buttons": ["space"]}
    assert p.with_stick(touch, "joystick") == {"buttons": ["space"]}
    assert p.with_stick(None, "wasd") == {"stick": "wasd"}


def test_setup_program_of_a_game(m, home):
    dg = m.dosgames
    lib = m.library
    folder = home.games / "Doom"
    game = lib.Game("Doom", "dosbox", folder, [], "DOOM", "DOOM.EXE")
    # None without a folder of its own or any setup
    assert dg.setup_program(lib.Game("Loom", "scummvm", None, ["loom"])) is None
    assert dg.setup_program(game) is None
    (folder / "c/doom").mkdir(parents=True)
    assert dg.setup_program(game) is None
    # In C:\ when the game's folder has none, whatever the case of the names
    (folder / "c/install.bat").write_bytes(b"")
    assert dg.setup_program(game) == ("", "INSTALL.BAT")
    (folder / "c/doom/Install.exe").write_bytes(b"")
    assert dg.setup_program(game) == ("DOOM", "INSTALL.EXE")
    # SETUP before the others, a folder named like it is no program
    (folder / "c/doom/setsound.com").write_bytes(b"")
    assert dg.setup_program(game) == ("DOOM", "SETSOUND.COM")
    (folder / "c/doom/SETUP.EXE").mkdir()
    assert dg.setup_program(game) == ("DOOM", "SETSOUND.COM")
    (folder / "c/doom/setup.exe").write_bytes(b"")
    assert dg.setup_program(game) == ("DOOM", "SETUP.EXE")
    # A game in C:\ itself looks there once
    keen = lib.Game("Keen", "dosbox", home.games / "Keen", [], "", "KEEN.EXE")
    (keen.folder / "game").mkdir(parents=True)
    (keen.folder / "game/SETUP.EXE").write_bytes(b"")
    assert dg.setup_program(keen) == ("", "SETUP.EXE")


def test_play_the_setup(m, home, monkeypatch):
    dg = m.dosgames
    folder = home.games / "Doom"
    (folder / "c/DOOM").mkdir(parents=True)
    monkeypatch.setattr(dg.shutil, "which", lambda name: None)
    seen = []
    monkeypatch.setattr(dg.subprocess, "run", lambda cmd, env=None: seen.append(
        autoexec(Path(cmd[-1]).read_text())) or types.SimpleNamespace(returncode=0))
    game = m.library.Game("Doom", "dosbox", folder, [], "DOOM", "DOOM.EXE")
    # Without a setup the game itself
    dg.play(game, setup=True)
    assert seen[-1][-3:] == ["cd \\DOOM", "DOOM.EXE", "exit"]
    (folder / "c/SETUP.EXE").write_bytes(b"")
    dg.play(game, setup=True)
    assert seen[-1] == [f'mount c "{folder / "c"}"', "c:", "SETUP.EXE", "exit"]
    dg.play(game)
    assert seen[-1][-3:] == ["cd \\DOOM", "DOOM.EXE", "exit"]


def test_play_sets_the_mouse_speed_first(m, home, monkeypatch):
    dg = m.dosgames
    folder = home.games / "Doom"
    (folder / "c").mkdir(parents=True)
    monkeypatch.setattr(dg.shutil, "which", lambda name: None)
    seen = []

    def run(cmd, env=None):
        conf = Path(cmd[-1]).read_text()
        tools = autoexec(conf)[1].split('"')[1]
        seen.append((autoexec(conf), tools, Path(tools, "MOUSE.COM").read_bytes()))
        return types.SimpleNamespace(returncode=0)
    monkeypatch.setattr(dg.subprocess, "run", run)
    game = m.library.Game("Doom", "dosbox", folder, [], "DOOM", "DOOM.EXE", mouse=[100, 50])
    dg.play(game)
    lines, tools, program = seen[0]
    # On a drive of its own before the game, which no longer sees it
    assert lines == [f'mount c "{folder / "c"}"', f'mount y "{tools}"', "y:\\mouse.com",
                     "mount -u y", "c:", "cd \\DOOM", "DOOM.EXE", "exit"]
    assert program == m.presets.mouse_program([100, 50])
    assert not Path(tools).exists()
    # Nothing of it without a speed, or with one that is not one
    monkeypatch.setattr(dg.subprocess, "run", lambda cmd, env=None: seen.append(
        autoexec(Path(cmd[-1]).read_text())) or types.SimpleNamespace(returncode=0))
    game.mouse = [500, 50]
    dg.play(game)
    assert not any("mouse.com" in line or line.startswith("mount y") for line in seen[-1])


def test_save_and_load_the_mouse_speed(m, home):
    lib = m.library
    folder = home.games / "Doom"
    folder.mkdir(parents=True)
    lib.save(lib.Game("Doom", "dosbox", folder, [], "DOOM", "DOOM.EXE", mouse=[100, 50]))
    assert lib.load(folder).mouse == [100, 50]
    record = folder / lib.RECORD
    record.write_text(record.read_text().replace("100", "1000"))
    assert lib.load(folder).mouse == []
    # Games installed before it was there
    lib.save(lib.Game("Doom", "dosbox", folder, [], "DOOM", "DOOM.EXE"))
    assert lib.load(folder).mouse == []


def test_after_install_takes_the_presets_mouse_speed(m, tmp_path):
    p = m.presets
    root = tmp_path / "c"
    touch(root / "DUKE1/DN1.EXE")
    preset = {"id": "duke", "title": "Duke", "installed": ["DUKE1\\DN1.EXE"], "mouse": [80, 30]}
    assert p.after_install({"games": [preset]}, str(root)).mouse == [80, 30]
    preset["mouse"] = [80, 300]
    assert p.after_install({"games": [preset]}, str(root)).mouse == []
    # A rule's speed stays when the preset has none of its own
    touch(root / "DOOM/DOOM.WAD")
    touch(root / "DOOM/DOOM.EXE")
    assert p.after_install({"games": [preset]}, str(root)).mouse == [100, 50]


# ----- install: the whole way -----

class Ui:
    """The installer's questions, answered from the test."""

    def __init__(self, go=True, program="first"):
        self.go = go
        self.program = program
        self.statuses, self.logs, self.asked, self.disks = [], [], [], []

    def status(self, text):
        self.statuses.append(text)

    def log(self, text):
        self.logs.append(text)

    def before_installer(self, program, floppies=False):
        self.asked.append(("installer", program, floppies))
        return self.go

    def floppies(self, disks):
        self.disks.append(disks)

    def choose_program(self, candidates, title):
        self.asked.append(("program", candidates, title))
        if self.program == "first":
            return (candidates[0], title + " Deluxe") if candidates else None
        return self.program


@pytest.fixture
def flow(m, home, monkeypatch):
    """ScummVM, DOSBox and the launcher writer as fakes for install.install."""
    state = types.SimpleNamespace(detect={}, finish=["target"], installs=[], drives=[],
                                  installs_files={"GAME/GAME.EXE": b"MZ"}, finished=[])
    monkeypatch.setattr(m.presets, "load", lambda log=print: {"schema": 1, "updated": 0,
                                                              "games": []})

    def detect(path):
        for marker, found in state.detect.items():
            if any(Path(path).rglob(marker)):
                return [m.scummvm.Detection(*f[:3], f"{path}/{f[3]}" if f[3] else path)
                        for f in found]
        return []

    def finish(root, log=print):
        state.finished.append(root)
        return state.finish

    def write_c(target):
        for name, data in state.installs_files.items():
            touch(Path(target) / name, data)

    def from_floppies(images, target, program, log=print, work=None, started=None, plan=None):
        state.installs.append((images, target, program, plan))
        started and started("disks")
        write_c(target)
        return 0, None

    def from_drive(target, program, cd=None, folder=None, log=print, plan=None):
        state.drives.append((target, program, cd, folder, plan))
        write_c(target)
        return 0

    monkeypatch.setattr(m.scummvm, "detect", detect)
    monkeypatch.setattr(m.install, "finish", finish)
    monkeypatch.setattr(m.dosbox, "install_from_floppies", from_floppies)
    monkeypatch.setattr(m.dosbox, "install_from_drive", from_drive)
    return state


def test_install_scummvm_game_from_a_zip(m, home, flow, tmp_path):
    src = make_zip(tmp_path / "dott.zip", {"DOTT/TENTACLE.000": b"0", "DOTT/MANIAC/MANIAC.EXE": b"m"})
    flow.detect = {"TENTACLE.000": [("scumm", "tentacle", "Day of the Tentacle (CD/DOS/English)", ""),
                                    ("scumm", "maniac", "Maniac Mansion (DOS)", "MANIAC")]}
    ui = Ui()
    game = m.install.install(m.sources.classify([src]), ui)
    folder = home.games / "Day of the Tentacle"
    assert game == m.library.Game("Day of the Tentacle", "scummvm", folder, ["target"])
    # The archive's one folder is the game
    assert (folder / "game/TENTACLE.000").read_bytes() == b"0"
    assert (folder / "game/MANIAC/MANIAC.EXE").exists()
    assert sorted(p.name for p in folder.iterdir()) == ["game", m.library.RECORD]
    assert flow.finished == [str(folder / "game")] and ui.asked == []
    assert m.library.load(folder) == game
    assert [p.name for p in home.games.iterdir()] == ["Day of the Tentacle"]


def test_install_dos_game_from_floppies(m, home, flow, tmp_path, monkeypatch):
    images = floppy_disks(tmp_path)
    ui = Ui()
    game = m.install.install(m.sources.classify([images[0]]), ui, title="My Game")
    assert flow.installs[0][0] == images and flow.installs[0][2] == "INSTALL.EXE"
    assert ui.asked[0] == ("installer", "INSTALL.EXE", True)
    assert ui.disks == ["disks", None]
    assert ui.asked[1] == ("program", [("GAME", "GAME.EXE")], "My Game")
    folder = home.games / "My Game Deluxe"
    assert game == m.library.Game("My Game Deluxe", "dosbox", folder, [], "GAME", "GAME.EXE")
    assert (folder / "c/GAME/GAME.EXE").exists() and not (folder / "tmp").exists()
    launcher = m.dosgames.APPS / f"{m.dosgames._app_id(game)}.desktop"
    assert "Name=My Game Deluxe\n" in launcher.read_text()
    assert "The installer is running" in ui.statuses


def test_install_cancelled_before_the_installer(m, home, flow, tmp_path):
    images = floppy_disks(tmp_path)
    with pytest.raises(m.install.Cancelled):
        m.install.install(m.sources.classify([images[0]]), Ui(go=False))
    assert list(home.games.iterdir()) == [] and flow.installs == []


def test_install_that_installed_nothing(m, home, flow, tmp_path):
    flow.installs_files = {}
    with pytest.raises(RuntimeError, match="did not install anything"):
        m.install.install(m.sources.classify([floppy_disks(tmp_path)[0]]), Ui())
    assert list(home.games.iterdir()) == []


def test_install_scummvm_game_after_its_installer(m, home, flow, tmp_path):
    flow.installs_files = {"DOTT/TENTACLE.000": b"0"}
    flow.detect = {"TENTACLE.000": [("scumm", "tentacle", "Day of the Tentacle (Floppy/DOS)", "")]}
    game = m.install.install(m.sources.classify([floppy_disks(tmp_path)[0]]), Ui())
    folder = home.games / "Day of the Tentacle"
    assert game.kind == "scummvm" and game.folder == folder
    assert flow.finished == [str(folder / "c")]


def test_install_failing_after_the_move_leaves_nothing(m, home, flow, tmp_path):
    flow.installs_files = {"DOTT/TENTACLE.000": b"0"}
    flow.detect = {"TENTACLE.000": [("scumm", "tentacle", "Day of the Tentacle (Floppy/DOS)", "")]}
    flow.finish = []
    # A game that was there before stays as it was
    touch(home.games / "Other/plinius-game.json", b"{}")
    with pytest.raises(RuntimeError, match="ScummVM did not take the game"):
        m.install.install(m.sources.classify([floppy_disks(tmp_path)[0]]), Ui())
    assert [p.name for p in home.games.iterdir()] == ["Other"]
    assert (home.games / "Other/plinius-game.json").read_bytes() == b"{}"


def test_install_failing_while_copying_the_cd_leaves_nothing(m, home, flow, tmp_path,
                                                             monkeypatch):
    touch(tmp_path / "cd/game.bin", b"track")
    cue = tmp_path / "cd/game.cue"
    cue.write_text('FILE "game.bin" BINARY\n')

    def copy2(src, dest):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(m.install.shutil, "copy2", copy2)
    with pytest.raises(OSError, match="No space left"):
        m.install.install(m.sources.classify([str(cue)]), Ui())
    assert list(home.games.iterdir()) == []
    # No launcher for a game that is not there
    assert not (m.dosgames.APPS.exists() and any(m.dosgames.APPS.iterdir()))


def test_install_never_moves_onto_a_folder_that_is_there(m, home, tmp_path):
    staging = home.games / ".installing-1-1"
    touch(staging / "c/X")
    taken = home.games / "Taken"
    taken.mkdir(parents=True)
    moved = []
    with pytest.raises(FileExistsError):
        m.install._move(staging, taken, moved)
    # rename() would have put the staging folder in place of the empty one
    assert moved == [] and list(taken.iterdir()) == [] and (staging / "c/X").exists()
    assert m.install._move(staging, home.games / "Free", moved) == home.games / "Free"
    assert moved == [home.games / "Free"] and (home.games / "Free/c/X").exists()


def test_install_from_a_cue_cd(m, home, flow, tmp_path):
    touch(tmp_path / "cd/game.bin", b"track")
    cue = tmp_path / "cd/game.cue"
    cue.write_text('FILE "game.bin" BINARY\n')
    ui = Ui()
    game = m.install.install(m.sources.classify([str(cue)]), ui)
    # Without the files of a .cue only INSTALL can be guessed
    assert flow.drives[0][1:4] == ("INSTALL", str(cue), None)
    folder = home.games / "game Deluxe"
    assert game.cd == "cd/game.cue" and game.program == "GAME.EXE"
    assert (folder / "cd/game.cue").exists() and (folder / "cd/game.bin").read_bytes() == b"track"
    assert m.library.load(folder).cd == "cd/game.cue"


def test_install_from_an_iso(m, home, flow, tmp_path, monkeypatch):
    def unpack_cd(iso, work, log=print):
        touch(Path(work) / "SETUP.EXE")
        touch(Path(work) / "DATA/X")
        return work
    monkeypatch.setattr(m.sources, "unpack_cd", unpack_cd)
    iso = touch(tmp_path / "Game_CD.iso")
    game = m.install.install(m.sources.classify([iso]), Ui())
    assert flow.drives[0][1:4] == ("SETUP.EXE", iso, None)
    assert game.cd == "cd/Game_CD.iso" and game.title == "Game Deluxe"


def test_install_folder_without_an_installer(m, home, flow, tmp_path):
    src = tmp_path / "Keen"
    touch(src / "KEEN1.EXE", b"k")
    touch(src / "EDITOR.EXE", b"e")
    ui = Ui()
    game = m.install.install(m.sources.classify([str(src)]), ui)
    # Two programs, none an installer: a DOS game as it is
    assert flow.drives == [] and game.title == "Keen Deluxe" and game.program in ("KEEN1.EXE", "EDITOR.EXE")
    assert (home.games / "Keen Deluxe/game/KEEN1.EXE").read_bytes() == b"k"
    assert not (home.games / "Keen Deluxe/c").exists()
    with pytest.raises(m.install.Cancelled):
        m.install.install(m.sources.classify([str(src)]), Ui(program=None))
    assert [p.name for p in home.games.iterdir()] == ["Keen Deluxe"]


def test_install_folder_with_its_installer(m, home, flow, tmp_path):
    src = tmp_path / "Game"
    touch(src / "INSTALL.EXE")
    m.install.install(m.sources.classify([str(src)]), Ui())
    assert flow.drives[0][1:4] == ("INSTALL.EXE", None, str(src))


def test_install_automatic_plan(m, home, flow, tmp_path, monkeypatch):
    # Duke Nukem's disk: self-extracting archives, no installer, no questions
    img = fat_image(tmp_path / "duke.img", {"DUKEZIP1.EXE": sfx()})
    flow.installs_files = {"DUKE1/DN1.EXE": b"MZ"}
    monkeypatch.setattr(m.presets, "load", lambda log=print: {"schema": 1, "games": [
        {"id": "duke", "title": "Duke Nukem", "source": ["DUKEZIP1.EXE"], "program": "DUKE1\\DN1.EXE"}]})
    ui = Ui()
    game = m.install.install(m.sources.classify([img]), ui)
    plan = flow.installs[0][3]
    assert plan.automatic and plan.title == "Duke Nukem"
    assert ui.asked == [] and "Installing Duke Nukem" in ui.statuses
    assert game == m.library.Game("Duke Nukem", "dosbox", home.games / "Duke Nukem", [], "DUKE1",
                                  "DN1.EXE")


def test_install_errors_remove_the_staging_folder(m, home, flow, tmp_path, monkeypatch):
    src = make_zip(tmp_path / "evil.zip", {"../x": b"1"})
    with pytest.raises(m.sources.SourceError):
        m.install.install(m.sources.classify([src]))
    assert list(home.games.iterdir()) == []


def test_install_helpers(m, tmp_path, capsys):
    ins = m.install
    touch(tmp_path / "a/b/c/GAME.EXE")
    assert ins._unwrap(str(tmp_path / "a")) == str(tmp_path / "a/b/c")
    touch(tmp_path / "a/b/c/x/y/z/DEEP.DAT")
    touch(tmp_path / "a/b/c/x/Mid.dat", b"mid")
    files = ins._FolderFiles(str(tmp_path / "a/b/c"))
    assert sorted(files) == ["GAME.EXE", "X/MID.DAT"] and len(files) == 2
    assert files["X/MID.DAT"] == b"mid"
    (tmp_path / "a/b/c/SUB").mkdir()
    assert ins._listing(str(tmp_path / "a/b/c")) == {"GAME.EXE": 1}
    assert ins._short("Day of the Tentacle (CD/DOS/English)") == "Day of the Tentacle"
    assert ins._short("(DOS)") == "(DOS)"
    d = m.scummvm.Detection
    assert ins._main([d("e", "a", "A", "/g/x/y"), d("e", "b", "B", "/g/x")]).gameid == "b"
    ui = ins.Ui()
    assert ui.before_installer("INSTALL.EXE") is True and ui.floppies(None) is None
    assert ui.choose_program([("", "A.EXE")], "T") == (("", "A.EXE"), "T")
    assert ui.choose_program([], "T") is None
    ui.log("hello")
    assert capsys.readouterr().out.endswith(" hello\n")


# ----- the command line -----

def test_command_line(m, monkeypatch, capsys):
    main = m.main
    calls = []
    game = m.library.Game("Doom", "dosbox", Path("/g/Doom"), [])
    monkeypatch.setattr(main, "install", lambda source: calls.append(("install", source)) or game)
    monkeypatch.setattr(main, "classify", lambda paths: ("source", paths))
    codes = iter([0, 5])
    monkeypatch.setattr(main, "install_from_floppies",
                        lambda images, target, program, log: (calls.append(
                            ("floppies", images, target, program)) or next(codes), None))
    results = iter([["t"], ["t"], []])
    monkeypatch.setattr(main, "finish", lambda target, log: calls.append(("finish", target))
                        or next(results))
    for argv, code in ((["install", "a.img", "b.img"], 0),
                       (["floppies", "--target", "/t", "--program", "GO.BAT", "a.img"], 0),
                       (["floppies", "--target", "/t", "a.img"], 5),
                       (["finish", "--target", "/t"], 0), (["finish", "--target", "/t"], 1)):
        monkeypatch.setattr(sys, "argv", ["dosinstall", *argv])
        assert main.main() == code
    assert calls == [("install", ("source", ["a.img", "b.img"])),
                     ("floppies", ["a.img"], "/t", "GO.BAT"), ("finish", "/t"),
                     ("floppies", ["a.img"], "/t", None), ("finish", "/t"), ("finish", "/t")]
    assert "installed: Doom (dosbox) in /g/Doom, targets []" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["dosinstall"])
    with pytest.raises(SystemExit):
        main.main()


def test_launcher_script_plays_a_game(m, home, monkeypatch):
    folder = home.games / "Doom"
    folder.mkdir(parents=True)
    game = m.library.Game("Doom", "dosbox", folder, [], "", "DOOM.EXE")
    m.library.save(game)
    played = []
    monkeypatch.setattr(m.dosgames, "play",
                        lambda g, setup=False: played.append((g, setup)) or 3)
    monkeypatch.setattr(sys, "path", list(sys.path))
    key = m.dosgames._app_id(game)[len(m.dosgames.PREFIX):]
    monkeypatch.setattr(sys, "argv", ["plinius-dos-installer", "--play", key])
    with pytest.raises(SystemExit) as e:
        load_script(DOS / "plinius-dos-installer", "plinius_dos_installer_play")
    assert e.value.code == 3 and played == [(game, False)]
    # The game's setup, from its page in the window
    monkeypatch.setattr(sys, "argv", ["plinius-dos-installer", "--setup", key])
    with pytest.raises(SystemExit) as e:
        load_script(DOS / "plinius-dos-installer", "plinius_dos_installer_play")
    assert played[-1] == (game, True)
    monkeypatch.setattr(sys, "argv", ["plinius-dos-installer", "--play", "nothing"])
    with pytest.raises(SystemExit) as e:
        load_script(DOS / "plinius-dos-installer", "plinius_dos_installer_play")
    assert e.value.code == "no such game: nothing"


# ----- plinius-scummvm-launchers -----

@pytest.fixture
def launchers(tmp_path, monkeypatch):
    mod = load_script(DOS / "plinius-scummvm-launchers", "plinius_scummvm_launchers_under_test")
    data = tmp_path / "data"
    monkeypatch.setattr(mod, "INI", tmp_path / "config/scummvm/scummvm.ini")
    monkeypatch.setattr(mod, "SYSTEM_PACKS", tmp_path / "usr-share-scummvm")
    monkeypatch.setattr(mod, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(mod, "APPS", data / "applications")
    monkeypatch.setattr(mod, "ICONS", data / "plinius-dos-installer/scummvm-icons")
    monkeypatch.setattr(mod, "FETCHED", data / "plinius-dos-installer/scummvm-icons-fetched")
    mod.INI.parent.mkdir(parents=True)
    return mod


def pack(path, members):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return path


def desktop(path):
    return dict(line.split("=", 1) for line in Path(path).read_text().splitlines()[1:])


def test_launchers_for_every_game(launchers, capsys):
    L = launchers
    L.INI.write_text(
        "[scummvm]\nversioninfo=2.8\n\n[tentacle]\ngameid=tentacle\nengineid=scumm\n"
        "description=Day of the Tentacle (CD/DOS/English)\n\n"
        "[monkey-de]\ngameid=monkey\nengineid=scumm\ndescription=Monkey Island (VGA/DOS/German)\n"
        "[monkey-en]\ngameid=monkey\nengineid=scumm\ndescription=Monkey Island (VGA/DOS/English)\n"
        "[bad name;rm]\ngameid=x\n[nogameid]\npath=/x\n[plain]\ngameid=plain\n")
    assert L.main() == 0
    apps = L.APPS
    assert sorted(p.name for p in apps.iterdir()) == [
        "org.plinius.ScummVM.monkey-de.desktop", "org.plinius.ScummVM.monkey-en.desktop",
        "org.plinius.ScummVM.plain.desktop", "org.plinius.ScummVM.tentacle.desktop"]
    t = desktop(apps / "org.plinius.ScummVM.tentacle.desktop")
    assert t["Name"] == "Day of the Tentacle" and t["Exec"] == "scummvm tentacle"
    assert t["Icon"] == "org.scummvm.scummvm" and t[L.MARKER] == "tentacle"
    # The same game twice: the full descriptions tell them apart
    assert desktop(apps / "org.plinius.ScummVM.monkey-de.desktop")["Name"] == \
        "Monkey Island (VGA/DOS/German)"
    assert desktop(apps / "org.plinius.ScummVM.plain.desktop")["Name"] == "plain"
    out = capsys.readouterr()
    assert "skipping target with unusual name: 'bad name;rm'" in out.err
    assert out.out == "4 games, 4 files changed\n"
    # Nothing changed: nothing written
    mtime = (apps / "org.plinius.ScummVM.plain.desktop").stat().st_mtime_ns
    assert L.main() == 0 and capsys.readouterr().out == "4 games, 0 files changed\n"
    assert (apps / "org.plinius.ScummVM.plain.desktop").stat().st_mtime_ns == mtime
    # A game removed in ScummVM loses its launcher; launchers not written here stay
    (apps / "org.plinius.ScummVM.mine.desktop").write_text("[Desktop Entry]\nName=Mine\n")
    (apps / "org.plinius.ScummVM.tentacle.desktop").write_text("[Desktop Entry]\nName=Mine\n")
    (apps / "other.desktop").write_text("x")
    L.INI.write_text("[plain]\ngameid=plain\n[tentacle]\ngameid=tentacle\n")
    assert L.main() == 0
    assert sorted(p.name for p in apps.iterdir()) == [
        "org.plinius.ScummVM.mine.desktop", "org.plinius.ScummVM.plain.desktop",
        "org.plinius.ScummVM.tentacle.desktop", "other.desktop"]
    assert (apps / "org.plinius.ScummVM.tentacle.desktop").read_text() == \
        "[Desktop Entry]\nName=Mine\n"
    assert "leaving" in capsys.readouterr().err


def test_launchers_without_or_with_a_broken_ini(launchers, capsys):
    assert launchers.main() == 0 and not launchers.APPS.exists()
    launchers.INI.write_text("[tentacle\ngameid=x\n")
    assert launchers.main() == 1
    assert "cannot read" in capsys.readouterr().err


def test_launcher_icons(launchers, tmp_path, capsys):
    L = launchers
    pack(L.SYSTEM_PACKS / "gui-icons.dat", {"icons/scumm-tentacle.png": png(), "icons/scumm.png":
                                            png(2, 2), "icons/platforms/amiga.svg": b"<svg/>",
                                            "readme.txt": b"x"})
    (L.SYSTEM_PACKS / "gui-icons-broken.dat").write_bytes(b"no zip")
    L.INI.write_text("[scummvm]\niconspath=%s\n[tentacle]\ngameid=tentacle\nengineid=scumm\n"
                     "[maniac]\ngameid=maniac\nengineid=SCUMM\n"
                     "[agi]\ngameid=kq1\nengineid=agi\nplatform=Amiga\n"
                     "[big]\ngameid=big\nengineid=big\n" % (tmp_path / "icons-path"))
    # Downloaded packs come after ScummVM's own and win
    pack(tmp_path / "icons-path/gui-icons-full.dat", {"icons/big-big.png": png(512, 300),
                                                     "icons/scumm-tentacle.png": png(3, 3)})
    assert L.main() == 0
    assert "cannot read" in capsys.readouterr().err

    def icon_of(target):
        return Path(desktop(L.APPS / f"org.plinius.ScummVM.{target}.desktop")["Icon"])
    # Then the engine's icon; then the platform's
    assert icon_of("tentacle").read_bytes() == png(3, 3)
    assert icon_of("maniac").read_bytes() == png(2, 2)
    assert icon_of("agi").read_bytes() == b"<svg/>" and icon_of("agi").suffix == ".svg"
    # A large icon is stored scaled down
    import gi
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf
    pixbuf = GdkPixbuf.Pixbuf.new_from_file(str(icon_of("big")))
    assert (pixbuf.get_width(), pixbuf.get_height()) == (256, 150)
    # A fetched icon wins over the packs, and the old icon file goes
    old = icon_of("tentacle")
    L.FETCHED.mkdir(parents=True)
    (L.FETCHED / "scumm-tentacle.png").write_bytes(png(4, 4))
    (L.FETCHED / "notes.txt").write_text("x")
    assert L.main() == 0
    assert icon_of("tentacle").read_bytes() == png(4, 4) and not old.exists()
    assert icon_of("tentacle").name.startswith("org.plinius.ScummVM.tentacle-")


def test_launcher_helpers(launchers, capsys):
    L = launchers
    assert L.desktop_value("a\\b\nc\r\td") == "a\\\\b\\nc\\td"
    assert L.shrink_icon(b"<svg/>", ".svg") == b"<svg/>"
    assert L.shrink_icon(b"broken", ".png") == b"broken"
    assert "cannot scale an icon down" in capsys.readouterr().err
    small = png(10, 10)
    assert L.shrink_icon(small, ".png") == small
    assert L.icon_packs({}) == []
    assert L.short_name("t", {"description": "(DOS)"}) == "(DOS)"
    assert L.full_name("t", {}) == "t"
    assert L.is_ours(L.APPS / "missing.desktop") is False
    assert L.pick_icon({}, {"gameid": "x"}) == (None, None)


def test_launcher_icon_packs_folder_unreadable(launchers, monkeypatch):
    def broken(self, pattern):
        raise PermissionError("no access")
    monkeypatch.setattr(type(launchers.SYSTEM_PACKS), "glob", broken)
    assert launchers.icon_packs(None) == []


# ----- run as programs -----

def test_module_entry_points(m, home, monkeypatch, tmp_path, capsys):
    import runpy
    # python3 -m dosinstall finish: the exit status says whether ScummVM took anything
    monkeypatch.setattr(m.finish, "finish", lambda target, log: [])
    monkeypatch.setattr(sys, "argv", ["dosinstall", "finish", "--target", str(tmp_path)])
    # Run afresh as __main__, as python3 -m does (the imported modules stay as they are)
    monkeypatch.delitem(sys.modules, "dosinstall.__main__")
    monkeypatch.delitem(sys.modules, "dosinstall.presets")
    with pytest.raises(SystemExit) as e:
        runpy.run_module("dosinstall.__main__", run_name="__main__")
    assert e.value.code == 1
    # python3 -m dosinstall.presets --offline: which list would be used, without a network
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(sys, "argv", ["presets", "--offline"])
    runpy.run_module("dosinstall.presets", run_name="__main__")
    out = capsys.readouterr().out.splitlines()
    assert json.loads(out[-1]) == {"updated": 1794996000, "games": ["duke-nukem-1"]}


def test_launchers_as_a_program_follow_xdg(tmp_path, monkeypatch, capsys):
    import runpy
    for var in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
        monkeypatch.setenv(var, str(tmp_path / var.lower()))
    ini = tmp_path / "xdg_config_home/scummvm/scummvm.ini"
    ini.parent.mkdir(parents=True)
    ini.write_text("[plinius-test]\ngameid=plinius-test\nengineid=plinius-test\n")
    with pytest.raises(SystemExit) as e:
        runpy.run_path(str(DOS / "plinius-scummvm-launchers"), run_name="__main__")
    assert e.value.code == 0
    launcher = tmp_path / "xdg_data_home/applications/org.plinius.ScummVM.plinius-test.desktop"
    assert "Exec=scummvm plinius-test\n" in launcher.read_text()
