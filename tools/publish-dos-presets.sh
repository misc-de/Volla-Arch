#!/bin/bash
# Publishes the DOS Installer's list of games (pkgbuilds/plinius-dos-installer/presets.json)
# as the list the app fetches before an installation, so that a new game needs no package
# update. The published copy carries its publication time ("updated": the app refuses a list
# older than the one it keeps) and a detached signature made with the repository key
# (tools/keygen.sh), which the app checks against plinius-keyring, as for the CVE assessments.
#
# Usage: PRESETS_TARGET=user@host:/path/to/dos [PRESETS_SSH_PORT=22]
#        [PRESETS_SSH_OPTS="-o ..."] tools/publish-dos-presets.sh
set -euo pipefail
. "$(dirname "$0")/lib.sh"

target=${PRESETS_TARGET:?set PRESETS_TARGET=user@host:/path/to/dos}
src=$PKGBUILDS/plinius-dos-installer/presets.json
keyring=$PKGBUILDS/plinius-keyring/plinius.gpg
out=$BUILD/dos-presets
fpr=$(sign_key)
[[ -n $fpr ]] || die "no signing key in $SIGN_GNUPG, run tools/keygen.sh first"
grep -q "^$fpr:" "$PKGBUILDS/plinius-keyring/plinius-trusted" ||
	die "plinius-keyring does not contain the key $fpr"

mkdir -p "$out"
tmp=$(mktemp -d)
trap 'command rm -rf "$tmp"' EXIT

msg "Checking and stamping $src"
# The checks the app makes on what a preset asks for, and the publication time
PYTHONPATH=$PKGBUILDS python3 -I - "$src" "$tmp/presets.json" "$PKGBUILDS/plinius-dos-installer" <<'EOF'
import json, sys, time, types
pkg = types.ModuleType("dosinstall")
pkg.__path__ = [sys.argv[3]]
sys.modules["dosinstall"] = pkg
from dosinstall import presets
with open(sys.argv[1], encoding="utf-8") as f:
    data = json.load(f)
if data.get("schema") != presets.SCHEMA:
    sys.exit(f"schema must be {presets.SCHEMA}")
ids = set()
for game in data["games"]:
    gid = game.get("id")
    if not gid or gid in ids:
        sys.exit(f"missing or repeated id: {gid!r}")
    ids.add(gid)
    if not game.get("source") and not game.get("installed"):
        sys.exit(f"{gid}: neither source nor installed files to know it by")
    spec = game.get("install", {})
    for line in spec.get("commands", []) + ([spec["program"]] if spec.get("program") else []):
        presets.check_command(line)
    for d in spec.get("mkdirs", []):
        presets._dos_path(d)
    for path in game.get("files", {}):
        presets._dos_path(path)
    if game.get("program"):
        presets._dos_path(game["program"])
    if presets.dosbox_settings(game.get("dosbox")) != (game.get("dosbox") or {}):
        sys.exit(f"{gid}: DOSBox settings outside the allowed ones")
    if "mouse" in game and not presets.mouse_speed(game["mouse"]):
        sys.exit(f"{gid}: mouse must be [sideways, up and down], each 1 to 100")
data["updated"] = int(time.time())
with open(sys.argv[2], "w", encoding="utf-8") as f:
    json.dump(data, f, indent=1, ensure_ascii=False)
    f.write("\n")
print(f"{len(data['games'])} games")
EOF

msg "Signing with $fpr"
GNUPGHOME=$SIGN_GNUPG gpg --batch --yes --quiet --local-user "$fpr" --detach-sign --no-armor \
	-o "$tmp/presets.json.sig" "$tmp/presets.json"
# The same check the app makes: gpgv with nothing but the key from plinius-keyring
mkdir -m 700 "$tmp/home"
gpg --batch --quiet --homedir "$tmp/home" --dearmor -o "$tmp/keyring.gpg" "$keyring"
status=$(gpgv --homedir "$tmp/home" --status-fd 1 --keyring "$tmp/keyring.gpg" \
	"$tmp/presets.json.sig" "$tmp/presets.json" 2>/dev/null) || true
[[ $status == *"VALIDSIG $fpr"* ]] || die "the signature does not verify against plinius-keyring"
install -m644 "$tmp/presets.json" "$tmp/presets.json.sig" "$out/"

msg "Uploading to $target"
host=${target%%:*}
dir=${target#*:}
ssh_cmd="ssh -p ${PRESETS_SSH_PORT:-22} -o BatchMode=yes ${PRESETS_SSH_OPTS:-}"
$ssh_cmd "$host" "mkdir -p $(printf %q "$dir")"
# Both files are put in place together at the end; an app that still catches the old
# signature with the new list refuses the pair and uses the list it has
rsync -tz --no-owner --no-group --chmod=F644 --delay-updates -e "$ssh_cmd" \
	"$out/presets.json" "$out/presets.json.sig" "$target/"
msg "Done"
