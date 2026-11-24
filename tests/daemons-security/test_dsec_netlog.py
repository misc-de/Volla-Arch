# plinius-netlog: the connection log behind Settings > Device Security > Connections
# (plinius-netlogd.c through c/dsec_netlog_harness.c). Address scopes, app names from cgroup
# paths (which apps name themselves), the DNS answers read off every interface (anyone on the
# network can send such packets), the counting into SQLite and who may read it. No BPF, no packet
# socket, no bus; the database lives in tmp_path.

import random
import sqlite3
import struct

import pytest

from conftest import PKG
from dsec_helpers import CDIR, build, run_harness

NETLOG = PKG / "plinius-netlog"
NOW = 1800000000


@pytest.fixture(scope="module")
def users(tmp_path_factory):
    """logind's /run/systemd/users for the caller check: 1000 and 1001 (dsec_fakebus.h's
    default caller) are logged in, 32011 over SSH"""
    d = tmp_path_factory.mktemp("netlog-users")
    (d / "1000").write_text("# This is private data. Do not parse.\nNAME=anna\nSTATE=active\n")
    (d / "1001").write_text("NAME=ben\nSTATE=active\n")
    (d / "32011").write_text("NAME=ssh\nSTATE=online\n")
    return d


WHEEL = "wheel:x:998:anna\n"


@pytest.fixture(scope="module")
def group_file(tmp_path_factory):
    """The daemon's /etc/group: wheel is 998, as the fake bus's default caller groups say"""
    f = tmp_path_factory.mktemp("netlog-etc") / "group"
    f.write_text("root:x:0:\n" + WHEEL + "users:x:984:\n")
    return f


@pytest.fixture(scope="module")
def harness(tmp_path_factory, users, group_file):
    return build(tmp_path_factory.mktemp("netlog"), "netlog-harness",
                 [CDIR / "dsec_netlog_harness.c"],
                 flags=[f"-I{NETLOG}", "-Wno-unused-parameter",
                        f'-DNETLOG_LOGIN_USERS="{users}"', f'-DNETLOG_GROUP_FILE="{group_file}"'],
                 pkgs=["gio-unix-2.0", "libbpf", "sqlite3"])


def run(harness, *cmds):
    out = run_harness(harness, "\n".join(cmds) + "\n")
    assert len(out) == len(cmds), out
    return out


def v4(text):
    return bytes(int(x) for x in text.split(".")).hex() + "00" * 12


def v6(text):
    import ipaddress
    return ipaddress.IPv6Address(text).packed.hex()


@pytest.mark.parametrize("family,addr,scope,text", [
    (4, "8.8.8.8", 2, "8.8.8.8"),
    (4, "127.0.0.1", 0, "127.0.0.1"),
    (4, "127.255.0.9", 0, "127.255.0.9"),
    (4, "0.0.0.0", 0, "0.0.0.0"),
    (4, "10.1.2.3", 1, "10.1.2.3"),
    (4, "172.16.0.1", 1, "172.16.0.1"),
    (4, "172.31.255.1", 1, "172.31.255.1"),
    (4, "172.32.0.1", 2, "172.32.0.1"),
    (4, "172.15.0.1", 2, "172.15.0.1"),
    (4, "192.168.100.1", 1, "192.168.100.1"),
    (4, "192.169.0.1", 2, "192.169.0.1"),
    (4, "169.254.1.1", 1, "169.254.1.1"),
    (4, "100.64.0.1", 1, "100.64.0.1"),       # carrier-grade NAT
    (4, "100.127.255.1", 1, "100.127.255.1"),
    (4, "100.128.0.1", 2, "100.128.0.1"),
    (4, "224.0.0.251", 1, "224.0.0.251"),
    (4, "255.255.255.255", 1, "255.255.255.255"),
    (6, "::1", 0, "::1"),
    (6, "::", 0, "::"),
    (6, "::ffff:127.0.0.1", 0, "127.0.0.1"),  # mapped IPv4 counts as IPv4
    (6, "::ffff:8.8.4.4", 2, "8.8.4.4"),
    (6, "::ffff:192.168.100.2", 1, "192.168.100.2"),
    (6, "fe80::1", 1, "fe80::1"),
    (6, "febf::1", 1, "febf::1"),
    (6, "fec0::1", 2, "fec0::1"),
    (6, "fd12:3456::1", 1, "fd12:3456::1"),
    (6, "fc00::1", 1, "fc00::1"),
    (6, "ff02::fb", 1, "ff02::fb"),
    (6, "2a00:1450::1", 2, "2a00:1450::1"),
])
def test_scope_of_an_address(harness, family, addr, scope, text):
    hexaddr = v4(addr) if family == 4 else v6(addr)
    assert run(harness, f"classify {family} {hexaddr}") == [f"{scope} {text}"]


