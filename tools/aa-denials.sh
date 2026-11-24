#!/bin/bash
# Collects the AppArmor messages of the phone's profiles (plinius-apparmor) from its journal and
# sums them up per profile and access, as the input for completing a profile before it goes to
# enforce. In complain mode the kernel logs apparmor="ALLOWED", in enforce apparmor="DENIED".
#
# Usage: tools/aa-denials.sh [SINCE] [PROFILE]
#   SINCE    journalctl --since value, default "-7days"
#   PROFILE  only this profile (e.g. plinius.cupsd)
# PHONE (default root@10.15.19.82, USB), PHONE_SSH_OPTS for the connection.
#
# Prints, per profile: count, operation, requested access, object (path, capability, network
# family, signal or ptrace peer). Only names and modes, never file contents.
set -euo pipefail

since=${1:--7days}
only=${2:-}
phone=${PHONE:-root@10.15.19.82}
# shellcheck disable=SC2086
ssh ${PHONE_SSH_OPTS:--o HostKeyAlias=10.15.19.82} "$phone" \
	"journalctl -k --since '$since' --no-pager -o cat | grep -E 'apparmor=\"(ALLOWED|DENIED)\"' | grep 'profile=\"plinius\\.' || true" |
	python3 -c '
import re, sys, collections
only = sys.argv[1]
field = lambda k, l: (re.search(k + r"=(\"[^\"]*\"|\S+)", l) or [None, ""])[1].strip("\"")
count = collections.Counter()
for line in sys.stdin:
    prof = field("profile", line)
    if only and prof != only:
        continue
    op = field("operation", line)
    kind = field("apparmor", line)
    mask = field("requested_mask", line) or field("requested", line)
    obj = field("name", line) or field("capname", line) or field("family", line) or field("peer", line) or field("signal", line)
    if field("sock_type", line):
        obj += " " + field("sock_type", line)
    count[(prof, kind, op, mask, obj)] += 1
last = None
for (prof, kind, op, mask, obj), n in sorted(count.items()):
    if prof != last:
        print(f"\n## {prof}")
        last = prof
    print(f"{n:6}  {kind:7} {op:14} {mask:6} {obj}")
if not count:
    print("no AppArmor messages of plinius profiles in this period")
' "$only"
