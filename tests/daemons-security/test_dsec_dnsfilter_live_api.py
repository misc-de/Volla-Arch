# plinius-dnsfilter as a running daemon (c/dsec_dnsfilter_live_harness.c): its D-Bus interface as
# Settings uses it (properties, rules, lists and their downloads from a list server of the test,
# the statistics, the query log, the live log for watchers), its state files, start and exit.

import os
import subprocess
import time

import pytest

from conftest import PKG
from dnsf_live_helpers import (FakeHttps, Msg, dns, file_mode, live, log_entries,  # noqa: F401
                               nm_conf, nm_link, query, tls_files, unpack, write_lists,
                               write_settings, zone)

ADMIN = ":1.5"
LOG_ALL = "call :1.5 GetLog (uint32 0, uint32 2000, '', uint32 0)"


def gi_glib():
    return pytest.importorskip("gi.repository.GLib")


# ---- Start, state files, exit ----

def test_first_start_has_the_defaults_and_one_list(live):
    r = live.run(*[f"get {ADMIN} {p}" for p in (
        "Enabled", "Status", "PausedUntil", "BlockMode", "CustomServers", "Servers", "NetworkServers",
        "LocalViaNetwork", "SharedClients", "RedirectDns", "BlockEncryptedDns", "KeepLog", "LogSize",
        "StatsDays", "UpdateInterval", "UpdateOnMobileData", "RuleCount")], "get :1.5 Lists",
        "timers on_idle_exit", "show bus", sysbus=False)
    assert [unpack(x) for x in r[:17]] == [False, "off", 0, "null-ip", False, [], [], True, True, True,
                                           True, False, 5000, 7, 24, False, 0]
    lst, = unpack(r[17])
    assert (lst["id"], lst["enabled"], lst["catalog"], lst["rules"]) == ("hagezi-normal", True, True, 0)
    # Off: the service leaves after two minutes unless someone asks
    assert r[18] == "timers 120000"
    assert r[19] == "system"
    assert file_mode(live.state) == 0o700
    assert file_mode(live.state / "lists") == 0o600
    assert (live.state / "compiled").is_dir()


def test_files_of_older_versions_are_made_private(live):
    live.state.mkdir()
    (live.state / "sub").mkdir()
    for f in ("rules", "sub/stats", "log"):
        (live.state / f).write_text("! x\n")
        os.chmod(live.state / f, 0o644)
    os.symlink("/nonexistent", live.state / "dangling")
    os.chmod(live.state, 0o755)
    live.run("quit", quits=True)
    assert file_mode(live.state) == 0o700
    assert file_mode(live.state / "rules") == file_mode(live.state / "sub" / "stats") == 0o600
    # The log of 0.1 (without apps) is gone
    assert not (live.state / "log").exists()


def test_settings_are_read_clamped_and_written_back(live):
    write_settings(live, enabled=False, block_mode="nxdomain", custom_servers=True,
                   servers=["9.9.9.9", "nonsense"], local_via_network=False, shared_clients=False,
                   redirect_dns=False, block_encrypted_dns=False, keep_log=True, log_size=5,
                   stats_days=500, update_hours=9999, update_on_mobile=True, paused_until=1)
    r = live.run(*[f"get {ADMIN} {p}" for p in ("BlockMode", "Servers", "LogSize", "StatsDays",
                                                 "UpdateInterval", "KeepLog", "SharedClients")],
                 "show custom", "set :1.5 StatsDays uint32 1000", "set :1.5 LogSize uint32 99999",
                 "set :1.5 UpdateInterval uint32 99999")
    assert [unpack(x) for x in r[:7]] == ["nxdomain", ["9.9.9.9", "nonsense"], 100, 90, 720, True, False]
    # A server that does not parse is left out
    assert r[7] == "1"
    text = (live.state / "settings").read_text()
    assert "stats-days=90" in text and "log-size=50000" in text and "update-hours=720" in text
    assert file_mode(live.state / "settings") == 0o600


@pytest.mark.parametrize("content", ["not a key file\x00", "[filter]\nblock-mode=evil\nlog-size=x\n"])
def test_broken_settings_give_the_defaults(live, content):
    live.state.mkdir()
    (live.state / "settings").write_text(content)
    r = live.run("get :1.5 BlockMode", "get :1.5 LogSize")
    assert [unpack(x) for x in r] == ["null-ip", 5000]


def test_settings_of_a_fresh_install_switch_the_filter_on(live):
    # What plinius-dnsfilter.install writes on a fresh install (also every new image), read by the
    # daemon itself. Only _switch_on runs, on a copy whose state path is in tmp
    script = (PKG / "plinius-dnsfilter" / "plinius-dnsfilter.install").read_text()
    body = script.split("_switch_on() {", 1)[1].split("\n}\n", 1)[0]
    assert "/etc/" not in body and "/run/" not in body
    copy = live.root / "dnsfilter.install"
    copy.write_text(script.replace("/var/lib/plinius-dnsfilter", str(live.state)))
    subprocess.run(["bash", "-c", f'. "{copy}"; _switch_on'], check=True, timeout=10)
    r = live.run("get :1.5 Enabled", "get :1.5 BlockMode")
    assert [unpack(x) for x in r] == [True, "null-ip"]

def test_options(live):
    r = live.run("show bus", args=live.args(extra=["--session"]))
    assert r[0] == "session"
    r = live.run(args=["--port", "x"])
    assert r.exit == 2
    # Without --state-dir: the default place (in the test copy under tmp)
    live.run("quit", args=["--no-system", "--port", str(live.port)], quits=True)
    assert (live.root / "default-state" / "lists").exists()


