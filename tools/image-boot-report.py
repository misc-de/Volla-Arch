#!/usr/bin/env python3
# Judges the boot of an image in QEMU (image/test-image.sh) from its serial log: the lines
# "@@IMAGE-CHECK KEY ..." that image/test-image-guest.sh prints, against the rules of
# image/test-image.expected (units that must be active, units that may fail or restart without
# the phone's hardware).
#
# Usage: tools/image-boot-report.py SERIAL.log EXPECTED [--report FILE]
# Prints the verdict and the problems, writes the full report to FILE; exits 1 if the image
# has a problem, 2 on wrong usage.

import collections
import re
import sys

MARK = "@@IMAGE-CHECK "
GOOD_STATES = {"running", "degraded"}  # degraded: only with failed units, judged one by one


def read_rules(path):
    rules = {"active": [], "fails": set(), "restarts": set()}
    with open(path) as f:
        for n, line in enumerate(f, 1):
            line = line.split("#", 1)[0].split()
            if not line:
                continue
            if len(line) != 2 or line[0] not in rules:
                raise ValueError(f"{path}:{n}: expected 'active|fails|restarts UNIT'")
            kind, unit = line
            if kind == "active":
                rules[kind].append(unit)
            else:
                rules[kind].add(unit)
    return rules


def read_log(path):
    """The check's lines as (key, rest), in order; anything before the mark on a line (a login
    prompt the console printed at the same time) is left out."""
    out = []
    with open(path, "rb") as f:
        for raw in f:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            i = line.find(MARK)
            if i < 0:
                continue
            key, _, rest = line[i + len(MARK):].partition(" ")
            out.append((key, rest))
    return out


def judge(lines, rules):
    """(problems, facts) of one boot"""
    facts = {"begin": False, "end": False, "state": None, "kernel": None, "failed": [],
             "why": collections.defaultdict(list), "restarts": {}, "units": {}, "pacman": [],
             "bus": None, "time": None, "blame": [], "journal": [], "jobs": []}
    for key, rest in lines:
        if key in ("begin", "end"):
            facts[key] = True
        elif key in ("state", "kernel", "bus", "time"):
            facts[key] = rest
        elif key == "failed":
            facts["failed"].append(rest)
        elif key == "why":
            unit, _, text = rest.partition(" ")
            facts["why"][unit].append(text)
        elif key == "restarts":
            unit, _, n = rest.partition(" ")
            facts["restarts"][unit] = int(n) if n.isdigit() else 0
        elif key == "unit":
            unit, *state = rest.split()
            facts["units"][unit] = state
        elif key == "pacman-dk":
            facts["pacman"].append(rest)
        elif key == "blame":
            facts["blame"].append(rest)
        elif key == "journal":
            facts["journal"].append(rest)
        elif key == "job":
            facts["jobs"].append(rest)

    problems = []
    if not facts["begin"]:
        problems.append("the check never started: the boot did not get past graphical.target")
        return problems, facts
    if not facts["end"]:
        problems.append("the check did not finish (hung or the machine stopped)")
    if facts["state"] not in GOOD_STATES:
        problems.append(f"system state after the boot: {facts['state']!r}")
        for j in facts["jobs"]:
            problems.append(f"  job still waiting: {j}")
    for unit in facts["failed"]:
        if unit not in rules["fails"]:
            problems.append(f"failed: {unit}")
            problems.extend(f"  {w}" for w in facts["why"].get(unit, []))
    for unit, n in sorted(facts["restarts"].items()):
        if unit not in rules["restarts"]:
            problems.append(f"restarted {n} times: {unit}")
            problems.extend(f"  {w}" for w in facts["why"].get(unit, []))
    for unit in rules["active"]:
        state = facts["units"].get(unit)
        if state is None:
            if facts["end"]:
                problems.append(f"not reported: {unit}")
        elif state[:1] != ["active"]:
            problems.append(f"not active: {unit} ({' '.join(state)})")
    if facts["end"] and facts["pacman"] != ["ok"]:
        problems.append("pacman -Dk: the installed packages' dependencies are broken")
        problems.extend(f"  {p}" for p in facts["pacman"])
    if facts["end"] and facts["bus"] != "ok":
        problems.append("the system bus does not answer")
    return problems, facts


def report(problems, facts, rules):
    out = ["Verdict: " + ("FAILED" if problems else "passed")]
    if problems:
        out += ["", "Problems:"] + [f"  {p}" for p in problems]
    out += ["", f"Kernel: {facts['kernel']}", f"State: {facts['state']}", f"Boot: {facts['time']}"]
    expected = sorted(u for u in facts["failed"] if u in rules["fails"])
    if expected:
        out += ["", "Failed as expected without the phone:"] + [f"  {u}" for u in expected]
    gone = sorted(rules["fails"] - set(facts["failed"]))
    if gone and facts["end"]:
        out += ["", "Allowed to fail, but did not:"] + [f"  {u}" for u in gone]
    if facts["restarts"]:
        out += ["", "Restarts:"] + [f"  {u}: {n}" for u, n in sorted(facts["restarts"].items())]
    if facts["blame"]:
        out += ["", "Slowest units:"] + [f"  {b.strip()}" for b in facts["blame"]]
    if facts["journal"]:
        counts = collections.Counter(re.sub(r"\d+", "N", j) for j in facts["journal"])
        out += ["", f"Journal errors ({len(facts['journal'])} lines, by kind):"]
        out += [f"  {n:4} {text}" for text, n in counts.most_common()]
    return "\n".join(out) + "\n"


def main(argv):
    args = list(argv)
    report_file = None
    if "--report" in args:
        i = args.index("--report")
        if i + 1 >= len(args):
            args = []
        else:
            report_file = args[i + 1]
            del args[i:i + 2]
    if len(args) != 2:
        print("usage: tools/image-boot-report.py SERIAL.log EXPECTED [--report FILE]",
              file=sys.stderr)
        return 2
    rules = read_rules(args[1])
    problems, facts = judge(read_log(args[0]), rules)
    text = report(problems, facts, rules)
    if report_file:
        with open(report_file, "w") as f:
            f.write(text)
    if problems:
        print("Error: the image failed its boot test:", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1
    print(f"==> boot test passed ({facts['time']})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
