# plinius-vdec-mdpd (mdpd.c), the only process that may drive the MDP: its request checks, how
# it is started (socket activation with its idle exit, --listen, --fake, usage), and that it
# survives clients that misbehave or leave early. The daemon runs with its own main; only the
# MDP is a stub that logs each call, and a file named dmabuf-* counts as a dma-buf (readlink of
# /proc/self/fd says "/dmabuf:" for those). tests/daemons-security/test_dsec_vdec.py covers the
# same checks from the security side; this file also counts the daemon's lines (its main is
# ended by SIGTERM here through exit(), so gcov's counters are written).

import array
import os
import signal
import socket
import struct
import subprocess
import time

import pytest

from conftest import GCOV_DIR
from dhw_harness import PKG, build_harness, sandboxed_source

VDEC = PKG / "plinius-vdec"
MAGIC = 0x504D4431
REPLY = struct.Struct("<i124s")
SCALE = 5 if GCOV_DIR else 1

HARNESS = r"""
#define _GNU_SOURCE
#include <errno.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <unistd.h>
#include <glib.h>
#include <gio/gio.h>
#include "mdp.h"

struct PvMdp {
	int id;
};

static void mdp_log(const char *fmt, ...)
{
	const char *path = getenv("VDEC_MDP_LOG");
	if (!path)
		return;
	FILE *f = fopen(path, "a");
	va_list args;
	va_start(args, fmt);
	vfprintf(f, fmt, args);
	va_end(args);
	fputc('\n', f);
	fclose(f);
}

PvMdp *pv_mdp_new(GError **error)
{
	static int ids;
	if (getenv("VDEC_MDP_NEW_FAIL")) {
		g_set_error(error, G_IO_ERROR, G_IO_ERROR_FAILED, "no libdpframework here");
		return NULL;
	}
	PvMdp *m = g_new0(PvMdp, 1);
	m->id = ++ids;
	mdp_log("new %d", m->id);
	return m;
}

void pv_mdp_free(PvMdp *mdp)
{
	mdp_log("free %d", mdp->id);
	g_free(mdp);
}

gboolean pv_mdp_hyfbc_to_nv12(PvMdp *mdp, int src_fd, guint src_size, guint coded_width, guint width,
			      guint height, int dst_fd, guint dst_pitch, GError **error)
{
	mdp_log("convert %d %u %u %u %u %u", mdp->id, src_size, coded_width, width, height, dst_pitch);
	if (getenv("VDEC_MDP_SLEEP_MS"))
		g_usleep(atoi(getenv("VDEC_MDP_SLEEP_MS")) * 1000);
	if (getenv("VDEC_MDP_FAIL")) {
		g_set_error(error, G_IO_ERROR, G_IO_ERROR_FAILED, "MDP said no");
		return FALSE;
	}
	return TRUE;
}

/* A file named dmabuf-* is a dma-buf; for one named gone-* the link cannot be read */
static ssize_t fake_readlink(const char *path, char *buf, size_t size)
{
	char real[4096];
	ssize_t n = readlink(path, real, sizeof real - 1);
	if (n < 0)
		return n;
	real[n] = '\0';
	const char *base = strrchr(real, '/');
	if (base && !strncmp(base + 1, "gone-", 5)) {
		errno = ENOENT;
		return -1;
	}
	if (base && !strncmp(base + 1, "dmabuf-", 7)) {
		snprintf(real, sizeof real, "/dmabuf:%s", base + 1);
		n = strlen(real);
	}
	if ((size_t)n > size)
		n = size;
	memcpy(buf, real, n);
	return n;
}

/* VDEC_POLL_FAIL=n: the n-th poll fails (EINVAL); VDEC_ACCEPT_FAIL=1: the first accept fails */
static int polls;
static int fake_poll(struct pollfd *fds, nfds_t n, int timeout)
{
	if (getenv("VDEC_POLL_FAIL") && ++polls == atoi(getenv("VDEC_POLL_FAIL"))) {
		errno = EINVAL;
		return -1;
	}
	return poll(fds, n, timeout);
}

static int accepts;
static int fake_accept4(int fd, struct sockaddr *addr, socklen_t *len, int flags)
{
	if (getenv("VDEC_ACCEPT_FAIL") && !accepts++) {
		errno = ECONNABORTED;
		return -1;
	}
	return accept4(fd, addr, len, flags);
}

#define readlink fake_readlink
#define poll fake_poll
#define accept4 fake_accept4
#define main mdpd_main
#include "mdpd.c"
#undef main

static void on_term(int sig)
{
	exit(0); /* writes gcov's counters in coverage builds */
}

static void on_usr1(int sig)
{
	fprintf(stderr, "usr1\n");
}

int main(int argc, char **argv)
{
	struct sigaction sa = {.sa_handler = on_usr1};
	sigaction(SIGUSR1, &sa, NULL);
	signal(SIGTERM, on_term);
	/* VDEC_NOFILE=n: no more fds than that (set here: the dynamic loader needs some; lifted again
	 * before the exit, so that gcov can write its counters) */
	struct rlimit saved;
	getrlimit(RLIMIT_NOFILE, &saved);
	if (getenv("VDEC_NOFILE")) {
		struct rlimit rl = {atoi(getenv("VDEC_NOFILE")), saved.rlim_max};
		setrlimit(RLIMIT_NOFILE, &rl);
	}
	int r = mdpd_main(argc, argv);
	setrlimit(RLIMIT_NOFILE, &saved);
	return r;
}
"""


