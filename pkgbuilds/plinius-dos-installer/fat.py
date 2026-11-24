"""Read the files of a FAT12/FAT16 floppy image, without mounting it.

DOS floppies only use 8.3 names; long file names (VFAT) are skipped. Only the classic BIOS
parameter block is needed, so disks formatted before DOS 4 (no extended BPB) work as well.
"""

import struct
from dataclasses import dataclass, field


class FatError(Exception):
    pass


@dataclass
class FatImage:
    path: str
    label: str = ""
    # Path (upper case, "/"-separated) -> content
    files: dict = field(default_factory=dict)
    dirs: list = field(default_factory=list)


ATTR_VOLUME = 0x08
ATTR_DIR = 0x10
ATTR_LFN = 0x0F


def read_image(path):
    with open(path, "rb") as f:
        data = f.read()
    if len(data) < 512:
        raise FatError(f"{path}: too small for a floppy image")
    (bytes_per_sector, sectors_per_cluster, reserved, fats, root_entries, total16, _media,
     sectors_per_fat) = struct.unpack_from("<HBHBHHBH", data, 11)
    if bytes_per_sector not in (128, 256, 512, 1024, 2048, 4096) or not sectors_per_cluster \
            or not fats or not sectors_per_fat or not root_entries:
        raise FatError(f"{path}: no FAT file system")
    total = total16 or struct.unpack_from("<I", data, 32)[0]
    fat_start = reserved * bytes_per_sector
    root_start = fat_start + fats * sectors_per_fat * bytes_per_sector
    root_size = root_entries * 32
    data_start = root_start + ((root_size + bytes_per_sector - 1) // bytes_per_sector) * bytes_per_sector
    cluster_size = sectors_per_cluster * bytes_per_sector
    clusters = (total * bytes_per_sector - data_start) // cluster_size
    fat12 = clusters < 4085
    fat = data[fat_start:fat_start + sectors_per_fat * bytes_per_sector]
    end_mark = 0xFF8 if fat12 else 0xFFF8

    def next_cluster(c):
        if fat12:
            off = c + c // 2
            v = struct.unpack_from("<H", fat, off)[0]
            return v >> 4 if c & 1 else v & 0xFFF
        return struct.unpack_from("<H", fat, c * 2)[0]

    def chain(first):
        c, seen = first, set()
        while 2 <= c < end_mark:
            if c in seen or c - 2 >= clusters:
                raise FatError(f"{path}: broken cluster chain")
            seen.add(c)
            yield c
            c = next_cluster(c)

    def read_chain(first, size=None):
        out = bytearray()
        for c in chain(first):
            off = data_start + (c - 2) * cluster_size
            out += data[off:off + cluster_size]
        return bytes(out if size is None else out[:size])

    image = FatImage(path)

    def walk(raw, prefix, depth):
        if depth > 16:
            raise FatError(f"{path}: directories nested too deep")
        for off in range(0, len(raw) - 31, 32):
            entry = raw[off:off + 32]
            if entry[0] == 0:
                break
            if entry[0] == 0xE5 or entry[11] == ATTR_LFN:
                continue
            attr = entry[11]
            name = entry[0:8].rstrip(b" ")
            ext = entry[8:11].rstrip(b" ")
            if name[:1] == b"\x05":
                name = b"\xe5" + name[1:]
            if attr & ATTR_VOLUME:
                if not prefix:
                    image.label = (name + ext).decode("cp437").strip()
                continue
            text = name.decode("cp437") + ("." + ext.decode("cp437") if ext else "")
            if text in (".", ".."):
                continue
            # DOS never writes these into a name; a crafted image would reach out of the
            # folder it is unpacked into ("../X", "/etc")
            if any(c in text for c in "/\\\0"):
                raise FatError(f"{path}: not a DOS file name: {text!r}")
            text = text.upper()
            first = struct.unpack_from("<H", entry, 26)[0]
            size = struct.unpack_from("<I", entry, 28)[0]
            full = prefix + text
            if attr & ATTR_DIR:
                image.dirs.append(full)
                walk(read_chain(first), full + "/", depth + 1)
            else:
                image.files[full] = read_chain(first, size) if size else b""

    walk(data[root_start:root_start + root_size], "", 0)
    return image
