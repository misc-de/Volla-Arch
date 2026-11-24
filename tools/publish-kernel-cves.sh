#!/bin/bash
# Sets up and feeds the daily kernel vulnerability lists on the project server
# (tools/kernel-cves). The phones fetch their list from
# https://plinius.cais.de/security/kernel/<kernel release>.json (plinius-cve-update).
#
#   install    copies the generator, its service, timer, sysusers and tmpfiles entries to the
#              server and enables the timer
#   manifest   lists the source files the phone's kernel configuration builds
#              (tools/kernel-files.py over the kernel tree), uploads the list for the phone's
#              kernel release and builds the lists once. Release and configuration come from
#              the phone over SSH (/proc/config.gz), or from RELEASE= and CONFIG=.
#              With KERNEL_GIT (a git repository) and KERNEL_REV (default HEAD), it also uploads
#              the commit ids of that kernel and the upstream ids its backports name, so the
#              lists leave out what the tree has fixed ahead of its version (plinius-kernel).
#              KERNEL_MISSING (a file of commit ids) uploads the merged commits whose changes the
#              kernel did not take; their CVEs stay listed.
#   package    the same for a built plinius-kernel package, right after the build, so a phone
#              finds the list of its new kernel release from the first start (every pkgrel is a
#              release of its own). PKGFILE defaults to the package of pkgbuilds/plinius-kernel's
#              version in build/repo/local. Release and configuration come from the package; the
#              tree and the commit ids from the merge history in KERNEL_GIT (default
#              build/kernel-trial): the commit of a plinius-* branch with the same tree as the
#              PKGBUILD's source commit, or KERNEL_REV. KERNEL_MISSING defaults to
#              pkgbuilds/plinius-kernel/not-taken.txt.
#
# Usage: KCVE_TARGET=root@host [KCVE_SSH_PORT=22] [KCVE_SSH_OPTS="-o ..."]
#        [KERNEL_TREE=build/kernel-src] [KERNEL_GIT=build/kernel-trial KERNEL_REV=plinius-6.1]
#        [PLINIUS_SSH="ssh root@10.15.19.82"]
#        tools/publish-kernel-cves.sh install|manifest|package [PKGFILE]
set -euo pipefail
. "$(dirname "$0")/lib.sh"

target=${KCVE_TARGET:?set KCVE_TARGET=root@host}
src=$VOLLA_DIR/tools/kernel-cves
ssh_cmd=(ssh -p "${KCVE_SSH_PORT:-22}" -o BatchMode=yes ${KCVE_SSH_OPTS:-})
remote() { "${ssh_cmd[@]}" "$target" "$@"; }
tmp=$(mktemp -d)
tree_dir=
trap 'command rm -rf "$tmp" ${tree_dir:+"$tree_dir"}' EXIT

install_server() {
	msg "Installing the generator on $target"
	# One tar stream: the files land with the right modes, nothing else on the server changes
	tar -C "$src" -cf "$tmp/files.tar" plinius-kernel-cves plinius-kernel-cves.service \
		plinius-kernel-cves.timer plinius-kernel-cves.sysusers plinius-kernel-cves.tmpfiles
	remote 'set -e
		d=$(mktemp -d); trap "rm -rf $d" EXIT
		tar -C "$d" -xf -
		install -Dm755 "$d/plinius-kernel-cves" /usr/local/lib/plinius-kernel-cves/plinius-kernel-cves
		install -Dm644 "$d/plinius-kernel-cves.service" /etc/systemd/system/plinius-kernel-cves.service
		install -Dm644 "$d/plinius-kernel-cves.timer" /etc/systemd/system/plinius-kernel-cves.timer
		install -Dm644 "$d/plinius-kernel-cves.sysusers" /etc/sysusers.d/plinius-kernel-cves.conf
		install -Dm644 "$d/plinius-kernel-cves.tmpfiles" /etc/tmpfiles.d/plinius-kernel-cves.conf
		systemd-sysusers /etc/sysusers.d/plinius-kernel-cves.conf
		systemd-tmpfiles --create /etc/tmpfiles.d/plinius-kernel-cves.conf
		systemctl daemon-reload
		systemctl enable --now plinius-kernel-cves.timer' < "$tmp/files.tar"
	msg "Done; the first run needs a manifest (tools/publish-kernel-cves.sh manifest)"
}

