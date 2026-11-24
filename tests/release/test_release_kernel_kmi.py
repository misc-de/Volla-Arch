# tools/kernel-kmi.py (measurements of a linux-6.1.y merge against the vendor modules' KMI) and
# tools/kernel-checkbuild.sh (check build of a kernel commit with the PKGBUILD's own prepare() and
# build()). kernel-kmi runs on small git repositories made here, with a merge that conflicts, a
# genksyms type graph and stand-ins for pahole and llvm-objdump; kernel-checkbuild runs as a copy
# with a stand-in PKGBUILD, so no kernel is compiled.

import shutil
import subprocess
import textwrap

import pytest

from conftest import TOOLS, load_script

KMI = TOOLS / "kernel-kmi.py"
CHECKBUILD = TOOLS / "kernel-checkbuild.sh"


@pytest.fixture
def kmi():
    return load_script(KMI)


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def commit(repo, files, message):
    for name, text in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        if text is None:
            p.unlink()
        else:
            p.write_text(textwrap.dedent(text))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "kernel"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "test@example.org")
    git(r, "config", "user.name", "test")
    git(r, "config", "merge.conflictStyle", "diff3")
    return r


def run(kmi, capsys, *argv):
    kmi.GIT[1:] = []
    kmi.main([str(a) for a in argv])
    out, err = capsys.readouterr()
    return [line.split("\t") for line in out.splitlines()], err


# --- conflicts -----------------------------------------------------------------------------------

BASE = """\
int a(void)
{
\treturn 1;
}

int b(void)
{
\treturn 2;
}

int c(void)
{
\treturn 3;
}

int d(void)
{
\treturn 4;
}
"""


def conflicting_merge(repo):
    base = commit(repo, {"f.c": BASE, "g.c": "int x;\n"}, "base")
    git(repo, "checkout", "-q", "-b", "theirs")
    commit(repo, {"f.c": BASE.replace("return 1;", "return 10;")
                  .replace("return 2;", "return 20;")
                  .replace("return 3;", "return 3;\n\tfix(c);")
                  .replace("\treturn 4;", "\tfoo();\n\treturn 4;")}, "theirs")
    git(repo, "checkout", "-q", "main")
    commit(repo, {"f.c": BASE.replace("return 1;", "return 10;  ")
                  .replace("return 2;", "return 99;")
                  .replace("return 3;", "return 3;\n\tour(c);")
                  .replace("\treturn 4;", "\tbar();\n\treturn 4;") + "\nvoid e(void) { fix(c); }\n"},
           "ours")
    return base


def test_conflicts_in_the_worktree(kmi, repo, capsys, monkeypatch):
    conflicting_merge(repo)
    r = subprocess.run(["git", "-C", str(repo), "merge", "-q", "theirs"], capture_output=True)
    assert r.returncode != 0
    monkeypatch.chdir(repo)
    rows, err = run(kmi, capsys, "conflicts")
    assert rows == [["f.c", "4", "same real already-in both-insert"]]
    assert "4 regions: already-in 1, both-insert 1, real 1, same 1" in err


def test_conflicts_in_a_merge_tree(kmi, repo, capsys):
    conflicting_merge(repo)
    r = subprocess.run(["git", "-C", str(repo), "merge-tree", "--write-tree", "main", "theirs"],
                       capture_output=True, text=True)
    tree = r.stdout.split()[0]
    rows, _ = run(kmi, capsys, "-C", repo, "conflicts", "--tree", tree)
    assert rows == [["f.c", "4", "same real already-in both-insert"]]
    # Named files: one without markers gives no line
    rows, _ = run(kmi, capsys, "-C", repo, "conflicts", "--tree", tree, "g.c")
    assert rows == []


def test_near_same_and_stray_markers(kmi):
    whole = kmi.norm("x")
    assert kmi.classify(["int value = 1;\n"], ["int v;\n"], ["int value = 2;\n"], whole) == "near-same"
    # Lines outside a region and an unfinished region are no regions
    assert kmi.regions(["a\n", ">>>>>>> x\n", "<<<<<<< y\n", "b\n"]) == []