@pytest.mark.parametrize("cmd", ["sigterm", "namelost", "fire on_idle_exit"])
def test_ways_to_leave(live, cmd):
    r = live.run(cmd, "get :1.5 Status", quits=True)
    assert r.out == (["fired 1"] if cmd.startswith("fire") else ["ok"])
    assert r.exit == 0


def test_no_idle_exit_while_on_or_watched(live):
    r = live.run("signals", "call :1.5 Watch -", "fire on_idle_exit", "call :1.5 Watch -", "signals",
                 "call :1.5 Unwatch -", "timers on_idle_exit", "call :1.5 Watch -", "vanish :1.5",
                 "show watchers", "set :1.5 Enabled true", "timers on_idle_exit", "fire on_idle_exit",
                 "get :1.5 Status", "set :1.5 Enabled false", "fire on_idle_exit", "get :1.5 Status", quits=True)
    # Watched: the timer runs out, the daemon stays
    assert r[2] == "fired 1"
    assert r[4] == "signals watch::1.5"
    assert r[6] == "timers 120000"
    assert r[9] == "0"
    # On: no idle exit at all
    assert r[11] == "timers -"
    assert r[12] == "fired 0"
    assert unpack(r[13]) == "no-network"
    assert r[15] == "fired 1" and len(r.out) == 16


def test_statistics_and_log_are_saved_on_exit_and_by_the_timer(live, dns):
    gi_glib()
    write_settings(live, enabled=True, custom_servers=True, servers=[dns.addr], keep_log=True)
    live.run(f"uq {query('a.test')}", "timers on_save_timer", "fire on_save_timer", "quit", quits=True)
    assert file_mode(live.state / "stats") == file_mode(live.state / "querylog") == 0o600
    r = live.run(LOG_ALL, "call :1.5 GetStats (uint32 1,)", f"uq {query('b.test')}", "quit", quits=True)
    e, = log_entries(r[0])
    assert e["name"] == "a.test"
    assert unpack(r[1])[0]["queries"] == 1
    r = live.run(LOG_ALL, "set :1.5 KeepLog false", "quit", quits=True)
    assert [e["name"] for e in log_entries(r[0])] == ["b.test", "a.test"]
    # Not kept any more: the file goes
    assert not (live.state / "querylog").exists()


# ---- Who may ----

@pytest.mark.parametrize("call", [
    "call :1.9 GetRules -", "call :1.9 Pause (uint32 5,)", "get :1.9 Servers", "get :1.9 Lists",
    "set :1.9 Enabled true", "set :1.9 BlockMode 'nxdomain'",
])
def test_only_an_administrator_in_front_of_the_phone(live, call):
    r = live.run("bus caller 1002", call, "bus caller 1001", call, system=True, path_env="")
    assert "Only an administrator in front of the phone" in r[1]
    assert r[3].startswith("ok")


def test_public_properties_need_no_administrator(live):
    r = live.run("bus caller 1002", "get :1.9 Status", "get :1.9 RuleCount", system=True, path_env="")
    assert r[1:] == ["ok 'off'", "ok uint32 0"]


# ---- Properties ----

def test_setting_properties(live):
    r = live.run("set :1.5 BlockMode 'refused'", "set :1.5 BlockMode 'evil'",
                 "set :1.5 Servers ['9.9.9.9', 'tls://dns.quad9.net']", "show custom",
                 "set :1.5 Servers ['9.9.9.9', 'dns.quad9.net']", "get :1.5 Servers",
                 "set :1.5 CustomServers true", "set :1.5 LocalViaNetwork false",
                 "set :1.5 SharedClients false", "set :1.5 RedirectDns false",
                 "set :1.5 BlockEncryptedDns false", "set :1.5 UpdateOnMobileData true",
                 "set :1.5 LogSize uint32 10", "get :1.5 LogSize", "set :1.5 Status 'x'",
                 "set :1.5 Enabled 'yes'", "setraw :1.5 Status 'x'", "signals")
    assert r[0] == "ok"
    assert r[1] == "error Unknown mode evil"
    assert r[3] == "2"
    # One bad entry: nothing changes
    assert "need an address" in r[4]
    assert unpack(r[5]) == ["9.9.9.9", "tls://dns.quad9.net"]
    assert unpack(r[13]) == 100
    assert "PropertyReadOnly" in r[14]
    assert "InvalidArgs" in r[15]
    assert r[16] == "error Status is read-only"
    s = (live.state / "settings").read_text()
    for line in ("block-mode=refused", "custom-servers=true", "local-via-network=false",
                 "shared-clients=false", "redirect-dns=false", "block-encrypted-dns=false",
                 "update-on-mobile=true", "log-size=100", "servers=9.9.9.9;tls://dns.quad9.net;"):
        assert line in s
    # Servers go only to watchers (a DoH address may carry a profile id); the rest to everyone
    assert "*>Servers" not in r[17] and "*>BlockMode" in r[17] and "*>CustomServers" in r[17]


def test_the_log_keeps_its_newest_entries_when_it_shrinks(live, dns):
    write_settings(live, enabled=True, custom_servers=True, servers=[dns.addr], log_size=100)
    names = [f"n{i}.a.test" for i in range(130)]
    r = live.run(*[f"uq {query(n)}" for n in names], "show log", LOG_ALL, "set :1.5 LogSize uint32 200",
                 "show log", f"uq {query('last.test')}", "set :1.5 LogSize uint32 100", "show log", LOG_ALL)
    assert r[130] == "100 100"
    assert [e["name"] for e in log_entries(r[131])] == names[:29:-1]
    assert r[133] == "100 200"
    assert r[136] == "100 100"
    assert [e["name"] for e in log_entries(r[137])][:2] == ["last.test", names[-1]]


# ---- Own rules ----

