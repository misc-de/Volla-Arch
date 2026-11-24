# A stand-in for lpac (tests/gcc/test_gcc_esim_dialog.py): answers each call from a scenario and
# records it.
#
# Usage: python3 -I fake_lpac.py SCENARIO.json ARGS…  (the fake's lpac in PATH execs this).
# SCENARIO: {"log": FILE, "state": FILE, "answers": {"<args joined by spaces>": [answer, …]}};
# the answers of a call are used one after the other (the last one stays); a key also matches
# the calls it is the start of ("profile download" matches "profile download -a LPA:1$…"), the
# longest key wins, without one the call succeeds with data null. An answer is
# {"out": [line, …] (a JSON value is printed as one line), "err": text, "exit": n, "sleep": s}.
# Each call appends {"argv", "env" (the LPAC_* variables), "trust" (the trust store's text)} to
# FILE as a JSON line.

import fcntl
import json
import os
import sys
import time


def main():
    with open(sys.argv[1]) as f:
        scenario = json.load(f)
    argv = sys.argv[2:]
    env = {k: v for k, v in os.environ.items() if k.startswith("LPAC_")}
    store = env.get("LPAC_HTTP_TRUST_STORE")
    trust = None
    if store:
        with open(store) as f:
            trust = f.read()
    with open(scenario["log"], "a") as f:
        f.write(json.dumps({"argv": argv, "env": env, "trust": trust,
                            "lc_all": os.environ.get("LC_ALL")}) + "\n")

    words = " ".join(argv)
    keys = [k for k in scenario["answers"] if words == k or words.startswith(k + " ")]
    answer = {"out": [{"type": "lpa", "payload": {"code": 0, "message": "success", "data": None}}]}
    if keys:
        key = max(keys, key=len)
        with open(scenario["state"], "a+") as st:
            fcntl.flock(st, fcntl.LOCK_EX)
            st.seek(0)
            counts = json.loads(st.read() or "{}")
            n = counts.get(key, 0)
            counts[key] = n + 1
            st.seek(0)
            st.truncate()
            st.write(json.dumps(counts))
        answers = scenario["answers"][key]
        answer = answers[min(n, len(answers) - 1)]
    if answer.get("sleep"):
        time.sleep(answer["sleep"])
    for line in answer.get("out", []):
        text = (line if isinstance(line, str) else json.dumps(line)) + "\n"
        # surrogateescape: a test can send bytes that are no UTF-8
        sys.stdout.buffer.write(text.encode("utf-8", "surrogateescape"))
    sys.stdout.flush()
    if answer.get("err"):
        sys.stderr.buffer.write(answer["err"].encode("utf-8", "surrogateescape"))
    sys.exit(answer.get("exit", 0))


if __name__ == "__main__":
    main()
