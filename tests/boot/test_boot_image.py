# boot-image of plinius-crypt: the parser and writer of the phone's vendor_boot and init_boot
# images. A wrong byte here leaves a slot that does not start, so the round trips are checked
# against synthetic images built from the AOSP v4 layout (boot_helpers), never a real image.

import os
import stat
import struct
import subprocess

import pytest

from boot_helpers import (BOOT_IMAGE, ENTRY_SIZE, PAGE, align, boot_image_namespace, init_boot,
                          lvgl_xrgb, lz4_legacy, need, newc, parse_newc, run_boot_image, unlz4,
                          ut_init_ramdisk, vendor_boot, write, zstd_pipe)
from conftest import PKG

PLATFORM = bytes(range(256)) * 11 + b"UT-PLATFORM-END"
RECOVERY = b"RECOVERY" * 100


@pytest.fixture
def bi():
    return boot_image_namespace()


def ut_vendor(**kw):
    return vendor_boot([(PLATFORM, 1), (RECOVERY, 2)], **kw)


def ok(r):
    assert r.returncode == 0, r.stderr.decode()
    return r.stdout.decode()


def refused(r, text=None):
    assert r.returncode != 0
    if text:
        assert text in r.stderr.decode()
    return r.stderr.decode()


# --- the copy in plinius-crypt-ramdisk -----------------------------------------------------------

def test_ramdisk_package_uses_the_same_boot_image():
    # plinius-crypt-ramdisk builds its parts with this file: a diverging copy would make parts
    # that halium-rebuild (plinius-crypt's copy) puts together differently
    other = PKG / "plinius-crypt-ramdisk" / "boot-image"
    assert other.read_bytes() == BOOT_IMAGE.read_bytes()


def test_usage_errors():
    assert "usage" in refused(run_boot_image())
    assert "usage" in refused(run_boot_image("frobnicate"))
    assert "usage" in refused(run_boot_image("length"))
    assert "usage" in refused(run_boot_image("length", "a", "b"))
    # build-vendor needs at least UT_VENDOR OUT PROGRAMS
    assert "usage" in refused(run_boot_image("build-vendor", "a", "b"))


# --- vendor_boot: layout, kind, length -----------------------------------------------------------

def test_layout_matches_aosp_v4(bi):
    image = ut_vendor()
    p = bi["layout"](image[:PAGE])
    assert p["first"] == PAGE
    assert p["ramdisks"] == len(PLATFORM) + len(RECOVERY)
    assert p["dtb_at"] == PAGE + align(p["ramdisks"])
    assert p["count"] == 2 and p["entry_size"] == ENTRY_SIZE
    assert p["end"] == len(image)
    assert bi["entries"](image, p) == [(len(PLATFORM), 0, 1), (len(RECOVERY), len(PLATFORM), 2)]


@pytest.mark.parametrize("page", [2048, 4096, 16384])
def test_length_for_page_sizes(tmp_path, page):
    image = ut_vendor(page=page)
    f = write(tmp_path / "v.img", image + b"\xff" * 5000)  # a partition goes on behind the image
    assert ok(run_boot_image("length", f)).strip() == str(len(image))


def test_kind(tmp_path):
    assert ok(run_boot_image("kind", write(tmp_path / "ut", ut_vendor()))).strip() == "ut"
    plinius = vendor_boot([(PLATFORM, 1)])
    assert ok(run_boot_image("kind", write(tmp_path / "pl", plinius))).strip() == "plinius"


@pytest.mark.parametrize("ramdisks", [
    [(PLATFORM, 2)],                              # one entry, but not a platform ramdisk
    [(PLATFORM, 1), (RECOVERY, 1)],               # second one not recovery
    [(PLATFORM, 1), (RECOVERY, 2), (b"x", 2)],    # three entries
])
def test_kind_refuses_other_tables(tmp_path, ramdisks):
    refused(run_boot_image("kind", write(tmp_path / "v", vendor_boot(ramdisks))),
            "unexpected ramdisks")


