# plinius-dnsfilter as a running daemon (c/dsec_dnsfilter_live_harness.c): questions from the
# phone over UDP and TCP, the answers it gets from the servers (a plain DNS server of the test on
# 127.0.0.1), what it blocks, caches, retries and refuses. The daemon's real main() and sockets;
# only the buses, the timers' waiting, the app lookup and interface names are the harness's.

import socket
import struct

import pytest

from dnsf_live_helpers import (FakeDns, FakeDot, FakeHttps, Msg, Zone, dns, live,  # noqa: F401
                               log_entries, nm_conf, nm_link, query, tls_files, unpack,
                               write_settings, zone)

def filtering(live, servers, **more):
    write_settings(live, enabled=True, custom_servers=True, servers=servers, **more)


def get_log(res_line):
    return log_entries(res_line)


LOG_ALL = "call :1.5 GetLog (uint32 0, uint32 100, '', uint32 0)"


# ---- Answers from the own server ----

def test_a_question_goes_to_the_server_and_back(live, dns, zone):
    filtering(live, [dns.addr])
    r = live.run(f"uq {query('A.Test', qid=0x1234)}", LOG_ALL)
    m = Msg(r[0])
    assert (m.id, m.rcode, m.addrs) == (0x1234, 0, ["192.0.2.1"])
    proto, name, qtype, qid, edns = zone.seen[-1]
    # Our own ID towards the server, EDNS with our buffer size
    assert (proto, name, qtype) == ("udp", "a.test", "A")
    assert edns[3:5] == struct.pack(">H", 1232)
    e, = get_log(r[1])
    assert (e["name"], e["kind"], e["status"], e["answer"], e["upstream"], e["cached"]) == \
        ("a.test", "phone", 0, "192.0.2.1", dns.addr, False)


def test_answers_come_from_the_cache_until_their_ttl_ends(live, dns, zone):
    zone.names["c.test"] = {"a": ["192.0.2.9"], "ttl": 300}
    filtering(live, [dns.addr])
    q = query("c.test")
    r = live.run(f"uq {q}", f"uq {q}", "clock 100", f"uq {q}", "show cache", "clock 250", f"uq {q}",
                 f"uq {query('c.test', edns=1232, do=True)}", LOG_ALL)
    assert [a[1] for a in Msg(r[1]).answers] == [300]
    # Stored 100 s ago: the TTL says so
    assert [a[1] for a in Msg(r[3]).answers] == [200]
    assert r[4] == "1"
    assert zone.count("c.test") == 3   # first, after expiry, and with the DO bit (its own entry)
    cached = [e["cached"] for e in reversed(get_log(r[8]))]
    assert cached == [False, True, True, False, False]


def test_negative_answers_are_kept_five_minutes_at_most(live, dns, zone):
    zone.names["gone.test"] = {"rcode": 3, "soa": 3600}
    filtering(live, [dns.addr])
    q = query("gone.test")
    r = live.run(f"uq {q}", f"uq {q}", "clock 301", f"uq {q}")
    assert Msg(r[0]).rcode == 3
    assert zone.count("gone.test") == 2


def test_answers_without_ttl_or_with_errors_are_not_cached(live, dns, zone):
    zone.names["fail.test"] = {"rcode": 2}
    zone.names["empty.test"] = {"a": [], "ttl": 0}
    filtering(live, [dns.addr])
    r = live.run(*[f"uq {query(n)}" for n in ("fail.test", "fail.test", "empty.test", "empty.test")],
                 "show cache")
    # The last (only) server's SERVFAIL stands
    assert [Msg(x).rcode for x in r[:4]] == [2, 2, 0, 0]
    assert zone.count("fail.test") == 2 and zone.count("empty.test") == 2
    assert r[4] == "0"


def test_a_server_failure_moves_on_to_the_next_server(live, dns, zone):
    zone.names["x.test"] = {"rcode": 2}
    other_zone = Zone({"x.test": {"a": ["192.0.2.77"]}})
    other = FakeDns(other_zone)
    try:
        filtering(live, [dns.addr, other.addr])
        r = live.run(f"uq {query('x.test')}", "show upstreams")
        assert Msg(r[0]).addrs == ["192.0.2.77"]
        assert r[1] == f"{dns.addr}=1,{other.addr}=0"
    finally:
        other.close()


@pytest.mark.parametrize("spec", [{"mismatch": True}, {"garbage": True}])
def test_what_is_not_an_answer_to_the_question_counts_as_no_answer(live, dns, zone, spec):
    zone.names["x.test"] = dict(spec, a=["192.0.2.3"])
    other = FakeDns(Zone({"x.test": {"a": ["192.0.2.78"]}}))
    try:
        filtering(live, [dns.addr, other.addr])
        r = live.run(f"uq {query('x.test')}")
        assert Msg(r[0]).addrs == ["192.0.2.78"]
    finally:
        other.close()
    # Alone: SERVFAIL, and the log says it failed
    filtering(live, [dns.addr])
    r = live.run(f"uq {query('x.test')}", LOG_ALL)
    assert Msg(r[0]).rcode == 2
    assert get_log(r[1])[0]["status"] == 7   # FAILED