@pytest.mark.parametrize("path,app", [
    ("/user.slice/user-1000.slice/user@1000.service/app.slice/app-gnome-org.gnome.Maps-1234.scope",
     "org.gnome.Maps"),
    ("/user.slice/user-1000.slice/user@1000.service/app.slice/app-flatpak-org.mozilla.firefox-77.scope",
     "org.mozilla.firefox"),
    ("/user.slice/user-1000.slice/user@1000.service/app.slice/app-gnome-firefox-123.scope", "firefox"),
    ("/user.slice/user-1000.slice/user@1000.service/app.slice/app-org.gnome.Weather@x1.service",
     "org.gnome.Weather"),
    ("/user.slice/user-1000.slice/user@1000.service/app.slice/dbus-:1.2-org.gnome.Weather@0.service",
     "org.gnome.Weather"),
    # Split while escaped: a dash systemd escaped is part of the name, no launcher prefix
    ("/user.slice/user-1000.slice/user@1000.service/app.slice/app-gnome\\x2dsession\\x2dmanager@phosh.service",
     "gnome-session-manager"),
    ("/user.slice/user-1000.slice/user@1000.service/app.slice/app-my\\x2dtool-123.scope", "my-tool"),
    ("/user.slice/user-1000.slice/user@1000.service/app.slice/app-gnome-org.example.my\\x2dapp-9.scope",
     "org.example.my-app"),
    ("/user.slice/user-1000.slice/user@1000.service/app.slice/dbus-:1.2-org.example.my\\x2dapp@0.service",
     "org.example.my-app"),
    # The heuristic of app_from_cgroup: a first part without a dot is a launcher prefix
    ("/user.slice/user-1000.slice/user@1000.service/app.slice/app-my-tool-123.scope", "tool"),
    ("/system.slice/foo\\x2dbar.service", "service:foo-bar"),
    ("/system.slice/foo\\x40.service", "service:foo@"),     # only a literal '@' ends a template
    # An escaped NUL does not cut the name short into another app's
    ("/user.slice/user-1000.slice/user@1000.service/app.slice/app-org.gnome.Maps\\x00x-1.scope", ""),
    ("dbus-:1.2-org.gnome.Maps\\x00@0.service", ""),
    ("/system.slice/foo\\x00bar.service", ""),
    ("/system.slice/NetworkManager.service", "service:NetworkManager"),
    ("/system.slice/system-getty.slice/getty@tty1.service", "service:getty@tty1"),
    ("/system.slice/foo@.service", "service:foo"),
    ("/user.slice/user-1000.slice/session-2.scope", "session"),
    ("/lxc.payload.android/uid_10123/pid_1", "android"),
    ("/lxc.monitor.android", "android"),
    ("/init.scope", "init.scope"),
    ("/", ""),
    ("", ""),
    ("app-.scope", ""),
    ("app-.service", ""),
    ("dbus-:.service", "service:dbus-:"),
    ("dbus-:1.2-.service", ""),
    ("a\\x4", "a\\x4"),
    ("\\x00.service", ""),                      # an escaped NUL ends the name
])
def test_app_from_cgroup(harness, path, app):
    assert run(harness, "app " + path) == [app]


# ---- DNS answers off the network ----

def wname(name):
    return b"".join(bytes([len(l)]) + l.encode() for l in name.split(".")) + b"\0"


