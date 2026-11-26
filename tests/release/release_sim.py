# Shared building blocks for the tests in tests/release: synthetic Android boot images with AVB
# data, a stand-in for the boot menu card read through debugfs, pacman databases, and copies of
# scripts with their system paths pointed into a test directory.
#
# Not a test module itself (no test_ prefix); the tests import it by name.

import gzip
import hashlib
import io
import re
import struct
import tarfile
from pathlib import Path

import pytest


# --- copies of scripts with fixed paths -------------------------------------------------------

SYSTEM_PATH = re.compile(r"(?<![\w.$}/@-])/(dev|proc|sys|usr|var|run|etc|boot)(/|\b)")


def rewrite(text, mapping):
    """Replaces each key of mapping in text; every key must occur (so a changed script fails
    loudly instead of silently reaching the real path)."""
    for old, new in mapping.items():
        assert old in text, f"{old!r} not found: the script changed, update the test"
        text = text.replace(old, new)
    return text


def assert_no_system_paths(text, allowed=("/dev/zero", "/dev/null")):
    """No absolute system path is left outside comments, except the harmless ones."""
    for n, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        for a in allowed:
            line = line.replace(a, "")
        m = SYSTEM_PATH.search(line)
        assert not m, f"line {n} still has a system path: {line.strip()}"


def copy_script(src, dst, mapping, root=None, allowed=("/dev/zero", "/dev/null"), cut=None):
    """A copy of script src at dst with mapping applied. Mapping values may use @ROOT@ for the
    test's root directory (checked before it is filled in). cut: drop the text from this marker
    on (e.g. the command dispatch, to source only the functions)."""
    text = rewrite(Path(src).read_text(), mapping)
    if cut is not None:
        assert cut in text
        text = text[:text.index(cut)]
    assert_no_system_paths(text, allowed)
    if root is not None:
        text = text.replace("@ROOT@", str(root))
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(text)
    dst.chmod(0o755)
    return dst


# --- Android boot images ----------------------------------------------------------------------

def align(x, page=4096):
    return (x + page - 1) // page * page


def kernel_blob(release, gz=True):
    """A stand-in kernel Image that carries the version string the kernel prints."""
    k = b"\x00ARM64\x00" * 8 + f"Linux version {release} (misc@plinius) (clang) #1 SMP PREEMPT".encode() \
        + b"\x00" * 100
    return gzip.compress(k, mtime=0) if gz else k


def boot_img(release, gz=True):
    """Boot image header v4 with only a kernel, like mkbootimg in plinius-kernel's package()."""
    k = kernel_blob(release, gz)
    hdr = bytearray(4096)
    hdr[:8] = b"ANDROID!"
    struct.pack_into("<IIII", hdr, 8, len(k), 0, 0, 1584)  # kernel, ramdisk, os_version, header
    struct.pack_into("<I", hdr, 40, 4)  # header_version
    return bytes(hdr) + k + b"\0" * (align(len(k)) - len(k))


def vendor_boot(cmdline, ramdisk, dtb, page=4096):
    """vendor_boot with a v4 header, one vendor ramdisk and a dtb."""
    hdr = bytearray(page)
    hdr[:8] = b"VNDRBOOT"
    struct.pack_into("<III", hdr, 8, 4, page, 0x40080000)  # version, page size, kernel addr
    struct.pack_into("<II", hdr, 20, 0x51b00000, len(ramdisk))  # ramdisk addr, size
    c = cmdline.encode()
    hdr[28:28 + len(c)] = c
    struct.pack_into("<II", hdr, 2096, 2128, len(dtb))  # header size, dtb size
    return bytes(hdr) + ramdisk + b"\0" * (align(len(ramdisk), page) - len(ramdisk)) + dtb


def init_boot(ramdisk):
    """init_boot: a boot image v4 holding only the generic ramdisk."""
    hdr = bytearray(4096)
    hdr[:8] = b"ANDROID!"
    struct.pack_into("<II", hdr, 8, 0, len(ramdisk))
    return bytes(hdr) + ramdisk


# --- AVB: vbmeta with a hash descriptor, footer at the end of the partition --------------------