# --- types ---------------------------------------------------------------------------------------

HEADER_OLD = """\
struct seen { int a; int b; };
struct hidden { int a; };
typedef struct { int x; } tdef_t;
enum mode { M_A, M_B };
struct shielded {
\tint a;
\tANDROID_KABI_RESERVE(1);
};
"""

HEADER_NEW = """\
/* a comment changes nothing */
struct seen { int a; long b; };
struct hidden { int a; int more; };
typedef struct { int x; int y; } tdef_t;
enum mode { M_A, M_B, M_C };
struct fresh { int n; };
struct shielded {
\tint a;
#ifndef __GENKSYMS__
\tint hole_filler;
#endif
#ifdef __GENKSYMS__
\tint old;
#else
\tint renamed;
#endif
#if defined(CONFIG_X)
\tint cfg;
#endif
\tANDROID_KABI_USE(1, struct { int p; int q; });
};
"""


@pytest.fixture
def types_repo(repo, tmp_path):
    old = commit(repo, {"include/t.h": HEADER_OLD.replace("struct shielded {\n\tint a;",
                                                          "struct shielded {\n\tint a;\n\tint old;\n#if defined(CONFIG_X)\n\tint cfg;\n#endif"),
                        "drivers/x.c": "struct gone_type { int z; };\n", "README": "x\n"}, "old")
    new = commit(repo, {"include/t.h": HEADER_NEW, "drivers/x.c": None, "README": "y\n"}, "new")
    sym = tmp_path / "obj" / "sub"
    sym.mkdir(parents=True)
    (sym / "a.symtypes").write_text(
        "s#seen struct seen { int a ; int b ; }\n"
        "s#shielded struct shielded { int a ; }\n"
        "t#tdef_t typedef struct { int x ; } tdef_t\n"
        "e#mode enum mode { M_A , M_B }\n"
        "s#gone_type struct gone_type { int z ; }\n"
        "vendor_fn int vendor_fn ( s#seen * , t#tdef_t , e#mode , s#shielded * , s#gone_type * )\n"
        "lonely\n")
    (sym / "ignored.txt").write_text("s#hidden struct hidden { }\n")
    needs = tmp_path / "vendor-needs.txt"
    needs.write_text("0x1 vendor_fn\n0x2 not_in_graph\n\n")
    return repo, old, new, tmp_path / "obj", needs


def test_types_marks_what_vendor_modules_reach(kmi, types_repo, capsys):
    repo, old, new, obj, needs = types_repo
    rows, err = run(kmi, capsys, "-C", repo, "types", old, new, "--symtypes", obj, "--needs", needs)
    got = {(r[3], r[1]): r[0] for r in rows}
    assert got == {("seen", "changed"): "R", ("hidden", "changed"): "-", ("tdef_t", "changed"): "R",
                   ("mode", "changed"): "R", ("fresh", "new"): "-", ("shielded", "changed"): "R",
                   ("gone_type", "gone"): "R"}
    assert "5 reachable definitions differ" in err


def test_types_in_the_genksyms_view(kmi, types_repo, capsys):
    repo, old, new, obj, needs = types_repo
    rows, _ = run(kmi, capsys, "-C", repo, "types", "--genksyms", old, new, "--symtypes", obj,
                  "--needs", needs, "include/t.h", "README")
    # shielded: the hidden member, the old name for genksyms, the reserve taken by KABI_USE
    assert "shielded" not in {r[3] for r in rows}
    assert {r[3] for r in rows} == {"seen", "hidden", "tdef_t", "mode", "fresh"}


def test_genksyms_view_keeps_other_conditionals(kmi):
    src = "#ifdef A\nx\n#else\ny\n#endif\n#ifndef __GENKSYMS__\nz\n#endif\n"
    assert kmi.genksyms_view(src) == "#ifdef A\nx\n#else\ny\n#endif\n"


