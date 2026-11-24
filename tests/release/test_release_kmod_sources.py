# The out-of-tree kernel modules (plinius-backlight, plinius-btsuspend, plinius-novt) and the
# ABI check they are built with. The modules are kernel code for the phone's arm64 kernel: they
# compile only inside its configured source tree and run only on the phone, so no host test runs
# them. Checked here instead: plinius-kmod (for UT's kernel) and plinius-kernel (our own build)
# carry the very same module sources and Kbuild, and abicheck.c, which tools/kernel-abi.py
# generates from the kernel's BTF, is complete and consistent: every struct member the modules
# reach into is among its asserts, each struct's offsets lie inside its size and in member order.

import re

import pytest

from conftest import PKG

KMOD = PKG / "plinius-kmod"
KERNEL = PKG / "plinius-kernel"
MODULES = ["plinius-backlight.c", "plinius-btsuspend.c", "plinius-novt.c"]

SIZEOF = re.compile(r'_Static_assert\(sizeof\(struct (\w+)\) == (\d+), "size of (\w+)"\);')
OFFSETOF = re.compile(r'_Static_assert\(offsetof\(struct (\w+), (\w+)\) == (\d+), "(\w+)\.(\w+)"\);')

# The members the modules read or write themselves (the rest goes through kernel functions)
USED = {
    "plinius-backlight.c": [("backlight_device", "props"), ("backlight_properties", "brightness"),
                            ("backlight_properties", "max_brightness"), ("backlight_properties", "type"),
                            ("backlight_properties", "scale"), ("backlight_ops", "update_status"),
                            ("led_classdev", "name"), ("led_classdev", "dev"),
                            ("led_classdev", "brightness"), ("led_classdev", "max_brightness"),
                            ("device", "parent"), ("device", "driver"), ("device_driver", "owner")],
    "plinius-btsuspend.c": [("kprobe", "symbol_name"), ("kprobe", "pre_handler")],
    "plinius-novt.c": [("platform_device", "dev")],
}


def asserts():
    """{struct: (size, [(member, offset), ...])} in the order of abicheck.c"""
    out = {}
    for line in (KMOD / "abicheck.c").read_text().splitlines():
        if m := SIZEOF.fullmatch(line):
            assert m.group(1) == m.group(3), line
            assert m.group(1) not in out, f"{m.group(1)} twice"
            out[m.group(1)] = (int(m.group(2)), [])
        elif m := OFFSETOF.fullmatch(line):
            struct, member = m.group(1), m.group(2)
            assert (struct, member) == (m.group(4), m.group(5)), line
            # The size comes first, and a struct's asserts stay together
            assert list(out)[-1] == struct, line
            out[struct][1].append((member, int(m.group(3))))
    return out


@pytest.mark.parametrize("name", MODULES + ["Kbuild"])
def test_both_packages_build_the_same_modules(name):
    assert (KMOD / name).read_bytes() == (KERNEL / name).read_bytes()


@pytest.mark.parametrize("pkgbuild", [KMOD / "PKGBUILD", KERNEL / "PKGBUILD"])
def test_every_module_is_built_and_installed(pkgbuild):
    text = pkgbuild.read_text()
    kbuild = (pkgbuild.parent / "Kbuild").read_text()
    for src in MODULES:
        mod = src.removesuffix(".c")
        assert f"obj-m += {mod}.o" in kbuild
        assert re.search(rf"^\s+{re.escape(src)}\)?$", text, re.M), f"{src} not a source"
        assert re.search(rf"for m in [^;]*\b{mod}\b[^;]*; do", text), f"{mod}.ko not installed"


def test_abicheck_has_only_generated_asserts():
    lines = (KMOD / "abicheck.c").read_text().splitlines()
    other = [line for line in lines if line and not SIZEOF.fullmatch(line) and not OFFSETOF.fullmatch(line)]
    # The header comment, the headers of the checked structs, the license kbuild wants
    assert other[-1] == 'MODULE_LICENSE("GPL");'
    for line in other[:-1]:
        assert line.startswith(("//", "/*", " *", " */", "#include <linux/")), line


def test_abicheck_is_consistent():
    checked = asserts()
    assert len(checked) >= 15
    for struct, (size, members) in checked.items():
        assert size > 0
        offsets = [off for _m, off in members]
        # In member order, as the BTF lists them, and inside the struct
        assert offsets == sorted(offsets), struct
        assert all(0 <= off < size for off in offsets), struct
        assert len({m for m, _o in members}) == len(members), struct


@pytest.mark.parametrize("module,uses", list(USED.items()))
def test_abicheck_covers_what_the_modules_use(module, uses):
    checked = asserts()
    for struct, member in uses:
        assert struct in checked, f"{module}: struct {struct} not checked"
        assert member in dict(checked[struct][1]), f"{module}: {struct}.{member} not checked"


def test_the_modules_reach_into_these_members():
    # The table above is what the code does: each member is used by its module
    for module, uses in USED.items():
        text = (KMOD / module).read_text()
        for _struct, member in uses:
            assert re.search(rf"(->|\.){member}\b", text), (module, member)