def src_bytes(coded_width, height):
    ypitch = coded_width * 12 // 8
    return ypitch * height + ypitch // 2 * (height // 2)


def dst_bytes(pitch, height):
    return pitch * height + pitch * (height // 2)


@pytest.fixture(scope="module")
def daemon_exe(tmp_path_factory):
    d = tmp_path_factory.mktemp("mdpd")
    # The idle exit after 1 s instead of a minute; the daemon only reads /proc/self/fd
    sandboxed_source(VDEC / "mdpd.c", d, [("#define IDLE_EXIT   60 /* s */", "#define IDLE_EXIT   1 /* s */")],
                     allow=("/proc/self/fd/",))
    return build_harness(d, "mdpd", HARNESS, flags=[f"-I{VDEC}", "-Wno-unused-parameter"], pkgs=("gio-2.0",))


def env(tmp_path, **extra):
    e = dict(os.environ)
    e.update(ASAN_OPTIONS="detect_leaks=0:abort_on_error=0:exitcode=86",
             UBSAN_OPTIONS="print_stacktrace=1:halt_on_error=1:exitcode=87",
             VDEC_MDP_LOG=str(tmp_path / "mdp.log"), LC_ALL="C")
    e.update({k: str(v) for k, v in extra.items()})
    return e


class Daemon:
    def __init__(self, exe, tmp_path, *args, **extra):
        self.tmp = tmp_path
        self.sock = tmp_path / "mdp.socket"
        self.proc = subprocess.Popen([str(exe), "--listen", str(self.sock), *args], stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, env=env(tmp_path, **extra))
        for _ in range(500):
            if self.sock.exists():
                return
            time.sleep(0.01)
        raise AssertionError("daemon did not start")

    def connect(self):
        c = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        c.settimeout(5 * SCALE)
        c.connect(str(self.sock))
        return c

    def log(self):
        f = self.tmp / "mdp.log"
        return f.read_text().splitlines() if f.exists() else []

    def stop(self):
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
        err, _ = self.proc.communicate(timeout=10 * SCALE)
        assert "Sanitizer" not in err and "runtime error" not in err, err
        return self.proc.returncode, err


@pytest.fixture
def daemon(daemon_exe, tmp_path):
    d = Daemon(daemon_exe, tmp_path)
    yield d
    assert d.stop()[0] == 0


def frame(tmp_path, w=64, h=64, coded=None, pitch=None, src_size=None, dst_size=None, src="dmabuf-src",
          dst="dmabuf-dst"):
    coded = coded or w
    pitch = pitch or w
    files = []
    for name, size in ((src, src_bytes(coded, h) if src_size is None else src_size),
                       (dst, dst_bytes(pitch, h) if dst_size is None else dst_size)):
        with open(tmp_path / name, "wb") as f:
            f.truncate(size)
        files.append(tmp_path / name)
    return files, struct.pack("<5I", MAGIC, coded, w, h, pitch)


def send(conn, payload, files):
    fds = [os.open(f, os.O_RDWR) for f in files]
    try:
        anc = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds))] if fds else []
        conn.sendmsg([payload], anc)
    finally:
        for fd in fds:
            os.close(fd)


def ask(conn, payload, files):
    send(conn, payload, files)
    data = conn.recv(256)
    if not data:
        return None
    ok, msg = REPLY.unpack(data)
    return ok, msg.split(b"\0", 1)[0].decode()