def test_reachability_starts_only_at_known_symbols(kmi, types_repo):
    _, _, _, obj, needs = types_repo
    seen = kmi.reachable(obj, needs)
    assert seen == {"vendor_fn", "s#seen", "t#tdef_t", "e#mode", "s#shielded", "s#gone_type"}


# --- inlines -------------------------------------------------------------------------------------

def test_inlines_finds_macros_inline_functions_and_anonymous_enums(kmi, repo, capsys):
    old = commit(repo, {"include/linux/s.h": """\
        #define SHIFT 16
        #define MASK ((1 << SHIFT) - 1)
        #define GONE 1
        #define LONG(a) \\
        \t(a + 1)
        static inline int same(int a) { return a; }
        static __always_inline int helper(int a)
        {
        \treturn a & MASK;
        }
        static inline void dropped(void) { }
        enum { F_ONE, F_TWO };
        enum { Q_A, Q_B };
        enum named { N_A };
        """, "include/other.c.h": "#define X 1\n", "arch/arm64/include/asm/a.h": "#define R 1\n",
                        "tools/t.h": "#define T 1\n"}, "old")
    new = commit(repo, {"include/linux/s.h": """\
        #define SHIFT 30
        #define MASK ((1 << SHIFT) - 1)
        #define NEW 2
        #define LONG(a) \\
        \t(a + 2)
        static inline int same(int a) { return a; }
        static __always_inline int helper(int a)
        {
        \treturn a & MASK & 0xff;
        }
        static inline int added(void) { return 0; }
        enum { F_ONE, F_TWO, F_THREE };
        enum { Q_A, Q_NEW, Q_B };
        enum named { N_A, N_B };
        """, "include/other.c.h": None, "arch/arm64/include/asm/a.h": "#define R 2\n",
                        "tools/t.h": "#define T 2\n"}, "new")
    rows, _ = run(kmi, capsys, "-C", repo, "inlines", old, new)
    assert sorted(map(tuple, rows)) == sorted([
        ("changed", "define", "SHIFT", "include/linux/s.h"),
        ("gone", "define", "GONE", "include/linux/s.h"),
        ("new", "define", "NEW", "include/linux/s.h"),
        ("changed", "define", "LONG", "include/linux/s.h"),
        ("changed", "inline", "helper", "include/linux/s.h"),
        ("gone", "inline", "dropped", "include/linux/s.h"),
        ("new", "inline", "added", "include/linux/s.h"),
        ("appended", "enum", "F_ONE", "include/linux/s.h"),
        ("changed", "enum", "Q_A", "include/linux/s.h"),
        ("gone", "define", "X", "include/other.c.h"),
        ("changed", "define", "R", "arch/arm64/include/asm/a.h"),
    ])


# --- dupes ---------------------------------------------------------------------------------------

def test_dupes_finds_a_fix_applied_twice(kmi, repo, capsys):
    own = commit(repo, {"a.c": "int a;\n", "b.c": "int b;\n", "doc.txt": "x\n"}, "base")
    backport = commit(repo, {"a.c": "int a;\nvoid fix(void)\n{\n\tcheck();\n\tmore();\n}\n"
                                    "int z;\n",
                             "b.c": "int b;\nint other;\n"}, "our backport")
    merged = commit(repo, {"a.c": "int a;\nvoid fix(void)\n{\n\tcheck();\n\tmore();\n}\n"
                                  "int z;\nvoid fix(void)\n{\n\tcheck();\n\tmore();\n}\n",
                           "b.c": "int b;\nint other;\nint x;\nint y;\nint w;\n",
                           "c.c": "void fix(void)\n{\n\tcheck();\n\tmore();\n}\n"}, "merge")
    rows, _ = run(kmi, capsys, "-C", repo, "dupes", backport, merged, "--own", own)
    assert len(rows) == 1
    assert rows[0][0] == "a.c" and rows[0][2].startswith("5 lines already in")
    # With a higher threshold the five lines are not enough
    rows, _ = run(kmi, capsys, "-C", repo, "dupes", backport, merged, "--own", own, "--min", "6")
    assert rows == []


