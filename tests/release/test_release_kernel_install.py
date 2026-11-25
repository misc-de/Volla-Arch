# plinius-kernel-install: which kernel ends up in boot<slot>, the trial start from the boot menu
# card, the fallback, and the AVB data of the boot partition.
#
# The script runs as a copy whose system paths (/usr/lib/plinius, /var/lib/plinius-kernel,
# /dev/disk/by-partlabel, /dev/mmcblk0p1, /proc, /sys/fs/pstore) lie in tmp_path. Partitions are
# plain files ("-b" becomes "-e" in the copy), the card is a directory that a debugfs stand-in
# reads and that findmnt reports as mounted. Every other program that changes state is a stub.

import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import PKG
from release_sim import (DEBUGFS, avb_partition, boot_img, card_list, copy_script, init_boot,
                         kernel_blob, last_index, need_inode_order, read_avb, vendor_boot,
                         write_last_index)

SRC = PKG / "plinius-kernel" / "plinius-kernel-install"

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
    "/sys/fs/pstore/console-ramoops-0": "@ROOT@/sys/fs/pstore/console-ramoops-0",
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
                  self.root / "var/lib/lxc/android", self.root / "sys/fs/pstore"):
            d.mkdir(parents=True, exist_ok=True)
        self.script = copy_script(SRC, tmp_path / "plinius-kernel-install", MAPPING, self.root)
        self.functions = copy_script(SRC, tmp_path / "kernel-functions.sh", MAPPING, self.root,
                                     cut="\ncase ${1:-} in")
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
        fake_bin.stub("systemctl", '''
case "$*" in
"is-system-running --wait") exit 0 ;;
is-system-running) echo "$FAKE_SYSSTATE"; [[ $FAKE_SYSSTATE == running ]] ;;
"-q is-active graphical.target") [[ $FAKE_GRAPHICAL == active ]] ;;
*) exit 1 ;;
esac''')
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

    def card_system(self, entry="plinius", sp="/dev/mmcblk0p2", kernel=ORIG):
        """An Arch on card partitions as Plinius Boot sets it up: its own entry next to real.conf,
        and the command line of a start from it."""
        self.boot_menu()
        (self.card / entry).mkdir()
        (self.card / entry / "zImage").write_bytes(kernel_blob(kernel))
        (self.card / entry / "initrd.cpio.gz").write_bytes(b"SD-RAMDISK")
        (self.card / "db/entries" / f"{entry}.conf").write_text(
            f"title Arch Linux (SD)\nlinux {entry}/zImage\ninitrd {entry}/initrd.cpio.gz\n"
            f"dtb {entry}/dtb.dtb\noptions {VENDOR_CMDLINE} systempart={sp} datapart=/dev/mmcblk0p3\n"
            "xtype UT\nxpart 2:3\n")
        self.cmdline(f"{VENDOR_CMDLINE} systempart={sp} datapart=/dev/mmcblk0p3")
        return self.card / entry

    # --- running ---
    def run(self, *args, uname=ORIG, sysstate="running", graphical="inactive", stdin=None,
            **extra):
        env = self.fake_bin.env(isolated=True, FAKE_UNAME=uname, FAKE_SYSSTATE=sysstate,
                                FAKE_GRAPHICAL=graphical, **extra)
        return subprocess.run(["bash", str(self.script), *args], env=env, input=stdin,
                              capture_output=True, text=True, timeout=60)

    def call(self, code, **extra):
        """Runs bash code with the script's functions defined."""
        env = self.fake_bin.env(isolated=True, FAKE_UNAME=ORIG, FAKE_SYSSTATE="running",
                                FAKE_GRAPHICAL="inactive", **extra)
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
    assert f"Fallback: {ORIG}" in r.stdout


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
    assert phone.get("trial") is None


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


# --- trial start from the boot menu card --------------------------------------------------------

def entry(phone, name):
    return (phone.card / "db/entries" / f"{name}.conf").read_text()


