# plinius-dnsfilter: the DNS wire format (dnsmsg.c), which reads messages from any client of the
# phone, hotspot and USB, and from servers, and the block-list parser and compiled sets
# (filter.c), which read lists from the network. Table-driven edge cases and a seeded,
# deterministic mutation run, all under AddressSanitizer/UBSan where the host has them.

import random
import struct

import pytest

from conftest import PKG
from dsec_helpers import CDIR, build, run_harness

DNSFILTER = PKG / "plinius-dnsfilter"
CFLAGS = ["-Wno-unused-parameter", "-Wno-missing-field-initializers"]


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    return build(tmp_path_factory.mktemp("dnsmsg"), "dnsmsg-harness",
                 [CDIR / "dsec_dnsmsg_harness.c", DNSFILTER / "dnsmsg.c", DNSFILTER / "filter.c"],
                 flags=CFLAGS, pkgs=["gio-2.0"])


def run(harness, *cmds):
    out = run_harness(harness, "\n".join(cmds) + "\n", timeout=120)
    assert len(out) == len(cmds), out
    return out


# ---- Message builders ----

def wname(name):
    if isinstance(name, bytes):
        return name
    out = b""
    for label in name.split(".") if name else []:
        out += bytes([len(label)]) + label.encode()
    return out + b"\0"


def header(id=0x1234, flags=0x0100, qd=1, an=0, ns=0, ar=0):
    return struct.pack(">HHHHHH", id, flags, qd, an, ns, ar)


def question(name, qtype=1, qclass=1):
    return wname(name) + struct.pack(">HH", qtype, qclass)


def rr(name, rtype, ttl, rdata, rclass=1):
    return wname(name) + struct.pack(">HHIH", rtype, rclass, ttl, len(rdata)) + rdata


def opt(size=4096, do=False, rdata=b""):
    return b"\0" + struct.pack(">HHIH", 41, size, 0x8000 if do else 0, len(rdata)) + rdata


def query(name="example.com", qtype=1, **kw):
    return header(**kw) + question(name, qtype)


PTR = b"\xc0\x0c"   # pointer to the question's name


# ---- Queries from clients ----

def test_plain_query_and_what_goes_out(harness):
    out, = run(harness, "query " + query("Www.Example.COM").hex())
    assert out == ("ok id=4660 name=www.example.com odd=0 type=1 class=1 end=33 edns=0 udp=0 do=0"
                   " fwd=same/1/48879/do=0 reply0=match/rcode=0/answers=1/qd=1"
                   " reply3=match/rcode=3/answers=0/qd=1")


def test_edns_size_and_do_bit(harness):
    m = header(ar=1) + question("a.test", 28) + opt(100, True)
    out, = run(harness, "query " + m.hex())
    # Below 512 counts as 512 (RFC 6891); the client's DO bit goes out with the question
    assert " edns=1 udp=512 do=1 fwd=same/28/48879/do=1 " in out


@pytest.mark.parametrize("label,text", [
    (b"ex.mple", "ex\\046mple"),     # a dot inside a label is not a separator
    (b"a\\b", "a\\092b"),             # a backslash cannot fake an escape
    (b"a\\046b", "a\\092046b"),
    (b"nul\0x", "nul\\000x"),
    (b"sp ace", "sp\\032ace"),
    (bytes([0xc3, 0xbc]), "\\195\\188"),
])
def test_odd_label_bytes_are_escaped_and_forwarded_unchanged(harness, label, text):
    m = header() + bytes([len(label)]) + label + wname("test") + struct.pack(">HH", 1, 1)
    out, = run(harness, "query " + m.hex())
    assert f" name={text}.test odd=1 " in out
    # The forwarded question has the same bytes again, not a "cleaned" other name
    assert " fwd=same/" in out