def test_git_errors_end_the_run(kmi, repo, capsys):
    commit(repo, {"a.c": "x\n"}, "one")
    with pytest.raises(SystemExit, match="git diff"):
        run(kmi, capsys, "-C", repo, "inlines", "nope", "HEAD")


# --- layout --------------------------------------------------------------------------------------

PAHOLE_OLD = """\
struct a {
\tint                        x;                    /*     0     4 */
\tunion {
\t\tint                y;                    /*     4     4 */
\t\tchar               z;                    /*     4     1 */
\t};                                               /*     4     4 */
\tlong                       w;                    /*     8     8 */
\tlong                       gone;                 /*    16     8 */

\t/* size: 24, cachelines: 1, members: 4 */
};
struct b {
\tint                        k:3;                  /*     0: 0  4 */
\tchar                       arr[4];               /*     4     4 */

\t/* size: 8, cachelines: 1, members: 2 */
};
struct only_old {
\tint                        q;                    /*     0     4 */

\t/* size: 4, cachelines: 1, members: 1 */
};
"""

PAHOLE_NEW = PAHOLE_OLD.replace("/*     8     8 */", "/*    16     8 */") \
    .replace("\tlong                       gone;                 /*    16     8 */\n", "") \
    .replace("size: 24", "size: 32").replace("struct only_old", "struct only_new")


def pahole_stub(fake_bin, tmp_path):
    (tmp_path / "old.out").write_text(PAHOLE_OLD)
    (tmp_path / "new.out").write_text(PAHOLE_NEW)
    fake_bin.stub("pahole", 'for a; do last=$a; done; cat "${last%.vmlinux}.out"')


def test_layout_compares_sizes_and_offsets(kmi, fake_bin, tmp_path, capsys, monkeypatch):
    pahole_stub(fake_bin, tmp_path)
    monkeypatch.setenv("PATH", fake_bin.env()["PATH"])
    rows, err = run(kmi, capsys, "layout", tmp_path / "old.vmlinux", tmp_path / "new.vmlinux",
                    "a", "b", "only_old")
    assert rows == [["a", "size", "24 -> 32"], ["a", "w", "8 -> 16"], ["a", "gone", "gone (was at 16)"]]
    assert "2 structs compared" in err
    # All structures of the file, the named ones picked out (pahole -C misses some of a list)
    assert fake_bin.calls("pahole")[0][:2] == ["-F", "dwarf"]


def test_layout_takes_the_reachable_structs(kmi, fake_bin, types_repo, tmp_path, capsys,
                                            monkeypatch):
    _, _, _, obj, needs = types_repo
    pahole_stub(fake_bin, tmp_path)
    monkeypatch.setenv("PATH", fake_bin.env()["PATH"])
    (tmp_path / "old.out").write_text(PAHOLE_OLD.replace("struct a {", "struct seen {")
                                      .replace("struct b {", "struct nested_twin {"))
    (tmp_path / "new.out").write_text(PAHOLE_NEW.replace("struct a {", "struct seen {")
                                      .replace("struct b {", "struct nested_twin {")
                                      + PAHOLE_OLD.replace("struct a {", "struct seen {"))
    rows, err = run(kmi, capsys, "layout", tmp_path / "old.vmlinux", tmp_path / "new.vmlinux",
                    "--symtypes", obj, "--needs", needs)
    # Only reachable structs, each the first time pahole prints it
    assert [r[0] for r in rows] == ["seen", "seen", "seen"]
    assert "1 structs compared" in err


def test_layout_needs_structs_or_the_graph(kmi, capsys):
    with pytest.raises(SystemExit):
        run(kmi, capsys, "layout", "a", "b")
    assert "name structs" in capsys.readouterr().err