def test_install_with_card_creates_a_trial_entry_and_leaves_the_partition(phone, tmp_path):
    need_inode_order(tmp_path)
    phone.package(NEW)
    phone.boot_menu()
    r = ok(phone.run("install", NEW, PLINIUS_KERNEL_OPTIONS="initcall_debug"))
    assert phone.boot() == avb_partition(phone.orig_img, PART_SIZE, VB_OFF)
    assert phone.get("trial") == NEW
    assert phone.get("installed") == ORIG  # read from the partition
    assert card_list(phone.card) == ["real.conf", "plinius-trial.conf"]
    assert last_index(phone.card) == 1  # the next start runs the trial
    conf = entry(phone, "plinius-trial")
    assert f"title Arch Linux (new kernel {NEW}, trial)" in conf
    assert "linux plinius-trial/zImage\n" in conf
    assert f"options {VENDOR_CMDLINE} plinius.kernel=trial initcall_debug\n" in conf
    assert "xpart real\n" in conf and "logo real/logo.bin\n" in conf
    d = phone.card / "plinius-trial"
    assert (d / "zImage").read_bytes() == kernel_blob(NEW)
    assert (d / "initrd.cpio.gz").read_bytes() == phone.vendor_ramdisk + phone.generic_ramdisk
    assert (d / "dtb.dtb").read_bytes() == phone.dtb
    assert (d / "entry.conf").read_text() == conf
    assert phone.accepted() == [["accept", "--written", "abm-entry:plinius-trial"]]
    assert "trial" in r.stdout


def test_trial_confirmed_after_a_successful_start(phone, tmp_path):
    need_inode_order(tmp_path)
    phone.package(NEW)
    phone.boot_menu()
    ok(phone.run("install", NEW))
    phone.cmdline(VENDOR_CMDLINE + " plinius.kernel=trial")
    ok(phone.run("boot", uname=NEW))
    new_img = (phone.lib / NEW / "boot.img").read_bytes()
    assert phone.boot()[:len(new_img)] == new_img
    assert read_avb(phone.boot())["desc_size"] == len(new_img)
    assert phone.get("installed") == NEW
    assert phone.get("previous") == ORIG
    assert phone.get("trial") is None
    # Menu: "Arch Linux" first, the previous kernel right below, no trial; autostart on Arch Linux
    assert card_list(phone.card) == ["real.conf", "plinius-previous.conf"]
    assert not (phone.card / "plinius-trial").exists()
    assert last_index(phone.card) == 0
    conf = entry(phone, "plinius-previous")
    assert f"title Arch Linux (previous kernel {ORIG})" in conf
    assert "plinius.kernel=previous" in conf
    assert (phone.card / "plinius-previous/zImage").read_bytes() == kernel_blob(ORIG)
    assert phone.accepted()[-1] == ["accept", "--written", str(phone.parts / "boot_b"),
                                    "abm-entry:plinius-previous"]


def test_second_update_makes_the_confirmed_kernel_the_previous_one(phone, tmp_path):
    need_inode_order(tmp_path)
    phone.package(NEW)
    phone.package(NEWER)
    phone.boot_menu()
    ok(phone.run("install", NEW))
    phone.cmdline("plinius.kernel=trial")
    ok(phone.run("boot", uname=NEW))
    phone.cmdline("")
    ok(phone.run("install", NEWER, uname=NEW))
    assert card_list(phone.card) == ["real.conf", "plinius-previous.conf", "plinius-trial.conf"]
    assert last_index(phone.card) == 2
    phone.cmdline("plinius.kernel=trial")
    ok(phone.run("boot", uname=NEWER))
    assert phone.get("installed") == NEWER and phone.get("previous") == NEW
    assert (phone.card / "plinius-previous/zImage").read_bytes() == kernel_blob(NEW)
    assert card_list(phone.card) == ["real.conf", "plinius-previous.conf"]
    assert last_index(phone.card) == 0


def test_trial_stays_when_the_system_does_not_come_up(phone, tmp_path):
    need_inode_order(tmp_path)
    phone.package(NEW)
    phone.boot_menu()
    ok(phone.run("install", NEW))
    phone.cmdline("plinius.kernel=trial")
    r = ok(phone.run("boot", uname=NEW, sysstate="starting", graphical="inactive"))
    assert "stays a trial" in r.stdout
    assert phone.boot() == avb_partition(phone.orig_img, PART_SIZE, VB_OFF)
    assert phone.get("trial") == NEW and phone.get("installed") == ORIG
    assert card_list(phone.card) == ["real.conf", "plinius-trial.conf"]


