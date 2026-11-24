#!/usr/bin/env python3
"""Print the static final int constants of every dex class that declares a field whose name
matches a pattern, e.g. the WFC error codes in MediaTek's ImsService.apk.
Usage: dexconsts.py classes.dex <field-name-regex>"""
import re
import struct
import sys


def uleb(b, o):
    r = s = 0
    while True:
        x = b[o]; o += 1
        r |= (x & 0x7f) << s
        if x < 0x80:
            return r, o
        s += 7


def main():
    d = open(sys.argv[1], 'rb').read()
    pat = re.compile(sys.argv[2])
    sid_n, sid_o, tid_n, tid_o = struct.unpack_from('<4I', d, 0x38)
    fid_n, fid_o = struct.unpack_from('<2I', d, 0x50)
    cd_n, cd_o = struct.unpack_from('<2I', d, 0x60)

    def string(i):
        off = struct.unpack_from('<I', d, sid_o + 4 * i)[0]
        _, o = uleb(d, off)
        return d[o:d.index(b'\0', o)].decode('utf-8', 'replace')

    def typ(i):
        return string(struct.unpack_from('<I', d, tid_o + 4 * i)[0])

    def field(i):
        cls, ft, name = struct.unpack_from('<HHI', d, fid_o + 8 * i)
        return cls, typ(ft), string(name)

    for c in range(cd_n):
        cls_idx, _, _, _, _, _, cdata, sval = struct.unpack_from('<8I', d, cd_o + 32 * c)
        if not cdata:
            continue
        o = cdata
        sf, o = uleb(d, o); _, o = uleb(d, o); _, o = uleb(d, o); _, o = uleb(d, o)
        fields, idx = [], 0
        for _ in range(sf):
            diff, o = uleb(d, o); _, o = uleb(d, o)
            idx += diff
            fields.append(field(idx))
        if not any(pat.search(f[2]) for f in fields) or not sval:
            continue
        # encoded_array of static values, in field order
        n, o = uleb(d, sval)
        print('## %s' % typ(cls_idx))
        for k in range(n):
            vt = d[o]; o += 1
            vtype, size = vt & 0x1f, (vt >> 5) + 1
            raw = d[o:o + size] if vtype not in (0x1e, 0x1f) else b''
            if vtype in (0x1e, 0x1f):
                size = 0
            o += size
            val = int.from_bytes(raw, 'little', signed=True) if raw else (vt >> 5 if vtype == 0x1f else None)
            if k < len(fields) and vtype in (0x00, 0x02, 0x04, 0x06):
                print('  %-50s %s' % (fields[k][2], val))


main()
