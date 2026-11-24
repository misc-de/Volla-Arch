# plinius-vdec: the request checks of plinius-vdec-mdpd, the only process that may drive the
# MDP (/dev/mtk_mdp), against what any program of the user can send it over its socket, and the
# plugin's client (mdp-client.c). The daemon runs as itself (mdpd.c with its own main and
# --listen in tmp_path); only the MDP is a stub and plain files named dsec-dmabuf* count as
# dma-bufs (c/dsec_mdpd_harness.c).

import array
import os
import socket
import struct
import subprocess
import time

import pytest

from conftest import PKG
from dsec_helpers import CDIR, build, san_env

VDEC = PKG / "plinius-vdec"
MAGIC = 0x504D4431
REPLY = struct.Struct("<i124s")


def src_bytes(coded_width, height):
    ypitch = coded_width * 12 // 8
    return ypitch * height + ypitch // 2 * (height // 2)


def dst_bytes(pitch, height):
    return pitch * height + pitch * (height // 2)


@pytest.fixture(scope="module")
def binaries(tmp_path_factory):
    d = tmp_path_factory.mktemp("vdec")
    flags = ["-Wno-unused-parameter", "-Wno-missing-field-initializers"]
    return {
        "daemon": build(d, "mdpd-harness", [CDIR / "dsec_mdpd_harness.c"], flags=flags,
                        pkgs=["gio-2.0"]),
        "client": build(d, "mdp-client-harness",
                        [CDIR / "dsec_mdp_client_harness.c", VDEC / "mdp-client.c"], flags=flags,
                        pkgs=["gio-2.0"]),
    }


class Daemon:
    def __init__(self, exe, tmp_path, **env):
        self.sock = tmp_path / "mdp.socket"
        self.log = tmp_path / "mdp.log"
        self.proc = subprocess.Popen([str(exe), "--listen", str(self.sock)],
                                     stderr=subprocess.PIPE,
                                     env=san_env(DSEC_MDP_LOG=self.log, **env))
        for _ in range(250):
            if self.sock.exists():
                return
            time.sleep(0.02)
        raise AssertionError("daemon did not start")

    def connect(self):
        c = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        c.settimeout(5)
        c.connect(str(self.sock))
        return c

    def conversions(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def fds(self):
        return len(os.listdir(f"/proc/{self.proc.pid}/fd"))

    def stop(self):
        self.proc.terminate()
        _, err = self.proc.communicate(timeout=10)
        # A sanitizer report in the daemon fails the test
        assert b"ERROR: AddressSanitizer" not in err and b"runtime error" not in err, err.decode()


@pytest.fixture
def daemon(binaries, tmp_path):
    d = Daemon(binaries["daemon"], tmp_path)
    yield d
    d.stop()


def buffer(tmp_path, name, size):
    f = tmp_path / name
    with open(f, "wb") as out:
        out.truncate(size)
    return f


def frame(tmp_path, w=1920, h=1088, coded=None, pitch=None, src_size=None, dst_size=None,
          src_name="dsec-dmabuf-src", dst_name="dsec-dmabuf-dst"):
    coded = coded or w
    pitch = pitch or w
    src = buffer(tmp_path, src_name, src_bytes(coded, h) if src_size is None else src_size)
    dst = buffer(tmp_path, dst_name, dst_bytes(pitch, h) if dst_size is None else dst_size)
    return src, dst, struct.pack("<5I", MAGIC, coded, w, h, pitch)


def ask(conn, payload, files=(), fds=None):
    opened = [os.open(f, os.O_RDWR) for f in files]
    try:
        fd_list = opened if fds is None else fds
        anc = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fd_list))] if fd_list else []
        conn.sendmsg([payload], anc)
        data = conn.recv(256)
    finally:
        for fd in opened:
            os.close(fd)
    if not data:
        return None
    assert len(data) == REPLY.size
    ok, msg = REPLY.unpack(data)
    return ok, msg.split(b"\0", 1)[0].decode()


def test_a_good_request_is_converted_with_the_checked_size(daemon, tmp_path):
    src, dst, req = frame(tmp_path, src_size=src_bytes(1920, 1088) + 4096)
    with daemon.connect() as c:
        assert ask(c, req, [src, dst]) == (1, "")
    # The source size comes from the buffer itself, never from the client
    assert daemon.conversions() == [f"convert {src_bytes(1920, 1088) + 4096} 1920 1920 1088 1920"]


