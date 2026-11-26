# plinius-connectivity-set (device-volla-ansuz): NetworkManager's connection check from Settings >
# Privacy > Automatic Connections, run through pkexec. Only a copy with its config path in
# tmp_path runs; nmcli is a stub that records the reload. The policy lets the active user run
# exactly this tool without a password.

import xml.etree.ElementTree as ET

import pytest

from conftest import PKG
from system_helpers import rewritten_copy, run

TOOL = PKG / "device-volla-ansuz" / "plinius-connectivity-set"
POLICY = PKG / "device-volla-ansuz" / "org.plinius.connectivity.policy"
HEADER = "# Written by plinius-connectivity-set (GNOME Settings > Privacy > Automatic Connections)\n"


class Tool:
    def __init__(self, tmp_path, fake_bin):
        self.bin = fake_bin
        self.conf = tmp_path / "etc/NetworkManager/conf.d/90-plinius-connectivity.conf"
        self.script = rewritten_copy(TOOL, tmp_path / "plinius-connectivity-set",
                                     {"/etc/NetworkManager/conf.d/90-plinius-connectivity.conf": self.conf})
        fake_bin.stub("nmcli")

    def run(self, *args):
        return run(["bash", self.script, *args], self.bin.env(isolated=True))


@pytest.fixture
def tool(tmp_path, fake_bin):
    return Tool(tmp_path, fake_bin)


def test_off_turns_the_check_off_and_reloads(tool):
    r = tool.run("off")
    assert r.returncode == 0, r.stderr
    assert tool.conf.read_text() == HEADER + "[connectivity]\nenabled=false\n"
    assert oct(tool.conf.stat().st_mode & 0o777) == "0o644"
    assert tool.bin.calls("nmcli") == [["general", "reload", "conf"]]


@pytest.mark.parametrize("uri, response", [
    ("http://ping.archlinux.org/nm-check.txt", "NetworkManager is online"),
    ("http://nmcheck.gnome.org/check_network_status.txt", "NetworkManager is online"),
    ("http://connectivity-check.ubuntu.com./", ""),
    ("https://192.0.2.7:8443/check", "OK"),
    ("http://printer.local", "fine, thanks!"),
])
def test_on_writes_the_server(tool, uri, response):
    tool.conf.parent.mkdir(parents=True)
    tool.conf.write_text("[connectivity]\nenabled=false\n")
    r = tool.run("on", uri, response)
    assert r.returncode == 0, r.stderr
    assert tool.conf.read_text() == HEADER + f"[connectivity]\nenabled=true\nuri={uri}\nresponse={response}\n"
    assert tool.bin.calls("nmcli") == [["general", "reload", "conf"]]
    # no temporary file left next to it
    assert [p.name for p in tool.conf.parent.iterdir()] == [tool.conf.name]


@pytest.mark.parametrize("uri", [
    "ftp://192.0.2.7/", "file:///etc/shadow", "http://", "http://-bad.example/", "printer.local/x",
    "http://192.0.2.7/a b", "http://192.0.2.7/\nenabled=false", "http://user@192.0.2.7/",
    "http://192.0.2.7/?q=1", "http://192.0.2.7:123456/", "http://" + "a" * 200 + ".example/",
])
def test_bad_addresses_are_refused(tool, uri):
    r = tool.run("on", uri, "OK")
    assert r.returncode == 3
    assert r.stderr == f"plinius-connectivity-set: not an http or https address: {uri}\n"
    assert not tool.conf.exists() and tool.bin.calls("nmcli") == []


@pytest.mark.parametrize("response", [" OK", "OK ", "two  spaces", "a\nb", "[connectivity]", "x=y", "a" * 101,
                                      "#comment", "quote\"d"])
def test_bad_answers_are_refused(tool, response):
    r = tool.run("on", "http://192.0.2.7/", response)
    assert r.returncode == 3
    assert r.stderr.startswith("plinius-connectivity-set: not a plain answer: ")
    assert not tool.conf.exists()


@pytest.mark.parametrize("args", [(), ("maybe",), ("off", "x"), ("on",), ("on", "http://192.0.2.7/")])
def test_usage(tool, args):
    r = tool.run(*args)
    assert r.returncode == 2
    assert r.stderr == "usage: plinius-connectivity-set off | on URI RESPONSE\n"
    assert not tool.conf.exists()


def test_written_without_networkmanager(tool):
    tool.bin.stub("nmcli", "exit 8")
    r = tool.run("off")
    assert r.returncode == 0, r.stderr
    assert tool.conf.read_text().endswith("enabled=false\n")


def test_a_link_at_the_path_is_replaced_not_followed(tool, tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text("keep\n")
    tool.conf.parent.mkdir(parents=True)
    tool.conf.symlink_to(target)
    assert tool.run("off").returncode == 0
    assert not tool.conf.is_symlink() and target.read_text() == "keep\n"


def test_policy_lets_the_active_user_run_only_this_tool():
    action = ET.parse(POLICY).getroot().find("action")
    assert action.get("id") == "org.plinius.connectivity.set"
    d = action.find("defaults")
    assert (d.findtext("allow_any"), d.findtext("allow_inactive"), d.findtext("allow_active")) == ("no", "no", "yes")
    path = action.find("annotate[@key='org.freedesktop.policykit.exec.path']").text
    assert path == "/usr/lib/plinius/plinius-connectivity-set"
    pkgbuild = (PKG / "device-volla-ansuz" / "PKGBUILD").read_text()
    assert 'install -Dm755 plinius-connectivity-set -t "$pkgdir/usr/lib/plinius"' in pkgbuild
    assert 'install -Dm644 org.plinius.connectivity.policy -t "$pkgdir/usr/share/polkit-1/actions"' in pkgbuild
    # the GNOME Settings page calls it there
    gcc = (PKG / "gnome-control-center-plinius" / "cc-plinius-connections.c").read_text()
    assert f'#define SET_TOOL   "{path}"' in gcc
