# plinius-mobile-firewall (NetworkManager dispatcher script): the chain that drops new
# connections from the internet on mobile data. nmcli, the iptables tools and logger are stubs;
# the script touches no paths of its own, so the original runs.

import pytest

from conftest import PKG
from system_helpers import run

SRC = PKG / "device-volla-ansuz" / "plinius-mobile-firewall"


class Firewall:
    def __init__(self, tmp_path, fake_bin):
        self.tmp = tmp_path
        self.bin = fake_bin
        self.devices = tmp_path / "devices"
        # NetworkManager's view: device type state ip-interface
        self.set_devices([("/ril_0", "gsm", "connected", "ccmni2"),
                          ("/ril_1", "gsm", "disconnected", "ccmni4"),
                          ("wlan0", "wifi", "connected", "wlan0"),
                          ("ccmni1", "gsm", "connected (externally)", "ccmni1x")])
        fake_bin.stub("nmcli", f'''
if [ "$1" = -t ]; then awk '{{ s = $3; for (i = 4; i < NF; i++) s = s " " $i; print $1 ":" $2 ":" s }}' "{self.devices}"; exit; fi
# -g FIELD device show DEV
line=$(awk -v d="$5" '$1 == d' "{self.devices}")
[ -n "$line" ] || exit 10
case $2 in GENERAL.TYPE) echo "$line" | awk '{{ print $2 }}' ;; GENERAL.IP-IFACE) echo "$line" | awk '{{ print $NF }}' ;; esac''')
        for tool in ("iptables-legacy", "ip6tables-legacy"):
            fake_bin.stub(tool + "-restore", f'cat > "{tmp_path}/{tool}.rules"')
            fake_bin.stub(tool, f'case $1 in -C) [ -e "{tmp_path}/{tool}.jump" ] ;; '
                          f'-I) touch "{tmp_path}/{tool}.jump" ;; esac')
        fake_bin.stub("logger")

    def set_devices(self, rows):
        self.devices.write_text("".join(" ".join(r) + "\n" for r in rows))

    def run(self, *args, **env):
        return run(["bash", SRC, *args], self.bin.env(isolated=True, **env))

    def rules(self, family=4):
        tool = "iptables-legacy" if family == 4 else "ip6tables-legacy"
        f = self.tmp / f"{tool}.rules"
        return f.read_text().splitlines() if f.exists() else None

    def interfaces(self, family=4):
        return sorted({r.split(" -i ")[1].split()[0] for r in self.rules(family) if " -i " in r})


@pytest.fixture
def fw(tmp_path, fake_bin):
    return Firewall(tmp_path, fake_bin)


def test_apply_filters_every_connected_mobile_interface(fw):
    r = fw.run("apply")
    assert r.returncode == 0, r.stderr
    v4 = fw.rules(4)
    assert v4[:2] == ["*filter", ":plinius-mobile - [0:0]"] and v4[-1] == "COMMIT"
    # "connected (externally)" counts too; Wi-Fi and the disconnected modem do not
    assert fw.interfaces(4) == ["ccmni1x", "ccmni2"]
    # Per interface: answers and ICMP in, then everything else dropped, in that order
    for i in ("ccmni1x", "ccmni2"):
        at = v4.index(f"-A plinius-mobile -i {i} -m conntrack --ctstate ESTABLISHED,RELATED "
                      "-j RETURN")
        assert v4[at + 1:at + 3] == [f"-A plinius-mobile -i {i} -p icmp -j RETURN",
                                     f"-A plinius-mobile -i {i} -j DROP"]
    assert "-A plinius-mobile -i ccmni2 -p ipv6-icmp -j RETURN" in fw.rules(6)
    assert not any("-p icmp " in r for r in fw.rules(6))
    assert fw.bin.calls("iptables-legacy-restore") == [["--noflush"]]


def test_without_arguments_means_apply(fw):
    assert fw.run().returncode == 0
    assert fw.interfaces() == ["ccmni1x", "ccmni2"]


def test_jump_inserted_first_and_only_once(fw):
    assert fw.run("apply").returncode == 0
    assert fw.run("apply").returncode == 0
    inserts = [c for c in fw.bin.calls("iptables-legacy") if c[0] == "-I"]
    assert inserts == [["-I", "INPUT", "1", "-j", "plinius-mobile"]]


def test_no_mobile_data_empty_chain(fw):
    fw.set_devices([("wlan0", "wifi", "connected", "wlan0")])
    assert fw.run("apply").returncode == 0
    assert fw.rules(4) == ["*filter", ":plinius-mobile - [0:0]", "COMMIT"]


def test_pre_up_adds_the_interface_before_it_is_connected(fw):
    fw.set_devices([("/ril_0", "gsm", "connecting (prepare)", "ccmni2")])
    r = fw.run("/ril_0", "pre-up", DEVICE_IFACE="/ril_0", DEVICE_IP_IFACE="ccmni2")
    assert r.returncode == 0, r.stderr
    assert fw.interfaces() == ["ccmni2"]


def test_down_removes_the_interface_still_listed_as_connected(fw):
    r = fw.run("/ril_0", "down", DEVICE_IFACE="/ril_0", DEVICE_IP_IFACE="ccmni2")
    assert r.returncode == 0, r.stderr
    assert fw.interfaces() == ["ccmni1x"]


def test_modem_without_ip_interface_adds_nothing(fw):
    # Without its own IP interface NetworkManager passes the device name ("/ril_0")
    fw.set_devices([("/ril_0", "gsm", "connecting (getting IP configuration)", "/ril_0")])
    r = fw.run("/ril_0", "up", DEVICE_IFACE="/ril_0", DEVICE_IP_IFACE="/ril_0")
    assert r.returncode == 0
    assert fw.interfaces() == []


def test_events_of_other_devices_change_nothing(fw):
    r = fw.run("wlan0", "up", DEVICE_IFACE="wlan0", DEVICE_IP_IFACE="wlan0")
    assert r.returncode == 0
    assert fw.rules() is None and fw.bin.calls("iptables-legacy") == []


@pytest.mark.parametrize("action", ["hostname", "connectivity-change", "vpn-up", "bogus", ""])
def test_other_actions_are_ignored(fw, action):
    r = fw.run("ccmni2", action, DEVICE_IFACE="/ril_0")
    assert r.returncode == 0
    assert fw.rules() is None and fw.bin.calls("nmcli") == []


def test_failure_is_logged_and_reported(fw, fake_bin):
    fake_bin.stub("ip6tables-legacy-restore", "cat > /dev/null; exit 1")
    r = fw.run("apply")
    assert r.returncode == 1
    # IPv4 was still applied
    assert fw.interfaces(4) == ["ccmni1x", "ccmni2"]
    (call,) = fake_bin.calls("logger")
    assert call[:2] == ["-t", "plinius-mobile-firewall"] and "ccmni2" in call[2]
    # No jump to a chain that may not exist
    assert not [c for c in fake_bin.calls("ip6tables-legacy") if c[0] == "-I"]
