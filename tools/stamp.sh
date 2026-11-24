#!/bin/bash
# Signs the freshness stamps of the published channels anew (plinius.stamp, see write_stamp in
# lib.sh), without publishing anything: devices warn under Updates once the stamp of their
# channel is older than 30 days, as a server handing out an old state would look. Each stamp
# names the local database of its channel; tools/upload-repo.sh brings both to the server
# together. Run it when nothing was published for a while, then tools/upload-repo.sh.
#
# Usage: [ARCH=x86_64] tools/stamp.sh
set -euo pipefail
. "$(dirname "$0")/lib.sh"

fpr=$(sign_key)
[[ -n $fpr ]] || die "no signing key in $SIGN_GNUPG, run tools/keygen.sh first"
stamped=
for channel in beta stable; do
	out=$(channel_dir "$channel" "$PUBREPO")
	[[ -e $out/plinius.db ]] || continue
	write_stamp "$channel" "$out" "$fpr"
	msg "$channel: stamped ($(cat "$out/plinius.stamp" | tr '\n' ' '))"
	stamped=1
done
[[ -n $stamped ]] || die "nothing published in $PUBREPO, run tools/publish.sh first"
msg "Next: tools/upload-repo.sh"
