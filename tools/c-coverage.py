#!/usr/bin/env python3
# Line coverage of the repository's own C code by the host tests: reads the gcov counters of the
# programs tests/conftest.py's compile_c built (tools/run-tests.sh --coverage sets
# PLINIUS_TEST_GCOV) and adds them up per source file under pkgbuilds/. A file compiled into
# several test programs counts a line as run when any of them ran it. Versioned C files no test
# program compiles are listed apart, with their size: gcov cannot know their executable lines.
#
# Usage: tools/c-coverage.py GCOV_DIR REPORT [--json FILE]

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "pkgbuilds"


def gcda_files(out):
    """The counter files of one build: gcc names them <output>-<source>.gcda next to the output,
    or <output>.gcda when the output has the name of its only source. A program that never ran
    has only the .gcno: gcov then reads every line as run 0 times, so the file counts as compiled
    and not covered (not as compiled by no test)."""
    out = Path(out)
    if not out.parent.is_dir():
        return []
    stems = {}
    for pattern in (f"{out.name}-*.gcno", f"{out.name}-*.gcda", f"{out.name}.gcno",
                    f"{out.name}.gcda"):
        for f in out.parent.glob(pattern):
            if f.suffix == ".gcda" or f.with_suffix(".gcda") not in stems:
                stems[f.with_suffix(".gcda")] = f
    return sorted(stems.values())


def counters(gcda):
    """{source path: {line: count}} of one counter file."""
    r = subprocess.run(["gcov", "--json-format", "--stdout", gcda.name], cwd=gcda.parent,
                       capture_output=True, text=True)
    # Counters of a cross compiler one minor version apart (aarch64-linux-gnu-gcc, run under
    # qemu-user) are read all the same; gcov only warns and exits with 3
    if r.returncode not in (0, 3) or not r.stdout.strip():
        return {}
    result = {}
    # One JSON document per line, one per .gcda
    for doc in r.stdout.splitlines():
        data = json.loads(doc)
        cwd = data.get("current_working_directory", str(gcda.parent))
        for f in data["files"]:
            path = Path(os.path.normpath(os.path.join(cwd, f["file"])))
            lines = result.setdefault(path, {})
            for line in f["lines"]:
                n = line["line_number"]
                lines[n] = max(lines.get(n, 0), line["count"])
    return result


def versioned_c_files():
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "pkgbuilds/*.c"],
                         capture_output=True, check=True).stdout.decode()
    return {ROOT / p for p in out.split("\0") if p}


def main(argv):
    gcov_dir, report = Path(argv[1]), Path(argv[2])
    json_out = Path(argv[argv.index("--json") + 1]) if "--json" in argv else None
    builds = gcov_dir / "builds.txt"
    lines = {}
    for out in sorted(set(builds.read_text().split())) if builds.exists() else []:
        for gcda in gcda_files(out):
            for path, counts in counters(gcda).items():
                if PKG not in path.parents:
                    continue  # the test programs themselves, system headers
                merged = lines.setdefault(path, {})
                for n, c in counts.items():
                    merged[n] = max(merged.get(n, 0), c)

    rows = []
    for path, counts in lines.items():
        run = sum(1 for c in counts.values() if c > 0)
        rows.append((run / len(counts) if counts else 1.0, path.relative_to(ROOT), len(counts),
                     run, sorted(n for n, c in counts.items() if c == 0)))
    rows.sort()
    total = sum(r[2] for r in rows)
    covered = sum(r[3] for r in rows)
    untested = sorted(versioned_c_files() - set(lines))

    with open(report, "w") as f:
        f.write(f"{'File':<70} {'Lines':>6} {'Miss':>6} {'Cover':>6}\n")
        for share, rel, n, run, _missing in rows:
            f.write(f"{str(rel):<70} {n:>6} {n - run:>6} {share:>6.0%}\n")
        f.write("-" * 92 + "\n")
        f.write(f"{'TOTAL (C files compiled by the tests)':<70} {total:>6} {total - covered:>6} "
                f"{(covered / total if total else 1):>6.0%}\n")
        if untested:
            f.write("\nC files no test compiles (source lines):\n")
            for path in untested:
                f.write(f"  {path.relative_to(ROOT)}  {len(path.read_text().splitlines())}\n")
    if json_out:
        json_out.write_text(json.dumps({
            str(rel): {"lines": n, "covered": run, "missing": missing}
            for _share, rel, n, run, missing in rows}, indent=1))
    print(f"C: {covered}/{total} lines ({covered / total if total else 1:.0%}) in {len(rows)} "
          f"files compiled by the tests, {len(untested)} C files not compiled by any test")


if __name__ == "__main__":
    main(sys.argv)
