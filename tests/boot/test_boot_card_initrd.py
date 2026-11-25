# tools/card-initrd.py: the ramdisk of a boot entry on the card as one zstd archive, with the
# same bytes for the kernel to unpack as the programs' zstd frame and the lz4 archives behind it.

import importlib.util
import subprocess
import sys
from compression import zstd

import pytest

from conftest import TOOLS

SCRIPT = TOOLS / "card-initrd.py"


@pytest.fixture
def mod():
    spec = importlib.util.spec_from_file_location("card_initrd", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def lz4_legacy(data):
    return subprocess.run(["lz4", "-q", "-12", "-l", "-c"], input=data, capture_output=True,
                          check=True).stdout


def cpio(name, body):
    """Stands in for a newc archive: what matters is that the bytes come back in order."""
    return b"070701" + name.encode() + b"\0" + body + b"TRAILER!!!\0"


PROGRAMS = cpio("plinius", b"P" * 5003)  # not a multiple of 4: padded in the stream
PLATFORM = cpio("platform", bytes(range(256)) * 300)
INIT = cpio("init", b"I" * 700)


def card_ramdisk(pad=b"\0"):
    """As crypt-ramdisk.sh puts it together: programs (zstd), zeros, then lz4 legacy archives."""
    return zstd.compress(PROGRAMS) + pad + lz4_legacy(PLATFORM) + lz4_legacy(INIT)


def run(*args):
    return subprocess.run([sys.executable, "-I", str(SCRIPT), *map(str, args)], capture_output=True,
                          text=True)


def test_one_zstd_archive_with_the_same_bytes(tmp_path):
    src, out = tmp_path / "in", tmp_path / "out"
    src.write_bytes(card_ramdisk())
    r = run(src, out)
    assert r.returncode == 0, r.stderr
    packed = out.read_bytes()
    d = zstd.ZstdDecompressor()
    raw = d.decompress(packed)
    assert d.eof and d.unused_data == b""  # one frame, nothing behind it
    assert raw == PROGRAMS + b"\0" * (-len(PROGRAMS) % 4) + PLATFORM + INIT
    assert "as one zstd archive" in r.stderr


def test_window_fits_the_kernel(mod, tmp_path):
    src, out = tmp_path / "in", tmp_path / "out"
    # More than the kernel's 8 MB window: zstd must not pick a larger one
    big = cpio("big", bytes(range(256)) * (40 << 12))
    src.write_bytes(zstd.compress(PROGRAMS) + lz4_legacy(big))
    assert run(src, out).returncode == 0
    assert mod.zstd_window(out.read_bytes()) <= mod.WINDOW_MAX


@pytest.mark.parametrize("pad", [b"", b"\0\0\0"])
def test_zeros_between_the_archives(tmp_path, pad):
    src, out = tmp_path / "in", tmp_path / "out"
    src.write_bytes(card_ramdisk(pad))
    assert run(src, out).returncode == 0
    assert zstd.decompress(out.read_bytes()).endswith(PLATFORM + INIT)


@pytest.mark.parametrize("data, message", [
    (lz4_legacy(PLATFORM), "does not start with the programs' zstd frame"),
    (zstd.compress(PROGRAMS)[:-6], "cut off"),
    (zstd.compress(PROGRAMS) + b"\x1f\x8b\x08" + b"x" * 20, "no lz4 legacy archives"),
    (zstd.compress(PROGRAMS) + b"\x02\x21\x4c\x18" + b"\xff" * 40, "do not unpack"),
])
def test_refuses_what_is_no_card_ramdisk(tmp_path, data, message):
    src, out = tmp_path / "in", tmp_path / "out"
    src.write_bytes(data)
    r = run(src, out)
    assert r.returncode != 0 and message in r.stderr
    assert not out.exists()


def test_usage(tmp_path):
    r = run(tmp_path / "in")
    assert r.returncode != 0 and "usage" in r.stderr


def test_zstd_window_of_a_single_segment_frame(mod):
    # Small content: zstd writes a single-segment frame, the window is the content size
    assert mod.zstd_window(zstd.compress(b"x" * 100)) == 100
    assert mod.zstd_window(zstd.compress(b"x" * 1000)) == 1000
    with pytest.raises(SystemExit, match="no zstd frame"):
        mod.zstd_window(b"\0\0\0\0\0\0")


def test_pack_checks_what_zstd_made(mod, monkeypatch):
    real = subprocess.run

    def zstd_fails(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, b"", b"out of memory")
    monkeypatch.setattr(mod.subprocess, "run", zstd_fails)
    with pytest.raises(SystemExit, match="zstd failed: out of memory"):
        mod.pack(b"raw")

    def other_bytes(cmd, **kw):
        return real(cmd, input=b"other", capture_output=True)
    monkeypatch.setattr(mod.subprocess, "run", other_bytes)
    with pytest.raises(SystemExit, match="does not unpack to the same bytes"):
        mod.pack(b"raw")

    def big_window(cmd, **kw):
        # A frame header with a 16 MB window (exponent 14 → 1 << 24)
        return subprocess.CompletedProcess(cmd, 0, b"\x28\xb5\x2f\xfd\x00" + bytes([14 << 3]), b"")
    monkeypatch.setattr(mod.subprocess, "run", big_window)
    with pytest.raises(SystemExit, match="larger than the kernel takes"):
        mod.pack(b"raw")


def test_crypt_ramdisk_packs_the_card_ramdisk_with_it():
    text = (TOOLS / "crypt-ramdisk.sh").read_text()
    call = text.index('tools/card-initrd.py" "$out/initrd.cpio.gz"')
    # After the check that the card's ramdisk is vendor_boot's and init_boot's, before the limit
    assert text.index("is not vendor_boot's and init_boot's") < call < text.index("22020096")
    assert "lz4 zstd readelf" in text


def test_main_in_process(mod, tmp_path, capsys):
    src, out = tmp_path / "in", tmp_path / "out"
    src.write_bytes(card_ramdisk())
    mod.main(["card-initrd.py", str(src), str(out)])
    assert zstd.decompress(out.read_bytes()).endswith(PLATFORM + INIT)
    assert "bytes unpacked" in capsys.readouterr().err
    with pytest.raises(SystemExit, match="usage"):
        mod.main(["card-initrd.py"])


@pytest.mark.parametrize("data, message", [
    (lz4_legacy(PLATFORM), "zstd frame"),
    (zstd.compress(PROGRAMS)[:-6], "cut off"),
    (zstd.compress(PROGRAMS) + b"PK\x03\x04", "no lz4 legacy"),
    (zstd.compress(PROGRAMS) + b"\x02\x21\x4c\x18" + b"\xff" * 40, "do not unpack"),
])
def test_unpack_in_process_refuses(mod, data, message):
    with pytest.raises(SystemExit, match=message):
        mod.unpack(data)
