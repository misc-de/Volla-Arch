#!/usr/bin/env python3
# Splits the test files under the given paths into N groups of about the same running time, for
# tools/run-tests.sh --jobs: one pytest process per group. The time of a file is taken from the
# last run's durations (build/test-durations.json, written by tools/run-tests.sh after a run), a
# file without one counts by its size. Prints one group per line, its files separated by spaces.
#
# Usage: tools/test-shards.py N [PATH...]   (default path: tests)
#        tools/test-shards.py --record JUNIT.xml...   (adds the files' times to the durations)

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DURATIONS = ROOT / "build" / "test-durations.json"
# Seconds per byte of a test file without a measured time: about what the tests here take
SECONDS_PER_BYTE = 1 / 2000


def test_files(paths):
    files = []
    for p in paths:
        p = (ROOT / p) if not Path(p).is_absolute() else Path(p)
        if p.is_file():
            files.append(p)
        elif p.is_dir():
            files.extend(sorted(p.rglob("test_*.py")))
    return files


def weights(files, durations):
    out = {}
    for f in files:
        rel = str(f.relative_to(ROOT))
        out[rel] = durations.get(rel, f.stat().st_size * SECONDS_PER_BYTE)
    return out


def shards(n, weight):
    """Longest first into the lightest group (LPT): close to the best split for these sizes."""
    groups = [[0.0, []] for _ in range(n)]
    for name, w in sorted(weight.items(), key=lambda kv: (-kv[1], kv[0])):
        lightest = min(groups, key=lambda g: g[0])
        lightest[0] += w
        lightest[1].append(name)
    return [sorted(g[1]) for g in groups if g[1]]


def load_durations(path=None):
    path = path or DURATIONS
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return {k: float(v) for k, v in data.items() if isinstance(v, (int, float))} \
        if isinstance(data, dict) else {}


def record(xml_files, path=None):
    """Sums the test times of each file from pytest's JUnit reports into the durations file
    (files of this run replace their old time, the others keep theirs)."""
    path = path or DURATIONS
    times = {}
    for x in xml_files:
        try:
            root = ET.parse(x).getroot()
        except (OSError, ET.ParseError):
            continue
        for case in root.iter("testcase"):
            # classname is the module path with dots: tests.gcc.test_gcc_usb[.Class]
            parts = case.get("classname", "").split(".")
            while parts and not parts[-1].startswith("test_"):
                parts.pop()
            if not parts:
                continue
            name = "/".join(parts) + ".py"
            times[name] = times.get(name, 0.0) + float(case.get("time") or 0)
    if not times:
        return
    data = load_durations(path)
    data.update({k: round(v, 2) for k, v in times.items()})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(sorted(data.items())), indent=1) + "\n")


def main(argv):
    if argv[1:2] == ["--record"]:
        record(argv[2:])
        return
    n = int(argv[1])
    if n < 1:
        raise SystemExit("N must be at least 1")
    files = test_files(argv[2:] or ["tests"])
    for group in shards(n, weights(files, load_durations())):
        print(" ".join(group))


if __name__ == "__main__":
    main(sys.argv)