@pytest.mark.parametrize("sysstate,graphical", [("degraded", "inactive"), ("starting", "active")])
def test_trial_confirmed_when_degraded_or_the_session_is_up(phone, tmp_path, sysstate, graphical):
    need_inode_order(tmp_path)
    phone.package(NEW)
    phone.boot_menu()
    ok(phone.run("install", NEW))
    phone.cmdline("plinius.kernel=trial")
    ok(phone.run("boot", uname=NEW, sysstate=sysstate, graphical=graphical))
    assert phone.get("installed") == NEW and phone.get("trial") is None


def test_trial_dropped_when_the_menu_started_the_old_kernel(phone, tmp_path):
    need_inode_order(tmp_path)
    phone.package(NEW)
    phone.boot_menu()
    ok(phone.run("install", NEW))
    # The trial hung; "Arch Linux" in the menu started the kernel in boot<slot>
    (phone.root / "sys/fs/pstore/console-ramoops-0").write_text("[    3.1] panic in trial\n")
    phone.cmdline(VENDOR_CMDLINE)
    r = ok(phone.run("boot", uname=ORIG))
    assert "was not confirmed" in r.stdout
    assert phone.boot() == avb_partition(phone.orig_img, PART_SIZE, VB_OFF)
    assert phone.get("trial") is None and phone.get("failed") == NEW
    assert phone.get("installed") == ORIG
    assert card_list(phone.card) == ["real.conf"]
    assert not (phone.card / "plinius-trial").exists()
    assert last_index(phone.card) == 0
    assert (phone.state / f"console-{NEW}.log").read_text() == "[    3.1] panic in trial\n"


def test_trial_dropped_when_another_kernel_runs_with_the_trial_role(phone, tmp_path):
    need_inode_order(tmp_path)
    phone.package(NEW)
    phone.boot_menu()
    ok(phone.run("install", NEW))
    phone.cmdline("plinius.kernel=trial")
    ok(phone.run("boot", uname=ORIG))
    assert phone.get("trial") is None and phone.get("failed") == NEW
    assert phone.get("installed") == ORIG


def test_trial_dropped_without_card_when_the_card_was_taken_out(phone, tmp_path):
    need_inode_order(tmp_path)
    phone.package(NEW)
    phone.boot_menu()
    ok(phone.run("install", NEW))
    phone.card_dev.unlink()
    ok(phone.run("boot", uname=ORIG))
    assert phone.get("trial") is None and phone.get("failed") == NEW


def test_cut_off_confirmation_is_finished_at_the_next_start(phone, tmp_path):
    need_inode_order(tmp_path)
    phone.package(NEW)
    phone.boot_menu()
    ok(phone.run("install", NEW))
    # The restart came after boot<slot> was written and noted, before the card was done
    ok(phone.call(f'save_fallback "{phone.parts / "boot_b"}" {ORIG}; '
                  f'write_boot "{phone.lib / NEW / "boot.img"}" "{phone.parts / "boot_b"}"; '
                  f'put installed {NEW}'))
    assert card_list(phone.card) == ["real.conf", "plinius-trial.conf"]
    phone.cmdline("plinius.kernel=trial")
    ok(phone.run("boot", uname=NEW))
    assert phone.get("trial") is None
    assert card_list(phone.card) == ["real.conf", "plinius-previous.conf"]
    assert last_index(phone.card) == 0
    assert (phone.card / "plinius-previous/zImage").read_bytes() == kernel_blob(ORIG)


def test_boot_without_a_trial_does_nothing(phone):
    before = phone.boot()
    ok(phone.run("boot"))
    assert phone.boot() == before
    assert phone.accepted() == []
    assert not phone.state.exists() or not any(phone.state.iterdir())


# --- an Arch on a memory card partition: its own boot menu entry ---------------------------------

