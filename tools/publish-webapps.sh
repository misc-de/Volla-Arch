#!/bin/bash
# Publishes the catalog of web apps (pkgbuilds/plinius-webapps/catalog.json) as the catalog that
# Settings > Apps > Web Apps > Add > Search fetches, so that a new entry needs no package update.
# The published copy carries its publication time ("updated": the phone refuses a catalog older
# than the one it keeps) and a detached signature made with the repository key (tools/keygen.sh),
# which the phone checks against plinius-keyring, as for the DOS Installer's list of games.
# The icons (webapps-icons.tar, tools/webapps-icons.py) go to icons/<id>.png next to it, each
# named in the catalog by its SHA-256 ("icon"): a phone takes an icon only with that checksum.
# The readable list on the website (webapps.html) comes from the same file with site/publish.sh.
#
# Usage: WEBAPPS_TARGET=user@host:/path/to/webapps [WEBAPPS_SSH_PORT=22]
#        [WEBAPPS_SSH_OPTS="-o ..."] tools/publish-webapps.sh
set -euo pipefail
. "$(dirname "$0")/lib.sh"

target=${WEBAPPS_TARGET:?set WEBAPPS_TARGET=user@host:/path/to/webapps}
src=$PKGBUILDS/plinius-webapps/catalog.json
icons=$PKGBUILDS/plinius-webapps/webapps-icons.tar
helper=$PKGBUILDS/plinius-webapps/plinius-webapps-catalog
keyring=$PKGBUILDS/plinius-keyring/plinius.gpg
out=$BUILD/webapps
fpr=$(sign_key)
[[ -n $fpr ]] || die "no signing key in $SIGN_GNUPG, run tools/keygen.sh first"
grep -q "^$fpr:" "$PKGBUILDS/plinius-keyring/plinius-trusted" ||
	die "plinius-keyring does not contain the key $fpr"

mkdir -p "$out"
tmp=$(mktemp -d)
trap 'command rm -rf "$tmp"' EXIT

msg "Checking and stamping $src"
# The checks the phone makes, strictly: an entry the phone would leave out stops the run
python3 -I "$helper" --check "$src" || die "the catalog has errors"
mkdir "$tmp/icons-src"
tar -xf "$icons" -C "$tmp/icons-src"
python3 -I - "$src" "$tmp/catalog.json" "$tmp/icons-src/icons" <<'PY'
import hashlib, json, sys, time
from pathlib import Path
with open(sys.argv[1], encoding="utf-8") as f:
    data = json.load(f)
data["updated"] = int(time.time())
for app in data["apps"]:
    icon = Path(sys.argv[3]) / f"{app['id']}.png"
    if icon.is_file():
        app["icon"] = hashlib.sha256(icon.read_bytes()).hexdigest()
with open(sys.argv[2], "w", encoding="utf-8") as f:
    json.dump(data, f, indent=1, ensure_ascii=False)
    f.write("\n")
PY
python3 -I "$helper" --check "$tmp/catalog.json" >/dev/null

msg "Signing with $fpr"
GNUPGHOME=$SIGN_GNUPG gpg --batch --yes --quiet --local-user "$fpr" --detach-sign --no-armor \
	-o "$tmp/catalog.json.sig" "$tmp/catalog.json"
# The same check the phone makes: gpgv with nothing but the key from plinius-keyring
mkdir -m 700 "$tmp/home"
gpg --batch --quiet --homedir "$tmp/home" --dearmor -o "$tmp/keyring.gpg" "$keyring"
status=$(gpgv --homedir "$tmp/home" --status-fd 1 --keyring "$tmp/keyring.gpg" \
	"$tmp/catalog.json.sig" "$tmp/catalog.json" 2>/dev/null) || true
[[ $status == *"VALIDSIG $fpr"* ]] || die "the signature does not verify against plinius-keyring"
install -m644 "$tmp/catalog.json" "$tmp/catalog.json.sig" "$out/"
command rm -rf "$out/icons"
mkdir -p "$out/icons"
install -m644 "$tmp/icons-src/icons/"*.png "$out/icons/"

msg "Uploading to $target"
host=${target%%:*}
dir=${target#*:}
ssh_cmd="ssh -p ${WEBAPPS_SSH_PORT:-22} -o BatchMode=yes ${WEBAPPS_SSH_OPTS:-}"
$ssh_cmd "$host" "mkdir -p $(printf %q "$dir/icons")"
# The icons first: a catalog never names an icon the server does not have yet
rsync -tz --no-owner --no-group --chmod=F644 --delay-updates -e "$ssh_cmd" \
	"$out/icons/"*.png "$target/icons/"
# Both files are put in place together at the end; a phone that still catches the old
# signature with the new catalog refuses the pair and uses the catalog it has
rsync -tz --no-owner --no-group --chmod=F644 --delay-updates -e "$ssh_cmd" \
	"$out/catalog.json" "$out/catalog.json.sig" "$target/"
msg "Done"
