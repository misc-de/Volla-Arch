#!/usr/bin/env python3
# Lists the characters the unl0kr on-screen keyboard can type, over all layouts and layers,
# one per line (UTF-8). Reads unl0kr/sq2lv_layouts.c: every keycap that is a single character
# types it; longer keycaps ("123", "äÄ", "<hidden>") switch layers, LV_SYMBOL_* are actions.
# plinius-encrypt only accepts new passwords made of these characters.
# Usage: unl0kr-keycaps.py sq2lv_layouts.c > unl0kr-keycaps

import re
import sys

source = open(sys.argv[1], encoding="utf-8").read()
chars = set()
for block in re.findall(r"keycaps_\w+\[\] = \{(.*?)\};", source, re.S):
    for literal in re.findall(r'"((?:[^"\\]|\\.)*)"', block):
        text = re.sub(r"\\(.)", lambda m: "\n" if m.group(1) == "n" else m.group(1), literal)
        if len(text) == 1 and text != "\n":
            chars.add(text)
if not chars:
    sys.exit("no keycaps found in " + sys.argv[1])
sys.stdout.write("".join(c + "\n" for c in sorted(chars)))