def test_card_system_install_creates_a_trial_of_its_own_entry(phone, tmp_path):
    need_inode_order(tmp_path)
    phone.package(NEW)
    own = phone.card_system()
    r = ok(phone.run("install", NEW, PLINIUS_KERNEL_OPTIONS="initcall_debug"))
    # Nothing in the partition, the entry's kernel untouched
    assert phone.boot() == avb_partition(phone.orig_img, PART_SIZE, VB_OFF)
    assert (own / "zImage").read_bytes() == kernel_blob(ORIG)
    assert phone.get("trial") == NEW and phone.get("installed") == ORIG and phone.get("entry") == "plinius"
    assert phone.get("replace_previous") == "1"
    assert card_list(phone.card) == ["real.conf", "plinius.conf", "plinius-kernel-trial.conf"]
    assert last_index(phone.card) == 2
    conf = entry(phone, "plinius-kernel-trial")
    assert conf.splitlines() == [
        f"title Arch Linux (SD) (new kernel {NEW}, trial)", "linux plinius-kernel-trial/zImage",
        "initrd plinius/initrd.cpio.gz", "dtb plinius/dtb.dtb", "xtype UT", "xpart 2:3",
        f"options {VENDOR_CMDLINE} systempart=/dev/mmcblk0p2 datapart=/dev/mmcblk0p3 "
        "plinius.kernel=trial initcall_debug"]
    assert (phone.card / "plinius-kernel-trial/zImage").read_bytes() == kernel_blob(NEW)
    # plinius-bootcheck leaves card systems alone
    assert phone.accepted() == []
    assert "Arch Linux (SD)" in r.stdout


def test_card_system_trial_confirmed_goes_into_the_entry(phone, tmp_path):
    need_inode_order(tmp_path)
    phone.package(NEW)
    own = phone.card_system()
    ok(phone.run("install", NEW))
    phone.cmdline(entry(phone, "plinius-kernel-trial").split("options ", 1)[1].split("\n")[0])
    ok(phone.run("boot", uname=NEW))
    assert (own / "zImage").read_bytes() == kernel_blob(NEW)
    assert (own / "initrd.cpio.gz").read_bytes() == b"SD-RAMDISK"
    assert phone.boot() == avb_partition(phone.orig_img, PART_SIZE, VB_OFF)
    assert phone.get("installed") == NEW and phone.get("previous") == ORIG
    assert phone.get("trial") is None and phone.get("replace_previous") is None
    assert card_list(phone.card) == ["real.conf", "plinius.conf", "plinius-kernel-previous.conf"]
    assert not (phone.card / "plinius-kernel-trial").exists()
    assert last_index(phone.card) == 1  # the entry of the card system
    conf = entry(phone, "plinius-kernel-previous")
    assert f"title Arch Linux (SD) (previous kernel {ORIG})" in conf
    assert "initrd plinius/initrd.cpio.gz\n" in conf
    assert conf.rstrip().endswith("datapart=/dev/mmcblk0p3 plinius.kernel=previous")
    assert "plinius.kernel=trial" not in conf
    assert (phone.card / "plinius-kernel-previous/zImage").read_bytes() == kernel_blob(ORIG)
    assert phone.accepted() == []


def test_card_system_second_update_and_the_fallback_rule(phone, tmp_path):
    need_inode_order(tmp_path)
    phone.package(NEW)
    phone.package(NEWER)
    own = phone.card_system()
    ok(phone.run("install", NEW))
    base = entry(phone, "plinius").split("options ", 1)[1].split("\n")[0]
    phone.cmdline(base + " plinius.kernel=trial")
    ok(phone.run("boot", uname=NEW))
    # Running the fallback (the new kernel in the entry does not work): a newer one replaces only
    # the entry's kernel, the fallback that started for sure stays
    phone.cmdline(base + " plinius.kernel=previous")
    ok(phone.run("install", NEWER, uname=ORIG))
    assert phone.get("replace_previous") is None
    assert card_list(phone.card) == ["real.conf", "plinius.conf", "plinius-kernel-previous.conf",
                                     "plinius-kernel-trial.conf"]
    phone.cmdline(base + " plinius.kernel=trial")
    ok(phone.run("boot", uname=NEWER))
    assert (own / "zImage").read_bytes() == kernel_blob(NEWER)
    assert (phone.card / "plinius-kernel-previous/zImage").read_bytes() == kernel_blob(ORIG)
    assert phone.get("installed") == NEWER and phone.get("previous") == ORIG


