#!/bin/bash
# Fills the local repo build/repo/local ([plinius-local]) with the published, signed packages of
# [plinius] instead of building them all from pkgbuilds/. image/mkimage.sh takes our own packages
# from that repo, so with this an image can be made within an hour instead of a day of builds.
#
# Every file is checked against the repository key in pkgbuilds/plinius-keyring (the same key the
# phones trust), and against more than its signature, as a phone does it (plinius-repo-stamp):
# - the database must be signed, and so must the channel's freshness stamp (tools/lib.sh
#   write_stamp). The stamp must name this channel, be at most 30 days old and carry the SHA-256
#   of exactly this database: an old signed database (no updates from then on) or beta's under
#   the stable path stops the run.
# - each package must be signed, its file name must be the name and version its entry in the
#   database gives, and it must have that entry's SHA-256: an older signed package under the name
#   of the current one is refused. This also holds for a package already in the local repo; one
#   that differs (an older download, a local build of the same version) is downloaded again.
# A file that fails a check stops the run. Afterwards the fetched versions are what
# [plinius-local] offers; a package built locally later with tools/build.sh replaces its entry again.
#
# Usage: [CHANNEL=stable|beta] [REPO_URL=https://plinius.cais.de/repo] tools/repo-fetch.sh
#        [REPO_FETCH_DEST=dir] (default: build/repo/local)
#        [REPO_FETCH_KEYDIR=dir with plinius.gpg and plinius-trusted] (default: pkgbuilds/plinius-keyring)
set -euo pipefail
. "$(dirname "$0")/lib.sh"

channel=${CHANNEL:-stable}
url=$(channel_dir "$channel" "${REPO_URL:-https://plinius.cais.de/repo}")
dest=${REPO_FETCH_DEST:-$LOCALREPO}
keydir=${REPO_FETCH_KEYDIR:-$PKGBUILDS/plinius-keyring}
keyring=$keydir/plinius.gpg
trusted=$keydir/plinius-trusted
revoked=$keydir/plinius-revoked
# As plinius-repo-stamp on the phone: an older stamp is a warning there
MAX_AGE_DAYS=30

for tool in curl gpg gpgv bsdtar repo-add sha256sum; do
	command -v "$tool" >/dev/null || die "$tool missing"
done

tmp=$(mktemp -d)
trap 'command rm -rf "$tmp"' EXIT
# A keyring of its own: nothing of the user's GnuPG setup is read or changed
export GNUPGHOME=$tmp/gnupg
mkdir -m700 "$GNUPGHOME"
gpg --batch --quiet --dearmor -o "$tmp/plinius.kbx" < "$keyring"
fpr=$(gpg --batch --with-colons --show-keys "$keyring" | awk -F: '$1 == "fpr" { print $10; exit }')
grep -q "^$fpr:" "$trusted" || die "the key in $keyring is not the trusted one in $trusted"
[[ ! -f $revoked ]] || ! grep -qi "^$fpr:" "$revoked" || die "the key in $keyring is revoked ($revoked)"

# verify FILE: the detached signature FILE.sig must be made by the trusted repository key
verify() {
	gpgv --status-fd 1 --keyring "$tmp/plinius.kbx" "$1.sig" "$1" 2>/dev/null |
		awk -v fpr="$fpr" '$1 == "[GNUPG:]" && $2 == "VALIDSIG" && ($3 == fpr || $NF == fpr) { ok = 1 }
			END { exit !ok }'
}

# sha256 FILE: prints the SHA-256 of FILE
sha256() {
	local sum
	sum=$(sha256sum < "$1")
	echo "${sum%% *}"
}

# fetch NAME DIR: NAME and NAME.sig from the channel into DIR
fetch() {
	curl -fsSL -A "$USER_AGENT" --retry 5 --retry-all-errors --retry-delay 2 -o "$2/$1" "$url/$1" &&
		curl -fsSL -A "$USER_AGENT" --retry 5 --retry-all-errors --retry-delay 2 -o "$2/$1.sig" "$url/$1.sig"
}

# stamp KEY: the value of KEY in the fetched stamp (the last one, as plinius-repo-stamp reads it)
stamp() {
	awk -v k="$1=" 'index($0, k) == 1 { v = substr($0, length(k) + 1) } END { print v }' "$tmp/plinius.stamp"
}

