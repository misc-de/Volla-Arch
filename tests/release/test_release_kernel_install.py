# plinius-kernel-install: which kernel ends up in boot<slot> (or the boot menu entry of an Arch on
# the card), the fallback and its boot menu entry, and the AVB data of the boot partition.
#
# The script runs as a copy whose system paths (/usr/lib/plinius, /var/lib/plinius-kernel,
# /dev/disk/by-partlabel, /dev/mmcblk0p1, /proc) lie in tmp_path. Partitions are
# plain files ("-b" becomes "-e" in the copy), the card is a directory that a debugfs stand-in
# reads and that findmnt reports as mounted. Every other program that changes state is a stub.

import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import PKG
from release_sim import (CMDLINE_PATTERNS, DEBUGFS, avb_partition, boot_img, card_list, copy_script, init_boot,
                         kernel_blob, last_index, need_inode_order, read_avb, vendor_boot,
                         write_last_index)

SRC = PKG / "plinius-kernel" / "plinius-kernel-install"
AVB = PKG / "plinius-kernel" / "plinius-kernel-avb"

ORIG = "6.1.145-g8bd3c12ba5c3"  # UT's kernel, in boot_b before the first update
NEW = "6.1.192-plinius17"
NEWER = "6.1.193-plinius1"
PART_SIZE = 256 * 1024
VB_OFF = 160 * 1024  # where the original vbmeta sits

MAPPING = {
    "lib=/usr/lib/plinius/kernel": "lib=@ROOT@/usr/lib/plinius/kernel",
    "kmods=/usr/lib/plinius/modules": "kmods=@ROOT@/usr/lib/plinius/modules",
    "state=/var/lib/plinius-kernel": "state=@ROOT@/var/lib/plinius-kernel",
    "dlkm=/var/lib/lxc/android/system_dlkm.img": "dlkm=@ROOT@/var/lib/lxc/android/system_dlkm.img",
    "card=/dev/mmcblk0p1": "card=@ROOT@/dev/mmcblk0p1",
    "mnt=/run/plinius-kernel/card": "mnt=@ROOT@/run/plinius-kernel/card",
    "/proc/bootconfig": "@ROOT@/proc/bootconfig",
    "/proc/cmdline": "@ROOT@/proc/cmdline",
    "/dev/disk/by-partlabel/": "@ROOT@/dev/disk/by-partlabel/",
    "[[ -b $card ]]": "[[ -e $card ]]",
    "[[ -b $part ]]": "[[ -e $part ]]",
}

VENDOR_CMDLINE = "bootopt=64S3,32N2,64N2 androidboot.selinux=permissive"


class Phone:
    """The phone as plinius-kernel-install sees it, below tmp_path."""

    def __init__(self, tmp_path, fake_bin):
        self.tmp = tmp_path
        self.root = tmp_path / "phone"
        self.fake_bin = fake_bin
        self.lib = self.root / "usr/lib/plinius/kernel"
        self.kmods = self.root / "usr/lib/plinius/modules"
        self.state = self.root / "var/lib/plinius-kernel"
        self.parts = self.root / "dev/disk/by-partlabel"
        self.card_dev = self.root / "dev/mmcblk0p1"
        self.card = tmp_path / "card"
        for d in (self.lib, self.kmods, self.parts, self.root / "proc",
                  self.root / "var/lib/lxc/android"):
            d.mkdir(parents=True, exist_ok=True)
        allowed = ("/dev/zero", "/dev/null") + CMDLINE_PATTERNS
        self.script = copy_script(SRC, tmp_path / "plinius-kernel-install", MAPPING, self.root, allowed=allowed)
        shutil.copy(AVB, self.lib / "plinius-kernel-avb")
        self.functions = copy_script(SRC, tmp_path / "kernel-functions.sh", MAPPING, self.root,
                                     cut="\ncase ${1:-} in", allowed=allowed)
        self.bootconfig('_b')
        self.cmdline("")
        self.orig_img = boot_img(ORIG)
        (self.parts / "boot_b").write_bytes(avb_partition(self.orig_img, PART_SIZE, VB_OFF))
        (self.parts / "boot_a").write_bytes(avb_partition(boot_img("5.10.0-volla"), PART_SIZE, VB_OFF))
        self.vendor_ramdisk = b"VENDOR-RAMDISK" * 50
        self.generic_ramdisk = b"GENERIC-RAMDISK" * 30
        self.dtb = b"\xd0\x0d\xfe\xed" + b"DTB" * 40
        (self.parts / "vendor_boot_b").write_bytes(
            vendor_boot(VENDOR_CMDLINE, self.vendor_ramdisk, self.dtb))
        (self.parts / "init_boot_b").write_bytes(init_boot(self.generic_ramdisk))

        fake_bin.stub("debugfs", DEBUGFS)
        # The card counts as mounted at its directory; the script's own mount point never is
        fake_bin.stub("findmnt", 'if [[ " $* " == *" -S "* ]]; then cat "${@: -1}" 2>/dev/null; exit 0; fi\nexit 1')
        fake_bin.stub("mount")
        fake_bin.stub("umount")
        fake_bin.stub("sync")
        fake_bin.stub("mountpoint", "exit ${FAKE_MOUNTPOINT:-1}")
        fake_bin.stub("blockdev", '[[ $1 == --getsize64 ]] && stat -c %s "$2"')
        fake_bin.stub("uname", 'echo "$FAKE_UNAME"')
        fake_bin.stub("plinius-bootcheck")

    # --- setup ---
    def bootconfig(self, slot):
        text = f'androidboot.slot_suffix = "{slot}"\n' if slot else "androidboot.hardware = mt6789\n"
        (self.root / "proc/bootconfig").write_text("androidboot.serialno = 123\n" + text)

    def cmdline(self, text):
        (self.root / "proc/cmdline").write_text(text + "\n")

    def package(self, rel):
        """The files plinius-kernel's package() installs for a release."""
        d = self.lib / rel
        d.mkdir(parents=True, exist_ok=True)
        (d / "boot.img").write_bytes(boot_img(rel))
        (d / "Image.gz").write_bytes(kernel_blob(rel))
        (d / "system_dlkm.img").write_bytes(b"erofs " + rel.encode())
        (self.kmods / rel).mkdir(parents=True, exist_ok=True)
        (self.kmods / rel / "plinius-backlight.ko").write_bytes(b"ko " + rel.encode())
        return d

    def kept(self, rel):
        """A fallback as image/mkimage.sh puts it into a new system: the release's files kept,
        named as previous."""
        d = self.state / "kept" / rel
        (d / "modules").mkdir(parents=True, exist_ok=True)
        (d / "boot.img").write_bytes(boot_img(rel))
        (d / "Image.gz").write_bytes(kernel_blob(rel))
        (d / "system_dlkm.img").write_bytes(b"erofs " + rel.encode())
        (d / "modules/plinius-backlight.ko").write_bytes(b"ko " + rel.encode())
        (self.state / "previous").write_text(rel + "\n")
        return d

    def boot_menu(self, extra=()):
        """A card with the Android Boot Manager: real.conf (Arch on the phone's storage)."""
        entries = self.card / "db/entries"
        entries.mkdir(parents=True)
        (self.card / "real").mkdir()
        (self.card / "real/logo.bin").write_bytes(b"LOGO")
        (entries / "real.conf").write_text("title Arch Linux\nlinux real/zImage\nlogo real/logo.bin\n")
        for name in extra:
            (entries / f"{name}.conf").write_text(f"title {name}\nxpart 1:2\n")
        write_last_index(self.card, 0)
        self.card_dev.write_text(str(self.card))

    def card_system(self, entry="plinius", sp="/dev/mmcblk0p2", kernel=ORIG, in_data=False):
        """An Arch on card partitions as Plinius Boot sets it up: its own entry next to real.conf,
        and the command line of a start from it. in_data: the system is rootfs.img on the data
        partition, and the entry names only that one."""
        self.boot_menu()
        (self.card / entry).mkdir()
        (self.card / entry / "zImage").write_bytes(kernel_blob(kernel))
        (self.card / entry / "initrd.cpio.gz").write_bytes(b"SD-RAMDISK")
        parts = "datapart=/dev/mmcblk0p3" if in_data else f"systempart={sp} datapart=/dev/mmcblk0p3"
        (self.card / "db/entries" / f"{entry}.conf").write_text(
            f"title Arch Linux (SD)\nlinux {entry}/zImage\ninitrd {entry}/initrd.cpio.gz\n"
            f"dtb {entry}/dtb.dtb\noptions {VENDOR_CMDLINE} {parts}\n"
            "xtype UT\nxpart 2:3\n")
        self.cmdline(f"{VENDOR_CMDLINE} {parts}")
        return self.card / entry

    # --- running ---
    def run(self, *args, uname=ORIG, stdin=None, **extra):
        env = self.fake_bin.env(isolated=True, FAKE_UNAME=uname, **extra)
        return subprocess.run(["bash", str(self.script), *args], env=env, input=stdin,
                              capture_output=True, text=True, timeout=60)

    def call(self, code, **extra):
        """Runs bash code with the script's functions defined."""
        env = self.fake_bin.env(isolated=True, FAKE_UNAME=ORIG, **extra)
        return subprocess.run(["bash", "-c", f'source "{self.functions}"\n{code}'], env=env,
                              capture_output=True, text=True, timeout=60)

    # --- reading ---
    def get(self, name):
        p = self.state / name
        return p.read_text().strip() if p.exists() else None

    def boot(self, slot="b"):
        return (self.parts / f"boot_{slot}").read_bytes()

    def accepted(self):
        return self.fake_bin.calls("plinius-bootcheck")


