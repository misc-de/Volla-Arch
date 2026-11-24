# The reverse-engineering parsers: tools/dexconsts.py and tools/dexcalls.py (dex files) and
# tools/aidl-codes.py (AIDL transaction codes from a disassembly). Each runs on a synthetic input:
# a small dex file built here, and stand-ins for nm, c++filt, strings and llvm-objdump.

import struct
import subprocess
import sys

import pytest

from conftest import TOOLS


def uleb(n):
    out = bytearray()
    while True:
        b = n & 0x7f
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


class Dex:
    """Lays out a minimal dex file: the id tables, class data, static values and code."""

    def __init__(self):
        self.strings, self.types, self.fields, self.methods, self.protos = [], [], [], [], []
        # (type, static fields [(field, value bytes)], direct methods [(m, code)][, virtual
        # methods]); statics and methods None: a class without class data
        self.classes = []

    def s(self, text):
        if text not in self.strings:
            self.strings.append(text)
        return self.strings.index(text)

    def t(self, desc):
        i = self.s(desc)
        if i not in self.types:
            self.types.append(i)
        return self.types.index(i)

    def field(self, cls, typ, name):
        self.fields.append((self.t(cls), self.t(typ), self.s(name)))
        return len(self.fields) - 1

    def proto(self, ret, params):
        self.protos.append((self.s("V" + "I" * len(params)), self.t(ret), [self.t(p) for p in params]))
        return len(self.protos) - 1

    def method(self, cls, proto, name):
        self.methods.append((self.t(cls), proto, self.s(name)))
        return len(self.methods) - 1

    def build(self):
        out = bytearray(0x70)
        def here(align=4):
            while len(out) % align:
                out.append(0)
            return len(out)
        # string data first, then the tables pointing at it
        sdata = []
        for text in self.strings:
            sdata.append(here(1))
            out += uleb(len(text)) + text.encode() + b"\0"
        sid = here()
        for off in sdata:
            out += struct.pack("<I", off)
        tid = here()
        for si in self.types:
            out += struct.pack("<I", si)
        plists = []
        for _, _, params in self.protos:
            if not params:
                plists.append(0)
                continue
            plists.append(here())
            out += struct.pack("<I", len(params)) + b"".join(struct.pack("<H", p) for p in params)
        pid = here()
        for (shorty, ret, _), pl in zip(self.protos, plists):
            out += struct.pack("<III", shorty, ret, pl)
        fid = here()
        for c, t, n in self.fields:
            out += struct.pack("<HHI", c, t, n)
        mid = here()
        for c, p, n in self.methods:
            out += struct.pack("<HHI", c, p, n)
        # code items, class data, static values
        meta = []
        for typ, statics, methods, *virtual in self.classes:
            if statics is None and methods is None:
                meta.append((self.t(typ), 0, 0))
                continue
            virtual = virtual[0] if virtual else []
            codes = []
            for _, insns in methods + virtual:
                if insns is None:
                    codes.append(0)
                    continue
                codes.append(here())
                out += struct.pack("<HHHHII", 4, 1, 1, 0, 0, len(insns))
                out += b"".join(struct.pack("<H", u) for u in insns)
            cdata = here(1)
            out += uleb(len(statics)) + uleb(0) + uleb(len(methods)) + uleb(len(virtual))
            prev = 0
            for f, _ in statics:
                out += uleb(f - prev) + uleb(0x19)
                prev = f
            # direct, then virtual methods, each list with its own index differences
            for group, group_codes in ((methods, codes[:len(methods)]),
                                       (virtual, codes[len(methods):])):
                prev = 0
                for (m, _), code in zip(group, group_codes):
                    out += uleb(m - prev) + uleb(0x1 if group is virtual else 0x9) + uleb(code)
                    prev = m
            sval = here(1)
            out += uleb(len(statics)) + b"".join(v for _, v in statics)
            meta.append((self.t(typ), cdata, sval if statics else 0))
        cd = here()
        for typ, cdata, sval in meta:
            out += struct.pack("<8I", typ, 1, 0, 0, 0, 0, cdata, sval)
        struct.pack_into("<4I", out, 0x38, len(self.strings), sid, len(self.types), tid)
        struct.pack_into("<2I", out, 0x48, len(self.protos), pid)
        struct.pack_into("<2I", out, 0x50, len(self.fields), fid)
        struct.pack_into("<2I", out, 0x58, len(self.methods), mid)
        struct.pack_into("<2I", out, 0x60, len(self.classes), cd)
        out[:8] = b"dex\n039\0"
        return bytes(out)