def test_a_late_answer_with_another_id_is_ignored(live, dns, zone):
    zone.names["late.test"] = {"a": ["192.0.2.4"], "wrongid": True}
    filtering(live, [dns.addr])
    r = live.run(f"uq {query('late.test')}")
    assert Msg(r[0]).addrs == ["192.0.2.4"]


def test_a_server_that_is_not_there_gives_way_to_the_next(live, dns):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        closed = f"127.0.0.1:{s.getsockname()[1]}"
    filtering(live, [closed, dns.addr])
    r = live.run(f"uq {query('a.test')}", "show upstreams")
    assert Msg(r[0]).addrs == ["192.0.2.1"]
    assert r[1] == f"{closed}=1,{dns.addr}=0"


def test_the_plain_servers_dnssec_bit_is_not_passed_on(live, dns, zone):
    # Anyone between us and a plain server could set AD; glibc trusts ours (trust-ad)
    zone.names["signed.test"] = {"a": ["192.0.2.5"], "ad": True}
    filtering(live, [dns.addr])
    r = live.run(f"uq {query('signed.test')}", f"uq {query('signed.test')}")
    assert not Msg(r[0]).ad and not Msg(r[1]).ad


def test_a_truncated_answer_is_asked_again_over_tcp(live, dns, zone):
    zone.names["big.test"] = {"a": ["192.0.2.6"], "tc": True}
    filtering(live, [dns.addr])
    r = live.run(f"uq {query('big.test')}", "fire on_stream_idle", f"uq {query('big.test', qtype='AAAA')}")
    assert Msg(r[0]).addrs == ["192.0.2.6"]
    assert [p for p, n, *_ in zone.seen if n == "big.test"] == ["udp", "tcp", "udp", "tcp"]
    # The TCP connection to the server is closed when idle (2 s for plain servers)
    assert r[1] == "fired 1"


def test_silent_servers_time_out_then_the_question_fails(live, dns, zone):
    zone.names["slow.test"] = {"drop": True}
    other = FakeDns(Zone({"slow.test": {"a": ["192.0.2.79"]}}))
    try:
        filtering(live, [dns.addr, other.addr])
        r = live.run("timers on_attempt_timeout", f"ukeep 1 {query('slow.test')}", "run 100",
                     "timers on_attempt_timeout", "fire on_attempt_timeout", "urecv 1",
                     f"ukeep 2 {query('slow.test', qtype='AAAA')}", "run 100", "timers on_deadline",
                     "fire on_deadline", "urecv 2", LOG_ALL, "show requests")
    finally:
        other.close()
    assert r[0] == "timers -"
    assert r[3] == "timers 1500"
    assert Msg(r[5]).addrs == ["192.0.2.79"]
    # The whole question has 8 s: then SERVFAIL, whoever is still being asked
    assert r[8] == "timers 8000"
    assert Msg(r[10]).rcode == 2
    assert [e["status"] for e in get_log(r[11])] == [7, 0]
    assert r[12] == "0 0"


def test_without_any_server_the_answer_is_servfail(live):
    write_settings(live, enabled=True)
    r = live.run(f"uq {query('a.test')}", "show status", LOG_ALL, "nm -", "nmchanged")
    assert Msg(r[0]).rcode == 2
    assert r[1] == "no-network"
    assert get_log(r[2])[0]["status"] == 7


# ---- Blocking ----

@pytest.mark.parametrize("mode,qtype,rcode,answers", [
    ("null-ip", "A", 0, [("A", 10, "0.0.0.0")]),
    ("null-ip", "AAAA", 0, [("AAAA", 10, "::")]),
    ("null-ip", "MX", 0, []),
    ("nxdomain", "A", 3, []),
    ("refused", "A", 5, []),
])
def test_blocked_names_get_the_block_answer_and_never_reach_a_server(live, dns, zone, mode, qtype,
                                                                     rcode, answers):
    zone.names["ads.test"] = {"a": ["192.0.2.66"]}
    filtering(live, [dns.addr], block_mode=mode)
    r = live.run("call :1.5 AddRule ('ads.test',)", f"uq {query('x.ads.test', qtype)}", LOG_ALL)
    m = Msg(r[1])
    assert (m.rcode, m.answers) == (rcode, answers)
    assert zone.count("x.ads.test") == 0
    e = get_log(r[2])[0]
    assert (e["status"], e["reason"], e["rcode"]) == (5, "||ads.test^", rcode)
    assert e["answer"] == ({"A": "0.0.0.0", "AAAA": "::"}.get(qtype, "") if mode == "null-ip" else "")


def test_firefox_canary_gets_nxdomain_whatever_the_mode(live, dns, zone):
    filtering(live, [dns.addr], block_mode="refused")
    r = live.run(f"uq {query('use-application-dns.net')}")
    assert Msg(r[0]).rcode == 3
    assert zone.count("use-application-dns.net") == 0