@pytest.fixture
def phone(tmp_path, fake_bin):
    return Phone(tmp_path, fake_bin)


def ok(r):
    assert r.returncode == 0, f"exit {r.returncode}\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    return r


def fails(r):
    assert r.returncode != 0, f"unexpected success\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    return r


# --- the debugfs stand-in behaves like the real one ---------------------------------------------

def test_debugfs_stand_in_matches_real_debugfs(tmp_path, fake_bin):
    """The tests rely on debugfs's exit status and output; compare the stand-in with the real
    tool on a real ext4 image (read-only, built from a directory without root)."""
    mke2fs, debugfs = shutil.which("mke2fs") or "/usr/bin/mke2fs", shutil.which("debugfs")
    if not debugfs or not Path(mke2fs).exists():
        pytest.skip("e2fsprogs missing on the host")
    src = tmp_path / "src"
    (src / "db/entries").mkdir(parents=True)
    (src / "db/entries/real.conf").write_text("title Arch Linux\n")
    img = tmp_path / "card.img"
    subprocess.run([mke2fs, "-q", "-t", "ext4", "-d", str(src), str(img), "4M"], check=True)
    dev = tmp_path / "dev"
    dev.write_text(str(src))
    fake_bin.stub("debugfs", DEBUGFS)
    stub = str(fake_bin.path / "debugfs")

    def both(cmd):
        real = subprocess.run([debugfs, "-c", "-R", cmd, str(img)], capture_output=True, text=True)
        fake = subprocess.run([stub, "-c", "-R", cmd, str(dev)], capture_output=True, text=True)
        return real, fake

    for cmd in ("stat /db/entries/real.conf", "stat /db/entries/missing.conf"):
        real, fake = both(cmd)
        assert (real.returncode, "Inode:" in real.stdout) == (fake.returncode, "Inode:" in fake.stdout)
    real, fake = both("cat /db/entries/real.conf")
    assert real.stdout == fake.stdout
    real, fake = both("ls -p /db/entries")

    def names(out):
        return [line.split("/")[5] for line in out.splitlines() if line.startswith("/")]
    assert names(real.stdout) == names(fake.stdout) == [".", "..", "real.conf"]
    # A device that cannot be opened is no error for debugfs either
    r = subprocess.run([debugfs, "-c", "-R", "stat /x", str(tmp_path / "none.img")],
                       capture_output=True, text=True)
    assert r.returncode == 0


# --- small functions --------------------------------------------------------------------------

@pytest.mark.parametrize("gz", [True, False])
def test_image_release_reads_the_kernel_version(phone, tmp_path, gz):
    img = tmp_path / "boot.img"
    img.write_bytes(boot_img(NEW, gz=gz))
    r = ok(phone.call(f'image_release "{img}"'))
    assert r.stdout.strip() == NEW


def test_image_release_of_a_partition_with_avb_data(phone):
    r = ok(phone.call(f'image_release "{phone.parts / "boot_b"}"'))
    assert r.stdout.strip() == ORIG


def test_slot_is_read_from_bootconfig_and_remembered(phone):
    assert ok(phone.call("slot")).stdout.strip() == "_b"
    assert phone.get("slot") == "_b"
    # A start from a card entry may come without bootconfig: the remembered slot holds
    phone.bootconfig(None)
    assert ok(phone.call("slot")).stdout.strip() == "_b"


def test_slot_unknown_stops(phone):
    phone.bootconfig(None)
    r = fails(phone.call("slot"))
    assert "active slot unknown" in r.stderr


def test_slot_never_takes_another_value(phone):
    (phone.root / "proc/bootconfig").write_text('androidboot.slot_suffix = "_c"\n')
    fails(phone.call("slot"))


@pytest.mark.parametrize("cmdline,role", [
    ("console=tty0 plinius.kernel=trial quiet", "trial"),
    ("plinius.kernel=previous", "previous"),
    ("console=tty0 quiet", ""),
])
def test_role_comes_from_the_command_line(phone, cmdline, role):
    phone.cmdline(cmdline)
    assert ok(phone.call("role")).stdout.strip() == role


def test_release_dir_prefers_the_package_over_the_kept_copy(phone):
    phone.package(NEW)
    (phone.state / "kept" / NEW).mkdir(parents=True)
    (phone.state / "kept" / ORIG).mkdir(parents=True)
    assert ok(phone.call(f"release_dir {NEW}")).stdout.strip() == str(phone.lib / NEW)
    assert ok(phone.call(f"release_dir {ORIG}")).stdout.strip() == str(phone.state / "kept" / ORIG)
    assert ok(phone.call("release_dir 9.9.9")).stdout.strip() == ""


# --- write_boot: the AVB data of boot<slot> stays valid ------------------------------------------

def test_write_boot_moves_vbmeta_behind_the_image_and_updates_its_hash(phone, tmp_path):
    big = boot_img(NEW) + b"\x11" * 170 * 1024  # reaches over the old vbmeta at 160 KiB
    img = tmp_path / "new.img"
    img.write_bytes(big)
    part = phone.parts / "boot_b"
    before = read_avb(phone.boot())
    ok(phone.call(f'write_boot "{img}" "{part}"'))
    data = phone.boot()
    assert data[:len(big)] == big
    avb = read_avb(data)
    assert avb["image_size"] == avb["desc_size"] == len(big)
    assert avb["voff"] == (len(big) + 4095) // 4096 * 4096
    import hashlib
    assert avb["digest"] == hashlib.sha256(before["salt"] + big).digest()
    assert avb["salt"] == before["salt"] and avb["vsize"] == before["vsize"]
    assert len(data) == PART_SIZE
    # The valid block is kept for later, the temporary image is gone
    assert (phone.state / "boot-vbmeta.bin").read_bytes()[:4] == b"AVB0"
    assert not (phone.state / "boot-new.img").exists()


def test_write_boot_without_footer_writes_the_image_alone(phone, tmp_path):
    part = phone.parts / "boot_b"
    part.write_bytes(b"\xee" * PART_SIZE)
    img = tmp_path / "new.img"
    img.write_bytes(boot_img(NEW))
    r = ok(phone.call(f'write_boot "{img}" "{part}"'))
    data = part.read_bytes()
    assert data[:len(img.read_bytes())] == img.read_bytes()
    # notrunc: the rest of the partition stays as it was
    assert data[len(img.read_bytes()):] == b"\xee" * (PART_SIZE - len(img.read_bytes()))
    assert "no AVB footer" in r.stderr