def test_rules_as_settings_sees_them(live):
    r = live.run("call :1.5 AddRule ('Ads.Test',)", "call :1.5 AddRule ('ads.test',)",
                 "call :1.5 AddRule ('@@||ads.test^',)", "call :1.5 AddRule ('||x.test^$app=app/firefox',)",
                 "call :1.5 AddRule ('not a rule',)", "call :1.5 GetRules -", "get :1.5 RuleCount",
                 "call :1.5 RemoveRule ('||x.test^$app=app/firefox',)", "call :1.5 RemoveRule ('nothing',)",
                 "call :1.5 GetRules -", "signals")
    assert unpack(r[0]) == ("||ads.test^",)
    # The exception replaces the block of the same domain
    assert unpack(r[5]) == (["@@||ads.test^", "||x.test^$app=app/firefox"],)
    assert unpack(r[6]) == 2
    assert "Not a" in r[4]
    assert unpack(r[9]) == (["@@||ads.test^"],)
    # Four added (one twice), one removed; removing what is not there changes nothing
    assert r[10].count("RuleCount") == 5
    text = (live.state / "rules").read_text()
    assert text.splitlines() == ["! Own rules of the DNS filter (Settings > Network & VPN > DNS Filter)",
                                 "@@||ads.test^"]
    assert file_mode(live.state / "rules") == 0o600
    # Read back at the next start, comments and blank lines skipped
    (live.state / "rules").write_text("! x\n\n  ||b.test^  \n")
    r = live.run("call :1.5 GetRules -")
    assert unpack(r[0]) == (["||b.test^"],)


def test_check_shows_the_decision_even_while_off(live):
    r = live.run("call :1.5 AddRule ('ads.test',)", "call :1.5 AddRule ('@@||ok.ads.test^$app=app/x',)",
                 "call :1.5 Check ('X.ADS.TEST',)", "call :1.5 Check ('bücher.test',)",
                 "call :1.5 CheckNames ('app/x', ['ok.ads.test', 'ads.test', 'ping.archlinux.org'])",
                 "call :1.5 CheckNames ('', ['ok.ads.test'])", "get :1.5 Status")
    assert unpack(r[2]) == (5, "||ads.test^")
    assert unpack(r[3]) == (0, "")
    assert unpack(r[4]) == ([(1, "@@||ok.ads.test^$app=app/x"), (5, "||ads.test^"), (3, "")],)
    assert unpack(r[5]) == ([(5, "||ads.test^")],)
    assert unpack(r[6]) == "off"


def test_check_names_takes_2000_at_most(live):
    names = ", ".join(f"'n{i}.test'" for i in range(2100))
    r = live.run(f"call :1.5 CheckNames ('', [{names}])")
    assert len(unpack(r[0])[0]) == 2000


# ---- Lists ----

@pytest.fixture
def tls(tmp_path_factory):
    return tls_files(tmp_path_factory)


@pytest.fixture
def lists(tls, zone):
    server = FakeHttps(zone, tls[1], tls[2])
    yield server
    server.close()


def list_env(live, dns, tls):
    live.env.update(LIVE_NM=nm_conf(nm_link("wlan0", [dns.addr])), LIVE_CA=tls[0])


def list_file(live, lists, entries):
    text = ""
    for i, (name, enabled) in enumerate(entries):
        text += (f"[l{i}]\nname=List {i}\nurl=https://lists.test:{lists.port}/{name}\n"
                 f"enabled={str(enabled).lower()}\n")
    write_lists(live, text)


def test_a_list_is_downloaded_compiled_and_used(live, dns, lists, tls):
    lists.lists["ads.txt"] = b"! comment\n||ads.test^\n||track.test^$third-party\n/regex/\n"
    write_settings(live, custom_servers=True, servers=[dns.addr])
    list_env(live, dns, tls)
    list_file(live, lists, [("ads.txt", True)])
    r = live.run("set :1.5 Enabled true", "timers on_update_timer", "fire on_update_timer",
                 "until lists-idle 20000", "get :1.5 Lists", f"uq {query('x.ads.test')}",
                 "call :1.5 GetStats (uint32 1,)", "timers on_update_timer")
    # Switched on: the lists come right away
    assert r[1] == "timers 1000"
    lst, = unpack(r[4])
    assert (lst["rules"], lst["unsupported"], lst["downloading"], "error" in lst) == (1, 2, False, False)
    assert Msg(r[5]).addrs == ["0.0.0.0"]
    assert unpack(r[6])[0]["rules"] == 1
    # The next check in half an hour
    assert r[7] == "timers 1800000"
    assert (live.state / "compiled" / "l0.bin").exists()
    saved = (live.state / "lists").read_text()
    assert 'etag="ads.txt-' in saved and "last-modified=Wed, 18 Nov 2026" in saved
    # Unchanged on the server: 304, the compiled file stays
    r = live.run("call :1.5 UpdateLists -", "until lists-idle 20000", "get :1.5 Lists",
                 f"uq {query('x.ads.test')}")
    assert lists.requests[-1][2].get("If-None-Match", "").startswith('"ads.txt-')
    assert {r[2].get("User-Agent") for r in lists.requests} == {"Plinius Arch Linux"}
    assert unpack(r[2])[0]["rules"] == 1
    assert Msg(r[3]).addrs == ["0.0.0.0"]
    # 304 while the compiled file is gone: downloaded again without the cache headers
    r = live.run(f"unlink {live.state}/compiled/l0.bin", "call :1.5 UpdateLists -", "until lists-idle 20000",
                 "get :1.5 Lists", "call :1.5 UpdateLists -", "until lists-idle 20000", "get :1.5 Lists")
    assert unpack(r[3])[0]["error"] == "Retrying without cache"
    assert "If-None-Match" not in lists.requests[-1][2]
    assert "error" not in unpack(r[6])[0] and unpack(r[6])[0]["rules"] == 1


