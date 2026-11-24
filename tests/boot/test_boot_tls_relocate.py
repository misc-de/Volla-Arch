# hybris-tls-relocate: patches Firefox's and Thunderbird's executable so its own TLS block leaves
# bionic's slots tp+16..tp+63 (Mali's GL driver writes there). A wrong patch gives a browser that
# crashes or silently shares memory with the GL driver, so the tests check exactly which bytes it
# changes and what it refuses. Synthetic aarch64 ELF files are built here; with an aarch64 cross
# compiler and qemu-user on the host, real binaries are patched and run as well.

import os
import shutil
import struct
import subprocess

import pytest

from conftest import PKG, REPO, compile_c, load_script

TOOL = PKG / "hybris-tls-relocate" / "hybris-tls-relocate"
PT_LOAD, PT_INTERP, PT_TLS = 1, 3, 7
SHT_DYNSYM, SHT_RELA = 11, 4


@pytest.fixture(scope="module")
def tls():
    return load_script(TOOL)


# --- aarch64 instructions ------------------------------------------------------------------------

def movz(rd, imm=0, hw=1):
    return 0xD2800000 | hw << 21 | imm << 5 | rd


def movk(rd, imm, hw=0):
    return 0xF2800000 | hw << 21 | imm << 5 | rd


def mrs_tp(rd):
    return 0xD53BD040 | rd


def add_imm(rd, rn, imm12, lsl12=False):
    return 0x91000000 | int(lsl12) << 22 | imm12 << 10 | rn << 5 | rd


def add_reg(rd, rn, rm):
    return 0x8B000000 | rm << 16 | rn << 5 | rd


RET = 0xD65F03C0
NOP = 0xD503201F


def relaxed_access(rd, offset):
    """What the linker makes of an initial-exec access in an executable."""
    return [movz(rd), movk(rd, offset), mrs_tp(1), add_reg(rd, 1, rd), RET]


# --- ELF files -----------------------------------------------------------------------------------

def elf(code, *, e_type=3, machine=183, ident=b"\x7fELF\x02\x01\x01", interp=True,
        tls=dict(memsz=8, align=4, vaddr=0x1fdc8, filesz=0), dynsym=(), rela=(),
        sh_addralign=8):
    """A small aarch64 executable: PT_INTERP, one executable PT_LOAD holding CODE, PT_TLS, and
    optionally .dynsym (st_info values) and .rela.dyn ((offset, info, addend) entries)."""
    phdrs = []
    ph_count = (1 if interp else 0) + 1 + (1 if tls else 0)
    body_at = 64 + 56 * ph_count
    body = bytearray()
    if interp:
        interp_at = body_at + len(body)
        body += b"/lib/ld-linux-aarch64.so.1\0"
        phdrs.append((PT_INTERP, 4, interp_at, interp_at, len(body), len(body), 1))
    body += b"\0" * (-(body_at + len(body)) % 16)
    code_at = body_at + len(body)
    code_bytes = b"".join(struct.pack("<I", w) for w in code)
    body += code_bytes
    phdrs.append((PT_LOAD, 5, code_at, code_at, len(code_bytes), len(code_bytes), 0x10000))
    if tls:
        phdrs.append((PT_TLS, 4, 0x1fdc8 if tls["vaddr"] else 0, tls["vaddr"], tls["filesz"],
                      tls["memsz"], tls["align"]))
    sections = [bytes(64)]
    for kind, entries in ((SHT_DYNSYM, dynsym), (SHT_RELA, rela)):
        if not entries:
            continue
        body += b"\0" * (-(body_at + len(body)) % 8)
        at = body_at + len(body)
        if kind == SHT_DYNSYM:
            data = b"".join(struct.pack("<IBBHQQ", 1, info, 0, 1, 0x1000, 8) for info in entries)
        else:
            data = b"".join(struct.pack("<QQq", *e) for e in entries)
        body += data
        sections.append(struct.pack("<IIQQQQIIQQ", 0, kind, 2, at, at, len(data), 0, 0,
                                    sh_addralign, 24))
    body += b"\0" * (-(body_at + len(body)) % 8)
    sh_at = body_at + len(body)
    head = ident.ljust(16, b"\0") + struct.pack("<HHIQQQIHHHHHH", e_type, machine, 1, code_at,
                                                64, sh_at, 0, 64, 56, ph_count, 64,
                                                len(sections), 0)
    ph = b"".join(struct.pack("<IIQQQQQQ", t, f, off, va, va, fs, ms, al)
                  for t, f, off, va, fs, ms, al in phdrs)
    return bytearray(head + ph + body + b"".join(sections))