def test_one_stream_per_connection_made_on_the_first_request(daemon, tmp_path):
    files, req = frame(tmp_path, w=1280, h=720, src_size=src_bytes(1280, 720) + 4096, pitch=1280)
    with daemon.connect() as c:
        assert ask(c, req, files) == (1, "")
        assert ask(c, req, files) == (1, "")
    with daemon.connect() as c:
        assert ask(c, req, files) == (1, "")
    deadline = time.monotonic() + 5 * SCALE
    while "free 2" not in daemon.log() and time.monotonic() < deadline:
        time.sleep(0.02)
    # The size handed to the MDP is the buffer's own; the stream goes with its connection
    conv = f"{src_bytes(1280, 720) + 4096} 1280 1280 720 1280"
    assert daemon.log() == ["new 1", f"convert 1 {conv}", f"convert 1 {conv}", "free 1",
                            "new 2", f"convert 2 {conv}", "free 2"]


@pytest.mark.parametrize("kw,answer", [
    ({}, (1, "")),
    ({"w": 15}, (0, "picture 15x64 (coded width 15, pitch 15) out of range")),
    ({"h": 4100}, (0, "picture 64x4100 (coded width 64, pitch 64) out of range")),
    ({"coded": 62}, (0, "picture 64x64 (coded width 62, pitch 64) out of range")),
    # whole decoded lines go into the destination (2026-11-21)
    ({"coded": 80, "pitch": 64}, (0, "picture 64x64 (coded width 80, pitch 64) out of range")),
    ({"pitch": 8200}, (0, "picture 64x64 (coded width 64, pitch 8200) out of range")),
    ({"src": "plain-src"}, (0, "both buffers must be dma-bufs")),
    ({"dst": "plain-dst"}, (0, "both buffers must be dma-bufs")),
    # Whatever it is, a buffer the daemon cannot look at is refused
    ({"src": "gone-src"}, (0, "both buffers must be dma-bufs")),
    ({"dst_size": dst_bytes(64, 64) - 1},
     (0, f"buffers of {src_bytes(64, 64)} and {dst_bytes(64, 64) - 1} bytes are too small for 64x64")),
    ({"src_size": 100}, (0, f"buffers of 100 and {dst_bytes(64, 64)} bytes are too small for 64x64")),
])
def test_request_checks(daemon, tmp_path, kw, answer):
    files, req = frame(tmp_path, **kw)
    with daemon.connect() as c:
        assert ask(c, req, files) == answer
        # Answered: the connection stays for the next request
        good, req2 = frame(tmp_path, src="dmabuf-s2", dst="dmabuf-d2")
        assert ask(c, req2, good) == (1, "")
    assert len([line for line in daemon.log() if line.startswith("convert")]) == 1 + (answer[0] == 1)


@pytest.mark.parametrize("variant", ["magic", "short", "one-fd", "three-fds"])
def test_malformed_requests_end_the_connection(daemon, tmp_path, variant):
    files, req = frame(tmp_path)
    if variant == "magic":
        req = struct.pack("<I", 1) + req[4:]
    elif variant == "short":
        req = req[:12]
    elif variant == "one-fd":
        files = files[:1]
    else:
        files = files + [files[0]]
    with daemon.connect() as c:
        assert ask(c, req, files) == (0, "malformed request")
        assert c.recv(256) == b""
    assert not [line for line in daemon.log() if line.startswith("convert")]


def test_mdp_failures_are_answered(daemon_exe, tmp_path):
    for extra, message in (({"VDEC_MDP_NEW_FAIL": 1}, "no libdpframework here"), ({"VDEC_MDP_FAIL": 1}, "MDP said no")):
        d = Daemon(daemon_exe, tmp_path, **extra)
        try:
            files, req = frame(tmp_path)
            with d.connect() as c:
                assert ask(c, req, files) == (0, message)
                # Not given up on: asked again, tried again
                assert ask(c, req, files) == (0, message)
        finally:
            assert d.stop()[0] == 0
            d.sock.unlink()


def test_fake_mode_answers_without_the_mdp(daemon_exe, tmp_path):
    d = Daemon(daemon_exe, tmp_path, "--fake")
    try:
        files, req = frame(tmp_path)
        with d.connect() as c:
            assert ask(c, req, files) == (1, "")
            # The checks stay
            bad, req2 = frame(tmp_path, src="plain")
            assert ask(c, req2, bad) == (0, "both buffers must be dma-bufs")
    finally:
        assert d.stop()[0] == 0
    assert d.log() == []


