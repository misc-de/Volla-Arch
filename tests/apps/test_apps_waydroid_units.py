# waydroid-plinius: how Android starts. The container service runs from boot on once Waydroid is
# initialised, the battery and NFC bridges start with it, and the install script enables the
# container service once (systemctl and vercmp are stubs).

import subprocess

import pytest

from conftest import PKG

INSTALL = PKG / "waydroid-plinius" / "waydroid-plinius.install"
# vercmp as pacman's for the versions used here: older than 1.6.3-6 gives -1
VERCMP = r'''
case "$1" in 1.6.3-[1-5]) echo -1 ;; 1.6.3-6) echo 0 ;; *) echo 1 ;; esac
'''


def unit(path):
    """The sections of a unit file; repeated keys (After=) are joined, as systemd adds them up."""
    text = {}
    section = None
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            section = text.setdefault(line[1:-1], {})
            continue
        key, _, value = line.partition("=")
        section[key] = (section.get(key, "") + " " + value).strip()
    return text


def scriptlet(fake_bin, func, *args):
    fake_bin.stub("systemctl")
    fake_bin.stub("vercmp", VERCMP)
    q = " ".join(args)
    r = subprocess.run(["bash", "-c", f'. "{INSTALL}"; {func} {q}'], env=fake_bin.env(isolated=True),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return fake_bin.calls("systemctl")


def test_bridges_for_android_only_start_with_the_container():
    dropin = unit(PKG / "waydroid-plinius" / "50-plinius.conf")["Unit"]
    for bridge in ("health-hal-bridge.service", "nfc-hal-bridge.service"):
        assert bridge in dropin["Wants"].split() and bridge in dropin["After"].split()
        service = unit(PKG / bridge.removesuffix(".service") / bridge)
        assert service["Install"]["WantedBy"] == "waydroid-container.service"
    # The bridges Linux needs anyway are started by their own [Install], here only ordered
    for bridge in ("sensor-proxy-bridge.service", "gnss-nmea-bridge.service", "vibrator-ff-bridge.service"):
        assert bridge in dropin["After"].split() and bridge not in dropin["Wants"].split()
    assert "waydroid-plinius-screen.service" in dropin["Wants"].split()


def test_container_needs_an_initialised_waydroid():
    dropin = unit(PKG / "waydroid-plinius" / "50-plinius.conf")["Unit"]
    assert dropin["ConditionPathExists"] == "/var/lib/waydroid/waydroid.cfg"


def test_install_enables_the_container(fake_bin):
    assert scriptlet(fake_bin, "post_install", "1.6.3-6") == [["enable", "waydroid-container.service"]]


@pytest.mark.parametrize("old, enabled", [("1.6.3-5", True), ("1.6.3-6", False), ("1.6.3-7", False)])
def test_upgrade_enables_it_only_once(fake_bin, old, enabled):
    calls = scriptlet(fake_bin, "post_upgrade", "1.6.3-7", old)
    assert calls == ([["enable", "waydroid-container.service"]] if enabled else [])


def test_remove_disables_the_container(fake_bin):
    assert scriptlet(fake_bin, "pre_remove", "1.6.3-6") == [["disable", "waydroid-container.service"]]