upload_manifest() {
	local tree=${KERNEL_TREE:-$BUILD/kernel-src} release=${RELEASE:-} config=${CONFIG:-}
	if [[ -z $release || -z $config ]]; then
		local phone
		read -ra phone <<<"${PLINIUS_SSH:-ssh root@10.15.19.82}"
		msg "Reading kernel release and configuration from the phone"
		release=$("${phone[@]}" uname -r)
		"${phone[@]}" 'zcat /proc/config.gz' >"$tmp/config"
		config=$tmp/config
	fi
	[[ $release =~ ^[0-9]+\.[0-9]+\.[0-9]+[A-Za-z0-9._+-]*$ ]] || die "unexpected kernel release: $release"
	[[ -s $config ]] || die "no kernel configuration"
	[[ -f $tree/Makefile ]] || die "no kernel tree at $tree"
	# The tree has to be the phone's kernel version, or the Makefiles describe another kernel
	local tree_version
	# Read from the Makefile: no make on the tree (its recipes write into the tree)
	tree_version=$(awk -F' *= *' '$1 == "VERSION" { v = $2 } $1 == "PATCHLEVEL" { p = $2 }
		$1 == "SUBLEVEL" { s = $2; print v "." p "." s; exit }' "$tree/Makefile")
	[[ ${release%%-*} == "$tree_version" ]] ||
		die "kernel tree is $tree_version, the phone runs ${release%%-*}"
	msg "Listing the sources built for $release"
	python3 -I "$VOLLA_DIR/tools/kernel-files.py" "$tree" "$config" >"$tmp/$release.files"
	if [[ -n ${KERNEL_GIT:-} ]]; then
		local rev=${KERNEL_REV:-HEAD} tree_rev_version
		# awk reads to the end: an early exit would hand git a SIGPIPE, and pipefail the script
		tree_rev_version=$(git -C "$KERNEL_GIT" show "$rev:Makefile" | awk -F' *= *' '$1 == "VERSION" && !v { v = $2 }
			$1 == "PATCHLEVEL" && !p { p = $2 } $1 == "SUBLEVEL" && !s { s = $2 } END { print v "." p "." s }')
		[[ ${release%%-*} == "$tree_rev_version" ]] ||
			die "$KERNEL_GIT $rev is $tree_rev_version, the phone runs ${release%%-*}"
		msg "Listing the commits of $rev"
		{
			git -C "$KERNEL_GIT" rev-list "$rev"
			# Over the whole log, since a reference may wrap ("ported from linux-6.12.y commit"
			# at the end of one line, the id on the next); "ported from" names our own ports
			git -C "$KERNEL_GIT" log --format=%B "$rev" | perl -0777 -ne '
				while (/commit\s+([0-9a-f]{40})\s+upstream|Upstream\s+commit\s+([0-9a-f]{40})|cherry\s+picked\s+from\s+commit\s+([0-9a-f]{40})|ported\s+from\s+linux-[0-9.]+y\s+commit\s+([0-9a-f]{40})/gi) {
					print grep(defined, $1, $2, $3, $4), "\n";
				}'
		} | sort -u >"$tmp/$release.commits"
		msg "$(wc -l <"$tmp/$release.commits") commit ids"
		remote "umask 022 && f=/etc/plinius-kernel-cves/manifests/$release.commits &&
			cat >\"\$f.new\" && mv \"\$f.new\" \"\$f\"" <"$tmp/$release.commits"
	fi
	if [[ -n ${KERNEL_MISSING:-} ]]; then
		msg "$(grep -c . "$KERNEL_MISSING") commits not taken"
		remote "umask 022 && f=/etc/plinius-kernel-cves/manifests/$release.missing &&
			cat >\"\$f.new\" && mv \"\$f.new\" \"\$f\"" <"$KERNEL_MISSING"
	fi
	msg "Uploading the list and building the vulnerability lists (first run clones the CNA repository)"
	# A start while a run is active joins that run, which read the manifests before this one
	# arrived: then the list is still missing or older, and a second run builds it
	remote "umask 022 && f=/etc/plinius-kernel-cves/manifests/$release.files &&
		cat >\"\$f.new\" && mv \"\$f.new\" \"\$f\" &&
		systemctl start plinius-kernel-cves.service &&
		{ [ /var/www/plinius/security/kernel/$release.json -nt \"\$f\" ] ||
			systemctl start plinius-kernel-cves.service; } &&
		journalctl -u plinius-kernel-cves.service -n 200 --no-pager -o cat | grep -F \" $release: \" | tail -n 1" <"$tmp/$release.files"
}

from_package() {
	local kdir=$VOLLA_DIR/pkgbuilds/plinius-kernel pkg=${1:-} ver rel pkgver kcommit tree
	ver=$(sed -n 's/^pkgver=//p' "$kdir/PKGBUILD")
	rel=$(sed -n 's/^pkgrel=//p' "$kdir/PKGBUILD")
	if [[ -z $pkg ]]; then
		local f
		for f in "$BUILD/repo/local/plinius-kernel-$ver-$rel-aarch64.pkg.tar."*; do
			[[ -f $f ]] && pkg=$f
		done
	fi
	[[ -n $pkg && -f $pkg ]] || die "no plinius-kernel $ver-$rel package${pkg:+ at $pkg}"
	pkgver=$(bsdtar -xOf "$pkg" .PKGINFO | sed -n 's/^pkgver = //p')
	RELEASE=$(bsdtar -tf "$pkg" | sed -n 's|^usr/lib/plinius/kernel/\([^/]*\)/config$|\1|p')
	[[ $RELEASE =~ ^[0-9.]+-plinius[0-9]+$ && $RELEASE != *$'\n'* ]] ||
		die "$pkg: no single kernel configuration (${RELEASE:-none})"
	CONFIG=$tmp/config
	bsdtar -xOf "$pkg" "usr/lib/plinius/kernel/$RELEASE/config" >"$CONFIG"
	KERNEL_GIT=${KERNEL_GIT:-$BUILD/kernel-trial}
	if [[ -z ${KERNEL_REV:-} ]]; then
		# The PKGBUILD names the source commit only for its own version
		[[ $pkgver == "$ver-$rel" ]] ||
			die "$pkg is $pkgver, the PKGBUILD $ver-$rel: set KERNEL_REV to the kernel's branch commit"
		kcommit=$(sed -n 's/^_kcommit=//p' "$kdir/PKGBUILD")
		tree=$(git -C "$KERNEL_GIT" rev-parse --verify -q "$kcommit^{tree}") ||
			die "$KERNEL_GIT does not have the source commit $kcommit (git fetch github)"
		# The source commit is a snapshot of one commit of the merge history: same tree
		KERNEL_REV=$(git -C "$KERNEL_GIT" log --format='%H %T' -n 5000 --branches='plinius-*' |
			awk -v t="$tree" '$2 == t && !h { h = $1 } END { print h }')
		[[ -n $KERNEL_REV ]] || die "no plinius-* branch commit has the tree of $kcommit"
	fi
	msg "$RELEASE: $(git -C "$KERNEL_GIT" log -1 --format='%h %s' "$KERNEL_REV")"
	KERNEL_MISSING=${KERNEL_MISSING-$kdir/not-taken.txt}
	if [[ -z ${KERNEL_TREE:-} ]]; then
		# On disk, not in /tmp: the tree is about 1.5 GB
		tree_dir=$(mktemp -d -p "$BUILD" kcve-tree.XXXXXX)
		git -C "$KERNEL_GIT" archive "$KERNEL_REV" | tar -x -C "$tree_dir"
		KERNEL_TREE=$tree_dir
	fi
	upload_manifest
}

case ${1:-} in
	install) install_server ;;
	manifest) upload_manifest ;;
	package) from_package "${2:-}" ;;
	*) die "usage: $0 install|manifest|package [PKGFILE]" ;;
esac