def test_a_tracker_behind_a_cname_is_blocked(live, dns, zone):
    zone.names["shop.test"] = {"cname": "shop.tracker.test", "a": ["192.0.2.10"]}
    zone.names["ok.test"] = {"cname": "shop.tracker.test", "a": ["192.0.2.11"]}
    zone.names["cdn.test"] = {"cname": "edge.cdn.example", "a": ["192.0.2.12"]}
    filtering(live, [dns.addr])
    r = live.run("call :1.5 AddRule ('tracker.test',)", "call :1.5 AddRule ('@@ok.test',)",
                 f"uq {query('shop.test')}", f"uq {query('ok.test')}", LOG_ALL, f"uq {query('cdn.test')}")
    assert Msg(r[2]).answers == [("A", 10, "0.0.0.0")]
    # An exception for the name itself: its CNAMEs are not checked
    assert Msg(r[3]).addrs == ["192.0.2.11"]
    # A CNAME no rule blocks: the answer as it came
    assert Msg(r[5]).answers == [("CNAME", 300, "edge.cdn.example"), ("A", 300, "192.0.2.12")]
    ok, shop = get_log(r[4])
    assert (shop["status"], shop["via"], shop["reason"]) == (5, "shop.tracker.test", "||tracker.test^")
    assert ok["status"] == 1


def test_rules_of_one_app_hold_for_its_questions_only(live, dns, zone):
    zone.names["ads.test"] = {"a": ["192.0.2.66"]}
    filtering(live, [dns.addr])
    r = live.run("call :1.5 AddRule ('||ads.test^$app=app/firefox',)", "app app/firefox",
                 f"uq {query('ads.test')}", "app app/maps", f"uq {query('ads.test')}", LOG_ALL)
    assert Msg(r[2]).addrs == ["0.0.0.0"]
    assert Msg(r[4]).addrs == ["192.0.2.66"]
    maps, firefox = get_log(r[5])
    assert (firefox["app"], firefox["status"], maps["app"], maps["status"]) == \
        ("app/firefox", 5, "app/maps", 0)


def test_a_pause_lets_everything_through_until_it_ends(live, dns, zone):
    zone.names["ads.test"] = {"a": ["192.0.2.66"]}
    filtering(live, [dns.addr])
    r = live.run("call :1.5 AddRule ('ads.test',)", "call :1.5 Pause (uint32 60,)",
                 "show status", "timers on_pause_over", f"uq {query('ads.test')}", "signals",
                 "fire on_pause_over", "show status", f"uq {query('ads.test')}", "get :1.5 PausedUntil",
                 LOG_ALL)
    assert r[2] == "paused"
    assert r[3] == "timers 60000"
    assert Msg(r[4]).addrs == ["192.0.2.66"]
    assert "*>PausedUntil" in r[5]
    assert r[7] == "active"
    assert Msg(r[8]).addrs == ["0.0.0.0"]
    assert r[9] == "ok int64 0"
    blocked, unfiltered = get_log(r[10])
    assert (unfiltered["status"], blocked["status"]) == (6, 5)


# ---- Questions that are not questions ----

def header(qid=0x1111, flags=0x0100, qd=1, an=0, ns=0, ar=0):
    return struct.pack(">HHHHHH", qid, flags, qd, an, ns, ar)


@pytest.mark.parametrize("msg,reply", [
    (b"\x12", None),                                              # shorter than a header
    (header(flags=0x8180) + bytes.fromhex(query("a.test"))[12:], None),  # an answer, not a question
    (header(qd=2) + bytes.fromhex(query("a.test"))[12:] * 2, 1),        # FORMERR
    (header(flags=0x2100) + bytes.fromhex(query("a.test"))[12:], 4),    # opcode UPDATE: NOTIMP
])
def test_messages_that_are_not_one_question(live, dns, msg, reply):
    filtering(live, [dns.addr])
    r = live.run("wait 300", f"uq {msg.hex()}", "show requests")
    if reply is None:
        assert r[1] == "noreply"
    else:
        m = Msg(r[1])
        # Only the header comes back, with the client's ID and RD
        assert (m.id, m.rcode, len(m.raw), m.flags & 0x8100) == (0x1111, reply, 12, 0x8100)
    assert r[2] == "0 0"


def test_long_answers_are_truncated_for_clients_without_edns(live, dns, zone):
    zone.names["txt.test"] = {"txt": 1500}
    filtering(live, [dns.addr])
    r = live.run(f"uq {query('txt.test', 'TXT')}", f"uq {query('txt.test', 'TXT', edns=4096)}",
                 "topen 1", f"tsend 1 {query('txt.test', 'TXT')}", "trecv 1")
    small = Msg(r[0])
    assert small.tc and small.answers == [] and len(small.raw) <= 512
    big = Msg(r[1])
    assert not big.tc and len(big.answers) == 1
    assert len(Msg(r[4]).answers) == 1


def test_the_v6_listener_answers_too(live, dns):
    filtering(live, [dns.addr])
    r = live.run(f"uq6 {query('a.test', 'AAAA')}", "topen 1 6", f"tsend 1 {query('a.test')}", "trecv 1")
    assert Msg(r[0]).addrs == ["2001:db8::1"]
    assert Msg(r[3]).addrs == ["192.0.2.1"]


# ---- Limits: one app can't take all places ----