def test_a_client_leaving_before_the_answer_does_not_kill_it(daemon_exe, tmp_path):
    d = Daemon(daemon_exe, tmp_path, VDEC_MDP_SLEEP_MS=300, G_MESSAGES_DEBUG="all")
    try:
        files, req = frame(tmp_path)
        c = d.connect()
        send(c, req, files)
        c.close()
        time.sleep(0.6)
        # No SIGPIPE: still there for the next client
        with d.connect() as c2:
            assert ask(c2, req, files) == (1, "")
    finally:
        code, err = d.stop()
    assert code == 0
    # g_debug goes to stdout
    assert "reply: Broken pipe" in err


def test_interrupted_poll_and_failed_accept_are_not_fatal(daemon_exe, tmp_path):
    d = Daemon(daemon_exe, tmp_path, VDEC_ACCEPT_FAIL=1)
    try:
        d.proc.send_signal(signal.SIGUSR1)
        time.sleep(0.2)
        files, req = frame(tmp_path)
        # The first accept fails: the connection waits and is taken on the next round
        with d.connect() as c:
            assert ask(c, req, files) == (1, "")
    finally:
        code, err = d.stop()
    assert code == 0 and "usr1" in err


def test_a_broken_poll_ends_the_daemon(daemon_exe, tmp_path):
    d = Daemon(daemon_exe, tmp_path, VDEC_POLL_FAIL=1)
    assert d.proc.wait(timeout=10 * SCALE) == 1


def test_usage_and_listen_errors(daemon_exe, tmp_path):
    r = subprocess.run([str(daemon_exe), "--bogus"], capture_output=True, text=True, env=env(tmp_path))
    assert r.returncode == 2 and r.stderr.startswith("Usage: plinius-vdec-mdpd")
    r = subprocess.run([str(daemon_exe), "--listen", str(tmp_path / "missing" / "s")], capture_output=True,
                       text=True, env=env(tmp_path))
    assert r.returncode == 1 and "cannot listen on" in r.stderr and "No such file" in r.stderr
    r = subprocess.run([str(daemon_exe), "--listen", "/" + "x" * 120], capture_output=True, text=True,
                       env=env(tmp_path))
    assert r.returncode == 1 and "cannot listen on" in r.stderr


def test_without_a_free_fd_it_cannot_listen(daemon_exe, tmp_path):
    r = subprocess.run([str(daemon_exe), "--listen", str(tmp_path / "s")], capture_output=True, text=True,
                       env=env(tmp_path, VDEC_NOFILE=3))
    assert r.returncode == 1 and "cannot listen on" in r.stderr and "Too many open files" in r.stderr


def test_an_overlong_socket_path_is_named_as_the_reason(daemon_exe, tmp_path):
    path = "/" + "x" * 120
    r = subprocess.run([str(daemon_exe), "--listen", path], capture_output=True, text=True,
                       env=env(tmp_path))
    assert r.returncode == 1
    assert f"cannot listen on {path}: File name too long" in r.stderr
    assert "Success" not in r.stderr


@pytest.mark.parametrize("listen", [{}, {"LISTEN_PID": "1", "LISTEN_FDS": "1"},
                                    {"LISTEN_PID": "self", "LISTEN_FDS": "2"}])
def test_without_socket_activation_it_refuses(daemon_exe, tmp_path, listen):
    cmd = [str(daemon_exe)]
    e = env(tmp_path, **{k: v for k, v in listen.items() if v != "self"})
    if listen.get("LISTEN_PID") == "self":
        cmd = ["sh", "-c", 'LISTEN_PID=$$ exec "$0"', str(daemon_exe)]
    r = subprocess.run(cmd, capture_output=True, text=True, env=e)
    assert r.returncode == 1
    assert "start through plinius-vdec-mdpd.socket or with --listen" in r.stderr


def test_socket_activated_it_serves_and_exits_when_idle(daemon_exe, tmp_path):
    path = tmp_path / "activated.socket"
    lsock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    lsock.bind(str(path))
    lsock.listen(4)
    # fd 3 as systemd hands it over, LISTEN_PID the daemon's own
    proc = subprocess.Popen(["sh", "-c", 'exec 3<&0 0</dev/null; LISTEN_PID=$$ exec "$0"', str(daemon_exe)],
                            stdin=lsock.fileno(), stderr=subprocess.PIPE, text=True,
                            env=env(tmp_path, LISTEN_FDS=1))
    lsock.close()
    try:
        files, req = frame(tmp_path)
        c = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        c.settimeout(5 * SCALE)
        c.connect(str(path))
        assert ask(c, req, files) == (1, "")
        # Not while a client is connected
        time.sleep(1.5)
        assert proc.poll() is None
        c.close()
        start = time.monotonic()
        assert proc.wait(timeout=10 * SCALE) == 0
        assert time.monotonic() - start >= 0.5
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate()
