#!/bin/bash
# Packs the kernel source of pkgbuilds/plinius-kernel next to the PKGBUILD, under the name and with
# the top directory of GitHub's archive of github.com/misc-de/plinius-kernel, so makepkg takes it
# instead of downloading (needed while that repository is private; the source cache belongs to the
# build user of the namespaces; .gitignore keeps *.tar.gz out). The commit is the _kcommit of the
# PKGBUILD, from build/kernel-trial (remote "github").
#
# Usage: tools/kernel-source.sh   (KERNEL_REPO overrides build/kernel-trial)
set -euo pipefail
. "$(dirname "$0")/lib.sh"

repo=${KERNEL_REPO:-$BUILD/kernel-trial}
commit=$(sed -n 's/^_kcommit=//p' "$PKGBUILDS/plinius-kernel/PKGBUILD")
[[ -n $commit ]] || die "no _kcommit in pkgbuilds/plinius-kernel/PKGBUILD"
git -C "$repo" cat-file -e "$commit^{commit}" 2>/dev/null || die "commit $commit not in $repo"
out=$PKGBUILDS/plinius-kernel/plinius-kernel-$commit.tar.gz
if [[ -e $out ]]; then
	msg "$out exists"
	exit 0
fi
msg "Packing $commit of $repo"
# Fixed mtime from the commit, so the same commit gives the same tarball
git -C "$repo" archive --format=tar --prefix="plinius-kernel-$commit/" "$commit" | gzip -n > "$out.part"
mv "$out.part" "$out"
msg "Done: $out ($(du -h "$out" | cut -f1))"