@pytest.mark.parametrize("kw,text", [
    (dict(magic=b"ANDROID!"), "not a vendor boot image of version 4"),
    (dict(version=3), "not a vendor boot image of version 4"),
])
def test_wrong_magic_or_version_is_refused(tmp_path, kw, text):
    f = write(tmp_path / "v", ut_vendor(**kw))
    for cmd in (["length", f], ["kind", f], ["platform", f, tmp_path / "o"],
                ["ramdisk", f, tmp_path / "o"], ["dtb", f, tmp_path / "o"]):
        refused(run_boot_image(*cmd), text)
    assert not (tmp_path / "o").exists()


def test_truncated_vendor_boot_is_refused(tmp_path):
    image = ut_vendor()
    cut = write(tmp_path / "cut", image[:-PAGE])  # bootconfig page missing
    refused(run_boot_image("kind", cut), "cut short")
    refused(run_boot_image("platform", cut, tmp_path / "o"), "cut short")
    refused(run_boot_image("build-vendor", cut, tmp_path / "o", tmp_path / "p"), "cut short")
    assert not (tmp_path / "o").exists()
    # Not even a whole header
    tiny = write(tmp_path / "tiny", image[:100])
    refused(run_boot_image("length", tiny))
    refused(run_boot_image("kind", tiny))


def test_ramdisk_of_plinius_image_and_its_truncation(tmp_path):
    rd = b"ONE-ENTRY" * 700
    image = vendor_boot([(rd, 1)])
    ok(run_boot_image("ramdisk", write(tmp_path / "v", image), tmp_path / "out"))
    assert (tmp_path / "out").read_bytes() == rd
    refused(run_boot_image("ramdisk", write(tmp_path / "cut", image[:PAGE + 100]), tmp_path / "o2"),
            "cut short")
    refused(run_boot_image("ramdisk", write(tmp_path / "ut", ut_vendor()), tmp_path / "o3"),
            "2 entries")


def test_platform_dtb_cmdline(tmp_path):
    f = write(tmp_path / "ut", ut_vendor(dtb=b"\xd0\x0d\xfe\xed" + b"D" * 5000,
                                         cmdline=b"bootconfig console=tty0"))
    ok(run_boot_image("platform", f, tmp_path / "plat"))
    assert (tmp_path / "plat").read_bytes() == PLATFORM
    ok(run_boot_image("dtb", f, tmp_path / "dtb"))
    assert (tmp_path / "dtb").read_bytes() == b"\xd0\x0d\xfe\xed" + b"D" * 5000
    assert ok(run_boot_image("cmdline", f)) == "bootconfig console=tty0\n"


# --- build-vendor --------------------------------------------------------------------------------

def test_build_vendor_round_trip(tmp_path):
    need("zstd")
    ut = ut_vendor(dtb=b"DTB!" * 999, bootconfig=b"androidboot.x=1\n" * 7)
    programs = zstd_pipe(b"programs " * 3000)
    assert len(programs) % 4, "want a length that needs the zero padding"
    a1, a2 = b"ARCHIVE-ONE" * 33, b"ARCHIVE-TWO" * 55
    files = [write(tmp_path / n, d) for n, d in
             (("ut", ut), ("p.zst", programs), ("a1", a1), ("a2", a2))]
    out = tmp_path / "new.img"
    size = int(ok(run_boot_image("build-vendor", files[0], out, *files[1:])))
    expect = programs + b"\0" * (-len(programs) % 4) + PLATFORM + a1 + a2
    assert size == len(expect)
    assert ok(run_boot_image("kind", out)).strip() == "plinius"
    ok(run_boot_image("ramdisk", out, tmp_path / "rd"))
    assert (tmp_path / "rd").read_bytes() == expect
    assert int(ok(run_boot_image("length", out))) == out.stat().st_size
    # Everything else stays as UT had it
    ok(run_boot_image("dtb", out, tmp_path / "dtb"))
    assert (tmp_path / "dtb").read_bytes() == b"DTB!" * 999
    assert ok(run_boot_image("cmdline", out)) == ok(run_boot_image("cmdline", files[0]))
    new = out.read_bytes()
    assert struct.unpack_from("<I", new, 24)[0] == size
    old_p = boot_image_namespace()["layout"](ut[:PAGE])
    new_p = boot_image_namespace()["layout"](new[:PAGE])
    assert new[new_p["config_at"]:new_p["config_at"] + new_p["bootconfig"]] == \
        ut[old_p["config_at"]:old_p["config_at"] + old_p["bootconfig"]]
    assert new_p["table_size"] == ENTRY_SIZE and new_p["count"] == 1
    # The entry keeps UT's name and board id
    assert new[new_p["table_at"] + 12:new_p["table_at"] + ENTRY_SIZE] == \
        ut[old_p["table_at"] + 12:old_p["table_at"] + ENTRY_SIZE]
    # The platform ramdisk starts at a multiple of 4 (the kernel reads a plain cpio only there)
    assert expect.index(PLATFORM) % 4 == 0
    assert ok(run_boot_image("contains", tmp_path / "rd", write(tmp_path / "plat", PLATFORM))) == ""


