# Helpers of the tests of plinius-dnsfilter as a running daemon (test_dsec_dnsfilter_live_*.py):
# the harness build (c/dsec_dnsfilter_live_harness.c with a sandboxed copy of the daemon), a
# runner per test, DNS messages, and the servers the daemon talks to, all on 127.0.0.1 in the
# test: a plain DNS server (UDP and TCP), DNS over TLS and HTTPS servers and a list server with
# certificates of a CA made for the test. Nothing leaves the machine.

import datetime
import http.server
import os
import select
import socket
import socketserver
import ssl
import struct
import subprocess
import threading
import time
from pathlib import Path

import pytest

from conftest import GCOV_DIR, PKG, REPO, load_script
from dsec_helpers import CDIR, build, san_env

DNSFILTER = PKG / "plinius-dnsfilter"
dhw = load_script(REPO / "tests" / "daemons-hw" / "dhw_harness.py", "dhw_harness")

TYPES = {"A": 1, "NS": 2, "CNAME": 5, "SOA": 6, "MX": 15, "TXT": 16, "AAAA": 28, "OPT": 41}
NAMES = {v: k for k, v in TYPES.items()}
STATUS = ["PASSED", "ALLOWED_RULE", "ALLOWED_LIST", "ALLOWED_SYSTEM", "BLOCKED_LIST",
          "BLOCKED_RULE", "UNFILTERED", "FAILED", "BLOCKED_SPECIAL"]


# ---- The harness ----

def build_live(tmp_dir):
    """The harness with the daemon's fixed paths under <tmp_dir>/root, a symlink each test points
    to its own tmp_path (point_root)."""
    tmp_dir = Path(tmp_dir)
    root = tmp_dir / "root"
    src = dhw.sandboxed_source(DNSFILTER / "plinius-dnsfilter.c", tmp_dir, [
        ('"/var/lib/plinius-dnsfilter"', f'"{root}/default-state"'),
        ('"/run/plinius-dns"', f'"{root}/default-link"'),
        ('"/run/NetworkManager/resolv.conf"', f'"{root}/nm/resolv.conf"'),
        ('"/var/lib/NetworkManager"', f'"{root}/nmlib"'),
        ('"/etc/resolv.conf"', f'"{root}/etc-resolv.conf"'),
        # Hotspot and USB clients: their port (5300) is the test's, on loopback only
        ("#define SHARED_PORT 5300", "#define SHARED_PORT live_shared_port"),
        ('"0.0.0.0", SHARED_PORT', '"127.0.0.1", SHARED_PORT'),
        ('"::", SHARED_PORT', '"::1", SHARED_PORT'),
    ])
    assert src.read_text().startswith("#line 1"), "a replacement moved lines: coverage would not count"
    binary = build(tmp_dir, "dnsfilter-live",
                   [CDIR / "dsec_dnsfilter_live_harness.c", DNSFILTER / "appid.c",
                    DNSFILTER / "dnsmsg.c", DNSFILTER / "filter.c"],
                   flags=[f"-I{tmp_dir}", f"-I{DNSFILTER}", "-Wno-unused-parameter",
                          "-Wno-missing-field-initializers", "-Wno-unused-variable",
                          "-Wno-unused-but-set-variable"],
                   pkgs=["gio-unix-2.0", "libsoup-3.0"])
    return binary, root


def free_port():
    """A port free for UDP and TCP on 127.0.0.1 and ::1."""
    for _ in range(50):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        try:
            socks = []
            for fam, addr in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
                for kind in (socket.SOCK_DGRAM, socket.SOCK_STREAM):
                    s = socket.socket(fam, kind)
                    socks.append(s)
                    s.bind((addr, port))
            return port
        except OSError:
            continue
        finally:
            for s in socks:
                s.close()
    raise AssertionError("no free port")


class Result:
    def __init__(self, out, exit_code, stderr, cmds):
        self.out = out
        self.exit = exit_code
        self.stderr = stderr
        self.cmds = cmds

    def __getitem__(self, i):
        return self.out[i]

    def __len__(self):
        return len(self.out)

    def of(self, prefix):
        """Outputs of the commands that start with prefix"""
        return [o for c, o in zip(self.cmds, self.out) if c.startswith(prefix)]


