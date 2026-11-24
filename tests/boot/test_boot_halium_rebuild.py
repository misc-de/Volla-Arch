# halium-rebuild of plinius-crypt: decides what goes into vendor_boot and init_boot of which slot,
# and when nothing may be written. It runs as a copy in tmp_path with every fixed system path
# pointed into tmp_path, against partitions that are plain files (PLINIUS_CRYPT_PARTITIONS). dd is
# a guard that refuses any path outside tmp_path; mount, debugfs, systemctl and the like are stubs
# that fail, and the tests check they were never called.

import contextlib
import io
import random
import re
import shutil
import struct
import subprocess

import pytest

from boot_helpers import (PAGE, BootImageServer, boot_image_namespace, init_boot, lvgl_xrgb,
                          lz4_legacy, need, newc, parse_newc, unlz4, ut_init_ramdisk, vendor_boot,
                          write, zstd_pipe)
from conftest import PKG

SCRIPT = PKG / "plinius-crypt" / "halium-rebuild"
MiB = 1 << 20
# Small partitions: vendor_boot keeps 1 MiB at its end free, the images here are some 60 kB
VENDOR_PART = MiB + 512 * 1024
INIT_PART = 256 * 1024
PLATFORM = random.Random(1).randbytes(20000)
RECOVERY = b"RECOVERY" * 200
HOOK = b"#!/usr/bin/ash\n# a comment the ramdisk has no room for\nrun_hook() { :; }\n"
UNLOCK = b"#!/bin/sh\ncat <<-EOF\n# kept: here-document\nEOF\n"
BI = boot_image_namespace()
FORBIDDEN = ("mount", "umount", "debugfs", "blockdev", "systemctl", "plinius-bootcheck")


def rewritten_script(t):
    """halium-rebuild with its fixed system paths pointed into t."""
    text = SCRIPT.read_text()
    for old, new in (("/var/lib/plinius-crypt/halium", t / "default-conf"),
                     ("/usr/share/plinius-boot", t / "default-boot"),
                     ("/usr/lib/plinius-crypt/ramdisk", t / "default-parts"),
                     ("/var/lib/plinius-kernel/trial", t / "kernel/trial"),
                     ("/dev/disk/by-partlabel", t / "default-partitions"),
                     ("/dev/disk/by-partuuid/", f"{t}/by-partuuid/"),
                     ("/proc/cmdline", t / "default-cmdline"),
                     ("/proc/bootconfig", t / "bootconfig"),
                     ("/vendor/firmware/haptic_ram.bin", t / "vendor-fw/haptic_ram.bin"),
                     ("/run/", f"{t}/run/")):
        text = text.replace(old, str(new))
    # Whatever absolute path is left in the code may only be read, and only these
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    left = set(re.findall(r'(?<![\w.$}/-])/(?:dev|proc|sys|run|var|usr|etc|vendor|boot|mnt|lib)'
                          r'\b[^\s"\')|;]*', code))
    assert left <= {"/dev/zero", "/dev/null", "/dev/mmcblk*"}, left
    return text


