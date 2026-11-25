#!/bin/bash
# The night measurement of the SoC's deep sleep (docs/STANDBY.md, "Der SoC erreicht nie den
# Tiefschlaf"): installs a passive logger on the phone that survives a restart, fetches its log
# and sums it up.
#
# Usage: tools/lpm-night.sh start [HOURS]   logger on, ends by itself after HOURS (default 36)
#        tools/lpm-night.sh status          whether it runs, size of its log
#        tools/lpm-night.sh fetch           log → build/lpm-night/, removed from the phone after
#                                           the copy checked out, then the summary
#        tools/lpm-night.sh stop            logger off and gone (the log stays until fetch)
# PHONE (default root@10.15.19.82, USB), PHONE_SSH_OPTS for the connection.
#
# The logger (tools/lpm-night-logger.sh) only reads: kernel lines of the SPM, battery, backlight,
# radio blocks and the number of connected Bluetooth devices, never addresses or names.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=tools/lib.sh
. "$here/lib.sh"

phone=${PHONE:-root@10.15.19.82}
remote_dir=/var/tmp/plinius-lpmlog
unit=plinius-lpmlog.service
local_dir=${LPM_LOCAL_DIR:-$here/../build/lpm-night}

on_phone() {
	# shellcheck disable=SC2086
	ssh ${PHONE_SSH_OPTS:--o HostKeyAlias=10.15.19.82} "$phone" "$@"
}

case ${1:-} in
start)
	hours=${2:-36}
	[[ $hours =~ ^[0-9]+$ ]] && [ "$hours" -ge 1 ] && [ "$hours" -le 96 ] ||
		die "HOURS must be 1 to 96, not $hours"
	on_phone "mkdir -p $remote_dir && cat > $remote_dir/lpmlog.sh && chmod 755 $remote_dir/lpmlog.sh" \
		< "$here/lpm-night-logger.sh"
	on_phone "echo \$(( \$(date +%s) + $hours * 3600 )) > $remote_dir/until
cat > /etc/systemd/system/$unit <<'EOF'
[Unit]
Description=Night log of the SoC's low-power states (tools/lpm-night.sh, temporary)

[Service]
ExecStart=$remote_dir/lpmlog.sh
Restart=on-failure
RestartSec=10
Nice=10

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload && systemctl enable -q $unit && systemctl restart $unit && systemctl is-active $unit"
	msg "logger runs on the phone for $hours h, also after a restart"
	;;
status)
	on_phone "systemctl is-active $unit || true; date -d @\$(cat $remote_dir/until) '+until %F %T'; ls -l $remote_dir/*.txt 2>/dev/null || echo 'no log yet'"
	;;
fetch)
	mkdir -p "$local_dir"
	files=$(on_phone "cd $remote_dir 2>/dev/null && ls *.txt 2>/dev/null || true")
	[ -n "$files" ] || die "no log on the phone"
	for f in $files; do
		on_phone "cat $remote_dir/$f" > "$local_dir/$f"
		remote_sum=$(on_phone "sha256sum < $remote_dir/$f" | cut -d' ' -f1)
		local_sum=$(sha256sum < "$local_dir/$f" | cut -d' ' -f1)
		[ "$remote_sum" = "$local_sum" ] || die "$f: copy differs, left on the phone"
		# A log still being written stays on the phone
		if on_phone "systemctl is-active -q $unit"; then
			msg "$f: copied, the logger still runs, it stays on the phone"
		else
			on_phone "rm -f $remote_dir/$f"
			msg "$f: copied and removed from the phone"
		fi
	done
	on_phone "systemctl is-active -q $unit || rmdir $remote_dir 2>/dev/null || true"
	# shellcheck disable=SC2086
	python3 "$here/lpm-night-report.py" $(printf "$local_dir/%s " $files)
	;;
stop)
	on_phone "systemctl disable --now $unit 2>/dev/null || true; rm -f /etc/systemd/system/$unit $remote_dir/lpmlog.sh $remote_dir/until; systemctl daemon-reload; ls $remote_dir 2>/dev/null || true"
	msg "logger stopped and removed; fetch takes the log"
	;;
*)
	sed -n '6,11p' "$0" >&2
	exit 2
	;;
esac
