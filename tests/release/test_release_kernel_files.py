# tools/kernel-files.py: the manifest of built kernel sources that filters the CVE list. A file
# wrongly left out hides a vulnerability, so the evaluation of Kbuild files is checked on a small
# synthetic tree.

import subprocess
import sys

import pytest

from conftest import TOOLS, load_script

SCRIPT = TOOLS / "kernel-files.py"

FOO_MAKEFILE = """\
# SPDX-License-Identifier: GPL-2.0
obj-$(CONFIG_FOO_A) += a.o
obj-$(CONFIG_FOO_B) += b.o
foo-core-y := core1.o core2.o \\
	core3.o
foo-core-$(CONFIG_FOO_EXTRA) += extra.o
obj-$(CONFIG_FOO) += foo-core.o
bar-objs := part.o
obj-m += bar.o
ifdef CONFIG_FOO_DEBUG
obj-y += debug.o
else
obj-y += nodebug.o
endif
ifeq ($(CONFIG_FOO_MODE),fast)
obj-y += fast.o
else ifeq ($(CONFIG_FOO_MODE),slow)
obj-y += slow.o
else
obj-y += default.o
endif
ifneq ($(SRCARCH),arm64)
obj-y += unknown.o
endif
ifeq "$(CONFIG_FOO_A)" "m"
obj-y += quoted.o
endif
obj-y += sub/deep/ # a multi-level directory
obj-$(CONFIG_FOO) += mid/leaf.o
obj-$(CONFIG_GONE) += gone/
clean-files := generated.c
"""

CONFIG = """\
CONFIG_FOO=y
CONFIG_FOO_A=m
# CONFIG_FOO_B is not set
CONFIG_FOO_MODE="fast"
CONFIG_BASE=y
"""


@pytest.fixture
def tree(tmp_path):
    t = tmp_path / "linux"

    def put(path, text=""):
        p = t / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    put("drivers/Makefile", "obj-$(CONFIG_FOO) += foo/\nobj-$(CONFIG_BAR) += bar/\n"
                            "obj-$(CONFIG_BASE) += base/\n")
    put("drivers/foo/Makefile", FOO_MAKEFILE)
    for f in ("a", "b", "core1", "core2", "core3", "extra", "part", "debug", "nodebug", "fast",
              "slow", "default", "unknown", "quoted", "unnamed"):
        put(f"drivers/foo/{f}.c")
    put("drivers/foo/x.h")
    put("drivers/foo/sub/deep/Makefile", "obj-y += d.o\n")
    put("drivers/foo/sub/deep/d.c")
    put("drivers/foo/mid/leaf.c")
    put("drivers/foo/gone/Makefile", "obj-y += g.o\n")
    put("drivers/foo/gone/g.c")
    put("drivers/bar/Makefile", "obj-y += x.o\n")
    put("drivers/bar/x.c")
    put("drivers/base/Makefile", "obj-y += core.o\n")
    put("drivers/base/core.c")
    put("drivers/base/firmware_loader/fw.c")  # nobody names it, its neighbours are built
    put("arch/arm64/Kbuild", "obj-y += kernel/\n")
    put("arch/arm64/kernel/Makefile", "obj-y += setup.o entry.o\n")
    put("arch/arm64/kernel/setup.c")
    put("arch/arm64/kernel/entry.S")
    put("arch/x86/kernel/Makefile", "obj-y += x86.o\n")
    put("arch/x86/kernel/x86.c")
    put("Documentation/example.c")
    put("tools/perf/perf.c")
    config = tmp_path / "config"
    config.write_text(CONFIG)
    return t, config


