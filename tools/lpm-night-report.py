#!/usr/bin/env python3
"""Sums up the logs of tools/lpm-night-logger.sh: for every stretch with the same radios and
display state, how long, battery %/h, median current, how often the SPM named each subsystem
that blocked the low-power state, and whether the 26 MHz clock and Vcore ever went off.

Usage: tools/lpm-night-report.py FILE... (the .txt files fetched by tools/lpm-night.sh fetch)
"""
import collections
import datetime
import re
import statistics
import sys

BLOCKED = re.compile(r"System LPM is blocked by (\w+)")
OFF_PCT = re.compile(r"\b(26M_off_pct|vcore_off_pct)\s*=\s*(\d+)")


def kernel_time(stamp):
    """Epoch of dmesg's --time-format iso (2026-11-25T01:02:03,456789+01:00)."""
    return datetime.datetime.fromisoformat(stamp.replace(",", ".")).timestamp()


def parse(lines):
    """(status records, kernel records) sorted by time."""
    status, kernel = [], []
    for line in lines:
        if line.startswith("S "):
            parts = line.split()
            rec = {"t": int(parts[1])}
            rec.update(p.split("=", 1) for p in parts[2:])
            status.append(rec)
        elif line.startswith("K "):
            _, stamp, text = (line.rstrip("\n").split(" ", 2) + [""])[:3]
            try:
                kernel.append((kernel_time(stamp), text))
            except ValueError:
                continue
    return sorted(status, key=lambda r: r["t"]), sorted(kernel)


def state_of(rec):
    display = "off" if rec.get("bl") == "0" else "on"
    bt = rec.get("bt", "?")
    if bt == "unblocked" and rec.get("btconn", "0") not in ("0", "?"):
        bt = "connected"
    return (f"display {display}", f"wlan {rec.get('wlan', '?')}", f"bt {bt}",
            "charging" if rec.get("st") in ("Charging", "Full") else "battery")


def segments(status):
    """Consecutive status records with the same state: (state, first, last)."""
    out = []
    for rec in status:
        st = state_of(rec)
        if out and out[-1][0] == st:
            out[-1][2] = rec
        else:
            out.append([st, rec, rec])
    return out


def number(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def summary(status, kernel):
    rows = []
    for st, first, last in segments(status):
        t0, t1 = first["t"], last["t"]
        hours = (t1 - t0) / 3600
        in_seg = [text for t, text in kernel if t0 <= t <= t1 + 60]
        blocked = collections.Counter(m.group(1) for text in in_seg
                                      for m in [BLOCKED.search(text)] if m)
        off = collections.Counter()
        for text in in_seg:
            for name, pct in OFF_PCT.findall(text):
                off[(name, int(pct) > 0)] += 1
        currents = [c for c in (number(r.get("cur")) for r in status if t0 <= r["t"] <= t1)
                    if c is not None]
        cap0, cap1 = number(first.get("cap")), number(last.get("cap"))
        rate = (cap0 - cap1) / hours if hours > 0 and cap0 is not None and cap1 is not None \
            else None
        rows.append({
            "state": ", ".join(st), "from": t0, "to": t1, "hours": hours, "rate": rate,
            "current_ma": abs(statistics.median(currents)) / 1000 if currents else None,
            "suspends": sum("PM: suspend entry" in text for text in in_seg),
            "blocked": blocked, "off": off,
        })
    return rows


def clock(t):
    return datetime.datetime.fromtimestamp(t).strftime("%d.%m. %H:%M")


def render(rows):
    out = []
    for r in rows:
        out.append(f"## {clock(r['from'])}–{clock(r['to'])} ({r['hours']:.1f} h): {r['state']}")
        if r["rate"] is not None:
            out.append(f"   battery {r['rate']:.1f} %/h")
        if r["current_ma"] is not None:
            out.append(f"   median current {r['current_ma']:.0f} mA")
        out.append(f"   suspends {r['suspends']}")
        if r["blocked"]:
            out.append("   low-power state blocked by: " + ", ".join(
                f"{name} {n}" for name, n in r["blocked"].most_common()))
        for name in ("26M_off_pct", "vcore_off_pct"):
            on, zero = r["off"][(name, True)], r["off"][(name, False)]
            if on or zero:
                out.append(f"   {name}: > 0 in {on} of {on + zero} reports")
    return "\n".join(out)


def main(argv):
    if len(argv) < 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    lines = []
    for name in argv[1:]:
        with open(name, encoding="utf-8", errors="replace") as f:
            lines.extend(f)
    status, kernel = parse(lines)
    if not status:
        print("no status lines", file=sys.stderr)
        return 1
    print(render(summary(status, kernel)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