def words_at(data, start, n):
    return list(struct.unpack_from(f"<{n}I", data, start))


def code_start(data):
    e_phoff, = struct.unpack_from("<Q", data, 32)
    e_phnum, = struct.unpack_from("<H", data, 56)
    for i in range(e_phnum):
        t, f, off = struct.unpack_from("<IIQ", data, e_phoff + 56 * i)
        if t == PT_LOAD:
            return off


def tls_header(data):
    e_phoff, = struct.unpack_from("<Q", data, 32)
    e_phnum, = struct.unpack_from("<H", data, 56)
    for i in range(e_phnum):
        h = struct.unpack_from("<IIQQQQQQ", data, e_phoff + 56 * i)
        if h[0] == PT_TLS:
            return h


# --- patching ------------------------------------------------------------------------------------

def test_relaxed_offsets_move_by_the_shift(tls):
    code = relaxed_access(0, 0x10) + relaxed_access(3, 0x14) + [NOP]
    data = elf(code)
    before = bytes(data)
    msg = tls.relocate(data)
    assert msg == "2 offsets +48, TLS block now at tp+64"
    c = code_start(data)
    # Only the movk immediates change: tp+16 -> tp+64
    assert words_at(data, c, len(code)) == relaxed_access(0, 0x40) + relaxed_access(3, 0x44) + [NOP]
    _, _, p_offset, p_vaddr, p_paddr, p_filesz, p_memsz, p_align = tls_header(data)
    assert p_align == 64 and p_vaddr % 64 == 0 and p_memsz == 8 and p_filesz == 0
    assert 0x1fdc8 - p_vaddr == 0x1fdc8 % 64 and p_paddr == p_vaddr
    # Nothing else in the file
    diff = [i for i in range(len(data)) if data[i] != before[i]]
    ph = tls_header_offset(data)
    assert all(c <= i < c + len(code) * 4 or ph <= i < ph + 56 for i in diff)
    # A second run changes nothing
    again = bytearray(data)
    assert tls.relocate(again) is None and again == data


def tls_header_offset(data):
    e_phoff, = struct.unpack_from("<Q", data, 32)
    e_phnum, = struct.unpack_from("<H", data, 56)
    return next(e_phoff + 56 * i for i in range(e_phnum)
                if struct.unpack_from("<I", data, e_phoff + 56 * i)[0] == PT_TLS)


@pytest.mark.parametrize("align,shift", [(1, 48), (8, 48), (16, 48)])
def test_shift_follows_the_old_alignment(tls, align, shift):
    data = elf(relaxed_access(0, 16), tls=dict(memsz=4, align=align, vaddr=0x2000, filesz=0))
    assert tls.relocate(data) == f"1 offsets +{shift}, TLS block now at tp+64"


def test_executable_without_tls_is_left_alone(tls):
    data = elf(relaxed_access(0, 0x10), tls=None)
    before = bytes(data)
    assert tls.relocate(data) is None and data == before


@pytest.mark.parametrize("kw,text", [
    (dict(ident=b"\x7fELG\x02\x01\x01"), "not a 64-bit little-endian ELF"),
    (dict(ident=b"\x7fELF\x01\x01\x01"), "not a 64-bit little-endian ELF"),
    (dict(ident=b"\x7fELF\x02\x02\x01"), "not a 64-bit little-endian ELF"),
    (dict(machine=62), "not an aarch64 executable"),
    (dict(e_type=1), "not an aarch64 executable"),
    (dict(interp=False), "not a dynamically linked executable"),
    (dict(tls=dict(memsz=8, align=32, vaddr=0x2000, filesz=0)), "TLS alignment 32"),
    (dict(tls=dict(memsz=8, align=8, vaddr=0x2000, filesz=4)), "initialised data"),
    (dict(dynsym=(0x12, 0x16)), "exports TLS symbols"),
    (dict(rela=((0x3000, 1027, 0x1234), (0x3008, (5 << 32) | 1030, 0))), "has TLS relocations"),
])
def test_refusals(tls, kw, text):
    data = elf(relaxed_access(0, 0x10), **kw)
    before = bytes(data)
    with pytest.raises(tls.Refused, match=text):
        tls.relocate(data)
    assert data == before


