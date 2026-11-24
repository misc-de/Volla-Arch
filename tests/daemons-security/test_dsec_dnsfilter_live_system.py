# plinius-dnsfilter as a running daemon (c/dsec_dnsfilter_live_harness.c) and the system around
# it: the resolv.conf link and stub file (in tmp_path), the firewall rules it hands to
# iptables-legacy (stand-ins that record them), NetworkManager's servers from the bus or its
# file, and what happens when its ports are taken.

import os
import socket

import pytest

from dnsf_live_helpers import (Msg, dns, file_mode, live, nm_conf, nm_link, query, unpack,  # noqa: F401
                               write_settings, zone)

IPT = r'''
case " $* " in
*" -D "*) f="$(dirname "$0")/deleted-$(echo "$0 $*" | tr ' /' '__')"; [ -e "$f" ] && exit 1; : > "$f"; exit 0 ;;
*" -C "*) exit 1 ;;
esac
exit 0
'''
RESTORE = 'cat >> "$0.input"; [ -z "$RESTORE_FAIL" ] || { echo "line 3 failed" >&2; exit 1; }'


@pytest.fixture
def firewall(fake_bin):
    for v in ("iptables-legacy", "ip6tables-legacy"):
        fake_bin.stub(v, IPT)
        fake_bin.stub(v + "-restore", RESTORE)
    return fake_bin


def restored(fake_bin, v6=False):
    f = fake_bin.path / (("ip6tables" if v6 else "iptables") + "-legacy-restore.input")
    return f.read_text() if f.exists() else ""


def ipt_calls(fake_bin, v6=False):
    return [c[1:] for c in fake_bin.calls("ip6tables-legacy" if v6 else "iptables-legacy")]


def hooks_inserted(fake_bin, v6=False):
    return [c for c in ipt_calls(fake_bin, v6) if "-I" in c]


def test_switching_on_points_the_phone_to_the_filter(live, dns, firewall):
    (live.root / "nm" / "resolv.conf").write_text("# NM\nsearch lan corp.example\nnameserver 192.168.1.1\n"
                                                  "domain lan\noptions rotate\n")
    write_settings(live, enabled=True, custom_servers=True, servers=[dns.addr])
    live.env["LIVE_USERS"] = "1000,65534,999,1001"
    r = live.run(f"uq {query('a.test')}", "show status", "quit", system=True, quits=True)
    assert Msg(r[0]).addrs == ["192.0.2.1"]
    assert r[1] == "active"
    stub = (live.link / "stub-resolv.conf").read_text()
    assert stub.splitlines()[2:] == ["search lan corp.example", "domain lan", "nameserver 127.0.0.1",
                                     "options edns0 trust-ad"]
    assert file_mode(live.link / "stub-resolv.conf") == 0o644
    for v6 in (False, True):
        rules = restored(firewall, v6)
        # The filter table first: the shared port is closed before anything is redirected to it
        assert rules.index("*filter") < rules.index("*nat")
        port = live.shared_port
        for line in (f"-A PLINIUS_DNS_IN -i lo -p udp --dport {port} -j DROP",
                     f"-A PLINIUS_DNS_IN -i ap+ -p tcp --dport {port} -j RETURN",
                     f"-A PLINIUS_DNS_PRE -i usb+ -p udp --dport 53 -j REDIRECT --to-ports {port}",
                     f"-A PLINIUS_DNS_OUT -m owner --uid-owner 1000 -p udp --dport 53 -j REDIRECT --to-ports {live.port}",
                     "-A PLINIUS_DNS_FWD -i ap+ -p tcp --dport 853 -j REJECT --reject-with tcp-reset",
                     "-A PLINIUS_DNS_OUT -m owner --uid-owner 1001 -p udp --dport 853 -j REJECT"):
            assert line + "\n" in rules, line
        # Android's and the system's users keep their DNS
        assert "--uid-owner 65534" not in rules and "--uid-owner 999 " not in rules
        assert [c[3] for c in hooks_inserted(firewall, v6)] == ["PREROUTING", "OUTPUT", "FORWARD", "OUTPUT",
                                                                 "INPUT"]
    # Leaving: NetworkManager's DNS again, our chains gone (each hook until it is no longer there)
    assert os.readlink(live.link / "resolv.conf") == str(live.nm_resolv)
    calls = ipt_calls(firewall)
    assert ["-t", "nat", "-F", "PLINIUS_DNS_PRE"] in calls and ["-t", "filter", "-X", "PLINIUS_DNS_IN"] in calls
    dels = [c for c in calls if "-D" in c]
    assert len(dels) == 10 and len({tuple(c) for c in dels}) == 5