@pytest.mark.parametrize("labels,result", [
    ([63, 63, 63, 61], "ok"),        # 254 bytes + root: the longest legal name
    ([63, 63, 63, 63], "fail"),      # 256 + root
    ([1] * 127, "ok"),               # 254 bytes of one-letter labels
    ([1] * 128, "fail"),
])
def test_name_length_limit(harness, labels, result):
    name = ".".join("x" * n for n in labels)
    out, = run(harness, "query " + query(name).hex())
    assert out.split()[0] == result
    if result == "ok":
        assert " fwd=same/" in out


def test_name_one_byte_too_long_is_never_forwarded(harness):
    # 255 wire bytes before the root are accepted by the reader but cannot be written back:
    # no question goes upstream (the daemon answers SERVFAIL), the reply is FORMERR
    name = ".".join("x" * n for n in [63, 63, 63, 62])
    out, = run(harness, "query " + query(name).hex())
    assert out.startswith("ok ")
    assert " fwd=NULL " in out
    assert " reply0=header-only/rcode=1/qd=0" in out


@pytest.mark.parametrize("msg,rcode", [
    (b"", -1),
    (b"\x12\x34\x01", -1),                                    # shorter than a header
    (header()[:11], -1),
    (query(flags=0x8180), -1),                                # an answer: dropped, not answered
    (query(flags=0x2900), 4),                                 # opcode UPDATE: NOTIMP
    (query(flags=0x1100), 4),                                 # opcode STATUS
    (header(qd=0), 1),
    (header(qd=2) + question("a.test") + question("b.test"), 1),
    (header() + b"\x03abc", 1),                               # name cut short
    (header() + wname("a.test") + b"\x00", 1),                # type/class cut short
    (header() + b"\x40abc\x00\x00\x01\x00\x01", 1),           # extended label type
    (header() + b"\x80abc\x00\x00\x01\x00\x01", 1),
    (header() + b"\xc0\x0c\x00\x01\x00\x01", 1),              # pointer to itself
    (header() + b"\xc0", 1),                                  # half a pointer
    (header() + b"\xc0\xff\x00\x01\x00\x01", 1),              # pointer past the end
    (header(ar=1) + question("a.test"), 1),                   # announced OPT missing
    (header(ar=1) + question("a.test") + opt()[:-1], 1),
    (header(an=1) + question("a.test") + PTR + struct.pack(">HHIH", 1, 1, 1, 10) + b"\1\2\3",
     1),                                                      # rdlength beyond the end
    (header(an=65535, ns=65535) + question("a.test"), 1),
    (header(ar=1) + question("a.test") + b"\0" + struct.pack(">HHIH", 41, 4096, 0, 4000), 1),
])
def test_malformed_queries(harness, msg, rcode):
    out, = run(harness, "query " + msg.hex())
    assert out == f"fail rcode={rcode}"


def test_pointer_chains(harness):
    # A forward pointer is fine; a loop through two pointers and a chain longer than 64 are not
    m = header() + b"\xc0\x0e" + b"\x01a\x00" + struct.pack(">HH", 1, 1)
    loop = header() + b"\xc0\x0e\xc0\x0c" + struct.pack(">HH", 1, 1)
    chain = bytearray(header())
    hops = 70
    for i in range(hops):
        chain += struct.pack(">H", 0xc000 | (12 + 2 * (i + 1)))
    chain += b"\x01a\x00" + struct.pack(">HH", 1, 1)
    out = run(harness, "name " + m.hex() + " 12", "name " + loop.hex() + " 12",
              "name " + bytes(chain).hex() + " 12", "skip " + m.hex() + " 12")
    assert out == ["14 a 0", "-1", "-1", "14"]


def test_read_name_output_buffer_is_respected(harness):
    # 63 odd bytes become 252 characters: the text never runs past DNS_NAME_TEXT
    label = bytes(range(128, 191))
    m = header() + (bytes([63]) + label) * 3 + bytes([61]) + label[:61] + b"\0"
    out, = run(harness, "name " + m.hex() + " 12")
    end, text, odd = out.split()
    assert int(end) == len(m) and odd == "1" and len(text) == 4 * 250 + 3


