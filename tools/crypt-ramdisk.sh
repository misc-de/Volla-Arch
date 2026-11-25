#!/bin/bash
# Adds full-disk encryption to the boot files of the phone (docs/PLAN-VERSCHLUESSELUNG.md).
#
# The bootloader loads vendor_boot's ramdisks and then init_boot's. vendor_boot has a second,
# "recovery" ramdisk with nothing but ramdisk-recovery.img, of no use here. So:
#  - vendor_boot: the programs (/plinius) with their libraries go in the same table entry as the
#    platform ramdisk, in front of it as zstd (boot-image says why); the recovery one is dropped.
#    As a ramdisk of its own in place of the recovery one they were missing on 08.11.
#    (cryptsetup: not found), although the bootloader
#    handed the kernel all of it (initrd size in the device tree), also after the entry's type
#    was set to platform: the card's single file with the same bytes in a row works.
#  - init_boot: the original ramdisk stays byte for byte; small archives behind it replace
#    scripts/halium by a copy with the call of the unlock script, and add that script.
#    On a plain data partition without a request the result starts like the original.
# The parts are those of the package plinius-crypt-ramdisk (programs.zst, init.lz4), put together
# by boot-image of plinius-crypt the same way the phone does it after an update (halium-rebuild).
# Without that package in PKGS they are made here from the packages (its make-parts).
#
# Usage: tools/crypt-ramdisk.sh <dir from tools/boot-files.sh> <output dir>
#   PKGS   directory with our aarch64 packages (default: the local repo)
#   CACHE  pacman cache with the Arch packages (default: build/cache/pacman), without the package
#   UNLOCK_SCRIPT  a file to run instead of plinius-crypt's halium-unlock, for tests on the device
#                  (always makes the parts here)
#   VENDOR_IMG  vendor.img of the UT build, for the firmware of the vibration motor (default: the
#               newest build/ut/*/android/vendor.img; needs dump.erofs from erofs-utils)
#   HAPTIC_FW  that firmware file itself instead (haptic_ram.bin), e.g. copied from the phone
#
# Output: vendor_boot.img, init_boot.img for the phone's own storage, initrd.cpio.gz for a boot entry
# on the memory card, parts/ with the parts used (the image keeps them as the confirmed ones).
# Nothing is written to a device.
set -euo pipefail
. "$(dirname "$0")/lib.sh"

src=${1:?usage: tools/crypt-ramdisk.sh <boot files dir> <output dir>}
out=${2:?usage: tools/crypt-ramdisk.sh <boot files dir> <output dir>}
PKGS=${PKGS:-$VOLLA_DIR/build/repo/plinius/aarch64}
CACHE=${CACHE:-$VOLLA_DIR/build/cache/pacman}
crypt_src=$VOLLA_DIR/pkgbuilds/plinius-crypt

for f in vendor_boot.img init_boot.img; do
	[[ -f $src/$f ]] || die "$src/$f missing"
done
for cmd in bsdtar cpio lz4 zstd readelf python3; do
	command -v "$cmd" >/dev/null || die "$cmd missing"
