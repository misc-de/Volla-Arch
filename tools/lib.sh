# Shared paths and helper functions for the build scripts (sourced, not executed).
# Everything runs without root: user namespace (unshare --map-auto) + qemu-binfmt.
# ARCH selects the target (aarch64 default, x86_64 for the KVM preview); MIRRORS and
# SIGN_GNUPG can be overridden from the environment.

VOLLA_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PKGBUILDS=$VOLLA_DIR/pkgbuilds
BUILD=$VOLLA_DIR/build
DOWNLOADS=$BUILD/downloads
# Sources (git clones, tarballs) of all PKGBUILDs, architecture independent (makepkg SRCDEST)
SRCCACHE=$BUILD/cache/sources
# Signing key of the [plinius] repo (tools/keygen.sh), deliberately outside build/:
# build/ can be deleted, the key cannot be replaced (devices trust only this key)
SIGN_GNUPG=${SIGN_GNUPG:-$HOME/.local/share/plinius/gnupg}
# Published repo [plinius], one subdirectory per architecture (tools/publish.sh)
PUBREPO=$BUILD/repo/plinius
# What every download from these scripts says it is (curl -A; pacman in the chroots through
# HTTP_USER_AGENT, see in_chroot)
USER_AGENT="Plinius Arch Linux"

# Channels of [plinius] (docs/PAKETIERUNG.md, hosting): beta gets every published state,
# stable only what tools/release.sh takes over from beta after testing. Both carry the repo
# name [plinius], so a device switches by its Server line alone. Stable stays at the top level,
# where the devices installed before the channels point.
# Prints the directory of channel $1 below the repo root $2 (local $PUBREPO or the server path)
channel_dir() {
	case $1 in
		stable) echo "$2/$ARCH" ;;
		beta) echo "$2/beta/$ARCH" ;;
		*) die "unknown channel: $1" ;;
	esac
}

# Prints "name version" for every package in the database $1, sorted by name
db_versions() {
	bsdtar -xOf "$1" '*/desc' | awk '
		/^%NAME%$/ { getline; name = $0 }
		/^%VERSION%$/ { getline; print name, $0 }' | LC_ALL=C sort -k1,1
}

