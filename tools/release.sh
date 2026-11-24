#!/bin/bash
# Takes the published beta state of [plinius] over to stable (docs/PAKETIERUNG.md, hosting):
# the same signed packages and the same signed database, so no package is signed again. Only the
# freshness stamp of stable (plinius.stamp, see write_stamp in lib.sh) is signed anew, which
# takes the passphrase. Run after the beta state has been tested on a phone, then
# tools/upload-repo.sh.
#
# Usage: [ARCH=x86_64] [REPO_URL=https://host/repo] tools/release.sh [--dry-run]
#   REPO_URL   compare first: the beta database on the server must be the local one, else the
#              tested state is not the one being released
#   --dry-run  only list what changes in stable
set -euo pipefail
. "$(dirname "$0")/lib.sh"

dry=
case ${1:-} in
	--dry-run) dry=1 ;;
	"") ;;
	*) die "usage: tools/release.sh [--dry-run]" ;;
esac

src=$(channel_dir beta "$PUBREPO")
out=$(channel_dir stable "$PUBREPO")
[[ -e $src/plinius.db.tar.gz ]] || die "nothing published to beta in $src, run tools/publish.sh first"

if [[ -n ${REPO_URL:-} ]]; then
	url=$(channel_dir beta "${REPO_URL%/}")/plinius.db.tar.gz
	remote=$(curl -fsS --max-time 60 "$url" | sha256sum | cut -d' ' -f1) || die "cannot fetch $url"
	[[ $remote == $(sha256sum < "$src/plinius.db.tar.gz" | cut -d' ' -f1) ]] ||
		die "the beta database on the server differs from $src: release only what was tested"
fi

# Everything must already carry a valid signature: devices accept nothing else (SigLevel = Required)
export GNUPGHOME=$SIGN_GNUPG
gpg --batch --quiet --verify "$src/plinius.db.tar.gz.sig" "$src/plinius.db.tar.gz" 2>/dev/null ||
	die "beta database without a valid signature, run tools/publish.sh again"
mapfile -t files < <(bsdtar -xOf "$src/plinius.db.tar.gz" '*/desc' | awk '/^%FILENAME%$/ { getline; print }' | sort)
((${#files[@]})) || die "no packages in the beta database"
for f in "${files[@]}"; do
	gpg --batch --quiet --verify "$src/$f.sig" "$src/$f" 2>/dev/null ||
		die "$f in beta without a valid signature, run tools/publish.sh again"
done

# What changes in stable
if [[ -e $out/plinius.db.tar.gz ]]; then
	LC_ALL=C join -a1 -a2 -e - -o 0,1.2,2.2 <(db_versions "$out/plinius.db.tar.gz") <(db_versions "$src/plinius.db.tar.gz") |
		awk '$2 != $3 { print "    " $1 ": " $2 " → " $3; n++ } END { if (!n) print "    (no changes)" }' >&2
else
	msg "stable is new: ${#files[@]} packages"
fi
[[ -z $dry ]] || exit 0

mkdir -p "$out"
# Packages first, each linked in via a temporary name and renamed (atomic), the database after
# them, old packages last: a device downloading at that moment finds every package its database
# names. Hard links, so stable takes no extra space; publish.sh never writes into an existing file.
for f in "${files[@]}"; do
	for g in "$f" "$f.sig"; do
		[[ $src/$g -ef $out/$g ]] && continue
		ln -f "$src/$g" "$out/.$g.part"
		mv -f "$out/.$g.part" "$out/$g"
	done
done
for f in plinius.db.tar.gz plinius.db.tar.gz.sig plinius.files.tar.gz plinius.files.tar.gz.sig; do
	cp "$src/$f" "$out/.$f.part"
	mv -f "$out/.$f.part" "$out/$f"
done
ln -sfn plinius.db.tar.gz "$out/plinius.db"
ln -sfn plinius.db.tar.gz.sig "$out/plinius.db.sig"
ln -sfn plinius.files.tar.gz "$out/plinius.files"
ln -sfn plinius.files.tar.gz.sig "$out/plinius.files.sig"
fpr=$(sign_key)
[[ -n $fpr ]] || die "no signing key in $SIGN_GNUPG for the stamp of stable"
write_stamp stable "$out" "$fpr"

list=$(printf '%s\n' "${files[@]}")
for f in "$out"/*.pkg.tar.*; do
	n=${f##*/}
	n=${n%.sig}
	grep -qxF -- "$n" <<< "$list" || rm -f -- "$f"
done

msg "Released to stable: ${#files[@]} packages in $out; next: tools/upload-repo.sh"