def test_build_vendor_is_deterministic(tmp_path):
    need("zstd")
    ut = write(tmp_path / "ut", ut_vendor())
    p = write(tmp_path / "p", zstd_pipe(b"x" * 999))
    ok(run_boot_image("build-vendor", ut, tmp_path / "a", p))
    ok(run_boot_image("build-vendor", ut, tmp_path / "b", p))
    assert (tmp_path / "a").read_bytes() == (tmp_path / "b").read_bytes()


def test_build_vendor_wants_uts_image(tmp_path):
    need("zstd")
    plinius = write(tmp_path / "pl", vendor_boot([(PLATFORM, 1)]))
    p = write(tmp_path / "p", zstd_pipe(b"x"))
    refused(run_boot_image("build-vendor", plinius, tmp_path / "o", p), "is not UT's vendor_boot")
    assert not (tmp_path / "o").exists()


def test_build_vendor_wants_a_zstd_frame(tmp_path):
    ut = write(tmp_path / "ut", ut_vendor())
    p = write(tmp_path / "p", lz4_legacy(b"not zstd"))
    refused(run_boot_image("build-vendor", ut, tmp_path / "o", p), "no zstd frame")
    assert not (tmp_path / "o").exists()


def test_build_vendor_refuses_a_window_the_kernel_cannot_take(tmp_path):
    need("zstd")
    ut = write(tmp_path / "ut", ut_vendor())
    ok(run_boot_image("build-vendor", ut, tmp_path / "o23", write(tmp_path / "w23",
                                                                   zstd_pipe(b"y" * 100, 23))))
    big = write(tmp_path / "w24", zstd_pipe(b"y" * 100, 24))
    refused(run_boot_image("build-vendor", ut, tmp_path / "o24", big), "larger than the kernel takes")
    assert not (tmp_path / "o24").exists()


# --- zstd window (RFC 8878 3.1.1.1) --------------------------------------------------------------

@pytest.mark.parametrize("wlog", [10, 17, 20, 23, 24, 27])
def test_zstd_window_from_window_descriptor(bi, wlog):
    need("zstd")
    frame = zstd_pipe(b"z" * 50, wlog)
    assert bi["zstd_window"](frame) == 1 << wlog


def test_zstd_window_descriptor_mantissa(bi):
    # Exponent 13, mantissa 5: base 2^23, plus 5/8 of it
    frame = b"\x28\xb5\x2f\xfd" + bytes([0x00, (13 << 3) | 5])
    assert bi["zstd_window"](frame) == (1 << 23) + 5 * (1 << 20)


@pytest.mark.parametrize("size", [1, 255, 256, 1000, 65791, 65792, 300000])
def test_zstd_window_single_segment_is_the_content_size(bi, tmp_path, size):
    need("zstd")
    # From a file zstd knows the size and writes a single-segment frame for a small input
    src = write(tmp_path / "in", os.urandom(size))
    frame = subprocess.run(["zstd", "-q", "-c", str(src)], capture_output=True, check=True).stdout
    assert frame[4] & 0x20, "expected a single-segment frame"
    assert bi["zstd_window"](frame) == size


