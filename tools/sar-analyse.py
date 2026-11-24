#!/usr/bin/env python3
"""Evaluate a run of tools/sar-current.sh: does a SAR scene lower the transmit power?

Usage: tools/sar-analyse.py sar-current.log sar-activity.log

Per phase: median battery current, upload rate and the share of the modem's transmit time per
power level (getModemActivityInfo: <0, 0-8, 8-15, 15-20, >20 dBm). Every scene phase is compared
with the -1 phase right before it. A scene lowers the power when, in every valid pair, it moves
at least DROP_PP points of transmit time out of the >20 dBm level, with the -1 phase at full
power (at least FULL_PP points there). A back-off that keeps the cap above 20 dBm cannot show in
the levels; the current is the cross-check then (docs/MTKRADIOEX.md).
"""
import re
import statistics
import struct
import sys

LEVELS = ("<0", "0-8", "8-15", "15-20", ">20")
TOP = len(LEVELS) - 1
NORMAL = -1
DROP_PP = 20.0     # points of transmit time out of >20 dBm that count as a back-off
FULL_PP = 50.0     # the -1 phase must spend this much of its transmit time above 20 dBm
MIN_PAIRS = 2      # valid pairs a verdict needs
UPLOAD_SHARE = 0.5  # a phase with less than this share of the run's median upload is invalid
MATCH_S = 5.0      # an activity line belongs to the query at most this long before it

# The modem's answer as the MIPC RIL logs it: 16 bytes header, then TLVs (type, length: 16 bit
# little endian). 0x0 result, 0x100 transmit ms per level (uint32 each), 0x101 rx, 0x102 sleep,
# 0x103 idle (uint32 ms), 0x104 number of levels (uint8); RmmOemRequestHandler::requestGetActivityInfo
ACTIVITY = re.compile(r"^\s*(\d+(?:\.\d+)?)\s.*NW_GET_ACTIVITY_INFO_CNF\].*DUMP=([0-9A-Fa-f]+)")
MIPC_HEADER = 16
T_RESULT, T_TX, T_RX, T_SLEEP, T_IDLE, T_LEVELS = 0x0, 0x100, 0x101, 0x102, 0x103, 0x104
QUERY = re.compile(r"^# activity t=(\d+(?:\.\d+)?)")
PHASE = re.compile(r"^# phase scene=(-?\d+) rc=(\d+)\s*(.*)$")
SAMPLE = re.compile(r"^\d\d:\d\d:\d\d (-?\d+) (-?\d+) (-?\d+) (\d+)$")


class Phase:
    def __init__(self, scene, rc, radio):
        self.scene, self.rc, self.radio = scene, rc, radio
        self.current = []   # uA
        self.tx_bytes = []
        self.levels = None  # ms per level
        self.problems = []

    @property
    def current_ma(self):
        return statistics.median(self.current) / 1000 if self.current else None

    @property
    def upload_kib(self):
        if len(self.tx_bytes) < 2:
            return 0.0
        return (self.tx_bytes[-1] - self.tx_bytes[0]) / (len(self.tx_bytes) - 1) / 1024

    @property
    def tx_ms(self):
        return sum(self.levels) if self.levels else 0

    def share(self, level):
        """Share of transmit time in a level, in points; None without transmit time"""
        return 100.0 * self.levels[level] / self.tx_ms if self.tx_ms else None

    @property
    def valid(self):
        return not self.problems


def decode(dump):
    """[tx0..tx4, rx, sleep, idle] in ms from the hex dump of one answer"""
    data = bytes.fromhex(dump)
    fields, i = {}, MIPC_HEADER
    while i + 4 <= len(data):
        kind, length = struct.unpack_from("<HH", data, i)
        fields[kind] = data[i + 4:i + 4 + length]
        i += 4 + length
    if fields.get(T_RESULT) != bytes(4) or fields.get(T_LEVELS) != bytes([len(LEVELS)]) \
            or len(fields.get(T_TX, b"")) != 4 * len(LEVELS) \
            or any(len(fields.get(k, b"")) != 4 for k in (T_RX, T_SLEEP, T_IDLE)):
        raise ValueError(f"unexpected activity answer: {dump}")
    tx = list(struct.unpack("<5I", fields[T_TX]))
    return tx + [struct.unpack("<I", fields[k])[0] for k in (T_RX, T_SLEEP, T_IDLE)]


def parse_activity(lines):
    """[(time, [tx0..tx4, rx, sleep, idle])] from the radio log (logcat -v epoch)"""
    out = []
    for line in lines:
        m = ACTIVITY.match(line)
        if m:
            out.append((float(m.group(1)), decode(m.group(2))))
    return out


