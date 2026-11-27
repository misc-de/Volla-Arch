# tools/ut-fetch.sh, tools/boot-files.sh and tools/repo-fetch.sh: the downloads that let someone
# else make an image without building every package, and the check of the boot images. They run
# against a stand-in curl that serves files from a directory; nothing goes to the network.
#
# ut-fetch.sh and boot-files.sh run as copies in a tree of their own, whose tools/ut-build.sha256
# pins the small test images: boot images straight from the server, the Android images copied out
# of a real (small) ext4 system.img with debugfs, system.img removed unless kept, nothing fetched
# twice. A download, a file copied out of system.img or a file already there that is not the
# pinned one stops the run, as do another build (unless asked for, unchecked, for new checksums),
# a bad build id and a file missing in system.img.
# boot-files.sh refuses a vendor_boot.img with another kernel command line before unpacking it.
# repo-fetch.sh: database, stamp and packages are checked against a keyring made for the test (a
# real GnuPG key); a wrong signature, a key that is not the trusted one or is revoked, a stamp of
# another channel, an old stamp, a stamp of another database, an older signed package under the
# current name (also one already in the local repo) and a file name that would leave the target
# directory or does not fit its entry stop the run before anything is added.

import hashlib
import os
import shutil
import struct
import subprocess
import time

import pytest

from conftest import REPO, TOOLS

UT_FETCH = REPO / "tools" / "ut-fetch.sh"
REPO_FETCH = REPO / "tools" / "repo-fetch.sh"
BOOT_FILES = REPO / "tools" / "boot-files.sh"
ANDROID = ["android-rootfs.img", "vendor.img", "vendor_dlkm.img", "system_dlkm.img"]
BOOT = ["boot.img", "vendor_boot.img", "init_boot.img"]

# Serves $SRV/<last path part of the URL> for -o, like curl -f (exit 22 when missing); a call
# without our User-Agent fails with 98
CURL = r"""
out= url= ua=
while [ $# -gt 0 ]; do
	case $1 in
		-o) out=$2; shift 2 ;;
		-A) ua=$2; shift 2 ;;
		-C|--retry|--retry-delay) shift 2 ;;
		-*) shift ;;
		*) url=$1; shift ;;
	esac
done
[ "$ua" = "Plinius Arch Linux" ] || exit 98
f="$SRV/${url##*/}"
[ -f "$f" ] || exit 22
cp "$f" "$out"
"""

# unpack_bootimg as far as boot-files.sh uses it: the kernel of boot.img, two vendor ramdisks
# (and the directory unpack_bootimg puts next to them), the ramdisk of init_boot.img
UNPACK_BOOTIMG = r"""
img= out=
while [ $# -gt 0 ]; do
	case $1 in
		--boot_img) img=$2; shift 2 ;;
		--out) out=$2; shift 2 ;;
		*) shift ;;
	esac
done
mkdir -p "$out"
case ${img##*/} in
	boot.img) cp "$img" "$out/kernel" ;;
	vendor_boot.img) printf ramdisk0 > "$out/vendor_ramdisk00"; printf ramdisk1 > "$out/vendor_ramdisk01"
		mkdir -p "$out/vendor-ramdisk-by-name" ;;
	init_boot.img) cp "$img" "$out/ramdisk" ;;
esac
"""


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def link_tools(fake_bin, *names):
    for name in names:
        found = shutil.which(name)
        if not found:
            pytest.skip(f"{name} missing on the host")
        (fake_bin.path / name).symlink_to(found)


def run(script, fake_bin, *args, **env):
    return subprocess.run(["bash", str(script), *args], capture_output=True, text=True,
                          env=fake_bin.env(isolated=True, **env))


def vendor_boot(cmdline="console=tty0 systempart=/dev/disk/by-partlabel/system"):
    """A vendor boot image of header version 4 as far as boot-files.sh reads it."""
    header = b"VNDRBOOT" + struct.pack("<I", 4) + b"\0" * 16 + cmdline.encode().ljust(2048, b"\0")
    return header + b"ramdisks" * 64


# --- the pinned UT build ---------------------------------------------------------------------

def test_the_pinned_build_lists_every_file_the_tools_take():
    # The names ut-fetch.sh lays out, boot-files.sh unpacks and mkimage.sh checks, each once
    lines = [line.split() for line in (TOOLS / "ut-build.sha256").read_text().splitlines()]
    comments = [line for line in lines if line and line[0] == "#"]
    pins = [line for line in lines if line and line[0] != "#"]
    assert ["#", "build", "04112026"] in comments
    assert sorted(name for _, name in pins) == sorted(BOOT + [f"android/{n}" for n in ANDROID])
    assert all(len(h) == 64 and set(h) <= set("0123456789abcdef") for h, _ in pins)
    assert 'android_files=(android-rootfs.img vendor.img vendor_dlkm.img system_dlkm.img)' in \
        UT_FETCH.read_text()