class Rig:
    """The phone's state in tmp_path: UT's images, the offered parts, both slots' partitions."""

    def __init__(self, t, fake_bin, server):
        self.t, self.fake_bin = t, fake_bin
        self.lib = t / "lib"
        self.lib.mkdir()
        (self.lib / "halium-rebuild").write_text(rewritten_script(t))
        # boot-image through the fork server (boot_helpers.BootImageServer): the same script,
        # without a new Python for each of its some 20 runs per build
        server.client_into(self.lib / "boot-image")
        write(self.lib / "look-files",
              b'#!/bin/bash\nset -e\nmkdir -p "$1/etc/unl0kr.conf.d"\n'
              b'printf "[theme]\\naccent=#123456\\n" > "$1/etc/unl0kr.conf.d/60-plinius-look.conf"\n'
              b'[[ -z ${FAKE_WALLPAPER:-} ]] || install -D -m644 "$FAKE_WALLPAPER" '
              b'"$1/usr/share/plinius-crypt/glass-background.bin"\n')
        for f in ("halium-rebuild", "boot-image", "look-files"):
            (self.lib / f).chmod(0o755)
        self.conf, self.boot, self.offered = t / "conf", t / "boot", t / "offered"
        self.parts, self.cmdline = t / "partitions", t / "cmdline"
        (t / "tmp").mkdir()
        self.conf.mkdir()
        write(self.conf / "target", b"vendor_boot\n")
        write(self.cmdline, b"console=ttyS0 androidboot.slot_suffix=_b quiet\n")
        self.ut_vendor = write(self.boot / "vendor_boot.img",
                               vendor_boot([(PLATFORM, 1), (RECOVERY, 2)]))
        self.ut_init_rd = ut_init_ramdisk()
        self.ut_init = write(self.boot / "init_boot.img",
                             init_boot(self.ut_init_rd, total=INIT_PART - 64 * 1024))
        # Larger than the old ones: the new image is longer than the one on the partition
        self.programs = write(self.offered / "programs.zst",
                              zstd_pipe(random.Random(2).randbytes(30000)))
        write(self.offered / "init.lz4",
              lz4_legacy(newc([("plinius", 0o40755, b""), ("plinius/hook", 0o100755, HOOK),
                               ("plinius/unlock", 0o100755, UNLOCK)])))
        write(self.offered / "versions", b"plinius-crypt 0.13-12\n")
        # What the partitions hold now: an image made from older programs
        old = write(t / "old-programs.zst", zstd_pipe(b"old programs " * 400))
        self.old_vendor = self.build_vendor(old)
        for s in ("_a", "_b"):
            self.set_part("vendor_boot" + s, self.old_vendor, VENDOR_PART)
            self.set_part("init_boot" + s, self.ut_init.read_bytes(), INIT_PART)
        self.before = {p.name: p.read_bytes() for p in self.parts.iterdir()}
        self.stub_programs()

    def build_vendor(self, programs, ut=None):
        out = self.t / "built-vendor"
        with contextlib.redirect_stdout(io.StringIO()):
            BI["build_vendor"](str(ut or self.ut_vendor), str(out), str(programs))
        return out.read_bytes()

    def set_part(self, name, image, size, tail=b""):
        data = image.ljust(size - len(tail), b"\0") + tail
        assert len(data) == size
        write(self.parts / name, data)

    def part(self, name):
        return (self.parts / name).read_bytes()

    def stub_programs(self):
        fb, t = self.fake_bin, self.t
        real_dd = shutil.which("dd")
        # dd only on files below the test directory; "corrupt-next-write" spoils one write, as a
        # partition that does not read back as written
        fb.stub("dd", f'''
for a in "$@"; do
  case $a in if=*|of=*)
    p=$(realpath -m -- "${{a#*=}}")
    case $p in "{t}"/*) ;; *) echo "dd: refused, outside the test: $p" >&2; exit 99 ;; esac ;;
  esac
done
if [[ -e "{t}/corrupt-next-write" && " $* " == *conv=notrunc* ]]; then
  command rm -f "{t}/corrupt-next-write"
  "{real_dd}" "$@" || exit
  for a in "$@"; do [[ $a == of=* ]] && of=${{a#of=}}; done
  printf '\\xab' | "{real_dd}" of="$of" bs=1 seek=100 conv=notrunc status=none
  exit 0
fi
exec "{real_dd}" "$@"''')
        fb.stub("findmnt", '[[ -n ${FAKE_USERDATA:-} ]] || exit 1\n'
                           '[[ " $* " == *" SOURCE "* ]] && echo "$FAKE_USERDATA"; exit 0')
        fb.stub("sync")
        for name in FORBIDDEN:
            fb.stub(name, "exit 1")
        fb.stub("lsblk", '[[ -n ${FAKE_LSBLK:-} ]] || exit 1\nprintf "%b" "$FAKE_LSBLK"')

    def run(self, *args, check=None, **env):
        settings = dict(PLINIUS_CRYPT_HALIUM=self.conf, PLINIUS_CRYPT_BOOT_FILES=self.boot,
                        PLINIUS_CRYPT_PARTS=self.offered, PLINIUS_CRYPT_PARTITIONS=self.parts,
                        PLINIUS_CRYPT_CMDLINE=self.cmdline,
                        PLINIUS_CRYPT_KERNEL_TRIAL=self.t / "kernel/trial",
                        PLINIUS_CRYPT_UP="yes", PLINIUS_CRYPT_KERNEL_WAIT=0, TMPDIR=self.t / "tmp")
        settings.update(env)
        e = self.fake_bin.env(isolated=True, **settings)
        r = subprocess.run(["bash", str(self.lib / "halium-rebuild"), *map(str, args)], env=e,
                           capture_output=True, text=True, timeout=60)
        self.last = r
        if check is not None:
            assert (r.returncode == 0) == check, r.stderr
        for name in FORBIDDEN:
            assert self.fake_bin.calls(name) == [], f"{name} was called"
        assert not any("refused, outside the test" in line for line in r.stderr.splitlines())
        return r

    def writes(self):
        """The dd calls that wrote into a partition."""
        return [c for c in self.fake_bin.calls("dd")
                if any(a.startswith(f"of={self.parts}") for a in c)]

    def unchanged(self):
        return {p.name: p.read_bytes() for p in self.parts.iterdir()} == self.before


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    need("lz4")
    s = BootImageServer(tmp_path_factory.mktemp("boot-image-server"))
    yield s
    s.stop()


