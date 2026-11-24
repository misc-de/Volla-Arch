#!/bin/bash
# Automated test of full-disk encryption (package plinius-crypt) in the QEMU preview:
#  1. Copy the preview, set the request (like "plinius-encrypt")
#  2. Boot 0: "set password", a mismatching repetition, empty password back to the choice,
#     "continue without encryption" -> Phosh; the request is gone, still unencrypted
#  3. Boot 1 (request set again): password twice -> encryption with progress view ->
#     "complete" message -> Phosh
#  4. Boot 2: a wrong password, then the right one -> Phosh
#  5. Check that the copy is a LUKS2 container
# Each boot runs in a fresh QEMU, so the image can be changed in between.
# Every screen is captured (NN-name.png in the output directory).
# The original image stays unchanged. Fast with KVM: ARCH=x86_64 image/test-crypt.sh
# Output in build/crypt-test-$ARCH (disk.img, serial.log, screenshots).
# CRYPT_INITRD=file uses a different ramdisk, e.g. with a modified hook (without mkpreview.sh).
# CRYPT_APPEND appends kernel parameters.
#
# The serial log patterns awaited below are printed by the initramfs hook
# (pkgbuilds/plinius-crypt/hook; it logs the English prompt text, the screen shows the
# translation) and by Phosh; keep both sides in sync.
set -euo pipefail
. "$(dirname "$0")/../tools/lib.sh"

p=$BUILD/preview
[[ $ARCH == aarch64 ]] || p=$BUILD/preview-$ARCH
[[ -f $p/preview.img ]] || die "no preview in $p, run ARCH=$ARCH image/mkpreview.sh first"
out=$BUILD/crypt-test-$ARCH
pass=geheim1234   # lowercase letters/digits without y/z only: independent of the keyboard layout
other=anders1234  # mismatching repetition and wrong unlock password
port=45455

mkdir -p "$out"
: > "$out/serial.log"
msg "Copying preview to $out/disk.img"
cp --sparse=always "$p/preview.img" "$out/disk.img"

# has_request: true if the image carries the encryption request
has_request() {
	debugfs -R "stat /var/lib/plinius-crypt/request" "$out/disk.img" 2>/dev/null | grep -q 'Type: regular'
}

# set_request: sets the request like plinius-encrypt
set_request() {
	# The kernel would replay an open journal (VM not shut down cleanly) on mount and
	# thereby overwrite what debugfs is about to write
	e2fsck -fy "$out/disk.img" >/dev/null 2>&1 || (($? <= 2)) || die "e2fsck reports errors in the image"
	printf 'test-crypt\n' > "$out/request"
	# The directory does not exist in the preview image yet
	debugfs -w -R "mkdir /var/lib/plinius-crypt" "$out/disk.img" >/dev/null 2>&1 || true
	debugfs -w -R "write $out/request /var/lib/plinius-crypt/request" "$out/disk.img" >/dev/null 2>&1 || true
	has_request || die "could not write the request into the image"
}

case $ARCH in
	aarch64) qemu=(qemu-system-aarch64 -machine virt -cpu max,pauth-impdef=on) console=ttyAMA0 ;;
	x86_64) qemu=(qemu-system-x86_64 -machine q35,i8042=off,vmport=off -accel kvm -cpu host -vga none) console=ttyS0 ;;
esac
qpid=
trap '[[ -z $qpid ]] || kill $qpid 2>/dev/null || true' EXIT

# start_vm: boots the image in a new QEMU; the serial log is appended to
start_vm() {
	new_boot
	"${qemu[@]}" -smp 8 -m 4G \
		-kernel "$p/kernel" -initrd "${CRYPT_INITRD:-$p/initramfs-preview.img}" \
		-append "root=/dev/vda rw console=$console systemd.journald.forward_to_console=1 ${CRYPT_APPEND:-}" \
		-drive file="$out/disk.img",format=raw,if=virtio \
		-device virtio-gpu-pci,xres=720,yres=1440,edid=off -display none \
		-device virtio-tablet-pci -device virtio-keyboard-pci \
		-netdev user,id=n0 -device virtio-net-pci,netdev=n0 \
		-chardev file,id=s0,path="$out/serial.log",append=on -serial chardev:s0 \
		-monitor tcp:127.0.0.1:$port,server,nowait &
	qpid=$!
	sleep 3
	kill -0 $qpid 2>/dev/null || die "QEMU did not start"
}