WFC = "Lcom/mtk/ims/WfcCodes;"


def sample_dex():
    d = Dex()
    a = d.field(WFC, "I", "WFC_ERR_A")
    b = d.field(WFC, "I", "WFC_ERR_B")
    flag = d.field(WFC, "Z", "WFC_FLAG")
    name = d.field(WFC, "Ljava/lang/String;", "WFC_NAME")
    big = d.field(WFC, "I", "WFC_BIG")
    other = d.field("Lcom/other/X;", "I", "OTHER")
    p = d.proto("V", ["I"])
    call = d.method(WFC, p, "call")
    helper = d.method(WFC, d.proto("I", []), "helper")
    hello = d.s("hello")
    insns = [0x3012,                      # const/4 v0, #3
             0xf112,                      # const/4 v1, #-1
             0x001a, hello,               # const-string v0, "hello"
             0x106e, helper, 0x0000,      # invoke-virtual {v0}, helper
             0x0060, a,                   # sget v0, WFC_ERR_A
             0x0067, b,                   # sput v0, WFC_ERR_B
             0x0013, 0xff85,              # const/16 v0, #-123
             0x002b, 0x0006, 0x0000,      # packed-switch v0, +6
             0x000e,                      # return-void
             0x0000,                      # nop (alignment)
             0x0100, 1, 0, 0, 5, 0]       # packed-switch payload: one target
    d.classes.append((WFC, [
        (a, bytes([0x04, 0x01])), (b, bytes([0x04, 0xfe])), (flag, bytes([0x3f])),
        (name, bytes([0x1e])), (big, bytes([0x44, 0x70, 0x11, 0x01]))],
        [(call, insns), (helper, None)]))
    d.classes.append(("Lcom/other/X;", [(other, bytes([0x04, 0x05]))], []))
    return d.build()


def run(script, *args, env=None):
    return subprocess.run([sys.executable, str(TOOLS / script), *map(str, args)],
                          capture_output=True, text=True, timeout=30, env=env)


def test_dexconsts_prints_the_int_constants_of_matching_classes(tmp_path):
    dex = tmp_path / "classes.dex"
    dex.write_bytes(sample_dex())
    r = run("dexconsts.py", dex, "^WFC_ERR")
    assert r.returncode == 0, r.stderr
    lines = [l.split() for l in r.stdout.splitlines()]
    assert lines == [["##", WFC], ["WFC_ERR_A", "1"], ["WFC_ERR_B", "-2"], ["WFC_BIG", "70000"]]
    assert run("dexconsts.py", dex, "^NOTHING$").stdout == ""


def test_dexcalls_lists_calls_strings_literals_and_fields(tmp_path):
    dex = tmp_path / "classes.dex"
    dex.write_bytes(sample_dex())
    r = run("dexcalls.py", dex, "Lcom/mtk/")
    assert r.returncode == 0, r.stderr
    assert r.stdout.split("\n## ")[1:] == [
        "com.mtk.ims.WfcCodes.call(I)V\n"
        "  lit 3\n  lit -1\n  str 'hello'\n  call WfcCodes.helper\n  get WfcCodes.WFC_ERR_A\n"
        "  put WfcCodes.WFC_ERR_B\n  lit -123\n  switch\n",
        "com.mtk.ims.WfcCodes.helper()I\n"]