def test_write_boot_with_broken_vbmeta_uses_the_kept_copy(phone, tmp_path):
    part = phone.parts / "boot_b"
    img = tmp_path / "new.img"
    img.write_bytes(boot_img(NEW))
    ok(phone.call(f'write_boot "{img}" "{part}"'))  # keeps a valid copy
    part.write_bytes(avb_partition(phone.orig_img, PART_SIZE, VB_OFF, broken=True))
    img.write_bytes(boot_img(NEWER))
    r = ok(phone.call(f'write_boot "{img}" "{part}"'))
    assert "kept copy is used" in r.stderr
    avb = read_avb(part.read_bytes())
    assert avb["desc_size"] == len(img.read_bytes())


def test_write_boot_with_broken_vbmeta_and_no_copy_writes_nothing(phone, tmp_path):
    part = phone.parts / "boot_b"
    part.write_bytes(avb_partition(phone.orig_img, PART_SIZE, VB_OFF, broken=True))
    before = part.read_bytes()
    img = tmp_path / "new.img"
    img.write_bytes(boot_img(NEW))
    r = fails(phone.call(f'write_boot "{img}" "{part}"'))
    assert "nothing written" in r.stderr
    assert part.read_bytes() == before


def test_write_boot_refuses_an_image_that_leaves_no_room_for_vbmeta(phone, tmp_path):
    part = phone.parts / "boot_b"
    before = part.read_bytes()
    img = tmp_path / "huge.img"
    img.write_bytes(boot_img(NEW) + b"\x22" * (PART_SIZE - 6000))
    fails(phone.call(f'write_boot "{img}" "{part}"'))
    assert part.read_bytes() == before


def test_write_part_refuses_an_image_larger_than_the_partition(phone, tmp_path):
    img = tmp_path / "huge.img"
    img.write_bytes(b"\x33" * (PART_SIZE + 1))
    part = phone.parts / "boot_b"
    before = part.read_bytes()
    r = fails(phone.call(f'write_part "{img}" "{part}"'))
    assert "does not fit" in r.stderr
    assert part.read_bytes() == before


# --- install without the boot menu card ---------------------------------------------------------

def test_install_without_card_writes_the_partition_and_keeps_the_running_kernel(phone):
    phone.package(NEW)
    r = ok(phone.run("install", NEW))
    new_img = (phone.lib / NEW / "boot.img").read_bytes()
    assert phone.boot()[:len(new_img)] == new_img
    assert read_avb(phone.boot())["desc_size"] == len(new_img)
    assert phone.get("installed") == NEW
    assert phone.get("previous") == ORIG
    # The fallback is the whole partition as it was (it starts as it did)
    orig = avb_partition(phone.orig_img, PART_SIZE, VB_OFF)
    assert (phone.state / "boot-previous.img").read_bytes() == orig
    assert (phone.state / "boot-orig.img").read_bytes() == orig
    # The other slot is never touched
    assert phone.boot("a") == avb_partition(boot_img("5.10.0-volla"), PART_SIZE, VB_OFF)
    assert phone.accepted() == [["accept", "--written", str(phone.parts / "boot_b")]]
    assert "it starts with the next restart" in r.stdout and f"Fallback: {ORIG}" in r.stdout


def test_install_while_running_on_the_fallback_keeps_that_fallback(phone):
    # NEW was installed but does not start; the phone runs ORIG from the fallback. NEWER must
    # replace NEW and leave ORIG as the fallback.
    phone.package(NEW)
    phone.package(NEWER)
    ok(phone.run("install", NEW))
    fallback = (phone.state / "boot-previous.img").read_bytes()
    ok(phone.run("install", NEWER, uname=ORIG))
    assert phone.get("installed") == NEWER
    assert phone.get("previous") == ORIG
    assert (phone.state / "boot-previous.img").read_bytes() == fallback


def test_install_after_a_confirmed_start_makes_the_running_kernel_the_fallback(phone):
    phone.package(NEW)
    phone.package(NEWER)
    ok(phone.run("install", NEW))
    ok(phone.run("install", NEWER, uname=NEW))
    assert phone.get("previous") == NEW
    new_img = (phone.lib / NEW / "boot.img").read_bytes()
    assert (phone.state / "boot-previous.img").read_bytes()[:len(new_img)] == new_img
    # The very first original stays kept, whatever came after
    assert (phone.state / "boot-orig.img").read_bytes()[:len(phone.orig_img)] == phone.orig_img


def test_install_of_the_kernel_already_in_place_writes_nothing(phone):
    phone.package(NEW)
    ok(phone.run("install", NEW))
    before = phone.boot()
    phone.fake_bin.path.joinpath("plinius-bootcheck.log").unlink()
    r = ok(phone.run("install", NEW, uname=NEW))
    assert "already in" in r.stdout
    assert phone.boot() == before
    assert phone.accepted() == []


def test_install_of_an_unknown_release_stops(phone):
    r = fails(phone.run("install", "9.9.9-x"))
    assert "no kernel 9.9.9-x" in r.stderr
    assert phone.boot() == avb_partition(phone.orig_img, PART_SIZE, VB_OFF)


def test_install_needs_a_release(phone):
    fails(phone.run("install"))


def test_install_on_an_arch_from_a_card_partition_without_its_entry_stops(phone):
    phone.package(NEW)
    phone.boot_menu()
    phone.cmdline("root=/dev/mmcblk0p3 systempart=/dev/mmcblk0p3")
    r = fails(phone.run("install", NEW))
    assert "no single entry for systempart=/dev/mmcblk0p3" in r.stderr
    assert phone.boot() == avb_partition(phone.orig_img, PART_SIZE, VB_OFF)
    assert card_list(phone.card) == ["real.conf"]


def test_install_without_a_partition_stops(phone):
    phone.package(NEW)
    (phone.parts / "boot_b").unlink()
    r = fails(phone.run("install", NEW))
    assert "missing" in r.stderr


def test_hook_installs_the_release_named_on_stdin(phone):
    phone.package(NEW)
    ok(phone.run("hook", stdin=f"usr/lib/plinius/kernel/{NEW}/boot.img\n"))
    assert phone.get("installed") == NEW


def test_hook_takes_the_last_of_several_paths(phone):
    phone.package(NEW)
    phone.package(NEWER)
    ok(phone.run("hook", stdin=f"usr/lib/plinius/kernel/{NEW}/boot.img\n"
                               f"usr/lib/plinius/kernel/{NEWER}/boot.img\n"))
    assert phone.get("installed") == NEWER


def test_hook_without_a_path_stops(phone):
    r = fails(phone.run("hook", stdin=""))
    assert "passed no kernel" in r.stderr


def test_unknown_command_prints_the_usage(phone):
    r = fails(phone.run("frobnicate"))
    assert "usage:" in r.stderr
    fails(phone.run())


def test_restore_writes_the_fallback_back(phone):
    phone.package(NEW)
    ok(phone.run("install", NEW))
    r = ok(phone.run("restore", uname=NEW))
    assert phone.boot() == avb_partition(phone.orig_img, PART_SIZE, VB_OFF)
    assert phone.get("installed") == ORIG
    assert f"fallback {ORIG} written" in r.stdout


def test_restore_without_a_fallback_stops(phone):
    r = fails(phone.run("restore"))
    assert "no fallback" in r.stderr
    assert phone.boot() == avb_partition(phone.orig_img, PART_SIZE, VB_OFF)


def test_restore_refused_on_an_arch_from_a_card_partition(phone):
    phone.cmdline("systempart=/dev/mmcblk0p3")
    fails(phone.run("restore"))