@pytest.fixture
def rig(tmp_path, fake_bin, server):
    need("lz4", "zstd", "flock")
    rig = Rig(tmp_path, fake_bin, server)
    yield rig
    # /tmp is a tmpfs: the partitions and the kept images are the bulk of what a test leaves
    shutil.rmtree(rig.parts, ignore_errors=True)
    shutil.rmtree(rig.conf, ignore_errors=True)


def vendor_ramdisk(data):
    """The one ramdisk of a Plinius vendor_boot (in-process, the tests run boot-image often
    enough through halium-rebuild)."""
    p = BI["layout"](data[:PAGE])
    assert BI["vendor_kind"](data, p) == "plinius"
    return data[p["first"]:p["first"] + p["ramdisks"]]


def look_files(rd):
    """The newc entries of the archives behind UT's platform ramdisk (firmware, look)."""
    return {n: b for n, _, b in parse_newc(unlz4(rd[rd.index(PLATFORM) + len(PLATFORM):]))}


def init_files(rig, data):
    """The newc entries of an init_boot image's ramdisk (all its lz4 archives)."""
    size = struct.unpack_from("<I", data, 12)[0]
    return {n: b for n, _, b in parse_newc(unlz4(data[PAGE:PAGE + size]))}


# --- writing new parts ---------------------------------------------------------------------------

def footer(vbmeta_offset):
    """An AVB footer (64 bytes at the end of a partition) naming where its vbmeta block starts."""
    return (b"AVBf" + struct.pack(">II", 1, 0)
            + struct.pack(">QQQ", 1, vbmeta_offset, 64)).ljust(64, b"\0")


