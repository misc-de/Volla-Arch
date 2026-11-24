#!/bin/sh
# Battery state for the bar as waybar JSON; the CSS class picks the icon
dev=$(upower -e | grep -m1 '/battery_')
info=$(upower -i "$dev")
pct=$(printf '%s\n' "$info" | sed -n 's/^ *percentage: *\([0-9]*\).*/\1/p' | head -1)
state=$(printf '%s\n' "$info" | sed -n 's/^ *state: *//p' | head -1)
level=$(( (${pct:-0} + 5) / 10 * 10 ))
case $state in
	# The theme has no icon for charging at 100 %
	charging) if [ "$level" -ge 100 ]; then suffix=-charged; else suffix=-charging; fi ;;
	fully-charged) suffix=-charged; level=100 ;;
	pending-charge|pending-discharge) suffix=-plugged-in ;;
	*) suffix= ;;
esac
[ "$suffix" = -charged ] || [ "$level" -le 100 ] || level=100
printf '{"text": "▣", "class": "l%s%s"}\n' "$level" "$suffix"
