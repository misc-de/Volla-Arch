#!/bin/bash
# Battery current of video decoding: alternates idle, hardware decoding (GStreamer, plinius-vdec)
# and software decoding (FFmpeg) of the same file in real time, logging the battery current.
# Display off during the run, so that only the decoding differs between the phases.
# Run on the phone as root:
#   systemd-run --unit=video-current systemd-inhibit --what=sleep /root/video-current.sh
# Waits for the USB cable to be unplugged, then SETTLE_S seconds (time to switch the display off).
set -u
LOG=/root/video-current.log
FILE=${FILE:-/home/plinius/Videos/plinius-vdec-test.mkv}
PHASE_S=${PHASE_S:-60}     # seconds per phase
CYCLES=${CYCLES:-3}        # rounds over idle, hw, sw
SETTLE_S=${SETTLE_S:-30}
BAT=/sys/class/power_supply/battery
LIGHT=$(ls -d /sys/class/backlight/* | head -1)

as_user() { runuser -u plinius -- env HOME=/home/plinius "$@"; }

workload() {
	case $1 in
	hw) as_user timeout $((PHASE_S + 2)) gst-launch-1.0 -q filesrc location="$FILE" ! matroskademux ! \
		h264parse ! pliniush264dec ! fakesink sync=true ;;
	sw) as_user timeout $((PHASE_S + 2)) ffmpeg -nostdin -loglevel error -re -i "$FILE" -an -f null - ;;
	idle) sleep $((PHASE_S + 2)) ;;
	esac
}

exec >"$LOG" 2>&1
echo "start $(date +%T), waiting for unplug"
# usb0 loses its carrier when the cable is pulled
while [ "$(cat /sys/class/net/usb0/carrier 2>/dev/null)" = 1 ]; do sleep 1; done
echo "unplugged $(date +%T), settling ${SETTLE_S}s"
sleep "$SETTLE_S"

# Log every second: time phase current_now(uA) voltage(uV) brightness
for c in $(seq 1 "$CYCLES"); do
	for phase in idle hw sw; do
		echo "# phase $phase cycle $c"
		workload $phase >/dev/null 2>&1 &
		W=$!
		sleep 2  # start-up of the pipeline is not part of the phase
		end=$((SECONDS + PHASE_S - 2))
		while [ $SECONDS -lt $end ]; do
			echo "$(date +%T) $phase $(cat $BAT/current_now) $(cat $BAT/voltage_now) $(cat "$LIGHT"/brightness)"
			sleep 1
		done
		wait $W
	done
done
echo "done $(date +%T)"
