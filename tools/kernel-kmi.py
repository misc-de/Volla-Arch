#!/usr/bin/env python3
"""Measurements for a merge of linux-6.1.y into plinius-kernel, against the interface (KMI) of the
prebuilt MediaTek modules. Run in the kernel repository (or with -C <dir>); revisions are git revs.

  conflicts [--tree <tree>] [<file>...]
                              conflict regions of a merge (diff3 markers), classified; in the
                              worktree (default: the files git lists as unmerged) or in the
                              result tree of git merge-tree --write-tree
  types <old> <new>           struct/union/enum/typedef definitions that differ, marked R when
                              the vendor modules reach them (genksyms type graph from
                              --symtypes, imported symbols from --needs); --genksyms compares
                              what genksyms sees (__GENKSYMS__ branches resolved, ANDROID_KABI_USE
                              read as the reserve it replaces): after the KMI repair no R is left
  inlines <old> <new>         #define values, static inline functions and anonymous enums of the
                              changed headers that differ: compiled into the vendor modules, so
                              neither CRCs nor BTF show a change of them
  dupes <old> <new> --own <base>
                              hunks old..new that add lines old already has in one piece, in the
                              files changed between <base> and <old> (our own commits): a fix we
                              backported before, applied a second time by the merge
  layout <old vmlinux> <new vmlinux>
                              size and member offsets of the reachable structs in two builds
                              (DWARF, read with pahole): what the CRCs do not prove
  vendor-const <dir> <value>  vendor modules (*.ko below <dir>) with <value> as an immediate in
                              their code, with the function and the next store: an inline helper
                              of the old headers compiled into them (llvm-objdump)

Output: one line per finding, tab separated; the exit status is 0 also when there are findings.
"""
import argparse
import difflib
import os
import re
import subprocess
import sys

GIT = ['git']


def git(*args, check=True):
    r = subprocess.run(GIT + list(args), capture_output=True)
    if check and r.returncode:
        sys.exit(f'git {" ".join(args)}: {r.stderr.decode(errors="replace").strip()}')
    return r.stdout.decode('utf-8', 'replace') if r.returncode == 0 else ''


def show(rev, path):
    return git('show', f'{rev}:{path}', check=False)


def changed_files(old, new, *patterns):
    return git('diff', '--name-only', old, new, '--', *patterns).split()


def norm(text):
    return re.sub(r'\s+', '', text)


# --- conflicts -----------------------------------------------------------------------------------

def regions(lines):
    """(ours, base, theirs) of every diff3 conflict region."""
    out, cur, parts = [], None, None
    for line in lines:
        if line.startswith('<<<<<<< '):
            parts = ([], [], [])
            cur = parts[0]
        elif parts is None:
            continue
        elif line.startswith('||||||| ') and cur is parts[0]:
            cur = parts[1]
        elif line.startswith('=======') and cur is not parts[2]:
            cur = parts[2]
        elif line.startswith('>>>>>>> '):
            out.append(parts)
            parts = None
        else:
            cur.append(line)
    return out


def outside(lines):
    """The lines of a file outside its conflict regions, normalized."""
    keep, inside = [], False
    for line in lines:
        if line.startswith('<<<<<<< '):
            inside = True
        elif line.startswith('>>>>>>> '):
            inside = False
        elif not inside:
            keep.append(line)
    return norm(''.join(keep))


def classify(ours, base, theirs, rest):
    """same: ours == theirs; already-in: every line of theirs is in the file outside the
    conflict regions (a backport with other context); both-insert: empty base; near-same:
    >= 80 % alike."""
    o, b, t = norm(''.join(ours)), norm(''.join(base)), norm(''.join(theirs))
    tl = [norm(x) for x in theirs if norm(x)]
    if o == t:
        return 'same'
    if tl and all(x in rest for x in tl):
        return 'already-in'
    if not b:
        return 'both-insert'
    if difflib.SequenceMatcher(None, o, t).ratio() >= 0.8:
        return 'near-same'
    return 'real'