# --- the boot menu card: the fallback as an entry of its own -----------------------------------

def entry(phone, name):
    return (phone.card / "db/entries" / f"{name}.conf").read_text()


def test_install_with_card_writes_the_partition_and_the_fallback_entry(phone, tmp_path):
    need_inode_order(tmp_path)
    phone.package(NEW)
    phone.boot_menu()
    r = ok(phone.run("install", NEW))
    # The new kernel is the default at once: in boot<slot>, AVB data kept valid
    new_img = (phone.lib / NEW / "boot.img").read_bytes()
    assert phone.boot()[:len(new_img)] == new_img
    assert read_avb(phone.boot())["desc_size"] == len(new_img)
    assert phone.get("installed") == NEW and phone.get("previous") == ORIG
    assert phone.get("trial") is None
    # Menu: "Arch Linux" first, the previous kernel right below; autostart on Arch Linux
    assert card_list(phone.card) == ["real.conf", "plinius-previous.conf"]
    assert last_index(phone.card) == 0
    conf = entry(phone, "plinius-previous")
    assert f"title Arch Linux (previous kernel {ORIG})" in conf
    assert "linux plinius-previous/zImage\n" in conf
    assert f"options {VENDOR_CMDLINE} plinius.kernel=previous\n" in conf
    assert "xpart real\n" in conf and "logo real/logo.bin\n" in conf
    d = phone.card / "plinius-previous"
    assert (d / "zImage").read_bytes() == kernel_blob(ORIG)
    assert (d / "initrd.cpio.gz").read_bytes() == phone.vendor_ramdisk + phone.generic_ramdisk
    assert (d / "dtb.dtb").read_bytes() == phone.dtb
    assert (d / "entry.conf").read_text() == conf
    assert phone.accepted() == [["accept", "--written", str(phone.parts / "boot_b"),
                                 "abm-entry:plinius-previous"]]
    assert "it starts with the next restart" in r.stdout


def test_install_with_card_needs_no_confirmation_at_the_next_start(phone, tmp_path):
    need_inode_order(tmp_path)
    phone.package(NEW)
    phone.boot_menu()
    ok(phone.run("install", NEW))
    before = (phone.boot(), card_list(phone.card), last_index(phone.card))
    phone.fake_bin.path.joinpath("plinius-bootcheck.log").unlink()
    r = ok(phone.run("boot", uname=NEW))
    assert r.stdout == ""
    assert (phone.boot(), card_list(phone.card), last_index(phone.card)) == before
    assert phone.accepted() == []


def test_second_update_makes_the_running_kernel_the_previous_entry(phone, tmp_path):
    need_inode_order(tmp_path)
    phone.package(NEW)
    phone.package(NEWER)
    phone.boot_menu()
    ok(phone.run("install", NEW))
    ok(phone.run("install", NEWER, uname=NEW))
    assert phone.get("installed") == NEWER and phone.get("previous") == NEW
    assert (phone.card / "plinius-previous/zImage").read_bytes() == kernel_blob(NEW)
    assert f"title Arch Linux (previous kernel {NEW})" in entry(phone, "plinius-previous")
    assert card_list(phone.card) == ["real.conf", "plinius-previous.conf"]
    assert last_index(phone.card) == 0


def test_update_while_the_fallback_runs_keeps_it_and_starts_the_new_kernel_next(phone, tmp_path):
    need_inode_order(tmp_path)
    phone.package(NEW)
    phone.package(NEWER)
    phone.boot_menu()
    ok(phone.run("install", NEW))
    # NEW does not start: the user chose the fallback in the menu, which remembers the choice
    write_last_index(phone.card, 1)
    phone.cmdline(VENDOR_CMDLINE + " plinius.kernel=previous")
    phone.fake_bin.path.joinpath("plinius-bootcheck.log").unlink()
    ok(phone.run("install", NEWER, uname=ORIG))
    new_img = (phone.lib / NEWER / "boot.img").read_bytes()
    assert phone.boot()[:len(new_img)] == new_img
    assert phone.get("installed") == NEWER and phone.get("previous") == ORIG
    # The fallback that started for sure stays, as file and entry; the entry is not rewritten
    assert (phone.card / "plinius-previous/zImage").read_bytes() == kernel_blob(ORIG)
    assert phone.accepted() == [["accept", "--written", str(phone.parts / "boot_b"),
                                 "abm-entry:plinius-previous"]]
    # The next start runs the new kernel, not the fallback chosen last time
    assert last_index(phone.card) == 0


def test_install_with_card_writes_a_missing_fallback_entry(phone, tmp_path):
    need_inode_order(tmp_path)
    phone.package(NEW)
    ok(phone.run("install", NEW))
    # The card came later; the kernel is in place, only its fallback entry is missing
    phone.boot_menu()
    ok(phone.run("install", NEW, uname=NEW))
    assert card_list(phone.card) == ["real.conf", "plinius-previous.conf"]
    assert (phone.card / "plinius-previous/zImage").read_bytes() == kernel_blob(ORIG)
    assert phone.accepted()[-1] == ["accept", "--written", str(phone.parts / "boot_b"),
                                    "abm-entry:plinius-previous"]


def test_install_drops_the_trial_of_an_older_version(phone, tmp_path):
    need_inode_order(tmp_path)
    phone.package(NEW)
    phone.boot_menu()
    # An older plinius-kernel-install left a trial pending: entry, autostart on it, state
    (phone.card / "plinius-trial").mkdir()
    (phone.card / "plinius-trial/zImage").write_bytes(b"old trial")
    (phone.card / "db/entries/plinius-trial.conf").write_text("title trial\nxpart real\n")
    write_last_index(phone.card, 1)
    phone.state.mkdir(parents=True)
    for name in ("trial", "failed"):
        (phone.state / name).write_text(NEW + "\n")
    ok(phone.run("install", NEW))
    assert card_list(phone.card) == ["real.conf", "plinius-previous.conf"]
    assert not (phone.card / "plinius-trial").exists()
    assert last_index(phone.card) == 0
    assert phone.get("trial") is None and phone.get("failed") is None
    assert phone.get("installed") == NEW


def test_install_without_card_drops_the_state_of_an_older_trial(phone):
    phone.package(NEW)
    phone.state.mkdir(parents=True)
    (phone.state / "trial").write_text(NEW + "\n")
    (phone.state / "replace_previous").write_text("1\n")
    ok(phone.run("install", NEW))
    assert phone.get("trial") is None and phone.get("replace_previous") is None
    assert phone.get("installed") == NEW


def test_boot_with_a_kernel_set_up_does_nothing(phone):
    phone.package(NEW)
    ok(phone.run("install", NEW))
    before = phone.boot()
    phone.fake_bin.path.joinpath("plinius-bootcheck.log").unlink()
    r = ok(phone.run("boot", uname=NEW))
    assert r.stdout == "" and phone.boot() == before
    assert phone.accepted() == []


# --- an Arch on a memory card partition: its own boot menu entry ---------------------------------

def test_card_system_install_writes_its_entry_and_keeps_the_fallback(phone, tmp_path):
    need_inode_order(tmp_path)
    phone.package(NEW)
    own = phone.card_system()
    r = ok(phone.run("install", NEW))
    # Into the entry at once; the partition stays, the ramdisk is the entry's
    assert (own / "zImage").read_bytes() == kernel_blob(NEW)
    assert (own / "initrd.cpio.gz").read_bytes() == b"SD-RAMDISK"
    assert phone.boot() == avb_partition(phone.orig_img, PART_SIZE, VB_OFF)
    assert phone.get("installed") == NEW and phone.get("previous") == ORIG and phone.get("entry") == "plinius"
    assert phone.get("trial") is None and phone.get("replace_previous") is None
    assert card_list(phone.card) == ["real.conf", "plinius.conf", "plinius-kernel-previous.conf"]
    assert last_index(phone.card) == 1  # the entry of the card system
    conf = entry(phone, "plinius-kernel-previous")
    assert conf.splitlines() == [
        f"title Arch Linux (SD) (previous kernel {ORIG})", "linux plinius-kernel-previous/zImage",
        "initrd plinius/initrd.cpio.gz", "dtb plinius/dtb.dtb", "xtype UT", "xpart 2:3",
        f"options {VENDOR_CMDLINE} systempart=/dev/mmcblk0p2 datapart=/dev/mmcblk0p3 "
        "plinius.kernel=previous"]
    assert (phone.card / "plinius-kernel-previous/zImage").read_bytes() == kernel_blob(ORIG)
    # plinius-bootcheck judges the entries of the card system it runs from: both are reported
    assert phone.accepted() == [["accept", "--written", "abm-entry:plinius", "abm-entry:plinius-kernel-previous"]]
    assert "boot menu entry plinius" in r.stdout and "plinius-kernel-previous" in r.stdout