class Live:
    """One test's daemon: state and link directory in tmp_path, ports of its own, the fixed paths
    of the copy (root) pointed into tmp_path."""

    def __init__(self, binary, root_link, tmp_path, fake_bin):
        self.binary = binary
        self.tmp = Path(tmp_path)
        self.root = self.tmp / "root"
        for d in ("nm", "nmlib", "default-state", "default-link"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        if root_link.is_symlink() or root_link.exists():
            root_link.unlink()
        root_link.symlink_to(self.root)
        # The daemon's own names of its fixed paths (through the link)
        self.nm_resolv = root_link / "nm" / "resolv.conf"
        self.state = self.tmp / "state"
        self.link = self.tmp / "link"
        self.link.mkdir(exist_ok=True)
        self.port = free_port()
        self.shared_port = free_port()
        self.fake_bin = fake_bin
        self.env = {}

    def args(self, system=False, extra=()):
        a = ["--state-dir", str(self.state), "--link-dir", str(self.link), "--port", str(self.port)]
        if not system:
            a.append("--no-system")
        return a + list(extra)

    def run(self, *cmds, system=False, args=None, env=None, timeout=90, sysbus=True, quits=False,
            path_env=None):
        e = san_env(GIO_USE_PROXY_RESOLVER="dummy", GSETTINGS_BACKEND="memory", GIO_USE_VFS="local",
                    GIO_USE_VOLUME_MONITOR="unix", no_proxy="*", LIVE_SHARED_PORT=self.shared_port,
                    DBUS_SESSION_BUS_ADDRESS="disabled:", DBUS_SYSTEM_BUS_ADDRESS="disabled:")
        for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY",
                  "G_MESSAGES_DEBUG", "G_DEBUG"):
            e.pop(k, None)
        if sysbus:
            e["LIVE_SYSBUS"] = "1"
        if path_env is not None:
            e["PATH"] = path_env
        elif self.fake_bin is not None:
            e["PATH"] = self.fake_bin.env(isolated=True)["PATH"]
        e.update({k: str(v) for k, v in self.env.items()})
        e.update({k: str(v) for k, v in (env or {}).items()})
        argv = [str(self.binary), *(self.args(system) if args is None else args)]
        script = "\n".join(cmds) + "\n"
        r = subprocess.run(argv, input=script, capture_output=True, text=True, env=e,
                           timeout=timeout * (5 if GCOV_DIR else 1), cwd=self.tmp)
        assert r.returncode == 0, f"harness exit {r.returncode}\n{r.stderr[-6000:]}"
        lines = r.stdout.splitlines()
        exit_code = None
        if lines and lines[-1].startswith("exit "):
            exit_code = int(lines.pop().split()[1])
        assert not any(line.startswith(("<unknown", "<bad", "BADREPLY")) for line in lines), \
            "\n".join(lines)
        if not quits:
            assert len(lines) == len(cmds), (lines, cmds, r.stderr[-3000:])
        return Result(lines, exit_code, r.stderr, list(cmds))


# ---- DNS messages ----

def wname(name):
    out = b""
    for label in name.rstrip(".").split(".") if name.strip(".") else []:
        out += bytes([len(label)]) + label.encode()
    return out + b"\0"


def query(name, qtype="A", qid=0x4242, flags=0x0100, edns=None, do=False):
    t = TYPES[qtype] if isinstance(qtype, str) else qtype
    ar = 1 if edns else 0
    m = struct.pack(">HHHHHH", qid, flags, 1, 0, 0, ar) + wname(name) + struct.pack(">HH", t, 1)
    if edns:
        m += b"\0" + struct.pack(">HHIH", 41, edns, 0x8000 if do else 0, 0)
    return m.hex()


def read_name(msg, off):
    labels = []
    jumped = None
    while True:
        n = msg[off]
        if n == 0:
            off += 1
            break
        if n & 0xC0 == 0xC0:
            if jumped is None:
                jumped = off + 2
            off = ((n & 0x3F) << 8) | msg[off + 1]
            continue
        labels.append(msg[off + 1:off + 1 + n].decode("latin-1"))
        off += 1 + n
    return ".".join(labels), (jumped if jumped is not None else off)