@pytest.mark.parametrize("body,status,error", [
    (b"", 200, "The download has no rules (not a blocklist?)"),
    (b"<html>not a list</html>\n", 200, "The download has no rules (not a blocklist?)"),
    (None, 404, "HTTP 404 Not Found"),
    (b"x" * (33 * 1024 * 1024), 200, "The list is larger than 32 MB"),
], ids=["empty", "html", "404", "33mb"])
def test_downloads_that_are_no_list(live, dns, lists, tls, body, status, error):
    if body is not None:
        lists.lists["l.txt"] = body
    write_settings(live, enabled=True)
    list_env(live, dns, tls)
    list_file(live, lists, [("l.txt", True)])
    r = live.run("call :1.5 UpdateLists -", "until lists-idle 60000", "get :1.5 Lists", timeout=180)
    assert unpack(r[2])[0]["error"] == error
    assert not (live.state / "compiled" / "l0.bin").exists()


def test_a_list_that_cannot_be_saved(live, dns, lists, tls):
    lists.lists["l.txt"] = b"||ads.test^\n"
    write_settings(live, enabled=True)
    list_env(live, dns, tls)
    list_file(live, lists, [("l.txt", True)])
    # Read-only after the start (which makes the directory 0700)
    try:
        r = live.run(f"chmod {live.state}/compiled 500", "call :1.5 UpdateLists -", "until lists-idle 20000",
                     "get :1.5 Lists")
    finally:
        os.chmod(live.state / "compiled", 0o700)
    assert "l0.bin" in unpack(r[3])[0]["error"]


def test_list_addresses_must_be_https_and_reachable(live, dns, tls):
    write_settings(live, enabled=True)
    list_env(live, dns, tls)
    write_lists(live, "[plain]\nname=Plain\nurl=http://lists.test/l.txt\nenabled=true\n"
                      "[nohost]\nname=No host\nurl=https://\nenabled=true\n"
                      "[gone]\nname=Gone\nurl=https://missing.test/l.txt\nenabled=true\n"
                      "[bad/id]\nname=Bad\nurl=https://x.test/\nenabled=true\n[noname]\nurl=https://x/\n")
    r = live.run("call :1.5 UpdateLists -", "until lists-idle 20000", "get :1.5 Lists")
    got = {lst["id"]: lst.get("error") for lst in unpack(r[2])}
    assert got["plain"] == "Lists need an https:// address"
    assert got["nohost"] == "Not a web address"
    assert got["gone"]
    # Groups that are not a list are skipped
    assert set(got) == {"plain", "nohost", "gone"}


def test_when_lists_are_updated(live, dns, lists, tls):
    lists.lists["a.txt"] = b"||a.example^\n"
    lists.lists["b.txt"] = b"||b.example^\n"
    write_settings(live, enabled=True, update_hours=24)
    list_env(live, dns, tls)
    list_file(live, lists, [("a.txt", True), ("b.txt", False)])
    r = live.run("fire on_update_timer", "until lists-idle 20000", "get :1.5 Lists")
    a, b = unpack(r[2])
    assert (a["rules"], b["rules"]) == (1, 0)
    n = len(lists.requests)
    # Not due yet; on mobile data not even when due; offline never
    r = live.run("fire on_update_timer", "until lists-idle 20000", "clock 90000", "metered 1", "nmchanged",
                 "fire on_update_timer", "until lists-idle 20000", "conn 1", "metered 4", "nmchanged",
                 "fire on_update_timer", "until lists-idle 20000", "conn 4", "nmchanged",
                 "timers on_update_timer", "fire on_update_timer", "until lists-idle 20000")
    # Back online: due lists in 5 s
    assert r[14] == "timers 5000"
    assert len(lists.requests) == n + 1
    # A failed list waits half an hour unless the user asks
    lists.status["a.txt"] = 500
    # (the last download is a day ahead: the clock of the run before was)
    r = live.run("clock 180000", "call :1.5 UpdateLists -", "until lists-idle 20000", "fire on_update_timer",
                 "until lists-idle 20000", "clock 1801", "fire on_update_timer", "until lists-idle 20000",
                 "get :1.5 Lists")
    assert len(lists.requests) == n + 3
    assert unpack(r[8])[0]["error"] == "HTTP 500 Internal Server Error"


def test_adding_enabling_and_removing_lists(live, dns, lists, tls):
    lists.lists["c.txt"] = b"||c.example^\n"
    write_settings(live, enabled=True)
    list_env(live, dns, tls)
    url = f"https://lists.test:{lists.port}/c.txt"
    r = live.run("call :1.5 AddList ('oisd-small', '')", f"call :1.5 AddList ('{url}', '')",
                 "until lists-idle 20000", f"call :1.5 AddList ('{url}', 'again')",
                 "call :1.5 AddList ('http://x.test/', 'x')", "call :1.5 GetCatalog -",
                 "call :1.5 SetListEnabled ('hagezi-normal', false)", "call :1.5 SetListEnabled ('nope', true)",
                 "call :1.5 RemoveList ('nope',)", "get :1.5 Lists", "signals")
    assert unpack(r[0]) == ("oisd-small",)
    cid, = unpack(r[1])
    assert cid.startswith("custom-")
    assert unpack(r[3]) == (cid,)
    assert "secure web addresses" in r[4]
    catalog = {c["id"]: c["added"] for c in unpack(r[5])[0]}
    assert catalog["oisd-small"] and catalog["hagezi-normal"] and not catalog["oisd-big"]
    assert "No list nope" in r[7] and "No list nope" in r[8]
    by_id = {lst["id"]: lst for lst in unpack(r[9])}
    assert by_id[cid]["name"] == url and by_id[cid]["rules"] == 1 and not by_id["hagezi-normal"]["enabled"]
    # Lists change only for watchers (an address may carry a token)
    assert "*>Lists" not in r[10]
    r = live.run(f"call :1.5 SetListEnabled ('{cid}', false)", f"call :1.5 SetListEnabled ('{cid}', true)",
                 f"call :1.5 RemoveList ('{cid}',)", "get :1.5 Lists")
    assert not (live.state / "compiled" / f"{cid}.bin").exists()
    assert cid not in {lst["id"] for lst in unpack(r[3])}