@pytest.mark.parametrize("did_flag,did_len", [(1, 1), (2, 2), (3, 4)])
def test_zstd_window_single_segment_after_dictionary_id(bi, did_flag, did_len):
    # Field sizes: 4-byte content size (flag 2) behind a dictionary id of 1, 2 or 4 bytes
    descriptor = (2 << 6) | 0x20 | did_flag
    frame = b"\x28\xb5\x2f\xfd" + bytes([descriptor]) + b"\xaa" * did_len \
        + (123456).to_bytes(4, "little")
    assert bi["zstd_window"](frame) == 123456


# --- append (the base of the earlier versions) ---------------------------------------------------

def test_append_round_trip(tmp_path):
    rd = b"BASE-RAMDISK" * 345
    base = vendor_boot([(rd, 1)], dtb=b"T" * 777, bootconfig=b"bc=1\n")
    look = b"LOOK" * 1001
    f, a = write(tmp_path / "base", base), write(tmp_path / "look", look)
    size = int(ok(run_boot_image("append", f, a, tmp_path / "new")))
    assert size == len(rd) + len(look)
    out = tmp_path / "new"
    ok(run_boot_image("ramdisk", out, tmp_path / "rd"))
    assert (tmp_path / "rd").read_bytes() == rd + look
    ok(run_boot_image("dtb", out, tmp_path / "dtb"))
    assert (tmp_path / "dtb").read_bytes() == b"T" * 777
    assert ok(run_boot_image("kind", out)).strip() == "plinius"
    assert int(ok(run_boot_image("length", out))) == out.stat().st_size
    assert out.stat().st_size % PAGE == 0


def test_append_refuses_uts_two_entries(tmp_path):
    f, a = write(tmp_path / "ut", ut_vendor()), write(tmp_path / "a", b"x")
    refused(run_boot_image("append", f, a, tmp_path / "o"), "unexpected ramdisks")
    assert not (tmp_path / "o").exists()


def test_append_refuses_a_base_without_its_table(tmp_path):
    base = vendor_boot([(b"BASE" * 100, 1)], dtb=b"T" * 5000)
    cut = write(tmp_path / "base", base[:2 * PAGE + 1000])  # header, ramdisk, part of the dtb
    r = run_boot_image("append", cut, write(tmp_path / "a", b"LOOK"), tmp_path / "o")
    assert r.returncode != 0 and not (tmp_path / "o").exists()


def test_append_refuses_a_base_cut_short_in_its_bootconfig(tmp_path):
    base = vendor_boot([(b"BASE" * 100, 1)], bootconfig=b"androidboot.slot=x\n" * 20)
    cut = write(tmp_path / "base", base[:len(base) - PAGE + 10])
    r = run_boot_image("append", cut, write(tmp_path / "a", b"LOOK"), tmp_path / "o")
    assert r.returncode != 0 and not (tmp_path / "o").exists()


# --- init_boot -----------------------------------------------------------------------------------

def test_init_ramdisk_and_size(tmp_path):
    rd = b"INIT-RAMDISK" * 500
    f = write(tmp_path / "init", init_boot(rd, total=64 * 1024))
    assert ok(run_boot_image("ramdisk-size", f)).strip() == str(len(rd))
    ok(run_boot_image("init-ramdisk", f, tmp_path / "rd"))
    assert (tmp_path / "rd").read_bytes() == rd


@pytest.mark.parametrize("kw", [dict(magic=b"VNDRBOOT"), dict(version=3), dict(kernel_size=4096)])
def test_init_boot_refusals(tmp_path, kw):
    f = write(tmp_path / "init", init_boot(b"rd" * 100, total=32768, **kw))
    a = write(tmp_path / "a", b"arch")
    refused(run_boot_image("init-ramdisk", f, tmp_path / "o"))
    refused(run_boot_image("build-init", f, tmp_path / "o", a))
    if "kernel_size" not in kw:
        refused(run_boot_image("ramdisk-size", f), "not a boot image of version 4")
    assert not (tmp_path / "o").exists()


