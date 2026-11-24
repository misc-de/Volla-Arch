# Scripted stand-ins for the programs a panel runs (pacman, flatpak, plinius-pkg, …), for the
# panel tests in tests/gcc (test_gcc_software.py, test_gcc_webapps.py).
#
# Programs(dir).program(path_or_fake_bin, name) writes a stub that logs its arguments (like
# conftest.FakeBin) and answers by rules: Programs.rule(name, args, out=..., code=..., ...).
# A rule matches a call whose arguments equal args (a list), or start with prefix; the first
# matching rule answers. "outs" are answers for successive calls (the last one stays).
# Extra actions of a rule: "write" {path: text} before answering, "remove" [paths],
# "early" text printed before waiting, "wait" path (until it exists, at most 20 s), "sleep" seconds, "exec" (the arguments after
# the first "--", or all of them, run as the program's own: pkexec, systemd-run).

import json
import stat
from pathlib import Path

RESPONDER = r'''
import json, os, subprocess, sys, time
spec_file, name, log, args = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]
with open(log, "a") as f:
    f.write(json.dumps(args) + "\n")
with open(spec_file) as f:
    spec = json.load(f)
rules = spec.get(name, [])
for i, rule in enumerate(rules):
    if "args" in rule and rule["args"] != args:
        continue
    if "prefix" in rule and args[:len(rule["prefix"])] != rule["prefix"]:
        continue
    break
else:
    rule = {}
    i = -1
for path, text in rule.get("write", {}).items():
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
for path in rule.get("remove", []):
    try:
        os.unlink(path)
    except OSError:
        pass
if rule.get("early"):
    sys.stdout.write(rule["early"])
    sys.stdout.flush()
if rule.get("wait"):
    end = time.time() + 20
    while not os.path.exists(rule["wait"]) and time.time() < end:
        time.sleep(0.02)
if rule.get("sleep"):
    time.sleep(rule["sleep"])
if rule.get("exec"):
    run = args[args.index("--") + 1:] if "--" in args else args
    sys.exit(subprocess.call(run))
out, code = rule.get("out", ""), rule.get("code", 0)
if "outs" in rule:
    counter = f"{log}.{i}.n"
    n = int(open(counter).read()) if os.path.exists(counter) else 0
    with open(counter, "w") as f:
        f.write(str(n + 1))
    step = rule["outs"][min(n, len(rule["outs"]) - 1)]
    out, code = (step, code) if isinstance(step, str) else (step.get("out", ""), step.get("code", code))
sys.stdout.write(out)
sys.stdout.flush()
sys.exit(code)
'''


class Programs:
    def __init__(self, directory):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.spec = self.dir / "rules.json"
        self.responder = self.dir / "responder.py"
        self.responder.write_text(RESPONDER)
        self.rules = {}
        self.paths = {}
        self._save()

    def _save(self):
        self.spec.write_text(json.dumps(self.rules))

    def program(self, path, name=None):
        """A stub at path (a file name, its directory is made); answers by the rules of name
        (default: the file's name)."""
        path = Path(path)
        name = name or path.name
        path.parent.mkdir(parents=True, exist_ok=True)
        log = self.dir / f"{name}.log"
        path.write_text(f'#!/bin/sh\nexec python3 -I "{self.responder}" "{self.spec}" "{name}" "{log}" "$@"\n')
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        self.paths[name] = path
        self.rules.setdefault(name, [])
        self._save()
        return path

    def rule(self, name, args=None, prefix=None, first=False, **answer):
        r = dict(answer)
        if args is not None:
            r["args"] = list(args)
        if prefix is not None:
            r["prefix"] = list(prefix)
        rules = self.rules.setdefault(name, [])
        rules.insert(0, r) if first else rules.append(r)
        self._save()

    def clear(self, name):
        self.rules[name] = []
        self._save()

    def calls(self, name):
        log = self.dir / f"{name}.log"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]


def executable(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


__all__ = ["Programs", "executable"]
