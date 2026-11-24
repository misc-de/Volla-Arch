# The shell scripts of plinius-desktop: the firewall switch (plinius-desktop-set, as a copy with
# its paths in tmp_path), the bar's Wi-Fi and battery status, fullscreen, the VNC keys.

import json
import os
import stat
import subprocess

import pytest

from conftest import PKG

DESK = PKG / "plinius-desktop"


# ----- plinius-desktop-set -----

@pytest.fixture
def dset(tmp_path, fake_bin):
    """plinius-desktop-set with conf and marker in tmp_path; iptables stubs log their rules."""
    conf = tmp_path / "etc" / "plinius-desktop.conf"
    conf.parent.mkdir()
    marker = tmp_path / "run" / "plinius-desktop-firewall"
    marker.parent.mkdir()
    text = (DESK / "plinius-desktop-set").read_text()
    assert "conf=/etc/plinius-desktop.conf\n" in text and "firewall_ok=/run/" in text
    text = text.replace("conf=/etc/plinius-desktop.conf", f"conf={conf}")
    text = text.replace("firewall_ok=/run/plinius-desktop-firewall", f"firewall_ok={marker}")
    script = tmp_path / "plinius-desktop-set"
    script.write_text(text)
    rules = tmp_path / "rules"
    rules.mkdir()
    for tool in ("iptables-legacy", "ip6tables-legacy"):
        fake_bin.stub(tool + "-restore", f'cat > "{rules}/{tool}"; exit "${{RESTORE_FAIL:-0}}"')
        # -C answers "jump exists" when JUMP_EXISTS is set
        fake_bin.stub(tool, '[ "$2" = -C ] && { [ -n "$JUMP_EXISTS" ]; exit; }; exit 0')

    def run(*args, **env):
        return subprocess.run(["bash", str(script), *args], capture_output=True, text=True,
                              env=fake_bin.env(isolated=True, **env))
    run.conf, run.marker, run.rules, run.bin = conf, marker, rules, fake_bin
    return run


def rules_of(dset, tool):
    return (dset.rules / tool).read_text().splitlines()


def test_status_defaults_without_a_file(dset):
    r = dset("status")
    assert (r.returncode, r.stdout) == (0, "usb=yes\nlocal=no\n")


def test_status_reads_the_file(dset):
    dset.conf.write_text("# comment\n  usb = no  # off\nlocal=maybe\nlocal=yes\n")
    assert dset("status").stdout == "usb=no\nlocal=yes\n"


def test_apply_defaults_usb_only(dset):
    r = dset("apply")
    assert r.returncode == 0, r.stderr
    v4 = rules_of(dset, "iptables-legacy")
    assert v4 == ["*filter", ":plinius-desktop - [0:0]", "-A plinius-desktop -i lo -j RETURN",
                  "-A plinius-desktop -i usb0 -j RETURN", "-A plinius-desktop -j DROP", "COMMIT"]
    assert rules_of(dset, "ip6tables-legacy") == v4
    assert dset.marker.exists()
    # The jump from INPUT is checked and, missing, inserted for both families
    for tool in ("iptables-legacy", "ip6tables-legacy"):
        assert dset.bin.calls(tool) == [
            ["-w", "-C", "INPUT", "-p", "tcp", "--dport", "5900", "-j", "plinius-desktop"],
            ["-w", "-I", "INPUT", "1", "-p", "tcp", "--dport", "5900", "-j", "plinius-desktop"]]
        assert dset.bin.calls(tool + "-restore") == [["-w", "--noflush"]]


def test_apply_keeps_an_existing_jump(dset):
    assert dset("apply", JUMP_EXISTS="1").returncode == 0
    assert [c[1] for c in dset.bin.calls("iptables-legacy")] == ["-C"]


def test_allow_local_writes_the_file_and_opens_local_networks(dset):
    dset.conf.write_text((DESK / "plinius-desktop.conf").read_text())
    r = dset("allow", "local")
    assert r.returncode == 0, r.stderr
    text = dset.conf.read_text()
    # Comments and the other key stay, the key changes in place
    assert text.startswith("# The networks")
    assert "usb=yes\n" in text and "local=yes\n" in text and "local=no" not in text
    assert stat.S_IMODE(dset.conf.stat().st_mode) == 0o644
    v4 = rules_of(dset, "iptables-legacy")
    v6 = rules_of(dset, "ip6tables-legacy")
    for iface in ("wlan+", "ap+", "p2p+", "eth+", "enx+"):
        for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"):
            assert f"-A plinius-desktop -i {iface} -s {net} -j RETURN" in v4
        for net in ("fe80::/10", "fc00::/7"):
            assert f"-A plinius-desktop -i {iface} -s {net} -j RETURN" in v6
        assert not any("fe80" in rule for rule in v4)
    # Never mobile data, and everything else is dropped last
    assert not any("ccmni" in rule or "rmnet" in rule for rule in v4 + v6)
    assert v4[-2:] == ["-A plinius-desktop -j DROP", "COMMIT"]


def test_deny_usb_without_a_file(dset):
    assert dset("deny", "usb").returncode == 0
    assert dset.conf.read_text() == "usb=no\n"
    assert "-A plinius-desktop -i usb0 -j RETURN" not in rules_of(dset, "iptables-legacy")
    assert dset("status").stdout == "usb=no\nlocal=no\n"


def test_no_marker_when_the_rules_fail(dset):
    r = dset("apply", RESTORE_FAIL="1")
    assert r.returncode != 0
    assert not dset.marker.exists()