# --- vendor-const --------------------------------------------------------------------------------

OBJDUMP = """\

m.ko:\tfile format elf64-littleaarch64

Disassembly of section .text:

0000000000000000 <first>:
       0:      \tmov\tw8, #0x10001            // =65537
       4:      \tadd\tx10, x10, x11
       8:      \tstr\tw8, [x10, #0x20]
0000000000000010 <second>:
      10:      \torr\tw19, w8, #0x10001
      14:      \tret
      18:      \tmov\tw1, #65537
"""


def test_vendor_const_names_function_and_store(kmi, fake_bin, tmp_path, capsys, monkeypatch):
    mods = tmp_path / "mods" / "lib"
    mods.mkdir(parents=True)
    (mods / "m.ko").write_bytes(b"\x7fELF")
    (mods / "notes.txt").write_text("x")
    (tmp_path / "objdump.out").write_text(OBJDUMP)
    fake_bin.stub("llvm-objdump", f'cat "{tmp_path}/objdump.out"')
    monkeypatch.setenv("PATH", fake_bin.env()["PATH"])
    rows, _ = run(kmi, capsys, "vendor-const", tmp_path / "mods", "0x10001")
    assert rows == [["m.ko", "first", "0", "str w8, [x10, #0x20]"], ["m.ko", "second", "10", ""],
                    ["m.ko", "second", "18", ""]]


