# image/mkimage.sh before it builds anything: the UT files it is given must be the ones pinned in
# tools/ut-build.sha256, and the PIN of -P follows the greeter's rule and reaches the image only as
# a yescrypt hash, through stdin and a file, never on a command line.
#
# The build itself (namespaces, the aarch64 chroot, an hour of package installs) does not run
# here. mkimage.sh runs as a copy in a tree without a build chroot: it stops at "build chroot
# missing", after the checks of its arguments. Its PIN functions run on their own (bash_functions);
# their hash goes through chpasswd -e as the build does it, into a small root in a user namespace.

import ctypes
import ctypes.util
import hashlib
import os
import re
import shutil
import subprocess

import pytest

from conftest import REPO, TOOLS, bash_functions

MKIMAGE = REPO / "image" / "mkimage.sh"
ANDROID = ["android-rootfs.img", "vendor.img", "vendor_dlkm.img", "system_dlkm.img"]
BOOT = ["boot.img", "vendor_boot.img", "init_boot.img"]
FILES = {**{b: b.encode() * 40 for b in BOOT}, **{f"android/{a}": a.encode() * 30 for a in ANDROID}}


@pytest.fixture
def tree(tmp_path):
    """A copy of mkimage.sh with lib.sh, pins for FILES, and a UT directory holding FILES."""
    root = tmp_path / "volla"
    (root / "tools").mkdir(parents=True)
    (root / "image").mkdir()
    shutil.copy2(MKIMAGE, root / "image" / "mkimage.sh")
    shutil.copy2(TOOLS / "lib.sh", root / "tools" / "lib.sh")
    (root / "tools" / "ut-build.sha256").write_text("# build 04112026\n" + "".join(
        f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in FILES.items()))
    ut = tmp_path / "ut"
    (ut / "android").mkdir(parents=True)
    for name, data in FILES.items():
        (ut / name).write_bytes(data)
    return root, ut


def mkimage(tree, fake_bin, *args):
    root, _ = tree
    return subprocess.run(["bash", str(root / "image" / "mkimage.sh"), *args], capture_output=True,
                          text=True, env=fake_bin.env(isolated=True), stdin=subprocess.DEVNULL)


def test_mkimage_takes_the_pinned_ut_files(tree, fake_bin):
    _, ut = tree
    r = mkimage(tree, fake_bin, "-a", str(ut / "android" / "android-rootfs.img"))
    assert "UT files as pinned in tools/ut-build.sha256" in r.stderr
    # ... and goes on to the next check (no chroot in this tree)
    assert r.returncode != 0 and "build chroot missing" in r.stderr


@pytest.mark.parametrize("name", list(FILES))
def test_mkimage_refuses_a_ut_file_that_differs(tree, fake_bin, name):
    _, ut = tree
    (ut / name).write_bytes(b"from a server in other hands")
    r = mkimage(tree, fake_bin, "-a", str(ut / "android" / "android-rootfs.img"))
    assert r.returncode != 0
    assert f"{ut / name} is not {name} of the UT build pinned in tools/ut-build.sha256" in r.stderr
    assert "build chroot missing" not in r.stderr, "the check comes before everything else"


def test_mkimage_checks_the_android_image_under_any_name(tree, fake_bin):
    # -a names the container image; whatever its name, it must be the pinned one
    _, ut = tree
    other = ut / "android" / "rootfs-copy.img"
    other.write_bytes(b"something else")
    r = mkimage(tree, fake_bin, "-a", str(other))
    assert r.returncode != 0 and "is not android/android-rootfs.img" in r.stderr


def test_mkimage_goes_without_the_optional_android_images(tree, fake_bin):
    _, ut = tree
    (ut / "android" / "vendor_dlkm.img").unlink()
    r = mkimage(tree, fake_bin, "-a", str(ut / "android" / "android-rootfs.img"))
    assert "Note: no vendor_dlkm.img next to" in r.stderr
    assert "UT files as pinned" in r.stderr and "build chroot missing" in r.stderr