def test_card_system_second_update_and_the_fallback_rule(phone, tmp_path):
    need_inode_order(tmp_path)
    phone.package(NEW)
    phone.package(NEWER)
    own = phone.card_system()
    base = entry(phone, "plinius").split("options ", 1)[1].split("\n")[0]
    ok(phone.run("install", NEW))
    # Running the fallback (the new kernel in the entry does not work): a newer one replaces only
    # the entry's kernel, the fallback that started for sure stays; the menu remembered the
    # fallback, the next start runs the entry again
    phone.cmdline(base + " plinius.kernel=previous")
    write_last_index(phone.card, 2)
    ok(phone.run("install", NEWER, uname=ORIG))
    assert (own / "zImage").read_bytes() == kernel_blob(NEWER)
    assert (phone.card / "plinius-kernel-previous/zImage").read_bytes() == kernel_blob(ORIG)
    assert phone.get("installed") == NEWER and phone.get("previous") == ORIG
    assert last_index(phone.card) == 1


def test_card_system_update_while_running_the_entry_makes_it_the_fallback(phone, tmp_path):
    need_inode_order(tmp_path)
    phone.package(NEW)
    phone.package(NEWER)
    own = phone.card_system()
    base = entry(phone, "plinius").split("options ", 1)[1].split("\n")[0]
    ok(phone.run("install", NEW))
    phone.cmdline(base)
    ok(phone.run("install", NEWER, uname=NEW))
    assert (own / "zImage").read_bytes() == kernel_blob(NEWER)
    assert (phone.card / "plinius-kernel-previous/zImage").read_bytes() == kernel_blob(NEW)
    assert f"(previous kernel {NEW})" in entry(phone, "plinius-kernel-previous")
    assert phone.get("previous") == NEW


def test_card_system_install_drops_the_trial_of_an_older_version(phone, tmp_path):
    need_inode_order(tmp_path)
    phone.package(NEW)
    own = phone.card_system()
    (phone.card / "plinius-kernel-trial").mkdir()
    (phone.card / "plinius-kernel-trial/zImage").write_bytes(b"old trial")
    (phone.card / "db/entries/plinius-kernel-trial.conf").write_text(
        entry(phone, "plinius").replace("linux plinius/", "linux plinius-kernel-trial/")
        + "options systempart=/dev/mmcblk0p2 plinius.kernel=trial\n")
    write_last_index(phone.card, 2)
    phone.state.mkdir(parents=True)
    (phone.state / "trial").write_text(NEW + "\n")
    (phone.state / "replace_previous").write_text("1\n")
    ok(phone.run("install", NEW))
    assert (own / "zImage").read_bytes() == kernel_blob(NEW)
    assert card_list(phone.card) == ["real.conf", "plinius.conf", "plinius-kernel-previous.conf"]
    assert not (phone.card / "plinius-kernel-trial").exists()
    assert last_index(phone.card) == 1
    assert phone.get("trial") is None and phone.get("replace_previous") is None


def test_card_system_install_of_the_kernel_already_in_its_entry(phone, tmp_path):
    need_inode_order(tmp_path)
    phone.package(NEW)
    phone.card_system(kernel=NEW)
    r = ok(phone.run("install", NEW, uname=NEW))
    assert "already in the boot menu entry plinius" in r.stdout
    assert phone.get("installed") == NEW and phone.get("trial") is None
    assert card_list(phone.card) == ["real.conf", "plinius.conf"]
    assert phone.accepted() == []


def test_card_system_entries_of_other_systems_stay(phone, tmp_path):
    need_inode_order(tmp_path)
    phone.package(NEW)
    phone.card_system()
    # The Arch on the phone's storage has its own fallback; another card system its own entry
    # and a trial of an older version
    (phone.card / "db/entries/plinius-previous.conf").write_text("title Arch Linux (previous)\nxpart real\n")
    (phone.card / "db/entries/rom3.conf").write_text(
        "title Other\nlinux rom3/zImage\noptions systempart=/dev/mmcblk0p5\nxpart 5:6\n")
    (phone.card / "db/entries/rom3-kernel-trial.conf").write_text(
        "title Other (trial)\nlinux rom3-kernel-trial/zImage\noptions systempart=/dev/mmcblk0p5\nxpart 5:6\n")
    ok(phone.run("install", NEW))
    names = card_list(phone.card)
    assert names[:2] == ["real.conf", "plinius-previous.conf"]
    assert names.index("plinius-kernel-previous.conf") == names.index("plinius.conf") + 1
    assert names.index("rom3-kernel-trial.conf") == names.index("rom3.conf") + 1
    assert (phone.card / "db/entries/plinius-previous.conf").read_text() == "title Arch Linux (previous)\nxpart real\n"


def test_card_system_entry_must_be_unique(phone):
    phone.package(NEW)
    phone.card_system()
    (phone.card / "db/entries/copy.conf").write_text(
        "title Copy\nlinux copy/zImage\noptions systempart=/dev/mmcblk0p2\nxpart 2:3\n")
    r = fails(phone.run("install", NEW))
    assert "(copy plinius)" in r.stderr or "(plinius copy)" in r.stderr


def test_card_system_install_without_boot_menu_stops(phone):
    phone.package(NEW)
    phone.cmdline("systempart=/dev/mmcblk0p2")
    r = fails(phone.run("install", NEW))
    assert "boot menu is missing" in r.stderr


def test_card_system_install_without_the_entry_kernel_stops(phone):
    phone.package(NEW)
    own = phone.card_system()
    (own / "zImage").unlink()
    r = fails(phone.run("install", NEW))
    assert "zImage missing" in r.stderr


def test_card_system_restore_writes_the_fallback_into_the_entry(phone, tmp_path):
    need_inode_order(tmp_path)
    phone.package(NEW)
    own = phone.card_system()
    ok(phone.run("install", NEW))
    r = ok(phone.run("restore", uname=NEW))
    assert (own / "zImage").read_bytes() == kernel_blob(ORIG)
    assert phone.get("installed") == ORIG
    assert "fallback" in r.stdout


def test_card_system_restore_without_a_fallback_stops(phone):
    phone.card_system()
    r = fails(phone.run("restore"))
    assert "no previous kernel" in r.stderr


def test_card_system_restore_without_boot_menu_stops(phone):
    phone.cmdline("systempart=/dev/mmcblk0p2")
    r = fails(phone.run("restore"))
    assert "boot menu on the memory card is missing" in r.stderr


def test_card_system_copy_to_card_checks_what_was_written(phone, tmp_path):
    src = tmp_path / "k"
    src.write_bytes(b"KERNEL")
    phone.fake_bin.stub("cmp", "exit 1")
    r = phone.call(f'copy_to_card "{src}" "{tmp_path / "dst"}"')
    assert r.returncode != 0 and "differs" in r.stderr


# --- the first start of a new system: the package's kernel is set up once -----------------------

