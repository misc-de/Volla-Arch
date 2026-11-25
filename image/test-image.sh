#!/bin/bash
# Boot test of an image from image/mkimage.sh: boots the image itself in QEMU (aarch64, emulated)
# up to graphical.target and judges what it finds. The phone's Halium kernel does not run in
# QEMU, so the generic kernel of ALARM (linux-aarch64 from build/cache/pacman) boots it; its
# modules come from a second disk mounted at /usr/lib/modules/<its version>. What needs the
# phone (Android container, GPU, charger, sensors) fails there: image/test-image.expected names
# those units, every other failed or restarting unit fails the test, so do a broken unit file, a
# missing dependency (pacman -Dk), a dead system bus or one of the units that must run.
#
# The image is never written (QEMU snapshot=on). The check (image/test-image-guest.sh) reaches the
# guest as a raw third disk, started by a unit handed over as a systemd credential on the kernel
# command line; the boot itself is the image's own. The journal keeps info lines there too
# (systemd.journald.max_level_store=info; the image stores only notice and above), so that what a
# failing unit wrote to stderr reaches the report. About 6 to 10 minutes.
#
# Usage: image/test-image.sh IMAGE.img [OUTPUT-DIR]   (default: build/image-test/<image name>)
# IMAGE_TEST_KERNEL=pkg takes another linux-aarch64 package, IMAGE_TEST_TIMEOUT (seconds,
# default 2400) ends a boot that hangs. Result: OUTPUT-DIR/report.txt and serial.log.
set -euo pipefail
. "$(dirname "$0")/../tools/lib.sh"

img=${1:?usage: image/test-image.sh IMAGE.img [OUTPUT-DIR]}
[[ -f $img ]] || die "$img missing"
img=$(realpath "$img")
out=${2:-$BUILD/image-test/$(basename "$img" .img)}
timeout=${IMAGE_TEST_TIMEOUT:-2400}
for tool in qemu-system-aarch64 bsdtar depmod mke2fs debugfs; do
	command -v "$tool" > /dev/null || die "$tool missing on the host"
done
debugfs -R "stats" "$img" 2>/dev/null | grep -q "^Filesystem volume name:" ||
	die "$img is not an ext4 image (a .zst has to be unpacked first)"

pkg=${IMAGE_TEST_KERNEL:-}
if [[ -z $pkg ]]; then
	pkg=$(find "$BUILD/cache/pacman" -maxdepth 1 -name 'linux-aarch64-[0-9]*.pkg.tar.*' ! -name '*.sig' 2>/dev/null |
		sort -V | tail -n 1)
	[[ -n $pkg ]] || die "no linux-aarch64 package in $BUILD/cache/pacman (image/mkpreview.sh caches it), or set IMAGE_TEST_KERNEL"
fi
[[ -f $pkg ]] || die "$pkg missing"

# Kernel and module disk, made once per kernel package
kdir=$BUILD/image-test/kernel/$(basename "$pkg")
if [[ ! -f $kdir/modules.img ]]; then
	msg "Unpacking $(basename "$pkg") for the boot test"
	mkdir -p "$(dirname "$kdir")"
	tmp=$(mktemp -d "$kdir.XXXXXX")
	trap 'command rm -rf "$tmp"' EXIT
	bsdtar -xf "$pkg" -C "$tmp" boot/Image usr/lib/modules || die "$pkg has no boot/Image or modules"
	kver=$(cd "$tmp/usr/lib/modules" && ls -d */ | grep -v '^extramodules' | head -n 1)
	kver=${kver%/}
	ln -s usr/lib "$tmp/lib"  # depmod -b looks in lib/modules
	depmod -b "$tmp" "$kver"
	size=$(($(du -sm "$tmp/usr/lib/modules/$kver" | cut -f1) * 2 + 64))
	mke2fs -q -t ext4 -L kmods -d "$tmp/usr/lib/modules/$kver" "$tmp/modules.img" "${size}M"
	mkdir "$tmp/k"
	mv "$tmp/boot/Image" "$tmp/modules.img" "$tmp/k/"
	echo "$kver" > "$tmp/k/version"
	command rm -rf "$kdir"
	mv "$tmp/k" "$kdir"
	command rm -rf "$tmp"
	trap - EXIT
fi
kver=$(cat "$kdir/version")

mkdir -p "$out"
# The check with the units that must be active in front of it, padded to whole sectors
active=$(awk -F'#' '{ split($1, w, " ") } w[1] == "active" && w[2] != "" { printf "%s ", w[2] }' \
	"$VOLLA_DIR/image/test-image.expected")
{
	echo "ACTIVE=\"$active\""
	cat "$VOLLA_DIR/image/test-image-guest.sh"
} > "$out/guest.img"
truncate -s %512 "$out/guest.img"
unit=$(printf '%s\n' '[Unit]' 'Description=Boot test of the image (image/test-image.sh)' \
	'After=multi-user.target graphical.target' '[Service]' 'Type=exec' \
	'ExecStart=/usr/bin/bash /dev/vdb' | base64 -w0)

accel=(-cpu max,pauth-impdef=on)
[[ $(uname -m) == aarch64 && -w /dev/kvm ]] && accel=(-accel kvm -cpu host)
: > "$out/serial.log"
msg "Booting $(basename "$img") with kernel $kver (log: $out/serial.log)"
status=0
# TMPDIR: QEMU keeps the snapshot's changes there, not in /tmp
TMPDIR=$out timeout "$timeout" qemu-system-aarch64 -machine virt "${accel[@]}" -smp 8 -m 4G \
	-display none -monitor none -serial "file:$out/serial.log" -no-reboot -nic none \
	-kernel "$kdir/Image" \
	-append "root=/dev/vda rw console=ttyAMA0 systemd.journald.max_level_store=info systemd.set_credential_binary=systemd.extra-unit.image-check.service:$unit systemd.wants=image-check.service systemd.mount-extra=LABEL=kmods:/usr/lib/modules/$kver:ext4:ro" \
	-drive "file=$img,format=raw,if=virtio,snapshot=on" \
	-drive "file=$out/guest.img,format=raw,if=virtio,readonly=on" \
	-drive "file=$kdir/modules.img,format=raw,if=virtio,snapshot=on" || status=$?
((status != 124)) || msg "the boot did not end within $timeout s"
python3 "$VOLLA_DIR/tools/image-boot-report.py" "$out/serial.log" "$VOLLA_DIR/image/test-image.expected" \
	--report "$out/report.txt" || die "$(basename "$img") failed the boot test, see $out/report.txt"
msg "Report: $out/report.txt"
