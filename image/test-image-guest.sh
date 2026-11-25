#!/bin/bash
# Runs inside the image booted by image/test-image.sh, as the unit image-check.service (ordered
# after graphical.target), and reports on the serial console in lines "@@IMAGE-CHECK KEY ...",
# which tools/image-boot-report.py reads. test-image.sh puts a line ACTIVE="unit ..." (the units
# that must be active, from image/test-image.expected) in front of this script.
# Reads only, then powers the machine off. The image itself is never written: QEMU runs it with
# snapshot=on.
exec > /dev/ttyAMA0 2>&1
say() { printf '@@IMAGE-CHECK %s\n' "$*"; }
pre() { sed "s/^/@@IMAGE-CHECK $1 /"; }
# What a unit's program said, found by its name: a process that ends at once is often gone
# before journald can tell its unit
why() {
	local u=$1 ident
	ident=$(systemctl show -P SyslogIdentifier "$u")
	[[ -n $ident ]] || ident=$(systemctl show -P ExecStart "$u" | sed -n 's/^{ path=\([^ ;]*\).*/\1/p' | head -n 1)
	[[ -z $ident ]] || journalctl -b -t "${ident##*/}" -o cat -n 8 --no-pager | pre "why $u"
	journalctl -b -u "$u" -o cat -n 3 --no-pager | pre "why $u"
}

say begin
say state "$(timeout 1800 systemctl is-system-running --wait 2>&1)"
say kernel "$(uname -r)"
systemctl list-jobs --no-legend --plain | pre job
for u in $(systemctl --failed --no-legend --plain | awk '{print $1}'); do
	say failed "$u"
	why "$u"
done
for r in $(systemctl show -p Id -p NRestarts '*.service' |
	awk -F= '/^Id=/ { id = $2 } /^NRestarts=/ && $2 > 0 { print id "=" $2 }'); do
	say restarts "${r%=*}" "${r#*=}"
	systemctl is-failed -q "${r%=*}" || why "${r%=*}"
done
for u in ${ACTIVE:-}; do
	say unit "$u" "$(systemctl show -P ActiveState "$u")" "$(systemctl show -P Result "$u")"
done
if pacman -Dk > /run/image-check-dk 2>&1; then say pacman-dk ok; else pre pacman-dk < /run/image-check-dk; fi
if busctl --system --no-pager list > /dev/null 2>&1; then say bus ok; else say bus broken; fi
systemd-analyze 2>&1 | head -n 1 | pre time
systemd-analyze blame --no-pager 2>&1 | head -n 15 | pre blame
journalctl -b -p err --no-pager -o cat | pre journal
say end
systemctl poweroff
exit 0
