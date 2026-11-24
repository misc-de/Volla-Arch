#!/usr/bin/python3
"""Lists the source files of a kernel tree that a given kernel configuration builds.

The list is the "manifest" the kernel vulnerability list is filtered with
(tools/kernel-cves/plinius-kernel-cves): a CVE of the kernel CNA names the files its fix
touches ("programFiles"), and it matters for a phone only if one of them is part of the
phone's kernel.

kbuild itself is not run: "make -n" still executes every recipe line that contains $(MAKE),
and on a tree prepared for another compiler that does real work in the output directory.
Instead the Kbuild/Makefile of every directory is read and evaluated with the configuration:

  - "obj-$(CONFIG_FOO) += bar.o" builds bar.c when CONFIG_FOO is y or m;
  - "bar-y += a.o b.o", "bar-$(CONFIG_X) += c.o" and "bar-objs := ..." are the parts of the
    composite object bar.o, built when bar.o is;
  - "obj-$(CONFIG_FOO) += dir/" descends into dir;
  - ifdef/ifndef/ifeq/ifneq on CONFIG_ symbols are followed, other conditions count as true.

Where the evaluation cannot decide (a file that no Makefile names, variables it cannot
expand), the file is listed: the list is meant to hide what is surely not built, never to
hide what might be.

Usage: kernel-files.py SOURCE_TREE CONFIG [--arch arm64] > manifest.files
Output: one path per line relative to the tree, after "# " header lines.
"""

import argparse
import os
import re
import sys

# Top-level directories kbuild always enters (Makefile: core-y, drivers-y, libs-y, ...);
# arch/<arch> is added from --arch. Everything below is reached only through obj-*/subdir-*.
TOP_DIRS = ("init", "usr", "kernel", "certs", "mm", "fs", "ipc", "security", "crypto",
            "block", "io_uring", "drivers", "sound", "samples", "net", "virt", "lib")
SOURCE_EXT = (".c", ".S", ".rs")
ASSIGN = re.compile(r"^([A-Za-z0-9_.$()/-]+)\s*(\+=|:=|\?=|=)\s*(.*)$")
CONFIG_REF = re.compile(r"\$\((CONFIG_[A-Za-z0-9_]+)\)")


def read_config(path):
    values = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = re.match(r"^(CONFIG_[A-Za-z0-9_]+)=(.*)$", line.strip())
            if m:
                values[m.group(1)] = m.group(2).strip('"')
    return values


def expand(text, config):
    """Replaces $(CONFIG_X) by its value ("" when unset); other variables stay as they are."""
    return CONFIG_REF.sub(lambda m: config.get(m.group(1), ""), text)


def condition(line, config):
    """Truth of an ifdef/ifndef/ifeq/ifneq line; None if it does not depend on CONFIG_ only."""
    m = re.match(r"^(ifdef|ifndef)\s+(\S+)", line)
    if m:
        if not m.group(2).startswith("CONFIG_"):
            return None
        defined = config.get(m.group(2), "") not in ("", "n")
        return defined if m.group(1) == "ifdef" else not defined
    m = re.match(r"^(ifeq|ifneq)\s*(.*)$", line)
    if not m:
        return None
    args = m.group(2).strip()
    # ifeq (a,b) or ifeq "a" "b" / 'a' 'b'
    pm = re.match(r"^\((.*),(.*)\)$", args)
    if pm:
        a, b = pm.group(1), pm.group(2)
    else:
        qm = re.match(r"""^["'](.*)["']\s+["'](.*)["']$""", args)
        if not qm:
            return None
        a, b = qm.group(1), qm.group(2)
    a, b = expand(a, config).strip(), expand(b, config).strip()
    if "$" in a or "$" in b:
        return None
    equal = a == b
    return equal if m.group(1) == "ifeq" else not equal


