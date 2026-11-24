#!/bin/bash
# Starts the QEMU preview from image/mkpreview.sh in a phone's portrait format.
#
# Usage: [ARCH=x86_64] image/run-preview.sh
#   ARCH=aarch64 (default): ARM emulated, ALARM packages as on the device, slow
#   ARCH=x86_64: near-native speed with KVM (package qemu-system-x86)
# Without 3D on the host: PREVIEW_GL=off image/run-preview.sh
# Input: touch (virtio-multitouch, fed by the host touchscreen), keyboard, and the mouse as an
# absolute tablet. Without an absolute pointing device the GTK window grabs the mouse on click
# (released only with Ctrl+Alt+G); PREVIEW_MOUSE=off leaves out the tablet anyway. The mouse
# switches Phosh into docked mode, so the preview pins the phone settings via dconf
# (docs/GRAFIK.md).
# Window: QEMU GTK stays at its initial size of 640x480 and does not follow the guest's
# 720x1440 mode. Under X11, xdotool sets the window to 720x1440 once; zoom-to-fit then scales
# any window size with the correct aspect ratio (without zoom-to-fit QEMU crops).
# GDK_SCALE=1 so that 720x1440 maps 1:1 onto the screen even on HiDPI hosts.
# Menu bar off, Ctrl+Alt+M shows it.
# edid=off: with EDID, QEMU reports 180x360 mm and squeekboard then uses the wide tablet layout;
# without a size it uses its phone values. phosh sets the scale of 2 in phoc.ini.
# x86_64: q35 without PS/2 (i8042) and VMware port, either would silently add a pointer.
# PREVIEW_SNAPSHOT=on discards all changes to the image on exit.
# While the VM runs, the script serves the local repo (build/repo/local*) to it over HTTP on
# 127.0.0.1:45461 (PREVIEW_REPO_PORT).
# QEMU monitor only locally on 127.0.0.1:45460 (PREVIEW_PORT), e.g. for screenshots:
#   exec 3<>/dev/tcp/127.0.0.1/45460; echo "screendump /tmp/vm.ppm" >&3
set -euo pipefail
. "$(dirname "$0")/../tools/lib.sh"

p=$BUILD/preview
[[ $ARCH == aarch64 ]] || p=$BUILD/preview-$ARCH
[[ -f $p/preview.img ]] || die "no preview in $p, run ARCH=$ARCH image/mkpreview.sh first"

if [[ ${PREVIEW_GL:-on} == on ]]; then
	gpu=(-device virtio-gpu-gl-pci,xres=720,yres=1440,edid=off -display gtk,gl=on,zoom-to-fit=on,show-menubar=off)
else
	gpu=(-device virtio-gpu-pci,xres=720,yres=1440,edid=off -display gtk,zoom-to-fit=on,show-menubar=off)
fi

mouse=(-device virtio-tablet-pci)
[[ ${PREVIEW_MOUSE:-on} == on ]] || mouse=()

drive=file="$p/preview.img",format=raw,if=virtio
snapshot=()
[[ ${PREVIEW_SNAPSHOT:-off} != on ]] || { drive+=,file.locking=off; snapshot=(-snapshot); }

name=plinius-vorschau-$$
# Resize the window once it appears (see header); found by its unique title
if [[ -n ${DISPLAY:-} ]] && command -v xdotool >/dev/null; then
	(
		for _ in $(seq 60); do
			w=$(xdotool search --name "$name" 2>/dev/null | head -1) || true
			if [[ -n $w ]]; then
				sleep 1
				xdotool windowsize "$w" 720 1440
				exit
			fi
			sleep 1
		done
	) &
fi

case $ARCH in
	aarch64)
		# pauth-impdef: fast instead of exact PAuth emulation, noticeably quicker under TCG
		qemu=(qemu-system-aarch64 -machine virt -cpu max,pauth-impdef=on)
		console=ttyAMA0
		;;
	x86_64)
		qemu=(qemu-system-x86_64 -machine q35,i8042=off,vmport=off -accel kvm -cpu host -vga none)
		console=ttyS0
		;;
esac

# Local repo for the VM (server line of [plinius-local] in mkpreview.sh): new packages of ours
# and approvals (plinius-freigabe) arrive via pacman -Syu, as later from the server to the device.
# Only on 127.0.0.1, which the VM reaches as 10.0.2.2. If one is already running, keep using it.
repo_port=${PREVIEW_REPO_PORT:-45461}
if ! (exec 3<>"/dev/tcp/127.0.0.1/$repo_port") 2>/dev/null; then
	python3 -I -m http.server --bind 127.0.0.1 --directory "$LOCALREPO" "$repo_port" >/dev/null 2>&1 &
	repo_pid=$!
	trap 'kill $repo_pid 2>/dev/null' EXIT
fi

env GDK_SCALE=1 GDK_DPI_SCALE=1 "${qemu[@]}" -name "$name" -smp 8 -m 4G \
	-kernel "$p/kernel" -initrd "$p/initramfs-preview.img" \
	-append "root=/dev/vda rw console=tty0 console=$console" \
	-drive "$drive" "${snapshot[@]}" \
	"${gpu[@]}" \
	-device virtio-keyboard-pci -device virtio-multitouch-pci "${mouse[@]}" \
	-netdev user,id=n0 -device virtio-net-pci,netdev=n0 \
	-monitor tcp:127.0.0.1:${PREVIEW_PORT:-45460},server,nowait