def test_refused_without_offsets_or_with_one_outside_the_block(tls):
    with pytest.raises(tls.Refused, match="no thread-pointer offsets"):
        tls.relocate(elf([NOP, RET, NOP]))
    data = elf(relaxed_access(0, 0x10) + relaxed_access(1, 0x30))  # block is tp+16..tp+24
    with pytest.raises(tls.Refused, match="outside the TLS block"):
        tls.relocate(data)


def test_relocations_are_read_by_their_entry_size(tls):
    # An ordinary absolute relocation whose addend happens to be 1030 (R_AARCH64_TLS_TPREL64)
    data = elf(relaxed_access(0, 0x10), rela=((0x3000, (7 << 32) | 257, 1030),))
    assert tls.relocate(data) is not None


def test_unknown_access_forms_are_refused(tls):
    code = relaxed_access(0, 0x14) + [mrs_tp(0), add_imm(0, 0, 0, lsl12=True), add_imm(0, 0, 0x10),
                                      RET]
    with pytest.raises(tls.Refused):
        tls.relocate(elf(code))


def test_offsets_beyond_64k_are_not_left_behind(tls):
    big = dict(memsz=0x20000, align=16, vaddr=0x40000, filesz=0)
    code = relaxed_access(0, 0x10) + [movz(1, 1), movk(1, 0x10), mrs_tp(2), add_reg(1, 2, 1), RET]
    with pytest.raises(tls.Refused):
        tls.relocate(elf(code, tls=big))


def test_shift_never_overflows_the_immediate(tls):
    big = dict(memsz=0x10000, align=16, vaddr=0x40000, filesz=0)
    with pytest.raises(tls.Refused):
        tls.relocate(elf(relaxed_access(0, 0xFFF0), tls=big))


# --- the command line ----------------------------------------------------------------------------

def run_tool(*args, stdin=None):
    return subprocess.run(["python3", "-I", str(TOOL), *map(str, args)], input=stdin,
                          capture_output=True, text=True)


def test_file_is_replaced_with_its_mode(tmp_path):
    f = tmp_path / "firefox"
    f.write_bytes(elf(relaxed_access(0, 0x10)))
    f.chmod(0o751)
    ino = f.stat().st_ino
    r = run_tool(f)
    assert r.returncode == 0 and "1 offsets +48" in r.stdout
    assert f.stat().st_mode & 0o7777 == 0o751
    # A new file through a rename: a running executable cannot be written (ETXTBSY)
    assert f.stat().st_ino != ino
    assert os.listdir(tmp_path) == ["firefox"]
    assert words_at(f.read_bytes(), code_start(f.read_bytes()), 2)[1] == movk(0, 0x40)
    # Again: nothing to do, success, untouched
    ino = f.stat().st_ino
    r = run_tool(f)
    assert r.returncode == 0 and r.stdout == "" and f.stat().st_ino == ino


def test_dry_run_and_refusal_leave_files_alone(tmp_path):
    good, bad = tmp_path / "good", tmp_path / "bad"
    good.write_bytes(elf(relaxed_access(0, 0x10)))
    bad.write_bytes(elf([NOP, RET]))
    data = good.read_bytes(), bad.read_bytes()
    r = run_tool("-n", good, bad)
    assert r.returncode == 1
    assert "offsets +48" in r.stdout and "no thread-pointer offsets found, left unchanged" in r.stderr
    assert (good.read_bytes(), bad.read_bytes()) == data
    # Without -n the good one is patched, the bad one not, and the run reports the failure
    r = run_tool(good, bad)
    assert r.returncode == 1 and good.read_bytes() != data[0] and bad.read_bytes() == data[1]