def logical_lines(path):
    """The lines of a Makefile with continuations joined and comments removed."""
    with open(path, encoding="utf-8", errors="replace") as f:
        raw = f.read().split("\n")
    out, cur, joining = [], "", False
    for line in raw:
        if line.startswith("\t") and not joining:
            # Recipe line: not an assignment (a continued line may start with a tab, too)
            continue
        if line.rstrip().endswith("\\"):
            cur += line.rstrip()[:-1] + " "
            joining = True
            continue
        cur += line
        joining = False
        # A comment ends the line; "\#" is an escaped hash, not a comment
        cur = re.split(r"(?<!\\)#", cur, maxsplit=1)[0].strip()
        if cur:
            out.append(cur)
        cur = ""
    return out


class Directory:
    """What one Kbuild/Makefile says: objects with their conditions and the subdirectories."""

    def __init__(self):
        # object path (relative to this directory) -> list of (owner composite or None, active)
        self.objects = {}
        # subdirectory path (relative) -> active
        self.subdirs = {}
        self.found = False


def parse_dir(tree, rel, config):
    d = Directory()
    for name in ("Kbuild", "Makefile"):
        path = os.path.join(tree, rel, name)
        if os.path.isfile(path):
            break
    else:
        return d
    d.found = True
    stack = []  # each: (this branch true?, condition known?, any earlier branch taken?)
    for line in logical_lines(path):
        word = line.split(None, 1)[0]
        if word in ("ifdef", "ifndef", "ifeq", "ifneq"):
            c = condition(line, config)
            stack.append([True if c is None else c, c is None, bool(c)])
            continue
        if word == "else":
            if stack:
                top = stack[-1]
                rest = line[4:].strip()
                if rest:
                    # "else ifeq ...": a chain; known only if both parts are
                    c = condition(rest, config)
                    if top[1] or c is None:
                        top[0], top[1] = True, True
                    else:
                        top[0] = (not top[2]) and c
                        top[2] = top[2] or c
                elif not top[1]:
                    # The last else of a chain: only if no branch before it was taken
                    top[0] = not top[2]
                    top[2] = True
            continue
        if word == "endif":
            if stack:
                stack.pop()
            continue
        active = all(s[0] for s in stack)
        m = ASSIGN.match(line)
        if not m:
            continue
        lhs, rhs = expand(m.group(1), config), expand(m.group(3), config)
        # The variable kinds kbuild reads: obj-y/obj-m, subdir-y/-m, lib-y, composite parts
        km = re.match(r"^(obj|subdir|lib|extra|core|drivers|libs|net|virt)-(.*)$", lhs)
        if km:
            kind, flag = km.groups()
            on = active and flag in ("y", "m")
            if "$" in flag:
                on = active  # an unexpanded condition: assume built
            owner = None
        else:
            cm = re.match(r"^(.+?)-(y|m|objs|\$\(.*\)|)$", lhs)
            if not cm or not cm.group(1):
                continue
            flag = cm.group(2)
            on = active and (flag in ("y", "m", "objs") or "$" in flag)
            owner = cm.group(1) + ".o"
            kind = "part"
        for token in rhs.split():
            if "$" in token:
                continue
            if token.endswith("/"):
                if kind in ("obj", "subdir", "core", "drivers", "libs", "net", "virt"):
                    sub = os.path.normpath(token)
                    d.subdirs[sub] = d.subdirs.get(sub, False) or on
                continue
            if token.endswith((".o", ".a")):
                obj = os.path.normpath(token[:-2] + ".o")
                d.objects.setdefault(obj, []).append((owner, on))
    return d