def test_update_writes_both_images_of_the_active_slot(rig):
    fw = write(rig.t / "fw/haptic_ram.bin", b"HAPTIC" * 100)
    # A vbmeta block far behind the image; the slot named on the command line wins over the
    # remembered one; a higher lk limit from the environment is ignored
    rig.set_part("vendor_boot_b", rig.old_vendor, VENDOR_PART, tail=footer(MiB + 256 * 1024))
    rig.before = {p.name: p.read_bytes() for p in rig.parts.iterdir()}
    write(rig.conf / "slot", b"_a\n")
    rig.run("--update", check=True, PLINIUS_CRYPT_HAPTIC_FW=fw, PLINIUS_CRYPT_MAX_RAMDISK=10 ** 12)
    # Slot _a stays as it was
    assert rig.part("vendor_boot_a") == rig.before["vendor_boot_a"]
    assert rig.part("init_boot_a") == rig.before["init_boot_a"]
    # vendor_boot_b: the new programs in front of UT's platform ramdisk, then firmware and look
    vb = rig.part("vendor_boot_b")
    rd = vendor_ramdisk(vb)
    assert rd.startswith(rig.programs.read_bytes())
    assert PLATFORM in rd and RECOVERY not in rd
    look = look_files(rd)
    assert look["./lib/firmware/haptic_ram.bin"] == b"HAPTIC" * 100
    assert look["./etc/unl0kr.conf.d/60-plinius-look.conf"] == b"[theme]\naccent=#123456\n"
    # init_boot_b: UT's ramdisk, then the scripts with the unlock call, then the slimmed parts
    ib = rig.part("init_boot_b")
    assert ib[PAGE:PAGE + len(rig.ut_init_rd)] == rig.ut_init_rd
    files = init_files(rig, ib)
    assert "/plinius/unlock" in files["./scripts/halium"].decode()
    assert files["plinius/hook"] == b"#!/usr/bin/ash\nrun_hook() { :; }\n"
    assert files["plinius/unlock"] == UNLOCK
    assert "./etc/unl0kr.conf.d/60-plinius-look.conf" not in files
    # Only the image was written: the rest of both partitions is as before (the footer too)
    vlen = BI["layout"](vb[:PAGE])["end"]
    assert vlen > len(rig.old_vendor)
    assert vb[vlen:] == rig.before["vendor_boot_b"][vlen:]
    assert len(ib) == INIT_PART
    # The old images are kept, the parts confirmed with their own boot-image
    assert (rig.conf / "init_boot_b.prev").read_bytes() == rig.ut_init.read_bytes()
    assert (rig.conf / "vendor_boot_b.prev").read_bytes() == rig.old_vendor
    for f in ("programs.zst", "init.lz4", "versions"):
        assert (rig.conf / "confirmed" / f).read_bytes() == (rig.offered / f).read_bytes()
    assert (rig.conf / "confirmed/boot-image").read_bytes() == (rig.lib / "boot-image").read_bytes()
    assert (rig.conf / "slot").read_text() == "_b\n"
    # init_boot is written before vendor_boot
    order = [next(a for a in c if a.startswith("of=")) for c in rig.writes()]
    assert order == [f"of={rig.parts}/init_boot_b", f"of={rig.parts}/vendor_boot_b"]

    # The same once more: nothing to write
    first = {p.name: p.read_bytes() for p in rig.parts.iterdir()}
    rig.run("--update", check=True, PLINIUS_CRYPT_HAPTIC_FW=fw)
    assert "in use already" in rig.last.stderr and "up to date" in rig.last.stderr
    assert len(rig.writes()) == 2
    assert {p.name: p.read_bytes() for p in rig.parts.iterdir()} == first


def test_remembered_slot_without_one_on_the_cmdline(rig):
    # A start from a card entry has no bootconfig and no slot on its command line
    write(rig.cmdline, b"console=ttyS0\n")
    write(rig.conf / "slot", b"_a\n")
    rig.run("--update", check=True)
    assert rig.part("vendor_boot_b") == rig.before["vendor_boot_b"]
    assert rig.part("init_boot_b") == rig.before["init_boot_b"]
    assert rig.part("vendor_boot_a") != rig.before["vendor_boot_a"]
    assert rig.part("init_boot_a") != rig.before["init_boot_a"]


def test_unknown_slot_writes_nothing(rig):
    write(rig.cmdline, b"androidboot.slot_suffix=_c console=ttyS0\n")
    rig.run("--update", check=False)
    assert "active slot is unknown" in rig.last.stderr
    assert rig.unchanged() and rig.writes() == []


# --- refusals: nothing is written ----------------------------------------------------------------

def test_vendor_boot_of_another_ut_build_is_left_alone(rig):
    other = write(rig.t / "other-ut", vendor_boot([(b"OTHER-PLATFORM" * 900, 1), (RECOVERY, 2)]))
    old = write(rig.t / "p.zst", zstd_pipe(b"x" * 99))
    rig.set_part("vendor_boot_b", rig.build_vendor(old, ut=other), VENDOR_PART)
    rig.before = {p.name: p.read_bytes() for p in rig.parts.iterdir()}
    rig.run("--update", check=False)
    assert "is not of the UT build" in rig.last.stderr
    assert rig.unchanged() and rig.writes() == []
    assert not (rig.conf / "confirmed").exists()