def test_one_app_gets_servfail_beyond_its_share_of_questions(live, dns, zone):
    zone.names["hang.test"] = {"drop": True}
    filtering(live, [dns.addr])
    cmds = ["app app/flood"] + [f"usend {query(f'r{i}.hang.test')}" for i in range(256)]
    cmds += ["run 200", "show requests", "wait 3000", f"uq {query('a.test')}",
             "app app/other", f"uq {query('a.test', qtype='AAAA')}"]
    zone.names.update({f"r{i}.hang.test": {"drop": True} for i in range(256)})
    r = live.run(*cmds)
    assert r[-5] == "256 1"
    # The flooding app's next question fails at once, another app is still served
    assert Msg(r[-3]).rcode == 2
    assert Msg(r[-1]).addrs == ["2001:db8::1"]


def test_all_apps_together_get_servfail_beyond_the_limit(live, dns, zone):
    filtering(live, [dns.addr])
    cmds = []
    for a in range(4):
        cmds.append(f"app app/a{a}")
        for i in range(256):
            name = f"r{i}.a{a}.hang.test"
            zone.names[name] = {"drop": True}
            cmds.append(f"usend {query(name)}")
    cmds += ["run 300", "show requests", "app app/fifth", "wait 3000", f"uq {query('a.test')}",
             "fire on_deadline", "show requests", f"uq {query('a.test')}"]
    r = live.run(*cmds, timeout=180)
    assert r[-7] == "1024 4"
    assert Msg(r[-4]).rcode == 2
    assert r[-2] == "0 0"
    assert Msg(r[-1]).addrs == ["192.0.2.1"]


# ---- TCP clients ----

def test_tcp_questions_are_answered_in_turn(live, dns):
    filtering(live, [dns.addr])
    r = live.run("topen 1", f"tsend 1 {query('a.test', qid=1)}", f"tsend 1 {query('b.test', qid=2)}",
                 "trecv 1", "trecv 1", "show tcp", "tclose 1", "run 50", "show tcp")
    assert {(Msg(r[3]).id, *Msg(r[3]).addrs), (Msg(r[4]).id, *Msg(r[4]).addrs)} == \
        {(1, "192.0.2.1"), (2, "192.0.2.2")}
    assert r[5] == "1 0 0"
    assert r[8] == "0 0 0"


def test_tcp_clients_with_broken_framing_are_closed(live, dns):
    filtering(live, [dns.addr])
    r = live.run("topen 1", "traw 1 000501020304ff", "trecv 1",
                 # less than the length says, then the client gives up
                 "topen 2", "traw 2 0040" + "00" * 10, "tclose 2", "run 50", "show tcp")
    assert r[2] == "eof"   # a length below a DNS header
    assert r[7] == "0 0 0"


def test_a_tcp_client_that_sends_an_answer_is_cut_off(live, dns):
    # Nothing to answer: over TCP that ends the connection, it can't hold its place
    filtering(live, [dns.addr])
    msg = header(flags=0x8180) + bytes.fromhex(query("a.test"))[12:]
    garbage = header(qd=2) + bytes.fromhex(query("a.test"))[12:] * 2
    r = live.run("topen 1", f"tsend 1 {garbage.hex()}", "trecv 1", f"tsend 1 {msg.hex()}", "trecv 1")
    assert Msg(r[2]).rcode == 1
    assert r[4] == "eof"


def test_quiet_and_old_tcp_connections_are_closed(live, dns, zone):
    zone.names["hang.test"] = {"drop": True}
    filtering(live, [dns.addr])
    r = live.run("topen 1", "topen 2", "timers on_tcp_idle", "timers on_tcp_lifetime",
                 f"tsend 2 {query('hang.test')}", "run 50", "fire on_tcp_idle", "wait 300", "trecv 1",
                 "show tcp", "fire on_tcp_lifetime", "trecv 2", "show tcp")
    assert r[2] == "timers 10000,10000"
    assert r[3] == "timers 120000,120000"
    # The one with a question in flight waits for it
    assert r[8] == "eof"
    assert r[9] == "1 0 0"
    assert r[11] == "eof"
    assert r[12] == "0 0 0"


def test_the_oldest_tcp_connection_makes_room(live, dns):
    filtering(live, [dns.addr])
    cmds = [f"topen {i}" for i in range(65)] + ["run 100", "show tcp", "wait 300", "trecv 0",
                                                 f"tsend 64 {query('a.test')}", "trecv 64"]
    r = live.run(*cmds)
    assert r[66] == "64 0 0"
    assert r[68] == "eof"
    assert Msg(r[70]).addrs == ["192.0.2.1"]


def test_an_answer_for_a_client_that_left_is_dropped(live, dns, zone):
    zone.names["hang.test"] = {"drop": True}
    filtering(live, [dns.addr])
    r = live.run("topen 1", f"tsend 1 {query('a.test')}", "tclose 1", "run 200", "topen 2",
                 f"tsend 2 {query('hang.test')}", "run 50", "tclose 2", "run 50", "fire on_deadline",
                 "show tcp", "show requests")
    assert r[-2] == "0 0 0"
    assert r[-1] == "0 0"