def test_build_init_round_trip(tmp_path):
    rd = b"UT-INIT" * 300
    old = init_boot(rd, total=128 * 1024, signature=b"SIG" * 100)
    f = write(tmp_path / "ut", old)
    a1, a2 = write(tmp_path / "a1", b"HALIUM" * 50), write(tmp_path / "a2", b"INIT-LZ4" * 70)
    size = int(ok(run_boot_image("build-init", f, tmp_path / "new", a1, a2)))
    new = (tmp_path / "new").read_bytes()
    assert size == len(rd) + 300 + 560
    assert len(new) == len(old), "init_boot keeps UT's length"
    assert struct.unpack_from("<I", new, 12)[0] == size
    assert struct.unpack_from("<I", new, 1580)[0] == 0, "no signature any more"
    assert new[PAGE:PAGE + size] == rd + b"HALIUM" * 50 + b"INIT-LZ4" * 70
    assert new[PAGE + size:] == b"\0" * (len(new) - PAGE - size)
    # Header fields other than the ramdisk size and the signature size stay
    assert new[:12] == old[:12] and new[16:1580] == old[16:1580]
    ok(run_boot_image("init-ramdisk", tmp_path / "new", tmp_path / "rd"))
    assert (tmp_path / "rd").read_bytes().startswith(rd)


def test_build_init_refuses_growing_past_uts_length(tmp_path):
    f = write(tmp_path / "ut", init_boot(b"r" * 100, total=3 * PAGE))
    big = write(tmp_path / "big", b"A" * (2 * PAGE))
    refused(run_boot_image("build-init", f, tmp_path / "o", big), "would be")
    assert not (tmp_path / "o").exists()
    # Exactly filling it is fine
    fits = write(tmp_path / "fits", b"A" * (PAGE - 100))
    ok(run_boot_image("build-init", f, tmp_path / "o", fits))
    assert (tmp_path / "o").stat().st_size == 3 * PAGE


def test_build_init_refuses_a_truncated_init_boot(tmp_path):
    # Caught when it is read: its ramdisk is not all there
    cut = write(tmp_path / "ut", init_boot(b"UT-INIT" * 2000)[:PAGE + 5000])
    refused(run_boot_image("build-init", cut, tmp_path / "o", write(tmp_path / "a", b"arch")),
            "cut short")
    assert not (tmp_path / "o").exists()


def test_init_ramdisk_refuses_a_truncated_init_boot(tmp_path):
    cut = write(tmp_path / "ut", init_boot(b"UT-INIT" * 2000)[:PAGE + 5000])
    r = run_boot_image("init-ramdisk", cut, tmp_path / "o")
    assert r.returncode != 0


def test_kernel_of_a_boot_image(tmp_path):
    kernel = b"ARM64-KERNEL" * 1000
    head = struct.pack("<8sIIII", b"ANDROID!", len(kernel), 0, 0, 1584).ljust(PAGE, b"\0")
    f = write(tmp_path / "boot", head + kernel + b"\0" * 100)
    ok(run_boot_image("kernel", f, tmp_path / "k"))
    assert (tmp_path / "k").read_bytes() == kernel
    refused(run_boot_image("kernel", write(tmp_path / "x", b"\0" * PAGE), tmp_path / "k2"),
            "not a boot image")


def test_contains(tmp_path):
    f = write(tmp_path / "f", b"aaaa" + PLATFORM + b"bbbb")
    assert run_boot_image("contains", f, write(tmp_path / "p", PLATFORM)).returncode == 0
    assert run_boot_image("contains", f, write(tmp_path / "q", PLATFORM + b"!")).returncode == 1


# --- cpio ----------------------------------------------------------------------------------------

def make_tree(root, mtime):
    (root / "etc/unl0kr.conf.d").mkdir(parents=True)
    (root / "etc/unl0kr.conf.d/60-look.conf").write_text("[theme]\n")
    (root / "a-b").write_bytes(b"dash")
    (root / "usr/share").mkdir(parents=True)
    (root / "usr/share/bin.dat").write_bytes(bytes(range(256)) * 3 + b"x")
    (root / "run.sh").write_text("#!/bin/sh\n")
    (root / "run.sh").chmod(0o750)
    (root / "etc").chmod(0o700)
    for p in [root, *root.rglob("*")]:
        os.utime(p, (mtime, mtime), follow_symlinks=False)