def test_vendor_boot_with_uts_two_entries_is_left_alone(rig):
    rig.set_part("vendor_boot_b", rig.ut_vendor.read_bytes(), VENDOR_PART)
    rig.before = {p.name: p.read_bytes() for p in rig.parts.iterdir()}
    rig.run("--update", check=False)
    assert "no vendor_boot with the one ramdisk of Plinius" in rig.last.stderr
    assert rig.unchanged() and rig.writes() == []


def test_init_boot_of_another_ut_build_is_left_alone(rig):
    rig.set_part("init_boot_b", init_boot(ut_init_ramdisk(extra=b"other build"),
                                          total=INIT_PART - 64 * 1024), INIT_PART)
    rig.before = {p.name: p.read_bytes() for p in rig.parts.iterdir()}
    rig.run("--update", check=False)
    assert "init_boot_b is not of the UT build" in rig.last.stderr
    assert rig.unchanged() and rig.writes() == []


def test_init_boot_that_is_no_image_is_left_alone(rig):
    rig.set_part("init_boot_b", b"\0" * PAGE, INIT_PART)
    rig.before = {p.name: p.read_bytes() for p in rig.parts.iterdir()}
    rig.run("--update", check=False)
    assert "not an init_boot image" in rig.last.stderr
    assert rig.unchanged() and rig.writes() == []


def test_missing_ut_images(rig):
    (rig.boot / "vendor_boot.img").unlink()
    rig.run("--update", check=False)
    assert "UT's vendor_boot.img and init_boot.img are in neither" in rig.last.stderr
    assert rig.unchanged()


def test_ut_images_in_the_ut_subdirectory(rig):
    # The image keeps the Plinius images in boot_files for Plinius Boot, UT's in ut/
    (rig.boot / "ut").mkdir()
    shutil.move(rig.boot / "vendor_boot.img", rig.boot / "ut/vendor_boot.img")
    shutil.move(rig.boot / "init_boot.img", rig.boot / "ut/init_boot.img")
    write(rig.boot / "vendor_boot.img", rig.old_vendor)  # a Plinius image: not UT's
    write(rig.boot / "init_boot.img", b"junk")
    rig.run("--update", check=True)
    assert PLATFORM in vendor_ramdisk(rig.part("vendor_boot_b"))


def test_ramdisk_too_large_for_lk(rig):
    rig.run("--update", check=False, PLINIUS_CRYPT_MAX_RAMDISK=10000)
    assert "is larger than" in rig.last.stderr
    assert rig.unchanged() and rig.writes() == []
    assert not (rig.conf / "confirmed").exists()


def ramdisk_size(stderr):
    return int(re.search(r"ramdisk (\d+) bytes \(look", stderr).group(1))


def test_wallpaper_is_left_out_when_the_ramdisk_would_not_fit(rig):
    wallpaper = write(rig.t / "wallpaper.bin",
                      lvgl_xrgb(108, 240, lambda x, y: tuple(random.Random(x * 999 + y)
                                                             .randbytes(3))))
    shutil.copytree(rig.offered, rig.conf / "confirmed")
    out = rig.t / "dry.img"
    rig.run("--output", out, check=True, FAKE_WALLPAPER=wallpaper)
    with_wallpaper = ramdisk_size(rig.last.stderr)
    assert "./usr/share/plinius-crypt/glass-background.bin" in look_files(
        vendor_ramdisk(out.read_bytes()))
    # One byte short of the room with the wallpaper and the bootconfig
    rig.run("--output", out, check=True, FAKE_WALLPAPER=wallpaper,
            PLINIUS_CRYPT_MAX_RAMDISK=with_wallpaper + 1024 - 1)
    assert "without it, the prompt shows the default one" in rig.last.stderr
    assert ramdisk_size(rig.last.stderr) < with_wallpaper - 1000
    look = look_files(vendor_ramdisk(out.read_bytes()))
    assert "./usr/share/plinius-crypt/glass-background.bin" not in look
    assert "./etc/unl0kr.conf.d/60-plinius-look.conf" in look
    assert rig.writes() == []


