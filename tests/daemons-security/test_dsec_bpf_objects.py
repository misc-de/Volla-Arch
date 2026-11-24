# The BPF objects of plinius-netlog (netlog.bpf.c) and plinius-appaccess (appaccess.bpf.c), built
# as their PKGBUILDs build them (clang -target bpf -O2 -g). Loading and running them needs the
# phone's kernel (and root); what can be checked here is what the daemons rely on when they load
# the object: it compiles without warnings, every program is a cgroup program (the daemons attach
# each one with bpf_program__attach_cgroup), the maps they look up by name exist with the type,
# size and flags the code expects (read from the BTF that libbpf reads them from), the records
# and keys have the layout the daemon (compiled for the host here) reads, and netlog's programs
# never refuse anything.

import re
import shutil
import struct
import subprocess

import pytest

from conftest import PKG, compile_c

NETLOG = PKG / "plinius-netlog"
APPACCESS = PKG / "plinius-appaccess"

HASH, LRU_HASH, SK_STORAGE, RINGBUF = 1, 9, 24, 27
BPF_F_NO_PREALLOC = 1


def build(src, out):
    for tool in ("clang", "llvm-objcopy", "llvm-readelf", "llvm-objdump"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} missing on the host")
    if not (r := subprocess.run(["clang", "-print-targets"], capture_output=True, text=True)).stdout \
            or not re.search(r"^\s*bpf\s", r.stdout, re.M):
        pytest.skip("clang without the BPF target")
    # As in the PKGBUILDs
    cmd = ["clang", "-target", "bpf", "-O2", "-g", "-Wall", f"-I{src.parent}", "-c", str(src),
           "-o", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=src.parent)
    if r.returncode != 0 and ("bpf/bpf_helpers.h" in r.stderr or "linux/bpf.h" in r.stderr):
        pytest.skip("libbpf or kernel headers missing on the host")
    assert r.returncode == 0, r.stderr
    assert r.stderr == "", r.stderr
    return out


class Btf:
    """The types of a .BTF section: enough to follow libbpf's map definitions"""

    EXTRA = {1: lambda v: 4, 3: lambda v: 12, 4: lambda v: 12 * v, 5: lambda v: 12 * v,
             6: lambda v: 8 * v, 13: lambda v: 8 * v, 14: lambda v: 4, 15: lambda v: 12 * v,
             17: lambda v: 4, 19: lambda v: 12 * v}

    def __init__(self, data):
        magic, _ver, _flags, hlen, toff, tlen, soff, slen = struct.unpack_from("<HBBIIIII", data, 0)
        assert magic == 0xeb9f
        types = data[hlen + toff:hlen + toff + tlen]
        self.strings = data[hlen + soff:hlen + soff + slen]
        self.types = [None]
        p = 0
        while p < len(types):
            name_off, info, size_or_type = struct.unpack_from("<III", types, p)
            kind, vlen = info >> 24 & 0x1f, info & 0xffff
            extra = types[p + 12:p + 12 + self.EXTRA.get(kind, lambda v: 0)(vlen)]
            self.types.append({"kind": kind, "name": self.name(name_off), "size": size_or_type,
                               "type": size_or_type, "vlen": vlen, "kflag": info >> 31, "extra": extra})
            p += 12 + len(extra)

    def name(self, off):
        return self.strings[off:self.strings.index(b"\0", off)].decode()

    def members(self, tid):
        t = self.types[tid]
        assert t["kind"] in (4, 5)
        for i in range(t["vlen"]):
            n, mt, off = struct.unpack_from("<III", t["extra"], 12 * i)
            yield self.name(n), mt, (off & 0xffffff if t["kflag"] else off) // 8

    def resolve(self, tid):
        """Through typedefs and qualifiers"""
        while self.types[tid]["kind"] in (8, 9, 10, 11, 18):
            tid = self.types[tid]["type"]
        return tid

    def size(self, tid):
        tid = self.resolve(tid)
        t = self.types[tid]
        if t["kind"] == 3:
            elem, _index, n = struct.unpack("<III", t["extra"])
            return self.size(elem) * n
        if t["kind"] == 2:
            return 8
        return t["size"]

    def by_name(self, name, kind=4):
        found = [i for i, t in enumerate(self.types) if t and t["kind"] == kind and t["name"] == name]
        assert len(found) == 1, name
        return found[0]

    def layout(self, name):
        sid = self.by_name(name)
        return self.size(sid), {m: off for m, _t, off in self.members(sid)}

    def maps(self):
        """{name: {"type": .., "max_entries": .., "key": size, "value": size, ...}} of .maps"""
        sec = [i for i, t in enumerate(self.types) if t and t["kind"] == 15 and t["name"] == ".maps"]
        assert len(sec) == 1
        out = {}
        for i in range(self.types[sec[0]]["vlen"]):
            var, _off, _size = struct.unpack_from("<III", self.types[sec[0]]["extra"], 12 * i)
            v = self.types[var]
            fields = {}
            for m, mt, _off in self.members(self.resolve(v["type"])):
                target = self.resolve(self.types[mt]["type"])  # every field is a pointer
                if self.types[target]["kind"] == 3:            # __uint(name, n): int (*name)[n]
                    fields[m] = struct.unpack("<III", self.types[target]["extra"])[2]
                else:                                          # __type(name, T): T *name
                    fields[m] = self.size(target)
            out[v["name"]] = fields
        return out


