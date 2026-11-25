#!/usr/bin/env python3
"""Packs the ramdisk of a boot entry on the memory card as one zstd archive (tools/crypt-ramdisk.sh).

The card's ramdisk is vendor_boot's and init_boot's in a row: the programs of the password prompt
as a zstd frame, then lz4 legacy archives (UT's platform and init ramdisks, ours behind them). lk
copies the whole file to a room that ended at some 22.0 MB on a card start (25.11.2026: 21,981,472
bytes ended in lk's "data fault", the same content as one zstd archive, 16.4 MB, started). The
kernel unpacks a zstd ramdisk (CONFIG_RD_ZSTD) with windows of up to 8 MB.

halium-rebuild puts the look behind this file as an lz4 archive: the kernel's zstd decoder stops
at the end of the frame and goes on with the next archive.

Usage: tools/card-initrd.py IN OUT
"""
import subprocess
import sys
from compression import zstd

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
LZ4_LEGACY_MAGIC = b"\x02\x21\x4c\x18"
# lib/decompress_unzstd.c, ZSTD_WINDOWSIZE_MAX
WINDOW_MAX = 8 << 20


def fail(text):
    sys.exit(f"card-initrd: {text}")


def zstd_window(data):
    """The window size of the zstd frame at the start of DATA (RFC 8878, 3.1.1.1), as boot-image
    of plinius-crypt reads it."""
    if data[:4] != ZSTD_MAGIC:
        fail("no zstd frame")
    descriptor = data[4]
    if descriptor & 0x20:
        size_bytes = {0: 1, 1: 2, 2: 4, 3: 8}[descriptor >> 6]
        at = 5 + {0: 0, 1: 1, 2: 2, 3: 4}[descriptor & 3]
        size = int.from_bytes(data[at:at + size_bytes], "little")
        return size + 256 if size_bytes == 2 else size
    wd = data[5]
    base = 1 << (10 + (wd >> 3))
    return base + base // 8 * (wd & 7)


def unpack(data):
    """The cpio archives of a card ramdisk, decompressed, in the order the kernel reads them."""
    if data[:4] != ZSTD_MAGIC:
        fail("the ramdisk does not start with the programs' zstd frame")
    d = zstd.ZstdDecompressor()
    programs = d.decompress(data)
    rest = d.unused_data
    if not d.eof:
        fail("the programs' zstd frame is cut off")
    # The kernel skips zeros between archives (the next one starts 4-byte aligned)
    rest = rest.lstrip(b"\0")
    if rest[:4] != LZ4_LEGACY_MAGIC:
        fail("no lz4 legacy archives behind the programs")
    r = subprocess.run(["lz4", "-q", "-dc"], input=rest, capture_output=True)
    if r.returncode != 0:
        fail("the lz4 archives do not unpack: " + r.stderr.decode(errors="replace").strip())
    # Each archive begins 4-byte aligned in the stream; zeros between archives are skipped
    programs += b"\0" * (-len(programs) % 4)
    return programs + r.stdout


def pack(raw):
    r = subprocess.run(["zstd", "-q", "-19", "--zstd=wlog=23", "-c"], input=raw, capture_output=True)
    if r.returncode != 0:
        fail("zstd failed: " + r.stderr.decode(errors="replace").strip())
    packed = r.stdout
    if zstd_window(packed) > WINDOW_MAX:
        fail("the zstd window is larger than the kernel takes (8 MB)")
    if zstd.decompress(packed) != raw:
        fail("the zstd archive does not unpack to the same bytes")
    return packed


def main(argv):
    if len(argv) != 3:
        fail("usage: card-initrd.py IN OUT")
    raw = unpack(open(argv[1], "rb").read())
    packed = pack(raw)
    with open(argv[2], "wb") as f:
        f.write(packed)
    print(f"card ramdisk: {len(raw)} bytes unpacked, {len(packed)} bytes as one zstd archive",
          file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv)