def test_a_list_removed_while_it_downloads(live, dns, lists, tls):
    lists.lists["d.txt"] = b"||d.example^\n"
    write_settings(live, enabled=True)
    list_env(live, dns, tls)
    list_file(live, lists, [("d.txt", True)])
    r = live.run("call :1.5 UpdateLists -", "call :1.5 RemoveList ('l0',)", "run 1500", "get :1.5 Lists")
    assert "l0" not in {lst["id"] for lst in unpack(r[3])}


def test_a_broken_compiled_list_is_not_used(live):
    write_lists(live, "[l0]\nname=L\nurl=https://x.test/\nenabled=true\nrules=5\n")
    (live.state / "compiled").mkdir(parents=True)
    (live.state / "compiled" / "l0.bin").write_bytes(b"garbage")
    r = live.run("call :1.5 Check ('x.test',)", "get :1.5 Lists")
    assert unpack(r[0]) == (0, "")


# ---- Statistics ----

def stats(text):
    return unpack(text)[0]


def test_statistics_count_what_happened(live, dns, zone):
    zone.names["fail.test"] = {"drop": True}
    write_settings(live, enabled=True, custom_servers=True, servers=[dns.addr])
    odd = query("we!rd.test")
    r = live.run("call :1.5 AddRule ('ads.test',)", "app app/firefox", f"uq {query('a.test')}",
                 f"uq {query('a.test')}", f"uq {query('ads.test')}", "app -", f"uq {query('b.test')}",
                 f"ukeep 1 {query('fail.test')}", "run 50", "fire on_deadline", "urecv 1",
                 f"uq {odd}", "call :1.5 GetStats (uint32 0,)",
                 "call :1.5 GetNamesOf (uint32 1, 'app/firefox')",
                 "call :1.5 GetNamesOf (uint32 1, '\tphone\t')", "call :1.5 GetAskersOf (uint32 1, 'a.test')")
    s = stats(r[12])
    assert (s["days"], s["queries"], s["blocked"], s["cached"], s["failed"], s["rules"]) == (1, 6, 1, 1, 1, 0)
    assert dict(s["top-domains"])["a.test"] == 2
    assert s["top-blocked"] == [("ads.test", 1)]
    assert s["top-apps"] == [("app/firefox", 3)]
    # A name with odd bytes counts in the totals only
    assert dict(s["top-clients"]) == {"phone\t": 5}
    assert "we!rd.test" not in dict(s["top-domains"])
    # Answers and failures of the server (not the cached or blocked ones)
    ups = {u[0]: u[1] for u in s["upstreams"]}
    assert ups[dns.addr] == 4
    assert len(s["series"]) == 1 and s["series"][0][1] == 6
    assert unpack(r[13]) == ([("a.test", 2), ("ads.test", 1)],)
    # The phone covers its apps
    assert dict(unpack(r[14])[0])["a.test"] == 2 and dict(unpack(r[14])[0])["b.test"] == 1
    assert unpack(r[15]) == ([("app/firefox", 2)],)


def test_statistics_over_days(live, dns):
    write_settings(live, enabled=True, custom_servers=True, servers=[dns.addr], stats_days=2)
    r = live.run(f"uq {query('a.test')}", "clock 86400", f"uq {query('b.test')}", "clock 86400",
                 f"uq {query('b.test', 'AAAA')}", "show hours", "call :1.5 GetStats (uint32 1,)",
                 "call :1.5 GetStats (uint32 2,)", "call :1.5 ClearStats -", "call :1.5 GetStats (uint32 7,)")
    # Two days kept
    assert r[5] == "3 2"
    assert stats(r[6])["queries"] == 1
    assert stats(r[7])["queries"] == 2 and dict(stats(r[7])["top-domains"]) == {"b.test": 2}
    assert stats(r[9])["queries"] == 0


def test_the_top_tables_keep_the_most_asked(live, dns, zone):
    zone.names.update({f"n{i}.test": {"a": ["192.0.2.1"]} for i in range(620)})
    zone.names["hot.test"] = {"a": ["192.0.2.1"]}
    write_settings(live, enabled=True, custom_servers=True, servers=[dns.addr])
    cmds = [f"uq {query('hot.test', t)}" for t in ("A", "AAAA", "MX")]
    cmds += [f"uq {query(f'n{i}.test')}" for i in range(620)]
    r = live.run(*cmds, "call :1.5 GetStats (uint32 1,)", timeout=180)
    top = stats(r[-1])["top-domains"]
    # 600 names in the table: the top half stays, the most asked first
    assert top[0] == ("hot.test", 3) and len(top) == 20


# Statistics files of the earlier versions, built with GLib's own serializer