def test_first_start_on_a_card_system_writes_its_entry(phone, tmp_path):
    need_inode_order(tmp_path)
    phone.package(NEW)
    own = phone.card_system()
    r = ok(phone.run("boot"))
    assert f"setting up kernel {NEW}" in r.stdout
    assert (own / "zImage").read_bytes() == kernel_blob(NEW)
    assert phone.get("installed") == NEW and phone.get("previous") == ORIG
    assert card_list(phone.card) == ["real.conf", "plinius.conf", "plinius-kernel-previous.conf"]
    # Once: a kernel is recorded now
    phone.fake_bin.path.joinpath("plinius-bootcheck.log").unlink()
    ok(phone.run("boot", uname=NEW))
    assert phone.accepted() == []


def test_first_start_without_card_writes_the_partition(phone):
    phone.package(NEW)
    ok(phone.run("boot"))
    new_img = (phone.lib / NEW / "boot.img").read_bytes()
    assert phone.boot()[:len(new_img)] == new_img
    assert phone.get("installed") == NEW and phone.get("previous") == ORIG


def test_first_start_when_the_package_kernel_runs_records_it(phone):
    phone.package(NEW)
    before = phone.boot()
    ok(phone.run("boot", uname=NEW))
    assert phone.get("installed") == NEW and phone.boot() == before


def test_first_start_does_nothing_once_a_kernel_is_installed(phone):
    phone.package(NEW)
    before = phone.boot()
    phone.state.mkdir(parents=True)
    (phone.state / "installed").write_text(ORIG + "\n")
    ok(phone.run("boot"))
    assert phone.boot() == before


def test_first_start_without_a_package_kernel_does_nothing(phone):
    before = phone.boot()
    r = ok(phone.run("boot"))
    assert r.stdout == "" and phone.boot() == before
    assert phone.get("installed") is None


# --- a new system from the image: the package's kernel runs, an older one is the fallback -------

OLDER = "6.1.192-plinius23"


def test_first_start_with_the_image_kernel_sets_up_the_kept_fallback(phone):
    phone.package(NEW)
    phone.kept(OLDER)
    # Plinius Boot wrote the image's boot.img: the package's kernel inside UT's AVB data
    (phone.parts / "boot_b").write_bytes(avb_partition(boot_img(NEW), PART_SIZE, VB_OFF))
    before = phone.boot()
    r = ok(phone.run("boot", uname=NEW))
    assert phone.get("installed") == NEW and phone.get("previous") == OLDER
    assert phone.boot() == before
    assert (phone.state / "boot-orig.img").read_bytes() == before
    # The fallback image: the older kernel inside this partition's AVB data, valid hash included
    prev = (phone.state / "boot-previous.img").read_bytes()
    old_img = boot_img(OLDER)
    assert len(prev) == PART_SIZE and prev[:len(old_img)] == old_img
    avb = read_avb(prev)
    import hashlib
    assert avb["desc_size"] == len(old_img)
    assert avb["digest"] == hashlib.sha256(avb["salt"] + old_img).digest()
    assert (phone.state / "boot-vbmeta.bin").read_bytes()[:4] == b"AVB0"
    assert not (phone.state / "boot-previous.img.new").exists()
    assert f"fallback: kernel {OLDER}" in r.stdout
    # restore starts it, and its modules come from the kept copy
    ok(phone.run("restore", uname=NEW))
    assert phone.boot() == prev and phone.get("installed") == OLDER
    ok(phone.run("dlkm", uname=OLDER))
    assert str(phone.state / "kept" / OLDER / "system_dlkm.img") in phone.fake_bin.calls("mount")[0]
    # keep leaves the fallback alone
    ok(phone.run("keep", uname=OLDER))
    assert (phone.state / "kept" / OLDER / "boot.img").read_bytes() == old_img


def test_first_start_with_the_image_kernel_and_the_boot_menu(phone, tmp_path):
    need_inode_order(tmp_path)
    phone.package(NEW)
    phone.kept(OLDER)
    phone.boot_menu()
    ok(phone.run("boot", uname=NEW))
    assert card_list(phone.card) == ["real.conf", "plinius-previous.conf"]
    assert last_index(phone.card) == 0
    conf = entry(phone, "plinius-previous")
    assert f"title Arch Linux (previous kernel {OLDER})" in conf
    assert "plinius.kernel=previous" in conf
    assert (phone.card / "plinius-previous/zImage").read_bytes() == kernel_blob(OLDER)
    assert phone.accepted() == [["accept", "--written", "abm-entry:plinius-previous"]]
    # A fallback entry already there stays as it is
    (phone.card / "plinius-previous/zImage").write_bytes(b"other")
    (phone.state / "installed").unlink()
    ok(phone.run("boot", uname=NEW))
    assert (phone.card / "plinius-previous/zImage").read_bytes() == b"other"
    assert len(phone.accepted()) == 1


def test_first_start_on_a_card_system_with_the_image_kernel(phone, tmp_path):
    need_inode_order(tmp_path)
    phone.package(NEW)
    phone.kept(OLDER)
    own = phone.card_system(kernel=NEW)
    write_last_index(phone.card, 1)  # Plinius Boot starts the system's own entry
    base = entry(phone, "plinius").split("options ", 1)[1].split("\n")[0]
    r = ok(phone.run("boot", uname=NEW))
    assert phone.get("installed") == NEW and phone.get("entry") == "plinius"
    assert card_list(phone.card)[:2] == ["real.conf", "plinius.conf"]
    assert "plinius-kernel-previous.conf" in card_list(phone.card)
    assert card_list(phone.card)[last_index(phone.card)] == "plinius.conf"
    conf = entry(phone, "plinius-kernel-previous")
    assert f"title Arch Linux (SD) (previous kernel {OLDER})\n" in conf
    assert "linux plinius-kernel-previous/zImage\n" in conf
    assert f"options {base} plinius.kernel=previous\n" in conf
    assert "initrd plinius/initrd.cpio.gz\n" in conf
    assert (phone.card / "plinius-kernel-previous/zImage").read_bytes() == kernel_blob(OLDER)
    assert (own / "zImage").read_bytes() == kernel_blob(NEW)
    assert not (phone.state / "boot-previous.img").exists()
    assert "plinius-kernel-previous" in r.stdout
    # restore puts the fallback into the system's entry
    ok(phone.run("restore", uname=NEW))
    assert (own / "zImage").read_bytes() == kernel_blob(OLDER)
    # Once there, the fallback entry is not written again
    (phone.card / "plinius-kernel-previous/zImage").write_bytes(b"other")
    ok(phone.call("first_fallback"))
    assert (phone.card / "plinius-kernel-previous/zImage").read_bytes() == b"other"


def test_first_start_on_a_card_system_without_the_boot_menu_stops(phone):
    phone.package(NEW)
    phone.kept(OLDER)
    phone.cmdline("systempart=/dev/mmcblk0p2")
    r = fails(phone.run("boot", uname=NEW))
    assert "boot menu is missing" in r.stderr


def test_first_start_without_a_kept_fallback_only_records_the_kernel(phone):
    phone.package(NEW)
    # Named as previous, but its files are not there
    phone.state.mkdir(parents=True)
    (phone.state / "previous").write_text(OLDER + "\n")
    ok(phone.run("boot", uname=NEW))
    assert phone.get("installed") == NEW
    assert not (phone.state / "boot-previous.img").exists()


def test_first_start_with_an_image_fallback_but_another_kernel_running(phone):
    # A system that started with UT's kernel all the same: the package's kernel is set up as an
    # update, the kernel that ran becomes the fallback
    phone.package(NEW)
    phone.kept(OLDER)
    ok(phone.run("boot", uname=ORIG))
    assert phone.get("installed") == NEW and phone.get("previous") == ORIG
    assert (phone.state / "boot-previous.img").read_bytes() == avb_partition(
        phone.orig_img, PART_SIZE, VB_OFF)


def test_first_start_fallback_needs_the_boot_partition(phone):
    phone.kept(OLDER)
    (phone.parts / "boot_b").unlink()
    r = fails(phone.call("first_fallback"))
    assert "boot_b missing" in r.stderr


