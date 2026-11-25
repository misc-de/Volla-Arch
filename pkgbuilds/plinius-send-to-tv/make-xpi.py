#!/usr/bin/env python3
# Builds the Firefox add-on Send to TV from its sources next to this file, or checks a signed
# copy against them.
#
#   make-xpi.py SRCDIR OUT.xpi          the add-on to upload to AMO (unlisted) for signing
#   make-xpi.py --check SRCDIR IN.xpi   the signed add-on holds exactly these sources (exit 1 if not)
#
# The strings come from the .po files: msgctxt is the key in messages.json, the template's msgid
# is the English text (default_locale). The archive is reproducible (sorted, fixed times), so the
# same sources always give the same bytes. Signing adds META-INF/ and leaves the rest alone.

import json
import re
import sys
import zipfile
from pathlib import Path

FILES = ("manifest.json", "background.js", "send-to-tv.svg")
TEMPLATE = "send-to-tv.pot"
LANGS = ("de", "es", "fr", "it", "nl", "pl", "pt", "ru")
STAMP = (2026, 1, 1, 0, 0, 0)
# AMO's limits for a translated name and description
LIMITS = {"name": 45, "description": 132}

_ESCAPES = {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}


def _unquote(text):
    text = text.strip()
    if len(text) < 2 or text[0] != '"' or text[-1] != '"':
        raise ValueError(f"not a quoted string: {text}")
    return re.sub(r"\\(.)", lambda m: _ESCAPES.get(m.group(1), m.group(1)), text[1:-1])


def read_po(path):
    """{msgctxt: (msgid, msgstr)} of a .po/.pot file; fuzzy entries count as untranslated."""
    entries, cur, field, fuzzy = {}, {}, None, False

    def flush():
        nonlocal cur, fuzzy
        if "msgctxt" in cur:
            ctx = cur["msgctxt"]
            if ctx in entries:
                raise ValueError(f"{path}: key {ctx} twice")
            entries[ctx] = (cur.get("msgid", ""), "" if fuzzy else cur.get("msgstr", ""))
        cur, fuzzy = {}, False

    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            flush()
            field = None
        elif line.startswith("#,"):
            fuzzy = fuzzy or "fuzzy" in line
        elif line.startswith("#"):
            continue
        elif line.startswith('"'):
            if field is None:
                raise ValueError(f"{path}: continuation without a field: {line}")
            cur[field] += _unquote(line)
        else:
            field, _, value = line.partition(" ")
            if field not in ("msgctxt", "msgid", "msgstr"):
                raise ValueError(f"{path}: unknown line: {line}")
            if field == "msgctxt" and cur.get("msgid") is not None and "msgstr" in cur:
                flush()
            cur[field] = _unquote(value)
    flush()
    return entries


def messages(src):
    """{locale: messages.json content}: English from the template, the rest from the .po files."""
    src = Path(src)
    template = read_po(src / TEMPLATE)
    out = {"en": {key: {"message": en} for key, (en, _) in sorted(template.items())}}
    for lang in LANGS:
        po = read_po(src / f"{lang}.po")
        stray = sorted(set(po) - set(template))
        if stray:
            raise ValueError(f"{lang}.po: keys not in the template: {stray}")
        out[lang] = {}
        for key, (en, _) in sorted(template.items()):
            have = po.get(key)
            if have and have[0] == en and have[1]:
                # Missing or outdated translations fall back to English (default_locale)
                out[lang][key] = {"message": have[1]}
    for lang, table in out.items():
        for key, limit in LIMITS.items():
            if key in table and len(table[key]["message"]) > limit:
                raise ValueError(f"{lang}: {key} longer than {limit} characters")
    return out


def contents(src):
    """{path in the archive: bytes}"""
    src = Path(src)
    files = {name: (src / name).read_bytes() for name in FILES}
    for lang, table in messages(src).items():
        text = json.dumps(table, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        files[f"_locales/{lang}/messages.json"] = text.encode("utf-8")
    return files


def build(src, out):
    files = contents(src)           # first, so a fault leaves no half archive behind
    with zipfile.ZipFile(out, "w") as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, STAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, data)


def check(src, signed):
    """The differences between a signed add-on and the sources, an empty list if none."""
    want = contents(src)
    with zipfile.ZipFile(signed) as archive:
        have = {n: archive.read(n) for n in archive.namelist()
                if not n.startswith("META-INF/") and not n.endswith("/")}
        signature = [n for n in archive.namelist() if n.startswith("META-INF/")]
    problems = []
    if not signature:
        problems.append("no signature (META-INF/) in the add-on")
    for name in sorted(set(want) | set(have)):
        if name not in have:
            problems.append(f"missing in the add-on: {name}")
        elif name not in want:
            problems.append(f"not from the sources: {name}")
        elif have[name] != want[name]:
            problems.append(f"differs from the sources: {name}")
    return problems


def main(argv):
    if len(argv) == 4 and argv[1] == "--check":
        problems = check(argv[2], argv[3])
        for p in problems:
            print(p, file=sys.stderr)
        return 1 if problems else 0
    if len(argv) == 3:
        build(argv[1], argv[2])
        return 0
    print("usage: make-xpi.py SRCDIR OUT.xpi | make-xpi.py --check SRCDIR IN.xpi", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