def stats_file(live, version, hours, days):
    GLib = gi_glib()
    types = {1: "(ua(xuuuut)a(xa{su}a{su}a{su}a{s(tt)}))",
             2: "(ua(xuuuut)a(xa{su}a{su}a{su}a{s(tt)}a{su}))",
             3: "(ua(xuuuuta{su})a(xa{su}a{su}a{su}a{s(tt)}a{su}))",
             4: "(ua(xuuuuta{su})a(xa{su}a{su}a{su}a{s(tt)}a{su}a{su}))"}
    v = GLib.Variant(types[version], (version, hours, days))
    live.state.mkdir(parents=True, exist_ok=True)
    (live.state / "stats").write_bytes(v.get_data_as_bytes().get_data())


def now_hour():
    return int(time.time()) // 3600


def test_statistics_of_version_1_move_blocked_names_to_their_hours(live):
    h = now_hour()
    day = h * 3600 // 86400
    stats_file(live, 1, [(h, 5, 2, 0, 0, 100), (h, 3, 0, 1, 1, 30)],
               [(day, {"a.test": 4}, {"ads.test": 2}, {"phone\t": 8}, {"9.9.9.9": (6, 600)})])
    r = live.run("call :1.5 GetStats (uint32 1,)")
    s = stats(r[0])
    assert (s["queries"], s["blocked"], s["cached"], s["failed"]) == (8, 2, 1, 1)
    assert s["top-blocked"] == [("ads.test", 2)]
    assert s["upstreams"] == [("9.9.9.9", 6, 100)]


@pytest.mark.parametrize("version", [2, 3, 4])
def test_statistics_of_later_versions(live, version):
    h = now_hour()
    day = h * 3600 // 86400
    names = {f"b{i}.test": i for i in range(150)}
    hour = (h, 10, 4, 0, 0, 100) + ((names,) if version >= 3 else ())
    d = (day, {"a.test": 4}, {} if version >= 3 else {"old.test": 1}, {"phone\t": 8}, {}, {"app/x": 4})
    if version == 4:
        d += ({"app/x\na.test": 4, "broken": 1},)
    stats_file(live, version, [hour], [d])
    r = live.run("call :1.5 GetStats (uint32 1,)", "call :1.5 GetNamesOf (uint32 1, 'app/x')")
    s = stats(r[0])
    assert s["top-apps"] == [("app/x", 4)]
    if version >= 3:
        # Over the limit of 0.1-8 and before: the top 50 stay
        assert s["top-blocked"][0] == ("b149.test", 149)
    else:
        assert s["top-blocked"] == [("old.test", 1)]
    assert unpack(r[1]) == (([("a.test", 4)],) if version == 4 else ([],))


@pytest.mark.parametrize("data", [b"", b"\x09\0\0\0", b"\x04\0\0\0garbage", b"\0\0\0\0"])
def test_broken_statistics_files_give_empty_statistics(live, data):
    live.state.mkdir()
    (live.state / "stats").write_bytes(data)
    r = live.run("call :1.5 GetStats (uint32 1,)")
    assert stats(r[0])["queries"] == 0


def test_a_statistics_file_larger_than_16_mb_is_not_read(live):
    live.state.mkdir()
    with open(live.state / "stats", "wb") as f:
        f.write(b"\x04\0\0\0")
        f.truncate(17 * 1024 * 1024)
    r = live.run("call :1.5 GetStats (uint32 1,)")
    assert stats(r[0])["queries"] == 0


def test_statistics_beyond_8_mb_lose_the_names_of_the_oldest_days(live):
    h = now_hour()
    day = h * 3600 // 86400
    days = []
    for k in range(6, -1, -1):
        names = {f"{k}-{i}-" + "x" * 200 + ".test": 1 for i in range(9000)}
        days.append((day - k, names, {}, {}, {}, {}, {}))
    stats_file(live, 4, [(h - 24 * 6, 1, 1, 0, 0, 1, {"ads.test": 1}), (h, 1, 0, 0, 0, 1, {})], days)
    r = live.run("call :1.5 GetStats (uint32 90,)", "call :1.5 ClearStats -", timeout=180)
    s = stats(r[0])
    # The counts stay, the newest names stay
    assert s["queries"] == 2
    assert all(n.startswith("0-") for n, _ in s["top-domains"])
    assert s["top-blocked"] == []


# ---- The query log ----

def test_the_log_as_settings_reads_it(live, dns, zone):
    write_settings(live, enabled=True, custom_servers=True, servers=[dns.addr])
    r = live.run("call :1.5 AddRule ('ads.test',)", "app app/Firefox", f"uq {query('a.test')}",
                 f"uq {query('ads.test')}", "app -", f"uq {query('a.test', 'AAAA')}", f"uq {query('b.test')}",
                 LOG_ALL, "call :1.5 GetLog (uint32 0, uint32 2, '', uint32 0)",
                 "call :1.5 GetLog (uint32 3, uint32 10, '', uint32 0)",
                 "call :1.5 GetLog (uint32 0, uint32 10, 'FIREFOX', uint32 0)",
                 "call :1.5 GetLog (uint32 0, uint32 10, '192.0.2.2', uint32 0)",
                 "call :1.5 GetLog (uint32 0, uint32 10, '', uint32 1)",
                 "call :1.5 GetLog (uint32 0, uint32 10, '', uint32 2)",
                 "call :1.5 GetLog (uint32 0, uint32 10, 'a.test', uint32 4)",
                 "call :1.5 GetLogNames (uint32 0, uint32 10, '', uint32 0)",
                 "call :1.5 GetLogNames (uint32 1, uint32 1, '', uint32 0)",
                 "call :1.5 ClearLog -", LOG_ALL)

    def names(x):
        return [e["name"] for e in log_entries(x)]

    assert names(r[7]) == ["b.test", "a.test", "ads.test", "a.test"]
    assert names(r[8]) == ["b.test", "a.test"]
    assert names(r[9]) == ["ads.test", "a.test"]
    assert names(r[10]) == ["ads.test", "a.test"]
    assert names(r[11]) == ["b.test"]
    assert names(r[12]) == ["ads.test"]
    assert names(r[13]) == ["b.test", "a.test", "a.test"]
    assert names(r[14]) == ["a.test", "a.test"]
    by_name = [(e[2], c, b) for e, c, b in unpack(r[15])[0]]
    assert by_name == [("b.test", 1, 0), ("a.test", 2, 0), ("ads.test", 1, 1)]
    assert [e[2] for e, c, b in unpack(r[16])[0]] == ["a.test"]
    assert names(r[18]) == []