# stop_vm: ends QEMU (like pulling the plug)
stop_vm() {
	monitor quit
	wait $qpid || true
	qpid=
}

# monitor CMD: sends one command to the QEMU monitor
monitor() {
	exec 3<>/dev/tcp/127.0.0.1/$port
	echo "$1" >&3
	sleep 1
	exec 3>&-
}

# First line of the serial log that wait_for looks at; set to the end of the log by new_boot
since=1

# new_boot: makes wait_for count only what the following boot prints (called by start_vm)
new_boot() {
	since=$(( $(wc -l < "$out/serial.log") + 1 ))
}

# wait_for PATTERN SECONDS [COUNT]: waits until PATTERN occurs COUNT times in the serial log
# since the last new_boot
wait_for() {
	local pattern=$1 timeout=$2 count=${3:-1} t=0
	while (( t < timeout )); do
		(( $(tail -n +"$since" "$out/serial.log" | grep -a -c -- "$pattern" || true) >= count )) && return 0
		kill -0 $qpid 2>/dev/null || die "QEMU exited while waiting for '$pattern'"
		sleep 1
		t=$((t + 1))
	done
	die "timeout: '$pattern' not seen (${timeout}s), see $out/serial.log"
}

# type_pass [TEXT]: types TEXT (default $pass) followed by Enter via QEMU sendkey
type_pass() {
	local text=${1-$pass} i
	sleep 2   # unl0kr needs a moment after the prompt before it accepts input
	for ((i = 0; i < ${#text}; i++)); do
		monitor "sendkey ${text:i:1}"
	done
	monitor "sendkey ret"
}

# shot NAME: screenshot to $out/NAME.png (QEMU writes PPM, converted if ImageMagick is there)
shot() {
	monitor "screendump $out/$1.ppm"
	if command -v magick >/dev/null && magick "$out/$1.ppm" "$out/$1.png"; then
		rm -f "$out/$1.ppm"
		msg "Screenshot: $out/$1.png"
	else
		msg "Screenshot: $out/$1.ppm"
	fi
}

msg "Boot 0: try, go back, continue without encryption"
set_request
start_vm
wait_for "plinius-crypt: Encrypt the device?" 120
sleep 2
shot 01-choice
monitor "sendkey ret"
wait_for "plinius-crypt: Set up encryption: new password" 60
sleep 2
shot 02-setup
type_pass
wait_for "plinius-crypt: Repeat new password" 60
type_pass "$other"
wait_for "plinius-crypt: The passwords do not match" 60
sleep 2
shot 03-mismatch
type_pass ""
wait_for "plinius-crypt: Encrypt the device?" 60 2
sleep 2
monitor "sendkey tab"
shot 04-choice-without
monitor "sendkey ret"
wait_for "plinius-crypt: Continue without encryption" 60
wait_for "Phosh ready" 180
stop_vm
cryptsetup isLuks "$out/disk.img" && die "boot 0 encrypted although encryption was declined"
has_request && die "boot 0 kept the request although encryption was declined"

msg "Boot 1: setting up encryption"
set_request
start_vm
wait_for "plinius-crypt: Encrypt the device?" 120
sleep 2   # the choice needs a moment before it accepts input
monitor "sendkey ret"
wait_for "plinius-crypt: Set up encryption: new password" 60
type_pass
wait_for "plinius-crypt: Repeat new password" 60
sleep 2
shot 05-repeat
type_pass
wait_for "Encrypting" 60
sleep 6
shot 06-progress
sleep 6
shot 07-progress
wait_for "plinius-crypt: Encryption complete" 1800
msg "Encryption done"
sleep 2
shot 08-complete
wait_for "Phosh ready" 180
sleep 5
shot 09-boot1-phosh
stop_vm

msg "Boot 2: unlock"
start_vm
wait_for "plinius-crypt: Password to unlock" 120
sleep 2
shot 10-unlock
type_pass "$other"
wait_for "plinius-crypt: Wrong password" 60
sleep 2
shot 11-wrong
type_pass
wait_for "Phosh ready" 180
sleep 5
shot 12-boot2-phosh
stop_vm

if cryptsetup isLuks "$out/disk.img"; then
	msg "Result: $out/disk.img is a LUKS container"
	cryptsetup luksDump "$out/disk.img" | grep -E '^(Version|Requirements)|cipher:' | head -4 >&2
else
	die "Result: $out/disk.img is NOT a LUKS container"
fi