def cmd_conflicts(a):
    if a.tree:
        files = a.files or git('grep', '-l', '-e', '^<<<<<<< ', a.tree, check=False).split()
        files = [f.split(':', 1)[1] if f.startswith(a.tree + ':') else f for f in files]
        read = lambda f: show(a.tree, f)
    else:
        files = a.files or sorted(set(git('diff', '--name-only', '--diff-filter=U').split()))
        read = lambda f: open(f, errors='replace').read()
    total = {}
    for f in files:
        lines = read(f).splitlines(True)
        found = regions(lines)
        if not found:
            continue
        rest = outside(lines)
        kinds = [classify(o, b, t, rest) for o, b, t in found]
        for k in kinds:
            total[k] = total.get(k, 0) + 1
        print(f'{f}\t{len(found)}\t{" ".join(kinds)}')
    print(f'# {sum(total.values())} regions: ' +
          ', '.join(f'{k} {v}' for k, v in sorted(total.items())), file=sys.stderr)


# --- types ---------------------------------------------------------------------------------------

def strip_comments(s):
    s = re.sub(r'/\*.*?\*/', ' ', s, flags=re.S)
    s = re.sub(r'//[^\n]*', ' ', s)
    return re.sub(r'"(\\.|[^"\\\n])*"', '""', s)


def genksyms_view(s):
    """The source as genksyms sees it: only the __GENKSYMS__ conditionals resolved, every
    ANDROID_KABI_USE(n, ...) read as the ANDROID_KABI_RESERVE(n) it takes."""
    out, stack = [], []
    for line in s.split('\n'):
        t = line.strip()
        m = re.match(r'#\s*(ifdef|ifndef)\s+__GENKSYMS__\b', t)
        if m:
            stack.append(['G', m.group(1) == 'ifdef'])
            continue
        if re.match(r'#\s*if', t):
            stack.append(['O', True])
        elif re.match(r'#\s*else\b', t) and stack and stack[-1][0] == 'G':
            stack[-1][1] = not stack[-1][1]
            continue
        elif re.match(r'#\s*endif\b', t) and stack:
            if stack.pop()[0] == 'G':
                continue
        if all(v for _, v in stack):
            out.append(line)
    s = '\n'.join(out)
    return re.sub(r'ANDROID_KABI_USE\(\s*(\d+)\s*,(?:[^()]|\([^()]*\))*\)',
                  r'ANDROID_KABI_RESERVE(\1)', s)


DEF = re.compile(r'\b(struct|union|enum)\s+([A-Za-z_]\w*)?\s*(?:__\w+\s*(?:\(\([^)]*\)\))?\s*)*\{')


def definitions(src, genksyms=False):
    """{(kind, name): normalized body} of every named or typedef'd definition."""
    if genksyms:
        src = genksyms_view(src)
    s, out, pos = strip_comments(src), {}, 0
    while True:
        m = DEF.search(s, pos)
        if not m:
            return out
        i, depth = m.end(), 1
        while i < len(s) and depth:
            depth += {'{': 1, '}': -1}.get(s[i], 0)
            i += 1
        kind, name = m.group(1), m.group(2)
        if not name:
            t = re.match(r'\s*(?:__\w+\s*)*([A-Za-z_]\w*)\s*;', s[i:i + 80])
            if 'typedef' in s[max(0, m.start() - 20):m.start()] and t:
                kind, name = 't', t.group(1)
        if name:
            out.setdefault((kind, name), re.sub(r'\s+', ' ', s[m.end():i - 1]).strip())
        pos = m.end()


def reachable(symdir, needs):
    """Types the vendor modules reach: the genksyms graph of the .symtypes files below symdir,
    from the symbols in needs ("crc symbol" lines)."""
    graph = {}
    for root, _, files in os.walk(symdir):
        for fn in files:
            if fn.endswith('.symtypes'):
                for line in open(os.path.join(root, fn), errors='replace'):
                    p = line.split(None, 1)
                    if len(p) == 2:
                        graph.setdefault(p[0], set()).update(re.findall(r'\b[sute]#\w+', p[1]))
    todo = [line.split()[1] for line in open(needs) if len(line.split()) > 1]
    todo = [w for w in todo if w in graph]
    seen = set()
    while todo:
        k = todo.pop()
        if k not in seen:
            seen.add(k)
            todo += graph.get(k, ())
    return seen


PREFIX = {'struct': 's#', 'union': 'u#', 'enum': 'e#', 't': 't#'}


