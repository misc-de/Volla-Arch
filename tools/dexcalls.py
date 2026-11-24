#!/usr/bin/env python3
"""Minimal dex reader: for every method of the classes matching a prefix, print the
sequence of invoked methods, field accesses, const-strings and small literals.
Usage: dexcalls.py classes.dex <class-prefix> [method-substring]"""
import struct
import sys

# instruction length in 16-bit code units, by opcode
L = [1] * 256
def rng(a, b, n):
    for i in range(a, b + 1):
        L[i] = n
rng(0x02, 0x02, 2); rng(0x03, 0x03, 3); rng(0x05, 0x05, 2); rng(0x06, 0x06, 3)
rng(0x08, 0x08, 2); rng(0x09, 0x09, 3); rng(0x13, 0x13, 2); rng(0x14, 0x14, 3)
rng(0x15, 0x16, 2); rng(0x17, 0x17, 3); rng(0x18, 0x18, 5); rng(0x19, 0x1a, 2)
rng(0x1b, 0x1b, 3); rng(0x1c, 0x1c, 2); rng(0x1f, 0x20, 2); rng(0x22, 0x23, 2)
rng(0x24, 0x26, 3); rng(0x29, 0x29, 2); rng(0x2a, 0x2c, 3); rng(0x2d, 0x3d, 2)
rng(0x44, 0x6d, 2); rng(0x6e, 0x72, 3); rng(0x74, 0x78, 3); rng(0x90, 0xaf, 2)
rng(0xd0, 0xe2, 2); rng(0xfa, 0xfb, 4); rng(0xfc, 0xfd, 3); rng(0xfe, 0xff, 2)


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
    prefix = sys.argv[2]
    msub = sys.argv[3] if len(sys.argv) > 3 else None
    verbose = len(sys.argv) > 4
    h = struct.unpack_from('<8x4x20x8I8I', d, 0)
    (_, _, _, _, _, _, sid_n, sid_o) = h[0:8]
    tid_n, tid_o, pid_n, pid_o, fid_n, fid_o, mid_n, mid_o = h[8:16]
    cd_n, cd_o = struct.unpack_from('<2I', d, 0x60)

    def string(i):
        off = struct.unpack_from('<I', d, sid_o + 4 * i)[0]
        _, o = uleb(d, off)
        e = d.index(b'\0', o)
        return d[o:e].decode('utf-8', 'replace')

    def typ(i):
        return string(struct.unpack_from('<I', d, tid_o + 4 * i)[0])

    def short(t):
        return t.split('/')[-1].rstrip(';')

    def method(i):
        cls, proto, name = struct.unpack_from('<HHI', d, mid_o + 8 * i)
        return '%s.%s' % (short(typ(cls)), string(name))

    def field(i):
        cls, ft, name = struct.unpack_from('<HHI', d, fid_o + 8 * i)
        return '%s.%s' % (short(typ(cls)), string(name))

    def proto_of(i):
        _, proto, _ = struct.unpack_from('<HHI', d, mid_o + 8 * i)
        _, ret, par = struct.unpack_from('<III', d, pid_o + 12 * proto)
        ps = []
        if par:
            n = struct.unpack_from('<I', d, par)[0]
            ps = [short(typ(struct.unpack_from('<H', d, par + 4 + 2 * k)[0])) for k in range(n)]
        return '(%s)%s' % (','.join(ps), short(typ(ret)))

    for c in range(cd_n):
        cls_idx, _, _, _, _, _, cdata, _ = struct.unpack_from('<8I', d, cd_o + 32 * c)
        cname = typ(cls_idx)
        if not cname.startswith(prefix) or not cdata:
            continue
        o = cdata
        sf, o = uleb(d, o); inf, o = uleb(d, o); dm, o = uleb(d, o); vm, o = uleb(d, o)
        for _ in range(sf + inf):
            _, o = uleb(d, o); _, o = uleb(d, o)
        midx = 0
        for k in range(dm + vm):
            if k == dm:
                midx = 0
            diff, o = uleb(d, o); _, o = uleb(d, o); code, o = uleb(d, o)
            midx += diff
            mname = method(midx)
            if msub and msub not in mname:
                continue
            print('\n## %s%s' % (cname[1:-1].replace('/', '.') + '.' + mname.split('.', 1)[1], proto_of(midx)))
            if not code:
                continue
            n = struct.unpack_from('<I', d, code + 12)[0]
            ins = struct.unpack_from('<%dH' % n, d, code + 16)
            regs, ins_n = struct.unpack_from('<HH', d, code)
            if verbose:
                print('  regs=%d ins=%d (params start at v%d)' % (regs, ins_n, regs - ins_n))
            pc = 0
            out = []
            while pc < n:
                u = ins[pc]; op = u & 0xff
                if op == 0 and u in (0x100, 0x200, 0x300):
                    if u == 0x100:
                        if verbose:
                            n_ = ins[pc + 1]; k0 = ins[pc + 2] | (ins[pc + 3] << 16)
                            tg = [struct.unpack('<i', struct.pack('<HH', ins[pc + 4 + 2 * j], ins[pc + 5 + 2 * j]))[0] for j in range(n_)]
                            out.append('%04x packed-switch-payload first_key=%d rel_targets=%s' % (pc, k0, tg))
                        pc += ins[pc + 1] * 2 + 4
                    elif u == 0x200:
                        pc += ins[pc + 1] * 4 + 2
                    else:
                        w = ins[pc + 1]; sz = ins[pc + 2] | (ins[pc + 3] << 16)
                        pc += (sz * w + 1) // 2 + 4
                    continue
                if verbose:
                    a = (u >> 8) & 0xff
                    if 0x6e <= op <= 0x72:
                        cnt = u >> 12; g = (u >> 8) & 0xf; w = ins[pc + 2]
                        rs = [w & 0xf, (w >> 4) & 0xf, (w >> 8) & 0xf, (w >> 12) & 0xf, g][:cnt]
                        out.append('%04x call %s %s' % (pc, method(ins[pc + 1]), ','.join('v%d' % r for r in rs)))
                    elif 0x74 <= op <= 0x78:
                        out.append('%04x call/range %s v%d..+%d' % (pc, method(ins[pc + 1]), ins[pc + 2], a))
                    elif op == 0x12:
                        v = (u >> 12) & 0xf
                        out.append('%04x v%d = %d' % (pc, (u >> 8) & 0xf, v - 16 if v > 7 else v))
                    elif op == 0x13:
                        out.append('%04x v%d = %d' % (pc, a, struct.unpack('<h', struct.pack('<H', ins[pc + 1]))[0]))
                    elif op == 0x1a:
                        out.append('%04x v%d = %r' % (pc, a, string(ins[pc + 1])))
                    elif op in (0x0c, 0x0a, 0x0b):
                        out.append('%04x v%d = result' % (pc, a))
                    elif op in (0x07, 0x01):
                        out.append('%04x v%d = v%d' % (pc, (u >> 8) & 0xf, u >> 12))
                    elif op in (0x08, 0x02):
                        out.append('%04x v%d = v%d' % (pc, a, ins[pc + 1]))
                    elif op == 0x4d:
                        out.append('%04x v%d[v%d] = v%d' % (pc, ins[pc + 1] & 0xff, ins[pc + 1] >> 8, a))
                    elif op == 0x23:
                        out.append('%04x v%d = new %s[v%d]' % (pc, (u >> 8) & 0xf, short(typ(ins[pc + 1])), u >> 12))
                    elif 0x52 <= op <= 0x5f:
                        out.append('%04x %s v%d <-> v%d.%s' % (pc, 'iget' if op < 0x59 else 'iput', (u >> 8) & 0xf, u >> 12, field(ins[pc + 1])))
                    else:
                        out.append('%04x op %02x a=%d' % (pc, op, a))
                    pc += L[op]
                    continue
                if op in range(0x6e, 0x73) or op in range(0x74, 0x79):
                    out.append('call ' + method(ins[pc + 1]))
                elif op == 0x1a:
                    out.append('str %r' % string(ins[pc + 1]))
                elif op == 0x1b:
                    out.append('str %r' % string(ins[pc + 1] | (ins[pc + 2] << 16)))
                elif op == 0x12:
                    v = (u >> 12) & 0xf
                    out.append('lit %d' % (v - 16 if v > 7 else v))
                elif op == 0x13:
                    out.append('lit %d' % struct.unpack('<h', struct.pack('<H', ins[pc + 1]))[0])
                elif 0x52 <= op <= 0x6d:
                    out.append(('get ' if (op - 0x52) % 14 < 7 else 'put ') + field(ins[pc + 1]))
                elif op == 0x2b or op == 0x2c:
                    out.append('switch')
                pc += L[op]
            print('  ' + '\n  '.join(out))


main()