def parse_run(lines):
    """Phases and query times from the main log"""
    phases, queries = [], []
    for line in lines:
        line = line.rstrip("\n")
        if m := QUERY.match(line):
            queries.append(float(m.group(1)))
        elif m := PHASE.match(line):
            phases.append(Phase(int(m.group(1)), int(m.group(2)), m.group(3).strip()))
        elif (m := SAMPLE.match(line)) and phases:
            # current_now is signed; only the amount counts here
            phases[-1].current.append(abs(int(m.group(2))))
            phases[-1].tx_bytes.append(int(m.group(4)))
    return phases, queries


def match_queries(queries, activity):
    """The first activity line at or after each query (None if none within MATCH_S)"""
    out, i = [], 0
    for t in queries:
        while i < len(activity) and activity[i][0] < t:
            i += 1
        if i < len(activity) and activity[i][0] - t <= MATCH_S:
            out.append(activity[i][1])
            i += 1
        else:
            out.append(None)
    return out


def cumulative(answers):
    """True when the modem counts up since boot, False when it starts over with every query.
    Counting up, no counter ever falls and the sum of all of them (sleep and idle included)
    grows with every query, as time passes; equal phases counted anew give equal sums."""
    seen = [a for a in answers if a is not None]
    return all(all(b >= a for a, b in zip(x, y)) and sum(y) > sum(x) for x, y in zip(seen, seen[1:]))


def assign_levels(phases, answers):
    """Phase i lies between query i and query i+1"""
    counting_up = cumulative(answers)
    for i, phase in enumerate(phases):
        start = answers[i] if i < len(answers) else None
        end = answers[i + 1] if i + 1 < len(answers) else None
        if end is None or (counting_up and start is None):
            phase.problems.append("no activity numbers")
            continue
        tx = end[:len(LEVELS)]
        if counting_up:
            tx = [b - a for a, b in zip(start[:len(LEVELS)], tx)]
        phase.levels = tx
    return counting_up


def check(phases):
    uploads = [p.upload_kib for p in phases if p.upload_kib > 0]
    median_upload = statistics.median(uploads) if uploads else 0
    radios = {p.radio for p in phases if p.radio}
    for p in phases:
        if p.rc:
            p.problems.append(f"scene refused (rc={p.rc})")
        if not p.radio:
            p.problems.append("no data connection")
        if p.upload_kib < UPLOAD_SHARE * median_upload:
            p.problems.append(f"upload {p.upload_kib:.0f} KiB/s")
        if p.levels is not None and not p.tx_ms:
            p.problems.append("no transmit time")
    return radios


def pairs(phases):
    """(-1 phase, scene phase) for every scene phase that follows a -1 phase"""
    return [(a, b) for a, b in zip(phases, phases[1:]) if a.scene == NORMAL and b.scene != NORMAL]


def verdict(scene_pairs):
    valid = [(a, b) for a, b in scene_pairs if a.valid and b.valid and a.share(TOP) >= FULL_PP]
    if len(valid) < MIN_PAIRS:
        return "inconclusive", valid
    if all(a.share(TOP) - b.share(TOP) >= DROP_PP for a, b in valid):
        return "lowers power", valid
    if all(a.share(TOP) - b.share(TOP) < DROP_PP for a, b in valid):
        return "no back-off above 20 dBm", valid
    return "inconclusive", valid


def fmt(value, spec):
    return "-" if value is None else format(value, spec)


def report(phases, counting_up, radios):
    out = [f"counters: {'since boot' if counting_up else 'since the last query'}",
           f"radio: {', '.join(sorted(radios)) or 'none'}", "",
           "scene  mA     KiB/s  tx s   " + "  ".join(f"{l:>6}" for l in LEVELS) + "  problems"]
    for p in phases:
        levels = "  ".join(fmt(p.share(i) if p.levels else None, "6.1f") for i in range(len(LEVELS)))
        out.append(f"{p.scene:>5}  {fmt(p.current_ma, '5.0f')}  {p.upload_kib:5.0f}  "
                   f"{p.tx_ms / 1000:5.1f}  {levels}  {'; '.join(p.problems)}")
    out.append("")
    by_scene = {}
    for a, b in pairs(phases):
        by_scene.setdefault(b.scene, []).append((a, b))
    for scene in sorted(by_scene):
        result, valid = verdict(by_scene[scene])
        drops = ", ".join(f"{a.share(TOP) - b.share(TOP):+.0f}" for a, b in valid)
        currents = ", ".join(f"{b.current_ma - a.current_ma:+.0f}" for a, b in valid)
        out.append(f"scene {scene}: {result} ({len(valid)} valid pairs; "
                   f">20 dBm points out: {drops or '-'}; current mA: {currents or '-'})")
    return "\n".join(out)


def analyse(run_lines, activity_lines):
    phases, queries = parse_run(run_lines)
    answers = match_queries(queries, parse_activity(activity_lines))
    counting_up = assign_levels(phases, answers)
    radios = check(phases)
    return report(phases, counting_up, radios)


def main(argv):
    if len(argv) != 3:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    with open(argv[1]) as run, open(argv[2]) as activity:
        print(analyse(run, activity))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