def test_image_would_overwrite_the_vbmeta_block(rig):
    # The vbmeta block right behind the old image; the new one is longer
    rig.set_part("vendor_boot_b", rig.old_vendor, VENDOR_PART, tail=footer(len(rig.old_vendor)))
    rig.before = {p.name: p.read_bytes() for p in rig.parts.iterdir()}
    rig.run("--update", check=False)
    assert "would overwrite the vbmeta block" in rig.last.stderr
    assert rig.part("vendor_boot_b") == rig.before["vendor_boot_b"]
    assert not (rig.conf / "confirmed").exists()


def test_image_must_leave_the_reserve_free(rig):
    # vendor_boot keeps 1 MiB at its end free: a partition only that much larger than the old
    # image has no room for the longer new one
    rig.set_part("vendor_boot_b", rig.old_vendor, len(rig.old_vendor) + MiB)
    rig.before = {p.name: p.read_bytes() for p in rig.parts.iterdir()}
    rig.run("--update", check=False)
    assert "does not fit into" in rig.last.stderr
    assert rig.part("vendor_boot_b") == rig.before["vendor_boot_b"]
    assert not (rig.conf / "confirmed").exists()


def test_failed_write_puts_the_old_image_back(rig):
    write(rig.t / "corrupt-next-write", b"")
    rig.run("--update", check=False)
    assert "does not read back as written, writing the old image back" in rig.last.stderr
    assert "init_boot_b unchanged" in rig.last.stderr
    assert rig.unchanged()
    assert not (rig.conf / "confirmed").exists()


def test_partitions_missing(rig):
    (rig.parts / "init_boot_b").unlink()
    rig.run("--update", check=False)
    assert rig.part("vendor_boot_b") == rig.before["vendor_boot_b"]
    assert rig.writes() == []


# --- trials and their state ----------------------------------------------------------------------

def test_failed_parts_are_not_tried_again_without_retry(rig):
    rig.run("--status", check=True)
    offered = re.search(r"offered: +(\S+)", rig.last.stdout).group(1)
    write(rig.conf / "failed", (offered + "\n").encode())
    rig.run("--update", check=True)
    assert "did not start in their trial" in rig.last.stderr
    assert rig.unchanged() and rig.writes() == []
    rig.run("--retry", check=True)
    assert rig.part("vendor_boot_b") != rig.before["vendor_boot_b"]
    assert not (rig.conf / "failed").exists()


def test_kernel_trial_makes_the_parts_wait_until_it_ended(rig, fake_bin):
    write(rig.t / "kernel/trial", b"1")
    rig.run("--update", check=True)
    assert "a new kernel is on trial" in rig.last.stderr
    assert (rig.conf / "waiting").exists()
    assert rig.unchanged() and rig.writes() == []
    # Still on trial at the next start: the start waits a while (no real sleep here), then gives up
    fake_bin.stub("sleep")
    rig.run("--boot", check=True, PLINIUS_CRYPT_KERNEL_WAIT=10)
    assert "has not ended" in rig.last.stderr
    assert len(fake_bin.calls("sleep")) == 2
    assert rig.unchanged() and (rig.conf / "waiting").exists()
    # Kernel trial over: the parts follow
    (rig.t / "kernel/trial").unlink()
    rig.run("--boot", check=True)
    assert not (rig.conf / "waiting").exists()
    assert rig.part("vendor_boot_b") != rig.before["vendor_boot_b"]


def make_trial(rig, slot="_a"):
    """A trial as --update sets it up on a phone with a card: parts, images, slot."""
    shutil.copytree(rig.offered, rig.conf / "confirmed")
    out = rig.t / "trial-vendor.img"
    rig.run("--output", out, check=True)
    shutil.rmtree(rig.conf / "confirmed")
    trial = rig.conf / "trial"
    shutil.copytree(rig.offered, trial)
    shutil.copy(rig.lib / "boot-image", trial / "boot-image")
    shutil.copy(out, trial / "vendor_boot.img")
    shutil.copy(f"{out}.init_boot", trial / "init_boot.img")
    write(trial / "slot", f"{slot}\n".encode())
    write(trial / "kernel", b"")
    return out