def test_simple_query_rejects_names_it_cannot_write(harness):
    out = run(harness, "simple dns.example 1", "simple a..b 1", "simple " + "x" * 64 + ".test 1",
              "simple " + ".".join(["x" * 63] * 4) + " 1")
    assert out[0].startswith("1234010000010000000000010364")
    assert out[1:] == ["NULL", "NULL", "NULL"]


# ---- Answers from servers ----

def answer(name="a.test", records=(), flags=0x8180, ns=(), ar=(), qtype=1):
    return (header(flags=flags, an=len(records), ns=len(ns), ar=len(ar)) + question(name, qtype)
            + b"".join(records) + b"".join(ns) + b"".join(ar))


def soa(ttl, minimum):
    rdata = wname("ns.test") + wname("host.test") + struct.pack(">IIIII", 1, 2, 3, 4, minimum)
    return rr(wname("test"), 6, ttl, rdata)


@pytest.mark.parametrize("msg,expected", [
    (answer(records=[rr(PTR, 1, 300, bytes([1, 2, 3, 4])), rr(PTR, 1, 60, bytes([5, 6, 7, 8]))]),
     "ok rcode=0 name=a.test type=1 answers=2 ttl=60 cnames= summary=1.2.3.4, 5.6.7.8"),
    (answer(records=[rr(PTR, 28, 30, bytes(15) + b"\1")]),
     "ok rcode=0 name=a.test type=1 answers=1 ttl=30 cnames= summary=::1"),
    # A records with the wrong length are counted, never printed as an address
    (answer(records=[rr(PTR, 1, 30, b"\1\2\3")]),
     "ok rcode=0 name=a.test type=1 answers=1 ttl=30 cnames= summary="),
    (answer(records=[rr(PTR, 5, 30, wname("T.Track.test"))]),
     "ok rcode=0 name=a.test type=1 answers=1 ttl=30 cnames=t.track.test summary=t.track.test"),
    # Negative answer: min(SOA TTL, SOA minimum), RFC 2308
    (answer(flags=0x8183, ns=[soa(3600, 120)]),
     "ok rcode=3 name=a.test type=1 answers=0 ttl=120 cnames= summary="),
    (answer(flags=0x8183, ns=[soa(60, 900)]),
     "ok rcode=3 name=a.test type=1 answers=0 ttl=60 cnames= summary="),
    # Additional records do not decide how long the answer lives
    (answer(records=[rr(PTR, 1, 300, bytes(4))], ar=[rr(wname("x.test"), 1, 5, bytes(4))]),
     "ok rcode=0 name=a.test type=1 answers=1 ttl=300 cnames= summary=0.0.0.0"),
    (answer(records=[rr(PTR, 1, 300, bytes(4))], ar=[opt()]),
     "ok rcode=0 name=a.test type=1 answers=1 ttl=300 cnames= summary=0.0.0.0"),
    # Cut short with TC: what is there is still read
    (answer(flags=0x8380, records=[rr(PTR, 1, 300, bytes(4)), rr(PTR, 1, 300, bytes(4))])[:-10],
     "ok rcode=0 name=a.test type=1 answers=1 ttl=300 cnames= summary=0.0.0.0"),
    (answer(records=[rr(PTR, 1, 300, bytes(4)), rr(PTR, 1, 300, bytes(4))])[:-10], "fail"),
    (answer(flags=0x0100), "fail"),                       # not an answer
    (header(flags=0x8180, qd=0), "fail"),
    (header(flags=0x8180, qd=2) + question("a.test") + question("b.test"), "fail"),
    (header(flags=0x8180) + b"\xc0\x0c", "fail"),
])
def test_answers(harness, msg, expected):
    assert run(harness, "answer " + msg.hex()) == [expected]


