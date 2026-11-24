# plinius-dnsfilter daemon logic (plinius-dnsfilter.c through c/dsec_dnsfilter_daemon_harness.c):
# which rule decides for a name and an app, what the user may enter as a rule over D-Bus, the
# server specs, the network domains that may see names in plain text, and who may change the
# settings (against a scripted bus). Nothing is started.

import pytest

from conftest import PKG
from dsec_helpers import CDIR, build, run_harness

DNSFILTER = PKG / "plinius-dnsfilter"


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    return build(tmp_path_factory.mktemp("dnsfilter-daemon"), "dnsfilter-daemon-harness",
                 [CDIR / "dsec_dnsfilter_daemon_harness.c", DNSFILTER / "appid.c",
                  DNSFILTER / "dnsmsg.c", DNSFILTER / "filter.c"],
                 flags=["-Wno-unused-parameter", "-Wno-missing-field-initializers",
                        "-Wno-unused-variable", "-Wno-unused-but-set-variable"],
                 pkgs=["gio-unix-2.0", "libsoup-3.0"])


def run(harness, *cmds):
    out = run_harness(harness, "\n".join(cmds) + "\n")
    assert len(out) == len(cmds), out
    return out


def decide(harness, tmp_path, name, app=None, rules=(), lists=(), pre=()):
    cmds = list(pre)
    for r in rules:
        cmds.append("rule " + r)
    cmds.append("rebuild")
    for i, text in enumerate(lists):
        f = tmp_path / f"list{i}.txt"
        f.write_text(text)
        cmds.append(f"list L{i} {f}")
    cmds.append(f"decide {name} {app or ''}".rstrip())
    return run(harness, *cmds)[-1]


# ---- Decisions ----

@pytest.mark.parametrize("name,app,rules,lists,result", [
    ("ads.test", None, [], [], "PASSED -"),
    ("ads.test", None, ["||ads.test^"], [], "BLOCKED_RULE ||ads.test^"),
    ("x.ads.test", None, ["||ads.test^"], [], "BLOCKED_RULE ||ads.test^"),
    ("x.ads.test", None, ["|ads.test^"], [], "PASSED -"),
    ("ads.test", None, [], ["||ads.test^"], "BLOCKED_LIST L0"),
    # An own exception beats any list, also $important
    ("ads.test", None, ["@@||ads.test^"], ["||ads.test^$important"], "ALLOWED_RULE @@||ads.test^"),
    # Own rules: the exception wins, however specific the block is
    ("x.ads.test", None, ["||x.ads.test^", "@@||ads.test^"], [], "ALLOWED_RULE @@||ads.test^"),
    # Lists: an exception beats a block, $important beats an exception of another list
    ("ads.test", None, [], ["||ads.test^", "@@||ads.test^"], "ALLOWED_LIST L1"),
    ("ads.test", None, [], ["@@||ads.test^", "||ads.test^$important"], "BLOCKED_LIST L1"),
    # Rules for one app
    ("ads.test", "app/firefox", ["||ads.test^$app=app/firefox"], [],
     "BLOCKED_RULE ||ads.test^$app=app/firefox"),
    ("ads.test", "app/other", ["||ads.test^$app=app/firefox"], [], "PASSED -"),
    ("ads.test", None, ["||ads.test^$app=app/firefox"], [], "PASSED -"),
    ("ads.test", "app/firefox", ["||ads.test^", "@@||ads.test^$app=app/firefox"], [],
     "ALLOWED_RULE @@||ads.test^$app=app/firefox"),
    ("ads.test", "app/maps", ["||ads.test^", "@@||ads.test^$app=app/firefox"], [],
     "BLOCKED_RULE ||ads.test^"),
    ("ads.test", "app/firefox", ["@@||ads.test^", "||ads.test^$app=app/firefox"], [],
     "BLOCKED_RULE ||ads.test^$app=app/firefox"),
    # Firefox' canary: no DNS over HTTPS behind the filter's back, whatever the rules say
    ("use-application-dns.net", None, ["@@||use-application-dns.net^"], [], "BLOCKED_SPECIAL -"),
    # What the phone itself needs is never blocked
    ("ping.archlinux.org", None, ["||archlinux.org^"], ["||ping.archlinux.org^$important"],
     "ALLOWED_SYSTEM -"),
    ("x.raw.githubusercontent.com", None, ["||githubusercontent.com^"], [], "ALLOWED_SYSTEM -"),
    ("evilping.archlinux.org", None, ["||archlinux.org^"], [], "BLOCKED_RULE ||archlinux.org^"),
    ("ping.archlinux.org.evil.test", None, ["||evil.test^"], [], "BLOCKED_RULE ||evil.test^"),
    ("nmcheck.gnome.org.", None, [], ["||nmcheck.gnome.org.^"], "PASSED -"),
])
def test_decide(harness, tmp_path, name, app, rules, lists, result):
    assert decide(harness, tmp_path, name, app, rules, lists) == result


