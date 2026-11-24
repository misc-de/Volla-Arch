# The downloads of the web server: tools/image-check.sh (an image for strangers carries nothing of
# its builder and everything a phone needs) and tools/downloads.sh (packs it, writes latest.json
# as Plinius Boot reads it, puts the app beside it, uploads in an order a download never trips on).
#
# The image is an ext4 made with mkfs.ext4 -d from a tree in tmp_path (sparse, room for the vendor
# files of the pins included), read by the real
# debugfs as the check reads a real one. The scripts run as copies in tmp_path/volla/tools, so
# build/ lies in tmp_path; pins are written for the fake Android and boot files. rsync is a stub.

import hashlib
import json
import shutil
import subprocess

import pytest

from conftest import TOOLS

pytestmark = pytest.mark.skipif(not (shutil.which("mkfs.ext4") and shutil.which("debugfs")),
                                reason="e2fsprogs missing")

UT_FILES = {
    "android/android-rootfs.img": "var/lib/lxc/android/android-rootfs.img",
    "android/vendor.img": "var/lib/lxc/android/vendor.img",
    "android/vendor_dlkm.img": "var/lib/lxc/android/vendor_dlkm.img",
    "android/system_dlkm.img": "var/lib/lxc/android/system_dlkm.img",
    "boot.img": "usr/share/plinius-boot/boot.img",
    "init_boot.img": "usr/share/plinius-boot/ut/init_boot.img",
}
# Not in an image for download: Plinius Boot copies them from Volla OS on the phone
FROM_PHONE = {"android/vendor.img", "android/vendor_dlkm.img"}
SHADOW = "root:*:20000::::::\nbin:!*:20000::::::\nplinius:!:20000:0:99999:7:::\n"
PACMAN_CONF = ("[options]\nDisableSandboxFilesystem\n\n[plinius]\nSigLevel = Required\n"
               "Server = https://repo.example.org/repo/aarch64\n\n[core]\nInclude = /etc/pacman.d/mirrorlist\n")


def good_tree(root):
    """The file tree of an image that may be offered."""
    files = {
        "etc/machine-id": "",
        "etc/shadow": SHADOW,
        "etc/pacman.conf": PACMAN_CONF,
        "etc/ssh/sshd_config": "PasswordAuthentication no\n",
        "var/lib/pacman/local/plinius-keyring-1-1/desc": "%NAME%\nplinius-keyring\n",
        "usr/lib/systemd/system/plinius-greeter-password.path": "[Path]\n",
        "usr/share/plinius-boot/ut/vendor_boot.img": "ut vendor_boot without systempart",
        "usr/share/plinius-boot/vendor_boot.img": "vendor_boot with prompt",
        "usr/share/plinius-boot/init_boot.img": "init_boot with prompt",
        "usr/share/plinius-boot/initrd.cpio.gz": "initrd with prompt",
        "usr/share/plinius-boot/zImage": "kernel",
        "home/plinius/.bashrc": "",
        "root/.cache/x": "",
    }
    for name, path in UT_FILES.items():
        if name not in FROM_PHONE:
            files[path] = f"content of {name}"
    files["usr/share/plinius-boot/vendor.sha256"] = "".join(
        f"{sha('content of ' + name)} {1 << 20} {name[8:-4]}\n" for name in sorted(FROM_PHONE))
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    (root / "var/cache/pacman/pkg").mkdir(parents=True)
    (root / "root/.ssh").mkdir(mode=0o700)
    return root


def make_image(tree, out, size="96M"):
    subprocess.run(["mkfs.ext4", "-q", "-L", "plinius-arch", "-d", str(tree), str(out), size], check=True)
    return out


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def volla(tmp_path, fake_bin):
    root = tmp_path / "volla"
    (root / "tools").mkdir(parents=True)
    for script in ("lib.sh", "image-check.sh", "downloads.sh"):
        shutil.copy2(TOOLS / script, root / "tools" / script)
    (root / "tools" / "ut-build.sha256").write_text("# build 04112026\n" + "".join(
        f"{sha('content of ' + name)}  {name}\n" for name in UT_FILES) +
        f"{sha('ut vendor_boot as published')}  vendor_boot.img\n")
    (root / "tools" / "boot-files.sha256").write_text(f"{sha('kernel')}  zImage\n{sha('ut ramdisk')}  initrd.cpio.gz\n")
    (fake_bin.path / "debugfs").symlink_to(shutil.which("debugfs"))
    fake_bin.stub("rsync")
    return root