def test_stdin_paths_are_relative_to_the_root(tls, monkeypatch):
    # The pacman hook hands over "usr/lib/firefox/firefox"; only the mapping is checked here, no
    # file of the host is opened
    seen = []
    monkeypatch.setattr(tls, "process", lambda p, dry: seen.append((p, dry)) or True)
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("usr/lib/firefox/firefox\n\n/usr/lib/x\n"))
    assert tls.main(["--stdin", "-n"]) == 0
    assert seen == [("/usr/lib/firefox/firefox", True), ("/usr/lib/x", True)]


def test_usage_without_paths(tls):
    with pytest.raises(SystemExit) as e:
        tls.main([])
    assert "Usage" in str(e.value)


# --- real binaries (aarch64 cross compiler and qemu-user, if the host has them) ------------------

def cross(tmp_path, name, main_c, other_c):
    cc = shutil.which("aarch64-linux-gnu-gcc")
    qemu = shutil.which("qemu-aarch64") or shutil.which("qemu-aarch64-static")
    sysroot = "/usr/aarch64-linux-gnu"
    if not cc or not qemu or not os.path.isdir(sysroot):
        pytest.skip("no aarch64 cross compiler, qemu-user or sysroot on the host")
    (tmp_path / "main.c").write_text(main_c)
    (tmp_path / "other.c").write_text(other_c)
    exe = compile_c(tmp_path / name, [tmp_path / "main.c", tmp_path / "other.c"],
                    flags=["-fPIE", "-pie"], cc=cc, counted=False)

    def run(path):
        r = subprocess.run([qemu, "-L", sysroot, str(path)], capture_output=True, text=True,
                           timeout=20)
        assert r.returncode == 0, r.stderr
        return [int(x) for x in r.stdout.split()]
    return exe, run


MAIN_IE = r"""
#include <stdio.h>
extern __thread int b;
extern __thread char c[100];
__attribute__((noinline)) int *pb(void) { return &b; }
__attribute__((noinline)) char *pc(void) { return c; }
int main(void)
{
    char *tp = __builtin_thread_pointer();
    *pb() = 7;
    pc()[99] = 3;
    printf("%ld %ld %d %d\n", (long) ((char *) pb() - tp), (long) (pc() - tp), *pb(), pc()[99]);
    return 0;
}
"""


def test_real_binary_runs_with_the_block_at_tp64(tmp_path):
    exe, run = cross(tmp_path, "ie", MAIN_IE, "__thread int b; __thread char c[100];\n")
    before = run(exe)
    r = run_tool(exe)
    assert r.returncode == 0, r.stderr
    after = run(exe)
    # Every variable moved by the same 48 bytes, all of them at tp+64 or later, values intact
    assert [after[0] - before[0], after[1] - before[1]] == [48, 48]
    assert min(after[:2]) >= 64 and after[2:] == [7, 3]


MAIN_MIXED = r"""
#include <stdio.h>
extern __thread int b;
static __thread int a;
__attribute__((noinline)) int *pa(void) { return &a; }
__attribute__((noinline)) int *pb(void) { return &b; }
int main(void)
{
    char *tp = __builtin_thread_pointer();
    *pa() = 5;
    *pb() = 7;
    printf("%ld %ld\n", (long) ((char *) pa() - tp), (long) ((char *) pb() - tp));
    return 0;
}
"""


def test_real_binary_with_a_static_tls_variable(tmp_path):
    exe, run = cross(tmp_path, "mixed", MAIN_MIXED, "__thread int b;\n")
    r = run_tool(exe)
    if r.returncode != 0:
        return  # refused: the right answer
    assert min(run(exe)) >= 64


# --- the real executables of the hook ----------------------------------------------------------

def cached_packages():
    cache = REPO / "build" / "cache" / "pacman"
    newest = {}
    for name in ("firefox", "thunderbird"):
        found = sorted((p for p in cache.glob(f"{name}-[0-9]*-aarch64.pkg.tar.*")
                        if p.suffix != ".sig"), key=lambda p: p.stat().st_mtime)
        if found:
            newest[name] = found[-1]
    return newest