def hash_descriptor(image, salt=b"\x5a" * 32, name=b"boot"):
    digest = hashlib.sha256(salt + image).digest()
    body = bytearray(116)
    struct.pack_into(">Q", body, 0, len(image))
    body[8:8 + 6] = b"sha256"
    struct.pack_into(">III", body, 40, len(name), len(salt), len(digest))
    body += name + salt + digest
    body += b"\0" * (align(len(body), 8) - len(body))
    return struct.pack(">QQ", 2, len(body)) + bytes(body)


def property_descriptor(key=b"com.android.build.boot.os_version", value=b"14"):
    body = struct.pack(">QQ", len(key), len(value)) + key + b"\0" + value + b"\0"
    body += b"\0" * (align(len(body), 8) - len(body))
    return struct.pack(">QQ", 0, len(body)) + body


def vbmeta(image, salt=b"\x5a" * 32, auth_size=64, key_area=32):
    """vbmeta blob: header, an (unused) authentication block, an auxiliary block with some key
    bytes and then a property and a hash descriptor."""
    descriptors = property_descriptor() + hash_descriptor(image, salt)
    aux = b"K" * key_area + descriptors
    aux += b"\0" * (align(len(aux), 64) - len(aux))
    hdr = bytearray(256)
    hdr[:4] = b"AVB0"
    struct.pack_into(">II", hdr, 4, 1, 0)
    struct.pack_into(">QQ", hdr, 12, auth_size, len(aux))
    struct.pack_into(">QQ", hdr, 96, key_area, len(descriptors))
    return bytes(hdr) + b"\0" * auth_size + aux


def avb_partition(image, size, vbmeta_off, salt=b"\x5a" * 32, broken=False):
    """A boot partition holding image, its vbmeta at vbmeta_off and the AVB footer."""
    vb = bytearray(vbmeta(image, salt))
    if broken:
        vb[:4] = b"XXXX"
    part = bytearray(size)
    part[:len(image)] = image
    part[vbmeta_off:vbmeta_off + len(vb)] = vb
    footer = bytearray(64)
    footer[:4] = b"AVBf"
    struct.pack_into(">IIQQQ", footer, 4, 1, 0, len(image), vbmeta_off, len(vb))
    part[-64:] = footer
    return bytes(part)


def read_avb(part):
    """(original image size, vbmeta offset, vbmeta size, image size and digest in the hash
    descriptor, salt) of a partition written by write_boot."""
    footer = part[-64:]
    assert footer[:4] == b"AVBf"
    _, _, img_size, voff, vsize = struct.unpack_from(">IIQQQ", footer, 4)
    vb = part[voff:voff + vsize]
    assert vb[:4] == b"AVB0"
    auth, _ = struct.unpack_from(">QQ", vb, 12)
    doff, dsize = struct.unpack_from(">QQ", vb, 96)
    p, end = 256 + auth + doff, 256 + auth + doff + dsize
    while p < end:
        tag, following = struct.unpack_from(">QQ", vb, p)
        if tag == 2:
            size, = struct.unpack_from(">Q", vb, p + 16)
            name_len, salt_len, digest_len = struct.unpack_from(">III", vb, p + 56)
            q = p + 132 + name_len
            return dict(image_size=img_size, voff=voff, vsize=vsize, desc_size=size,
                        salt=bytes(vb[q:q + salt_len]),
                        digest=bytes(vb[q + salt_len:q + salt_len + digest_len]))
        p += 16 + following
    raise AssertionError("no hash descriptor")


# --- boot menu card, as debugfs sees it ---------------------------------------------------------

# debugfs stand-in. The "device" is a file holding the path of a directory that plays the card.
# Like the real debugfs 1.47 (checked in test_release_kernel_install.py against a real ext4
# image): exit status 0 even for a missing file or device, errors on stderr, "Inode:" on stdout
# for an existing path. "ls -p" lists in directory order; ext4 puts entries of a freshly filled
# directory in the order they were created, which here is the order of the inode numbers.
DEBUGFS = r'''
dev=${@: -1}
cmd=
while (($#)); do
	case $1 in
	-R) cmd=$2; shift 2 ;;
	*) shift ;;
	esac
done
echo "debugfs 1.47.4 (6-Mar-2025)" >&2
root=$(cat "$dev" 2>/dev/null) || { echo "debugfs: No such file or directory while trying to open $dev" >&2; exit 0; }
read -r verb opt arg <<< "$cmd"
case $verb in
stat)
	if [[ -e $root$opt ]]; then
		echo "Inode: $(stat -c %i "$root$opt")   Type: regular    Mode:  0644"
	else
		echo "$opt: File not found by ext2_lookup " >&2
	fi ;;
cat)
	if [[ -f $root$opt ]]; then cat "$root$opt"; else echo "$opt: File not found by ext2_lookup " >&2; fi ;;
ls)
	[[ $opt == -p ]] || exit 0
	[[ -d $root$arg ]] || { echo "$arg: File not found by ext2_lookup " >&2; exit 0; }
	echo "/2/040755/0/0/.//"
	echo "/2/040755/0/0/..//"
	find "$root$arg" -mindepth 1 -maxdepth 1 -printf '%i %f\n' | sort -n |
		while read -r ino name; do echo "/$ino/100644/0/0/$name/0/"; done ;;
esac
exit 0
'''