@pytest.mark.parametrize("kw,why", [
    ({"src_size": src_bytes(1920, 1088) - 1}, "too small"),
    ({"dst_size": dst_bytes(1920, 1088) - 1}, "too small"),
    ({"src_size": 0}, "too small"),
    ({"w": 16, "h": 16}, None),
    ({"w": 4096, "h": 4096}, None),
    ({"w": 14, "h": 16}, "out of range"),
    ({"w": 4098, "h": 16, "coded": 4098}, "out of range"),
    ({"w": 17, "h": 16, "coded": 18, "pitch": 18}, "out of range"),     # odd width
    ({"w": 16, "h": 17}, "out of range"),
    ({"w": 16, "h": 4098}, "out of range"),
    ({"w": 64, "h": 64, "coded": 32}, "out of range"),                  # coded below visible
    ({"w": 64, "h": 64, "coded": 4100}, "out of range"),
    ({"w": 64, "h": 64, "pitch": 32}, "out of range"),
    ({"w": 64, "h": 64, "pitch": 8194}, "out of range"),
    ({"w": 64, "h": 64, "pitch": 8192}, None),
])
def test_size_checks(daemon, tmp_path, kw, why):
    src, dst, req = frame(tmp_path, **kw)
    with daemon.connect() as c:
        ok, msg = ask(c, req, [src, dst])
        if why is None:
            assert (ok, msg) == (1, "")
        else:
            assert ok == 0 and why in msg
            # A request out of range is answered; the connection stays usable
            src2, dst2, good = frame(tmp_path, w=64, h=64, src_name="dsec-dmabuf-s2",
                                     dst_name="dsec-dmabuf-d2")
            assert ask(c, good, [src2, dst2]) == (1, "")
    if why is not None:
        assert len(daemon.conversions()) == 1


def test_only_dma_bufs_reach_the_mdp(daemon, tmp_path):
    src, dst, req = frame(tmp_path, src_name="plain-src")
    with daemon.connect() as c:
        assert ask(c, req, [src, dst]) == (0, "both buffers must be dma-bufs")
        # A socket or a pipe is no dma-buf either
        r, w = os.pipe()
        s1, s2 = socket.socketpair()
        try:
            assert ask(c, req, fds=[r, s1.fileno()]) == (0, "both buffers must be dma-bufs")
        finally:
            os.close(r)
            os.close(w)
            s1.close()
            s2.close()
    assert daemon.conversions() == []


@pytest.mark.parametrize("variant", ["bad-magic", "short", "no-fds", "one-fd", "three-fds",
                                     "empty"])
def test_malformed_requests_close_the_connection(daemon, tmp_path, variant):
    src, dst, req = frame(tmp_path, w=64, h=64)
    extra = buffer(tmp_path, "dsec-dmabuf-extra", 4096)
    files = [src, dst]
    if variant == "bad-magic":
        req = struct.pack("<I", 0x12345678) + req[4:]
    elif variant == "short":
        req = req[:10]
    elif variant == "no-fds":
        files = []
    elif variant == "one-fd":
        files = [src]
    elif variant == "three-fds":
        files = [src, dst, extra]
    elif variant == "empty":
        req = b""
    with daemon.connect() as c:
        if variant == "empty":
            # A zero-length message is the end of the stream for the daemon
            c.sendmsg([req])
            assert c.recv(256) == b""
        else:
            assert ask(c, req, files) == (0, "malformed request")
            assert c.recv(256) == b""
    assert daemon.conversions() == []


def settled(daemon, quiet=0.3, timeout=5):
    """The daemon's fd count once it stayed the same for `quiet` seconds"""
    deadline = time.monotonic() + timeout
    last, since = daemon.fds(), time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(0.02)
        now = daemon.fds()
        if now != last:
            last, since = now, time.monotonic()
        elif time.monotonic() - since >= quiet:
            break
    return last


def test_no_fd_leaks(daemon, tmp_path):
    src, dst, good = frame(tmp_path, w=64, h=64)
    extra = buffer(tmp_path, "dsec-dmabuf-extra", 4096)
    plain = buffer(tmp_path, "plain", 1 << 20)
    with daemon.connect() as c:
        ask(c, good, [src, dst])
    # The daemon closes the warm-up connection after the client did: count once that settled
    base = settled(daemon)
    for i in range(30):
        with daemon.connect() as c:
            ask(c, good, [src, dst])
            ask(c, good, [plain, dst])
            ask(c, struct.pack("<5I", MAGIC, 8, 8, 8, 8), [src, dst])
            ask(c, good, [src, dst, extra, extra, extra])
    # Every fd that came in went again, also the ones beyond two. The daemon closes the last
    # connection after the client did: wait for it (under load that takes more than 0.1 s)
    deadline = time.monotonic() + 5
    while daemon.fds() != base and time.monotonic() < deadline:
        time.sleep(0.02)
    assert daemon.fds() == base