def cmd_types(a):
    seen = reachable(a.symtypes, a.needs)
    files = a.files or changed_files(a.old, a.new, '*.c', '*.h')
    nr = 0
    for f in files:
        if not f.endswith(('.c', '.h')):
            continue
        old, new = definitions(show(a.old, f), a.genksyms), definitions(show(a.new, f), a.genksyms)
        for key in sorted(set(old) | set(new)):
            if old.get(key) != new.get(key):
                st = 'new' if key not in old else 'gone' if key not in new else 'changed'
                r = 'R' if PREFIX[key[0]] + key[1] in seen else '-'
                nr += r == 'R'
                print(f'{r}\t{st}\t{key[0]}\t{key[1]}\t{f}')
    print(f'# {len(seen)} reachable types and symbols, {nr} reachable definitions differ',
          file=sys.stderr)


# --- inlines -------------------------------------------------------------------------------------

def inline_items(src):
    s = re.sub(r'\\\n', ' ', strip_comments(src))
    out = {}
    for m in re.finditer(r'^\s*#\s*define\s+(\w+)(\([^)]*\))?(.*)$', s, re.M):
        out[('define', m.group(1))] = re.sub(r'\s+', ' ', (m.group(2) or '') + m.group(3)).strip()
    for m in re.finditer(r'static\s+(?:__always_inline|inline)[^;{(]*?\b(\w+)\s*\([^;{]*\)\s*\{', s):
        i, depth = m.end(), 1
        while i < len(s) and depth:
            depth += {'{': 1, '}': -1}.get(s[i], 0)
            i += 1
        out[('inline', m.group(1))] = re.sub(r'\s+', ' ', s[m.start():i])
    for m in re.finditer(r'\benum\s*\{([^}]*)\}', s):
        names = [re.sub(r'\s+', ' ', x).strip() for x in m.group(1).split(',')
                 if x.strip() and not x.strip().startswith('#')]
        if names:
            out[('enum', names[0].split('=')[0].strip())] = ', '.join(names)
    return out


def cmd_inlines(a):
    # Only the headers a module build sees
    files = a.files or changed_files(a.old, a.new, 'include/*.h', 'arch/arm64/include/*.h')
    for f in files:
        old, new = inline_items(show(a.old, f)), inline_items(show(a.new, f))
        for key in sorted(set(old) | set(new)):
            if old.get(key) == new.get(key):
                continue
            st = 'new' if key not in old else 'gone' if key not in new else 'changed'
            if st == 'changed' and key[0] == 'enum' and new[key].startswith(old[key] + ', '):
                st = 'appended'
            print(f'{st}\t{key[0]}\t{key[1]}\t{f}')


# --- dupes ---------------------------------------------------------------------------------------

def cmd_dupes(a):
    own = set(changed_files(a.own, a.old, '*.c', '*.h'))
    for f in changed_files(a.old, a.new, '*.c', '*.h'):
        if f not in own:
            continue
        before = '\n'.join(re.sub(r'\s+', ' ', x).strip() for x in show(a.old, f).splitlines()
                           if x.strip())
        diff = git('diff', '-U0', a.old, a.new, '--', f)
        for hunk in re.split(r'\n(?=@@)', diff)[1:]:
            lines = hunk.splitlines()
            added = [re.sub(r'\s+', ' ', x[1:]).strip() for x in lines[1:]
                     if x.startswith('+') and x[1:].strip()]
            if len(added) >= a.min and '\n'.join(added) in before:
                print(f'{f}\t{lines[0]}\t{len(added)} lines already in {a.old}')


# --- layout --------------------------------------------------------------------------------------

def pahole_structs(vmlinux, names):
    """{name: (size, {member: offset})} from pahole's DWARF reading of vmlinux. pahole -C with
    a long list of names prints only some of them, so all structures are read, as a stream (the
    whole output of a kernel is several GB), and only the named ones kept."""
    want = set(names)
    proc = subprocess.Popen(['pahole', '-F', 'dwarf', vmlinux], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, errors='replace')
    out, cur, depth = {}, None, 0
    for line in proc.stdout:
        m = re.match(r'^(struct|union) (\w+) \{', line)
        if m and depth == 0:
            depth = 1
            cur = m.group(2) if m.group(2) in want and m.group(2) not in out else '?'
            if cur != '?':
                out[cur] = [None, {}]
            continue
        if cur == '?':
            depth += line.count('{') - line.count('}')
            if depth == 0:
                cur = None
            continue
        if cur is None:
            continue
        depth += line.count('{') - line.count('}')
        mem = re.match(r'\s+.*?\b(\w+)(\[\d*\])?(?::\d+)?;\s*/\*\s*(\d+)', line)
        if mem and depth == 1:
            out[cur][1].setdefault(mem.group(1), int(mem.group(3)))
        size = re.search(r'/\* size: (\d+)', line)
        if size and depth == 1:
            out[cur][0] = int(size.group(1))
        if depth == 0:
            cur = None
    proc.wait()
    return {k: (v[0], v[1]) for k, v in out.items()}


