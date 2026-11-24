# The GtkBuilder XML in tests/gcc/ui/ is written by hand from Blueprint files (the host has no
# blueprint-compiler): this checks that each .ui still says what its .blp says, so that a change of
# the .blp cannot leave the tests with a template that no longer matches. Compared in document
# order: every object's class and id, its signals (name, handler, object, swapped), its style
# classes; and the template's class and parent, the translation domain, the translatable texts.
# For our own .blp files the C file's bound template children and callbacks must be in the .ui.

import re
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

import pytest

from gcc_harness import GCC, UI, Unavailable, prepared_tree

TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|=>|[A-Za-z_$][\w.\-]*(?:::[\w\-]+)?|\d[\w.]*|[{}\[\]();:,|]')


def _unquote(tok):
    return bytes(tok[1:-1], "utf-8").decode("unicode_escape").encode("latin-1").decode("utf-8")


def _gtype(name, template):
    if name.startswith("$"):
        return name[1:]
    if "." in name:
        ns, cls = name.split(".", 1)
        return ns + cls
    return "Gtk" + name


def _is_class(tok):
    return tok.startswith("$") or tok[:1].isupper() or (
        "." in tok and tok.split(".", 1)[1][:1].isupper())


def parse_blp(text):
    """{"template": (class, parent), "domain", "objects": [{class, id, signals, styles}],
    "texts": Counter of translatable texts}."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"//[^\n]*", "", text)
    toks = TOKEN.findall(text)
    out = {"template": None, "domain": None, "objects": [], "texts": Counter()}
    stack = []
    template = None
    i = 0
    while i < len(toks):
        t = toks[i]
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        if t == "translation-domain":
            out["domain"] = _unquote(nxt)
            i += 2
            continue
        if t == "template":
            template = toks[i + 1][1:]
            out["template"] = (template, _gtype(toks[i + 3], template))
            obj = {"class": template, "id": None, "signals": [], "styles": []}
            out["objects"].append(obj)
            stack.append(obj)
            i += 5
            continue
        if _is_class(t) and (nxt == "{" or (re.fullmatch(r"[a-z_]\w*", nxt or "")
                                            and toks[i + 2:i + 3] == ["{"])):
            obj = {"class": _gtype(t, template), "id": nxt if nxt != "{" else None,
                   "signals": [], "styles": []}
            out["objects"].append(obj)
            stack.append(obj)
            i += 2 if nxt == "{" else 3
            continue
        if t == "}":
            stack.pop()
        elif t == "=>":
            name, handler = toks[i - 1], toks[i + 1][1:]
            j = i + 3
            args = []
            while toks[j] != ")":
                args.append(toks[j])
                j += 1
            obj_arg = args[0] if args else None
            # Flags after the call: swapped, not-swapped, after
            flags = []
            while toks[j + 1] not in (";", "}"):
                j += 1
                flags.append(toks[j])
            swapped = "swapped" in flags or (obj_arg is not None and "not-swapped" not in flags)
            stack[-1]["signals"].append((name, handler,
                                         template if obj_arg == "template" else obj_arg,
                                         bool(swapped)))
            i = j
        elif t == "styles" and nxt == "[":
            j = i + 2
            while toks[j] != "]":
                if toks[j].startswith('"'):
                    stack[-1]["styles"].append(_unquote(toks[j]))
                j += 1
            i = j
        elif t in ("_", "C_") and nxt == "(":
            out["texts"][_unquote(toks[i + 2 if t == "_" else i + 4])] += 1
        i += 1
    return out


def parse_ui(path):
    root = ET.parse(path).getroot()
    out = {"template": None, "domain": root.get("domain"), "objects": [], "texts": Counter()}

    def walk(el, current):
        if el.tag == "template":
            out["template"] = (el.get("class"), el.get("parent"))
            current = {"class": el.get("class"), "id": None, "signals": [], "styles": []}
            out["objects"].append(current)
        elif el.tag == "object":
            current = {"class": el.get("class"), "id": el.get("id"), "signals": [], "styles": []}
            out["objects"].append(current)
        elif el.tag == "signal":
            current["signals"].append((el.get("name"), el.get("handler"), el.get("object"),
                                       el.get("swapped") in ("yes", "true", "1")))
        elif el.tag == "class" and current is not None:
            current["styles"].append(el.get("name"))
        if el.get("translatable") == "yes":
            out["texts"][el.text or ""] += 1
        for child in el:
            walk(child, current)

    walk(root, None)
    return out


def pairs():
    """(.blp, .ui, our C file or None) for every hand-written .ui of tests/gcc/ui."""
    out = []
    tree = None
    for ui in sorted(UI.glob("*.ui")):
        ours = GCC / f"{ui.stem}.blp"
        if ours.exists():
            out.append(pytest.param(ours, ui, GCC / f"{ui.stem}.c", id=ui.name))
            continue
        if tree is None:
            try:
                tree = prepared_tree()
            except Unavailable:
                tree = False
        hits = sorted(tree.root.rglob(f"{ui.stem}.blp")) if tree else []
        if hits:
            out.append(pytest.param(hits[0], ui, None, id=ui.name))
        else:
            out.append(pytest.param(None, ui, None, id=ui.name))
    return out


@pytest.mark.parametrize("blp, ui, c_file", pairs())
def test_hand_written_ui_matches_its_blueprint(blp, ui, c_file):
    if blp is None:
        pytest.skip(f"{ui.name}: no .blp of that name here or in the prepared tree")
    want = parse_blp(blp.read_text())
    have = parse_ui(ui)
    assert have["template"] == want["template"]
    assert have["domain"] == want["domain"]
    assert [(o["class"], o["id"]) for o in have["objects"]] == \
        [(o["class"], o["id"]) for o in want["objects"]]
    for h, w in zip(have["objects"], want["objects"]):
        where = f'{w["class"]} {w["id"] or ""}'
        assert h["signals"] == w["signals"], where
        assert h["styles"] == w["styles"], where
    assert have["texts"] == want["texts"]
    if c_file is not None and c_file.exists():
        c = c_file.read_text()
        ids = {o["id"] for o in have["objects"] if o["id"]}
        handlers = {s[1] for o in have["objects"] for s in o["signals"]}
        children = re.findall(r"bind_template_child\s*\(\s*widget_class\s*,\s*\w+\s*,\s*(\w+)\s*\)", c)
        callbacks = re.findall(r"bind_template_callback\s*\(\s*widget_class\s*,\s*(\w+)\s*\)", c)
        assert children and set(children) <= ids
        assert callbacks and set(callbacks) <= handlers


def test_the_parser_sees_what_a_blueprint_says():
    blp = parse_blp('''using Gtk 4.0;
translation-domain "d";
// a comment with a { brace
template $T: Adw.Dialog {
  title: _("A \\"title\\"");
  child: Box box {
    Button b { clicked => $on_b(template); styles ["x", "y"] }
    Label { label: C_("ctx", "Lbl"); notify::visible => $on_v(); }
  };
}
Adw.AlertDialog extra { response::ok => $on_ok(template) not-swapped; }
''')
    assert blp["template"] == ("T", "AdwDialog")
    assert blp["domain"] == "d"
    assert [(o["class"], o["id"]) for o in blp["objects"]] == [
        ("T", None), ("GtkBox", "box"), ("GtkButton", "b"), ("GtkLabel", None),
        ("AdwAlertDialog", "extra")]
    assert blp["objects"][2]["signals"] == [("clicked", "on_b", "T", True)]
    assert blp["objects"][2]["styles"] == ["x", "y"]
    assert blp["objects"][3]["signals"] == [("notify::visible", "on_v", None, False)]
    assert blp["objects"][4]["signals"] == [("response::ok", "on_ok", "T", False)]
    assert blp["texts"] == Counter({'A "title"': 1, "Lbl": 1})