def test_mkimage_needs_the_boot_images(tree, fake_bin):
    _, ut = tree
    (ut / "init_boot.img").unlink()
    r = mkimage(tree, fake_bin, "-a", str(ut / "android" / "android-rootfs.img"))
    assert r.returncode != 0
    assert f"{ut / 'init_boot.img'} missing (-a takes the boot images from there)" in r.stderr


def test_mkimage_refuses_a_file_that_is_not_pinned(tree, fake_bin):
    root, ut = tree
    sums = root / "tools" / "ut-build.sha256"
    sums.write_text("".join(line + "\n" for line in sums.read_text().splitlines()
                            if not line.endswith("boot.img") or "vendor" in line or "init" in line))
    r = mkimage(tree, fake_bin, "-a", str(ut / "android" / "android-rootfs.img"))
    assert r.returncode != 0 and "is not boot.img of the UT build pinned" in r.stderr


# --- the PIN -----------------------------------------------------------------------------------

def libcrypt():
    name = ctypes.util.find_library("crypt")
    if not name:
        pytest.skip("libxcrypt missing on the host")
    lib = ctypes.CDLL(name)
    lib.crypt.restype = ctypes.c_char_p
    lib.crypt.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
    return lib


def read_pin(fake_bin, answers, **env):
    """read_pin of mkimage.sh with answers on stdin; returns the CompletedProcess."""
    script = (f'set -euo pipefail\n. "{TOOLS / "lib.sh"}"\n'
              + bash_functions(MKIMAGE, "hash_pin", "read_pin") + "read_pin\n")
    return subprocess.run(["bash", "-c", script], input=answers, capture_output=True, text=True,
                          env=fake_bin.env(isolated=True, **env))


@pytest.mark.parametrize("pin", ["1234", "123456", "123456789012"])
def test_read_pin_hashes_with_yescrypt(fake_bin, pin):
    lib = libcrypt()
    r = read_pin(fake_bin, f"{pin}\n{pin}\n")
    assert r.returncode == 0, r.stderr
    hashed = r.stdout.strip()
    # yescrypt with libxcrypt's default cost, as on the phone (not sha512crypt, $6$)
    assert re.fullmatch(r"\$y\$j9T\$[./0-9A-Za-z]+\$[./0-9A-Za-z]{43}", hashed), hashed
    assert lib.crypt(pin.encode(), hashed.encode()).decode() == hashed
    assert lib.crypt(b"0000", hashed.encode()).decode() != hashed
    assert pin not in r.stdout + r.stderr


def test_read_pin_gives_a_fresh_salt_each_time(fake_bin):
    libcrypt()
    first = read_pin(fake_bin, "2468\n2468\n").stdout
    assert first != read_pin(fake_bin, "2468\n2468\n").stdout


@pytest.mark.parametrize("pin", ["", "1", "123", "1234567890123", "12a4", "12 34", "١٢٣٤", "-123"])
def test_read_pin_takes_only_4_to_12_digits(fake_bin, pin):
    # The greeter's rule (plinius-greeter PIN_DIGITS); Arabic-Indic digits are no ASCII digits
    r = read_pin(fake_bin, f"{pin}\n{pin}\n")
    assert r.returncode != 0
    assert "a PIN has 4 to 12 digits" in r.stderr
    assert not r.stdout.strip()


def test_read_pin_has_the_greeters_bounds(fake_bin):
    # The same rule as the greeter, read from it: a change there shows here
    text = (REPO / "pkgbuilds/plinius-greeter/plinius-greeter").read_text()
    low, high = map(int, re.search(r"^PIN_DIGITS = \((\d+), (\d+)\)$", text, re.M).groups())
    libcrypt()
    for length, ok in ((low - 1, False), (low, True), (high, True), (high + 1, False)):
        pin = "7" * length
        assert (read_pin(fake_bin, f"{pin}\n{pin}\n").returncode == 0) == ok, length


