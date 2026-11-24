# make-parts of plinius-crypt-ramdisk: packs the password prompt's programs and scripts for the
# phone's Halium ramdisk. Run against a small system root in tmp_path whose "programs" are tiny
# shared objects compiled for the host (readelf only reads their NEEDED entries); the output goes
# into tmp_path. The parts must be what boot-image and the kernel take: one zstd frame with at most
# an 8 MB window, lz4 legacy for the scripts, the same bytes for the same input.

import shutil
import struct
import subprocess

import pytest

from boot_helpers import (boot_image_namespace, lvgl_xrgb, need, parse_newc, run_boot_image,
                          unlz4, vendor_boot, write)
from conftest import PKG, compile_c

MAKE_PARTS = PKG / "plinius-crypt-ramdisk" / "make-parts"
PROGRAMS = {"cryptsetup": "usr/bin/cryptsetup", "unl0kr": "usr/bin/unl0kr",
            "plinius-progress": "usr/bin/plinius-progress", "e2fsck": "usr/bin/e2fsck",
            "resize2fs": "usr/bin/resize2fs", "blkdiscard": "usr/bin/blkdiscard",
            "plinius-pwrate": "usr/lib/plinius-security/plinius-pwrate"}


@pytest.fixture(scope="module")
def sysroot(tmp_path_factory):
    need("cpio", "lz4", "zstd", "readelf")
    try:
        import PIL  # noqa: F401
    except ImportError:
        pytest.skip("no Pillow on the host")
    root = tmp_path_factory.mktemp("sysroot")
    build = tmp_path_factory.mktemp("objects")
    lib = root / "usr/lib"
    lib.mkdir(parents=True)
    (build / "x.c").write_text("int x;\n")
    so = ["-nostdlib", "-shared", "-fPIC"]
    # libdeep.so.2 <- libdep.so.1 <- each program; libgcc_s is loaded by name only
    compile_c(lib / "libdeep.so.2", [build / "x.c"], flags=so + ["-Wl,-soname,libdeep.so.2"])
    compile_c(lib / "libdep.so.1", [build / "x.c"],
              flags=so + ["-Wl,-soname,libdep.so.1", f"-L{lib}", "-l:libdeep.so.2"])
    compile_c(lib / "libgcc_s.so.1", [build / "x.c"], flags=so)
    write(lib / "ld-linux-aarch64.so.1", b"LOADER")
    for rel in PROGRAMS.values():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        compile_c(root / rel, [build / "x.c"], flags=so + [f"-L{lib}", "-l:libdep.so.1"])
    for rel, text in (("usr/share/X11/xkb/rules/evdev", "! model = keycodes\n"),
                      ("usr/share/X11/xkb/rules/base", "base rules\n"),
                      ("usr/share/X11/xkb/rules/evdev.lst", "list\n"),
                      ("usr/share/X11/xkb/geometry/pc", "geometry\n"),
                      ("usr/share/X11/xkb/symbols/us", "xkb_symbols\n"),
                      ("usr/share/X11/xkb/symbols/macintosh_vndr/us", "vendor\n"),
                      ("usr/share/libinput/50-system.quirks", "[quirk]\n"),
                      ("etc/unl0kr.conf", "[general]\n"),
                      ("etc/unl0kr.conf.d/60-plinius-look.conf", "[theme]\naccent=#000000\n"),
                      ("var/lib/pacman/local/cryptsetup-2.7.5-1/desc", "x"),
                      ("var/lib/pacman/local/buffybox-3.6.0-15/desc", "x")):
        write(root / rel, text.encode())
    write(root / "usr/share/plinius-crypt/glass-background.bin",
          lvgl_xrgb(108, 240, lambda x, y: (x, y, 128)))
    return root


def make_parts(sysroot, out, fake_bin, **env):
    real_readelf = shutil.which("readelf")
    fake_bin.stub("readelf", f'exec "{real_readelf}" "$@"')
    e = fake_bin.env(isolated=True, TMPDIR=out.parent,
                     PLINIUS_CRYPT_HOOK=PKG / "plinius-crypt/hook",
                     UNLOCK_SCRIPT=PKG / "plinius-crypt/halium-unlock",
                     PLINIUS_CRYPT_CONF=PKG / "plinius-crypt/50-plinius.conf", **env)
    return subprocess.run(["bash", str(MAKE_PARTS), str(sysroot), str(out)], env=e,
                          capture_output=True, text=True, timeout=120)


@pytest.fixture(scope="module")
def parts(sysroot, tmp_path_factory):
    from conftest import FakeBin
    base = tmp_path_factory.mktemp("parts")
    fb = FakeBin(base / "bin")
    outs = []
    for name in ("one", "two"):
        out = base / name
        r = make_parts(sysroot, out, fb)
        assert r.returncode == 0, r.stderr
        outs.append(out)
    return outs


def programs_files(out):
    data = subprocess.run(["zstd", "-q", "-dc", str(out / "programs.zst")], capture_output=True,
                          check=True).stdout
    return parse_newc(data)


def test_same_input_gives_the_same_parts(parts):
    one, two = parts
    for f in ("programs.zst", "init.lz4", "versions"):
        assert (one / f).read_bytes() == (two / f).read_bytes(), f


def test_programs_frame_fits_the_kernels_window(parts):
    frame = (parts[0] / "programs.zst").read_bytes()
    bi = boot_image_namespace()
    assert not frame[4] & 0x20, "a single-segment frame would claim a window as large as itself"
    assert bi["zstd_window"](frame) == bi["ZSTD_WINDOW_MAX"]