def manifest(tree):
    t, config = tree
    r = subprocess.run([sys.executable, str(SCRIPT), str(t), str(config)], capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return [l for l in r.stdout.splitlines() if not l.startswith("# ")], r


def test_manifest_lists_what_the_configuration_builds(tree):
    files, r = manifest(tree)
    built = {"drivers/foo/a.c", "drivers/foo/core1.c", "drivers/foo/core2.c", "drivers/foo/core3.c",
             "drivers/foo/part.c", "drivers/foo/nodebug.c", "drivers/foo/fast.c",
             "drivers/foo/unknown.c", "drivers/foo/quoted.c", "drivers/foo/sub/deep/d.c",
             "drivers/foo/mid/leaf.c", "drivers/base/core.c", "arch/arm64/kernel/setup.c",
             "arch/arm64/kernel/entry.S"}
    # Listed because no Makefile decides: never hide what might be built
    undecided = {"drivers/foo/unnamed.c", "drivers/base/firmware_loader/fw.c"}
    not_built = {"drivers/foo/b.c", "drivers/foo/extra.c", "drivers/foo/debug.c",
                 "drivers/foo/slow.c", "drivers/foo/gone/g.c", "drivers/bar/x.c",
                 "arch/x86/kernel/x86.c", "Documentation/example.c", "tools/perf/perf.c"}
    assert built | undecided <= set(files)
    assert not (not_built & set(files))
    assert "drivers/foo/x.h" not in files  # only sources
    assert f"# {len(files)} files, " in r.stdout


def test_else_after_a_taken_branch_of_an_else_chain_is_inactive(tree):
    files, _ = manifest(tree)
    assert "drivers/foo/default.c" not in files


def test_condition(tmp_path):
    kf = load_script(SCRIPT)
    cfg = {"CONFIG_A": "y", "CONFIG_B": "m", "CONFIG_S": "fast", "CONFIG_N": "n"}
    assert kf.condition("ifdef CONFIG_A", cfg) is True
    assert kf.condition("ifdef CONFIG_N", cfg) is False
    assert kf.condition("ifndef CONFIG_X", cfg) is True
    assert kf.condition("ifdef SOMETHING", cfg) is None
    assert kf.condition("ifeq ($(CONFIG_S),fast)", cfg) is True
    assert kf.condition("ifneq ($(CONFIG_B),)", cfg) is True
    assert kf.condition("ifeq ($(CONFIG_X),y)", cfg) is False
    assert kf.condition("ifeq '$(CONFIG_A)' 'y'", cfg) is True
    assert kf.condition("ifeq ($(CC),clang)", cfg) is None
    assert kf.condition("ifeq ($(call x,y),z)", cfg) is None


def test_logical_lines_join_continuations_and_drop_comments_and_recipes(tmp_path):
    kf = load_script(SCRIPT)
    mk = tmp_path / "Makefile"
    mk.write_text("obj-y += a.o \\\n\tb.o \\\n  c.o # comment\nx: y\n\techo obj-y += r.o\n"
                  "CFLAGS += -DX=\\#y\n\n# only a comment\n")
    assert [l.split() for l in kf.logical_lines(str(mk))] == [
        ["obj-y", "+=", "a.o", "b.o", "c.o"], ["x:", "y"], ["CFLAGS", "+=", "-DX=\\#y"]]


def test_read_config_strips_quotes(tmp_path):
    kf = load_script(SCRIPT)
    c = tmp_path / "config"
    c.write_text('CONFIG_A=y\nCONFIG_S="str"\n# CONFIG_B is not set\nCONFIG_N=0x10\n')
    assert kf.read_config(str(c)) == {"CONFIG_A": "y", "CONFIG_S": "str", "CONFIG_N": "0x10"}


# --- the parser's corners ---------------------------------------------------------------------

ODD_MAKEFILE = """\
else
endif
obj-y += first.o
ifeq ($(CC),clang)
obj-y += clang.o
else ifeq ($(CONFIG_A),y)
obj-y += chain_unknown.o
else
obj-y += last_unknown.o
endif
ifdef CONFIG_A
obj-y += a_on.o
else ifeq ($(CC),gcc)
obj-y += a_else_unknown.o
endif
ifdef CONFIG_N
obj-y += n_off.o
else something odd
obj-y += else_text.o
endif
ifeq $(CONFIG_A) y
obj-y += odd_form.o
endif
include $(srctree)/other.mk
obj-$(SOMEVAR) += maybe.o
obj-y += $(EXTRA_OBJS) plain.o
lib-y += libdir/ lib.o
comp-y += compdir/ comp1.o
ccflags-y += -Iinclude -DFOO
obj-y += lib.a
"""


def test_parse_dir_corners(tmp_path):
    kf = load_script(SCRIPT)
    (tmp_path / "d").mkdir()
    (tmp_path / "d/Makefile").write_text(ODD_MAKEFILE)
    d = kf.parse_dir(str(tmp_path), "d", {"CONFIG_A": "y"})
    assert d.found
    on = [(None, True)]
    # Stray else/endif change nothing; a condition that is not on CONFIG_ only (or has an odd
    # form) counts as true, and so does every later branch of its chain: nothing hidden
    assert d.objects == {
        "first.o": on, "clang.o": on, "chain_unknown.o": on, "last_unknown.o": on, "a_on.o": on,
        "a_else_unknown.o": on, "n_off.o": [(None, False)], "else_text.o": on, "odd_form.o": on,
        # an unexpanded variable as the condition: assumed built; unexpanded objects are skipped
        "maybe.o": on, "plain.o": on,
        # lib.a counts as lib.o; lib-y and composite parts name no directories
        "lib.o": on + on, "comp1.o": [("comp.o", True)]}
    assert d.subdirs == {}
    assert not kf.parse_dir(str(tmp_path), "nowhere", {}).found


def test_condition_of_other_lines():
    kf = load_script(SCRIPT)
    assert kf.condition("obj-y += a.o", {}) is None
    assert kf.condition("ifeq $(CONFIG_A) y", {"CONFIG_A": "y"}) is None


@pytest.fixture
def small_tree(tmp_path):
    kf = load_script(SCRIPT)
    files = {
        "drivers/Makefile": "obj-y += gpu/ cyc/ parts/\n",
        "drivers/gpu/Makefile": "obj-y += drm/\n",
        "drivers/gpu/drm/Makefile": "obj-$(CONFIG_AMD) += amd/amdgpu/\n",
        "drivers/cyc/Makefile": "a-y := b.o\nb-y := a.o\n",
        "drivers/parts/Makefile": "c1-y := p.o\nc2-y := p.o\nobj-y += c2.o\n",
    }
    for path, text in files.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(text)
    return lambda config: kf.Tree(str(tmp_path), config, "arm64")


def test_reached_outside_the_built_tree(small_tree):
    t = small_tree({})
    assert t.reached("Documentation") is False       # a top-level directory kbuild never enters
    assert t.reached("arch/x86/kernel") is False     # another architecture
    assert t.reached("arch/arm64") is True
    # Nobody on the way names it, nor anything next to it: undecided, so listed
    assert t.reached("drivers/lonely/x") is None


def test_unnamed_directory_beside_named_ones(small_tree):
    # drm names only amd/amdgpu/; amd/amdkfd is pulled in through variables. Off with amdgpu
    assert small_tree({}).reached("drivers/gpu/drm/amd/amdkfd") is False
    assert small_tree({}).built("drivers/gpu/drm/amd/amdkfd/kfd.c") is False
    # With amdgpu built it may be built too: undecided, never hidden
    assert small_tree({"CONFIG_AMD": "y"}).reached("drivers/gpu/drm/amd/amdkfd") is None
    assert small_tree({"CONFIG_AMD": "y"}).built("drivers/gpu/drm/amd/amdkfd/kfd.c") is None


def test_composites(small_tree):
    t = small_tree({})
    # a.o is part of b.o, b.o part of a.o, neither is an obj-: a cycle, not built
    assert t.built("drivers/cyc/a.c") is False
    # p.o is part of c1.o (not built) and of c2.o (built)
    assert t.built("drivers/parts/p.c") is True