def test_dexcalls_method_filter_and_verbose(tmp_path):
    dex = tmp_path / "classes.dex"
    dex.write_bytes(sample_dex())
    r = run("dexcalls.py", dex, "Lcom/mtk/", "call", "-v")
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "## com.mtk.ims.WfcCodes.call(I)V" in out and "## com.mtk.ims.WfcCodes.helper" not in out
    assert "regs=4 ins=1 (params start at v3)" in out
    assert "0000 v0 = 3" in out and "0001 v1 = -1" in out
    assert "0002 v0 = 'hello'" in out
    assert "0004 call WfcCodes.helper v0" in out
    assert "000b v0 = -123" in out
    assert "packed-switch-payload first_key=0 rel_targets=[5]" in out
    assert run("dexcalls.py", dex, "Lnone/").stdout == ""


# --- aidl-codes ---------------------------------------------------------------------------------

MANGLED = {
    "_ZN4aidl6vendor8mediatek8hardware10mtkradioex3ims15BpMtkRadioExIms3getEPi":
        "aidl::vendor::mediatek::hardware::mtkradioex::ims::BpMtkRadioExIms::get(int*)",
    "_ZN4aidl6vendor8mediatek8hardware10mtkradioex3ims15BpMtkRadioExIms7setListEv":
        "aidl::vendor::mediatek::hardware::mtkradioex::ims::BpMtkRadioExIms::setList("
        "std::__1::vector<std::__1::basic_string<char, std::__1::char_traits<char>, "
        "std::__1::allocator<char> >, std::__1::allocator<std::__1::basic_string<char, "
        "std::__1::char_traits<char>, std::__1::allocator<char> > > > const&)",
    "_ZN4aidl6vendor8mediatek8hardware10mtkradioex3ims15BpMtkRadioExIms19getInterfaceVersionEPi":
        "aidl::vendor::mediatek::hardware::mtkradioex::ims::BpMtkRadioExIms::getInterfaceVersion(int*)",
    "_ZN4aidl6vendor8mediatek8hardware10mtkradioex3ims15BnMtkRadioExIms10onTransactEv":
        "aidl::vendor::mediatek::hardware::mtkradioex::ims::BnMtkRadioExIms::onTransact(unsigned int)",
    "_ZN4aidl6vendor8mediatek8hardware10mtkradioex3ims15BpMtkRadioExIms4pingEv":
        "aidl::vendor::mediatek::hardware::mtkradioex::ims::BpMtkRadioExIms::ping()",
}
NAMES = list(MANGLED)

OBJDUMP = f"""
libfoo.so:	file format elf64-littleaarch64

Disassembly of section .text:

0000000000001000 <{NAMES[0]}>:
    1000:      	mov	w1, #0x5
    1004:      	mov	x2, x19
    1008:      	bl	0x9000 <AIBinder_transact@plt>
    100c:      	mov	w1, #0x9

0000000000001100 <{NAMES[1]}>:
    1100:      	mov	w1, #3
    1104:      	orr	w1, wzr, #0x7
    1108:      	bl	0x9000 <AIBinder_transact@plt>

0000000000001200 <{NAMES[2]}>:
    1200:      	mov	w1, #0xffffff
    1204:      	bl	0x9000 <AIBinder_transact@plt>

0000000000001300 <{NAMES[3]}>:
    1300:      	mov	w1, #0x1
    1304:      	bl	0x9000 <AIBinder_transact@plt>

0000000000001400 <{NAMES[4]}>:
    1400:      	ret
"""


def test_aidl_codes_from_the_disassembly(tmp_path, fake_bin):
    nm = "\n".join(f"{0x1000 + 0x100 * i:016x} T {n}" for i, n in enumerate(NAMES))
    fake_bin.stub("nm", f"cat <<'EOF'\n{nm}\n0000000000000000 U AIBinder_transact\nEOF")
    cases = "\n".join(f"{m}) echo '{d}' ;;" for m, d in MANGLED.items())
    fake_bin.stub("c++filt", f'for a; do case $a in\n{cases}\n*) echo "$a" ;;\nesac; done')
    fake_bin.stub("strings", "echo vendor.mediatek.hardware.mtkradioex.ims.IMtkRadioExIms; echo junk")
    (tmp_path / "objdump.txt").write_text(OBJDUMP)
    fake_bin.stub("llvm-objdump", f"cat {tmp_path / 'objdump.txt'}")
    r = run("aidl-codes.py", "/vendor/lib64/libfoo.so", env=fake_bin.env(isolated=True))
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == [
        "### libfoo.so",
        "descriptors: vendor.mediatek.hardware.mtkradioex.ims.IMtkRadioExIms",
        "",
        "== MtkRadioExIms ==",
        "   5  get(int*)",
        "   7  setList(vector<string> const&)",
        ""]
    assert fake_bin.calls("nm") == [["-D", "--defined-only", "/vendor/lib64/libfoo.so"]]


