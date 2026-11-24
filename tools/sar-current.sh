#!/bin/bash
# SAR proof: upload over mobile data while switching the modem's SAR scene between -1 (normal
# power) and each scene in SCENES. Two measures per phase:
#   - the modem's own transmit time per power level (getModemActivityInfo of IRadioModem, the
#     levels are <0, 0-8, 8-15, 15-20 and >20 dBm); the RIL logs the modem's answer
#     (NW_GET_ACTIVITY_INFO_CNF) as a hex dump in the radio log. The modem counts since the last
#     query, so nothing else may ask during the run.
#   - the battery current
# A real back-off moves transmit time out of the >20 dBm level against the -1 phase before it
# (and lowers the current). Evaluate on the PC with tools/sar-analyse.py (docs/MTKRADIOEX.md).
# Needs binder-call (pacman -S libgbinder-tools).
# Run on the phone as root: systemd-run --unit=sar-current systemd-inhibit --what=sleep /root/sar-current.sh
# Waits for the USB cable to be unplugged, keeps the phone awake, restores plinius-sar at the end.
# Runs on the phone without the repository, so it does not source tools/lib.sh; no -e, because
# the upload and the radio queries may fail for a moment without ending the run.
set -u
LOG=${LOG:-/root/sar-current.log}
ACTLOG=${ACTLOG:-/root/sar-activity.log}  # only the RIL's activity and ERFIDX lines
PHASE_S=${PHASE_S:-60}     # seconds per scene
CYCLES=${CYCLES:-3}        # rounds over SCENES
SCENES=${SCENES:-2 3 6 0}  # scenes to compare with -1; 0 is the control (no back-off on NR)
RATE=${RATE:-100K}         # upload rate limit, keeps the data volume small
SLOT=${SLOT:-slot1}        # radio slot of the SIM that carries mobile data
MIN_UPLOAD=${MIN_UPLOAD:-20000}  # bytes the settling time and each phase must upload at least
# The mobile data interface: the ccmni with a default route (it changes, ccmni1 or ccmni2)
IFACE=${IFACE:-$(ip route show default 2>/dev/null | awk '{ for (i = 1; i < NF; i++) if ($i == "dev" && $(i + 1) ~ /^ccmni/) { print $(i + 1); exit } }')}
IFACE=${IFACE:-ccmni1}
URL=https://speed.cloudflare.com/__up
MODEM=android.hardware.radio.modem.IRadioModem/$SLOT
GET_MODEM_ACTIVITY_INFO=5  # transaction code (tools/aidl-codes.py on android.hardware.radio.modem-V2-ndk.so)
# Serials far above ofono's own (they count up from 1): ofono gets the answer and drops it
SERIAL=2147418112          # 0x7fff0000

# Access technology and signal of the connected modem (the second modem object stays "failed")
radio_state() {
	local m
	for m in $(mmcli -L 2>/dev/null | grep -o 'Modem/[0-9]*' | cut -d/ -f2); do
		timeout 5 mmcli -m "$m" -K 2>/dev/null | awk -F': ' '
			/generic.state +:/ { state = $2 }
			/generic.access-technologies.value\[1\]/ { tech = $2 }
			/generic.signal-quality.value/ { sig = $2 }
			END { if (state == "connected") print "tech=" tech " signal=" sig }'
	done
}

# Asks the modem for its activity counters; the answer goes to ofono, the numbers to the radio
# log. The comment line marks the moment, sar-analyse.py takes the next activity line after it.
activity() {
	SERIAL=$((SERIAL + 1))
	echo "# activity t=$(date +%s.%N) serial=$SERIAL"
	# A oneway call has no reply: binder-call then says "NO REPLY" and ends with 2. Only a
	# missing service is an error (it says so on stderr).
	local err
	err=$(binder-call -q -a -o -d /dev/binder "$MODEM" $GET_MODEM_ACTIVITY_INFO $SERIAL 2>&1 >/dev/null)
	[ -z "$err" ] || echo "# activity failed: $err"
}

sent() {
	cat "/sys/class/net/$IFACE/statistics/tx_bytes" 2>/dev/null || echo 0
}

# The radio log of the Android container from now on, filtered (it also holds subscriber ids)
radio_log() {
	lxc-attach -n android -- /system/bin/logcat -b radio -v epoch -T 1 2>/dev/null |
		grep --line-buffered -E 'NW_GET_ACTIVITY_INFO_CNF|ERFIDX'
}

main() {
	exec >"$LOG" 2>&1
	if ! command -v binder-call >/dev/null; then
		echo "binder-call missing: pacman -S libgbinder-tools"
		exit 1
	fi
	echo "start $(date +%T), waiting for unplug; scenes: $SCENES, $CYCLES cycles of $PHASE_S s, $SLOT"
	# usb0 loses its carrier when the cable is pulled
	while [ "$(cat /sys/class/net/usb0/carrier 2>/dev/null)" = 1 ]; do sleep 1; done
	echo "unplugged $(date +%T)"
	systemctl stop plinius-sar

	radio_log >"$ACTLOG" &
	RADIO=$!
	# Endless rate-limited upload over the mobile interface (restarted if curl ends)
	( while :; do head -c 50M /dev/urandom | curl -s -o /dev/null --interface $IFACE \
		--limit-rate "$RATE" -X POST --data-binary @- $URL; done ) &
	UP=$!

	# Settle 20 s, then log every second: time scene current_now(uA) voltage(uV) tx_bytes.
	# Without an upload the modem does not transmit and nothing can be measured: stop at once
	# if the settling time uploads too little, and after two such phases in a row.
	local before dead=0 scene
	before=$(sent)
	sleep 20
	if [ $(($(sent) - before)) -lt "$MIN_UPLOAD" ]; then
		echo "abort: $(($(sent) - before)) bytes uploaded over $IFACE while settling $(radio_state)"
	else
		activity
		for scene in $(for _ in $(seq 1 "$CYCLES"); do for x in $SCENES; do echo -1 "$x"; done; done); do
			plinius-sar --scene "$scene" >/dev/null 2>&1
			# Comment line per phase: a refused scene or a radio change invalidates the phase
			echo "# phase scene=$scene rc=$? $(radio_state)"
			before=$(sent)
			end=$((SECONDS + PHASE_S))
			while [ $SECONDS -lt $end ]; do
				echo "$(date +%T) $scene $(cat /sys/class/power_supply/battery/current_now) $(cat /sys/class/power_supply/battery/voltage_now) $(sent)"
				sleep 1
			done
			activity
			if [ $(($(sent) - before)) -lt "$MIN_UPLOAD" ]; then
				dead=$((dead + 1))
				if [ $dead -ge 2 ]; then
					echo "abort: two phases without upload over $IFACE"
					break
				fi
			else
				dead=0
			fi
		done
	fi

	pkill -P $UP 2>/dev/null; kill $UP
	sleep 3  # the last activity line
	pkill -P $RADIO 2>/dev/null; kill $RADIO 2>/dev/null
	plinius-sar --scene -1 >/dev/null 2>&1
	systemctl start plinius-sar
	echo "done $(date +%T)"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
	main "$@"
fi
