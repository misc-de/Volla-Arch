#!/usr/bin/env python3
# Symbol versions (modversions CRCs) of the running kernel, as Module.symvers lines for
# KBUILD_EXTRA_SYMBOLS (pkgbuilds/plinius-kmod/vmlinux.symvers). The kernel neither lists them in
# kallsyms nor has /proc/kcore, so they are read from the kernel image (UT's boot.img), at the
# offsets of __ksymtab/__kcrctab that kallsyms gives relative to _text.
# ksymtab entries are PREL32 (value, name, namespace offsets), kcrctab is a parallel u32 array.
#
# Usage: ssh root@phone 'echo 0 > /proc/sys/kernel/kptr_restrict; cat /proc/kallsyms;
#            echo 2 > /proc/sys/kernel/kptr_restrict' > kallsyms
#        tools/kernel-crcs.py boot.img kallsyms [symbol...] > vmlinux.symvers
#        (without symbols: all exports)
import gzip
import struct
import sys

boot, kallsyms, wanted = sys.argv[1], sys.argv[2], set(sys.argv[3:])
img = open(boot, 'rb').read()
if img[:8] == b'ANDROID!':
    size, = struct.unpack_from('<I', img, 8)
    img = img[4096:4096 + size]
if img[:2] == b'\x1f\x8b':
    img = gzip.decompress(img)

addr = {}
for line in open(kallsyms):
    a, _, name = line.split()[:3]
    if name == '_text' or name == 'linux_banner' or name.startswith(('__start___k', '__stop___k')):
        addr[name] = int(a, 16)
if not addr.get('_text'):
    sys.exit('kallsyms without addresses: kptr_restrict was not 0')
off = {n: a - addr['_text'] for n, a in addr.items()}
banner = img[off['linux_banner']:off['linux_banner'] + 64]
if not banner.startswith(b'Linux version '):
    sys.exit('the image does not belong to the running kernel')
print('kernel image: %s' % banner.split(b' (')[0].decode(), file=sys.stderr)

for kind in ('', '_gpl'):
    start, stop = off['__start___ksymtab' + kind], off['__stop___ksymtab' + kind]
    crcs = off['__start___kcrctab' + kind]
    for i in range((stop - start) // 12):
        entry = start + i * 12
        name_off, = struct.unpack_from('<i', img, entry + 4)
        s = entry + 4 + name_off
        name = img[s:img.index(b'\0', s)].decode()
        if wanted and name not in wanted:
            continue
        crc, = struct.unpack_from('<I', img, crcs + i * 4)
        print('0x%08x\t%s\tvmlinux\tEXPORT_SYMBOL%s\t' % (crc, name, kind.upper()))