# --- ut-fetch.sh -----------------------------------------------------------------------------

@pytest.fixture
def volla(tmp_path):
    """A tree with copies of the scripts; pin() writes its tools/ut-build.sha256."""
    root = tmp_path / "volla"
    (root / "tools").mkdir(parents=True)
    for name in ("ut-fetch.sh", "boot-files.sh", "lib.sh"):
        shutil.copy2(TOOLS / name, root / "tools" / name)
    return root


def pin(volla, files, build="04112026"):
    """files: {name in ut-build.sha256: content}."""
    text = f"# test pins\n# build {build}\n" + "".join(
        f"{sha256(data)}  {name}\n" for name, data in files.items())
    (volla / "tools" / "ut-build.sha256").write_text(text)


UT_FILES = {"boot.img": b"boot" * 100, "vendor_boot.img": vendor_boot(),
            "init_boot.img": b"init_boot" * 100,
            **{f"android/{n}": n.encode() * 50 for n in ANDROID}}


def make_system_img(tmp_path, srv, contents):
    tree = tmp_path / "tree" / "var/lib/lxc/android"
    if tree.exists():
        shutil.rmtree(tmp_path / "tree")
    tree.mkdir(parents=True)
    for name, data in contents.items():
        (tree / name).write_bytes(data)
    mke2fs = shutil.which("mke2fs") or shutil.which("mkfs.ext4")
    if not mke2fs:
        pytest.skip("mke2fs missing on the host")
    (srv / "system.img").unlink(missing_ok=True)
    subprocess.run([mke2fs, "-q", "-t", "ext4", "-d", str(tmp_path / "tree"),
                    str(srv / "system.img"), "8M"], check=True)


@pytest.fixture
def ut_server(tmp_path, fake_bin, volla):
    """A server directory with the boot images and a system.img holding the Android images, and
    the tree's pins for exactly these files."""
    srv = tmp_path / "srv"
    srv.mkdir()
    for img in BOOT:
        (srv / img).write_bytes(UT_FILES[img])
    make_system_img(tmp_path, srv, {n: UT_FILES[f"android/{n}"] for n in ANDROID})
    pin(volla, UT_FILES)
    fake_bin.stub("curl", CURL)
    link_tools(fake_bin, "debugfs", "sha256sum", "mktemp")
    return srv


def ut_fetch(volla, fake_bin, *args, **env):
    return run(volla / "tools" / "ut-fetch.sh", fake_bin, *args, **env)


def test_ut_fetch_lays_out_what_mkimage_needs(tmp_path, fake_bin, volla, ut_server):
    out = tmp_path / "ut"
    system = tmp_path / "dl" / "system.img"
    r = ut_fetch(volla, fake_bin, "04112026", str(out), SRV=ut_server, UT_SYSTEM_IMG=system)
    assert r.returncode == 0, r.stderr
    for img in BOOT:
        assert (out / img).read_bytes() == UT_FILES[img]
    for name in ANDROID:
        assert (out / "android" / name).read_bytes() == UT_FILES[f"android/{name}"]
    assert not list(out.rglob("*.part"))
    assert not system.exists(), "system.img is removed after copying"
    assert "kernel and ramdisks not checked" in r.stderr
    assert "Done: image/mkimage.sh -a" in r.stderr

    # A second run checks everything there and downloads nothing
    before = len(fake_bin.calls("curl"))
    r = ut_fetch(volla, fake_bin, "04112026", str(out), SRV=ut_server,
                 UT_SYSTEM_IMG=system)
    assert r.returncode == 0, r.stderr
    assert len(fake_bin.calls("curl")) == before
    assert r.stderr.count("Already there and as pinned") == 7


def test_ut_fetch_takes_the_pinned_build_by_default(tmp_path, fake_bin, volla, ut_server):
    r = ut_fetch(volla, fake_bin, SRV=ut_server, UT_SYSTEM_IMG=tmp_path / "s.img")
    assert r.returncode == 0, r.stderr
    assert (volla / "build/ut/04112026/android/vendor.img").is_file()
    assert all("/04112026/" in c[-1] for c in fake_bin.calls("curl"))