def test_summary_is_cut_for_long_answers(harness):
    records = [rr(PTR, 1, 60, bytes([10, 0, i // 256, i % 256])) for i in range(400)]
    out, = run(harness, "answer " + answer(records=records).hex())
    summary = out.split(" summary=", 1)[1]
    assert out.startswith("ok rcode=0 name=a.test type=1 answers=400 ")
    assert summary.endswith(" …") and len(summary) < 240


def test_addresses_only_from_good_records(harness):
    m = answer(records=[rr(PTR, 1, 60, bytes([1, 1, 1, 1])),
                        rr(PTR, 1, 60, bytes([2, 2, 2, 2]), rclass=3),    # class CHAOS
                        rr(PTR, 1, 60, b"\1\1"),
                        rr(PTR, 28, 60, bytes(16)),
                        rr(PTR, 5, 60, wname("x.test"))])
    nx = answer(flags=0x8183, records=[rr(PTR, 1, 60, bytes([9, 9, 9, 9]))])
    assert run(harness, "addr " + m.hex(), "addr " + nx.hex(),
               "addr " + m[:-30].hex()) == ["addr 1.1.1.1 ::", "addr", "addr 1.1.1.1"]


# ---- Replies to clients (cache, forwarded answers) ----

def test_prepare_ages_ttls_and_removes_opt(harness):
    m = answer(records=[rr(PTR, 1, 300, bytes(4)), rr(PTR, 1, 5, bytes(4))], ar=[opt()])
    out, = run(harness, f"prepare {m.hex()} 4321 10 0 4096")
    assert out.startswith("ok ")
    got = bytes.fromhex(out[3:])
    expected = (header(id=4321, flags=0x8180, an=2) + question("a.test")
                + rr(PTR, 1, 290, bytes(4)) + rr(PTR, 1, 0, bytes(4)))
    assert got == expected


def test_prepare_keeps_opt_for_an_edns_client_and_truncates(harness):
    m = answer(records=[rr(PTR, 1, 300, bytes(4))] * 30, ar=[opt()])
    out, = run(harness, f"prepare {m.hex()} 7 0 1 100")
    got = bytes.fromhex(out[3:])
    # Header and question only, TC set, all counts 0
    assert got == header(id=7, flags=0x8380, an=0) + question("a.test")


@pytest.mark.parametrize("msg", [
    b"",
    header(flags=0x8180)[:11],
    header(flags=0x8180) + b"\x03ab",
    answer(records=[rr(PTR, 1, 300, bytes(4))])[:-1],
    header(flags=0x8180, qd=40) + question("a.test"),
])
def test_prepare_refuses_broken_messages(harness, msg):
    assert run(harness, f"prepare {msg.hex()} 1 0 0 4096") == ["fail"]


def test_prepare_refuses_record_counts_that_wrap(harness):
    m = (header(flags=0x8180, an=65535, ns=1) + question("a.test")
         + rr(PTR, 1, 300, bytes(4)))
    assert run(harness, f"prepare {m.hex()} 1 10 0 4096") == ["fail"]


# ---- Mutation run ----

def seeds():
    return [
        query("www.example.com"),
        header(ar=1) + question("a.test", 28) + opt(4096, True, b"\x00\x08\x00\x04\x00\x01\x18\x00"),
        answer(records=[rr(PTR, 5, 30, b"\x01t\x05track\xc0\x0e"),
                        rr(b"\xc0\x24", 1, 30, bytes([1, 2, 3, 4]))]),
        answer(flags=0x8183, ns=[soa(3600, 120)], ar=[opt()]),
        answer(records=[rr(PTR, 28, 30, bytes(16))] * 3, ar=[opt()]),
    ]


def mutate(rng, msg):
    m = bytearray(msg)
    for _ in range(rng.randint(1, 6)):
        op = rng.randrange(6)
        if op == 0 and m:
            m[rng.randrange(len(m))] = rng.randrange(256)
        elif op == 1 and m:
            del m[rng.randrange(len(m)):]
        elif op == 2:
            m[rng.randrange(len(m) + 1):0] = bytes(rng.randrange(256) for _ in range(rng.randint(1, 8)))
        elif op == 3 and len(m) > 13:
            # A compression pointer somewhere, often into the message itself
            i = rng.randrange(12, len(m) - 1)
            m[i:i + 2] = struct.pack(">H", 0xc000 | rng.randrange(len(m) + 4))
        elif op == 4 and len(m) >= 12:
            # Record counts: small, big, 0xffff
            i = rng.choice([4, 6, 8, 10])
            m[i:i + 2] = struct.pack(">H", rng.choice([0, 1, 2, 3, 255, 0x7fff, 0xffff]))
        elif op == 5 and len(m) > 2:
            # A label length byte with the reserved bits
            m[rng.randrange(len(m))] = rng.choice([0x3f, 0x40, 0x80, 0xbf, 0xc0, 0xff])
    return bytes(m)


def test_mutated_messages_do_not_break_the_parsers(harness, tmp_path):
    rng = random.Random(20261114)
    data = bytearray()
    count = 0
    for seed in seeds():
        for _ in range(1500):
            m = mutate(rng, seed)[:65535]
            data += struct.pack(">H", len(m)) + m
            count += 1
    for n in range(0, 40):
        # Random bytes, and headers with random counts in front of random bodies
        m = bytes(rng.randrange(256) for _ in range(rng.randrange(80)))
        data += struct.pack(">H", len(m)) + m
        count += 1
    f = tmp_path / "corpus"
    f.write_bytes(bytes(data))
    assert run(harness, f"fuzz {f}") == [f"done {count}"]


# ---- Block lists (filter.c) ----

def line(harness, *texts):
    return [o.strip() for o in run(harness, *("line " + t.encode().hex() if isinstance(t, str)
                                              else "line " + t.hex() for t in texts))]


@pytest.mark.parametrize("text,result", [
    ("||ads.test^", "ads.test/1/0 RULE"),
    ("||ADS.Test^", "ads.test/1/0 RULE"),
    ("||ads.test", "ads.test/1/0 RULE"),
    ("||ads.test^|", "ads.test/1/0 RULE"),
    ("|ads.test^", "ads.test/0/0 RULE"),
    ("|ads.test|", "ads.test/0/0 RULE"),
    ("@@||ads.test^", "ads.test/3/0 RULE"),
    ("@@|ads.test^", "ads.test/2/0 RULE"),
    ("||ads.test^$important", "ads.test/5/0 RULE"),
    ("||ads.test^$badfilter", "ads.test/1/1 RULE"),
    ("||ads.test^$important,badfilter", "ads.test/5/1 RULE"),
    ("||*.ads.test^", "ads.test/1/0 RULE"),
    ("*.ads.test", "ads.test/1/0 RULE"),
    (".ads.test^", "ads.test/1/0 RULE"),
    ("ads.test", "ads.test/0/0 RULE"),
    ("  ads.test  ", "ads.test/0/0 RULE"),
    ("ads.test\r", "ads.test/0/0 RULE"),
    ("ads.test # why", "ads.test/0/0 RULE"),
    ("0.0.0.0 a.test b.test # two", "a.test/0/0 b.test/0/0 RULE"),
    ("127.0.0.1\tA.Test.", "a.test/0/0 RULE"),
    (":: a.test", "a.test/0/0 RULE"),
    ("0.0.0.0 localhost", "SKIP"),
    ("::1 ip6-localhost ip6-loopback", "SKIP"),
    ("0.0.0.0 0.0.0.0", "SKIP"),
    ("10.0.0.1 mapped.test", "UNSUPPORTED"),       # a local mapping, not a block
    ("0.0.0.0 a..test", "UNSUPPORTED"),
    ("0.0.0.0 bücher.test", "UNSUPPORTED"),
    ("", "SKIP"),
    ("   \t ", "SKIP"),
    ("! comment", "SKIP"),
    ("# comment", "SKIP"),
    ("[Adblock Plus 2.0]", "SKIP"),
    ("/ads[0-9]+/", "UNSUPPORTED"),
    ("||ads*.test^", "UNSUPPORTED"),
    ("||ads.test^$third-party", "UNSUPPORTED"),
    ("||ads.test^$important,third-party", "UNSUPPORTED"),
    ("||ads.test^$", "ads.test/1/0 RULE"),            # no modifiers at all
    ("||ads.test^^", "UNSUPPORTED"),
    ("||ads.test^/path", "UNSUPPORTED"),
    ("||.ads.test^", "UNSUPPORTED"),
    ("||ads.test.^", "UNSUPPORTED"),
    ("||", "UNSUPPORTED"),
    ("@@", "UNSUPPORTED"),
    ("|", "UNSUPPORTED"),
    ("^", "UNSUPPORTED"),
    ("$important", "UNSUPPORTED"),
    ("||-a_b.test^", "-a_b.test/1/0 RULE"),
    ("||" + "a" * 63 + ".test^", "a" * 63 + ".test/1/0 RULE"),
    ("||" + "a" * 64 + ".test^", "UNSUPPORTED"),
    ("||" + ".".join(["a" * 61] * 4) + "^", "UNSUPPORTED"),     # 247 + 3 dots = 250
    ("ads.test other", "UNSUPPORTED"),
    ("a" * 1100, "UNSUPPORTED"),
])
def test_list_lines(harness, text, result):
    if text == "||" + ".".join(["a" * 61] * 4) + "^":
        # 4 * 61 + 3 = 247 characters: a valid name after all
        result = ".".join(["a" * 61] * 4) + "/1/0 RULE"
    assert line(harness, text) == [result]


def test_list_line_with_nul_and_odd_bytes(harness):
    # The parser gets a length; a NUL ends the text it looks at, odd bytes never make a rule
    assert line(harness, b"||ads.test^\0garbage", b"||ad\xffs.test^", b"\0||ads.test^") == [
        "ads.test/1/0 RULE", "UNSUPPORTED", "UNSUPPORTED"]


@pytest.mark.parametrize("name,ok", [
    ("a.b", True), ("a-b_c.d", True), ("1.2.3.4", True), ("a" * 63, True), ("a" * 64, False),
    (".".join(["a" * 63] * 3) + "." + "a" * 61, True),     # 253 characters
    (".".join(["a" * 63] * 3) + "." + "a" * 62, False),    # 254
    ("", False), (".", False), ("a.", False), (".a", False), ("a..b", False), ("A.b", False),
    ("a b", False), ("a/b", False), ("a\\b", False), ("ä.b", False), ("a*b", False),
])
def test_valid_domain(harness, name, ok):
    assert run(harness, "valid " + name) == ["yes" if ok else "no"]


LIST = """||ads.test^
||tracker.test^$important
@@||ok.ads.test^
@@||tracker.test^
|exact.test^
||gone.test^
||gone.test^$badfilter
0.0.0.0 host.test
plain.test
||dup.test^
||dup.test^
"""


@pytest.fixture
def built(harness, tmp_path):
    f = tmp_path / "list.txt"
    f.write_text(LIST)
    return f


@pytest.mark.parametrize("name,hits", [
    ("ads.test", "block ads.test/1 allow - important -"),
    ("x.y.ads.test", "block ads.test/1 allow - important -"),
    ("ok.ads.test", "block ads.test/1 allow ok.ads.test/3 important -"),
    ("a.ok.ads.test", "block ads.test/1 allow ok.ads.test/3 important -"),
    ("tracker.test", "block tracker.test/5 allow tracker.test/3 important tracker.test/5"),
    ("exact.test", "block exact.test/0 allow - important -"),
    ("sub.exact.test", "block - allow - important -"),
    ("gone.test", "block - allow - important -"),
    ("host.test", "block host.test/0 allow - important -"),
    ("sub.host.test", "block - allow - important -"),
    ("plain.test", "block plain.test/0 allow - important -"),
    ("test", "block - allow - important -"),
    ("", "block - allow - important -"),
    ("ads.test.", "block - allow - important -"),
    ("xads.test", "block - allow - important -"),
    ("ads\\046test", "block - allow - important -"),
    ("ads.test\\000", "block - allow - important -"),
])
def test_lookups(harness, built, name, hits):
    out = run(harness, f"build {built}", f"lookup {name}")
    assert out[0] == "rules 8 unsupported 0 toomany 0"
    assert out[1] == hits


def test_compiled_set_round_trip(harness, built, tmp_path):
    f = tmp_path / "set.bin"
    out = run(harness, f"build {built}", f"save {f}", f"load {f}", "lookup a.ok.ads.test")
    assert out[1:] == ["ok", "ok 8", "block ads.test/1 allow ok.ads.test/3 important -"]


def damaged(data, how):
    if how == "short":
        return data[:20]
    if how == "magic":
        return b"XLDNSF\x01\0" + data[8:]
    if how == "truncated":
        return data[:-1]
    if how == "longer":
        return data + b"\0"
    if how == "no-final-nul":
        return data[:-1] + b"x"
    if how == "count-huge":
        return data[:8] + struct.pack("<I", 0xffffffff) + data[12:]
    if how == "strings-zero":
        return data[:12] + struct.pack("<I", 0) + data[16:]
    if how == "name-out-of-range":
        # The first entry's string offset beyond the string table
        entry = 24
        return data[:entry + 8] + struct.pack("<I", 0x7fffffff) + data[entry + 12:]
    raise AssertionError(how)


@pytest.mark.parametrize("how", ["short", "magic", "truncated", "longer", "no-final-nul",
                                 "count-huge", "strings-zero", "name-out-of-range"])
def test_damaged_compiled_set_is_refused(harness, built, tmp_path, how):
    f = tmp_path / "set.bin"
    run(harness, f"build {built}", f"save {f}")
    f.write_bytes(damaged(f.read_bytes(), how))
    out = run(harness, f"build {built}", f"load {f}", "lookup ads.test")
    assert out[1].startswith("error ")
    # The set built before stays in use
    assert out[2] == "block ads.test/1 allow - important -"


def test_unsorted_compiled_set_only_misses(harness, built, tmp_path):
    # Entries in the wrong order (a file changed behind our back): lookups may miss, nothing worse
    f = tmp_path / "set.bin"
    run(harness, f"build {built}", f"save {f}")
    data = bytearray(f.read_bytes())
    count = struct.unpack_from("<I", data, 8)[0]
    entries = [bytes(data[24 + 16 * i:40 + 16 * i]) for i in range(count)]
    data[24:24 + 16 * count] = b"".join(reversed(entries))
    f.write_bytes(bytes(data))
    out = run(harness, f"load {f}", *[f"lookup {n}" for n in ("ads.test", "tracker.test",
                                                               "plain.test", "nothing.test")])
    assert out[0] == "ok 8"


def test_mutated_list_lines(harness, tmp_path):
    rng = random.Random(1114)
    alphabet = "abc.-_|@^$*!#[]/ \t\r:0123456789,importantbadfilterÄ\\"
    base = ["||ads.test^", "@@||a.b.test^$important", "0.0.0.0 a.test b.test", "*.x.test"]
    lines = []
    for _ in range(3000):
        s = list(rng.choice(base))
        for _ in range(rng.randint(1, 5)):
            i = rng.randrange(len(s) + 1)
            if rng.random() < 0.5 and s:
                del s[min(i, len(s) - 1)]
            else:
                s.insert(i, rng.choice(alphabet))
        lines.append("".join(s))
    f = tmp_path / "list.txt"
    f.write_text("\n".join(lines))
    out = run(harness, *("line " + l.encode().hex() for l in lines[:500]), f"build {f}")
    assert out[-1].startswith("rules ")
