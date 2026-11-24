#!/bin/bash
# Smoke test of the preview after unlocking: boots (image unchanged thanks to -snapshot),
# unlocks Phosh via keyboard with the preview PIN and takes a screenshot of the overview
# (app grid). Result: <output>/pin.ppm, unlocked.ppm, overview.ppm
#
# Usage: ARCH=x86_64 image/test-unlock.sh [output-dir]   (default: build/preview-unlock-test)
# PREVIEW_APP=uhren additionally starts an app via the search (lowercase, no y/z; the preview
# UI is German, "uhren" = Clocks) and takes a screenshot of it (app.ppm).
# PREVIEW_DIR=dir uses a different preview, PREVIEW_DEVICES="-device ..." adds QEMU devices.
# Waits for "Phosh ready" (printed by Phosh) in the serial log.
set -euo pipefail
. "$(dirname "$0")/../tools/lib.sh"

p=$BUILD/preview
[[ $ARCH == aarch64 ]] || p=$BUILD/preview-$ARCH
p=${PREVIEW_DIR:-$p}
[[ -f $p/preview.img ]] || die "no preview in $p, run ARCH=$ARCH image/mkpreview.sh first"
out=${1:-$BUILD/preview-unlock-test}
pin=1234
port=45457

case $ARCH in
	aarch64) qemu=(qemu-system-aarch64 -machine virt -cpu max,pauth-impdef=on) console=ttyAMA0 boot=600 ;;
	x86_64) qemu=(qemu-system-x86_64 -machine q35,i8042=off,vmport=off -accel kvm -cpu host -vga none) console=ttyS0 boot=90 ;;
esac

mkdir -p "$out"
: > "$out/serial.log"

"${qemu[@]}" -smp 8 -m 4G \
	-kernel "$p/kernel" -initrd "$p/initramfs-preview.img" \
	-append "root=/dev/vda rw console=$console systemd.journald.forward_to_console=1" \
	-drive file="$p/preview.img",format=raw,if=virtio,file.locking=off -snapshot \
	-device virtio-gpu-pci,xres=720,yres=1440,edid=off -display none \
	-device virtio-keyboard-pci -device virtio-multitouch-pci ${PREVIEW_DEVICES:-} \
	-netdev user,id=n0 -device virtio-net-pci,netdev=n0 \
	-serial file:"$out/serial.log" \
	-monitor tcp:127.0.0.1:$port,server,nowait &
qpid=$!
trap 'kill $qpid 2>/dev/null || true' EXIT

# monitor CMD: sends one command to the QEMU monitor
monitor() {
	exec 3<>/dev/tcp/127.0.0.1/$port
	echo "$1" >&3
	sleep 1
	exec 3>&-
}

t=0
until grep -a -q 'Phosh ready' "$out/serial.log"; do
	kill -0 $qpid 2>/dev/null || die "QEMU exited, see $out/serial.log"
	((t++ < boot)) || die "Phosh not ready after ${boot}s, see $out/serial.log"
	sleep 1
done
sleep 8

# A key press wakes the PIN entry of the lock screen, then PIN and Enter
monitor "sendkey spc"
sleep 5   # wait for the PIN page fade-in animation, otherwise digits get lost
monitor "sendkey backspace"   # otherwise the wake-up space ends up in the PIN
for ((i = 0; i < ${#pin}; i++)); do
	monitor "sendkey ${pin:i:1}"
done
monitor "screendump $out/pin.ppm"
monitor "sendkey ret"
sleep 8

monitor "screendump $out/unlocked.ppm"
# On first login the Phosh Tour opens; close it, then Phosh shows the overview
monitor "sendkey ctrl-q"
sleep 4
monitor "screendump $out/overview.ppm"

if [[ -n ${PREVIEW_APP:-} ]]; then
	for ((i = 0; i < ${#PREVIEW_APP}; i++)); do
		monitor "sendkey ${PREVIEW_APP:i:1}"
	done
	sleep 2
	monitor "sendkey ret"
	sleep 8
	monitor "screendump $out/app.ppm"
fi
monitor quit
wait $qpid || true
trap - EXIT
msg "Screenshot after unlocking: $out/unlocked.ppm"