def test_ut_fetch_keeps_system_img_on_request(tmp_path, fake_bin, volla, ut_server):
    system = tmp_path / "dl" / "system.img"
    r = ut_fetch(volla, fake_bin, "04112026", str(tmp_path / "ut"), SRV=ut_server,
                 UT_SYSTEM_IMG=system, KEEP_SYSTEM=1)
    assert r.returncode == 0, r.stderr
    assert system.is_file()

    # Kept, it is used again instead of downloading it once more
    (tmp_path / "ut" / "android" / "vendor.img").unlink()
    calls = len(fake_bin.calls("curl"))
    r = ut_fetch(volla, fake_bin, "04112026", str(tmp_path / "ut"), SRV=ut_server,
                 UT_SYSTEM_IMG=system, KEEP_SYSTEM=1)
    assert r.returncode == 0, r.stderr
    assert len(fake_bin.calls("curl")) == calls
    assert (tmp_path / "ut" / "android" / "vendor.img").read_bytes() == UT_FILES["android/vendor.img"]


def test_ut_fetch_refuses_another_build(tmp_path, fake_bin, volla, ut_server):
    r = ut_fetch(volla, fake_bin, "19112026", str(tmp_path / "ut"), SRV=ut_server,
                 UT_SYSTEM_IMG=tmp_path / "s.img")
    assert r.returncode != 0
    assert "build 19112026 is not pinned in tools/ut-build.sha256 (04112026 is)" in r.stderr
    assert "UT_UNPINNED=1 fetches it" in r.stderr
    assert not fake_bin.calls("curl")


def test_ut_fetch_takes_another_build_unchecked_on_request(tmp_path, fake_bin, volla, ut_server):
    # For new checksums: fetched without checks, the lines for tools/ut-build.sha256 on stdout
    (ut_server / "boot.img").write_bytes(b"the kernel of a newer build")
    out = tmp_path / "ut"
    r = ut_fetch(volla, fake_bin, "19112026", str(out), SRV=ut_server,
                 UT_SYSTEM_IMG=tmp_path / "s.img", UT_UNPINNED=1)
    assert r.returncode == 0, r.stderr
    assert "build 19112026 is not pinned, its files are not checked" in r.stderr
    assert all("/19112026/" in c[-1] for c in fake_bin.calls("curl"))
    files = {**UT_FILES, "boot.img": b"the kernel of a newer build"}
    assert r.stdout.splitlines() == ["# build 19112026"] + [
        f"{sha256(files[name])}  {name}" for name in BOOT + [f"android/{n}" for n in ANDROID]]
    assert "compare the lines above with the phone" in r.stderr

    r = ut_fetch(volla, fake_bin, "19112026", str(out), SRV=ut_server, UT_UNPINNED=1)
    assert r.returncode == 0, r.stderr
    assert r.stderr.count("Already there, unchecked") == 7


def test_ut_fetch_needs_a_pinned_build_id(tmp_path, fake_bin, volla, ut_server):
    (volla / "tools" / "ut-build.sha256").write_text("# no build here\n")
    r = ut_fetch(volla, fake_bin, SRV=ut_server)
    assert r.returncode != 0 and "no build id in" in r.stderr
    assert not fake_bin.calls("curl")


def test_ut_fetch_refuses_a_tampered_download(tmp_path, fake_bin, volla, ut_server):
    # The audit's case: a vendor_boot.img whose command line switches the security off
    (ut_server / "vendor_boot.img").write_bytes(vendor_boot("init=/bin/sh apparmor=0 lsm=capability"))
    out = tmp_path / "ut"
    r = ut_fetch(volla, fake_bin, "04112026", str(out), SRV=ut_server, UT_SYSTEM_IMG=tmp_path / "s.img")
    assert r.returncode != 0
    assert "vendor_boot.img is not vendor_boot.img of UT build 04112026" in r.stderr
    assert not (out / "vendor_boot.img").exists() and not (out / "vendor_boot.img.part").exists()
    assert not any(c[-1].endswith("/system.img") for c in fake_bin.calls("curl"))


def test_ut_fetch_refuses_a_file_already_there_that_differs(tmp_path, fake_bin, volla, ut_server):
    out = tmp_path / "ut"
    (out / "android").mkdir(parents=True)
    (out / "android" / "vendor.img").write_bytes(b"left there by someone else")
    r = ut_fetch(volla, fake_bin, "04112026", str(out), SRV=ut_server, UT_SYSTEM_IMG=tmp_path / "s.img")
    assert r.returncode != 0
    assert "android/vendor.img is not android/vendor.img of UT build 04112026" in r.stderr
    assert "remove it to download it again" in r.stderr
    assert (out / "android" / "vendor.img").read_bytes() == b"left there by someone else"
    assert not any(c[-1].endswith("/system.img") for c in fake_bin.calls("curl"))