def test_read_pin_refuses_two_different_pins(fake_bin):
    r = read_pin(fake_bin, "1234\n1235\n")
    assert r.returncode != 0 and "the PINs differ" in r.stderr
    assert not r.stdout.strip()


def test_read_pin_puts_the_pin_on_no_command_line(fake_bin):
    # python3 logs its arguments and then runs the real one: the PIN reaches it on stdin only
    real = shutil.which("python3")
    fake_bin.stub("python3", f'exec "{real}" "$@"')
    libcrypt()
    r = read_pin(fake_bin, "97531\n97531\n")
    assert r.returncode == 0, r.stderr
    [call] = fake_bin.calls("python3")
    assert not any("97531" in arg for arg in call)


def test_read_pin_refuses_a_hash_that_is_not_yescrypt(fake_bin):
    # A libcrypt without yescrypt would leave the weak hash of old: no image with it
    fake_bin.stub("python3", "cat > /dev/null; echo '$6$salt$hash'")
    r = read_pin(fake_bin, "1234\n1234\n")
    assert r.returncode != 0 and "could not be hashed with yescrypt" in r.stderr


def test_read_pin_stops_when_hashing_fails(fake_bin):
    fake_bin.stub("python3", "cat > /dev/null; echo 'libcrypt (libxcrypt) missing' >&2; exit 1")
    r = read_pin(fake_bin, "1234\n1234\n")
    assert r.returncode != 0
    assert "libcrypt (libxcrypt) missing" in r.stderr and "the PIN could not be hashed" in r.stderr