def test_watchers_get_the_live_log_in_batches(live, dns):
    write_settings(live, enabled=True, custom_servers=True, servers=[dns.addr])
    r = live.run("signals", "call :1.5 Watch -", "call :1.7 Watch -", "signals", f"uq {query('a.test')}",
                 f"uq {query('b.test')}", "timers on_log_batch", "fire on_log_batch", "signals",
                 "fire on_log_batch", "signals")
    assert r[3] == "signals watch::1.5;watch::1.7"
    assert r[6] == "timers 500"
    # Each watcher by name, nobody else: the log is private
    assert sorted(r[8].split(" ")[1].split(";")) == [":1.5>Logged:2", ":1.7>Logged:2"]
    assert r[10] == "signals -"


def test_a_kept_log_comes_back_after_a_restart(live, dns):
    gi_glib()
    write_settings(live, enabled=True, custom_servers=True, servers=[dns.addr], keep_log=True,
                   log_size=100)
    live.run("app app/x", f"uq {query('a.test')}", "quit", quits=True)
    r = live.run(LOG_ALL, f"uq {query('b.test')}", LOG_ALL)
    old, = log_entries(r[0])
    assert (old["name"], old["app"], old["kind"]) == ("a.test", "app/x", "phone")
    new = log_entries(r[2])[0]
    assert new["id"] == old["id"] + 1


def test_a_broken_log_file_reads_as_far_as_it_goes(live):
    GLib = gi_glib()
    entries = [(i, 0, f"n{i}.test", 1, kind, "", status, "", "", "", "", 0, 0, False, "")
               for i, (kind, status) in enumerate([("hotspot", 4), ("usb", 99), ("nonsense", 0)], 1)]
    v = GLib.Variant("a(uxsqssussssuqbs)", entries)
    write_settings(live, keep_log=True, log_size=100)
    (live.state / "querylog").write_bytes(v.get_data_as_bytes().get_data())
    r = live.run(LOG_ALL)
    got = [(e["name"], e["kind"], e["status"]) for e in log_entries(r[0])]
    # An unknown status reads as passed, an unknown kind as the phone
    assert got == [("n3.test", "phone", 0), ("n2.test", "usb", 0), ("n1.test", "hotspot", 4)]


# ---- More of the same, at the edges ----

def test_state_that_cannot_be_written_is_reported(live, dns):
    write_settings(live, enabled=True, custom_servers=True, servers=[dns.addr])
    try:
        r = live.run(f"uq {query('a.test')}", f"chmod {live.state} 500", "call :1.5 AddRule ('ads.test',)",
                     "call :1.5 AddList ('oisd-big', '')", "set :1.5 BlockMode 'nxdomain'",
                     "call :1.5 ClearStats -", f"chmod {live.state} 700")
    finally:
        os.chmod(live.state, 0o700)
    for name in ("rules", "lists", "settings", "stats"):
        assert f"Cannot save {live.state}/{name}" in r.stderr, name
    # The rule holds anyway, until the next start
    assert unpack(r[2]) == ("||ads.test^",)


def test_odd_statistics_files(live, dns):
    h = now_hour()
    day = h * 3600 // 86400
    live.state.mkdir()
    (live.state / "stats").write_bytes(b"\x01\0")
    r = live.run("call :1.5 GetStats (uint32 1,)")
    assert stats(r[0])["queries"] == 0
    # Version 1, two days: one without blocked names, one whose hours are all of another day
    stats_file(live, 1, [(h - 48, 1, 1, 0, 0, 1), (h, 2, 0, 0, 0, 1)],
               [(day - 1, {"x.test": 1}, {"gone.test": 1}, {}, {}), (day, {"a.test": 2}, {}, {}, {})])
    r = live.run("call :1.5 GetStats (uint32 90,)", "call :1.5 GetNamesOf (uint32 1, '\tphone\t')")
    assert stats(r[0])["top-blocked"] == []
    assert unpack(r[1]) == ([],)


def test_statistics_of_one_day_beyond_8_mb_lose_the_blocked_names_of_its_hours(live):
    h = now_hour()
    day = h * 3600 // 86400
    names = {f"{i}-" + "x" * 200 + ".test": 1 for i in range(42000)}
    stats_file(live, 4, [(h, 1, 1, 0, 0, 1, {"ads.test": 1})], [(day, names, {}, {}, {}, {}, {})])
    r = live.run("call :1.5 GetStats (uint32 1,)", "call :1.5 ClearStats -", timeout=180)
    s = stats(r[0])
    assert s["top-blocked"] == [] and s["blocked"] == 1 and len(s["top-domains"]) == 20


def test_at_most_32_servers_per_day_in_the_statistics(live, dns):
    h = now_hour()
    day = h * 3600 // 86400
    ups = {f"192.0.2.{i}": (1, 10) for i in range(32)}
    old_hours = [(h - 30 + i, 1, 0, 0, 0, 1, {}) for i in range(30)]
    stats_file(live, 4, old_hours, [(day, {}, {}, {}, ups, {}, {})])
    write_settings(live, enabled=True, custom_servers=True, servers=[dns.addr], stats_days=1)
    r = live.run(f"uq {query('a.test')}", "show hours", "call :1.5 GetStats (uint32 1,)")
    # One day of hours kept
    assert r[1] == "24 1"
    assert dns.addr not in {u[0] for u in stats(r[2])["upstreams"]}