def test_boot_confirms_a_trial_that_came_up(rig):
    image = make_trial(rig, "_a").read_bytes()
    # A start from the card's entry has no slot on its command line; the remembered one is _a
    write(rig.cmdline, b"console=ttyS0 plinius.ramdisk=trial\n")
    write(rig.conf / "slot", b"_a\n")
    rig.run("--boot", check=True)
    assert "confirmed and written to vendor_boot_a and init_boot_a" in rig.last.stderr
    assert rig.part("vendor_boot_a")[:len(image)] == image
    assert rig.part("vendor_boot_b") == rig.before["vendor_boot_b"]
    assert rig.part("init_boot_b") == rig.before["init_boot_b"]
    assert not (rig.conf / "trial").exists()
    assert (rig.conf / "confirmed/programs.zst").read_bytes() == rig.programs.read_bytes()


def test_boot_keeps_a_trial_while_the_system_is_not_up(rig):
    make_trial(rig)
    write(rig.cmdline, b"plinius.ramdisk=trial\n")
    rig.run("--boot", check=True, PLINIUS_CRYPT_UP="no")
    assert "stays on trial" in rig.last.stderr
    assert (rig.conf / "trial").is_dir() and rig.unchanged()
    # A kernel on trial starts with the partitions' ramdisk: that says nothing about this trial
    write(rig.cmdline, b"plinius.kernel=trial plinius.ramdisk=trial\n")
    rig.run("--boot", check=True)
    assert (rig.conf / "trial").is_dir() and not (rig.conf / "failed").exists()
    assert rig.unchanged()
    # Started without the trial entry: the trial failed
    write(rig.cmdline, b"androidboot.slot_suffix=_b\n")
    rig.run("--boot", check=True)
    assert "was not confirmed" in rig.last.stderr
    assert not (rig.conf / "trial").exists()
    assert (rig.conf / "failed").read_text().strip()
    assert rig.unchanged() and rig.writes() == []


def test_output_is_a_dry_run(rig):
    shutil.copytree(rig.offered, rig.conf / "confirmed")
    out = rig.t / "dry.img"
    rig.run("--output", out, check=True)
    assert rig.unchanged() and rig.writes() == []
    assert vendor_ramdisk(out.read_bytes()).startswith(rig.programs.read_bytes())
    init = (rig.t / "dry.img.init_boot").read_bytes()
    assert init[PAGE:PAGE + len(rig.ut_init_rd)] == rig.ut_init_rd
    assert not (rig.conf / "slot").exists()


def test_status_and_usage(rig):
    rig.run("--status", check=True)
    assert re.search(r"^offered: +[0-9a-f]{16}$", rig.last.stdout, re.M)
    assert "confirmed: \n" in rig.last.stdout and "base:      none" in rig.last.stdout
    rig.run("--frobnicate", check=False)
    assert "usage" in rig.last.stderr


def test_not_a_halium_system(rig):
    (rig.conf / "target").unlink()
    rig.run("--update", check=True)
    assert "not started through the Halium ramdisk" in rig.last.stderr
    assert rig.unchanged()


def test_target_from_the_disk_under_userdata(rig):
    # No target file: the data partition in the phone's own storage (UFS, sdc) means vendor_boot;
    # a kernel trial stops it right there, before anything is put together
    (rig.conf / "target").unlink()
    write(rig.t / "kernel/trial", b"1")
    rig.run("--update", check=True, FAKE_USERDATA="/dev/mapper/plinius-data",
            FAKE_LSBLK="plinius-data\\n`-sdc32\\n  `-sdc\\n")
    assert "a new kernel is on trial" in rig.last.stderr
    # On the card it is a search for the card's entry, which --update cannot try
    rig.run("--update", check=False, FAKE_USERDATA="/dev/mmcblk0p3",
            FAKE_LSBLK="mmcblk0p3\\n`-mmcblk0\\n")
    assert "no trial of new parts for the target card-search" in rig.last.stderr
    assert rig.unchanged()


