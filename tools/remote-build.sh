#!/bin/bash
# Builds packages on a second build host with the same tools and its own build chroot, e.g.
# while this machine is busy with other builds. Sends the working tree (uncommitted PKGBUILDs
# included) and the local repo there, runs tools/build.sh at the lowest CPU and I/O priority
# (other work on that host comes first) and takes the new packages back into the local repo.
#
# The host needs uidmap, bsdtar (libarchive-tools) and for aarch64 a qemu binfmt handler with
# the F flag (Debian: qemu-user-binfmt); --init then creates its build chroot.
#
# Usage: REMOTE=user@host [REMOTE_SSH_PORT=22] [REMOTE_DIR=volla] [ARCH=...]
#        tools/remote-build.sh [--init] [<package>...]
set -euo pipefail
. "$(dirname "$0")/lib.sh"

remote=${REMOTE:?set REMOTE=user@host}
dir=${REMOTE_DIR:-volla}
# Further ssh options, e.g. REMOTE_SSH_OPTS="-o UserKnownHostsFile=..." (split at spaces)
read -ra extra <<< "${REMOTE_SSH_OPTS:-}"
ssh=(ssh -p "${REMOTE_SSH_PORT:-22}" -o BatchMode=yes -o ServerAliveInterval=30 "${extra[@]}")
rsync_ssh=${ssh[*]}
# Lowest priority: services on the host take the CPU and the disk first
nice=(nice -n 19 ionice -c 3)

init=0
[[ ${1:-} == --init ]] && { init=1; shift; }
(( init || $# )) || die "usage: tools/remote-build.sh [--init] [<package>...]"

# Local repo relative to the repository, the same path on the host
repo_rel=${LOCALREPO#"$VOLLA_DIR"/}

msg "Sending the working tree to $remote:$dir"
# docs/ and site/ are kept local only (each has its own repository) and no build needs them
rsync -a --delete --exclude=/build/ --exclude=/.git/ --exclude=/docs/ --exclude=/site/ --exclude=__pycache__ \
	-e "$rsync_ssh" "$VOLLA_DIR/" "$remote:$dir/"
# Own packages other builds depend on; the host's database is replaced by ours
mkdir -p "$LOCALREPO"
"${ssh[@]}" "$remote" "mkdir -p $(printf %q "$dir/$repo_rel")"
rsync -a --delete -e "$rsync_ssh" "$LOCALREPO/" "$remote:$dir/$repo_rel/"

# Debian keeps chroot in /usr/sbin, which is not in a user's PATH there
cmd="cd $(printf %q "$dir") && export ARCH=$(printf %q "$ARCH") PATH=\$PATH:/usr/sbin:/sbin"
(( init )) && cmd+=" && ${nice[*]} tools/chroot-init.sh"
for pkg in "$@"; do
	cmd+=" && ${nice[*]} tools/build.sh $(printf %q "$pkg")"
done
msg "Building on $remote: ${*:-(chroot only)}"
status=0
"${ssh[@]}" "$remote" "$cmd" || status=$?

# New packages back, also after a failure (earlier packages of the list may be done). --update
# keeps packages built here in the meantime; the databases stay ours, repo-add updates them.
new=$(rsync -a --update --out-format=%n --exclude='plinius-local.*' --exclude='.lock' \
	--include='*.pkg.tar.*' --exclude='*' -e "$rsync_ssh" "$remote:$dir/$repo_rel/" "$LOCALREPO/")
exec 9> "$LOCALREPO/.lock"
flock 9
for f in $new; do
	# rsync also names the directory itself (./)
	[[ $f == *.pkg.tar.* && $f != *.sig ]] || continue
	repo-add -q "$LOCALREPO/plinius-local.db.tar.gz" "$LOCALREPO/$f"
	echo "  -> $f"
done
exit "$status"