def test_the_hash_goes_into_shadow_as_the_build_puts_it(tmp_path, fake_bin):
    # The build writes "plinius:HASH" to a 0600 file and gives it to chpasswd -e on stdin
    lib = libcrypt()
    chpasswd = shutil.which("chpasswd") or "/usr/sbin/chpasswd"
    if not os.access(chpasswd, os.X_OK) or not shutil.which("unshare"):
        pytest.skip("chpasswd or unshare missing on the host")
    if subprocess.run(["unshare", "--map-root-user", "true"], capture_output=True).returncode:
        pytest.skip("no user namespaces on this host")
    root = tmp_path / "root"
    (root / "etc").mkdir(parents=True)
    (root / "etc/passwd").write_text("root:x:0:0::/root:/bin/bash\n"
                                     "plinius:x:32011:32011::/home/plinius:/bin/bash\n")
    (root / "etc/group").write_text("root:x:0:\nplinius:x:32011:\n")
    (root / "etc/shadow").write_text("root:!*:20000::::::\nplinius:!:20000:0:99999:7:::\n")
    (root / "etc/gshadow").write_text("root:!*::\nplinius:!::\n")
    hashed = read_pin(fake_bin, "864209\n864209\n").stdout.strip()
    pin_file = tmp_path / "pin-hash"
    pin_file.write_text(f"plinius:{hashed}\n")
    with open(pin_file) as f:
        r = subprocess.run(["unshare", "--map-root-user", chpasswd, "-e", "-R", str(root)],
                           stdin=f, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    line = next(x for x in (root / "etc/shadow").read_text().splitlines() if x.startswith("plinius:"))
    stored = line.split(":")[1]
    assert stored == hashed
    assert lib.crypt(b"864209", stored.encode()).decode() == stored


def test_the_build_hands_the_pin_over_off_the_command_line():
    # What cannot run here: the namespace part of the build. The hash is no argument of it, it
    # comes from the 0600 file, and the image is made under umask 077.
    text = MKIMAGE.read_text()
    assert "usermod -p" not in text and "openssl passwd" not in text
    assert '(umask 077; printf \'plinius:%s\\n\' "$pin_hash" > "$work/pin-hash")' in text
    assert 'chpasswd -e < "$work/pin-hash"' in text
    run_args = text[text.index("' \"$CHROOT\" \"$work\""):].splitlines()[0]
    assert "pin" not in run_args
    assert re.search(r"\n\tumask 077\n\tmkfs\.ext4 ", text)


def test_mkimage_vendor_from_the_phone_needs_the_ut_build(tree, fake_bin):
    # -V leaves vendor.img and vendor_dlkm.img out and names them by the pins of the UT build of -a
    r = mkimage(tree, fake_bin, "-V")
    assert r.returncode != 0 and "-V needs -a" in r.stderr
    assert "build chroot missing" not in r.stderr


def test_mkimage_vendor_from_the_phone_goes_on_with_the_ut_build(tree, fake_bin):
    _, ut = tree
    r = mkimage(tree, fake_bin, "-V", "-a", str(ut / "android" / "android-rootfs.img"))
    assert "UT files as pinned in tools/ut-build.sha256" in r.stderr
    assert r.returncode != 0 and "build chroot missing" in r.stderr


# --- the kernel of the image and its fallback ---------------------------------------------------

from release_sim import avb_partition, boot_img, kernel_blob, read_avb  # noqa: E402

KREL, OLDREL = "6.1.193-plinius103", "6.1.192-plinius23"
UT_PART = 256 * 1024


def kernel_package(path, rel):
    """A plinius-kernel package with the files image_kernel takes from it."""
    src = path.parent / f"src-{rel}"
    d = src / "usr/lib/plinius/kernel" / rel
    d.mkdir(parents=True)
    (d / "boot.img").write_bytes(boot_img(rel))
    (d / "Image.gz").write_bytes(kernel_blob(rel))
    (src / "usr/lib/plinius/kernel/plinius-kernel-install").write_text("#!/bin/bash\n")
    (src / "usr/lib/plinius/modules" / rel).mkdir(parents=True)
    subprocess.run(["bsdtar", "--zstd", "-cf", str(path), "-C", str(src), "usr"], check=True)
    return path


def local_repo(repo, entries):
    """plinius-local.db.tar.gz naming (name, file) pairs, as repo-add writes desc."""
    import io
    import tarfile
    repo.mkdir(parents=True, exist_ok=True)
    with tarfile.open(repo / "plinius-local.db.tar.gz", "w:gz") as t:
        for name, file in entries:
            data = f"%FILENAME%\n{file}\n\n%NAME%\n{name}\n\n%VERSION%\n1-1\n\n".encode()
            info = tarfile.TarInfo(f"{name}-1-1/desc")
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))


@pytest.fixture
def kernel_tree(tmp_path):
    root = tmp_path / "volla"
    (root / "image").mkdir(parents=True)
    repo = tmp_path / "repo"
    kfile = "plinius-kernel-6.1.193-103-aarch64.pkg.tar.zst"
    ofile = "plinius-kernel-6.1.192-23-aarch64.pkg.tar.zst"
    local_repo(repo, [("plinius-kernel-tools", "other.pkg.tar.zst"), ("plinius-kernel", kfile)])
    kernel_package(repo / kfile, KREL)
    old = kernel_package(repo / ofile, OLDREL)
    (root / "image" / "fallback-kernel.sha256").write_text(
        f"# pin\n{hashlib.sha256(old.read_bytes()).hexdigest()}  {ofile}\n")
    files = tmp_path / "boot-files"
    files.mkdir()
    ut = avb_partition(boot_img("6.1.145-g8bd3c12ba5c3"), UT_PART, 160 * 1024)
    (files / "boot.img").write_bytes(ut)
    (files / "zImage").write_bytes(b"UT kernel")
    work = tmp_path / "work"
    work.mkdir()
    return root, repo, files, work, old