@pytest.mark.parametrize("args", [(), ("allow",), ("allow", "mobile"), ("open", "usb")])
def test_usage_errors(dset, args):
    r = dset(*args)
    assert r.returncode == 2 and "Usage" in r.stderr
    assert not dset.conf.exists() and dset.bin.calls("iptables-legacy-restore") == []


# ----- wifi-status.sh -----

@pytest.mark.parametrize("listing, cls", [
    ("", "offline"), (" :70\n", "offline"),
    ("*:92\n", "excellent"), ("*:80\n", "excellent"), (" :99\n*:79\n", "good"),
    ("*:55\n", "good"), ("*:30\n", "ok"), ("*:5\n", "weak"), ("*:4\n", "none"),
])
def test_wifi_status(fake_bin, listing, cls):
    fake_bin.stub("nmcli", f"printf '{listing}'")
    r = subprocess.run(["sh", str(DESK / "wifi-status.sh")], capture_output=True, text=True,
                       env=fake_bin.env(isolated=True))
    assert json.loads(r.stdout) == {"text": "▣", "class": cls}
    assert fake_bin.calls("nmcli") == [["-t", "-f", "IN-USE,SIGNAL", "dev", "wifi", "list",
                                        "--rescan", "no"]]


# ----- battery-status.sh -----

UPOWER = """\
  native-path:          battery
  battery
    present:             yes
    state:               {state}
    energy-rate:         4,417 W
    percentage:          {pct}
"""


@pytest.mark.parametrize("pct, state, cls", [
    ("57%", "discharging", "l60"), ("54%", "discharging", "l50"), ("4%", "discharging", "l0"),
    ("100%", "discharging", "l100"), ("45%", "charging", "l50-charging"),
    # The theme has no 100 % charging icon
    ("96%", "charging", "l100-charged"), ("88%", "fully-charged", "l100-charged"),
    ("50%", "pending-charge", "l50-plugged-in"), ("31%", "pending-discharge", "l30-plugged-in"),
    ("", "unknown", "l0"),
])
def test_battery_status(fake_bin, pct, state, cls):
    fake_bin.stub("upower", f"""
if [ "$1" = -e ]; then
  echo /org/freedesktop/UPower/devices/line_power_ac
  echo /org/freedesktop/UPower/devices/battery_battery
  exit 0
fi
cat <<'EOF'
{UPOWER.format(state=state, pct=pct)}EOF""")
    r = subprocess.run(["sh", str(DESK / "battery-status.sh")], capture_output=True, text=True,
                       env=fake_bin.env(isolated=True))
    assert json.loads(r.stdout) == {"text": "▣", "class": cls}
    assert fake_bin.calls("upower")[1] == ["-i", "/org/freedesktop/UPower/devices/battery_battery"]


def test_fullscreen_sends_super_f11(fake_bin):
    fake_bin.stub("wtype")
    subprocess.run(["sh", str(DESK / "fullscreen.sh")], check=True,
                   env=fake_bin.env(isolated=True))
    assert fake_bin.calls("wtype") == [["-M", "logo", "-k", "F11", "-m", "logo"]]


# ----- plinius-desktop-keys -----

OPENSSL = r"""
out=; keyout=
while [ $# -gt 0 ]; do
  case $1 in -out) out=$2; shift ;; -keyout) keyout=$2; shift ;; esac
  shift
done
[ -n "$keyout" ] && echo key > "$keyout"
[ -n "$out" ] && echo pem > "$out"
[ -z "$out$keyout" ] && echo "sha256 Fingerprint=AB:CD:EF"
exit 0
"""


def test_keys_are_made_once_and_kept_private(tmp_path, fake_bin):
    fake_bin.stub("openssl", OPENSSL)
    state = tmp_path / "state"
    env = fake_bin.env(isolated=True, HOME=tmp_path / "home", XDG_STATE_HOME=state)
    r = subprocess.run(["sh", str(DESK / "plinius-desktop-keys")], capture_output=True,
                       text=True, env=env)
    assert (r.returncode, r.stdout) == (0, "AB:CD:EF\n"), r.stderr
    d = state / "plinius-desktop"
    assert sorted(os.listdir(d)) == ["rsa_key.pem", "tls_cert.pem", "tls_key.pem"]
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    for name in os.listdir(d):
        assert stat.S_IMODE((d / name).stat().st_mode) == 0o600
    kinds = [c[0] for c in fake_bin.calls("openssl")]
    assert kinds == ["req", "genrsa", "x509"]
    req = fake_bin.calls("openssl")[0]
    assert "ec_paramgen_curve:prime256v1" in req and "-nodes" in req
    # A second run keeps the keys: the viewer has learnt this fingerprint
    r = subprocess.run(["sh", str(DESK / "plinius-desktop-keys")], capture_output=True,
                       text=True, env=env)
    assert r.stdout == "AB:CD:EF\n"
    assert [c[0] for c in fake_bin.calls("openssl")] == ["req", "genrsa", "x509", "x509"]


def test_keys_redo_an_empty_certificate(tmp_path, fake_bin):
    fake_bin.stub("openssl", OPENSSL)
    d = tmp_path / "state" / "plinius-desktop"
    d.mkdir(parents=True)
    for name in ("tls_key.pem", "rsa_key.pem"):
        (d / name).write_text("old\n")
    (d / "tls_cert.pem").write_text("")
    subprocess.run(["sh", str(DESK / "plinius-desktop-keys")], check=True, capture_output=True,
                   env=fake_bin.env(isolated=True, HOME=tmp_path, XDG_STATE_HOME=tmp_path / "state"))
    assert [c[0] for c in fake_bin.calls("openssl")] == ["req", "x509"]
    assert (d / "tls_cert.pem").read_text() == "pem\n"
    assert (d / "rsa_key.pem").read_text() == "old\n"