def test_bad_card_target_is_refused(rig):
    # The earlier way (a base) is the cheapest path to the card's checks
    write(rig.conf / "base", b"an initrd.cpio.gz")
    for target, text in ((b"card not-a-uuid rom3", "bad PARTUUID"),
                         (b"card 01234567-89ab-cdef-0123-456789abcdef ../x", "bad entry"),
                         (b"card 01234567-89ab-cdef-0123-456789abcdef rom3", "(no card?)")):
        write(rig.conf / "target", target + b"\n")
        rig.run(check=False)
        assert text in rig.last.stderr
    assert rig.unchanged()


# --- the earlier way: the look behind a base -----------------------------------------------------

def base_rig(rig):
    base_rd = b"CRYPT-RAMDISK-OF-OLD" * 900
    base = write(rig.conf / "base", vendor_boot([(base_rd, 1)]))
    rig.set_part("vendor_boot_b", base.read_bytes(), VENDOR_PART)
    rig.before = {p.name: p.read_bytes() for p in rig.parts.iterdir()}
    return base_rd


def test_look_behind_the_base(rig):
    base_rd = base_rig(rig)
    rig.run(check=True, PLINIUS_CRYPT_INIT_BOOT=rig.ut_init)
    rd = vendor_ramdisk(rig.part("vendor_boot_b"))
    assert rd.startswith(base_rd)
    files = {n for n, _, _ in parse_newc(unlz4(rd[len(base_rd):]))}
    assert "./etc/unl0kr.conf.d/60-plinius-look.conf" in files
    assert rig.part("init_boot_b") == rig.before["init_boot_b"]
    assert rig.part("vendor_boot_a") == rig.before["vendor_boot_a"]


def test_look_needs_a_vendor_boot_made_from_the_base(rig):
    base_rig(rig)
    rig.set_part("vendor_boot_b", vendor_boot([(b"SOMETHING ELSE" * 99, 1)]), VENDOR_PART)
    rig.before = {p.name: p.read_bytes() for p in rig.parts.iterdir()}
    rig.run(check=False, PLINIUS_CRYPT_INIT_BOOT=rig.ut_init)
    assert "was not made from" in rig.last.stderr
    assert rig.unchanged() and rig.writes() == []


def test_look_counts_init_boots_ramdisk_too(rig):
    base_rd = base_rig(rig)
    limit = len(base_rd) + len(rig.ut_init_rd) + 1024 + 50  # no room for the look archive
    rig.run(check=False, PLINIUS_CRYPT_INIT_BOOT=rig.ut_init, PLINIUS_CRYPT_MAX_RAMDISK=limit)
    assert "is larger than" in rig.last.stderr
    assert rig.unchanged()


def test_look_refuses_a_base_cut_short(rig):
    base = vendor_boot([(b"CRYPT-RAMDISK-OF-OLD" * 900, 1)], bootconfig=b"androidboot.x=y\n" * 9)
    rig.set_part("vendor_boot_b", base, VENDOR_PART)
    write(rig.conf / "base", base[:len(base) - PAGE + 10])
    rig.before = {p.name: p.read_bytes() for p in rig.parts.iterdir()}
    rig.run(check=False, PLINIUS_CRYPT_INIT_BOOT=rig.ut_init)
    assert rig.unchanged()


def test_look_with_confirmed_parts_uses_them(rig):
    shutil.copytree(rig.offered, rig.conf / "confirmed")
    confirmed = rig.programs.read_bytes()
    # Offered parts changed meanwhile: a change of the look keeps the confirmed ones
    write(rig.offered / "programs.zst", zstd_pipe(b"even newer " * 100))
    rig.run(check=True)
    rd = vendor_ramdisk(rig.part("vendor_boot_b"))
    assert rd.startswith(confirmed)
    assert rig.part("vendor_boot_a") == rig.before["vendor_boot_a"]