# Prints the number of parallel compile jobs. A machine with a battery is a notebook: from 20:00
# to 06:00 it builds with 80 % of its threads to keep the fans down. A build host always uses all.
build_jobs() {
	local threads hour bat
	threads=$(nproc)
	hour=$(date +%H)
	hour=$((10#$hour))
	for bat in /sys/class/power_supply/BAT*; do
		if [[ -e $bat ]] && (( hour >= 20 || hour < 6 )); then
			threads=$(( threads * 80 / 100 ))
			break
		fi
	done
	echo $(( threads > 0 ? threads : 1 ))
}

# Signs the freshness stamp of a published channel (docs/PAKETIERUNG.md, hosting): DIR/plinius.stamp
# says which channel this is, since when, and which database it vouches for: the SHA-256 of the
# file plinius.db links to (plinius.db.tar.gz, what pacman downloads as plinius.db) and of its
# signature. plinius.stamp.sig is signed with the repo key. Devices (plinius-repo-stamp in
# plinius-software) compare it with the database pacman fetched: a server that hands out an old
# database (devices would never hear of updates again), beta's on the stable path, or a stamp
# without its database is noticed. Run it after the database is in place. Needs the passphrase.
# Usage: write_stamp CHANNEL DIR FINGERPRINT
write_stamp() {
	local stamp=$2/plinius.stamp db sig
	db=$(readlink -f -- "$2/plinius.db") && [[ -f $db ]] || die "write_stamp: no database $2/plinius.db"
	sig=$(readlink -f -- "$2/plinius.db.sig") && [[ -f $sig ]] || die "write_stamp: no signature $2/plinius.db.sig"
	printf 'channel=%s\ntime=%s\ndb=%s\ndb_sha256=%s\nsig_sha256=%s\n' "$1" "$(date +%s)" "${db##*/}" \
		"$(sha256sum < "$db" | cut -d' ' -f1)" "$(sha256sum < "$sig" | cut -d' ' -f1)" > "$stamp.part"
	rm -f "$stamp.sig.part"
	GNUPGHOME=$SIGN_GNUPG gpg --batch --yes --quiet --local-user "$3" --detach-sign --no-armor \
		-o "$stamp.sig.part" "$stamp.part"
	mv -f "$stamp.sig.part" "$stamp.sig"
	mv -f "$stamp.part" "$stamp"
}

# Prints the fingerprint of the signing key; prints nothing if there is no key
sign_key() {
	[[ -d $SIGN_GNUPG ]] || return 0
	GNUPGHOME=$SIGN_GNUPG gpg --batch --list-secret-keys --with-colons 2>/dev/null |
		awk -F: '$1 == "fpr" { print $10; exit }'
}

# Target architecture: aarch64 (device, default) or x86_64 (fast KVM preview only).
# Each architecture has its own build chroot, local repo and pacman cache.
ARCH=${ARCH:-aarch64}
case $ARCH in
	aarch64)
		CHROOT=$BUILD/chroot/root
		LOCALREPO=$BUILD/repo/local
		PKGCACHE=$BUILD/cache/pacman
		KEYRING=archlinuxarm
		# Directly from ALARM mirrors until our own snapshots exist (docs/PAKETIERUNG.md).
		# Several, because individual mirrors hang now and then; pacman then tries the next one.
		# de3 is the only one with a valid certificate: first, over https. The databases of
		# ALARM are unsigned, over http they could be swapped for old ones (updates held back);
		# the others only as a fallback. Packages are signed either way.
		MIRRORS=${MIRRORS:-"https://de3.mirror.archlinuxarm.org http://de.mirror.archlinuxarm.org http://eu.mirror.archlinuxarm.org http://mirror.archlinuxarm.org"}
		MIRROR_PATH='$arch/$repo'
		;;
	x86_64)
		CHROOT=$BUILD/chroot-x86_64/root
		LOCALREPO=$BUILD/repo/local-x86_64
		PKGCACHE=$BUILD/cache/pacman-x86_64
		KEYRING=archlinux
		MIRRORS=${MIRRORS:-"https://geo.mirror.pkgbuild.com https://mirror.rackspace.com/archlinux https://ftp.fau.de/archlinux"}
		MIRROR_PATH='$repo/os/$arch'
		;;
	*)
		echo "Error: ARCH=$ARCH not supported (aarch64, x86_64)" >&2
		exit 1
		;;
esac
MIRROR=${MIRRORS%% *}

# Prints the pacman mirrorlist for $MIRRORS
mirrorlist() {
	local m
	for m in $MIRRORS; do echo "Server = $m/$MIRROR_PATH"; done
}

# die: print an error and exit; msg: progress message (both to stderr)
die() { echo "Error: $*" >&2; exit 1; }
msg() { echo "==> $*" >&2; }

# Checks that the host can run the build: user namespaces with subordinate IDs and, for
# aarch64, a qemu binfmt handler usable inside a chroot.
check_host() {
	unshare --map-auto --map-root-user true 2>/dev/null ||
		die "user namespaces with --map-auto not possible (check /etc/subuid)"
	# On an aarch64 host (the device itself) the chroot runs natively, without qemu
	[[ $ARCH == aarch64 && $(uname -m) != aarch64 ]] || return 0
	local f=/proc/sys/fs/binfmt_misc/qemu-aarch64
	[[ -r $f ]] || die "qemu-aarch64 binfmt missing (package qemu-user-static-binfmt)"
	# F = interpreter is opened at registration time, otherwise qemu does not work in the chroot
	grep -q '^flags: .*F' "$f" || die "qemu-aarch64 binfmt without F flag"
}

