#!/usr/bin/env python3
"""Recover AIDL transaction codes from NDK backend proxy libraries.

For every exported Bp<Interface>::method() the disassembly is scanned for the
AIBinder_transact() call and the immediate loaded into w1 (the transaction
code) just before it.  Output: one table per interface, sorted by code.
"""
import re, subprocess, sys, collections

def run(*a):
    return subprocess.run(a, capture_output=True, text=True, check=True).stdout

def demangle(names):
    out = run('c++filt', *names) if names else ''
    return dict(zip(names, out.splitlines()))

def analyse(path):
    # exported function symbols
    syms = {}
    for line in run('nm', '-D', '--defined-only', path).splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[1] in 'TtWw':
            syms[int(parts[0], 16)] = parts[2]
    dem = demangle(list(syms.values()))
    # interface descriptors
    descs = sorted(set(re.findall(r'vendor\.mediatek\.hardware\.mtkradioex\.[a-z]+\.I[A-Za-z]+', run('strings', path))))
    # disassembly
    dis = run('llvm-objdump', '-d', '--no-show-raw-insn', path)
    funcs = collections.OrderedDict()
    cur = None
    for line in dis.splitlines():
        m = re.match(r'^([0-9a-f]+) <(.+)>:$', line)
        if m:
            cur = m.group(2); funcs[cur] = []
            continue
        if cur is not None and line.strip():
            funcs[cur].append(line)
    results = collections.defaultdict(list)  # class -> [(code, method)]
    for mangled, body in funcs.items():
        d = dem.get(mangled)
        if not d or '::Bp' not in d:
            continue
        qual = d[:d.index('(')]
        cls = qual.rsplit('::', 2)[-2]
        meth = d[len(qual) - len(qual.rsplit('::', 1)[-1]):]
        code = None
        last_w1 = None
        for ins in body:
            m = re.search(r'\b(mov|orr)\s+w1,\s*(?:wzr,\s*)?#(0x[0-9a-f]+|\d+)', ins)
            if m:
                last_w1 = int(m.group(2), 0)
            if 'AIBinder_transact' in ins:
                code = last_w1
                break
        if code is None:
            continue
        results[cls].append((code, meth))
    return descs, results

PLAIN = [
    ('std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char> >', 'string'),
    ('std::__1::shared_ptr<', 'sp<'),
    ('std::__1::optional<', 'optional<'),
    ('std::__1::array<', 'array<'),
    ('aidl::android::hardware::radio::', 'radio::'),
    ('aidl::vendor::mediatek::hardware::mtkradioex::', 'mtk::'),
    ('ndk::', ''),
]

def _matching(sig, i):
    """Index of the '>' closing the '<' at position i."""
    depth = 0
    for j in range(i, len(sig)):
        if sig[j] == '<':
            depth += 1
        elif sig[j] == '>':
            depth -= 1
            if depth == 0:
                return j
    return -1

def simplify(sig):
    """Shorten the demangled C++ signature so a line stays readable."""
    for pat, rep in PLAIN:
        sig = sig.replace(pat, rep)
    key = 'std::__1::vector<'
    start = 0
    while True:
        i = sig.find(key, start)
        if i < 0:
            break
        j = _matching(sig, i + len(key) - 1)
        if j < 0:
            # No closing '>' (a cut-off signature): leave this one as it is
            start = i + 1
            continue
        inner = sig[i + len(key):j]
        # inner = "T, std::__1::allocator<T >"  -> keep T only; the allocator at the top
        # level, not one of a vector inside T
        k = _top_level(inner, ', std::__1::allocator<')
        elem = inner[:k] if k > 0 else inner
        sig = sig[:i] + 'vector<' + elem + '>' + sig[j + 1:]
    return sig

def _top_level(s, needle):
    """Index of needle outside all <...> of s, or -1."""
    depth = 0
    for i, ch in enumerate(s):
        if depth == 0 and s.startswith(needle, i):
            return i
        if ch == '<':
            depth += 1
        elif ch == '>':
            depth -= 1
    return -1


def main():
    for path in sys.argv[1:]:
        descs, results = analyse(path)
        print(f'### {path.rsplit("/",1)[-1]}')
        print('descriptors: ' + ', '.join(descs))
        for cls in sorted(results):
            print(f'\n== {cls[2:]} ==')
            for code, meth in sorted(results[cls]):
                if code == 0xffffff:   # getInterfaceVersion/Hash meta transactions
                    continue
                print(f'{code:4d}  {simplify(meth)}')
        print()

main()