done
python3 -I -c 'import PIL' 2>/dev/null || die "Pillow (python-pillow) missing"
if [[ -z ${HAPTIC_FW:-} ]]; then
	VENDOR_IMG=${VENDOR_IMG:-$(ls -1 "$VOLLA_DIR"/build/ut/*/android/vendor.img 2>/dev/null | sort | tail -n 1)}
	[[ -f $VENDOR_IMG ]] || die "no vendor.img (VENDOR_IMG) for the firmware of the vibration motor"
	command -v dump.erofs >/dev/null || die "dump.erofs missing (package erofs-utils), or set HAPTIC_FW"
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
boot_image=$crypt_src/boot-image
mkdir -p "$out/parts"

# newest_pkg DIR NAME: the newest package NAME in DIR (empty if none)
newest_pkg() {
	find "$1" -maxdepth 1 \( -name "$2-[0-9]*-aarch64.pkg.tar.*" -o -name "$2-[0-9]*-any.pkg.tar.*" \) \
		! -name '*.sig' | sort -V | tail -n 1
}

ramdisk_pkg=$(newest_pkg "$PKGS" plinius-crypt-ramdisk)
if [[ -n $ramdisk_pkg && -z ${UNLOCK_SCRIPT:-} ]]; then
	msg "Parts from ${ramdisk_pkg##*/}"
	bsdtar -xf "$ramdisk_pkg" -C "$tmp" usr/lib/plinius-crypt/ramdisk
	cp "$tmp"/usr/lib/plinius-crypt/ramdisk/* "$out/parts/"
else
	msg "Making the parts from the packages"
	sysroot=$tmp/sysroot
	mkdir -p "$sysroot"
	# extract_pkg DIR NAME: unpacks the newest package NAME from DIR into the sysroot
	extract_pkg() {
		local f
		f=$(newest_pkg "$1" "$2")
		[[ -n $f ]] || die "no package $2 in $1"
		bsdtar -xf "$f" -C "$sysroot" --exclude '.*'
	}
	for p in buffybox plinius-progress plinius-crypt plinius-security device-volla-ansuz; do
		extract_pkg "$PKGS" "$p"
	done
	# Whatever make-parts reports as missing belongs in this list (and in the makedepends of
	# plinius-crypt-ramdisk)
	for p in cryptsetup e2fsprogs glibc libgcc openssl device-mapper json-c popt util-linux util-linux-libs zlib \
		brotli zstd libinih libinput systemd-libs libxkbcommon libdrm libevdev mtdev libcap xz lz4 \
		libgudev glib2 pcre2 libffi libwacom lua54 xkeyboard-config; do
		extract_pkg "$CACHE" "$p"
	done
	UNLOCK_SCRIPT=${UNLOCK_SCRIPT:-$crypt_src/halium-unlock} \
		"$PKGBUILDS/plinius-crypt-ramdisk/make-parts" "$sysroot" "$out/parts"
fi

# What comes from the phone where it puts the ramdisk together itself: the firmware of the vibration
# motor and the call of the unlock script in UT's scripts/halium
# pack DIR FILE: as halium-rebuild packs its archives
pack() {
	"$boot_image" cpio "$1" "$tmp/pack.cpio"
	lz4 -q -12 -l -c "$tmp/pack.cpio" > "$2"
}
# The vibration motor (Awinic aw86927) plays nothing without its waveform RAM: "ram init failed,
# not allow to play!" on every key press. The driver loads haptic_ram.bin from
# firmware_class.path=/vendor/firmware, which the ramdisk does not have; /lib/firmware is among
# the paths the kernel tries after it.
mkdir -p "$tmp/fw/lib/firmware"
if [[ -n ${HAPTIC_FW:-} ]]; then
	install -m644 "$HAPTIC_FW" "$tmp/fw/lib/firmware/haptic_ram.bin"
else
	dump.erofs --cat --path=/firmware/haptic_ram.bin "$VENDOR_IMG" > "$tmp/fw/lib/firmware/haptic_ram.bin"
fi
[[ -s $tmp/fw/lib/firmware/haptic_ram.bin ]] || die "haptic_ram.bin is empty"
pack "$tmp/fw" "$tmp/fw.lz4"
"$boot_image" halium-script "$src/init_boot.img" "$tmp/halium"
pack "$tmp/halium" "$tmp/halium.lz4"

msg "Putting the boot images together"
vsize=$("$boot_image" build-vendor "$src/vendor_boot.img" "$out/vendor_boot.img" "$out/parts/programs.zst" "$tmp/fw.lz4")
isize=$("$boot_image" build-init "$src/init_boot.img" "$out/init_boot.img" "$tmp/halium.lz4" "$out/parts/init.lz4")
# For a boot entry on the card, in the order the bootloader uses
"$boot_image" ramdisk "$out/vendor_boot.img" "$out/initrd.cpio.gz"
"$boot_image" init-ramdisk "$out/init_boot.img" "$tmp/init-ramdisk"
cat "$tmp/init-ramdisk" >> "$out/initrd.cpio.gz"
total=$(stat -c %s "$out/initrd.cpio.gz")
printf 'programs %d bytes, init_boot ramdisk %d bytes, whole ramdisk %d bytes\n' \
	"$(stat -c %s "$out/parts/programs.zst")" "$isize" "$total" >&2
((total == vsize + isize)) || die "the card's ramdisk ($total) is not vendor_boot's and init_boot's ($vsize + $isize)"
# The card's ramdisk as one zstd archive, some 5 MB smaller (card-initrd.py says why)
python3 -I "$VOLLA_DIR/tools/card-initrd.py" "$out/initrd.cpio.gz" "$tmp/card-initrd"
mv "$tmp/card-initrd" "$out/initrd.cpio.gz"
total=$(stat -c %s "$out/initrd.cpio.gz")
# The largest ramdisk lk loads with plinius-kernel's 6.1.192 (09.11.2026: 22,090,524 bytes failed
# with "data fault ... FAR 0x68400000", 22,018,783 started); UT's 6.1.145 took more (25,397,680 on
# 07.11.). lk puts the bootconfig (some 640 bytes) behind it in the same room, so 1 kB stays free;
# halium-rebuild on the phone keeps to the same limit (MAX_RAMDISK, BOOTCONFIG_ROOM).
((total + 1024 <= 22020096)) || die "the whole ramdisk ($total bytes) leaves less than 1024 of 22,020,096 bytes for the bootconfig"
chmod 644 "$out"/*.img "$out"/initrd.cpio.gz "$out"/parts/*
msg "Files in $out"