def response(qname, records, flags=0x8180, qd=1):
    m = struct.pack(">HHHHHH", 1, flags, qd, len(records), 0, 0)
    m += wname(qname) + struct.pack(">HH", 1, 1)
    for rtype, rdata in records:
        m += b"\xc0\x0c" + struct.pack(">HHIH", rtype, 1, 60, len(rdata)) + rdata
    return m


def test_answer_names_the_address_by_the_asked_name(harness):
    m = response("WWW.Example.COM", [(5, wname("cdn.test")), (1, bytes([93, 184, 216, 34])),
                                     (28, bytes(15) + b"\1")])
    out = run(harness, f"dns {m.hex()}", "name 93.184.216.34", "name ::1", "name 1.1.1.1")
    assert out == ["names 2", "www.example.com", "www.example.com", "-"]


@pytest.mark.parametrize("msg", [
    response("a.test", [(1, bytes(4))], flags=0x0100),    # a query, not a response
    response("a.test", [(1, bytes(4))], qd=2),
    response("a.test", [(1, b"\1\2\3")]),                 # A with 3 bytes
    response("a.test", [(28, bytes(4))]),
    response("a.test", [(1, bytes(4))])[:-1],             # cut short
    b"\0" * 11,
    b"",
])
def test_answers_that_name_nothing(harness, msg):
    assert run(harness, f"dns {msg.hex() or '00'}")[0] == "names 0"


@pytest.mark.parametrize("msg,off,result", [
    (b"\x01a\x02bc\x00", 0, "6 a.bc"),
    (b"\x00", 0, "1 "),
    (b"\x01a\xc0\x00", 0, "-1"),                          # loop
    (b"\x01a", 0, "-1"),                                  # no end
    (b"\x05ab", 0, "-1"),                                 # label past the end
    (b"\x40" + b"a" * 64 + b"\x00", 0, "-1"),             # longer than 63
    (b"\x80a\x00", 0, "-1"),
    (b"\xc0", 0, "-1"),
    # 255 characters still fit the 256-byte buffer, one label more does not
    ((b"\x3f" + b"a" * 63) * 4 + b"\x00", 0, f"257 {'.'.join(['a' * 63] * 4)}"),
    ((b"\x3f" + b"a" * 63) * 4 + b"\x01a\x00", 0, "-1"),
    ((b"\x3f" + b"a" * 63) * 3 + b"\x3d" + b"a" * 61 + b"\x00", 0,
     f"255 {'.'.join(['a' * 63] * 3 + ['a' * 61])}"),
])
def test_dns_name(harness, msg, off, result):
    assert run(harness, f"dnsname {msg.hex()} {off}") == [result]


@pytest.mark.parametrize("hops,ok", [(16, True), (17, False)])
def test_dns_name_pointer_hops(harness, hops, ok):
    m = b""
    for i in range(hops):
        m += struct.pack(">H", 0xc000 | (2 * (i + 1)))
    m += b"\x01a\x00"
    out, = run(harness, f"dnsname {m.hex()} 0")
    assert out == ("2 a" if ok else "-1")


def test_mutated_answers(harness, tmp_path):
    rng = random.Random(4711)
    seeds = [response("a.example.com", [(5, b"\x03cdn\xc0\x0e"), (1, bytes(4)), (28, bytes(16))]),
             response("b.test", [(1, bytes([1, 2, 3, 4]))] * 5)]
    data = bytearray()
    count = 0
    for seed in seeds:
        for _ in range(3000):
            m = bytearray(seed)
            for _ in range(rng.randint(1, 5)):
                op = rng.randrange(4)
                if op == 0:
                    m[rng.randrange(len(m))] = rng.randrange(256)
                elif op == 1:
                    del m[rng.randrange(len(m)):]
                elif op == 2 and len(m) > 13:
                    i = rng.randrange(12, len(m) - 1)
                    m[i:i + 2] = struct.pack(">H", 0xc000 | rng.randrange(len(m) + 8))
                elif op == 3 and len(m) > 12:
                    i = rng.choice([4, 6])
                    m[i:i + 2] = struct.pack(">H", rng.choice([1, 2, 0xff, 0xffff]))
                if not m:
                    break
            data += struct.pack(">H", len(m)) + m
            count += 1
    f = tmp_path / "corpus"
    f.write_bytes(bytes(data))
    assert run(harness, f"fuzz {f}") == [f"done {count}"]