def test_a_tcp_client_that_reads_no_answers_is_cut_off(live, dns, zone):
    # Without reading its answers a client could fill our memory: once the kernel's buffers are
    # full (4 MiB here), it is closed beyond 256 KiB queued
    zone.names.update({f"t{i}.test": {"txt": 60000} for i in range(160)})
    filtering(live, [dns.addr])
    cmds = ["topen 1", "until tcp1 10000"] + [f"tsend 1 {query(f't{i}.test', 'TXT', qid=i)}" for i in range(160)]
    cmds += ["until tcp0 60000", "show tcp", "call :1.5 GetLog (uint32 0, uint32 2000, '', uint32 0)"]
    r = live.run(*cmds, timeout=120)
    assert r[1] == "ok"
    assert r[-2] == "0 0 0"
    # Cut off with answers still to come: not all 160 questions were read
    assert len(get_log(r[-1])) < 160


# ---- Hotspot and USB clients (the shared port, redirected there by the firewall) ----

LO = socket.if_nametoindex("lo")


@pytest.mark.parametrize("iface,kind", [("ap0", "hotspot"), ("usb0", "usb")])
def test_hotspot_and_usb_clients_are_answered_and_named(live, dns, iface, kind):
    filtering(live, [dns.addr])
    (live.root / "nmlib" / "dnsmasq-ap0.leases").write_text(
        "1700000000 aa:bb:cc:dd:ee:ff 10.42.0.99 other *\n1700000000 aa:bb:cc:dd:ee:00 127.0.0.1 laptop *\n")
    (live.root / "nmlib" / "dnsmasq-other.conf").write_text("not a lease file\n")
    (live.root / "nmlib" / "dnsmasq-unreadable.leases").mkdir()
    r = live.run(f"ifname {LO} {iface}", f"uqs {query('a.test')}", f"uqs6 {query('b.test')}", LOG_ALL,
                 system=True)
    assert Msg(r[1]).addrs == ["192.0.2.1"]
    assert Msg(r[2]).addrs == ["192.0.2.2"]
    six, four = get_log(r[3])
    assert (four["kind"], six["kind"]) == (kind, kind)
    # Hotspot clients by the name their lease has
    assert four["client"] == ("laptop (127.0.0.1)" if kind == "hotspot" else "127.0.0.1")
    assert six["client"] == "::1"


@pytest.mark.parametrize("pre", [["ifname 1 wlan0"], [], ["setraw :1.5 SharedClients false"]])
def test_the_shared_port_answers_nobody_else(live, dns, pre):
    # Wi-Fi neighbours (any other interface), an unknown interface, or shared clients switched off
    filtering(live, [dns.addr])
    pre = [p.replace("ifname 1", f"ifname {LO}") for p in pre]
    if pre and pre[0].startswith("setraw"):
        pre = [f"ifname {LO} ap0"] + pre
    r = live.run(*pre, "wait 300", f"uqs {query('a.test')}", "topen 1 shared", "trecv 1", "show tcp",
                 system=True, path_env="")
    assert r[-4] == "noreply"
    assert r[-2] == "eof"
    assert r[-1] == "0 0 0"


def test_tcp_clients_of_the_hotspot_have_places_of_their_own(live, dns):
    filtering(live, [dns.addr])
    cmds = [f"ifname {LO} ap0"]
    # Eight per device; the ninth of a device is turned away at once
    cmds += [f"topen {i} shared 127.0.0.2" for i in range(9)] + ["run 50", "show tcp", "wait 300", "trecv 8"]
    # 32 for all devices: the 33rd ends the oldest
    for d in range(3, 6):
        cmds += [f"topen {d * 10 + i} shared 127.0.0.{d}" for i in range(8)]
    cmds += ["run 50", "show tcp", "topen 99 shared 127.0.0.9", "run 50", "trecv 0", "show tcp",
             f"tsend 99 {query('a.test')}", "wait 10000", "trecv 99", "topen 98 shared6",
             f"tsend 98 {query('b.test')}", "trecv 98", "topen 100", "show tcp", "tclose 99", "show tcp"]
    r = live.run(*cmds, system=True, path_env="")
    # The last ones: the 33rd device, ::1 (which ends the next oldest), and the phone's own
    # ... and the device with its only connection closed is forgotten
    assert r.of("show tcp") == ["0 8 1", "0 32 4", "0 32 5", "1 32 6", "1 31 5"]
    assert r[13] == "eof"
    assert r.of("trecv")[1] == "eof"
    assert Msg(r.of("trecv")[2]).addrs == ["192.0.2.1"]
    assert Msg(r.of("trecv")[3]).addrs == ["192.0.2.2"]


# ---- Servers of the network (NetworkManager) and the own ones ----