def test_simplify_signatures():
    src = (TOOLS / "aidl-codes.py").read_text()
    assert src.rstrip().endswith("main()")
    ns = {}
    exec(compile(src.rstrip()[:-len("main()")], str(TOOLS / "aidl-codes.py"), "exec"), ns)
    simplify = ns["simplify"]
    assert simplify("f(std::__1::optional<std::__1::vector<int, std::__1::allocator<int> > >*)") == \
        "f(optional<vector<int> >*)"
    assert simplify("g(std::__1::shared_ptr<aidl::android::hardware::radio::RadioIndication> const&)") == \
        "g(sp<radio::RadioIndication> const&)"
    assert simplify("ndk::ScopedAStatus x()") == "ScopedAStatus x()"


def aidl_simplify():
    src = (TOOLS / "aidl-codes.py").read_text()
    ns = {}
    exec(compile(src.rstrip()[:-len("main()")], str(TOOLS / "aidl-codes.py"), "exec"), ns)
    return ns["simplify"]


def test_simplify_nested_vector_keeps_its_brackets():
    simplify = aidl_simplify()
    assert simplify("h(std::__1::vector<std::__1::vector<int, std::__1::allocator<int> >, "
                    "std::__1::allocator<std::__1::vector<int, std::__1::allocator<int> > > >)") == \
        "h(vector<vector<int>>)"


# --- virtual methods, payloads, the remaining instructions ---------------------------------------

VIRT = "Lcom/mtk/ims/Virt;"


def virtual_dex():
    d = Dex()
    p = d.proto("V", [])
    # The virtual methods have the lower indices: their index differences start again at 0
    virt = d.method(VIRT, p, "virt")
    other = d.method(VIRT, p, "zother")
    direct = d.method(VIRT, p, "direct")
    count = d.field(VIRT, "I", "count")
    arr = d.t("[I")
    jumbo = d.s("jumbo")
    insns = [0x0274, virt, 0x0005,           # invoke-virtual/range {v5..v6}, virt
             0x000c,                         # move-result-object v0
             0x1007,                         # move-object v0, v1
             0x0208, 0x0010,                 # move-object/from16 v2, v16
             0x004d, 0x0201,                 # aput-object v0, v1, v2
             0x1023, arr,                    # new-array v0, v1, [I
             0x1052, count,                  # iget v0, v1, count
             0x1059, count,                  # iput v0, v1, count
             0x001b, jumbo, 0,               # const-string/jumbo v0, "jumbo"
             0x000e,                         # return-void
             0x0200, 1, 7, 0, 3, 0,          # sparse-switch payload: one key, one target
             0x2012,                         # const/4 v0, #2
             0x0300, 4, 3, 0, 1, 0, 2, 0, 3, 0,  # fill-array-data payload: 3 ints
             0x3012]                         # const/4 v0, #3
    d.classes.append((VIRT, [], [(direct, None)], [(virt, insns), (other, None)]))
    d.classes.append(("Lcom/mtk/ims/Marker;", None, None))  # no class data
    return d.build()


