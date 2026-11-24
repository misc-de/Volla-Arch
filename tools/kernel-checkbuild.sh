#!/bin/bash
# Check build of a kernel source commit before it becomes a plinius-kernel package, always with
# pkgrel 99, so that its release (<pkgver>-plinius99) can never be taken for a published one.
#
# Without --package: prepare() and build() of pkgbuilds/plinius-kernel on this machine (host
# toolchain, as the cross build in the x86_64 chroot) for any commit of the kernel repository.
# build() compiles with make -k (all errors in one go) and checks every CRC the prebuilt vendor
# modules import. The object tree stays in <out>/src/obj for the layout comparison with an
# earlier build (tools/kernel-kmi.py layout): vmlinux there has the DWARF of all structures.
#
# With --package: the whole package, built by tools/build.sh like a real one, from a copy of
# pkgbuilds/plinius-kernel in <out>/pkg (commit, version, pkgrel 99 and the checksum of the
# commit's tarball filled in) into <out>/repo, a repo of its own: nothing reaches the local repo.
# Such a package goes to the phone only by hand, for a trial start.
#
# Usage: tools/kernel-checkbuild.sh [--package] <rev> [<pkgver>]
#   <rev>     commit of the kernel repository (KERNEL_REPO, default build/kernel-trial)
#   <pkgver>  default: VERSION.PATCHLEVEL.SUBLEVEL of its Makefile
# OUT (default build/kernel-check/<pkgver>) is replaced on each run.
# Example: tools/kernel-checkbuild.sh plinius-6.1-193
set -euo pipefail
. "$(dirname "$0")/lib.sh"

package=0
[[ ${1:-} == --package ]] && { package=1; shift; }
rev=${1:?usage: tools/kernel-checkbuild.sh [--package] <rev> [<pkgver>]}
repo=${KERNEL_REPO:-$BUILD/kernel-trial}
pkg=$PKGBUILDS/plinius-kernel
commit=$(git -C "$repo" rev-parse --verify "$rev^{commit}") || die "no commit $rev in $repo"
if [[ -n ${2:-} ]]; then
	pkgver=$2
else
	# awk reads to the end: leaving early would give git a SIGPIPE, which pipefail reports
	pkgver=$(git -C "$repo" show "$commit:Makefile" |
		awk -F' = ' '$1 == "VERSION" && !v { v = $2 } $1 == "PATCHLEVEL" && !p { p = $2 }
			$1 == "SUBLEVEL" && !s { s = $2 } END { print v "." p "." s }')
fi
[[ $pkgver =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "no kernel version: $pkgver"
# Absolute: build.sh mounts it inside its namespace
out=$(realpath -m "${OUT:-$BUILD/kernel-check/$pkgver}")
src=$out/src

msg "Check build of $commit ($pkgver-plinius99) in $out"
rm -rf "$out"

if ((package)); then
	mkdir -p "$out/pkg" "$out/repo"
	find "$pkg" -maxdepth 1 -type f ! -name '*.tar.gz' -exec cp -t "$out/pkg" {} +
	# Packed as tools/kernel-source.sh does (the same bytes for the same commit)
	tarball=$out/pkg/plinius-kernel-$commit.tar.gz
	git -C "$repo" archive --format=tar --prefix="plinius-kernel-$commit/" "$commit" | gzip -n > "$tarball"
	sum=$(sha256sum "$tarball" | cut -d' ' -f1)
	# The tarball is the first source, so its checksum is the first one
	sed -i -E -e "s/^_kcommit=.*/_kcommit=$commit/" -e "s/^pkgver=.*/pkgver=$pkgver/" \
		-e 's/^pkgrel=.*/pkgrel=99/' -e "s/^(sha256sums=\(')[0-9a-f]+'/\1$sum'/" "$out/pkg/PKGBUILD"
	PKGSRC=$out/pkg PKGOUT=$out/repo "$VOLLA_DIR/tools/build.sh" plinius-kernel 2>&1 | tee "$out/build.log"
	msg "Done: package in $out/repo (log: $out/build.log)"
	exit 0
fi

mkdir -p "$src"
# The files of the package next to the tree, as makepkg would link them into $srcdir
find "$pkg" -maxdepth 1 -type f ! -name '*.tar.gz' ! -name PKGBUILD -exec cp -t "$src" {} +
git -C "$repo" archive --format=tar --prefix="plinius-kernel-$commit/" "$commit" | tar -xf - -C "$src"

# prepare() and build() of the PKGBUILD itself, with the commit, version and pkgrel of this check
(
	cd "$src"
	# The PKGBUILD sets pkgver and _kcommit itself: keep this check's values apart
	want_ver=$pkgver
	# shellcheck source=/dev/null
	. "$pkg/PKGBUILD"
	_kcommit=$commit pkgver=$want_ver pkgrel=99
	_krelease=$pkgver-plinius$pkgrel
	srcdir=$src
	prepare
	build
) 2>&1 | tee "$out/build.log"
msg "Done: $out (log: $out/build.log)"