def test_card_system_update_while_running_the_entry_makes_it_the_fallback(phone, tmp_path):
    need_inode_order(tmp_path)
    phone.package(NEW)
    phone.package(NEWER)
    own = phone.card_system()
    base = entry(phone, "plinius").split("options ", 1)[1].split("\n")[0]
    ok(phone.run("install", NEW))
    phone.cmdline(base + " plinius.kernel=trial")
    ok(phone.run("boot", uname=NEW))
    phone.cmdline(base)
    ok(phone.run("install", NEWER, uname=NEW))
    phone.cmdline(base + " plinius.kernel=trial")
    ok(phone.run("boot", uname=NEWER))
    assert (own / "zImage").read_bytes() == kernel_blob(NEWER)
    assert (phone.card / "plinius-kernel-previous/zImage").read_bytes() == kernel_blob(NEW)
    assert phone.get("previous") == NEW


def test_card_system_trial_stays_while_the_system_is_not_up(phone, tmp_path):
    need_inode_order(tmp_path)
    phone.package(NEW)
    phone.card_system()
    ok(phone.run("install", NEW))
    phone.cmdline("systempart=/dev/mmcblk0p2 plinius.kernel=trial")
    r = ok(phone.run("boot", uname=NEW, sysstate="starting"))
    assert "stays a trial" in r.stdout
    assert phone.get("trial") == NEW and phone.get("installed") == ORIG
    assert (phone.card / "plinius/zImage").read_bytes() == kernel_blob(ORIG)


def test_card_system_trial_dropped_when_the_entry_started(phone, tmp_path):
    need_inode_order(tmp_path)
    phone.package(NEW)
    own = phone.card_system()
    ok(phone.run("install", NEW))
    (phone.root / "sys/fs/pstore/console-ramoops-0").write_text("lk: data fault")
    # The user chose "Arch Linux (SD)" in the menu: the old kernel runs without a role
    ok(phone.run("boot", uname=ORIG))
    assert phone.get("trial") is None and phone.get("failed") == NEW
    assert (own / "zImage").read_bytes() == kernel_blob(ORIG)
    assert card_list(phone.card) == ["real.conf", "plinius.conf"]
    assert last_index(phone.card) == 1
    assert (phone.state / f"console-{NEW}.log").read_text() == "lk: data fault"


def test_card_system_cut_off_confirmation_is_finished(phone, tmp_path):
    need_inode_order(tmp_path)
    phone.package(NEW)
    own = phone.card_system()
    ok(phone.run("install", NEW))
    # The kernel reached the entry, then the phone restarted before the trial entry went
    (own / "zImage").write_bytes(kernel_blob(NEW))
    (phone.state / "installed").write_text(NEW + "\n")
    ok(phone.run("boot", uname=NEW))
    assert phone.get("trial") is None
    assert card_list(phone.card) == ["real.conf", "plinius.conf"]
    assert last_index(phone.card) == 1


def test_card_system_install_of_the_kernel_already_in_its_entry(phone, tmp_path):
    need_inode_order(tmp_path)
    phone.package(NEW)
    phone.card_system(kernel=NEW)
    r = ok(phone.run("install", NEW, uname=NEW))
    assert "already in the boot menu entry plinius" in r.stdout
    assert phone.get("installed") == NEW and phone.get("trial") is None
    assert card_list(phone.card) == ["real.conf", "plinius.conf"]


def test_card_system_entries_of_other_systems_stay(phone, tmp_path):
    need_inode_order(tmp_path)
    phone.package(NEW)
    phone.card_system()
    # The Arch on the phone's storage has its own fallback; another card system its own entry
    (phone.card / "db/entries/plinius-previous.conf").write_text("title Arch Linux (previous)\nxpart real\n")
    (phone.card / "db/entries/rom3.conf").write_text(
        "title Other\nlinux rom3/zImage\noptions systempart=/dev/mmcblk0p5\nxpart 5:6\n")
    ok(phone.run("install", NEW))
    names = card_list(phone.card)
    assert names[:2] == ["real.conf", "plinius-previous.conf"]
    assert names.index("plinius-kernel-trial.conf") == names.index("plinius.conf") + 1
    assert "rom3.conf" in names


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
    base = entry(phone, "plinius").split("options ", 1)[1].split("\n")[0]
    ok(phone.run("install", NEW))
    phone.cmdline(base + " plinius.kernel=trial")
    ok(phone.run("boot", uname=NEW))
    phone.cmdline(base)
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