def test_ut_fetch_refuses_a_tampered_android_image(tmp_path, fake_bin, volla, ut_server):
    make_system_img(tmp_path, ut_server, {**{n: UT_FILES[f"android/{n}"] for n in ANDROID},
                                          "android-rootfs.img": b"a container of someone else"})
    out = tmp_path / "ut"
    r = ut_fetch(volla, fake_bin, "04112026", str(out), SRV=ut_server, UT_SYSTEM_IMG=tmp_path / "s.img")
    assert r.returncode != 0
    assert "android-rootfs.img in" in r.stderr and "is not android/android-rootfs.img" in r.stderr
    assert not list((out / "android").iterdir())


def test_ut_fetch_refuses_a_missing_android_image(tmp_path, fake_bin, volla, ut_server):
    make_system_img(tmp_path, ut_server, {"android-rootfs.img": UT_FILES["android/android-rootfs.img"]})
    out = tmp_path / "ut"
    r = ut_fetch(volla, fake_bin, "04112026", str(out), SRV=ut_server, UT_SYSTEM_IMG=tmp_path / "s.img")
    assert r.returncode != 0
    assert "/var/lib/lxc/android/vendor.img not found" in r.stderr
    assert not (out / "android" / "vendor.img").exists()


def test_ut_fetch_refuses_a_file_that_is_not_pinned(tmp_path, fake_bin, volla, ut_server):
    pin(volla, {k: v for k, v in UT_FILES.items() if k != "android/system_dlkm.img"})
    r = ut_fetch(volla, fake_bin, "04112026", str(tmp_path / "ut"), SRV=ut_server,
                 UT_SYSTEM_IMG=tmp_path / "s.img")
    assert r.returncode != 0
    assert "android/system_dlkm.img is not pinned in tools/ut-build.sha256" in r.stderr


def test_ut_fetch_refuses_a_bad_build_id(tmp_path, fake_bin, volla, ut_server):
    r = ut_fetch(volla, fake_bin, "../../etc", str(tmp_path / "ut"), SRV=ut_server)
    assert r.returncode != 0 and "invalid build id" in r.stderr
    assert not fake_bin.calls("curl")


def boot_files_pins(volla, initrd_tail=b""):
    """tools/boot-files.sha256 of the tree for what the stand-in unpack_bootimg makes."""
    (volla / "tools" / "boot-files.sha256").write_text(
        f"{sha256(UT_FILES['boot.img'])}  zImage\n"
        f"{sha256(b'ramdisk0ramdisk1' + UT_FILES['init_boot.img'] + initrd_tail)}  initrd.cpio.gz\n")


def test_ut_fetch_checks_kernel_and_ramdisks(tmp_path, fake_bin, volla, ut_server):
    fake_bin.stub("unpack_bootimg", UNPACK_BOOTIMG)
    boot_files_pins(volla)
    out = tmp_path / "ut"
    r = ut_fetch(volla, fake_bin, "04112026", str(out), SRV=ut_server, UT_SYSTEM_IMG=tmp_path / "s.img")
    assert r.returncode == 0, r.stderr
    assert "Boot files match tools/boot-files.sha256" in r.stderr

    # Kernel or ramdisk of another build (here: checksums for another one) stop the run
    boot_files_pins(volla, initrd_tail=b"other")
    r = ut_fetch(volla, fake_bin, "04112026", str(out), SRV=ut_server, UT_SYSTEM_IMG=tmp_path / "s.img")
    assert r.returncode != 0
    assert "this kernel does not fit plinius-kmod" in r.stderr
    assert "the boot files of build 04112026 do not match tools/boot-files.sha256" in r.stderr


# --- boot-files.sh ---------------------------------------------------------------------------

@pytest.fixture
def boot_dir(tmp_path, fake_bin, volla):
    src = tmp_path / "ut"
    src.mkdir()
    for img in BOOT:
        (src / img).write_bytes(UT_FILES[img])
    pin(volla, UT_FILES)
    boot_files_pins(volla)
    fake_bin.stub("unpack_bootimg", UNPACK_BOOTIMG)
    return src


def test_boot_files_makes_the_files_of_the_pinned_build(tmp_path, fake_bin, volla, boot_dir):
    out = tmp_path / "out"
    r = run(volla / "tools" / "boot-files.sh", fake_bin, str(boot_dir), str(out))
    assert r.returncode == 0, r.stderr
    assert (out / "zImage").read_bytes() == UT_FILES["boot.img"]
    assert (out / "boot.img").read_bytes() == UT_FILES["boot.img"]
    image = (out / "vendor_boot.img").read_bytes()
    assert len(image) == len(UT_FILES["vendor_boot.img"])
    assert image[28:2076].rstrip(b"\0") == b"console=tty0"