def test_log_names_by_search(live, dns):
    write_settings(live, enabled=True, custom_servers=True, servers=[dns.addr])
    r = live.run(f"uq {query('a.test')}", f"uq {query('b.test')}",
                 "call :1.5 GetLogNames (uint32 0, uint32 10, 'b.t', uint32 0)")
    assert [e[2] for e, c, b in unpack(r[2])[0]] == ["b.test"]


def test_a_second_pause_replaces_the_first(live):
    write_settings(live, enabled=True)
    r = live.run("call :1.5 Pause (uint32 60,)", "call :1.5 Pause (uint32 999999,)", "timers on_pause_over",
                 "get :1.5 PausedUntil", "call :1.5 Pause (uint32 0,)", "get :1.5 PausedUntil",
                 "timers on_pause_over")
    # A week at most
    assert r[2] == "timers 604800000"
    assert unpack(r[3]) - int(time.time()) in range(604790, 604810)
    assert unpack(r[5]) == 0 and r[6] == "timers -"


def test_a_caller_the_bus_does_not_know_is_refused(live):
    r = live.run("bus caller -1", "call :1.9 GetRules -", system=True, path_env="")
    assert "no such sender" in r[1]


def test_what_introspection_does_not_offer(live):
    # GDBus refuses these itself; the daemon refuses them too
    r = live.run("callraw :1.5 Explode", "getraw :1.5 Nothing")
    # G_DBUS_ERROR_UNKNOWN_METHOD
    assert r[0] == "error g-dbus-error-quark:19: No method Explode"
    assert r[1] == "none -"


def test_switching_a_list_on_fetches_it(live, dns, lists, tls):
    lists.lists["e.txt"] = b"||e.example^\n"
    write_settings(live, enabled=True)
    list_env(live, dns, tls)
    list_file(live, lists, [("e.txt", False)])
    r = live.run("call :1.5 SetListEnabled ('l0', true)", "call :1.5 UpdateLists -", "until lists-idle 20000",
                 "get :1.5 Lists")
    assert unpack(r[3])[0]["rules"] == 1
    # Asked twice while downloading: one download
    assert len(lists.requests) == 1


def test_a_server_that_says_unchanged_without_being_asked(live, dns, lists, tls):
    lists.status["f.txt"] = 304
    write_settings(live, enabled=True)
    list_env(live, dns, tls)
    list_file(live, lists, [("f.txt", True)])
    (live.state / "compiled").mkdir(parents=True)
    (live.state / "compiled" / "l0.bin").write_bytes(b"broken")
    r = live.run("call :1.5 UpdateLists -", "until lists-idle 20000", "get :1.5 Lists")
    lst, = unpack(r[2])
    assert "error" not in lst and lst["updated"] > 0
    assert "List l0:" in r.stderr


def test_a_list_cut_off_or_removed_during_its_body(live, dns, lists, tls):
    lists.lists["g.txt"] = b"||g.example^\n" * 1000
    lists.truncated.add("g.txt")
    lists.lists["h.txt"] = b"||h.example^\n" * 1000
    lists.slow["h.txt"] = 1.5
    write_settings(live, enabled=True)
    list_env(live, dns, tls)
    list_file(live, lists, [("g.txt", True), ("h.txt", True)])
    r = live.run("call :1.5 UpdateLists -", "run 700", "call :1.5 RemoveList ('l1',)", "until lists-idle 20000",
                 "run 1500", "get :1.5 Lists")
    lst, = unpack(r[5])
    assert lst["id"] == "l0" and lst["error"] and lst["rules"] == 0


def test_a_list_with_more_than_2_million_rules_is_refused(live, dns, lists, tls):
    lists.lists["huge.txt"] = b"".join(b"||r%d.t^\n" % i for i in range(2_000_100))
    write_settings(live, enabled=True)
    list_env(live, dns, tls)
    list_file(live, lists, [("huge.txt", True)])
    r = live.run("call :1.5 UpdateLists -", "until lists-idle 120000", "get :1.5 Lists", timeout=600)
    assert unpack(r[2])[0]["error"] == "The list has more than 2 million rules"


def test_firewall_without_logind(live, dns, fake_bin):
    for v in ("iptables-legacy", "ip6tables-legacy", "iptables-legacy-restore", "ip6tables-legacy-restore"):
        fake_bin.stub(v, 'cat > "$0.input" 2>/dev/null; exit 0' if "restore" in v else "exit 1")
    write_settings(live, enabled=True)
    live.env["LIVE_USERS"] = "-"
    live.run("show status", system=True)
    rules = (fake_bin.path / "iptables-legacy-restore.input").read_text()
    # Nobody to redirect, the rest as always
    assert "--uid-owner" not in rules and "PLINIUS_DNS_PRE -i ap+" in rules


def test_a_statistics_file_that_is_no_file(live):
    (live.state / "stats").mkdir(parents=True)
    r = live.run("call :1.5 GetStats (uint32 1,)")
    assert stats(r[0])["queries"] == 0


def test_a_caller_that_leaves_while_being_checked_is_refused(live):
    r = live.run("nocreds", "call :1.5 GetRules -", system=True, path_env="")
    assert "Only an administrator in front of the phone" in r[1]


def test_a_name_that_is_no_domain_is_no_rule(live):
    r = live.run("call :1.5 AddRule ('" + "ä" * 70 + ".test',)")
    assert "Not a domain:" in r[0]
