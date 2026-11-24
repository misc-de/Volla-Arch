#!/bin/bash
# Automated smoke test of the QEMU preview: boots headless (image unchanged thanks to
# -snapshot), records the serial log and takes screenshots via the monitor.
#
# Usage: [ARCH=x86_64] image/test-preview.sh [output-dir] [seconds-until-screenshot ...]
# Default: build/preview-test, screenshots after 120, 240 and 360 seconds.
# PREVIEW_APPEND appends kernel parameters, e.g. systemd.journald.forward_to_console=1
# PREVIEW_DIR=dir uses a different preview (e.g. a debug build from mkpreview.sh -o … -d)
# PREVIEW_GL=on tests with virgl (like run-preview.sh) via -display egl-headless
# PREVIEW_IMG=copy.img boots a copy writable (without -snapshot), so that the journal can be
# read afterwards: debugfs -R "rdump /var/log/journal target" copy.img
# PREVIEW_DEVICES="-device ..." adds QEMU devices.
set -euo pipefail
. "$(dirname "$0")/../tools/lib.sh"

p=$BUILD/preview
[[ $ARCH == aarch64 ]] || p=$BUILD/preview-$ARCH
p=${PREVIEW_DIR:-$p}
[[ -f $p/preview.img ]] || die "no preview, run image/mkpreview.sh first"
out=${1:-$BUILD/preview-test}
shift || true
shots=("${@:-120 240 360}")
read -r -a shots <<< "${shots[*]}"
port=45454

gpu=(-device virtio-gpu-pci,xres=720,yres=1440,edid=off -display none)
[[ ${PREVIEW_GL:-off} != on ]] || gpu=(-device virtio-gpu-gl-pci,xres=720,yres=1440,edid=off -display egl-headless)
case $ARCH in
	aarch64) qemu=(qemu-system-aarch64 -machine virt -cpu max,pauth-impdef=on) console=ttyAMA0 ;;
	x86_64) qemu=(qemu-system-x86_64 -machine q35,i8042=off,vmport=off -accel kvm -cpu host -vga none) console=ttyS0 ;;
esac
# -snapshot only reads the image: locking off, so the test also works alongside a running preview
snapshot=(-snapshot) locking=,file.locking=off
[[ -z ${PREVIEW_IMG:-} ]] || { snapshot=(); locking=; }

mkdir -p "$out"
: > "$out/serial.log"

"${qemu[@]}" -smp 8 -m 4G \
	-kernel "$p/kernel" -initrd "$p/initramfs-preview.img" \
	-append "root=/dev/vda rw earlycon console=$console ${PREVIEW_APPEND:-}" \
	-drive file="${PREVIEW_IMG:-$p/preview.img}",format=raw,if=virtio$locking "${snapshot[@]}" \
	"${gpu[@]}" \
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
	sleep 2
	exec 3>&-
}

sleep 5
kill -0 $qpid 2>/dev/null || die "QEMU did not start"
msg "QEMU running (PID $qpid), log: $out/serial.log"

elapsed=0
for t in "${shots[@]}"; do
	sleep $((t - elapsed))
	elapsed=$t
	kill -0 $qpid 2>/dev/null || die "QEMU exited after ${t}s"
	monitor "screendump $out/shot-${t}s.ppm"
	msg "Screenshot after ${t}s: $out/shot-${t}s.ppm"
done
monitor quit
wait $qpid || true
trap - EXIT

msg "Excerpt from the boot log:"
grep -a -E 'Linux version|Reached target|greetd|phosh|Failed|login:' "$out/serial.log" | tail -20 || true