def test_names_of_a_network_go_to_its_servers_the_rest_to_the_own(live, zone):
    corp = FakeDns(Zone({"intra.corp.example": {"a": ["10.1.1.1"]}, "router.lan": {"a": ["10.2.2.2"]},
                         "a.test": {"a": ["10.9.9.9"]}}))
    wifi = FakeDns(Zone({"router.lan": {"a": ["192.168.1.1"]}, "a.test": {"a": ["192.168.9.9"]}}))
    own = FakeDns(Zone({"a.test": {"a": ["192.0.2.1"]}, "x.corp.example": {"a": ["9.9.9.9"]}}))
    try:
        filtering(live, [own.addr])
        live.env["LIVE_NM"] = nm_conf(
            nm_link("wlan0", [wifi.addr, "127.0.0.1", "::1", "not-an-address"], 100,
                    domains=["lan", "~lan", "com"]),
            nm_link("eth0", ["192.0.2.254"], 150),
            nm_link("tun0", [corp.addr], 50, vpn=True, domains=["~corp.example", "~."]),
            nm_link("rmnet0", [], 200, domains=["mobile.example"]))
        r = live.run(*[f"uq {query(n)}" for n in ("intra.corp.example", "router.lan", "a.test", "host")],
                     "get :1.5 NetworkServers", "set :1.5 LocalViaNetwork false", f"uq {query('router.lan', 'AAAA')}",
                     "conn 2", "nmchanged", f"uq {query('a.test', 'AAAA')}", "show connectivity")
        assert Msg(r[0]).addrs == ["10.1.1.1"]
        # The search domain of the Wi-Fi; "com" is a public suffix and counts for nothing
        assert Msg(r[1]).addrs == ["192.168.1.1"]
        assert Msg(r[2]).addrs == ["192.0.2.1"]
        # A single label is local: the network's servers, VPN first
        assert corp.zone.count("host") == 1
        # Ourselves (loopback, any) and broken entries are no server
        assert unpack(r[4]) == [f"{corp.addr} (tun0)", f"{wifi.addr} (wlan0)", "192.0.2.254 (eth0)"]
        # Local names through the own servers when the user says so
        assert own.zone.count("router.lan") == 1
        # At a login page only the network's servers reach it
        assert wifi.zone.count("a.test") + corp.zone.count("a.test") == 1
        assert r[10] == "2 4"
    finally:
        for s in (corp, wifi, own):
            s.close()


def test_the_portal_rule_can_be_switched_off_for_testing(live, dns):
    other = FakeDns(Zone({"a.test": {"a": ["10.0.0.1"]}}))
    try:
        filtering(live, [dns.addr])
        live.env.update(LIVE_NM=nm_conf(nm_link("wlan0", [other.addr])), LIVE_CONNECTIVITY=2)
        r = live.run(f"uq {query('a.test')}", args=live.args(extra=["--ignore-portal"]))
        assert Msg(r[0]).addrs == ["192.0.2.1"]
    finally:
        other.close()


def test_servers_that_keep_failing_are_asked_last(live, dns, zone):
    zone.names.update({f"f{i}.test": {"rcode": 5} for i in range(4)})
    good = FakeDns(Zone({f"f{i}.test": {"a": ["192.0.2.50"]} for i in range(4)}))
    try:
        filtering(live, [dns.addr, good.addr])
        r = live.run(*[f"uq {query(f'f{i}.test')}" for i in range(4)], "clock 61", f"uq {query('a.test')}")
        assert all(Msg(x).addrs == ["192.0.2.50"] for x in r[:4])
        # Three failures in a row: the fourth question goes to the good server first
        assert [zone.count(f"f{i}.test") for i in range(4)] == [1, 1, 1, 0]
        # A minute later it gets its chance again
        assert zone.count("a.test") == 1
    finally:
        good.close()


# ---- Encrypted servers: DNS over TLS and HTTPS ----

@pytest.fixture
def tls(tmp_path_factory):
    return tls_files(tmp_path_factory)


def encrypted(live, dns, servers, tls):
    filtering(live, servers)
    live.env.update(LIVE_NM=nm_conf(nm_link("wlan0", [dns.addr])), LIVE_CA=tls[0])


def test_dns_over_tls_keeps_its_connection(live, dns, zone, tls):
    dot = FakeDot(Zone({"a.test": {"a": ["192.0.2.31"], "ad": True}, "b.test": {"a": ["192.0.2.32"]}}),
                  tls[1], tls[2])
    try:
        encrypted(live, dns, [f"tls://dot.test:{dot.port}"], tls)
        r = live.run(f"uq {query('a.test')}", f"uq {query('b.test')}", "timers on_stream_idle",
                     "fire on_stream_idle", f"uq {query('a.test', 'AAAA')}", LOG_ALL)
        m = Msg(r[0])
        # An authenticated server's DNSSEC bit stays
        assert (m.addrs, m.ad) == (["192.0.2.31"], True)
        assert Msg(r[1]).addrs == ["192.0.2.32"]
        assert r[2] == "timers 20000"
        assert dot.connections == 2
        # Its address came from the network's servers (A and AAAA), never from ourselves
        assert zone.count("dot.test") == 4
        assert get_log(r[5])[-1]["upstream"] == f"tls://dot.test:{dot.port}"
    finally:
        dot.close()