def test_first_start_fallback_without_avb_data_to_keep_stops(phone):
    phone.kept(OLDER)
    part = phone.parts / "boot_b"
    data = bytearray(part.read_bytes())
    data[VB_OFF:VB_OFF + 4] = b"XXXX"  # the footer names a broken block, no copy kept
    part.write_bytes(bytes(data))
    r = fails(phone.call("first_fallback"))
    assert "no fallback image" in r.stderr
    assert not (phone.state / "boot-previous.img").exists()


def test_first_start_off_the_phone_does_nothing(phone):
    # The image's preview in QEMU: no boot partitions, no card system
    phone.package(NEW)
    phone.kept(OLDER)
    for p in phone.parts.glob("boot_*"):
        p.unlink()
    r = ok(phone.run("boot", uname="6.17.0-qemu"))
    assert r.stdout == "" and phone.get("installed") is None


def test_avb_tool_usage_and_a_partition_without_footer(phone, tmp_path):
    r = subprocess.run(["python3", str(AVB)], capture_output=True, text=True)
    assert r.returncode != 0 and "Usage: plinius-kernel-avb" in r.stderr
    part = tmp_path / "plain.img"
    part.write_bytes(b"\xee" * PART_SIZE)
    img = tmp_path / "k.img"
    img.write_bytes(boot_img(NEW))
    out = tmp_path / "out.img"
    r = subprocess.run(["python3", str(AVB), str(img), str(part), str(out)], capture_output=True,
                       text=True)
    assert r.returncode == 0 and "no AVB footer" in r.stderr
    assert out.read_bytes() == boot_img(NEW)
    # Without a place to keep it, a valid block is used and nothing else is written
    part.write_bytes(avb_partition(boot_img(ORIG), PART_SIZE, VB_OFF))
    r = subprocess.run(["python3", str(AVB), str(img), str(part), str(out)], capture_output=True,
                       text=True)
    assert r.returncode == 0 and read_avb(out.read_bytes())["desc_size"] == len(boot_img(NEW))
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.endswith(".new")) == []


def test_card_order_keeps_the_autostart_on_the_same_entry(phone, tmp_path):
    need_inode_order(tmp_path)
    # Another tool's entries come first on the card and the autostart names one of them
    phone.boot_menu()
    entries = phone.card / "db/entries"
    (entries / "real.conf").unlink()
    (entries / "crypt-trial.conf").write_text("title crypt trial\nxpart real\n")
    (entries / "android.conf").write_text("title Android\nxpart 1:2\n")
    (entries / "real.conf").write_text("title Arch Linux\n")
    (entries / "plinius-previous.conf").write_text("title previous\n")
    assert card_list(phone.card) == ["crypt-trial.conf", "android.conf", "real.conf",
                                     "plinius-previous.conf"]
    write_last_index(phone.card, 1)  # android.conf
    ok(phone.call(f'card_order "{phone.card}"'))
    order = card_list(phone.card)
    assert order[:2] == ["real.conf", "plinius-previous.conf"]
    assert sorted(order[2:]) == ["android.conf", "crypt-trial.conf"]
    assert order[last_index(phone.card)] == "android.conf"
    # The entry files survive the swap unchanged
    assert (entries / "android.conf").read_text() == "title Android\nxpart 1:2\n"
    assert not (phone.card / "db/.entries.new").exists()


def test_card_autostart_names_the_entry_by_its_position(phone, tmp_path):
    need_inode_order(tmp_path)
    phone.boot_menu(extra=("android",))
    ok(phone.call("card_autostart android"))
    assert last_index(phone.card) == 1
    r = fails(phone.call("card_autostart missing"))
    assert "no entry missing" in r.stderr


def test_a_card_without_boot_menu_is_not_used(phone):
    phone.package(NEW)
    # A plain memory card (photos, no Android Boot Manager)
    phone.card.mkdir()
    (phone.card / "DCIM").mkdir()
    phone.card_dev.write_text(str(phone.card))
    r = phone.run("install", NEW)
    assert r.returncode == 0, r.stderr
    new_img = (phone.lib / NEW / "boot.img").read_bytes()
    assert phone.boot()[:len(new_img)] == new_img
    assert sorted(p.name for p in phone.card.iterdir()) == ["DCIM"]


def test_status_reports_the_state(phone, tmp_path):
    need_inode_order(tmp_path)
    phone.package(NEW)
    phone.boot_menu()
    ok(phone.run("install", NEW))
    phone.cmdline("plinius.kernel=previous")
    r = ok(phone.run("status", uname=ORIG))
    assert r.stdout.splitlines() == [
        f"running:   {ORIG} previous", f"installed: {NEW}", f"fallback:  {ORIG}",
        "card:      boot menu found"]


# --- keep and dlkm: the files of releases still in use ------------------------------------------

def test_keep_copies_releases_in_use_and_drops_the_rest(phone):
    phone.package(NEW)
    phone.package(NEWER)
    phone.package("6.1.150-plinius3")  # installed once, no longer in use
    kept = phone.state / "kept"
    (kept / "6.1.140-plinius1").mkdir(parents=True)  # an old kept copy
    phone.state.joinpath("installed").write_text(NEW + "\n")
    phone.state.joinpath("previous").write_text(NEWER + "\n")
    ok(phone.run("keep", uname=ORIG))
    assert sorted(p.name for p in kept.iterdir()) == sorted([NEW, NEWER])
    assert (kept / NEW / "boot.img").read_bytes() == (phone.lib / NEW / "boot.img").read_bytes()
    assert (kept / NEW / "modules/plinius-backlight.ko").read_bytes() == b"ko " + NEW.encode()


def test_keep_keeps_a_copy_whose_package_is_already_gone(phone):
    kept = phone.state / "kept"
    (kept / NEW).mkdir(parents=True)
    (kept / NEW / "boot.img").write_bytes(b"x")
    phone.state.joinpath("previous").write_text(NEW + "\n")
    ok(phone.run("keep", uname=ORIG))
    assert (kept / NEW / "boot.img").read_bytes() == b"x"


def test_keep_keeps_the_running_kernel(phone):
    phone.package(NEW)
    ok(phone.run("keep", uname=NEW))
    assert (phone.state / "kept" / NEW / "system_dlkm.img").exists()


def test_dlkm_binds_the_modules_of_the_running_kernel(phone):
    phone.package(NEW)
    ok(phone.run("dlkm", uname=NEW))
    assert phone.fake_bin.calls("mount") == [[
        "--bind", "-o", "ro", str(phone.lib / NEW / "system_dlkm.img"),
        str(phone.root / "var/lib/lxc/android/system_dlkm.img")]]


def test_dlkm_uses_the_kept_copy(phone):
    d = phone.state / "kept" / NEW
    d.mkdir(parents=True)
    (d / "system_dlkm.img").write_bytes(b"erofs")
    ok(phone.run("dlkm", uname=NEW))
    assert phone.fake_bin.calls("mount")[0][3] == str(d / "system_dlkm.img")


@pytest.mark.parametrize("mounted", ["0", "1"])
def test_dlkm_leaves_the_original_for_ut_kernel_or_when_mounted(phone, mounted):
    phone.package(NEW)
    uname = NEW if mounted == "0" else ORIG
    ok(phone.run("dlkm", uname=uname, FAKE_MOUNTPOINT=mounted))
    assert phone.fake_bin.calls("mount") == []


# --- together with plinius-bootcheck ------------------------------------------------------------