def test_first_start_on_a_card_system_sets_up_a_trial(phone, tmp_path):
    need_inode_order(tmp_path)
    phone.package(NEW)
    phone.card_system()
    r = ok(phone.run("boot"))
    assert f"setting up kernel {NEW}" in r.stdout
    assert phone.get("trial") == NEW and phone.get("installed") == ORIG
    assert "plinius-kernel-trial.conf" in card_list(phone.card)
    # Once: the next start (the trial) confirms it, a start after a failed trial does nothing
    phone.cmdline("systempart=/dev/mmcblk0p2")
    ok(phone.run("boot"))
    assert phone.get("failed") == NEW
    ok(phone.run("boot"))
    assert phone.get("trial") is None


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


def test_first_start_does_nothing_once_a_kernel_is_installed_or_failed(phone):
    phone.package(NEW)
    before = phone.boot()
    phone.state.mkdir(parents=True)
    (phone.state / "failed").write_text(NEW + "\n")
    ok(phone.run("boot"))
    assert phone.boot() == before and phone.get("installed") is None
    (phone.state / "failed").unlink()
    (phone.state / "installed").write_text(ORIG + "\n")
    ok(phone.run("boot"))
    assert phone.boot() == before


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
        f"running:   {ORIG} previous", f"installed: {ORIG}", "fallback:  ",
        f"trial:     {NEW}", "failed:    ", "card:      boot menu found"]


# --- keep and dlkm: the files of releases still in use ------------------------------------------

def test_keep_copies_releases_in_use_and_drops_the_rest(phone):
    phone.package(NEW)
    phone.package(NEWER)
    phone.package("6.1.150-plinius3")  # installed once, no longer in use
    kept = phone.state / "kept"
    (kept / "6.1.140-plinius1").mkdir(parents=True)  # an old kept copy
    phone.state.joinpath("installed").write_text(NEW + "\n")
    phone.state.joinpath("trial").write_text(NEWER + "\n")
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
    """What the trial and the confirmation write is accepted by plinius-bootcheck, so the check
    at the next start stays quiet; a change by anyone else still raises the alarm."""
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
    assert r.returncode == 0, r.stderr  # the trial entry was accepted, no alarm
    phone.cmdline("plinius.kernel=trial")
    r = ok(phone.run("boot", uname=NEW))
    assert "did not take" not in r.stdout
    r = check()
    assert r.returncode == 0, r.stderr
    (phone.card / "plinius-previous/zImage").write_bytes(b"swapped by someone")
    r = check()
    assert r.returncode == 2 and "abm:/plinius-previous/zImage" in r.stderr


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


def test_boot_unit_runs_with_a_trial_pending_or_no_kernel_set_up():
    unit = (PKG / "plinius-kernel" / "plinius-kernel-boot.service").read_text()
    script = SRC.read_text()
    assert "state=/var/lib/plinius-kernel\n" in script and "put trial " in script and "put installed " in script
    conditions = [line for line in unit.splitlines() if line.startswith("Condition")]
    assert conditions == ["ConditionPathExists=|/var/lib/plinius-kernel/trial",
                          "ConditionPathExists=|!/var/lib/plinius-kernel/installed"]
    assert "ExecStart=/usr/lib/plinius/kernel/plinius-kernel-install boot\n" in unit


def test_rollback_hook_keeps_the_previous_version_in_the_cache():
    h = hook_fields(PKG / "plinius-rollback" / "plinius-rollback.hook")
    assert h["When"] == ["PostTransaction"]
    exe = h["Exec"][0].split()
    assert exe[0] == "/usr/bin/paccache" and "--remove" in exe
    assert exe[exe.index("--keep") + 1] == "2"