def test_dexcalls_virtual_methods_and_payloads(tmp_path):
    dex = tmp_path / "classes.dex"
    dex.write_bytes(virtual_dex())
    r = run("dexcalls.py", dex, "Lcom/mtk/")
    assert r.returncode == 0, r.stderr
    assert r.stdout.split("\n## ")[1:] == [
        "com.mtk.ims.Virt.direct()V\n",
        "com.mtk.ims.Virt.virt()V\n"
        "  call Virt.virt\n  get Virt.count\n  put Virt.count\n  str 'jumbo'\n  lit 2\n  lit 3\n",
        "com.mtk.ims.Virt.zother()V\n"]


def test_dexcalls_verbose_decodes_each_instruction(tmp_path):
    dex = tmp_path / "classes.dex"
    dex.write_bytes(virtual_dex())
    r = run("dexcalls.py", dex, "Lcom/mtk/ims/Virt", "virt", "-v")
    assert r.returncode == 0, r.stderr
    body = r.stdout.split("## com.mtk.ims.Virt.virt()V\n")[1].splitlines()
    assert body == ["  regs=4 ins=1 (params start at v3)",
                    "  0000 call/range Virt.virt v5..+2",
                    "  0003 v0 = result",
                    "  0004 v0 = v1",
                    "  0005 v2 = v16",
                    "  0007 v1[v2] = v0",
                    "  0009 v0 = new [I[v1]",
                    "  000b iget v0 <-> v1.Virt.count",
                    "  000d iput v0 <-> v1.Virt.count",
                    "  000f op 1b a=0",
                    "  0012 op 0e a=0",
                    "  0019 v0 = 2",   # the sparse-switch payload (6 units) skipped
                    "  0024 v0 = 3"]   # the fill-array-data payload (10 units) skipped


def test_dexconsts_long_names_and_classes_without_data(tmp_path):
    d = Dex()
    long_name = "WFC_" + "X" * 130  # its length takes two uleb128 bytes
    f = d.field(WFC, "I", long_name)
    d.classes.append(("Lcom/mtk/ims/Marker;", None, None))
    d.classes.append((WFC, [(f, bytes([0x04, 0x07]))], []))
    dex = tmp_path / "classes.dex"
    dex.write_bytes(d.build())
    r = run("dexconsts.py", dex, "^WFC_X+$")
    assert r.returncode == 0, r.stderr
    assert [l.split() for l in r.stdout.splitlines()] == [["##", WFC], [long_name, "7"]]


def test_matching_bracket():
    src = (TOOLS / "aidl-codes.py").read_text()
    ns = {}
    exec(compile(src.rstrip()[:-len("main()")], str(TOOLS / "aidl-codes.py"), "exec"), ns)
    assert ns["_matching"]("a<b<c>d>e", 1) == 7
    assert ns["_matching"]("a<b<c>", 1) == -1


def simplify_limited(sig):
    """simplify(sig) in a process of its own with 1 GiB of memory: a signature that makes it
    grow without end fails there instead of filling the test run's memory."""
    code = ("import resource, sys\n"
            "resource.setrlimit(resource.RLIMIT_AS, (1 << 30, 1 << 30))\n"
            f"src = open({str(TOOLS / 'aidl-codes.py')!r}).read().rstrip()[:-len('main()')]\n"
            "ns = {}\n"
            f"exec(compile(src, {str(TOOLS / 'aidl-codes.py')!r}, 'exec'), ns)\n"
            f"print(ns['simplify']({sig!r}))\n")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-300:]
    return r.stdout.removesuffix("\n")


def test_simplify_leaves_an_unbalanced_vector_alone():
    sig = "f(std::__1::vector<int, std::__1::allocator<int"
    assert simplify_limited(sig) == sig


def test_simplify_goes_on_after_an_unbalanced_vector():
    # A complete vector inside the cut-off one is still shortened
    sig = ("f(std::__1::vector<std::__1::vector<int, std::__1::allocator<int> >, "
           "std::__1::allocator<x")
    assert simplify_limited(sig) == "f(std::__1::vector<vector<int>, std::__1::allocator<x"


def test_simplify_vector_without_allocator():
    # Newer libc++ demangles without the default allocator: the vector stays as it is written
    assert aidl_simplify()("f(std::__1::vector<int> const&)") == "f(vector<int> const&)"