def test_main_runs_as_a_script(repo):
    commit(repo, {"include/a.h": "#define A 1\n"}, "one")
    second = commit(repo, {"include/a.h": "#define A 2\n"}, "two")
    r = subprocess.run(["python3", str(KMI), "-C", str(repo), "inlines", second + "~1", second],
                       capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout == "changed\tdefine\tA\tinclude/a.h\n"


# --- kernel-checkbuild.sh ------------------------------------------------------------------------

STAND_IN_PKGBUILD = """\
pkgname=plinius-kernel
_kcommit=0000
pkgver=6.1.1
pkgrel=7
_krelease=$pkgver-plinius$pkgrel
prepare() {
\techo "prepare $PWD" > "$srcdir/prepare.log"
}
build() {
\t{ echo "commit=$_kcommit ver=$pkgver rel=$pkgrel release=$_krelease"
\t  ls "$srcdir"; cat "$srcdir/plinius-kernel-$_kcommit/Makefile"; } > "$srcdir/build.out"
\t[[ -z ${FAIL_BUILD:-} ]]
}
"""


@pytest.fixture
def checkbuild(tmp_path, repo):
    volla = tmp_path / "volla"
    (volla / "tools").mkdir(parents=True)
    for f in (CHECKBUILD, TOOLS / "lib.sh"):
        shutil.copy(f, volla / "tools" / f.name)
    pkg = volla / "pkgbuilds" / "plinius-kernel"
    pkg.mkdir(parents=True)
    (pkg / "PKGBUILD").write_text(STAND_IN_PKGBUILD)
    (pkg / "config").write_text("CONFIG_X=y\n")
    (pkg / "plinius-kernel-0000.tar.gz").write_text("old tarball\n")
    rev = commit(repo, {"Makefile": "VERSION = 6\nPATCHLEVEL = 1\nSUBLEVEL = 193\n"
                                    "EXTRAVERSION =\nSUBLEVEL = 0\n"}, "kernel")
    return volla, rev


def run_checkbuild(volla, *args, **env):
    return subprocess.run(["bash", str(volla / "tools" / "kernel-checkbuild.sh"), *args],
                          capture_output=True, text=True, env={**__import__("os").environ, **env})


def test_checkbuild_runs_prepare_and_build_with_pkgrel_99(checkbuild, repo):
    volla, rev = checkbuild
    r = run_checkbuild(volla, "main", KERNEL_REPO=str(repo))
    assert r.returncode == 0, r.stderr
    src = volla / "build" / "kernel-check" / "6.1.193" / "src"
    out = (src / "build.out").read_text().splitlines()
    assert out[0] == f"commit={rev} ver=6.1.193 rel=99 release=6.1.193-plinius99"
    # The package's files, not its tarballs or the PKGBUILD, next to the tree
    assert sorted(out[1:4]) == ["build.out", "config", f"plinius-kernel-{rev}"]
    assert (src / "prepare.log").read_text() == f"prepare {src}\n"
    assert (src.parent / "build.log").exists()


def test_checkbuild_takes_a_given_version_and_reports_failures(checkbuild, repo):
    volla, _ = checkbuild
    # A relative OUT is taken from the current directory
    r = subprocess.run(["bash", str(volla / "tools" / "kernel-checkbuild.sh"), "main", "6.1.200"],
                       capture_output=True, text=True, cwd=volla,
                       env={**__import__("os").environ, "KERNEL_REPO": str(repo),
                            "FAIL_BUILD": "1", "OUT": "out"})
    assert r.returncode != 0
    assert "release=6.1.200-plinius99" in (volla / "out" / "src" / "build.out").read_text()


def test_checkbuild_refuses_unknown_commits_and_versions(checkbuild, repo):
    volla, _ = checkbuild
    r = run_checkbuild(volla, "no-such-rev", KERNEL_REPO=str(repo))
    assert r.returncode != 0 and "no commit no-such-rev" in r.stderr
    r = run_checkbuild(volla, "main", "6.1", KERNEL_REPO=str(repo))
    assert r.returncode != 0 and "no kernel version: 6.1" in r.stderr
    r = run_checkbuild(volla)
    assert r.returncode != 0 and "usage" in r.stderr


BUILD_SH_STUB = """\
#!/bin/bash
# Stand-in for tools/build.sh: records what it would build
{ echo "pkg=$1 src=$PKGSRC out=$PKGOUT"; grep -E '^(_kcommit|pkgver|pkgrel)=' "$PKGSRC/PKGBUILD"
  grep -A1 '^sha256sums=' "$PKGSRC/PKGBUILD"; ls "$PKGSRC"; } > "$PKGSRC/../build-sh.out"
"""


def test_checkbuild_package_builds_with_pkgrel_99_outside_the_repo(checkbuild, repo):
    volla, rev = checkbuild
    pkg = volla / "pkgbuilds" / "plinius-kernel"
    (pkg / "PKGBUILD").write_text(STAND_IN_PKGBUILD + "sha256sums=('aaaa'\n            'bbbb')\n")
    stub = volla / "tools" / "build.sh"
    stub.write_text(BUILD_SH_STUB)
    stub.chmod(0o755)
    r = run_checkbuild(volla, "--package", "main", KERNEL_REPO=str(repo))
    assert r.returncode == 0, r.stderr
    out = volla / "build" / "kernel-check" / "6.1.193"
    lines = (out / "build-sh.out").read_text().splitlines()
    tarball = f"plinius-kernel-{rev}.tar.gz"
    digest = subprocess.run(["sha256sum", str(out / "pkg" / tarball)], capture_output=True,
                            text=True).stdout.split()[0]
    assert lines[0] == f"pkg=plinius-kernel src={out / 'pkg'} out={out / 'repo'}"
    assert lines[1:4] == [f"_kcommit={rev}", "pkgver=6.1.193", "pkgrel=99"]
    assert lines[4:6] == [f"sha256sums=('{digest}'", "            'bbbb')"]
    # The commit's tarball, not the one of the published package
    assert tarball in lines and "plinius-kernel-0000.tar.gz" not in lines
    # The same bytes as tools/kernel-source.sh packs: git archive | gzip -n
    listing = subprocess.run(["tar", "-tzf", str(out / "pkg" / tarball)], capture_output=True,
                             text=True).stdout.split()
    assert f"plinius-kernel-{rev}/Makefile" in listing
