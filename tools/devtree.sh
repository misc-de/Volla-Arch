#!/bin/bash
# Persistent build tree of one package for quick tests: unlike tools/build.sh, the overlay
# upper directory (build/devtree/<package>) is kept, so after one full build a changed source
# file only recompiles itself (meson/ninja), seconds to minutes instead of the whole package.
# The result is for trying out on the device only; packages for the repo always come from
# tools/build.sh.
#
# Usage: [ARCH=x86_64] tools/devtree.sh <package> <command> [args]
#   setup             full makepkg run (prepare + build, no package) on the current PKGBUILD
#   cp FILE DEST      copy host FILE to DEST, relative to the package's src/ directory
#   make [TARGET...]  rebuild incrementally: ninja in src/build (or what DEVTREE_BUILD names)
#   sh CMD            run CMD as the build user in src/ (e.g. git diff, ninja -C build -t targets)
#   get PATH OUT      copy PATH (relative to src/) out to host file OUT
#   pkg               package what is built (makepkg --repackage, no compiling) into
#                     build/devtree/<arch>-<package>/out, for installing on the test device
#   rm                delete the tree
set -euo pipefail
. "$(dirname "$0")/lib.sh"

pkg=${1:?usage: tools/devtree.sh <package> setup|cp|make|sh|get|pkg|rm}
cmd=${2:?usage: tools/devtree.sh <package> setup|cp|make|sh|get|pkg|rm}
shift 2
src=$PKGBUILDS/$pkg
[[ -f $src/PKGBUILD ]] || die "no PKGBUILD in $src"
[[ -d $CHROOT/usr ]] || die "build chroot missing, run tools/chroot-init.sh first"
check_host

tree=$BUILD/devtree/$ARCH-$pkg
if [[ $cmd == rm ]]; then
	ns_rm "$tree"
	exit 0
fi
[[ $cmd == setup || -d $tree/upper/build/pkg/src ]] || die "no tree for $pkg, run: tools/devtree.sh $pkg setup"
mkdir -p "$tree"/{upper,ovl,merged} "$LOCALREPO" "$SRCCACHE"

# Host files for cp/get go through /host in the chroot
case $cmd in
	cp) hostdir=$(cd "$(dirname "${1:?cp FILE DEST}")" && pwd) ;;
	get) mkdir -p "$(dirname "${2:?get PATH OUT}")"; hostdir=$(cd "$(dirname "$2")" && pwd) ;;
	*) hostdir=$tree ;;
esac

ns_run '
	lower=$1 tree=$2 src=$3 repo=$4 jobs=$5 srccache=$6 hostdir=$7 cmd=$8
	shift 8
	r=$tree/merged
	mount -t overlay overlay -o "userxattr,lowerdir=$lower,upperdir=$tree/upper,workdir=$tree/ovl" "$r"
	chroot_mounts "$r"
	mkdir -p "$r/repo" "$r/build/sources" "$r/host"
	mount --bind "$repo" "$r/repo"
	mount --bind "$srccache" "$r/build/sources"
	mount --bind "$hostdir" "$r/host"
	as_builder() {
		in_chroot "$r" su builder -c "cd /build/pkg/src && $1"
	}

	case $cmd in
	setup)
		# Same chroot preparation as tools/build.sh, done once
		if ! grep -q "^\[plinius-local\]" "$r/etc/pacman.conf"; then
			sed -i "0,/^\[core\]/s//[plinius-local]\nSigLevel = Optional TrustAll\nServer = file:\/\/\/repo\n\n[core]/" \
				"$r/etc/pacman.conf"
			echo "MAKEFLAGS=\"-j$jobs\"" >> "$r/etc/makepkg.conf"
			echo "SRCDEST=/build/sources" >> "$r/etc/makepkg.conf"
		fi
		rm -rf "$r/build/pkg"
		cp -rL "$src" "$r/build/pkg"
		in_chroot "$r" chown -R builder:builder /build/pkg
		in_chroot "$r" pacman -Syu --noconfirm >/dev/null
		srcinfo=$(in_chroot "$r" su builder -c "cd /build/pkg && makepkg --printsrcinfo")
		own=$(sed -n -E "s/^pkgname = //p" <<< "$srcinfo")
		deps=$(sed -n -E "s/^\t(make|check)?depends(_[a-z0-9_]+)? = //p" <<< "$srcinfo" |
			while read -r d; do grep -qxF "${d%%[<>=]*}" <<< "$own" || echo "$d"; done)
		[[ -z $deps ]] || in_chroot "$r" pacman -S --noconfirm --needed --asdeps $deps
		# --noarchive: build only; src/ stays for incremental rebuilds
		in_chroot "$r" su builder -c "cd /build/pkg && makepkg --noconfirm --noprogressbar --noarchive"
		;;
	cp)
		# makepkg links local sources into src/; replace the link, not its target
		rm -f "$r/build/pkg/src/$2"
		cp "$r/host/${1##*/}" "$r/build/pkg/src/$2"
		in_chroot "$r" chown builder:builder "/build/pkg/src/$2"
		;;
	make)
		as_builder "ninja -C ${DEVTREE_BUILD:-build} $*"
		;;
	sh)
		as_builder "$*"
		;;
	get)
		cp "$r/build/pkg/src/$1" "$r/host/${2##*/}"
		;;
	pkg)
		rm -f "$r"/build/pkg/*.pkg.tar.*
		in_chroot "$r" su builder -c "cd /build/pkg && makepkg --repackage --force --noconfirm --noprogressbar"
		mkdir -p "$r/host/out"
		cp "$r"/build/pkg/*.pkg.tar.* "$r/host/out/"
		ls "$r/host/out"
		;;
	esac
' "$CHROOT" "$tree" "$src" "$LOCALREPO" "$(build_jobs)" "$SRCCACHE" "$hostdir" "$cmd" "$@"