@pytest.mark.parametrize("name", ["firefox", "thunderbird"])
def test_hook_targets_from_the_package_cache_are_patched(tls, tmp_path, name):
    """What the pacman hook gets: the executable as the package ships it must be patched, not
    refused (a refusal leaves Mali's GL driver writing into its TLS block)."""
    pkg = cached_packages().get(name)
    if pkg is None:
        pytest.skip(f"no {name} package in build/cache/pacman")
    member = f"usr/lib/{name}/{name}"
    subprocess.run(["bsdtar", "-xf", str(pkg), "-C", str(tmp_path), member], check=True)
    data = bytearray((tmp_path / member).read_bytes())
    msg = tls.relocate(data)
    assert msg and "TLS block now at tp+64" in msg, msg
    assert tls.relocate(data) is None  # patched: nothing more to do


# --- more forms the patch must recognise or refuse -------------------------------------------

def ldr_imm(rt, rn, imm12):
    """ldr Xt, [Xn, #imm12 * 8]"""
    return 0xF9400000 | imm12 << 10 | rn << 5 | rt


def ldr_q_imm(rt, rn, imm12):
    """ldr Qt, [Xn, #imm12 * 16] (SIMD&FP, 128 bit)"""
    return 0x3DC00000 | imm12 << 10 | rn << 5 | rt


def test_section_without_an_entry_size_is_refused(tls):
    data = elf(relaxed_access(0, 0x10), dynsym=(0x12,))
    e_shoff, = struct.unpack_from("<Q", data, 40)
    struct.pack_into("<Q", data, e_shoff + 64 + 56, 0)  # section 1's sh_entsize
    before = bytes(data)
    with pytest.raises(tls.Refused, match="section 1 without an entry size"):
        tls.relocate(data)
    assert data == before


def test_movk_into_another_register_is_no_offset(tls):
    code = [movz(0), movk(1, 0x10), NOP] + relaxed_access(2, 0x14)
    data = elf(code)
    assert tls.relocate(data) == "1 offsets +48, TLS block now at tp+64"
    # The movk that did not follow its movz is left as it was
    assert words_at(data, code_start(data), len(code)) == \
        [movz(0), movk(1, 0x10), NOP] + relaxed_access(2, 0x44)


@pytest.mark.parametrize("load", [ldr_imm(0, 2, 2), ldr_q_imm(0, 2, 1)])
def test_load_with_an_immediate_offset_into_the_block_is_refused(tls, load, tmp_path, capsys):
    # tp+16 (block tp+16..tp+24): an offset the patch would not move
    data = elf(relaxed_access(0, 0x10) + [mrs_tp(2), load, RET])
    with pytest.raises(tls.Refused, match="load/store"):
        tls.relocate(bytearray(data))
    # The check comes after the movk immediates were changed in memory: the file stays as it is
    f = tmp_path / "firefox"
    f.write_bytes(data)
    assert tls.process(str(f), False) is False
    assert f.read_bytes() == data and os.listdir(tmp_path) == ["firefox"]
    assert "thread-pointer offset in a load/store" in capsys.readouterr().err


def test_load_of_the_tcb_below_the_block_is_fine(tls):
    # ldr x0, [tp]: the dtv pointer, not the block
    code = relaxed_access(0, 0x10) + [mrs_tp(2), ldr_imm(0, 2, 0), RET]
    data = elf(code)
    assert tls.relocate(data) == "1 offsets +48, TLS block now at tp+64"
    assert words_at(data, code_start(data), len(code))[-2:] == [ldr_imm(0, 2, 0), RET]


class OsWithout:
    """os with one function failing, for process()'s write path."""

    def __init__(self, name):
        self.name = name

    def __getattr__(self, attr):
        if attr == self.name:
            def fail(*args):
                raise OSError(28, "No space left on device")
            return fail
        return getattr(os, attr)


def test_failed_replace_leaves_the_file_and_no_temporary(tls, tmp_path, monkeypatch):
    f = tmp_path / "firefox"
    original = bytes(elf(relaxed_access(0, 0x10)))
    f.write_bytes(original)
    monkeypatch.setattr(tls, "os", OsWithout("rename"))
    with pytest.raises(OSError, match="No space left"):
        tls.process(str(f), False)
    assert f.read_bytes() == original and os.listdir(tmp_path) == ["firefox"]