def inode_order_works(tmp_path):
    """True when new files get rising inode numbers here (what the debugfs stand-in relies on)."""
    d = tmp_path / ".inode-probe"
    d.mkdir()
    inodes = []
    for n in ("c", "a", "b"):
        (d / n).write_text("")
        inodes.append((d / n).stat().st_ino)
    return inodes == sorted(inodes)


def need_inode_order(tmp_path):
    if not inode_order_works(tmp_path):
        pytest.skip("this file system does not hand out rising inode numbers")


def card_list(card_root):
    """Entry names (*.conf) of the card in the order the boot menu numbers them."""
    d = Path(card_root) / "db" / "entries"
    return [p.name for p in sorted(d.iterdir(), key=lambda p: p.stat().st_ino)
            if p.name.endswith(".conf")]


def last_index(card_root):
    raw = (Path(card_root) / "db" / "last_index").read_bytes()
    assert len(raw) == 128, "last_index must stay 128 bytes"
    return int(raw.rstrip(b"\0").decode())


def write_last_index(card_root, i):
    s = str(i).encode()
    (Path(card_root) / "db" / "last_index").write_bytes(s + b"\0" * (128 - len(s)))


# --- pacman databases -------------------------------------------------------------------------

def desc_text(fields):
    out = []
    for key, values in fields.items():
        if isinstance(values, str):
            values = [values]
        out.append(f"%{key}%")
        out.extend(values)
        out.append("")
    return "\n".join(out) + "\n"


def write_db(path, entries, member="desc"):
    """A pacman database (gzip tar): entries = {"name-ver-rel": {FIELD: value(s)}}."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as t:
        for entry, fields in entries.items():
            d = tarfile.TarInfo(entry)
            d.type = tarfile.DIRTYPE
            d.mode = 0o755
            t.addfile(d)
            data = desc_text(fields).encode()
            info = tarfile.TarInfo(f"{entry}/{member}")
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return path


def write_tar(path, members):
    """A tar (package stand-in): members = {name: bytes or str}; names ending in / are dirs."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as t:
        for name, data in members.items():
            if name.endswith("/"):
                d = tarfile.TarInfo(name.rstrip("/"))
                d.type = tarfile.DIRTYPE
                t.addfile(d)
                continue
            if isinstance(data, str):
                data = data.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            t.addfile(info, io.BytesIO(data))
    return path


# --- plinius-bootcheck as a copy below a test root ----------------------------------------------

BOOTCHECK_MAPPING = {
    "LISTS=/usr/lib/plinius-bootcheck.d": "LISTS=@ROOT@/usr/lib/plinius-bootcheck.d",
    "STATE=/var/lib/plinius-bootcheck": "STATE=@ROOT@/var/lib/plinius-bootcheck",
    "RUN=/run/plinius-bootcheck": "RUN=@ROOT@/run/plinius-bootcheck",
    "/proc/bootconfig": "@ROOT@/proc/bootconfig",
    "/proc/cmdline": "@ROOT@/proc/cmdline",
    "[[ -n $card && -b $card ]]": "[[ -n $card && -e $card ]]",
}


# A pattern for words of the kernel command line, not a path that is opened
CMDLINE_PATTERNS = ("=/dev/mmcblk*)",)


def bootcheck_copy(src, dst, root):
    return copy_script(src, dst, BOOTCHECK_MAPPING, root, allowed=("/dev/zero", "/dev/null") + CMDLINE_PATTERNS)
