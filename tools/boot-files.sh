#!/bin/bash
# Makes what a system needs to start out of the boot images of a UT build:
# - for a boot entry on the memory card, as the installer of the multi-boot manager does it: the kernel
#   of boot.img as zImage, and initrd.cpio.gz from all vendor ramdisks followed by the ramdisk of
#   init_boot.img (docs/BOOTKETTE.md)
# - for the phone's own storage: the three images themselves. In vendor_boot.img the kernel command
#   line loses systempart=, which would take precedence over the system in rootfs.img on the data
#   partition (docs/PLAN-IMAGE.md). Only those bytes of the header change.
#
# Usage: tools/boot-files.sh <dir with boot.img, vendor_boot.img, init_boot.img> <output dir>
#
# The three images must be those of the UT build pinned in tools/ut-build.sha256 (whole, headers
# with the kernel command line, DTB and bootconfig included), checked before anything is unpacked.
# The result must match tools/boot-files.sha256, measured on the device. This is no mere check of
# the download: plinius-kmod is built against the symbol CRCs of exactly this kernel. A new kernel
# needs a new plinius-kmod first, and only then new checksums in both files.
set -euo pipefail
. "$(dirname "$0")/lib.sh"

src=${1:?usage: tools/boot-files.sh <dir with boot images> <output dir>}
out=${2:?usage: tools/boot-files.sh <dir with boot images> <output dir>}
command -v unpack_bootimg >/dev/null || die "unpack_bootimg missing (package android-tools)"
for img in boot vendor_boot init_boot; do
	[[ -f $src/$img.img ]] || die "$src/$img.img missing"
	want=$(awk -v n="$img.img" '$1 !~ /^#/ && $2 == n { print $1 }' "$VOLLA_DIR/tools/ut-build.sha256")
	got=$(sha256sum < "$src/$img.img")
	[[ $want =~ ^[0-9a-f]{64}$ && ${got%% *} == "$want" ]] ||
		die "$src/$img.img is not the one of the UT build pinned in tools/ut-build.sha256 (SHA-256 differs)"
done

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
for img in boot vendor_boot init_boot; do
	unpack_bootimg --boot_img "$src/$img.img" --out "$tmp/$img" >/dev/null
done

mkdir -p "$out"
install -m644 "$tmp/boot/kernel" "$out/zImage"
# The glob sorts vendor_ramdisk00 before 01, and leaves out the directory vendor-ramdisk-by-name
cat "$tmp"/vendor_boot/vendor_ramdisk* "$tmp/init_boot/ramdisk" > "$out/initrd.cpio.gz"
chmod 644 "$out/initrd.cpio.gz"

(cd "$out" && sha256sum --quiet -c "$VOLLA_DIR/tools/boot-files.sha256") ||
	die "boot files differ from tools/boot-files.sha256, this kernel does not fit plinius-kmod"

install -m644 "$src/boot.img" "$src/init_boot.img" "$out/"
python3 -I - "$src/vendor_boot.img" "$out/vendor_boot.img" <<-'EOF'
	import re, struct, sys
	image = bytearray(open(sys.argv[1], "rb").read())
	# Header v4: magic, version, page size, kernel and ramdisk address, ramdisk size, then the command line
	if image[:8] != b"VNDRBOOT" or struct.unpack_from("<I", image, 8)[0] != 4:
	    sys.exit("not a vendor boot image of version 4")
	at, size = 28, 2048
	line = bytes(image[at:at + size]).rstrip(b"\0").decode()
	new = " ".join(w for w in line.split() if not w.startswith("systempart="))
	if new == line:
	    sys.exit(f"no systempart= in the command line: {line}")
	image[at:at + size] = new.encode().ljust(size, b"\0")
	open(sys.argv[2], "wb").write(image)
	EOF
chmod 644 "$out/vendor_boot.img"
msg "Boot files in $out"