def test_while_on_the_link_points_to_the_stub(live, dns, firewall):
    write_settings(live, enabled=True, custom_servers=True, servers=[dns.addr])
    r = live.run(f"readlink {live.link}/resolv.conf", "set :1.5 Enabled false",
                 f"readlink {live.link}/resolv.conf", system=True)
    assert r[0] == f"link {live.link}/stub-resolv.conf"
    assert r[2] == f"link {live.nm_resolv}"
    # Without NetworkManager's file: no search domains
    assert "search" not in (live.link / "stub-resolv.conf").read_text()


def test_settings_change_the_rules(live, dns, firewall):
    write_settings(live, enabled=True, custom_servers=True, servers=[dns.addr], shared_clients=False,
                   redirect_dns=False, block_encrypted_dns=False)
    live.env["LIVE_USERS"] = "1000"
    live.run("show status", system=True)
    rules = restored(firewall)
    assert "ap+" not in rules and "--uid-owner" not in rules and "853" not in rules
    # Still: the shared port only for hotspot and USB, never over lo
    assert f"-A PLINIUS_DNS_IN -p tcp --dport {live.shared_port} -j DROP" in rules
    before = rules.count("*filter")
    live.run("set :1.5 BlockEncryptedDns true", "set :1.5 RedirectDns true", "set :1.5 SharedClients true",
             "users 1000,1002", "userschanged", "userschanged", "timers on_users_changed_idle",
             "fire on_users_changed_idle", system=True)
    rules = restored(firewall)
    # Three settings and one burst of logins: four new rule sets, the last with the new user
    assert rules.count("*filter") == before + 1 + 4
    # Its DNS redirected and DNS over TLS refused, for UDP and TCP
    assert rules.rstrip().split("*filter")[-1].count("--uid-owner 1002") == 4


def test_users_changing_while_off_touch_nothing(live, firewall):
    live.run("userschanged", "fire on_users_changed_idle", system=True)
    assert restored(firewall) == ""


def test_rules_that_iptables_refuses_are_not_hooked_in(live, dns, firewall):
    write_settings(live, enabled=True, custom_servers=True, servers=[dns.addr])
    r = live.run("show status", system=True, env={"RESTORE_FAIL": "1"})
    assert r[0] == "active"
    assert hooks_inserted(firewall) == [] and hooks_inserted(firewall, True) == []
    assert "failed, rules unchanged: line 3 failed" in r.stderr


def test_without_iptables_the_filter_still_answers(live, dns, fake_bin):
    write_settings(live, enabled=True, custom_servers=True, servers=[dns.addr])
    r = live.run(f"uq {query('a.test')}", system=True)
    assert Msg(r[0]).addrs == ["192.0.2.1"]
    assert "Cannot run iptables-legacy-restore" in r.stderr
    assert "Cannot run iptables-legacy" in r.stderr


def test_cleanup_undoes_link_and_rules(live, firewall):
    os.symlink(live.link / "stub-resolv.conf", live.link / "resolv.conf")
    r = live.run(args=["--cleanup", "--link-dir", str(live.link), "--state-dir", str(live.state)])
    assert r.exit == 0
    assert os.readlink(live.link / "resolv.conf") == str(live.nm_resolv)
    assert len([c for c in ipt_calls(firewall) if "-X" in c]) == 5
    assert not live.state.exists()


def test_a_correct_link_is_left_alone_and_a_broken_place_is_reported(live, firewall):
    os.symlink(live.nm_resolv, live.link / "resolv.conf")
    ino = os.lstat(live.link / "resolv.conf").st_ino
    live.run("show status", system=True)
    assert os.lstat(live.link / "resolv.conf").st_ino == ino
    os.chmod(live.link, 0o500)
    try:
        write_settings(live, enabled=True)
        r = live.run("show status", system=True)
    finally:
        os.chmod(live.link, 0o700)
    assert "Cannot write" in r.stderr and "Cannot point" in r.stderr