@pytest.mark.parametrize("mode", ["close", "badlen", "half", "silent"])
def test_a_broken_tls_server_gives_way_to_the_next(live, dns, tls, mode):
    dot = FakeDot(Zone({"a.test": {"a": ["192.0.2.31"]}, "b.test": {"a": ["192.0.2.2"]}}), tls[1], tls[2],
                  mode=mode)
    try:
        encrypted(live, dns, [f"tls://dot.test:{dot.port}", dns.addr], tls)
        cmds = [f"ukeep 1 {query('a.test')}", "run 300"]
        if mode == "silent":
            cmds.append("fire on_attempt_timeout")
        cmds += ["urecv 1", f"uq {query('b.test')}", "show upstreams"]
        r = live.run(*cmds)
        first = Msg(r.of("urecv")[0])
        assert first.addrs == (["192.0.2.31"] if mode == "close" else ["192.0.2.1"])
        assert Msg(r.of("uq")[0]).addrs == ["192.0.2.2"]
        # Closed after its answer: the next question opens a new connection
        if mode == "close":
            assert dot.connections == 2
    finally:
        dot.close()


@pytest.mark.parametrize("name", ["wrong.test", "missing.test"])
def test_a_tls_server_without_a_valid_certificate_or_address_is_not_used(live, dns, tls, name):
    dot = FakeDot(Zone({"a.test": {"a": ["192.0.2.31"]}}), tls[1], tls[2])
    try:
        encrypted(live, dns, [f"tls://{name}:{dot.port}", dns.addr], tls)
        r = live.run(f"uq {query('a.test')}", "show upstreams")
        assert Msg(r[0]).addrs == ["192.0.2.1"]
        assert r[1].startswith(f"tls://{name}:{dot.port}=1,")
    finally:
        dot.close()


def test_dns_over_https(live, dns, tls):
    https = FakeHttps(Zone({"a.test": {"a": ["192.0.2.41"]}, "b.test": {"drop": True}}), tls[1], tls[2])
    try:
        base = f"https://doh.test:{https.port}"
        encrypted(live, dns, [f"{base}/dns-query", f"{base}/fail", f"{base}/long", dns.addr], tls)
        r = live.run(f"uq {query('a.test', qid=7)}", f"uq {query('b.test')}", "show upstreams")
        m = Msg(r[0])
        assert (m.id, m.addrs) == (7, ["192.0.2.41"])
        method, path, headers, body = https.requests[0]
        # RFC 8484: ID 0, the DNS message as the body
        assert (method, path, headers["Accept"], headers["Content-Type"], body[:2]) == \
            ("POST", "/dns-query", "application/dns-message", "application/dns-message", b"\0\0")
        assert headers["User-Agent"] == "Plinius Arch Linux"
        # An empty answer, a 500 and an answer longer than DNS allows: on to the plain server
        assert Msg(r[1]).addrs == ["192.0.2.2"]
        assert r[2] == f"{base}/dns-query=1,{base}/fail=1,{base}/long=1,{dns.addr}=0,{dns.addr}=0"
    finally:
        https.close()


def test_a_silent_https_server_times_out(live, dns, tls):
    https = FakeHttps(Zone({}), tls[1], tls[2])
    https.httpd.RequestHandlerClass.do_POST = lambda self: __import__("time").sleep(3)
    try:
        encrypted(live, dns, [f"https://doh.test:{https.port}/dns-query", dns.addr], tls)
        r = live.run(f"ukeep 1 {query('a.test')}", "run 300", "fire on_attempt_timeout", "urecv 1",
                     "run 3500")
        assert Msg(r[3]).addrs == ["192.0.2.1"]
    finally:
        https.close()


# ---- Lookups of the daemon itself (bootstrap of encrypted servers and lists) ----

def test_own_lookups_use_the_network_servers(live, dns, zone):
    zone.names["dual.test"] = {"a": ["192.0.2.60"], "aaaa": ["2001:db8::60"]}
    write_settings(live, enabled=True)
    live.env["LIVE_NM"] = nm_conf(nm_link("wlan0", [dns.addr]))
    r = live.run("resolve Dual.Test", "resolve dual.test 4", "resolve dual.test 6", "resolve nothing.test",
                 "resolve bad..name", "resolvesync dual.test", "reverse 192.0.2.60", LOG_ALL)
    assert r[0] == "addrs 192.0.2.60,2001:db8::60"
    assert r[1] == "addrs 192.0.2.60"
    assert r[2] == "addrs 2001:db8::60"
    assert r[3] == "error No address for nothing.test from the network's DNS"
    assert r[4].startswith("error ")
    assert r[5] == "error Only asynchronous lookups"
    assert r[6] == "error Not supported"
    # Not in the phone's log, not in the cache
    assert unpack(r[7]) == ([],)


def test_test_server(live, dns, zone):
    zone.names["example.com"] = {"a": ["192.0.2.70"]}
    write_settings(live)
    r = live.run(f"call :1.5 TestServer ('{dns.addr}',)", "await 10000",
                 "call :1.5 TestServer ('dns.invalid',)",
                 f"call :1.5 TestServer ('tls://dot.test:1',)", "fire on_attempt_timeout", "await 10000")
    ok, ms, text = unpack(r[1])
    assert (ok, text) == (True, "192.0.2.70")
    assert unpack(r[2])[0] is False and "need an address" in unpack(r[2])[2]
    assert unpack(r[5])[0] is False and unpack(r[5])[2] == "The server did not answer"
    zone.names["example.com"] = {"rcode": 3}
    r = live.run(f"call :1.5 TestServer ('{dns.addr}',)", "await 10000")
    assert unpack(r[1])[0] is False and unpack(r[1])[2] == "No answer"