msg "Reading the $channel database from $url"
db=$tmp/plinius.db.tar.gz
fetch plinius.db.tar.gz "$tmp"
verify "$db" || die "the database is not signed by the repository key"

fetch plinius.stamp "$tmp" || die "the server hands out no stamp (plinius.stamp) for $channel"
verify "$tmp/plinius.stamp" || die "the stamp is not signed by the repository key"
when=$(stamp time) db_sum=$(stamp db_sha256) sig_sum=$(stamp sig_sha256)
[[ $when =~ ^[0-9]{1,12}$ && $sig_sum =~ ^([0-9a-f]{64})?$ ]] || die "the stamp is malformed"
[[ $(stamp channel) == "$channel" ]] ||
	die "the stamp is of channel $(stamp channel), not $channel: the server hands out another channel"
[[ $db_sum =~ ^[0-9a-f]{64}$ ]] || die "the stamp does not name its database (no db_sha256)"
[[ $(sha256 "$db") == "$db_sum" && ( -z $sig_sum || $(sha256 "$db.sig") == "$sig_sum" ) ]] ||
	die "the database does not match the stamp: an old database, or an upload under way (try again in a few minutes)"
days=$(( ($(date +%s) - when) / 86400 ))
((days <= MAX_AGE_DAYS)) ||
	die "the stamp is $days days old (more than $MAX_AGE_DAYS): the server may hand out an old state"
msg "Stamp of $channel: $(date -u -d "@$when" '+%Y-%m-%d %H:%M UTC'), matches the database"

# One line per package: file name|name|version|SHA-256 ('|' is in none of them)
mapfile -t entries < <(bsdtar -xOf "$db" '*/desc' | awk '
	function flush() { if (file != "") print file "|" name "|" version "|" sum; file = name = version = sum = "" }
	/^%FILENAME%$/ { flush(); getline; file = $0; next }
	/^%NAME%$/ { getline; name = $0; next }
	/^%VERSION%$/ { getline; version = $0; next }
	/^%SHA256SUM%$/ { getline; sum = $0; next }
	END { flush() }')
((${#entries[@]})) || die "the database lists no packages"

mkdir -p "$dest"
new=0
files=()
for entry in "${entries[@]}"; do
	IFS='|' read -r f name version sum <<< "$entry"
	[[ $f =~ ^[A-Za-z0-9@._+:-]+$ && $f != *..* ]] || die "unexpected file name in the database: $f"
	[[ $name =~ ^[A-Za-z0-9@._+-]+$ && $version =~ ^[A-Za-z0-9._+:~-]+$ ]] ||
		die "unexpected name or version in the database entry of $f: '$name' '$version'"
	[[ $sum =~ ^[0-9a-f]{64}$ ]] || die "the database gives no SHA-256 for $f"
	# name-version-arch.pkg.tar.*: the file is the package its entry describes
	[[ $f == "$name-$version-"* && ${f#"$name-$version-"} =~ ^[A-Za-z0-9_]+\.pkg\.tar(\.[a-z0-9]+)?$ ]] ||
		die "$f is not the file of $name $version, as its entry in the database says"
	files+=("$f")
	if [[ -s $dest/$f && -s $dest/$f.sig ]] && [[ $(sha256 "$dest/$f") == "$sum" ]] && verify "$dest/$f"; then
		continue
	fi
	if [[ -e $dest/$f ]]; then
		msg "$f in $dest is not the one in the database, downloading it again"
	else
		msg "Downloading $f"
	fi
	# A connection that breaks off can leave a damaged file behind: try again before giving up
	for try in 1 2 3; do
		if ! fetch "$f" "$tmp"; then
			problem="could not be downloaded"
		elif ! verify "$tmp/$f"; then
			problem="is not signed by the repository key"
		elif [[ $(sha256 "$tmp/$f") != "$sum" ]]; then
			problem="is not the package the database lists (its SHA-256 differs)"
		else
			break
		fi
		((try < 3)) || die "$f $problem"
		msg "$f $problem, downloading it again"
	done
	mv "$tmp/$f" "$tmp/$f.sig" "$dest/"
	new=$((new + 1))
done
msg "$new of ${#files[@]} packages downloaded, adding them to $dest"
(cd "$dest" && repo-add --quiet plinius-local.db.tar.gz "${files[@]}")
msg "Done: image/mkimage.sh now takes our packages from $dest"