@pytest.mark.parametrize("img", BOOT)
def test_boot_files_refuses_an_image_that_is_not_pinned(tmp_path, fake_bin, volla, boot_dir, img):
    # Another command line in vendor_boot.img, the same kernel and ramdisks: the checksums of
    # tools/boot-files.sha256 alone would pass it
    data = vendor_boot("init=/bin/sh apparmor=0 lsm=capability") if img == "vendor_boot.img" \
        else UT_FILES[img] + b"x"
    (boot_dir / img).write_bytes(data)
    r = run(volla / "tools" / "boot-files.sh", fake_bin, str(boot_dir), str(tmp_path / "out"))
    assert r.returncode != 0
    assert f"{img} is not the one of the UT build pinned in tools/ut-build.sha256" in r.stderr
    assert not fake_bin.calls("unpack_bootimg"), "nothing is unpacked before the check"
    assert not (tmp_path / "out").exists()


def test_boot_files_needs_all_three_images(tmp_path, fake_bin, volla, boot_dir):
    (boot_dir / "init_boot.img").unlink()
    r = run(volla / "tools" / "boot-files.sh", fake_bin, str(boot_dir), str(tmp_path / "out"))
    assert r.returncode != 0 and "init_boot.img missing" in r.stderr


# --- repo-fetch.sh ---------------------------------------------------------------------------

class Keys:
    """A GnuPG home with one signing key, and a keyring directory in plinius-keyring's form."""

    def __init__(self, tmp_path, name):
        self.home = tmp_path / f"gnupg-{name}"
        self.home.mkdir(mode=0o700)
        self.gpg(["--quick-gen-key", "--passphrase", "", f"{name} <{name}@example.org>",
                  "ed25519", "sign", "never"])
        out = self.gpg(["--with-colons", "--list-keys"]).stdout
        self.fpr = next(line.split(":")[9] for line in out.splitlines() if line.startswith("fpr"))
        self.dir = tmp_path / f"keyring-{name}"
        self.dir.mkdir()
        (self.dir / "plinius.gpg").write_text(self.gpg(["--armor", "--export", self.fpr]).stdout)
        (self.dir / "plinius-trusted").write_text(f"{self.fpr}:4:\n")
        (self.dir / "plinius-revoked").write_text("")

    def gpg(self, args):
        return subprocess.run(["gpg", "--batch", "--yes", "--pinentry-mode", "loopback",
                               "--homedir", str(self.home), *args],
                              capture_output=True, text=True, check=True)

    def sign(self, path):
        self.gpg(["--detach-sign", "-o", f"{path}.sig", str(path)])


@pytest.fixture
def keys(tmp_path):
    if not shutil.which("gpg") or not shutil.which("gpgv"):
        pytest.skip("gpg missing on the host")
    return Keys(tmp_path, "repo")


def name_version(f):
    """Name and version a package file name stands for (name-pkgver-pkgrel-arch.pkg.tar.*)."""
    parts = f.split(".pkg.tar")[0].rsplit("-", 3)
    return (parts[0], f"{parts[1]}-{parts[2]}") if len(parts) == 4 else (f, "1-1")


def write_stamp(srv, keys, **fields):
    """plinius.stamp as tools/lib.sh write_stamp makes it, signed by keys; a field given as None
    is left out."""
    db = srv / "plinius.db.tar.gz"
    stamp = {"channel": "stable", "time": int(time.time()), "db": db.name,
             "db_sha256": sha256(db.read_bytes()),
             "sig_sha256": sha256((srv / "plinius.db.tar.gz.sig").read_bytes())}
    stamp.update(fields)
    (srv / "plinius.stamp").write_text("".join(f"{k}={v}\n" for k, v in stamp.items() if v is not None))
    keys.sign(srv / "plinius.stamp")


def make_repo(srv, keys, files, entries=None, **stamp):
    """Packages, a database listing them (as repo-add writes the fields repo-fetch.sh reads) and
    the channel's stamp, all signed by keys. entries: {file: {field: value or None}} changes the
    database entries."""
    srv.mkdir(exist_ok=True)
    db = srv / "db"
    db.mkdir()
    for i, f in enumerate(files):
        digest = "0" * 64
        if "/" not in f:
            (srv / f).write_bytes(f.encode() * 20)
            keys.sign(srv / f)
            digest = sha256((srv / f).read_bytes())
        name, version = name_version(f)
        fields = {"FILENAME": f, "NAME": name, "BASE": name, "VERSION": version,
                  "CSIZE": 20 * len(f), "SHA256SUM": digest, "ARCH": "aarch64"}
        fields.update((entries or {}).get(f, {}))
        (db / f"pkg{i}").mkdir(parents=True)
        (db / f"pkg{i}" / "desc").write_text(
            "".join(f"%{k}%\n{v}\n\n" for k, v in fields.items() if v is not None))
    subprocess.run(["bsdtar", "-czf", str(srv / "plinius.db.tar.gz"), "-C", str(db), "."], check=True)
    keys.sign(srv / "plinius.db.tar.gz")
    write_stamp(srv, keys, **stamp)


