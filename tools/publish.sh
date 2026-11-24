#!/bin/bash
# Publishes the current state of the local repo as the signed repo [plinius] (docs/PAKETIERUNG.md,
# hosting), into the channel beta: build/repo/plinius/beta/<arch>/. After testing on a phone,
# tools/release.sh takes that state over to stable (build/repo/plinius/<arch>/), then
# tools/upload-repo.sh brings both to the web server. Devices get stable configured via
# image/mkimage.sh -r; beta is a switch under Settings → Apps → Settings.
#
# Usage: [ARCH=x86_64] tools/publish.sh [--stable] [--force]
#   --stable  release the new state to stable right away (tools/release.sh), for urgent fixes
#   --force   publish even if the approval does not match the repo (tools/freigabe.py)
#
# All packages and the database are signed with the key from tools/keygen.sh. Packages that
# already have a valid signature are not signed again; one unchanged since stable keeps the
# signature from there.
set -euo pipefail
. "$(dirname "$0")/lib.sh"

force= stable=
for a; do
	case $a in
		--force) force=1 ;;
		--stable) stable=1 ;;
		*) die "usage: tools/publish.sh [--stable] [--force]" ;;
	esac
done

export GNUPGHOME=$SIGN_GNUPG
fpr=$(sign_key)
[[ -n $fpr ]] || die "no signing key in $SIGN_GNUPG, run tools/keygen.sh first"
db=$LOCALREPO/plinius-local.db.tar.gz
[[ -e $db ]] || die "local repo empty ($LOCALREPO)"

# Files of the current state: only what the database lists (older versions remain on disk)
mapfile -t files < <(bsdtar -xOf "$db" '*/desc' | awk '/^%FILENAME%$/ { getline; print }' | sort)
((${#files[@]})) || die "no packages in $db"

# Devices verify against plinius-keyring: without a package carrying this key no device can
# verify the signatures, and an outdated key in the package locks them out
list=$(printf '%s\n' "${files[@]}")
keyring=$(grep '^plinius-keyring-' <<< "$list" || true)
[[ -n $keyring ]] || die "plinius-keyring missing in the local repo, run tools/build.sh plinius-keyring first"
trusted=$(bsdtar -xOf "$LOCALREPO/$keyring" usr/share/pacman/keyrings/plinius-trusted)
grep -q "^$fpr:" <<< "$trusted" ||
	die "$keyring does not contain the key $fpr, run tools/keygen.sh and tools/build.sh plinius-keyring"

if ! ARCH=$ARCH "$VOLLA_DIR/tools/freigabe.py" --check; then
	[[ -n $force ]] || die "approval does not match the repo (tools/freigabe.py), or use --force"
	msg "approval does not match, publishing anyway (--force)"
fi

out=$(channel_dir beta "$PUBREPO")
released=$(channel_dir stable "$PUBREPO")
mkdir -p "$out"
# Copy changed packages (via .part, so no half-written file is ever served; a stale .sig goes
# with the old file), then sign every package without a valid signature. A package that stable
# already has with a valid signature is linked from there instead (same file system).
for f in "${files[@]}"; do
	if ! cmp -s "$LOCALREPO/$f" "$out/$f"; then
		if cmp -s "$LOCALREPO/$f" "$released/$f" &&
			gpg --batch --quiet --verify "$released/$f.sig" "$released/$f" 2>/dev/null; then
			ln -f "$released/$f" "$out/$f.part"
			ln -f "$released/$f.sig" "$out/$f.sig.part"
			mv -f "$out/$f.part" "$out/$f"
			mv -f "$out/$f.sig.part" "$out/$f.sig"
		else
			cp "$LOCALREPO/$f" "$out/$f.part"
			mv -f "$out/$f.part" "$out/$f"
			rm -f "$out/$f.sig"
		fi
	fi
	if ! gpg --batch --quiet --verify "$out/$f.sig" "$out/$f" 2>/dev/null; then
		msg "Signing $f"
		# Never write into the old file: stable may share it (hard link)
		rm -f "$out/$f.sig"
		gpg --batch --yes --quiet --local-user "$fpr" --detach-sign --no-armor \
			-o "$out/$f.sig" "$out/$f"
	fi
done

# Rebuild the database every time: it contains exactly this state. Build it alongside, then swap;
# a device downloading at that moment sees either the old or the new state.
tmp=$(mktemp -d "$out/.db.XXXXXX")
trap 'rm -f "$tmp"/*; rmdir "$tmp"' EXIT
(cd "$out" && repo-add -q --sign --key "$fpr" --verify "$tmp/plinius.db.tar.gz" "${files[@]}")
for f in plinius.db.tar.gz plinius.db.tar.gz.sig plinius.files.tar.gz plinius.files.tar.gz.sig; do
	mv -f "$tmp/$f" "$out/$f"
done
ln -sfn plinius.db.tar.gz "$out/plinius.db"
ln -sfn plinius.db.tar.gz.sig "$out/plinius.db.sig"
ln -sfn plinius.files.tar.gz "$out/plinius.files"
ln -sfn plinius.files.tar.gz.sig "$out/plinius.files.sig"
write_stamp beta "$out" "$fpr"

# Remove packages the new state no longer lists
for f in "$out"/*.pkg.tar.*; do
	n=${f##*/}
	n=${n%.sig}
	grep -qxF -- "$n" <<< "$list" || rm -f -- "$f"
done

msg "Published to beta: ${#files[@]} packages in $out (key $fpr)"
if [[ -n $stable ]]; then
	"$VOLLA_DIR/tools/release.sh"
else
	msg "Next: tools/upload-repo.sh; after testing tools/release.sh and tools/upload-repo.sh again"
fi