class Msg:
    """A parsed DNS message: id, flags, rcode, question, answer records (type, ttl, text)."""

    def __init__(self, data):
        if isinstance(data, str):
            data = bytes.fromhex(data.split()[-1]) if data.startswith("reply") else bytes.fromhex(data)
        self.raw = data
        self.id, self.flags, qd, an, ns, ar = struct.unpack(">HHHHHH", data[:12])
        self.rcode = self.flags & 0xF
        self.tc = bool(self.flags & 0x0200)
        self.ad = bool(self.flags & 0x0020)
        off = 12
        self.qname = self.qtype = None
        for _ in range(qd):
            self.qname, off = read_name(data, off)
            self.qtype = struct.unpack(">H", data[off:off + 2])[0]
            off += 4
        self.answers = []
        self.additional = []
        for section, count in (("an", an), ("ns", ns), ("ar", ar)):
            for _ in range(count):
                name, off = read_name(data, off)
                rtype, _, ttl, rdlen = struct.unpack(">HHIH", data[off:off + 10])
                off += 10
                rdata = data[off:off + rdlen]
                if rtype == 1:
                    text = socket.inet_ntop(socket.AF_INET, rdata)
                elif rtype == 28:
                    text = socket.inet_ntop(socket.AF_INET6, rdata)
                elif rtype == 5:
                    text = read_name(data, off)[0]
                else:
                    text = rdata.hex()
                off += rdlen
                rec = (NAMES.get(rtype, rtype), ttl, text)
                if section == "an":
                    self.answers.append(rec)
                elif section == "ar":
                    self.additional.append(rec)

    @property
    def addrs(self):
        return [t for k, _, t in self.answers if k in ("A", "AAAA")]


def parse_query(data):
    qid, flags, qd = struct.unpack(">HHH", data[:6])
    name, off = read_name(data, 12)
    qtype = struct.unpack(">H", data[off:off + 2])[0]
    edns = data[off + 4:]
    return qid, flags, name.lower(), qtype, off + 4, edns


def rr(name, rtype, ttl, rdata):
    return wname(name) + struct.pack(">HHIH", TYPES[rtype], 1, ttl, len(rdata)) + rdata


def answer(q, records=(), rcode=0, flags=0x8180, qid=None, qname=None, authority=b"", nscount=0):
    qid0, qflags, name, qtype, end, _ = parse_query(q)
    head = struct.pack(">HHHHHH", qid0 if qid is None else qid, flags | rcode | (qflags & 0x0110), 1,
                       len(records), nscount, 0)
    question = q[12:end] if qname is None else wname(qname) + q[end - 4:end]
    return head + question + b"".join(records) + authority


# ---- Upstream servers ----

