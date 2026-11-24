#!/bin/sh
# Wi-Fi state for the bar as waybar JSON; the CSS class picks the icon
line=$(nmcli -t -f IN-USE,SIGNAL dev wifi list --rescan no 2>/dev/null | grep '^\*' | head -1)
if [ -z "$line" ]; then
	class=offline
else
	signal=${line#*:}
	if [ "$signal" -ge 80 ]; then class=excellent
	elif [ "$signal" -ge 55 ]; then class=good
	elif [ "$signal" -ge 30 ]; then class=ok
	elif [ "$signal" -ge 5 ]; then class=weak
	else class=none; fi
fi
printf '{"text": "▣", "class": "%s"}\n' "$class"