def test_a_server_that_cannot_be_asked_fails_at_once(live, dns):
    # A broadcast address: connect() refuses it, nothing goes out
    filtering(live, ["255.255.255.255", dns.addr])
    r = live.run(f"uq {query('a.test')}", "show upstreams")
    assert Msg(r[0]).addrs == ["192.0.2.1"]
    assert r[1] == f"255.255.255.255=1,{dns.addr}=0"


def test_answers_that_are_not_kept(live, dns, zone, tls):
    # FORMERR is passed on but not stored; nor is an answer that is truncated even over TCP
    zone.names["formerr.test"] = {"rcode": 1}
    dot = FakeDot(Zone({"cut.test": {"a": ["192.0.2.1"], "tc_always": True}}), tls[1], tls[2])
    try:
        encrypted(live, dns, [dns.addr], tls)
        r = live.run(f"uq {query('formerr.test')}", f"uq {query('formerr.test')}",
                     f"set :1.5 Servers ['tls://dot.test:{dot.port}']", f"uq {query('cut.test')}",
                     f"uq {query('cut.test')}", "show cache")
        assert [Msg(x).rcode for x in r[:2]] == [1, 1]
        assert zone.count("formerr.test") == 2
        assert Msg(r[3]).tc
        assert dot.zone.count("cut.test") == 2
        assert r[5] == "0"
    finally:
        dot.close()


def test_two_questions_at_once_store_one_answer(live, dns, zone):
    zone.names["slow.test"] = {"a": ["192.0.2.88"], "delay": 0.3}
    filtering(live, [dns.addr])
    r = live.run(f"ukeep 1 {query('slow.test', qid=1)}", f"ukeep 2 {query('slow.test', qid=2)}", "urecv 1",
                 "urecv 2", "show cache", f"uq {query('slow.test', qid=3)}")
    assert {Msg(r[2]).id, Msg(r[3]).id} == {1, 2}
    assert r[4] == "1"
    assert zone.count("slow.test") == 2
    assert Msg(r[5]).addrs == ["192.0.2.88"]


def test_the_cache_forgets_the_least_used_beyond_4096_answers(live, dns, zone):
    zone.names.update({f"c{i}.test": {"a": ["192.0.2.1"]} for i in range(4097)})
    filtering(live, [dns.addr])
    cmds = [f"usend {query(f'c{i}.test')}" for i in range(4097)]
    r = live.run(*cmds, "until requests0 60000", "show cache", f"uq {query('c0.test')}",
                 f"uq {query('c4096.test')}", timeout=300)
    assert r[-3] == "4096"
    # The oldest went; the newest is still there
    assert zone.count("c0.test") == 2 and zone.count("c4096.test") == 1


def test_a_stream_ends_with_its_server(live, dns, tls):
    dot = FakeDot(Zone({"a.test": {"a": ["192.0.2.31"]}, "example.com": {"a": ["192.0.2.30"]}}), tls[1],
                  tls[2])
    hang = FakeDot(Zone({}), tls[1], tls[2], mode="nohandshake")
    stall = FakeDot(Zone({}), tls[1], tls[2], mode="stall")
    try:
        encrypted(live, dns, [f"tls://dot.test:{dot.port}"], tls)
        r = live.run(f"uq {query('a.test')}", "set :1.5 Servers ['9.9.9.9']",
                     # The server goes while its connection is being made: the late result is dropped
                     f"set :1.5 Servers ['tls://dot.test:{hang.port}']", f"ukeep 1 {query('a.test', 'AAAA')}",
                     "run 500", "fire on_attempt_timeout", "urecv 1", "set :1.5 Servers ['9.9.9.9']", "run 300",
                     # An answer cut off after its length, the question timed out, then the server goes
                     f"set :1.5 Servers ['tls://dot.test:{stall.port}']", f"ukeep 2 {query('b.test')}",
                     "run 500", "fire on_attempt_timeout", "urecv 2", "set :1.5 Servers ['9.9.9.9']",
                     "run 300", f"call :1.5 TestServer ('tls://dot.test:{dot.port}',)", "await 10000")
        assert Msg(r[0]).addrs == ["192.0.2.31"]
        assert Msg(r[6]).rcode == 2
        assert Msg(r[13]).rcode == 2
        assert unpack(r[17])[0] is True
    finally:
        for s in (dot, hang, stall):
            s.close()


def test_a_cut_off_https_answer_counts_as_none(live, dns, tls):
    https = FakeHttps(Zone({}), tls[1], tls[2])
    try:
        encrypted(live, dns, [f"https://doh.test:{https.port}/short", dns.addr], tls)
        r = live.run(f"uq {query('a.test')}", "show upstreams")
        assert Msg(r[0]).addrs == ["192.0.2.1"]
        assert r[1].startswith(f"https://doh.test:{https.port}/short=1,")
    finally:
        https.close()


def test_synchronous_lookups_are_refused(live, dns):
    write_settings(live)
    r = live.run("resolvesyncflags dual.test")
    assert r[0] == "error Only asynchronous lookups"