def test_cpio_is_deterministic(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    make_tree(a, 1000)
    make_tree(b, 2_000_000_000)
    ok(run_boot_image("cpio", a, tmp_path / "a.cpio"))
    ok(run_boot_image("cpio", b, tmp_path / "b.cpio"))
    assert (tmp_path / "a.cpio").read_bytes() == (tmp_path / "b.cpio").read_bytes()


def test_cpio_contents(tmp_path):
    root = tmp_path / "root"
    make_tree(root, 5)
    ok(run_boot_image("cpio", root, tmp_path / "o.cpio"))
    data = (tmp_path / "o.cpio").read_bytes()
    assert len(data) % 4 == 0
    entries = parse_newc(data)
    names = [n for n, _, _ in entries]
    # Sorted, each directory before what is in it, the trailer last
    assert names == ["./a-b", "./etc", "./etc/unl0kr.conf.d", "./etc/unl0kr.conf.d/60-look.conf",
                     "./run.sh", "./usr", "./usr/share", "./usr/share/bin.dat", "TRAILER!!!"]
    modes = {n: m for n, m, _ in entries}
    assert modes["./run.sh"] == stat.S_IFREG | 0o750
    assert modes["./etc"] == stat.S_IFDIR | 0o700
    bodies = {n: b for n, _, b in entries}
    assert bodies["./usr/share/bin.dat"] == (root / "usr/share/bin.dat").read_bytes()
    # Owner root, time 0, consecutive inode numbers in every header
    for i, pos in enumerate(p for p in range(0, len(data) - 6) if data[p:p + 6] == b"070701"):
        h = data[pos:pos + 110]
        ino, uid, gid, mtime = (int(h[a:a + 8], 16) for a in (6, 22, 30, 46))
        assert (uid, gid, mtime) == (0, 0, 0)
        assert ino == (i + 1 if i + 1 < len(entries) else 0)


def test_cpio_extracts_with_gnu_cpio(tmp_path):
    need("cpio")
    root = tmp_path / "root"
    make_tree(root, 5)
    ok(run_boot_image("cpio", root, tmp_path / "o.cpio"))
    dest = tmp_path / "x"
    dest.mkdir()
    subprocess.run(["cpio", "--quiet", "-i", "-d", "--no-absolute-filenames", "-F",
                    str(tmp_path / "o.cpio")], cwd=dest, check=True)
    assert (dest / "usr/share/bin.dat").read_bytes() == (root / "usr/share/bin.dat").read_bytes()
    assert (dest / "run.sh").stat().st_mode & 0o777 == 0o750


def test_cpio_refuses_links(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to("/etc/passwd")
    refused(run_boot_image("cpio", root, tmp_path / "o"), "only directories and files")


# --- slim ----------------------------------------------------------------------------------------

def test_slim_drops_comment_lines_of_scripts_only(tmp_path):
    need("lz4")
    script = b"#!/bin/sh\n# a comment\necho one  # stays\n    # indented comment\necho two\n"
    heredoc = b"#!/bin/sh\ncat <<EOF\n# not a comment\nEOF\n"
    data = b"# a data file, not a script\n\x00\x01"
    entries = [("plinius", 0o40755, b""), ("plinius/hook", 0o100755, script),
               ("plinius/unlock", 0o100755, heredoc), ("plinius/data", 0o100644, data)]
    src = write(tmp_path / "init.lz4", lz4_legacy(newc(entries)))
    ok(run_boot_image("slim", src, tmp_path / "out.lz4"))
    out = parse_newc(unlz4((tmp_path / "out.lz4").read_bytes()))
    assert [(n, m) for n, m, _ in out] == [(n, m) for n, m, _ in entries] + [("TRAILER!!!", 0)]
    bodies = {n: b for n, _, b in out}
    assert bodies["plinius/hook"] == b"#!/bin/sh\necho one  # stays\necho two\n"
    assert bodies["plinius/unlock"] == heredoc
    assert bodies["plinius/data"] == data


def test_slim_keeps_the_real_hook_working(tmp_path):
    # The hook of plinius-crypt goes into init.lz4 and is slimmed there: its functions and
    # settings must stay exactly the same
    need("lz4")
    hook = (PKG / "plinius-crypt/hook").read_bytes()
    unlock = (PKG / "plinius-crypt/halium-unlock").read_bytes()
    src = write(tmp_path / "init.lz4", lz4_legacy(newc([("plinius/hook", 0o100755, hook),
                                                        ("plinius/unlock", 0o100755, unlock)])))
    ok(run_boot_image("slim", src, tmp_path / "out.lz4"))
    bodies = {n: b for n, _, b in parse_newc(unlz4((tmp_path / "out.lz4").read_bytes()))}
    assert len(bodies["plinius/hook"]) < len(hook)
    # halium-unlock has here-documents: left as it is
    assert bodies["plinius/unlock"] == unlock
    slim = write(tmp_path / "slim-hook", bodies["plinius/hook"])
    orig = PKG / "plinius-crypt/hook"
    show = 'set -e; . "$1"; declare -f; declare -p | grep "^declare -- PLINIUS_"'
    a = subprocess.run(["bash", "-c", show, "x", str(orig)], capture_output=True, text=True)
    b = subprocess.run(["bash", "-c", show, "x", str(slim)], capture_output=True, text=True)
    assert a.returncode == 0 and a.stdout
    assert a.stdout == b.stdout


# --- halium-script -------------------------------------------------------------------------------

def run_halium_script(tmp_path, ramdisk):
    f = write(tmp_path / "ut-init", init_boot(ramdisk, total=256 * 1024))
    out = tmp_path / "scripts-out"
    return run_boot_image("halium-script", f, out), out


def test_halium_script(tmp_path):
    need("lz4")
    r, out = run_halium_script(tmp_path, ut_init_ramdisk(archives=2))
    ok(r)
    text = (out / "scripts/halium").read_text()
    # The unlock call right before UT's formatting of a wiped userdata (whose comment goes with
    # the others)
    assert "( . /plinius/unlock \"$path\" )" in text
    assert text.index("/plinius/unlock") < text.index("mount \"$path\" /tmpmnt")
    assert text.index("path=/dev/sda32") < text.index("/plinius/unlock")
    assert "[ -s /run/plinius-crypt/data-dev ] && path=$(cat /run/plinius-crypt/data-dev)" in text
    # The fixed second is gone, the battery modules load in the background
    assert "\tsleep 1\n" not in text and "sleep 0.1" in text
    assert "sh366100_battery|mt6375-battery)" in text
    assert "/sbin/modprobe \"$modname\" > /dev/kmsg 2>&1 & ;;" in text
    # Comment lines gone (the shebang stays); taken from the last of UT's two archives
    assert text.startswith("#!/bin/sh\n")
    assert all(not line.lstrip().startswith("#") for line in text.split("\n")[1:])
    assert "older" not in text
    assert (out / "scripts/halium").stat().st_mode & 0o777 == 0o750
    order = (out / "scripts/init-top/ORDER").read_text()
    lines = order.splitlines()
    assert lines[0] == '/scripts/init-top/plinius-first "$@"'
    assert lines[-2] == '/scripts/init-top/plinius-last "$@"'
    assert '/scripts/init-top/udev "$@"' in lines[2:-2]
    for name in ("plinius-first", "plinius-last"):
        f = out / "scripts/init-top" / name
        assert f.stat().st_mode & 0o777 == 0o755
        r = subprocess.run(["bash", "-n", str(f)], capture_output=True)
        assert r.returncode == 0
    assert (out / "scripts/init-top/ORDER").stat().st_mode & 0o777 == 0o644
    r = subprocess.run(["bash", "-n", str(out / "scripts/halium")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("change,text", [
    (lambda s: s.replace("\t# Format a wiped userdata without a filesystem header\n", ""),
     "place for the unlock call"),
    (lambda s: s + "\t# Format a wiped userdata without a filesystem header\n",
     "place for the unlock call"),
    (lambda s: s.replace("\tsleep 1\n", "\tsleep 2\n"), "start time change"),
    (lambda s: s.replace("/sbin/modprobe", "modprobe"), "start time change"),
])
def test_halium_script_refuses_an_unknown_script(tmp_path, change, text):
    from boot_helpers import UT_HALIUM
    need("lz4")
    r, out = run_halium_script(tmp_path, ut_init_ramdisk(halium=change(UT_HALIUM)))
    refused(r, text)
    assert not (out / "scripts/halium").exists()


def test_halium_script_needs_the_script_and_order(tmp_path):
    need("lz4")
    no_script = lz4_legacy(newc([("init", 0o100755, b"x")]))
    r, _ = run_halium_script(tmp_path, no_script)
    refused(r, "no scripts/halium")
    from boot_helpers import UT_HALIUM
    no_order = lz4_legacy(newc([("scripts/halium", 0o100755, UT_HALIUM.encode())]))
    r, _ = run_halium_script(tmp_path / "2", no_order)
    refused(r, "no scripts/init-top/ORDER")


# --- shrink-background ---------------------------------------------------------------------------

def read_lvgl(data):
    return struct.unpack_from("<BBHHHHH", data), data[12:]


def test_shrink_background_uniform_and_size(tmp_path):
    src = write(tmp_path / "bg", lvgl_xrgb(108, 240, lambda x, y: (200, 100, 50)))
    ok(run_boot_image("shrink-background", src, tmp_path / "o"))
    head, pixels = read_lvgl((tmp_path / "o").read_bytes())
    assert head == (0x19, 0x11, 0, 27, 60, 108, 0)
    assert len(pixels) == 27 * 60 * 4
    assert pixels == bytes((200, 100, 50, 255)) * (27 * 60)


def test_shrink_background_averages_areas(tmp_path):
    # Columns alternate black/white, 4 source pixels per target pixel; the stride has padding
    src = write(tmp_path / "bg", lvgl_xrgb(108, 240, lambda x, y: (255,) * 3 if x % 2 else (0,) * 3,
                                           stride=108 * 4 + 16))
    ok(run_boot_image("shrink-background", src, tmp_path / "o"))
    _, pixels = read_lvgl((tmp_path / "o").read_bytes())
    assert set(pixels[i] for i in range(0, len(pixels), 4)) == {128}  # round(127.5)
    # A left/right split stays split
    src = write(tmp_path / "bg2", lvgl_xrgb(54, 120, lambda x, y: (0, 0, 255) if x < 27 else (0, 255, 0)))
    ok(run_boot_image("shrink-background", src, tmp_path / "o2"))
    _, pixels = read_lvgl((tmp_path / "o2").read_bytes())
    row = [tuple(pixels[i:i + 3]) for i in range(0, 27 * 4, 4)]
    assert row[:13] == [(0, 0, 255)] * 13 and row[14:] == [(0, 255, 0)] * 13
    assert row[13] == (0, 128, 128)


@pytest.mark.parametrize("data", [
    b"short",
    lvgl_xrgb(4, 4)[:-1],                                        # pixels cut short
    bytes([0x18]) + lvgl_xrgb(4, 4)[1:],                         # magic
    lvgl_xrgb(4, 4)[:1] + bytes([0x10]) + lvgl_xrgb(4, 4)[2:],   # ARGB8888
    struct.pack("<BBHHHHH", 0x19, 0x11, 0, 4, 4, 15, 0) + b"\0" * 64,   # stride < 4*w
    struct.pack("<BBHHHHH", 0x19, 0x11, 0, 0, 4, 0, 0),                 # no width
])
def test_shrink_background_refuses_other_images(tmp_path, data):
    refused(run_boot_image("shrink-background", write(tmp_path / "bg", data), tmp_path / "o"),
            "LVGL image")
    assert not (tmp_path / "o").exists()