def run(volla, fake_bin, script, *args, **env):
    return subprocess.run(["bash", str(volla / "tools" / script), *map(str, args)], capture_output=True,
                          text=True, env=fake_bin.env(isolated=True, **env), stdin=subprocess.DEVNULL)


def check(volla, fake_bin, image):
    return run(volla, fake_bin, "image-check.sh", image)


# --- image-check.sh -----------------------------------------------------------------------------

def test_a_clean_image_passes_every_check(tmp_path, volla, fake_bin):
    r = check(volla, fake_bin, make_image(good_tree(tmp_path / "t"), tmp_path / "a.img"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "FAIL" not in r.stdout
    assert r.stdout.count("ok: ") == 34
    assert "can be offered for download" in r.stderr


def spoil_key(t):
    (t / "root/.ssh/authorized_keys").write_text("ssh-ed25519 AAAA builder@pc\n")


def spoil_user_key(t):
    (t / "home/plinius/.ssh").mkdir()
    (t / "home/plinius/.ssh/authorized_keys").write_text("ssh-ed25519 AAAA\n")


def spoil_history(t):
    (t / "root/.bash_history").write_text("passwd plinius\n")


def spoil_host_key(t):
    (t / "etc/ssh/ssh_host_ed25519_key").write_text("PRIVATE")


def spoil_machine_id(t):
    (t / "etc/machine-id").write_text("0123456789abcdef0123456789abcdef\n")


def spoil_no_machine_id(t):
    (t / "etc/machine-id").unlink()


def spoil_gnupg(t):
    (t / "etc/pacman.d/gnupg").mkdir(parents=True)


def spoil_cache(t):
    (t / "var/cache/pacman/pkg/vim-9.1-1-aarch64.pkg.tar.zst").write_text("x")


def spoil_pin(t):
    (t / "etc/shadow").write_text(SHADOW.replace("plinius:!:", "plinius:$y$j9T$salt$hash:"))


def spoil_root_password(t):
    (t / "etc/shadow").write_text(SHADOW.replace("root:*:", "root::"))


def spoil_no_greeter(t):
    (t / "usr/lib/systemd/system/plinius-greeter-password.path").unlink()


def spoil_no_repo(t):
    (t / "etc/pacman.conf").write_text("[options]\n\n[core]\nInclude = /etc/pacman.d/mirrorlist\n")


def spoil_optional_sig(t):
    (t / "etc/pacman.conf").write_text(PACMAN_CONF.replace("SigLevel = Required", "SigLevel = Optional"))


def spoil_http(t):
    (t / "etc/pacman.conf").write_text(PACMAN_CONF.replace("https://", "http://"))


def spoil_no_keyring(t):
    shutil.rmtree(t / "var/lib/pacman/local/plinius-keyring-1-1")


def spoil_vendor(t):
    (t / "var/lib/lxc/android/vendor.img").write_text("content of android/vendor.img")


def spoil_vendor_dlkm(t):
    (t / "var/lib/lxc/android/vendor_dlkm.img").write_text("content of android/vendor_dlkm.img")


def spoil_pins(t):
    pins = t / "usr/share/plinius-boot/vendor.sha256"
    pins.write_text(pins.read_text().replace(sha("content of android/vendor.img"), "0" * 64))


def spoil_no_pins(t):
    pins = t / "usr/share/plinius-boot/vendor.sha256"
    pins.write_text("".join(l + "\n" for l in pins.read_text().splitlines() if not l.endswith(" vendor")))


def spoil_android(t):
    (t / "var/lib/lxc/android/system_dlkm.img").write_text("modules of another kernel")


def spoil_kernel(t):
    (t / "usr/share/plinius-boot/zImage").write_text("another kernel")


def spoil_no_prompt(t):
    (t / "usr/share/plinius-boot/init_boot.img").unlink()


@pytest.mark.parametrize("spoil, line", [
    (spoil_key, "no SSH key in /root"),
    (spoil_user_key, "no SSH key in /home/plinius"),
    (spoil_history, "no .bash_history in /root"),
    (spoil_host_key, "no SSH host keys"),
    (spoil_machine_id, "machine-id is there and empty"),
    (spoil_no_machine_id, "machine-id is there and empty"),
    (spoil_gnupg, "no pacman keyring"),
    (spoil_cache, "no package cache"),
    (spoil_pin, "plinius has no password"),
    (spoil_root_password, "root has no password"),
    (spoil_no_greeter, "the phone asks for a PIN at its first start"),
    (spoil_optional_sig, "[plinius] takes signed packages only"),
    (spoil_http, "[plinius] comes from an https server"),
    (spoil_no_keyring, "plinius-keyring is installed"),
    (spoil_vendor, "no vendor.img"),
    (spoil_vendor_dlkm, "no vendor_dlkm.img"),
    (spoil_pins, "vendor.sha256 names vendor.img"),
    (spoil_no_pins, "vendor.sha256 names vendor.img"),
    (spoil_android, "system_dlkm.img as pinned"),
    (spoil_kernel, "zImage as pinned"),
    (spoil_no_prompt, "init_boot.img with the password prompt"),
], ids=lambda v: v.__name__ if callable(v) else None)
def test_an_image_with_a_flaw_is_refused(tmp_path, volla, fake_bin, spoil, line):
    tree = good_tree(tmp_path / "t")
    spoil(tree)
    r = check(volla, fake_bin, make_image(tree, tmp_path / "a.img"))
    assert r.returncode == 1
    failed = [l for l in r.stdout.splitlines() if l.startswith("FAIL: ")]
    assert len(failed) == 1 and failed[0].startswith("FAIL: " + line), r.stdout
    assert "must not be offered for download" in r.stderr


def test_a_large_package_database_is_read_whole(tmp_path, volla, fake_bin):
    # A real image lists about 900 packages, plinius-keyring somewhere in between. grep -q stops at
    # the first match; if the listing goes on writing after that, its SIGPIPE must not turn the check
    # into a failure (pipefail). debugfs here prints that line first and the rest a moment later,
    # which makes the race of a real image certain.
    real = shutil.which("debugfs")
    (fake_bin.path / "debugfs").unlink()
    fake_bin.stub("debugfs", f"""
if [[ $2 == 'ls -p "/var/lib/pacman/local"' ]]; then
	"{real}" "$@" 2>/dev/null | grep plinius-keyring
	sleep 0.3
	for i in $(seq 3000); do echo "/1/040755/0/0/package-$i-1.0-1//"; done
	exit 0
fi
exec "{real}" "$@"
""")
    r = check(volla, fake_bin, make_image(good_tree(tmp_path / "t"), tmp_path / "a.img"))
    assert r.returncode == 0, r.stdout


def test_an_image_without_our_repo_gets_no_updates_and_is_refused(tmp_path, volla, fake_bin):
    tree = good_tree(tmp_path / "t")
    spoil_no_repo(tree)
    r = check(volla, fake_bin, make_image(tree, tmp_path / "a.img"))
    assert r.returncode == 1
    assert [l for l in r.stdout.splitlines() if l.startswith("FAIL: ")] == [
        "FAIL: [plinius] takes signed packages only", "FAIL: [plinius] comes from an https server"]


def test_an_image_without_room_for_the_vendor_files_is_refused(tmp_path, volla, fake_bin):
    # 1 MiB each in the pins, 64 MiB beyond: a file system of 48 MiB is too small
    r = check(volla, fake_bin, make_image(good_tree(tmp_path / "t"), tmp_path / "a.img", "48M"))
    assert r.returncode == 1
    assert [l for l in r.stdout.splitlines() if l.startswith("FAIL: ")] == [
        "FAIL: room for vendor.img and vendor_dlkm.img"]


def test_the_check_reads_only_ext4(tmp_path, volla, fake_bin):
    packed = tmp_path / "a.img.zst"
    packed.write_bytes(b"\x28\xb5\x2f\xfd not ext4")
    r = check(volla, fake_bin, packed)
    assert r.returncode == 1 and "is not an ext4 image" in r.stderr
    r = check(volla, fake_bin, tmp_path / "missing.img")
    assert r.returncode == 1 and "missing" in r.stderr


def test_the_check_leaves_the_image_as_it_was(tmp_path, volla, fake_bin):
    image = make_image(good_tree(tmp_path / "t"), tmp_path / "a.img")
    before = hashlib.sha256(image.read_bytes()).hexdigest()
    assert check(volla, fake_bin, image).returncode == 0
    assert hashlib.sha256(image.read_bytes()).hexdigest() == before


# --- downloads.sh image -------------------------------------------------------------------------

def downloads(volla):
    return volla / "build" / "downloads"


def test_image_is_packed_with_a_latest_json_plinius_boot_takes(tmp_path, volla, fake_bin):
    image = make_image(good_tree(tmp_path / "t"), tmp_path / "plinius-arch-22112026.img")
    r = run(volla, fake_bin, "downloads.sh", "image", image, "2026.11.22")
    assert r.returncode == 0, r.stderr
    d = downloads(volla) / "images"
    packed = d / "plinius-arch-22112026.img.zst"
    assert subprocess.run(["zstd", "-dc", str(packed)], capture_output=True, check=True).stdout == image.read_bytes()
    digest = hashlib.sha256(packed.read_bytes()).hexdigest()
    assert json.loads((d / "latest.json").read_text()) == {
        "version": "2026.11.22", "url": "https://plinius.cais.de/images/plinius-arch-22112026.img.zst",
        "size": packed.stat().st_size, "sha256": digest}
    assert (d / "plinius-arch-22112026.img.zst.sha256").read_text() == f"{digest}  plinius-arch-22112026.img.zst\n"
    for f in d.iterdir():
        assert f.stat().st_mode & 0o777 == 0o644, f
    assert not list(d.glob("*.part"))


def test_latest_json_is_what_plinius_boot_accepts(tmp_path, volla, fake_bin):
    # latest_image() in Plinius Boot: https on the server of latest.json, a name of its rule, an
    # int size, 64 hex digits (the same rules, restated here as plinius-boot is another project)
    image = make_image(good_tree(tmp_path / "t"), tmp_path / "plinius-arch-22112026.img")
    assert run(volla, fake_bin, "downloads.sh", "image", image, DOWNLOAD_URL="https://dl.example.org").returncode == 0
    data = json.loads((downloads(volla) / "images" / "latest.json").read_text())
    assert data["url"] == "https://dl.example.org/images/plinius-arch-22112026.img.zst"
    assert type(data["size"]) is int and 0 < data["size"] < 1 << 40
    assert len(data["sha256"]) == 64 and set(data["sha256"]) <= set("0123456789abcdef")
    assert len(data["version"]) == 10  # the date, YYYY.MM.DD


def test_an_image_that_fails_the_check_is_not_offered(tmp_path, volla, fake_bin):
    tree = good_tree(tmp_path / "t")
    spoil_key(tree)
    image = make_image(tree, tmp_path / "plinius-arch-22112026.img")
    r = run(volla, fake_bin, "downloads.sh", "image", image)
    assert r.returncode == 1 and "failed the check, it is not offered" in r.stderr
    assert not (downloads(volla) / "images").exists()


@pytest.mark.parametrize("args, env, error", [
    (["image", "missing.img"], {}, "missing.img missing"),
    (["image", "with space.img"], {}, "is not a name Plinius Boot takes"),
    (["image", "plinius.img", "1 0"], {}, "invalid version"),
    (["image", "plinius.img"], {"DOWNLOAD_URL": "http://plinius.example.org"}, "DOWNLOAD_URL must be https"),
    (["image", "plinius.img"], {"ZSTD_LEVEL": "22"}, "ZSTD_LEVEL must be 1 to 19"),
    (["app", "plinius-boot.zip"], {}, "is not a Flatpak of Plinius Boot"),
    (["app", "org.plinius.Boot.flatpak", "x"], {}, None),
    (["upload"], {}, "DOWNLOAD_TARGET"),
    (["upload"], {"DOWNLOAD_TARGET": "u@h:/srv/www"}, "nothing in"),
    (["publish"], {}, "usage:"),
])
def test_wrong_arguments_stop_before_anything_happens(tmp_path, volla, fake_bin, args, env, error):
    for name in ("with space.img", "plinius.img", "plinius-boot.zip"):
        (tmp_path / name).write_text("x")
    r = subprocess.run(["bash", str(volla / "tools" / "downloads.sh"), *args], capture_output=True, text=True,
                       cwd=tmp_path, env=fake_bin.env(isolated=True, **env), stdin=subprocess.DEVNULL)
    if error is None:  # app with a missing file
        assert r.returncode == 1 and "org.plinius.Boot.flatpak missing" in r.stderr
    else:
        assert r.returncode == 1 and error in r.stderr, r.stderr
    assert fake_bin.calls("rsync") == []
    assert not (downloads(volla) / "images").exists()


def test_the_image_before_stays_older_ones_leave(tmp_path, volla, fake_bin):
    tree = good_tree(tmp_path / "t")
    for day in ("20", "21", "22"):
        image = make_image(tree, tmp_path / f"plinius-arch-{day}112026.img")
        assert run(volla, fake_bin, "downloads.sh", "image", image, f"2026.11.{day}").returncode == 0
    d = downloads(volla) / "images"
    assert sorted(f.name for f in d.iterdir()) == [
        "latest.json", "plinius-arch-21112026.img.zst", "plinius-arch-21112026.img.zst.sha256",
        "plinius-arch-22112026.img.zst", "plinius-arch-22112026.img.zst.sha256"]
    assert json.loads((d / "latest.json").read_text())["version"] == "2026.11.22"


# --- downloads.sh app ---------------------------------------------------------------------------

def test_the_app_goes_under_its_name_and_the_one_the_homepage_links(tmp_path, volla, fake_bin):
    app = tmp_path / "org.plinius.Boot-0.16-x86_64.flatpak"
    app.write_bytes(b"flatpak bundle")
    assert run(volla, fake_bin, "downloads.sh", "app", app).returncode == 0
    d = downloads(volla) / "downloads"
    assert (d / app.name).read_bytes() == b"flatpak bundle"
    assert (d / "org.plinius.Boot-x86_64.flatpak").read_bytes() == b"flatpak bundle"
    digest = hashlib.sha256(b"flatpak bundle").hexdigest()
    assert (d / "org.plinius.Boot-x86_64.flatpak.sha256").read_text() == f"{digest}  org.plinius.Boot-x86_64.flatpak\n"


def test_the_app_under_the_linked_name_is_not_doubled(tmp_path, volla, fake_bin):
    app = tmp_path / "org.plinius.Boot-x86_64.flatpak"
    app.write_bytes(b"bundle")
    assert run(volla, fake_bin, "downloads.sh", "app", app).returncode == 0
    assert sorted(f.name for f in (downloads(volla) / "downloads").iterdir()) == [
        "org.plinius.Boot-x86_64.flatpak", "org.plinius.Boot-x86_64.flatpak.sha256"]


# --- downloads.sh upload ------------------------------------------------------------------------

OPTIONS = ["-rltz", "--mkpath", "--no-owner", "--no-group", "--chmod=D755,F644", "--partial-dir=.upload",
           "-e", "ssh -p 2222 -o BatchMode=yes"]
ONLY_OURS = ["--include=*.img.zst", "--include=*.img.zst.sha256", "--include=latest.json", "--exclude=*"]


def test_upload_images_first_latest_json_after_then_the_app(tmp_path, volla, fake_bin):
    image = make_image(good_tree(tmp_path / "t"), tmp_path / "plinius-arch-22112026.img")
    app = tmp_path / "org.plinius.Boot-x86_64.flatpak"
    app.write_bytes(b"bundle")
    assert run(volla, fake_bin, "downloads.sh", "image", image).returncode == 0
    assert run(volla, fake_bin, "downloads.sh", "app", app).returncode == 0
    r = run(volla, fake_bin, "downloads.sh", "upload", DOWNLOAD_TARGET="u@h:/srv/www", DOWNLOAD_SSH_PORT="2222")
    assert r.returncode == 0, r.stderr
    d = downloads(volla)
    images = [f"{d}/images/", "u@h:/srv/www/images/"]
    assert fake_bin.calls("rsync") == [
        OPTIONS + ["--exclude=latest.json"] + ONLY_OURS + images,
        OPTIONS + ["--delay-updates"] + ONLY_OURS + images,
        OPTIONS + ["--delete-after"] + ONLY_OURS + images,
        OPTIONS + [f"{d}/downloads/", "u@h:/srv/www/downloads/"],
    ]


def test_upload_of_the_app_alone_leaves_images_alone(tmp_path, volla, fake_bin):
    app = tmp_path / "org.plinius.Boot-x86_64.flatpak"
    app.write_bytes(b"bundle")
    assert run(volla, fake_bin, "downloads.sh", "app", app).returncode == 0
    r = run(volla, fake_bin, "downloads.sh", "upload", DOWNLOAD_TARGET="u@h:/srv/www")
    assert r.returncode == 0
    calls = fake_bin.calls("rsync")
    assert len(calls) == 1 and calls[0][-1] == "u@h:/srv/www/downloads/"
    assert calls[0][7] == "ssh -p 22 -o BatchMode=yes"
