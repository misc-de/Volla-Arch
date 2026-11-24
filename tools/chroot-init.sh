#!/bin/bash
# Creates the build chroot for $ARCH (default aarch64) or brings it up to date.
# Idempotent: an existing chroot is only updated.
#   aarch64: ALARM tarball, runs via qemu-user (the device)
#   x86_64:  Arch bootstrap tarball, runs natively (only for the KVM preview)
#
# Usage: [ARCH=x86_64] tools/chroot-init.sh
set -euo pipefail
. "$(dirname "$0")/lib.sh"

check_host

if [[ ! -d $CHROOT/usr ]]; then
	mkdir -p "$DOWNLOADS" "$CHROOT"
	case $ARCH in
		aarch64)
			tarball=ArchLinuxARM-aarch64-latest.tar.gz
			url=$MIRROR/os/$tarball
			sumfile=$tarball.md5 sumtool=md5sum strip=0
			;;
		x86_64)
			tarball=archlinux-bootstrap-x86_64.tar.zst
			url=https://geo.mirror.pkgbuild.com/iso/latest/$tarball
			sumfile=sha256sums.txt sumtool=sha256sum strip=1   # content is in root.x86_64/
			;;
	esac
	if [[ ! -f $DOWNLOADS/$tarball ]]; then
		msg "Downloading $tarball"
		curl -fL -o "$DOWNLOADS/$tarball.part" "$url"
		curl -fsL -o "$DOWNLOADS/$sumfile" "${url%/*}/$sumfile"
		mv "$DOWNLOADS/$tarball.part" "$DOWNLOADS/$tarball"
	fi
	[[ -f $DOWNLOADS/$tarball.sig ]] || curl -fsL -o "$DOWNLOADS/$tarball.sig" "$url.sig"
	(cd "$DOWNLOADS" && grep " $tarball\$" "$sumfile" | $sumtool -c --quiet) ||
		die "checksum of $tarball does not match"
	# The checksum comes from the same server and only verifies the transfer. The tarball is
	# authentic only with a valid signature; it also supplies the keyring pacman trusts afterwards.
	case $ARCH in
		aarch64)
			# Arch Linux ARM Build System <builder@archlinuxarm.org>
			fpr=68B3537F39A313B3E574D06777193F152BDBE6A6
			keyring=$DOWNLOADS/archlinuxarm-builder.gpg
			if [[ ! -s $keyring ]]; then
				gnupg=$(mktemp -d)
				gpg --homedir "$gnupg" --quiet --keyserver hkps://keyserver.ubuntu.com --recv-keys "$fpr"
				gpg --homedir "$gnupg" --export "$fpr" > "$keyring"
				gpgconf --homedir "$gnupg" --kill all
				rm -rf "$gnupg"
			fi
			;;
		x86_64)
			# Release keys are in archlinux-keyring: on an Arch or Manjaro build host its keyring
			# file or pacman's own; elsewhere (Debian) a copy of that file in the downloads,
			# taken over from such a host (scp /usr/share/pacman/keyrings/archlinux.gpg)
			keyring=
			for k in /usr/share/pacman/keyrings/archlinux.gpg /etc/pacman.d/gnupg/pubring.gpg \
				"$DOWNLOADS/archlinux.gpg"; do
				[[ -r $k ]] && { keyring=$k; break; }
			done
			[[ -n $keyring ]] || die "no Arch Linux keyring, cannot verify the bootstrap signature" \
				"(copy /usr/share/pacman/keyrings/archlinux.gpg to $DOWNLOADS/archlinux.gpg)"
			# archlinux-keyring ships it ASCII-armored; gpgv only reads binary keyrings
			if [[ $(head -c 5 "$keyring") == ----- ]]; then
				gpg --dearmor < "$keyring" > "$DOWNLOADS/archlinux-keyring.bin"
				keyring=$DOWNLOADS/archlinux-keyring.bin
			fi
			;;
	esac
	# gpgv is not on every build host (Debian minimal installs); gpg --verify with a throwaway home
	# checks against the same keyring
	if command -v gpgv >/dev/null; then
		verify=(gpgv --keyring "$keyring")
	else
		vhome=$(mktemp -d)
		verify=(gpg --homedir "$vhome" --batch --no-default-keyring --keyring "$keyring" --verify)
	fi
	"${verify[@]}" "$DOWNLOADS/$tarball.sig" "$DOWNLOADS/$tarball" 2>/dev/null ||
		die "signature of $tarball missing or invalid"
	[[ -z ${vhome:-} ]] || { gpgconf --homedir "$vhome" --kill all; rm -rf "$vhome"; }
	msg "Extracting to $CHROOT"
	unshare --map-auto --map-root-user bsdtar -xpf "$DOWNLOADS/$tarball" \
		--strip-components "$strip" -C "$CHROOT"
fi

msg "Configuring and updating the chroot (takes a while under qemu)"
ns_run '
	r=$1 keyring=$2 mirrorlist=$3
	# Landlock and switching to the alpm user do not work under qemu-user.
	# Applies only to this build chroot, not to the device.
	grep -q "^DisableSandbox" "$r/etc/pacman.conf" ||
		sed -i "s/^\[options\]/[options]\nDisableSandbox/" "$r/etc/pacman.conf"
	# Up to 10 downloads at once instead of the default 5
	sed -i "s/^#\?ParallelDownloads.*/ParallelDownloads = 10/" "$r/etc/pacman.conf"
	printf "%s\n" "$mirrorlist" > "$r/etc/pacman.d/mirrorlist"

	chroot_mounts "$r"
	if [[ ! -f $r/etc/pacman.d/gnupg/trustdb.gpg ]]; then
		in_chroot "$r" pacman-key --init
		in_chroot "$r" pacman-key --populate "$keyring"
	fi
	# A build chroot needs no kernel or firmware; they only make every update longer.
	for p in linux-aarch64 linux-firmware; do
		in_chroot "$r" pacman -Q "$p" >/dev/null 2>&1 && in_chroot "$r" pacman -Rns --noconfirm "$p"
	done
	in_chroot "$r" pacman -Syu --noconfirm --needed base-devel git

	# Fixed UID in both architectures (on ALARM, 1000 belongs to user alarm): build.sh hands the
	# shared source cache to builder, so different UIDs would keep taking it from each other.
	if ! in_chroot "$r" id builder >/dev/null 2>&1; then
		in_chroot "$r" useradd -m -u 1001 builder
	elif [ "$(in_chroot "$r" id -u builder)" != 1001 ]; then
		in_chroot "$r" groupmod -g 1001 builder
		in_chroot "$r" usermod -u 1001 -g 1001 builder
	fi
	# build.sh installs dependencies as namespace root, sudo is not needed.
	rm -f "$r/etc/sudoers.d/builder"
' "$CHROOT" "$KEYRING" "$(mirrorlist)"

msg "Build chroot ready: $CHROOT"
