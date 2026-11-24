#!/bin/bash
# Publishes the vulnerability assessments (pkgbuilds/plinius-security/assessments.json) as the
# central list that every phone fetches once a day (plinius-cve-update), so that a new
# assessment needs no package update. The published copy carries its publication time
# ("updated", phones refuse a list older than the one they keep) and a detached signature made
# with the repository key (tools/keygen.sh), which the phones check against plinius-keyring.
#
# Usage: ASSESSMENTS_TARGET=user@host:/path/to/security [ASSESSMENTS_SSH_PORT=22]
#        [ASSESSMENTS_SSH_OPTS="-o ..."] tools/publish-assessments.sh
set -euo pipefail
. "$(dirname "$0")/lib.sh"

target=${ASSESSMENTS_TARGET:?set ASSESSMENTS_TARGET=user@host:/path/to/security}
src=$PKGBUILDS/plinius-security/assessments.json
keyring=$PKGBUILDS/plinius-keyring/plinius.gpg
out=$BUILD/assessments
fpr=$(sign_key)
[[ -n $fpr ]] || die "no signing key in $SIGN_GNUPG, run tools/keygen.sh first"
# Phones trust only what plinius-keyring lists
grep -q "^$fpr:" "$PKGBUILDS/plinius-keyring/plinius-trusted" ||
	die "plinius-keyring does not contain the key $fpr"

mkdir -p "$out"
tmp=$(mktemp -d)
trap 'command rm -rf "$tmp"' EXIT

msg "Stamping $src"
# Checks the file the way the phones read it and adds the publication time
python3 -I - "$src" "$tmp/assessments.json" <<'EOF'
import json, sys, time
with open(sys.argv[1], encoding="utf-8") as f:
    data = json.load(f)
statuses = {"affected", "mitigated", "not_affected", "fixed"}
bad = [k for k, v in data["assessments"].items() if v.get("status") not in statuses]
bad += [e.get("cve", "?") for e in data.get("extra", []) if e.get("status") not in statuses]
if bad:
    sys.exit(f"invalid status in: {', '.join(bad)}")
data = {"updated": int(time.time()), **data}
with open(sys.argv[2], "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
    f.write("\n")
print(f"{len(data['assessments'])} assessments, {len(data.get('extra', []))} extra issues")
EOF

msg "Signing with $fpr"
GNUPGHOME=$SIGN_GNUPG gpg --batch --yes --quiet --local-user "$fpr" --detach-sign --no-armor \
	-o "$tmp/assessments.json.sig" "$tmp/assessments.json"
# The same check the phones make: gpgv with nothing but the key from plinius-keyring
mkdir -m 700 "$tmp/home"
gpg --batch --quiet --homedir "$tmp/home" --dearmor -o "$tmp/keyring.gpg" "$keyring"
status=$(gpgv --homedir "$tmp/home" --status-fd 1 --keyring "$tmp/keyring.gpg" \
	"$tmp/assessments.json.sig" "$tmp/assessments.json" 2>/dev/null) || true
[[ $status == *"VALIDSIG $fpr"* ]] || die "the signature does not verify against plinius-keyring"
install -m644 "$tmp/assessments.json" "$tmp/assessments.json.sig" "$out/"

msg "Uploading to $target"
host=${target%%:*}
dir=${target#*:}
ssh_cmd="ssh -p ${ASSESSMENTS_SSH_PORT:-22} -o BatchMode=yes ${ASSESSMENTS_SSH_OPTS:-}"
$ssh_cmd "$host" "mkdir -p $(printf %q "$dir")"
# Both files are put in place together at the end; a phone that still catches the old
# signature with the new list refuses the pair and keeps its last list until the next day
rsync -tz --no-owner --no-group --chmod=F644 --delay-updates -e "$ssh_cmd" \
	"$out/assessments.json" "$out/assessments.json.sig" "$target/"
msg "Done"