def test_filter_off_or_paused_filters_nothing(harness, tmp_path):
    assert decide(harness, tmp_path, "ads.test", rules=["||ads.test^"],
                  pre=["enable 0"]) == "UNFILTERED -"
    assert decide(harness, tmp_path, "ads.test", rules=["||ads.test^"],
                  pre=["pause 600"]) == "UNFILTERED -"
    assert decide(harness, tmp_path, "ads.test", rules=["||ads.test^"],
                  pre=["pause -5"]) == "BLOCKED_RULE ||ads.test^"


def test_stored_rules_that_do_not_parse_are_inert(harness, tmp_path):
    # A rules file edited by hand: broken lines must neither block nor crash
    rules = ["||ads.test^$app=", "||x.test^$app=bad key", "garbage^^", "$app=app/firefox",
             "||ads.test^$app=app/firefox$important"]
    assert decide(harness, tmp_path, "ads.test", "app/firefox", rules) == "PASSED -"


# ---- Rules as the user enters them (AddRule) ----

@pytest.mark.parametrize("text,rule", [
    ("ads.test", "||ads.test^"),
    ("ADS.Test", "||ads.test^"),
    ("  ads.test  ", "||ads.test^"),
    ("https://ads.test/path?x=1", "||ads.test^"),
    ("http://ads.test:8080/", "||ads.test^"),
    ("http://user@ads.test/", "ERROR"),          # "@" reads as an exception mark
    ("bücher.test", "||xn--bcher-kva.test^"),
    ("@@||ads.test^", "@@||ads.test^"),
    ("|ads.test^", "|ads.test^"),
    ("*.ads.test", "||ads.test^"),
    ("0.0.0.0 ads.test", "|ads.test^"),
    ("||ads.test^$important", "||ads.test^$important"),
    ("||ads.test^$app=app/firefox", "||ads.test^$app=app/firefox"),
    ("ads.test$app=app/firefox", "||ads.test^$app=app/firefox"),
    ("@@||ads.test^$app=android/uid_10123", "@@||ads.test^$app=android/uid_10123"),
    ("||ads.test^$badfilter", "ERROR"),
    ("||ads.test^$app=", "ERROR"),
    ("||ads.test^$app=bad key", "ERROR"),
    ("||ads.test^$important$app=app/x", "ERROR"),
    ("||ads.test^$app=app/x$important", "ERROR"),
    ("$app=app/x", "ERROR"),
    ("", "ERROR"),
    ("   ", "ERROR"),
    ("*", "ERROR"),
    ("||*^", "ERROR"),
    ("ads.test/path", "ERROR"),
    ("javascript:alert(1)", "ERROR"),
    ("/regex/", "ERROR"),
    ("||ads*.test^", "ERROR"),
    ("10.0.0.1 mapped.test", "ERROR"),
    ("0.0.0.0 localhost", "ERROR"),
])
def test_canonical_rule(harness, text, rule):
    assert run(harness, "canon " + text) == [rule]


@pytest.mark.parametrize("text", [
    "a.test\n||b.test^", "a.test\nb.test", "||a.test^\n@@||b.test^", "a.test\r\nb.test",
    "||a.test^$app=app/x\n||b.test^", "a.test\n", "\x00a.test", "a.te\x00st",
])
def test_canonical_rule_never_carries_a_line_break(harness, text):
    # Own rules are stored one per line: a rule with a line break would smuggle in a second one
    out, = run(harness, "canonhex " + text.encode().hex())
    assert out != "LINEBREAK"


@pytest.mark.parametrize("rule,split", [
    ("||a.test^$app=app/x", "app/x ||a.test^"),
    ("||a.test^", "- ||a.test^"),
    ("||a.test^$app=", "- ||a.test^$app="),
    ("@@|a.test^$app=user/pipewire.service", "user/pipewire.service @@|a.test^"),
])
def test_app_rule_split(harness, rule, split):
    assert run(harness, "split " + rule) == [split]


@pytest.mark.parametrize("key,ok", [
    ("app/firefox", True), ("android/uid_10123", True), ("system/NetworkManager.service", True),
    ("user/session.scope", True), ("app/org.gnome.Fractal", True), ("a@b+c~d:e", True),
    ("x" * 255, True), ("x" * 256, False), ("", False), ("a b", False), ("a$b", False),
    ("app/ä", False), ("a,b", False), ("a^b", False), ("a|b", False), ("a\\b", False),
])
def test_valid_app_key(harness, key, ok):
    assert run(harness, "appkey " + key) == ["yes" if ok else "no"]


# ---- Where names may go ----

