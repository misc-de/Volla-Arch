# The level our services' plain stdout and stderr lines reach the journal at
# (device-volla-ansuz/50-plinius-log-level.conf). The journal stores only notice and above
# (50-plinius-journald.conf), systemd gives a line without a "<N>" prefix info: without the
# drop-in, what a failing service wrote (g_printerr(), a Python traceback, echo >&2) was lost.
# The package() of device-volla-ansuz runs for real into tmp_path; every service unit of the
# tree has to get the drop-in, through the prefix plinius- or by its own name.

import re
import shutil
import subprocess

import pytest

from conftest import PKG, REPO, git_files

DEVICE = PKG / "device-volla-ansuz"
DROPIN = DEVICE / "50-plinius-log-level.conf"
LEVELS = ["emerg", "alert", "crit", "err", "warning", "notice", "info", "debug"]
# Services of the tree that keep systemd's default: the Android container's init writes its
# boot lines there, which the journal is meant to leave out
KEEP_INFO = {"lxc-android-config"}


def settings(path):
    """{section: {key: value}} of a systemd style file"""
    out, section = {}, None
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            section = out.setdefault(line.strip("[]"), {})
        else:
            key, _, value = line.partition("=")
            section[key] = value
    return out


@pytest.fixture(scope="module")
def installed(tmp_path_factory):
    """The files package() of device-volla-ansuz installs, below their pkgdir"""
    base = tmp_path_factory.mktemp("device")
    src = base / "src"
    shutil.copytree(DEVICE, src, symlinks=False)
    pkgdir = base / "pkg"
    r = subprocess.run(["bash", "-c", 'set -e; source PKGBUILD; srcdir=$PWD; pkgdir=$1; package',
                        "bash", str(pkgdir)], cwd=src, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return pkgdir


def tree_services():
    """{unit name without .service: "system" or "user"} of every service unit in pkgbuilds/,
    the manager from the install line in its package's PKGBUILD"""
    out = {}
    for path in git_files("pkgbuilds"):
        if path.suffix != ".service" or "[Service]" not in path.read_text() or path.stem in KEEP_INFO:
            continue
        pkg = path.relative_to(PKG).parts[0]
        package = (PKG / pkg / "PKGBUILD").read_text().split("package() {", 1)[1]
        # the install line that names it, continued with "\" or not, the target anywhere in it
        lines = [line for line in package.replace("\\\n", " ").splitlines()
                 if re.search(r"\b" + re.escape(path.name) + r"\b", line)
                 and re.search(r"usr/lib/systemd/(system|user)\b", line)]
        assert lines, f"{path.relative_to(REPO)}: no install line in {pkg}/PKGBUILD"
        out[path.stem] = re.search(r"usr/lib/systemd/(system|user)\b", lines[0]).group(1)
    return out


def test_plain_output_is_stored_at_notice():
    assert settings(DROPIN) == {"Service": {"SyslogLevel": "notice"}}


def test_the_level_is_one_the_journal_keeps():
    journal = settings(DEVICE / "50-plinius-journald.conf")["Journal"]
    stored = LEVELS.index(journal["MaxLevelStore"])
    assert LEVELS.index(settings(DROPIN)["Service"]["SyslogLevel"]) <= stored
    # systemd's own default for such lines would be lost: the drop-in is needed at all
    assert LEVELS.index("info") > stored


def test_installed_for_every_plinius_unit_of_both_managers(installed):
    for manager in ("system", "user"):
        f = installed / f"usr/lib/systemd/{manager}/plinius-.service.d/50-plinius-log-level.conf"
        assert f.read_bytes() == DROPIN.read_bytes()


def test_every_service_of_the_tree_gets_it(installed):
    services = tree_services()
    # the tree has units of both managers with and without the prefix; a regex that found
    # none would let everything pass
    assert (services["plinius-charge"], services["plinius-cardshare"], services["gnss-nmea-bridge"],
            services["audio-hal-bridge"]) == ("system", "user", "system", "user")
    missing = []
    for name, manager in sorted(services.items()):
        # systemd applies plinius-.service.d to plinius-x.service and to plinius-x@i.service
        dirs = [f"{name}.service.d"] + (["plinius-.service.d"] if name.startswith("plinius-") else [])
        if not any((installed / f"usr/lib/systemd/{manager}" / d / DROPIN.name).is_file() for d in dirs):
            missing.append(f"{manager}/{name}.service")
    assert not missing, "no 50-plinius-log-level.conf for: " + ", ".join(missing)


def test_no_drop_in_for_a_unit_the_tree_does_not_have(installed):
    services = tree_services()
    for manager in ("system", "user"):
        for f in (installed / f"usr/lib/systemd/{manager}").glob(f"*.service.d/{DROPIN.name}"):
            name = f.parent.name.removesuffix(".service.d")
            if name == "plinius-":
                continue
            assert services.get(name) == manager, f"{manager}/{f.parent.name}: no such unit"


def test_the_android_container_keeps_the_default(installed):
    assert (PKG / "lxc-android-halium/lxc-android-config.service").is_file()
    assert not list(installed.glob(f"usr/lib/systemd/*/lxc-android-config.service.d/{DROPIN.name}"))
