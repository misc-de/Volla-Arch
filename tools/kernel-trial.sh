#!/bin/bash
# Tries a new UT kernel on the Arch that already runs from the memory card: adds a second boot menu
# entry with the same system and data partitions, and the modules of plinius-kmod next to the old
# ones (they live in one directory per kernel release). The old entry stays as it is, so choosing
# it in the menu brings back the previous state.
#
# Usage: tools/kernel-trial.sh <build dir>            only shows what would be done
#        tools/kernel-trial.sh <build dir> --apply    does it
#        tools/kernel-trial.sh <build dir> --remove   takes it back; run under the old kernel
#
# Example: tools/kernel-trial.sh build/ut/04112026
#
# The modules are not installed as a package here, because pacman would remove those of the old
# kernel. Run --remove before installing plinius-kmod for good: pacman refuses files it does not know.
#
# PLINIUS_SSH replaces the SSH command (default: ssh root@10.15.19.82).
set -euo pipefail
. "$(dirname "$0")/lib.sh"

build=${1:?usage: tools/kernel-trial.sh <build dir> [--apply|--remove]}
mode=${2:-}
ssh_cmd=(${PLINIUS_SSH:-ssh root@10.15.19.82})
# Partition of the boot entries, and the entry the trial copies. Only these names are written to.
part=/dev/mmcblk0p1
base=rom1
new=rom2
title="Arch Linux ($(basename "$build"))"

pkg=$(ls "$LOCALREPO"/plinius-kmod-*-aarch64.pkg.tar.* | grep -v '\.sig$' | sort -V | tail -1)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

"$VOLLA_DIR/tools/boot-files.sh" "$build" "$tmp/boot" 2>/dev/null
bsdtar -xf "$pkg" -C "$tmp" usr/lib/plinius/modules
release=$(ls "$tmp/usr/lib/plinius/modules")
[[ $(echo "$release" | wc -l) == 1 ]] || die "more than one kernel release in $pkg"
# Into a variable first: grep -q would end the pipe early, and pipefail would take that for a failure
banner=$(gzip -dc "$tmp/boot/zImage" | strings | grep '^Linux version [0-9]') || true
[[ $banner == "Linux version $release "* ]] ||
	die "the kernel of $build is not $release, the release of $pkg"

# Reads the card without mounting it, then says what the current state allows
state=$("${ssh_cmd[@]}" "
	findmnt -rno TARGET -S $part || true
	echo \"running \$(uname -r)\"
	debugfs -c -R 'ls -p /db/entries' $part 2>/dev/null | cut -d/ -f6 | grep '\.conf$' | sed 's/^/entry /'
	echo \"last \$(debugfs -c -R 'cat /db/last_index' $part 2>/dev/null | tr -d '\\000')\"
	[ ! -d /usr/lib/plinius/modules/$release ] || echo modules
")
grep -q "^entry $base.conf$" <<< "$state" || die "no entry $base on the card"
running=$(sed -n 's/^running //p' <<< "$state")
entry=$("${ssh_cmd[@]}" "debugfs -c -R 'cat /db/entries/$base.conf' $part 2>/dev/null")
# Same system and data; own kernel, ramdisk and logo, and a title of its own
text=$(sed -e "s#^linux .*#linux $new/zImage#" -e "s#^initrd .*#initrd $new/initrd.cpio.gz#" \
	-e "s#^logo .*#logo $new/logo.bin#" -e "s#^title .*#title $title#" <<< "$entry")

case $mode in
"")
	echo "Running kernel: $running"
	echo "Trial kernel:   $release"
	echo "Entries now:    $(sed -n 's/^entry //p' <<< "$state" | tr '\n' ' ') (last_index $(sed -n 's/^last //p' <<< "$state"))"
	grep -q "^entry $new.conf$" <<< "$state" && echo "Note: $new already exists, --apply would refuse"
	grep -q '^/' <<< "$state" && echo "Note: $part is mounted, --apply would refuse"
	echo "Would copy zImage ($(stat -c %s "$tmp/boot/zImage")), initrd.cpio.gz ($(stat -c %s "$tmp/boot/initrd.cpio.gz")) and the logo to $new/"
	echo "Would copy the modules to /usr/lib/plinius/modules/$release"
	echo "Would write db/entries/$new.conf:"
	sed 's/^/    /' <<< "$text"
	echo "Nothing was changed. --apply does it."
	;;
--apply)
	grep -q "^entry $new.conf$" <<< "$state" && die "$new already exists"
	grep -q '^/' <<< "$state" && die "$part is mounted"
	[[ $running != "$release" ]] || die "the phone already runs $release"
	tar -C "$tmp" --owner=root --group=root -cf - usr/lib/plinius/modules | "${ssh_cmd[@]}" "tar -xf - -C /"
	"${ssh_cmd[@]}" "mkdir -p /run/plinius-trial && mount $part /run/plinius-trial && mkdir -m 700 /run/plinius-trial/$new"
	# The entry goes in last and through a rename from outside the directory: the menu numbers
	# entries by their position there, and no leftover slot should shift them
	cat "$tmp/boot/zImage" | "${ssh_cmd[@]}" "cat > /run/plinius-trial/$new/zImage"
	cat "$tmp/boot/initrd.cpio.gz" | "${ssh_cmd[@]}" "cat > /run/plinius-trial/$new/initrd.cpio.gz"
	"${ssh_cmd[@]}" "cp /run/plinius-trial/$base/logo.bin /run/plinius-trial/$new/logo.bin"
	"${ssh_cmd[@]}" "cat > /run/plinius-trial/db/$new.conf.new" <<< "$text"
	"${ssh_cmd[@]}" "
		set -e
		cd /run/plinius-trial
		chown -R 1000:1000 $new db/$new.conf.new
		chmod 600 $new/zImage $new/initrd.cpio.gz $new/logo.bin db/$new.conf.new
		sync
		mv db/$new.conf.new db/entries/$new.conf
		sync
		cd /
		umount /run/plinius-trial
		rmdir /run/plinius-trial
	"
	echo "Done. After a restart, choose \"$title\" in the boot menu."
	;;
--remove)
	[[ $running != "$release" ]] || die "the phone runs $release: choose the old entry in the boot menu first"
	# The menu does not check last_index. Under the old kernel it points at the old entry, which
	# stands before this one, so it stays valid
	"${ssh_cmd[@]}" "
		set -e
		mkdir -p /run/plinius-trial
		mount $part /run/plinius-trial
		cd /run/plinius-trial
		rm -f db/entries/$new.conf $new/zImage $new/initrd.cpio.gz $new/logo.bin
		rmdir $new
		sync
		cd /
		umount /run/plinius-trial
		rmdir /run/plinius-trial
		rm -f /usr/lib/plinius/modules/$release/*.ko
		rmdir /usr/lib/plinius/modules/$release
	"
	echo "Done. Only the old entry is left."
	;;
*)
	die "usage: tools/kernel-trial.sh <build dir> [--apply|--remove]"
	;;
esac