@pytest.fixture
def repo_env(tmp_path, fake_bin, keys):
    fake_bin.stub("curl", CURL)
    fake_bin.stub("repo-add")
    link_tools(fake_bin, "gpg", "gpgv", "bsdtar", "mktemp", "gpg-agent", "gpgconf")
    return dict(REPO_FETCH_KEYDIR=keys.dir, REPO_FETCH_DEST=tmp_path / "local",
                REPO_URL="https://repo.example.org/repo", HOME=tmp_path)


FILES = ["a-1-1-aarch64.pkg.tar.xz", "b-1-1-any.pkg.tar.xz", "cups-plinius-2:2.4.20-5-aarch64.pkg.tar.xz"]


def fetch_refused(tmp_path, fake_bin, repo_env, srv, message, **env):
    r = run(REPO_FETCH, fake_bin, SRV=srv, **repo_env, **env)
    assert r.returncode != 0
    assert message in r.stderr
    assert not fake_bin.calls("repo-add")
    return r


def test_repo_fetch_checks_and_adds_every_package(tmp_path, fake_bin, keys, repo_env):
    srv = tmp_path / "srv"
    make_repo(srv, keys, FILES)
    r = run(REPO_FETCH, fake_bin, SRV=srv, **repo_env)
    assert r.returncode == 0, r.stderr
    dest = tmp_path / "local"
    for f in FILES:
        assert (dest / f).read_bytes() == (srv / f).read_bytes()
        assert (dest / f"{f}.sig").is_file()
    [call] = fake_bin.calls("repo-add")
    assert call == ["--quiet", "plinius-local.db.tar.gz", *FILES]
    assert "Stamp of stable:" in r.stderr and "matches the database" in r.stderr
    assert "3 of 3 packages downloaded" in r.stderr
    # stable is the top level of the repo
    assert all("/repo/aarch64/" in c[-1] for c in fake_bin.calls("curl"))

    # A second run only reads the database and the stamp again
    calls = len(fake_bin.calls("curl"))
    r = run(REPO_FETCH, fake_bin, SRV=srv, **repo_env)
    assert r.returncode == 0, r.stderr
    assert len(fake_bin.calls("curl")) == calls + 4
    assert "0 of 3 packages downloaded" in r.stderr


def test_repo_fetch_beta_channel(tmp_path, fake_bin, keys, repo_env):
    make_repo(tmp_path / "srv", keys, FILES[:1], channel="beta")
    r = run(REPO_FETCH, fake_bin, SRV=tmp_path / "srv", CHANNEL="beta", **repo_env)
    assert r.returncode == 0, r.stderr
    assert all("/repo/beta/aarch64/" in c[-1] for c in fake_bin.calls("curl"))


def test_repo_fetch_stops_at_a_tampered_package(tmp_path, fake_bin, keys, repo_env):
    srv = tmp_path / "srv"
    make_repo(srv, keys, FILES)
    (srv / FILES[1]).write_bytes(b"changed after signing")
    fetch_refused(tmp_path, fake_bin, repo_env, srv, f"{FILES[1]} is not signed by the repository key")
    r = run(REPO_FETCH, fake_bin, SRV=srv, **repo_env)
    assert r.stderr.count(f"{FILES[1]} is not signed by the repository key, downloading it again") == 2
    assert not (tmp_path / "local" / FILES[1]).exists()


def test_repo_fetch_stops_at_an_old_package_under_the_current_name(tmp_path, fake_bin, keys, repo_env):
    # The server swaps in an older package with its own valid signature: the signature passes,
    # the SHA-256 in the database does not
    srv = tmp_path / "srv"
    make_repo(srv, keys, FILES)
    (srv / FILES[0]).write_bytes(b"a-0.9-1, signed back then")
    keys.sign(srv / FILES[0])
    r = fetch_refused(tmp_path, fake_bin, repo_env, srv,
                      f"{FILES[0]} is not the package the database lists (its SHA-256 differs)")
    assert "downloading it again" in r.stderr
    assert not (tmp_path / "local" / FILES[0]).exists()