class Zone:
    """What the test's servers answer. Per name (lower case): a dict of
    a=[addresses], aaaa=[...], cname=TARGET, ttl=N, rcode=N, tc=True (UDP only), ad=True,
    drop=True (no answer), delay=S (answers S seconds late), tc_always=True (TC over TCP too),
    wrongid=True (an answer with another ID first, then the right one),
    mismatch=True (an answer to another question), txt=N (a TXT record of N bytes), soa=TTL
    (negative answer with an SOA), garbage=True (bytes that are not DNS)."""

    def __init__(self, names=None):
        self.names = dict(names or {})
        self.seen = []
        self.lock = threading.Lock()

    def count(self, name, proto=None):
        with self.lock:
            return sum(1 for p, n, *_ in self.seen if n == name and (proto is None or p == proto))

    def respond(self, q, proto):
        """The answers for a query (a list: several for wrongid), [] for none"""
        qid, flags, name, qtype, end, edns = parse_query(q)
        with self.lock:
            self.seen.append((proto, name, NAMES.get(qtype, qtype), qid, edns))
        spec = self.names.get(name)
        if spec is None:
            return [answer(q, rcode=3, authority=rr("test", "SOA", 60, b"\0" * 22), nscount=1)]
        if spec.get("delay"):
            time.sleep(spec["delay"])
        if spec.get("drop") or (spec.get("drop_udp") and proto == "udp"):
            return []
        if spec.get("garbage"):
            return [b"\x81\x80garbage!"]
        ttl = spec.get("ttl", 300)
        records = []
        if spec.get("cname"):
            target = spec["cname"]
            records.append(rr(name, "CNAME", ttl, wname(target)))
            name = target
        if qtype == 1:
            records += [rr(name, "A", ttl, socket.inet_aton(a)) for a in spec.get("a", [])]
        if qtype == 28:
            records += [rr(name, "AAAA", ttl, socket.inet_pton(socket.AF_INET6, a))
                        for a in spec.get("aaaa", [])]
        if qtype == 16 and spec.get("txt"):
            size = spec["txt"]
            chunks = b"".join(bytes([min(255, size - i)]) + b"x" * min(255, size - i)
                              for i in range(0, size, 255))
            records.append(rr(name, "TXT", ttl, chunks))
        flags = 0x8180 | (0x0020 if spec.get("ad") else 0)
        if (spec.get("tc") and proto == "udp") or spec.get("tc_always"):
            flags |= 0x0200
            records = []
        authority, nscount = b"", 0
        if "soa" in spec:
            authority, nscount = rr("test", "SOA", spec["soa"], b"\0" * 18 + struct.pack(">I", spec["soa"])), 1
        out = []
        if spec.get("wrongid"):
            out.append(answer(q, records, qid=(qid + 1) & 0xFFFF))
        if spec.get("mismatch"):
            return [answer(q, records, qname="other.test")]
        out.append(answer(q, records, rcode=spec.get("rcode", 0), flags=flags,
                          authority=authority, nscount=nscount))
        return out


class FakeDns:
    """A plain DNS server on 127.0.0.1 (UDP and TCP on the same port) answering from a Zone."""

    def __init__(self, zone, host="127.0.0.1"):
        self.zone = zone
        for _ in range(50):
            self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.udp.bind((host, 0))
            self.port = self.udp.getsockname()[1]
            self.tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                self.tcp.bind((host, self.port))
                break
            except OSError:
                self.udp.close()
                self.tcp.close()
        self.tcp.listen(16)
        self.addr = f"{host}:{self.port}"
        self._stop = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self._stop:
            r, _, _ = select.select([self.udp, self.tcp], [], [], 0.1)
            for s in r:
                if s is self.udp:
                    try:
                        q, peer = self.udp.recvfrom(65535)
                    except OSError:
                        continue
                    for a in self.zone.respond(q, "udp"):
                        self.udp.sendto(a, peer)
                else:
                    conn, _ = self.tcp.accept()
                    threading.Thread(target=self._tcp, args=(conn,), daemon=True).start()

    def _tcp(self, conn):
        with conn:
            conn.settimeout(10)
            try:
                while True:
                    head = _recv_all(conn, 2)
                    if not head:
                        return
                    q = _recv_all(conn, struct.unpack(">H", head)[0])
                    for a in self.zone.respond(q, "tcp"):
                        conn.sendall(struct.pack(">H", len(a)) + a)
            except OSError:
                return

    def close(self):
        self._stop = True
        self.thread.join(timeout=5)
        self.udp.close()
        self.tcp.close()


def _recv_all(conn, n):
    data = b""
    while len(data) < n:
        chunk = conn.recv(n - len(data))
        if not chunk:
            return None
        data += chunk
    return data


# ---- TLS: a CA of the test and servers with its certificates ----