def sections(obj):
    out = subprocess.run(["llvm-readelf", "-SW", str(obj)], capture_output=True, text=True,
                         check=True).stdout
    return re.findall(r"\]\s+(\S+)\s+PROGBITS", out)


def btf(obj, tmp_path):
    dump = tmp_path / f"{obj.stem}.btf"
    subprocess.run(["llvm-objcopy", f"--dump-section=.BTF={dump}", str(obj)], check=True)
    return Btf(dump.read_bytes())


def programs(obj):
    """{program: [instructions]} from the disassembly"""
    out = subprocess.run(["llvm-objdump", "-d", "--no-show-raw-insn", str(obj)], capture_output=True,
                         text=True, check=True).stdout
    progs, current = {}, None
    for line in out.splitlines():
        if m := re.match(r"^[0-9a-f]+ <(\w+)>:", line):
            current = progs.setdefault(m.group(1), [])
        elif current is not None and (m := re.match(r"^\s+\d+:\s+(.*\S)", line)):
            current.append(m.group(1))
    return progs


def ringbuf_record_sizes(obj):
    """The size of every record the programs put into a ring buffer: the immediate in r2 for
    bpf_ringbuf_reserve (helper 131), in r3 for bpf_ringbuf_output (helper 130)"""
    sizes = set()
    for insns in programs(obj).values():
        for i, ins in enumerate(insns):
            reg = {"call 0x83": "2", "call 0x82": "3"}.get(ins)
            if reg is None:
                continue
            for prev in reversed(insns[:i]):
                if m := re.fullmatch(rf"[rw]{reg} = (\S+)", prev):
                    sizes.add(int(m.group(1), 0))
                    break
    return sizes


def host_layout(tmp_path, header, structs):
    """sizeof and member offsets as the daemon, compiled for the host, sees them"""
    lines = []
    for s, members in structs.items():
        lines.append(f'printf("{s} %zu", sizeof(struct {s}));')
        for m in members:
            lines.append(f'printf(" {m}=%zu", offsetof(struct {s}, {m}));')
        lines.append('printf("\\n");')
    src = tmp_path / "layout.c"
    src.write_text(f'#include <stddef.h>\n#include <stdio.h>\n#include "{header}"\n'
                   f'int main(void) {{ {" ".join(lines)} return 0; }}\n')
    exe = compile_c(tmp_path / "layout", [src], counted=False)
    out = {}
    for line in subprocess.run([str(exe)], capture_output=True, text=True, check=True).stdout.splitlines():
        name, size, *fields = line.split()
        out[name] = (int(size), {k: int(v) for k, v in (f.split("=") for f in fields)})
    return out


@pytest.fixture(scope="module")
def netlog(tmp_path_factory):
    return build(NETLOG / "netlog.bpf.c", tmp_path_factory.mktemp("bpf") / "netlog.bpf.o")


@pytest.fixture(scope="module")
def appaccess(tmp_path_factory):
    return build(APPACCESS / "appaccess.bpf.c", tmp_path_factory.mktemp("bpf") / "appaccess.bpf.o")


# --- plinius-netlog -------------------------------------------------------------------------------

def test_netlog_programs_are_cgroup_programs(netlog):
    secs = sections(netlog)
    progs = [s for s in secs if not s.startswith((".", "license"))]
    assert progs == ["cgroup/connect4", "cgroup/connect6", "cgroup/sendmsg4", "cgroup/sendmsg6",
                     "cgroup_skb/egress"]
    assert "license" in secs and ".maps" in secs