def cmd_layout(a):
    names = a.structs or sorted(k[2:] for k in reachable(a.symtypes, a.needs)
                                if k.startswith(('s#', 'u#')))
    old, new = pahole_structs(a.old, names), pahole_structs(a.new, names)
    for n in sorted(set(old) & set(new)):
        (so, mo), (sn, mn) = old[n], new[n]
        if so != sn:
            print(f'{n}\tsize\t{so} -> {sn}')
        for m in sorted(set(mo) & set(mn), key=lambda m: mo[m]):
            if mo[m] != mn[m]:
                print(f'{n}\t{m}\t{mo[m]} -> {mn[m]}')
        for m in sorted(set(mo) - set(mn)):
            print(f'{n}\t{m}\tgone (was at {mo[m]})')
    print(f'# {len(set(old) & set(new))} structs compared', file=sys.stderr)


# --- vendor-const --------------------------------------------------------------------------------

def cmd_vendor_const(a):
    value = int(a.value, 0)
    pat = re.compile(r'#(0x%x|%d)\b' % (value, value))
    for root, _, files in os.walk(a.dir):
        for fn in sorted(files):
            if not fn.endswith('.ko'):
                continue
            path = os.path.join(root, fn)
            r = subprocess.run(['llvm-objdump', '-d', '--no-show-raw-insn', path],
                               capture_output=True, text=True, errors='replace')
            func, lines = '?', r.stdout.splitlines()
            for i, line in enumerate(lines):
                m = re.match(r'^[0-9a-f]+ <(.+)>:', line)
                if m:
                    func = m.group(1)
                elif pat.search(line):
                    store = next((' '.join(x.split('\t', 1)[-1].split()) for x in lines[i + 1:i + 4]
                                  if re.search(r'\bst(r|ur|lr)[bh]?\b', x)), '')
                    print(f'{fn}\t{func}\t{line.split(":")[0].strip()}\t{store}')


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument('-C', dest='repo', help='kernel repository (default: current directory)')
    sub = p.add_subparsers(dest='cmd', required=True)
    c = sub.add_parser('conflicts')
    c.add_argument('--tree', help='tree of git merge-tree --write-tree (default: the worktree)')
    c.add_argument('files', nargs='*')
    for name in ('types', 'inlines', 'dupes'):
        s = sub.add_parser(name)
        s.add_argument('old')
        s.add_argument('new')
        if name != 'dupes':
            s.add_argument('files', nargs='*')
    t = sub.choices['types']
    t.add_argument('--symtypes', required=True, help='build tree with the .symtypes files')
    t.add_argument('--needs', required=True, help='vendor-needs.txt')
    t.add_argument('--genksyms', action='store_true')
    d = sub.choices['dupes']
    d.add_argument('--own', required=True, help='commit before our own changes')
    d.add_argument('--min', type=int, default=3, help='least added lines of a hunk')
    lay = sub.add_parser('layout')
    lay.add_argument('old')
    lay.add_argument('new')
    lay.add_argument('structs', nargs='*')
    lay.add_argument('--symtypes')
    lay.add_argument('--needs')
    v = sub.add_parser('vendor-const')
    v.add_argument('dir')
    v.add_argument('value')
    a = p.parse_args(argv)
    if a.repo:
        GIT[1:] = ['-C', a.repo]
    if a.cmd == 'layout' and not a.structs and not (a.symtypes and a.needs):
        p.error('layout: name structs or give --symtypes and --needs')
    {'conflicts': cmd_conflicts, 'types': cmd_types, 'inlines': cmd_inlines, 'dupes': cmd_dupes,
     'layout': cmd_layout, 'vendor-const': cmd_vendor_const}[a.cmd](a)


if __name__ == '__main__':
    main()