def test_mdp_errors_are_passed_on(binaries, tmp_path):
    for env, message in (({"DSEC_MDP_NEW_FAIL": "1"}, "no libdpframework here"),
                         ({"DSEC_MDP_FAIL": "1"}, "MDP said no")):
        d = Daemon(binaries["daemon"], tmp_path, **env)
        try:
            src, dst, req = frame(tmp_path, w=64, h=64)
            with d.connect() as c:
                assert ask(c, req, [src, dst]) == (0, message)
        finally:
            d.stop()
            d.sock.unlink()


def test_all_slots_taken_waits_instead_of_dropping(daemon, tmp_path):
    idle = [daemon.connect() for _ in range(16)]
    src, dst, req = frame(tmp_path, w=64, h=64)
    late = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    late.settimeout(0.5)
    late.connect(str(daemon.sock))     # waits in the backlog
    fds = [os.open(f, os.O_RDWR) for f in (src, dst)]
    late.sendmsg([req], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds))])
    with pytest.raises(socket.timeout):
        late.recv(256)
    idle.pop().close()
    late.settimeout(5)
    assert REPLY.unpack(late.recv(256))[0] == 1
    for fd in fds:
        os.close(fd)
    late.close()
    for c in idle:
        c.close()


# ---- The client in the plugin ----

def client(binaries, sock, src, src_size, coded, w, h, dst, pitch):
    r = subprocess.run([str(binaries["client"]), str(src), str(src_size), str(coded), str(w),
                        str(h), str(dst), str(pitch)], capture_output=True, text=True,
                       timeout=30, env=san_env(PLINIUS_VDEC_MDP_SOCKET=sock))
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_client_through_the_daemon(binaries, daemon, tmp_path):
    src, dst, _ = frame(tmp_path, w=1280, h=720)
    assert client(binaries, daemon.sock, src, src_bytes(1280, 720), 1280, 1280, 720, dst,
                  1280) == "ok"
    out = client(binaries, daemon.sock, src, src_bytes(1280, 720), 1280, 1280, 720, dst, 9000)
    assert out.startswith("error MDP service: picture 1280x720")


def test_client_refuses_a_small_buffer_itself(binaries, daemon, tmp_path):
    src, dst, _ = frame(tmp_path, w=1280, h=720)
    out = client(binaries, daemon.sock, src, src_bytes(1280, 720) - 1, 1280, 1280, 720, dst, 1280)
    assert out.startswith("error HyFBC buffer of")
    assert daemon.conversions() == []


def test_client_without_daemon(binaries, tmp_path):
    out = client(binaries, tmp_path / "none", "/dev/null", 0, 64, 64, 64, "/dev/null", 64)
    assert out.startswith("error MDP service") and "No such file" in out


def test_client_with_overlong_socket_path(binaries, tmp_path):
    out = client(binaries, "/" + "x" * 200, "/dev/null", 0, 64, 64, 64, "/dev/null", 64)
    assert out == "error socket path too long"


@pytest.mark.parametrize("reply,expected", [
    (struct.pack("<i", 0) + b"A" * 124, "error MDP service: " + "A" * 123),  # no NUL in it
    (struct.pack("<i", 1), "error MDP service: no answer"),                 # cut short
    (b"", "error MDP service: no answer"),
])
def test_client_with_a_broken_reply(binaries, tmp_path, reply, expected):
    path = tmp_path / "fake.socket"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    srv.bind(str(path))
    srv.listen(1)
    src, dst, _ = frame(tmp_path, w=64, h=64)
    proc = subprocess.Popen([str(binaries["client"]), str(src), str(src_bytes(64, 64)), "64",
                             "64", "64", str(dst), "64"], stdout=subprocess.PIPE, text=True,
                            env=san_env(PLINIUS_VDEC_MDP_SOCKET=path))
    conn, _ = srv.accept()
    with conn:
        conn.recvmsg(64, socket.CMSG_SPACE(16))
        if reply:
            conn.send(reply)
    out = proc.communicate(timeout=30)[0].strip()
    srv.close()
    assert proc.returncode == 0
    assert out == expected
