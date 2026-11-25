#!/bin/bash
# Runs on the phone (plinius-lpmlog.service, installed by tools/lpm-night.sh): writes down what
# the night's deep-sleep question needs and nothing else. Read-only: it switches nothing.
#
#   K <ISO time> <line>   kernel lines of MediaTek's SPM on low-power states and of suspend:
#                         "System LPM is blocked by <subsystem>", 26M_off_pct, vcore_off_pct,
#                         PM: suspend entry/exit. These are info messages, the journal (notice
#                         and up) drops them and the kernel's buffer holds only ~25 minutes.
#   S <epoch> cap=… cur=… st=… bl=… wlan=… bt=… btconn=… susp=…/…
#                         once a minute: battery percent, current (µA), charger status,
#                         backlight, Wi-Fi and Bluetooth soft block, number of connected
#                         Bluetooth devices (only the count, no address), suspend_stats
#                         success/fail
#
# Ends by itself once the time in $DIR/until (epoch) has passed, also after a restart.
# LPMLOG_DIR, LPMLOG_SYS, LPMLOG_INTERVAL and LPMLOG_ONCE are for the tests.
set -euo pipefail
# The phone's locale would translate rfkill's words ("entsperrt"), the report reads English
export LC_ALL=C

DIR=${LPMLOG_DIR:-/var/tmp/plinius-lpmlog}
SYS=${LPMLOG_SYS:-/sys}
INTERVAL=${LPMLOG_INTERVAL:-60}
PATTERN='System LPM is blocked by|26M_off_pct|vcore_off_pct|PM: suspend (entry|exit)'

until_epoch=$(cat "$DIR/until" 2>/dev/null || echo 0)
expired() { [ "$(date +%s)" -ge "$until_epoch" ]; }
if expired; then
	echo "lpmlog: past $(date -d "@$until_epoch" '+%F %T'), nothing to do"
	exit 0
fi

out="$DIR/$(date +%Y%m%d-%H%M%S).txt"
read_sys() { cat "$SYS/$1" 2>/dev/null || echo "?"; }
soft() { rfkill -n -o TYPE,SOFT 2>/dev/null | awk -v t="$1" '$1 == t { print $2; exit }'; }

status() {
	local conn
	conn=$(bluetoothctl devices Connected 2>/dev/null | grep -c '^Device ' || true)
	printf 'S %s cap=%s cur=%s st=%s bl=%s wlan=%s bt=%s btconn=%s susp=%s/%s\n' \
		"$(date +%s)" \
		"$(read_sys class/power_supply/battery/capacity)" \
		"$(read_sys class/power_supply/battery/current_now)" \
		"$(read_sys class/power_supply/battery/status | tr ' ' _)" \
		"$(read_sys class/backlight/plinius-backlight/brightness)" \
		"$(soft wlan)" "$(soft bluetooth)" "${conn:-0}" \
		"$(read_sys power/suspend_stats/success)" "$(read_sys power/suspend_stats/fail)" \
		>> "$out"
}

if [ -n "${LPMLOG_ONCE:-}" ]; then
	status
	dmesg --time-format iso 2>/dev/null | grep -E "$PATTERN" | sed 's/^/K /' >> "$out" || true
	exit 0
fi

# Kernel lines as they come; -W: only new ones, a restart does not write the buffer again.
# $! is dmesg itself: when it ends, grep and sed see the end of their input and end too
dmesg -W --time-format iso 2>/dev/null \
	> >(grep --line-buffered -E "$PATTERN" 2>/dev/null | sed -u 's/^/K /' >> "$out" 2>/dev/null) &
reader=$!
trap 'kill "$reader" 2>/dev/null || true' EXIT

while ! expired; do
	status
	sleep "$INTERVAL"
done
echo "lpmlog: past $(date -d "@$until_epoch" '+%F %T'), done"