def test_netlog_maps(netlog, tmp_path):
    b = btf(netlog, tmp_path)
    maps = b.maps()
    assert set(maps) == {"events", "pending", "recent"}
    # The daemon finds the ring buffer by name; libbpf wants its size a power of two of pages
    assert maps["events"] == {"type": RINGBUF, "max_entries": 256 * 1024}
    assert maps["pending"] == {"type": SK_STORAGE, "map_flags": BPF_F_NO_PREALLOC, "key": 4,
                               "value": b.layout("pending")[0]}
    assert maps["recent"] == {"type": LRU_HASH, "max_entries": 4096, "key": b.layout("flow_key")[0],
                              "value": 8}
    # The pending event is the one reported: the whole record in front of the flag
    size, members = b.layout("pending")
    assert members == {"ev": 0, "armed": b.layout("netlog_event")[0]}


def test_netlog_event_as_the_daemon_reads_it(netlog, tmp_path):
    bpf = btf(netlog, tmp_path).layout("netlog_event")
    # Every member the BPF side writes, at the offset the daemon reads it from
    host = host_layout(tmp_path, NETLOG / "netlog.h", {"netlog_event": list(bpf[1])})
    assert list(bpf[1]) == ["cgroup_id", "uid", "pid", "addr", "port", "family", "proto", "kind",
                            "pad", "comm"]
    assert bpf == host["netlog_event"]
    # The records are whole events, both those reserved and the pending ones sent later
    assert ringbuf_record_sizes(netlog) == {host["netlog_event"][0]}


def test_netlog_never_refuses(netlog):
    progs = programs(netlog)
    assert set(progs) == {"netlog_connect4", "netlog_connect6", "netlog_sendmsg4", "netlog_sendmsg6",
                          "netlog_egress"}
    for name, insns in progs.items():
        exits = [i for i, ins in enumerate(insns) if ins == "exit"]
        # One way out, and it returns 1 (allow)
        assert len(exits) == 1, name
        assert insns[exits[0] - 1] in ("w0 = 0x1", "r0 = 0x1"), (name, insns[exits[0] - 1])


# --- plinius-appaccess ----------------------------------------------------------------------------

def test_appaccess_programs_are_cgroup_programs(appaccess):
    progs = [s for s in sections(appaccess) if not s.startswith((".", "license"))]
    assert progs == ["cgroup/dev", "cgroup/sock_create"]


def test_appaccess_maps_as_the_daemon_uses_them(appaccess, tmp_path):
    b = btf(appaccess, tmp_path)
    maps = b.maps()
    # Looked up by name in plinius-appaccessd (map_fd)
    assert set(maps) == {"devices", "cgroups", "counts", "recent", "events"}
    shared = ("aa_cgroup", "aa_count_key", "aa_count")
    host = host_layout(tmp_path, APPACCESS / "appaccess.h",
                       {**{s: list(b.layout(s)[1]) for s in shared},
                        "aa_event": ["cgroup_id", "uid", "res", "kind", "verdict"]})
    # devices: the daemon writes a guint32 dev_t and a guint32 resource
    assert maps["devices"] == {"type": HASH, "max_entries": 1024, "key": 4, "value": 4}
    # cgroups: keyed by the 64-bit cgroup id, the verdicts as the daemon writes them
    assert maps["cgroups"] == {"type": HASH, "max_entries": 4096, "key": 8,
                               "value": host["aa_cgroup"][0]}
    assert maps["counts"] == {"type": HASH, "max_entries": 8192, "key": host["aa_count_key"][0],
                              "value": host["aa_count"][0]}
    assert maps["recent"] == {"type": LRU_HASH, "max_entries": 4096, "key": host["aa_count_key"][0],
                              "value": 8}
    assert maps["events"] == {"type": RINGBUF, "max_entries": 64 * 1024}
    # Every member at the offset the daemon (compiled for the host) has it
    for s in shared:
        assert b.layout(s) == host[s], s
    assert list(b.layout("aa_count_key")[1]) == ["cgroup_id", "res", "pad"]
    # The events (a local struct, not in the BTF): as long as the daemon's struct aa_event
    assert ringbuf_record_sizes(appaccess) == {host["aa_event"][0]}


def test_appaccess_license_is_gpl(appaccess, tmp_path):
    # bpf_get_current_cgroup_id and the ring buffer are GPL-only helpers
    for obj in (appaccess,):
        dump = tmp_path / "license"
        subprocess.run(["llvm-objcopy", f"--dump-section=license={dump}", str(obj)], check=True)
        assert dump.read_bytes() == b"GPL\0"


def test_netlog_license_is_gpl(netlog, tmp_path):
    dump = tmp_path / "license"
    subprocess.run(["llvm-objcopy", f"--dump-section=license={dump}", str(netlog)], check=True)
    assert dump.read_bytes() == b"GPL\0"