class Tree:
    def __init__(self, root, config, arch):
        self.root = root
        self.config = config
        self.dirs = {}
        self.arch = arch

    def dir(self, rel):
        if rel not in self.dirs:
            self.dirs[rel] = parse_dir(self.root, rel, self.config)
        return self.dirs[rel]

    def reached(self, rel, _seen=None):
        """True if kbuild descends into rel, None if no Makefile on the way decides it."""
        if rel in TOP_DIRS or rel == f"arch/{self.arch}":
            return True
        parent = os.path.dirname(rel)
        if not parent:
            return False  # another top-level directory (Documentation, tools, other arch)
        if rel.startswith("arch/") and not rel.startswith(f"arch/{self.arch}/"):
            return False
        # Any ancestor may name this directory with a multi-level path ("obj-y += a/b/")
        anc = parent
        while True:
            d = self.dir(anc)
            sub = os.path.relpath(rel, anc)
            if sub in d.subdirs:
                return d.subdirs[sub] and self.reached(anc) is not False
            # The ancestor builds objects inside this directory directly ("obj-y += a/b.o")
            if any(o.startswith(sub + "/") and any(on for _, on in uses)
                   for o, uses in d.objects.items()):
                return self.reached(anc) is not False
            if not anc or anc in TOP_DIRS or anc == f"arch/{self.arch}":
                break
            anc = os.path.dirname(anc)
        # Nobody names the directory itself. Directories pulled in through variables
        # ("include $(path)/Makefile", "$(AMDKFD_FILES)") sit next to ones that are named: when
        # every entry an ancestor has below the same first path component is off ("amd/amdgpu/"
        # for drm/amd/amdkfd), the directory is not built either.
        anc = parent
        while True:
            d = self.dir(anc)
            top = os.path.relpath(rel, anc).split("/")[0]
            near = [on for s, on in d.subdirs.items() if s == top or s.startswith(top + "/")]
            near += [any(on for _, on in uses) for o, uses in d.objects.items()
                     if o.startswith(top + "/")]
            if near:
                if not any(near):
                    return False
                return None if self.reached(anc) is not False else False
            if not anc or anc in TOP_DIRS or anc == f"arch/{self.arch}":
                break
            anc = os.path.dirname(anc)
        return None

    def built(self, rel_file):
        """True / False / None (undecided) for a source file relative to the tree."""
        base = os.path.splitext(rel_file)[0] + ".o"
        anc = os.path.dirname(rel_file)
        decided = False
        while True:
            d = self.dir(anc)
            obj = os.path.relpath(base, anc) if anc else base
            if obj in d.objects:
                decided = True
                if self.object_built(anc, obj, set()) and self.reached(anc) is not False:
                    return True
            if not anc or anc in TOP_DIRS or anc == f"arch/{self.arch}":
                break
            anc = os.path.dirname(anc)
        if decided:
            return False
        if self.reached(os.path.dirname(rel_file)) is False:
            return False
        return None

    def object_built(self, rel, obj, seen):
        if (rel, obj) in seen:
            return False
        seen.add((rel, obj))
        d = self.dir(rel)
        for owner, on in d.objects.get(obj, []):
            if not on:
                continue
            if owner is None:
                return True
            if self.object_built(rel, os.path.normpath(owner), seen):
                return True
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("tree")
    ap.add_argument("config")
    ap.add_argument("--arch", default="arm64")
    args = ap.parse_args()
    config = read_config(args.config)
    tree = Tree(os.path.abspath(args.tree), config, args.arch)

    files, undecided, skipped = [], 0, 0
    for top in sorted(os.listdir(tree.root)):
        if top not in TOP_DIRS and top != "arch":
            continue
        start = os.path.join(tree.root, top if top != "arch" else f"arch/{args.arch}")
        for dirpath, dirnames, filenames in os.walk(start):
            dirnames.sort()
            rel_dir = os.path.relpath(dirpath, tree.root)
            if tree.reached(rel_dir) is False:
                dirnames[:] = []
                skipped += 1
                continue
            for name in sorted(filenames):
                if not name.endswith(SOURCE_EXT):
                    continue
                rel = os.path.join(rel_dir, name)
                b = tree.built(rel)
                if b is None:
                    undecided += 1
                if b is not False:
                    files.append(rel)
    print(f"# Source files built with {os.path.basename(args.config)} (arch {args.arch})")
    print(f"# {len(files)} files, {undecided} of them listed because no Makefile decides")
    for f in files:
        print(f)
    print(f"kernel-files: {len(files)} files ({undecided} undecided), {skipped} directories not built",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