def image_kernel(kernel_tree, fake_bin):
    root, repo, files, work, _ = kernel_tree
    script = (f'set -euo pipefail\n. "{TOOLS / "lib.sh"}"\n'
              f'LOCALREPO="{repo}" VOLLA_DIR="{root}" PKGBUILDS="{REPO / "pkgbuilds"}"\n'
              + bash_functions(MKIMAGE, "db_file", "fallback_kernel", "image_kernel")
              + f'image_kernel "{files}" "{work}"\n')
    # bsdtar (libarchive) is no basic tool of the isolated PATH
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=fake_bin.env())


def test_image_kernel_puts_the_package_kernel_into_the_boot_files(kernel_tree, fake_bin):
    _, _, files, work, old = kernel_tree
    r = image_kernel(kernel_tree, fake_bin)
    assert r.returncode == 0, r.stderr
    part = (files / "boot.img").read_bytes()
    img = boot_img(KREL)
    # boot.img: the package's kernel inside UT's partition content, its AVB data valid
    assert len(part) == UT_PART and part[:len(img)] == img
    avb = read_avb(part)
    assert avb["desc_size"] == len(img)
    assert avb["digest"] == hashlib.sha256(avb["salt"] + img).digest()
    assert (files / "zImage").read_bytes() == kernel_blob(KREL)
    assert not (work / "boot.img.new").exists()
    assert f"Kernel of the image: {KREL}" in r.stderr
    assert (work / "fallback-kernel.pkg").resolve() == old.resolve()
    assert f"Fallback kernel: {old.name}" in r.stderr


def test_image_kernel_goes_without_the_fallback_package(kernel_tree, fake_bin):
    _, _, files, work, old = kernel_tree
    old.unlink()
    r = image_kernel(kernel_tree, fake_bin)
    assert r.returncode == 0, r.stderr
    assert f"Note: no fallback kernel ({old.name} is not in" in r.stderr
    assert not (work / "fallback-kernel.pkg").exists()
    assert (files / "zImage").read_bytes() == kernel_blob(KREL)


def test_image_kernel_refuses_a_fallback_that_differs(kernel_tree, fake_bin):
    _, _, files, _, old = kernel_tree
    old.write_bytes(b"from a server in other hands")
    r = image_kernel(kernel_tree, fake_bin)
    assert r.returncode != 0
    assert "is not the fallback kernel pinned in image/fallback-kernel.sha256" in r.stderr


def test_image_kernel_needs_the_kernel_package(kernel_tree, fake_bin):
    _, repo, files, _, _ = kernel_tree
    local_repo(repo, [("plinius-kernel-tools", "other.pkg.tar.zst")])
    r = image_kernel(kernel_tree, fake_bin)
    assert r.returncode != 0 and "plinius-kernel missing in" in r.stderr
    assert (files / "zImage").read_bytes() == b"UT kernel"


def test_image_kernel_stops_when_the_kernel_does_not_fit(kernel_tree, fake_bin):
    _, _, files, _, _ = kernel_tree
    # A boot partition far too small for the kernel and the vbmeta block
    (files / "boot.img").write_bytes(avb_partition(b"x" * 100, 8 * 1024, 4 * 1024))
    r = image_kernel(kernel_tree, fake_bin)
    assert r.returncode != 0 and "does not fit into UT's boot.img" in r.stderr


def test_the_build_keeps_the_fallback_as_plinius_kernel_install_does():
    # What cannot run here: the namespace part that puts the fallback into the image
    text = MKIMAGE.read_text()
    part = text[text.index('if [[ -e $work/fallback-kernel.pkg ]]; then'):]
    part = part[:part.index("\n\tfi\n")]
    assert 'cp -r "$d" "$t/var/lib/plinius-kernel/kept/$rel"' in part
    assert '"$t/var/lib/plinius-kernel/kept/$rel/modules"' in part
    assert 'echo "$rel" > "$t/var/lib/plinius-kernel/previous"' in part
    assert "'" not in part, "inside the single-quoted script of ns_run"
