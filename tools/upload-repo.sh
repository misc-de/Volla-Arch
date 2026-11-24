#!/bin/bash
# Uploads what tools/publish.sh and tools/release.sh prepared to the web server
# (docs/PAKETIERUNG.md, hosting): first the channel beta (<path>/beta/<arch>), then stable
# (<path>/<arch>). Per channel the packages go first, the database last, and old packages leave
# only after that: a device downloading at that moment always finds every package its database
# names. A package one channel already has on the server is hard-linked there, not sent again.
# Database, its signature and the stamp (plinius.stamp, which names the database) go up with
# --delay-updates: each lands under a temporary name and all are renamed at the end, so they
# switch over together and a device sees a database without its stamp only for a moment (it
# fetches both again before it warns, plinius-repo-stamp).
#
# Usage: REPO_TARGET=user@host:/path/to/repo [REPO_SSH_PORT=22] [ARCH=...] tools/upload-repo.sh
set -euo pipefail
. "$(dirname "$0")/lib.sh"

target=${REPO_TARGET:?set REPO_TARGET=user@host:/path/to/repo}
ssh_cmd="ssh -p ${REPO_SSH_PORT:-22} -o BatchMode=yes"

# Readable by the web server, whichever user it runs as; owners of this machine mean nothing there
options=(-rltz --mkpath --no-owner --no-group --chmod=D755,F644 --partial-dir=.upload -e "$ssh_cmd")
db=(--exclude='plinius.db*' --exclude='plinius.files*' --exclude='plinius.stamp*')

# upload CHANNEL LINK-DEST (relative to the channel's directory on the server)
upload() {
	local out dest
	out=$(channel_dir "$1" "$PUBREPO")
	dest=$(channel_dir "$1" "$target")
	if [[ ! -e $out/plinius.db ]]; then
		msg "$1: nothing published in $out, skipped"
		return 0
	fi
	msg "$1: packages to $dest"
	rsync "${options[@]}" --link-dest="$2" "${db[@]}" "$out/" "$dest/"
	msg "$1: database and stamp"
	rsync "${options[@]}" --delay-updates "$out/" "$dest/"
	msg "$1: removing what the database no longer names"
	rsync "${options[@]}" --delete-after "$out/" "$dest/"
	uploaded=1
}

uploaded=
upload beta "../../$ARCH"
upload stable "../beta/$ARCH"
[[ -n $uploaded ]] || die "nothing published in $PUBREPO, run tools/publish.sh first"
msg "Done"
