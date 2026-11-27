#!/bin/bash
# Fetches the parts of an Ubuntu Touch build for the Plinius that image/mkimage.sh needs, from
# Volla's server for its multi-boot manager (abm.volla.tech), and lays them out as mkimage.sh -a
# expects them:
#
#   build/ut/<build>/boot.img, vendor_boot.img, init_boot.img
#   build/ut/<build>/android/android-rootfs.img, vendor.img, vendor_dlkm.img, system_dlkm.img
#
# The three boot images are downloaded directly. The Android images live inside UT's system.img
# (an ext4 image of about 5 GB) under /var/lib/lxc/android/; they are copied out with debugfs, no
# mount and no root needed. system.img is deleted afterwards unless KEEP_SYSTEM=1.
#
# Only the build pinned in tools/ut-build.sha256 is taken, and every file must have the SHA-256
# listed there: a downloaded file, one copied out of system.img and one already there from an
# earlier run alike. A file that differs stops the run (a fresh download is removed again). The
# pinned build is the one tools/boot-files.sha256 is made for: plinius-kmod is built against
# exactly that kernel. A newer build needs a new plinius-kmod and new checksums in both files:
# UT_UNPINNED=1 fetches a build that is not pinned, unchecked, and prints the lines for
# tools/ut-build.sha256 (to be compared with the phone first). mkimage.sh refuses its files until
# they are pinned.
#
# Usage: tools/ut-fetch.sh [BUILD] [OUTPUT_DIR]
#   BUILD       UT build id as on abm.volla.tech (default: the pinned build; another one only
#               with UT_UNPINNED=1)
#   OUTPUT_DIR  default: build/ut/BUILD
# Environment: UT_BASE_URL (default https://abm.volla.tech/ansuz/ubports), KEEP_SYSTEM=1,
#              UT_SYSTEM_IMG (where system.img is kept, default build/downloads/ut-BUILD-system.img),
#              UT_UNPINNED=1
set -euo pipefail
. "$(dirname "$0")/lib.sh"

sums=$VOLLA_DIR/tools/ut-build.sha256
pinned=$(awk '$1 == "#" && $2 == "build" && $3 ~ /^[0-9]+$/ { print $3; exit }' "$sums")
[[ $pinned =~ ^[0-9]{8}$ ]] || die "no build id in $sums"
build=${1:-$pinned}
[[ $build =~ ^[0-9]{8}$ ]] || die "invalid build id: $build (expected 8 digits, e.g. $pinned)"
unpinned=0
if [[ $build != "$pinned" ]]; then
	[[ ${UT_UNPINNED:-0} == 1 ]] ||
		die "build $build is not pinned in tools/ut-build.sha256 ($pinned is): a new build needs a new plinius-kmod and new checksums first (UT_UNPINNED=1 fetches it for them)"
	unpinned=1
	msg "Warning: build $build is not pinned, its files are not checked; mkimage.sh refuses them"
fi
out=${2:-$BUILD/ut/$build}
base=${UT_BASE_URL:-https://abm.volla.tech/ansuz/ubports}/$build
system=${UT_SYSTEM_IMG:-$DOWNLOADS/ut-$build-system.img}
android_files=(android-rootfs.img vendor.img vendor_dlkm.img system_dlkm.img)

for tool in curl debugfs sha256sum; do
	command -v "$tool" >/dev/null || die "$tool missing"
done

# ut_pinned FILE NAME: whether FILE is NAME of the pinned build (NAME as listed in
# tools/ut-build.sha256: boot.img, android/vendor.img, ...)
ut_pinned() {
	local want got
	((!unpinned)) || return 0
	want=$(awk -v n="$2" '$1 !~ /^#/ && $2 == n { print $1 }' "$sums")
	[[ $want =~ ^[0-9a-f]{64}$ ]] || die "$2 is not pinned in tools/ut-build.sha256"
	got=$(sha256sum < "$1")
	[[ ${got%% *} == "$want" ]]
}

# have FILE NAME: whether FILE is there; dies when it is, but not the pinned NAME
have() {
	[[ -s $1 ]] || return 1
	ut_pinned "$1" "$2" ||
		die "${1#"$VOLLA_DIR"/} is not $2 of UT build $build (SHA-256 differs from tools/ut-build.sha256); remove it to download it again"
	if ((unpinned)); then
		msg "Already there, unchecked: ${1#"$VOLLA_DIR"/}"
	else
		msg "Already there and as pinned: ${1#"$VOLLA_DIR"/}"
	fi
}

# Downloads $1 to $2, resuming a partial download from an earlier run. With $3 (its name in
# tools/ut-build.sha256) the download must be that file.
fetch() {
	local url=$1 dest=$2 name=${3:-}
	msg "Downloading $url"
	curl -fL -A "$USER_AGENT" --retry 5 --retry-all-errors --retry-delay 2 -C - -o "$dest.part" "$url"
	if [[ -n $name ]] && ! ut_pinned "$dest.part" "$name"; then
		command rm -f "$dest.part"
		die "$url is not $name of UT build $build (SHA-256 differs from tools/ut-build.sha256)"
	fi
	mv "$dest.part" "$dest"
}

mkdir -p "$out/android" "$(dirname "$system")"
for img in boot vendor_boot init_boot; do
	have "$out/$img.img" "$img.img" || fetch "$base/$img.img" "$out/$img.img" "$img.img"
done

missing=()
for f in "${android_files[@]}"; do
	have "$out/android/$f" "android/$f" || missing+=("$f")
done
if ((${#missing[@]})); then
	# system.img itself is not pinned, only what is taken out of it
	[[ -s $system ]] || fetch "$base/system.img" "$system"
	for f in "${missing[@]}"; do
		msg "Copying $f out of system.img"
		# debugfs reports a missing file on stderr but still exits 0, so check the result
		debugfs -R "dump /var/lib/lxc/android/$f $out/android/$f.part" "$system" 2>/dev/null
		[[ -s $out/android/$f.part ]] ||
			die "/var/lib/lxc/android/$f not found in $system"
		if ! ut_pinned "$out/android/$f.part" "android/$f"; then
			command rm -f "$out/android/$f.part"
			die "$f in $system is not android/$f of UT build $build (SHA-256 differs from tools/ut-build.sha256)"
		fi
		mv "$out/android/$f.part" "$out/android/$f"
	done
	[[ ${KEEP_SYSTEM:-0} == 1 ]] || command rm -f "$system"
fi

if ((unpinned)); then
	# What tools/ut-build.sha256 would list, on stdout
	echo "# build $build"
	(cd "$out" && sha256sum boot.img vendor_boot.img init_boot.img "${android_files[@]/#/android/}")
	msg "Build $build fetched unchecked: compare the lines above with the phone before they go into tools/ut-build.sha256"
	exit 0
fi

# The same check mkimage.sh makes before it installs anything: does this kernel fit plinius-kmod?
if command -v unpack_bootimg >/dev/null; then
	tmp=$(mktemp -d)
	trap 'command rm -rf "$tmp"' EXIT
	"$VOLLA_DIR/tools/boot-files.sh" "$out" "$tmp" >/dev/null 2>"$tmp/log" || {
		cat "$tmp/log" >&2
		die "the boot files of build $build do not match tools/boot-files.sha256"
	}
	msg "Boot files match tools/boot-files.sha256"
else
	msg "Note: unpack_bootimg missing (package android-tools), kernel and ramdisks not checked"
fi
msg "Done: image/mkimage.sh -a ${out#"$VOLLA_DIR"/}/android/android-rootfs.img"