def test_repo_fetch_replaces_a_package_there_that_differs(tmp_path, fake_bin, keys, repo_env):
    # A signed package left in the local repo from an earlier (older) download is not taken
    srv = tmp_path / "srv"
    make_repo(srv, keys, FILES[:1])
    dest = tmp_path / "local"
    dest.mkdir()
    (dest / FILES[0]).write_bytes(b"older, but signed")
    keys.sign(dest / FILES[0])
    r = run(REPO_FETCH, fake_bin, SRV=srv, **repo_env)
    assert r.returncode == 0, r.stderr
    assert f"{FILES[0]} in {dest} is not the one in the database, downloading it again" in r.stderr
    assert (dest / FILES[0]).read_bytes() == (srv / FILES[0]).read_bytes()
    assert "1 of 1 packages downloaded" in r.stderr


def test_repo_fetch_downloads_a_damaged_package_again(tmp_path, fake_bin, keys, repo_env):
    srv = tmp_path / "srv"
    make_repo(srv, keys, FILES[:1])
    # The first download of the package arrives cut off, the second one complete
    fake_bin.stub("curl", "n=$(ls \"$SRV\"/.count-* 2>/dev/null | wc -l)\n" + CURL + """
case $url in *.pkg.tar.xz)
	touch "$SRV/.count-$n"
	[ "$n" -gt 0 ] || head -c 5 "$f" > "$out" ;;
esac
""")
    r = run(REPO_FETCH, fake_bin, SRV=srv, **repo_env)
    assert r.returncode == 0, r.stderr
    assert "downloading it again" in r.stderr
    assert (tmp_path / "local" / FILES[0]).read_bytes() == (srv / FILES[0]).read_bytes()


def test_repo_fetch_stops_when_a_package_cannot_be_downloaded(tmp_path, fake_bin, keys, repo_env):
    srv = tmp_path / "srv"
    make_repo(srv, keys, FILES[:1])
    (srv / f"{FILES[0]}.sig").unlink()
    fetch_refused(tmp_path, fake_bin, repo_env, srv, f"{FILES[0]} could not be downloaded")


@pytest.mark.parametrize("entry, message", [
    ({"NAME": "b"}, f"{FILES[0]} is not the file of b 1-1"),
    ({"VERSION": "2-1"}, f"{FILES[0]} is not the file of a 2-1"),
    ({"SHA256SUM": None}, f"the database gives no SHA-256 for {FILES[0]}"),
    ({"VERSION": None}, f"unexpected name or version in the database entry of {FILES[0]}"),
])
def test_repo_fetch_refuses_an_entry_that_does_not_fit(tmp_path, fake_bin, keys, repo_env, entry, message):
    srv = tmp_path / "srv"
    make_repo(srv, keys, FILES[:1], entries={FILES[0]: entry})
    fetch_refused(tmp_path, fake_bin, repo_env, srv, message)
    assert not (tmp_path / "local" / FILES[0]).exists()


def test_repo_fetch_stops_at_a_database_of_another_key(tmp_path, fake_bin, keys, repo_env):
    srv = tmp_path / "srv"
    make_repo(srv, keys, FILES)
    Keys(tmp_path, "other").sign(srv / "plinius.db.tar.gz")
    fetch_refused(tmp_path, fake_bin, repo_env, srv, "the database is not signed by the repository key")


def test_repo_fetch_refuses_a_key_that_is_not_trusted(tmp_path, fake_bin, keys, repo_env):
    make_repo(tmp_path / "srv", keys, FILES)
    (keys.dir / "plinius-trusted").write_text("0" * 40 + ":4:\n")
    fetch_refused(tmp_path, fake_bin, repo_env, tmp_path / "srv", "is not the trusted one")
    assert not fake_bin.calls("curl")


def test_repo_fetch_refuses_a_revoked_key(tmp_path, fake_bin, keys, repo_env):
    make_repo(tmp_path / "srv", keys, FILES)
    (keys.dir / "plinius-revoked").write_text(f"{keys.fpr.lower()}:\n")
    fetch_refused(tmp_path, fake_bin, repo_env, tmp_path / "srv", "is revoked")
    assert not fake_bin.calls("curl")


# --- the stamp: a phone's checks (plinius-repo-stamp) -------------------------------------------

def test_repo_fetch_stops_at_another_channels_stamp(tmp_path, fake_bin, keys, repo_env):
    # beta's database and stamp under the stable path
    make_repo(tmp_path / "srv", keys, FILES, channel="beta")
    fetch_refused(tmp_path, fake_bin, repo_env, tmp_path / "srv",
                  "the stamp is of channel beta, not stable")


