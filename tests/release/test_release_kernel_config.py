# plinius-kernel's prepare(): the options it sets on top of UT's config. prepare() runs as in
# makepkg, in a source directory made here, with a stand-in for the kernel's scripts/config that
# edits the .config the same way (-e, -d, -m, --set-val, --set-str), so no kernel tree is needed.

import re
import shutil
import subprocess

import pytest

from conftest import PKG

KPKG = PKG / "plinius-kernel"

# scripts/config of the kernel, reduced to what prepare() uses: an option is replaced where it
# stands ("CONFIG_X=..." or "# CONFIG_X is not set") or appended
FAKE_CONFIG = r"""#!/bin/bash
set -euo pipefail
[[ $1 == --file ]]; f=$2; shift 2
put() {
	local name=CONFIG_$1 line=$2
	if grep -qE "^($name=|# $name is not set)" "$f"; then
		sed -i -E "s|^($name=.*\|# $name is not set)\$|$line|" "$f"
	else
		echo "$line" >> "$f"
	fi
}
while (( $# )); do
	case $1 in
		-e) put "$2" "CONFIG_$2=y"; shift 2 ;;
		-m) put "$2" "CONFIG_$2=m"; shift 2 ;;
		-d) put "$2" "# CONFIG_$2 is not set"; shift 2 ;;
		--set-val) put "$2" "CONFIG_$2=$3"; shift 3 ;;
		--set-str) put "$2" "CONFIG_$2=\"$3\""; shift 3 ;;
		*) echo "unknown argument $1" >&2; exit 2 ;;
	esac
done
"""

# Modules prepare() switches on and system_dlkm-modules.load loads: module file -> option
ADDED_MODULES = {
    "ch341.ko": "USB_SERIAL_CH341",
    "cp210x.ko": "USB_SERIAL_CP210X",
    "pl2303.ko": "USB_SERIAL_PL2303",
    "rndis_host.ko": "USB_NET_RNDIS_HOST",
}


def pkgbuild_var(name):
    m = re.search(rf"^{name}=(\S+)$", (KPKG / "PKGBUILD").read_text(), re.M)
    assert m, name
    return m.group(1)


@pytest.fixture(scope="module")
def config(tmp_path_factory):
    """obj/.config as prepare() leaves it, as a dict option -> value (None: not set)."""
    src = tmp_path_factory.mktemp("kernel-src")
    for f in KPKG.iterdir():
        if f.is_file() and not f.name.endswith(".tar.gz"):
            shutil.copy(f, src)
    tree = src / f"plinius-kernel-{pkgbuild_var('_kcommit')}"
    for rel in ("drivers/leds/uleds.c", "net/ipv4/netfilter/ipt_ah.c", "net/ipv6/netfilter/ip6t_ah.c"):
        (tree / rel).parent.mkdir(parents=True, exist_ok=True)
        (tree / rel).write_text("/* stand-in */\n")
    script = tree / "scripts/config"
    script.parent.mkdir(parents=True)
    script.write_text(FAKE_CONFIG)
    script.chmod(0o755)
    subprocess.run(["bash", "-c", 'set -euo pipefail; srcdir=$PWD; . ./PKGBUILD; prepare'],
                   cwd=src, check=True, capture_output=True, text=True)
    values = {}
    for line in (src / "obj/.config").read_text().splitlines():
        if m := re.fullmatch(r"CONFIG_(\w+)=(.*)", line):
            values[m.group(1)] = m.group(2)
        elif m := re.fullmatch(r"# CONFIG_(\w+) is not set", line):
            values[m.group(1)] = None
    return values


def test_landlock_is_built_in_and_in_the_lsm_order(config):
    assert config["SECURITY_LANDLOCK"] == "y"
    # Landlock depends on the path hooks; the LSM order decides whether it starts at all
    assert config["SECURITY_PATH"] == "y"
    assert "landlock" in config["LSM"].strip('"').split(",")


@pytest.mark.parametrize("module,option", ADDED_MODULES.items())
def test_added_modules_are_built_and_loaded(config, module, option):
    assert config[option] == "m"
    loaded = (KPKG / "system_dlkm-modules.load").read_text().split()
    assert module in loaded


def test_added_modules_load_after_what_they_need():
    loaded = (KPKG / "system_dlkm-modules.load").read_text().split()
    for module in ("ch341.ko", "cp210x.ko", "pl2303.ko"):
        assert loaded.index("usbserial.ko") < loaded.index(module)
    assert loaded.index("usbnet.ko") < loaded.index("rndis_host.ko")
    assert loaded.index("cdc_ether.ko") < loaded.index("rndis_host.ko")
    assert len(loaded) == len(set(loaded))


def test_earlier_options_stay(config):
    # The options of earlier releases: Yama, POSIX ACLs on tmpfs, no f2fs, no -Werror
    assert config["SECURITY_YAMA"] == "y"
    assert config["TMPFS_POSIX_ACL"] == "y"
    assert config["F2FS_FS"] is None
    assert config["WERROR"] is None
    assert config["LOG_BUF_SHIFT"] == "21"
    assert config["LOCALVERSION"] == '""'