def test_name_cache_starts_over_when_full(harness):
    cmds = []
    for i in range(8193):
        m = response(f"h{i}.test", [(1, bytes([10, 0, i // 256, i % 256]))])
        cmds.append(f"dns {m.hex()}")
    out = run(harness, *cmds)
    assert out[8191] == "names 8192"
    assert out[8192] == "names 1"


# ---- Counting ----

def test_events_are_counted_per_minute_with_host_names(harness, tmp_path):
    db = tmp_path / "netlog.db"
    dns = response("dns.google", [(1, bytes([8, 8, 8, 8]))])
    out = run(harness, f"db {db}", f"time {NOW}", f"dns {dns.hex()}",
              "cgapp 77 org.gnome.Maps",
              f"event 77 1000 4 {v4('8.8.8.8')} 443 6",
              f"event 77 1000 4 {v4('8.8.8.8')} 443 6",
              f"event 77 1000 4 {v4('192.168.1.1')} 80 6",
              f"event 77 1000 4 {v4('127.0.0.1')} 631 6",        # loopback: never recorded
              f"event 77 1000 6 {v6('::1')} 631 6",
              "pending", "flush", "pending", f"time {NOW + 30}",
              "rows 900", "summary 900")
    assert out[9] == "2"
    assert out[11] == "0"
    rows = out[13]
    start = NOW // 60 * 60
    assert rows == ("([('org.gnome.Maps', 'dsec-comm-name16', 6, 443, '8.8.8.8', 'dns.google', 2, 2, "
                    f"{start}, {NOW + 30}), ('org.gnome.Maps', 'dsec-comm-name16', 6, 80, "
                    f"'192.168.1.1', '', 1, 1, {start}, {NOW + 30})],)")
    assert out[14] == "(2, 1, 1, 1)"


def test_unknown_cgroup_is_unknown(harness, tmp_path):
    db = tmp_path / "netlog.db"
    out = run(harness, f"db {db}", f"time {NOW}", f"event 999 1000 4 {v4('1.1.1.1')} 53 17",
              "flush", "rows 60")
    assert "'unknown', 'dsec-comm-name16', 17, 53, '1.1.1.1'" in out[-1]


def test_app_names_cannot_break_the_database(harness, tmp_path):
    # Apps choose their own unit names; they only ever reach SQLite as bound values
    db = tmp_path / "netlog.db"
    evil = "x');DROP/**/TABLE/**/conn;--"
    out = run(harness, f"db {db}", f"time {NOW}", f"cgapp 5 {evil}",
              f"event 5 1000 4 {v4('1.1.1.1')} 443 6", "flush",
              f"event 5 1000 4 {v4('1.1.1.1')} 443 6", "flush", "rows 300")
    assert f"'{evil}'" in out[-1] or evil in out[-1]
    assert "'1.1.1.1', '', 2, 2, " in out[-1]


def test_views_cover_their_span(harness, tmp_path):
    db = tmp_path / "netlog.db"
    out = run(harness, f"db {db}", f"time {NOW}", "cgapp 1 app.a",
              f"event 1 1000 4 {v4('1.1.1.1')} 443 6", "flush",
              f"time {NOW + 3 * 3600}", "rows 3600", "rows 86400", "summary 3600",
              f"time {NOW + 9 * 86400}", "rows 604800")
    # Three hours later the 60 minute view is empty, the day view still has it (hour buckets)
    assert out[6] == "([],)"
    assert "'app.a'" in out[7]
    assert out[8] == "(0, 0, 0, 0)"
    # After eight days it is out of every view
    assert out[10] == "([],)"


# ---- Who may read it ----

@pytest.mark.parametrize("caller,allowed", [
    (0, True), (1000, True), (32011, True),
    (999, False), (1, False), (-1, False),
    (65534, False),   # nobody: no session
    (61184, False),   # a DynamicUser service
    (1004, False),    # a regular user without a session
    (1002, False),    # lingering: a user manager, no session
    (1003, False),    # a broken state file
])
def test_caller_check(harness, users, tmp_path, caller, allowed):
    (users / "1002").write_text("NAME=bob\nSTATE=lingering\n")
    (users / "1003").write_text("STATE=activeX\n")
    db = tmp_path / "netlog.db"
    out = run(harness, f"db {db}", f"bus caller {caller}", "call Summary 900", "call Query 900")
    if allowed:
        assert out[2] == "ok (0, 0, 0, 0)"
        assert out[3] == "ok ([],)"
    else:
        assert out[2] == out[3] == "error g-dbus-error-quark:9: Not allowed"


def test_seconds_are_clamped(harness, tmp_path):
    db = tmp_path / "netlog.db"
    out = run(harness, f"db {db}", f"time {NOW}", "cgapp 1 app.a",
              f"event 1 1000 4 {v4('1.1.1.1')} 443 6", "call Query 0", "call Query 4294967295")
    # 0 seconds still shows the current minute; a huge span is the eight days kept
    assert "'app.a'" in out[4] and "'app.a'" in out[5]


# ---- What each caller sees ----

def two_accounts(db):
    """One connection of uid 1000 (an administrator) and two of uid 1001 (a second account)"""
    return [f"db {db}", f"time {NOW}", "cgapp 1 app.admin", "cgapp 2 app.second",
            f"event 1 1000 4 {v4('1.1.1.1')} 443 6",
            f"event 2 1001 4 {v4('9.9.9.9')} 443 6", f"event 2 1001 4 {v4('9.9.9.9')} 443 6",
            "flush"]


def test_rows_and_summary_per_uid(harness, tmp_path):
    out = run(harness, *two_accounts(tmp_path / "netlog.db"),
              "rows 900", "rows 900 1001", "rows 900 1000", "rows 900 4711",
              "summary 900", "summary 900 1001", "summary 900 4711")
    assert "'app.admin'" in out[8] and "'app.second'" in out[8]
    assert "'app.second'" in out[9] and "'app.admin'" not in out[9]
    assert "'app.admin'" in out[10] and "'app.second'" not in out[10]
    assert out[11] == "([],)"
    assert out[12] == "(3, 2, 2, 0)"
    assert out[13] == "(2, 1, 1, 0)"
    assert out[14] == "(0, 0, 0, 0)"


def two_accounts_same_destination(db):
    """The same app to the same address from two accounts: once from 1000, twice from 1001"""
    return [f"db {db}", f"time {NOW}", "cgapp 1 org.gnome.Maps",
            f"event 1 1000 4 {v4('1.1.1.1')} 443 6", f"event 1 1001 4 {v4('1.1.1.1')} 443 6",
            f"event 1 1001 4 {v4('1.1.1.1')} 443 6", "flush",
            "summary 900 1000", "summary 900 1001", "summary 900"]


def test_counts_of_two_accounts_stay_apart(harness, tmp_path):
    # Two rows of the database, so neither account sees how often the other one connected
    db = tmp_path / "netlog.db"
    out = run(harness, *two_accounts_same_destination(db))
    assert out[-3:] == ["(1, 1, 1, 0)", "(2, 1, 1, 0)", "(3, 1, 1, 0)"]
    # A minute and an hour bucket per account
    with sqlite3.connect(db) as c:
        assert sorted(c.execute("SELECT span, uid, count FROM conn")) == [
            (60, 1000, 1), (60, 1001, 2), (3600, 1000, 1), (3600, 1001, 2)]


# ---- The database of an older version ----

OLD_SCHEMA = ("CREATE TABLE conn ("
              "  span INTEGER NOT NULL, start INTEGER NOT NULL, app TEXT NOT NULL, exe TEXT NOT NULL,"
              "  uid INTEGER, proto INTEGER NOT NULL, addr TEXT NOT NULL, port INTEGER NOT NULL,"
              "  host TEXT, scope INTEGER NOT NULL, count INTEGER NOT NULL,"
              "  PRIMARY KEY (span, start, app, exe, proto, addr, port)) WITHOUT ROWID")
MINUTE = NOW // 60 * 60
HOUR = NOW // 3600 * 3600
# The executable as the harness's events name it, so a new count of another account would meet
# the old row in a key without the uid
COMM = "dsec-comm-name16"
OLD_ROWS = [
    (60, MINUTE, "org.gnome.Maps", COMM, 1000, 6, "1.1.1.1", 443, "one.test", 2, 5),
    (3600, HOUR, "org.gnome.Maps", COMM, 1000, 6, "1.1.1.1", 443, "one.test", 2, 5),
    (60, MINUTE, "app.second", "second", 1001, 17, "192.168.1.9", 53, None, 1, 2),
    (60, MINUTE, "app.nobody", "x", None, 6, "9.9.9.9", 443, None, 2, 1),    # no uid
]


def schema(db):
    with sqlite3.connect(db) as c:
        key = [r[1] for r in sorted(c.execute("PRAGMA table_info(conn)"), key=lambda r: r[5]) if r[5]]
        tables = sorted(r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'"))
        rows = sorted(c.execute("SELECT * FROM conn"), key=repr)
    return key, tables, rows


NEW_KEY = ["span", "start", "app", "exe", "uid", "proto", "addr", "port"]


def old_database(path, rows=OLD_ROWS, table=OLD_SCHEMA):
    with sqlite3.connect(path) as c:
        c.execute(table)
        for r in rows:
            c.execute(f"INSERT INTO conn VALUES ({', '.join('?' * len(r))})", r)
    return path


def test_an_old_database_is_converted_with_its_rows(harness, tmp_path):
    db = old_database(tmp_path / "netlog.db")
    out = run(harness, f"db {db}", f"time {NOW + 30}", "summary 900", "summary 900 1000", "summary 900 1001",
              "summary 900 4294967295", "rows 86400 1000")
    key, tables, rows = schema(db)
    assert key == NEW_KEY
    assert tables == ["conn"]                       # nothing left of the conversion
    # Every row kept; the one without a uid is -1 now, which no account is
    assert rows == sorted([r[:4] + (-1 if r[4] is None else r[4],) + r[5:] for r in OLD_ROWS], key=repr)
    assert out[2:6] == ["(6, 2, 2, 2)", "(5, 1, 1, 0)", "(0, 0, 0, 2)", "(0, 0, 0, 0)"]
    assert out[6] == (f"([('org.gnome.Maps', 'dsec-comm-name16', 6, 443, '1.1.1.1', 'one.test', 2, 5, {HOUR}, "
                      f"{NOW + 30})],)")


def test_after_the_conversion_two_accounts_stay_apart(harness, tmp_path):
    db = old_database(tmp_path / "netlog.db", rows=[])
    out = run(harness, *two_accounts_same_destination(db))
    assert out[-3:] == ["(1, 1, 1, 0)", "(2, 1, 1, 0)", "(3, 1, 1, 0)"]


def test_an_old_row_and_a_new_count_of_another_account_stay_apart(harness, tmp_path):
    db = old_database(tmp_path / "netlog.db")
    out = run(harness, f"db {db}", f"time {NOW}", "cgapp 1 org.gnome.Maps",
              f"event 1 1001 4 {v4('1.1.1.1')} 443 6", "flush", "summary 900 1000", "summary 900 1001")
    # 1001's new connection is a row of its own beside 1000's old one to the same place
    assert out[-2:] == ["(5, 1, 1, 0)", "(1, 1, 1, 2)"]
    with sqlite3.connect(db) as c:
        assert sorted(c.execute("SELECT uid, count FROM conn WHERE span = 60 AND app = 'org.gnome.Maps'")) == [
            (1000, 5), (1001, 1)]


def test_a_converted_database_is_not_converted_again(harness, tmp_path):
    db = old_database(tmp_path / "netlog.db")
    run(harness, f"db {db}", f"time {NOW}", "cgapp 1 org.gnome.Maps",
        f"event 1 1001 4 {v4('1.1.1.1')} 443 6", "flush")
    before = schema(db)
    # Opened again (the daemon's next start), twice: the same table, the same rows
    assert run(harness, f"db {db}", f"db {db}", "summary 900") == ["ok", "ok", "(7, 2, 2, 2)"]
    assert schema(db) == before


def test_a_new_database_has_the_uid_in_the_key(harness, tmp_path):
    db = tmp_path / "netlog.db"
    assert run(harness, f"db {db}", f"db {db}") == ["ok", "ok"]
    assert schema(db) == (NEW_KEY, ["conn"], [])


def test_a_database_that_cannot_be_converted_stays_as_it_was(harness, tmp_path):
    # Not a table of any version (no uid at all): the daemon does not start, the rows stay
    db = old_database(tmp_path / "netlog.db", rows=[(60, MINUTE, "a", "b", 6, "1.1.1.1", 443, None, 2, 1)],
                      table=OLD_SCHEMA.replace("  uid INTEGER,", ""))
    with sqlite3.connect(db) as c:
        before = sorted(c.execute("SELECT * FROM conn"))
    out = run(harness, f"db {db}")
    assert out == ["error"]
    with sqlite3.connect(db) as c:
        assert sorted(c.execute("SELECT * FROM conn")) == before
        assert [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")] == ["conn"]


@pytest.mark.parametrize("old", [True, False])
def test_temporary_files_stay_in_memory(harness, tmp_path, old):
    # The AppArmor profile grants no /var/tmp: the conversion's statement journal there failed
    # with "unable to open database file" and the daemon did not start (2026-11-20)
    db = old_database(tmp_path / "netlog.db") if old else tmp_path / "netlog.db"
    assert run(harness, f"db {db}", "tempstore") == ["ok", "2"]
    assert not list(tmp_path.glob("etilqs_*"))


@pytest.mark.parametrize("bus,sees", [
    (["bus caller 0", "bus groups 0"], "all"),            # root
    (["bus caller 1000", "bus groups 1000,998"], "all"),  # an administrator
    (["bus caller 1001", "bus groups 1001,984"], "own"),  # a second account
    (["bus caller 1001", "bus groups none"], "own"),
    (["bus caller 1001", "bus groups -"], "own"),         # the bus gave no groups
    # A group id that is wheel's on another system does not count: only this group file
    (["bus caller 1001", "bus groups 10"], "own"),
])
def test_caller_sight(harness, tmp_path, bus, sees):
    out = run(harness, *two_accounts(tmp_path / "netlog.db"), *bus, "call Query 900",
              "call Summary 900")
    rows, summary = out[-2], out[-1]
    if sees == "all":
        assert "'app.admin'" in rows and "'app.second'" in rows
        assert summary == "ok (3, 2, 2, 0)"
    else:
        assert "'app.second'" in rows and "'app.admin'" not in rows
        assert summary == "ok (2, 1, 1, 0)"


@pytest.mark.parametrize("content", [
    "root:x:0:\n",                     # no wheel
    "wheel:x::anna\n",                 # wheel without an id
    "wheel:x:998x:\n",                 # not a number
    "wheel:x:99999999999:\n",          # out of range
    "wheel\n",                         # cut off
    None,                              # no group file
])
def test_without_wheel_nobody_is_an_administrator(harness, tmp_path, group_file, content):
    try:
        if content is None:
            group_file.unlink()
        else:
            group_file.write_text(content)
        out = run(harness, *two_accounts(tmp_path / "netlog.db"),
                  "bus caller 1000", "bus groups 1000,998", "call Query 900", "bus caller 0",
                  "call Summary 900")
        # The administrator sees only their own connections, root still all
        assert "'app.admin'" in out[-3] and "'app.second'" not in out[-3]
        assert out[-1] == "ok (3, 2, 2, 0)"
    finally:
        group_file.write_text("root:x:0:\n" + WHEEL + "users:x:984:\n")