def test_programs_contents(parts):
    entries = programs_files(parts[0])
    files = {n.lstrip("./"): (m, b) for n, m, b in entries}
    for name in PROGRAMS:
        mode, wrapper = files[f"plinius/bin/{name}"]
        assert mode & 0o777 == 0o755
        env = b"DM_DISABLE_UDEV=1 " if name == "cryptsetup" else b""
        assert wrapper == (b"#!/bin/sh\n" + env + b"exec /plinius/lib/ld-linux-aarch64.so.1 "
                           b"--library-path /plinius/lib /plinius/libexec/" + name.encode()
                           + b' "$@"\n')
        assert f"plinius/libexec/{name}" in files
    # The libraries, also those only libraries need, and the loader
    for lib in ("libdep.so.1", "libdeep.so.2", "libgcc_s.so.1", "ld-linux-aarch64.so.1"):
        assert f"plinius/lib/{lib}" in files
    # What libxkbcommon never reads stays out
    names = set(files)
    assert "usr/share/X11/xkb/rules/evdev" in names
    assert not {"usr/share/X11/xkb/rules/base", "usr/share/X11/xkb/rules/evdev.lst",
                "usr/share/X11/xkb/geometry/pc", "usr/share/X11/xkb/symbols/macintosh_vndr/us"} & names
    assert "usr/share/X11/xkb/symbols/us" in names
    # Only the package's configuration: a look of the build system does not go in
    assert "etc/unl0kr.conf.d/60-plinius-look.conf" not in names
    assert files["etc/unl0kr.conf.d/50-plinius.conf"][1] == \
        (PKG / "plinius-crypt/50-plinius.conf").read_bytes()
    assert files["etc/unl0kr.conf.d/10-plinius-drm.conf"][1] == b"[general]\nbackend=drm\n"
    assert files["etc/unl0kr.conf.d/55-plinius-first-accent.conf"][1] == b"[theme]\naccent=#e66100\n"
    # The background at the ramdisk's small size
    bg = files["usr/share/plinius-crypt/glass-background.bin"][1]
    assert struct.unpack_from("<BBHHHHH", bg) == (0x19, 0x11, 0, 27, 60, 108, 0)
    # The boot logo as LVGL ARGB8888 images
    for logo in ("mark.bin", "wordmark.bin"):
        head = struct.unpack_from("<BBHHHHH", files[f"plinius/share/boot-logo/{logo}"][1])
        assert head[:3] == (0x19, 0x10, 0) and head[5] == head[3] * 4
        assert len(files[f"plinius/share/boot-logo/{logo}"][1]) == 12 + head[4] * head[5]


def test_archives_are_owned_by_root_with_time_zero(parts):
    for raw in (subprocess.run(["zstd", "-q", "-dc", str(parts[0] / "programs.zst")],
                               capture_output=True, check=True).stdout,
                unlz4((parts[0] / "init.lz4").read_bytes())):
        for pos in (p for p in range(len(raw) - 6) if raw[p:p + 6] == b"070701"):
            h = raw[pos:pos + 110]
            uid, gid, mtime = (int(h[a:a + 8], 16) for a in (22, 30, 46))
            assert (uid, gid, mtime) == (0, 0, 0)


def test_init_holds_the_hook_and_the_unlock_script(parts):
    data = (parts[0] / "init.lz4").read_bytes()
    assert data[:4] == b"\x02\x21\x4c\x18", "lz4 legacy, as UT's ramdisk"
    files = {n.lstrip("./"): (m, b) for n, m, b in parse_newc(unlz4(data))}
    assert files["plinius/hook"] == (0o100755, (PKG / "plinius-crypt/hook").read_bytes())
    assert files["plinius/unlock"] == (0o100755, (PKG / "plinius-crypt/halium-unlock").read_bytes())


def test_versions(parts):
    assert (parts[0] / "versions").read_text() == "buffybox 3.6.0-15\ncryptsetup 2.7.5-1\n"


def test_parts_go_into_a_vendor_boot(parts, tmp_path):
    # The contract with boot-image: build-vendor takes programs.zst, slim takes init.lz4
    ut = write(tmp_path / "ut", vendor_boot([(b"PLATFORM" * 100, 1), (b"REC", 2)]))
    r = run_boot_image("build-vendor", ut, tmp_path / "v", parts[0] / "programs.zst")
    assert r.returncode == 0, r.stderr
    r = run_boot_image("slim", parts[0] / "init.lz4", tmp_path / "init.lz4")
    assert r.returncode == 0, r.stderr
    files = {n.lstrip("./") for n, _, _ in parse_newc(unlz4((tmp_path / "init.lz4").read_bytes()))}
    assert {"plinius/hook", "plinius/unlock"} <= files


def test_missing_program_or_library_stops_it(sysroot, tmp_path, fake_bin):
    broken = tmp_path / "root"
    shutil.copytree(sysroot, broken, symlinks=True)
    (broken / "usr/lib/libdeep.so.2").unlink()
    r = make_parts(broken, tmp_path / "out", fake_bin)
    assert r.returncode != 0 and "not in the system root: libdeep.so.2" in r.stderr
    (broken / "usr/bin/unl0kr").unlink()
    r = make_parts(broken, tmp_path / "out2", fake_bin)
    assert r.returncode != 0 and "usr/bin/unl0kr is not in the system root" in r.stderr
    assert not (tmp_path / "out2/programs.zst").exists()