def test_kernel_update_raises_no_boot_chain_alarm(phone, tmp_path):
    """What an update writes is accepted by plinius-bootcheck, so the check at the next start stays
    quiet; a change by anyone else still raises the alarm."""
    from release_sim import bootcheck_copy
    need_inode_order(tmp_path)
    phone.package(NEW)
    phone.boot_menu()
    lists = phone.root / "usr/lib/plinius-bootcheck.d"
    lists.mkdir(parents=True)
    (lists / "device.list").write_text(f"{phone.parts}/boot@slot\n{phone.parts}/vendor_boot@slot\n"
                                       f"abm {phone.card_dev}\n")
    bootcheck = bootcheck_copy(PKG / "plinius-bootcheck" / "plinius-bootcheck",
                               phone.fake_bin.path / "plinius-bootcheck", phone.root)
    phone.fake_bin.stub("id", "echo 0")

    def check():
        env = phone.fake_bin.env(isolated=True)
        return subprocess.run(["bash", str(bootcheck), "check"], env=env, capture_output=True,
                              text=True, timeout=60)

    assert check().returncode == 0  # the first reference
    r = ok(phone.run("install", NEW))
    assert "did not take" not in r.stdout
    r = check()
    assert r.returncode == 0, r.stderr  # boot<slot> and the fallback entry were accepted, no alarm
    (phone.card / "plinius-previous/zImage").write_bytes(b"swapped by someone")
    r = check()
    assert r.returncode == 2 and "abm:/plinius-previous/zImage" in r.stderr


def test_card_system_on_its_data_partition_is_a_card_system(phone, tmp_path):
    """The system in rootfs.img on its data partition: the command line names only datapart=, and the
    kernel goes into its own entry, never into boot<slot>."""
    need_inode_order(tmp_path)
    phone.package(NEW)
    own = phone.card_system(in_data=True)
    ok(phone.run("install", NEW))
    assert (own / "zImage").read_bytes() == kernel_blob(NEW)
    assert phone.boot() == avb_partition(phone.orig_img, PART_SIZE, VB_OFF)
    assert phone.get("entry") == "plinius"
    assert card_list(phone.card) == ["real.conf", "plinius.conf", "plinius-kernel-previous.conf"]
    conf = entry(phone, "plinius-kernel-previous")
    assert f"options {VENDOR_CMDLINE} datapart=/dev/mmcblk0p3 plinius.kernel=previous\n" in conf + "\n"
    ok(phone.run("restore", uname=NEW))
    assert (own / "zImage").read_bytes() == kernel_blob(ORIG)
    assert phone.accepted()[-1] == ["accept", "--written", "abm-entry:plinius"]


def test_a_data_partition_in_the_phone_is_no_card_system(phone):
    """datapart= naming the phone's own storage is the Arch there: its kernel goes into boot<slot>."""
    phone.package(NEW)
    phone.cmdline(f"{VENDOR_CMDLINE} datapart=/dev/disk/by-partlabel/userdata")
    ok(phone.run("install", NEW))
    assert phone.get("installed") == NEW and phone.boot() != avb_partition(phone.orig_img, PART_SIZE, VB_OFF)


def test_card_system_entry_matches_its_whole_word(phone, tmp_path):
    """Another system on the data partition mmcblk0p31 is not this one on mmcblk0p3."""
    need_inode_order(tmp_path)
    phone.package(NEW)
    phone.card_system(in_data=True)
    (phone.card / "db/entries/other.conf").write_text(
        "title Other\nlinux other/zImage\noptions datapart=/dev/mmcblk0p31\nxpart 30:31\n")
    ok(phone.run("install", NEW))
    assert phone.get("entry") == "plinius"
    (phone.card / "db/entries/plinius.conf").unlink()
    r = fails(phone.run("install", NEW))
    assert "no single entry for datapart=/dev/mmcblk0p3 in the boot menu (none)" in r.stderr


def test_first_start_on_a_card_system_reports_its_fallback_entry(phone, tmp_path):
    need_inode_order(tmp_path)
    phone.package(NEW)
    phone.kept(ORIG)
    phone.card_system(in_data=True, kernel=NEW)
    ok(phone.run("boot", uname=NEW))
    assert phone.get("installed") == NEW
    assert phone.accepted() == [["accept", "--written", "abm-entry:plinius-kernel-previous"]]
    phone.fake_bin.path.joinpath("plinius-bootcheck.log").unlink()
    phone.state.joinpath("installed").unlink()
    ok(phone.run("boot", uname=NEW))
    assert phone.accepted() == []  # the fallback entry was there already


def test_card_system_kernel_update_raises_no_boot_chain_alarm(phone, tmp_path):
    """On an Arch on the card plinius-bootcheck judges its entries; what an update writes there is
    accepted, a change by anyone else still raises the alarm."""
    from release_sim import bootcheck_copy
    need_inode_order(tmp_path)
    phone.package(NEW)
    own = phone.card_system(in_data=True)
    lists = phone.root / "usr/lib/plinius-bootcheck.d"
    lists.mkdir(parents=True)
    (lists / "device.list").write_text(f"{phone.parts}/boot@slot\nabm {phone.card_dev}\n")
    bootcheck = bootcheck_copy(PKG / "plinius-bootcheck" / "plinius-bootcheck",
                               phone.fake_bin.path / "plinius-bootcheck", phone.root)
    phone.fake_bin.stub("id", "echo 0")

    def check():
        env = phone.fake_bin.env(isolated=True)
        return subprocess.run(["bash", str(bootcheck), "check"], env=env, capture_output=True,
                              text=True, timeout=60)

    assert check().returncode == 0
    r = ok(phone.run("install", NEW))
    assert "did not take" not in r.stdout + r.stderr
    r = check()
    assert r.returncode == 0, r.stderr
    (own / "initrd.cpio.gz").write_bytes(b"a prompt that keeps the password")
    r = check()
    assert r.returncode == 2 and "abm:/plinius/initrd.cpio.gz" in r.stderr


# --- the pacman hooks and the unit call it the way it expects ------------------------------------

def hook_fields(path):
    fields = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = (x.strip() for x in line.split("=", 1))
            fields.setdefault(k, []).append(v)
        elif line.strip() == "NeedsTargets":
            fields["NeedsTargets"] = [True]
    return fields


def test_install_hook_passes_the_boot_image_paths(phone):
    h = hook_fields(PKG / "plinius-kernel" / "90-plinius-kernel.hook")
    assert h["Operation"] == ["Install", "Upgrade"] and h["When"] == ["PostTransaction"]
    assert h["Target"] == ["usr/lib/plinius/kernel/*/boot.img"] and h["NeedsTargets"] == [True]
    assert h["Exec"] == ["/usr/lib/plinius/kernel/plinius-kernel-install hook"]
    # pacman passes the matching targets, one per line: the release is the directory name
    phone.package(NEW)
    ok(phone.run("hook", stdin=h["Target"][0].replace("*", NEW) + "\n"))
    assert phone.get("installed") == NEW


def test_keep_hook_runs_before_upgrade_and_removal():
    h = hook_fields(PKG / "plinius-kernel" / "10-plinius-kernel-keep.hook")
    assert h["Operation"] == ["Upgrade", "Remove"] and h["When"] == ["PreTransaction"]
    assert h["Exec"] == ["/usr/lib/plinius/kernel/plinius-kernel-install keep"]


def test_boot_unit_runs_only_while_no_kernel_is_set_up():
    unit = (PKG / "plinius-kernel" / "plinius-kernel-boot.service").read_text()
    script = SRC.read_text()
    assert "state=/var/lib/plinius-kernel\n" in script and "put installed " in script
    assert "put trial " not in script
    conditions = [line for line in unit.splitlines() if line.startswith("Condition")]
    assert conditions == ["ConditionPathExists=!/var/lib/plinius-kernel/installed"]
    assert "ExecStart=/usr/lib/plinius/kernel/plinius-kernel-install boot\n" in unit


def test_rollback_hook_keeps_the_previous_version_in_the_cache():
    h = hook_fields(PKG / "plinius-rollback" / "plinius-rollback.hook")
    assert h["When"] == ["PostTransaction"]
    exe = h["Exec"][0].split()
    assert exe[0] == "/usr/bin/paccache" and "--remove" in exe
    assert exe[exe.index("--keep") + 1] == "2"