def test_a_taken_port_is_an_error_and_the_phone_keeps_its_dns(live, firewall):
    write_settings(live, enabled=True)
    with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as s:
        s.bind(("::1", live.port))
        r = live.run("show status", "show error", "show listening", system=True)
    assert r[0] == "error"
    assert f"::1 port {live.port}" in r[1]
    assert r[2] == "0 0"
    assert os.readlink(live.link / "resolv.conf") == str(live.nm_resolv)
    assert hooks_inserted(firewall) == []
    assert any("-F" in c for c in ipt_calls(firewall))


@pytest.mark.parametrize("family,addr", [(socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")])
def test_tcp_port_taken(live, firewall, family, addr):
    write_settings(live, enabled=True)
    with socket.socket(family, socket.SOCK_STREAM) as s:
        s.bind((addr, live.port))
        s.listen(1)
        r = live.run("show status", system=True)
    assert r[0] == "error"


def test_a_taken_shared_port_leaves_the_phone_filtered(live, dns, firewall):
    write_settings(live, enabled=True, custom_servers=True, servers=[dns.addr])
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", live.shared_port))
        r = live.run("show status", f"uq {query('a.test')}", system=True)
    assert r[0] == "active"
    assert Msg(r[1]).addrs == ["192.0.2.1"]
    assert "Hotspot and USB clients:" in r.stderr


# ---- NetworkManager ----

def test_network_servers_from_the_bus(live, dns):
    live.env["LIVE_NM"] = nm_conf(nm_link("wlan0", ["192.168.1.1", "2001:db8::53"], 100),
                                  nm_link("tun0", ["10.8.0.1"], 50, vpn=True),
                                  nm_link("eth0", ["127.0.0.53"], 10))
    write_settings(live, enabled=True)
    r = live.run("get :1.5 NetworkServers", "show links", "show connectivity", "nmbad", "nmchanged",
                 "show links", "signals", "conn -2", "metered -1", "nmchanged", "show connectivity")
    # VPN first, then by priority; the link with only ourselves is dropped
    assert unpack(r[0]) == ["10.8.0.1 (tun0)", "192.168.1.1 (wlan0)", "2001:db8::53 (wlan0)"]
    assert r[1] == "2"
    assert r[2] == "4 4"
    # A reply of the wrong type: no servers from the bus (nor from the files, which are empty)
    assert r[5] == "0"
    assert "*>NetworkServers" in r[6] and "*>Status" in r[6]
    assert r[10] == "0 0"


def test_without_networkmanager_the_servers_come_from_its_file(live, dns):
    nm = live.root / "nm" / "resolv.conf"
    nm.write_text(f"nameserver 127.0.0.1\nnameserver ::1\nnameserver bogus\nnameserver {dns.addr}\n\n"
                  "nameserver\n")
    write_settings(live, enabled=True)
    # NetworkManager on the bus says nothing: its file
    r = live.run("show links", f"uq {query('a.test')}", f"writefile {nm} nameserver 127.0.0.1\\n",
                 "run 1500", "show links")
    assert r[0] == "1"
    assert Msg(r[1]).addrs == ["192.0.2.1"]
    # The file changed: only ourselves left, so nothing
    assert r[4] == "0"


def test_when_testing_etc_resolv_conf_is_the_last_resort(live, dns):
    (live.root / "etc-resolv.conf").write_text(f"nameserver {dns.addr}\n")
    write_settings(live, enabled=True)
    r = live.run("show links", f"uq {query('a.test')}")
    assert r[0] == "1"
    assert Msg(r[1]).addrs == ["192.0.2.1"]
    # Not on the phone: there /etc/resolv.conf is our own link
    r = live.run("show links", system=True, path_env="")
    assert r[0] == "0"


def test_a_change_of_networkmanagers_file_renews_the_stub(live, firewall):
    nm = live.root / "nm" / "resolv.conf"
    nm.write_text("search one.example\nnameserver 192.168.1.1\n")
    write_settings(live, enabled=True)
    r = live.run(f"writefile {nm} search two.example\\nnameserver 192.168.1.2\\n", "run 1500",
                 "show links", "nmchanged", system=True, sysbus=False)
    assert "search two.example" in (live.link / "stub-resolv.conf").read_text()
    # Without NetworkManager on the bus its file is what counts
    assert r[2] == "1"