# ns_run SCRIPT [ARGS...]
# Runs a bash script as namespace root (own mount and PID namespace; everything mounted
# disappears with it). ARGS become $1... of the script. The script can use the helpers
# chroot_mounts, install_root, image_first_boot and in_chroot defined below.
ns_run() {
	local script=$1; shift
	mkdir -p "$PKGCACHE"
	PKGCACHE=$PKGCACHE KEYRING=$KEYRING unshare --map-auto --map-root-user --mount --pid --fork --kill-child -- \
		/bin/bash -euo pipefail -c "
			# Prepares \$r as a chroot: dev/proc/sys/tmp, shared pacman cache, host resolv.conf
			chroot_mounts() {
				local r=\$1
				mount --bind \"\$r\" \"\$r\"   # pacman needs / to be a mount point (CheckSpace)
				mount --rbind /dev \"\$r/dev\"
				mount -t proc proc \"\$r/proc\"
				mount --rbind /sys \"\$r/sys\"
				mount -t tmpfs tmpfs \"\$r/tmp\"
				mkdir -p \"\$r/var/cache/pacman/pkg\"
				mount --bind \"\$PKGCACHE\" \"\$r/var/cache/pacman/pkg\"
				rm -f \"\$r/etc/resolv.conf\"
				cp -L /etc/resolv.conf \"\$r/etc/resolv.conf\"
			}
			# Replacement for pacstrap: pacstrap and arch-chroot mount a fresh sysfs,
			# which is not allowed in a user namespace without its own network namespace.
			# Installs into \$r\$t (seen from inside the build chroot \$r: \$t).
			install_root() {
				local r=\$1 t=\$2; shift 2
				mkdir -p \"\$r\$t\"/{dev,proc,sys,tmp,etc,var/lib/pacman,var/cache/pacman/pkg}
				mount --rbind /dev \"\$r\$t/dev\"
				mount -t proc proc \"\$r\$t/proc\"
				mount --rbind /sys \"\$r\$t/sys\"
				mount -t tmpfs tmpfs \"\$r\$t/tmp\"
				# base does not pull in the ALARM keyring (on Arch x86_64 it does, no harm)
				# Use the build chroot's cache (= PKGCACHE) so nothing ends up in the image
				in_chroot \"\$r\" pacman -r \"\$t\" --cachedir /var/cache/pacman/pkg \
					-Sy --noconfirm --needed \"\$KEYRING-keyring\" \"\$@\"
				in_chroot \"\$r\$t\" pacman-key --init
				in_chroot \"\$r\$t\" pacman-key --populate \"\$KEYRING\"
			}
			# Leaves out of the image at \$1/target what each device must create for itself on its
			# first start: the pacman keyring (plinius-pacman-init.service) and the machine ID.
			# systemd writes a new ID into an empty machine-id. Empty, not missing and not
			# \"uninitialized\": those make it a first boot (machine-id(5)), on which systemd enables
			# what the presets name (systemd-resolved, systemd-homed, networkd-wait-online …) and
			# systemd-firstboot asks for time zone and root password on the console.
			image_first_boot() {
				local r=\$1 t=\$1/target
				rm -rf \"\$t/etc/pacman.d/gnupg\"
				install -m644 \"$VOLLA_DIR/image/plinius-pacman-init.service\" \"\$t/etc/systemd/system/\"
				in_chroot \"\$r\" systemctl --root=/target enable plinius-pacman-init.service
				: > \"\$t/etc/machine-id\"
			}
			# Runs a command in chroot \$1 with a clean environment
			in_chroot() {
				local r=\$1; shift
				chroot \"\$r\" /usr/bin/env -i HOME=/root LANG=C.UTF-8 TERM=dumb HTTP_USER_AGENT=\"$USER_AGENT\" \
					PATH=/usr/local/sbin:/usr/local/bin:/usr/bin \"\$@\"
			}
			$script" ns_run "$@"
}

# Deletes directories containing files owned by mapped UIDs (not removable as the host user).
ns_rm() {
	unshare --map-auto --map-root-user rm -rf -- "$@"
}
