#!/bin/bash
# Builds pkgbuilds/<package> in a fresh overlay copy of the build chroot.
# The result goes into the local repo build/repo/local ([plinius-local]), which later
# builds use as the source for our own dependencies.
#
# Usage: [ARCH=x86_64] tools/build.sh <package>
#
# A PKGBUILD with the line "# plinius-build: cross" is built for aarch64 in the x86_64 chroot
# (CARCH=aarch64, the tools are x86_64 ones). For packages whose build is a cross build anyway,
# like the kernel: under qemu-user it would take many hours instead of minutes. [plinius-local]
# is then not used as a package source, its aarch64 packages would not install there.
set -euo pipefail
. "$(dirname "$0")/lib.sh"

pkg=${1:?usage: tools/build.sh <package>}
src=$PKGBUILDS/$pkg
[[ -f $src/PKGBUILD ]] || die "no PKGBUILD in $src"
cross=0
if [[ $ARCH == aarch64 ]] && grep -qx '# plinius-build: cross' "$src/PKGBUILD"; then
	cross=1
	CHROOT=$BUILD/chroot-x86_64/root
	PKGCACHE=$BUILD/cache/pacman-x86_64
fi
[[ -d $CHROOT/usr ]] || die "build chroot missing, run tools/chroot-init.sh first"
check_host

mkdir -p "$LOCALREPO" "$SRCCACHE"
work=$(mktemp -d "$BUILD/work-$pkg.XXXXXX")
trap 'ns_rm "$work"' EXIT
mkdir -p "$work"/{upper,ovl,merged}

msg "Building $pkg"
ns_run '
	lower=$1 work=$2 src=$3 repo=$4 jobs=$5 arch=$6 srccache=$7 cross=$8
	r=$work/merged
	# userxattr: without it, rmdir on a directory of the lower layer fails with EIO in an unprivileged
	# overlay (kernel 7.2), and pacman leaves half-removed entries in its database
	mount -t overlay overlay -o "userxattr,lowerdir=$lower,upperdir=$work/upper,workdir=$work/ovl" "$r"
	chroot_mounts "$r"

	mkdir -p "$r/repo" "$r/build"
	mount --bind "$repo" "$r/repo"
	if [[ ! -e $repo/plinius-local.db ]]; then
		bsdtar -czf "$repo/plinius-local.db.tar.gz" -T /dev/null
		ln -sf plinius-local.db.tar.gz "$repo/plinius-local.db"
	fi
	# Local repo before the ALARM repos, as [plinius] will be on the device later.
	# Not for a cross build: the x86_64 chroot cannot install its aarch64 packages.
	if [[ $cross == 0 ]]; then
		sed -i "0,/^\[core\]/s//[plinius-local]\nSigLevel = Optional TrustAll\nServer = file:\/\/\/repo\n\n[core]/" \
			"$r/etc/pacman.conf"
	else
		echo "CARCH=\"$arch\"" >> "$r/etc/makepkg.conf"
	fi
	echo "MAKEFLAGS=\"-j$jobs\"" >> "$r/etc/makepkg.conf"
	mkdir -p "$r/build/sources"
	mount --bind "$srccache" "$r/build/sources"
	echo "SRCDEST=/build/sources" >> "$r/etc/makepkg.conf"

	# -L: own packages link LICENSE to the one at the top of the repository
	cp -rL "$src" "$r/build/pkg"
	in_chroot "$r" chown -R builder:builder /build
	# Full upgrade to the current mirror state, not just the dependencies: -Sy followed by -S
	# only upgrades the named packages, the rest (glibc, glib2 ...) would stay at the state of
	# the last chroot-init.sh. tools/freigabe.py checks via .BUILDINFO that we built against it.
	in_chroot "$r" pacman -Syu --noconfirm >/dev/null
	# sudo does not work under qemu-user (setuid needs the binfmt C flag), so namespace root
	# installs the dependencies from .SRCINFO instead of makepkg --syncdeps.
	srcinfo=$(in_chroot "$r" su builder -c "cd /build/pkg && makepkg --printsrcinfo")
	# Split packages depend on each other; they only come into existence in this build
	own=$(sed -n -E "s/^pkgname = //p" <<< "$srcinfo")
	# Drop our own split packages also when versioned (foo=1.0, foo>=1.0)
	# A cross build needs only its build tools; the runtime dependencies are aarch64 packages
	kinds="(make|check)?"
	[[ $cross == 0 ]] || kinds="(make|check)"
	deps=$(sed -n -E "s/^\t${kinds}depends(_$arch)? = //p" <<< "$srcinfo" |
		while read -r d; do grep -qxF "${d%%[<>=]*}" <<< "$own" || echo "$d"; done)
	[[ -z $deps ]] || in_chroot "$r" pacman -S --noconfirm --needed --asdeps $deps
	# makepkg would miss the aarch64 runtime dependencies of a cross build; its tools are there
	nodeps=
	[[ $cross == 0 ]] || nodeps=--nodeps
	in_chroot "$r" su builder -c "cd /build/pkg && makepkg --noconfirm --noprogressbar $nodeps"

	# Several builds may run at once: serialize repo-add
	exec 9> "$repo/.lock"
	flock 9
	for f in "$r"/build/pkg/*.pkg.tar.*; do
		[[ $f == *.sig ]] && continue
		cp "$f" "$repo/"
		in_chroot "$r" repo-add -q /repo/plinius-local.db.tar.gz "/repo/${f##*/}"
		echo "  -> ${f##*/}"
	done
' "$CHROOT" "$work" "$src" "$LOCALREPO" "$(build_jobs)" "$ARCH" "$SRCCACHE" "$cross"

msg "Done: $pkg (repo: $LOCALREPO)"
# Coupled libraries may have changed along with this package
ARCH=$ARCH "$VOLLA_DIR/tools/freigabe.py" --check ||
	msg "The approval (plinius-freigabe) no longer matches the packages; run tools/freigabe.py after testing"