def make_ca(tmp_dir, names=("dot.test", "doh.test", "lists.test", "bad.test")):
    """CA and server certificate (for names) as PEM files; returns (ca, cert, key) paths."""
    crypto = pytest.importorskip("cryptography")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    del crypto
    tmp_dir = Path(tmp_dir)
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "plinius test CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key()).serial_number(1)
          .not_valid_before(now - datetime.timedelta(days=1))
          .not_valid_after(now + datetime.timedelta(days=2))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                       content_commitment=False, key_encipherment=False,
                                       data_encipherment=False, key_agreement=False,
                                       encipher_only=False, decipher_only=False), critical=True)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
            .issuer_name(ca_name).public_key(key.public_key()).serial_number(2)
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=2))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in names]),
                           critical=False)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
                           critical=False)
            .sign(ca_key, hashes.SHA256()))
    pem = serialization.Encoding.PEM
    (tmp_dir / "ca.pem").write_bytes(ca.public_bytes(pem))
    (tmp_dir / "server.pem").write_bytes(cert.public_bytes(pem))
    (tmp_dir / "server.key").write_bytes(key.private_bytes(
        pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return tmp_dir / "ca.pem", tmp_dir / "server.pem", tmp_dir / "server.key"


def server_context(cert, key):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    return ctx


class FakeDot:
    """DNS over TLS on 127.0.0.1. mode: "ok", "close" (closes after the first answer),
    "badlen" (a length below a DNS header), "half" (a length, then the connection ends),
    "silent" (reads, never answers), "stall" (a length, then nothing for 5 s), "nohandshake" (TCP only, no TLS for 5 s)."""

    def __init__(self, zone, cert, key, mode="ok"):
        self.zone = zone
        self.mode = mode
        self.ctx = server_context(cert, key)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.connections = 0
        self._stop = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        self.sock.settimeout(0.1)
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            self.connections += 1
            threading.Thread(target=self._conn, args=(conn,), daemon=True).start()

    def _conn(self, raw):
        if self.mode == "nohandshake":
            time.sleep(5)
            raw.close()
            return
        try:
            conn = self.ctx.wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError):
            raw.close()
            return
        with conn:
            conn.settimeout(10)
            try:
                while True:
                    head = _recv_all(conn, 2)
                    if not head:
                        return
                    q = _recv_all(conn, struct.unpack(">H", head)[0])
                    if self.mode == "silent":
                        continue
                    if self.mode == "badlen":
                        conn.sendall(b"\x00\x05hello")
                        return
                    if self.mode == "half":
                        conn.sendall(b"\x00\x40abc")
                        return
                    if self.mode == "stall":
                        conn.sendall(b"\x00\x40")
                        time.sleep(5)
                        return
                    for a in self.zone.respond(q, "tls"):
                        conn.sendall(struct.pack(">H", len(a)) + a)
                    if self.mode == "close":
                        return
            except (OSError, ssl.SSLError):
                return

    def close(self):
        self._stop = True
        self.thread.join(timeout=5)
        self.sock.close()


class FakeHttps:
    """HTTPS on 127.0.0.1: DNS over HTTPS (POST /dns-query, answers from the zone; /fail gives
    500, /long gives more than a DNS message) and lists (GET /lists/NAME from self.lists, with
    ETag and Last-Modified; If-None-Match answers 304; self.status[NAME] another status)."""

    def __init__(self, zone, cert, key):
        outer = self
        self.zone = zone
        self.lists = {}
        self.status = {}
        self.slow = {}        # name -> seconds between the two halves of the body
        self.truncated = set()  # names whose body ends before its Content-Length
        self.requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code, body=b"", headers=()):
                self.send_response(code)
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.requests.append(("POST", self.path, dict(self.headers), body))
                if self.path == "/fail":
                    return self._send(500, b"no")
                if self.path == "/short":
                    # Says more than it sends, then ends the connection
                    self.send_response(200)
                    self.send_header("Content-Length", "100")
                    self.end_headers()
                    self.wfile.write(b"\0" * 20)
                    self.wfile.flush()
                    self.close_connection = True
                    return
                if self.path == "/long":
                    return self._send(200, b"\0" * 70000, [("Content-Type", "application/dns-message")])
                answers = outer.zone.respond(body, "https")
                if not answers:
                    return self._send(200, b"", [("Content-Type", "application/dns-message")])
                self._send(200, answers[-1], [("Content-Type", "application/dns-message")])

            def do_GET(self):
                outer.requests.append(("GET", self.path, dict(self.headers), b""))
                name = self.path.rsplit("/", 1)[-1]
                if outer.status.get(name) == 304:
                    self.send_response(304)
                    self.end_headers()
                    return
                if name in outer.status:
                    return self._send(outer.status[name], b"gone")
                if name in outer.slow or name in outer.truncated:
                    body = outer.lists.get(name, b"")
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body) + (1000 if name in outer.truncated else 0)))
                    self.end_headers()
                    self.wfile.write(body[:len(body) // 2])
                    self.wfile.flush()
                    time.sleep(outer.slow.get(name, 0))
                    self.wfile.write(body[len(body) // 2:])
                    self.wfile.flush()
                    self.close_connection = True
                    return
                if name not in outer.lists:
                    return self._send(404, b"not found")
                body = outer.lists[name]
                etag = f'"{name}-{len(body)}"'
                if self.headers.get("If-None-Match") == etag:
                    return self._send(304)
                self._send(200, body, [("ETag", etag),
                                       ("Last-Modified", "Wed, 18 Nov 2026 10:00:00 GMT")])

        class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True

        self.httpd = Server(("127.0.0.1", 0), Handler)
        self.httpd.socket = server_context(cert, key).wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05},
                                       daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def nm_link(iface, servers, priority=100, vpn=False, domains=()):
    """One entry of NetworkManager's DnsManager Configuration, as GVariant text"""
    parts = [f"'interface': <'{iface}'>", f"'priority': <int32 {priority}>",
             f"'vpn': <{'true' if vpn else 'false'}>",
             "'nameservers': <@as [" + ", ".join(f"'{s}'" for s in servers) + "]>"]
    if domains:
        parts.append("'domains': <@as [" + ", ".join(f"'{d}'" for d in domains) + "]>")
    return "{" + ", ".join(parts) + "}"


def nm_conf(*links):
    return "[" + ", ".join(links) + "]" if links else "@aa{sv} []"


def wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def file_mode(path):
    return os.stat(path).st_mode & 0o777


# ---- Fixtures (imported by the test files) ----

_BUILT = {}


def live_binary(tmp_path_factory):
    if "bin" not in _BUILT:
        _BUILT["bin"] = build_live(tmp_path_factory.mktemp("dnsf-live"))
    return _BUILT["bin"]


def tls_files(tmp_path_factory):
    if "tls" not in _BUILT:
        _BUILT["tls"] = make_ca(tmp_path_factory.mktemp("dnsf-tls"))
    return _BUILT["tls"]


@pytest.fixture
def live(tmp_path_factory, tmp_path, fake_bin):
    binary, root = live_binary(tmp_path_factory)
    return Live(binary, root, tmp_path, fake_bin)


@pytest.fixture
def zone():
    return Zone({"a.test": {"a": ["192.0.2.1"], "aaaa": ["2001:db8::1"]},
                 "b.test": {"a": ["192.0.2.2"]},
                 "dot.test": {"a": ["127.0.0.1"]},
                 "doh.test": {"a": ["127.0.0.1"]},
                 "lists.test": {"a": ["127.0.0.1"]},
                 "bad.test": {"a": ["127.0.0.1"]},
                 "wrong.test": {"a": ["127.0.0.1"]}})


@pytest.fixture
def dns(zone):
    server = FakeDns(zone)
    yield server
    server.close()


def write_settings(live, **values):
    """The daemon's settings file; keys with '_' for '-'; lists as Python lists"""
    live.state.mkdir(parents=True, exist_ok=True)
    lines = ["[filter]"]
    for k, v in values.items():
        if isinstance(v, bool):
            v = "true" if v else "false"
        elif isinstance(v, (list, tuple)):
            v = "".join(f"{x};" for x in v)
        lines.append(f"{k.replace('_', '-')}={v}")
    (live.state / "settings").write_text("\n".join(lines) + "\n")


def write_lists(live, text):
    live.state.mkdir(parents=True, exist_ok=True)
    (live.state / "lists").write_text(text)


def log_entries(text):
    """GetLog's reply as dicts (the fields of LOG_TYPE)"""
    GLib = pytest.importorskip("gi.repository.GLib")

    v = GLib.Variant.parse(None, text.split(" ", 1)[1], None, None)
    keys = ("id", "time", "name", "type", "kind", "client", "status", "reason", "via", "answer",
            "upstream", "duration", "rcode", "cached", "app")
    return [dict(zip(keys, e)) for e in v.unpack()[0]]


def unpack(text):
    """An 'ok VALUE' line as Python values"""
    GLib = pytest.importorskip("gi.repository.GLib")

    assert text.startswith("ok "), text
    return GLib.Variant.parse(None, text[3:], None, None).unpack()