@pytest.mark.parametrize("domain,ok", [
    ("lan", True), ("LAN", True), ("home", True), ("local", True), ("internal", True),
    ("localdomain", True), ("example.com", True), ("corp.example", True),
    ("example.co.uk", True), ("a.b.co.uk", True),
    # A network that hands out a public suffix would get every such name in plain text
    ("com", False), ("de", False), ("co.uk", False), ("CO.UK", False), ("com.au", False),
    ("", False), (".", False), ("com.", False), ("lan.", False),
])
def test_usable_network_domain(harness, domain, ok):
    assert run(harness, "usable " + domain) == ["yes" if ok else "no"]


@pytest.mark.parametrize("name,local", [
    ("router", True), ("printer.local", True), ("PRINTER.LOCAL", True), ("nas.lan", True),
    ("x.home.arpa", True), ("4.3.2.10.in-addr.arpa", True), ("1.1.168.192.in-addr.arpa", True),
    ("1.20.172.in-addr.arpa", True), ("1.31.172.in-addr.arpa", True),
    ("1.15.172.in-addr.arpa", False), ("1.32.172.in-addr.arpa", False),
    ("1.1.1.1.in-addr.arpa", False), ("a.notlocal", False), ("example.com", False),
    ("1.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.8.e.f.ip6.arpa", True),
])
def test_local_names(harness, name, local):
    assert run(harness, "local " + name) == ["yes" if local else "no"]


@pytest.mark.parametrize("addr,is_self", [
    ("127.0.0.1", True), ("127.0.0.53", True), ("::1", True), ("0.0.0.0", True), ("::", True),
    ("::ffff:127.0.0.1", True), ("127.0.0.1#53", True), ("::1%lo", True),
    ("9.9.9.9", False), ("not-an-address", False), ("", False),
])
def test_server_is_self(harness, addr, is_self):
    # Our own address as an upstream would send every question back to ourselves
    assert run(harness, "self " + addr) == ["yes" if is_self else "no"]


# The Servers property takes a list only if upstream_parse() takes every entry


@pytest.mark.parametrize("spec,parsed", [
    ("9.9.9.9", "udp 9.9.9.9:53"),
    ("  9.9.9.9  ", "udp 9.9.9.9:53"),
    ("udp://9.9.9.9", "udp 9.9.9.9:53"),
    ("9.9.9.9:5353", "udp 9.9.9.9:5353"),
    ("2620:fe::fe", "udp [2620:fe::fe]:53"),
    ("[2620:fe::fe]:5353", "udp [2620:fe::fe]:5353"),
    ("tls://dns.quad9.net", "tls dns.quad9.net 853"),
    ("tls://dns.quad9.net:8853", "tls dns.quad9.net 8853"),
    ("tls://9.9.9.9", "tls 9.9.9.9 853"),
    ("https://dns.quad9.net/dns-query", "https https://dns.quad9.net/dns-query"),
    # A plain server must be an address: a name would need DNS to find DNS
    ("dns.quad9.net", "ERROR"),
    ("udp://dns.quad9.net", "ERROR"),
    # GUri and GNetworkAddress give "" as the host of these
    ("https://", "ERROR"),
    ("https:///dns-query", "ERROR"),
    ("tls://", "ERROR"),
    ("", "ERROR"),
    ("9.9.9.9:99999", "ERROR"),
])
def test_upstream_specs(harness, spec, parsed):
    assert run(harness, "upstream " + spec) == [parsed]


# ---- Who may change settings ----

@pytest.mark.parametrize("bus,result", [
    ([], "allowed"),                                     # the user of the active session
    (["bus caller 0"], "allowed"),                       # root
    (["bus caller 0", "bus session -"], "allowed"),
    (["bus caller 1002"], "denied"),                     # another user
    (["bus caller -1"], "denied"),                       # the bus does not know the sender
    (["bus session -"], "denied"),                       # nobody in front of the phone
    (["bus session badtype"], "denied"),
    (["bus suid -1"], "denied"),
    (["bus suid -2"], "denied"),
    (["bus caller 1002", "bus suid 1002", "bus groups 1002,998"], "allowed"),
    # 0.1-12: the user in front of the phone must also be an administrator (L15)
    (["bus caller 1002", "bus suid 1002", "bus groups 1002"], "denied"),
    (["bus groups none"], "denied"),
    (["bus groups -"], "denied"),                        # the bus gave no groups
    (["bus wheel -1"], "denied"),                        # no group wheel at all
    (["bus wheel 10"], "denied"),                        # 998 is not wheel here
    (["bus caller 0", "bus groups -", "bus wheel -1"], "allowed"),  # root needs neither
])
def test_check_caller(harness, bus, result):
    out = run(harness, *bus, "caller :1.42")
    assert out[-1].split()[0] == result