def test_repo_fetch_stops_at_an_old_stamp(tmp_path, fake_bin, keys, repo_env):
    # A frozen server: database and stamp of 31 days ago, both genuine
    make_repo(tmp_path / "srv", keys, FILES, time=int(time.time()) - 31 * 86400)
    fetch_refused(tmp_path, fake_bin, repo_env, tmp_path / "srv", "the stamp is 31 days old")


def test_repo_fetch_takes_a_stamp_of_30_days(tmp_path, fake_bin, keys, repo_env):
    make_repo(tmp_path / "srv", keys, FILES[:1], time=int(time.time()) - 30 * 86400 + 60)
    r = run(REPO_FETCH, fake_bin, SRV=tmp_path / "srv", **repo_env)
    assert r.returncode == 0, r.stderr


def test_repo_fetch_stops_at_an_old_database_under_a_fresh_stamp(tmp_path, fake_bin, keys, repo_env):
    # The stamp is current, the database an old signed one
    srv = tmp_path / "srv"
    make_repo(srv, keys, FILES)
    stamp, sig = (srv / "plinius.stamp").read_bytes(), (srv / "plinius.stamp.sig").read_bytes()
    shutil.rmtree(srv)
    make_repo(srv, keys, FILES[:1])
    (srv / "plinius.stamp").write_bytes(stamp)
    (srv / "plinius.stamp.sig").write_bytes(sig)
    fetch_refused(tmp_path, fake_bin, repo_env, srv, "the database does not match the stamp")


def test_repo_fetch_stops_at_another_database_signature(tmp_path, fake_bin, keys, repo_env):
    make_repo(tmp_path / "srv", keys, FILES, sig_sha256="1" * 64)
    fetch_refused(tmp_path, fake_bin, repo_env, tmp_path / "srv", "the database does not match the stamp")


def test_repo_fetch_takes_a_stamp_without_the_signature_hash(tmp_path, fake_bin, keys, repo_env):
    make_repo(tmp_path / "srv", keys, FILES[:1], sig_sha256=None)
    r = run(REPO_FETCH, fake_bin, SRV=tmp_path / "srv", **repo_env)
    assert r.returncode == 0, r.stderr


def test_repo_fetch_needs_the_database_in_the_stamp(tmp_path, fake_bin, keys, repo_env):
    make_repo(tmp_path / "srv", keys, FILES, db_sha256=None)
    fetch_refused(tmp_path, fake_bin, repo_env, tmp_path / "srv",
                  "the stamp does not name its database")


@pytest.mark.parametrize("field", [{"time": "yesterday"}, {"time": None}, {"sig_sha256": "xyz"}])
def test_repo_fetch_refuses_a_malformed_stamp(tmp_path, fake_bin, keys, repo_env, field):
    make_repo(tmp_path / "srv", keys, FILES, **field)
    fetch_refused(tmp_path, fake_bin, repo_env, tmp_path / "srv", "the stamp is malformed")


def test_repo_fetch_stops_at_a_stamp_of_another_key(tmp_path, fake_bin, keys, repo_env):
    make_repo(tmp_path / "srv", keys, FILES)
    Keys(tmp_path, "other").sign(tmp_path / "srv" / "plinius.stamp")
    fetch_refused(tmp_path, fake_bin, repo_env, tmp_path / "srv",
                  "the stamp is not signed by the repository key")


def test_repo_fetch_needs_a_stamp(tmp_path, fake_bin, keys, repo_env):
    make_repo(tmp_path / "srv", keys, FILES)
    (tmp_path / "srv" / "plinius.stamp").unlink()
    fetch_refused(tmp_path, fake_bin, repo_env, tmp_path / "srv",
                  "the server hands out no stamp (plinius.stamp) for stable")


@pytest.mark.parametrize("bad", ["../evil.pkg.tar.xz", "a b.pkg.tar.xz", "x..y.pkg.tar.xz"])
def test_repo_fetch_refuses_odd_file_names(tmp_path, fake_bin, keys, repo_env, bad):
    make_repo(tmp_path / "srv", keys, [bad])
    fetch_refused(tmp_path, fake_bin, repo_env, tmp_path / "srv", "unexpected file name")


def test_repo_fetch_refuses_an_empty_database(tmp_path, fake_bin, keys, repo_env):
    make_repo(tmp_path / "srv", keys, [])
    r = run(REPO_FETCH, fake_bin, SRV=tmp_path / "srv", **repo_env)
    assert r.returncode != 0 and "lists no packages" in r.stderr


def test_scripts_are_executable():
    for script in (UT_FETCH, REPO_FETCH, BOOT_FILES):
        assert os.access(script, os.X_OK)
